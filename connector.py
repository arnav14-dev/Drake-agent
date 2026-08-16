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
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"

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
    cred = _cred_read()
    if not cred:
        print("Not paired. Run:  connector.py pair --server <url> --code <CODE>", file=sys.stderr)
        return 1
    server, token = cred["server"], cred["token"]

    # Imported here, not at module scope, so `pair` / `status` / `unpair` work on a machine
    # where Drake is not installed and pywinauto cannot bind anything.
    import agent as agent_mod
    from drake_driver import DrakeDriver

    binding = agent_mod.load_binding(args.binding)
    nav_token = (binding.get("navigation", {}) or {}).get("headsdown_checkbox_true", "X")

    print(f"Fynn Drake connector {VERSION}")
    print(f"Server : {server}")
    print(f"Machine: {cred.get('name')!r}")
    print("Leave Drake on its home screen. The connector opens the client, the screen and "
          "the record itself.\nCtrl+C to stop.\n")

    driver = None
    while True:
        try:
            # RULE 1 — Drake first, then work. Claiming a job we cannot do would mark it
            # running on the server and manufacture a stuck job needing a human.
            if driver is None:
                try:
                    d = DrakeDriver(binding)
                    d.connect()
                    if d.w32 is None:
                        raise RuntimeError("no win32 popup connection — heads-down entry needs it")
                    wi = d.window_info()
                    print(f"Drake connected: {wi.get('title')!r}")
                    driver = d
                except Exception as e:
                    # Liveness only. The portal then says the honest thing — the PC is on,
                    # Drake is not open — rather than showing the office as offline.
                    print(f"waiting for Drake ({type(e).__name__}: {e})")
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
                    return 1
                print(f"waiting to reach the server: {e}", file=sys.stderr)
                time.sleep(10)
                continue

            if res.get("halted"):
                print(f"HELD: {res['halted']['note']}")
                time.sleep(20)
                continue

            job = res.get("job")
            if not job:
                continue  # normal — nothing to do, ask again

            job_id = job["job_id"]
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
            if not report.get("ok"):
                print("\nThat document did not complete cleanly. Nothing further will be "
                      "handed to this machine until somebody reviews it in the portal.",
                      file=sys.stderr)

        except KeyboardInterrupt:
            print("\nstopped.")
            return 0


def main(argv=None) -> int:
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

    sr = sub.add_parser("run", help="take work and enter it into Drake")
    sr.add_argument("--binding", default="binding.json")
    sr.add_argument("--no-navigate", action="store_true",
                    help="do not open the client/screen — enter into whatever is in front "
                         "(for debugging only; the identity check is what this skips)")
    sr.add_argument("--slow", action="store_true", help="slower keystrokes so you can watch")
    sr.add_argument("--dry-run", action="store_true", help="plan and report, type nothing")
    sr.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
