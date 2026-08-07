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
import re as _re
from types import SimpleNamespace as _SimpleNamespace
from typing import Any, Optional

# Dialogs a run may close by itself, by clicking a button IDENTIFIED BY NAME. Override with
# navigation.auto_dismiss in binding.json; set it to [] to make every dialog halt.
#
# Only one entry, and it earns its place: Drake raises this warning when the screen is left
# with e-file-required boxes still empty, which is the normal state of a W-2 halfway through
# being keyed. Clicking OK is the answer that KEEPS US ON THE SCREEN — the message itself
# says "To enter this data now, click OK" — so the remaining field numbers still address the
# W-2. The other answer leaves the screen, and every field after it would land somewhere
# else. If this build's buttons are named differently, change "button" here rather than
# letting the driver guess.
_DEFAULT_AUTO_DISMISS = [
    {"match": r"must contain data if you are planning to e-?file",
     "button": "OK",
     "why": "Drake's e-file completeness warning — OK stays on this screen to finish keying it."},
]

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

try:
    # Pillow ALONE — no Tesseract. The tick on a checkbox is a coloured square, not text,
    # so it is read by looking at the pixels rather than by OCR. Tracked separately because
    # a machine with Pillow but no Tesseract binary can still verify a checkbox.
    from PIL import Image as _PILImage  # noqa: F401
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _say(msg: str) -> None:
    """print(), but a console that cannot spell the character does not kill the run.

    Every warning this driver prints goes through here. On Windows a redirected stdout
    defaults to the legacy ANSI codepage, and printing '⚠' into cp1252 raises
    UnicodeEncodeError — which headsdown_type's outer `except Exception` then reports as a
    HALT ("'charmap' codec can't encode character"). A field that entered perfectly would
    stop the batch because of a symbol in a message about it. Redirecting a run to a log
    file is the normal thing to do with an audit artifact, so this must not be fragile."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


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
        # Seconds between injected keystrokes. Drake is a DOS-heritage app that can drop
        # keys sent too fast, so this is deliberately conservative — but it is also charged
        # on every character of every value AND on the 16-backspace clear, twice a field.
        # Tunable per VM because the right value is a property of the machine, and because
        # a value too low cannot pass silently: a dropped character fails the settle gate
        # and HALTS rather than committing a short value.
        self.key_pause = float(self.nav.get("key_pause", key_pause))
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
        self._warned_surface_blind = False
        self._warned_recycled = False
        # Has the caret re-arm already been spent this run? Once, deliberately — see
        # _rearm_caret. A state one re-arm does not fix is a state a human should see.
        self._caret_rearmed = False
        # Screen y below which the data-entry FORM starts. Above it are Drake's toolbar and
        # tab strip, which have Edits of their own; the caret belongs on a form box.
        self._canvas_form_top = int(self.nav.get("canvas_form_top", 150))
        # Has this build's heads-down popup been OBSERVED to own no child windows? None
        # until the first full-budget resolution answers it. See _resolve_popup_edit.
        self._popup_painted = None
        self._last_ocr_error = None   # why the screen-reading channel came back empty
        # How the heads-down popup is READ when it owns no child window (CONFIRMED on Drake
        # 2025: EnumChildWindows returns [] — Drake paints the box itself). Order matters:
        # UIA is exact if Drake exposes anything, OCR is approximate but cannot be refused.
        # Set to [] to force the run to prove nothing can be read and stop.
        self.popup_read_channels = list(self.nav.get("headsdown_read_channels", ["uia", "ocr"]))
        # CHECKBOX fields (Box 13) have their own value stage: Drake puts a real checkbox
        # widget on the popup instead of a text box, so the tick is read rather than the
        # text. "uia" is the Toggle pattern on the widget; "pixel" is the glyph on screen —
        # the channel that cannot be taken away, and the fallback if this build turns out
        # not to expose the checkbox to accessibility.
        self.checkbox_channels = list(self.nav.get("headsdown_checkbox_channels",
                                                   ["uia", "pixel"]))
        # Tokens tried, IN ORDER, until the tick actually flips — each one verified before
        # the next is sent, so a token that toggles rather than sets cannot leave the box
        # in the wrong state unnoticed. 'X' is Drake's classic token; '1' and Space are the
        # documented alternatives on other builds. Sent as pywinauto key specs, so
        # "{SPACE}" works — these are configuration, never extracted data.
        self.checkbox_tokens = [str(t) for t in (
            self.nav.get("headsdown_checkbox_tokens")
            or [self.nav.get("headsdown_checkbox_true", "X"), "1", "{SPACE}"])]
        self._last_tick_error = None      # why the screen tick-read came back empty
        self._last_checkbox_note = None   # e.g. more than one checkbox on the popup
        # How long to wait for Drake to move from the field-number prompt to the value
        # prompt. This is a BUSY budget, not a refusal test: committing a field can fire
        # Drake's employer-database lookup and auto-fill, which blocks its UI thread, and
        # the jump lands whenever that finishes. At 2.5s the live run of 2026-08-04 called
        # field 14 refused and halted — while the screenshot taken moments later showed the
        # popup sitting on field 14's value box, i.e. the jump had happened, just late.
        # Waiting longer costs nothing on a healthy field (the loop returns as soon as the
        # prompt moves) and only slows down a field that was genuinely declined.
        self.jump_timeout = float(self.nav.get("headsdown_jump_timeout", 8.0))
        # Dialogs the run may close BY ITSELF, each by clicking a NAMED button. Everything
        # else still halts for a human. Keep this list short and specific: an entry here is
        # permission to answer a question about a tax return without being asked.
        self.auto_dismiss = list(self.nav.get("auto_dismiss", _DEFAULT_AUTO_DISMISS))
        self._popup_hwnd = None  # last resolved popup handle (exact; see _find_popup_hwnd)
        # Last popup-edit resolution ({hwnd, class_name, how, children}) — kept so a halt
        # dump can report the popup's real control tree instead of just "timed out".
        self._last_edit_info = None
        # Structural dialog-gate state (see _detect_unexpected_dialog): windows that already
        # exist at attach are baseline furniture (e.g. the 'Drake Software Chat' overlay) and
        # can never halt a run by existing; windows classified benign mid-run are remembered
        # so each is logged once, not re-litigated per field.
        self.pid = None
        self._baseline_hwnds: set = set()
        self._benign_hwnds: set = set()
        # Was Drake's main frame ALREADY disabled when we attached? See _snapshot_baseline.
        self._baseline_main_disabled = False
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
            _say(f"  · app_title_re only matched mid-title — connected by process id "
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
                _say("WARNING: Drake appears to run ELEVATED but this agent is NOT — Windows "
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
            _say(f"  · the heads-down popup (hwnd={hwnd}) is NOT owned by the Drake process "
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

    # -- reading a popup Drake paints itself ---------------------------------

    def _read_popup_uia(self, popup_hwnd) -> Optional[str]:
        """The popup's text via UI Automation, addressed by HANDLE.

        Worth trying even though Drake's grid exposes nothing: UIA bridges MSAA/IAccessible,
        so an owner-drawn control with any accessibility support surfaces here even with no
        child HWND. Returns None when the channel is unavailable or silent — never ''.
        '' is a claim ("the box is empty") and this must not make claims it cannot support."""
        if self.app is None:
            return None
        try:
            spec = self.app.window(handle=int(popup_hwnd))
            parts = []
            for el in spec.descendants():
                try:
                    t = (el.window_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    parts.append(t)
                for getter in ("get_value", "legacy_properties"):
                    try:
                        v = getattr(el, getter)()
                    except Exception:
                        continue
                    if isinstance(v, dict):
                        v = v.get("Value")
                    v = (str(v).strip() if v is not None else "")
                    if v and v not in parts:
                        parts.append(v)
            return " ".join(parts) or None
        except Exception:
            return None

    def _read_popup_ocr(self, popup_hwnd) -> Optional[str]:
        """The popup's text read off the SCREEN — the channel that cannot be refused.

        Drake paints this box, so a picture of it is the same evidence a human has. The
        popup is small, high-contrast and single-line-ish, which is the case OCR is most
        reliable on — unlike the dense grid behind it. Whole-window grab (psm 6): the caller
        looks for a token, not an exact string."""
        if pytesseract is None:
            self._last_ocr_error = ("pytesseract/Pillow not installed — pip install "
                                    "pytesseract pillow")
            return None
        try:
            from PIL import ImageGrab
            if self.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
            # A screen grab reads whatever PIXELS are there, so it is only evidence about
            # the popup while the popup is what is on top. If something else has come
            # forward, the crop shows that window instead — and a token found in it would
            # be read as our own keystroke. No foreground, no reading.
            if int(_keyboard_target_info().get("root") or 0) != int(popup_hwnd):
                self._last_ocr_error = "the popup is not the foreground window — grab skipped"
                return None
            r = _wintypes.RECT()
            _u32().GetWindowRect(int(popup_hwnd), _ctypes.byref(r))
            if r.right <= r.left or r.bottom <= r.top:
                return None
            box = (r.left, r.top, r.right, r.bottom)
            try:
                # all_screens: GetWindowRect is in VIRTUAL desktop coordinates, so a popup on
                # a second monitor has an origin outside the primary screen — without this
                # the grab silently returns the wrong region (or a negative-origin crop).
                img = ImageGrab.grab(bbox=box, all_screens=True)
            except TypeError:      # Pillow < 9.2 has no all_screens
                img = ImageGrab.grab(bbox=box)
            if img.width < 8 or img.height < 8:
                return None
            # 3x upscale + greyscale: Drake's popup font is small, and Tesseract's accuracy
            # falls off a cliff below ~20px of x-height.
            img = img.convert("L").resize((img.width * 3, img.height * 3))
            txt = (pytesseract.image_to_string(img, config="--psm 6") or "").strip()
            self._last_ocr_error = None
            return " ".join(txt.split()) or None
        except Exception as e:
            # Kept, not swallowed: the Python package installs cleanly while the Tesseract
            # BINARY is missing, and that failure ("tesseract is not installed or it's not
            # in your PATH") is the single most useful line the probe can print.
            self._last_ocr_error = f"{type(e).__name__}: {e}"
            return None

    def _read_popup_channels(self, popup_hwnd, edit_hwnd=None) -> dict:
        """{channel: text} for every channel that answered THIS INSTANT. Silent channels are
        absent from the dict — absence is no evidence, never a reading of ''.

        Kept per channel rather than joined into one string, because the two ways of being
        wrong pull in opposite directions. First-wins lets a channel that answers with
        something useless shadow one that can actually see the typed text. Joining does the
        reverse: the combined string is only as steady as the LEAST steady channel, so an
        OCR feed jittering by a character stops a perfectly clean UIA reading from ever
        converging — and the gates, which all require two consecutive equal readings, then
        time out on keystrokes that landed correctly. Judging each channel on its own
        evidence avoids both."""
        out = {}
        if edit_hwnd and int(edit_hwnd) != int(popup_hwnd):
            t = _read_edit_or_none(edit_hwnd)
            if t is not None:
                out["win32"] = t
                return out
        for name, fn in (("uia", self._read_popup_uia), ("ocr", self._read_popup_ocr)):
            if name not in self.popup_read_channels:
                continue
            t = fn(popup_hwnd)
            # A reading with no alphanumerics in it is not a reading. OCR fails far more
            # often by returning punctuation ('|_. -~') than by returning nothing, and such
            # a frame is non-empty enough to look like an answer while carrying no
            # information. Admitting it would clobber the good frame beside it — every gate
            # here needs two consecutive readings that agree, so one interloper between two
            # identical readings is enough to stall the lot.
            if t is not None and _norm_prompt(t):
                out[name] = t
        return out

    def _read_popup_surface(self, popup_hwnd, edit_hwnd=None):
        """(text, channel) — everything the popup is showing, for halt messages and for the
        prompt text callers display. (None, "none") means NO channel could read it, which
        callers must treat as no evidence rather than as agreement.

        The GATES do not use this: they compare channels individually — see
        _read_popup_channels."""
        chans = self._read_popup_channels(popup_hwnd, edit_hwnd)
        if not chans:
            return None, "none"
        return " ".join(chans.values()), "+".join(chans)

    def _surface_count(self, text, expected) -> int:
        """How many times this popup reading shows `expected` — as WHOLE TOKENS, in order.

        Substring matching is not enough: '5' is inside '52000', and on a surface read the
        prompt shares one string with whatever has been typed. Whole-token matching alone is
        not enough either — 'TEST EMPLOYER LLC' is three tokens, and Drake renders 52000 as
        '52,000', which is two. So both sides are reduced to a token list and the match is a
        RUN of consecutive tokens whose concatenation equals the expected concatenation.
        That accepts every cosmetic split OCR or Drake can introduce while still refusing a
        match that starts or ends mid-token."""
        hay, want = _tokens(text), _tokens(expected)
        if not hay or not want:
            return 0
        target = "".join(want)
        n, i, hits = len(hay), 0, 0
        while i < n:
            acc, j = "", i
            while j < n and len(acc) < len(target):
                acc += hay[j]
                j += 1
                if acc == target:
                    hits += 1
                    break
                if not target.startswith(acc):
                    break
            i = j if acc == target else i + 1
        return hits

    def _surface_shows(self, text, expected) -> bool:
        return self._surface_count(text, expected) > 0

    def _settle_surface(self, popup_hwnd, expected, baseline, *, timeout=3.5, poll=0.05):
        """Wait until the popup SHOWS `expected` and has stopped changing. (ok, text, channel).

        The surface equivalent of _settle_read, carrying the same drain proof: two consecutive
        identical readings mean every posted keystroke has already been consumed, so none can
        arrive after the check passes.

        The extra condition is `baseline` — what the popup showed BEFORE we typed. We require
        one MORE occurrence of the token than the baseline had, not merely its presence:
        Drake's own value prompt names the field ("Enter the value for field 1…"), so a
        presence test would pass on the prompt's own '1' when the value 'T' never landed, and
        would also refuse the legitimate entry of the value '1'. Counting distinguishes them.

        `baseline` is the per-channel dict from _clear_target, so each channel is compared
        against what IT showed before the keystroke — one channel is enough to prove the
        keystroke landed, and a channel that never sees anything simply never votes."""
        import time
        base = baseline if isinstance(baseline, dict) else {"*": baseline}
        deadline = time.time() + timeout
        prev, last, chan = {}, None, "none"
        while True:
            chans = self._read_popup_channels(popup_hwnd)
            for name, text in chans.items():
                last, chan = text, name
                # Stability is compared on the NORMALISED reading. A screen read of an
                # unchanged popup is not byte-identical twice running — spacing and
                # punctuation move — and demanding that would make every OCR build time out
                # on a keystroke that had in fact landed.
                cur = _norm_prompt(text)
                base_n = self._surface_count(base.get(name, base.get("*", "")), expected)
                if (prev.get(name) == cur
                        and self._surface_count(text, expected) > base_n):
                    return True, text, chan
                prev[name] = cur
            if time.time() >= deadline:
                return False, last, (chan if last is not None else "none")
            time.sleep(poll)

    # -- checkbox fields (Box 13) -------------------------------------------
    #
    # CONFIRMED on Drake 2025 (live run 2026-08-03, field 47): a checkbox field's value
    # stage is NOT a text box. The popup keeps its field-number box and puts a real
    # CHECKBOX widget beside it, captioned with the field's name ("Retirement plan"), and
    # the token flips the tick. So there is nothing for the text gates to count: the tick
    # is a GLYPH, not a character, and _settle_surface — which requires the typed token to
    # appear in the popup's text one more time than before — can never be satisfied. That
    # is exactly what halted the first 20-field run: the X had landed, the box was ticked
    # on screen, and the driver correctly refused to commit something it could not read.
    #
    # The fix is a different CHANNEL, not a weaker gate: read the tick itself.

    def _read_popup_checkbox_uia(self, popup_hwnd) -> Optional[list]:
        """Every checkbox the popup exposes to UI Automation: [{name, state, rect}].

        `state` is True/False, or None when the element is there but will not say. The
        return is None when the CHANNEL could not answer at all (no UIA connection, the
        walk blew up) and [] when it walked the popup and there is genuinely no checkbox
        on it. Those two are not the same claim and must not be collapsed — [] is evidence
        that this is a text field, None is no evidence about anything.

        Worth expecting to work here even though Drake's grid exposes nothing: the popup is
        a WPF window (class HwndWrapper[DrakeTax2025;;…]) that owns no child HWNDs, and WPF
        controls are UIA elements rather than windows — which is why the popup reads through
        UIA at all. A WPF CheckBox carries the Toggle pattern, so its tick is readable even
        though nothing about it is a window."""
        if self.app is None:
            return None
        try:
            spec = self.app.window(handle=int(popup_hwnd))
            try:
                els = spec.descendants(control_type="CheckBox")
            except Exception:
                els = []
            if not els:
                # NEVER take an empty filtered result as "there is no checkbox".
                # UIAElementInfo._get_elements catches COMError and returns [] (pywinauto
                # 0.6.9), so a channel failure is indistinguishable from an answer — and
                # this one's answer is load-bearing: [] is what the drift guard reads as
                # "this is a text field". Confirm it with the unfiltered walk, which is a
                # different code path, before believing it. The popup holds a handful of
                # elements, so the second walk costs nothing worth saving.
                els = [e for e in spec.descendants() if _looks_like_checkbox(e)]
            out = []
            for el in els:
                out.append({"name": _element_name(el),
                            "state": _element_toggle_state(el),
                            "rect": _element_rect(el)})
            return out
        except Exception:
            return None

    def _read_popup_checkbox_pixels(self, popup_hwnd) -> Optional[bool]:
        """Is a TICKED checkbox glyph showing on the popup? True / None — never False.

        The screen channel for a tick, and deliberately one-sided. A ticked WPF checkbox is
        a solid accent-coloured square (measured on this build: a compact 14x14 blob of
        RGB 0,103,192 filling ~92% of its bounding box); nothing else the popup draws comes
        close. So a blob like that IS a tick.

        The absence of one is NOT a reading of "unticked", because an unticked checkbox and
        a plain text box are pixel-identical to this test — both simply have no blue square.
        Returning False there would let "this is a text field" masquerade as "the checkbox
        is clear", so it returns None: no evidence. Positive evidence only is enough to run
        on, because the only state this driver ever needs to PROVE is the ticked one."""
        if not _PIL_OK:
            self._last_tick_error = "Pillow not installed — pip install pillow"
            return None
        try:
            from PIL import ImageGrab
            # Same rule as the OCR channel: a screen grab is only evidence about the popup
            # while the popup is what is on top of it.
            if int(_keyboard_target_info().get("root") or 0) != int(popup_hwnd):
                self._last_tick_error = "the popup is not the foreground window — grab skipped"
                return None
            r = _wintypes.RECT()
            _u32().GetWindowRect(int(popup_hwnd), _ctypes.byref(r))
            if r.right <= r.left or r.bottom <= r.top:
                return None
            try:
                img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
            except TypeError:      # Pillow < 9.2 has no all_screens
                img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
            if img.width < 8 or img.height < 8:
                return None
            self._last_tick_error = None
            return True if _find_tick_glyph(img.convert("RGB")) else None
        except Exception as e:
            self._last_tick_error = f"{type(e).__name__}: {e}"
            return None

    def _checkbox_hwnd(self, hwnd) -> int:
        """The window the tick is drawn on — the POPUP, not the typing box.

        On the confirmed build they are one handle: Drake paints the box onto the dialog, so
        the popup IS the edit. On a build that has a real child Edit they are not, and the
        checkbox is that edit's SIBLING — walking the edit's own descendants would find
        nothing and report a perfectly readable tick as unreadable."""
        try:
            return int(self._popup_hwnd or hwnd)
        except Exception:
            return int(hwnd)

    def _read_checkbox_state(self, popup_hwnd) -> dict:
        """{channel: True/False} for every channel with a DEFINITE answer this instant.

        A channel that cannot see the tick is ABSENT from the dict, exactly as in
        _read_popup_channels — silence is never a vote. Two checkboxes on one popup would
        make "the state" meaningless, so that reports nothing and leaves a note for the
        halt message instead of picking one."""
        popup_hwnd = self._checkbox_hwnd(popup_hwnd)
        out = {}
        if "uia" in self.checkbox_channels:
            els = self._read_popup_checkbox_uia(popup_hwnd)
            known = [e for e in (els or []) if e.get("state") is not None]
            if len(known) == 1:
                out["uia"] = bool(known[0]["state"])
            elif len(known) > 1:
                self._last_checkbox_note = (
                    f"the popup exposes {len(known)} checkboxes "
                    f"({', '.join(repr(e.get('name')) for e in known)}) — which one is the "
                    f"field is not decidable, so no tick state was read")
        if "pixel" in self.checkbox_channels:
            t = self._read_popup_checkbox_pixels(popup_hwnd)
            if t is not None:
                out["pixel"] = bool(t)
        return out

    def _popup_checkbox_present(self, popup_hwnd, *, screen: bool = True) -> Optional[bool]:
        """Is the popup showing a checkbox? True / False / None (cannot tell).

        Only UIA can say NO — see _read_popup_checkbox_pixels for why the screen channel
        can only ever say yes. Used for the build-drift guard (Drake showing a checkbox for
        a field the map calls money means the field numbers have moved) and for messages;
        the entry gate does not rest on it, because the tick evidence is what actually has
        to be true before the Enter.

        `screen=False` asks UIA only. That is what entry uses, on every field of every run:
        the screen half costs a grab and a scan, and its unique contribution — "a tick is
        visible" — cannot change the routing of a field the map already calls a checkbox."""
        popup_hwnd = self._checkbox_hwnd(popup_hwnd)
        els = self._read_popup_checkbox_uia(popup_hwnd)
        if els:
            return True
        if screen and self._read_popup_checkbox_pixels(popup_hwnd):
            return True
        return False if els == [] else None

    def _settle_checkbox(self, popup_hwnd, desired=None, *, timeout: float = 1.5,
                         poll: float = 0.05):
        """Wait until the tick has STOPPED CHANGING. (ok, state, channel).

        `desired=None` converges on whatever state the box is in — that is how the arrival
        state is read, and it is what makes "it is already ticked, type nothing" possible.
        `desired=True/False` waits for that specific state, which is what a keystroke has
        to produce before it may be committed.

        Same drain proof as every other gate here: two consecutive readings that agree mean
        the keystroke has already been consumed, so none can arrive after the check passes.
        Two extra rules, both learned on the text path:
          • channels that DISAGREE are not a reading — the pair is discarded rather than
            one of them being picked, so a stale screen frame can never outvote UIA;
          • a channel with nothing to say is skipped, never recorded as a state. No
            evidence is not evidence."""
        import time
        deadline = time.time() + timeout
        prev, chan = None, "none"
        while True:
            st = self._read_checkbox_state(popup_hwnd)
            vals = set(st.values())
            if len(vals) == 1:
                cur = vals.pop()
                chan = "+".join(st)
                if prev is not None and prev == cur and (desired is None or cur == desired):
                    return True, cur, chan
                prev = cur
            elif len(vals) > 1:
                prev, chan = None, "+".join(st)
            if time.time() >= deadline:
                return False, prev, chan
            time.sleep(poll)

    def _checkbox_halt_reason(self, fn, desired, state, chan, tail) -> str:
        want = "ticked" if desired else "clear"
        note = f" ({self._last_checkbox_note})" if self._last_checkbox_note else ""
        if chan in ("", "none") or state is None:
            return (f"NOTHING can read the tick on field {fn}'s checkbox{note} — UI Automation "
                    f"exposes no checkbox on this popup and no tick glyph is on screen"
                    f"{'; ' + self._last_tick_error if self._last_tick_error else ''}. "
                    f"{tail} Run `agent.py probe-checkbox --field {fn}` to see what the popup "
                    f"really exposes; if UIA is silent on this build, set "
                    f"navigation.headsdown_checkbox_channels to [\"pixel\"] so the tick is "
                    f"confirmed off the screen instead.")
        shown = "ticked" if state else "clear"
        return (f"field {fn}'s checkbox still reads {shown}, not {want} (via {chan}){note}. {tail}")

    def _surface_halt_reason(self, what, got, chan, tail) -> str:
        if chan == "none" or got is None:
            return (f"NOTHING can read the heads-down popup on this build — it owns no child "
                    f"window (Drake paints it), UI Automation returned nothing, and OCR said: "
                    f"{self._last_ocr_error or 'no answer'}. {what} was typed but could not be "
                    f"verified, so the driver {tail}. Install Tesseract (and set "
                    f"navigation.tesseract_cmd if it is not on PATH) to enable the "
                    f"screen-reading channel — it is the only read-back this build allows.")
        return (f"the popup never showed {what} (last read via {chan}: {got!r}) — {tail}")

    def _clear_target(self, edit) -> dict:
        """Empty the popup's box before typing, and return what it reads as afterwards.

        {"ok": True, "baseline": <text>} or {"ok": False, "reason": …}. Residue is not
        cosmetic: a digit left in the box PREFIXES the next number, turning field 23 into
        field 223 — a different box, silently.

        A real Edit is cleared with WM_SETTEXT (atomic) and then proven empty. A painted
        surface has nothing to set, so it is cleared with Backspaces and its remaining text
        becomes the baseline the settle check counts against."""
        if not edit.surface:
            try:
                edit.set_edit_text("")   # EM_REPLACESEL, atomic
            except Exception:
                pass
            stale = _read_edit_or_none(edit.handle)
            if stale:
                return {"ok": False,
                        "reason": f"popup edit still holds {stale!r} before typing — refusing "
                                  f"to type onto residue"}
            return {"ok": True, "baseline": {}, "baseline_text": ""}
        # Painted box: no WM_SETTEXT target. Backspace is the only clear, and it is safe on
        # an empty box. Deliberately not Ctrl+A/Delete — Ctrl chords are toxic on this app.
        self._keys("{BACKSPACE 16}")
        import time
        time.sleep(0.06)
        chans = self._read_popup_channels(edit.handle)
        text = " ".join(chans.values()) if chans else None
        chan = "+".join(chans) if chans else "none"
        if text is None and chan == "none" and not self._warned_surface_blind:
            self._warned_surface_blind = True
            _say(f"  ⚠ the heads-down popup owns no child window and NOTHING can read it "
                  f"(UIA silent; OCR: {self._last_ocr_error or 'no answer'}). Entry cannot "
                  f"verify a keystroke before committing it, so it will halt rather than "
                  f"type blind.")
        # Per channel: the settle check compares each channel against what THAT channel
        # showed here, so a channel that reads the prompt and one that reads only the typed
        # text are both usable, and neither has to agree with the other.
        return {"ok": True, "baseline": chans, "baseline_text": text or ""}

    def _popup_edit_children(self, popup_hwnd, timeout: float = 2.0) -> list:
        """The popup's child windows, polled until it has some.

        Polled because a dialog's controls are created as it initialises: asking the instant
        the window appears can legitimately see an empty tree. Returns [] if it stays
        childless for the whole timeout — which is itself the finding, not an error."""
        import time
        deadline = time.time() + max(0.0, float(timeout))
        kids = []
        while True:
            try:
                kids = _enum_child_summaries(int(popup_hwnd), cap=32)
            except Exception:
                kids = []
            if kids or time.time() >= deadline:
                return kids
            time.sleep(0.05)

    def _resolve_popup_edit(self, popup_hwnd, timeout: float = 2.0) -> dict:
        """Identify the popup's typing box by STRUCTURE. {hwnd, class_name, how, children}.

        This used to be `popup.child_window(class_name="Edit")`, and that is what halted the
        live run at the first field with "popup edit not ready / no handle: timed out".
        pywinauto's class_name= criterion is EXACT equality, so it finds nothing unless the
        toolkit happens to name its control exactly "Edit" — and its only regex variant,
        class_name_re=, is the same anchored re.match that hid the popup window itself.
        Enumerating with ctypes and ranking the result cannot fail that way, and when it does
        fail it reports the class names it actually saw."""
        # WAIT ONCE, NOT EVERY FIELD. The poll exists because a dialog creates its controls
        # as it initialises, so asking the instant it appears can legitimately see an empty
        # tree — but on the confirmed Drake shape the tree is empty FOREVER (Drake paints the
        # box), so the full budget was being burned on every resolution, twice per field.
        # Measured live: 2s each, ~4s of the 8.2s a field was taking.
        #
        # So the first resolution pays the full budget and LEARNS the shape; afterwards a
        # popup already known to be painted gets a short probe. The enumeration itself still
        # runs every time — if children ever do appear they are still found and used — this
        # only stops re-proving a negative that was established once.
        budget = 0.0 if self._popup_painted else timeout
        kids = self._popup_edit_children(popup_hwnd, timeout=budget)
        if kids or budget == timeout:
            self._popup_painted = not kids
        try:
            focused = self._focused_hwnd()[0]
        except Exception:
            focused = None
        hwnd, how = _rank_popup_edit(kids, preferred_class=self.popup_edit_class,
                                     focused_hwnd=focused)
        # CONFIRMED on Drake 2025: the heads-down popup owns NO child windows at all
        # (EnumChildWindows returns []) — Drake paints the box onto the dialog's own canvas,
        # exactly as it does on the data-entry grid. The window that HOLDS THE KEYBOARD is
        # then the popup itself, and that is the only surface keystrokes can go to. Accepting
        # it requires that proof, not an assumption: if something else owns focus, the keys
        # would land there instead and we must not type.
        surface = False
        if hwnd is None and focused is not None and int(focused) == int(popup_hwnd):
            hwnd, how, surface = int(popup_hwnd), "the popup itself (it owns no child windows)", True
        cls = next((c.get("class_name") for c in kids
                    if hwnd is not None and int(c["hwnd"]) == hwnd), None)
        return {"hwnd": hwnd, "class_name": cls, "how": how if hwnd is not None else None,
                "why": None if hwnd is not None else how,
                "surface": surface,
                "popup_hwnd": int(popup_hwnd), "children": kids,
                "focused_hwnd": focused}

    def _popup_edit(self, popup):
        """The popup's typing box as something we can focus, clear and read.

        Raises PopupEditNotFound — carrying the popup's full child topology — rather than
        returning a specification that will time out three lines later with nothing to
        show for it. Every call site already halts on the exception."""
        hwnd = self._popup_hwnd
        if hwnd is None:
            try:
                hwnd = int(popup.handle)
            except Exception as e:
                raise PopupEditNotFound({"popup_hwnd": None, "children": [],
                                         "why": f"the popup has no resolvable handle ({e})"})
        info = self._resolve_popup_edit(hwnd)
        self._last_edit_info = info
        if info["hwnd"] is None:
            raise PopupEditNotFound(info)
        if info.get("surface"):
            # No child window, so no WM_GETTEXT target: reading this box means reading the
            # popup as a picture/accessibility tree, which _read_popup_surface does.
            try:
                w = self.w32.window(handle=info["hwnd"]).wrapper_object()
            except Exception:
                w = None
            return _EditTarget(info["hwnd"], None, info["how"], wrapper=w, surface=True)
        wrapper = None
        try:
            # By handle: exact, and immune to both the exact-class-name and the anchored
            # -title_re traps. This wrapper is used only for set_focus/set_edit_text.
            wrapper = self.w32.window(handle=info["hwnd"]).wrapper_object()
        except Exception:
            wrapper = None
        return _EditTarget(info["hwnd"], info["class_name"], info["how"], wrapper=wrapper)

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
        wins, enabled = snap
        self._baseline_hwnds = {int(w["hwnd"]) for w in wins}
        # The main frame's DISABLED bit is furniture too when it is already set at attach.
        # Drake nests its screens: opening a return's data-entry screen creates a new
        # top-level window and puts WS_DISABLED on the frames behind it. That is Drake at
        # rest, not a modal that arrived to block us — and it is indistinguishable from one
        # by looking at the bit alone, which is what stopped a live run dead (2026-08-04:
        # "unexpected dialog before entry: main-disabled", nothing typed, no dialog on
        # screen, every window in the process already in the baseline).
        #
        # A modal that appears LATER still disables the frame and still halts, because the
        # test is "did this change since we attached", not "is the frame disabled".
        self._baseline_main_disabled = (enabled is False)
        if self._baseline_main_disabled:
            _say("  · Drake's main frame is already disabled at attach (its data-entry "
                 "screen is a nested window) — baseline, not a modal. A modal appearing "
                 "later still halts.")
        extras = [w for w in wins
                  if int(w["hwnd"]) != int(self.main_hwnd or 0)
                  and w.get("visible")
                  and not _re.search(self.popup_title_re, w.get("title") or "")]
        if extras:
            names = ", ".join(repr(w.get("title") or w.get("class_name")) for w in extras[:6])
            _say(f"  · {len(extras)} other window(s) in the Drake process at attach — "
                  f"benign baseline, ignored unless one blocks input: {names}")

    def rebaseline(self, why: str = "") -> None:
        """Re-take the window baseline after the AGENT itself changed Drake's screen.

        The dialog gate's question is "did this change since we attached", which is exactly
        right while a human does the navigating and the agent only types. The moment the
        agent opens returns for itself, that question has a stale answer: opening a return
        creates a new top-level window and disables the frames behind it — structurally
        identical to a modal arriving. It halted the first live navigate-and-fill run on
        field 1, reporting a modal, with nothing typed and no dialog anywhere on screen.

        Called ONLY after navigation has proved from Drake's OWN window title that the
        right return is open. That is not a weakened gate: a window the agent opened on
        purpose and then verified is the definition of expected, and anything appearing
        after this point still halts exactly as before.
        """
        self._snapshot_baseline()
        if why:
            _say(f"  · window baseline re-taken after {why} — the return's own window is "
                 f"now Drake at rest, not a blocker")

    def _note_benign(self, w) -> None:
        line = (f"ignoring benign window {w.get('title')!r} "
                f"(class={w.get('class_name')}, hwnd={w.get('hwnd')}) — non-modal, not a dialog")
        self.benign_notes.append(line)
        _say(f"  · {line}")

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
                  baseline=self._baseline_hwnds, benign_seen=self._benign_hwnds,
                  main_disabled_at_attach=self._baseline_main_disabled)
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

    def _dismiss_rule_for(self, dlg) -> Optional[dict]:
        """The auto-dismiss rule matching this dialog, or None. Matching is on the dialog's
        TEXT, never its title alone — 'Drake 2025 - Data Entry' is the caption of several
        different dialogs, and they do not want the same answer."""
        blob = " ".join(str(dlg.get(k) or "") for k in ("title", "text"))
        import re as _r
        for rule in self.auto_dismiss:
            try:
                if _r.search(rule.get("match", r"(?!)"), blob, _r.I):
                    return rule
            except Exception:
                continue
        return None

    def _dismiss_dialog(self, dlg, rule) -> dict:
        """Close a KNOWN dialog by clicking a button we identified by name.

        Deliberately not the requested blind Enter/Space. Enter presses whatever the dialog's
        DEFAULT button happens to be — unseen, and different per dialog. On this particular
        warning the two answers are 'go back and enter the data' and 'leave the screen
        anyway', and the second one silently moves Drake off the W-2 mid-batch, after which
        every remaining field number addresses a different screen. So: find the named button,
        click that button, and prove the dialog is gone. No button, no keystroke."""
        h = int(dlg.get("handle") or 0)
        want = str(rule.get("button") or "OK")
        if not h:
            return {"ok": False, "reason": "the dialog has no window handle to click into"}
        want_n = _norm_prompt(want)
        found = []
        for c in _enum_child_summaries(h, cap=32):
            cls, txt = (c.get("class_name") or ""), (c.get("text") or "")
            if "button" not in cls.lower():
                continue
            found.append(txt)
            if _norm_prompt(txt.replace("&", "")) == want_n:
                try:
                    self.w32.window(handle=int(c["hwnd"])).wrapper_object().click()
                except Exception as e:
                    return {"ok": False, "reason": f"clicking {txt!r} failed: {e}"}
                import time
                time.sleep(0.35)
                still = self._detect_unexpected_dialog()
                if still and int(still.get("handle") or 0) == h:
                    return {"ok": False, "reason": f"clicked {txt!r} but the dialog is still up"}
                self.benign_notes.append(
                    f"auto-dismissed {dlg.get('title')!r} by clicking {txt!r} ({rule.get('why', '')})")
                _say(f"  ⚠ auto-dismissed {dlg.get('title')!r} — clicked {txt!r}. "
                      f"{rule.get('why', '')}")
                return {"ok": True, "clicked": txt}
        return {"ok": False,
                "reason": f"no button named {want!r} on this dialog (buttons: {found or 'none found'}) "
                          f"— refusing to press Enter blind, because the default button is "
                          f"unknown and one of the answers leaves the data-entry screen"}

    def _clear_blocking_dialog(self):
        """Detect a blocker and auto-dismiss it if it is one we have a rule for.
        Returns the blocker still standing, or None if the way is clear."""
        dlg = self._detect_unexpected_dialog()
        if dlg is None:
            return None
        rule = self._dismiss_rule_for(dlg)
        if rule is None:
            return dlg
        res = self._dismiss_dialog(dlg, rule)
        if not res.get("ok"):
            dlg["dismiss_attempt"] = res.get("reason")
            return dlg
        return self._detect_unexpected_dialog()

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
            # Which child of the popup we would type into, and why — read-only. This is the
            # verdict that decides whether entry can run at all, so it belongs in the dump
            # rather than only in the halt line of a run that already stopped.
            popup_edit = None
            ph = self._popup_hwnd or next(
                (int(w["hwnd"]) for w in wins if w.get("role") == "heads-down-popup"), None)
            if ph:
                popup_edit = {k: v for k, v in self._resolve_popup_edit(ph, timeout=0.3).items()
                              if k != "children"}
            return {"ok": True, "takenAt": _now(), "pid": pid,
                    "main_hwnd": self.main_hwnd, "main_enabled": main_enabled,
                    "baseline_hwnds": sorted(int(x) for x in self._baseline_hwnds),
                    "keyboard_target": _keyboard_target_info(),
                    "popup_edit": popup_edit,
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
                # PRESENT IS NOT THE SAME AS ARMED. Confirmed live after the EIN commits
                # (2026-08-04): Drake's auto-fill finishes, the popup is on screen, and the
                # KEYBOARD is still held by the data-entry window. Typing then goes to the
                # canvas, so the run halted at the next field with "could not identify the
                # popup's text box … the keyboard is held by hwnd=…, not the popup".
                if not self._popup_holds_keyboard(popup):
                    cycled = self._recycle_popup(method=method)
                    if not cycled.get("ok"):
                        return cycled
                    popup = cycled["popup"]
                ready = self._popup_ready_for_number(popup)
                if not ready.get("ok"):
                    return ready
                return {"ok": True, "opened": i > 0, "attempts": i, "popup": popup}
            if self.w32 is None:
                return {"ok": False, "reason": "win32 popup connection unavailable (reconnect on the VM)"}
            # Clear a KNOWN dialog (the e-file completeness warning) by clicking its named
            # button; anything else still halts. Never fire a chord into a modal.
            bad = self._clear_blocking_dialog()
            if bad:
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
        # Every attempt fired into Drake's frame and nothing came up. "Drake is busy" is now
        # exhausted as an explanation; the remaining one is that no field holds the caret, so
        # Ctrl+N has been a no-op every time. Answer the question the old message could only
        # ask ("is a canvas field active?") instead of handing it back to the operator.
        rearmed = self._rearm_caret(method=method)
        if rearmed.get("ok"):
            return {"ok": True, "opened": True, "attempts": attempts, "popup": rearmed["popup"],
                    "rearmed": True}
        return {"ok": False,
                "reason": f"Ctrl+N did not open the heads-down popup after {attempts} attempts. "
                          f"{rearmed.get('reason', '')}".strip()}

    def _popup_holds_keyboard(self, popup, tries: int = 4) -> bool:
        """Does the heads-down popup actually own the keyboard? Retried briefly, because
        focus arrives a moment after the window does.

        "The popup is up" and "keys will land in it" are different claims, and only the
        second one matters. On the confirmed Drake shape the popup owns no child windows, so
        the popup itself must hold focus. On a build that HAS a real child Edit the keyboard
        belongs to that CHILD, and that is perfectly healthy — accepting only the popup's own
        handle there would call every healthy popup inert and recycle it on every field,
        which is the double-toggle cascade wearing a different hat."""
        import time
        h = self._popup_hwnd
        if h is None:
            try:
                h = int(popup.handle)
            except Exception:
                return False
        for _ in range(max(1, tries)):
            try:
                focused = int(self._focused_hwnd()[0])
                if focused == int(h):
                    return True
                try:
                    kids = _enum_child_summaries(int(h), cap=32)
                except Exception:
                    kids = []
                if any(int(k.get("hwnd") or 0) == focused for k in kids):
                    return True
            except Exception:
                pass
            time.sleep(0.12)
        return False

    def _recycle_popup(self, *, method: str = "scancode") -> dict:
        """Close a popup that is on screen but not holding the keyboard, then open a fresh
        one — Ctrl+N twice, VERIFIED at each step rather than fired blind.

        The founder found this by hand after an EIN commit: one Ctrl+N to drop the stale
        popup, a second to bring up a live one, and heads-down then behaves normally. The
        reason it is not the double-toggle bug this driver guards against is the state it
        starts from — a popup that is up but inert. A blind double-tap on a HEALTHY popup
        would still be a bug, which is why each toggle here is confirmed by looking at the
        window before the next one is sent: close must be observed before open is sent, and
        the new popup must be seen to hold the keyboard before it is handed back."""
        import time
        ok_scope, where = self._input_scope(allow_popup=True)
        if not ok_scope:
            return {"ok": False,
                    "reason": f"the heads-down popup is up but does not hold the keyboard, "
                              f"and the keyboard is in {where} — outside Drake entirely. "
                              f"Refusing to send Ctrl+N. Click a Drake field and re-run."}
        self.headsdown_toggle(method=method)          # 1) drop the inert popup
        gone = False
        for _ in range(12):
            if self._find_headsdown_popup(timeout=0.05) is None:
                gone = True
                break
            time.sleep(0.1)
        if not gone:
            # Ctrl+N could not close it because Ctrl+N is doing NOTHING — no caret. Esc
            # closes a popup without needing one, and Tab puts the caret back. Verified
            # end-to-end before this returns ok.
            return self._rearm_caret(method=method)
        self._popup_owned = False
        self.headsdown_toggle(method=method)          # 2) bring up a live one
        popup = self._find_headsdown_popup(timeout=2.0)
        if popup is None:
            return {"ok": False,
                    "reason": "closed the inert heads-down popup, but Ctrl+N did not bring a "
                              "new one back. Nothing was typed — click a Drake field and re-run."}
        if not self._popup_holds_keyboard(popup, tries=8):
            return {"ok": False,
                    "reason": "re-opened the heads-down popup and it STILL does not hold the "
                              "keyboard, so a keystroke would land on the canvas. Nothing was "
                              "typed. Click into a Drake field and re-run."}
        # WE opened this one, so its state is known: it is on the field-number prompt.
        self._popup_owned = True
        self._note_recycled()
        return {"ok": True, "popup": popup, "recycled": True}

    def _focus_canvas_field(self) -> bool:
        """Put the caret back on a real data-entry box, through the accessibility tree.

        NOT a coordinate click. The element is found in the canvas window's UIA tree — the
        same tree FORM CHECK already reads — and asked to take focus, so there is no screen
        position to get wrong, nothing to land on a button, and no dependence on the window
        being where it was last time. Taking focus does not alter a box's contents.

        Boxes above the form band are skipped: the toolbar has its own Edits, and while
        focusing one does happen to arm Ctrl+N on this build, a field inside the form is
        the thing we actually mean. Deterministic order (top, then left) so the same box is
        chosen every run and a failure is reproducible.
        """
        if self.dry_run or self.app is None:
            return False
        best = None
        for h in self.canvas_windows():
            try:
                els = self.app.window(handle=h).descendants()
            except Exception:
                continue
            for el in els:
                try:
                    if str(el.element_info.control_type or "") != "Edit":
                        continue
                    if not (el.is_enabled() and el.is_visible()):
                        continue
                    r = el.rectangle()
                except Exception:
                    continue
                if int(r.top) < self._canvas_form_top:
                    continue                     # toolbar / tab strip, not the form
                # The Data Entry MENU is titled 'Data Entry (...)' too, so canvas_windows()
                # accepts it — and the only Edit on it is the screen-search box at the
                # bottom. Arming the caret there would send field numbers into a search
                # field. Callers gate on nav_data_entry_window()['kind'] == 'form'; this
                # is the belt to that braces.
                try:
                    if str(el.element_info.automation_id or "") == "MenuScreenWindow_TextBoxSearch":
                        continue
                except Exception:
                    pass
                key = (int(r.top), int(r.left))
                if best is None or key < best[0]:
                    best = (key, el)
        if best is None:
            return False
        try:
            best[1].set_focus()
        except Exception:
            return False
        import time
        for _ in range(10):
            got = _element_has_keyboard_focus(best[1])
            if got:
                return True
            if got is None:
                # The toolkit will not answer. Fall back to the window-level fact — the
                # keyboard is on a data-entry window — which is weaker but still evidence.
                # The run must ALSO see the popup open and hold the keyboard before it
                # types, so a generous answer here cannot let a keystroke through.
                now = int(self._focused_hwnd()[0] or 0)
                if now and now in set(self.canvas_windows()):
                    return True
            time.sleep(0.08)
        return False

    def _rearm_caret(self, *, method: str = "scancode") -> dict:
        """Recover the state a FINISHED run leaves behind, in which Ctrl+N does nothing.

        Drake arms heads-down off an ACTIVE CARET, not window focus. With no box active,
        Ctrl+N is a SILENT no-op — no popup, no error, no sound. A completed run leaves
        exactly that: the caret gone, focus drifted onto a different Drake window, and the
        old popup still on screen and holding no keyboard. A second back-to-back entry
        cannot bootstrap itself out of it. Confirmed live 2026-08-05: run 1 wrote 78/78,
        run 2 halted on field 1 having typed nothing.

        MEASURED, after a first attempt built on guesswork failed three times running:
          - Esc does NOT reach that popup. It has no keyboard, so Esc goes to whatever does.
          - Tab does not reliably give a box the caret from this state either.
          - Focusing a canvas Edit through UIA DOES, and Ctrl+N then behaves normally.
          - Closing the stale popup drops focus to NOTHING (hwnd 0), so the caret has to be
            put back a second time before the popup can be re-opened. Missing that step is
            why the first version got as far as closing the popup and no further.

        Attempted ONCE per run: a state this does not fix is a state a human should see,
        and retrying only repeats a sequence against a screen we have already misread.

        This lowers no bar. The popup it produces still has to be found, still has to hold
        the keyboard, and still has to be on the field-number prompt before one character is
        typed — the same three proofs demanded of any other popup. It only reaches a state
        the run could otherwise reach only by asking a human to click.
        """
        import time
        if self._caret_rearmed:
            return {"ok": False,
                    "reason": "heads-down still will not arm after a caret re-arm was already "
                              "tried this run. Nothing was typed. Click into a Drake field and "
                              "re-run."}
        self._caret_rearmed = True
        stale = self._find_headsdown_popup(timeout=0.05) is not None
        if stale:
            # Ctrl+N cannot close it while nothing holds the caret — that is exactly why we
            # are here. Give the caret back FIRST, then the toggle is heard.
            if not self._focus_canvas_field():
                return {"ok": False,
                        "reason": "the heads-down popup is up, holds no keyboard, and no box on "
                                  "the data-entry form would take the caret — so nothing can "
                                  "reach it. Nothing was typed. Close it in Drake, click a "
                                  "field, and re-run."}
            ok_scope, where = self._input_scope(allow_popup=True)
            if not ok_scope:
                return {"ok": False,
                        "reason": f"heads-down will not arm and the keyboard is in {where} — "
                                  f"outside Drake entirely. Refusing to send Ctrl+N. Click a "
                                  f"Drake field and re-run."}
            self.headsdown_toggle(method=method)
            gone = False
            for _ in range(12):
                if self._find_headsdown_popup(timeout=0.05) is None:
                    gone = True
                    break
                time.sleep(0.1)
            if not gone:
                return {"ok": False,
                        "reason": "the heads-down popup is up, holds no keyboard, and would not "
                                  "close even once the caret was restored. Nothing was typed. "
                                  "Close it in Drake, click a field, and re-run."}
            self._popup_owned = False
        # Closing the popup drops focus to nothing, so the caret goes back a second time.
        # Unconditional: on the no-popup path this is the only place it happens at all.
        if not self._focus_canvas_field():
            return {"ok": False,
                    "reason": "heads-down will not arm and no box on the data-entry form would "
                              "take the caret — is a return's data-entry screen actually open? "
                              "Nothing was typed. Click a Drake field and re-run."}
        ok_scope, where = self._input_scope(allow_popup=False)
        if not ok_scope:
            return {"ok": False,
                    "reason": f"restored the caret, but the keyboard is in {where} — not Drake's "
                              f"frame. Refusing to send Ctrl+N. Click a Drake field and re-run."}
        self.headsdown_toggle(method=method)
        popup = self._find_headsdown_popup(timeout=2.0)
        if popup is None:
            return {"ok": False,
                    "reason": "restored the caret on a data-entry box, but Ctrl+N still did not "
                              "open the heads-down popup. Nothing was typed. Click a Drake field "
                              "and re-run."}
        if not self._popup_holds_keyboard(popup, tries=8):
            return {"ok": False,
                    "reason": "restored the caret and the heads-down popup opened, but it does "
                              "NOT hold the keyboard, so a keystroke would land on the canvas. "
                              "Nothing was typed. Click into a Drake field and re-run."}
        # WE opened this one, so its state is known: it is on the field-number prompt.
        self._popup_owned = True
        self._note_rearmed()
        return {"ok": True, "popup": popup, "rearmed": True}

    def _note_rearmed(self) -> None:
        if getattr(self, "_warned_rearmed", False):
            return
        self._warned_rearmed = True
        _say("  · heads-down would not arm (no active caret — the state a finished run leaves "
             "behind). Put the caret back on a data-entry box and re-opened the popup; entry "
             "continues.")

    def _note_recycled(self) -> None:
        if getattr(self, "_warned_recycled", False):
            return
        self._warned_recycled = True
        _say("  · the heads-down popup was up but inert (no keyboard) — closed and re-opened "
             "it. Expected right after Drake's employer auto-fill; entry continues.")

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
            prompt = self._stable_prompt(popup)
            import re as _re
            if prompt and not _re.search(self.number_prompt_re, prompt, _re.I):
                return {"ok": False,
                        "reason": f"the heads-down popup is armed for a VALUE, not a field "
                                  f"number (prompt reads {prompt[:80]!r}). Refusing to type a "
                                  f"field number into it — that would commit the number as the "
                                  f"previous field's value."}
            return {"ok": True}
        prompt = self._stable_prompt(popup)
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
            t = _popup_prompt_text(hwnd, edit_hwnd, self.popup_edit_class)
        except Exception:
            t = ""
        if t:
            return t
        # No child windows to harvest text from (the confirmed Drake 2025 shape). Fall back
        # to reading the popup as a whole. That reading INCLUDES anything typed, which is
        # correct here: every caller baselines this string and watches it change, and "the
        # number is still showing" is exactly the evidence that Drake refused it.
        if edit_hwnd is None or int(edit_hwnd) == int(hwnd):
            text, _chan = self._read_popup_surface(hwnd)
            return text or ""
        return ""

    def _stable_prompt(self, popup, edit_hwnd=None, *, timeout: float = 3.0,
                       poll: float = 0.05) -> str:
        """A prompt reading that has been seen TWICE — '' if it never settles.

        Every refusal check compares later readings against a baseline captured here, and a
        baseline is not exempt from the noise the comparisons are guarded against. Take one
        corrupted frame as the baseline and the logic inverts: every CLEAN frame afterwards
        differs from it, so a field Drake silently refused reads as accepted, and the value
        gets typed at the number prompt. That is not hypothetical — it is what the
        character-noise case caught in this driver.

        '' when it cannot converge, which callers already treat as "this build exposes no
        prompt" and degrade (on a painted popup: halt) rather than trusting noise.

        An UNREADABLE frame is skipped, not recorded as a reading of ''. This is what halted
        the first live run at field 1: right after the keystroke Drake is still repainting,
        the first read comes back with nothing, and treating that as a reading discarded the
        good reading either side of it — so a popup that was perfectly legible produced no
        baseline, and the field halted with 'could not read the popup at all'. No evidence
        is not evidence of emptiness.

        Per channel, so a jittery OCR feed cannot stop a clean UIA reading from settling."""
        import time
        deadline = time.time() + timeout
        prev, attempts = {}, 0
        while True:
            chans = self._popup_prompt_channels(popup, edit_hwnd)
            attempts += 1
            for name, t in chans.items():
                # Every reading here already carries words — _read_popup_channels drops the
                # blank and the punctuation-only ones, so nothing that reaches this loop can
                # displace a good reading.
                n = _norm_prompt(t)
                if prev.get(name) == n:
                    return t
                prev[name] = n
            # Give it a MINIMUM number of tries as well as a deadline: a UIA tree walk can
            # take most of a second by itself, so a purely wall-clock budget can expire
            # having barely looked.
            if attempts >= 3 and time.time() >= deadline:
                return ""
            time.sleep(poll)

    def _popup_prompt_channels(self, popup, edit_hwnd=None) -> dict:
        """{channel: text} for a popup WindowSpecification — the per-channel form of
        _popup_prompt. Child-window text (a real Static prompt) is reported under 'win32'."""
        hwnd = self._popup_hwnd
        if hwnd is None:
            try:
                hwnd = int(popup.handle)
            except Exception:
                return {}
        try:
            t = _popup_prompt_text(hwnd, edit_hwnd, self.popup_edit_class)
        except Exception:
            t = ""
        if t:
            return {"win32": t}
        if edit_hwnd is None or int(edit_hwnd) == int(hwnd):
            return self._read_popup_channels(hwnd)
        return {}

    def _note_prompt_blind(self) -> None:
        if self._warned_prompt_blind:
            return
        self._warned_prompt_blind = True
        _say("  ⚠ this build exposes NO readable prompt text on the heads-down popup, so a "
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
        base_norm, prev_norm = _norm_prompt(base_prompt), None
        while time.time() < deadline:
            bad = self._detect_unexpected_dialog()
            if bad:
                return ("error", bad)
            live = self._find_headsdown_popup(timeout=0.05)
            if live is None:
                return ("per-jump", None)
            if base_prompt:
                cur = self._popup_prompt(live, edit_hwnd)
                cur_norm = _norm_prompt(cur)
                # Normalised, and STABLE across two readings. On a screen-read channel the
                # raw text jitters by a character or two between frames, and a single
                # "it differs" would call a refusal an acceptance — the cascade this
                # function exists to prevent.
                if cur and cur_norm != base_norm and cur_norm == prev_norm:
                    return ("persistent", {"prompt": cur})
                # ONLY on a real reading. An unreadable frame is no evidence, and letting it
                # overwrite the previous good reading means two good readings never sit next
                # to each other — every jump would then time out and report the field as
                # refused when Drake had accepted it.
                if cur:
                    prev_norm = cur_norm
            elif not self._is_surface_hwnd(edit_hwnd):
                # Blind build: the best available signal is the edit clearing.
                cur_edit = _read_edit_or_none(edit_hwnd)
                if cur_edit is not None and cur_edit != number:
                    self._note_prompt_blind()
                    return ("persistent", {"prompt": ""})
            time.sleep(0.08)
        if base_prompt:
            return ("rejected", None)
        # No prompt text AND no child box: WM_GETTEXT on the popup returns its CAPTION, a
        # constant that has nothing to do with what Drake is asking — reading it as "the box
        # changed" would report every refusal as an acceptance. Unknown is the honest answer.
        if self._is_surface_hwnd(edit_hwnd):
            return ("unknown", None)
        return ("unknown", None) if _read_edit_or_none(edit_hwnd) is None else ("rejected", None)

    def _is_surface_hwnd(self, edit_hwnd) -> bool:
        """Is this 'edit' handle actually the popup itself (a box Drake paints)?"""
        try:
            return (self._popup_hwnd is not None
                    and int(edit_hwnd) == int(self._popup_hwnd))
        except Exception:
            return False

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
        last, prev_norm = "", None
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
                cur_norm = _norm_prompt(cur)
                if cur_norm == _norm_prompt(number_prompt):
                    return True, "popup returned to the field-number prompt"
                # Same stability requirement as _classify_after_jump: one differing frame
                # from a screen read is jitter, and calling that "Drake took the value"
                # feeds the next field's number in as this one's value.
                if value_prompt and cur_norm != _norm_prompt(value_prompt) and cur_norm == prev_norm:
                    return True, f"prompt moved on ({cur[:60]!r})"
                prev_norm = cur_norm
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

    def _enter_checkbox(self, popup, eh, fn, val, base_prompt) -> dict:
        """The VALUE stage for a checkbox field — state-driven, never keystroke-driven.

        Drake's checkbox value stage has no text box (CONFIRMED live on field 47, Box 13
        "Retirement plan"): the popup shows the tick box itself. So the question this has
        to answer is not "did my character appear" but "is the box in the state I want",
        and it is answered by reading the widget:

          1. converge on the ARRIVAL state — what the box shows before we touch it
          2. already correct? type NOTHING. A token sent at an already-ticked box is a
             coin flip between a no-op and silently clearing a human's tick.
          3. otherwise send a token and wait for the tick to actually FLIP, verified
             between tokens, so an escalation cannot double-toggle unnoticed
          4. re-prove the state immediately before the Enter
          5. prove Drake took it (the shared prompt-moved commit gate)

        Every exit before step 5 leaves Drake exactly as it was found: the popup is still
        up, nothing has been committed, and a human ticks the box by hand."""
        desired = _as_checkbox_desired(val)
        if desired is None:
            return {"ok": False, "halt": True,
                    "reason": f"field {fn} is a checkbox and {val!r} is not a yes-or-no "
                              f"value for one. Nothing was entered."}
        self._last_checkbox_note = None
        # The VALUE prompt, captured before anything is typed — step 5 compares against it.
        value_prompt = self._stable_prompt(popup, eh)
        ok0, arrival, chan0 = self._settle_checkbox(eh, timeout=1.5)
        if not ok0 and desired is False:
            # Unticking needs to know the box IS a checkbox and IS currently ticked, and
            # only the accessibility channel can establish that — the screen channel can
            # confirm a tick but never its absence (see _read_popup_checkbox_pixels).
            return {"ok": False, "halt": True,
                    "reason": self._checkbox_halt_reason(
                        fn, desired, arrival, chan0,
                        "Clearing a tick has to start from a state that was actually read, "
                        "and nothing read it, so nothing was typed.")}
        tokens_tried = []
        state, chan = arrival, chan0
        if not (ok0 and arrival == desired):
            for token in self.checkbox_tokens:
                tokens_tried.append(token)
                self._keys(token)
                ok, state, chan = self._settle_checkbox(eh, desired, timeout=1.5)
                if ok:
                    break
            else:
                return {"ok": False, "halt": True,
                        "reason": self._checkbox_halt_reason(
                            fn, desired, state, chan,
                            f"The tick was never SEEN in that state, so the Enter was not "
                            f"pressed and nothing was committed — the popup is still open on "
                            f"this field. Press Esc in Drake and tick this box by hand, or "
                            f"set navigation.headsdown_checkbox_tokens to the token this "
                            f"build takes (tried, in order: {tokens_tried}).")}
        # 4) Prove it once more, right before the irreversible Enter. The token loop's own
        #    check can be several hundred milliseconds old by now, and a checkbox that
        #    flipped back (a stray key, a repaint) must stop the commit rather than ride
        #    on a stale reading.
        ok2, state, chan = self._settle_checkbox(eh, desired, timeout=1.0)
        if not ok2:
            return {"ok": False, "halt": True,
                    "reason": self._checkbox_halt_reason(
                        fn, desired, state, chan,
                        "It would not hold that state long enough to be committed, so the "
                        "Enter was NOT pressed and nothing was written.")}
        self._keys("{ENTER}")
        took, how = self._verify_value_committed(base_prompt, value_prompt)
        if not took:
            return {"ok": False, "halt": True,
                    "reason": f"Drake did not accept the tick for field {fn} — it is still "
                              f"asking for that field's value. NOTHING was committed. Tick "
                              f"this box by hand.",
                    "prompt_after_value": how}
        shown = "ticked" if desired else "clear"
        return {"ok": True,
                "read_back": f"checkbox {shown} — confirmed via {chan}",
                "commit_evidence": how,
                "checkbox": {"desired": desired, "arrival": arrival if ok0 else None,
                             "tokens_tried": tokens_tried, "verified_by": chan,
                             "note": self._last_checkbox_note}}

    def headsdown_type(self, field_no, value, *, method: str = "scancode",
                       settle_after: float = 0.0, kind: Optional[str] = None) -> dict:
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
          6b. CHECKBOX fields (Box 13) take a different value stage — Drake shows the tick
             box itself, not a text box — so they route to _enter_checkbox, which reads the
             TICK instead of counting characters. `kind` comes from the field map; when it
             is not supplied the popup is asked, and a checkbox appearing where the map
             expects money or text HALTs as build drift.
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
            # 2) unexpected/error dialog already up? (a known one is cleared by clicking its
            #    named button — see _dismiss_dialog; everything else halts)
            bad = self._clear_blocking_dialog()
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
            pre = self._clear_target(edit)
            if not pre.get("ok"):
                return {"ok": False, "halt": True, "reason": pre["reason"]}
            self._keys(fn)  # real VK/scan keys land in the focused popup edit (focus proven)
            if edit.surface:
                # No child window: verify against what the popup SHOWS. Same drain proof,
                # different channel — see _settle_surface.
                settled, got, chan = self._settle_surface(eh, fn, pre.get("baseline"))
                if not settled:
                    return {"ok": False, "halt": True,
                            "reason": self._surface_halt_reason(
                                f"field number {fn!r}", got, chan,
                                "refusing to press Enter on a number it cannot confirm")}
            else:
                # EXACT comparison for the number: _same_value's cosmetic tolerance is for
                # money and must never bless a field number ('4' vs '4.0' is a different box).
                settled, got = self._settle_read(eh, fn, exact=True, timeout=1.0)
                if not settled:
                    return {"ok": False, "halt": True,
                            "reason": f"popup edit never settled on field number {fn!r} "
                                      f"(last read {got!r}) — refusing to press Enter"}
            # Baseline the NUMBER prompt while it is still showing, so step 6 can tell
            # "Drake took the number" from "Drake silently refused it".
            base_prompt = self._stable_prompt(popup, eh)
            if edit.surface and not base_prompt:
                # No baseline means step 6 cannot tell acceptance from refusal, and on a
                # painted popup there is no weaker test to fall back on. Stop HERE rather
                # than after the Enter: the number has only been typed, not committed, so
                # nothing has happened to the return yet.
                return {"ok": False, "halt": True,
                        "reason": f"could not get a steady reading of the popup before "
                                  f"committing field {fn} — two consecutive reads never "
                                  f"agreed (channels: {'+'.join(self._read_popup_channels(eh)) or 'none'}"
                                  f"{'; OCR: ' + self._last_ocr_error if self._last_ocr_error else ''}). "
                                  f"Nothing was committed. If this repeats, the popup is "
                                  f"legible but unstable — raise navigation.headsdown_read_"
                                  f"channels to just the steady one."}
            # 6) fire the jump, then OBSERVE what Drake actually did.
            self._keys("{ENTER}")
            model, dlg = self._classify_after_jump(eh, fn, base_prompt=base_prompt,
                                                   timeout=self.jump_timeout)
            if model == "error":
                return {"ok": False, "halt": True,
                        "reason": f"error dialog after jumping to field {fn}: {dlg['summary']}", "dialog": dlg}
            if model == "rejected":
                # Say what was OBSERVED, not what it means. "Drake refused this number" was
                # a conclusion the evidence does not support: the popup staying on the
                # number prompt for the whole budget is equally consistent with Drake being
                # busy (an employer-lookup auto-fill blocks its UI thread), and reporting
                # the conclusion sent a live debug down the wrong path — the number 14 was
                # fine and the jump had simply not landed yet.
                return {"ok": False, "halt": True,
                        "reason": f"the popup never moved off the field-number prompt for "
                                  f"field {fn} within {self.jump_timeout:g}s, so nothing was "
                                  f"entered. Either Drake declined that number (greyed-out "
                                  f"and foreign-address-only boxes decline silently — check "
                                  f"the number is right for this screen), or it was still "
                                  f"busy: committing a field can fire Drake's employer "
                                  f"auto-fill, and the jump lands when that finishes. If the "
                                  f"screenshot shows the popup ON that field's value box, it "
                                  f"was the second one — raise "
                                  f"navigation.headsdown_jump_timeout."}
            if model == "unknown":
                return {"ok": False, "halt": True,
                        "reason": f"could not read the popup at all after jumping to field {fn} "
                                  f"— refusing to type a value blind"}
            # read_back stays None on the per-jump path: the canvas exposes no value to read,
            # which is exactly why that path types no trailing Enter.
            committed, commit_evidence, read_back = False, None, None
            checkbox_info = None
            if model == "per-jump" and kind == "checkbox":
                # The caret would be on a canvas checkbox, and the canvas is the surface
                # that exposes nothing at all — no child window, no UIA value, and no
                # popup rectangle to look at. A tick typed there could not be confirmed,
                # and an unconfirmable tick is exactly what this driver does not commit.
                return {"ok": False, "halt": True,
                        "reason": f"field {fn} is a checkbox and this build put the caret on "
                                  f"the CANVAS instead of keeping the popup up. A tick on the "
                                  f"canvas cannot be read back, so it will not be typed — "
                                  f"tick this box by hand."}
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
                # Is this field's value stage a CHECKBOX rather than a text box? Asked of
                # the popup BEFORE anything is typed — including before _clear_target's
                # backspaces, which have no business being sent at a checkbox.
                shows_checkbox = self._popup_checkbox_present(eh, screen=False)
                if shows_checkbox is True and kind not in (None, "checkbox"):
                    # Drake is showing a tick box for a field this run believes is money or
                    # text. That is build drift — the heads-down numbers moved — and typing
                    # here would put a value in some other box entirely.
                    return {"ok": False, "halt": True,
                            "reason": f"Drake is showing a CHECKBOX for field {fn}, but the "
                                      f"map says this field is {kind!r}. The heads-down field "
                                      f"numbers have moved on this build — re-verify them "
                                      f"against the screen before running again. Nothing was "
                                      f"entered."}
                if kind == "checkbox" or (kind is None and shows_checkbox is True):
                    res = self._enter_checkbox(popup, eh, fn, val, base_prompt)
                    if not res.get("ok"):
                        return res
                    checkbox_info = res.get("checkbox")
                    committed, commit_evidence = True, res.get("commit_evidence")
                    read_back = res.get("read_back")
                    if settle_after:
                        time.sleep(settle_after)
                    bad = self._detect_unexpected_dialog()
                    if bad:
                        return {"ok": False, "halt": True,
                                "reason": f"error dialog after entering field {fn}: {bad['summary']}",
                                "dialog": bad}
                    return {"ok": True, "field_no": field_no, "model": model,
                            "value_committed": True, "commit_evidence": commit_evidence,
                            "read_back": read_back, "checkbox": checkbox_info,
                            "popup_reopened": opened.get("opened"),
                            "popup_attempts": opened.get("attempts"),
                            "popup_after_value": self._find_headsdown_popup(timeout=0.2) is not None}
                pre = self._clear_target(edit)
                if not pre.get("ok"):
                    return {"ok": False, "halt": True, "reason": pre["reason"]}
                value_prompt = self._stable_prompt(popup, eh)  # the VALUE prompt, for step 7
                self._keys(_escape_keys(val))
                # Wait for the box to SETTLE on the value. No set_edit_text "repair": that
                # is a SENT message that jumps ahead of still-queued keystrokes, so it can
                # make a half-typed box look correct and let the rest of the keys land
                # AFTER the gate passes (52000 -> read '520' -> "repair" -> queued '00'
                # arrives -> 5,200,000 committed, reported OK).
                if edit.surface:
                    # Per-channel baseline from the clear, not the joined value_prompt: each
                    # channel is judged against what IT showed before the value was typed.
                    settled, read_back, chan = self._settle_surface(eh, val, pre.get("baseline"))
                    if not settled:
                        return {"ok": False, "halt": True,
                                "reason": self._surface_halt_reason(
                                    f"value {val!r} for field {fn}", read_back, chan,
                                    "refusing to commit a value it cannot confirm")}
                else:
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
            # The control tree via ctypes, NOT popup.descendants(): pywinauto's own
            # enumeration is the layer that could not see this popup's text box in the first
            # place, and the whole point of the dump is to be true when pywinauto is not.
            # Recorded BEFORE resolution so a failure to identify the box still reports what
            # is actually in there — that list is the fix.
            out["popup_controls"] = self._popup_edit_children(self._popup_hwnd or int(popup.handle))
            try:
                edit = self._popup_edit(popup)
            except PopupEditNotFound as e:
                out["ok"] = False
                out["reason"] = str(e)
                out["edit_resolution"] = {k: v for k, v in e.info.items() if k != "children"}
                # Nothing was typed, so Drake is not armed — but the popup we opened is
                # still up. Close it so the next run starts from a clean screen.
                out["disarm"] = self._disarm_popup_blind()
                return out
            eh = int(edit.handle)
            out["edit_handle"] = eh
            out["edit_class_name"] = edit.class_name
            out["edit_resolved_by"] = edit.how
            try:
                edit.set_focus()
            except Exception as e:
                out["set_focus_error"] = str(e)
            out["focus_is_edit_after_setfocus"] = (self._focused_hwnd()[0] == eh)
            out["surface_mode"] = edit.surface
            # Which READ CHANNELS work on this popup, one by one and by name. With no child
            # window there is nothing to WM_GETTEXT, so this is the measurement that decides
            # whether entry can verify a keystroke at all — every gate downstream is built
            # on being able to read this box.
            out["read_channels"] = self._probe_read_channels(eh, edit)
            # STATE 1 — waiting for a FIELD NUMBER. This string is the baseline every
            # later refusal check compares against; if it is empty, this build exposes no
            # prompt text and refusal detection degrades (the driver says so at runtime).
            out["prompt_at_number"] = self._popup_prompt(popup, eh)
            # Put the probe number in the box the way the entry path does, then read it back
            # through every channel. A channel that can see it is a channel that can gate a
            # real field number before the irreversible Enter.
            pre = self._clear_target(edit)
            out["baseline_before_typing"] = pre.get("baseline")
            if edit.surface:
                # Compare each channel against ITSELF either side of the keystroke — the
                # same before/after count the entry gate uses, so this verdict means what
                # it says rather than approximating it.
                before = self._probe_read_channels(eh, edit)
                out["channels_after_clear"] = before
                self._keys(probe_field)
                time.sleep(0.35)
                out["after_typing"] = self._probe_read_channels(eh, edit)
                out["channels_that_saw_the_typed_number"] = [
                    name for name, r in out["after_typing"].items()
                    if r.get("text") and self._surface_count(r["text"], probe_field)
                    > self._surface_count((before.get(name) or {}).get("text"), probe_field)]
            else:
                try:
                    edit.set_edit_text(probe_field)
                    out["set_edit_text_readback"] = _safe_read_edit(eh)
                except Exception as e:
                    out["set_edit_text_error"] = str(e)
                out["channels_that_saw_the_typed_number"] = ["win32"]
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
            saw = out.get("channels_that_saw_the_typed_number") or []
            out["read_back"] = (
                f"AVAILABLE via {', '.join(saw)} — a keystroke can be confirmed BEFORE the "
                f"irreversible Enter" if saw else
                "UNAVAILABLE — no channel could see the probe number, so entry cannot verify "
                "what it typed and will halt rather than commit blind. Install Tesseract "
                "(and set navigation.tesseract_cmd if it is not on PATH) to enable the "
                "screen-reading channel — on a popup Drake paints itself, that is the only "
                "read-back there is")
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

    def probe_checkbox_field(self, field_no: str = "47", flip: bool = True) -> dict:
        """Calibration probe for a CHECKBOX field: what does Drake's popup actually expose,
        and which token flips the tick?

        Answers the three questions the entry path has to have right, and answers them by
        measurement rather than by assumption:
          • is the value stage a checkbox at all, and does UI Automation expose it (Toggle
            pattern) or is the on-screen glyph the only evidence?
          • what state does the box arrive in — Drake may pre-tick the field you jumped to,
            in which case sending a token would CLEAR it;
          • does the configured token flip it, and is the flip a SET or a TOGGLE?

        Commits nothing: it flips the tick, flips it back, and leaves with Esc. The tick is
        only ever pending — Enter is what would write it — but this is a probe, so verify
        the box by eye on the screenshot afterwards regardless. `flip=False` makes it purely
        observational: it reads the arrival state and leaves without sending a token."""
        out = {"ok": True, "field_no": str(field_no)}
        if self.dry_run or self.w32 is None:
            return {"ok": False, "reason": "no win32 popup connection (run on the VM after connect)"}
        fn = str(field_no)
        try:
            import time
            self.begin_batch()
            popup = self._find_headsdown_popup(timeout=0.5)
            if popup is None:
                out["ensure_open"] = self._ensure_popup_open(timeout=2.5)
                popup = self._find_headsdown_popup(timeout=1.0)
            if popup is None:
                # Say WHY it did not open. "Click a Drake field first" is one cause among
                # several, and printing it unconditionally sends the operator to check the
                # one thing they already did while the real reason sits in the JSON below.
                why = (out.get("ensure_open") or {}).get("reason")
                return {"ok": False, "ensure_open": out.get("ensure_open"),
                        "reason": why or ("the heads-down popup did not open — click a Drake "
                                          "field so its cursor is blinking, then re-run")}
            ready = self._popup_ready_for_number(popup)
            out["popup_ready_for_number"] = ready
            if not ready.get("ok"):
                return {"ok": False, "reason": ready.get("reason")}
            edit = self._popup_edit(popup)
            edit.wait("ready", timeout=2)
            eh = int(edit.handle)
            if not self._focus_popup_edit(edit, eh):
                return {"ok": False, "reason": "popup edit never took keyboard focus"}
            pre = self._clear_target(edit)
            if not pre.get("ok"):
                return {"ok": False, "reason": pre["reason"]}
            self._keys(fn)
            settled, got, chan = self._settle_surface(eh, fn, pre.get("baseline"))
            out["field_number_settled"] = {"ok": settled, "read": got, "channel": chan}
            if not settled:
                out["ok"] = False
                out["reason"] = f"the popup never showed field number {fn} — nothing typed on"
                out["disarm"] = self._disarm_popup()
                return out
            base_prompt = self._stable_prompt(popup, eh)
            self._keys("{ENTER}")
            model, dlg = self._classify_after_jump(eh, fn, base_prompt=base_prompt,
                                                   timeout=self.jump_timeout)
            out["model"] = model
            if model != "persistent":
                out["ok"] = False
                out["reason"] = (f"after jumping to field {fn} the popup did not come back "
                                 f"asking for a value (model={model!r}"
                                 f"{', dialog: ' + dlg['summary'] if dlg else ''})")
                out["disarm"] = self._disarm_popup()
                return out
            out["prompt_at_value"] = self._stable_prompt(popup, eh)
            # RAW channel evidence, by name — the same shape probe-popup reports for text,
            # so a build that exposes nothing says so plainly instead of being inferred.
            els = self._read_popup_checkbox_uia(eh)
            out["uia_checkboxes"] = els
            out["uia_channel"] = ("silent (no answer at all)" if els is None else
                                  f"{len(els)} checkbox element(s)")
            out["pixel_tick_visible"] = self._read_popup_checkbox_pixels(eh)
            out["pixel_error"] = self._last_tick_error
            ok0, arrival, chan0 = self._settle_checkbox(eh, timeout=2.0)
            out["arrival_state"] = {"read": ok0, "ticked": arrival, "channel": chan0}
            out["is_checkbox_field"] = self._popup_checkbox_present(eh)
            if flip and ok0:
                flips = []
                for token in self.checkbox_tokens:
                    self._keys(token)
                    ok, st, ch = self._settle_checkbox(eh, not arrival, timeout=1.5)
                    flips.append({"token": token, "flipped": bool(ok), "state_after": st,
                                  "channel": ch})
                    if ok:
                        break
                out["token_trials"] = flips
                out["token_that_worked"] = next((f["token"] for f in flips if f["flipped"]), None)
                if out["token_that_worked"]:
                    # Put it back the way it was found. If the same token does NOT restore
                    # it, the token SETS rather than toggles — worth knowing, and worth
                    # saying loudly, because the box is then left showing a pending tick.
                    self._keys(out["token_that_worked"])
                    ok_r, st_r, ch_r = self._settle_checkbox(eh, arrival, timeout=1.5)
                    out["restored"] = {"ok": bool(ok_r), "state_after": st_r, "channel": ch_r}
                    out["token_behaviour"] = "toggle" if ok_r else "set (not a toggle)"
            elif flip:
                out["token_trials"] = "skipped — the arrival state could not be read, so a " \
                                      "token's effect could not be measured either"
            out["disarm"] = self._disarm_popup()
            out["popup_open_after_disarm"] = self._find_headsdown_popup(timeout=0.3) is not None
            out["unexpected_dialog"] = self._detect_unexpected_dialog()
            out["verdict"] = (
                "READABLE — the tick can be confirmed before the Enter that commits it"
                if ok0 else
                "UNREADABLE — neither UI Automation nor the screen could report the tick "
                "state, so checkbox fields will HALT rather than commit blind")
        except Exception as e:
            out["ok"] = False
            out["reason"] = str(e)
        return out

    def _probe_read_channels(self, edit_hwnd, edit) -> dict:
        """Try EVERY read channel against the popup and report each one's answer by name.

        Not a fallback chain — the point is to learn which channels exist on this build, so
        the run afterwards is not a guess. `null` text means the channel returned nothing;
        `error` means it is not installed or blew up."""
        out = {}
        if edit is not None and not edit.surface:
            out["win32"] = {"text": _read_edit_or_none(edit_hwnd)}
        for name, fn in (("uia", self._read_popup_uia), ("ocr", self._read_popup_ocr)):
            try:
                out[name] = {"text": fn(edit_hwnd)}
            except Exception as e:
                out[name] = {"text": None, "error": str(e)}
        if out.get("ocr", {}).get("text") is None and self._last_ocr_error:
            out.setdefault("ocr", {})["error"] = self._last_ocr_error
        return out

    def _disarm_popup_blind(self) -> dict:
        """Close the popup when its text box could NOT be identified.

        _disarm_popup proves focus by comparing the focused HWND to the edit's — impossible
        here, that is the whole failure. The weaker but still sufficient proof: the popup
        must be the FOREGROUND ROOT, so the keystroke goes to a window inside it and not to
        Drake's canvas, where Esc exits the data-entry screen. No proof, no Esc."""
        popup = self._find_headsdown_popup(timeout=0.5)
        if popup is None:
            self._popup_owned = False
            return {"ok": True, "note": "popup already closed"}
        try:
            root = int(_keyboard_target_info().get("root") or 0)
        except Exception as e:
            return {"ok": False, "note": f"could not read the foreground window ({e}) — did "
                                         f"NOT send Esc. Press Esc in Drake by hand."}
        if root != int(self._popup_hwnd or 0):
            return {"ok": False,
                    "note": f"the heads-down popup is not the foreground window (hwnd={root} "
                            f"is) — did NOT send Esc, because an Esc on Drake's canvas exits "
                            f"the data-entry screen. Press Esc in Drake by hand."}
        self._keys("{ESC}")
        import time
        time.sleep(0.25)
        still = self._find_headsdown_popup(timeout=0.3) is not None
        self._popup_owned = False
        return {"ok": not still,
                "note": ("popup closed" if not still else
                         "Esc did not close the popup — press Esc in Drake by hand")}

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
            # Leaving Drake armed is worse than the weaker proof: fall back to "the popup is
            # the foreground window", which still guarantees the Esc cannot reach the canvas.
            blind = self._disarm_popup_blind()
            blind["note"] = f"could not resolve the popup edit ({e}); " + blind.get("note", "")
            return blind
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

    def canvas_windows(self) -> list:
        """Drake's DATA-ENTRY windows — the ones that hold the form, not the app frame.

        This distinction is the whole point. The project's founding measurement, "Drake
        exposes 0 Edit controls and 0 readable values", was taken against the MAIN FRAME
        (`Drake 2025 Tax Software`). Drake hosts a return's data-entry screen in a separate
        top-level window, and that window exposes 253 UIA elements with real values —
        `12-3456789`, `test employer llc`, `52000`. The canvas was readable all along; the
        probe was pointed at the wrong window."""
        if not _WINFN or self.dry_run:
            return []
        try:
            pid = int(self.pid or self.win.element_info.process_id)
        except Exception:
            return []
        out = []
        for w in _enum_toplevel_windows(pid):
            if not w.get("visible"):
                continue
            if _re.search(self.popup_title_re, w.get("title") or ""):
                continue      # the heads-down popup is not the canvas
            r = w.get("rect") or [0, 0, 0, 0]
            if r[2] < self.min_main_w or r[3] < self.min_main_h:
                continue      # overlays and the chat bubble
            if "data entry" not in (w.get("title") or "").lower():
                continue
            out.append(int(w["hwnd"]))
        return out

    def read_canvas_values(self) -> list:
        """Every value currently readable on Drake's data-entry canvas: [{text, value,
        control_type, rect}]. Empty list when nothing can be read — never a claim.

        This is the read-back the driver has never had. Everything else verifies what the
        POPUP echoed, which is what the operator typed — and the live run of 2026-08-04
        proved that is not the same thing: Drake accepted 'DALLAS' into an empty Box 20
        locality dropdown, echoed it back in the popup, took the Enter, and left the box on
        the form EMPTY. Four fields reported OK and wrote nothing."""
        out, seen = [], set()
        for h in self.canvas_windows():
            try:
                els = self.app.window(handle=h).descendants()
            except Exception:
                continue
            for el in els:
                try:
                    ct = str(el.element_info.control_type or "")
                    tx = (el.window_text() or "").strip()
                except Exception:
                    continue
                val = ""
                try:
                    v = el.get_value()
                    val = str(v).strip() if v is not None else ""
                except Exception:
                    pass
                if not (tx or val):
                    continue
                try:
                    r = el.rectangle()
                    rect = [int(r.left), int(r.top), int(r.right), int(r.bottom)]
                except Exception:
                    rect = None
                key = (ct, tx, val, tuple(rect or ()))
                if key in seen:
                    continue      # the same control is reachable from two window handles
                seen.add(key)
                out.append({"control_type": ct, "text": tx, "value": val, "rect": rect})
        return out

    def explore_screen(self, *, cap: int = 1500) -> dict:
        """READ-ONLY reconnaissance: everything Drake is showing right now.

        Presses nothing, clicks nothing, focuses nothing, changes nothing. Its only job is
        to answer "what is ACTUALLY on screen" so navigation can be built against Drake as
        it is, rather than against a guess about how it probably works.

        This tool exists because of a specific failure. The caret re-arm of 2026-08-05 was
        reasoned about instead of looked at — Esc would reach the popup, Tab would move
        focus, a changed focus HWND would prove it worked — and every one of those was
        wrong, live, three times. `open_return()` and `open_screen()` in this file are
        still guesses of exactly that kind. Nothing gets built on them until this has
        shown what the screens really contain.

        Returns every visible top-level window of the Drake process and, for each, its UIA
        tree: control type, name, value, automation id, rect, enabled, and which element
        holds the keyboard.
        """
        if not _WINFN or self.dry_run or self.app is None:
            return {"ok": False, "error": "explore runs on Windows with a live connection"}
        try:
            pid = int(self.pid or self.win.element_info.process_id)
        except Exception as e:
            return {"ok": False, "error": f"no Drake process: {type(e).__name__}: {e}"}

        windows, budget = [], int(cap)
        for w in _enum_toplevel_windows(pid):
            if not w.get("visible"):
                continue
            r = w.get("rect") or [0, 0, 0, 0]
            entry = {"hwnd": int(w["hwnd"]), "title": w.get("title"),
                     "class_name": w.get("class_name"), "rect": r,
                     "enabled": w.get("enabled"),
                     "is_main": bool(self.main_hwnd and int(w["hwnd"]) == int(self.main_hwnd)),
                     "elements": [], "truncated": False}
            if r[2] < 120 or r[3] < 60:
                entry["note"] = "too small to be a screen — not walked"
                windows.append(entry)
                continue
            try:
                els = self.app.window(handle=int(w["hwnd"])).descendants()
            except Exception as e:
                entry["note"] = f"UIA tree unavailable: {type(e).__name__}: {e}"
                windows.append(entry)
                continue
            for el in els:
                if budget <= 0:
                    entry["truncated"] = True
                    break
                budget -= 1
                entry["elements"].append(_describe_element(el))
            windows.append(entry)

        return {"ok": True, "takenAt": _now(), "pid": pid, "main_hwnd": self.main_hwnd,
                "keyboard_target": _keyboard_target_info(),
                "canvas_windows": self.canvas_windows(),
                "budget_left": budget, "windows": windows}

    # -- navigation primitives --------------------------------------------------------
    # These only ACT. Every decision about whether acting is safe — which client, which
    # screen, whether the right return is open — lives in drake_nav.py, where it can be
    # tested without Drake. Nothing here chooses a target.

    def nav_find_window(self, title_re: str, *, timeout: float = 8.0):
        """hwnd of a visible Drake window whose title matches, or None after `timeout`.

        Polls, because Drake's windows appear on their own schedule: the Open/Create dialog
        takes a beat, and a return with a lot of screens takes longer. A fixed sleep would
        be either too short on a slow box or wasted time on a fast one.
        """
        import time
        deadline = time.time() + float(timeout)
        while True:
            try:
                pid = int(self.pid or self.win.element_info.process_id)
                for w in _enum_toplevel_windows(pid):
                    if w.get("visible") and _re.search(title_re, w.get("title") or ""):
                        return int(w["hwnd"])
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def nav_wait_gone(self, hwnd, *, timeout: float = 8.0) -> bool:
        """Wait for a window to close. Used to confirm the Open/Create dialog actually
        went away — a dialog still on screen means the click did not take, and pressing on
        would send keystrokes into it."""
        import time
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            try:
                pid = int(self.pid or self.win.element_info.process_id)
                alive = any(int(w["hwnd"]) == int(hwnd) and w.get("visible")
                            for w in _enum_toplevel_windows(pid))
            except Exception:
                alive = False
            if not alive:
                return True
            time.sleep(0.1)
        return False

    def nav_window_title(self, hwnd) -> str:
        try:
            for w in _enum_toplevel_windows(int(self.pid or self.win.element_info.process_id)):
                if int(w["hwnd"]) == int(hwnd):
                    return w.get("title") or ""
        except Exception:
            pass
        return ""

    def nav_elements(self, hwnd) -> list:
        """Every element in a window's UIA tree, flattened. The list drake_nav's choosers
        pick from — they receive data, never live COM handles, which is what lets the same
        choosing logic be tested offline against dumps of the real screens."""
        try:
            els = self.app.window(handle=int(hwnd)).descendants()
        except Exception:
            return []
        out = []
        for el in els:
            d = _describe_element(el)
            d["_el"] = el                 # the live handle, for nav_act; never serialised
            out.append(d)
        return out

    def nav_act(self, element: dict, *, want: str = "invoke") -> dict:
        """Press/select one element. {'ok', 'how', 'error'}.

        Tries the accessibility patterns first and a real mouse click only as a last
        resort. Invoke and Select are coordinate-free: they cannot land on the wrong
        control because the window moved, was partly off-screen, or was overlapped — and
        Drake's own client list is a WPF grid whose rows scroll under a fixed header.
        `how` is reported so a run's log says which one Drake accepted.
        """
        el = (element or {}).get("_el")
        if el is None:
            return {"ok": False, "how": None, "error": "no live element to act on"}
        order = ({"invoke": ["invoke", "select", "click"],
                  "select": ["select", "invoke", "click"]}).get(want, ["invoke", "select", "click"])
        errors = []
        for how in order:
            try:
                if how == "invoke":
                    el.invoke()
                elif how == "select":
                    el.select()
                else:
                    el.click_input()
                return {"ok": True, "how": how, "error": None}
            except Exception as e:
                errors.append(f"{how}: {type(e).__name__}")
        return {"ok": False, "how": None, "error": "; ".join(errors)}

    def nav_type_into(self, element: dict, text: str) -> dict:
        """Clear a text box and type into it the way a person would — focus, select all,
        delete, then keystrokes. {'ok', 'error'}.

        NOT a programmatic set_text. Drake's client search filters its grid as characters
        arrive; a value poked straight into the control can leave the bound list showing
        the results of the PREVIOUS search, and the row we then pick would belong to
        whoever was on screen before. Typing makes Drake do its own filtering.
        """
        el = (element or {}).get("_el")
        if el is None:
            return {"ok": False, "error": "no live element to type into"}
        try:
            el.set_focus()
        except Exception as e:
            return {"ok": False, "error": f"could not focus the box: {type(e).__name__}: {e}"}
        try:
            self._keys("^a{DELETE}")
            self._keys(_escape_keys(str(text)))
            return {"ok": True, "error": None}
        except Exception as e:
            return {"ok": False, "error": f"could not type: {type(e).__name__}: {e}"}

    def form_record_state(self) -> dict:
        """What is already on the CURRENT data-entry record.

        {ok, index, count, values, populated, reason}

        Drake screens are RECORDS, not pages: one W-2 screen holds one employer, and Page
        Down opens another. Nothing in this project ever looked, so every run typed into
        whatever record happened to be showing — fine while a human picked the screen, and
        a way to overwrite an existing W-2 or duplicate one the moment the agent navigates
        for itself. Duplicating a W-2 does not look like a bug; it looks like a client who
        earned twice as much.

        Values come from UIA, which is trustworthy here: the blue blocks on Drake's form
        are FLAGGED-field highlights, not masked data, and the first end-to-end read an EIN
        back through this same channel. An unreadable form returns ok=False rather than an
        empty list, because "I could not look" must not be mistaken for "nothing is there".
        """
        de = self.nav_data_entry_window()
        if de["kind"] != "form":
            return {"ok": False, "reason": f"no data-entry form is open (kind={de['kind']})",
                    "index": None, "count": None, "values": [], "populated": 0}
        els = self.nav_elements(de["hwnd"])
        if not els:
            return {"ok": False, "reason": "the form's control tree could not be read",
                    "index": None, "count": None, "values": [], "populated": 0}

        index = count = None
        for e in els:
            if e.get("automation_id") == "txtInstance":
                from drake_nav import parse_record_position
                pos = parse_record_position(e.get("name"))
                if pos:
                    index, count = pos["index"], pos["count"]
                break

        values, seen = [], set()
        for e in els:
            if e.get("control_type") not in ("Edit", "ComboBox"):
                continue
            r = e.get("rect")
            if not r or int(r[1]) < self._canvas_form_top:
                continue                      # toolbar / tab strip, not the form
            v = (e.get("value") or "").strip()
            if not v:
                continue
            # A ComboBox and its inner PART_EditableTextBox are the same box twice.
            key = (int(r[1]), int(r[0]), v)
            if key in seen:
                continue
            seen.add(key)
            values.append({"automation_id": e.get("automation_id"), "value": v,
                           "rect": r})
        return {"ok": True, "reason": "", "index": index, "count": count,
                "values": values, "populated": len(values)}

    def form_new_record(self, *, timeout: float = 6.0) -> dict:
        """Page Down to a fresh blank record on the open screen. {ok, index, count, reason}.

        Verifies afterwards that the record really is blank. Drake's own status bar says
        'Press Page Down for New Screen', but a keystroke that silently did nothing would
        otherwise leave the run typing a second W-2 on top of the first.
        """
        import time
        before = self.form_record_state()
        if not before["ok"]:
            return {"ok": False, "reason": before["reason"]}
        if not self._focus_canvas_field():
            return {"ok": False, "reason": "could not put the caret on the form before Page Down"}
        try:
            self._keys("{PGDN}")
        except Exception as e:
            return {"ok": False, "reason": f"Page Down failed: {type(e).__name__}: {e}"}

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            after = self.form_record_state()
            if after["ok"] and after["populated"] == 0 and (
                    after["index"] != before["index"] or after["count"] != before["count"]):
                return {"ok": True, "index": after["index"], "count": after["count"],
                        "reason": ""}
            time.sleep(0.15)
        # WHY it did not work matters more than that it did not. Drake answers a Page Down
        # off an incomplete screen with a modal — "There are fields on this screen that must
        # contain data if you are planning to e-file this return" — and reporting that as
        # "Page Down did not work" sends the operator looking at the keyboard instead of at
        # the question Drake is actually asking them.
        #
        # It is NOT auto-answered here. The two buttons mean opposite things ("enter the
        # data now" keeps this record, "exit this screen" leaves it), so the choice decides
        # which record the next 78 values land in. binding.json's auto_dismiss rule answers
        # OK for the ENTRY path, where staying put is right; that answer is wrong here, and
        # a rule that is right in one place and wrong in another is worse than no rule.
        after = self.form_record_state()
        dlg = self._detect_unexpected_dialog()
        if dlg:
            said = " ".join(str(dlg.get("text") or dlg.get("summary") or "").split())[:300]
            return {"ok": False, "index": after.get("index"), "count": after.get("count"),
                    "dialog": {"title": dlg.get("title"), "text": said},
                    "reason": f"Drake is asking a question instead of opening a new record — "
                              f"{dlg.get('title')!r}: {said!r}. This normally means the W-2 "
                              f"record already on screen is incomplete. Answer it in Drake "
                              f"(or clear that record), then re-send. Nothing was typed."}
        return {"ok": False, "index": after.get("index"), "count": after.get("count"),
                "reason": f"Page Down did not produce a blank record (still "
                          f"{after.get('populated')} value(s) on record "
                          f"{after.get('index')} of {after.get('count')})"}

    def nav_data_entry_window(self) -> dict:
        """The open return's window and WHICH KIND it is: {'hwnd', 'title', 'kind'}.

        kind is 'menu' (the Data Entry Menu), 'form' (a tax screen), 'unknown', or 'none'.
        Both real windows are titled 'Data Entry (...)', so the title cannot tell them
        apart, and the difference decides everything downstream: `_focus_canvas_field()`
        picks the topmost Edit in a data-entry window, and on the MENU the only Edit is the
        screen-search box — a run starting there would arm its caret in a search field and
        type field numbers into it.
        """
        from drake_nav import classify_data_entry_window, DATA_ENTRY_TITLE_RE
        hwnd = self.nav_find_window(DATA_ENTRY_TITLE_RE, timeout=0.0)
        if not hwnd:
            return {"hwnd": None, "title": "", "kind": "none"}
        ids = [e.get("automation_id") for e in self.nav_elements(hwnd)]
        return {"hwnd": hwnd, "title": self.nav_window_title(hwnd),
                "kind": classify_data_entry_window(ids)}

    def audit_canvas(self, entries: list) -> dict:
        """Which planned values are ACTUALLY on the form afterwards? {ok, missing, checked}.

        Deliberately a presence test and nothing more. It cannot say a value is in the RIGHT
        box — that needs a field-number-to-control map this build does not have yet — but it
        does catch the failure the popup gate structurally cannot see: a value that never
        reached the form at all. Comparison is normalised, because Drake reformats what it
        stores: an EIN comes back '12-3456789', a ZIP '75001-____', and a city it corrected
        from the ZIP comes back as its own spelling."""
        canvas = self.read_canvas_values()
        if not canvas:
            return {"ok": False, "reason": "nothing on the data-entry canvas could be read, "
                                           "so no value could be confirmed on the form",
                    "missing": [], "checked": 0, "canvas_elements": 0}
        blobs = {_norm_prompt(f"{c['text']} {c['value']}") for c in canvas}
        joined = " ".join(blobs)
        missing = []
        for e in entries:
            want = _norm_prompt(str(e.get("value", "")))
            if not want:
                continue
            # A checkbox has no text on the canvas — its state is a glyph, and it was already
            # proven at entry by reading the widget. Nothing to look for here.
            if e.get("kind") == "checkbox":
                continue
            if want in joined:
                continue
            # DRAKE ROUNDS MONEY TO WHOLE DOLLARS on this screen — 29476.71 is stored and
            # shown as 29477 — which is what the IRS expects and what a preparer keying by
            # hand would produce. Before this, the check called every amount with cents
            # "not on the form": the first real W-2 through the pipeline reported 9 of 25
            # values missing, and all nine were present and correct. A check that cries wolf
            # on normal behaviour is worse than no check, because it teaches people to
            # ignore the one time it is right.
            #
            # This is NOT a looser gate. Only the value ROUNDED HALF-UP is accepted as well,
            # so 29476.71 matches 29477 and nothing else: 29470, 2947 and 294770 all still
            # fail, which are the transpositions and lost/extra digits that actually matter.
            rounded = _whole_dollars(e.get("value"))
            if rounded is not None and rounded in joined:
                continue
            missing.append(e)
        return {"ok": not missing, "missing": missing, "checked": len(entries),
                "canvas_elements": len(canvas)}

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
                    "enabled": bool(u32.IsWindowEnabled(h)),
                    "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top]})
        return True

    u32.EnumChildWindows(int(hwnd), _cb, 0)
    return out


# Window classes that ARE a typing box. Matched as a substring, case-insensitively, because
# the class name is a toolkit detail we do not control: plain Win32 gives "Edit", Delphi/C++
# Builder gives "TEdit"/"TMemo"/"TMaskEdit", .NET gives "WindowsForms10.EDIT.app.0.378734a",
# rich text gives "RichEdit20W". pywinauto's class_name= criterion is EXACT string equality
# (findwindows.py:257) and its class_name_re= is anchored re.match — so asking it for
# class_name="Edit" finds NOTHING on any of those builds, which is what made the popup's
# text box "not ready / no handle: timed out" on the live machine.
_EDITISH_CLASS = _re.compile(r"edit|textbox|memo|combobox", _re.I)


def _editish(class_name: str) -> bool:
    return bool(_EDITISH_CLASS.search(class_name or ""))


def _tokens(s) -> list:
    """A reading split into comparable tokens: alphanumeric runs, lowercased. Punctuation,
    case and spacing are dropped because they are exactly what varies between two readings
    of one unchanged screen."""
    return [t.lower() for t in _re.split(r"[^0-9A-Za-z]+", str(s or "")) if t]


def _norm_prompt(s) -> str:
    """A prompt reading reduced to what it MEANS, so two readings of the same screen state
    compare equal. Alphanumerics only, lowercased: OCR and Drake both vary punctuation,
    spacing and case between frames, and prompt comparison decides whether a value was
    accepted — it must not turn on a stray comma."""
    return "".join(ch for ch in str(s or "") if ch.isalnum()).lower()


def _rank_popup_edit(children, *, preferred_class="Edit", focused_hwnd=None):
    """Pick the popup's typing box out of its children. Returns (hwnd, how) or (None, why).

    Structure, not a hardcoded class name — see _EDITISH_CLASS. Ordered by how much each
    signal actually proves:

      1. the exact class the binding was calibrated to (confirmed on this build)
      2. an edit-shaped class — the focused one first when there is more than one box
      3. the child that currently OWNS THE KEYBOARD, whatever its class: an owner-drawn
         input is still where the keystrokes will land, and we prove focus + read the
         digits back before the irreversible Enter either way

    A Static/Button child is never chosen: the prompt label is not the box. Returning None
    is a real answer — the caller HALTS rather than typing at a window it could not identify.
    """
    kids = list(children or [])
    if not kids:
        return None, "the popup reports NO child windows at all"
    usable = [c for c in kids if c.get("enabled", True)]
    if not usable:                       # e.g. a modal validator has disabled the whole tree
        return None, "every child of the popup is DISABLED"
    shown = [c for c in usable if c.get("visible", True)] or usable

    def _pick(cands):
        for c in cands:                  # focus breaks ties: it is where keys actually go
            if focused_hwnd and int(c["hwnd"]) == int(focused_hwnd):
                return c
        return cands[0]

    exact = [c for c in shown if (c.get("class_name") or "") == preferred_class]
    if exact:
        return int(_pick(exact)["hwnd"]), f"exact class {preferred_class!r}"
    editish = [c for c in shown if _editish(c.get("class_name"))]
    if editish:
        hit = _pick(editish)
        return int(hit["hwnd"]), f"edit-shaped class {hit.get('class_name')!r}"
    if focused_hwnd:
        for c in usable:
            if int(c["hwnd"]) == int(focused_hwnd):
                return int(c["hwnd"]), f"child that owns the keyboard ({c.get('class_name')!r})"
    seen = ", ".join(sorted({(c.get("class_name") or "?") for c in kids})[:8])
    return None, f"no child looks like a text box (classes present: {seen})"


class PopupEditNotFound(Exception):
    """The heads-down popup is open but its typing box could not be identified.

    Carries the popup's whole child topology, because THAT is the missing fact: a halt that
    says only "timed out" costs a round trip to the Windows laptop, while one that lists the
    real class names answers the question in the same run."""

    def __init__(self, info: dict):
        self.info = info
        kids = info.get("children") or []
        if kids:
            rows = "; ".join(
                f"{c.get('class_name')} hwnd={c.get('hwnd')} "
                f"{'vis' if c.get('visible') else 'hidden'}"
                f"{'' if c.get('enabled', True) else ' DISABLED'}"
                f"{(' text=' + repr(c.get('text'))) if c.get('text') else ''}"
                for c in kids[:12])
            detail = f"its {len(kids)} child window(s) are: {rows}"
            fix = ("Send this line (or env-dump-halt.json) — the class name above is what "
                   "`headsdown_popup_edit_class` must be set to.")
        else:
            # The confirmed Drake 2025 shape. Reaching here means the popup is painted AND
            # something other than the popup holds the keyboard, so there is no safe target
            # at all — not even the popup itself.
            detail = (f"it reports NO child windows (Drake paints the box), and the keyboard "
                      f"is held by hwnd={info.get('focused_hwnd')}, not the popup")
            fix = ("Click into Drake and re-run. If the keyboard really is in the popup and "
                   "this still fires, send env-dump-halt.json.")
        super().__init__(
            f"could not identify the heads-down popup's text box ({info.get('why')}). "
            f"The popup IS open (hwnd={info.get('popup_hwnd')}) and {detail}. "
            f"Nothing was typed. {fix}")


class _EditTarget:
    """The popup's typing box, addressed by HANDLE.

    Wraps the same three operations the old pywinauto child specification provided —
    set_focus / set_edit_text / .handle — but resolution already happened by ctypes
    enumeration, so nothing here can fail the exact-class-name match that broke the live
    run. wait() is a no-op on purpose: readiness is proven downstream by the focus oracle
    (GetGUIThreadInfo) and by watching the digits settle, which are facts about this
    window rather than pywinauto's opinion of it."""

    def __init__(self, hwnd, class_name, how, wrapper=None, surface=False):
        self.handle = int(hwnd)
        self.class_name = class_name
        self.how = how
        # surface=True: the popup itself, painted by Drake with no child window. There is
        # nothing to WM_GETTEXT (that returns the CAPTION) and nothing to WM_SETTEXT.
        self.surface = bool(surface)
        self.is_edit = (not surface) and (_editish(class_name) or (class_name or "") == "Edit")
        self._wrapper = wrapper
        self.element_info = _SimpleNamespace(class_name=class_name, handle=int(hwnd))

    def wait(self, flags="ready", timeout=1.0):
        return self

    def set_focus(self):
        if self._wrapper is not None:
            self._wrapper.set_focus()
        return self

    def set_edit_text(self, text):
        """Clear/seed the box. Only ever called on a control we identified as a text box —
        WM_SETTEXT on a window that is NOT an edit rewrites its CAPTION instead, which on
        the popup itself would rename the very window we find it by."""
        if not self.is_edit:
            raise RuntimeError(
                f"refusing to set text on a {self.class_name!r} — it is not a text box, and "
                f"WM_SETTEXT would rewrite its caption instead of its contents")
        if self._wrapper is None:
            raise RuntimeError("no window wrapper for the popup edit")
        self._wrapper.set_edit_text(text)
        return self


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
                              main_enabled, main_disabled_at_attach=False):
    """The pure decision core of the structural dialog gate — no Windows calls, so it is
    provable offline (simulate_headsdown.py feeds it synthetic snapshots). Given the
    process's top-level windows, decide what (if anything) is actually BLOCKING entry:

      rule 1  a NEW dialog-shaped window (not baseline, not already-benign) is a
              validator/error/prompt → blocker, even if the main frame is still enabled;
      rule 2  main frame DISABLED with no heads-down popup up → a modal is pumping:
              blame a new enabled window if there is one, else an enabled dialog-shaped
              one, else report the modality itself (never blame baseline furniture like
              the chat overlay by name) — UNLESS the frame was already disabled when we
              attached, in which case the modality is furniture too: Drake nests its
              data-entry screens and disables the frames behind them, and blaming that
              stopped a live run with no dialog anywhere on screen. A modal that arrives
              LATER still disables the frame and is still caught, because the surviving
              test is "did this change since attach", not "is the frame disabled";
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
        if not main_disabled_at_attach:
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
            cls = c.get("class_name") or ""
            # Skip the configured class AND anything else edit-shaped. If a second text box
            # leaked into the "prompt", the baseline would change every time we TYPED rather
            # than only when Drake changed what it is asking for — and prompt movement is
            # the whole basis of telling acceptance from silent refusal.
            if cls == edit_class or _editish(cls):
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


def _looks_like_checkbox(el) -> bool:
    """Is this UIA element a checkbox? Asked of the element, never of its name.

    Two spellings because pywinauto reports the control type differently depending on
    version and backend (`element_info.control_type` -> "CheckBox";
    `friendly_class_name()` -> "CheckBox" / "Check Box"). Both are cheap and either one
    being right is enough."""
    for getter in (lambda: el.element_info.control_type,
                   lambda: el.friendly_class_name()):
        try:
            t = str(getter() or "")
        except Exception:
            continue
        if t.replace(" ", "").lower() == "checkbox":
            return True
    return False


def _element_name(el) -> str:
    try:
        return (el.window_text() or "").strip()
    except Exception:
        return ""


def _element_rect(el):
    try:
        r = el.rectangle()
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        return None


def _describe_element(el) -> dict:
    """One UIA element flattened to plain data. Never raises — a control that refuses to
    answer a property reports that property empty rather than aborting the walk, because a
    reconnaissance dump that dies halfway is worth less than a partial one."""
    d: dict = {}
    try:
        info = el.element_info
    except Exception as e:
        return {"error": f"no element_info: {type(e).__name__}"}
    try:
        d["control_type"] = str(info.control_type or "")
    except Exception:
        d["control_type"] = ""
    d["name"] = _element_name(el)
    for key, attr in (("automation_id", "automation_id"), ("class_name", "class_name")):
        try:
            d[key] = str(getattr(info, attr, "") or "")
        except Exception:
            d[key] = ""
    try:
        v = el.get_value()
        d["value"] = str(v).strip() if v is not None else ""
    except Exception:
        d["value"] = ""
    d["rect"] = _element_rect(el)
    for key, get in (("enabled", el.is_enabled), ("visible", el.is_visible)):
        try:
            d[key] = bool(get())
        except Exception:
            d[key] = None
    d["focused"] = _element_has_keyboard_focus(el)
    d["toggle"] = _element_toggle_state(el)
    return d


def _whole_dollars(value) -> Optional[str]:
    """'29476.71' -> '29477', the way Drake stores money on the W-2 screen. None if the
    value is not a plain decimal number, or already has no cents to round.

    ROUND_HALF_UP, not Python's round(), which is banker's rounding and would turn 0.5 to
    the nearest EVEN — 2448.50 would become 2448 while Drake makes it 2449, and the check
    would then report a value that is genuinely on the form as missing. Measured against
    Drake on the first real W-2: .71 .92 .56 .72 all rounded up, .41 .31 down.
    """
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    s = str(value if value is not None else "").strip().replace(",", "")
    if "." not in s:
        return None                       # nothing to round; the exact match already ran
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return str(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _element_has_keyboard_focus(el) -> Optional[bool]:
    """Does this UIA element hold the keyboard? None when the toolkit will not say.

    'No' and 'we could not tell' must not look alike. Drake's data-entry canvas is WPF —
    ONE window hosting every box — so the FOCUSED HWND is identical whether the caret is
    on a box or on none of them. Asserting the hwnd had changed (the first attempt at this)
    made the caret re-arm pass or fail on where focus happened to be beforehand: it worked
    when focus sat on the app frame and failed when it was already on the canvas, which is
    exactly the flapping seen live on 2026-08-05. The element itself is the only thing that
    knows, and it answers: False before set_focus, True after.
    """
    try:
        return bool(el.element_info.element.CurrentHasKeyboardFocus)
    except Exception:
        pass
    try:
        return bool(el.has_keyboard_focus())
    except Exception:
        return None


def _element_toggle_state(el) -> Optional[bool]:
    """True (ticked) / False (clear) / None (the element will not say).

    Three ways of asking, because which one answers depends on how the control is
    implemented: the Toggle pattern is what WPF and WinForms checkboxes expose, and the
    legacy IAccessible STATE_SYSTEM_CHECKED bit is what an owner-drawn control that only
    bridges MSAA has. None is returned when NONE of them answered — never a default of
    False, which would read as "the box is clear" and let a tick that never happened be
    committed as one that did."""
    for getter in (lambda: el.get_toggle_state(),
                   lambda: el.iface_toggle.CurrentToggleState):
        try:
            v = getter()
        except Exception:
            continue
        if v in (0, 1):            # 2 = indeterminate: a real answer, but not one to act on
            return bool(v)
    try:
        leg = el.legacy_properties() or {}
        if "State" in leg:
            return bool(int(leg["State"]) & 0x10)   # STATE_SYSTEM_CHECKED
    except Exception:
        pass
    return None


# A ticked checkbox is a solid accent-coloured square. Measured on the live build
# (w2-after.png, field 47): a 14x14 blob of RGB(0,103,192) filling 180 of its 196 pixels,
# with nothing else on the popup within reach of the test — the next largest blue blob was
# 9 pixels of text anti-aliasing.
_TICK_MIN_SIDE = 8       # 100% DPI draws ~14px; below this it is anti-aliasing, not a glyph
_TICK_MAX_SIDE = 34      # 200% DPI draws ~28px; above this it is a picture, not a checkbox
_TICK_MIN_FILL = 0.80    # the white tick eats into it, so it is never solid — measured 0.918
_TICK_MIN_CORNER = 0.5   # a SQUARE fills its corners; a round icon does not — see below
_TICK_MIN_AREA = 60


def _is_tick_blue(p) -> bool:
    """Strongly blue, the way an accent-coloured fill is — not the way anti-aliased black
    text on a white background is."""
    r, g, b = p[0], p[1], p[2]
    return b > 90 and (b - r) > 55 and (b - g) > 35


def _find_tick_glyph(img):
    """The bounding box of a ticked-checkbox glyph in this image, or None.

    Connected components of accent-blue pixels, kept only if the blob is a FILLED SQUARE of
    checkbox size. Shape is what separates a tick from the other blue things on a Drake
    screen, and it has to be more than "roughly square": Drake's toolbar has a 22x22 round
    blue Help icon, which passes every size and aspect test there is. Measured on real
    screenshots of both —

        ticked checkbox   fill 0.918   corners 0.67 0.78 0.78 0.89
        Help icon         fill 0.651   corners 0.24 0.24 0.32 0.32

    — so the discriminator is that a square FILLS ITS CORNERS and a circle does not. The
    production read only ever grabs the popup's own rectangle, which no toolbar is inside;
    this is the second line of defence, for the day a grab catches something else."""
    w, h = img.size
    px = img.load()
    seen = set()
    for y in range(h):
        for x in range(w):
            if (x, y) in seen or not _is_tick_blue(px[x, y]):
                continue
            stack, comp = [(x, y)], []
            seen.add((x, y))
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (1, 1), (-1, -1), (1, -1), (-1, 1)):
                    nx, ny = cx + dx, cy + dy
                    if (0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen
                            and _is_tick_blue(px[nx, ny])):
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            if len(comp) < _TICK_MIN_AREA:
                continue
            xs = [p[0] for p in comp]
            ys = [p[1] for p in comp]
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            if not (_TICK_MIN_SIDE <= bw <= _TICK_MAX_SIDE
                    and _TICK_MIN_SIDE <= bh <= _TICK_MAX_SIDE):
                continue
            if not (0.6 <= bw / bh <= 1.7):
                continue
            if len(comp) / float(bw * bh) < _TICK_MIN_FILL:
                continue
            if not _corners_filled(comp, min(xs), min(ys), bw, bh):
                continue
            return [min(xs), min(ys), bw, bh]
    return None


def _corners_filled(comp, x0, y0, bw, bh) -> bool:
    """Does this blob reach into all four corners of its bounding box?

    True for a square (a checkbox), false for a circle (an icon). The corner cell is a
    quarter of the shorter side so it scales with DPI, and it is judged at half occupancy
    rather than fully, because the checkbox's own corners are slightly rounded and the
    yellow caption highlight blends into them."""
    pts = set(comp)
    k = max(2, min(bw, bh) // 4)
    for cx, cy in ((0, 0), (bw - k, 0), (0, bh - k), (bw - k, bh - k)):
        hit = sum(1 for j in range(k) for i in range(k)
                  if (x0 + cx + i, y0 + cy + j) in pts)
        if hit / float(k * k) < _TICK_MIN_CORNER:
            return False
    return True


_CHECKBOX_ON = {"x", "1", "y", "yes", "true", "t", "on", "checked", "check", "tick", " ",
                "{space}"}
_CHECKBOX_OFF = {"0", "n", "no", "false", "f", "off", "unchecked", "clear", "blank"}


def _as_checkbox_desired(value) -> Optional[bool]:
    """What state does this value ask a checkbox to end up in? True / False / None (not a
    yes-or-no answer, so the caller must halt rather than guess at a tick)."""
    s = str(value).strip().lower()
    if s in _CHECKBOX_ON:
        return True
    if s in _CHECKBOX_OFF:
        return False
    return None


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
