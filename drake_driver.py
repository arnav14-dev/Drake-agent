"""
Drake driver — the pywinauto backend that actually drives Drake's on-screen data
entry, box by box, and reads each box straight back.

It implements the SAME contract as Fynn's `DrakeDriver` TypeScript seam
(src/agents/preparer/adapters/drake/driver.ts): every method returns a plain dict
(an "ack" or a "field read"), never raises for an expected outcome — it raises ONLY
on transport/automation failure, which the caller turns into ok:false.

THE HONEST READ-BACK is the whole point: `read_field` reports HOW it read a value
(method + confidence). If Drake's grid does not expose a box value to UI Automation,
it returns confidence 0 rather than guess — and `capabilities.can_read_field_values`
stays False so Fynn refuses to run live (it will not claim a verified read-back it
cannot deliver).

This file MUST be run and iterated on the Windows VM that has Drake. The focus /
type / read strategies below are best-effort and depend on your Drake build's UI
Automation surface — confirm them with `agent.py probe` and fill binding.json.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

try:
    from pywinauto import Application
    from pywinauto.keyboard import send_keys
    from pywinauto.timings import TimeoutError as PWTimeout
except Exception:  # pragma: no cover - importable on non-Windows for reading only
    Application = None  # type: ignore
    send_keys = None  # type: ignore
    PWTimeout = TimeoutError  # type: ignore  (valid `except` target off-Windows)

try:
    import pyperclip  # clipboard read-back (Plan B — Drake exposes no UIA field values)
except Exception:  # pragma: no cover
    pyperclip = None  # type: ignore

try:
    # OCR read-back (Plan C — Drake exposes NO programmatic value: not UIA, not win32,
    # not clipboard). We screenshot a calibrated field crop and read the pixels.
    import pytesseract  # needs the Tesseract binary installed on the VM
    from PIL import Image  # noqa: F401  (pywinauto capture already returns a PIL image)
except Exception:  # pragma: no cover
    pytesseract = None  # type: ignore
    Image = None  # type: ignore


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class DrakeDriver:
    """Drives one running Drake instance. Attach to Drake AFTER a return is open."""

    def __init__(self, binding: dict, *, key_pause: float = 0.03, dry_run: bool = False,
                 vk_packet: Optional[bool] = None):
        self.binding = binding
        self.nav = binding.get("navigation", {})
        self.caps_cfg = binding.get("capabilities", {})
        # How we read a box back. On Drake Tax 2025 ALL programmatic reads are dead:
        # UIA exposes 0 values, win32 sees 0 inner fields, and clipboard copy returns
        # nothing (Ctrl-chords even break focus + pop modal validators). So on Drake:
        #   "ocr"        -> screenshot a calibrated field crop and OCR it (approximate)
        #   "screenshot" -> no automated value; a human verifies the capture (the floor)
        #   "none"       -> no verification at all (honesty gate keeps Fynn live OFF)
        # "uia"/"clipboard" are kept for a future Drake build or other software.
        self.read_back_method = self.caps_cfg.get("read_back_method", "none")
        # Tight default so we attach to the tax app, not "Drake Software Chat" or a
        # browser tab that merely contains "Drake" (those triggered ElementAmbiguousError).
        self.title_re = binding.get("app_title_re", r"Drake \d{4} Tax Software")
        # The Drake Live Chat overlay is a tiny (~84x84) *topmost* window in the same
        # process, so top_window() grabs it before the data-entry frame. Require a real
        # frame size to skip such overlay/tool widgets. Override via binding if needed.
        mw = binding.get("main_window_min", [600, 400])
        self.min_main_w, self.min_main_h = int(mw[0]), int(mw[1])
        # How literal characters are injected. Drake is a DOS-heritage app that reads the
        # keyboard the OLD way (WM_KEYDOWN + scan codes), so pywinauto's default Unicode
        # "packet" injection is often IGNORED — the caret sits in the field but nothing
        # types. vk_packet=False sends real virtual-key + scan-code events legacy apps
        # accept. Default False for Drake; override per binding, or per run for typetest.
        self.vk_packet = binding.get("send_vk_packet", False) if vk_packet is None else bool(vk_packet)
        # Path to tesseract.exe for OCR read-back, if it isn't on PATH (Windows installs
        # often aren't). e.g. "C:\\Program Files\\Tesseract-OCR\\tesseract.exe".
        self.tesseract_cmd = binding.get("tesseract_cmd")
        self.key_pause = key_pause
        self.dry_run = dry_run
        self.app = None          # backend="uia" connection to the main data-entry frame
        self.win = None          # the resolved main-frame window
        self.w32 = None          # SEPARATE backend="win32" connection (same PID) for the
                                 # heads-down popup dialog — a real HWND Edit the canvas lacks
        self.main_hwnd = None    # cached main-frame handle (focus/dialog allowlist anchor)
        # The heads-down popup is a real dialog window we drive directly (title + edit class).
        self.popup_title_re = self.nav.get("headsdown_popup_title_re", r"Heads.?Down Data Entry")
        self.popup_edit_class = self.nav.get("headsdown_popup_edit_class", "Edit")
        # How the popup's NUMBER prompt reads, so an inherited popup that is actually armed
        # for a VALUE can be told apart from one waiting for a field number. Matched against
        # the popup's child text; on this build: "To begin, enter desired field number and
        # press enter."
        self.number_prompt_re = self.nav.get("headsdown_number_prompt_re", r"field\s*number")
        # Did WE open the popup that is currently up? An inherited popup (left armed by
        # probe-popup or a halted run) has an UNKNOWN prompt state, and typing a field
        # number into a box that is waiting for a VALUE commits the number as the value.
        self._popup_owned = False
        self._warned_prompt_blind = False
        self._warned_popup_foreign = False
        self._popup_hwnd = None  # last resolved popup handle (exact; see _find_popup_hwnd)
        # Structural dialog-gate state (see _detect_unexpected_dialog): windows that already
        # exist at attach are baseline furniture (e.g. the 'Drake Software Chat' overlay) and
        # can never halt a run by existing; windows classified benign mid-run are remembered
        # so each is logged once, not re-litigated per field.
        self.pid = None
        self._baseline_hwnds: set = set()
        self._benign_hwnds: set = set()
        self.benign_notes: list = []

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Attach to the running Drake process and resolve the MAIN data-entry frame —
        never a floating overlay (the Live Chat bubble is a topmost ~84x84 window that
        top_window() would grab first, sending keystrokes to the chat widget)."""
        if self.dry_run:
            return
        if Application is None:
            raise RuntimeError("pywinauto is not available (run on the Windows VM)")
        self.app = self._connect_uia()
        self.win = self._resolve_main_window()
        self.main_hwnd = int(self.win.handle)
        self.pid = int(self.win.element_info.process_id)
        self._connect_win32_popup()  # second connection for the heads-down dialog
        self._snapshot_baseline()    # pre-existing windows = benign furniture, never a halt
        self._warn_if_elevation_mismatch()
        self._foreground()

    def _connect_uia(self):
        """Attach to Drake's process, tolerating pywinauto's ANCHORED title matching.

        `connect(title_re=...)` matches with `re.match`, so `app_title_re` only works if it
        matches from the very first character of the title. The live data-entry frame is
        titled 'Drake 2025 - Data Entry (…)', which the default pattern 'Drake \\d{4} Tax
        Software' cannot match — the same trap that made the heads-down popup unfindable.

        So: try pywinauto's way first (it works when some window does start with the
        pattern), and on failure find the process ourselves with `re.search` over every
        top-level window title and connect by PID."""
        try:
            return Application(backend="uia").connect(title_re=self.title_re, timeout=20)
        except Exception as first:
            if not _WINFN:
                raise
            import re as _re
            pat = _re.compile(self.title_re, _re.I)
            best = None
            for w in _enum_toplevel_windows(None):
                if not w.get("visible") or not pat.search(w.get("title") or ""):
                    continue
                area = (w["rect"][2] or 0) * (w["rect"][3] or 0)
                if best is None or area > best[1]:
                    best = (int(w["pid"]), area)
            if best is None:
                raise RuntimeError(
                    f"no visible window matches app_title_re {self.title_re!r} "
                    f"(searched every process; pywinauto's own anchored match also failed: "
                    f"{first})")
            print(f"  · app_title_re only matched mid-title — connected by process id "
                  f"{best[0]} instead (pywinauto's title_re is anchored at the start).")
            return Application(backend="uia").connect(process=best[0], timeout=20)

    def _warn_if_elevation_mismatch(self) -> None:
        """If Drake runs elevated (as Admin) and this agent does not, Windows UIPI
        SILENTLY discards our keystrokes — no error, fields just stay empty. Warn loudly
        so it's an obvious check, not a mystery. (Diagnosis says this is unlikely on the
        current build — a synthetic ^a still popped Drake's own modal, so input IS getting
        through — but it's a cheap, permanent guard for other setups.)"""
        try:
            import ctypes
            agent_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            drake_elevated = self._process_is_elevated(self.win.element_info.process_id)
            if drake_elevated and not agent_admin:
                print("WARNING: Drake appears to run ELEVATED but this agent is NOT — Windows "
                      "UIPI will SILENTLY DROP keystrokes (fields stay empty, no error). "
                      "Relaunch this agent as Administrator, or run Drake un-elevated.")
        except Exception:
            pass

    def _process_is_elevated(self, pid):
        """Best-effort: is the process at `pid` running elevated? None if we can't tell."""
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TokenElevation = 0x1000, 0x0008, 20
            k32, a32 = ctypes.windll.kernel32, ctypes.windll.advapi32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return None
            try:
                tok = wintypes.HANDLE()
                if not a32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
                    return None
                try:
                    elevated, ret_len = wintypes.DWORD(), wintypes.DWORD()
                    ok = a32.GetTokenInformation(tok, TokenElevation, ctypes.byref(elevated),
                                                 ctypes.sizeof(elevated), ctypes.byref(ret_len))
                    return bool(elevated.value) if ok else None
                finally:
                    k32.CloseHandle(tok)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None

    def _resolve_main_window(self):
        """Pick Drake's main window: the LARGEST top-level window of the Drake process
        that meets a real frame size (min_main_w × min_main_h), preferring ones whose
        title matches app_title_re. Filters out the chat bubble and other tool overlays.
        Falls back to top_window() only if nothing qualifies."""
        candidates = []
        pools = []
        try:
            pools.append(self.app.windows(title_re=self.title_re))
        except Exception:
            pass
        try:
            pools.append(self.app.windows())  # any top-level window of the process
        except Exception:
            pass
        seen = set()
        for pool in pools:
            for w in pool:
                try:
                    handle = getattr(w, "handle", None)
                    if handle in seen:
                        continue
                    seen.add(handle)
                    r = w.rectangle()
                    candidates.append((w, r.width(), r.height()))
                except Exception:
                    continue
        # Real frames only (skips the 84x84 chat bubble); largest area wins.
        big = [c for c in candidates if c[1] >= self.min_main_w and c[2] >= self.min_main_h]
        pool = big or candidates
        if pool:
            pool.sort(key=lambda t: t[1] * t[2], reverse=True)
            return pool[0][0]
        return self.app.top_window()  # last resort — original behaviour

    def _keys(self, chord: str) -> None:
        if self.dry_run:
            print(f"[dry-run] send_keys({chord!r})")
            return
        # vk_packet=False => real VK + scan-code events (legacy/DOS-heritage apps like
        # Drake need this; the default Unicode packets are silently ignored). Special keys
        # ({ENTER}/{TAB}/{ESC}) are VK-based regardless, so this only changes literal chars.
        send_keys(chord, pause=self.key_pause, with_spaces=True, vk_packet=self.vk_packet)

    def _field_binding(self, screen: str, field: str) -> dict:
        scr = self.binding.get("screens", {}).get(screen)
        if not scr:
            raise RuntimeError(f"no binding for screen {screen!r}")
        fb = scr.get("fields", {}).get(field)
        if fb is None:
            raise RuntimeError(f"no binding for field {screen}/{field}")
        return fb

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> dict:
        return {
            "backend": "uia",
            # Honest by default: only True once probe/clip confirms a real read-back.
            "canReadFieldValues": bool(self.caps_cfg.get("can_read_field_values", False)),
            "readBackMethod": self.read_back_method,
            "canObserveElementState": bool(self.caps_cfg.get("can_observe_element_state", True)),
            "canScreenshot": bool(self.caps_cfg.get("can_screenshot", True)),
            "canKeyboardNavigate": bool(self.caps_cfg.get("can_keyboard_navigate", True)),
        }

    # -- return / screen navigation ----------------------------------------

    def open_return(self, return_ref: str) -> dict:
        """
        Verify Drake is attached with a return open, and echo the locator.
        NOTE: opening/creating the return + keying the header (SSN, filing status)
        is a HUMAN step — the agent only enters data-entry screens.
        """
        try:
            if not self.dry_run:
                self.connect()
            return {"ok": True, "locator": return_ref}
        except Exception as e:  # transport
            return {"ok": False, "error": f"could not attach to Drake: {e}"}

    def open_screen(self, screen: str, instance: int = 0) -> dict:
        try:
            self._keys(self.nav.get("to_data_entry_selector", "{ESC}"))
            self._keys(screen + self.nav.get("open_screen_suffix", "{ENTER}"))
            for _ in range(max(0, int(instance))):
                self._keys(self.nav.get("new_instance", "{PGDN}"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_form_instance(self, screen: str) -> dict:
        try:
            self._keys(self.nav.get("new_instance", "{PGDN}"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def press(self, keys: list[str]) -> dict:
        try:
            for k in keys:
                # A single-token chord like "Esc"/"PageDown" → pywinauto brace form.
                self._keys("{" + k.upper() + "}" if len(k) > 1 else k)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- field focus / type / read (the calibration-sensitive core) --------

    def _edit_by_auto_id(self, auto_id: str):
        return self.win.child_window(auto_id=auto_id, control_type="Edit")

    def focus(self, target: dict) -> dict:
        """
        Plant a caret in the field. Precedence today: automation_id -> click_xy ->
        ocr_box center -> field_no (heads-down, by field NUMBER) -> tab_index.
        On Drake's custom canvas UIA/win32 expose no field element, so targeting is
        either a physical click at the field's point OR Drake's own heads-down field
        addressing (Ctrl+N + number) — the latter is coordinate/DPI-immune and, once
        confirmed on the VM, becomes the PRIMARY path over click_xy (see PRECISION-PLAN.md).
        NB: Ctrl+A / Ctrl+C (clipboard) ARE toxic on the canvas; Ctrl+N (the heads-down
        mode toggle) is Drake's own documented hotkey and is a different thing entirely.
        """
        screen, field = target["screen"], target["field"]
        try:
            fb = self._field_binding(screen, field)
            if self.dry_run:
                print(f"[dry-run] focus {screen}/{field}")
                return {"ok": True}
            if fb.get("automation_id"):
                self._edit_by_auto_id(fb["automation_id"]).set_focus()
                return {"ok": True}
            click = fb.get("click_xy") or _box_center(fb.get("ocr_box"))
            if click is not None:
                self.win.set_focus()  # Drake to foreground so the click lands on it
                self.win.click_input(coords=(int(click[0]), int(click[1])))
                return {"ok": True}
            if fb.get("field_no") is not None:
                # Heads-down jump: address the field by its NUMBER — Drake's own
                # coordinate-free targeting. The toggle defaults to Ctrl+N (Drake's
                # documented mode hotkey; NOT one of the toxic clipboard chords). If a
                # field is being entered while already in heads-down mode, set the binding
                # toggle to "" so we don't flip the mode back off per field.
                tog = self.nav.get("headsdown_toggle", "")
                if tog:
                    self._keys(tog)
                self._keys(str(fb["field_no"]) + self.nav.get("headsdown_jump_suffix", "{ENTER}"))
                return {"ok": True}
            if fb.get("tab_index") is not None:
                self._keys("{TAB}" * int(fb["tab_index"]))
                return {"ok": True}
            return {"ok": False,
                    "error": f"{screen}/{field} is not bound — set click_xy or ocr_box "
                             f"(preferred) / field_no / tab_index in binding.json"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _send_key_chord(self, vks) -> None:
        """Press a chord of virtual-key codes as low-level SCAN-CODE key events (all keys
        down in order, then all up in reverse, with a hold pause between) — mirroring a
        real hardware keypress. DOS/legacy-heritage apps like Drake register modifier
        chords (Ctrl+N) far more reliably this way than via pywinauto's high-level '^n',
        which fires the combo too fast/loose and Drake drops it (the same class of problem
        as literal typing needing vk_packet=False — this is the chord equivalent).
        Windows-only; keybd_event with KEYEVENTF_SCANCODE is the same injection path that
        made typing land. vks e.g. [0x11, 0x4E] = Ctrl+N."""
        import ctypes, time
        user32 = ctypes.windll.user32
        KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP, MAPVK_VK_TO_VSC = 0x0008, 0x0002, 0
        hold = max(self.key_pause, 0.03)
        def _scan(vk):
            return user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        for vk in vks:  # press down in order (modifier first)
            user32.keybd_event(0, _scan(vk), KEYEVENTF_SCANCODE, 0)
            time.sleep(hold)
        for vk in reversed(vks):  # release in reverse (modifier last)
            user32.keybd_event(0, _scan(vk), KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0)
            time.sleep(hold)

    # -- heads-down popup: driven as a REAL dialog window (not blind global keys) --------

    def _connect_win32_popup(self) -> None:
        """Open a SECOND pywinauto connection with backend='win32' to the SAME Drake PID.
        The heads-down popup ('Drake … Heads Down Data Entry') is a real classic dialog
        with a focusable Edit control — the win32 backend exposes that native HWND plus
        message-based control methods (set_edit_text / WM_GETTEXT read-back) that the
        custom canvas never exposed and the uia backend can't drive. Best-effort: a
        failure just leaves self.w32 None (heads-down entry then reports it and HALTs)."""
        if self.dry_run or Application is None or self.win is None:
            return
        try:
            pid = self.win.element_info.process_id
            self.w32 = Application(backend="win32").connect(process=pid, timeout=20)
        except Exception:
            self.w32 = None

    def _window_alive(self) -> bool:
        """Is Drake's main frame still a live window?

        NOT `self.win.exists()`: `_resolve_main_window` returns a RESOLVED WRAPPER
        (UIAWrapper), and `exists()` only lives on WindowSpecification — the un-resolved
        query object. Calling it on a wrapper raises AttributeError, which is what halted
        the first live run at field 4. Ask Windows directly instead: IsWindow(hwnd) is the
        actual question ("does this handle still refer to a window?") and it cannot be
        confused by backend object types."""
        hwnd = self.main_hwnd
        if not hwnd and self.win is not None:
            try:
                hwnd = int(self.win.handle)
            except Exception:
                hwnd = None
        if hwnd:
            try:
                import ctypes
                return bool(ctypes.windll.user32.IsWindow(int(hwnd)))
            except Exception:
                pass  # non-Windows / no ctypes — fall through to the wrapper probe
        try:
            return bool(self.win.is_visible())  # raises once the window is destroyed
        except Exception:
            return False

    def _focused_hwnd(self):
        """(hwndFocus, hwndCaret, rcCaret, flags) for Drake's GUI thread — the cross-process
        truth of which control owns the keyboard/caret RIGHT NOW (no AttachThreadInput).
        The focus/caret oracle for the guarded-keystroke protocol."""
        return _gui_thread_info(self.main_hwnd)

    def _find_popup_hwnd(self, timeout: float = 0.0):
        """The heads-down popup's HWND, by OUR OWN enumeration + `re.search`.
        Returns (hwnd, scope) with scope "pid" or "global"; (None, "") if absent.

        Deliberately NOT pywinauto's `title_re`. pywinauto matches it with
        `re.compile(pattern).match(title)` — ANCHORED AT THE START (findwindows.py:274-281)
        — so the pattern 'Heads.?Down Data Entry' never matched the real window title
        'Drake 2025 - Heads Down Data Entry'. Every popup lookup silently returned None
        while `_input_scope` and the dialog classifier, which use `re.search`, saw the same
        window perfectly well. That disagreement is what made `probe-popup` report "popup
        not open — click a Drake field first" about a popup that was on screen AND focused,
        and it also silently broke `_ensure_popup_open`'s core promise: presence could never
        be detected, so the "never toggle an open popup back off" guarantee did not hold and
        the retry loop could fire Ctrl+N repeatedly.

        Falls back to a process-wide sweep because a popup hosted by a DIFFERENT process
        would have been equally invisible to an Application connected by process=pid."""
        if not _WINFN or self.dry_run:
            return None, ""
        import re as _re
        import time
        pat = _re.compile(self.popup_title_re, _re.I)
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            for scope, pid in (("pid", int(self.pid or 0)), ("global", None)):
                if scope == "pid" and not pid:
                    continue
                try:
                    wins = _enum_toplevel_windows(pid)
                except Exception:
                    continue
                for w in wins:
                    if w.get("visible") and pat.search(w.get("title") or ""):
                        return int(w["hwnd"]), scope
            if time.time() >= deadline:
                return None, ""
            time.sleep(0.05)

    def _find_headsdown_popup(self, timeout: float = 0.5):
        """The heads-down popup as a win32 WindowSpecification, or None if not present.
        Re-found each cycle — the dialog is created/destroyed per jump on this build, so a
        cached wrapper goes stale."""
        if self.w32 is None:
            return None
        hwnd, scope = self._find_popup_hwnd(timeout=timeout)
        if hwnd is None:
            self._popup_hwnd = None
            return None
        if scope == "global" and not self._warned_popup_foreign:
            self._warned_popup_foreign = True
            print(f"  · the heads-down popup (hwnd={hwnd}) is NOT owned by the Drake process "
                  f"we attached to — driving it by handle.")
        try:
            # By HANDLE: find_elements returns the element directly for a handle criterion,
            # short-circuiting every other filter (title matching, process). Exact, and
            # immune to the anchored-regex trap above.
            spec = self.w32.window(handle=hwnd)
            self._popup_hwnd = hwnd
            return spec
        except Exception:
            self._popup_hwnd = None
            return None

    def _popup_edit(self, popup):
        """The popup's text box (its real Edit control)."""
        try:
            return popup.child_window(class_name=self.popup_edit_class)
        except Exception:
            return popup.child_window(control_type="Edit")

    def _window_snapshot(self):
        """(top-level windows of the Drake process, is-main-frame-enabled) — the raw
        material for the structural dialog gate. None when there is nothing real to
        snapshot (dry-run / not connected / not Windows). SimDriver overrides this to
        drive the gate offline."""
        if not _WINFN or self.dry_run or self.main_hwnd is None:
            return None
        try:
            pid = int(self.pid or self.win.element_info.process_id)
            wins = _enum_toplevel_windows(pid)
            enabled = bool(_u32().IsWindowEnabled(int(self.main_hwnd)))
            return wins, enabled
        except Exception:
            return None

    def _snapshot_baseline(self) -> None:
        """Record every window the Drake process ALREADY has at attach. Pre-existing
        windows — the 'Drake Software Chat' overlay, tool panels — are Drake's normal
        furniture: the gate only ever blocks on what APPEARS mid-run or what actually
        takes modality away from the main frame, never on a window for existing."""
        snap = self._window_snapshot()
        if snap is None:
            return
        import re as _re
        wins, _enabled = snap
        self._baseline_hwnds = {int(w["hwnd"]) for w in wins}
        extras = [w for w in wins
                  if int(w["hwnd"]) != int(self.main_hwnd or 0)
                  and w.get("visible")
                  and not _re.search(self.popup_title_re, w.get("title") or "")]
        if extras:
            names = ", ".join(repr(w.get("title") or w.get("class_name")) for w in extras[:6])
            print(f"  · {len(extras)} other window(s) in the Drake process at attach — "
                  f"benign baseline, ignored unless one blocks input: {names}")

    def _note_benign(self, w) -> None:
        line = (f"ignoring benign window {w.get('title')!r} "
                f"(class={w.get('class_name')}, hwnd={w.get('hwnd')}) — non-modal, not a dialog")
        self.benign_notes.append(line)
        print(f"  · {line}")

    def _input_scope(self, allow_popup: bool = True):
        """HWND-scoped keystroke gate: (ok, where). ok=True only when the FOREGROUND root
        window — where SendInput's keys will actually land — is a legitimate Drake entry
        surface: the bound main frame, the heads-down popup, or another BASELINE window of
        the Drake process with a real frame size (some builds host data entry in its own
        top-level window; the frame-size floor is what keeps the ~84x84 chat overlay out).
        Anything else — another app, the chat window, an unknown new window — blocks the
        keystroke. Unscoped (always ok) off-Windows and in the simulator."""
        if not _WINFN or self.dry_run or self.main_hwnd is None:
            return True, "unscoped"
        try:
            kb = _keyboard_target_info()
            root = int(kb.get("root") or 0)
            if root == int(self.main_hwnd):
                return True, "main-frame"
            if root and kb.get("root_pid") == int(self.pid or 0):
                import re as _re
                if allow_popup and _re.search(self.popup_title_re, kb.get("root_title") or ""):
                    return True, "heads-down-popup"
                if root in self._baseline_hwnds:
                    r = _wintypes.RECT()
                    _u32().GetWindowRect(root, _ctypes.byref(r))
                    if ((r.right - r.left) >= self.min_main_w
                            and (r.bottom - r.top) >= self.min_main_h):
                        return True, "drake-frame"
            where = kb.get("root_title") or kb.get("root_class") or hex(root)
            return False, f"{where!r} (hwnd={root})"
        except Exception as e:  # a scope-oracle hiccup must not brick entry — note it
            return True, f"scope-check-unavailable: {e}"

    def _detect_unexpected_dialog(self):
        """STRUCTURAL dialog gate. Decides from a window snapshot whether something is
        actually BLOCKING data entry — never from a title allowlist. (The old version
        halted on ANY extra window in the process, so the always-present 'Drake Software
        Chat' overlay killed every run at the first field.) Structure means:

          • windows already present at attach (chat overlay, tool panels) are baseline —
            benign by definition, they never halt a run by existing;
          • a NEW dialog-class window (#32770, or an owned+captioned popup — the shape of
            every validator/error/prompt) IS a blocker → halt;
          • the main frame DISABLED while the heads-down popup is NOT up means a modal is
            pumping somewhere → halt (debounced once — creation/teardown churn disables
            the frame for a moment) — caught even if the modal can't be enumerated;
          • anything else that appears (toasts, dropdowns, tooltips, chat expanding) is
            benign: logged once, remembered, never a halt.

        Returns the blocker {title, summary, text, handle, class, why} or None. NEVER
        auto-clicks or dismisses anything."""
        snap = self._window_snapshot()
        if snap is None:
            return None
        wins, main_enabled = snap
        kw = dict(popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,
                  baseline=self._baseline_hwnds, benign_seen=self._benign_hwnds)
        blocker, benign_new, _pp = _classify_process_windows(wins, main_enabled=main_enabled, **kw)
        if blocker is not None and blocker.get("why") == "main-disabled" and not blocker.get("dialogish"):
            import time as _t
            _t.sleep(0.12)  # weak evidence — debounce the transient disable once
            snap = self._window_snapshot()
            if snap is not None:
                wins, main_enabled = snap
                blocker, benign_new, _pp = _classify_process_windows(wins, main_enabled=main_enabled, **kw)
        for w in benign_new:
            self._benign_hwnds.add(int(w["hwnd"]))
            self._note_benign(w)
        if blocker is None:
            return None
        h = int(blocker.get("hwnd") or 0)
        title = blocker.get("title")
        text = " ".join(s for s in [title, blocker.get("_text")] if s)
        if h and _WINFN:
            try:
                text = (text + " " + _window_deep_text(h)).strip()
            except Exception:
                pass
        if blocker.get("why") == "main-disabled":
            who = (f"{title!r} (class {blocker.get('class_name')})" if title
                   else f"window not identified (visible: {blocker.get('candidates')})")
            summary = f"main frame DISABLED by a modal — {who}"
        else:
            summary = f"new dialog window {title!r} (class {blocker.get('class_name')})"
        return {"title": title, "summary": summary, "text": text[:400], "handle": h,
                "class": blocker.get("class_name"), "why": blocker.get("why"),
                "candidates": blocker.get("candidates")}

    def dump_windows(self) -> dict:
        """Full window topology of the Drake process + where the keyboard would land —
        the halt-time diagnostic. Written automatically to env-dump-halt.json whenever a
        batch halts, and on demand via `agent.py envdump`. Every top-level window carries
        the gate's verdict (role) so a bad halt is diagnosable from the JSON alone."""
        if not _WINFN or self.dry_run or self.win is None:
            return {"ok": False, "error": "env dump runs on Windows with a live connection"}
        try:
            import re as _re
            pid = int(self.pid or self.win.element_info.process_id)
            wins = _enum_toplevel_windows(pid)
            blocker, main_enabled = None, None
            snap = self._window_snapshot()
            if snap is not None:
                swins, main_enabled = snap
                blocker, _b, _p = _classify_process_windows(
                    swins, popup_title_re=self.popup_title_re, main_hwnd=self.main_hwnd,
                    baseline=self._baseline_hwnds, benign_seen=self._benign_hwnds,
                    main_enabled=main_enabled)
            for w in wins:
                h = int(w["hwnd"])
                if self.main_hwnd and h == int(self.main_hwnd):
                    w["role"] = "main-frame"
                elif _re.search(self.popup_title_re, w.get("title") or ""):
                    w["role"] = "heads-down-popup"
                elif not w.get("visible"):
                    w["role"] = "hidden"
                elif (w.get("class_name") or "") in _COSMETIC_CLASSES:
                    w["role"] = "cosmetic"
                elif h in self._baseline_hwnds:
                    w["role"] = "baseline-benign"
                elif h in self._benign_hwnds:
                    w["role"] = "benign-seen"
                else:
                    w["role"] = "new"
                w["dialogish"] = _dialogish(w)
                if blocker and int(blocker.get("hwnd") or 0) == h:
                    w["role"] = "BLOCKING"
                if w.get("visible"):
                    w["children"] = _enum_child_summaries(h)
            return {"ok": True, "takenAt": _now(), "pid": pid,
                    "main_hwnd": self.main_hwnd, "main_enabled": main_enabled,
                    "baseline_hwnds": sorted(int(x) for x in self._baseline_hwnds),
                    "keyboard_target": _keyboard_target_info(),
                    "blocker": blocker, "benign_notes": list(self.benign_notes),
                    "windows": wins}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _ensure_popup_open(self, *, method: str = "scancode", timeout: float = 1.5,
                           attempts: int = 3) -> dict:
        """IDEMPOTENT + RETRYING open of the heads-down popup.

        Idempotent: presence is re-checked before EVERY Ctrl+N, so we can never toggle a
        popup that is already open back OFF (the old double-toggle bug). That is what makes
        retrying safe.

        Retrying matters because Drake SWALLOWS a modifier chord while it is busy committing
        a field. Confirmed case — Field 4 (employer EIN): committing the EIN fires Drake's
        employer lookup + auto-fill and auto-advances the caret to Box 1 (field 23), and the
        Ctrl+N we send during that work is eaten. One attempt = the popup never opens and the
        NEXT field number types onto the canvas (the observed cascade: '5' into Box 1, then
        each value one box further down). Retrying with a settle recovers it deterministically.
        """
        import time
        for i in range(max(1, attempts)):
            popup = self._find_headsdown_popup(timeout=0.3 if i == 0 else 0.15)
            if popup is not None:
                ready = self._popup_ready_for_number(popup)
                if not ready.get("ok"):
                    return ready
                return {"ok": True, "opened": i > 0, "attempts": i, "popup": popup}
            if self.w32 is None:
                return {"ok": False, "reason": "win32 popup connection unavailable (reconnect on the VM)"}
            bad = self._detect_unexpected_dialog()
            if bad:  # never fire a chord into a modal we did not expect
                return {"ok": False, "reason": f"unexpected dialog before Ctrl+N: {bad['summary']}",
                        "dialog": bad}
            # HWND-scoped gate: Ctrl+N is a GLOBAL chord — prove the foreground root is
            # Drake's frame first, or the chord goes into the chat/terminal/whatever.
            # allow_popup stays False on purpose: we only get here when the popup was NOT
            # found, and firing Ctrl+N into a focused popup would toggle heads-down OFF.
            ok_scope, where = self._input_scope(allow_popup=False)
            if not ok_scope:
                import re as _re
                if _re.search(self.popup_title_re, where, _re.I):
                    # The popup is focused, yet the lookup above said it does not exist.
                    # That contradiction is a bug in window resolution, not something the
                    # operator can fix by clicking — say so instead of blaming them.
                    return {"ok": False,
                            "reason": f"the heads-down popup is focused ({where}) but could "
                                      f"not be resolved as a window — window lookup is "
                                      f"broken, not your focus. Run `agent.py envdump` and "
                                      f"send the JSON."}
                return {"ok": False,
                        "reason": f"refusing to send Ctrl+N — the keyboard is in {where}, "
                                  f"not Drake's frame (click a Drake field, hands off)"}
            self.headsdown_toggle(method=method)  # Ctrl+N (scancode)
            # Wait for the popup by HANDLE resolution, not pywinauto's anchored title_re
            # (see _find_popup_hwnd) — the old `window(title_re=...).wait(...)` here could
            # never succeed, so this loop always fell through to another Ctrl+N.
            spec = self._find_headsdown_popup(timeout=timeout)
            if spec is not None:
                self._popup_owned = True  # WE opened it, so its state is known: number prompt
                return {"ok": True, "opened": True, "attempts": i + 1, "popup": spec}
            time.sleep(0.25 * (i + 1))  # Drake is busy (auto-fill / commit) — let it settle
        return {"ok": False,
                "reason": f"Ctrl+N did not open the heads-down popup after {attempts} attempts "
                          f"(is a canvas field active?)"}

    def _popup_ready_for_number(self, popup) -> dict:
        """Is the popup that is ALREADY up waiting for a FIELD NUMBER? {"ok":True} or a halt.

        Presence is not readiness. The popup has two states — asking for a number, and
        asking for the VALUE of a field it already jumped to — and they look identical from
        the outside. Typing a field number into one that is armed for a value COMMITS THE
        NUMBER AS THE VALUE: '23' lands in the employer EIN, and every field after it is
        one step out of phase.

        That is not hypothetical. `probe-popup` ends by pressing Enter on a field number,
        which leaves Drake armed for exactly that value, and the agent's own help tells the
        operator to run it FIRST.

        Two independent checks, so neither has to be perfect:
          1. OWNERSHIP — if we did not open this popup during this batch, its state is
             unknown by definition. Halt.
          2. PROMPT TEXT — if it is readable and does not look like the number prompt, halt
             no matter who opened it."""
        if self._popup_owned:
            prompt = self._popup_prompt(popup)
            import re as _re
            if prompt and not _re.search(self.number_prompt_re, prompt, _re.I):
                return {"ok": False,
                        "reason": f"the heads-down popup is armed for a VALUE, not a field "
                                  f"number (prompt reads {prompt[:80]!r}). Refusing to type a "
                                  f"field number into it — that would commit the number as the "
                                  f"previous field's value."}
            return {"ok": True}
        prompt = self._popup_prompt(popup)
        import re as _re
        if prompt and _re.search(self.number_prompt_re, prompt, _re.I):
            self._popup_owned = True  # inherited, but provably on the number prompt
            return {"ok": True}
        detail = (f"its prompt reads {prompt[:80]!r}" if prompt
                  else "and this build exposes no prompt text, so its state cannot be read")
        return {"ok": False,
                "reason": f"a heads-down popup was ALREADY open before this run started — "
                          f"{detail}. It may be armed for a VALUE (probe-popup and any halted "
                          f"run leave it that way), and typing a field number into it would "
                          f"commit that number as a value. Press Esc in Drake to close it, "
                          f"click a field, and re-run."}

    def begin_batch(self) -> None:
        """Reset per-batch popup state. Called before a run so a popup inherited from a
        PREVIOUS run (or from probe-popup) is treated as foreign and challenged, rather
        than trusted because an earlier batch happened to open one."""
        self._popup_owned = False
        self._warned_prompt_blind = False

    def _focus_popup_edit(self, edit, edit_hwnd, tries: int = 6) -> bool:
        """Give the popup's Edit real keyboard focus and PROVE it via GetGUIThreadInfo —
        the cross-process truth of who owns the keyboard. Returns False rather than let a
        keystroke go somewhere we did not verify."""
        import time
        for _ in range(tries):
            try:
                edit.set_focus()
            except Exception:
                pass
            try:
                # Two proofs, both required: Drake's GUI thread focuses THIS edit, AND the
                # foreground root is one of OUR windows (Drake-thread focus is meaningless
                # if another app owns the foreground — SendInput would go there).
                if self._focused_hwnd()[0] == edit_hwnd and self._input_scope(allow_popup=True)[0]:
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    def _settle_read(self, hedit, expected, *, exact: bool = False,
                     timeout: float = 1.0, poll: float = 0.04):
        """Poll the edit until it CONVERGES on `expected` — two consecutive identical reads
        that both match. Returns (ok, last_read_or_None).

        Convergence is a DRAIN PROOF, and it is why the old "read it back, and if it's wrong
        repair it with set_edit_text" pattern had to go. `self._keys()` POSTS keystrokes to
        Drake's input queue; WM_GETTEXT and set_edit_text are SENT messages, handled ahead of
        anything still queued. So the old sequence could read a half-arrived '520', "repair"
        it to '52000', re-read a clean '52000', pass the gate — and only then would the
        still-queued '00' arrive and append, committing 5,200,000 while reporting success.
        Waiting for the box to stop changing means every injected key has already been
        consumed, so none can land after the check.

        A failed read (None) is NO EVIDENCE, not agreement — it just keeps polling."""
        import time
        cmp = (lambda a, b: a == b) if exact else _same_value
        deadline = time.time() + timeout
        prev, last = None, None
        while time.time() < deadline:
            cur = _read_edit_or_none(hedit)
            if cur is not None:
                last = cur
                if prev is not None and cur == prev and cmp(cur, expected):
                    return True, cur
                prev = cur
            time.sleep(poll)
        return False, last

    def _popup_prompt(self, popup, edit_hwnd=None) -> str:
        """The prompt text of a popup WindowSpecification ('' if unreadable).

        Prefers the handle `_find_popup_hwnd` already resolved: reading `.handle` off a
        WindowSpecification re-runs pywinauto's search criteria, which is both slower and
        the layer that produced the anchored-title_re failure in the first place."""
        hwnd = self._popup_hwnd
        if hwnd is None:
            try:
                hwnd = int(popup.handle)
            except Exception:
                return ""
        try:
            return _popup_prompt_text(hwnd, edit_hwnd, self.popup_edit_class)
        except Exception:
            return ""

    def _note_prompt_blind(self) -> None:
        if self._warned_prompt_blind:
            return
        self._warned_prompt_blind = True
        print("  ⚠ this build exposes NO readable prompt text on the heads-down popup, so a "
              "silently REFUSED field number or value cannot be positively detected. "
              "Falling back to the weaker 'did the edit box change' test — verify the "
              "screenshot field by field.")

    def _classify_after_jump(self, edit_hwnd, number: str, base_prompt: str = "",
                             timeout: float = 2.5):
        """After Enter on a field NUMBER, determine what Drake ACTUALLY did — by observation,
        never assumption. Returns (model, info):

          "per-jump"    the popup CLOSED -> the caret is on the canvas field, value goes there.
          "persistent"  the popup is now prompting for the VALUE -> the value goes back into
                        the popup, followed by Enter.
          "error"       an unexpected/validation dialog appeared (returned as `info`).
          "rejected"    Drake did NOT take the number — still sitting on the number prompt.
          "unknown"     the popup stayed but nothing could be read -> the caller HALTs.

        WHY THE PROMPT TEXT AND NOT JUST THE EDIT BOX: a REFUSED number clears the edit
        exactly like an ACCEPTED one does, so "the edit no longer holds the number" cannot
        tell them apart. Reading a refusal as an acceptance is what makes the NEXT field's
        number get typed as THIS field's value — after which every value lands one box off,
        every row reporting OK. That is the cascade, and it is silent.

        With `base_prompt` (the number prompt captured immediately before Enter) the test is
        "has the prompt moved off the number prompt?", which distinguishes the two. Compared
        against the CAPTURED baseline rather than a hardcoded English string, so a build that
        rewords the prompt cannot silently disable the check. If this build exposes no prompt
        text at all, it degrades to the old edit-changed heuristic and says so out loud."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            bad = self._detect_unexpected_dialog()
            if bad:
                return ("error", bad)
            live = self._find_headsdown_popup(timeout=0.05)
            if live is None:
                return ("per-jump", None)
            if base_prompt:
                cur = self._popup_prompt(live, edit_hwnd)
                if cur and cur != base_prompt:
                    return ("persistent", {"prompt": cur})
            else:
                # Blind build: the best available signal is the edit clearing.
                cur_edit = _read_edit_or_none(edit_hwnd)
                if cur_edit is not None and cur_edit != number:
                    self._note_prompt_blind()
                    return ("persistent", {"prompt": ""})
            time.sleep(0.08)
        if base_prompt:
            return ("rejected", None)
        return ("unknown", None) if _read_edit_or_none(edit_hwnd) is None else ("rejected", None)

    def _verify_value_committed(self, number_prompt: str, value_prompt: str,
                                timeout: float = 1.2):
        """After Enter on a VALUE, prove Drake actually took it. Returns (ok, how).

          popup GONE                       -> the confirmed auto-advance (field 4 / EIN). Taken.
          popup back on the NUMBER prompt  -> taken; it is asking for the next field.
          popup still on the VALUE prompt  -> REFUSED. Nothing was committed.

        Without this, a value Drake declines (it beeps and stays on the value prompt — no
        dialog, so the dialog gate sees nothing) was reported ok, and the next field's NUMBER
        was then consumed as THIS field's value. Same cascade, one layer down. On a build
        with no readable prompt text there is nothing to compare, so it reports unverified
        rather than inventing a verdict."""
        import time
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            live = self._find_headsdown_popup(timeout=0.05)
            if live is None:
                return True, "popup closed (auto-advance — Drake moved the caret itself)"
            if not number_prompt:
                self._note_prompt_blind()
                return True, "UNVERIFIED (this build exposes no popup prompt text)"
            cur = self._popup_prompt(live)
            if cur:
                last = cur
                if cur == number_prompt:
                    return True, "popup returned to the field-number prompt"
                if value_prompt and cur != value_prompt:
                    return True, f"prompt moved on ({cur[:60]!r})"
            time.sleep(0.08)
        return False, last

    def headsdown_toggle(self, method: str = "scancode") -> dict:
        """Toggle Drake's HEADS-DOWN data entry (Ctrl+N). In heads-down mode every field
        displays a stable NUMBER; you address a field by typing its number, so NO pixel
        click is needed — Drake's own coordinate-free field addressing, immune to DPI /
        resolution / window position (the exact thing our fragile click_xy is not). The
        on-screen numbers double as an OCR read-back anchor.

        Ctrl+N is Drake's documented mode hotkey — categorically different from the toxic
        clipboard chords Ctrl+A / Ctrl+C.

        method = how Ctrl+N is injected (legacy apps are picky about modifier chords):
          "scancode"  low-level hardware scan-code down/up via keybd_event — mimics a real
                      keypress, holds Ctrl while N is pressed. MOST RELIABLE for Drake; the
                      default, and what to use when high-level '^n' does nothing.
          "vkhold"    pywinauto explicit {VK_CONTROL down}n{VK_CONTROL up} (Ctrl held).
          "pywinauto" pywinauto high-level '^n' (fast; Drake ignored it in testing).
        """
        try:
            if self.dry_run:
                print(f"[dry-run] headsdown toggle (Ctrl+N, method={method})")
                return {"ok": True, "method": method}
            if method == "scancode":
                self._send_key_chord([0x11, 0x4E])  # VK_CONTROL, VK_N
            elif method == "vkhold":
                self._keys("{VK_CONTROL down}n{VK_CONTROL up}")
            else:
                self._keys(self.nav.get("headsdown_toggle") or "^n")
            return {"ok": True, "method": method}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def headsdown_type(self, field_no, value, *, method: str = "scancode",
                       settle_after: float = 0.0) -> dict:
        """Enter ONE value BY FIELD NUMBER — race-free, VERIFIED, and model-agnostic — via
        the heads-down popup. Replaces the old open-loop "fire Ctrl+N, sleep, blind-type"
        that raced keystrokes between the popup and the canvas (digits into EIN, 'invalid
        field', cascade). Every step is gated:

          1. Drake main frame still alive?                         else HALT
          2. no unexpected/error dialog already up?                else HALT
          3. ensure the popup is open — IDEMPOTENT (presence re-checked before every Ctrl+N,
             so never a mode-killing double toggle) and RETRYING (Drake eats a chord while
             it commits a field — the Field-4/EIN auto-advance case)   else HALT
          4. focus the popup's real Edit and PROVE focus == that Edit (GetGUIThreadInfo)
                                                                   else HALT
          5. type the field number into the FOCUSED popup edit and wait for it to SETTLE on
             exactly that number (convergence, not a single read) BEFORE the irreversible
             Enter                                                 else HALT
          6. press Enter → then OBSERVE what Drake did (_classify_after_jump), using the
             popup's PROMPT TEXT rather than assuming a model:
               per-jump   popup closed  → type the value onto the CANVAS (no trailing Enter;
                          the next field's jump is what leaves this field)
               persistent popup now asking for the value → re-focus its edit, type the value
                          THERE, wait for it to settle, then Enter to commit
             rejected / unknown / error dialog                     → HALT
          7. PROVE the value was accepted (_verify_value_committed) else HALT
          8. no error dialog after the value?                      else HALT

        The field number can never land on the canvas: we prove the popup edit owns the
        keyboard and watch the digits settle before committing. And because steps 6-7 read
        Drake's own prompt rather than assuming, neither a silently refused field number nor
        a silently refused value can be mistaken for success — which is what previously let
        one bad field push every later value one box out of phase, with every row reporting
        OK. Returns {ok:True,...} or {ok:False, halt:True, reason:...} — the caller STOPS the
        batch for a human. Never auto-dismisses a dialog."""
        fn = str(field_no)
        val = "" if value is None else str(value)
        # A blank commit is not a no-op: it is an Enter on an empty box, which CLEARS
        # whatever the field already held. `--seq "4="` would have wiped the employer EIN
        # and reported [OK] field 4 = ''. To leave a box alone, omit it.
        if not val.strip():
            return {"ok": False, "halt": True,
                    "reason": f"refusing to commit an EMPTY value to field {fn} — that would "
                              f"clear whatever the box already holds. Omit the field to leave "
                              f"it alone."}
        try:
            if self.dry_run:
                print(f"[dry-run] headsdown field {fn} = {val!r}")
                return {"ok": True, "field_no": field_no, "model": "dry-run"}
            import time
            # 1) app alive?
            if self.win is None or not self._window_alive():
                return {"ok": False, "halt": True, "reason": "Drake main frame vanished (app closed)"}
            # 2) unexpected/error dialog already up?
            bad = self._detect_unexpected_dialog()
            if bad:
                return {"ok": False, "halt": True, "reason": f"unexpected dialog before entry: {bad['summary']}", "dialog": bad}
            # 3) idempotent + retrying popup open
            opened = self._ensure_popup_open(method=method)
            if not opened.get("ok"):
                return {"ok": False, "halt": True, "reason": opened.get("reason"),
                        "dialog": opened.get("dialog")}
            popup = self._find_headsdown_popup(timeout=1.0)
            if popup is None:
                return {"ok": False, "halt": True, "reason": "heads-down popup not found after open"}
            edit = self._popup_edit(popup)
            try:
                edit.wait("ready", timeout=2)
                eh = int(edit.handle)
            except Exception as e:
                return {"ok": False, "halt": True, "reason": f"popup edit not ready / no handle: {e}"}
            # 4) focus the edit + PROVE it via the ctypes GUI-thread oracle
            if not self._focus_popup_edit(edit, eh):
                return {"ok": False, "halt": True, "reason": "popup edit never took keyboard focus"}
            # 5) place the number in the FOCUSED popup edit, then wait for it to SETTLE.
            try:
                edit.set_edit_text("")  # clear stale digits (EM_REPLACESEL, atomic)
            except Exception:
                pass
            stale = _read_edit_or_none(eh)
            if stale:  # a digit left over here would PREFIX the number: 2 + 23 -> field 223
                return {"ok": False, "halt": True,
                        "reason": f"popup edit still holds {stale!r} before typing field number "
                                  f"{fn!r} — refusing to type onto residue"}
            self._keys(fn)  # real VK/scan keys land in the focused popup edit (focus proven)
            # EXACT comparison for the number: _same_value's cosmetic tolerance is for money
            # and must never bless a field number ('4' vs '4.0' is a different box).
            settled, got = self._settle_read(eh, fn, exact=True, timeout=1.0)
            if not settled:
                return {"ok": False, "halt": True,
                        "reason": f"popup edit never settled on field number {fn!r} "
                                  f"(last read {got!r}) — refusing to press Enter"}
            # Baseline the NUMBER prompt while it is still showing, so step 6 can tell
            # "Drake took the number" from "Drake silently refused it".
            base_prompt = self._popup_prompt(popup, eh)
            # 6) fire the jump, then OBSERVE what Drake actually did.
            self._keys("{ENTER}")
            model, dlg = self._classify_after_jump(eh, fn, base_prompt=base_prompt, timeout=2.5)
            if model == "error":
                return {"ok": False, "halt": True,
                        "reason": f"error dialog after jumping to field {fn}: {dlg['summary']}", "dialog": dlg}
            if model == "rejected":
                return {"ok": False, "halt": True,
                        "reason": f"Drake did NOT accept field number {fn} on this screen — it "
                                  f"stayed on the field-number prompt. Nothing was entered. "
                                  f"(Is that number right for this screen? Boxes that are "
                                  f"greyed out or foreign-address-only decline silently.)"}
            if model == "unknown":
                return {"ok": False, "halt": True,
                        "reason": f"could not read the popup at all after jumping to field {fn} "
                                  f"— refusing to type a value blind"}
            # read_back stays None on the per-jump path: the canvas exposes no value to read,
            # which is exactly why that path types no trailing Enter.
            committed, commit_evidence, read_back = False, None, None
            if model == "per-jump":
                # The caret is on the canvas field. HWND-scoped gate first: after the popup
                # closes the foreground root must be Drake's frame again — retried briefly
                # (destroy/refocus transition), then HALT rather than type the value into
                # whatever else holds the keyboard. No set_focus recovery on purpose: that
                # would reset the canvas caret and the value would land nowhere.
                ok_scope, where = True, ""
                for _ in range(6):
                    ok_scope, where = self._input_scope(allow_popup=False)
                    if ok_scope:
                        break
                    time.sleep(0.05)
                if not ok_scope:
                    return {"ok": False, "halt": True,
                            "reason": f"after the jump the keyboard is in {where}, "
                                      f"not Drake — value NOT typed"}
                # Type the value — global keys, vk_packet=False, NO trailing Enter: the next
                # field's jump leaves this field cleanly, and not pressing Enter is precisely
                # what keeps a mis-detected state from cascading.
                self._keys(_escape_keys(val))
            else:  # persistent command bar — the value goes back INTO the popup, then Enter
                popup = self._find_headsdown_popup(timeout=0.5)
                if popup is None:
                    return {"ok": False, "halt": True,
                            "reason": "popup vanished between the jump and the value"}
                edit = self._popup_edit(popup)
                try:
                    edit.wait("ready", timeout=2)
                    eh = int(edit.handle)  # re-resolve: a recreated dialog invalidates the old handle
                except Exception as e:
                    return {"ok": False, "halt": True, "reason": f"popup edit not ready for the value: {e}"}
                if not self._focus_popup_edit(edit, eh):
                    return {"ok": False, "halt": True, "reason": "popup edit never took focus for the value"}
                try:
                    edit.set_edit_text("")
                except Exception:
                    pass
                value_prompt = self._popup_prompt(popup, eh)  # the VALUE prompt, for step 7
                self._keys(_escape_keys(val))
                # Wait for the box to SETTLE on the value. No set_edit_text "repair": that
                # is a SENT message that jumps ahead of still-queued keystrokes, so it can
                # make a half-typed box look correct and let the rest of the keys land
                # AFTER the gate passes (52000 -> read '520' -> "repair" -> queued '00'
                # arrives -> 5,200,000 committed, reported OK).
                settled, read_back = self._settle_read(eh, val, timeout=1.2)
                if not settled:
                    return {"ok": False, "halt": True,
                            "reason": f"popup edit never settled on value {val!r} for field {fn} "
                                      f"(last read {read_back!r}) — refusing to commit"}
                self._keys("{ENTER}")
                # 7) PROVE Drake took the value — a refusal is silent (it just stays on the
                # value prompt), and treating that as success feeds the next field's NUMBER
                # in as this field's value.
                took, how = self._verify_value_committed(base_prompt, value_prompt)
                if not took:
                    return {"ok": False, "halt": True,
                            "reason": f"Drake did not accept {val!r} for field {fn} — it is still "
                                      f"asking for that field's value. NOTHING was committed; the "
                                      f"refused text is still on screen. Enter this box by hand.",
                            "prompt_after_value": how}
                committed = True
                commit_evidence = how
            # 8) settle, then make sure the value did not trip a validator.
            if settle_after:
                time.sleep(settle_after)
            bad = self._detect_unexpected_dialog()
            if bad:
                return {"ok": False, "halt": True,
                        "reason": f"error dialog after entering field {fn}: {bad['summary']}", "dialog": bad}
            return {"ok": True, "field_no": field_no, "model": model,
                    "value_committed": committed,
                    "commit_evidence": commit_evidence,
                    "read_back": read_back,
                    "popup_reopened": opened.get("opened"),
                    "popup_attempts": opened.get("attempts"),
                    # False here after a committing field is the Field-4/EIN signature:
                    # Drake dropped heads-down to auto-advance. The next field's retrying
                    # _ensure_popup_open re-opens it, so it is informational, not an error.
                    "popup_after_value": self._find_headsdown_popup(timeout=0.2) is not None}
        except Exception as e:
            return {"ok": False, "halt": True, "reason": str(e)}

    def probe_headsdown_popup(self, probe_field: str = "6") -> dict:
        """Calibration probe for the heads-down popup: dump its real control tree, round-trip
        a field number, and record the popup's PROMPT TEXT at each of three states — waiting
        for a number, waiting for a value, and after disarm. Those three strings are what the
        entry driver's refusal detection compares against, so this is the run that makes
        `headsdown_type` able to tell acceptance from silent refusal.

        NOT read-only, and the docstring used to claim otherwise: pressing Enter on a field
        number ARMS Drake for that field's value. It probed field **4** — the employer EIN,
        an auto-fill field that already holds a real value and is the one box flagged
        do-not-touch — and then walked away leaving Drake armed, so the next run's first
        field number was committed as the EIN. It now probes field 6 (employer "Name cont.",
        normally empty and inert) and DISARMS with a focus-proven Esc before returning.

        Still writes no field value: the value prompt is answered with Esc, never text."""
        out = {"ok": True, "probe_field": probe_field}
        if self.dry_run or self.w32 is None:
            return {"ok": False, "reason": "no win32 popup connection (run on the VM after connect)"}
        try:
            import time
            f0 = self._focused_hwnd()
            out["focus_before"] = {"hwndFocus": f0[0], "hwndCaret": f0[1], "rcCaret": f0[2],
                                   "caret_blinking": bool(f0[3] & _GUI_CARETBLINKING),
                                   "main_hwnd": self.main_hwnd}
            self.begin_batch()  # a popup already up is foreign until proven otherwise
            popup = self._find_headsdown_popup(timeout=0.5)
            out["popup_present_initially"] = popup is not None
            if popup is None:
                out["ensure_open"] = self._ensure_popup_open(timeout=2.5)
                popup = self._find_headsdown_popup(timeout=1.0)
            if popup is None:
                out["ok"] = False
                out["reason"] = "popup not open — click a Drake field first, then re-run"
                return out
            # An INHERITED popup may be armed for a VALUE, not a field number. Typing the
            # probe number into that state commits it as some field's value — the precise
            # accident this probe used to cause on field 4. Refuse instead.
            ready = self._popup_ready_for_number(popup)
            out["popup_ready_for_number"] = ready
            if not ready.get("ok"):
                out["ok"] = False
                out["reason"] = ready.get("reason")
                return out
            out["popup_title"] = popup.window_text()
            controls = []
            for c in popup.descendants():
                try:
                    r = c.rectangle()
                    controls.append({"class": c.friendly_class_name(),
                                     "class_name": getattr(c.element_info, "class_name", None),
                                     "text": c.window_text(), "rect": [r.left, r.top, r.width(), r.height()],
                                     "handle": int(c.handle)})
                except Exception:
                    continue
            out["popup_controls"] = controls
            edit = self._popup_edit(popup)
            eh = int(edit.handle)
            out["edit_handle"] = eh
            out["edit_class_name"] = getattr(edit.element_info, "class_name", None)
            try:
                edit.set_focus()
            except Exception as e:
                out["set_focus_error"] = str(e)
            out["focus_is_edit_after_setfocus"] = (self._focused_hwnd()[0] == eh)
            # STATE 1 — waiting for a FIELD NUMBER. This string is the baseline every
            # later refusal check compares against; if it is empty, this build exposes no
            # prompt text and refusal detection degrades (the driver says so at runtime).
            out["prompt_at_number"] = self._popup_prompt(popup, eh)
            try:
                edit.set_edit_text(probe_field)
                out["set_edit_text_readback"] = _safe_read_edit(eh)
            except Exception as e:
                out["set_edit_text_error"] = str(e)
            try:
                self._keys("{ENTER}")
            except Exception as e:
                out["enter_error"] = str(e)
            time.sleep(0.4)
            live = self._find_headsdown_popup(timeout=0.5)
            out["popup_still_open_after_enter"] = live is not None
            # STATE 2 — waiting for the VALUE (persistent build only).
            out["prompt_at_value"] = self._popup_prompt(live, eh) if live is not None else ""
            out["prompt_changed"] = bool(out["prompt_at_number"]
                                         and out["prompt_at_value"]
                                         and out["prompt_at_number"] != out["prompt_at_value"])
            f1 = self._focused_hwnd()
            out["focus_after_enter"] = {"hwndFocus": f1[0], "hwndCaret": f1[1], "rcCaret": f1[2],
                                        "caret_blinking": bool(f1[3] & _GUI_CARETBLINKING)}
            out["model"] = ("persistent-command-bar (value -> popup)" if out["popup_still_open_after_enter"]
                            else "per-jump dialog (value -> canvas)")
            out["refusal_detection"] = (
                "AVAILABLE — the prompt changes between the number and value states, so a "
                "silently refused number or value can be detected" if out["prompt_changed"] else
                "DEGRADED — the popup prompt is unreadable or does not change, so a silent "
                "refusal cannot be positively detected; verify every box on the screenshot")
            out["disarm"] = self._disarm_popup()
            # STATE 3 — after disarm. Drake must be back to a normal, unarmed screen.
            after = self._find_headsdown_popup(timeout=0.3)
            out["popup_open_after_disarm"] = after is not None
            out["prompt_after_disarm"] = self._popup_prompt(after, eh) if after is not None else ""
            out["unexpected_dialog"] = self._detect_unexpected_dialog()
        except Exception as e:
            out["ok"] = False
            out["reason"] = str(e)
        return out

    def _disarm_popup(self) -> dict:
        """Send Esc to the heads-down popup so Drake is not left ARMED for a value.

        The Esc is deliberately NOT blind. `nav.to_data_entry_selector` is also Esc, and an
        Esc that lands on the canvas instead of the popup EXITS the W-2 screen. So: re-find
        the popup, prove its edit owns the keyboard, and only then send the key."""
        popup = self._find_headsdown_popup(timeout=0.5)
        if popup is None:
            self._popup_owned = False
            return {"ok": True, "note": "popup already closed — nothing to disarm"}
        try:
            edit = self._popup_edit(popup)
            edit.wait("ready", timeout=2)
            eh = int(edit.handle)
        except Exception as e:
            return {"ok": False, "note": f"could not resolve the popup edit to disarm: {e}"}
        if not self._focus_popup_edit(edit, eh):
            return {"ok": False,
                    "note": "popup edit never took focus — did NOT send Esc, because an Esc on "
                            "Drake's canvas exits the data-entry screen. Press Esc by hand."}
        self._keys("{ESC}")
        import time
        time.sleep(0.25)
        still = self._find_headsdown_popup(timeout=0.3) is not None
        self._popup_owned = False
        return {"ok": not still,
                "note": ("popup closed — Drake is no longer armed" if not still else
                         "Esc did not close the popup; press Esc in Drake by hand before the "
                         "next run, or it will be armed for a value")}

    def type_text(self, target: dict, text: str, opts: Optional[dict] = None) -> dict:
        opts = opts or {}
        screen, field = target["screen"], target["field"]
        try:
            self._field_binding(screen, field)  # validate the field is bound (raises if not)
            if self.dry_run:
                print(f"[dry-run] type {screen}/{field} = {text!r} (opts={opts})")
                return {"ok": True}
            # Plant a caret FIRST. send_keys is a GLOBAL injection into whatever holds
            # focus — on Drake's canvas there is no caret until we click, so an unfocused
            # type silently vanishes into the frame. If we can't focus, do NOT type into
            # the void and report ok:true; fail honestly so the caller halts.
            # EXCEPTION: opts.focus == false → sequential/tab-order entry that types
            # wherever the caret already sits (right after openScreen, or after a Tab/Enter).
            if opts.get("focus", True):
                f = self.focus(target)
                if not f.get("ok"):
                    return {"ok": False, "error": f"cannot focus {screen}/{field} before typing: {f.get('error')}"}
            # else: focus:false → type at the caret Drake already placed (on screen-open,
            # or after a prior Enter/Tab). Do NOT set_focus/foreground — that would reset
            # the canvas caret and the keys would land nowhere.
            if opts.get("clearFirst"):
                # Ctrl-free clear: End, then Backspaces. "^a{BACKSPACE}" is toxic on Drake.
                self._keys(self.nav.get("field_clear", "{END}{BACKSPACE 40}"))
            self._keys(_escape_keys(text))  # escape pywinauto special chars; typed literally
            if opts.get("commit"):
                self._keys(self.nav.get("field_commit", "{ENTER}"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def type_raw(self, text: str, advance: Optional[str] = None) -> dict:
        """
        Type literal text into whatever Drake field currently holds the caret, using
        genuine foreground synthetic keystrokes (pywinauto send_keys = SendInput) — no
        field binding, no click. This is the ISOLATION PROOF that our keystrokes actually
        land on Drake's custom canvas (the thing accessibility/set_text could not do).
        Optionally press an advance key afterwards (e.g. "TAB" or "ENTER").
        """
        try:
            if self.dry_run:
                print(f"[dry-run] type_raw {text!r} advance={advance}")
                return {"ok": True}
            # Do NOT set_focus / foreground here. You already clicked the field, so it
            # holds the caret and Drake is in front. set_focus() on the top window would
            # RESET the canvas caret (the field's cursor vanishes) and the keys land
            # nowhere — exactly the "cursor disappeared, nothing typed" symptom. Just type
            # into whatever currently has focus; send_keys injects to the foreground window.
            self._keys(_escape_keys(text))
            if advance:
                self._keys("{" + advance.upper() + "}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def click_at(self, x, y) -> dict:
        """Physically click a window-relative (x, y) point — the agent's own
        click-to-focus, exposed for diagnosis. Returns the window's top-left + the
        absolute screen point clicked, so a coordinate/DPI mismatch is visible in the
        numbers (and you can watch where the cursor actually lands)."""
        if self.dry_run or self.win is None:
            return {"ok": False, "error": "no window"}
        try:
            r = self.win.rectangle()
            self.win.set_focus()
            self.win.click_input(coords=(int(x), int(y)))
            return {"ok": True, "window_topleft": [r.left, r.top],
                    "window_size": [r.width(), r.height()],
                    "clicked_abs": [r.left + int(x), r.top + int(y)]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def read_field(self, target: dict) -> dict:
        screen, field = target["screen"], target["field"]
        if self.dry_run:
            return {"ok": True, "value": None, "method": "none", "confidence": 0.0,
                    "error": "dry-run: no live read"}
        try:
            fb = self._field_binding(screen, field)
            value, method, confidence = None, "none", 0.0
            # 1) UIA ValuePattern — only if this build actually exposes it (dead on Drake).
            if fb.get("automation_id") and self.read_back_method in ("uia", "auto"):
                ctrl = self._edit_by_auto_id(fb["automation_id"])
                value, method, confidence = _read_value(ctrl)
            # 2) Clipboard copy-back — dead on Drake 2025 (copy returns nothing + breaks
            #    focus). Kept for other software / a future build.
            if value is None and self.read_back_method in ("clipboard", "auto"):
                v = self.copy_focused_to_clipboard()
                if v:
                    value, method, confidence = v, "clipboard", 1.0
            # 3) OCR of the field's calibrated pixel crop — approximate, but the only
            #    automated read-back Drake actually allows. confidence < 1.0 on purpose:
            #    an OCR match is a typo-catcher, never proof — a human still gates.
            if value is None and self.read_back_method in ("ocr", "auto") and fb.get("ocr_box"):
                v = self._ocr_read(fb["ocr_box"])
                if v is not None:
                    value, method, confidence = v, "ocr", 0.6
            # 4) Screenshot floor — no automated value at all; the honest read-back is a
            #    human looking at the capture. Reported as needsHuman, not an error.
            if value is None and self.read_back_method == "screenshot":
                return {"ok": False, "value": None, "method": "screenshot",
                        "confidence": 0.0, "needsHuman": True,
                        "error": "screenshot read-back — verify this field visually"}
            # 5) Last resort: whatever UIA element holds focus (dead on Drake, kept honest).
            if value is None and self.read_back_method in ("uia", "auto"):
                foc = _focused_element(self.app)
                if foc is not None:
                    value, method, confidence = _read_value(foc)
            ok = value is not None and confidence > 0
            out = {"ok": ok, "value": value, "method": method, "confidence": confidence}
            if not ok:
                out["error"] = "no readable value (Drake exposes none; set read_back_method to ocr/screenshot)"
            return out
        except Exception as e:
            return {"ok": False, "value": None, "method": "none", "confidence": 0.0, "error": str(e)}

    def copy_focused_to_clipboard(self) -> Optional[str]:
        """
        Read the CURRENTLY FOCUSED Drake field by copying it (Ctrl+A, Ctrl+C) and
        returning the clipboard text. This is the Plan-B read-back now that Drake's
        grid exposes no UIA value. The field must already hold keyboard focus and must
        NOT have been committed/advanced yet. Clears the clipboard first so a stale
        value can never masquerade as a successful read.
        """
        if self.dry_run:
            return None
        if pyperclip is None:
            raise RuntimeError("clipboard read-back needs 'pyperclip' (pip install pyperclip)")
        import time
        try:
            pyperclip.copy("")  # clear — a leftover clipboard must not look like a read
        except Exception:
            pass
        self._keys("^a^c")
        time.sleep(0.15)  # give Windows a moment to populate the clipboard
        try:
            v = pyperclip.paste()
        except Exception:
            v = ""
        return v or None

    def _foreground(self) -> None:
        """
        Bring the Drake window to the FRONT before any screen capture. `capture_as_image`
        grabs the window's screen rectangle, not its pixels directly — so if Drake is
        occluded by the editor/terminal, the capture returns the *occluding* window
        (this is why an early after.png caught VS Code). Restores it if minimized, then
        raises + focuses. All best-effort: a missing method just no-ops.
        """
        if self.dry_run or self.win is None:
            return
        import time
        try:
            if self.win.is_minimized():
                self.win.restore()
        except Exception:
            pass
        try:
            # pywinauto's set_focus does the AttachThreadInput + SetForegroundWindow
            # dance that reliably surfaces a window owned by another process.
            self.win.set_focus()
        except Exception:
            pass
        # Fallback z-order raise for when SetForegroundWindow is restricted (Python
        # launched from the editor's terminal may lack foreground rights — then keystrokes
        # still reach Drake via AttachThreadInput but the window never rises, which is how
        # a capture caught VS Code even though data went into Drake). ctypes.windll only
        # resolves on Windows; this path never runs on Mac (guarded by dry_run/win above).
        try:
            import ctypes
            hwnd = int(self.win.handle)
            user32 = ctypes.windll.user32
            user32.BringWindowToTop(hwnd)
            SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        except Exception:
            pass
        time.sleep(0.25)  # let the window actually paint on top before we grab pixels

    def _capture_window_image(self):
        """A PIL image of JUST the Drake window rectangle, Drake brought to front first.
        `capture_as_image()` already crops to the window's bounding rect (not the whole
        monitor), so background windows are excluded once Drake is foreground."""
        self._foreground()
        return self.win.capture_as_image()

    def _ocr_read(self, box) -> Optional[str]:
        """
        OCR the calibrated [x, y, w, h] pixel crop of the Drake window (coordinates
        relative to the window's top-left). This is Plan C: Drake exposes no
        programmatic value, so we read the box the way a human would — off the screen.
        Approximate by nature; caller keeps confidence < 1.0 and a human still gates.
        Needs Pillow + pytesseract + the Tesseract binary on the VM.
        """
        if self.dry_run or self.win is None:
            return None
        if pytesseract is None:
            raise RuntimeError(
                "OCR read-back needs pytesseract + Pillow (pip install pytesseract pillow) "
                "and the Tesseract binary installed on the VM")
        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        # Capture WITHOUT foregrounding: _foreground()'s set_focus would reset Drake's
        # canvas caret mid-entry, desyncing the next Enter/type. During an active run
        # Drake is already the foreground window, so a direct grab is correct here.
        img = self.win.capture_as_image()
        x, y, w, h = (int(n) for n in box)
        crop = img.crop((x, y, x + w, y + h))
        # psm 7 = "treat the crop as a single line" — right for one field's value.
        text = pytesseract.image_to_string(crop, config="--psm 7").strip()
        return text or None

    def window_info(self) -> dict:
        """Identity of the window the agent bound to — so you can confirm it's the main
        data-entry frame and not the chat overlay (title + size + handle)."""
        if self.dry_run or self.win is None:
            return {"title": None, "width": None, "height": None, "handle": None}
        try:
            r = self.win.rectangle()
            return {"title": self.win.window_text(), "width": r.width(),
                    "height": r.height(), "handle": getattr(self.win, "handle", None)}
        except Exception as e:
            return {"title": None, "width": None, "height": None, "handle": None, "error": str(e)}

    def save_screenshot(self, path: str, grid: bool = False) -> dict:
        """Save a PNG of the live Drake window to disk — the human-verify floor and the
        source you read click_xy / ocr_box coordinates off. (screenshot() returns base64
        for the wire; this writes a file for a person to open.) Brings Drake to the front
        first so the capture is Drake, never the editor/terminal that launched the run.
        grid=True overlays a labeled pixel grid so you can read coordinates by eye."""
        try:
            if self.dry_run or self.win is None:
                return {"ok": False, "path": None, "error": "no window"}
            img = self._capture_window_image()
            if grid:
                img = _overlay_grid(img)
            img.save(path, format="PNG")
            return {"ok": True, "path": path, "takenAt": _now()}
        except Exception as e:
            return {"ok": False, "path": None, "error": str(e)}

    # -- probe / calibrate helpers -----------------------------------------

    def list_edit_controls(self) -> list[dict]:
        """Every Edit control on the active window, with its UIA identity + readability."""
        if self.dry_run or self.win is None:
            return []
        found = []
        for ctrl in self.win.descendants(control_type="Edit"):
            value, method, confidence = _read_value(ctrl)
            info = ctrl.element_info
            found.append({
                "automation_id": getattr(info, "automation_id", None),
                "name": getattr(info, "name", None),
                "value": value,
                "read_method": method,
                "readable": confidence >= 1.0,
            })
        return found

    def screenshot(self) -> dict:
        try:
            if self.dry_run or self.win is None:
                return {"ok": False, "pngBase64": None, "takenAt": _now(), "error": "no window"}
            import base64, io
            img = self._capture_window_image()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return {"ok": True, "pngBase64": base64.b64encode(buf.getvalue()).decode(), "takenAt": _now()}
        except Exception as e:
            return {"ok": False, "pngBase64": None, "takenAt": _now(), "error": str(e)}


# --- module helpers ---------------------------------------------------------

# Win32 focus/caret + cross-process text oracles for the guarded heads-down protocol.
# GUITHREADINFO.hwndFocus is the truth of who owns the keyboard, read cross-process WITHOUT
# AttachThreadInput (which is racy); WM_GETTEXT reads the popup edit's text back out as
# proof the number landed before we press the irreversible Enter. Windows-only; the whole
# block degrades to None off-Windows (ctypes.wintypes only imports there) — the callers
# only run on the VM (dry_run returns before reaching them).
try:
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _GTI(_ctypes.Structure):
        _fields_ = [("cbSize", _wintypes.DWORD), ("flags", _wintypes.DWORD),
                    ("hwndActive", _wintypes.HWND), ("hwndFocus", _wintypes.HWND),
                    ("hwndCapture", _wintypes.HWND), ("hwndMenuOwner", _wintypes.HWND),
                    ("hwndMoveSize", _wintypes.HWND), ("hwndCaret", _wintypes.HWND),
                    ("rcCaret", _wintypes.RECT)]
except Exception:  # pragma: no cover - non-Windows
    _ctypes = None  # type: ignore
    _wintypes = None  # type: ignore
    _GTI = None  # type: ignore

_GUI_CARETBLINKING = 0x00000001

# True only where the win32 API actually exists — ctypes.wintypes imports fine on
# mac/linux (which is how the simulator runs), but windll does not.
_WINFN = bool(_ctypes is not None and hasattr(_ctypes, "windll"))

_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000  # WS_BORDER | WS_DLGFRAME — a real titlebar
_GA_ROOT = 2
_GWL_STYLE = -16
_GW_OWNER = 4

# Window classes that are pure UI chrome — never data-entry-relevant, never logged:
# tooltips, menus/dropdown lists, IME helpers, shadow/ghost effects.
_COSMETIC_CLASSES = {"tooltips_class32", "#32768", "ComboLBox", "SysShadow",
                     "IME", "MSCTFIME UI", "Ghost"}

_U32 = None


def _u32():
    """user32 with HWND-returning functions given a proper restype — the default c_int
    restype can sign-mangle handles on 64-bit, and the structural gate compares handles
    for identity, so they must round-trip exactly."""
    global _U32
    if _U32 is None:
        u = _ctypes.windll.user32
        u.GetWindow.restype = _wintypes.HWND
        u.GetForegroundWindow.restype = _wintypes.HWND
        u.GetAncestor.restype = _wintypes.HWND
        _U32 = u
    return _U32


def _enum_toplevel_windows(pid=None) -> list:
    """Snapshot top-level windows: handle, pid, title, class, visibility, enabled state,
    owner, style bits, rect. `pid=None` enumerates EVERY process (used to locate a popup
    that turns out not to be owned by the process we attached to). Raw ctypes (no pywinauto
    wrapping) so it is fast enough to run per-field and cannot be fooled by backend
    quirks — notably pywinauto's anchored title_re. Windows-only."""
    u32 = _u32()
    out = []

    @_ctypes.WINFUNCTYPE(_wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        wpid = _wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, _ctypes.byref(wpid))
        if pid is not None and int(wpid.value) != int(pid):
            return True
        title = _ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, title, 256)
        cls = _ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(hwnd, cls, 128)
        r = _wintypes.RECT()
        u32.GetWindowRect(hwnd, _ctypes.byref(r))
        style = int(u32.GetWindowLongW(hwnd, _GWL_STYLE)) & 0xFFFFFFFF
        out.append({
            "hwnd": int(hwnd),
            "pid": int(wpid.value),
            "title": title.value,
            "class_name": cls.value,
            "visible": bool(u32.IsWindowVisible(hwnd)),
            "enabled": bool(u32.IsWindowEnabled(hwnd)),
            "owner": int(u32.GetWindow(hwnd, _GW_OWNER) or 0),
            "style": style,
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
        })
        return True

    u32.EnumWindows(_cb, 0)
    return out


def _window_pid(hwnd) -> int:
    pid = _wintypes.DWORD()
    _u32().GetWindowThreadProcessId(int(hwnd), _ctypes.byref(pid))
    return int(pid.value)


def _keyboard_target_info() -> dict:
    """Where a synthetic keystroke lands RIGHT NOW: the foreground root window plus the
    focused control of the foreground THREAD — SendInput's actual destination. This is
    the oracle behind the HWND-scoped keystroke gate. Windows-only."""
    u32 = _u32()
    fg = int(u32.GetForegroundWindow() or 0)
    root = int(u32.GetAncestor(fg, _GA_ROOT) or 0) if fg else 0
    root = root or fg
    focus = 0
    if fg:
        try:
            tid = u32.GetWindowThreadProcessId(fg, None)
            g = _GTI()
            g.cbSize = _ctypes.sizeof(_GTI)
            if u32.GetGUIThreadInfo(tid, _ctypes.byref(g)) and g.hwndFocus:
                focus = int(g.hwndFocus)
        except Exception:
            pass
    title = _ctypes.create_unicode_buffer(256)
    cls = _ctypes.create_unicode_buffer(128)
    if root:
        u32.GetWindowTextW(root, title, 256)
        u32.GetClassNameW(root, cls, 128)
    return {"foreground": fg, "root": root, "focus": focus,
            "root_title": title.value, "root_class": cls.value,
            "root_pid": _window_pid(root) if root else 0}


def _enum_child_summaries(hwnd, cap: int = 64) -> list:
    """Children of a window with class/text/rect — the message text of a dialog lives in
    its Static children. Text via hang-safe WM_GETTEXT (300ms); capped so a huge tree
    (the chat's embedded browser) can't stall a halt report."""
    u32 = _u32()
    out = []

    @_ctypes.WINFUNCTYPE(_wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)
    def _cb(h, _lparam):
        if len(out) >= cap:
            return False
        cls = _ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(h, cls, 128)
        r = _wintypes.RECT()
        u32.GetWindowRect(h, _ctypes.byref(r))
        try:
            txt = (_read_edit_text(h, timeout_ms=300) or "").strip()
        except Exception:
            txt = ""
        out.append({"hwnd": int(h), "class_name": cls.value, "text": txt[:120],
                    "visible": bool(u32.IsWindowVisible(h)),
                    "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top]})
        return True

    u32.EnumChildWindows(int(hwnd), _cb, 0)
    return out


def _window_deep_text(hwnd, cap: int = 40) -> str:
    """Every child's WM_GETTEXT joined — so a halt names WHAT a dialog says, not just
    that one exists."""
    parts = []
    try:
        for c in _enum_child_summaries(hwnd, cap=cap):
            if c.get("text"):
                parts.append(c["text"])
    except Exception:
        pass
    return " ".join(parts)


def _dialogish(w) -> bool:
    """Does this window have the STRUCTURE of a dialog (vs chrome/toast/overlay)?
    #32770 is THE Windows dialog class (every MessageBox and .rc dialog). Custom popups
    count only when they look like a framed, owned dialog: WS_POPUP + a real caption +
    an owner. Captionless popups (dropdown lists, toasts, the chat overlay) do not."""
    if (w.get("class_name") or "") == "#32770":
        return True
    style = int(w.get("style") or 0)
    return (bool(style & _WS_POPUP)
            and (style & _WS_CAPTION) == _WS_CAPTION
            and bool(w.get("owner")))


def _classify_process_windows(wins, *, popup_title_re, main_hwnd, baseline, benign_seen,
                              main_enabled):
    """The pure decision core of the structural dialog gate — no Windows calls, so it is
    provable offline (simulate_headsdown.py feeds it synthetic snapshots). Given the
    process's top-level windows, decide what (if anything) is actually BLOCKING entry:

      rule 1  a NEW dialog-shaped window (not baseline, not already-benign) is a
              validator/error/prompt → blocker, even if the main frame is still enabled;
      rule 2  main frame DISABLED with no heads-down popup up → a modal is pumping:
              blame a new enabled window if there is one, else an enabled dialog-shaped
              one, else report the modality itself (never blame baseline furniture like
              the chat overlay by name);
      else    new non-dialog windows are benign (returned for logging), baseline windows
              are furniture, cosmetic classes/invisible/zero-area windows are ignored.

    Returns (blocker | None, benign_new, popup_present)."""
    import re as _re
    pat = _re.compile(popup_title_re)
    popup_present = False
    cands = []
    for w in wins:
        h = int(w.get("hwnd") or 0)
        if main_hwnd is not None and h == int(main_hwnd):
            continue
        if pat.search(w.get("title") or ""):
            popup_present = popup_present or bool(w.get("visible", True))
            continue
        if not w.get("visible"):
            continue
        r = w.get("rect") or [0, 0, 0, 0]
        if r[2] <= 0 or r[3] <= 0:
            continue
        if (w.get("class_name") or "") in _COSMETIC_CLASSES:
            continue
        cands.append(w)
    new = [w for w in cands
           if int(w["hwnd"]) not in baseline and int(w["hwnd"]) not in benign_seen]
    for w in new:  # rule 1
        if _dialogish(w):
            b = dict(w)
            b["why"], b["dialogish"] = "new-dialog", True
            return b, [], popup_present
    if main_enabled is False and not popup_present:  # rule 2
        for pool in ([w for w in new if w.get("enabled")],
                     [w for w in cands if w.get("enabled") and _dialogish(w)]):
            if pool:
                b = dict(pool[0])
                b["why"], b["dialogish"] = "main-disabled", _dialogish(pool[0])
                return b, [], popup_present
        b = {"hwnd": 0, "title": None, "class_name": None, "why": "main-disabled",
             "dialogish": False, "candidates": [w.get("title") for w in cands]}
        return b, [], popup_present
    return None, new, popup_present


def _gui_thread_info(any_hwnd):
    """(hwndFocus, hwndCaret, (l,t,r,b), flags) for the GUI thread owning any_hwnd —
    Drake's real keyboard-focus/caret state, cross-process, no AttachThreadInput. Handles
    are returned as ints (0 if null). Windows-only."""
    u32 = _ctypes.windll.user32
    tid = u32.GetWindowThreadProcessId(int(any_hwnd), None)
    g = _GTI()
    g.cbSize = _ctypes.sizeof(_GTI)
    if not u32.GetGUIThreadInfo(tid, _ctypes.byref(g)):
        raise OSError("GetGUIThreadInfo failed")
    rc = g.rcCaret
    fh = int(g.hwndFocus) if g.hwndFocus else 0
    ch = int(g.hwndCaret) if g.hwndCaret else 0
    return (fh, ch, (rc.left, rc.top, rc.right, rc.bottom), int(g.flags))


def _read_edit_text(hedit, timeout_ms: int = 1000) -> str:
    """Read another process's Edit control text via WM_GETTEXT (hang-safe
    SendMessageTimeout). GetWindowText does NOT work cross-process for a foreign Edit.
    Windows-only."""
    WM_GETTEXT, WM_GETTEXTLENGTH, SMTO_ABORTIFHUNG = 0x000D, 0x000E, 0x0002
    u32 = _ctypes.windll.user32
    n = _wintypes.DWORD()
    u32.SendMessageTimeoutW(int(hedit), WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, _ctypes.byref(n))
    buf = _ctypes.create_unicode_buffer(n.value + 1)
    res = _wintypes.DWORD()
    ok = u32.SendMessageTimeoutW(int(hedit), WM_GETTEXT, n.value + 1, buf,
                                 SMTO_ABORTIFHUNG, timeout_ms, _ctypes.byref(res))
    if not ok:
        raise TimeoutError("popup WM_GETTEXT timed out")
    return buf.value


def _safe_read_edit(hedit) -> str:
    """_read_edit_text, stripped, never raising ('' on any failure)."""
    try:
        return (_read_edit_text(hedit) or "").strip()
    except Exception:
        return ""


def _read_edit_or_none(hedit):
    """The edit's text, or None if the READ ITSELF failed.

    Distinct from _safe_read_edit, which collapses failure into '' — and '' is a
    meaningful answer here ("Drake cleared the box"). The WM_GETTEXT timeout fires
    precisely when Drake is busy, which is precisely when a fabricated '' would be
    believed. Callers must treat None as NO EVIDENCE, never as agreement."""
    try:
        return (_read_edit_text(hedit) or "").strip()
    except Exception:
        return None


def _popup_prompt_text(popup_hwnd, edit_hwnd=None, edit_class: str = "Edit") -> str:
    """The heads-down popup's PROMPT — its child text minus the box you type into.

    On this build the number prompt reads "To begin, enter desired field number and press
    enter."; once Drake has taken the number it asks for the value instead. Comparing this
    against a baseline captured just before Enter is the ONLY way to tell "Drake consumed
    the number" from "Drake silently refused it" — both leave the edit empty. Returns ''
    when nothing is readable, which callers must treat as no evidence."""
    parts = []
    try:
        for c in _enum_child_summaries(int(popup_hwnd), cap=24):
            if edit_hwnd and int(c.get("hwnd") or 0) == int(edit_hwnd):
                continue
            if (c.get("class_name") or "") == edit_class:
                continue
            t = (c.get("text") or "").strip()
            if t:
                parts.append(t)
    except Exception:
        return ""
    return " ".join(parts)


def _as_number(s):
    """Decimal value of a money-ish string, or None if it is not cleanly numeric.
    Accepts a leading sign, '$', thousands commas, spaces and accounting parens. Rejects
    anything else (hyphens mid-string, exponents, nan/inf) so EINs and ZIPs fall through
    to the textual comparison rather than being mangled into numbers."""
    from decimal import Decimal, InvalidOperation
    t = str(s).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    if t[:1] in ("+", "-"):
        neg = neg or t[0] == "-"
        t = t[1:]
    if not t or t == "." or t.count(".") > 1:
        return None
    if not all(ch.isdigit() or ch == "." for ch in t):
        return None
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return -d if neg else d


def _same_value(got, expected) -> bool:
    """Did the box end up holding the value we meant?

    This is the LAST gate before an irreversible Enter, on a surface with no read-back, so
    it forgives only COSMETIC differences — the reformatting a field does as you type —
    and never a difference in what the number MEANS.

    Forgives: thousands separators and currency ('52,000' == '52000'), the hyphens Drake
    inserts into an EIN or ZIP ('12-3456789' == '123456789'), trailing cents ('52000.00'
    == '52000'), and case.

    Never forgives: a moved decimal point or a flipped sign. '322450' is not '3224.50' (a
    100x error) and '5000' is not '-5000'. Both were silently APPROVED before, because the
    old normalization deleted '.', '-' and '()' from both sides before comparing — so the
    one gate protecting a tax return blessed exactly the two ways a money value goes
    catastrophically wrong."""
    if got is None:
        return False
    a, b = str(got).strip(), str(expected).strip()
    if a == b:
        return True
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return na == nb  # Decimal equality: 52000.00 == 52000, 3224.50 != 322450
    # Not a clean numeric pair. If either side carries a decimal point or a leading sign,
    # the difference is arithmetic rather than cosmetic — refuse instead of normalizing it
    # away. (An EIN/ZIP hyphen sits mid-string, so those still reach the fallback.)
    if any(("." in s) or s[:1] in ("-", "+", "(") for s in (a, b)):
        return False
    norm = lambda s: "".join(ch for ch in s if ch.isalnum()).lower()
    return norm(a) == norm(b)


def _overlay_grid(img, step: int = 50, label_every: int = 100):
    """Draw a labeled pixel grid over a capture so you can read a field's click_xy /
    ocr_box coordinates straight off the PNG: faint lines every `step`px, red labeled
    lines every `label_every`px (labels are window-relative pixels — the same frame
    click_xy/ocr_box use). No-op if Pillow's ImageDraw isn't available."""
    try:
        from PIL import ImageDraw
    except Exception:
        return img
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(0, w, step):
        major = x % label_every == 0
        draw.line([(x, 0), (x, h)], fill=(255, 80, 80) if major else (215, 215, 215))
        if major:
            draw.text((x + 2, 2), str(x), fill=(255, 0, 0))
    for y in range(0, h, step):
        major = y % label_every == 0
        draw.line([(0, y), (w, y)], fill=(255, 80, 80) if major else (215, 215, 215))
        if major:
            draw.text((2, y + 2), str(y), fill=(255, 0, 0))
    return img


def _box_center(box):
    """Center point (x, y) of a [x, y, w, h] box, or None. Lets a field reuse its
    ocr_box as a click-to-focus target when no explicit click_xy is set."""
    if not box:
        return None
    x, y, w, h = box
    return (int(x) + int(w) // 2, int(y) + int(h) // 2)


def _escape_keys(text: str) -> str:
    """Escape pywinauto send_keys metacharacters so text is typed literally."""
    out = []
    for ch in str(text):
        if ch in "{}()+^%~[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def _read_value(ctrl) -> tuple[Optional[str], str, float]:
    """
    Try to read a control's value the HONEST way. Returns (value, method, confidence).
    ValuePattern (UIA) → confidence 1.0; legacy/name text → 1.0 if present; else 0.
    """
    # 1) UIA ValuePattern — the gold standard.
    try:
        v = ctrl.get_value()
        if v is not None and v != "":
            return str(v), "uia-value", 1.0
    except Exception:
        pass
    # 2) LegacyIAccessible value.
    try:
        legacy = ctrl.legacy_properties()
        v = legacy.get("Value")
        if v:
            return str(v), "uia-value", 1.0
    except Exception:
        pass
    # 3) Name / window text — weaker, but still an accessibility read.
    try:
        v = ctrl.window_text()
        if v:
            return str(v), "uia-name", 1.0
    except Exception:
        pass
    return None, "none", 0.0


def _focused_element(app):
    """The element that currently has keyboard focus (best-effort, UIA)."""
    try:
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo
        from pywinauto.controls.uiawrapper import UIAWrapper
        raw = IUIA().iuia.GetFocusedElement()
        return UIAWrapper(UIAElementInfo(raw))
    except Exception:
        return None
