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
    "structural dialog gate blinded": (
        "        wins, main_enabled = snap\n"
        "        kw = dict(popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,",
        "        return None\n"
        "        wins, main_enabled = snap\n"
        "        kw = dict(popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,"),
}


def run(dirpath):
    p = subprocess.run([sys.executable, "simulate_headsdown.py"], cwd=dirpath,
                       capture_output=True, text=True, timeout=600)
    tail = [l for l in p.stdout.splitlines() if "cases pass" in l]
    return p.returncode, (tail[-1] if tail else "?")


base_dir = tempfile.mkdtemp()
for f in ("drake_driver.py", "simulate_headsdown.py", "w2_map.py", "protocol.py"):
    shutil.copy(SRC / f, base_dir)
rc, line = run(base_dir)
print(f"baseline: {line}  (exit {rc})\n")
if rc != 0:
    print("baseline is not green — fix that before mutating")
    raise SystemExit(1)

original = (SRC / "drake_driver.py").read_text()
survived = []
for name, (anchor, repl) in MUTANTS.items():
    n = original.count(anchor)
    if n != 1:
        print(f"  ??   {name}\n         -> anchor matched {n} times, not 1 — mutant not applied")
        survived.append(name)
        continue
    d = tempfile.mkdtemp()
    for f in ("simulate_headsdown.py", "w2_map.py", "protocol.py"):
        shutil.copy(SRC / f, d)
    (pathlib.Path(d) / "drake_driver.py").write_text(original.replace(anchor, repl))
    rc, line = run(d)
    killed = rc != 0
    if not killed:
        survived.append(name)
    print(f"  {'ok  ' if killed else 'GAP '} {name}\n"
          f"         -> {'KILLED' if killed else 'SURVIVED — no test covers this'}  [{line}]")
    shutil.rmtree(d, ignore_errors=True)

shutil.rmtree(base_dir, ignore_errors=True)
print(f"\n{len(MUTANTS) - len(survived)}/{len(MUTANTS)} mutants killed")
for s in survived:
    print(f"  still alive: {s}")
raise SystemExit(0 if not survived else 1)
