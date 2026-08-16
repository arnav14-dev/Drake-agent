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
    # `base_prompt = self._stable_prompt(popup, eh)` appears at TWO call sites, so both of
    # these mutants quoted an ambiguous line and were applied to neither. Anchored on the
    # comment above the heads-down entry path to pick one deliberately. The second site (the
    # probe path) is left unmutated and so is NOT covered by these two — a smaller gap than
    # the one that was there, and a stated one.
    "prompt baseline removed (classify by the edit box alone)": (
        '            # "Drake took the number" from "Drake silently refused it".\n'
        "            base_prompt = self._stable_prompt(popup, eh)",
        '            # "Drake took the number" from "Drake silently refused it".\n'
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
        '            # "Drake took the number" from "Drake silently refused it".\n'
        "            base_prompt = self._stable_prompt(popup, eh)",
        '            # "Drake took the number" from "Drake silently refused it".\n'
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
    "nav: the screen heading is never checked (78 values into whatever screen is open)": (
        "    return bool(re.search(pat, blob, re.I))",
        "    return True"),
    "nav: an unmeasured screen is claimed as verified instead of reported unproved": (
        "    if not pat:\n        return None",
        "    if not pat:\n        return True"),
    "nav: auto-create ignores a client already on the books (misread SSN buries a return)": (
        '        if names_match(r.get("name"), first_name, last_name)["ok"]:\n            hits.append(r)',
        '        if False:\n            hits.append(r)'),
    "nav: grid mode is never detected (61 field numbers into a spreadsheet)": (
        '    return any(str(i or "").lower().startswith(pre) for i in (element_ids or ()))',
        "    return False"),
    "nav: everything is called a grid (the form view can never be entered)": (
        '    return any(str(i or "").lower().startswith(pre) for i in (element_ids or ()))',
        "    return True"),
    # -- the 1099-INT map and the generic planner (int_map.py / form_plan.py) -----------
    # The W-2 taught this the expensive way: the map IS the safety mechanism, because a
    # wrong field number has no downstream defense. Drake accepts a number on any screen,
    # the popup echoes the value back, the read-back gate passes, and a plausible amount
    # ends up in the wrong box of a real return with a green run on the operator's screen.
    "int: two keys may claim the same box (one value silently overwrites another)": (
        "            if n in seen:\n"
        "                raise RuntimeError(f\"{where}: field {n} is claimed by both {seen[n]!r} and {k!r}\")",
        "            if False:\n"
        "                raise RuntimeError(f\"{where}: field {n} is claimed by both {seen[n]!r} and {k!r}\")"),
    "int: a key may be bound to the forbidden '<Click to Access>' sub-screen": (
        "        bad = sorted((k, n) for k, n in nums if n in self.forbidden)",
        "        bad = []"),
    "int: field numbers above the highest legible one are allowed": (
        "        out_of_range = sorted((k, n) for k, n in nums if not 1 <= n <= self.max_field)",
        "        out_of_range = []"),
    "int: an unknown value kind falls through to plain text instead of refusing": (
        '        unknown_kinds = sorted({s["kind"] for s in self.fields.values()} - {',
        '        unknown_kinds = [] or sorted(set() - {'),
    "int: a percentage over 100 is entered instead of refused": (
        "    if pct < 0 or pct > 100:\n        return None",
        "    if False:\n        return None"),
    "int: an unparseable date is typed anyway instead of refused": (
        "    if not (1 <= int(m) <= 12 and 1 <= int(d) <= 31 and 1900 <= int(y) <= 2100):\n"
        "        return None",
        "    if False:\n        return None"),
    "int: a joint 'J' is quietly downgraded to taxpayer": (
        '    if s in ("T", "S", "J"):\n        return s',
        '    if s in ("T", "S", "J"):\n        return "T"'),
    "int: a locality is typed as its printed name instead of Drake's code": (
        '            if res["code"] is None:\n                row["value"] = _clean_text(raw)',
        '            if True:\n                row["value"] = _clean_text(raw)'),
    # --- 1099-DIV ---------------------------------------------------------------------
    # This screen brought two things the INT screen did not: codes carrying a DIGIT, and a
    # sibling screen with an almost identical heading that it links to by name.
    # ANCHOR REWRITTEN 2026-08-15. The original quoted _clean_code_alnum as it was when the
    # DIV screen landed; the 1099-R symbol codes then rewrote that function's last line, and
    # this mutant silently stopped applying. A mutant that does not apply is reported as a
    # survivor, which reads as "the guard is untested" — the harness blaming the tests for
    # its own stale quotation. See the anchor preflight below, which now refuses to run at
    # all rather than let that happen again.
    "div: a dropdown's description is compressed into a code instead of refused": (
        "    s = str(v).strip().upper()\n"
        "    if not 1 <= len(s) <= 2:\n"
        "        return None\n"
        "    return s if all(ch.isalnum() or ch in DRAKE_CODE_SYMBOLS for ch in s) else None",
        '    s = "".join(ch for ch in str(v) if ch.isalnum() or ch in DRAKE_CODE_SYMBOLS).upper()\n'
        "    return s or None"),
    # The reason code_an is a SEPARATE kind. If someone ever "simplifies" the two together,
    # this is the W-2 behaviour that goes quiet: 'D 23' becomes 'D' and the whole deferral
    # is measured against the current year's limit.
    "div: the W-2's Box 12 digit rule is loosened, so 'D 23' becomes 'D'": (
        "    if any(ch.isdigit() for ch in raw):\n        return None",
        "    if False:\n        return None"),
    # The guard that came out of the live INT halts: a value of exactly the right SHAPE that
    # Drake's list does not contain. The popup echoes it perfectly and Drake rejects it
    # afterwards, so nothing downstream can catch this one.
    "div: a value outside a box's fixed list is entered instead of refused": (
        '        allowed = field.get("values")',
        "        allowed = None"),
    # Field 68 holds three characters. Trimming a QUANTITY to fit turns 6010 into 601 and
    # attaches a warning to a number that is ten times too small — which reads as a success.
    "div: a number too long for its box is cut down to fit instead of refused": (
        '            if field["kind"] in NUMERIC_KINDS:',
        "            if False:"),
    "div: the DIV screen signature is loose enough to match the INT screen": (
        '    "DIV": r"Schedule\\s+B\\s*-\\s*Dividend\\s+Income\\s*\\(1099-DIV\\)",',
        '    "DIV": r"Schedule\\s+B",'),
    # --- 1099-R ---------------------------------------------------------------------
    # Box 7 is TWO boxes on this screen. Dropping the second code is not a lost detail: '1'
    # on its own is a valid code Drake accepts without complaint, and it means an early
    # distribution with no known exception — so the 10% penalty rides on the character that
    # went missing. Nothing downstream can see it.
    "r_: Box 7's second distribution code is dropped instead of entered in its own box": (
        "    if len(s) == 2 and s[0] in DIST_CODES and s[1] in DIST_CODES:\n"
        "        return s[0], s[1]",
        "    if len(s) == 2 and s[0] in DIST_CODES and s[1] in DIST_CODES:\n"
        "        return s[0], None"),
    # This screen offers T and S only. Accepting J would file a spouse's pension under
    # whoever Drake defaults to.
    "r_: a joint 'J' is accepted on a screen that only offers T and S": (
        '    if s in ("T", "S"):\n        return s',
        '    if s in ("T", "S", "J"):\n        return s'),
    # Seven of the pension-type codes are symbols. Refusing them is our planner turning away
    # a value Drake accepts — the same defect as the IL Schedule M list that only knew A-Z.
    "codes: Drake's symbol selection codes are refused as if they were junk": (
        'DRAKE_CODE_SYMBOLS = frozenset("#$%&*=@")',
        "DRAKE_CODE_SYMBOLS = frozenset()"),
    # Without the length bound, the descriptive text printed beside a code becomes a code.
    "codes: a selection code of any length is accepted, so a description becomes a code": (
        "    if not 1 <= len(s) <= 2:\n        return None",
        "    if False:\n        return None"),
    # --- SSA-1099 ---------------------------------------------------------------------
    # A value the screen has no box for must be reported BY NAME with the reason. Dropping it
    # silently is the failure this whole layer exists to prevent, and the SSA screen is where
    # it bites hardest: eleven of the twenty values on the form have nowhere to go, and two
    # of them are the prior-year benefits the lump-sum election runs on.
    "ssa_: a value the screen has no box for is dropped silently instead of reported": (
        '            not_on_screen.append({"key": key, "raw": raw, "why": spec.not_on_screen[key]})',
        "            pass"),
    # The menu link 'SSA|SSA-1099, Social Security' is a PREFIX of the screen's heading, so a
    # signature that stops early reports the screen as open from the Data Entry Menu.
    "ssa_: the SSA signature stops at 'Social Security', which the MENU also says": (
        '    "SSA": r"SSA-1099,\\s*Social\\s+Security\\s+Benefits\\s+Statement",',
        '    "SSA": r"SSA-1099,\\s*Social\\s+Security",'),
    # The 1098 country boxes are the only place where a value can be VALID, accepted, echoed,
    # readable on the form, and still the wrong country — Drake's codes are not ISO. The plan
    # printing the country NAME is the only check a human can make, so removing it must fail.
    "m1098: the country code is entered without naming the country it selects": (
        '            plan["warnings"].append(\n'
        '                f"{whose} country {e[\'value\']!r} selects {name.upper()} in Drake. Drake\'s "\n'
        '                f"codes are NOT ISO — check this is the country on the document.")',
        '            pass'),
    # A country NAME must never be shortened into a code. 'Switzerland'[:2] is 'SW', which is
    # Sweden on Drake's list — a real country, silently wrong, and readable on the form.
    "m1098: a country NAME is truncated into a code instead of refused": (
        '    s = "".join(str(v).split()).upper()\n'
        '    return s if len(s) == 2 and s.isalpha() else None',
        '    s = "".join(str(v).split()).upper()[:2]\n'
        '    return s if len(s) == 2 and s.isalpha() else None'),
    # Field 3's list holds four-character codes (4835, 8829). Collapsing this kind into the
    # two-character `code_an` refuses both, and the interest silently stays on Schedule A.
    "m1098: the four-character form codes are squeezed through the 2-char code kind": (
        '    if kind == "code_form":\n        return _clean_code_form(value)',
        '    if kind == "code_form":\n        return _clean_code_alnum(value)'),
    # Screen 1098 is on the 'Other Forms' tab. Without the tab walk, open_screen sees only
    # whichever tab is showing and reports the screen as not existing.
    "m1098: open_screen gives up on the first tab instead of searching the others": (
        "        tried = []\n        for tab in _tabs():",
        "        tried = []\n        for tab in []:"),
    # The exact state two screens shipped in: their extractor's identity key is not on the
    # list, so the payload has no client and the screen cannot be sent from the browser at
    # all — while every other test, and a perfect live run, stays green.
    "forms: the client-identity key list forgets the two screens that do not say 'recipient'": (
        '    return {"ssn": first("client_ssn", "employee_ssn", "recipient_tin", "recipient_ssn",\n'
        '                         "beneficiary_ssn", "borrower_ssn"),',
        '    return {"ssn": first("client_ssn", "employee_ssn", "recipient_tin", "recipient_ssn"),'),
    # A screen link clicked from a form opens Drake's RECORD CHOOSER, not the screen. Every
    # batch meets this on its second document; a single-document run never does, because
    # those always start from the menu.
    "nav: a screen is opened from whatever is on screen, so Drake shows its record chooser": (
        '    if cur["kind"] == "form":',
        '    if False:'),
    # The chooser lists every record on the screen. Skipping that check is a WEAKER duplicate
    # guard than the one that existed before it — a client's second identical 1099 goes in.
    "chooser: the record list is not checked for this payer, so a duplicate is entered": (
        "            dupes = nav.forms_list_matches(rows, payload[key])" + chr(10) +
        "    return dupes",
        "            dupes = nav.forms_list_matches(rows, payload[key])" + chr(10) +
        "    return []"),
    # Matching only on the ID never fires on a W-2 chooser, which prints Employer Name and
    # no EIN at all — the screen where doubling someone's wages is easiest.
    "chooser: only the payer ID is compared, not the name the chooser actually prints": (
        '        if not dupes and str(payload.get(key) or "").strip():',
        "        if False:"),
    # Open on 'whatever row is selected' puts this document's values on top of an existing
    # record instead of adding one.
    "chooser: any row will do, not the New Record row": (
        "        if FORMS_LIST_NEW_CELL not in texts:" + chr(10) + "            continue",
        "        if False:" + chr(10) + "            continue"),
    "forms: an unmapped screen falls back to the W-2 map": (
        '    return _FORMS.get(str(screen or "").strip().upper())',
        '    return _FORMS.get(str(screen or "").strip().upper()) or _FORMS["W2"]'),
    "forms: the dedupe id always comes from the W-2's employer EIN key": (
        '            "ein": g(form.get("id_key") or "employer_ein"),',
        '            "ein": g("employer_ein"),'),
}

# Which source file each mutant edits. drake_driver.py unless named here — the guards that
# decide what gets TYPED do not all live in the driver, and a mutation harness that can only
# reach one file quietly reports "all covered" about the other.
TARGET = {n: "w2_map.py" for n in MUTANTS if n.startswith("locality:")}
TARGET.update({n: "drake_nav.py" for n in MUTANTS if n.startswith("nav:")})
# The 1099-INT guards live in the generic planner, not in the INT map — that is the point
# of the planner. `forms:` mutants edit the dispatcher in agent.py, which is what decides
# which map plans a payload at all.
TARGET.update({n: "form_plan.py" for n in MUTANTS if n.startswith("int:")})
TARGET.update({n: "agent.py" for n in MUTANTS if n.startswith("forms:")})
# The `div:` mutants do NOT share one file: two are in the planner, one is the W-2's own
# sanitizer (which is the point of that mutant — proving the DIV work did not loosen it),
# and one is the screen signature in the navigator. Routed by name for that reason.
TARGET.update({n: "form_plan.py" for n in MUTANTS if n.startswith("div:")})
TARGET["div: the W-2's Box 12 digit rule is loosened, so 'D 23' becomes 'D'"] = "w2_map.py"
TARGET["div: the DIV screen signature is loose enough to match the INT screen"] = "drake_nav.py"
# The `r_:` mutants split the same way: the Box 7 guard lives in the 1099-R map itself,
# the TS guard is the W-2's own sanitizer, and the code-shape guards are in the planner.
TARGET.update({n: "form_plan.py" for n in MUTANTS if n.startswith("codes:")})
TARGET.update({n: "form_plan.py" for n in MUTANTS if n.startswith("ssa_:")})
TARGET["ssa_: the SSA signature stops at 'Social Security', which the MENU also says"] = "drake_nav.py"
TARGET["r_: Box 7's second distribution code is dropped instead of entered in its own box"] = "r_map.py"
TARGET["r_: a joint 'J' is accepted on a screen that only offers T and S"] = "w2_map.py"
# The 1098 guards are spread across three files, which is the point of naming them here:
# the country NAMING lives in the map, the country/code SHAPE rules in the planner, and the
# menu-tab walk in the navigator.
TARGET.update({n: "form_plan.py" for n in MUTANTS if n.startswith("m1098:")})
TARGET.update({n: "drake_nav.py" for n in MUTANTS if n.startswith("chooser:")})
TARGET["chooser: the record list is not checked for this payer, so a duplicate is entered"] = "agent.py"
TARGET["chooser: only the payer ID is compared, not the name the chooser actually prints"] = "agent.py"
TARGET["m1098: the country code is entered without naming the country it selects"] = "m1098_map.py"
TARGET["m1098: open_screen gives up on the first tab instead of searching the others"] = "drake_nav.py"
MUTABLE_FILES = ("drake_driver.py", "w2_map.py", "drake_nav.py", "form_plan.py",
                 "int_map.py", "div_map.py", "r_map.py", "ssa_map.py", "m1098_map.py",
                 "agent.py")


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
FAMILY.update({n: "int" for n in MUTANTS if n.startswith("int:")})
FAMILY.update({n: "div" for n in MUTANTS if n.startswith("div:")})
FAMILY.update({n: "r_" for n in MUTANTS if n.startswith("r_:")})
FAMILY.update({n: "ssa_" for n in MUTANTS if n.startswith("ssa_:")})
FAMILY.update({n: "m1098" for n in MUTANTS if n.startswith("m1098:")})
FAMILY.update({n: "record_chooser" for n in MUTANTS if n.startswith("chooser:")})
FAMILY["nav: a screen is opened from whatever is on screen, so Drake shows its record chooser"] = "nav_menu_first"
FAMILY.update({n: "form_dispatch" for n in MUTANTS if n.startswith("forms:")})
# This one is proven by the identity case, not the dispatcher cases.
FAMILY["forms: the client-identity key list forgets the two screens that do not say 'recipient'"] = "payload_target"
# The grid-mode guard lives in drake_nav (so it is a `nav:` mutant) but the cases that
# prove it are the INT ones — the grid only exists on screens like INT. Filtering to the
# 'nav' family would run a set of cases that never touches it, and a mutant that survives
# because its test was never RUN reads exactly like a mutant that survived because the
# guard is untested.
FAMILY.update({n: "grid" for n in MUTANTS if "grid" in n})


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

# Everything the suite needs to run standing on its own in a temp directory. It must be a
# SUPERSET of MUTABLE_FILES: a mutant written into a file the suite never loaded would run
# against the pristine original and be reported as a survivor — the harness accusing the
# tests of a gap that is really its own.
COPY_FILES = ("drake_driver.py", "simulate_headsdown.py", "w2_map.py", "protocol.py",
              "drake_nav.py", "form_plan.py", "int_map.py", "div_map.py", "r_map.py",
              "ssa_map.py", "m1098_map.py", "agent.py", "sample_1099int_full.json",
              "sample_1099div_full.json", "sample_1099r_full.json",
              "sample_ssa1099_full.json", "sample_1098_full.json")
missing = sorted(set(MUTABLE_FILES) - set(COPY_FILES))
if missing:
    raise SystemExit(f"mutants.py: {missing} can be mutated but is never copied into the "
                     f"sandbox — every mutant against it would falsely SURVIVE")

# ANCHOR PREFLIGHT. Every mutant quotes a piece of source verbatim; when that source is
# edited for an unrelated reason, the quotation goes stale and the mutant stops applying.
# The harness then reports it as SURVIVED — indistinguishable, in the output, from a guard
# that genuinely has no test. That happened to the DIV description mutant: the 1099-R symbol
# work rewrote the function it quoted, and the mutant read as an untested guard for two days.
#
# Checked for EVERY mutant up front, not just the ones this run selected, because a stale
# anchor in a family nobody filtered to is exactly how the last one hid.
stale = []
for _name, (_anchor, _repl) in MUTANTS.items():
    _tgt = TARGET.get(_name, "drake_driver.py")
    _n = (SRC / _tgt).read_text(encoding="utf-8").count(_anchor)
    if _n != 1:
        stale.append(f"  {_name}\n      quotes {_tgt}, found {_n} match(es), expected exactly 1")
if stale:
    raise SystemExit("mutants.py: these mutants quote source that no longer exists, so they "
                     "would be applied to nothing and reported as SURVIVED — a gap in the "
                     "harness masquerading as a gap in the tests. Re-quote them:\n"
                     + "\n".join(stale))

base_dir = tempfile.mkdtemp()
for f in COPY_FILES:
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
    for f in COPY_FILES:
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
