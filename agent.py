#!/usr/bin/env python3
"""
Fynn Drake agent — the robot that drives Drake's on-screen data entry.

Run this ON THE WINDOWS VM, in the interactive (unlocked) session, with Drake open
on a TEST return. It NEVER files: there is no file/e-file/submit command anywhere in
this agent or its protocol. It types data-entry boxes and reads them back; a human
verifies and executes in Drake.

Modes (run in this order the first time):
  probe      Attach to Drake, dump the active screen's Edit controls + whether each
             exposes a readable UIA value. THIS answers the make-or-break question:
             can the read-back moat work? Do this first.
  calibrate  Same dump, formatted to help you fill binding.json (logical field →
             automation_id / field_no / tab_index).
  selftest   Run a local plan file (a list of protocol commands) against Drake and
             print each result — proves the type→read-back loop end-to-end, no Fynn.
  connect    Dial Fynn over WebSocket and serve the live protocol (needs a signed
             authorization + Fynn's agent endpoint; the last step).

Examples:
  python agent.py probe --binding binding.json
  python agent.py calibrate --binding binding.json --screen W2
  python agent.py selftest --binding binding.json --plan selftest.plan.json
  python agent.py connect --binding binding.json --url wss://api.fynnalabs.com/drake-agent --token $DRAKE_AGENT_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from drake_driver import DrakeDriver
from protocol import dispatch


def load_binding(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_probe(args) -> int:
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    controls = driver.list_edit_controls()
    readable = [c for c in controls if c["readable"]]
    print(f"\nActive Drake screen: {len(controls)} Edit control(s), {len(readable)} expose a readable UIA value.\n")
    for i, c in enumerate(controls):
        flag = "READABLE" if c["readable"] else "opaque  "
        print(f"  [{i:2}] {flag}  auto_id={c['automation_id']!r}  name={c['name']!r}  value={c['value']!r}  via={c['read_method']}")
    print()
    if not readable:
        print("VERDICT: Drake's boxes do NOT expose values to UI Automation on this screen.")
        print("         The per-field read-back moat cannot verify here — do not run live;")
        print("         report this back so we rethink (screenshot/OCR fallback or CCH-first).")
    else:
        print("VERDICT: Drake boxes are UIA-readable. Set capabilities.can_read_field_values=true")
        print("         in binding.json and map these auto_ids to logical fields (calibrate).")
    return 0


def cmd_calibrate(args) -> int:
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    controls = driver.list_edit_controls()
    print(f"\n# Open Drake to the {args.screen or '<screen>'} screen, then map these into binding.json:\n")
    print(f'  "screens": {{ "{args.screen or "SCREEN"}": {{ "fields": {{')
    for c in controls:
        aid = c["automation_id"]
        print(f'      "<logical_field>": {{ "automation_id": {json.dumps(aid)} }},   # name={c["name"]!r} value={c["value"]!r}')
    print("  } } }\n")
    print("Tip: focus a box in Drake and re-run to confirm which auto_id changed value.")
    return 0


def cmd_selftest(args) -> int:
    import time
    driver = DrakeDriver(load_binding(args.binding), dry_run=args.dry_run, key_pause=0.12 if args.slow else 0.03)
    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)
    print(f"\nRunning {len(plan)} step(s){' (dry-run)' if args.dry_run else ''}{' (slow — watch Drake)' if args.slow else ''}:\n")
    failures = 0
    for i, req in enumerate(plan):
        req.setdefault("id", i)
        if args.slow and not args.dry_run:
            time.sleep(0.6)  # let the eye follow each box
        reply = dispatch(driver, req)
        exp = req.get("_expect")  # optional inline assertion on a readField
        note = ""
        if exp is not None and args.dry_run:
            note = "  (skipped in dry-run — no live read)"
        elif exp is not None and "result" in reply:
            got = reply["result"].get("value")
            ok = str(got) == str(exp)
            note = f"  EXPECT {exp!r} -> {'OK' if ok else 'MISMATCH (' + repr(got) + ')'}"
            if not ok:
                failures += 1
        print(f"  [{i:2}] {req.get('method'):14} {json.dumps(req.get('params', {}))[:60]:60} -> {json.dumps(reply.get('result', reply.get('error')))[:70]}{note}")
    print(f"\n{'PASS' if failures == 0 else str(failures) + ' MISMATCH(es)'}\n")
    return 1 if failures else 0


def cmd_connect(args) -> int:
    try:
        import websocket  # websocket-client
    except Exception:
        print("connect mode needs 'websocket-client' (pip install websocket-client)", file=sys.stderr)
        return 2
    url = args.url or os.environ.get("DRAKE_AGENT_WS_URL")
    token = args.token or os.environ.get("DRAKE_AGENT_TOKEN")
    if not url or not token:
        print("connect mode needs --url and --token (or DRAKE_AGENT_WS_URL / DRAKE_AGENT_TOKEN)", file=sys.stderr)
        return 2

    driver = DrakeDriver(load_binding(args.binding))
    print(f"Connecting to {url} …  (Ctrl+C to stop; the agent NEVER files)")
    ws = websocket.create_connection(url, header=[f"Authorization: Bearer {token}"], timeout=60)
    try:
        while True:
            raw = ws.recv()
            if not raw:
                break
            req = json.loads(raw)
            reply = dispatch(driver, req)
            ws.send(json.dumps(reply))
    finally:
        ws.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Fynn Drake agent (pywinauto). Never files.")
    # --binding lives on a shared parent so it works AFTER the subcommand too,
    # e.g. `agent.py probe --binding binding.json`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--binding", default="binding.json", help="path to binding.json")
    sub = p.add_subparsers(dest="mode", required=True)

    sp = sub.add_parser("probe", parents=[common]); sp.set_defaults(func=cmd_probe)
    sc = sub.add_parser("calibrate", parents=[common]); sc.add_argument("--screen"); sc.set_defaults(func=cmd_calibrate)
    ss = sub.add_parser("selftest", parents=[common]); ss.add_argument("--plan", default="selftest.plan.json"); ss.add_argument("--dry-run", action="store_true"); ss.add_argument("--slow", action="store_true", help="slower keystrokes + pauses so you can watch Drake"); ss.set_defaults(func=cmd_selftest)
    scn = sub.add_parser("connect", parents=[common]); scn.add_argument("--url"); scn.add_argument("--token"); scn.set_defaults(func=cmd_connect)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
