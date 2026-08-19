"""Fynn Drake connector — the piece that runs on the firm's own Windows PC.

WHAT THIS REPLACED, AND WHY
---------------------------
The backend used to drop payload files into a folder and `agent.py watch` picked them up.
That worked while the backend and Drake were the same machine. With the backend in a data
centre there is no shared folder, so this dials OUT instead: it asks the server for work,
does it in Drake, and posts back what happened.

Outbound-only is the whole reason a non-technical office can install this. If the server
had to reach IN, every firm's IT would have to open a port — a security review and an
argument, on every single sale. A held HTTP request looks exactly like visiting a website.

LONG POLLING, NOT A QUEUE AND NOT A SOCKET
------------------------------------------
`GET /connector/jobs?wait=25` holds the line for up to 25 seconds and answers the instant
there is work. That is ~2 requests a minute instead of 12, delivery is immediate, and there
is no socket to keep alive through a corporate proxy that will silently drop it. A queue
(SQS/Redis/Rabbit) would not help: it lives in the cloud, and this PC would still have to
come and get the message — AWS's own recommended SQS consumer is long polling.

WHAT THIS FILE DOES NOT DO
--------------------------
It does not know anything about tax forms. Every field map, read-back gate, duplicate
guard, screen check and halt rule lives in `agent.py` and runs unchanged: this calls
`_run_one_payload`, the exact function the folder watcher calls. There is no file/e-file
command here or anywhere downstream of it.

THREE RULES THIS FILE IS RESPONSIBLE FOR
----------------------------------------
1. NEVER CLAIM WORK IT CANNOT DO. Claiming marks a job `running` on the server, and a job
   running on a machine that cannot drive Drake is a stuck job that needs a person —
   invented out of nothing. So Drake is connected FIRST, and while it is not, this reports
   liveness with `ready=0` and takes nothing.

2. A NETWORK FAILURE IS NOT A DRAKE HALT. If the report cannot be delivered, the values are
   still in Drake and the job is still `running` on the server. Losing that report is the
   one failure that silently strands a return, so reports are retried hard and spooled to
   disk if they still will not go — and spooled reports are delivered BEFORE any new work
   is claimed.

3. IT NEVER RETRIES A JOB. If a run dies halfway, this does not re-run it. Whether Drake
   received a whole W-2, half of one, or nothing is unknowable from here, and re-running a
   whole one doubles a client's wages on their return — a corruption every later check
   would confirm as correct, because Drake really does hold both copies.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"

# Where a fresh install points unless somebody says otherwise. Baked in because the person
# running setup is a tax preparer, not IT: the address field used to open EMPTY on a fresh
# machine, which meant copying a Railway URL by hand — and a typo there looks exactly like
# "Fynn is down". The field stays editable (a staging or self-hosted backend is typed over
# it), and `pair` still REQUIRES --server, because the CLI path is the explicit one.
# This URL is public by design; everything behind it is auth-gated.
DEFAULT_SERVER = "https://fynn-backend-production.up.railway.app"

# Where the token lives. Windows Credential Manager, not a file next to the .exe: the token
# can drive a keyboard inside a tax office, and "it was in a text file on the desktop" is
# not an answer anybody wants to give afterwards.
CRED_TARGET = "Fynn/DrakeConnector"

# Reports that could not be delivered. Kept next to the connector so a person can see that
# something is pending, and so a restart does not lose it.
SPOOL_DIR = Path(os.environ.get("FYNN_CONNECTOR_SPOOL", Path.home() / ".fynn-connector" / "spool"))

POLL_WAIT_SEC = 25          # server caps at 25; must stay under proxy idle timeouts
HTTP_TIMEOUT_SEC = 45       # generous margin over the server's hold
RESULT_ATTEMPTS = 6         # ~2 minutes of retries before spooling
DRAKE_RETRY_SEC = 15        # how often to look for Drake when it is not open

# Name of the logon task. One per user; `install` replaces rather than duplicates.
TASK_NAME = "FynnDrakeConnector"

# The single-instance lock, held for the life of the process (module-level so the garbage
# collector cannot quietly release the OS handle mid-run).
_RUN_MUTEX = None


def _already_running() -> bool:
    """True when another connector run loop is alive in this session.

    A windowed exe shows nothing when it starts, so the natural response to "nothing
    happened" is to double-click again — a real first run of this produced TWELVE copies,
    every one of them polling for the same firm's work. The server's claim is atomic, so
    no document is typed twice, but twelve processes taking turns at one keyboard is not a
    system anybody intended.

    A named mutex is the standard Windows answer: first `run` grabs it, every later `run`
    sees ERROR_ALREADY_EXISTS, says where the icon is, and exits. `Local\\` scopes it to
    this login session — elevated and non-elevated copies share it there, so "Run as
    administrator" cannot sneak a second keyboard past the check. The OS frees the mutex
    when the process dies, however it dies, so a crash can never wedge the next start.
    """
    global _RUN_MUTEX
    try:
        import win32event
        import win32api
        import winerror
        _RUN_MUTEX = win32event.CreateMutex(None, False, "Local\\FynnConnector-run")
        return win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except Exception:
        # No pywin32 — a dev checkout on a bare machine. Two dev copies are the
        # developer's own business; never block the real loop over the guard itself.
        return False


def is_frozen() -> bool:
    """True when running from the packaged .exe rather than a checkout."""
    return getattr(sys, "frozen", False)


def resource_path(name: str) -> str:
    """A file that ships INSIDE the exe (PyInstaller unpacks these to sys._MEIPASS).

    A copy sitting beside the exe wins, so a firm whose Drake build needs a tweaked binding
    can drop one in without waiting for us to cut a release.
    """
    beside = Path(sys.executable).parent / name if is_frozen() else Path.cwd() / name
    if beside.is_file():
        return str(beside)
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def _cred_write(data: dict) -> None:
    import win32cred
    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": CRED_TARGET,
            "UserName": str(data.get("agent_id", "fynn")),
            "CredentialBlob": json.dumps(data),
            # LOCAL_MACHINE so the connector still works when it starts at login on a
            # machine several staff share.
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


def _cred_read() -> dict | None:
    try:
        import win32cred
        cred = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        return None
    blob = cred.get("CredentialBlob")
    if isinstance(blob, bytes):
        # pywin32 hands back the raw blob; CredWrite stored a str, which Windows keeps as
        # UTF-16LE. Fall back to UTF-8 for anything written by another tool.
        for enc in ("utf-16-le", "utf-8"):
            try:
                blob = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    try:
        return json.loads(blob)
    except Exception:
        return None


def _cred_delete() -> bool:
    try:
        import win32cred
        win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ServerError(Exception):
    """Anything that came back from the server that we cannot act on."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(server: str, path: str, token: str | None = None, body: dict | None = None,
             method: str | None = None, timeout: int = HTTP_TIMEOUT_SEC) -> dict:
    url = f"{server.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "X-Fynn-Agent-Version": VERSION,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise ServerError(detail or f"server said {e.code}", status=e.code) from e
    except urllib.error.URLError as e:
        # No network, DNS failure, TLS problem, server down. NOT a Drake problem — the
        # caller retries rather than treating it as a halt.
        raise ServerError(f"could not reach {server}: {e.reason}") from e
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        # The connection died MID-RESPONSE: the read timed out, the server restarted
        # under a deploy, a proxy cut an idle line. For a long poll this is ROUTINE — a
        # held request is exactly the kind a middlebox kills — and it crashed the whole
        # connector the first day a person ran it unattended (TimeoutError escaped raw,
        # 2026-08-18). Ordering matters: URLError is itself an OSError, so its more
        # specific handler above must come first.
        raise ServerError(f"connection to {server} dropped: {type(e).__name__}: {e}") from e
    except json.JSONDecodeError as e:
        # A captive portal or interfering proxy answered with HTML. Retryable, like
        # every other "the network did something" — never a crash.
        raise ServerError(f"unreadable response from {server}") from e


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_pair(args) -> int:
    """Trade a typed pairing code for this machine's permanent token."""
    try:
        res = _request(args.server, "/api/v1/connector/pair",
                       body={"code": args.code, "version": VERSION}, timeout=30)
    except ServerError as e:
        print(f"Pairing failed: {e}", file=sys.stderr)
        return 1

    token = res.get("token")
    if not token:
        print("Pairing failed: the server did not return a token.", file=sys.stderr)
        return 1

    _cred_write({
        "server": args.server.rstrip("/"),
        "token": token,
        "agent_id": res.get("agent_id"),
        "name": res.get("name"),
    })
    print(f"Paired as {res.get('name')!r}.")
    print("The token is stored in Windows Credential Manager. It is not recoverable from "
          "the server — re-pair if this machine is rebuilt.")
    return 0


def cmd_unpair(args) -> int:
    print("Removed." if _cred_delete() else "Nothing was stored.")
    return 0


# ---------------------------------------------------------------------------
# Setup, for a person with no terminal
# ---------------------------------------------------------------------------

def _ask_gui(server_default: str):
    """A small window asking for the Fynn address and the pairing code.

    tkinter, because it is in the standard library. A tray icon or a real installer UI
    would be nicer, and both are a dependency and a build problem; what has to work on day
    one is that somebody who has never opened a terminal can finish setup.

    Returns (server, code), or None if they closed it.
    """
    import tkinter as tk
    from tkinter import ttk

    out = {}
    root = tk.Tk()
    root.title("Set up the Fynn Drake connector")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(
        frm,
        text="Paste the pairing code from Fynn.\nIt works once and expires in 15 minutes.",
        justify="left",
    ).grid(column=0, row=0, columnspan=2, sticky="w", pady=(0, 12))

    ttk.Label(frm, text="Fynn address").grid(column=0, row=1, sticky="w")
    server = ttk.Entry(frm, width=44)
    server.insert(0, server_default)
    server.grid(column=1, row=1, pady=4)

    ttk.Label(frm, text="Pairing code").grid(column=0, row=2, sticky="w")
    code = ttk.Entry(frm, width=44)
    code.grid(column=1, row=2, pady=4)
    code.focus()

    msg = ttk.Label(frm, text="", foreground="#b00")
    msg.grid(column=0, row=4, columnspan=2, sticky="w", pady=(8, 0))

    def go(*_):
        if not code.get().strip():
            msg.config(text="Enter the code shown in Fynn.")
            return
        out["server"] = server.get().strip()
        out["code"] = code.get().strip()
        root.destroy()

    ttk.Button(frm, text="Connect", command=go).grid(column=1, row=3, sticky="e", pady=(10, 0))
    root.bind("<Return>", go)
    root.mainloop()
    return (out["server"], out["code"]) if out else None


def _tell(title: str, message: str) -> None:
    """Say something to a person with no console. Falls back to stdout when there is one."""
    if not is_frozen():
        print(message)
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        print(message)


def cmd_setup(args) -> int:
    """Pair this machine and make it start at login — the whole install, for a normal person."""
    cred = _cred_read()
    # The prefill, most specific first: where this machine is ALREADY paired, then whatever
    # the command line said, then the baked-in production default. Chained with `or` rather
    # than dict defaults so an empty stored server still falls through to something a
    # person can actually use — this is a prefill, never a lock; the field stays editable.
    default_server = ((cred or {}).get("server") or getattr(args, "server", "")
                      or DEFAULT_SERVER)
    asked = _ask_gui(default_server)
    if not asked:
        return 1
    server, code = asked

    try:
        res = _request(server, "/api/v1/connector/pair",
                       body={"code": code, "version": VERSION}, timeout=30)
    except ServerError as e:
        _tell("Fynn", f"Could not pair: {e}")
        return 1
    if not res.get("token"):
        _tell("Fynn", "Could not pair: the server did not return a token.")
        return 1

    _cred_write({"server": server.rstrip("/"), "token": res["token"],
                 "agent_id": res.get("agent_id"), "name": res.get("name")})

    installed = _install_autostart()
    started = ("This PC will start the connector automatically when you log in."
               if installed else
               "Pairing worked, but automatic start-up could not be set up. Start "
               "'Fynn Connector' by hand after each restart.")
    _tell("Fynn", f"Connected as {res.get('name')!r}.\n\n{started}\n\n"
                  "Leave Drake open on its home screen when you want documents entered.")
    return 0


def _install_autostart() -> bool:
    """Run at logon, in the user's own session.

    NOT a Windows Service. Services run in session 0, which has no desktop — and this agent
    has to SEE Drake's windows in order to drive them. A service would start, find nothing
    to attach to, and fail forever in a way that looks exactly like a network problem.

    Task Scheduler rather than a Run registry key, because it can restart a task that dies,
    which matters for something meant to sit there for a whole filing season.
    """
    if is_frozen():
        cmd = f'"{sys.executable}" run'
    else:
        cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}" run'
    try:
        subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON",
             "/RL", "LIMITED", "/TR", cmd],
            check=True, capture_output=True, text=True,
        )
        return True
    except Exception:
        return False


def cmd_install(args) -> int:
    print("Start-at-login installed." if _install_autostart()
          else "Could not install the logon task.")
    return 0


def cmd_uninstall(args) -> int:
    try:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       check=True, capture_output=True, text=True)
        print("Start-at-login removed.")
    except Exception:
        print("No logon task was installed.")
    return 0


def cmd_status(args) -> int:
    cred = _cred_read()
    if not cred:
        print("Not paired. Run:  connector.py pair --server <url> --code <CODE>")
        return 1
    print(f"Paired as : {cred.get('name')!r}  (agent {cred.get('agent_id')})")
    print(f"Server    : {cred.get('server')}")
    try:
        res = _request(cred["server"], "/api/v1/connector/jobs?wait=0&ready=0",
                       token=cred["token"], timeout=20)
        print("Server    : reachable, token accepted")
        if res.get("halted"):
            print(f"HALTED    : {res['halted']['note']}")
    except ServerError as e:
        print(f"Server    : {e}")
        return 1
    return 0


def _deliver_result(server: str, token: str, job_id: str, payload: dict) -> bool:
    """Post one report. Returns False only after every retry failed.

    Retried hard on purpose. The values are already in Drake; the server has the job marked
    `running` and will hand out nothing else for this firm until it hears back. A report
    that quietly evaporates is the one failure mode that strands a live return.
    """
    delay = 2
    for attempt in range(1, RESULT_ATTEMPTS + 1):
        try:
            _request(server, f"/api/v1/connector/jobs/{job_id}/result",
                     token=token, body=payload, timeout=120)
            return True
        except ServerError as e:
            # 409 means the server already recorded this job — a previous attempt did land
            # and only the response was lost. Delivered, not failed.
            if e.status == 409:
                print("  (the server already had this report — nothing lost)")
                return True
            # 4xx other than 409 will not become true by repeating it.
            if e.status is not None and 400 <= e.status < 500 and e.status != 429:
                print(f"  report rejected: {e}", file=sys.stderr)
                return False
            print(f"  could not deliver report (attempt {attempt}/{RESULT_ATTEMPTS}): {e}",
                  file=sys.stderr)
            if attempt < RESULT_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, 30)
    return False


def _spool(job_id: str, payload: dict) -> Path:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOOL_DIR / f"{job_id}.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def _flush_spool(server: str, token: str) -> int:
    """Deliver anything stranded by an earlier network failure. Returns how many are left.

    Runs BEFORE any new work is claimed. A stranded report means the server still thinks
    that job is running, so it would not hand out new work anyway — but doing this first
    makes the recovery obvious instead of accidental.
    """
    if not SPOOL_DIR.is_dir():
        return 0
    left = 0
    for path in sorted(SPOOL_DIR.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print(f"  spooled report unreadable, leaving it in place: {path}", file=sys.stderr)
            left += 1
            continue
        job_id = path.stem
        print(f"delivering a report held from an earlier run: {job_id}")
        if _deliver_result(server, token, job_id, body):
            path.unlink(missing_ok=True)
        else:
            left += 1
    return left


def cmd_run(args) -> int:
    """The loop: hold the line, take one job, do it in Drake, report, repeat."""
    import tray as tray_mod

    # Before ANYTHING — even the log tee. One keyboard, one connector.
    if _already_running():
        _tell("Fynn connector",
              "The connector is already running.\n\n"
              "Look for the round icon near the clock, bottom-right — it may be "
              "behind the ^ arrow.\n\n"
              "Grey: waiting for Drake to be opened.  Green: ready and waiting for "
              "documents.  Blue: entering one now.")
        return 0

    # BEFORE the first print, including the banner — the banner names the version that
    # wrote every line under it, and a log that starts halfway through is a log that
    # answers "what did it type?" with "some of it".
    log_path = tray_mod.install_log_tee()

    cred = _cred_read()
    if not cred:
        print("Not paired. Run:  connector.py pair --server <url> --code <CODE>", file=sys.stderr)
        return 1
    server, token = cred["server"], cred["token"]

    # Imported here, not at module scope, so `pair` / `status` / `unpair` work on a machine
    # where Drake is not installed and pywinauto cannot bind anything.
    import agent as agent_mod
    from drake_driver import DrakeDriver

    # Frozen: binding.json ships inside the exe (a copy beside the exe still wins).
    binding = agent_mod.load_binding(resource_path(args.binding) if is_frozen() else args.binding)
    nav_token = (binding.get("navigation", {}) or {}).get("headsdown_checkbox_true", "X")

    # Quit from the tray sets this; the loop reads it at the TOP of an iteration only.
    # Stopping the instant somebody clicks would mean stopping mid-document — Drake left
    # holding half a W-2 and the server still holding the job as `running`, which is the
    # exact stranded state rule 3 exists to prevent. So a quit waits for a safe point.
    stop = threading.Event()

    tray = None
    if not getattr(args, "no_tray", False):
        tray = tray_mod.Tray(machine_name=str(cred.get("name") or ""), server=server,
                             on_quit=stop.set)
        if tray.start():
            # Registered rather than placed in a `finally`: cmd_run returns from six
            # different points, and Windows leaves a ghost icon behind until something
            # hovers over it if the process dies without removing it.
            atexit.register(tray.stop)
        else:
            tray = None
            # Windowed build with no console AND no tray means an invisible process. Say
            # so once, in the only channel left, rather than running unseen.
            if sys.stdout is None or not is_frozen():
                print("(no tray icon on this machine — pystray/Pillow unavailable)")
            else:
                # IN A THREAD. `_tell` is a modal message box and blocks until somebody
                # clicks OK. The connector starts at LOGON, so a machine that cannot show
                # a tray icon would sit behind a dialog nobody is looking at and enter
                # nothing all day, while the portal showed the office as simply offline.
                threading.Thread(
                    target=_tell,
                    args=("Fynn connector",
                          f"Running in the background.\n\nNo tray icon could be shown on "
                          f"this machine, so the log is the only view:\n{log_path}"),
                    daemon=True,
                ).start()

    def _state(name: str, detail: str = "") -> None:
        if tray is not None:
            tray.set_state(name, detail)

    def _notify(title: str, message: str) -> None:
        # A toast when a document finishes, so the operator does not have to walk back to
        # the portal and guess. OUTPUT ONLY, exactly like _state: it reports what already
        # happened and can influence nothing — not the job, not the halt, not this loop.
        # Tray.notify already promises never to raise, and the promise is not trusted
        # here anyway: a broken toast must never cost the loop an iteration, because the
        # loop is the thing the toast exists to report on.
        if tray is None:
            return
        try:
            tray.notify(title, message)
        except Exception:
            pass

    _state("starting")

    print(f"Fynn Drake connector {VERSION}")
    print(f"Server : {server}")
    print(f"Machine: {cred.get('name')!r}")
    print(f"Log    : {log_path}")
    print("Leave Drake on its home screen. The connector opens the client, the screen and "
          "the record itself.\nCtrl+C to stop.\n")

    driver = None
    while True:
        try:
            if stop.is_set():
                print("\nstopping (asked to quit from the tray).")
                return 0
            # RULE 1 — Drake first, then work. Claiming a job we cannot do would mark it
            # running on the server and manufacture a stuck job needing a human.
            if driver is None:
                try:
                    # --slow was inert until now, for the same reason --dry-run was.
                    d = DrakeDriver(binding, key_pause=0.12 if args.slow else 0.03)
                    d.connect()
                    if d.w32 is None:
                        raise RuntimeError("no win32 popup connection — heads-down entry needs it")
                    wi = d.window_info()
                    print(f"Drake connected: {wi.get('title')!r}")
                    driver = d
                    _state("idle")
                except Exception as e:
                    # Liveness only. The portal then says the honest thing — the PC is on,
                    # Drake is not open — rather than showing the office as offline.
                    print(f"waiting for Drake ({type(e).__name__}: {e})")
                    _state("no-drake", "Open Drake and leave it on its home screen.")
                    try:
                        _request(server, "/api/v1/connector/jobs?wait=0&ready=0",
                                 token=token, timeout=20)
                    except ServerError:
                        pass
                    time.sleep(DRAKE_RETRY_SEC)
                    continue

            # RULE 2 — anything stranded by a network failure goes first.
            if _flush_spool(server, token) > 0:
                time.sleep(5)
                continue

            try:
                res = _request(server, f"/api/v1/connector/jobs?wait={POLL_WAIT_SEC}",
                               token=token, timeout=HTTP_TIMEOUT_SEC)
            except ServerError as e:
                if e.status == 401:
                    print("This machine is no longer authorised (revoked, or the token was "
                          "replaced). Re-pair from the portal.", file=sys.stderr)
                    _state("unpaired", "Re-pair this PC from the Fynn portal.")
                    # SAY IT ON THE SCREEN, not just in the log. This is the one failure that
                    # ends the process, and in a windowed build stderr goes to a file nobody
                    # is looking at — so a revoked machine looked exactly like a broken exe:
                    # double-click, Drake flickers as the driver attaches, then nothing, with
                    # the office assuming the software is dead rather than un-paired.
                    # Modal is right HERE (unlike the missing-tray notice, which had to be
                    # threaded so it could not block entry): we are exiting either way, and a
                    # dialog holds the message until somebody actually reads it.
                    _tell("Fynn connector",
                          "This PC is no longer connected to Fynn.\n\n"
                          "Someone removed it in the portal, or it was paired again "
                          "somewhere else.\n\n"
                          "In Fynn: Settings → Connect a PC → copy the code.\n"
                          "Click OK and paste it into the window that opens.")
                    # ...then OPEN that window. Telling somebody to re-pair while leaving them
                    # no way to do it is the same dead end twice: double-clicking the exe again
                    # just repeats this failure, because a stored-but-rejected token still
                    # routes to `run`. Only when frozen — from a terminal the operator has the
                    # `setup` subcommand and does not need a window opened for them.
                    if is_frozen():
                        return cmd_setup(argparse.Namespace(server=server))
                    return 1
                print(f"waiting to reach the server: {e}", file=sys.stderr)
                _state("offline", str(e)[:80])
                time.sleep(10)
                continue

            if res.get("halted"):
                print(f"HELD: {res['halted']['note']}")
                _state("halted", str(res["halted"].get("note", ""))[:80])
                time.sleep(20)
                continue

            job = res.get("job")
            if not job:
                _state("idle")
                continue  # normal — nothing to do, ask again

            job_id = job["job_id"]
            _state("working", f"{job.get('doc_type')} — document {job.get('seq')} in this batch")
            print("=" * 74)
            print(f"ENTERING {job.get('doc_type')}  (document {job.get('doc_id')}, "
                  f"number {job.get('seq')} in this batch)")
            print("=" * 74)

            payload = job.get("payload") or {}
            try:
                report = agent_mod._run_one_payload(driver, payload, args, nav_token)
            except Exception as e:
                # An unexpected crash mid-entry. Reported as a halt, never retried: how much
                # of this document reached Drake is exactly what nobody knows.
                report = {
                    "ok": False,
                    "entered": 0,
                    "reason": f"the connector crashed while entering this document: "
                              f"{type(e).__name__}: {e}",
                    "crashed": True,
                }
                print(f"\n{report['reason']}", file=sys.stderr)

            shot_b64 = None
            try:
                shot_path = Path(SPOOL_DIR).parent / "last-screen.png"
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                shot = driver.save_screenshot(str(shot_path))
                if shot.get("ok"):
                    shot_b64 = base64.b64encode(Path(shot["path"]).read_bytes()).decode("ascii")
            except Exception as e:
                # The picture is evidence, not the record. Losing it must never cost us the
                # report of what a robot typed into somebody's return.
                print(f"  (screenshot failed: {type(e).__name__}: {e})", file=sys.stderr)

            body = {"ok": bool(report.get("ok")), "report": report}
            if shot_b64:
                body["screenshot_base64"] = shot_b64

            if not _deliver_result(server, token, job_id, body):
                path = _spool(job_id, body)
                print(f"  report held on disk and will be retried: {path}", file=sys.stderr)

            print("Values are IN Drake but NOT filed: a human still reviews and executes.")
            if args.once:
                print("\n--once: that job is reported. Stopping.")
                return 0 if report.get("ok") else 2
            if not report.get("ok"):
                print("\nThat document did not complete cleanly. Nothing further will be "
                      "handed to this machine until somebody reviews it in the portal.",
                      file=sys.stderr)
                _state("halted", str(report.get("reason", ""))[:80])
                # After the report is delivered (or spooled), never before: the toast may
                # only repeat what the report already said. It tells the operator the firm
                # is blocked — that is a fact about the server's halt guard, not a request.
                _notify("Fynn — run stopped",
                        f"The {job.get('doc_type')} run stopped and needs your review in "
                        f"the Fynn portal. Nothing else will be entered until someone "
                        f"reviews it.")
            else:
                _state("idle")
                # "entered" is the report's own count — the toast claims nothing the report
                # did not say, and it never says "filed", because nothing here files.
                _notify("Fynn — document entered",
                        f"{job.get('doc_type')} entered ({report.get('entered')} fields). "
                        f"Review it in the Fynn portal — nothing is filed.")

        except KeyboardInterrupt:
            print("\nstopped.")
            return 0
        except Exception as e:  # noqa: BLE001 — the last line of defence, deliberately broad
            # THE SERVICE MUST OUTLIVE THE SURPRISE. This program sits unattended through
            # a filing season; any exception that reaches here would otherwise kill it
            # silently (the window is invisible) and the office would find out days later
            # as "nothing has gone into Drake since Tuesday".
            #
            # Continuing is safe against the one thing that must never happen — retyping a
            # document — because the entry section has its own handler that converts a
            # crash into a reported halt, and the server never re-hands-out a job that is
            # `running` or an unreviewed halt. Everything else in the loop (polling,
            # spool delivery, Drake connection) is idempotent.
            print(f"unexpected error — continuing in 10s ({type(e).__name__}: {e})",
                  file=sys.stderr)
            _state("offline", f"{type(e).__name__}: {e}"[:80])
            # The driver may be mid-something unknowable; drop it and reconnect fresh.
            driver = None
            time.sleep(10)


def build_parser() -> argparse.ArgumentParser:
    """The CLI, exposed so the simulator can check the real thing.

    A test that rebuilds a copy of this parser proves nothing about the parser the connector
    actually runs — delete the `parents=[...]` below and a copy-based test stays green while
    the connector goes back to crashing on its first job in front of a live Drake."""
    p = argparse.ArgumentParser(
        prog="connector.py",
        description="Fynn Drake connector — takes work from Fynn and enters it into Drake.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pair", help="redeem a pairing code from the portal")
    sp.add_argument("--server", required=True, help="e.g. https://api.fynn.example")
    sp.add_argument("--code", required=True, help="the code shown in the portal")
    sp.set_defaults(func=cmd_pair)

    su = sub.add_parser("unpair", help="forget the stored token")
    su.set_defaults(func=cmd_unpair)

    ss = sub.add_parser("status", help="is this machine paired, and can it reach Fynn?")
    ss.set_defaults(func=cmd_status)

    # INHERITED, not re-declared. The entry path is shared with the folder watcher, so the
    # options it reads are shared too — declaring them here by hand is what put a connector
    # in front of a live Drake missing `--nav-timeout`, which crashed it on its first job.
    import agent as _agent_opts
    sr = sub.add_parser("run", parents=[_agent_opts.entry_options_parser()],
                        help="take work and enter it into Drake")
    sr.add_argument("--binding", default="binding.json")
    sr.add_argument("--slow", action="store_true", help="slower keystrokes so you can watch")
    sr.add_argument("--no-tray", action="store_true",
                    help="skip the tray icon (the log file is written either way)")
    # NO --dry-run HERE, deliberately. It existed and did nothing: no function in the shared
    # entry path reads it, and the driver is built without it — so `run --dry-run` would have
    # typed into a live tax return while telling the operator it would not. A safety flag
    # that is only sometimes true is worse than no flag, because it is the one somebody
    # reaches for precisely when they are unsure. Plan offline with
    # `agent.py write-form --dry-run`, which does honour it.
    sr.add_argument("--once", action="store_true",
                    help="do the job that is waiting, report it, then exit — what to use for a test")
    sr.set_defaults(func=cmd_run)

    # The whole install for somebody with no terminal: one window, then start-at-login.
    ssu = sub.add_parser("setup", help="pair this PC and start automatically at login")
    # Default "" on purpose: the prefill chain in cmd_setup resolves a blank to the stored
    # credential and then to DEFAULT_SERVER. Baking the constant in HERE would let the
    # parser's value shadow a stored credential, and a paired machine's setup window must
    # show where it actually points.
    ssu.add_argument("--server", default="",
                     help="prefill a different Fynn address (default: the production server)")
    ssu.set_defaults(func=cmd_setup)

    si = sub.add_parser("install", help="just the start-at-login part")
    si.set_defaults(func=cmd_install)

    sun = sub.add_parser("uninstall", help="stop starting at login")
    sun.set_defaults(func=cmd_uninstall)

    return p


def main(argv=None) -> int:
    # Before any output. The packaged exe is windowed, so `status` and `pair` typed into a
    # terminal would otherwise print nowhere at all.
    try:
        import tray as _tray
        _tray.attach_parent_console()
    except Exception:
        pass

    argv = list(sys.argv[1:] if argv is None else argv)

    # DOUBLE-CLICKED. argparse would exit(2) with a usage message onto a stderr nobody can
    # see, so the exe would look like it did nothing at all — the single most likely thing
    # for a firm to do with a file we emailed them.
    if not argv and is_frozen():
        argv = ["run"] if _cred_read() else ["setup"]

    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
