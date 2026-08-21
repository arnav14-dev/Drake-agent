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
SETUP_ATTEMPTS = 3          # pairing codes are typed by hand and expire; one shot is cruel

# How many "waiting for Drake" cycles between saying it out loud again. Counted in cycles
# rather than clock time so nothing in the loop needs a clock: ~30 minutes at 15s a cycle.
# It repeats because Drake can stay shut all morning, and it repeats SLOWLY because the
# toast is the only channel that puts words on a screen, and a channel people learn to
# dismiss is a channel we no longer have.
NO_DRAKE_SAY_EVERY = max(1, (30 * 60) // DRAKE_RETRY_SEC)

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


def _log_location() -> str:
    """The log file as it can honestly be described to a person right now.

    Never a bare path: if nothing could be opened, the sentence says so, because sending
    somebody to a file that does not exist is how a support call starts from zero.
    """
    try:
        import tray as _tray
        if getattr(_tray, "LOG_TEE_OK", False):
            return str(_tray.ACTIVE_LOG_PATH)
        # Not teed in THIS process — a terminal command, or the single-instance guard,
        # which deliberately runs before the tee. Name a file that is actually there.
        for cand in _tray._log_path_candidates():
            if cand.exists():
                return str(cand)
        return f"{_tray.LOG_PATH}  (nothing has been written there on this PC)"
    except Exception:
        return str(Path.home() / ".fynn-connector" / "connector.log")


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
            # LOCAL_MACHINE so the token survives a logoff and is there when the logon
            # task starts the connector. It does NOT mean "shared between the staff who
            # use this PC" — Credential Manager is per Windows account, so pairing is per
            # account too, and a second person signing in has no credential of their own.
            # (SESSION would be worse: it loses the token at logoff.)
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

def _raise_above_drake(root) -> None:
    """Put a tkinter window in front of Drake, and give it the keyboard.

    WITHOUT THIS, EVERY WINDOW THIS PROGRAM SHOWS IS INVISIBLE IN PRACTICE.
    Drake runs maximised and takes the foreground the moment the driver attaches to it, so
    a plain `Tk()` opens BEHIND it. The window is created, visible and waiting — and the
    person sees Drake flicker and nothing else, which reads as "the exe does not work".
    That cost a real setup session; it is not a cosmetic tweak.

    `-topmost` is dropped again after a moment: it is needed to WIN the foreground from a
    maximised Drake, and keeping it would pin a pairing dialog over every other window on
    the machine for as long as it is open. `focus_force` so the code can be pasted without
    clicking first, because the next thing the person does is paste.
    """
    try:
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        # After the window has won the front, stop insisting on it.
        root.after(600, lambda: root.attributes("-topmost", False))
    except Exception:
        # A window that could not be raised is still a window; never lose it over this.
        pass


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
    _raise_above_drake(root)

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(
        frm,
        # NO NUMBER HERE. It used to promise "expires in 15 minutes", which duplicated a
        # constant in the backend and, worse, was read as fifteen minutes from NOW — the
        # clock actually started when the portal showed the code, possibly twelve minutes
        # earlier. The reassurance was at its most confident exactly when it was wrong.
        text="Paste the pairing code from Fynn.\n"
             "It works once, and it stops working a few minutes after Fynn showed it to "
             "you.\nIf it is rejected, get a fresh one in the portal.",
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


def _tell(title: str, message: str, raise_it: bool = True) -> None:
    """Say something to a person with no console. Falls back to stdout when there is one.

    `raise_it=False` for the one caller that runs on a background thread while the driver
    may be attached to Drake: setting `-topmost` and stealing focus from a second Tk
    interpreter can put part of a Fynn box into an evidence screenshot or a read-back OCR
    crop, and that crop is a safety gate. A notice that cannot block the run loop must not
    be able to disturb what it reads either.
    """
    if not is_frozen():
        print(message)
        return
    # To the log as well as to the screen. In a windowed build these dialogs are the only
    # thing the person sees, and until now not one word of them survived the click — so
    # "what was this machine told?" had no answer on the machine afterwards.
    print(f"[{title}] {message}")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        # Same reason as _ask_gui: Drake is maximised and holds the foreground, so a box
        # shown without this sits behind it, unread, while the process waits on a click
        # nobody knows to make. `parent=root` is what makes the dialog inherit the raise —
        # a parentless messagebox builds its own toplevel and ignores these attributes.
        if raise_it:
            _raise_above_drake(root)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
    except Exception as e:
        # THE LAST RESORT MUST NOT BE A DEAD CHANNEL. This used to be `print(message)`,
        # and in the windowed build this branch exists for, sys.stdout is None: print
        # writes nowhere and raises nothing, so a broken tkinter turned every message
        # this program can produce into silence. MessageBoxW is drawn by Windows itself
        # and needs neither tkinter nor a console.
        print(f"(the message box failed: {type(e).__name__}: {e})", file=sys.stderr)
        try:
            import tray as _tray
            _tray.message_box(title, message)
        except Exception:
            pass


def cmd_setup(args) -> int:
    """Pair this machine and make it start at login — the whole install, for a normal person.

    IT ENDS BY RUNNING. This used to pair, say "this PC will start the connector
    automatically when you log in", and exit — true, and read by every single new customer
    as "you are done". Nothing was running: no tray icon, no loop, nothing entered, until
    the next sign-in, which on a desk that stays signed in can be days. The one moment the
    person is standing at the machine and paying attention was the one moment the
    connector was guaranteed not to be running.

    ENTERED TWO WAYS, AND THEY MUST END DIFFERENTLY:
      * double-clicked on an unpaired machine (via `main`) — nothing else is running here,
        so this enters the run loop itself once pairing is done;
      * from `_unpaired`, INSIDE a live run loop that is waiting for an answer. That loop
        already holds the single-instance mutex, so starting another one here would find
        the mutex taken, announce "the connector is already running", and exit — leaving
        nothing running at all, silently, which is the exact bug this function is fixing.
        That caller passes `resume_in_place` and resumes its own loop with the new token.
    """
    cred = _cred_read()
    # The prefill, most specific first: where this machine is ALREADY paired, then whatever
    # the command line said, then the baked-in production default. Chained with `or` rather
    # than dict defaults so an empty stored server still falls through to something a
    # person can actually use — this is a prefill, never a lock; the field stays editable.
    default_server = ((cred or {}).get("server") or getattr(args, "server", "")
                      or DEFAULT_SERVER)
    resume_in_place = bool(getattr(args, "resume_in_place", False))

    server, res = "", None
    for attempt in range(1, SETUP_ATTEMPTS + 1):
        try:
            asked = _ask_gui(default_server)
        except Exception as e:
            # tkinter itself failed. Everything below is unreachable, so say so in the one
            # channel that does not need tkinter rather than dying behind a blank screen.
            print(f"the setup window could not be opened: {type(e).__name__}: {e}",
                  file=sys.stderr)
            _tell("Fynn", "The pairing window could not be opened on this PC.\n\n"
                          f"{type(e).__name__}: {e}\n\n"
                          "Nothing was changed. Please send Fynn support the log at\n"
                          f"{_log_location()}")
            return 1
        if not asked:
            # CLOSING THE WINDOW IS A NORMAL THING TO DO — to go and find the code, to
            # take a phone call. It used to exit with no message at all, which from the
            # outside is identical to "the download is broken", and the exe had already
            # taken twenty seconds to appear.
            if is_frozen():
                # Re-read the credential rather than assuming. Two setup windows can be
                # open at once (double-clicking twice is exactly what people do when
                # nothing seems to happen), and the OTHER one may have paired this PC
                # while this window sat waiting. Telling a firm "this PC is not connected"
                # about a PC that just connected would send them to unplug a working
                # install — a confident sentence is only worth saying if it was checked.
                still_unpaired = not (_cred_read() or {}).get("token")
                _tell("Fynn", "Setup was cancelled — nothing was changed.\n\n"
                      + ("This PC is not connected to Fynn, so no documents will be "
                         "entered on it.\n\nDouble-click FynnConnector.exe again when you "
                         "have your pairing code."
                         if still_unpaired else
                         "This PC is already connected to Fynn — another setup window "
                         "finished the job. You can close this."))
            return 1
        server, code = asked

        try:
            res = _request(server, "/api/v1/connector/pair",
                           body={"code": code, "version": VERSION}, timeout=30)
            if not res.get("token"):
                raise ServerError("the server did not return a token")
        except ServerError as e:
            res = None
            # A TYPO AND AN EXPIRED CODE ARE NORMAL OUTCOMES, not exceptional ones: the
            # code is typed by hand and it expires. This used to end the whole application
            # on the first mistake, with no window left to correct it in — and on the
            # re-pair path (see _unpaired) that ended a connector that had been working
            # all season. So the window comes back, up to a few times.
            last = ("Could not reach Fynn to check that code:\n" + str(e)
                    if e.status is None else f"Could not pair: {e}")
            if attempt < SETUP_ATTEMPTS:
                nudge = ("Check the Fynn address and your internet, then try again."
                         if e.status is None else
                         "In Fynn: Settings → Connect a PC → copy a fresh code.")
                _tell("Fynn", f"{last}\n\n{nudge}\n\nThe window will open again so you "
                              "can retype it.")
                continue
            _tell("Fynn", f"{last}\n\nNothing was changed and this PC is not connected. "
                          "Double-click FynnConnector.exe again to try once more.")
            return 1
        break

    try:
        _cred_write({"server": server.rstrip("/"), "token": res["token"],
                     "agent_id": res.get("agent_id"), "name": res.get("name")})
    except Exception as e:
        # The code has already been spent server-side at this point, so "try again" needs
        # a FRESH one. Saying nothing here left a machine that had paired and could not
        # remember it, looping through this window forever.
        print(f"could not store the connection: {type(e).__name__}: {e}", file=sys.stderr)
        _tell("Fynn", "This PC paired with Fynn but could not store the connection "
                      "(Windows Credential Manager refused it).\n\n"
                      f"{type(e).__name__}: {e}\n\n"
                      "That pairing code is now used up — get a fresh one in the portal "
                      f"before trying again. The log is at\n{_log_location()}")
        return 1

    installed = _install_autostart()
    where = Path(sys.executable if is_frozen() else os.path.abspath(__file__))
    if installed:
        started = ("This PC will also start Fynn automatically when you log in.\n"
                   f"Keep {where.name} where it is, in {where.parent} — automatic start-up "
                   "points at that exact file, so moving or deleting it stops Fynn from "
                   "starting by itself.")
    else:
        # NAME THE REAL THING. This used to say to start "Fynn Connector" by hand, and
        # there is no such shortcut, no Start Menu entry and no installer — only the file
        # they downloaded, under a different name, in a folder nobody mentioned.
        started = ("Pairing worked, but automatic start-up could not be set up on this PC.\n"
                   f"Start it by hand after each restart by double-clicking {where.name} "
                   f"in {where.parent}.")

    if resume_in_place:
        now = "Fynn is carrying on from where it stopped — there is nothing else to do."
    elif is_frozen():
        now = ("Click OK and Fynn starts watching for documents straight away.\n"
               "Look for the round icon near the clock, bottom-right — it may be behind "
               "the ^ arrow.\n"
               f"If no icon appears, this file is the only view:\n{_log_location()}")
    else:
        # A developer typing `setup` gets the truth, not an auto-started loop they did not
        # ask for: the checkout path stays explicit, exactly like `pair`.
        now = "It is NOT running yet — start it with:  connector.py run"

    _tell("Fynn", f"Connected as {res.get('name')!r}.\n\n{now}\n\n{started}\n\n"
                  "Leave Drake open on its home screen when you want documents entered. "
                  "Fynn enters documents; it never files anything.")

    if not resume_in_place and is_frozen():
        return cmd_run(build_parser().parse_args(["run"]))
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
        print(f"start-at-login registered: {cmd}")
        return True
    except subprocess.CalledProcessError as e:
        # The reason used to be CAPTURED AND THROWN AWAY. Access denied, a group policy, a
        # rejected /TR — all identical from the outside, and the customer is told only
        # "could not be set up". Keep the words; they are the whole support call.
        print(f"schtasks refused to create the logon task (exit {e.returncode}):\n"
              f"{(e.stdout or '').strip()}\n{(e.stderr or '').strip()}", file=sys.stderr)
        return False
    except Exception as e:
        # schtasks.exe missing entirely, or something stranger. This function must never
        # be able to abort a pairing that already succeeded.
        print(f"could not create the logon task: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _autostart_points_here() -> bool | None:
    """Does the logon task still point at THIS exe? None when there is no task to ask about.

    The task stores the full path of the exe as it was at pairing time, which for almost
    every customer is their Downloads folder. Empty Downloads, tidy it onto the Desktop,
    let IT clear it — and from that logon on the task fires, fails with 0x2, and there is
    no icon, no dialog and no log line, because the process never starts. The portal shows
    the office offline with nothing on the PC to explain it. It is the only failure on
    this list that never recovers by itself.
    """
    try:
        res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                             capture_output=True)
        if res.returncode != 0:
            return None  # no such task — a machine that never installed one
        raw = res.stdout or b""
        # schtasks writes UTF-16 when its output is redirected, UTF-8 otherwise. Decoding
        # it wrong would look exactly like "the path does not match" and re-register the
        # task on every single start-up.
        text = (raw.decode("utf-16", errors="ignore")
                if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", errors="ignore"))
        return str(sys.executable).lower() in text.lower()
    except Exception:
        return None


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


# What happened to one report. THREE outcomes, not two: "it did not go" used to hide the
# difference between "the network is having a bad minute" (keep it, retry forever, it is
# still a good report) and "Fynn will never accept this" (retrying it every five seconds
# for the rest of the season, ahead of all new work, is what wedged the whole connector).
DELIVERED = "delivered"
UNDELIVERED = "undelivered"   # try again later: no network, 5xx, 429
REJECTED = "rejected"         # a permanent refusal; repeating it cannot change the answer

# Reports this process has already given up on, by job id. Belt and braces for the case
# where the file could not even be moved aside: without it, an unmovable rejected report
# would be retried on every pass forever — the wedge, one layer down.
_GAVE_UP_ON: set[str] = set()


def _deliver_result(server: str, token: str, job_id: str, payload: dict) -> str:
    """Post one report. Returns DELIVERED, UNDELIVERED (try later) or REJECTED (never).

    Retried hard on purpose. The values are already in Drake; the server has the job marked
    `running` and will hand out nothing else for this firm until it hears back. A report
    that quietly evaporates is the one failure mode that strands a live return.

    401/403 is none of the three and is RAISED instead. It is not a fact about this report:
    it means this machine has been removed in the portal. Classifying it as a rejection
    used to leave a revoked PC retrying a dead token every five seconds, forever, behind a
    green "connected, waiting for work" icon — the most invisible failure this program has
    ever had, because the icon was actively reporting health. The caller says so and offers
    to re-pair.
    """
    delay = 2
    for attempt in range(1, RESULT_ATTEMPTS + 1):
        try:
            _request(server, f"/api/v1/connector/jobs/{job_id}/result",
                     token=token, body=payload, timeout=120)
            return DELIVERED
        except ServerError as e:
            # 409 means the server already recorded this job — a previous attempt did land
            # and only the response was lost. Delivered, not failed.
            if e.status == 409:
                print("  (the server already had this report — nothing lost)")
                return DELIVERED
            # 401 ONLY, and deliberately not 403. The server's connector auth hook answers
            # exactly one code when a machine is not recognised — 401 — so 401 is the only
            # honest signal for "this PC was revoked", and it escapes to the caller, which
            # says so and offers to re-pair.
            #
            # 403 must NOT mean that. After an in-place re-pair this machine holds a new
            # agent id while the spool may still hold reports minted under the OLD one; a
            # server that answers 403 (or 404) for a job this agent does not own would then
            # be read as "revoked again", and the customer would get a re-pair dialog every
            # few seconds for a machine that is perfectly paired. Those fall through to the
            # permanent-refusal branch below, which sets the report aside and moves on.
            if e.status == 401:
                raise
            # 4xx other than 409 will not become true by repeating it.
            if e.status is not None and 400 <= e.status < 500 and e.status != 429:
                print(f"  Fynn refused this report and repeating it cannot change that: {e}",
                      file=sys.stderr)
                return REJECTED
            print(f"  could not deliver report (attempt {attempt}/{RESULT_ATTEMPTS}): {e}",
                  file=sys.stderr)
            if attempt < RESULT_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, 30)
    return UNDELIVERED


def _spool(job_id: str, payload: dict) -> Path:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOOL_DIR / f"{job_id}.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def _set_aside(path: Path, why: str) -> str:
    """Move a report that can never be delivered out of the way — WITHOUT losing it.

    RULE 2 SAYS A REPORT IS THE RECORD OF WHAT A ROBOT TYPED INTO SOMEBODY'S RETURN, so
    this never deletes one. But a report that Fynn will never accept must also stop being
    a gate: `_flush_spool` runs before every poll, so one permanently-refused file (404 on
    a cancelled job, 413 on an oversized screenshot, a 400, an unreadable file) held the
    loop at that gate every five seconds forever while the icon stayed green and the firm's
    documents were never entered. Aside, and out loud, beats in the way and silent.

    Returns the sentence to show a person. Even when the move itself fails, the job id is
    remembered so this process stops retrying it, and the payload goes to the log so the
    record survives in the one place that is not this folder.
    """
    _GAVE_UP_ON.add(path.stem.replace(".json", ""))
    dest = SPOOL_DIR / "rejected" / path.name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # NEVER overwrite one refused report with another. This function's whole promise is
        # that it does not lose the record of what was typed into somebody's return, and
        # `dest.unlink()` here would have broken that promise inside the function that
        # makes it — a same-named file (a support-directed restore, an operator copying one
        # back to retry it, a sync tool re-creating it) would silently destroy the older
        # report. Suffix instead; a folder of numbered files is a filing problem, and
        # deleting evidence is not.
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{path.stem}.{n}{path.suffix}")
            n += 1
        path.replace(dest)
        print(f"  {why}\n  the report is kept here instead: {dest}", file=sys.stderr)
        return f"a report could not be sent to Fynn — it is held at {dest}"
    except Exception as e:
        print(f"  {why}\n  and it could not even be moved aside "
              f"({type(e).__name__}: {e}); it stays at {path}", file=sys.stderr)
        try:
            # The log becomes the record of last resort. Nothing else has this content.
            print(f"  the report that could not be sent: {path.read_text(encoding='utf-8')}",
                  file=sys.stderr)
        except Exception:
            pass
        return f"a report could not be sent to Fynn — it is still at {path}"


def _flush_spool(server: str, token: str) -> tuple[int, list[str]]:
    """Deliver anything stranded by an earlier network failure.

    Returns (how many are still worth retrying, sentences about the ones given up on).
    Anything permanently refused is moved aside by `_set_aside` rather than counted, so it
    can never block the poll — see that function for why that mattered so much.

    Runs BEFORE any new work is claimed. A stranded report means the server still thinks
    that job is running, so it would not hand out new work anyway — but doing this first
    makes the recovery obvious instead of accidental.
    """
    if not SPOOL_DIR.is_dir():
        return 0, []
    left, given_up = 0, []
    for path in sorted(SPOOL_DIR.glob("*.json")):
        job_id = path.stem
        if job_id in _GAVE_UP_ON:
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            given_up.append(_set_aside(
                path, f"a held report could not be read ({type(e).__name__}: {e})"))
            continue
        print(f"delivering a report held from an earlier run: {job_id}")
        outcome = _deliver_result(server, token, job_id, body)
        if outcome == DELIVERED:
            try:
                path.unlink(missing_ok=True)
            except Exception as e:
                # DELIVERED, but the file would not go away — antivirus or an indexer
                # holding it open is enough. Left alone, this is the one loop in the program
                # that RE-SENDS a report already accepted, every ten seconds, for ever. The
                # server answers 409 and treats the repeat as a duplicate, so nothing is
                # double-recorded, but the machine would sit doing that instead of working.
                # Remembering the id stops the resend; the file is stale, not evidence — the
                # server has the report.
                _GAVE_UP_ON.add(job_id)
                print(f"  (delivered, but the held copy could not be removed: "
                      f"{type(e).__name__}: {e} — it will not be sent again)",
                      file=sys.stderr)
        elif outcome == REJECTED:
            given_up.append(_set_aside(path, "Fynn refused this held report"))
        else:
            left += 1
    # A torn write leaves <job>.json.part, which glob("*.json") can never match: the
    # payload sits there and no code path would ever deliver it or even mention it.
    for part in sorted(SPOOL_DIR.glob("*.json.part")):
        if part.stem.replace(".json", "") in _GAVE_UP_ON:
            continue   # already dealt with; do not report the same orphan every 5 seconds
        given_up.append(_set_aside(part, "a held report was only half-written to disk"))
    return left, given_up


def _drake_alive(d) -> bool:
    """Is the Drake we connected to still there, right now?

    RULE 1 WAS ONLY ENFORCED ONCE, AT STARTUP. After that the connection was never
    revalidated, so closing Drake at 5pm (or an IT restart, or a crash) left the connector
    claiming jobs it could not possibly do — and a claimed job is `running` on the server:
    a stuck job needing a human, invented out of nothing, whose halt reason names the
    document instead of the real cause. Worse, the driver caches Drake's pid, so reopening
    Drake did not fix it: every cleared halt bought one more halt on the next document
    until somebody restarted the connector.

    `window_info()` is cheap and never raises — it answers with an empty title when the
    window is gone. Falsy rather than `is None`, because a stale handle can also come back
    with an empty string, and w32 too, since the popup connection can die on its own and
    heads-down entry cannot work without it.
    """
    try:
        if getattr(d, "w32", None) is None:
            return False
        return bool((d.window_info() or {}).get("title"))
    except Exception:
        return False


def cmd_run(args) -> int:
    """The loop: hold the line, take one job, do it in Drake, report, repeat."""
    import tray as tray_mod

    # Was there a real console before ANY tee wrapped the streams?
    #
    # `sys.stdout is not None` is not the question, and asking it here is not enough either:
    # main() installs the tee for the non-run subcommands, so on the frozen setup→run path
    # (a fresh customer's very first double-click) stdout is ALREADY a _Tee by the time we
    # arrive, and this reads True on a machine with no console at all. That silently
    # reopens the exact hole this work exists to close: with no tray icon AND no console,
    # the "no tray icon could be shown" dialog would be skipped in favour of printing into
    # a log nobody is looking at, and the connector runs completely invisibly.
    #
    # A frozen build is a windowed build — console=False in connector.spec — so the only
    # console it can ever have is one AttachConsole borrowed from a parent terminal, which
    # is what `_tray.attach_parent_console()` in main() reports. Ask that, not the stream.
    had_console = (not is_frozen()) or bool(getattr(sys, "_fynn_attached_console", False))

    # Before ANYTHING — even the log tee. One keyboard, one connector.
    if _already_running():
        _tell("Fynn connector",
              # NOT "look for the icon": the mutex proves a process holds it, not that the
              # process has an icon or is healthy. It may be a copy whose tray failed to
              # start, and it may be one that was asked to quit and is still finishing a
              # document — a quit deliberately waits for a safe point.
              "Fynn is already running on this PC.\n\n"
              "If a round icon is near the clock, bottom-right (it may be behind the ^ "
              "arrow), that is it:\n"
              "  Purple: waiting for Drake to be opened.\n"
              "  Green: ready and waiting for documents.\n"
              "  Blue: entering one now.\n\n"
              "If you just clicked Quit, give it up to a minute — it never stops "
              "mid-document.\n\n"
              f"What it has been doing is written here:\n{_log_location()}")
        return 0

    # BEFORE the first print, including the banner — the banner names the version that
    # wrote every line under it, and a log that starts halfway through is a log that
    # answers "what did it type?" with "some of it".
    log_path = tray_mod.install_log_tee()

    cred = _cred_read()
    if not cred and is_frozen():
        # THE NORMAL LOGON-TASK FAILURE. The task runs `FynnConnector.exe run` explicitly,
        # so it lands here whenever the credential is unreadable — deleted through Manage
        # Windows Credentials, wiped with the profile, `unpair` (which leaves the logon
        # task installed), or an unparsable blob. It used to print one line to a log
        # nobody was reading and exit within a second: the office got no signal of any
        # kind, at this logon or any other, while the portal showed them offline and
        # everybody assumed the network. Offer the way out, like the revoked path does.
        _tell("Fynn connector",
              "This PC is not connected to Fynn yet.\n\n"
              "Nothing will be entered into Drake until it is.\n\n"
              "In Fynn: Settings → Connect a PC → copy the code.\n"
              "Click OK and paste it into the window that opens.")
        # In place: this run already holds the single-instance mutex, so cmd_setup must not
        # start a second loop. It pairs, and we carry straight on below with the new token.
        if cmd_setup(argparse.Namespace(server="", resume_in_place=True)) == 0:
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
            if had_console or not is_frozen():
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
                    # raise_it=False: this runs on a background thread and may fire while
                    # the driver is attached to Drake. A topmost Fynn box can land inside
                    # an evidence screenshot or a read-back OCR crop, and that crop is a
                    # safety gate. A notice that must not block the loop must not be able
                    # to change what the loop SEES either.
                    kwargs={"raise_it": False},
                    daemon=True,
                ).start()

    # Set when a report is lost FOR GOOD — Fynn refused it, or it could not even be saved
    # here. Never cleared, because nothing later in this run makes it untrue: Fynn's record
    # of what was typed into a live return is incomplete until a person deals with it. A
    # report that is merely WAITING (network down) is not this; that one clears itself and
    # is shown by the ordinary "still to go to Fynn" states.
    held_note = ""

    def _state(name: str, detail: str = "") -> None:
        # NEITHER GREEN NOR "cannot reach Fynn" ONCE A REPORT IS LOST. Green would say
        # "connected, waiting for work" over it, and amber would blame the network for
        # something the network did not do. A lost report outranks both.
        if held_note and name in ("idle", "offline"):
            name, detail = "error", held_note
        if tray is not None:
            tray.set_state(name, detail)

    # Set the moment _unpaired decides this machine is finished, cleared again if it
    # re-pairs. The catch-all at the bottom of the loop exists to outlive surprises, not to
    # overrule a deliberate exit: without this, an exception on the way out landed in that
    # handler, which printed into an invisible log, painted the tray orange "cannot reach
    # Fynn" over the honest red, slept ten seconds and started the whole thing again —
    # a revoked PC re-showing the same dialog forever, or worse, looping in silence.
    unpaired_seen = False

    def _unpaired(srv: str):
        """The server no longer knows this machine. Say so, then offer to fix it.

        Reached from three places — the job poll, the waiting-for-Drake liveness ping, and
        a report that could not be delivered — because a revoked PC has to be told whether
        or not Drake happens to be open, and whether or not it is holding a report.

        RETURNS None TO MEAN "CARRY ON". Re-pairing used to end the process: the person
        read "Connected as 'Front desk'. This PC will start the connector automatically
        when you log in", clicked OK, and the tray icon vanished — at the exact moment they
        were most confident they had just fixed it. Nothing then ran until the next sign-in,
        which on a machine that stays signed in is never. So a successful re-pair rebinds
        the new token here and the loop picks up where it stopped.

        In a windowed build stderr goes to a log file nobody is reading, so the dialog is
        modal on purpose, and it is followed by the setup window: telling somebody to
        re-pair while leaving them no way to do it is the same dead end twice, because
        double-clicking again just repeats this — a stored-but-rejected token still routes
        to `run`. From a terminal there is the `setup` subcommand and no need.
        """
        nonlocal server, token, unpaired_seen
        unpaired_seen = True
        print("This machine is no longer authorised (revoked, or the token was "
              "replaced). Re-pair from the portal.", file=sys.stderr)
        _state("unpaired", "Re-pair this PC from the Fynn portal.")
        _tell("Fynn connector",
              "This PC is no longer connected to Fynn.\n\n"
              "Someone removed it in the portal, or it was paired again somewhere else.\n\n"
              "In Fynn: Settings → Connect a PC → copy the code.\n"
              "Click OK and paste it into the window that opens.")
        if not is_frozen():
            return 1
        try:
            rc = cmd_setup(argparse.Namespace(server=srv, resume_in_place=True))
        except Exception as e:
            # The setup window itself failed. Leave the tray on the honest red with the
            # instruction in the tooltip — the state is the only channel that survives a
            # broken tkinter — rather than exiting into silence.
            print(f"the setup window could not be opened: {type(e).__name__}: {e}",
                  file=sys.stderr)
            _state("unpaired", "Re-pair this PC from the Fynn portal.")
            return 1
        if rc != 0:
            return 1
        fresh = _cred_read() or {}
        if not fresh.get("token"):
            return 1
        # REBIND, or the very next poll 401s on the old token and ping-pongs straight back
        # into this dialog. The tray label is rebuilt too, since the machine may have been
        # re-paired under a different name.
        server, token = fresh["server"], fresh["token"]
        if tray is not None:
            tray.machine_name = str(fresh.get("name") or "")
        unpaired_seen = False
        print(f"re-paired as {fresh.get('name')!r} — carrying on.")
        _state("starting", "re-paired — carrying on")
        return None

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

    def _announce(title: str, message: str) -> None:
        """A toast for a STATE the person can act on, rather than for a job outcome.

        Every state change used to be written to a log file and nowhere else, so the first
        real run of a machine with Drake shut showed the customer literally nothing: a grey
        dot, hidden by default in the ^ overflow, saying nothing they could see. The
        program did the right thing and told only itself.

        Frozen only. In a checkout every one of these lines is already on the console in
        front of the developer who started it, and a toast per start-up would be noise —
        the same rule `_tell` follows for its dialogs.
        """
        if not is_frozen():
            return
        _notify(title, message)

    _state("starting")

    print(f"Fynn Drake connector {VERSION}")
    print(f"Server : {server}")
    print(f"Machine: {cred.get('name')!r}")
    print(f"Log    : {log_path}" if tray_mod.LOG_TEE_OK else
          f"Log    : NOT BEING WRITTEN — {log_path} could not be opened")
    print("Leave Drake on its home screen. The connector opens the client, the screen and "
          "the record itself.\nCtrl+C to stop.\n")

    if is_frozen() and _autostart_points_here() is False:
        # The exe has moved since pairing (Downloads emptied, tidied onto the Desktop) and
        # the logon task still points at where it used to be, so it has been failing with
        # 0x2 at every sign-in — no process, therefore no icon, no dialog and no log line
        # to find. This launch is the one moment we can repair it, so repair it.
        print("the logon task pointed somewhere else — pointing it at this copy instead")
        if not _install_autostart():
            _announce("Fynn — check start-up",
                      "Fynn has moved since it was set up and could not update its "
                      "automatic start-up. It may not start by itself when you log in — "
                      "double-click FynnConnector.exe to start it.")

    driver = None
    # State-change reporting. Tracked rather than derived so the toast fires on the
    # TRANSITION and not on every fifteen-second pass through the same state.
    drake_seen = False        # has this run ever had Drake?
    said_no_drake = False     # ...and were they told to open it?
    no_drake_cycles = 0
    said_halted = False       # a firm-wide hold, said once per hold, not every 20s
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
                    # ONE POSITIVE CONFIRMATION. The first run of a new machine could
                    # otherwise finish its whole day without a single word on screen, and
                    # this also closes the loop on "open Drake" — the person who did what
                    # they were asked gets told it worked.
                    if not drake_seen or said_no_drake:
                        _announce("Fynn is connected to Drake",
                                  "Fynn is watching for documents. Leave Drake on its home "
                                  "screen — Fynn enters documents, it never files anything.")
                    drake_seen, said_no_drake, no_drake_cycles = True, False, 0
                except Exception as e:
                    # Liveness only. The portal then says the honest thing — the PC is on,
                    # Drake is not open — rather than showing the office as offline.
                    print(f"waiting for Drake ({type(e).__name__}: {e})")
                    # Its own colour, not the same grey as "starting": the one state a
                    # preparer can actually fix must not read as "booting up, wait".
                    _state("no-drake", "Open Drake and leave it on its home screen.")
                    # SAY IT ON SCREEN, on the transition and then rarely. The tooltip only
                    # reaches somebody who knows the icon exists, finds it behind the ^
                    # arrow, and hovers it.
                    if no_drake_cycles % NO_DRAKE_SAY_EVERY == 0:
                        _announce("Fynn — waiting for Drake",
                                  "Open Drake Tax and leave it on its home screen. Fynn "
                                  "will start entering documents by itself.")
                        said_no_drake = True
                    no_drake_cycles += 1
                    try:
                        _request(server, "/api/v1/connector/jobs?wait=0&ready=0",
                                 token=token, timeout=20)
                    except ServerError as se:
                        # A revoked machine must say so even with Drake shut. This ping used
                        # to swallow every error including 401, so a PC that had been removed
                        # in the portal sat on "waiting for Drake" for ever — the one state
                        # that looks completely normal — and no amount of opening Drake would
                        # have fixed it. Every other server error stays swallowed: this is a
                        # liveness ping, and being unable to reach Fynn is not a reason to
                        # stop waiting for Drake.
                        #
                        # 401 ONLY. Checked against the server rather than guessed: the
                        # connector auth hook answers 401 for every unrecognised, revoked
                        # or token-replaced machine and never 403, so widening this would
                        # only let an unrelated 403 masquerade as revocation and put a
                        # re-pair dialog in front of a firm whose pairing is fine.
                        if se.status == 401:
                            rc = _unpaired(server)
                            if rc is not None:
                                return rc
                            continue    # re-paired: back to looking for Drake
                    time.sleep(DRAKE_RETRY_SEC)
                    continue

            # RULE 2 — anything stranded by a network failure goes first.
            try:
                pending, given_up = _flush_spool(server, token)
            except ServerError as e:
                # Only 401/403 gets out of the delivery path, and it is not about the
                # report: this machine was revoked while it was still holding one. This is
                # the gate that runs before the poll, so without this branch a revoked PC
                # with one held report never reached the poll that says so — five seconds
                # apart, for ever, behind a green icon.
                if e.status != 401:
                    raise
                rc = _unpaired(server)
                if rc is not None:
                    return rc
                continue
            if given_up:
                # Never silent, and never green afterwards: this is the record of what a
                # robot typed into somebody's return, and Fynn has not got it.
                held_note = given_up[-1][:80]
                _state("error", held_note)
                _notify("Fynn — a report could not be sent",
                        f"{len(given_up)} report(s) could not be sent to Fynn and are kept "
                        f"on this PC. Fynn's copy of what was entered is incomplete — "
                        f"please tell Fynn support. The log has the details.")
            if pending > 0:
                # NOT GREEN WHILE A REPORT IS STRANDED. This gate can hold the loop for as
                # long as the network is out, and it used to hold it under "connected,
                # waiting for work" — a state byte-for-byte identical to working, while
                # Fynn's copy of what was typed into a return was sitting on this disk.
                _state("offline", f"{pending} report(s) still to go to Fynn")
                time.sleep(5)
                continue

            # RULE 1 AGAIN, IMMEDIATELY BEFORE CLAIMING. The poll below can hand back a
            # job, and claiming marks it `running` on the server: a job claimed by a
            # machine that cannot drive Drake is a stuck job needing a person, invented out
            # of nothing. Drake was checked once at startup and then trusted for ever —
            # so closing Drake at 5pm turned the next document into a halt against a real
            # client file that was never even opened.
            if not _drake_alive(driver):
                print("Drake is no longer there — dropping the connection and waiting.")
                _state("no-drake", "Open Drake and leave it on its home screen.")
                driver = None
                continue

            try:
                res = _request(server, f"/api/v1/connector/jobs?wait={POLL_WAIT_SEC}",
                               token=token, timeout=HTTP_TIMEOUT_SEC)
            except ServerError as e:
                if e.status == 401:
                    rc = _unpaired(server)
                    if rc is not None:
                        return rc
                    continue    # re-paired in place: carry on with the new token
                print(f"waiting to reach the server: {e}", file=sys.stderr)
                _state("offline", str(e)[:80])
                time.sleep(10)
                continue

            if res.get("halted"):
                # .get on the note as well: a malformed halt payload used to raise a
                # KeyError into the catch-all, which then told the firm their internet was
                # down — twice wrong about the same event.
                note = str((res.get("halted") or {}).get("note", ""))
                print(f"HELD: {note}")
                _state("halted", note[:80])
                # A halt raised by ANOTHER machine in the firm, or by a previous session,
                # used to be a colour change on an icon nobody was looking at — while the
                # halt stops work for everyone and is the state that most needs a person.
                # Once, on the transition: the branch below sleeps 20s and comes straight
                # back here, and a toast every 20 seconds is one people learn to dismiss.
                if not said_halted:
                    _notify("Fynn — everything is on hold",
                            f"Fynn is holding all work for this firm until someone reviews "
                            f"a document in the portal.\n{note}"[:250])
                    said_halted = True
                time.sleep(20)
                continue
            said_halted = False

            job = res.get("job")
            if not job:
                _state("idle")
                continue  # normal — nothing to do, ask again

            job_id = job["job_id"]

            # RULE 1, ONE LAST TIME — AFTER the wait, not just before it.
            #
            # The check before the poll is not enough on its own: that poll HOLDS THE LINE
            # FOR 25 SECONDS, and a preparer closing Drake, a Drake crash, or an IT restart
            # inside that window leaves a dead driver pointed at a job the server has now
            # marked `running`. Driving it would type into nothing, or worse into whatever
            # window inherited the focus, and the firm would be left with a stuck job and a
            # screen nobody can account for. Reporting it as a halt is the honest ending:
            # the document was never touched, it is not retried, and a person is told why.
            if not _drake_alive(driver):
                print("Drake went away while waiting for work — reporting this job "
                      "untouched rather than typing into a window that is gone.",
                      file=sys.stderr)
                driver = None
                gone = {
                    "ok": False,
                    "entered": 0,
                    "reason": "Drake closed on the office PC between this document being "
                              "handed out and being entered. NOTHING was typed for it.",
                    "untouched": True,
                }
                gone_body = {"ok": False, "report": gone}
                # Deliver it if we can, hold it if we cannot — the same contract as every
                # other report, minus the screenshot, because there is no window to
                # photograph. A 401 here means revoked as well as Drake-less; the held file
                # is written first so re-pairing sends it.
                try:
                    if _deliver_result(server, token, job_id, gone_body) == UNDELIVERED:
                        _spool(job_id, gone_body)
                except ServerError as e:
                    try:
                        _spool(job_id, gone_body)
                    except Exception:
                        pass
                    if e.status == 401:
                        rc = _unpaired(server)
                        if rc is not None:
                            return rc
                        continue
                except Exception as e:
                    print(f"  (could not report the untouched job: {type(e).__name__}: {e})",
                          file=sys.stderr)
                _state("no-drake", "Open Drake and leave it on its home screen.")
                _notify("Fynn — Drake closed",
                        "Drake closed before a document could be entered. Nothing was typed. "
                        "Open Drake and Fynn will carry on by itself.")
                continue

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

            revoked = None
            try:
                outcome = _deliver_result(server, token, job_id, body)
            except ServerError as e:
                # 401/403 comes out of the delivery path on purpose: this machine was
                # revoked between claiming the job and reporting it. The report has to
                # survive that, so it is held below BEFORE anybody is told anything, and a
                # re-pair sends it on the next pass.
                if e.status != 401:
                    raise
                outcome, revoked = UNDELIVERED, e

            if outcome == UNDELIVERED:
                try:
                    path = _spool(job_id, body)
                    print(f"  report held on disk and will be retried: {path}",
                          file=sys.stderr)
                except Exception as e:
                    # ENTERED, NOT REPORTED, AND NOT EVEN HELD — the one path where
                    # carrying on quietly is not safe. The values are in a live return, the
                    # server still shows the job running, and the only remaining copy of
                    # what was typed is this log, so the whole report goes into it.
                    print(f"  the report could NOT be held on disk either "
                          f"({type(e).__name__}: {e}) — it now exists nowhere but this "
                          f"log:\n  {json.dumps(body.get('report'))}", file=sys.stderr)
                    held_note = "a report could not be sent to Fynn OR saved on this PC"
                    _notify("Fynn — a report did not reach Fynn",
                            f"The {job.get('doc_type')} was entered in Drake, but Fynn has "
                            f"no report of it and this PC could not hold one. Please tell "
                            f"Fynn support before sending more documents.")
            elif outcome == REJECTED:
                # Fynn will never accept this one. Keep it — it is the record of what was
                # typed into a live return — but keep it out of the way of every future
                # document, which is what a permanent rejection used to block for ever.
                try:
                    held_note = _set_aside(_spool(job_id, body),
                                           "Fynn refused this report")[:80]
                except Exception as e:
                    print(f"  the refused report could not be saved either "
                          f"({type(e).__name__}: {e}) — it exists nowhere but this log:\n"
                          f"  {json.dumps(body.get('report'))}", file=sys.stderr)
                    held_note = "a report could not be sent to Fynn OR saved on this PC"
                _notify("Fynn — a report could not be sent",
                        f"The {job.get('doc_type')} was entered in Drake, but Fynn would "
                        f"not accept the report of it. It is kept on this PC — please tell "
                        f"Fynn support.")

            if revoked is not None:
                # No toast about this document first: nothing about it reached Fynn, so
                # there is nothing yet for anyone to review in the portal.
                rc = _unpaired(server)
                if rc is not None:
                    return rc
                if args.once:
                    print("\n--once: the report is held and goes out on the next run.")
                    return 2
                continue

            print("Values are IN Drake but NOT filed: a human still reviews and executes.")
            if args.once:
                print("\n--once: that job is reported. Stopping.")
                return 0 if report.get("ok") else 2
            if not report.get("ok"):
                print("\nThat document did not complete cleanly. Nothing further will be "
                      "handed to this machine until somebody reviews it in the portal.",
                      file=sys.stderr)
                _state("halted", str(report.get("reason", ""))[:80])
                if report.get("crashed"):
                    # Mid-something unknowable by definition. Dropped HERE rather than at
                    # the crash, so the evidence screenshot above still had a window to
                    # photograph; the next pass reconnects, or says Drake is gone.
                    driver = None
                # After the report is delivered (or held), never before: the toast may only
                # repeat what the report already said. It tells the operator the firm is
                # blocked — that is a fact about the server's halt guard, not a request.
                _notify("Fynn — run stopped",
                        f"The {job.get('doc_type')} run stopped and needs your review in "
                        f"the Fynn portal. Nothing else will be entered until someone "
                        f"reviews it."
                        + ("" if outcome == DELIVERED else
                           " (Fynn REFUSED this report, so the portal will not show this "
                           "run — check that document in Fynn.)" if outcome == REJECTED else
                           " (The report has not reached Fynn yet — it is held on this PC.)"))
            elif outcome == DELIVERED:
                _state("idle")
                # "entered" is the report's own count — the toast claims nothing the report
                # did not say, and it never says "filed", because nothing here files.
                _notify("Fynn — document entered",
                        f"{job.get('doc_type')} entered ({report.get('entered')} fields). "
                        f"Review it in the Fynn portal — nothing is filed.")
            elif outcome == REJECTED:
                # REFUSED FOR GOOD, and the difference from "held" is the whole point of
                # saying anything. The document IS entered — the report's own word — but
                # Fynn refused the report and `_set_aside` has already moved it out of the
                # queue, so no retry will ever deliver it. Telling somebody it "will be
                # sent automatically" here would be a promise the code has just made
                # impossible: the portal would sit on "running" for ever while the only
                # person who could reconcile it believed it was handling itself.
                _state("error", "Fynn refused a report — that document needs checking")
                _notify("Fynn — entered, but the report was refused",
                        f"{job.get('doc_type')} entered ({report.get('entered')} fields), "
                        f"but Fynn refused the report, so the portal will not show it. "
                        f"Check that document in Fynn. Nothing is filed.")
            else:
                # DO NOT SAY "review it in the portal" FOR A REPORT THE PORTAL NEVER GOT.
                # The document really was entered — that is the report's own word — but
                # Fynn has not heard it, so the portal will show this document as still
                # running until the held report goes out. Not green either, for the same
                # reason: the flush gate above will keep saying so until it lands.
                _state("offline", "a report is waiting to be sent to Fynn")
                _notify("Fynn — document entered, not yet reported",
                        f"{job.get('doc_type')} entered ({report.get('entered')} fields), "
                        f"but Fynn has not received the report yet. It is held on this PC "
                        f"and will be sent automatically. Nothing is filed.")

        except KeyboardInterrupt:
            print("\nstopped.")
            return 0
        except ServerError as e:
            # SOMETHING THE NETWORK DID, which is what "cannot reach Fynn" should mean and
            # nothing else. Anything reaching here is a server call whose own handler did
            # not cover it; it is retried like every other network failure.
            print(f"waiting to reach the server: {e}", file=sys.stderr)
            _state("offline", str(e)[:80])
            # The driver is deliberately KEPT. Nothing the network did says anything about
            # Drake, and dropping it here meant a reconnect every ten seconds through an
            # outage — each one pulling Drake to the front, in front of somebody trying to
            # work in it. The liveness check before the poll is what guards Rule 1 now.
            time.sleep(10)
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
            #
            # ...WITH ONE EXCEPTION. A machine that has been told it is no longer paired
            # has finished. This handler exists to outlive surprises, not to overrule a
            # decision: without the check it resurrected the process, and the office got
            # the same dialog every ten seconds — or, if the dialogs were failing too, an
            # invisible loop under an icon blaming the internet.
            if unpaired_seen:
                print("stopping: this PC is not paired with Fynn.", file=sys.stderr)
                _state("unpaired", "Re-pair this PC from the Fynn portal.")
                return 1
            # NOT "offline". Every unexpected internal fault used to be reported as a
            # network problem, so a bug in our own JSON handling sent the office to phone
            # their ISP — and if the fault was deterministic it repeated every ten seconds
            # for ever with the icon still blaming the weather.
            print(f"unexpected error — continuing in 10s ({type(e).__name__}: {e})",
                  file=sys.stderr)
            _state("error", f"{type(e).__name__}: {e}"[:80])
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
        # Recorded on `sys` because cmd_run has to know whether a REAL console ever existed
        # long after the tee has replaced the streams and made that unanswerable. Getting
        # this wrong skips the "running invisibly" dialog on a machine that has no other
        # channel — see `had_console` in cmd_run.
        sys._fynn_attached_console = bool(_tray.attach_parent_console())  # type: ignore[attr-defined]
    except Exception:
        pass

    argv = list(sys.argv[1:] if argv is None else argv)

    # DOUBLE-CLICKED. argparse would exit(2) with a usage message onto a stderr nobody can
    # see, so the exe would look like it did nothing at all — the single most likely thing
    # for a firm to do with a file we emailed them.
    if not argv and is_frozen():
        argv = ["run"] if _cred_read() else ["setup"]

    args = build_parser().parse_args(argv)

    # THE INSTALL LEFT NO TRACE. The tee lived inside `run` only, so setup — the one
    # command a brand-new customer reaches by double-clicking, and the moment when the most
    # can go wrong — printed into a stdout that is None in a windowed build. Support asked
    # for the log and there was no file, or one from an unrelated later run.
    #
    # `run` still installs its own, AFTER its single-instance guard: a second instance that
    # starts writing the shared log is already a mess, so that ordering is deliberate.
    if getattr(args, "cmd", None) != "run":
        try:
            _tray.install_log_tee()
        except Exception:
            pass

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 — nothing below this catches anything
        # There was no handler here at all, so anything the commands did not expect —
        # a scheme-less address typed into the setup window, a Tcl failure, a blocked
        # Credential Manager — ended as a traceback printed to a stderr that does not
        # exist, and the exe simply vanished. Silence is the failure this whole file is
        # written against.
        import traceback
        traceback.print_exc()
        # No claim about Drake here: what this process had or had not done by the time it
        # was surprised is exactly what is not known. The log says what it did.
        _tell("Fynn", f"Fynn hit a problem it did not expect and has stopped.\n\n"
                      f"{type(e).__name__}: {e}\n\n"
                      f"Nothing more will be entered until it is started again. Please "
                      f"send Fynn support the log:\n{_log_location()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
