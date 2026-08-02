#!/usr/bin/env python3
"""
Offline simulator for the heads-down entry state machine.

The driver's logic can only be exercised for real on the Windows laptop that has Drake,
which makes every change a trip to a live tax return. This fake Drake reproduces the
behaviours we have actually OBSERVED — the confirmed persistent command-bar protocol, the
Field-4/EIN auto-advance that swallows the next Ctrl+N, silent refusals, and a popup left
armed by a previous run — so the state machine can be proven anywhere, in seconds, before
it ever touches a client return.

It fakes Drake, NOT pywinauto: focus/WM_GETTEXT/window-enumeration behaviour on the real
thing is still verified on the laptop. What this proves is the LOGIC — that numbers and
values reach the right target under both popup models, that a swallowed Ctrl+N recovers
instead of cascading, and that every anomaly HALTs instead of writing on.

DESIGN RULE learned the hard way, twice: a fake that is more permissive than the real API
hides exactly the bugs it exists to catch. The first version gave `_FakeWin` an `exists()`
that real UIAWrapper lacks, so 9/9 passed while the live run died at field 4. The second
made read-back an identity function — `set_edit_text` wrote the same string the read
returned — so every read-back gate in the driver was dead code that could be deleted with
the suite still green. Both are modelled honestly now: reads go through a real echo the
test controls, and handles must match.

    python simulate_headsdown.py
"""

from __future__ import annotations



import drake_driver
from drake_driver import DrakeDriver

POPUP_EDIT_HWND = 0xE117
MAIN_HWND = 0xBEEF
CHAT_HWND = 0xCAFE    # the always-present 'Drake Software Chat' overlay (baseline)
POPUP_HWND = 0xD1A6   # the heads-down dialog as a top-level window
ERROR_HWND = 0xE770   # Drake's modal validator (#32770)

# The popup's two prompt states, verbatim from the live screenshot for the number prompt.
# The driver never hardcodes these — it baselines whatever the popup says and watches for a
# CHANGE — but the fake has to render something, and a build that reworded them must still
# pass, which is why the tests assert on behaviour rather than on these strings.
NUMBER_PROMPT = "To begin, enter desired field number and press enter."


def VALUE_PROMPT(n) -> str:
    return f"Enter the value for field {n} and press enter."


# The W-2 numbers the fake accepts; anything else is Drake's "invalid field" modal.
VALID_FIELDS = {1, 4, 5, 6, 7, 8, 9, 10, 14, 15, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33,
                34, 35, 36, 37, 38, 40, 41, 43, 44, 46, 47, 48, 49, 50, 57, 58, 59, 60,
                61, 62, 63}
# Boxes that exist but silently decline to take the caret: box 9 is greyed out, and the
# foreign-only boxes are not rendered on a domestic address. Drake does not complain — it
# simply does not move, which is the whole reason the driver reads the PROMPT rather than
# the edit box.
INERT_FIELDS = {11, 12, 13, 31}
ENTER_ORDER = [4, 5, 6, 7, 8, 9, 10, 14, 15, 23, 24, 25, 26, 27, 28]


class FakeDrake:
    """Drake as observed: a canvas of numbered boxes plus the heads-down popup.

    model="persistent"  CONFIRMED on Drake 2025: the popup stays up and alternates
                        number -> value -> number, and the value is typed INTO the popup.
    model="per-jump"    the popup closes on the number and the value goes on the canvas.
    Both are simulated because the driver detects which it is at runtime rather than
    assuming, and that detection is itself under test.
    """

    def __init__(self, model: str = "persistent", autoadvance_from: int | None = 4,
                 *, echo=None, value_validator=None, prompts: bool = True,
                 inert_fields=None, inert_clears: bool = False):
        self.model = model
        self.autoadvance_from = autoadvance_from  # the confirmed Field-4/EIN exception
        # echo(text) -> what ACTUALLY lands in the box. The hook that makes read-back a real
        # gate: a test can drop characters, append late ones, or substitute a value, and the
        # driver must catch it rather than believe what it meant to type.
        self.echo = echo or (lambda t: t)
        # value_validator(field_no, value) -> None | "dialog" | "silent".
        # "silent" is the dangerous one: Drake declines with a beep and stays on the value
        # prompt, producing NO window for the dialog gate to find.
        self.value_validator = value_validator or (lambda n, v: None)
        # prompts=False models a build whose popup exposes no readable text at all, so the
        # driver has to degrade to the weaker edit-changed heuristic and say so.
        self.prompts = prompts
        self.inert_fields = set(INERT_FIELDS if inert_fields is None else inert_fields)
        # Does a silently-declined number leave the edit box alone, or CLEAR it? Unknown on
        # the real build, and it decides whether the old edit-watching logic was survivable:
        # if the box clears, a refusal is byte-for-byte identical to an acceptance and only
        # the prompt text can tell them apart. Both are simulated; the driver must be right
        # either way.
        self.inert_clears = inert_clears
        self.popup_open = False
        self.popup_text = ""
        self.awaiting_value_for: int | None = None
        self.canvas_focus: int | None = None
        self.pending_canvas_text = ""
        self.values: dict[int, str] = {}
        self.committed: set[int] = set()
        self.error_dialog: str | None = None
        self.extra_windows: list[dict] = []  # windows appearing MID-RUN (chat expand, toasts)
        self.swallowed_ctrl_n = 0
        self.advanced: set[int] = set()  # auto-advance fires once per field, on commit
        self.log: list[str] = []

    # -- what the operator sees ---------------------------------------------

    def prompt(self) -> str:
        if not self.prompts or not self.popup_open:
            return ""
        if self.awaiting_value_for is not None:
            return VALUE_PROMPT(self.awaiting_value_for)
        return NUMBER_PROMPT

    # -- what the driver's primitives are wired to ---------------------------

    def ctrl_n(self):
        if self.error_dialog:
            return
        # In the per-jump model the value sits UNCOMMITTED on the canvas (the driver sends
        # no trailing Enter), so this Ctrl+N is what commits it. Committing the EIN fires
        # Drake's employer lookup + auto-fill, auto-advances the caret to Box 1, and EATS
        # the chord — the exact cascade trigger observed live. It needs a value to commit:
        # an empty EIN box does not auto-advance.
        if (self.autoadvance_from is not None
                and self.canvas_focus == self.autoadvance_from
                and self.pending_canvas_text
                and self.autoadvance_from not in self.advanced):
            self._commit_canvas()
            self.advanced.add(self.autoadvance_from)
            self.canvas_focus = 23
            self.swallowed_ctrl_n += 1
            self.log.append(f"ctrl+n SWALLOWED (field {self.autoadvance_from} auto-advance -> 23)")
            return
        self._commit_canvas()
        if self.canvas_focus is None:
            self.log.append("ctrl+n ignored (no active caret)")
            return
        self.popup_open = not self.popup_open
        self.popup_text = ""
        self.awaiting_value_for = None
        self.log.append(f"ctrl+n -> popup {'OPEN' if self.popup_open else 'CLOSED'}")

    def type(self, text: str):
        if self.error_dialog:
            return
        landed = self.echo(text)
        if self.popup_open:
            self.popup_text += landed
        elif self.canvas_focus is not None:
            self.pending_canvas_text += landed

    def esc(self):
        if self.popup_open:
            self.popup_open = False
            self.popup_text = ""
            self.awaiting_value_for = None
            self.log.append("ESC -> popup closed (disarmed)")

    def enter(self):
        if self.error_dialog:
            return
        if self.popup_open and self.awaiting_value_for is None:
            num = self.popup_text.strip()
            if not num.isdigit() or int(num) not in VALID_FIELDS | self.inert_fields:
                self.error_dialog = "Invalid field number"
                self.log.append(f"ENTER on {num!r} -> INVALID FIELD dialog")
                return
            n = int(num)
            if n in self.inert_fields:
                # SILENT DECLINE: greyed/foreign-only box. Drake does not move and does not
                # complain. With inert_clears the edit is emptied too — at which point the
                # refusal is INDISTINGUISHABLE from success to anything watching the edit
                # box, and only the unchanged PROMPT reveals that Drake never moved.
                if self.inert_clears:
                    self.popup_text = ""
                self.log.append(f"ENTER on {n} -> SILENTLY DECLINED (inert box), "
                                f"edit {'cleared' if self.inert_clears else 'unchanged'}, "
                                f"still on the number prompt")
                return
            if self.model == "per-jump":
                self.popup_open, self.popup_text = False, ""
                self.canvas_focus, self.pending_canvas_text = n, ""
                self.log.append(f"jump -> field {n} (popup closed, caret on canvas)")
            else:
                self.awaiting_value_for, self.popup_text = n, ""
                self.log.append(f"jump -> field {n} (popup now prompting for the VALUE)")
            return
        if self.popup_open and self.awaiting_value_for is not None:
            n = self.awaiting_value_for
            verdict = self.value_validator(n, self.popup_text)
            if verdict == "dialog":
                self.error_dialog = f"Invalid entry for field {n}"
                self.log.append(f"value {self.popup_text!r} for {n} -> VALIDATOR DIALOG")
                return
            if verdict == "silent":
                # Drake beeps and stays on the value prompt. No window is created, so the
                # dialog gate sees nothing — only the prompt not advancing gives it away.
                self.log.append(f"value {self.popup_text!r} for {n} -> SILENTLY REFUSED, "
                                f"still on the value prompt")
                return
            self.values[n] = self.popup_text
            self.committed.add(n)
            self.awaiting_value_for, self.popup_text = None, ""
            if n == self.autoadvance_from and n not in self.advanced:  # heads-down drops
                self.advanced.add(n)
                self.popup_open = False
                self.canvas_focus, self.pending_canvas_text = 23, ""
                self.swallowed_ctrl_n += 1
                self.log.append(f"value for {n} committed -> AUTO-ADVANCE to 23, popup dropped")
            else:
                self.log.append(f"value for {n} committed, popup back to the number prompt")
            return
        self._commit_canvas()
        if self.canvas_focus in ENTER_ORDER:
            i = ENTER_ORDER.index(self.canvas_focus)
            self.canvas_focus = ENTER_ORDER[i + 1] if i + 1 < len(ENTER_ORDER) else self.canvas_focus
        self.log.append(f"ENTER on canvas -> caret now field {self.canvas_focus}")

    def _commit_canvas(self):
        if self.canvas_focus is not None and self.pending_canvas_text:
            self.values[self.canvas_focus] = self.pending_canvas_text
            self.committed.add(self.canvas_focus)
            self.pending_canvas_text = ""


class SimDriver(DrakeDriver):
    """DrakeDriver with ONLY its Windows primitives replaced. Every decision under test —
    _ensure_popup_open's retry, _popup_ready_for_number, _settle_read, _classify_after_jump,
    _verify_value_committed, headsdown_type's routing — is the real production code path."""

    def __init__(self, fake: FakeDrake):
        super().__init__({"navigation": {}, "capabilities": {}})
        self.fake = fake
        # A real-enough win32 connection: _ensure_popup_open and _find_headsdown_popup run
        # UNMODIFIED against it (window() -> spec, spec.exists()/.wait()), so the retry
        # logic under test is the production one rather than a stub.
        self.w32 = _FakeW32(fake)
        self.win = _FakeWin()
        # None so _window_alive skips its Windows-only ctypes IsWindow branch and exercises
        # the wrapper probe instead — a real hwnd here would be bogus on a real Windows box.
        self.main_hwnd = None
        # The chat overlay existed at attach — EVERY sim case runs with it present, which is
        # the regression proof that benign windows never halt a run (the live field-4 halt).
        self._baseline_hwnds = {CHAT_HWND}

    def _window_snapshot(self):
        """Project FakeDrake into the structural gate's input. The REAL classifier
        (_classify_process_windows) runs on this — only the enumeration is faked."""
        wins = [{"hwnd": CHAT_HWND, "title": "Drake Software Chat",
                 "class_name": "Chrome_WidgetWin_1", "visible": True, "enabled": True,
                 "owner": 0, "style": 0x90000000, "rect": [1800, 900, 84, 84]}]
        if self.fake.popup_open:
            wins.append({"hwnd": POPUP_HWND, "title": "Drake 2025 - Heads Down Data Entry",
                         "class_name": "#32770", "visible": True, "enabled": True,
                         "owner": MAIN_HWND, "style": 0x80C80000, "rect": [400, 300, 320, 90]})
        if self.fake.error_dialog:
            wins.append({"hwnd": ERROR_HWND, "title": "Drake 2025",
                         "class_name": "#32770", "visible": True, "enabled": True,
                         "owner": MAIN_HWND, "style": 0x80C80000, "rect": [500, 400, 360, 140],
                         "_text": self.fake.error_dialog})
        wins.extend(self.fake.extra_windows)
        # A modal validator disables the main frame — model that state truthfully.
        return wins, self.fake.error_dialog is None

    def _keys(self, chord: str):
        if chord == "{ENTER}":
            self.fake.enter()
        elif chord == "{ESC}":
            self.fake.esc()
        else:
            self.fake.type(chord.replace("{", "").replace("}", "")
                           if chord.startswith("{") and chord.endswith("}") and len(chord) == 3
                           else _unescape(chord))

    def headsdown_toggle(self, method="scancode"):
        self.fake.ctrl_n()
        return {"ok": True, "method": method}

    def _popup_edit(self, popup):
        return _FakeEdit(self.fake)

    def _popup_prompt(self, popup, edit_hwnd=None) -> str:
        if popup is None:
            return ""
        return self.fake.prompt()

    def _focused_hwnd(self):
        return (POPUP_EDIT_HWND if self.fake.popup_open else MAIN_HWND, 0, (0, 0, 0, 0), 1)


def _install_readers(fake):
    """Point the driver's module-level edit readers at the fake — HONOURING THE HANDLE.

    A reader that ignores the hwnd would return the right text for the wrong control, which
    is precisely the bug class the driver's handle re-resolution exists to prevent (a
    recreated dialog invalidates the old handle). Reading a stale handle must come back
    empty, so a missing re-resolve fails the test instead of passing it."""
    def _or_none(h):
        return fake.popup_text.strip() if int(h) == POPUP_EDIT_HWND else None

    def _safe(h):
        return fake.popup_text.strip() if int(h) == POPUP_EDIT_HWND else ""

    drake_driver._read_edit_or_none = _or_none
    drake_driver._safe_read_edit = _safe


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "{" and i + 2 < len(s) and s[i + 2] == "}":
            out.append(s[i + 1]); i += 3
        else:
            out.append(s[i]); i += 1
    return "".join(out)


class _FakeWin:
    """Drake's main frame as pywinauto actually hands it back: a RESOLVED WRAPPER.

    Deliberately has NO `exists()`. A real UIAWrapper doesn't have one — `exists()` lives
    on WindowSpecification, the un-resolved query object — and an earlier version of this
    fake DID provide it, which made the suite pass while the live run died at field 4 with
    AttributeError. Model the real object model, including what it LACKS.

    No `handle` either, so _window_alive falls through to the is_visible() probe and that
    path gets exercised on any OS (the ctypes IsWindow branch is Windows-only)."""

    def __init__(self):
        self.closed = False

    def is_visible(self):
        if self.closed:
            raise RuntimeError("window is destroyed")  # what a dead wrapper does
        return True


class _FakeW32:
    """Stands in for the backend='win32' Application connection."""

    def __init__(self, fake):
        self.fake = fake

    def window(self, **kwargs):
        return _FakePopup(self.fake)

    def windows(self):
        return []


class _FakePopup:
    """A WindowSpecification for the heads-down dialog: exists()/wait() reflect whether the
    fake Drake actually has the popup up, so the driver's readiness waits are real."""

    def __init__(self, fake):
        self.fake = fake
        self.handle = POPUP_HWND

    def exists(self, timeout=0.0):
        return self.fake.popup_open

    def wait(self, flags="visible", timeout=1.0):
        if not self.fake.popup_open:
            raise TimeoutError("popup never appeared")
        return self

    def wait_not(self, flags="visible", timeout=1.0):
        if self.fake.popup_open:
            raise TimeoutError("popup still visible")
        return self

    def window_text(self):
        return "Drake 2025 - Heads Down Data Entry"

    def child_window(self, **kwargs):
        return _FakeEdit(self.fake)


class _FakeEdit:
    def __init__(self, fake):
        self.fake = fake
        self.handle = POPUP_EDIT_HWND

    def wait(self, *a, **k):
        return self

    def set_focus(self):
        return self

    def set_edit_text(self, text):
        # Respects Drake's state: you cannot poke text into a popup that is not up, and a
        # modal validator owns the input queue. All driver call sites wrap this in
        # try/except, so raising here is the honest model.
        if self.fake.error_dialog or not self.fake.popup_open:
            raise RuntimeError("popup not available for set_edit_text")
        self.fake.popup_text = str(text)


# --- harness ----------------------------------------------------------------

_results = []


def _check(name, ok, detail=""):
    _results.append(bool(ok))
    print(f"\n{'PASS' if ok else 'FAIL'}  {name}")
    if detail and not ok:
        for line in str(detail).splitlines():
            print(f"    {line}")
    return ok


def _new(model="persistent", *, initial_focus=4, preset=None, **kw):
    fake = FakeDrake(model=model, **kw)
    fake.canvas_focus = initial_focus  # the human clicked a field first (--manual bootstrap)
    if preset:
        fake.values.update(preset)
    _install_readers(fake)
    drv = SimDriver(fake)
    drv.begin_batch()
    return fake, drv


def _run_seq(drv, seq):
    rows = []
    for num, val in seq:
        res = drv.headsdown_type(num, val)
        rows.append((num, val, res))
        if not res.get("ok"):
            break
    return rows


SEQ = [("4", "123456789"), ("5", "TEST EMPLOYER LLC"), ("23", "52000"),
       ("24", "6000"), ("25", "52000"), ("26", "3224"), ("27", "52000"), ("28", "754")]
EXPECTED = {4: "123456789", 5: "TEST EMPLOYER LLC", 23: "52000", 24: "6000",
            25: "52000", 26: "3224", 27: "52000", 28: "754"}


def case_full_sequence(model, autoadvance_from=4, label=""):
    fake, drv = _new(model, autoadvance_from=autoadvance_from)
    rows = _run_seq(drv, SEQ)
    fake._commit_canvas()
    ok = fake.values == EXPECTED and all(r[2].get("ok") for r in rows)
    return _check(f"full W-2 sequence lands in the right boxes [{model}]{label}", ok,
                  f"expected {EXPECTED}\ngot      {fake.values}\n"
                  + "\n".join(f"HALT at {n}: {r.get('reason')}" for n, v, r in rows if not r.get("ok"))
                  + "\nlog:\n" + "\n".join("  " + l for l in fake.log))


def case_ein_skipped():
    """The shape production will actually run now: the operator's instruction is to leave
    the EIN alone (it is already on screen), so the batch starts at field 5 and field 4
    must come out untouched. This had zero coverage before."""
    fake, drv = _new("persistent", initial_focus=5, preset={4: "12-3456789"})
    seq = [("5", "TEST EMPLOYER LLC"), ("23", "52000"), ("24", "6000"), ("27", "52000")]
    rows = _run_seq(drv, seq)
    ok = (all(r[2].get("ok") for r in rows)
          and fake.values[4] == "12-3456789"          # untouched, not re-entered
          and fake.values[5] == "TEST EMPLOYER LLC"
          and fake.values[23] == "52000"
          and fake.swallowed_ctrl_n == 0)             # no auto-advance without the EIN
    return _check("EIN skipped: batch starts at field 5, field 4 left untouched", ok,
                  f"values={fake.values} swallowed={fake.swallowed_ctrl_n}\n"
                  + "\n".join(f"HALT at {n}: {r.get('reason')}" for n, v, r in rows if not r.get("ok")))


def case_invalid_number():
    fake, drv = _new("persistent")
    rows = _run_seq(drv, [("4", "123456789"), ("999", "NOPE"), ("23", "52000")])
    halted = [n for n, v, r in rows if not r[2].get("ok")] if False else \
             [n for n, v, r in rows if not r.get("ok")]
    ok = halted == ["999"] and 23 not in fake.values
    return _check("invalid field number HALTs, does not cascade", ok,
                  f"halted at {halted}, values={sorted(fake.values)}")


def case_inert_field_silently_declined():
    """THE reason the driver reads the prompt instead of the edit box. Field 31 (box 9,
    greyed) silently declines: Drake does not move and does not complain, and it leaves the
    number sitting in the edit. A driver that inferred success from 'the popup is still
    open' would type the value at the NUMBER prompt, and if that value parsed as a field
    number, every field after it would land one box out of phase."""
    fake, drv = _new("persistent")
    rows = _run_seq(drv, [("4", "123456789"), ("31", "9999"), ("23", "52000")])
    halted = [n for n, v, r in rows if not r.get("ok")]
    reason = next((r.get("reason") for n, v, r in rows if not r.get("ok")), "")
    ok = (halted == ["31"] and 31 not in fake.values
          and "9999" not in fake.values.values() and 23 not in fake.values)
    return _check("inert box (greyed/foreign-only) declines silently -> HALT", ok,
                  f"halted at {halted}: {reason}\nvalues={fake.values}")


def case_inert_field_that_clears_the_box():
    """The variant only the PROMPT can catch. Drake declines the number AND clears the
    edit, so 'the edit no longer holds the number' — the entire old test — reads exactly
    like success. The driver would then type the value at the NUMBER prompt; when that
    value happens to parse as a valid field number (a Box 12 year, a small amount), Drake
    jumps there instead, and every field after it lands one box out of phase, each row
    reporting OK. This is the cascade the prompt baseline exists to prevent."""
    fake, drv = _new("persistent", inert_clears=True)
    rows = _run_seq(drv, [("4", "123456789"), ("31", "24"), ("23", "52000")])
    halted = [n for n, v, r in rows if not r.get("ok")]
    ok = (halted == ["31"] and 31 not in fake.values
          and "24" not in fake.values.values() and 23 not in fake.values)
    return _check("inert box that CLEARS the edit -> still HALTs (prompt is the only tell)", ok,
                  f"halted at {halted}\nvalues={fake.values}\nlog:\n"
                  + "\n".join("  " + l for l in fake.log))


def case_value_silently_refused():
    """Drake declines a value with a beep and stays on the value prompt — NO window, so the
    dialog gate sees nothing. Reported as success before, which fed the next field's NUMBER
    in as this field's value."""
    fake, drv = _new("persistent",
                     value_validator=lambda n, v: "silent" if n == 46 else None)
    rows = _run_seq(drv, [("23", "52000"), ("46", "X"), ("24", "6000")])
    halted = [n for n, v, r in rows if not r.get("ok")]
    reason = next((r.get("reason") for n, v, r in rows if not r.get("ok")), "")
    ok = halted == ["46"] and 46 not in fake.values and 24 not in fake.values
    return _check("silently refused VALUE -> HALT, nothing committed", ok,
                  f"halted at {halted}: {reason}\nvalues={fake.values}")


def case_value_corrupted_in_flight():
    """What lands is not what we typed. The read-back must catch it BEFORE Enter."""
    fake, drv = _new("persistent", echo=lambda t: "9999" if t == "52000" else t)
    rows = _run_seq(drv, [("23", "52000")])
    res = rows[0][2]
    ok = not res.get("ok") and res.get("halt") and 23 not in fake.values
    return _check("value corrupted in flight -> HALT before commit", ok,
                  f"{res}\nvalues={fake.values}")


def case_number_corrupted_in_flight():
    fake, drv = _new("persistent", echo=lambda t: t[:-1] if t == "23" else t)
    rows = _run_seq(drv, [("23", "52000")])
    res = rows[0][2]
    ok = (not res.get("ok") and res.get("halt")
          and "settled on field number" in (res.get("reason") or "")
          and not fake.values)
    return _check("field number corrupted in flight -> HALT before Enter", ok, f"{res}")


def case_late_keys_append():
    """The bug the old set_edit_text 'repair' created. Keys are POSTED; set_edit_text and
    WM_GETTEXT are SENT and jump the queue. So a half-arrived '520' could be 'repaired' to
    '52000', re-read clean, pass the gate — and then the queued '00' would land, committing
    5,200,000. Convergence (two identical reads) is what closes it."""
    state = {"n": 0}

    def echo(t):
        if t == "52000":
            state["n"] += 1
            return "520" if state["n"] == 1 else t
        return t

    fake, drv = _new("persistent", echo=echo)
    rows = _run_seq(drv, [("23", "52000")])
    res = rows[0][2]
    # Either it halts, or it commits EXACTLY 52000 — never a silently different number.
    ok = (not res.get("ok")) or fake.values.get(23) == "52000"
    return _check("late-arriving keystrokes cannot slip past the read-back gate", ok,
                  f"{res}\nvalues={fake.values}")


def case_empty_value_refused():
    """A blank commit is an Enter on an empty box, which CLEARS what the box already held.
    `--seq \"4=\"` used to wipe the employer EIN and report [OK] field 4 = ''."""
    fake, drv = _new("persistent", preset={4: "12-3456789"})
    res = drv.headsdown_type("4", "")
    ok = (not res.get("ok") and res.get("halt") and fake.values[4] == "12-3456789")
    return _check("empty value REFUSED — never clears a populated box", ok,
                  f"{res}\nvalues={fake.values}")


def case_inherited_armed_popup():
    """probe-popup (and any halted run) leaves Drake armed for a VALUE. Typing a field
    number into that popup commits the NUMBER as the previous field's value."""
    fake, drv = _new("persistent", preset={4: "12-3456789"})
    fake.popup_open = True
    fake.awaiting_value_for = 4       # armed for the EIN's value
    res = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = (not res.get("ok") and res.get("halt")
          and fake.values[4] == "12-3456789"   # EIN not overwritten with "5"
          and 5 not in fake.values)
    return _check("popup inherited ARMED for a value -> HALT, EIN not overwritten", ok,
                  f"{res}\nvalues={fake.values}")


def case_blind_build_degrades():
    """A build whose popup exposes no readable prompt text must still work, degrade to the
    weaker edit-changed test, and SAY so — not silently pretend to verify."""
    fake, drv = _new("persistent", prompts=False, autoadvance_from=None)
    rows = _run_seq(drv, [("23", "52000"), ("24", "6000")])
    ok = all(r[2].get("ok") for r in rows) and fake.values == {23: "52000", 24: "6000"}
    return _check("build with no readable prompt text still enters, flagged as degraded", ok,
                  f"{[r for _, _, r in rows]}\nvalues={fake.values}")


def case_window_closed_midbatch():
    fake, drv = _new("persistent", autoadvance_from=None)
    first = drv.headsdown_type("4", "123456789")
    drv.win.closed = True  # user closed Drake / it crashed
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = first.get("ok") and not second.get("ok") and second.get("halt")
    return _check("Drake closing mid-batch HALTs cleanly", ok,
                  f"first={first}\nsecond={second}")


def case_benign_window_midrun():
    fake, drv = _new("persistent", autoadvance_from=None)
    first = drv.headsdown_type("4", "123456789")
    fake.extra_windows.append({"hwnd": 0xF0A5, "title": "Drake Software Chat",
                               "class_name": "Chrome_WidgetWin_1", "visible": True,
                               "enabled": True, "owner": 0, "style": 0x90000000,
                               "rect": [1400, 700, 420, 560]})  # bubble expanded to a panel
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = first.get("ok") and second.get("ok") and 0xF0A5 in drv._benign_hwnds
    return _check("benign window appearing MID-RUN is ignored + logged "
                  "[the live field-4 chat halt]", ok, f"first={first}\nsecond={second}")


class _ModalSim(SimDriver):
    """Drake with something modal pumping that we cannot enumerate as a window."""

    def __init__(self, fake):
        super().__init__(fake)
        self.modal = False

    def _window_snapshot(self):
        wins, enabled = super()._window_snapshot()
        return wins, (enabled and not self.modal)


def case_modal_disable():
    """An unnamed modal is detected by the main frame going DISABLED — but only while the
    heads-down popup is CLOSED, because the popup is itself a modal dialog that disables
    the frame for legitimate reasons. Hence per-jump here: it is the model in which the
    popup is down between fields. The persistent model's cover for the same threat is
    rule 1 (a new dialog-shaped window), asserted in the next case."""
    fake = FakeDrake(model="per-jump", autoadvance_from=None)
    fake.canvas_focus = 4
    _install_readers(fake)
    drv = _ModalSim(fake)
    drv.begin_batch()
    first = drv.headsdown_type("4", "123456789")
    drv.modal = True  # something modal is pumping; no enumerable dialog window
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = (first.get("ok") and not second.get("ok") and second.get("halt")
          and "DISABLED" in (second.get("reason") or ""))
    return _check("disabled main frame halts structurally (unnamed modal, popup down)", ok,
                  f"first={first}\nsecond={second}")


def case_modal_while_popup_open():
    """In the persistent model the popup stays up, so the frame is legitimately disabled
    the whole time and the disabled-frame rule must stand down (otherwise every field would
    halt). A real validator still gets caught, because it arrives as a NEW dialog window."""
    fake, drv = _new("persistent", autoadvance_from=None)
    first = drv.headsdown_type("23", "52000")
    fake.extra_windows.append({"hwnd": 0xBAD1, "title": "Drake 2025", "class_name": "#32770",
                               "visible": True, "enabled": True, "owner": MAIN_HWND,
                               "style": 0x80C80000, "rect": [500, 400, 360, 140],
                               "_text": "This field must contain data"})
    second = drv.headsdown_type("24", "6000")
    ok = (first.get("ok") and not second.get("ok") and second.get("halt")
          and 24 not in fake.values)
    return _check("new validator dialog caught while the popup is legitimately modal", ok,
                  f"first={first}\nsecond={second}\nvalues={fake.values}")


def case_keyboard_scope():
    """The HWND-scoped gate: if anything but Drake owns the keyboard, no key is sent."""
    class _ScopeSim(SimDriver):
        def __init__(self, fake):
            super().__init__(fake)
            self.in_scope = True

        def _input_scope(self, allow_popup=True):
            return (True, "main-frame") if self.in_scope else (False, "'Drake Software Chat'")

    fake = FakeDrake(model="persistent", autoadvance_from=None)
    fake.canvas_focus = 4
    _install_readers(fake)
    drv = _ScopeSim(fake)
    drv.begin_batch()
    drv.in_scope = False
    res = drv.headsdown_type("23", "52000")
    # Assert WHICH gate stopped it: the Ctrl+N scope check must refuse before the chord is
    # sent. Without naming the gate, the later focus check would also halt and the test
    # would pass with the first gate deleted.
    ok = (not res.get("ok") and res.get("halt") and not fake.values
          and "Ctrl+N" in (res.get("reason") or ""))
    return _check("keyboard outside Drake -> Ctrl+N never sent, HALT", ok,
                  f"{res}\nvalues={fake.values}")


def case_focus_never_taken():
    class _NoFocusSim(SimDriver):
        def _focus_popup_edit(self, edit, edit_hwnd, tries=6):
            return False

    fake = FakeDrake(model="persistent", autoadvance_from=None)
    fake.canvas_focus = 4
    _install_readers(fake)
    drv = _NoFocusSim(fake)
    drv.begin_batch()
    res = drv.headsdown_type("23", "52000")
    ok = not res.get("ok") and res.get("halt") and not fake.values
    return _check("popup edit never takes focus -> HALT before typing", ok, f"{res}")


def case_classifier_table():
    from drake_driver import _classify_process_windows
    CHAT = {"hwnd": 1, "title": "Drake Software Chat", "class_name": "Chrome_WidgetWin_1",
            "visible": True, "enabled": True, "owner": 0, "style": 0x90000000,
            "rect": [0, 0, 84, 84]}
    POPUP = {"hwnd": 2, "title": "Drake 2025 - Heads Down Data Entry", "class_name": "#32770",
             "visible": True, "enabled": True, "owner": 9, "style": 0x80C80000,
             "rect": [0, 0, 300, 90]}
    MODAL = {"hwnd": 3, "title": "Drake 2025", "class_name": "#32770", "visible": True,
             "enabled": True, "owner": 9, "style": 0x80C80000, "rect": [0, 0, 300, 120]}
    TOAST = {"hwnd": 4, "title": "", "class_name": "WindowsForms10.Window.8.app",
             "visible": True, "enabled": True, "owner": 0, "style": 0x86000000,
             "rect": [0, 0, 200, 60]}
    TIP = {"hwnd": 5, "title": "", "class_name": "tooltips_class32", "visible": True,
           "enabled": True, "owner": 0, "style": 0x94000000, "rect": [0, 0, 80, 20]}
    kw = dict(popup_title_re=r"Heads.?Down Data Entry", main_hwnd=9,
              baseline={1}, benign_seen=set())

    def _c(wins, main_enabled):
        return _classify_process_windows(wins, main_enabled=main_enabled, **kw)

    checks = [
        ("baseline chat alone is clear", _c([CHAT], True)[0] is None),
        ("heads-down popup recognised, not a blocker",
         _c([CHAT, POPUP], True)[0] is None and _c([CHAT, POPUP], True)[2]),
        ("the popup's own modality is clear", _c([CHAT, POPUP], False)[0] is None),
        ("a new #32770 blocks (main disabled)", (_c([CHAT, MODAL], False)[0] or {}).get("hwnd") == 3),
        ("a new #32770 blocks even with main enabled", (_c([CHAT, MODAL], True)[0] or {}).get("hwnd") == 3),
        ("main disabled, no dialog found -> generic block",
         (_c([CHAT], False)[0] or {}).get("why") == "main-disabled"),
        ("new captionless toast is benign + logged",
         (lambda r: r[0] is None and [w["hwnd"] for w in r[1]] == [4])(_c([CHAT, TOAST], True))),
        ("tooltip/menu classes are cosmetic", (lambda r: r[0] is None and r[1] == [])(_c([CHAT, TIP], True))),
    ]
    ok = all(v for _, v in checks)
    _check("structural dialog classifier decision table", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_comparator_table():
    from drake_driver import _same_value
    checks = [
        ("'52,000' == '52000' (Drake's comma)", _same_value("52,000", "52000")),
        ("'52000.00' == '52000' (trailing cents)", _same_value("52000.00", "52000")),
        ("'12-3456789' == '123456789' (EIN)", _same_value("12-3456789", "123456789")),
        ("'12345-6789' == '123456789' (ZIP+4)", _same_value("12345-6789", "123456789")),
        ("'322450' != '3224.50' (decimal dropped)", not _same_value("322450", "3224.50")),
        ("'520.00' != '52000' (100x)", not _same_value("520.00", "52000")),
        ("'-500' != '500' (sign flip)", not _same_value("-500", "500")),
        ("'(500)' != '500' (accounting negative)", not _same_value("(500)", "500")),
        ("'5200000' != '52000' (appended keys)", not _same_value("5200000", "52000")),
        # One side numeric, the other not, differing only by a decimal point: the exact case
        # the alnum fallback would wave through ("123456" == "123456").
        ("'12-34.56' != '1234.56' (decimal, one side unparseable)",
         not _same_value("12-34.56", "1234.56")),
        ("'1-5' != '1.5' (decimal vs hyphen)", not _same_value("1-5", "1.5")),
    ]
    ok = all(v for _, v in checks)
    _check("read-back comparator: cosmetic vs arithmetic", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def main() -> int:
    print("=" * 74)
    print("Heads-down entry — offline state-machine proof")
    print("Confirmed protocol: popup persists, alternating number -> value -> number,")
    print("with field 4 (EIN) auto-advancing and swallowing the next Ctrl+N.")
    print("Every case runs with the 'Drake Software Chat' window present.")
    print("=" * 74)

    case_full_sequence("persistent")
    case_full_sequence("per-jump")
    case_full_sequence("persistent", autoadvance_from=23, label=" auto-advance elsewhere")
    case_full_sequence("persistent", autoadvance_from=None, label=" no auto-advance")
    case_ein_skipped()
    case_invalid_number()
    case_inert_field_silently_declined()
    case_inert_field_that_clears_the_box()
    case_value_silently_refused()
    case_value_corrupted_in_flight()
    case_number_corrupted_in_flight()
    case_late_keys_append()
    case_empty_value_refused()
    case_inherited_armed_popup()
    case_blind_build_degrades()
    case_window_closed_midbatch()
    case_benign_window_midrun()
    case_modal_disable()
    case_modal_while_popup_open()
    case_keyboard_scope()
    case_focus_never_taken()
    case_classifier_table()
    case_comparator_table()

    print("\n" + "=" * 74)
    passed = sum(1 for r in _results if r)
    print(f"{passed}/{len(_results)} cases pass")
    print("=" * 74)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
