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
CHAT_HWND = 0xCAFE    # the always-present 'Drake Software Chat' overlay (baseline)
POPUP_HWND = 0xD1A6   # the heads-down dialog as a top-level window
ERROR_HWND = 0xE770   # Drake's modal validator (#32770)
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
        self.extra_windows: list[dict] = []  # windows appearing MID-RUN (chat expand, toasts)
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
        # A real-enough win32 connection: _ensure_popup_open and _find_headsdown_popup run
        # UNMODIFIED against it (window() -> spec, spec.exists()/.wait()), so the retry
        # logic under test is the production one rather than a stub.
        self.w32 = _FakeW32(fake)
        self.win = _FakeWin()
        # None so _window_alive skips its Windows-only ctypes IsWindow branch and exercises
        # the wrapper probe instead — a real hwnd here would be bogus on a real Windows box.
        # (It also makes _input_scope report "unscoped": the foreground oracle is Windows-only.)
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
    AttributeError. A fake that is more permissive than the real API hides exactly the bugs
    it exists to catch, so: model the real object model, including what it LACKS.

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


def run_window_closed_case():
    """Drake disappearing mid-batch must HALT on the next field, not keep typing into
    nothing. This also covers the live AttributeError that killed the first VM run:
    _window_alive replaced `self.win.exists()`, which does not exist on a resolved
    UIAWrapper."""
    fake = FakeDrake(model="per-jump", autoadvance_from=None)
    fake.canvas_focus = 4
    drake_driver._safe_read_edit = lambda h: fake.popup_text.strip()
    drv = SimDriver(fake)

    first = drv.headsdown_type("4", "123456789")
    drv.win.closed = True  # user closed Drake / it crashed
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")

    ok = bool(first.get("ok")) and not second.get("ok") and second.get("halt")
    print(f"\n{'PASS' if ok else 'FAIL'}  Drake closing mid-batch HALTs cleanly  [model=per-jump]")
    print(f"  field 4 -> ok={first.get('ok')}; after close, field 5 -> "
          f"halt={second.get('halt')} ({second.get('reason')})")
    if not ok and first.get("reason"):
        print(f"  field 4 unexpectedly failed: {first.get('reason')}")
    return ok


def run_benign_midrun_case():
    """A benign window APPEARING mid-run (chat bubble expanding into a panel, a toast)
    must be logged and ignored — the exact class of failure that halted the live run on
    'Drake Software Chat' at field 4."""
    fake = FakeDrake(model="per-jump", autoadvance_from=None)
    fake.canvas_focus = 4
    drake_driver._safe_read_edit = lambda h: fake.popup_text.strip()
    drv = SimDriver(fake)
    first = drv.headsdown_type("4", "123456789")
    fake.extra_windows.append({"hwnd": 0xF0A5, "title": "Drake Software Chat",
                               "class_name": "Chrome_WidgetWin_1", "visible": True,
                               "enabled": True, "owner": 0, "style": 0x90000000,
                               "rect": [1400, 700, 420, 560]})  # bubble expanded to a panel
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = bool(first.get("ok")) and bool(second.get("ok")) and 0xF0A5 in drv._benign_hwnds
    print(f"\n{'PASS' if ok else 'FAIL'}  benign window appearing MID-RUN is ignored + logged"
          f"  [the live field-4 chat halt]")
    if not ok:
        print(f"  field4={first}\n  field5={second}\n  benign={drv._benign_hwnds}")
    return ok


def run_modal_disable_case():
    """A modal that DISABLES the main frame must halt even when the modal window itself
    cannot be named — modality is detected structurally, never by title."""
    class _ModalSim(SimDriver):
        def __init__(self, fake):
            super().__init__(fake)
            self.modal = False

        def _window_snapshot(self):
            wins, enabled = super()._window_snapshot()
            return wins, (enabled and not self.modal)

    fake = FakeDrake(model="per-jump", autoadvance_from=None)
    fake.canvas_focus = 4
    drake_driver._safe_read_edit = lambda h: fake.popup_text.strip()
    drv = _ModalSim(fake)
    first = drv.headsdown_type("4", "123456789")
    drv.modal = True  # something modal is pumping; no enumerable dialog window
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    ok = (bool(first.get("ok")) and not second.get("ok") and second.get("halt")
          and "DISABLED" in (second.get("reason") or ""))
    print(f"\n{'PASS' if ok else 'FAIL'}  disabled main frame halts structurally (unnamed modal)")
    if not ok:
        print(f"  field4={first}\n  field5={second}")
    return ok


def run_classifier_cases():
    """Unit checks on the pure classifier — the decision table of the structural gate."""
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
        ("baseline chat alone is clear",
         _c([CHAT], True)[0] is None),
        ("heads-down popup is recognised, not a blocker",
         _c([CHAT, POPUP], True)[0] is None and _c([CHAT, POPUP], True)[2]),
        ("the popup's own modality (main disabled) is clear",
         _c([CHAT, POPUP], False)[0] is None),
        ("a new #32770 blocks (main disabled)",
         (_c([CHAT, MODAL], False)[0] or {}).get("hwnd") == 3),
        ("a new #32770 blocks even with main still enabled (modeless validator)",
         (_c([CHAT, MODAL], True)[0] or {}).get("hwnd") == 3),
        ("main disabled with no dialog found still blocks, generically",
         (_c([CHAT], False)[0] or {}).get("why") == "main-disabled"
         and (_c([CHAT], False)[0] or {}).get("hwnd") == 0),
        ("a new captionless toast is benign + reported for logging",
         (lambda r: r[0] is None and [w["hwnd"] for w in r[1]] == [4])(_c([CHAT, TOAST], True))),
        ("tooltip/menu classes are cosmetic — not even logged",
         (lambda r: r[0] is None and r[1] == [])(_c([CHAT, TIP], True))),
    ]
    ok = all(v for _, v in checks)
    print(f"\n{'PASS' if ok else 'FAIL'}  structural classifier decision table")
    for name, v in checks:
        print(f"  {'ok  ' if v else 'FAIL'}: {name}")
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
        run_window_closed_case(),
        run_benign_midrun_case(),
        run_modal_disable_case(),
        run_classifier_cases(),
    ]
    print("\n" + "=" * 72)
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} cases pass")
    print("=" * 72)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
