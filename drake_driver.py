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

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Attach to the running Drake process and resolve the MAIN data-entry frame —
        never a floating overlay (the Live Chat bubble is a topmost ~84x84 window that
        top_window() would grab first, sending keystrokes to the chat widget)."""
        if self.dry_run:
            return
        if Application is None:
            raise RuntimeError("pywinauto is not available (run on the Windows VM)")
        self.app = Application(backend="uia").connect(title_re=self.title_re, timeout=20)
        self.win = self._resolve_main_window()
        self.main_hwnd = int(self.win.handle)
        self._connect_win32_popup()  # second connection for the heads-down dialog
        self._warn_if_elevation_mismatch()
        self._foreground()

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

    def _focused_hwnd(self):
        """(hwndFocus, hwndCaret, rcCaret, flags) for Drake's GUI thread — the cross-process
        truth of which control owns the keyboard/caret RIGHT NOW (no AttachThreadInput).
        The focus/caret oracle for the guarded-keystroke protocol."""
        return _gui_thread_info(self.main_hwnd)

    def _find_headsdown_popup(self, timeout: float = 0.5):
        """The heads-down popup as a win32 WindowSpecification, or None if not present.
        Re-found each cycle — the dialog is created/destroyed per jump on this build, so a
        cached wrapper goes stale."""
        if self.w32 is None:
            return None
        try:
            spec = self.w32.window(title_re=self.popup_title_re)
            return spec if spec.exists(timeout=timeout) else None
        except Exception:
            return None

    def _popup_edit(self, popup):
        """The popup's text box (its real Edit control)."""
        try:
            return popup.child_window(class_name=self.popup_edit_class)
        except Exception:
            return popup.child_window(control_type="Edit")

    def _detect_unexpected_dialog(self):
        """Enumerate Drake's top-level windows; allow ONLY the main frame and the heads-down
        popup. Anything else (the 'invalid field' modal, any validation/error dialog) is an
        anomaly → return its identity + text so the caller HALTs for a human. NEVER
        auto-clicks / dismisses. None means all clear."""
        if self.w32 is None:
            return None
        import re as _re
        try:
            for w in self.w32.windows():
                try:
                    h = int(w.handle)
                except Exception:
                    continue
                if h == self.main_hwnd:
                    continue
                t = w.window_text() or ""
                if _re.search(self.popup_title_re, t):
                    continue
                try:
                    kids = " ".join((c.window_text() or "") for c in w.descendants())
                except Exception:
                    kids = ""
                return {"title": t, "text": (t + " " + kids).strip()[:400], "handle": h}
        except Exception:
            return None
        return None

    def _ensure_popup_open(self, *, method: str = "scancode", timeout: float = 2.5) -> dict:
        """IDEMPOTENT open of the heads-down popup. If it's already present, do NOTHING —
        never a second Ctrl+N (that would TOGGLE the mode back off, the old bug). Else fire
        Ctrl+N once and WAIT for the popup to become ready (readiness, not a fixed sleep).
        Ctrl+N only registers when a canvas field is active; if it doesn't open the popup
        we surface that as a HALT via the wait timeout (the reliable signal)."""
        popup = self._find_headsdown_popup(timeout=0.3)
        if popup is not None:
            return {"ok": True, "opened": False, "popup": popup}
        if self.w32 is None:
            return {"ok": False, "reason": "win32 popup connection unavailable (reconnect on the VM)"}
        self.headsdown_toggle(method=method)  # Ctrl+N (scancode)
        try:
            spec = self.w32.window(title_re=self.popup_title_re)
            spec.wait("visible ready", timeout=timeout)
            return {"ok": True, "opened": True, "popup": spec}
        except Exception as e:
            return {"ok": False, "reason": f"Ctrl+N did not open the heads-down popup "
                                           f"(is a canvas field active? {e})"}

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

    def headsdown_type(self, field_no, value, *, method: str = "scancode") -> dict:
        """Enter ONE value BY FIELD NUMBER — race-free and VERIFIED — via the heads-down
        popup. This replaces the old open-loop "fire Ctrl+N, sleep, blind-type" that raced
        keystrokes between the popup and the canvas (digits into EIN, 'invalid field',
        cascade). The guarded protocol, every step gated:

          1. Drake main frame still alive?                         else HALT
          2. no unexpected/error dialog already up?                else HALT
          3. ensure the popup is open (IDEMPOTENT — Ctrl+N only if absent, never a
             second toggle) and READY (wait, not sleep)           else HALT
          4. focus the popup's real Edit and VERIFY focus == that Edit (GetGUIThreadInfo)
                                                                   else HALT
          5. type the field number into the FOCUSED popup edit, then READ IT BACK
             (WM_GETTEXT) and assert it == the number BEFORE the irreversible Enter
                                                                   else HALT
          6. press Enter → the jump; PROVE the popup closed (number accepted)  else HALT
          7. no error dialog after the jump?                       else HALT
          8. type the VALUE onto the canvas (global keys, vk_packet=False, NO set_focus,
             NO trailing Enter — the next field's Ctrl+N is what leaves this field)

        The number can never land on the canvas because we verify the popup edit holds
        keyboard focus and read the digits back before committing. Returns {ok:True,...}
        or {ok:False, halt:True, reason:...} — the caller STOPS the batch for a human.
        Never presses Enter after the value; never auto-dismisses a dialog."""
        fn = str(field_no)
        try:
            if self.dry_run:
                print(f"[dry-run] headsdown field {fn} = {value!r}")
                return {"ok": True}
            import time
            # 1) app alive?
            if self.win is None or not self.win.exists():
                return {"ok": False, "halt": True, "reason": "Drake main frame vanished (app closed)"}
            # 2) unexpected/error dialog already up?
            bad = self._detect_unexpected_dialog()
            if bad:
                return {"ok": False, "halt": True, "reason": f"unexpected dialog before entry: {bad['title']!r}", "dialog": bad}
            # 3) idempotent popup open (never a second Ctrl+N if already open)
            opened = self._ensure_popup_open(method=method, timeout=2.5)
            if not opened.get("ok"):
                return {"ok": False, "halt": True, "reason": opened.get("reason")}
            popup = self._find_headsdown_popup(timeout=1.0)
            if popup is None:
                return {"ok": False, "halt": True, "reason": "heads-down popup not found after open"}
            edit = self._popup_edit(popup)
            try:
                edit.wait("ready", timeout=2)
                eh = int(edit.handle)
            except Exception as e:
                return {"ok": False, "halt": True, "reason": f"popup edit not ready / no handle: {e}"}
            # 4) focus the edit + VERIFY via the ctypes GUI-thread oracle (retry a few times)
            focused = False
            for _ in range(6):
                try:
                    edit.set_focus()
                except Exception:
                    pass
                try:
                    if self._focused_hwnd()[0] == eh:
                        focused = True
                        break
                except Exception:
                    pass
                time.sleep(0.05)
            if not focused:
                return {"ok": False, "halt": True, "reason": "popup edit never took keyboard focus"}
            # 5) place the number in the FOCUSED popup edit, then READ IT BACK before Enter.
            #    Clear first (atomic), then type real keys (Drake honors scan/VK), verify.
            try:
                edit.set_edit_text("")  # clear any stale digits (EM_REPLACESEL, atomic)
            except Exception:
                pass
            self._keys(fn)  # real VK/scan keys land in the focused popup edit (focus verified)
            got = _safe_read_edit(eh)
            if got != fn:
                # fallback: place atomically via window message, re-read
                try:
                    edit.set_edit_text(fn)
                except Exception:
                    pass
                got = _safe_read_edit(eh)
                if got != fn:
                    return {"ok": False, "halt": True,
                            "reason": f"popup edit shows {got!r}, expected {fn!r} — refusing to press Enter"}
            # 6) fire the jump, then PROVE the popup consumed it (number accepted)
            self._keys("{ENTER}")
            try:
                popup.wait_not("visible", timeout=3)
            except Exception:
                bad = self._detect_unexpected_dialog()
                reason = "popup still open after Enter — field number likely rejected for this screen"
                if bad:
                    reason = f"error dialog after Enter: {bad['title']!r}"
                return {"ok": False, "halt": True, "reason": reason, "dialog": bad}
            # 7) error modal after the jump?
            bad = self._detect_unexpected_dialog()
            if bad:
                return {"ok": False, "halt": True, "reason": f"error dialog after jump: {bad['title']!r}", "dialog": bad}
            # (advisory) did the caret land on a canvas field?
            landed = None
            try:
                landed = bool(self._focused_hwnd()[3] & _GUI_CARETBLINKING)
            except Exception:
                pass
            # 8) type the VALUE onto the canvas — global keys, vk_packet=False, NO set_focus,
            #    NO trailing Enter (the next field's Ctrl+N leaves this field cleanly).
            self._keys(_escape_keys(str(value)))
            return {"ok": True, "field_no": field_no, "reopened": opened.get("opened"),
                    "caret_after_jump": landed}
        except Exception as e:
            return {"ok": False, "halt": True, "reason": str(e)}

    def probe_headsdown_popup(self) -> dict:
        """READ-ONLY diagnostic: with the heads-down popup open, dump its real control tree,
        test a set_edit_text('4') round-trip, and report whether the popup CLOSES after
        Enter (per-jump: value goes to the canvas) or STAYS (persistent command-bar: value
        goes back to the popup). Resolves the exact Edit class + focus handles + model so
        the entry driver binds correctly. Writes NO field value — the test Enter only moves
        the caret to field 4 (EIN); nothing is typed into a box."""
        out = {"ok": True}
        if self.dry_run or self.w32 is None:
            return {"ok": False, "reason": "no win32 popup connection (run on the VM after connect)"}
        try:
            import time
            f0 = self._focused_hwnd()
            out["focus_before"] = {"hwndFocus": f0[0], "hwndCaret": f0[1], "rcCaret": f0[2],
                                   "caret_blinking": bool(f0[3] & _GUI_CARETBLINKING),
                                   "main_hwnd": self.main_hwnd}
            popup = self._find_headsdown_popup(timeout=0.5)
            out["popup_present_initially"] = popup is not None
            if popup is None:
                out["ensure_open"] = self._ensure_popup_open(timeout=2.5)
                popup = self._find_headsdown_popup(timeout=1.0)
            if popup is None:
                out["ok"] = False
                out["reason"] = "popup not open — click a Drake field first, then re-run"
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
            try:
                edit.set_edit_text("4")
                out["set_edit_text_readback"] = _safe_read_edit(eh)
            except Exception as e:
                out["set_edit_text_error"] = str(e)
            try:
                self._keys("{ENTER}")
            except Exception as e:
                out["enter_error"] = str(e)
            time.sleep(0.4)
            out["popup_still_open_after_enter"] = self._find_headsdown_popup(timeout=0.5) is not None
            f1 = self._focused_hwnd()
            out["focus_after_enter"] = {"hwndFocus": f1[0], "hwndCaret": f1[1], "rcCaret": f1[2],
                                        "caret_blinking": bool(f1[3] & _GUI_CARETBLINKING)}
            out["model"] = ("persistent-command-bar (value -> popup)" if out["popup_still_open_after_enter"]
                            else "per-jump dialog (value -> canvas)")
            out["unexpected_dialog"] = self._detect_unexpected_dialog()
        except Exception as e:
            out["ok"] = False
            out["reason"] = str(e)
        return out

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
