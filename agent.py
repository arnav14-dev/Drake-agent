#!/usr/bin/env python3
"""
Fynn Drake agent — the robot that drives Drake's on-screen data entry.

Run this ON THE WINDOWS VM, in the interactive (unlocked) session, with Drake open
on a TEST return. It NEVER files: there is no file/e-file/submit command anywhere in
this agent or its protocol. It types data-entry boxes and reads them back; a human
verifies and executes in Drake.

Modes (run in this order the first time):
  probe      Attach to Drake, dump the active screen's Edit controls + whether each
             exposes a readable UIA value. (Probe 2026-07: Drake's grid is custom-drawn
             and exposes ZERO Edit controls / values to UI Automation. Kept as a
             per-build re-check, but expect all-opaque.)
  clip       (Plan B, RULED OUT on Drake 2025) Manually focus a Drake field; the agent
             copies it (Ctrl+A, Ctrl+C) and prints the clipboard. On Drake this returns
             nothing AND breaks focus, so clipboard read-back is dead — kept as a
             per-build re-check for other software / a future build.
  shoot      Save a PNG of the live Drake window. Two jobs: (1) the human-verify FLOOR
             (no read-back? a person checks this capture), and (2) how you calibrate OCR
             boxes — open the PNG, read each field's [x,y,w,h], put it in binding.json.
  typetest   Proof that the robot can TYPE into Drake: you click a field, the agent types
             a value with genuine foreground synthetic keystrokes (Juno's technique). Run
             this first — it isolates "do our keys land?" from field-map calibration.
  calibrate  Same control dump, formatted to help you fill binding.json (logical field →
             automation_id / field_no / tab_index).
  selftest   Run a local plan file (a list of protocol commands) against Drake and print
             each result — proves the type→read-back loop end-to-end, no Fynn. With no
             programmatic read-back it types + saves a screenshot for a human to verify.
  connect    Dial Fynn over WebSocket and serve the live protocol (needs a signed
             authorization + Fynn's agent endpoint; the last step).

Examples:
  python agent.py probe --binding binding.json
  python agent.py shoot --binding binding.json --out drake.png
  python agent.py calibrate --binding binding.json --screen W2
  python agent.py selftest --binding binding.json --plan selftest.plan.json --shot after.png
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
    # utf-8-sig tolerates the UTF-8 BOM that PowerShell/Notepad prepend when you
    # create or edit binding.json on Windows (plain utf-8 chokes on it).
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def cmd_probe(args) -> int:
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}  (hwnd={wi.get('handle')})")
    print("  ^ must be the main Drake data-entry frame, NOT the ~84x84 chat bubble.")
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


def cmd_clip(args) -> int:
    """
    Clipboard read-back feasibility test — the NEW make-or-break question now that UIA
    exposes no field values. You focus a Drake field by hand; the agent copies it and
    prints what landed on the clipboard.

    Uses a countdown (not an Enter prompt) on purpose: pressing Enter in this console
    would steal focus away from Drake, so instead you get a few seconds to click into a
    field, and the agent copies whatever is focused when the countdown ends.
    """
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    print(f"\nClick into a Drake data-entry field that already holds a KNOWN value.")
    print(f"Copying in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print("\n")
    got = driver.copy_focused_to_clipboard()
    print(f"Clipboard after Ctrl+A / Ctrl+C: {got!r}\n")
    if got:
        print("VERDICT: Drake fields DO support copy → EXACT clipboard read-back is viable.")
        print("         Set capabilities.read_back_method = \"clipboard\" and")
        print("         can_read_field_values = true in binding.json, then run selftest.")
    else:
        print("VERDICT: nothing copied → no clipboard read-back on this build (expected on")
        print("         Drake 2025). Read-back falls to OCR of a screenshot crop")
        print("         (read_back_method=\"ocr\" + per-field ocr_box) or the screenshot +")
        print("         human floor (read_back_method=\"screenshot\"). Use `shoot` to capture.")
    return 0


def cmd_shoot(args) -> int:
    """
    Save a screenshot of the live Drake window. This is the screenshot 'read-back'
    FLOOR (no programmatic read exists on Drake, so a human verifies the capture) and
    the way you calibrate OCR boxes: open the PNG in any image editor, read off each
    field's [x, y, w, h] in pixels (relative to the window's top-left), and put it in
    binding.json under the field's "ocr_box".
    """
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}  (hwnd={wi.get('handle')})")
    if (wi.get("width") or 0) < 600 or (wi.get("height") or 0) < 400:
        print("  ⚠ that looks too small to be the data-entry frame — is it the chat overlay?")
        print("    Set \"main_window_min\" or \"app_title_re\" in binding.json.")
    res = driver.save_screenshot(args.out)
    if res.get("ok"):
        print(f"\nSaved Drake window screenshot -> {res['path']}")
        print("  • Read each field's pixel box [x,y,w,h] off it into binding.json \"ocr_box\".")
        print("  • This same capture is the human-verify floor when there's no read-back.\n")
        return 0
    print(f"\nCould not capture the Drake window: {res.get('error')}\n", file=sys.stderr)
    return 1


def cmd_typetest(args) -> int:
    """
    The isolation proof for "can the robot actually type into Drake?". You click a Drake
    field by hand; the agent types a value into it with genuine foreground synthetic
    keystrokes (the technique Juno uses — NOT the accessibility/set_text path that came
    back empty). This separates "do our keystrokes land?" from "is the field map right?".

    Uses a countdown (not an Enter prompt) so your console never steals focus from Drake.
    Internal proof / demo only — never files, and per Drake's 2026 license live automation
    needs written authorization before it touches real client returns.
    """
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    print(f"\nClick into a Drake data-entry field. Typing {args.text!r} in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print("\n")
    res = driver.type_raw(args.text)
    print(f"Sent {args.text!r} via synthetic keystrokes -> {json.dumps(res)}")
    if args.shot:
        s = driver.save_screenshot(args.shot)
        print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
    print("\nVERDICT: value appears in the box you clicked -> our keystrokes LAND on Drake's")
    print("         canvas; typing WORKS and the rest is just navigation/calibration.")
    print("         Box still empty -> keys are being dropped; check the elevation warning")
    print("         above and re-run this console 'as Administrator'.")
    return 0


def _values_match(got, exp) -> bool:
    """Compare a read-back to the expected value, tolerant of OCR/format noise:
    '$52,000' == '52000', '12-3456789' == '123456789'. Alphanumerics only, case-fold."""
    if got is None:
        return False
    def norm(s):
        return "".join(ch for ch in str(s) if ch.isalnum()).lower()
    return norm(got) == norm(exp)


def cmd_selftest(args) -> int:
    import time
    driver = DrakeDriver(load_binding(args.binding), dry_run=args.dry_run, key_pause=0.12 if args.slow else 0.03)
    with open(args.plan, "r", encoding="utf-8-sig") as f:
        plan = json.load(f)
    # Can this build read a value back programmatically? On Drake 2025 the honest answer
    # is no (method "none"/"screenshot") — then _expect checks can't pass or fail, they're
    # UNVERIFIED and a human confirms from the screenshot.
    programmatic = driver.read_back_method in ("uia", "clipboard", "ocr")
    print(f"\nRunning {len(plan)} step(s){' (dry-run)' if args.dry_run else ''}"
          f"{' (slow — watch Drake)' if args.slow else ''}"
          f"  read_back_method={driver.read_back_method!r}:\n")
    failures = 0
    unverified = 0
    for i, req in enumerate(plan):
        req.setdefault("id", i)
        if args.slow and not args.dry_run:
            time.sleep(0.6)  # let the eye follow each box
        reply = dispatch(driver, req)
        exp = req.get("_expect")  # optional inline assertion on a readField
        note = ""
        if exp is not None and args.dry_run:
            note = "  (skipped in dry-run — no live read)"
        elif exp is not None and not programmatic:
            note = f"  EXPECT {exp!r} -> UNVERIFIED (no programmatic read-back; verify via screenshot)"
            unverified += 1
        elif exp is not None and "result" in reply:
            got = reply["result"].get("value")
            ok = _values_match(got, exp)
            note = f"  EXPECT {exp!r} -> {'OK' if ok else 'MISMATCH (' + repr(got) + ')'}"
            if not ok:
                failures += 1
        print(f"  [{i:2}] {req.get('method'):14} {json.dumps(req.get('params', {}))[:60]:60} -> {json.dumps(reply.get('result', reply.get('error')))[:70]}{note}")
    shot_note = ""
    if args.shot and not args.dry_run:
        sres = driver.save_screenshot(args.shot)
        shot_note = (f"\nscreenshot -> {sres['path']}" if sres.get("ok")
                     else f"\n(screenshot failed: {sres.get('error')})")
    if failures:
        print(f"\n{failures} MISMATCH(es){shot_note}\n")
    elif unverified:
        print(f"\nTYPED OK — {unverified} field(s) UNVERIFIED: no programmatic read-back on this "
              f"build, so a human must confirm the values from the screenshot.{shot_note}\n")
    else:
        print(f"\nPASS{shot_note}\n")
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
    scl = sub.add_parser("clip", parents=[common]); scl.add_argument("--delay", type=int, default=5, help="seconds to click into a Drake field before the copy fires"); scl.set_defaults(func=cmd_clip)
    sst = sub.add_parser("shoot", parents=[common]); sst.add_argument("--out", default="drake.png", help="where to save the window PNG"); sst.set_defaults(func=cmd_shoot)
    stt = sub.add_parser("typetest", parents=[common]); stt.add_argument("--text", default="52000", help="value to type into the field you click"); stt.add_argument("--delay", type=int, default=15, help="seconds to click into a Drake field before typing fires"); stt.add_argument("--shot", help="save a screenshot here after typing"); stt.set_defaults(func=cmd_typetest)
    sc = sub.add_parser("calibrate", parents=[common]); sc.add_argument("--screen"); sc.set_defaults(func=cmd_calibrate)
    ss = sub.add_parser("selftest", parents=[common]); ss.add_argument("--plan", default="selftest.plan.json"); ss.add_argument("--dry-run", action="store_true"); ss.add_argument("--slow", action="store_true", help="slower keystrokes + pauses so you can watch Drake"); ss.add_argument("--shot", help="save a window screenshot here after the run (human-verify floor / OCR-box source)"); ss.set_defaults(func=cmd_selftest)
    scn = sub.add_parser("connect", parents=[common]); scn.add_argument("--url"); scn.add_argument("--token"); scn.set_defaults(func=cmd_connect)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
