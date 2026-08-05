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
  headsdown  THE precision path: address Drake fields BY NUMBER with NO mouse click,
             race-free + verified. With --seq "N=value,..." it drives the heads-down popup
             field by field (focus its edit, read the number back before Enter, prove the
             jump, type the value on the canvas), HALTing on any anomaly. --manual bootstraps
             by having you click a field first. Coordinate-free (see PRECISION-PLAN.md).
  probe-popup READ-ONLY: dump the "Heads Down Data Entry" popup's real control tree + test
             a number round-trip + report whether it closes after Enter. Run ONCE first to
             lock in the exact edit control the driver binds to. Writes no field value.
  probe-checkbox
             Calibration for a Box 13 CHECKBOX field (46/47/48), whose value stage is a tick
             box rather than a text box: reports whether the tick is readable (accessibility
             Toggle state and/or the on-screen glyph), what state the box arrives in, and
             which token flips it. Ticks and unticks to measure, then leaves with Esc —
             never presses Enter, so nothing is committed.
  envdump    READ-ONLY: write the Drake process's full window topology to JSON — every
             top-level window (class/style/owner/enabled/visible + children), where the
             keyboard would land, and the structural dialog gate's verdict per window.
             The halt diagnostic; also written automatically as env-dump-halt.json when
             a heads-down batch halts.
  write-w2   THE product path: extracted W-2 JSON (the LLM's structured output) -> Drake,
             every field entered BY NUMBER through the verified heads-down loop. Run with
             --dry-run first (works on any machine, touches nothing) to review exactly which
             box each value lands in before a keystroke is sent.
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
  python agent.py write-w2 --json sample_w2.json --dry-run
  python agent.py write-w2 --binding binding.json --json sample_w2.json
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
    res = driver.save_screenshot(args.out, grid=args.grid)
    if res.get("ok"):
        print(f"\nSaved Drake window screenshot -> {res['path']}{'  (with coordinate grid)' if args.grid else ''}")
        print("  • Each field's CENTER [x,y] -> binding.json \"click_xy\" (plants the caret).")
        print("  • Each field's box [x,y,w,h] -> binding.json \"ocr_box\" (OCR read-back).")
        print("  • Re-run with --grid to overlay labeled pixel lines and read them by eye.\n")
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
    driver = DrakeDriver(load_binding(args.binding), vk_packet=(True if args.unicode else None))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    method = "unicode-packet (modern)" if driver.vk_packet else "scancode/VK (legacy-app friendly)"
    print(f"Keystroke method: {method}")

    # Diagnostic: let the AGENT click at a given window-point, then type — so we can see
    # whether the agent's own click-to-focus lands (the only new variable vs the manual
    # typetest that worked). WATCH the mouse cursor.
    if args.click_xy:
        x, y = [int(v.strip()) for v in args.click_xy.split(",")]
        values = [v.strip() for v in args.text.split(",")]
        print(f"\nAgent will CLICK window-point ({x},{y}), then type. WATCH the mouse cursor.")
        print("Clicking in ", end="", flush=True)
        for n in range(3, 0, -1):
            print(f"{n}… ", end="", flush=True); time.sleep(1)
        print()
        cres = driver.click_at(x, y)
        print(f"click_at -> {json.dumps(cres)}")
        time.sleep(0.3)
        adv = (args.advance or "ENTER").upper()
        for i, v in enumerate(values):
            driver.type_raw(v, advance=(None if i == len(values) - 1 else adv))
        print(f"typed {values}")
        if args.shot:
            s = driver.save_screenshot(args.shot)
            print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
        print("\nVERDICT — tell me two things:")
        print("  1) did the mouse cursor jump to the box you intended?")
        print("  2) did the value appear in it?")
        print("  right box + typed   -> agent click-to-focus WORKS; calibrate the rest.")
        print("  WRONG spot          -> coordinate/DPI mismatch; paste the click_at numbers.")
        print("  right spot, no text -> focus/timing; we add a settle pause or a commit.")
        return 0

    print(f"\nClick into a Drake data-entry field. Typing {args.text!r} in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print("\n")
    values = [v.strip() for v in args.text.split(",")]
    if len(values) == 1:
        res = driver.type_raw(values[0])
        print(f"Sent {values[0]!r} via synthetic keystrokes -> {json.dumps(res)}")
        print("\nVERDICT: value appears in the box you clicked -> keystrokes LAND; typing WORKS.")
        print("         Still empty -> keys dropped; try `--unicode`, or run as Administrator.")
    else:
        adv = (args.advance or "ENTER").upper()
        for i, v in enumerate(values):
            last = i == len(values) - 1
            driver.type_raw(v, advance=(None if last else adv))
        print(f"Sent {len(values)} values, pressing {{{adv}}} between each: {values}")
        print(f"\nVERDICT: read off WHICH box each value landed in (in order {values}) — that's")
        print(f"         Drake's field order. If a value overwrites the previous instead of")
        print(f"         moving on, {{{adv}}} isn't advancing fields — retry with `--advance TAB`.")
    if args.shot:
        s = driver.save_screenshot(args.shot)
        print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
    return 0


def _parse_seq(seq: str, *, screen: str = "", allow_unconfirmed: bool = False,
               allow_protected: bool = False):
    """"N=value,N=value,…" -> [{field_no, value, label}] for the shared entry runner.

    Strict on purpose. The old version split on commas and silently `continue`d past any
    token without '=', so `--seq "5=ACME, INC,7=123 MAIN ST"` entered the employer as
    "ACME", dropped " INC", and then printed "All fields entered and verified". A value
    containing a comma is not expressible here at all — use repeated `--field N=VALUE`.
    """
    entries, bad = [], []
    for pair in seq.split(","):
        tok = pair.strip()
        if not tok:
            continue
        if "=" not in tok:
            bad.append(tok)
            continue
        num, val = tok.split("=", 1)
        entries.append({"field_no": num.strip(), "value": val.strip(), "label": ""})
    if bad:
        raise SystemExit(
            f"--seq: token(s) with no '=': {', '.join(repr(b) for b in bad)}\n"
            f"  A value containing a comma cannot be written with --seq (it splits on "
            f"commas). Use repeated --field, e.g.  --field '5=ACME, INC' --field '7=123 MAIN ST'")
    return _validate_entries(entries, screen=screen, allow_unconfirmed=allow_unconfirmed,
                             allow_protected=allow_protected)


# Header selectors + the EIN. 1/2/3 are dropdowns whose typed-value behaviour is
# unconfirmed; 4 auto-fills and auto-advances, and is the operator's standing do-not-touch.
PROTECTED_W2_FIELDS = {1, 2, 3, 4}


def _validate_entries(entries, *, screen: str = "", allow_unconfirmed: bool = False,
                      allow_protected: bool = False):
    """Reject a field number or value that cannot be right, BEFORE any keystroke."""
    from w2_map import MAX_CONFIRMED_FIELD
    is_w2 = (screen or "").upper() == "W2"
    for e in entries:
        num = str(e["field_no"]).strip()
        if not num.isdigit() or int(num) < 1:
            raise SystemExit(f"field number {num!r} is not a positive integer")
        if not str(e["value"]).strip():
            raise SystemExit(
                f"field {num} was given an EMPTY value. A blank commit CLEARS whatever the "
                f"box already holds — omit the field to leave it alone.")
        n = int(num)
        # Only bound-check W2: --screen takes any Drake screen code, and their numbering
        # is not ours to police.
        if is_w2 and n > MAX_CONFIRMED_FIELD and not allow_unconfirmed:
            raise SystemExit(
                f"field {n} is above the highest CONFIRMED W-2 field number "
                f"({MAX_CONFIRMED_FIELD}) — numbers past that are not legible on the "
                f"reference screen. Pass --allow-unconfirmed if you have verified it.")
        if is_w2 and n in PROTECTED_W2_FIELDS and not allow_protected:
            raise SystemExit(
                f"field {n} is protected (1/2/3 are header dropdowns with unconfirmed typed "
                f"behaviour; 4 is the employer EIN, which auto-fills and auto-advances). "
                f"Pass --allow-protected if you really mean it.")
    return entries


def _write_halt_dump(driver, path: str = "env-dump-halt.json") -> None:
    """On any halt, capture the full window topology automatically — the artifact that
    diagnoses a bad halt (which window, what class/style, who owned the keyboard) without
    a manual env-dump round-trip. Best-effort: a dump failure never masks the halt."""
    try:
        dump = driver.dump_windows()
        if dump.get("ok"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, default=str)
            print(f"  · window topology at halt -> {path} (paste this file if the halt looks wrong)")
    except Exception:
        pass


def _seq_entries(args):
    """Entries for the `headsdown` command: --field (repeatable, comma-safe) or --seq."""
    kw = dict(screen=getattr(args, "screen", "") or "",
              allow_unconfirmed=getattr(args, "allow_unconfirmed", False),
              allow_protected=getattr(args, "allow_protected", False))
    if getattr(args, "field", None):
        entries = []
        for spec in args.field:
            if "=" not in spec:
                raise SystemExit(f"--field expects NUM=VALUE, got {spec!r}")
            num, val = spec.split("=", 1)
            entries.append({"field_no": num.strip(), "value": val.strip(), "label": ""})
        return _validate_entries(entries, **kw)
    return _parse_seq(args.seq, **kw)


def _headsdown_run(driver, entries, *, method, settle_after=0.0):
    """Enter a list of {field_no, value} BY FIELD NUMBER through the heads-down popup, using
    the race-free VERIFIED driver (per field: focus the popup edit, watch the number settle
    before Enter, read Drake's own prompt to confirm it took the number, type the value,
    then PROVE the value was accepted). STOPS at the first field that does not return ok —
    no cascade, nothing committed past the failure. Requires an ACTIVE caret to bootstrap
    (a click, or a prior jump).

    Returns (rows, halted, reason); rows are dicts so the driver's per-field diagnostics
    survive to the report. They used to be dropped, which mattered once the EIN can be
    skipped: with field 4 out of the batch, a popup that vanishes after a value no longer
    means "the known EIN auto-advance" — it means some OTHER field auto-advanced, which is
    now the single most interesting anomaly the run can surface."""
    import time
    driver.begin_batch()  # a popup inherited from a previous run is foreign, not ours
    rows = []
    halted, reason = False, None
    for e in entries:
        num, val = str(e["field_no"]), str(e["value"])
        # `kind` travels with the entry so the driver knows a Box 13 tick box from a money
        # box BEFORE it types. --seq entries carry no kind; there the driver asks the popup.
        t0 = time.time()
        res = driver.headsdown_type(num, val, method=method, settle_after=settle_after,
                                    kind=e.get("kind"))
        # Per-field wall clock. "It feels slow" is not something you can optimise; a column
        # of seconds tells you WHICH field and therefore which gate is costing the time.
        secs = time.time() - t0
        rows.append({"secs": secs,
                     "num": num, "val": val, "label": e.get("label", ""),
                     "ok": bool(res.get("ok")), "reason": res.get("reason"),
                     "model": res.get("model"), "committed": res.get("value_committed"),
                     "evidence": res.get("commit_evidence"), "read_back": res.get("read_back"),
                     "attempts": res.get("popup_attempts"), "checkbox": res.get("checkbox"),
                     "popup_after": res.get("popup_after_value")})
        if not res.get("ok"):
            halted, reason = True, res.get("reason")
            _write_halt_dump(driver)
            break
    # Commit the final field ONLY if every field landed AND no dialog is up — never press
    # Enter over a corrupted/halted state. (In the persistent model each value was already
    # committed with its own Enter; this closes out the last canvas field in the per-jump one.)
    if (not halted and rows and rows[-1]["model"] == "per-jump"
            and driver._detect_unexpected_dialog() is None):
        driver.press(["Enter"])
    return rows, halted, reason


def _print_seq_outcome(rows, halted, reason, plan=None) -> int:
    """Print the per-field result of a heads-down run and return an exit code.

    Exit codes: 0 clean, 2 halted, 3 completed but something was REJECTED and is still
    blank in Drake. 3 exists because a run that entered 20 of 22 fields is not the same
    outcome as one that entered all 22, and "All fields entered" printed unconditionally
    was how that difference got lost.

    `plan` is passed only by write-w2 (the --seq paths have no plan), so the plan-free
    callers keep their accurate, narrower wording."""
    print("\nPer-field result (verified):")
    for r in rows:
        mark = "OK " if r["ok"] else "HALT"
        name = f" {r['label']}" if r["label"] else ""
        if r["ok"]:
            # Show what Drake actually had in the box, not what we meant to type.
            extra = f"   (read back {r['read_back']!r})" if r.get("read_back") else ""
        else:
            extra = f"   <-- {r['reason']}"
        secs = f"{r['secs']:5.1f}s " if r.get("secs") is not None else ""
        print(f"  [{mark}] {secs}field {r['num']:<3}{name:<34} = {r['val']!r}{extra}")

    models = {r["model"] for r in rows if r["model"]}
    if models:
        print(f"\nheads-down model observed: {', '.join(sorted(models))}")

    # Anomalies worth a human's eye even on a clean run. Narrow on purpose: popup_after is
    # structurally False for EVERY field under per-jump, and the first field legitimately
    # costs one Ctrl+N attempt.
    dropped = [r["num"] for r in rows
               if r["model"] == "persistent" and r.get("committed")
               and r.get("popup_after") is False and str(r["num"]) != "4"]
    swallowed = [r["num"] for r in rows if (r.get("attempts") or 0) >= 2]
    if dropped:
        print(f"\nNOTE: field(s) {', '.join(dropped)} dropped heads-down after committing — "
              f"that is the signature of Drake AUTO-FILLING from that field (field 4/EIN is "
              f"the known one). Entry recovered, but check what those fields populated.")
    if swallowed:
        print(f"NOTE: Ctrl+N had to be retried on field(s) {', '.join(swallowed)} — Drake was "
              f"busy committing. Recovered; informational.")

    # Checkbox fields are the one place a value is proven by reading a WIDGET rather than
    # text, so say which channel proved it and which token this build actually takes —
    # that is the line to copy into binding.json after the first successful run.
    checks = [r for r in rows if r.get("checkbox")]
    if checks:
        for r in checks:
            cb = r["checkbox"]
            tok = cb.get("tokens_tried") or []
            how = (f"token {tok[-1]!r}" if tok else "no keystroke needed (already in state)")
            print(f"NOTE: field {r['num']} checkbox -> "
                  f"{'ticked' if cb.get('desired') else 'cleared'} via {how}, confirmed by "
                  f"{cb.get('verified_by')}"
                  f"{'; arrived already ' + ('ticked' if cb.get('arrival') else 'clear') if cb.get('arrival') is not None else ''}.")
        pixel_only = [r["num"] for r in checks if r["checkbox"].get("verified_by") == "pixel"]
        if pixel_only:
            print(f"      field(s) {', '.join(pixel_only)} were confirmed by the SCREEN GLYPH "
                  f"only (Drake exposed no checkbox to accessibility) — the tick was seen, "
                  f"but check those boxes on the screenshot.")
        multi = sorted({tuple(r["checkbox"].get("tokens_tried") or []) for r in checks})
        if any(len(t) > 1 for t in multi):
            print(f"      more than one token was needed — pin the one that worked in "
                  f"binding.json as navigation.headsdown_checkbox_true to save the retries.")

    if halted:
        print(f"\nSTOP — human needed. The batch halted BEFORE mis-entering: {reason}")
        print("Nothing was committed past the failure. Review Drake, then we adjust.")
        print("A clean halt is the design working — not a cascade.")
        print("  · window topology at the halt is in env-dump-halt.json")
        return 2

    entered = sum(1 for r in rows if r["ok"])
    rejected = [s for s in (plan or {}).get("skipped", []) if s.get("rejected")]
    held = (plan or {}).get("skipped_by_request", [])
    not_here = (plan or {}).get("not_on_screen", [])
    total = entered + len(rejected)
    print(f"\n{entered} of {total} field(s) entered and verified by number — no cascade, "
          f"no error dialog.")
    if held:
        print(f"{len(held)} field(s) were held back BY REQUEST and are untouched: "
              f"{', '.join(str(e['field_no']) for e in held)}")
    for e in not_here:
        print(f"NOT ENTERED — {e['key']} has no box on this screen: {e['why']}")
    for e in (plan or {}).get("hand_entry", []):
        print(f"BY HAND — field {e['field_no']} {e['label']}: {e['value']!r} was NOT typed. "
              f"{e['why']}")
    if rejected:
        print(f"\n{len(rejected)} field(s) were REJECTED and are STILL BLANK in Drake — "
              f"enter them by hand:")
        for s in rejected:
            print(f"    field {s['field_no']:<4} {s['label']:<32} raw={s['raw']!r}")
        print("Values are IN Drake but NOT filed: a human still reviews and executes.")
        return 3
    print("Values are IN Drake but NOT filed: a human still reviews and executes.")
    return 0


def cmd_headsdown(args) -> int:
    """
    THE precision path: address Drake fields BY NUMBER via heads-down data entry — no
    pixel clicks, RACE-FREE and VERIFIED. Each field: focus the popup's real edit box,
    read the number back before pressing Enter, prove the jump, then type the value on the
    canvas — HALTing on any anomaly instead of cascading. Needs one active caret to
    bootstrap: --manual (you click a field first) is the confirmed path; the auto path
    opens the screen by code but may not leave an active caret (then Ctrl+N no-ops — use
    --manual). With no --seq it just opens the popup + screenshots so you can read the
    field numbers off the PNG. Internal proof only; never files.
    """
    import time
    # Resolve and VALIDATE the sequence BEFORE connecting to Drake and before the
    # countdown. A mis-numbered or truncated entry has to surface while it costs nothing —
    # not after the operator has already clicked into a live return and taken their hands
    # off the keyboard.
    entries = _seq_entries(args) if (args.seq or getattr(args, "field", None)) else None
    if entries:
        print("\nWill enter, in this order:")
        for e in entries:
            print(f"   field {e['field_no']:<4} = {e['value']!r}")

    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    if driver.w32 is None:
        print("  ⚠ could not open the win32 popup connection — heads-down entry needs it.")

    # --manual: YOU click a field first (active caret), the agent drives the popup. No
    # open_screen, no agent foreground — your click owns the focus. The proven bootstrap.
    if args.manual:
        print("\nMANUAL-FOCUS — click into any Drake W-2 field NOW so its cursor is blinking")
        print("(an ACTIVE caret), then take your hands off the keyboard.")
        print(f"Agent drives heads-down (method={args.toggle_method}) in ", end="", flush=True)
        for n in range(max(1, args.delay), 0, -1):
            print(f"{n}… ", end="", flush=True)
            time.sleep(1)
        print()
        if args.seq:
            rows, halted, reason = _headsdown_run(driver, entries,
                                                  method=args.toggle_method)
            code = _print_seq_outcome(rows, halted, reason)
            s = driver.save_screenshot(args.shot)
            print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
            return code
        r = driver.headsdown_toggle(method=args.toggle_method)
        print(f"headsdown_toggle -> {json.dumps(r)}")
        s = driver.save_screenshot(args.shot)
        print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
        print("\nVERDICT: numbers appeared -> read me the number on each field you care about.")
        return 0

    print(f"Opening screen {args.screen!r} by code (Selector — no mouse)…")
    r = driver.open_screen(args.screen)
    if not r.get("ok"):
        print(f"open_screen failed: {r.get('error')}", file=sys.stderr)
        return 1
    time.sleep(args.settle)
    if args.seq:
        print(f"Entering values BY FIELD NUMBER (method={args.toggle_method})…")
        rows, halted, reason = _headsdown_run(driver, entries,
                                              method=args.toggle_method)
        code = _print_seq_outcome(rows, halted, reason)
        s = driver.save_screenshot(args.shot)
        print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
        if halted and "did not open" in (reason or ""):
            print("(A code-opened screen may not leave an active caret to bootstrap Ctrl+N — use --manual.)")
        return code
    print(f"Toggling HEADS-DOWN (Ctrl+N, method={args.toggle_method}) — a NUMBER should appear…")
    r = driver.headsdown_toggle(method=args.toggle_method)
    time.sleep(args.settle)
    s = driver.save_screenshot(args.shot)
    print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
    print("\nVERDICT: numbers appeared -> read me the field numbers. Nothing? a code-opened")
    print("screen may leave no active caret for Ctrl+N — use --manual (click first).")
    return 0


def cmd_write_w2(args) -> int:
    """
    Extracted W-2 JSON -> Drake, end to end. Reads the LLM's structured output, resolves it
    through the fixed field map (w2_map.py), and enters every field BY NUMBER through the
    verified heads-down path.

    --dry-run prints the fully-resolved plan and touches nothing — run that FIRST, and on
    any machine: it shows exactly which box each value goes in, what was skipped, and what
    needs a human eye, before a single keystroke reaches Drake. Never files.
    """
    import time
    from w2_map import build_plan, format_plan

    with open(args.json, "r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    binding = load_binding(args.binding) if not args.dry_run else _try_load_binding(args.binding)
    token = (binding.get("navigation", {}) or {}).get("headsdown_checkbox_true", "X")
    plan = build_plan(payload, checkbox_token=token, include_zeros=args.include_zeros,
                      skip_fields=args.skip_field, ts=args.ts)
    print(format_plan(plan))

    if not plan["entries"]:
        print("Nothing to enter — the payload resolved to zero fields.\n", file=sys.stderr)
        return 1
    if args.dry_run:
        print("DRY RUN — nothing was sent to Drake. Re-run without --dry-run to enter these.\n")
        return 0

    # Abort BEFORE connecting if the extraction produced a value we could not resolve. The
    # run would otherwise enter everything else and then report unqualified success, with
    # the bad field silently blank. Nothing has been typed yet, so stopping here is free.
    rejected = [s for s in plan["skipped"] if s.get("rejected")]
    if rejected and not args.allow_rejected:
        print(f"REFUSING TO START: {len(rejected)} extracted value(s) could not be resolved to "
              f"a valid entry:", file=sys.stderr)
        for s in rejected:
            print(f"    field {s['field_no']:<4} {s['label']:<32} raw={s['raw']!r}", file=sys.stderr)
        print("Fix the extraction, or pass --allow-rejected to enter the rest and key these "
              "by hand.\n", file=sys.stderr)
        return 1

    driver = DrakeDriver(binding)
    driver.connect()
    wi = driver.window_info()
    print(f"Bound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    if driver.w32 is None:
        print("  ⚠ could not open the win32 popup connection — heads-down entry needs it.")
        return 1
    print(f"\nOpen the W-2 screen for this employer, then CLICK into any field on it so its")
    print("cursor is blinking (that active caret is what lets Ctrl+N arm heads-down).")
    print("Then take your hands off the keyboard. Entry starts in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print()

    rows, halted, reason = _headsdown_run(driver, plan["entries"],
                                          method=args.toggle_method,
                                          settle_after=args.settle_after)
    code = _print_seq_outcome(rows, halted, reason, plan=plan)

    # THE FORM-LEVEL CHECK. Everything above verifies what the POPUP showed, which is what
    # we typed — and that is not the same as what Drake KEPT. Live proof (2026-08-04):
    # 'DALLAS' typed into an empty Box 20 locality dropdown echoed back in the popup, took
    # the Enter, and left the box on the form empty. Four fields reported OK and wrote
    # nothing. So the last thing a run does is read the canvas back and say which planned
    # values are not on it.
    entered = [r for r in rows if r["ok"]]
    audit = driver.audit_canvas([e for e in plan["entries"]
                                 if str(e["field_no"]) in {r["num"] for r in entered}])
    print()
    if audit.get("canvas_elements"):
        if audit["ok"]:
            print(f"FORM CHECK: all {audit['checked']} entered value(s) are readable on the "
                  f"data-entry form ({audit['canvas_elements']} controls read).")
        else:
            print(f"FORM CHECK — {len(audit['missing'])} value(s) reported entered are NOT on "
                  f"the form. Drake took the keystrokes and kept nothing:")
            for e in audit["missing"]:
                print(f"    field {e['field_no']:<4} {e['label']:<32} = {e['value']!r}")
            print("    A dropdown with no matching entry does this: it accepts the typing,")
            print("    echoes it in the popup, and stores nothing. Enter these by hand.")
            code = max(code, 3)
    else:
        print(f"FORM CHECK: unavailable — {audit.get('reason')}")
    s = driver.save_screenshot(args.shot)
    print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
    print("\nVERIFY THE SCREENSHOT before doing anything else in Drake. This agent has no")
    print("file/e-file command by design — a human reviews and executes, always.")
    return code


def _try_load_binding(path: str) -> dict:
    """Binding if it's there, else {} — so --dry-run works on a machine that has no
    binding.json (it's per-VM and gitignored). Only affects the checkbox token."""
    try:
        return load_binding(path)
    except Exception:
        return {}


def cmd_probe_popup(args) -> int:
    """READ-ONLY diagnostic that dumps the heads-down popup's real control tree, tests a
    set_edit_text round-trip, and reports whether the popup closes after Enter (value ->
    canvas) or persists (value -> popup). Run this ONCE first: it locks in the exact edit
    control + model so the entry driver binds correctly. Writes no field value."""
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    print(f"win32 popup connection: {'OK' if driver.w32 is not None else 'FAILED'}")
    print("\nCLICK into any Drake W-2 field so its cursor is blinking, then hands off.")
    print("Probing in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print()
    res = driver.probe_headsdown_popup(probe_field=args.probe_field)
    print("\n===== HEADS-DOWN POPUP PROBE =====")
    print(json.dumps(res, indent=2, default=str))
    print("==================================")
    if res.get("edit_resolution"):
        # The box could not be identified — nothing was typed. popup_controls (above) is the
        # answer: it lists what the popup actually contains, class names included.
        print("\n⚠ the popup's TEXT BOX could not be identified, so nothing was typed.")
        print(f"  {(res.get('edit_resolution') or {}).get('why')}")
        print("  popup_controls above lists the children Drake really has. If there are")
        print("  some, set navigation.headsdown_popup_edit_class to the class of the box")
        print("  (the one that is not a Static label). If the list is empty, Drake paints")
        print("  the box and the popup must hold the keyboard — click into Drake first.")
        return 2
    print(f"\nmodel:            {res.get('model')}")
    print(f"edit control:     {res.get('edit_class_name')!r} "
          f"(hwnd={res.get('edit_handle')}, matched by {res.get('edit_resolved_by')})")
    for name, r in (res.get("read_channels") or {}).items():
        t = r.get("text")
        print(f"  channel {name:<6} {'-> ' + repr(t[:70]) if t else '(nothing)'}"
              f"{'  [' + r['error'][:80] + ']' if r.get('error') else ''}")
    print(f"read-back:        {res.get('read_back')}")
    print(f"number prompt:    {res.get('prompt_at_number')!r}")
    print(f"value prompt:     {res.get('prompt_at_value')!r}")
    print(f"refusal detection: {res.get('refusal_detection')}")
    dis = res.get("disarm") or {}
    print(f"disarm:           {dis.get('note')}")
    if res.get("popup_open_after_disarm"):
        print("\n⚠ Drake is STILL ARMED — press Esc in Drake before the next run, or the "
              "next field number will be committed as a value. (The entry driver will "
              "refuse to start in that state rather than mis-enter.)")
    return 0 if res.get("ok") else 2


def cmd_probe_checkbox(args) -> int:
    """Calibration probe for a CHECKBOX field (Box 13: 46/47/48).

    Drake's heads-down popup does NOT give a checkbox field a text box — it shows the tick
    box itself, so entry has to read the WIDGET, not the typed character. This probe reports
    what this build actually exposes (accessibility Toggle state, the on-screen glyph, or
    neither), what state the box arrives in, and which token flips it. It ticks and unticks
    to measure that, then leaves with Esc — it never presses Enter, so nothing is committed.
    Verify the box by eye afterwards regardless."""
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    print(f"win32 popup connection: {'OK' if driver.w32 is not None else 'FAILED'}")
    print(f"tokens to try: {driver.checkbox_tokens}   channels: {driver.checkbox_channels}")
    print("\nOpen the W-2 screen, CLICK into any field so its cursor is blinking, then hands off.")
    print("Probing in ", end="", flush=True)
    for n in range(max(1, args.delay), 0, -1):
        print(f"{n}… ", end="", flush=True)
        time.sleep(1)
    print()
    res = driver.probe_checkbox_field(field_no=args.field_no, flip=not args.no_flip)
    print("\n===== CHECKBOX FIELD PROBE =====")
    print(json.dumps(res, indent=2, default=str))
    print("================================")
    if not res.get("ok"):
        print(f"\n⚠ {res.get('reason')}")
        return 2
    print(f"\nfield:            {res.get('field_no')}   (value prompt: {res.get('prompt_at_value')!r})")
    print(f"UIA checkboxes:   {res.get('uia_channel')}")
    for el in (res.get("uia_checkboxes") or []):
        print(f"    {el.get('name')!r}  state={el.get('state')}  rect={el.get('rect')}")
    print(f"screen glyph:     {'a ticked box is visible' if res.get('pixel_tick_visible') else 'no tick visible'}"
          f"{'  [' + str(res.get('pixel_error'))[:70] + ']' if res.get('pixel_error') else ''}")
    a = res.get("arrival_state") or {}
    print(f"arrival state:    {'ticked' if a.get('ticked') else 'clear'} "
          f"(read={a.get('read')}, via {a.get('channel')})")
    if a.get("ticked"):
        print("    ^ Drake arrives with this box ALREADY TICKED. Entry types nothing in that "
              "state — a token there would clear it.")
    for t in (res.get("token_trials") or []) if isinstance(res.get("token_trials"), list) else []:
        print(f"    token {t['token']!r:<10} {'FLIPPED it' if t['flipped'] else 'did nothing'} "
              f"(state after: {t['state_after']}, via {t['channel']})")
    if res.get("token_that_worked"):
        print(f"token to use:     {res['token_that_worked']!r}  ({res.get('token_behaviour')})")
        print(f"    put it in binding.json as navigation.headsdown_checkbox_true")
        r = res.get("restored") or {}
        if not r.get("ok"):
            print("    ⚠ the box was NOT restored to how it was found — it is showing a "
                  "PENDING state. Esc was sent and nothing was committed, but LOOK at box 13 "
                  "in Drake before doing anything else.")
    print(f"\nverdict:          {res.get('verdict')}")
    dis = res.get("disarm") or {}
    print(f"disarm:           {dis.get('note')}")
    if res.get("popup_open_after_disarm"):
        print("\n⚠ Drake is STILL ARMED — press Esc in Drake before the next run.")
    return 0


def cmd_envdump(args) -> int:
    """Dump the FULL window topology of the live Drake process to JSON: every top-level
    window with class/style/owner/enabled/visible, its children, where the keyboard would
    land, and the structural gate's verdict (role) per window. This is the env-dump the
    dialog gate is built on — run it whenever a halt names a window that looks benign, and
    paste the file. --delay gives you time to arrange the state you want captured (e.g.
    click a field and open heads-down first)."""
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}  (hwnd={wi.get('handle')})")
    if args.delay:
        print("Capturing in ", end="", flush=True)
        for n in range(args.delay, 0, -1):
            print(f"{n}… ", end="", flush=True)
            time.sleep(1)
        print()
    dump = driver.dump_windows()
    if not dump.get("ok"):
        print(f"dump failed: {dump.get('error')}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"\nWrote {args.out}. Top-level windows of the Drake process:")
    for w in dump["windows"]:
        if not w.get("visible"):
            continue
        r = w.get("rect") or [0, 0, 0, 0]
        print(f"  [{w.get('role', '?'):>16}] {w.get('title')!r:<48} class={w.get('class_name')}"
              f"  {r[2]}x{r[3]}  enabled={w.get('enabled')}  dialogish={w.get('dialogish')}")
    kt = dump.get("keyboard_target") or {}
    print(f"\nkeyboard lands in: {kt.get('root_title')!r} (class={kt.get('root_class')}, hwnd={kt.get('root')})")
    blocker = dump.get("blocker")
    print(f"main frame enabled: {dump.get('main_enabled')}   "
          f"blocker: {blocker.get('title') if blocker else 'none'}")
    return 0


def _values_match(got, exp) -> bool:
    """Compare a read-back to the expected value. Delegates to the driver's comparator so
    selftest and live entry apply the SAME rule — the local alnum-only copy that used to
    live here accepted '52000' for an expected '520.00', which is the 100x error the driver
    now refuses."""
    from drake_driver import _same_value
    return _same_value(got, exp)


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
        method = req.get("method")
        result = reply.get("result")
        exp = req.get("_expect")  # optional inline assertion on a readField
        note = ""
        # An ACTION step (anything but readField) that errors or comes back ok:false is a
        # real failure — e.g. a click/focus that couldn't land, or a type into no caret.
        # Count it so a run where nothing actually typed can NEVER report PASS.
        if not args.dry_run and method != "readField" and (
                "error" in reply or (isinstance(result, dict) and result.get("ok") is False)):
            failures += 1
            note = "  <-- STEP FAILED"
        elif exp is not None and args.dry_run:
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
        print(f"\n{failures} FAILURE(S) — step failed or read-back MISMATCH{shot_note}\n")
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


def _set_dpi_aware() -> None:
    """Make the process DPI-aware so window rectangles, screenshots, and click
    coordinates all use the SAME physical-pixel space. On a scaled display (125%/150%)
    without this, the coords you read off a screenshot don't match where click_input
    actually clicks — the agent clicks empty space and nothing types. Windows-only;
    a no-op (silently) elsewhere. Must run before pywinauto attaches."""
    try:
        import ctypes
        try:
            # PER_MONITOR_AWARE_V2 (Win10 1703+): the strongest awareness. Screenshots,
            # window rects, GetCursorPos and click coords ALL share one physical-pixel
            # space, so a screenshot pixel IS a click pixel — no 125%/150% scaling drift
            # (the "agent clicks empty space, nothing types" symptom), no LOGPIXELS fudge.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1 (Win 8.1+)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()  # system aware (Vista+)
    except Exception:
        pass


def _utf8_console() -> None:
    """Make this process's output UTF-8, replacing anything the console cannot spell.

    Windows gives a REDIRECTED stdout the legacy ANSI codepage, so `agent.py write-w2 …
    > run.log` — keeping the audit artifact, i.e. the normal thing to do with a run you
    care about — turns every '⚠' in a message into a UnicodeEncodeError. Inside the entry
    loop that surfaces as a HALT with a charmap error in place of the real reason, on a
    field that entered perfectly. errors='replace' so a missing glyph costs a '?' and
    never a run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _utf8_console()
    _set_dpi_aware()
    p = argparse.ArgumentParser(description="Fynn Drake agent (pywinauto). Never files.")
    # --binding lives on a shared parent so it works AFTER the subcommand too,
    # e.g. `agent.py probe --binding binding.json`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--binding", default="binding.json", help="path to binding.json")
    sub = p.add_subparsers(dest="mode", required=True)

    sp = sub.add_parser("probe", parents=[common]); sp.set_defaults(func=cmd_probe)
    scl = sub.add_parser("clip", parents=[common]); scl.add_argument("--delay", type=int, default=5, help="seconds to click into a Drake field before the copy fires"); scl.set_defaults(func=cmd_clip)
    sst = sub.add_parser("shoot", parents=[common]); sst.add_argument("--out", default="drake.png", help="where to save the window PNG"); sst.add_argument("--grid", action="store_true", help="overlay a labeled pixel grid to read click_xy/ocr_box coordinates by eye"); sst.set_defaults(func=cmd_shoot)
    stt = sub.add_parser("typetest", parents=[common]); stt.add_argument("--text", default="52000", help="value to type into the field you click; a COMMA-separated list cascades through fields (e.g. 11111,22222,33333)"); stt.add_argument("--click-xy", dest="click_xy", help="AGENT clicks this window-relative x,y first (e.g. 420,180), then types — diagnoses whether the programmatic click lands"); stt.add_argument("--advance", default="", help="key pressed between values when --text is a list, e.g. ENTER (default) or TAB"); stt.add_argument("--delay", type=int, default=15, help="seconds to click into a Drake field before typing fires"); stt.add_argument("--shot", help="save a screenshot here after typing"); stt.add_argument("--unicode", action="store_true", help="force the modern Unicode-packet keystroke method (default is legacy scancode/VK, which Drake needs)"); stt.set_defaults(func=cmd_typetest)
    shd = sub.add_parser("headsdown", parents=[common]); shd.add_argument("--screen", default="W2", help="Drake screen code to open by keyboard, e.g. W2"); shd.add_argument("--field", action="append", metavar="N=VALUE", help="one field to enter, repeatable — the comma-safe alternative to --seq (e.g. --field '5=ACME, INC')"); shd.add_argument("--allow-unconfirmed", dest="allow_unconfirmed", action="store_true", help="permit W-2 field numbers above the highest confirmed one (87)"); shd.add_argument("--allow-protected", dest="allow_protected", action="store_true", help="permit the protected W-2 header fields 1/2/3 and the EIN (4)"); shd.add_argument("--seq", help='comma list of fieldNo=value to type BY NUMBER, e.g. "5=TEST EMPLOYER,23=52000,24=6000" (numbers are Drake heads-down field numbers — see w2_map.W2_FIELD_MAP; a value containing a comma needs --field)'); shd.add_argument("--toggle-method", dest="toggle_method", choices=["scancode", "vkhold", "pywinauto"], default="scancode", help="how Ctrl+N is injected: scancode (low-level hardware keys, default/best for Drake), vkhold (pywinauto Ctrl-held), pywinauto (high-level ^n)"); shd.add_argument("--shot", default="heads.png", help="screenshot after toggling/typing (read the field numbers off it)"); shd.add_argument("--settle", type=float, default=0.6, help="seconds to wait after open and after Ctrl+N"); shd.add_argument("--manual", action="store_true", help="YOU click a field first (active caret), then the agent drives heads-down by number — the confirmed-working bootstrap"); shd.add_argument("--delay", type=int, default=8, help="seconds to click into a Drake field before entry fires, in --manual mode"); shd.set_defaults(func=cmd_headsdown)
    spp = sub.add_parser("probe-popup", parents=[common]); spp.add_argument("--delay", type=int, default=8, help="seconds to click into a Drake field before the probe runs"); spp.add_argument("--probe-field", dest="probe_field", default="6", help="which field number the probe jumps to. Default 6 (employer 'Name cont.' — normally empty and inert). NEVER use 4: it is the EIN, it auto-fills, and it is the do-not-touch box"); spp.set_defaults(func=cmd_probe_popup)
    spc = sub.add_parser("probe-checkbox", parents=[common]); spc.add_argument("--delay", type=int, default=8, help="seconds to click into a Drake field before the probe runs"); spc.add_argument("--field", dest="field_no", default="47", help="which CHECKBOX field to probe. Default 47 (Box 13 retirement plan); 46 statutory employee, 48 sick pay"); spc.add_argument("--no-flip", dest="no_flip", action="store_true", help="observe only — read the arrival state and leave without sending any token"); spc.set_defaults(func=cmd_probe_checkbox)
    sw2 = sub.add_parser("write-w2", parents=[common]); sw2.add_argument("--json", required=True, help="extracted W-2 JSON (the LLM's structured output — see w2_map.W2_SCHEMA_KEYS)"); sw2.add_argument("--dry-run", action="store_true", help="resolve and PRINT the plan without touching Drake — run this first, works anywhere"); sw2.add_argument("--skip-field", dest="skip_field", type=int, action="append", metavar="N", help="do NOT enter this field number, even if the extraction has a value for it; repeatable. Use --skip-field 4 to leave the employer EIN alone (it also avoids Drake's auto-fill + auto-advance)"); sw2.add_argument("--ts", choices=["T", "S"], help="whose W-2 this is (field 1). Drake defaults to T; on a JOINT return an unset TS files the spouse's W-2 under the taxpayer"); sw2.add_argument("--allow-rejected", dest="allow_rejected", action="store_true", help="proceed even if some extracted values could not be resolved (they stay blank in Drake for you to key by hand)"); sw2.add_argument("--include-zeros", action="store_true", help="also enter money fields that are zero (default: skip — a blank box is zero on a tax form)"); sw2.add_argument("--toggle-method", dest="toggle_method", choices=["scancode", "vkhold", "pywinauto"], default="scancode", help="how Ctrl+N is injected (default scancode — what Drake accepts)"); sw2.add_argument("--settle-after", dest="settle_after", type=float, default=0.15, help="seconds to let Drake settle after each field (auto-fill/validation)"); sw2.add_argument("--delay", type=int, default=10, help="seconds to click into the W-2 screen before entry fires"); sw2.add_argument("--shot", default="w2-after.png", help="screenshot saved after the run — the verification artifact"); sw2.set_defaults(func=cmd_write_w2)
    sev = sub.add_parser("envdump", parents=[common]); sev.add_argument("--out", default="env-dump.json", help="where to write the window-topology JSON"); sev.add_argument("--delay", type=int, default=0, help="seconds before capture — time to click a field / open heads-down first"); sev.set_defaults(func=cmd_envdump)
    sc = sub.add_parser("calibrate", parents=[common]); sc.add_argument("--screen"); sc.set_defaults(func=cmd_calibrate)
    ss = sub.add_parser("selftest", parents=[common]); ss.add_argument("--plan", default="selftest.plan.json"); ss.add_argument("--dry-run", action="store_true"); ss.add_argument("--slow", action="store_true", help="slower keystrokes + pauses so you can watch Drake"); ss.add_argument("--shot", help="save a window screenshot here after the run (human-verify floor / OCR-box source)"); ss.set_defaults(func=cmd_selftest)
    scn = sub.add_parser("connect", parents=[common]); scn.add_argument("--url"); scn.add_argument("--token"); scn.set_defaults(func=cmd_connect)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
