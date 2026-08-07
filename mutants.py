#!/usr/bin/env python3
"""Mutation check: break each guard IN THE SOURCE and prove the suite goes red.

Run it with `python mutants.py` from this directory. Takes ~20 minutes (it runs the
whole simulator once per mutant, in an isolated copy). Exit 0 = every guard is covered.

Source-level, in an isolated copy, run as a subprocess — so the mutant is the real code
path, not a monkeypatch that a test might be overriding anyway.

A green suite means nothing unless it fails when the thing it protects is broken. The old
suite passed 9/9 while several read-back gates were dead code, so every guard has to be
shown to have a test that kills it.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

SRC = pathlib.Path(__file__).resolve().parent

# name -> (anchor that must appear exactly once, replacement)
MUTANTS = {
    "read-back comparator: back to alnum-only (deletes '.' and '-')": (
        "    na, nb = _as_number(a), _as_number(b)",
        "    na, nb = None, None"),
    "read-back comparator: drop the decimal/sign refusal": (
        '    if any(("." in s) or s[:1] in ("-", "+", "(") for s in (a, b)):\n        return False',
        "    if False:\n        return False"),
    "_settle_read: single read, no convergence (the old repair pattern)": (
        "        cmp = (lambda a, b: a == b) if exact else _same_value",
        "        return True, expected\n        cmp = (lambda a, b: a == b) if exact else _same_value"),
    "empty-value guard removed": (
        "        if not val.strip():",
        "        if False:"),
    "inherited-popup ownership check removed": (
        "        if self._popup_owned:\n            prompt = self._stable_prompt(popup)",
        "        if True:\n            return {\"ok\": True}\n        if self._popup_owned:\n"
        "            prompt = self._stable_prompt(popup)"),
    "prompt baseline removed (classify by the edit box alone)": (
        "            base_prompt = self._stable_prompt(popup, eh)",
        '            base_prompt = ""'),
    "commit proof removed (assume every value was accepted)": (
        "                took, how = self._verify_value_committed(base_prompt, value_prompt)",
        '                took, how = True, "assumed"'),
    # NOTE: the residue check is deliberately NOT listed as a mutant to kill. It is
    # redundant with the exact-match convergence gate (residue + typed digits can never
    # equal the expected number), and is kept only so that failure reports "still holds
    # '2'" instead of the vaguer "never settled". Redundant-by-design, not uncovered.
    "Ctrl+N keyboard-scope gate removed": (
        "            ok_scope, where = self._input_scope(allow_popup=False)\n"
        "            if not ok_scope:",
        "            ok_scope, where = self._input_scope(allow_popup=False)\n"
        "            if False:"),
    # The live halt: the popup's box is only called "Edit" on some toolkits.
    "popup edit: exact class name only (the pywinauto behaviour that halted the live run)": (
        "    editish = [c for c in shown if _editish(c.get(\"class_name\"))]",
        "    editish = []"),
    "popup edit: any child will do (a Static label becomes the typing target)": (
        "    exact = [c for c in shown if (c.get(\"class_name\") or \"\") == preferred_class]",
        "    exact = list(shown)"),
    "popup edit: no children means type at the popup itself (blind entry)": (
        "    if not kids:\n        return None, \"the popup reports NO child windows at all\"",
        "    if not kids:\n        return 1, \"popup itself\""),
    # The painted-popup path: everything now rests on reading the popup as a whole.
    "painted popup: commit without confirming the keystroke (no read channel needed)": (
        "                    settled, read_back, chan = self._settle_surface(eh, val, pre.get(\"baseline\"))",
        "                    settled, read_back, chan = True, val, 'assumed'"),
    "painted popup: press Enter on an unconfirmed field number": (
        "                settled, got, chan = self._settle_surface(eh, fn, pre.get(\"baseline\"))",
        "                settled, got, chan = True, fn, 'assumed'"),
    "painted popup: presence instead of counting (the prompt's own digits count as proof)": (
        "                        and self._surface_count(text, expected) > base_n):",
        "                        and self._surface_count(text, expected) >= 0):"),
    # NOT listed as mutants to kill — verified NOT to be gaps:
    #  • the `target.startswith(acc)` early-break in _surface_count is an EQUIVALENT mutant:
    #    acc only grows, so once it stops being a prefix of target no extension can ever
    #    equal target. Checked exhaustively against the guard-free version over 200k random
    #    (text, expected) pairs: 0 behavioural differences. It is a speed guard, not a gate.
    #  • _is_surface_hwnd in _classify_after_jump's final fallback chooses between
    #    ("unknown") and ("rejected") — BOTH of which halt the batch. It buys a truthful
    #    halt message, not a different outcome, so no test can distinguish them.
    "prompt change accepted from a single jittery frame (no stability)": (
        "                if cur and cur_norm != base_norm and cur_norm == prev_norm:",
        "                if cur and cur_norm != base_norm:"),
    "prompt BASELINE taken from a single frame (one bad read inverts every later check)": (
        "            base_prompt = self._stable_prompt(popup, eh)",
        "            base_prompt = self._popup_prompt(popup, eh)"),
    # The live field-1 halt: an unreadable frame is NO EVIDENCE, not a reading of ''.
    "informationless frame admitted as a reading (blank or punctuation-only)": (
        "            if t is not None and _norm_prompt(t):",
        "            if t is not None:"),
    "unreadable frame recorded as a reading of '' (after the jump)": (
        "                if cur:\n                    prev_norm = cur_norm",
        "                prev_norm = cur_norm"),
    "no baseline -> press Enter anyway and find out afterwards": (
        "            if edit.surface and not base_prompt:",
        "            if False:"),
    "auto-dismiss: press Enter blind instead of clicking a named button": (
        "            if _norm_prompt(txt.replace(\"&\", \"\")) == want_n:",
        "            if True:"),
    "auto-dismiss: every dialog is dismissable, not just the listed ones": (
        "        blob = \" \".join(str(dlg.get(k) or \"\") for k in (\"title\", \"text\"))",
        "        return self.auto_dismiss[0] if self.auto_dismiss else None\n"
        "        blob = \" \".join(str(dlg.get(k) or \"\") for k in (\"title\", \"text\"))"),
    "prompt baseline: edit-shaped children leak into the prompt": (
        "            if cls == edit_class or _editish(cls):",
        "            if cls == edit_class:"),
    # Box 13 checkboxes. The live halt of 2026-08-03: a tick is a GLYPH, so the text gates
    # can never see it. Each of these breaks one of the guards that replaced them.
    "checkbox: routed down the TEXT path again (the live field-47 halt)": (
        '                if kind == "checkbox" or (kind is None and shows_checkbox is True):',
        "                if False:"),
    "checkbox: commit the tick without re-proving it just before the Enter": (
        "        ok2, state, chan = self._settle_checkbox(eh, desired, timeout=1.0)",
        "        ok2, state, chan = True, desired, 'assumed'"),
    "checkbox: a single read is proof (no convergence)": (
        "                if prev is not None and prev == cur and (desired is None or cur == desired):",
        "                if (desired is None or cur == desired):"),
    "checkbox: disagreeing channels resolved instead of discarded": (
        "            vals = set(st.values())\n            if len(vals) == 1:",
        "            vals = set(st.values())\n            if vals:\n                vals = {max(vals)}\n"
        "            if len(vals) == 1:"),
    "checkbox: send a token even when the box is already in the wanted state": (
        "        if not (ok0 and arrival == desired):",
        "        if True:"),
    "checkbox: build-drift guard removed (money typed at a tick box)": (
        '                if shows_checkbox is True and kind not in (None, "checkbox"):',
        "                if False:"),
    "checkbox: a tick on the canvas is typed blind (per-jump build)": (
        '            if model == "per-jump" and kind == "checkbox":',
        "            if False:"),
    "checkbox: an unreadable arrival state is treated as 'clear' for an untick": (
        "        if not ok0 and desired is False:",
        "        if False:"),
    # NOT listed as a mutant to kill, and verified not to be a gap:
    #  • _read_popup_checkbox_pixels returning None rather than False for "no tick visible"
    #    is a CHANNEL semantic, and the simulator replaces both checkbox channels at the
    #    same boundary it replaces the UIA/OCR text channels — so no case can distinguish
    #    the two here. The rule is enforced instead by the fake's pixel_tick(), which never
    #    returns False, and by case_tick_glyph_table on the detector itself.
    # The EIN catch: the popup is present but INERT (keyboard still on the canvas).
    "inert popup accepted as ready (present == armed again)": (
        "                if not self._popup_holds_keyboard(popup):",
        "                if False:"),
    "recycle fires on a HEALTHY popup too (the double-toggle cascade)": (
        "                if focused == int(h):",
        "                if False:"),
    # The other live halt of 2026-08-04: a jump that lands LATE (Drake busy auto-filling)
    # read as a refusal. The budget is what buys the distinction.
    "jump budget back to the 2.5s that called a busy Drake a refusal": (
        '        self.jump_timeout = float(self.nav.get("headsdown_jump_timeout", 8.0))',
        "        self.jump_timeout = 2.5"),
    # The live halt of 2026-08-04: a frame that was ALREADY disabled at attach was read as
    # a modal, and no field could be entered. Both directions have to be covered — the
    # inherited state must not halt, and a modal that arrives later must still be caught.
    "inherited disabled frame treated as a modal again (the live 08-04 halt)": (
        "        if not main_disabled_at_attach:",
        "        if True:"),
    "attach never records that the frame was already disabled": (
        "        self._baseline_main_disabled = (enabled is False)",
        "        self._baseline_main_disabled = False"),
    "structural dialog gate blinded": (
        "        wins, main_enabled = snap\n"
        "        kw = dict(popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,",
        "        return None\n"
        "        wins, main_enabled = snap\n"
        "        kw = dict(popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,"),
    # --- caret re-arm (the between-runs stuck state) ---------------------------------
    # Ctrl+N is a SILENT no-op with no active caret, which is the state a finished run
    # leaves behind. These mutants restore the 2026-08-05 halt in each of the two places
    # it can happen, and check that the recovery cannot be reduced to "send keys and hope".
    "caret: re-arm never fires when a stale popup blocks Ctrl+N": (
        "            return self._rearm_caret(method=method)",
        '            return {"ok": False, "reason": "Ctrl+N did not close it."}'),
    "caret: re-arm never fires when Ctrl+N opens nothing": (
        "        rearmed = self._rearm_caret(method=method)",
        '        rearmed = {"ok": False, "reason": "no caret"}'),
    "caret: the caret is never restored before Ctrl+N": (
        "        if not self._focus_canvas_field():\n"
        "            return {\"ok\": False,\n"
        "                    \"reason\": \"heads-down will not arm and no box on the data-entry form would \"",
        "        if False:\n"
        "            return {\"ok\": False,\n"
        "                    \"reason\": \"heads-down will not arm and no box on the data-entry form would \""),
    # NOT listed as a mutant: the SECOND _focus_canvas_field call, after the stale popup is
    # closed. It is load-bearing — measured live 2026-08-05, closing that popup leaves NOTHING
    # focused (GetGUIThreadInfo returns hwnd 0) and the re-open goes nowhere; that is exactly
    # where the first version of this recovery got stuck. But the simulator's popup returns
    # the caret to the box it came from, which is what Drake does everywhere EXCEPT this one
    # state, so the fake cannot distinguish the two without modelling focus loss in a way
    # that would misrepresent the normal EIN recycle. Live-verified, not suite-verified, and
    # said plainly here rather than covered by a mutant that would pass for the wrong reason.
    "caret: re-arm claims success without proving a popup appeared": (
        "        if popup is None:\n"
        "            return {\"ok\": False,\n"
        "                    \"reason\": \"restored the caret on a data-entry box, but Ctrl+N still did not \"",
        "        if False:\n"
        "            return {\"ok\": False,\n"
        "                    \"reason\": \"restored the caret on a data-entry box, but Ctrl+N still did not \""),
    # --- w2_map: Box 20 locality resolution -----------------------------------------
    # These guards decide WHICH locality goes on the return. There is no downstream
    # defense against them: a wrong-but-valid code is typed cleanly, read back cleanly,
    # and sits in the box looking exactly like a right one. Only refusing to guess
    # protects that, so refusing has to be proven to have a test.
    "locality: ambiguous prefix picks the first match instead of refusing": (
        "    if len(prefix) == 1:",
        "    if len(prefix) >= 1:"),
    "locality: duplicate names pick the first instead of refusing": (
        "    if len(by_name) == 1:",
        "    if len(by_name) >= 1:"),
    "locality: an exact code is re-resolved by name/prefix instead of passing through": (
        "    if text in entries:                                        # already the code",
        "    if False:"),
    "locality: ' CITY' treated as noise (silently retargets 'Portland City')": (
        '    for suffix in (" COUNTY", " CO."):',
        '    for suffix in (" COUNTY", " CO.", " CITY"):'),
    "locality: an unresolvable value is entered anyway (the 2026-08-04 bug, restored)": (
        '            if res["code"] is None:',
        "            if False:"),
    # -- navigation (drake_nav.py) ----------------------------------------------------
    # These guards decide WHICH RETURN gets typed into. A survivor here is worse than a
    # survivor anywhere else in this file: every other guard protects a value, and these
    # protect the identity of the person the values belong to. A wrong-client entry passes
    # every read-back, every form check and every audit — it is invisible downstream.
    "nav: the open return's SSN is never checked against the one asked for": (
        '    if parsed["id"] != want:',
        "    if False:"),
    "nav: the open return's NAME is never checked": (
        '        nm = names_match(parsed["name"], first_name, last_name)\n        if not nm["ok"]:',
        '        nm = names_match(parsed["name"], first_name, last_name)\n        if False:'),
    "nav: two clients sharing an SSN — take the first instead of refusing": (
        '    if len(hits) == 1:\n        return {"ok": True, "row": hits[0], "reason": ""}',
        '    if hits:\n        return {"ok": True, "row": hits[0], "reason": ""}'),
    "nav: a row id of an unknown shape yields a partial id instead of nothing": (
        '    m = re.fullmatch(r"(\\d+)-(\\d+)", tail)\n    return m.group(1) if m else ""',
        '    return tail.split("-")[0]'),
    "nav: screen code matched by prefix (W2 also opens W2G, 1 also opens 1099)": (
        '    hits = [lk for lk, p in parsed if p and p["code"] == want]',
        '    hits = [lk for lk, p in parsed if p and p["code"].startswith(want)]'),
    "nav: the surname is never compared": (
        '    if want_last and got["last"] and want_last != got["last"]:',
        "    if False:"),
    "nav: given names compared by substring ('ANN' matches 'DEANNA')": (
        "    wt, gt = want.split(), got.split()\n"
        "    return all(w in gt for w in wt) or all(g in wt for g in gt)",
        "    return want in got or got in want"),
    "nav: an unreadable client name counts as a match": (
        '    if not got["last"] and not got["given"]:',
        "    if False:"),
    "nav: a client row borrows whatever name is nearest": (
        '            if top <= mid <= bottom:',
        "            if True:"),
    "nav: a window carrying BOTH menu and form markers is called a form": (
        '    if is_form and not is_menu:\n        return "form"',
        '    if is_form:\n        return "form"'),
    "nav: the duplicate-W2 check removed (re-sending doubles the client's wages)": (
        '            if normalize_id(v.get("value")) == want:',
        "            if False:"),
    "nav: an unreadable form counts as an empty record (types over existing data)": (
        '    if not state or not state.get("ok"):',
        "    if not state:"),
    "nav: stray text counts as a real W-2 again (the 2026-08-07 live halt)": (
        '    return "w2" if any(_looks_numeric(v) for v in vals) else "fragment"',
        '    return "w2"'),
    "nav: a real W-2 is mistaken for stray text (types over an existing W-2)": (
        '    return "w2" if any(_looks_numeric(v) for v in vals) else "fragment"',
        '    return "fragment"'),
}

# Which source file each mutant edits. drake_driver.py unless named here — the guards that
# decide what gets TYPED do not all live in the driver, and a mutation harness that can only
# reach one file quietly reports "all covered" about the other.
TARGET = {n: "w2_map.py" for n in MUTANTS if n.startswith("locality:")}
TARGET.update({n: "drake_nav.py" for n in MUTANTS if n.startswith("nav:")})
MUTABLE_FILES = ("drake_driver.py", "w2_map.py", "drake_nav.py")


# Mutants whose guard belongs to one family of cases may name that family, so the harness
# runs only those cases against them. The asymmetry is what makes this safe: running FEWER
# cases can only ever turn a kill into a reported GAP — never a survivor into a false kill.
# Unfiltered mutants still run the whole suite.
FAMILY = {name: "checkbox" for name in MUTANTS if name.startswith("checkbox:")}
FAMILY.update({n: "modal" for n in MUTANTS if "disabled frame" in n or "already disabled" in n})
FAMILY.update({n: "jump" for n in MUTANTS if "jump budget" in n})
FAMILY.update({n: "ctrln" for n in MUTANTS if "inert popup" in n or "double-toggle cascade" in n})
FAMILY.update({n: "locality" for n in MUTANTS if n.startswith("locality:")})
FAMILY.update({n: "caret" for n in MUTANTS if n.startswith("caret:")})
FAMILY.update({n: "nav" for n in MUTANTS if n.startswith("nav:")})


# Per-mutant wall clock. A BROKEN guard does not only fail cases — it stops the driver
# short-circuiting, so cases that normally end at the first refusal run every retry and
# timeout to the end. One mutant took the suite from ~3 minutes to over 30 and blew the old
# 1800s cap, which then raised TimeoutExpired out of run() and killed the whole sweep on its
# fifth mutant. The cap is generous now, and — more importantly — expiring it is a RESULT,
# not a crash: a sweep that dies partway reports nothing about the mutants it never reached.
RUN_TIMEOUT = 3600


def run(dirpath, only=None):
    cmd = [sys.executable, "simulate_headsdown.py"] + ([only] if only else [])
    try:
        p = subprocess.run(cmd, cwd=dirpath, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Treated as KILLED (non-zero), and labelled so it is not mistaken for a clean red:
        # the guard's absence was detected, just not within the budget. Worth a look if it
        # keeps happening — it usually means a mutant removed an early exit.
        return 124, f"TIMED OUT after {RUN_TIMEOUT}s (suite never finished)"
    tail = [l for l in p.stdout.splitlines() if "cases pass" in l]
    return p.returncode, (tail[-1] if tail else "?")


args = [a for a in sys.argv[1:] if not a.startswith("-")]
pick = args[0] if args else None      # substring: run only these mutants
if pick:
    print(f"(only mutants matching {pick!r})\n")

base_dir = tempfile.mkdtemp()
for f in ("drake_driver.py", "simulate_headsdown.py", "w2_map.py", "protocol.py",
          "drake_nav.py"):
    shutil.copy(SRC / f, base_dir)
todo = {k: v for k, v in MUTANTS.items() if not pick or pick.lower() in k.lower()}
if not todo:
    print(f"no mutants match {pick!r}")
    raise SystemExit(1)
# One baseline per family actually in play — a mutant is only evidence against a set of
# cases that passes without it.
baselines = {}
for fam in sorted({FAMILY.get(k) for k in todo}, key=lambda x: (x is not None, x)):
    rc, line = run(base_dir, fam)
    baselines[fam] = line
    print(f"baseline [{fam or 'full suite'}]: {line}  (exit {rc})")
    if rc != 0:
        print("baseline is not green — fix that before mutating")
        raise SystemExit(1)
print()

originals = {f: (SRC / f).read_text(encoding="utf-8") for f in MUTABLE_FILES}
survived = []
for name, (anchor, repl) in todo.items():
    target = TARGET.get(name, "drake_driver.py")
    original = originals[target]
    n = original.count(anchor)
    if n != 1:
        print(f"  ??   {name}\n         -> anchor matched {n} times in {target}, not 1 "
              f"— mutant not applied")
        survived.append(name)
        continue
    d = tempfile.mkdtemp()
    for f in ("drake_driver.py", "simulate_headsdown.py", "w2_map.py", "protocol.py",
          "drake_nav.py"):
        shutil.copy(SRC / f, d)
    (pathlib.Path(d) / target).write_text(original.replace(anchor, repl), encoding="utf-8")
    fam = FAMILY.get(name)
    rc, line = run(d, fam)
    killed = rc != 0
    if not killed:
        survived.append(name)
    print(f"  {'ok  ' if killed else 'GAP '} {name}\n"
          f"         -> {'KILLED' if killed else 'SURVIVED — no test covers this'}  [{line}"
          f"{'; cases: ' + fam if fam else ''}]")
    shutil.rmtree(d, ignore_errors=True)

shutil.rmtree(base_dir, ignore_errors=True)
print(f"\n{len(todo) - len(survived)}/{len(todo)} mutants killed")
for s in survived:
    print(f"  still alive: {s}")
raise SystemExit(0 if not survived else 1)
