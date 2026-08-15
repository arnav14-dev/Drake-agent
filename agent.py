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

from drake_driver import DrakeDriver, DrakeNotRunning
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


# --- which form is this? -----------------------------------------------------------------
# A payload names its Drake screen and this decides which field map plans it. Adding a form
# is one row here plus its map module — nothing else in the entry path knows how many forms
# exist, which is the only way the W-2's gates keep protecting the ones added after it.
#
# The nouns are for the operator, not the code: a message that says "W-2" while the 1099
# screen is on the monitor is a message nobody trusts.
_FORMS = {
    "W2": {"module": "w2_map", "label": "W-2",
           "id_key": "employer_ein", "id_noun": "employer EIN", "amount_noun": "wages"},
    "INT": {"module": "int_map", "label": "1099-INT",
            "id_key": "payer_tin", "id_noun": "payer TIN", "amount_noun": "interest income"},
    "DIV": {"module": "div_map", "label": "1099-DIV",
            "id_key": "payer_tin", "id_noun": "payer TIN", "amount_noun": "dividend income"},
    "1099": {"module": "r_map", "label": "1099-R",
             "id_key": "payer_tin", "id_noun": "payer TIN", "amount_noun": "pension income"},
    # NO id_key, and that is a real gap rather than an oversight. Every other screen dedupes
    # on the PAYER, but Social Security's payer is the government — it is the same on every
    # SSA-1099 there will ever be. What makes a second record legitimate here is a different
    # PERSON (field 1, T or S), and that is a letter, not an id the duplicate check can
    # normalise. So this screen has no automatic protection against the same person's
    # statement going in twice, which would double their benefits. The operator checks TS.
    "SSA": {"module": "ssa_map", "label": "SSA-1099",
            "id_key": None, "id_noun": "beneficiary", "amount_noun": "Social Security benefits"},
    # The LENDER is this screen's identity, not a payer. A client who refinanced mid-year
    # genuinely receives two 1098s from two lenders; the same lender twice is one statement
    # keyed twice, which doubles their mortgage interest deduction.
    "1098": {"module": "m1098_map", "label": "Form 1098",
             "id_key": "lender_tin", "id_noun": "lender Fed ID", "amount_noun": "mortgage interest"},
}


def _form_for(screen: str) -> dict:
    """The form definition for a Drake screen code, or None if this agent cannot drive it.

    Refusing an unknown screen is the whole point. Every other layer downstream — the
    read-back gate, the form check — verifies that Drake ACCEPTED what it was given, and
    Drake accepts a field number on any screen. Only the map knows whether field 20 is Box
    1 interest or something else entirely, so a screen with no map must never reach it.
    """
    return _FORMS.get(str(screen or "").strip().upper())


def _load_form_map(screen: str):
    """Import the map module for a screen. Raises ValueError with a usable message."""
    form = _form_for(screen)
    if not form:
        raise ValueError(
            f"this agent has no verified field map for Drake screen "
            f"{str(screen).strip().upper()!r}. Screens it can drive: "
            f"{', '.join(sorted(_FORMS))}. Entering without a map would put field numbers "
            f"into boxes nobody has checked.")
    import importlib
    return importlib.import_module(form["module"]), form


def cmd_write_form(args) -> int:
    """Extracted JSON -> Drake, end to end, for ANY screen this agent has a map for.

    The same command as `write-w2` with the form chosen by the payload's `drake_screen`
    instead of assumed. It is the path a new form is brought up on: --dry-run resolves the
    whole plan and touches nothing (works on a machine with no Drake at all), then the live
    run navigates to the client and the screen itself, types every field by number, reads
    each one back, and finishes with the form check and a screenshot.

    Never files. There is no file/e-file command in this agent by design.
    """
    import time

    with open(args.json, "r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    screen = str(args.screen or payload.get("drake_screen") or "W2").upper()
    payload["drake_screen"] = screen
    try:
        form_map, form = _load_form_map(screen)
    except ValueError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 1

    binding = load_binding(args.binding) if not args.dry_run else _try_load_binding(args.binding)
    token = (binding.get("navigation", {}) or {}).get("headsdown_checkbox_true", "X")
    plan = form_map.build_plan(payload, checkbox_token=token, include_zeros=args.include_zeros,
                              skip_fields=args.skip_field, ts=args.ts)
    print(f"\nForm: {form['label']}  (Drake screen {screen})")
    print(form_map.format_plan(plan))

    if not plan["entries"]:
        print("Nothing to enter — the payload resolved to zero fields.\n", file=sys.stderr)
        return 1
    if args.dry_run:
        print("DRY RUN — nothing was sent to Drake. Re-run without --dry-run to enter these.\n")
        return 0

    # Abort BEFORE connecting if the extraction produced a value we could not resolve. The
    # run would otherwise enter everything else and report unqualified success with the bad
    # field silently blank. Nothing has been typed yet, so stopping here is free.
    rejected = [s for s in plan["skipped"] if s.get("rejected")]
    if rejected and not args.allow_rejected:
        print(f"REFUSING TO START: {len(rejected)} value(s) could not be resolved to a valid "
              f"entry:", file=sys.stderr)
        for s in rejected:
            print(f"    field {s['field_no']:<4} {s['label']:<38} raw={s['raw']!r}", file=sys.stderr)
        print("Fix the payload, or pass --allow-rejected to enter the rest and key these by "
              "hand.\n", file=sys.stderr)
        return 1

    driver = DrakeDriver(binding)
    driver.connect()
    wi = driver.window_info()
    print(f"Bound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    if driver.w32 is None:
        print("  could not open the win32 popup connection — heads-down entry needs it.",
              file=sys.stderr)
        return 1

    if args.manual:
        print(f"\nOpen the {form['label']} screen ({screen}) for this client, then CLICK into "
              f"any field on it so\nits cursor is blinking (that active caret is what lets "
              f"Ctrl+N arm heads-down).")
        print("Then take your hands off the keyboard. Entry starts in ", end="", flush=True)
        for n in range(max(1, args.delay), 0, -1):
            print(f"{n}… ", end="", flush=True)
            time.sleep(1)
        print()
    else:
        # The real path: the operator leaves Drake on its home screen and the agent opens
        # the client, the screen and the record itself — with the identity check, the screen
        # signature and the grid-mode check all in front of the first keystroke.
        navr = _navigate_for_payload(driver, payload, args)
        if not navr["ok"]:
            print(f"\nNAVIGATION STOPPED: {navr['reason']}\nNothing was typed.\n", file=sys.stderr)
            return 1
        if not driver._focus_canvas_field():
            print("\nCould not put the caret on a Drake data-entry box after navigating. "
                  "Nothing was typed.\n", file=sys.stderr)
            return 1

    rows, halted, reason = _headsdown_run(driver, plan["entries"],
                                          method=args.toggle_method,
                                          settle_after=args.settle_after)
    code = _print_seq_outcome(rows, halted, reason, plan=plan)

    # THE FORM-LEVEL CHECK. Everything above verifies what the POPUP showed, which is what
    # we typed — not what Drake KEPT. A dropdown with no matching entry accepts the typing,
    # echoes it back, and stores nothing; this screen has ten of them, none confirmed.
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
                print(f"    field {e['field_no']:<4} {e['label']:<38} = {e['value']!r}")
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


def _payload_target(payload: dict) -> dict:
    """Who and what this payload is for: {ssn, first, last, ein, screen, form}.

    `ssn` is the CLIENT — whose return this document belongs to. It is never a box on the
    document's own screen (a W-2 screen has no SSN box, an INT screen has no recipient
    block); it is what the agent matches the client on, and it arrives under whichever name
    that form's extractor uses.

    `ein` is the DEDUPE id — the employer on a W-2, the payer on a 1099. Entering the same
    one twice doubles a client's income and every read-back would still verify perfectly,
    so which key holds it comes from the form definition rather than being hard-coded.
    """
    g = lambda k: str(payload.get(k) or "").strip()
    first = lambda *keys: next((g(k) for k in keys if g(k)), "")
    screen = (g("drake_screen") or "W2").upper()
    form = _form_for(screen) or {}
    return {"ssn": first("client_ssn", "employee_ssn", "recipient_tin", "recipient_ssn"),
            "first": first("client_first_name", "employee_first_name", "recipient_first_name"),
            "last": first("client_last_name", "employee_last_name", "recipient_last_name"),
            "ein": g(form.get("id_key") or "employer_ein"),
            "screen": screen,
            "form": form or None}


def _navigate_for_payload(driver, payload: dict, args) -> dict:
    """Open the right client, the right screen, and the right RECORD. {ok, reason, steps}.

    Runs before a single key is typed, and every failure returns ok=False having typed
    nothing. This is where the agent stops being "types into whatever is in front of it"
    and starts being answerable for the target it chose — so it is also where the identity
    check lives, and the check is against Drake's own window title rather than our record
    of what we asked for.
    """
    import drake_nav as nav
    t = _payload_target(payload)
    steps: list = []
    out = {"ok": False, "reason": None, "steps": steps, "target": t}

    if not t["form"]:
        out["reason"] = (f"the payload asks for Drake screen {t['screen']!r}, which this "
                         f"agent has no verified field map for. Screens it can drive: "
                         f"{', '.join(sorted(_FORMS))}. Nothing was opened.")
        return out
    if not t["ssn"]:
        out["reason"] = (f"the payload has no client SSN, so the agent cannot tell which "
                         f"return this {t['form']['label']} belongs to")
        return out

    r = nav.open_client(driver, t["ssn"], first_name=t["first"], last_name=t["last"],
                        create=bool(getattr(args, 'create', False)), timeout=args.nav_timeout)
    steps.append({"step": "client", **{k: r.get(k) for k in ("ok", "reason", "step")}})
    if not r["ok"]:
        out["reason"] = r["reason"]
        out["not_found"] = bool(r.get("not_found"))
        return out
    out["return_title"] = r.get("title")
    if r.get("created"):
        out["created_client"] = True
        out["incomplete"] = r.get("incomplete")
        print(f"  NEW CLIENT CREATED — {r['reason']}")
        print(f"    still needs a human on screen 1: {', '.join(r.get('incomplete') or [])}")

    s = nav.open_screen(driver, t["screen"], timeout=args.nav_timeout)
    steps.append({"step": "screen", **{k: s.get(k) for k in ("ok", "reason", "step")}})
    if not s["ok"]:
        out["reason"] = s["reason"]
        return out

    # RE-BASELINE HERE, not after the record work. Opening a return and a screen creates
    # new top-level windows and disables the frames behind them — structurally identical to
    # a modal arriving. Everything below this line consults the dialog gate, and on
    # 2026-08-09 `form_new_record` read the return's own newly-created window as a blocker
    # and reported "Drake is asking a question" when Drake had asked nothing.
    #
    # It is safe exactly here and nowhere earlier: the return has been verified against
    # Drake's own title, and the screen against its heading. Anything appearing after this
    # still halts.
    driver.rebaseline(why="opening the return and its screen")

    state = driver.form_record_state()
    plan = nav.plan_record_use(state, t["ein"], allow_new=not args.no_new_record,
                               noun=t["form"]["label"], id_noun=t["form"]["id_noun"],
                               amount_noun=t["form"]["amount_noun"])
    steps.append({"step": "record", "action": plan["action"], "reason": plan["reason"],
                  "index": state.get("index"), "count": state.get("count"),
                  "populated": state.get("populated")})
    print(f"  record: {plan['reason']}")
    if plan["action"] == "refuse":
        out["reason"] = plan["reason"]
        return out
    if plan["action"] == "new":
        n = driver.form_new_record(timeout=args.nav_timeout)
        steps.append({"step": "new-record", **{k: n.get(k) for k in ("ok", "reason",
                                                                     "index", "count")}})
        if not n["ok"]:
            out["reason"] = n["reason"]
            return out
        print(f"  opened a new {t['form']['label']} record ({n['index']} of {n['count']})")
        out["record"] = {"index": n["index"], "count": n["count"]}
    else:
        out["record"] = {"index": state.get("index"), "count": state.get("count")}

    out["ok"] = True
    out["reason"] = f"{r['reason']} / {s['reason']}"
    return out


def _enter_one_payload(driver, payload: dict, args, *, token: str) -> dict:
    """Plan one extracted document and enter it. Returns a report; never files, never raises.

    Composes the SAME helpers `write-w2` uses — build_plan, _headsdown_run, audit_canvas —
    rather than reimplementing them, so the watcher cannot drift into a second, less-tested
    entry path. Every gate that protects a hand-run protects this one.

    WHICH map plans it comes from the payload's own `drake_screen`. A payload for a screen
    this agent has no map for is refused here having typed nothing — the read-back gates
    downstream cannot save it, because Drake will happily accept a field number on any
    screen and only the map knows what that number means.
    """
    screen = str((payload or {}).get("drake_screen") or "W2").upper()
    report: dict = {"ok": False, "halted": False, "entered": 0, "planned": 0, "reason": None,
                    "screen": screen}
    try:
        form_map, form = _load_form_map(screen)
    except ValueError as e:
        report["reason"] = str(e)
        return report
    build_plan, format_plan = form_map.build_plan, form_map.format_plan

    try:
        plan = build_plan(payload, checkbox_token=token, include_zeros=args.include_zeros,
                          skip_fields=None, ts=args.ts)
    except Exception as e:
        report["reason"] = f"the payload could not be planned: {type(e).__name__}: {e}"
        return report

    print(format_plan(plan))
    report["planned"] = len(plan["entries"])
    report["warnings"] = plan["warnings"]
    report["hand_entry"] = [{"field_no": h["field_no"], "label": h["label"],
                             "value": h["value"], "why": h["why"]} for h in plan["hand_entry"]]
    if not plan["entries"]:
        report["reason"] = "the payload resolved to zero enterable fields"
        return report

    # Same refusal as a hand-run: a value the map could not resolve would otherwise sit
    # blank while the run reported success. Nothing has been typed yet, so stopping is free.
    rejected = [s for s in plan["skipped"] if s.get("rejected")]
    if rejected and not args.allow_rejected:
        report["reason"] = (f"{len(rejected)} extracted value(s) could not be resolved: "
                            + ", ".join(f"field {s['field_no']} ({s['label']})" for s in rejected))
        report["rejected"] = [{"field_no": s["field_no"], "label": s["label"], "raw": s["raw"]}
                              for s in rejected]
        return report

    rows, halted, reason = _headsdown_run(driver, plan["entries"],
                                          method=args.toggle_method,
                                          settle_after=args.settle_after)
    code = _print_seq_outcome(rows, halted, reason, plan=plan)
    entered = [r for r in rows if r["ok"]]
    report.update({"entered": len(entered), "halted": bool(halted), "reason": reason,
                   "fields": [{"field_no": r["num"], "ok": r["ok"], "value": r.get("value"),
                               "reason": r.get("reason")} for r in rows]})

    # The form-level check: everything above verifies what the POPUP showed, which is what
    # we typed — not what Drake KEPT.
    audit = driver.audit_canvas([e for e in plan["entries"]
                                 if str(e["field_no"]) in {r["num"] for r in entered}])
    report["form_check"] = audit
    if audit.get("canvas_elements"):
        if audit["ok"]:
            print(f"\nFORM CHECK: all {audit['checked']} entered value(s) are readable on the "
                  f"data-entry form ({audit['canvas_elements']} controls read).")
        else:
            print(f"\nFORM CHECK — {len(audit['missing'])} value(s) reported entered are NOT on "
                  f"the form:")
            for e in audit["missing"]:
                print(f"    field {e['field_no']:<4} {e['label']:<32} = {e['value']!r}")
            code = max(code, 3)
    else:
        print(f"\nFORM CHECK: unavailable — {audit.get('reason')}")

    report["ok"] = (code == 0)
    return report


def cmd_watch(args) -> int:
    """Watch a folder for payloads and enter each one into Drake.

    Each payload names its own Drake screen and is planned with THAT screen's verified field
    map (see `_FORMS`). A payload naming a screen with no map is refused having typed
    nothing — the read-back gates below cannot catch a wrong SCREEN, because Drake accepts a
    field number on whatever form is in front of it.

    This is the transport between Fynn's backend and Drake. The backend writes
    `w2-<docId>.json` into the folder (atomically — it renames a `.part` file into place, so
    a half-written payload is never visible); this picks it up, enters it through the same
    verified heads-down path as `write-w2`, and moves it to `done/` or `failed/` with a
    `.report.json` beside it saying exactly what happened, field by field.

    A folder is the transport on purpose. It needs no inbound port on the firm's machine, no
    socket to keep alive, and it survives either side restarting — the queue is just files.

    ONE AT A TIME, and it STOPS on the first halt. Drake is a single keyboard and a halt
    means the screen is in a state a human has not seen; starting the next W-2 on top of
    that is how one bad return becomes five. `--keep-going` overrides that, and says so
    loudly. There is no file/e-file command here either — a human still reviews and executes.
    """
    import time
    from pathlib import Path

    root = Path(args.dir).expanduser()
    if not root.is_dir():
        print(f"watch folder does not exist: {root}", file=sys.stderr)
        return 1
    done_dir, failed_dir = root / "done", root / "failed"
    for d in (done_dir, failed_dir):
        d.mkdir(parents=True, exist_ok=True)

    binding = load_binding(args.binding)
    token = (binding.get("navigation", {}) or {}).get("headsdown_checkbox_true", "X")
    driver = DrakeDriver(binding)
    driver.connect()
    wi = driver.window_info()
    print(f"Bound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    if driver.w32 is None:
        print("  could not open the win32 popup connection — heads-down entry needs it.",
              file=sys.stderr)
        return 1

    print(f"\nWatching {root} for payloads ({', '.join(sorted(_FORMS))}). Leave Drake on its "
          f"home screen — the agent opens the client, the screen and the record itself."
          f"\nCtrl+C to stop.\n")
    processed = 0
    try:
        while True:
            # Oldest first, and NEVER a '.part' — the backend renames into place, so a name
            # ending .json is a file that is completely written.
            queue = sorted((p for p in root.glob("*.json") if not p.name.endswith(".part")),
                           key=lambda p: p.stat().st_mtime)
            for path in queue:
                print("=" * 74)
                print(f"ENTERING {path.name}")
                print("=" * 74)
                # UNATTENDED BOOTSTRAP. `write-w2` gets this for free: it tells a human to
                # click a Drake field, and that click both brings Drake to the FOREGROUND
                # and arms the caret. Nobody clicks for a watcher. Without this the popup is
                # up, holds the keyboard, passes every gate — and the keystrokes go to
                # whatever window is actually in front, so the run halts on field 1 with
                # "the popup never showed field number '1'". Focusing a real data-entry box
                # through UIA does both jobs at once, and is the same call the caret re-arm
                # uses. It changes no value; it only decides where the next key lands.
                navr = None
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                except Exception as e:
                    report = {"ok": False, "reason": f"unreadable JSON: {type(e).__name__}: {e}"}
                    payload = None
                else:
                    report = None

                # NAVIGATE FIRST. Until now this step was a human: they opened the client
                # and the W-2 screen, and the agent typed into whatever was in front of it.
                # Doing it in code is what lets an operator just open Drake — and it is
                # also the first time the agent is answerable for WHICH return it picked,
                # so it ends with an identity check against Drake's own title and refuses
                # on any mismatch. Nothing is typed until it passes.
                if payload is not None and not args.no_navigate:
                    navr = _navigate_for_payload(driver, payload, args)
                    if not navr["ok"]:
                        report = {"ok": False, "reason": navr["reason"],
                                  "navigation": navr["steps"], "entered": 0,
                                  "not_found": navr.get("not_found", False)}
                        print(f"\nNAVIGATION STOPPED: {navr['reason']}", file=sys.stderr)
                    else:
                        report = None

                if report is None:
                    # The caret Drake needs to arm heads-down. Navigation leaves the form
                    # open but not necessarily carrying a caret, and Ctrl+N is a silent
                    # no-op without one.
                    if not driver._focus_canvas_field():
                        report = {"ok": False, "entered": 0,
                                  "reason": "could not put the caret on a Drake data-entry "
                                            "box after navigating. Nothing was typed."}
                        print(f"\n{report['reason']}", file=sys.stderr)
                    else:
                        report = _enter_one_payload(driver, payload, args, token=token)
                        if navr is not None:
                            report["navigation"] = navr["steps"]
                            report["return_title"] = navr.get("return_title")
                            report["record"] = navr.get("record")
                processed += 1

                shot = driver.save_screenshot(str(root / f"{path.stem}.png"))
                if shot.get("ok"):
                    report["screenshot"] = shot["path"]
                    print(f"screenshot -> {shot['path']}")
                dest = (done_dir if report.get("ok") else failed_dir) / path.name
                path.replace(dest)
                (dest.with_suffix(".report.json")).write_text(
                    json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
                print(f"-> {dest}")
                print("Values are IN Drake but NOT filed: a human still reviews and executes.")

                if not report.get("ok") and not args.keep_going:
                    print("\nSTOPPING. That payload did not complete cleanly, and Drake's screen "
                          "is in a state nobody has looked at yet — entering the next W-2 on top "
                          "of it is how one bad return becomes five. Review Drake, then restart "
                          "the watcher (or pass --keep-going if you accept that risk).",
                          file=sys.stderr)
                    return 2
            if args.once:
                print(f"--once: queue drained ({processed} payload(s)).")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped — {processed} payload(s) entered this session.")
        return 0


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


_INTERESTING_TYPES = {"Button", "Edit", "ComboBox", "List", "ListItem", "MenuItem", "Tab",
                      "TabItem", "CheckBox", "RadioButton", "Tree", "TreeItem", "DataGrid",
                      "DataItem", "Table", "Hyperlink", "SplitButton", "Document"}


def _explore_sort_key(e: dict):
    """Reading order: top, then left. Elements with no rectangle sort last so they never
    push a real control out of position."""
    r = e.get("rect")
    return (0, int(r[1]), int(r[0])) if r else (1, 0, 0)


def cmd_explore(args) -> int:
    """Show what Drake is displaying RIGHT NOW — every visible window and its controls.

    READ-ONLY. It presses nothing, clicks nothing, focuses nothing. Safe to run at any
    moment, including mid-return, including with a dialog up.

    This is the tool that has to come before any navigation code. `open_return()` and
    `open_screen()` in the driver were written from an assumption about how Drake probably
    works and have never been confirmed against the real thing; the last time this project
    built on that kind of assumption — the caret re-arm — it failed live three times in a
    row. Run this at each step you want automated (home screen, client selector, data entry
    menu) and the navigation gets built against what Drake actually shows.
    """
    import time
    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"\nBound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}")
    if args.delay:
        print("Capturing in ", end="", flush=True)
        for n in range(args.delay, 0, -1):
            print(f"{n}… ", end="", flush=True)
            time.sleep(1)
        print()

    dump = driver.explore_screen(cap=args.cap)
    if not dump.get("ok"):
        print(f"explore failed: {dump.get('error')}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, default=str)

    for w in dump["windows"]:
        r = w.get("rect") or [0, 0, 0, 0]
        tags = " [MAIN]" if w.get("is_main") else ""
        if int(w["hwnd"]) in (dump.get("canvas_windows") or []):
            tags += " [CANVAS]"
        print("\n" + "=" * 78)
        print(f"hwnd={w['hwnd']}  {w.get('title')!r}{tags}")
        print(f"  class={w.get('class_name')}  {r[2]}x{r[3]} at ({r[0]},{r[1]})  "
              f"enabled={w.get('enabled')}")
        if w.get("note"):
            print(f"  note: {w['note']}")
        els = w.get("elements") or []
        shown = els if args.all else [
            e for e in els
            if e.get("name") or e.get("value") or e.get("automation_id")
            or e.get("control_type") in _INTERESTING_TYPES]
        print(f"  {len(els)} element(s)" + (f", {len(shown)} with content" if not args.all else "")
              + ("  — TRUNCATED, raise --cap" if w.get("truncated") else ""))
        for e in sorted(shown, key=_explore_sort_key):
            r2 = e.get("rect") or [0, 0, 0, 0]
            bits = []
            if e.get("value"):
                bits.append(f"= {e['value']!r}")
            if e.get("automation_id"):
                bits.append(f"id={e['automation_id']}")
            if e.get("toggle") is not None:
                bits.append(f"checked={e['toggle']}")
            if e.get("enabled") is False:
                bits.append("DISABLED")
            if e.get("focused"):
                bits.append("<<< KEYBOARD")
            name = (e.get("name") or "")[:44]
            print(f"    {e.get('control_type', ''):<13} {name!r:<46} "
                  f"@({r2[0]},{r2[1]}) {'  '.join(bits)}")

    kt = dump.get("keyboard_target") or {}
    print("\n" + "=" * 78)
    print(f"keyboard lands in: {kt.get('root_title')!r} (hwnd={kt.get('root')})")
    print(f"data-entry canvas windows: {dump.get('canvas_windows') or 'none — no return open'}")
    if args.shot:
        s = driver.save_screenshot(args.shot)
        print(f"screenshot -> {s['path']}" if s.get("ok") else f"(screenshot failed: {s.get('error')})")
    print(f"full detail -> {args.out}")
    print("\nNothing was pressed, clicked or changed. This command only looks.")
    return 0


def cmd_navigate(args) -> int:
    """Open a client's return and a data-entry screen — and NOTHING else.

    Deliberately separate from entry so navigation can be proven on its own before it is
    ever allowed to run in front of the typing path. It opens, verifies, and stops; no
    value is entered, so a wrong turn here costs a screen change and nothing more.
    """
    import drake_nav as nav

    driver = DrakeDriver(load_binding(args.binding))
    driver.connect()
    wi = driver.window_info()
    print(f"Bound window: {wi.get('title')!r}  {wi.get('width')}x{wi.get('height')}\n")

    cur = driver.nav_data_entry_window()
    print(f"before: {cur['kind']} — {cur['title']!r}")

    r = nav.open_client(driver, args.ssn, first_name=args.first, last_name=args.last,
                        timeout=args.timeout)
    if not r["ok"]:
        print(f"\nSTOPPED at '{r['step']}': {r['reason']}", file=sys.stderr)
        if r.get("candidates"):
            print(f"  what Drake offered: {r['candidates']}", file=sys.stderr)
        return 2
    print(f"client OK ({r['step']}): {r['reason']}")

    if args.screen:
        s = nav.open_screen(driver, args.screen, timeout=args.timeout)
        if not s["ok"]:
            print(f"\nSTOPPED at '{s['step']}': {s['reason']}", file=sys.stderr)
            if s.get("candidates"):
                print(f"  screens on this menu: {', '.join(s['candidates'])}", file=sys.stderr)
            return 2
        print(f"screen OK: {s['reason']}")

    after = driver.nav_data_entry_window()
    print(f"\nafter: {after['kind']} — {after['title']!r}")
    if args.shot:
        sh = driver.save_screenshot(args.shot)
        print(f"screenshot -> {sh['path']}" if sh.get("ok") else f"(screenshot failed: {sh.get('error')})")
    print("\nNothing was typed. This command only navigates.")
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
    swt = sub.add_parser("watch", parents=[common]); swt.add_argument("--dir", required=True, help="folder the backend drops payloads into (its DRAKE_HANDOFF_DIR). Entered oldest-first, one at a time, then moved to done/ or failed/ with a .report.json"); swt.add_argument("--interval", type=float, default=2.0, help="seconds between folder checks"); swt.add_argument("--once", action="store_true", help="enter whatever is queued right now, then exit (what to use for a test)"); swt.add_argument("--keep-going", dest="keep_going", action="store_true", help="carry on to the next payload after one fails. OFF by default: a halt leaves Drake's screen in a state nobody has reviewed, and entering the next W-2 on top of it turns one bad return into several"); swt.add_argument("--ts", choices=["T", "S", "J"], help="whose documents these are (field 1), when the payload does not say. J (joint) is valid on the 1099 screens only — the W-2 screen's selector is TS and refuses it"); swt.add_argument("--allow-rejected", dest="allow_rejected", action="store_true", help="enter the rest even when some extracted values could not be resolved (they stay blank for you to key by hand)"); swt.add_argument("--include-zeros", action="store_true", help="also enter money fields that are zero"); swt.add_argument("--toggle-method", dest="toggle_method", choices=["scancode", "vkhold", "pywinauto"], default="scancode", help="how Ctrl+N is injected (default scancode — what Drake accepts)"); swt.add_argument("--settle-after", dest="settle_after", type=float, default=0.15, help="seconds to let Drake settle after each field"); swt.add_argument("--no-navigate", dest="no_navigate", action="store_true", help="do NOT open the client and screen — go back to typing into whatever a human already opened. The identity check goes away with it"); swt.add_argument("--nav-timeout", dest="nav_timeout", type=float, default=12.0, help="seconds to wait for each Drake window while navigating"); swt.add_argument("--create", dest="create", action="store_true", help="create a client Drake has never seen instead of refusing. OFF by default: clients are created by a human, and a run that refuses tells the operator which SSN was missing rather than quietly opening a new empty return"); swt.add_argument("--no-new-record", dest="no_new_record", action="store_true", help="refuse instead of pressing Page Down when the open W-2 record already has another employer on it"); swt.set_defaults(func=cmd_watch)
    swf = sub.add_parser("write-form", parents=[common], help="enter an extracted document into Drake on ANY screen this agent has a verified field map for (W2, INT)")
    swf.add_argument("--json", required=True, help="extracted JSON. The form is chosen by its 'drake_screen' key (or --screen)")
    swf.add_argument("--screen", help="override the payload's drake_screen (W2, INT)")
    swf.add_argument("--dry-run", action="store_true", help="resolve and PRINT the plan without touching Drake — run this first, works anywhere")
    swf.add_argument("--manual", action="store_true", help="do NOT navigate: YOU open the client and the screen and click a field, then the agent types. Skips the identity, screen and grid-mode checks with it")
    swf.add_argument("--skip-field", dest="skip_field", type=int, action="append", metavar="N", help="do NOT enter this field number even if the payload has a value for it; repeatable")
    swf.add_argument("--ts", choices=["T", "S", "J"], help="whose document this is (field 1). 1099 screens are TSJ and accept J (a joint account); the W-2 screen is TS only")
    swf.add_argument("--allow-rejected", dest="allow_rejected", action="store_true", help="proceed even if some values could not be resolved (they stay blank in Drake for you to key by hand)")
    swf.add_argument("--include-zeros", action="store_true", help="also enter money fields that are zero (default: skip — a blank box is zero on a tax form)")
    swf.add_argument("--toggle-method", dest="toggle_method", choices=["scancode", "vkhold", "pywinauto"], default="scancode", help="how Ctrl+N is injected (default scancode — what Drake accepts)")
    swf.add_argument("--settle-after", dest="settle_after", type=float, default=0.15, help="seconds to let Drake settle after each field")
    swf.add_argument("--nav-timeout", dest="nav_timeout", type=float, default=12.0, help="seconds to wait for each Drake window while navigating")
    swf.add_argument("--create", dest="create", action="store_true", help="create a client Drake has never seen instead of refusing. OFF by default: clients are created by a human")
    swf.add_argument("--no-new-record", dest="no_new_record", action="store_true", help="refuse instead of pressing Page Down when the open record already holds another payer/employer")
    swf.add_argument("--delay", type=int, default=10, help="seconds to click into the screen before entry fires, in --manual mode")
    swf.add_argument("--shot", default="form-after.png", help="screenshot saved after the run — the verification artifact")
    swf.set_defaults(func=cmd_write_form)
    sev = sub.add_parser("envdump", parents=[common]); sev.add_argument("--out", default="env-dump.json", help="where to write the window-topology JSON"); sev.add_argument("--delay", type=int, default=0, help="seconds before capture — time to click a field / open heads-down first"); sev.set_defaults(func=cmd_envdump)
    sx = sub.add_parser("explore", parents=[common], help="read-only: show every window and control Drake is displaying right now")
    sx.add_argument("--out", default="explore.json", help="where to write the full JSON dump")
    sx.add_argument("--delay", type=int, default=0, help="seconds before capture — time to arrange the Drake screen you want looked at")
    sx.add_argument("--cap", type=int, default=1500, help="max UIA elements to walk before truncating")
    sx.add_argument("--all", action="store_true", help="show every element, including nameless layout containers")
    sx.add_argument("--shot", help="also save a window screenshot here")
    sx.set_defaults(func=cmd_explore)
    sn = sub.add_parser("navigate", parents=[common], help="open a client's return and a screen — types no values")
    sn.add_argument("--ssn", required=True, help="the taxpayer's SSN/EIN, with or without dashes")
    sn.add_argument("--first", help="employee first name — checked against Drake's client record")
    sn.add_argument("--last", help="employee last name — checked against Drake's client record")
    sn.add_argument("--screen", default="W2", help="screen code to open (W2, 1099, INT...). Empty to stop at the menu")
    sn.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for each Drake window")
    sn.add_argument("--shot", help="save a screenshot when it lands")
    sn.set_defaults(func=cmd_navigate)
    sc = sub.add_parser("calibrate", parents=[common]); sc.add_argument("--screen"); sc.set_defaults(func=cmd_calibrate)
    ss = sub.add_parser("selftest", parents=[common]); ss.add_argument("--plan", default="selftest.plan.json"); ss.add_argument("--dry-run", action="store_true"); ss.add_argument("--slow", action="store_true", help="slower keystrokes + pauses so you can watch Drake"); ss.add_argument("--shot", help="save a window screenshot here after the run (human-verify floor / OCR-box source)"); ss.set_defaults(func=cmd_selftest)
    scn = sub.add_parser("connect", parents=[common]); scn.add_argument("--url"); scn.add_argument("--token"); scn.set_defaults(func=cmd_connect)

    args = p.parse_args()
    # Drake not being open is the most ordinary thing that can go wrong, and it used to
    # surface as a twenty-second wait followed by thirty lines of pywinauto internals —
    # which reads like a broken agent rather than a closed program. One sentence, and an
    # exit code the caller can act on.
    try:
        return args.func(args)
    except DrakeNotRunning as e:
        print(f"\nDrake is not open. Start Drake Tax 2025, leave it on its home screen, "
              f"then run this again.\n  ({e})", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
