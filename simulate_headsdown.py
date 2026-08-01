#!/usr/bin/env python3
"""
Offline simulator for the heads-down entry state machine.

The driver's logic can only be exercised for real on the Windows VM, which makes every
change a trip to the VM and a live Drake return. This fake Drake reproduces the behaviours
we have actually OBSERVED — including the Field-4/EIN auto-advance that swallows the next
Ctrl+N and produced the wrong-box cascade — so the state machine can be proven anywhere,
in a second, before it ever touches a client return.

It fakes Drake, NOT pywinauto's correctness: focus/WM_GETTEXT/window-enumeration behaviour
on the real thing is still VM-verified. What this proves is the LOGIC — that numbers and
values are routed to the right target under both popup models, that a swallowed Ctrl+N
recovers instead of cascading, and that anomalies HALT instead of writing on.

    python simulate_headsdown.py
"""

from __future__ import annotations

import sys

import drake_driver
from drake_driver import DrakeDriver

POPUP_EDIT_HWND = 0xE117
MAIN_HWND = 0xBEEF
# The W-2 numbers the fake accepts; anything else is Drake's "invalid field" modal.
VALID_FIELDS = {4, 5, 6, 7, 8, 9, 10, 13, 14, 15, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33,
                34, 35, 37, 38, 40, 41, 43, 44, 46, 47, 48, 57, 58, 59, 60, 61, 62, 63}
ENTER_ORDER = [4, 5, 6, 7, 8, 9, 10, 13, 14, 15, 23, 24, 25, 26, 27, 28]


class FakeDrake:
    """Drake as observed: a canvas of numbered boxes plus the heads-down popup.

    model="per-jump"    the popup closes on the number; the value is typed on the canvas.
    model="persistent"  the popup stays and prompts for the value.
    Both are simulated because the driver is supposed to detect which it is at runtime.
    """

    def __init__(self, model: str = "per-jump", autoadvance_from: int | None = 4):
        self.model = model
        self.autoadvance_from = autoadvance_from  # the confirmed Field-4/EIN exception
        self.popup_open = False
        self.popup_text = ""
        self.awaiting_value_for: int | None = None
        self.canvas_focus: int | None = None
        self.pending_canvas_text = ""
        self.values: dict[int, str] = {}
        self.committed: set[int] = set()
        self.error_dialog: str | None = None
        self.swallowed_ctrl_n = 0
        self.advanced: set[int] = set()  # auto-advance fires once per field, on commit
        self.log: list[str] = []

    # -- what the driver's primitives are wired to ---------------------------

    def ctrl_n(self):
        if self.error_dialog:
            return
        # In the per-jump model the value sits UNCOMMITTED on the canvas (the driver sends
        # no trailing Enter), so this Ctrl+N is what commits it. Committing the EIN fires
        # Drake's employer lookup + auto-fill, auto-advances the caret to Box 1, and EATS
        # the chord — the exact cascade trigger observed on the VM. It needs a value to
        # commit: an empty EIN box does not auto-advance.
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
        if self.popup_open:
            self.popup_text += text
        elif self.canvas_focus is not None:
            self.pending_canvas_text += text

    def enter(self):
        if self.error_dialog:
            return
        if self.popup_open and self.awaiting_value_for is None:
            num = self.popup_text.strip()
            if not num.isdigit() or int(num) not in VALID_FIELDS:
                self.error_dialog = "Invalid field number"
                self.log.append(f"ENTER on {num!r} -> INVALID FIELD dialog")
                return
            n = int(num)
            if self.model == "per-jump":
                self.popup_open, self.popup_text = False, ""
                self.canvas_focus, self.pending_canvas_text = n, ""
                self.log.append(f"jump -> field {n} (popup closed, caret on canvas)")
            else:
                self.awaiting_value_for, self.popup_text = n, ""
                self.log.append(f"jump -> field {n} (popup prompting for value)")
            return
        if self.popup_open and self.awaiting_value_for is not None:
            n = self.awaiting_value_for
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
                self.log.append(f"value for {n} committed, popup back to number prompt")
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
    _ensure_popup_open's retry, _classify_after_jump, headsdown_type's routing — is the
    real production code path."""

    def __init__(self, fake: FakeDrake):
        super().__init__({"navigation": {}, "capabilities": {}})
        self.fake = fake
        self.main_hwnd = MAIN_HWND
        # A real-enough win32 connection: _ensure_popup_open and _find_headsdown_popup run
        # UNMODIFIED against it (window() -> spec, spec.exists()/.wait()), so the retry
        # logic under test is the production one rather than a stub.
        self.w32 = _FakeW32(fake)
        self.win = _FakeWin()

    def _keys(self, chord: str):
        if chord == "{ENTER}":
            self.fake.enter()
        else:
            self.fake.type(chord.replace("{", "").replace("}", "")
                           if chord.startswith("{") and chord.endswith("}") and len(chord) == 3
                           else _unescape(chord))

    def headsdown_toggle(self, method="scancode"):
        self.fake.ctrl_n()
        return {"ok": True, "method": method}

    def _popup_edit(self, popup):
        return _FakeEdit(self.fake)

    def _focused_hwnd(self):
        return (POPUP_EDIT_HWND if self.fake.popup_open else MAIN_HWND, 0, (0, 0, 0, 0), 1)

    def _detect_unexpected_dialog(self):
        if self.fake.error_dialog:
            return {"title": self.fake.error_dialog, "text": self.fake.error_dialog, "handle": 1}
        return None


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "{" and i + 2 < len(s) and s[i + 2] == "}":
            out.append(s[i + 1]); i += 3
        else:
            out.append(s[i]); i += 1
    return "".join(out)


class _FakeWin:
    def exists(self):
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
        self.fake.popup_text = str(text)


def run_case(name, model, seq, expected, *, autoadvance_from=4):
    fake = FakeDrake(model=model, autoadvance_from=autoadvance_from)
    fake.canvas_focus = 4  # the human clicked EIN first (the --manual bootstrap)
    drake_driver._safe_read_edit = lambda h: fake.popup_text.strip()
    drv = SimDriver(fake)

    rows = []
    for num, val in seq:
        res = drv.headsdown_type(num, val)
        rows.append((num, val, res.get("ok"), res.get("reason"), res.get("model")))
        if not res.get("ok"):
            break

    got = {int(k): v for k, v in fake.values.items()}
    fake._commit_canvas()
    got = {int(k): v for k, v in fake.values.items()}
    ok = got == expected and all(r[2] for r in rows)
    print(f"\n{'PASS' if ok else 'FAIL'}  {name}  [model={model}]")
    if not ok:
        print(f"  expected {expected}")
        print(f"  got      {got}")
        for num, val, good, why, m in rows:
            if not good:
                print(f"  HALT at field {num}: {why}")
        print("  fake Drake log:")
        for line in fake.log:
            print(f"    {line}")
    else:
        print(f"  {len(seq)} field(s) all landed in the right boxes; "
              f"Ctrl+N swallowed {fake.swallowed_ctrl_n}x and recovered")
    return ok


def run_halt_case(name, model, seq, expect_halt_at):
    """An anomaly must stop the batch, not write on past it."""
    fake = FakeDrake(model=model)
    fake.canvas_focus = 4
    drake_driver._safe_read_edit = lambda h: fake.popup_text.strip()
    drv = SimDriver(fake)
    halted_at = None
    for num, val in seq:
        res = drv.headsdown_type(num, val)
        if not res.get("ok"):
            halted_at = num
            break
    ok = halted_at == expect_halt_at
    print(f"\n{'PASS' if ok else 'FAIL'}  {name}  [model={model}]")
    print(f"  halted at field {halted_at} (expected {expect_halt_at}); "
          f"boxes written: {sorted(fake.values)}")
    return ok


def main() -> int:
    # The EXACT sequence that cascaded on the VM: EIN, name, then boxes 1-6.
    seq = [("4", "123456789"), ("5", "TEST EMPLOYER LLC"), ("23", "52000"),
           ("24", "6000"), ("25", "52000"), ("26", "3224"), ("27", "52000"), ("28", "754")]
    expected = {4: "123456789", 5: "TEST EMPLOYER LLC", 23: "52000", 24: "6000",
                25: "52000", 26: "3224", 27: "52000", 28: "754"}

    print("=" * 72)
    print("Heads-down entry — offline state-machine proof")
    print("Reproduces the observed Field-4/EIN auto-advance that swallows Ctrl+N.")
    print("=" * 72)

    results = [
        run_case("regression: the cascade sequence", "per-jump", seq, expected),
        run_case("same sequence, persistent popup build", "persistent", seq, expected),
        run_case("auto-advance on a DIFFERENT field still recovers", "per-jump", seq, expected,
                 autoadvance_from=23),
        run_case("no auto-advance at all", "per-jump", seq, expected, autoadvance_from=None),
        run_halt_case("invalid field number HALTs, does not cascade", "per-jump",
                      [("4", "123456789"), ("999", "NOPE"), ("23", "52000")], "999"),
    ]
    print("\n" + "=" * 72)
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} cases pass")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
