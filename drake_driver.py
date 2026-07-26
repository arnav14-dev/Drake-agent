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
except Exception:  # pragma: no cover - importable on non-Windows for reading only
    Application = None  # type: ignore
    send_keys = None  # type: ignore

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

    def __init__(self, binding: dict, *, key_pause: float = 0.03, dry_run: bool = False):
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
        self.key_pause = key_pause
        self.dry_run = dry_run
        self.app = None
        self.win = None

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Attach to the already-running Drake process (UIA backend)."""
        if self.dry_run:
            return
        if Application is None:
            raise RuntimeError("pywinauto is not available (run on the Windows VM)")
        self.app = Application(backend="uia").connect(title_re=self.title_re, timeout=20)
        self.win = self.app.top_window()
        self.win.set_focus()

    def _keys(self, chord: str) -> None:
        if self.dry_run:
            print(f"[dry-run] send_keys({chord!r})")
            return
        send_keys(chord, pause=self.key_pause, with_spaces=True)

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
        screen, field = target["screen"], target["field"]
        try:
            fb = self._field_binding(screen, field)
            if self.dry_run:
                print(f"[dry-run] focus {screen}/{field}")
                return {"ok": True}
            if fb.get("automation_id"):
                self._edit_by_auto_id(fb["automation_id"]).set_focus()
            elif fb.get("field_no") is not None:
                # Drake heads-down: toggle, jump to the field number, confirm.
                self._keys(self.nav.get("headsdown_toggle", "^n"))
                self._keys(str(fb["field_no"]) + self.nav.get("headsdown_jump_suffix", "{ENTER}"))
            elif fb.get("tab_index") is not None:
                self._keys("{TAB}" * int(fb["tab_index"]))
            else:
                return {"ok": False, "error": f"{screen}/{field} is not bound (fill binding.json)"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def type_text(self, target: dict, text: str, opts: Optional[dict] = None) -> dict:
        opts = opts or {}
        screen, field = target["screen"], target["field"]
        try:
            fb = self._field_binding(screen, field)
            if self.dry_run:
                print(f"[dry-run] type {screen}/{field} = {text!r} (opts={opts})")
                return {"ok": True}
            auto_id = fb.get("automation_id")
            if auto_id:
                ctrl = self._edit_by_auto_id(auto_id)
                ctrl.set_focus()
                if opts.get("clearFirst"):
                    try:
                        ctrl.set_edit_text("")
                    except Exception:
                        self._keys(self.nav.get("field_clear", "^a{BACKSPACE}"))
                # send_keys types literal text; escape pywinauto's special chars.
                self._keys(_escape_keys(text))
            else:
                if opts.get("clearFirst"):
                    self._keys(self.nav.get("field_clear", "^a{BACKSPACE}"))
                self._keys(_escape_keys(text))
            if opts.get("commit"):
                self._keys(self.nav.get("field_commit", "{ENTER}"))
            return {"ok": True}
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
        img = self.win.capture_as_image()  # pywinauto returns a PIL image
        x, y, w, h = (int(n) for n in box)
        crop = img.crop((x, y, x + w, y + h))
        # psm 7 = "treat the crop as a single line" — right for one field's value.
        text = pytesseract.image_to_string(crop, config="--psm 7").strip()
        return text or None

    def save_screenshot(self, path: str) -> dict:
        """Save a PNG of the live Drake window to disk — the human-verify floor and the
        source you read OCR boxes off. (screenshot() returns base64 for the wire;
        this writes a file for a person to open.)"""
        try:
            if self.dry_run or self.win is None:
                return {"ok": False, "path": None, "error": "no window"}
            self.win.capture_as_image().save(path, format="PNG")
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
            img = self.win.capture_as_image()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return {"ok": True, "pngBase64": base64.b64encode(buf.getvalue()).decode(), "takenAt": _now()}
        except Exception as e:
            return {"ok": False, "pngBase64": None, "takenAt": _now(), "error": str(e)}


# --- module helpers ---------------------------------------------------------

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
