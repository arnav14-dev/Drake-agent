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

import sys

import drake_driver
from drake_driver import DrakeDriver

POPUP_EDIT_HWND = 0xE117
POPUP_STATIC_HWND = 0xE118   # the prompt label — a Static, never a typing target
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


# Box 13. CONFIRMED live (2026-08-03, field 47): these fields have NO text box in the value
# stage — the popup shows the tick box itself, captioned with the field's name. The caption
# is all the popup's text channels can see, and the tick is a GLYPH that appears in none of
# them. That is the whole reason the text gates could not pass this field.
CHECKBOX_FIELDS = {46: "Statutory employee", 47: "Retirement plan", 48: "Sick pay"}


def CHECKBOX_PROMPT(n) -> str:
    return f"{n} {CHECKBOX_FIELDS[int(n)]}"


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
                 inert_fields=None, inert_clears: bool = False,
                 edit_class: str = "Edit", popup_has_edit: bool = True,
                 surface_readable: bool = True, surface_jitter: bool = False,
                 surface_reads_before_blind: int | None = None,
                 checkbox_tokens=("X",), checkbox_behaviour: str = "set",
                 checkbox_uia: bool = True, checkbox_uia_state: bool = True,
                 checkbox_pixel: bool = True, checkbox_pixel_lies: bool = False,
                 checkbox_pretick: bool = False, checkbox_flicker: bool = False,
                 jump_delay_reads: int = 0, inert_after_field: int | None = None):
        self.model = model
        # --- Box 13 checkbox fields ------------------------------------------------
        # Which tokens THIS build's checkbox accepts. Empty models a build that ignores
        # every one of them, which must halt rather than commit an untouched box.
        self.checkbox_tokens = set(checkbox_tokens)
        # "set"    an accepted token always ticks it (Drake's classic X)
        # "toggle" an accepted token flips it — so sending a second one CLEARS it, which is
        #          what makes verifying between tokens rather than after them load-bearing
        self.checkbox_behaviour = checkbox_behaviour
        # Does the popup expose the checkbox to accessibility at all, and if so will it
        # report the tick STATE? A control can be visible to UIA and still refuse to answer
        # the Toggle pattern, which is a different failure from not being there.
        self.checkbox_uia = checkbox_uia
        self.checkbox_uia_state = checkbox_uia_state
        # Is the tick glyph on screen for the pixel channel? checkbox_pixel_lies models a
        # stale/incorrect screen read that DISAGREES with accessibility — neither may be
        # picked over the other, so nothing may be committed.
        self.checkbox_pixel = checkbox_pixel
        self.checkbox_pixel_lies = checkbox_pixel_lies
        # Does Drake arrive at the field with the box already ticked? Unknown on the real
        # build and it matters enormously: if it does, sending a token would CLEAR it.
        self.checkbox_pretick = checkbox_pretick
        # A state channel that JITTERS: every other read comes back ticked when the box is
        # not. The tick equivalent of the repaint noise the text path already models — Drake
        # repaints asynchronously, and an accessibility read taken mid-repaint hands back a
        # state that the next read contradicts. No single frame may be acted on, and a
        # channel that never says the same thing twice running must never converge at all.
        self.checkbox_flicker = checkbox_flicker
        self._flicker_n = 0
        # Drake BUSY: the jump is accepted but does not land for this many reads of the
        # popup. Committing a field can fire Drake's employer-database lookup and auto-fill,
        # which blocks its UI thread — so the popup sits on the number prompt for a while
        # and then moves. Indistinguishable from a refusal except by waiting long enough,
        # which is exactly what the live run of 2026-08-04 got wrong on field 14.
        self.jump_delay_reads = jump_delay_reads
        self._pending_jump = None
        # After committing THIS field, Drake leaves the popup on screen but keeps the
        # keyboard on the data-entry window — the confirmed post-EIN state. The only way
        # out is to close the inert popup and open a fresh one (Ctrl+N twice), which is
        # what the founder found by hand.
        self.inert_after_field = inert_after_field
        self.popup_inert = False
        # A screen on which NO box will take the caret. Drake always has one when a return
        # is open, but the recovery must be provable to fail safely, not only to succeed.
        self.no_focusable_field = False
        # Ctrl+N does nothing even WITH a caret — heads-down switched off in Drake's setup.
        # The caret restores, the chord is heard, and still no popup appears.
        self.ctrl_n_dead = False
        self.checks: dict[int, bool] = {}     # committed tick state, per field
        self.pending_check: bool | None = None  # what the popup is SHOWING, not yet committed
        # Every reading taken while a tick box was up. The tick must never appear in any of
        # them as TEXT — that is the property the live build has and the reason the text
        # gates could not verify field 47. Asserted on, so a fake that started leaking the
        # token would fail the case rather than quietly make the old path work again.
        self.checkbox_renders: list[str] = []
        # What the toolkit NAMES the popup's text box. "Edit" is plain Win32; a Delphi build
        # says "TEdit", .NET says "WindowsForms10.EDIT.app.0.378734a". The driver must find
        # the box by shape, because asking pywinauto for class_name="Edit" is exact-match and
        # found nothing on the live machine ("popup edit not ready / no handle: timed out").
        self.edit_class = edit_class
        # False models the CONFIRMED Drake 2025 shape: the popup owns no child controls at
        # all — EnumChildWindows returns [] — because Drake paints the box onto the dialog.
        # The popup itself is then the keyboard target, and the only way to read it is UIA
        # (if Drake exposes anything) or OCR of the screen.
        self.popup_has_edit = popup_has_edit
        # Can any channel read a painted popup on this build? False = every read returns
        # nothing, which must make entry HALT rather than commit a keystroke it never saw.
        self.surface_readable = surface_readable
        # Reads before the channel goes permanently silent, or None for "never". Models a
        # channel that DIES MID-FIELD: the field number verifies, and then there is nothing
        # left to baseline the prompt with. The number must not be committed in that state.
        self.surface_reads_before_blind = surface_reads_before_blind
        self._surface_reads = 0
        # Model a screen-read channel: the text is right but the punctuation/spacing jitters
        # frame to frame, exactly as OCR does. Nothing may turn on a stray comma.
        self.surface_jitter = surface_jitter
        self._jitter_n = 0
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
        # The buttons that dialog offers. Named, because the driver clicks a button BY NAME
        # rather than pressing Enter on an unseen default.
        self.dialog_buttons: list[str] = ["OK"]
        self.extra_windows: list[dict] = []  # windows appearing MID-RUN (chat expand, toasts)
        self.swallowed_ctrl_n = 0
        self.advanced: set[int] = set()  # auto-advance fires once per field, on commit
        self.log: list[str] = []
        self.typed: list[str] = []   # every string the driver sent, in order
        self.keys: list[str] = []    # every CHORD the driver sent, in order

    # -- what the operator sees ---------------------------------------------

    def _tick_pending_jump(self) -> None:
        """A jump Drake has accepted but not finished. Counted in READS rather than seconds
        so the fake stays deterministic — every observation of the popup moves it along."""
        if self._pending_jump is None:
            return
        n, left = self._pending_jump
        if left > 1:
            self._pending_jump = (n, left - 1)
            return
        self._pending_jump = None
        self.awaiting_value_for, self.popup_text = n, ""
        if n in CHECKBOX_FIELDS:
            self.pending_check = bool(self.checks.get(n, False)) or self.checkbox_pretick
        self.log.append(f"jump -> field {n} LANDED late (Drake was busy)")

    def prompt(self) -> str:
        self._tick_pending_jump()
        if not self.prompts or not self.popup_open:
            return ""
        if self.awaiting_value_for is not None:
            if self.awaiting_value_for in CHECKBOX_FIELDS:
                # The live shape: a checkbox field's popup shows the number and the field's
                # caption, and NOTHING that reflects the tick. Deliberately no English
                # "enter the value" prompt — this build does not print one, and a fake that
                # invented one would hand the driver a signal the real popup never gives it.
                return CHECKBOX_PROMPT(self.awaiting_value_for)
            return VALUE_PROMPT(self.awaiting_value_for)
        return NUMBER_PROMPT

    def is_checkbox_stage(self) -> bool:
        return (self.popup_open and self.awaiting_value_for is not None
                and self.awaiting_value_for in CHECKBOX_FIELDS)

    def uia_checkboxes(self, hwnd):
        """What _read_popup_checkbox_uia would return: a list, or None for 'the channel
        could not answer'. [] is the meaningful middle — UIA looked and there is no
        checkbox, i.e. this field is a text box."""
        if not self.popup_open or int(hwnd) != POPUP_HWND or not self.checkbox_uia:
            return None
        if not self.is_checkbox_stage():
            return []
        state = self.pending_check if self.checkbox_uia_state else None
        if self.checkbox_flicker:
            self._flicker_n += 1
            if self._flicker_n % 2:
                state = True    # a mid-repaint frame, contradicted by the very next read
        return [{"name": CHECKBOX_FIELDS[self.awaiting_value_for], "state": state,
                 "rect": [40, 30, 56, 46]}]

    def pixel_tick(self, hwnd):
        """What _read_popup_checkbox_pixels would return: True, or None. NEVER False —
        the real channel cannot tell an unticked checkbox from a text box, and a fake that
        answered False here would be more generous than the thing it stands in for."""
        if not self.popup_open or int(hwnd) != POPUP_HWND or not self.checkbox_pixel:
            return None
        if self.checkbox_pixel_lies:
            return True
        return True if (self.is_checkbox_stage() and self.pending_check) else None

    def render(self) -> str:
        """The popup AS SEEN — prompt and typed text in one string, which is all a screen
        read or an accessibility read of a painted window can offer. There is no separate
        'edit contents' to ask for; that is the whole difficulty."""
        if not self.popup_open:
            return ""
        text = " ".join(p for p in (self.prompt(), self.popup_text) if p)
        if self.is_checkbox_stage():
            self.checkbox_renders.append(text)
        if not self.surface_jitter:
            return text
        self._jitter_n += 1
        if self.surface_jitter == "repaint":
            # LIVE FAILURE (field 1, first write-w2 run): Drake repaints between reads and
            # the accessibility read intermittently comes back with NOTHING. Alternating is
            # the honest model of a slow channel against a busy app — it is what makes "two
            # consecutive readings that agree" hard to get, and it is the case the driver
            # got wrong: _settle_surface skipped unreadable frames (so the field NUMBER
            # verified fine), while _stable_prompt recorded them as a reading of '' — which
            # discarded the good reading either side, produced no baseline, and halted with
            # "could not read the popup at all after jumping to field 1".
            return "" if self._jitter_n % 2 == 0 else text
        if self.surface_jitter == "garbage":
            # What OCR does more often than going silent: it returns SOMETHING, and the
            # something is punctuation. Non-empty, so the channel reports it as a reading,
            # but it carries no words — normalising it leaves nothing. It must be discarded
            # for the same reason an absent reading is, or it clobbers the good frame beside
            # it and no baseline ever forms.
            return "|_. -~" if self._jitter_n % 2 == 0 else text
        if self.surface_jitter == "chars":
            # The noise OCR actually makes: every third frame a LETTER comes back wrong.
            # Normalising punctuation does not absorb this, so it is what proves the
            # "two consecutive identical readings" rule is load-bearing — one bad frame
            # must never be read as "Drake changed what it is asking".
            return text.replace("enter", "entcr", 1) if self._jitter_n % 3 == 0 else text
        # Punctuation/spacing noise: same words, unstable layout.
        return (text.replace(".", ",") if self._jitter_n % 2 else text.replace(" ", "  "))

    def children(self, hwnd: int) -> list:
        """The popup's child windows as EnumChildWindows would report them."""
        if int(hwnd) == ERROR_HWND and self.error_dialog:
            # A real Drake modal: the message in a Static, the answers as named Buttons.
            return [{"hwnd": 0xB000, "class_name": "Static", "text": self.error_dialog,
                     "visible": True, "enabled": True, "rect": [8, 8, 340, 40]}] + [
                {"hwnd": 0xB001 + i, "class_name": "Button", "text": b,
                 "visible": True, "enabled": True, "rect": [8 + 90 * i, 60, 80, 24]}
                for i, b in enumerate(self.dialog_buttons)]
        if int(hwnd) != POPUP_HWND or not self.popup_open:
            return []
        if not self.popup_has_edit:
            # A painted popup has NO children AT ALL — not even a Static holding the prompt.
            # That is what the live probe reported (popup_controls: []), and it matters: an
            # earlier version of this fake left the Static in place, which quietly fed the
            # driver clean prompt text through a path the real build does not have. Every
            # painted case passed while the screen-reading path it was meant to exercise
            # was barely used.
            return []
        return [{"hwnd": POPUP_STATIC_HWND, "class_name": "Static", "text": self.prompt(),
                 "visible": True, "enabled": True, "rect": [8, 8, 300, 18]},
                {"hwnd": POPUP_EDIT_HWND, "class_name": self.edit_class,
                 "text": self.popup_text, "visible": True, "enabled": True,
                 "rect": [8, 30, 300, 22]}]

    def backspace(self, n: int = 1):
        if self.is_checkbox_stage():
            return                      # there is no text on a tick box to delete
        if self.awaiting_value_for is not None or self.popup_open:
            self.popup_text = self.popup_text[:-n] if n < len(self.popup_text) else ""
        elif self.pending_canvas_text:
            self.pending_canvas_text = self.pending_canvas_text[:-n]

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
        if self.ctrl_n_dead:
            self.log.append("ctrl+n ignored (heads-down disabled in setup)")
            return
        self.popup_open = not self.popup_open
        self.popup_text = ""
        self.awaiting_value_for = None
        if not self.popup_open:
            self.popup_inert = False     # the stale window is gone
        self.log.append(f"ctrl+n -> popup {'OPEN' if self.popup_open else 'CLOSED'}"
                        f"{' (inert)' if self.popup_inert else ''}")

    def type(self, text: str):
        if self.error_dialog:
            return
        # Every string the driver ever sent, in order. Asserting on the OUTCOME is not
        # enough: a driver that mistakes a refusal for an acceptance types the value anyway
        # and still ends up halting — on the invalid-field modal its own mistake raised.
        # The interesting question is what it TYPED, not just how it finished.
        self.typed.append(text)
        if self.is_checkbox_stage():
            # A tick box swallows the keystroke: an accepted token moves the TICK and puts
            # no character anywhere. So there is nothing for a text read to find — which is
            # exactly why counting characters could never verify one of these fields.
            self._flicker_left = self.checkbox_flicker
            if text in self.checkbox_tokens:
                self.pending_check = (not self.pending_check
                                      if self.checkbox_behaviour == "toggle" else True)
                self.log.append(f"token {text!r} -> field {self.awaiting_value_for} tick is now "
                                f"{'ON' if self.pending_check else 'OFF'} (pending)")
            else:
                self.log.append(f"token {text!r} -> IGNORED by the checkbox")
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
            # The stale window is GONE, so its inertness goes with it — same as the Ctrl+N
            # close path. Leaving the flag set would model a popup that is closed and still
            # not holding the keyboard, which is not a state Drake can be in.
            self.popup_inert = False
            self.log.append("ESC -> popup closed (disarmed)")

    def focus_canvas_field(self) -> bool:
        """A data-entry box is asked to take focus through the accessibility tree.

        This is what actually re-arms Ctrl+N — MEASURED, after Esc and Tab were both tried
        live and neither worked (Esc cannot reach a popup that holds no keyboard; Tab did
        not reliably give any box the caret). `no_focusable_field` models a screen where
        nothing takes it, so the recovery can be proven to FAIL safely too.

        Focusing a box does not alter its contents, which is why this is allowed to happen
        without a human — unlike a click, it also has no coordinate to get wrong.
        """
        if self.error_dialog or self.no_focusable_field:
            return False
        self.canvas_focus = ENTER_ORDER[0]
        self.log.append(f"FOCUS -> caret restored on field {self.canvas_focus}")
        return True

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
            elif self.jump_delay_reads:
                # Accepted, but Drake is busy: the popup stays on the number prompt until
                # the auto-fill finishes. Nothing distinguishes this from a refusal except
                # waiting.
                self._pending_jump = (n, self.jump_delay_reads)
                self.log.append(f"jump -> field {n} ACCEPTED but Drake is busy "
                                f"({self.jump_delay_reads} reads)")
            else:
                self.awaiting_value_for, self.popup_text = n, ""
                if n in CHECKBOX_FIELDS:
                    # The tick box arrives showing the field's CURRENT state — or already
                    # ticked, on a build that pre-ticks whatever field you jumped to.
                    self.pending_check = bool(self.checks.get(n, False)) or self.checkbox_pretick
                    self.log.append(f"jump -> field {n} (popup now showing the TICK BOX, "
                                    f"{'ticked' if self.pending_check else 'clear'})")
                else:
                    self.pending_check = None
                    self.log.append(f"jump -> field {n} (popup now prompting for the VALUE)")
            return
        if self.popup_open and self.awaiting_value_for is not None:
            n = self.awaiting_value_for
            if n in CHECKBOX_FIELDS:
                verdict = self.value_validator(n, "X" if self.pending_check else "")
                if verdict == "dialog":
                    self.error_dialog = f"Invalid entry for field {n}"
                    self.log.append(f"tick for {n} -> VALIDATOR DIALOG")
                    return
                if verdict == "silent":
                    self.log.append(f"tick for {n} -> SILENTLY REFUSED, still on the tick box")
                    return
                self.checks[n] = bool(self.pending_check)
                self.values[n] = "X" if self.pending_check else ""
                self.committed.add(n)
                self.awaiting_value_for, self.pending_check = None, None
                self.log.append(f"tick for {n} committed as "
                                f"{'CHECKED' if self.checks[n] else 'clear'}, popup back to "
                                f"the number prompt")
                return
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
            if n == self.inert_after_field:
                self.popup_inert = True
                self.log.append(f"value for {n} committed -> popup left INERT "
                                f"(on screen, keyboard back on the canvas)")
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
        # Every chord, verbatim. Asserting on fake.log was unreliable — the persistent model
        # logs what an Enter DID ("value committed…"), never the key itself, so "no Enter was
        # pressed" passed whether or not one had been. A test that cannot fail is worse than
        # no test.
        self.fake.keys.append(chord)
        if chord == "{ENTER}":
            self.fake.enter()
        elif chord == "{ESC}":
            self.fake.esc()
        elif chord.startswith("{BACKSPACE"):
            n = chord.rstrip("}").split()
            self.fake.backspace(int(n[1]) if len(n) > 1 else 1)
        else:
            self.fake.type(chord.replace("{", "").replace("}", "")
                           if chord.startswith("{") and chord.endswith("}") and len(chord) == 3
                           else _unescape(chord))

    def headsdown_toggle(self, method="scancode"):
        self.fake.ctrl_n()
        return {"ok": True, "method": method}

    def _find_popup_hwnd(self, timeout: float = 0.0):
        """Stands in for the ctypes window enumeration. Returning a HANDLE (not a
        title-matched spec) keeps the production resolution path — handle -> w32.window(
        handle=...) -> spec — under test, which is where the anchored-regex bug lived."""
        return (POPUP_HWND, "pid") if self.fake.popup_open else (None, "")

    def _read_popup_uia(self, popup_hwnd):
        """The accessibility channel. Overriding the CHANNEL rather than
        _read_popup_surface keeps the driver's real channel ordering, its (text, channel)
        contract and every gate built on them in the tested path."""
        if not self.fake.popup_open or int(popup_hwnd) != POPUP_HWND:
            return None
        if not self.fake.surface_readable:
            return None
        lim = self.fake.surface_reads_before_blind
        self.fake._surface_reads += 1
        if lim is not None and self.fake._surface_reads > lim:
            return None
        # None, never '' — a channel with nothing to say must be ABSENT. The real channel
        # honours this ('.join(parts) or None'), and the distinction is the whole point:
        # '' is a claim that the popup is empty, and the gates would act on it.
        return self.fake.render() or None

    def _read_popup_ocr(self, popup_hwnd):
        return None      # Tesseract absent unless a case says otherwise

    def _read_popup_checkbox_uia(self, popup_hwnd):
        """The accessibility CHANNEL for a tick, overridden at the same boundary as the text
        channels — so _read_checkbox_state, _settle_checkbox, the drift guard and the whole
        of _enter_checkbox are the production code under test."""
        return self.fake.uia_checkboxes(popup_hwnd)

    def _read_popup_checkbox_pixels(self, popup_hwnd):
        return self.fake.pixel_tick(popup_hwnd)

    def _focus_canvas_field(self):
        # The UIA tree walk itself is live-only; what the suite proves is the SEQUENCE the
        # driver builds on it — that the caret is restored before Ctrl+N, again after the
        # stale popup closes, and that a screen which refuses focus halts instead of typing.
        return self.fake.focus_canvas_field()

    def _focused_hwnd(self):
        if not self.fake.popup_open:
            return (MAIN_HWND, 0, (0, 0, 0, 0), 1)
        # An INERT popup: on screen, but the keyboard is still held by the data-entry
        # window. Confirmed live right after Drake's employer auto-fill commits the EIN.
        if self.fake.popup_inert:
            return (MAIN_HWND, 0, (0, 0, 0, 0), 1)
        focus = POPUP_EDIT_HWND if self.fake.popup_has_edit else POPUP_HWND
        return (focus, 0, (0, 0, 0, 0), 1)


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
    # The child enumeration itself, so the REAL _popup_prompt_text / _resolve_popup_edit /
    # _rank_popup_edit run — including the prompt filter that must drop edit-shaped children.
    drake_driver._enum_child_summaries = lambda h, cap=64: fake.children(int(h))


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
        # Handle-addressed, exactly as the driver resolves the popup, its edit and a
        # dialog's buttons.
        h = int(kwargs.get("handle") or 0)
        if h == POPUP_EDIT_HWND:
            return _FakeEdit(self.fake)
        if 0xB001 <= h <= 0xB00F:
            return _FakeButton(self.fake, h)
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


class _FakeButton:
    """A dialog button. Clicking the FIRST one closes the dialog; clicking any other is
    modelled as 'the dialog is still up', because a wrong button is not a dismissal."""

    def __init__(self, fake, hwnd):
        self.fake = fake
        self.handle = hwnd

    def wrapper_object(self):
        return self

    def click(self):
        idx = self.handle - 0xB001
        if 0 <= idx < len(self.fake.dialog_buttons):
            self.fake.log.append(f"clicked dialog button {self.fake.dialog_buttons[idx]!r}")
            if idx == 0:
                self.fake.error_dialog = None


class _FakeEdit:
    def __init__(self, fake):
        self.fake = fake
        self.handle = POPUP_EDIT_HWND

    def wait(self, *a, **k):
        return self

    def wrapper_object(self):
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


def case_inert_popup_after_ein_is_recycled():
    """THE EIN CATCH, confirmed live 2026-08-04 and then by hand: after the employer EIN
    commits, Drake's auto-fill finishes with the heads-down popup STILL ON SCREEN but the
    keyboard back on the data-entry window. The popup is present and inert.

    "Is the popup up?" is the wrong question in that state — keys would land on the canvas.
    The recovery is Ctrl+N twice (drop the stale window, open a live one), which the founder
    found by hand and which is NOT the double-toggle bug: that bug is a blind second chord
    fired at a HEALTHY popup, and the next case pins that it still cannot happen."""
    fake, drv = _new("persistent", autoadvance_from=None, inert_after_field=4)
    rows = _run_seq(drv, [("4", "123456789"), ("5", "TEST EMPLOYER LLC"), ("23", "52000")])
    checks = [
        ("every field went in", all(r[2].get("ok") for r in rows)),
        ("values landed in the right boxes",
         fake.values == {4: "123456789", 5: "TEST EMPLOYER LLC", 23: "52000"}),
        ("the inert popup was recycled, not typed into",
         any("popup left INERT" in l for l in fake.log)),
        ("it ended holding the keyboard", not fake.popup_inert),
    ]
    ok = all(v for _, v in checks)
    _check("inert popup after the EIN is closed and re-opened, then entry continues", ok,
           "\n".join(f"HALT at {n}: {r.get('reason')}" for n, v, r in rows if not r.get("ok"))
           + f"\nvalues={fake.values}\nlog:\n" + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_healthy_popup_is_never_double_toggled(label="", **kw):
    """The other half of the same rule. A popup that IS holding the keyboard must never be
    sent a second Ctrl+N — that is the original cascade: the chord closes heads-down, the
    next field NUMBER types onto the canvas, and every value after it lands one box out
    while every row reports OK.

    So the recycle must fire on the INERT state only, and the way to prove that is to count
    the chords: a healthy batch sends exactly one Ctrl+N, for the very first field."""
    fake, drv = _new("persistent", autoadvance_from=None, **kw)
    rows = _run_seq(drv, SEQ[:4])
    ctrl_n = sum(1 for l in fake.log if l.startswith("ctrl+n"))
    ok = (all(r[2].get("ok") for r in rows) and ctrl_n == 1)
    return _check(f"a healthy popup is never toggled a second time{label}", ok,
                  f"ctrl+n chords={ctrl_n} (want 1)\nlog:\n"
                  + "\n".join("  " + l for l in fake.log))


def case_slow_jump_is_not_a_refusal():
    """THE LIVE HALT of 2026-08-04, field 14. Drake ACCEPTED the number and then took its
    time landing the jump, because committing the employer block fires Drake's
    employer-database lookup and auto-fill, which blocks its UI thread.

    At a 2.5s budget the driver called that a refusal and stopped the batch — and the
    screenshot taken moments later showed the popup sitting on field 14's value box, jump
    complete. A busy app and a declined field number look identical; the only thing that
    tells them apart is waiting long enough. So the budget is a BUSY budget, and it costs
    nothing on a healthy field: the loop returns the instant the prompt moves."""
    fake, drv = _new("persistent", autoadvance_from=None, jump_delay_reads=40)
    res = drv.headsdown_type("14", "JOHN")
    checks = [
        ("the late jump is entered, not called a refusal", bool(res.get("ok"))),
        ("the value landed in the right box", fake.values.get(14) == "JOHN"),
        ("the jump is recorded as late", any("LANDED late" in l for l in fake.log)),
    ]
    ok = all(v for _, v in checks)
    _check("a slow jump (Drake busy auto-filling) is not a refusal", ok,
           f"{res.get('reason')}\nvalues={fake.values}\nlog:\n"
           + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


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


def case_main_frame_disabled_before_we_attached():
    """THE LIVE HALT of 2026-08-04: Drake's main frame was already disabled when the agent
    attached, and every field halted with "main frame DISABLED by a modal — window not
    identified". There was no dialog: the screenshot taken at the halt shows a clean W-2
    screen, and every window in the process was in the baseline. Drake simply nests its
    data-entry screens and puts WS_DISABLED on the frames behind them.

    A disabled frame we INHERITED is furniture, exactly like the chat overlay — the gate's
    own founding rule. What must still halt is a frame that becomes disabled DURING the
    run, which is the case above."""
    fake = FakeDrake(model="per-jump", autoadvance_from=None)
    fake.canvas_focus = 4
    _install_readers(fake)
    drv = _ModalSim(fake)
    drv.modal = True                 # already modal BEFORE the baseline is taken
    # The REAL attach-time snapshot, not a hand-set flag — otherwise the recording of the
    # flag is untested and can be deleted with the suite still green.
    drv._snapshot_baseline()
    drv.begin_batch()
    first = drv.headsdown_type("4", "123456789")
    # Per-jump: the value sits on the canvas until the NEXT jump commits it (no trailing
    # Enter, by design), so read it there rather than from the committed values.
    landed = fake.pending_canvas_text
    # ...and a real validator arriving later is still caught, on the same run.
    fake.extra_windows.append({"hwnd": 0xBAD2, "title": "Drake 2025", "class_name": "#32770",
                               "visible": True, "enabled": True, "owner": MAIN_HWND,
                               "style": 0x80C80000, "rect": [500, 400, 360, 140],
                               "_text": "This field must contain data"})
    second = drv.headsdown_type("5", "TEST EMPLOYER LLC")
    checks = [
        ("the inherited disabled frame does NOT halt the run", bool(first.get("ok"))),
        ("the field was actually entered", landed == "123456789"),
        ("a validator arriving later still halts",
         second.get("ok") is False and second.get("halt") is True),
        ("and nothing was entered after it", 5 not in fake.values),
    ]
    ok = all(v for _, v in checks)
    _check("a main frame disabled BEFORE attach is furniture, not a modal", ok,
           f"first={first}\nsecond={second}\nvalues={fake.values}")
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


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


def case_anchored_title_regex():
    """REGRESSION: pywinauto matches `title_re` with `re.match` — ANCHORED at the start
    (findwindows.py:274-281) — while every hand-rolled check in the driver uses
    `re.search`. The two disagreed, so `_find_headsdown_popup` returned None for a popup
    that was on screen AND focused, and probe-popup reported 'popup not open — click a
    Drake field first' about a window the operator was staring at.

    It also silently voided `_ensure_popup_open`'s central promise. That function claims it
    can never toggle an open popup back off because it re-checks presence before every
    Ctrl+N — but presence was UNDETECTABLE, so the retry loop could fire Ctrl+N into an
    already-open popup and close it.

    Two assertions: the real titles must be found the way we look them up, and the source
    must not reach for pywinauto's title_re to locate a window."""
    import re
    import pathlib
    from drake_driver import DrakeDriver

    d = DrakeDriver({"navigation": {}, "capabilities": {}})
    popup_title = "Drake 2025 - Heads Down Data Entry"   # observed live
    frame_title = "Drake 2025 - Data Entry (123456789 - fynn, Test) - (CONTAINS SENSITIVE DATA)"
    raw = (pathlib.Path(__file__).parent / "drake_driver.py").read_text()
    # Code only: the comments deliberately quote the broken call so the trap stays
    # documented, and a structural check must not trip over its own explanation.
    src = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#"))

    checks = [
        ("popup title IS found by re.search (how we look it up)",
         bool(re.search(d.popup_title_re, popup_title, re.I))),
        ("popup title is NOT found by re.match (pywinauto's way — the bug)",
         not re.match(d.popup_title_re, popup_title, re.I)),
        ("frame title is NOT found by re.match either (same trap on connect)",
         not re.match(d.title_re, frame_title, re.I)),
        ("driver never RESOLVES a single window via pywinauto title_re",
         "window(title_re=" not in src),
        ("the one remaining title_re use is a best-effort pool with a full fallback",
         "self.app.windows(title_re=self.title_re)" in src
         and "pools.append(self.app.windows())" in src),
        ("popup is resolved by handle instead",
         "window(handle=" in src),
        ("connect has a re.search fallback when the anchored match fails",
         "_connect_uia" in src and "connect(process=" in src),
    ]
    ok = all(v for _, v in checks)
    _check("anchored-regex trap: popup and frame are findable the way we search", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_edit_class_is_not_literally_edit():
    """REGRESSION (live halt #2): the popup's text box is only called "Edit" if Drake's
    toolkit happens to name it that. pywinauto's class_name= criterion is EXACT string
    equality (findwindows.py:257) and its class_name_re= is the same anchored re.match, so
    `popup.child_window(class_name="Edit")` found nothing on the live machine — probe-popup
    returned the criteria dict as its 'reason', and write-w2 died on the FIRST field with
    "popup edit not ready / no handle: timed out".

    A Delphi build names it "TEdit", .NET names it "WindowsForms10.EDIT.app.0.378734a".
    Entry must work on all of them."""
    ok = True
    for cls in ("TEdit", "WindowsForms10.EDIT.app.0.378734a", "RichEdit20W", "TMaskEdit"):
        fake, drv = _new("persistent", edit_class=cls)
        rows = _run_seq(drv, SEQ)
        fake._commit_canvas()
        good = fake.values == EXPECTED and all(r[2].get("ok") for r in rows)
        ok = ok and good
        print(f"    {'ok  ' if good else 'FAIL'}: popup edit of class {cls!r} is found and typed into"
              + ("" if good else "  -> " + str([r[2].get("reason") for r in rows if not r[2].get("ok")])))
    return _check("popup edit is found by SHAPE, not by the literal class name 'Edit'", ok)


def case_painted_popup_enters(label="", **kw):
    """CONFIRMED Drake 2025: the heads-down popup owns NO child windows — EnumChildWindows
    returns [] because Drake paints the box onto the dialog itself, exactly as it does the
    data-entry grid. So the popup IS the keyboard target, and the box can only be read as a
    whole (accessibility tree or screen).

    A full W-2 must still land in the right boxes, with every commit still verified."""
    fake, drv = _new("persistent", popup_has_edit=False, **kw)
    rows = _run_seq(drv, SEQ)
    fake._commit_canvas()
    ok = fake.values == EXPECTED and all(r[2].get("ok") for r in rows)
    return _check(f"painted popup (no child HWND) enters a full W-2{label}", ok,
                  f"expected {EXPECTED}\ngot      {fake.values}\n"
                  + "\n".join(f"HALT at {n}: {r.get('reason')}" for n, v, r in rows if not r.get("ok")))


def case_painted_popup_unreadable():
    """The same painted popup on a build where NOTHING can read it — no accessibility text,
    no OCR installed.

    Every gate in this driver is built on reading the box back before the irreversible
    Enter. With no channel there is no gate, so the only honest move is to stop — and to
    name the one thing that would fix it, because "cannot verify" with no remedy reads as a
    dead end when it is an install away."""
    fake, drv = _new("persistent", popup_has_edit=False, surface_readable=False)
    res = drv.headsdown_type("23", "52000")
    reason = str(res.get("reason") or "")
    checks = [
        ("halts instead of typing blind", res.get("ok") is False and res.get("halt") is True),
        ("NOTHING was committed", fake.values == {} and fake.committed == set()),
        ("no Enter was pressed on the unverified number",
         not any("ENTER" in l for l in fake.log)),
        ("says the popup owns no child window", "no child window" in reason),
        ("names the remedy (Tesseract / the screen-reading channel)",
         "Tesseract" in reason and "tesseract_cmd" in reason),
    ]
    ok = all(v for _, v in checks)
    _check("painted popup that nothing can read -> HALT before Enter, with the remedy", ok, reason)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_painted_popup_channel_dies_before_the_baseline():
    """The read channel works long enough to verify the field NUMBER, then goes silent —
    so there is no steady reading to baseline the prompt against.

    Both a driver that checks and one that does not end up halting, so the halt alone proves
    nothing. What distinguishes them is WHEN: with the check, the number has only been
    typed, and Drake is left exactly as it was found. Without it, Enter is pressed on a
    field whose outcome cannot then be read — the number is committed and the run discovers
    afterwards that it is blind."""
    fake, drv = _new("persistent", popup_has_edit=False, surface_reads_before_blind=4)
    res = drv.headsdown_type("23", "52000")
    reason = str(res.get("reason") or "")
    checks = [
        ("halts", res.get("ok") is False and res.get("halt") is True),
        ("the Enter was NEVER pressed", "{ENTER}" not in fake.keys),
        ("nothing committed", fake.values == {} and fake.committed == set()),
        ("says the reading would not settle", "steady reading" in reason),
    ]
    ok = all(v for _, v in checks)
    _check("painted popup: no steady baseline -> stop BEFORE the Enter, not after", ok,
           f"{reason}\nlog:\n" + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_painted_popup_silent_refusal(label="", **kw):
    """An inert box on a painted popup. The number is typed and shown, Drake declines it
    silently, and the ONLY tell is that the popup's text never moves off the number prompt.
    A screen read has to be good enough to catch that, or the value goes in one box late.

    Run again with character-level screen noise: one frame in three comes back with a letter
    wrong, which LOOKS like the prompt changing. Reading a single differing frame as "Drake
    took the number" would type the value into a field Drake never moved to."""
    fake, drv = _new("persistent", popup_has_edit=False, **kw)
    res = drv.headsdown_type("13", "SOME FOREIGN POSTAL")
    # The value must never be TYPED, not merely never committed. A driver that reads one
    # noisy frame as "the prompt moved" types the value at the number prompt and then still
    # halts — on the invalid-field modal its own mistake raised. Asserting only on the halt
    # cannot tell that apart from getting it right.
    ok = (res.get("ok") is False and res.get("halt") is True
          and fake.values == {} and 13 not in fake.committed
          and "SOME FOREIGN POSTAL" not in fake.typed)
    return _check(f"painted popup: a silently refused field number still HALTs{label}", ok,
                  f"{res.get('reason')}\nvalues={fake.values}\ntyped={fake.typed}")


def case_painted_popup_keystrokes_vanish():
    """The keys never arrive — Drake ignored the injection, or the popup lost focus between
    the check and the keystroke. The popup still SHOWS its prompt, and that prompt names the
    field number ("Enter the value for field 1…"), so "is the number visible?" is not the
    question. What must be true is that the popup shows it ONE MORE TIME than before we
    typed. Nothing typed, nothing showing, no Enter."""
    fake, drv = _new("persistent", popup_has_edit=False, echo=lambda t: "")
    res = drv.headsdown_type("23", "52000")
    ok = (res.get("ok") is False and res.get("halt") is True
          and fake.values == {} and "{ENTER}" not in fake.keys)
    return _check("painted popup: vanished keystrokes -> HALT, Enter never pressed", ok,
                  f"{res.get('reason')}\nvalues={fake.values}\nlog:\n"
                  + "\n".join("  " + l for l in fake.log))


def case_painted_popup_value_corrupted():
    """What lands in a painted popup is not what we typed. The field NUMBER is fine, so the
    jump happens and Drake is armed for the value — the corruption has to be caught by
    reading the popup back, in the window between typing the value and the Enter that
    commits it."""
    fake, drv = _new("persistent", popup_has_edit=False,
                     echo=lambda t: "9999" if t == "52000" else t)
    res = drv.headsdown_type("23", "52000")
    ok = (res.get("ok") is False and res.get("halt") is True
          and fake.values.get(23) is None)
    return _check("painted popup: value corrupted in flight -> HALT before commit", ok,
                  f"{res.get('reason')}\nvalues={fake.values}")


def case_surface_token_counting():
    """Reading a painted popup returns the PROMPT and the typed text in one string, so
    "does it contain the value" is not a test: Drake's value prompt names the field
    ("Enter the value for field 1…"). Presence would pass on the prompt's own '1' when
    nothing was typed, and would refuse the legitimate entry of the value '1'. Counting
    occurrences against the pre-typing baseline distinguishes them."""
    fake, drv = _new("persistent", popup_has_edit=False)
    base = "Enter the value for field 1 and press enter."
    rows = [
        ("the prompt's own '1' is not proof the value landed", base, "1", 1),
        ("typing '1' into that prompt IS visible as one more", base + " 1", "1", 2),
        ("'5' does not match inside '52000'", "prompt 52000", "5", 0),
        ("'52000' matches as a whole token", "prompt 52000", "52000", 1),
        # Drake renders money with thousands commas and OCR adds punctuation of its own, so
        # a match may span several tokens — as long as their concatenation is exactly right.
        ("Drake's '52,000' matches the typed 52000", "value: 52,000.", "52000", 1),
        ("a multi-word value matches across its tokens",
         "Enter the value for field 5 and press enter. TEST EMPLOYER LLC",
         "TEST EMPLOYER LLC", 1),
        ("a run may not START mid-token", "value 152000", "52000", 0),
        ("a run may not END mid-token", "value 520005", "52000", 0),
    ]
    ok = True
    for name, text, tok, want in rows:
        got = drv._surface_count(text, tok)
        good = got == want
        ok = ok and good
        print(f"    {'ok  ' if good else 'FAIL'}: {name} (count {got}, want {want})")
    # And the gate built on it: a settle that only sees the baseline's own token fails.
    settled, _t, _c = drv._settle_surface(POPUP_HWND, "1", base, timeout=0.3)
    good = not settled
    ok = ok and good
    print(f"    {'ok  ' if good else 'FAIL'}: settle refuses when the token was already there")
    return _check("painted popup: token counting, not substring matching", ok)


WARNING = ("There are fields on this screen that must contain data if you are planning to "
           "e-file this return. To enter this data now, click OK.")


# --- Box 13 checkboxes ------------------------------------------------------
#
# THE LIVE HALT of 2026-08-03. Nineteen fields went in clean and field 47 stopped the run:
# the popup had swapped its text box for a tick box, the X had landed, the box was ticked
# on screen — and the gate, which counts how many times the typed token appears in the
# popup's TEXT, could never see a glyph. The driver was right to refuse; the model was
# wrong. These cases pin the model that replaced it: read the tick, not the character.


def _cb(field=47, value="X", kind="checkbox", **kw):
    fake, drv = _new("persistent", popup_has_edit=False, **kw)
    res = drv.headsdown_type(str(field), value, kind=kind)
    return fake, drv, res


def _after_jump(fake) -> list:
    """The keys sent AFTER the field-number Enter — i.e. everything that happened at the
    value stage. The number stage legitimately clears its box and presses Enter, so an
    assertion about the VALUE stage has to start here or it is asserting about the wrong
    half of the field."""
    return fake.keys[fake.keys.index("{ENTER}") + 1:] if "{ENTER}" in fake.keys else []


def case_checkbox_ticks_and_commits():
    """The field that halted the live run, done properly: the token flips the tick, the
    TICK is read back (not the character), and only then is it committed."""
    fake, drv, res = _cb()
    after = _after_jump(fake)
    checks = [
        ("committed as ticked", fake.checks.get(47) is True and 47 in fake.committed),
        ("the token was sent", "X" in fake.typed),
        ("the token went in BEFORE the committing Enter",
         after[:1] == ["X"] and after[-1:] == ["{ENTER}"]),
        # The proof that this cannot be passing on the old text path: the popup NEVER shows
        # the token. If a future change made the text gate the thing being satisfied here,
        # there is nothing for it to count. Sampled at the tick stage, not after the
        # commit, because by then the popup is back on the number prompt.
        ("the popup never showed the token as text", "X" not in fake.checkbox_renders),
        ("no backspaces were sent at the tick box",
         not any(k.startswith("{BACKSPACE") for k in after)),
        ("reports what confirmed it", "confirmed via" in str(res.get("read_back"))),
    ]
    ok = res.get("ok") is True and all(v for _, v in checks)
    _check("checkbox: the tick is read back off the widget, then committed", ok,
           f"{res}\ntick-stage reads={fake.checkbox_renders!r}\nkeys={fake.keys}\nlog:\n"
           + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_token_ignored_halts():
    """Drake ignores every token this build knows. The box stays clear, so there is nothing
    to commit — and committing anyway would report a ticked Box 13 that is not ticked, which
    is a wrong tax return that every downstream check would call verified."""
    fake, drv, res = _cb(checkbox_tokens=())
    checks = [
        ("halts", res.get("ok") is False and res.get("halt") is True),
        ("the committing Enter was NEVER pressed", fake.keys.count("{ENTER}") == 1),
        ("nothing committed", fake.checks == {} and 47 not in fake.committed),
        ("names the tokens it tried", "'X'" in str(res.get("reason"))),
        ("says where to put the right one",
         "headsdown_checkbox_tokens" in str(res.get("reason"))),
    ]
    ok = all(v for _, v in checks)
    _check("checkbox: no token flips the tick -> HALT with the box untouched", ok,
           str(res.get("reason")))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_unreadable_halts():
    """Neither channel can see the tick: no accessibility checkbox, no glyph on screen.
    Same rule as every other gate here — an unverifiable keystroke is not committed — and
    the halt has to name the remedy, because "cannot verify" with no way forward reads as a
    dead end when it is one binding line away."""
    fake, drv, res = _cb(checkbox_uia=False, checkbox_pixel=False)
    reason = str(res.get("reason"))
    checks = [
        ("halts", res.get("ok") is False and res.get("halt") is True),
        ("the committing Enter was NEVER pressed", fake.keys.count("{ENTER}") == 1),
        ("nothing committed", fake.checks == {}),
        ("names the probe", "probe-checkbox" in reason),
        ("names the pixel-channel escape hatch", "headsdown_checkbox_channels" in reason),
    ]
    ok = all(v for _, v in checks)
    _check("checkbox: nothing can read the tick -> HALT before Enter, with the remedy", ok, reason)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_already_ticked_types_nothing():
    """Drake arrives with the box ALREADY ticked — either because the return already has it
    or because this build pre-ticks the field you jump to.

    Sending the token there is a coin flip: on a build where it toggles, it CLEARS a tick a
    human put in, and the run reports success. So when the state is already the one asked
    for, nothing is typed at all."""
    fake, drv, res = _cb(checkbox_pretick=True, checkbox_behaviour="toggle")
    checks = [
        ("committed", res.get("ok") is True and fake.checks.get(47) is True),
        ("NO token was typed", "X" not in fake.typed),
        ("the arrival state is reported", (res.get("checkbox") or {}).get("arrival") is True),
        ("no tokens tried", (res.get("checkbox") or {}).get("tokens_tried") == []),
    ]
    ok = all(v for _, v in checks)
    _check("checkbox: already in the wanted state -> type NOTHING, just commit", ok,
           f"{res}\ntyped={fake.typed}\nlog:\n" + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_pixel_only():
    """A build that exposes no checkbox to accessibility at all. The tick glyph on screen is
    then the only evidence there is — and it is enough, because the state that has to be
    PROVEN is the ticked one, and a tick is exactly what that channel can see."""
    fake, drv, res = _cb(checkbox_uia=False, checkbox_pixel=True)
    ok = (res.get("ok") is True and fake.checks.get(47) is True
          and (res.get("checkbox") or {}).get("verified_by") == "pixel")
    return _check("checkbox: screen glyph alone is enough to confirm a tick", ok,
                  f"{res}\nlog:\n" + "\n".join("  " + l for l in fake.log))


def case_checkbox_uia_present_but_mute():
    """The checkbox is in the accessibility tree but will not report its Toggle state. That
    is not a reading of 'clear' — it is no reading — so the screen channel has to carry it,
    and the field must still go in."""
    fake, drv, res = _cb(checkbox_uia=True, checkbox_uia_state=False, checkbox_pixel=True)
    ok = (res.get("ok") is True and fake.checks.get(47) is True
          and (res.get("checkbox") or {}).get("verified_by") == "pixel")
    return _check("checkbox: element visible but mute -> the screen channel carries it", ok,
                  f"{res}\nlog:\n" + "\n".join("  " + l for l in fake.log))


def case_checkbox_channels_disagree():
    """Accessibility says clear, the screen says ticked. One of them is wrong and there is
    no way to know which, so neither is picked: the pair is discarded and the field halts.
    Picking the convenient one is how a box that was never ticked gets reported as ticked."""
    fake, drv, res = _cb(checkbox_pixel_lies=True, checkbox_tokens=())
    ok = (res.get("ok") is False and res.get("halt") is True
          and fake.checks == {} and fake.keys.count("{ENTER}") == 1)
    return _check("checkbox: channels disagree -> no reading, no commit", ok,
                  f"{res.get('reason')}\nchecks={fake.checks}\nkeys={fake.keys}")


def case_checkbox_commit_silently_refused():
    """The tick is showing and verified, the Enter goes in — and Drake silently declines it
    and stays on the tick box. No dialog, nothing on screen to see. Reported as success, the
    NEXT field's number would be typed at this field's tick box."""
    fake, drv, res = _cb(value_validator=lambda n, v: "silent" if n == 47 else None)
    ok = (res.get("ok") is False and res.get("halt") is True
          and fake.checks == {} and 47 not in fake.committed
          and "did not accept the tick" in str(res.get("reason")))
    return _check("checkbox: a silently refused tick still HALTs", ok,
                  f"{res.get('reason')}\nchecks={fake.checks}")


def case_checkbox_token_escalation():
    """'X' does nothing on this build but '1' works. Escalation is allowed BECAUSE each
    token is verified before the next is sent — the alternative, firing all three and
    looking afterwards, would leave a toggling build in whatever state the last one made."""
    fake, drv, res = _cb(checkbox_tokens=("1",))
    cb = res.get("checkbox") or {}
    ok = (res.get("ok") is True and fake.checks.get(47) is True
          and cb.get("tokens_tried") == ["X", "1"])
    return _check("checkbox: escalates to the token this build takes, verifying each", ok,
                  f"{res}\nlog:\n" + "\n".join("  " + l for l in fake.log))


def case_checkbox_toggle_not_double_flipped():
    """On a build where the token TOGGLES, the first one already did the job. Sending the
    next one 'to be sure' would untick it again — and the box would be committed clear while
    the run reported it ticked. The loop stops the moment the tick is right."""
    fake, drv, res = _cb(checkbox_tokens=("X", "1", "{SPACE}"), checkbox_behaviour="toggle")
    cb = res.get("checkbox") or {}
    ok = (res.get("ok") is True and fake.checks.get(47) is True
          and cb.get("tokens_tried") == ["X"] and fake.typed.count("X") == 1)
    return _check("checkbox: a toggling build is never double-flipped", ok,
                  f"{res}\ntyped={fake.typed}")


def case_checkbox_drift_guard():
    """Drake shows a tick box for a field the map calls money. The field numbers have moved
    on this build — every value after this one would land somewhere else — so the run stops
    before typing anything, rather than posting 52000 at a checkbox."""
    fake, drv, res = _cb(field=47, value="52000", kind="money")
    ok = (res.get("ok") is False and res.get("halt") is True
          and "52000" not in fake.typed and fake.checks == {}
          and "moved" in str(res.get("reason")))
    return _check("checkbox where the map expects money -> HALT as build drift", ok,
                  f"{res.get('reason')}\ntyped={fake.typed}")


def case_checkbox_map_wrong_field_is_text():
    """The mirror image: the map says field 23 is a checkbox, but Drake gives it a text box.
    No tick ever appears, so nothing is committed — the same drift caught from the other
    side, and without the driver having to be told which side it is on."""
    fake, drv, res = _cb(field=23, value="X", kind="checkbox")
    ok = (res.get("ok") is False and res.get("halt") is True
          and 23 not in fake.committed and fake.keys.count("{ENTER}") == 1)
    return _check("map says checkbox, Drake shows a text box -> HALT, nothing committed", ok,
                  f"{res.get('reason')}\ncommitted={fake.committed}\nkeys={fake.keys}")


def case_checkbox_per_jump_refused():
    """A build where the popup closes on the jump would put the caret on the CANVAS
    checkbox, and the canvas exposes nothing at all — no window, no value, no rectangle to
    look at. A tick typed there could never be confirmed, so it is not typed."""
    fake, drv = _new("per-jump", popup_has_edit=False)
    res = drv.headsdown_type("47", "X", kind="checkbox")
    ok = (res.get("ok") is False and res.get("halt") is True
          and "X" not in fake.typed and fake.checks == {}
          and "by hand" in str(res.get("reason")))
    return _check("checkbox on a per-jump build -> refuse, it cannot be read back", ok,
                  f"{res.get('reason')}\ntyped={fake.typed}")


class _FakeImage:
    """The bare surface _find_tick_glyph uses (size + load()), so the glyph detector can be
    tested with no Pillow and no Drake — it is pure pixel logic and deserves a table."""

    def __init__(self, w, h, pixels):
        self.size = (w, h)
        self._px = pixels

    def load(self):
        return self._px


def _img(w, h, shapes):
    px = {(x, y): (242, 240, 242) for x in range(w) for y in range(h)}
    for (x0, y0, sw, sh, colour, kind) in shapes:
        for y in range(y0, y0 + sh):
            for x in range(x0, x0 + sw):
                if kind == "circle":
                    cx, cy, r = x0 + sw / 2, y0 + sh / 2, sw / 2
                    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                        continue
                if kind == "cut-corners":
                    # A square with a quarter of each corner taken off: still square-ish,
                    # still mostly filled, but its CORNERS are empty. The one shape that
                    # isolates the corner test from the fill test.
                    i, j, c = x - x0, y - y0, min(sw, sh) // 4
                    if (min(i, sw - 1 - i) + min(j, sh - 1 - j)) < c:
                        continue
                px[(x, y)] = colour
    return _FakeImage(w, h, px)


def case_checkbox_glyph_table():
    """What counts as a tick, and what does not. The colour alone is not the test — a Drake
    screen has other blue on it — so the glyph is identified by SHAPE: square-ish, checkbox
    sized, and mostly filled. The accent colour and the 14x14 size are measured off the live
    build (w2-after.png, field 47)."""
    ACCENT = (0, 103, 192)
    rows = [
        ("a ticked checkbox (14x14 accent square, as measured live)",
         _img(300, 94, [(84, 56, 14, 14, ACCENT, "rect")]), True),
        ("an empty popup is not a tick", _img(300, 94, []), False),
        ("a blue hyperlink run is not a tick (too thin)",
         _img(300, 94, [(20, 40, 127, 13, (0, 102, 204), "rect")]), False),
        ("the Live Chat bubble is not a tick (too big, and round)",
         _img(300, 94, [(10, 10, 56, 56, (26, 115, 232), "circle")]), False),
        # A REAL false positive, found by running the detector over an earlier screenshot:
        # Drake's toolbar Help button is a 22x22 blue circle, which passes every size and
        # aspect test there is. Measured fill 0.651 and corner occupancy 0.24-0.32 against
        # the real tick's 0.918 and 0.67-0.89.
        ("Drake's round blue Help icon (22x22) is not a tick",
         _img(300, 94, [(40, 20, 22, 22, (0, 122, 204), "circle")]), False),
        ("a rounded blue square is not a tick either — its corners are empty",
         _img(300, 94, [(40, 20, 16, 16, (0, 103, 192), "cut-corners")]), False),
        ("anti-aliasing specks are not a tick",
         _img(300, 94, [(10, 10, 2, 3, ACCENT, "rect"), (40, 20, 2, 4, ACCENT, "rect")]), False),
        ("black text is not a tick", _img(300, 94, [(20, 20, 14, 14, (0, 0, 0), "rect")]), False),
        ("the yellow caption highlight is not a tick",
         _img(300, 94, [(20, 20, 90, 16, (253, 255, 147), "rect")]), False),
        ("a tick at 200% DPI (28x28) still reads",
         _img(300, 94, [(84, 30, 28, 28, ACCENT, "rect")]), True),
    ]
    ok = True
    for name, img, want in rows:
        got = drake_driver._find_tick_glyph(img) is not None
        ok = ok and got == want
        print(f"    {'ok  ' if got == want else 'FAIL'}: {name} -> {got}")
    return _check("tick glyph is identified by SHAPE, not just colour", ok)


def case_checkbox_flicker_is_not_proof():
    """The state channel JITTERS: every other read says the box is ticked when it is not.
    A mid-repaint accessibility read looks exactly like a successful tick, and acting on a
    single one commits a Box 13 that is not ticked while reporting it verified.

    Two consecutive agreeing readings is the same drain proof the text path uses, and here
    it is what makes a channel that never says the same thing twice converge on NOTHING —
    which is the honest answer. Without it the tick is committed on whichever frame happened
    to be flattering, and the box is left clear on the return.

    Deliberately jitters the ARRIVAL read too. A one-shot lie right after the token is not
    enough to prove the guard: the re-prove immediately before the Enter catches that one on
    its own, so a test built on it passes with the convergence rule deleted."""
    fake, drv, res = _cb(checkbox_tokens=(), checkbox_flicker=True)
    checks = [
        ("halts", res.get("ok") is False and res.get("halt") is True),
        ("the committing Enter was NEVER pressed", fake.keys.count("{ENTER}") == 1),
        ("nothing committed", fake.checks == {} and 47 not in fake.committed),
    ]
    ok = all(v for _, v in checks)
    _check("checkbox: a jittering state channel converges on nothing", ok,
           f"{res.get('reason')}\nchecks={fake.checks}\nkeys={fake.keys}")
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_untick_needs_a_read_first():
    """Clearing a tick is the one direction the screen channel cannot carry: an unticked
    checkbox and a plain text box are pixel-identical, so "no tick visible" is not evidence
    that a tick box is there and clear.

    Asked to untick with only that channel available, the honest move is to type NOTHING.
    Firing tokens at a box whose state cannot be read is how a build where the token
    TOGGLES ends up ticking the very box that was meant to be cleared."""
    fake, drv, res = _cb(value="0", checkbox_uia=False, checkbox_pixel=True,
                         checkbox_behaviour="toggle",
                         checkbox_tokens=("X", "1", "{SPACE}"))
    checks = [
        ("halts", res.get("ok") is False and res.get("halt") is True),
        ("NO token was fired at a box it cannot read",
         not any(t in fake.typed for t in ("X", "1", "{SPACE}"))),
        ("nothing committed", fake.checks == {}),
        ("says the state was never read", "actually read" in str(res.get("reason"))),
    ]
    ok = all(v for _, v in checks)
    _check("checkbox: an untick will not start from a state nothing could read", ok,
           f"{res.get('reason')}\ntyped={fake.typed}")
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_probe_measures_and_leaves_clean():
    """`probe-checkbox` is the first thing that runs on the live machine, and an untested
    probe is exactly the wasted trip this suite exists to prevent.

    It has to answer three questions — is this a checkbox, what state does it arrive in,
    which token flips it — and then leave Drake exactly as it found it: tick restored, popup
    closed, and NO Enter, because Enter is what would write the value."""
    fake, drv = _new("persistent", popup_has_edit=False, checkbox_behaviour="toggle")
    res = drv.probe_checkbox_field(field_no="47", flip=True)
    checks = [
        ("it ran", res.get("ok") is True),
        ("identifies the field as a checkbox", res.get("is_checkbox_field") is True),
        ("reports the arrival state", (res.get("arrival_state") or {}).get("ticked") is False),
        ("finds the token that flips it", res.get("token_that_worked") == "X"),
        ("says whether it sets or toggles", res.get("token_behaviour") == "toggle"),
        ("puts the tick back", (res.get("restored") or {}).get("ok") is True),
        ("committed NOTHING", fake.checks == {} and fake.committed == set()),
        ("never pressed Enter on the tick", fake.keys.count("{ENTER}") == 1),
        ("left the popup closed", res.get("popup_open_after_disarm") is False),
    ]
    ok = all(v for _, v in checks)
    _check("probe-checkbox measures the build and leaves Drake untouched", ok,
           f"{res}\nkeys={fake.keys}\nlog:\n" + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_checkbox_desired_table():
    """What a value means to a tick box. Anything that is not a yes-or-no answer must come
    back as None, so the caller halts instead of picking a state for the taxpayer."""
    rows = [("X", True), ("x", True), ("1", True), ("true", True), ("YES", True),
            ("{SPACE}", True), ("0", False), ("false", False), ("no", False),
            ("off", False), ("52000", None), ("D", None), ("", None)]
    ok = True
    for val, want in rows:
        got = drake_driver._as_checkbox_desired(val)
        ok = ok and got is want
        print(f"    {'ok  ' if got is want else 'FAIL'}: {val!r} -> {got}")
    return _check("checkbox values resolve to tick / clear / not-an-answer", ok)


def case_known_dialog_dismissed_by_name():
    """Drake's e-file completeness warning is the normal state of a half-keyed W-2, and it
    blocks entry. It may be answered automatically — but by CLICKING A NAMED BUTTON, never
    by pressing Enter on whichever button happens to be the default: one of the answers
    leaves the data-entry screen, after which every remaining field number addresses
    something else."""
    fake, drv = _new("persistent")
    fake.error_dialog = WARNING
    fake.dialog_buttons = ["OK", "Cancel"]
    res = drv.headsdown_type("23", "52000")
    clicked = [l for l in fake.log if "clicked dialog button" in l]
    checks = [
        ("the field was entered after the warning cleared",
         res.get("ok") is True and fake.values.get(23) == "52000"),
        ("it clicked OK, by name", clicked == ["clicked dialog button 'OK'"]),
        ("it is recorded, not silent", any("auto-dismissed" in n for n in drv.benign_notes)),
    ]
    ok = all(v for _, v in checks)
    _check("known warning dialog is dismissed by clicking its NAMED button", ok,
           f"{res.get('reason')}\nlog:\n" + "\n".join("  " + l for l in fake.log))
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_unknown_dialog_still_halts():
    """Auto-dismissal is a list of specific dialogs, not a policy. Anything else — a
    validator rejecting a value, a question about the return — still stops for a human, and
    a known dialog whose button is missing gets no keystroke either."""
    fake, drv = _new("persistent")
    fake.error_dialog = "Invalid entry: this field requires a numeric value."
    res = drv.headsdown_type("23", "52000")
    a = (res.get("ok") is False and res.get("halt") is True
         and not any("clicked" in l for l in fake.log))

    fake2, drv2 = _new("persistent")
    fake2.error_dialog = WARNING
    fake2.dialog_buttons = ["Continue", "Abandon"]   # named differently on this build
    res2 = drv2.headsdown_type("23", "52000")
    reason2 = str((res2.get("dialog") or {}).get("dismiss_attempt") or "")
    b = (res2.get("ok") is False and not any("clicked" in l for l in fake2.log)
         and "Continue" in reason2 and "Enter" in reason2)

    ok = a and b
    _check("unknown dialogs still halt, and a missing button is never a blind Enter", ok,
           f"unknown: {res.get('reason')}\nno-button: {reason2}")
    print(f"    {'ok  ' if a else 'FAIL'}: an unrecognised dialog halts, nothing clicked")
    print(f"    {'ok  ' if b else 'FAIL'}: known dialog + no matching button -> halt, buttons listed")
    return ok


def case_edit_ranking_table():
    """_rank_popup_edit in isolation: a Static prompt label is never a typing target, and
    focus only breaks ties between boxes — it must not promote a label."""
    from drake_driver import _rank_popup_edit

    def kid(h, cls, **kw):
        d = {"hwnd": h, "class_name": cls, "visible": True, "enabled": True, "text": ""}
        d.update(kw)
        return d

    STATIC, EDIT, EDIT2 = kid(1, "Static"), kid(2, "Edit"), kid(3, "TEdit")
    rows = [
        ("plain Edit wins over the Static label", [STATIC, EDIT], None, 2),
        ("TEdit is found when nothing is called 'Edit'", [STATIC, EDIT2], None, 3),
        ("exact configured class beats another edit", [EDIT2, EDIT], None, 2),
        ("focus breaks ties between two edit-shaped boxes", [EDIT2, kid(4, "TEdit")], 4, 4),
        ("a Static NEVER wins, even holding focus", [STATIC, EDIT], 1, 2),
        ("a focused non-edit child is used only when there is no box", [STATIC, kid(5, "Button")], 5, 5),
        ("labels only -> no target (halt)", [STATIC, kid(6, "Button")], None, None),
        ("no children -> no target (halt)", [], None, None),
        ("a disabled edit is not a target", [kid(7, "Edit", enabled=False)], None, None),
        ("a hidden edit is still a target when it is all there is",
         [kid(8, "Edit", visible=False)], None, 8),
    ]
    ok = True
    for name, kids, focus, want in rows:
        got, _how = _rank_popup_edit(kids, preferred_class="Edit", focused_hwnd=focus)
        good = got == want
        ok = ok and good
        print(f"    {'ok  ' if good else 'FAIL'}: {name} (got {got}, want {want})")
    return _check("popup-edit ranking: labels are never typed into", ok)


def case_prompt_excludes_the_typing_box():
    """The prompt baseline must exclude EVERY edit-shaped child, not just the one named
    "Edit". If the box we type into leaked into the "prompt", the baseline would move
    because WE typed rather than because Drake changed what it is asking for — and prompt
    movement is the entire basis for telling acceptance from silent refusal."""
    from drake_driver import _popup_prompt_text
    kids = [{"hwnd": 1, "class_name": "Static", "text": NUMBER_PROMPT,
             "visible": True, "enabled": True},
            {"hwnd": 2, "class_name": "TEdit", "text": "23", "visible": True, "enabled": True}]
    orig = drake_driver._enum_child_summaries
    drake_driver._enum_child_summaries = lambda h, cap=64: kids
    try:
        # edit_hwnd unknown (resolution not yet run) and the class not the configured one —
        # the case where the old two-rule filter let the typed digits through.
        prompt = _popup_prompt_text(1234, None, "Edit")
    finally:
        drake_driver._enum_child_summaries = orig
    checks = [("the prompt label is kept", NUMBER_PROMPT in prompt),
              ("the typed digits are NOT part of the prompt", "23" not in prompt)]
    ok = all(v for _, v in checks)
    _check("prompt baseline excludes edit-shaped children whatever they are called", ok,
           f"prompt = {prompt!r}")
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


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


def _stranded_fake():
    """The state a FINISHED run leaves behind, which the next run starts in.

    Popup still on screen, keyboard held by the canvas, and NO active caret — so Ctrl+N is
    a silent no-op and cannot even close the popup. Reproduced live 2026-08-05: run 1 wrote
    78/78 and run 2 halted on field 1 having typed nothing.
    """
    fake = FakeDrake(model="persistent", autoadvance_from=None)
    fake.popup_open = True
    fake.popup_inert = True
    fake.canvas_focus = None          # nobody clicked; this is what breaks Ctrl+N
    return fake


def case_stranded_run_rearms_caret():
    fake = _stranded_fake()
    _install_readers(fake)
    drv = SimDriver(fake)
    drv.begin_batch()
    rows = [(n, v, drv.headsdown_type(n, v)) for n, v in (("23", "52000"), ("24", "6000"))]
    focused = sum(1 for l in fake.log if l.startswith("FOCUS ->"))
    checks = [
        ("both fields entered", all(r[2].get("ok") for r in rows)),
        ("both values landed in the right boxes", fake.values == {23: "52000", 24: "6000"}),
        # TWICE, not once: closing the stale popup drops focus to nothing (measured live —
        # hwnd 0), so the caret has to go back a second time before Ctrl+N can re-open it.
        # Asserting "at least once" would pass the version that got stuck exactly there.
        ("the caret was restored twice — before the close AND after it", focused == 2),
        # Esc cannot reach a popup that holds no keyboard; it lands on whatever does. A
        # driver that sends it anyway is firing keys at a window it has not identified.
        ("no Esc was sent", fake.keys.count("{ESC}") == 0),
        ("the caret really was restored", fake.canvas_focus is not None),
        # Bound the recovery: a stuck-state fix, not a per-field habit.
        ("it recovered once, not once per field", focused == 2 and len(rows) == 2),
    ]
    ok = all(v for _, v in checks)
    _check("stranded after a previous run -> caret restored twice, entry continues", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_no_popup_no_caret_rearms():
    """The OTHER stranded shape: no popup at all, and still no caret.

    Seen live 2026-08-05 on the retry — the founder had pressed Esc, so nothing was on
    screen, and Ctrl+N still did nothing because no field held the caret. `_ensure_popup_open`
    burns all three attempts here rather than failing on a stale popup, so it is a genuinely
    different path into the same dead end and needs its own proof.
    """
    fake = FakeDrake(model="persistent", autoadvance_from=None)
    fake.popup_open = False
    fake.canvas_focus = None
    _install_readers(fake)
    drv = SimDriver(fake)
    drv.begin_batch()
    res = drv.headsdown_type("23", "52000")
    focused = sum(1 for l in fake.log if l.startswith("FOCUS ->"))
    checks = [
        ("the field was entered", bool(res.get("ok"))),
        ("the value landed in the right box", fake.values == {23: "52000"}),
        # ONCE here, against twice on the stale-popup path: there was no popup to close, so
        # there was no focus loss to repair. Pinning the exact count is what keeps the two
        # paths honest — a recovery that always focuses twice would pass a laxer check.
        ("the caret was restored exactly once", focused == 1),
        ("no Esc was sent — there was no popup to close", fake.keys.count("{ESC}") == 0),
    ]
    ok = all(v for _, v in checks)
    _check("no popup and no caret -> focusing a box re-arms, entry continues", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    if not ok:
        print(f"      res={res}\n      keys={fake.keys}\n      log={fake.log}")
    return ok


def case_rearm_that_cannot_work_halts_clean():
    """If nothing takes the caret, the recovery must HALT — not type into a dead screen.

    This is the half that matters. A recovery which only ever succeeds in the fake would
    let a real dead screen through, and the value would go to the canvas instead of the
    popup: the exact cascade every gate here exists to prevent.
    """
    fake = _stranded_fake()
    fake.no_focusable_field = True
    _install_readers(fake)
    drv = SimDriver(fake)
    drv.begin_batch()
    res = drv.headsdown_type("23", "52000")
    checks = [
        ("it halted", not res.get("ok") and bool(res.get("halt"))),
        ("NOTHING was typed", not fake.values and not fake.typed),
        ("no value was committed to the canvas either", not fake.committed),
        # Name WHICH dead end this was. "No box would take the caret" and "a popup opened
        # but the keyboard is elsewhere" need different things from the human, so a message
        # that could mean either is not a report.
        ("it says no box would take the caret",
         "take the caret" in (res.get("reason") or "")),
        # Exactly one Ctrl+N: the recycle path's legitimate first try, which is what
        # discovers the popup will not close. Once focusing a box has FAILED, no further
        # chord may be fired at that screen — firing anyway is how a field number ends up
        # typed onto the canvas instead of into the popup.
        ("no further Ctrl+N after the caret could not be restored",
         sum(1 for l in fake.log if "ctrl+n" in l) == 1),
    ]
    ok = all(v for _, v in checks)
    _check("re-arm impossible (no box takes the caret) -> clean HALT, nothing typed", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    if not ok:
        print(f"      res={res}\n      values={fake.values} typed={fake.typed}\n"
              f"      keys={fake.keys}\n      log={fake.log}")
    return ok


def case_rearm_works_but_headsdown_is_off():
    """Caret restored, chord heard, still no popup — heads-down switched off in setup.

    The recovery must not report success on the strength of its own actions. It did the
    right things; Drake did not respond; that is a halt. Distinguishing this from "no box
    would take the caret" matters because the two need completely different things from
    the human — one is a click, the other is a Drake setup option.
    """
    fake = FakeDrake(model="persistent", autoadvance_from=None)
    fake.popup_open = False
    fake.canvas_focus = None
    fake.ctrl_n_dead = True
    _install_readers(fake)
    drv = SimDriver(fake)
    drv.begin_batch()
    res = drv.headsdown_type("23", "52000")
    checks = [
        ("it halted", not res.get("ok") and bool(res.get("halt"))),
        ("NOTHING was typed", not fake.values and not fake.typed),
        ("the caret WAS restored — the recovery did its part",
         fake.canvas_focus is not None),
        ("it blames the popup not opening, not the caret",
         "still did not open the heads-down popup" in (res.get("reason") or "")),
    ]
    ok = all(v for _, v in checks)
    _check("caret restored but Ctrl+N still opens nothing -> HALT, nothing typed", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    if not ok:
        print(f"      res={res}\n      log={fake.log}")
    return ok


def case_form_check_whole_dollars():
    """Drake rounds money to whole dollars on the W-2 screen. The form check must know.

    The first REAL W-2 through the pipeline (2026-08-06) reported 9 of 25 values missing
    from the form. All nine were present and correct — they were the nine with cents, and
    Drake had rounded them: 29476.71 was sitting in Box 1 as 29477. A check that fires on
    normal behaviour is worse than no check, because people learn to ignore it.

    So the rounded form is accepted TOO — and nothing else. The second half of this case is
    the important half: the near-misses that matter must still be caught.
    """
    from drake_driver import _whole_dollars
    checks = [
        # Measured against Drake on that run: .71 .92 .56 .72 up, .41 .31 down.
        ("29476.71 -> 29477", _whole_dollars("29476.71") == "29477"),
        ("5400.92 -> 5401", _whole_dollars("5400.92") == "5401"),
        ("1827.56 -> 1828", _whole_dollars("1827.56") == "1828"),
        ("427.41 -> 427", _whole_dollars("427.41") == "427"),
        ("353.72 -> 354", _whole_dollars("353.72") == "354"),
        ("2448.31 -> 2448", _whole_dollars("2448.31") == "2448"),
        # HALF-UP, not banker's rounding. Python's round() sends .5 to the nearest EVEN, so
        # 2448.50 would become 2448 while Drake makes it 2449 — and the check would report a
        # value that is genuinely on the form as missing.
        ("2448.50 -> 2449, not 2448 (half-up, not banker's)", _whole_dollars("2448.50") == "2449"),
        ("2447.50 -> 2448 (same rule, odd side)", _whole_dollars("2447.50") == "2448"),
        ("commas are not digits: '29,476.71' -> 29477", _whole_dollars("29,476.71") == "29477"),
        # No cents means nothing to round: the exact match already ran, and returning a value
        # here would only widen what counts as a match for no reason.
        ("52000 -> None (nothing to round)", _whole_dollars("52000") is None),
        ("text -> None", _whole_dollars("CA SDI") is None),
        ("empty -> None", _whole_dollars("") is None),
        ("None -> None", _whole_dollars(None) is None),
    ]
    # The near-misses. Rounding must not become "close enough": a dropped digit, an extra
    # one, and a transposition are exactly the errors this whole project exists to catch.
    for bad in ("29470", "2947", "294770", "29477.71", "2477"):
        checks.append((f"29476.71 must NOT match {bad!r}", _whole_dollars("29476.71") != bad))
    ok = all(v for _, v in checks)
    _check("form check knows Drake rounds money to whole dollars", ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_locality_resolution():
    """Box 20 stores a CODE, not the name on the W-2.

    Pinned to a SYNTHETIC table, not the live CITY.HLP: this must fail on a machine with no
    Drake installed, and it must fail when the resolver breaks — not when Drake ships a new
    city list. The real file is only read live.
    """
    import w2_map as m
    saved_table, saved_src = m._LOCALITY_TABLE, m._LOCALITY_SOURCE
    m._LOCALITY_TABLE = {
        "PA": {"PL": "Philadelphia", "LC": "Local (Generic)", "PY": "Part Year"},
        "NY": {"NY": "New York City", "YONKERS": "Yonkers", "PY": "Part Year"},
        "IN": {"49": "MARION", "48": "MADISON", "50": "MARSHALL"},
        # Muskegon is not decoration: Drake really lists both 'Muskegon City' and
        # 'Muskegon Heights', so treating ' CITY' as a noise suffix turns a valid,
        # unambiguous locality into an ambiguous one. Without a collision like this in the
        # table, a ' CITY'-stripping bug is masked by the prefix rule and tests green.
        "MI": {"PC": "Portland City", "PH": "Port Huron", "PT": "Pontiac",
               "MC": "Muskegon City", "MH": "Muskegon Heights"},
        "CA": {"VD": "Voluntary Plan DI", "VI": "Voluntary Plan DI"},
    }
    m._LOCALITY_SOURCE = "<synthetic>"
    try:
        def code(st, raw):
            return m.resolve_locality(st, raw)["code"]

        def how(st, raw):
            return m.resolve_locality(st, raw)["how"]

        # The three values that were typed live on 2026-08-04 and stored nothing, plus the
        # codes they should have been. This is the regression the whole section exists for.
        checks = [
            ("'PHILA' -> 'PL' (was typed raw, stored nothing)", code("PA", "PHILA") == "PL"),
            ("'NYC' -> 'NY' (was typed raw, stored nothing)", code("NY", "NYC") == "NY"),
            ("'MARION' -> '49' (was typed raw, stored nothing)", code("IN", "MARION") == "49"),
            # A code that arrives already correct must pass through untouched, not get
            # re-resolved into something else by a name or prefix rule.
            ("'PL' stays 'PL' (already a code)", (code("PA", "PL"), how("PA", "PL")) == ("PL", "code")),
            ("'YONKERS' is both a code and a name -> code wins",
             (code("NY", "YONKERS"), how("NY", "YONKERS")) == ("YONKERS", "code")),
            ("case and padding are irrelevant", code("PA", "  philadelphia ") == "PL"),
            ("'Marion County' loses the noise suffix", code("IN", "Marion County") == "49"),
            # ' CITY' is NOT noise — Michigan lists 'Portland City' and stripping it would
            # silently retarget the entry.
            ("' CITY' is kept ('Portland City' is a real name)",
             code("MI", "Portland City") == "PC"),
            ("' CITY' is kept even when dropping it would collide (Muskegon City/Heights)",
             code("MI", "Muskegon City") == "MC"),
            ("unique prefix resolves ('Pont' -> Pontiac)", code("MI", "Pont") == "PT"),
            # Everything below must REFUSE. A locality that resolves to the wrong code is a
            # wrong number on a return that looks entirely plausible.
            ("ambiguous prefix refuses ('Port' matches 2)", code("MI", "Port") is None),
            ("duplicate names refuse (CA lists 2 for one name)",
             code("CA", "Voluntary Plan DI") is None),
            ("unknown name refuses", code("PA", "PITTSBURGH") is None),
            ("state with no localities refuses (TX)", code("TX", "DALLAS") is None),
            ("missing state refuses", code("", "PHILADELPHIA") is None),
            ("a refusal names the codes Drake does offer",
             sorted(m.resolve_locality("MI", "Port")["candidates"])
             == ["PC (Portland City)", "PH (Port Huron)"]),
        ]

        # No table at all (Drake not installed / moved) must disable resolution, never fall
        # back to typing the raw string — the exact failure this section was written to end.
        m._LOCALITY_TABLE = {}
        checks.append(("no CITY.HLP -> refuse, never type raw", code("PA", "PHILA") is None))
        m._LOCALITY_TABLE = {"PA": {"PL": "Philadelphia"}, "TX": {}}

        # build_plan integration: a resolved locality is ENTERED as the code; an unresolvable
        # one is diverted to hand-entry and must not reach the keyboard.
        plan = m.build_plan({"box15_state": "PA", "box20_locality": "Philadelphia",
                             "box15_state_2": "TX", "box20_locality_2": "DALLAS"})
        ent = {e["field_no"]: e for e in plan["entries"]}
        hand = {h["field_no"]: h for h in plan["hand_entry"]}
        checks += [
            ("plan enters field 63 as the CODE 'PL'", ent.get(63, {}).get("value") == "PL"),
            ("plan keeps what it resolved FROM, for review",
             ent.get(63, {}).get("resolved_from") == "PHILADELPHIA"),
            ("plan diverts the TX locality to hand-entry", 70 in hand and 70 not in ent),
            ("the diverted one says why", "no local income tax" in (hand.get(70, {}).get("why") or "")),
        ]
        ok = all(v for _, v in checks)
        _check("Box 20 locality resolves to Drake's code, or refuses", ok)
        for name, v in checks:
            print(f"    {'ok  ' if v else 'FAIL'}: {name}")
        return ok
    finally:
        m._LOCALITY_TABLE, m._LOCALITY_SOURCE = saved_table, saved_src


def _table(title, checks) -> bool:
    """Run a list of (description, boolean) and report. Shared by the navigation cases."""
    ok = all(v for _, v in checks)
    _check(title, ok)
    for name, v in checks:
        print(f"    {'ok  ' if v else 'FAIL'}: {name}")
    return ok


def case_nav_identity_from_title():
    """The gate that stands between a mis-click and 78 verified values in a stranger's
    return. Every string here is a real Drake 2025 window title from an explore dump."""
    from drake_nav import parse_data_entry_title, verify_open_return
    T = "Drake 2025 - Data Entry (123456789 - fynn, Test) - (CONTAINS SENSITIVE DATA)"
    HOME = "Drake 2025 Tax Software"
    p = parse_data_entry_title(T)
    checks = [
        ("the live title parses to id + name",
         p is not None and p["id"] == "123456789" and p["name"] == "fynn, Test"),
        # The W-2 screen's title carries a TRAILING SPACE that the menu's does not. Both are
        # the same return; a parser that keeps it would compare unequal forever.
        ("the trailing-space variant parses identically",
         parse_data_entry_title(T + " ") == p),
        ("the home screen is not a return", parse_data_entry_title(HOME) is None),
        ("a hyphenated SSN in the title normalises",
         (parse_data_entry_title("Data Entry (123-45-6789 - fynn, Test)") or {})["id"]
         == "123456789"),

        ("the right return passes", verify_open_return(T, "123456789", "Test", "fynn")["ok"]),
        ("a dashed SSN from the payload still passes",
         verify_open_return(T, "123-45-6789", "Test", "fynn")["ok"]),
        # The whole point of the module.
        ("a DIFFERENT return is refused",
         not verify_open_return(T, "500001007", "Media", "Blogger")["ok"]),
        ("the refusal names both ids so the operator can see what happened",
         "123456789" in verify_open_return(T, "500001007")["reason"]
         and "500001007" in verify_open_return(T, "500001007")["reason"]),
        # Off-by-one and prefix errors are how a wrong-client bug actually looks.
        ("an id that is a PREFIX of the open one is refused",
         not verify_open_return(T, "12345678")["ok"]),
        ("an id with one digit changed is refused",
         not verify_open_return(T, "123456780")["ok"]),
        ("no return open at all is refused", not verify_open_return(HOME, "123456789")["ok"]),
        ("an empty title is refused", not verify_open_return("", "123456789")["ok"]),
        ("no id to check against is refused, not waved through",
         not verify_open_return(T, "")["ok"]),
        # Right family, wrong person — the id matched but the name did not.
        ("matching id with a mismatched surname is refused",
         not verify_open_return(T, "123456789", "Test", "Smith")["ok"]),
        ("a payload with no name still passes on the id alone",
         verify_open_return(T, "123456789")["ok"]),
    ]
    return _table("navigation: the open return is proved from Drake's own title", checks)


def case_nav_name_matching():
    """Strict on surname, forgiving on given name — and never treating an unreadable
    name as agreement."""
    from drake_nav import names_match, split_drake_name
    ok_ = lambda d, f, l: names_match(d, f, l)["ok"]
    checks = [
        ("'fynn, Test' matches Test fynn", ok_("fynn, Test", "Test", "fynn")),
        ("case and spacing are ignored", ok_("  FYNN ,  TEST ", "test", "FYNN")),
        ("'WATERSON, MINERAL' matches Mineral Waterson",
         ok_("WATERSON, MINERAL", "Mineral", "Waterson")),
        # Joint returns: a spouse's W-2 belongs in this return too.
        ("a joint return accepts the first spouse",
         ok_("BLOGGER, MEDIA & NICHE", "Media", "Blogger")),
        ("a joint return accepts the SECOND spouse",
         ok_("BLOGGER, MEDIA & NICHE", "Niche", "Blogger")),
        ("a middle name on one side only still matches",
         ok_("RUNNER, MILES LANE", "Miles", "Runner")),
        ("punctuation differences are noise", ok_("O'BRIEN, SEAN", "Sean", "OBrien")),
        ("a generational suffix is noise", ok_("SMITH JR, JOHN", "John", "Smith")),

        ("a different surname is refused", not ok_("SHOEMAKER, OXFORD", "Oxford", "Loafer")),
        ("a different given name is refused", not ok_("fynn, Test", "Other", "fynn")),
        # Substring matching would pass this. It is a different person.
        ("'ANN' does not match 'DEANNA'", not ok_("SMITH, DEANNA", "Ann", "Smith")),
        ("'ROB' does not match 'ROBERT'", not ok_("SMITH, ROBERT", "Rob", "Smith")),
        ("an unreadable Drake name is refused, not assumed", not ok_("", "Test", "fynn")),
        ("a payload with no name at all is refused here", not ok_("fynn, Test", "", "")),

        ("'BLOGGER, MEDIA & NICHE' splits into a surname and two given names",
         split_drake_name("BLOGGER, MEDIA & NICHE")
         == {"last": "BLOGGER", "given": ["MEDIA", "NICHE"]}),
        ("a name with no comma treats the last token as the surname",
         split_drake_name("Test fynn")["last"] == "FYNN"),
    ]
    return _table("navigation: client name matching", checks)


def case_nav_row_selection():
    """Which search-result row is this taxpayer? Matched on the full id in the row's
    automation id — never the masked cell, never the name, never 'the only row'."""
    from drake_nav import choose_client_row, row_client_id
    P = "ClientSelectionUC_DataGridSearchResultsClientsItem"
    rows = [                                    # straight from the live explore dump
        {"automation_id": P + "500001007-0", "name": "BLOGGER, MEDIA & NICHE"},
        {"automation_id": P + "500001008-1", "name": "CATAMARAN, LEEWARD & STARBOARD"},
        {"automation_id": P + "123456789-8", "name": "fynn, Test"},
    ]
    pick = lambda ssn: choose_client_row(rows, ssn)
    checks = [
        ("the full id is read out of the row's automation id",
         row_client_id(P + "123456789-8") == "123456789"),
        ("a row id of another shape yields nothing, not a wrong match",
         row_client_id("SomeOtherControl") == "" and row_client_id(P + "abc-1") == ""),
        ("the right row is chosen", pick("123456789")["row"]["name"] == "fynn, Test"),
        ("a dashed SSN finds the same row", pick("123-45-6789")["row"]["name"] == "fynn, Test"),
        ("an id nobody has is refused", not pick("999999999")["ok"]),
        ("...and is reported as not-found, so the caller can offer to create",
         pick("999999999").get("not_found") is True),
        # The visible cell shows XXXXX6789. Four digits are not an identity.
        ("the masked suffix alone does not select a row", not pick("6789")["ok"]),
        ("an empty id is refused", not pick("")["ok"]),
        ("no rows at all is refused", not choose_client_row([], "123456789")["ok"]),
        ("two rows claiming one id refuse rather than pick the first",
         not choose_client_row(rows + [{"automation_id": P + "123456789-9",
                                        "name": "SOMEONE, Else"}], "123456789")["ok"]),
    ]
    return _table("navigation: choosing the client row", checks)


def case_nav_row_name_pairing():
    """The row carries the id, a separate 'ClientName' Text carries the name, and only
    geometry relates them. Rectangles below are the real ones from the live dump."""
    from drake_nav import collect_client_rows, choose_client_row
    P = "ClientSelectionUC_DataGridSearchResultsClientsItem"
    els = [
        {"automation_id": P + "500001007-0", "name": "row", "rect": [417, 259, 827, 277]},
        {"automation_id": "ClientName", "name": "BLOGGER, MEDIA & NICHE", "rect": [423, 260, 700, 275]},
        {"automation_id": "MaskedId", "name": "XXXXX1007", "rect": [882, 260, 940, 275]},
        {"automation_id": P + "123456789-8", "name": "row", "rect": [417, 411, 827, 429]},
        {"automation_id": "ClientName", "name": "fynn, Test", "rect": [423, 412, 700, 427]},
    ]
    got = collect_client_rows(els)
    by_id = {r["automation_id"]: r["name"] for r in got}
    orphan = collect_client_rows([e for e in els if e["automation_id"] != "ClientName"])
    checks = [
        ("both rows are found", len(got) == 2),
        ("each row gets the name sitting in ITS band",
         by_id.get(P + "500001007-0") == "BLOGGER, MEDIA & NICHE"
         and by_id.get(P + "123456789-8") == "fynn, Test"),
        # 152 pixels apart in the real dialog. Nearest-match would still be wrong.
        ("a row does NOT borrow the next row's name",
         by_id.get(P + "123456789-8") != "BLOGGER, MEDIA & NICHE"),
        ("a row with no name cell keeps name='' rather than a neighbour's",
         all(r["name"] == "" for r in orphan)),
        ("...and that empty name then fails the identity check, which is the point",
         not choose_client_row(orphan, "123456789")["ok"]
         or orphan[0]["name"] == ""),
        ("the masked-id cell is never mistaken for a name",
         "XXXXX1007" not in by_id.values()),
        ("no elements at all yields no rows", collect_client_rows([]) == []),
    ]
    return _table("navigation: pairing a client row with its displayed name", checks)


def case_nav_record_safety():
    """A W-2 screen is ONE employer. Getting this wrong duplicates a client's wages, and
    no read-back would catch it — every value would verify perfectly."""
    from drake_nav import parse_record_position, plan_record_use
    blank = {"ok": True, "index": 1, "count": 1, "populated": 0, "values": []}
    # FAKE employer, and it must stay fake — this file is in a PUBLIC repository and this
    # fixture held a real client's employer EIN and trading name until 2026-08-17.
    occupied = {"ok": True, "index": 1, "count": 1, "populated": 3, "values": [
        {"value": "00-1112222"}, {"value": "test employer llc"}, {"value": "29477"}]}
    unreadable = {"ok": False, "reason": "the form's control tree could not be read",
                  "index": None, "count": None, "populated": 0, "values": []}
    # What the first live navigate-and-fill run actually met: leftover City/State, no
    # employer, no money. Classifying this as a real W-2 is what made the run halt.
    fragment = {"ok": True, "index": 1, "count": 1, "populated": 2, "values": [
        {"value": "Van Nuys"}, {"value": "CA"}]}
    act = lambda s, ein=None, **kw: plan_record_use(s, ein, **kw)["action"]
    checks = [
        ("'Record 1 of 1' parses",
         parse_record_position("Record 1 of 1") == {"index": 1, "count": 1}),
        ("'Record 2 of 3' parses",
         parse_record_position("Record 2 of 3") == {"index": 2, "count": 3}),
        ("unrelated status text is not a record position",
         parse_record_position("Press Page Down for New Screen") is None),

        ("a blank record is used", act(blank) == "use"),
        ("a blank record is used even when an EIN is supplied",
         act(blank, "001112222") == "use"),
        # The dangerous one.
        ("re-sending the SAME employer REFUSES rather than duplicating wages",
         act(occupied, "00-1112222") == "refuse"),
        ("...and the refusal explains the consequence, not just the fact",
         "double" in plan_record_use(occupied, "001112222")["reason"]),
        ("a DIFFERENT employer opens a new record (a second job is normal)",
         act(occupied, "99-1112222") == "new"),
        ("an occupied record with no EIN to compare still opens a new record",
         act(occupied) == "new"),
        ("...but refuses instead when opening new records is disabled",
         act(occupied, allow_new=False) == "refuse"),
        # 'I could not look' must never mean 'nothing is there'.
        # The live halt of 2026-08-07: leftover City/State classified as a real W-2, so the
        # run pressed Page Down, Drake refused to leave an incomplete screen, and nothing
        # happened. The values are not a W-2 — no employer, no money.
        ("stray text with no employer and no money is FILLED IN, not paged past",
         act(fragment) == "use"),
        ("...and the reason says why, so the operator is not guessing",
         "not a W-2" in plan_record_use(fragment)["reason"]),
        ("a record holding only a ZIP still counts as real (errs towards keeping data)",
         act({"ok": True, "index": 1, "count": 1, "populated": 1,
              "values": [{"value": "75001"}]}) == "new"),
        ("a record holding only an amount counts as real",
         act({"ok": True, "index": 1, "count": 1, "populated": 1,
              "values": [{"value": "52,000.00"}]}) == "new"),
        ("a record with a street address but no numbers is still a fragment",
         act({"ok": True, "index": 1, "count": 1, "populated": 1,
              "values": [{"value": "MAIN ST"}]}) == "use"),
        ("an UNREADABLE form refuses rather than assuming it is blank",
         act(unreadable) == "refuse"),
        ("a missing state refuses", act(None) == "refuse"),
        ("the EIN comparison ignores dash formatting",
         act({"ok": True, "index": 1, "count": 1, "populated": 1,
              "values": [{"value": "001112222"}]}, "00-1112222") == "refuse"),
    ]
    return _table("navigation: one W-2 record per employer", checks)


def case_nav_screen_link():
    """'W2' must open Wages and never Gambling Income. The two buttons sit nineteen
    pixels apart on Drake's General tab, and any prefix rule confuses them."""
    from drake_nav import parse_screen_link, choose_screen_link
    links = [                                   # straight from the live explore dump
        {"automation_id": "LINK_0_Col0_Sel10", "name": "W2|Wages"},
        {"automation_id": "LINK_0_Col0_Sel11", "name": "W2G|Gambling Income"},
        {"automation_id": "LINK_0_Col0_Sel12", "name": "1099|1099-R, Retirement"},
        {"automation_id": "LINK_0_Col0_Sel17", "name": "99N|1099-NEC, Nonemployee Compensation"},
        {"automation_id": "LINK_0_Col0_Sel1", "name": "1|Name and Address"},
    ]
    pick = lambda c: choose_screen_link(links, c)
    checks = [
        ("'W2|Wages' parses into code and title",
         parse_screen_link("W2|Wages") == {"code": "W2", "title": "Wages"}),
        ("a label with no pipe is not a screen link",
         parse_screen_link("Import W2") is None),
        ("W2 opens Wages", pick("W2")["link"]["automation_id"] == "LINK_0_Col0_Sel10"),
        # The bug this rule exists to prevent.
        ("W2 does NOT open W2G", pick("W2")["link"]["name"] == "W2|Wages"),
        ("W2G still resolves to itself",
         pick("W2G")["link"]["automation_id"] == "LINK_0_Col0_Sel11"),
        ("lower case is accepted", pick("w2")["link"]["name"] == "W2|Wages"),
        ("a numeric code works", pick("1")["link"]["name"] == "1|Name and Address"),
        # '1' is a prefix of '1099'. A startswith rule opens the wrong screen here.
        ("'1' does not open '1099'", pick("1")["link"]["name"] != "1099|1099-R, Retirement"),
        ("a code that is not on the menu is refused", not pick("SCHC")["ok"]),
        ("...and the refusal lists what IS there", "W2" in pick("SCHC")["candidates"]),
        ("an empty code is refused", not pick("")["ok"]),
        ("two buttons claiming one code refuse rather than pick the first",
         not choose_screen_link(links + [{"automation_id": "LINK_0_Col1_Sel99",
                                          "name": "W2|Wages (duplicate)"}], "W2")["ok"]),
    ]
    return _table("navigation: choosing the screen link", checks)


def case_nav_screen_signature():
    """Nothing structural says which Drake screen is on display, so the proof has to come
    off the screen's own heading — BEFORE anything is typed.

    Measured 2026-08-07: Drake keeps several windows all titled 'Data Entry (...)', UIA's
    descendants() crosses window boundaries, and all four structural markers plus all 37
    screen links report present-and-visible in EVERY window in EVERY state. Heads-down
    field numbers are screen-specific, so opening the wrong screen and typing anyway would
    put 78 values into the wrong form with every read-back passing."""
    from drake_nav import screen_is_showing
    W2 = ["Form W-2 - Wage and Tax Statement", "Employer information", "Employee Name"]
    SCREEN1 = ["Return Options", "Firm #", "Preparer #", "ERO #", "Invoice number"]
    checks = [
        ("the W-2 screen is recognised by its heading", screen_is_showing(W2, "W2") is True),
        ("screen 1 is NOT mistaken for the W-2 screen",
         screen_is_showing(SCREEN1, "W2") is False),
        ("an empty label set is not a W-2 screen", screen_is_showing([], "W2") is False),
        ("lower case still matches", screen_is_showing(["form w-2 wage"], "W2") is True),
        ("'w2' as a code matches the same signature",
         screen_is_showing(W2, "w2") is True),
        # 'Import W2' is a BUTTON on the W-2 screen and also appears elsewhere; the
        # signature is the form's printed heading, not any mention of the code.
        ("a stray 'W2' mention is not the heading",
         screen_is_showing(["Import W2", "Return Options"], "W2") is False),
        # An unmeasured screen must report "cannot prove it", never "yes".
        # Computed, not hard-coded: this used to name screen '1099' and quietly went stale
        # the day that screen was measured, failing a case whose point had not changed.
        ("a screen with no measured signature returns None, not True",
         screen_is_showing(["anything at all"], _an_unmeasured_screen()) is None),
        ("...and None is not True", screen_is_showing(["x"], "INT") is not True),
    ]
    return _table("navigation: the right SCREEN is open, proved by its heading", checks)


def case_nav_create_name_collision():
    """Auto-create's one failure mode that looks like success: a misread SSN digit makes a
    brand-new empty return, the W-2 goes in it, the real client's return sits untouched,
    and every check downstream passes."""
    from drake_nav import name_collision, RESULT_ROW_ID_PREFIX as P
    rows = [
        {"automation_id": P + "123456789-0", "name": "fynn, Test"},
        {"automation_id": P + "500001007-1", "name": "BLOGGER, MEDIA & NICHE"},
    ]
    checks = [
        ("an existing client with the same name is found",
         [r["name"] for r in name_collision(rows, "Test", "fynn")] == ["fynn, Test"]),
        ("...so the run can refuse instead of creating a duplicate person",
         len(name_collision(rows, "Test", "fynn")) > 0),
        ("a genuinely new person collides with nobody",
         name_collision(rows, "Jane", "Newperson") == []),
        ("a joint-return spouse counts as a collision",
         len(name_collision(rows, "Niche", "Blogger")) == 1),
        ("a different surname is not a collision",
         name_collision(rows, "Test", "Fynnegan") == []),
        ("an empty client list collides with nobody",
         name_collision([], "Test", "fynn") == []),
        # No name to compare means no evidence — and no evidence must not read as "clear
        # to create", so names_match refuses and nothing collides. The caller still has
        # its own no-name refusal in create_client.
        ("a payload with no name yields no false collision",
         name_collision(rows, "", "") == []),
    ]
    return _table("navigation: auto-create refuses when the name is already on the books",
                  checks)


def case_nav_menu_is_not_the_form():
    """Both windows are titled 'Data Entry (...)'. Telling them apart by structure is what
    stops a run from arming its caret in the menu's screen-search box and typing field
    numbers into it."""
    from drake_nav import classify_data_entry_window
    MENU = ["menuTabControl", "MenuScreenWindow_TextBoxSearch", "statusbar", "txtReturnStatus"]
    FORM = ["taxTabControl", "ucTaxForm", "TAB_7_0", "Textbox_2", "Dropdown_1"]
    checks = [
        ("the Data Entry Menu is recognised", classify_data_entry_window(MENU) == "menu"),
        ("a tax form screen is recognised", classify_data_entry_window(FORM) == "form"),
        ("a window with neither marker is 'unknown', not guessed",
         classify_data_entry_window(["Minimize", "Close"]) == "unknown"),
        ("an empty tree is 'unknown'", classify_data_entry_window([]) == "unknown"),
        ("a window carrying BOTH markers is 'unknown', not silently called a form",
         classify_data_entry_window(MENU + FORM) == "unknown"),
    ]
    return _table("navigation: the menu screen is not the form screen", checks)


def _an_unmeasured_screen() -> str:
    """A Drake screen code that has NO measured heading signature.

    Computed so the case using it cannot go stale: any code not in SCREEN_SIGNATURES will
    do, and one is always available because the agent will never have measured every screen
    Drake has. '99N' (1099-NEC) is the preferred answer while it is still unmeasured.
    """
    from drake_nav import SCREEN_SIGNATURES
    for code in ("99N", "99M", "SCHC", "K1P", "ZZZ_NOT_A_SCREEN"):
        if code not in SCREEN_SIGNATURES:
            return code
    raise AssertionError("every candidate screen code now has a signature — pick another")


def plan_has_confirm(spec, field_no) -> bool:
    """Would a plan mark this field 'verify by eye'? Asked of the PLAN, not the table —
    that flag is what a preparer actually sees, and it is set by build_plan, not declared."""
    import form_plan
    key = next(k for k, f in spec.fields.items() if f["field_no"] == field_no)
    field = spec.fields[key]
    kind = field["kind"]
    # 'ts' was missing here, so a TS dropdown was probed with 'X', sanitized to None, and
    # produced no entry to inspect — the helper then reported "not flagged" about a box that
    # is flagged. A missing kind must not look like a missing guard.
    probe = {"checkbox": True, "pct": "10", "date": "12/31/2025", "money": "100",
             "state": "PA", "code": "CA", "tsj": "T", "ts": "T", "code_an": "A",
             # 1098 adds these two. Both fields that use them carry a `values` set, so the
             # branch below overrides these anyway — they are here so that a future field
             # WITHOUT a list does not silently fall through to "X" and sanitize to None,
             # which is the failure that made this helper lie about 'ts'.
             "code_form": "A", "country": "CA",
             "tin": "123456789", "zip": "19103", "year": "23"}.get(
                 kind, "0" if kind == "digits" else "X")
    # A box with a fixed list has to be probed with something ON the list, or the plan
    # rightly refuses it and there is no entry left to inspect for the flag.
    if field.get("values"):
        probe = sorted(field["values"])[0]
    payload = {key: probe}
    # A LOCALITY box resolves through Drake's city table, which is keyed on the state as
    # well as the name. Probed without one it resolves to nothing, lands in hand_entry, and
    # leaves no typed entry to inspect — the helper would then report "not flagged" for a
    # box that is simply not typed. Give it the pair the coverage payloads use.
    if field_no in (spec.locality_fields or {}):
        payload[key] = "PHILADELPHIA"
        payload[spec.locality_fields[field_no]] = "PA"
    plan = form_plan.build_plan(payload, spec)
    return any(e["field_no"] == field_no and e["confirm"] for e in plan["entries"])


def case_int_field_map():
    """The 1099-INT map is the safety mechanism for that screen, exactly as w2_map is for
    the W-2, so the same class of mistake has to be un-importable.

    A wrong field number has no downstream defense: Drake accepts a number on any screen,
    the popup echoes the value back, the read-back gate passes, and a plausible amount sits
    in the wrong box of a real return. The checks below are what `FormSpec.validate()`
    enforces at import time — this proves the enforcement, not just the current table."""
    import int_map
    from form_plan import FormSpec
    spec = int_map.INT_SPEC
    nums = sorted(f["field_no"] for f in int_map.INT_FIELD_MAP.values())

    def refuses(**over):
        """Does a spec built with this defect refuse to import?"""
        kw = dict(screen="TST", label="t", fields=dict(int_map.INT_FIELD_MAP),
                  max_field=int_map.MAX_FIELD, forbidden=dict(int_map.FORBIDDEN_FIELDS))
        kw.update(over)
        try:
            FormSpec(**kw)
            return False
        except RuntimeError:
            return True

    dup = dict(int_map.INT_FIELD_MAP)
    dup["a_second_key_for_box_1"] = {"field_no": 20, "kind": "money", "label": "clash"}
    forbidden_bind = dict(int_map.INT_FIELD_MAP)
    forbidden_bind["foreign_province"] = {"field_no": 14, "kind": "text", "label": "sub-screen"}
    over_range = dict(int_map.INT_FIELD_MAP)
    over_range["invented"] = {"field_no": 63, "kind": "money", "label": "not on the screen"}
    bad_kind = dict(int_map.INT_FIELD_MAP)
    bad_kind["odd"] = {"field_no": 63, "kind": "currency", "label": "not a real kind"}

    checks = [
        ("every field number 1-62 is mapped except 14",
         nums == [n for n in range(1, 63) if n != 14]),
        ("61 boxes are writable", len(nums) == 61),
        ("field 14 is FORBIDDEN — it is a '<Click to Access>' sub-screen, not a box",
         14 in int_map.FORBIDDEN_FIELDS and 14 not in nums),
        ("two keys claiming the same box refuses to import", refuses(fields=dup)),
        ("a key bound to the forbidden sub-screen refuses to import",
         refuses(fields=forbidden_bind)),
        ("a field number above the highest legible one refuses to import",
         refuses(fields=over_range, max_field=62)),
        ("an unknown value kind refuses to import — it would fall through to plain text",
         refuses(fields=bad_kind, max_field=63)),
        ("a locality field naming no mapped box refuses to import",
         refuses(locality_fields={99: "resident_state"})),
        # The dedupe id is what stops the same payer going in twice, so it has to BE a key.
        ("the dedupe key is a real key on this screen",
         spec.dedupe_key in int_map.INT_FIELD_MAP),
        ("field 4 resolves through Drake's locality table, not as free text",
         4 in spec.locality_fields),
        # A dropdown moves into the confirmed set only by being READ BACK on a live run, so
        # the contents of that set change over time and asserting a snapshot of it just goes
        # stale. What must hold forever is that it cannot claim a box that is not a dropdown
        # at all — that would silently drop the "check this by eye" flag from a real one.
        ("nothing is confirmed as a dropdown that is not a dropdown",
         spec.dropdowns_confirmed <= spec.dropdowns),
        ("a dropdown that is NOT confirmed still gets flagged for a human",
         all(f["field_no"] in spec.dropdowns_confirmed
             or plan_has_confirm(spec, f["field_no"])
             for f in int_map.INT_FIELD_MAP.values()
             if f["field_no"] in spec.dropdowns)),
    ]
    return _table("1099-INT: the field map cannot bind a box it must not touch", checks)


def case_int_value_kinds():
    """The three value kinds the 1099-INT screen needs that a W-2 never did.

    Each one rejects rather than guesses. A percentage box holding 150, a date Drake will
    parse differently from what was meant, or a 'J' silently downgraded to 'T' are all
    wrong values that LOOK right on a screenshot — the read-back cannot catch any of them,
    because Drake really did accept what it was given."""
    from form_plan import sanitize
    checks = [
        # pct — a real range, not a clamp.
        ("'100%' -> '100'", sanitize("pct", "100%") == "100"),
        ("'12.50' keeps its half a percent", sanitize("pct", "12.50") == "12.5"),
        ("0 percent is a value, not a blank", sanitize("pct", "0") == "0"),
        ("150% is REJECTED, not clamped to 100", sanitize("pct", "150") is None),
        ("a negative percentage is REJECTED", sanitize("pct", "-5") is None),
        ("'abc' is not a percentage", sanitize("pct", "abc") is None),
        # date — one unambiguous output, and a refusal when the input is not a date.
        ("'12/31/2025' -> MMDDYYYY", sanitize("date", "12/31/2025") == "12312025"),
        ("ISO '2025-12-31' lands on the same value", sanitize("date", "2025-12-31") == "12312025"),
        ("a 2-digit year is expanded", sanitize("date", "123125") == "12312025"),
        ("month 13 is REJECTED", sanitize("date", "13/01/2025") is None),
        ("day 32 is REJECTED", sanitize("date", "01/32/2025") is None),
        ("a 7-digit smear is REJECTED rather than sliced", sanitize("date", "1231202") is None),
        # tsj — the whole reason 'ts' could not be reused here.
        ("'J' survives on a 1099 screen — an account really can be joint",
         sanitize("tsj", "J") == "J"),
        ("'joint' spelled out is J", sanitize("tsj", "joint") == "J"),
        ("T and S still work", (sanitize("tsj", "T"), sanitize("tsj", "spouse")) == ("T", "S")),
        ("a 'J' on the W-2's TS selector is REJECTED, not turned into T",
         sanitize("ts", "J") is None),
        ("'X' is not a taxpayer designation", sanitize("tsj", "X") is None),
        # A TIN is digits; Drake formats it itself.
        ("a formatted payer TIN keeps only its digits",
         sanitize("tin", "93-1234567") == "931234567"),
        ("a routing number keeps only its digits", sanitize("digits", "031 000 053") == "031000053"),
    ]
    return _table("1099-INT: percentages, dates and TSJ reject rather than guess", checks)


def case_int_screen_and_grid():
    """Two ways to be on the wrong screen with the right heading.

    The INT screen can be drawn as a FORM or as a spreadsheet GRID, and Drake prints the
    same heading over both. Heads-down field numbers belong to the form; in the grid they
    address nothing. And 'Interest Income' on its own appears in the Data Entry Menu's own
    screen-link list — which is present in every window in every state — so a short
    signature would report the INT screen as open while a preparer is looking at the menu."""
    from drake_nav import screen_is_showing, screen_is_grid
    INT_FORM = ["Schedule B - Interest Income (1099-INT)", "Payer information",
                "*Use <F3> to switch to grid mode*"]
    MENU = ["INT|1099-INT, Interest Income", "W2|Wages, Salaries, Tips",
            "DIV|1099-DIV, Dividend Income"]
    # Automation ids, measured: the grid builds controls under 'ucTaxGrid…', the form
    # builds none.
    GRID_IDS = ["ucTaxGrid1", "ucTaxGrid1_DataGrid", "PART_ScrollBar", "txtInstance"]
    FORM_IDS = ["ucTaxForm", "taxTabControl", "Textbox_12", "Dropdown_3", "Label_6"]
    checks = [
        ("the INT screen is recognised by its full printed heading",
         screen_is_showing(INT_FORM, "INT") is True),
        ("the Data Entry MENU is not mistaken for the INT screen",
         screen_is_showing(MENU, "INT") is False),
        ("...even though the menu does contain the words 'Interest Income'",
         any("Interest Income" in l for l in MENU)),
        ("the W-2 screen is not mistaken for the INT screen",
         screen_is_showing(["Form W-2 - Wage and Tax Statement"], "INT") is False),
        ("the INT screen is not mistaken for the W-2 screen",
         screen_is_showing(INT_FORM, "W2") is False),
        ("lower case still matches", screen_is_showing(["schedule b - interest income (1099-int)"], "INT") is True),
        ("grid mode is detected from the tree", screen_is_grid(GRID_IDS) is True),
        ("the form view is NOT reported as a grid", screen_is_grid(FORM_IDS) is False),
        ("an unreadable tree is not reported as a grid", screen_is_grid([]) is False),
        ("a None in the id list does not crash the check", screen_is_grid([None, "ucTaxForm"]) is False),
        # The heading and the mode are independent questions; passing one is not passing both.
        ("the heading says nothing about which MODE is showing",
         screen_is_showing(INT_FORM, "INT") is True and screen_is_grid(GRID_IDS) is True),
    ]
    return _table("1099-INT: right screen, and the right MODE of it", checks)


def case_form_dispatch():
    """A payload picks its own Drake screen, so the agent has to refuse the ones it cannot
    drive — and it has to refuse them BEFORE anything is typed.

    Nothing downstream can catch this. The read-back gate proves Drake accepted a value and
    the form check proves the value is on the canvas; both pass happily when 61 INT field
    numbers are typed into a 1099-R. Only the map knows what a number means on a screen."""
    import agent as ag
    unknown = None
    try:
        ag._load_form_map("99N")
    except ValueError as e:
        unknown = str(e)
    # FAKE identifiers, and they must stay fake. This file is committed to a PUBLIC
    # repository: a real SSN, EIN or client name in a test fixture is a disclosure, and it
    # is permanent the moment it is pushed. Caught on 2026-08-17 with a live taxpayer's SSN
    # and surname sitting in this very line, one `git push` away from being world-readable.
    # Use 000-11-2222 / 00-1112222 and a name nobody has.
    w2_target = ag._payload_target({"employee_ssn": "000112222", "employer_ein": "001112222",
                                    "employee_first_name": "Test", "employee_last_name": "Taxpayer"})
    int_target = ag._payload_target({"drake_screen": "INT", "recipient_tin": "123456789",
                                     "payer_tin": "931234567", "employer_ein": "999999999",
                                     "recipient_first_name": "Test", "recipient_last_name": "fynn"})
    bogus = ag._payload_target({"drake_screen": "SCHC", "recipient_tin": "1"})
    checks = [
        ("a payload with no screen is still a W-2 — the default cannot change under us",
         w2_target["screen"] == "W2"),
        ("the W-2's dedupe id is the employer EIN", w2_target["ein"] == "001112222"),
        ("an INT payload targets the INT screen", int_target["screen"] == "INT"),
        ("the INT dedupe id is the PAYER TIN, not an employer EIN",
         int_target["ein"] == "931234567"),
        ("the client is found from the recipient TIN on a 1099",
         int_target["ssn"] == "123456789" and int_target["last"] == "fynn"),
        ("a screen with no field map resolves to no form", bogus["form"] is None),
        ("...and asking for its map raises rather than falling back to the W-2",
         unknown is not None),
        ("the refusal names the screens the agent CAN drive",
         unknown is not None and all(s in unknown for s in ("W2", "INT", "DIV", "1099", "SSA"))),
        ("every mapped screen exposes the same planning entry points",
         all(callable(getattr(ag._load_form_map(s)[0], fn))
             for s in ("W2", "INT", "DIV", "1099", "SSA") for fn in ("build_plan", "format_plan"))),
        # The two Schedule B screens share a dedupe id NAME and must not share a map.
        ("INT and DIV are different maps, not one map reached twice",
         ag._load_form_map("INT")[0] is not ag._load_form_map("DIV")[0]),
        ("a DIV payload targets the DIV screen and dedupes on the payer TIN",
         (lambda t: t["screen"] == "DIV" and t["ein"] == "941234567")(
             ag._payload_target({"drake_screen": "DIV", "recipient_tin": "123456789",
                                 "payer_tin": "941234567"}))),
    ]
    return _table("forms: a screen with no verified map is refused, not guessed", checks)


def case_int_full_coverage_plan():
    """The full-coverage dummy payload really does reach every writable box.

    This is the payload the first live INT run uses, and its whole value is that one run
    proves the entire map. A quietly dropped key would leave a box unproven while the run
    still reported success — so the count is asserted, not eyeballed."""
    import json as _json
    import int_map
    with open("sample_1099int_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = int_map.build_plan(payload, ts="T")
    got = sorted(e["field_no"] for e in plan["entries"])
    locality = [e for e in plan["entries"] if e["field_no"] == 4]
    checks = [
        ("all 61 writable boxes are planned", len(plan["entries"]) == 61),
        ("...and they are exactly 1-62 without the sub-screen",
         got == [n for n in range(1, 63) if n != 14]),
        ("nothing was rejected", [s for s in plan["skipped"] if s.get("rejected")] == []),
        ("no key in the payload was unrecognised", plan["unknown_keys"] == []),
        ("the client identity keys are acknowledged, not called unknown",
         "recipient_tin" in plan["identity_keys"]),
        ("the resident city is typed as Drake's CODE, not the printed name",
         bool(locality) and locality[0]["value"] == "PL"
         and locality[0]["resolved_from"] == "PHILADELPHIA"),
        ("every dropdown is flagged for a human's eye",
         all(e["confirm"] for e in plan["entries"] if e["field_no"] in int_map.DROPDOWN_FIELDS)),
        ("the plan says which screen it is for", plan["screen"] == "INT"),
    ]
    return _table("1099-INT: the dummy payload covers every box on the screen", checks)


def case_div_field_map():
    """The 1099-DIV map, held to the same import-time rules as the INT one.

    This screen adds a way to be wrong that the INT screen did not have: FOUR COLUMNS. Box
    1a is fields 18 (Total), 43 (Foreign Amount), 47 (Foreign Percent) and 51 (Nominee), and
    those are four different meanings of one printed box. Putting the nominee amount in the
    Total is not a typo, it is a different return — and every downstream check passes,
    because Drake really did accept the number."""
    import div_map
    from form_plan import FormSpec
    spec = div_map.DIV_SPEC
    nums = sorted(f["field_no"] for f in div_map.DIV_FIELD_MAP.values())

    def refuses(**over):
        kw = dict(screen="TST", label="t", fields=dict(div_map.DIV_FIELD_MAP),
                  max_field=div_map.MAX_FIELD, forbidden=dict(div_map.FORBIDDEN_FIELDS))
        kw.update(over)
        try:
            FormSpec(**kw)
            return False
        except RuntimeError:
            return True

    dup = dict(div_map.DIV_FIELD_MAP)
    dup["a_second_key_for_box_1a"] = {"field_no": 18, "kind": "money", "label": "clash"}
    forbidden_bind = dict(div_map.DIV_FIELD_MAP)
    forbidden_bind["foreign_province"] = {"field_no": 13, "kind": "text", "label": "sub-screen"}
    over_range = dict(div_map.DIV_FIELD_MAP)
    over_range["invented"] = {"field_no": 74, "kind": "money", "label": "not on the screen"}

    def field_of(key):
        return div_map.DIV_FIELD_MAP[key]["field_no"]

    checks = [
        ("every field number 1-73 is mapped except 13",
         nums == [n for n in range(1, 74) if n != 13]),
        ("72 boxes are writable", len(nums) == 72),
        ("field 13 is FORBIDDEN — it is a '<Click to Access>' sub-screen, not a box",
         13 in div_map.FORBIDDEN_FIELDS and 13 not in nums),
        ("two keys claiming the same box refuses to import", refuses(fields=dup)),
        ("a key bound to the forbidden sub-screen refuses to import",
         refuses(fields=forbidden_bind)),
        ("a field number above the highest measured one refuses to import",
         refuses(fields=over_range, max_field=73)),
        ("the dedupe key is a real key on this screen",
         spec.dedupe_key in div_map.DIV_FIELD_MAP),
        ("field 4 resolves through Drake's locality table, not as free text",
         4 in spec.locality_fields),
        ("nothing is confirmed as a dropdown that is not a dropdown",
         spec.dropdowns_confirmed <= spec.dropdowns),
        ("a dropdown that is NOT confirmed still gets flagged for a human",
         all(f["field_no"] in spec.dropdowns_confirmed
             or plan_has_confirm(spec, f["field_no"])
             for f in div_map.DIV_FIELD_MAP.values()
             if f["field_no"] in spec.dropdowns)),
        # The four columns. These are the numbers read off the live screen, and they are the
        # one thing on this map that no downstream layer could ever question.
        ("Box 1a is FOUR different boxes, one per column",
         [field_of("box1a_ordinary_dividends"), field_of("box1a_foreign_amount"),
          field_of("box1a_foreign_pct"), field_of("box1a_nominee")] == [18, 43, 47, 51]),
        ("Box 2a likewise", [field_of("box2a_total_capital_gain"),
                             field_of("box2a_foreign_amount"),
                             field_of("box2a_foreign_pct"),
                             field_of("box2a_nominee")] == [20, 45, 49, 53]),
        ("the Total and the Nominee amount are never the same box",
         all(field_of(f"box{b}_nominee") != field_of(t) for b, t in
             (("1a", "box1a_ordinary_dividends"), ("1b", "box1b_qualified_dividends"),
              ("2a", "box2a_total_capital_gain")))),
        ("the foreign PERCENT column is a percentage kind, not an amount",
         all(div_map.DIV_FIELD_MAP[k]["kind"] == "pct" for k in
             ("box1a_foreign_pct", "box1b_foreign_pct", "box2a_foreign_pct",
              "box6_foreign_pct"))),
        # Numbering does NOT carry over from the sibling screen. Field 5 is the proof.
        ("field 5 is 'Do not update' here, where the INT screen has a different box",
         field_of("do_not_update") == 5),
    ]
    return _table("1099-DIV: four columns, and no number borrowed from the INT screen", checks)


def case_div_code_kinds():
    """Drake's Section 1202 codes carry a DIGIT, and the W-2's Box 12 rule forbids that.

    `code` rejects anything with a digit on purpose: 'D 23' has a prior-year designation
    that belongs in its own box, and stripping it to 'D' measures the whole amount against
    the current year's deferral limit. That rule is right for Box 12 and wrong for Q1/Q3/Q4,
    so `code_an` is a separate kind. This case exists to stop anyone 'simplifying' the two
    back into one — the W-2 checks below are the ones that would break silently."""
    from form_plan import sanitize
    import div_map
    checks = [
        ("'Q1' survives as a Section 1202 code", sanitize("code_an", "Q1") == "Q1"),
        ("lower case is upper-cased", sanitize("code_an", "q3") == "Q3"),
        ("a plain letter code still works", sanitize("code_an", "a") == "A"),
        # The descriptive text Drake shows beside the code must not be squeezed into one.
        ("the dropdown's DESCRIPTION is refused, not compressed into a code",
         sanitize("code_an", "Q1 - QSB stock 50% acquired after 08/10/1993") is None),
        ("a code with a dash is refused rather than joined up",
         sanitize("code_an", "Q-1") is None),
        ("blank is a skip, not an empty code", sanitize("code_an", "   ") is None),
        # ---- the W-2 rule this kind must NOT have loosened ----
        ("the W-2's Box 12 code STILL rejects a digit — 'D 23' is not 'D'",
         sanitize("code", "D 23") is None),
        ("...and a bare letter code still passes there", sanitize("code", "D") == "D"),
        ("the two kinds really are different functions",
         sanitize("code", "Q1") is None and sanitize("code_an", "Q1") == "Q1"),
        # The list is what makes a wrong-but-code-shaped value loud.
        ("field 22 accepts exactly Q1, Q3, Q4 — Drake has no Q2",
         div_map.DIV_FIELD_MAP["box2c_section_1202_type"]["values"] == {"Q1", "Q3", "Q4"}),
        # ...and the list has to be ENFORCED, not merely declared. This is the guard that
        # came out of the live INT run: 'Q2' is exactly the right shape, the heads-down popup
        # would echo it back perfectly, and Drake would then reject it with a window that
        # holds the keyboard. The refusal has to happen before Drake is open.
        ("a plausible but non-existent code (Q2) is REFUSED, not typed",
         (lambda p: not any(e["field_no"] == 22 for e in p["entries"])
                    and any(s.get("rejected") and s["field_no"] == 22 for s in p["skipped"]))(
             div_map.build_plan({"box2c_section_1202_type": "Q2"}))),
        ("...and the refusal says so out loud rather than passing quietly",
         any("REJECTED" in w for w in
             div_map.build_plan({"box2c_section_1202_type": "Q2"})["warnings"])),
        ("a real code on the list is still entered",
         any(e["field_no"] == 22 and e["value"] == "Q3"
             for e in div_map.build_plan({"box2c_section_1202_type": "Q3"})["entries"])),
        # The correction that came out of reading the real list.
        ("the IL Schedule M list includes the two-letter territory codes",
         {"AA", "FF"} <= div_map.DIV_FIELD_MAP["il_schedule_m_source"]["values"]),
        ("a Puerto Rico bond code is accepted, not refused by our own planner",
         any(e["field_no"] == 71 and e["value"] == "BB"
             for e in div_map.build_plan({"il_schedule_m_source": "BB"})["entries"])),
        ("...and a code Drake does not have is still refused",
         any(s.get("rejected") and s["field_no"] == 71 for s in
             div_map.build_plan({"il_schedule_m_source": "ZZ"})["skipped"])),
        # Field 68, measured live: a 3-character box. Cutting a NUMBER to fit does not
        # shorten it, it changes it — Drake itself took '601' from '6010', which is where
        # this came from. A text box may be trimmed and reported; a quantity may not.
        ("a number too long for its box is REFUSED, not cut down to fit",
         (lambda p: not any(e["field_no"] == 68 for e in p["entries"])
                    and any(s.get("rejected") and s["field_no"] == 68 for s in p["skipped"]))(
             div_map.build_plan({"ftc_form_1116_code": "6010"}))),
        ("...and the refusal quotes what cutting it would have entered",
         any("601" in w and "REJECTED" in w for w in
             div_map.build_plan({"ftc_form_1116_code": "6010"})["warnings"])),
        ("a value that FITS the box is still entered",
         any(e["field_no"] == 68 and e["value"] == "1"
             for e in div_map.build_plan({"ftc_form_1116_code": "1"})["entries"])),
        # The W-2's Box 14 is a DESCRIPTION and trimming it to the box is what a preparer
        # does by hand. That behaviour must survive — this is a rule about quantities.
        ("a text field too long for its box is still trimmed and reported, not refused",
         (lambda p: any(e["field_no"] == 49 and e.get("trimmed_from") == "UNION DUES AND MORE"
                        and e["value"] == "UNION DU" for e in p["entries"]))(
             __import__("w2_map").build_plan({"box14_1_desc": "UNION DUES AND MORE"}))),
        # The rule only has to exist where a capped box holds a quantity. It does not on the
        # W-2 — every max_len there is a Box 14 DESCRIPTION — which is why that path keeps
        # its own trim and is left alone.
        ("no capped box on the W-2 screen holds a number, so nothing there needs the rule",
         all(__import__("w2_map").W2_FIELD_MAP[k]["kind"] == "text"
             for k, f in __import__("w2_map").W2_FIELD_MAP.items() if f.get("max_len"))),
    ]
    return _table("1099-DIV: alphanumeric codes, without loosening the W-2's rule", checks)


def case_div_screen_signature():
    """Two Schedule B screens that print almost the same heading, and cross-link to each
    other.

    'Screen INT for Interest' is printed ON the DIV screen, and the Data Entry Menu lists
    both ('DIV|1099-DIV, Dividend Income') in every window in every state. A signature loose
    enough to match either would let a DIV payload be typed into the INT screen, where the
    same field numbers mean entirely different boxes."""
    from drake_nav import screen_is_showing, screen_is_grid
    DIV_FORM = ["Schedule B - Dividend Income (1099-DIV)", "Payer Information",
                "Screen INT for Interest", "*Use <F3> to switch to grid mode*"]
    INT_FORM = ["Schedule B - Interest Income (1099-INT)", "Payer information"]
    MENU = ["INT|1099-INT, Interest Income", "DIV|1099-DIV, Dividend Income",
            "W2|Wages, Salaries, Tips"]
    checks = [
        ("the DIV screen is recognised by its full printed heading",
         screen_is_showing(DIV_FORM, "DIV") is True),
        ("the Data Entry MENU is not mistaken for the DIV screen",
         screen_is_showing(MENU, "DIV") is False),
        ("...even though the menu does contain the words 'Dividend Income'",
         any("Dividend Income" in l for l in MENU)),
        # The two Schedule B screens, each way round.
        ("the INT screen is not mistaken for the DIV screen",
         screen_is_showing(INT_FORM, "DIV") is False),
        ("the DIV screen is not mistaken for the INT screen",
         screen_is_showing(DIV_FORM, "INT") is False),
        ("...even though the DIV screen prints a link to the INT one",
         any("Screen INT" in l for l in DIV_FORM)),
        ("the W-2 screen is not mistaken for the DIV screen",
         screen_is_showing(["Form W-2 - Wage and Tax Statement"], "DIV") is False),
        ("lower case still matches",
         screen_is_showing(["schedule b - dividend income (1099-div)"], "DIV") is True),
        # This screen opens in GRID mode on a fresh record — measured 2026-08-12.
        ("grid mode is still detected on this screen",
         screen_is_grid(["ucTaxGrid1", "ucTaxGrid1_DataGrid"]) is True),
        ("the form view is NOT reported as a grid",
         screen_is_grid(["Textbox_18", "Dropdown_22", "CheckboxTextRight_17"]) is False),
    ]
    return _table("1099-DIV: told apart from the INT screen it links to", checks)


def case_div_full_coverage_plan():
    """The full-coverage dummy payload really does reach every writable box on the DIV
    screen — all 72 of them, so one live run proves the whole map."""
    import json as _json
    import div_map
    with open("sample_1099div_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = div_map.build_plan(payload, ts="T")
    got = sorted(e["field_no"] for e in plan["entries"])
    locality = [e for e in plan["entries"] if e["field_no"] == 4]
    by_no = {e["field_no"]: e for e in plan["entries"]}
    checks = [
        ("all 72 writable boxes are planned", len(plan["entries"]) == 72),
        ("...and they are exactly 1-73 without the sub-screen",
         got == [n for n in range(1, 74) if n != 13]),
        ("nothing was rejected", [s for s in plan["skipped"] if s.get("rejected")] == []),
        ("no key in the payload was unrecognised", plan["unknown_keys"] == []),
        ("the client identity keys are acknowledged, not called unknown",
         "recipient_tin" in plan["identity_keys"]),
        ("the resident city is typed as Drake's CODE, not the printed name",
         bool(locality) and locality[0]["value"] == "PL"
         and locality[0]["resolved_from"] == "PHILADELPHIA"),
        ("every dropdown is flagged for a human's eye",
         all(e["confirm"] for e in plan["entries"] if e["field_no"] in div_map.DROPDOWN_FIELDS)),
        ("the plan says which screen it is for", plan["screen"] == "DIV"),
        ("the Section 1202 code is planned as Q1, not stripped to Q",
         by_no[22]["value"] == "Q1"),
        ("the FTC date is planned as MMDDYYYY", by_no[67]["value"] == "12312025"),
        # Column staging: the dummy amounts are chosen so a value in the wrong column is
        # visible by eye on the screenshot. Assert the staging held.
        ("the four Box 1a columns carry four DIFFERENT values",
         len({by_no[n]["value"] for n in (18, 43, 47, 51)}) == 4),
    ]
    return _table("1099-DIV: the dummy payload covers every box on the screen", checks)


def case_r_field_map():
    """The 1099-R map. Same import-time rules, plus two shapes no earlier screen had.

    This screen is TS, not TSJ — Drake offers T and S only, because a pension belongs to one
    person — and its Box 7 is TWO boxes rather than one. Both are the kind of thing that
    looks fine right up until a return is wrong."""
    import r_map
    from form_plan import FormSpec
    spec = r_map.R_SPEC
    nums = sorted(f["field_no"] for f in r_map.R_FIELD_MAP.values())

    def refuses(**over):
        kw = dict(screen="TST", label="t", fields=dict(r_map.R_FIELD_MAP),
                  max_field=r_map.MAX_FIELD, forbidden=dict(r_map.FORBIDDEN_FIELDS))
        kw.update(over)
        try:
            FormSpec(**kw)
            return False
        except RuntimeError:
            return True

    dup = dict(r_map.R_FIELD_MAP)
    dup["a_second_key_for_box_1"] = {"field_no": 25, "kind": "money", "label": "clash"}
    over_range = dict(r_map.R_FIELD_MAP)
    over_range["invented"] = {"field_no": 74, "kind": "money", "label": "not on the screen"}
    kinds = {k: f["kind"] for k, f in r_map.R_FIELD_MAP.items()}

    checks = [
        ("every field number 1-73 is mapped, with no gaps and nothing forbidden",
         nums == list(range(1, 74))),
        ("73 boxes are writable", len(nums) == 73),
        ("no box on this screen is a '<Click to Access>' sub-screen",
         r_map.FORBIDDEN_FIELDS == {}),
        ("two keys claiming the same box refuses to import", refuses(fields=dup)),
        ("a field number above the highest measured one refuses to import",
         refuses(fields=over_range, max_field=73)),
        ("the dedupe key is a real key on this screen", spec.dedupe_key in r_map.R_FIELD_MAP),
        ("nothing is confirmed as a dropdown that is not a dropdown",
         spec.dropdowns_confirmed <= spec.dropdowns),
        ("a dropdown that is NOT confirmed still gets flagged for a human",
         all(f["field_no"] in spec.dropdowns_confirmed
             or plan_has_confirm(spec, f["field_no"])
             for f in r_map.R_FIELD_MAP.values() if f["field_no"] in spec.dropdowns)),
        # TS, not TSJ. The kind is the enforcement — `ts` rejects 'J' outright rather than
        # quietly downgrading it to 'T', which would file a spouse's pension under the
        # taxpayer.
        ("field 1 is the TS kind, so a joint 'J' is REFUSED, not downgraded",
         kinds["ts"] == "ts" and __import__("form_plan").sanitize("ts", "J") is None),
        ("...and T and S both still work",
         (__import__("form_plan").sanitize("ts", "T"),
          __import__("form_plan").sanitize("ts", "S")) == ("T", "S")),
        # Box 7 is two boxes.
        ("Box 7 is TWO separate boxes, 33 and 34",
         (r_map.R_FIELD_MAP["box7_dist_code"]["field_no"],
          r_map.R_FIELD_MAP["box7_dist_code_2"]["field_no"]) == (33, 34)),
        ("both halves accept the same 29 codes Drake offers",
         r_map.R_FIELD_MAP["box7_dist_code"]["values"]
         == r_map.R_FIELD_MAP["box7_dist_code_2"]["values"] == r_map.DIST_CODES
         and len(r_map.DIST_CODES) == 29),
        # Both locality rows resolve through Drake's table, keyed on their OWN state.
        ("each local row resolves its locality against its own state row",
         spec.locality_fields == {48: "box15_state", 55: "box15_state_2"}),
        # The override block is flagged, because writing one changes the return.
        ("every recipient OVERRIDE box is flagged for a human's eye",
         all(r_map.R_FIELD_MAP[k].get("confirm") for k in r_map.R_FIELD_MAP
             if k.endswith("_override"))),
        ("the override block is fields 16-24",
         sorted(f["field_no"] for k, f in r_map.R_FIELD_MAP.items()
                if k.endswith("_override")) == list(range(16, 25))),
    ]
    return _table("1099-R: TS not TSJ, and Box 7 is two boxes", checks)


def case_r_dist_code_split():
    """Box 7 printed as '1B' is TWO codes, and Drake has two boxes for them.

    The distribution code decides whether the 10% early-withdrawal penalty applies, whether
    the money is a non-taxable rollover, or whether it is a death benefit. Dropping the
    second character does not lose detail — it changes the tax. And nothing downstream would
    notice: '1' on its own is a perfectly valid code that Drake accepts without complaint."""
    import r_map
    split = r_map.split_distribution_code
    plan_1b = r_map.build_plan({"box7_dist_code": "1B"})
    plan_bad = r_map.build_plan({"box7_dist_code": "1BX"})
    by_no = {e["field_no"]: e for e in plan_1b["entries"]}
    checks = [
        ("'1B' is two codes", split("1B") == ("1", "B")),
        ("a single code leaves the second box alone", split("7") == ("7", None)),
        ("lower case is accepted", split("1b") == ("1", "B")),
        ("a space between them is not a third code", split("1 B") == ("1", "B")),
        # Refusals. Each of these is a string that LOOKS code-shaped.
        ("a three-character string is REFUSED, not truncated to the first two",
         split("1BX") == (None, None)),
        ("a code Drake does not offer is refused", split("Z") == (None, None)),
        ("...even when only the second half is wrong", split("1Z") == (None, None)),
        ("blank is not a code", split("") == (None, None)),
        # And the planner wires it up.
        ("build_plan splits '1B' across fields 33 and 34",
         by_no.get(33, {}).get("value") == "1" and by_no.get(34, {}).get("value") == "B"),
        ("...and says so, rather than doing it quietly",
         any("split" in w.lower() for w in plan_1b["warnings"])),
        ("an unusable code is entered NOWHERE",
         not any(e["field_no"] in (33, 34) for e in plan_bad["entries"])),
        ("...and the run is told to key it by hand",
         any("by hand" in w for w in plan_bad["warnings"])),
        # A caller that already knows the shape must not be second-guessed.
        ("an explicit pair is left exactly as given",
         (lambda p: {e["field_no"]: e["value"] for e in p["entries"] if e["field_no"] in (33, 34)}
                    == {33: "4", 34: "D"})(
             r_map.build_plan({"box7_dist_code": "4", "box7_dist_code_2": "D"}))),
    ]
    return _table("1099-R: Box 7 splits into the two boxes Drake has", checks)


def case_drake_symbol_codes():
    """Drake selection codes that are SYMBOLS, and the length bound that keeps them honest.

    The 1099-R pension-type list has 44 codes and seven are symbols — @ for Arizona, # for
    Connecticut, * for a Pennsylvania ESOP, % for New York, and &/$/= for Maryland. Refusing
    them would be the same defect as the IL Schedule M list that only knew A-Z: our own
    planner turning away a value Drake accepts."""
    from form_plan import sanitize
    import r_map
    checks = [
        ("'@' is a real Drake code, not junk", sanitize("code_an", "@") == "@"),
        ("so are '#', '%' and '='",
         (sanitize("code_an", "#"), sanitize("code_an", "%"), sanitize("code_an", "=")) == ("#", "%", "=")),
        ("the alphanumeric codes still work",
         (sanitize("code_an", "q1"), sanitize("code_an", "7"), sanitize("code_an", "aa"))
         == ("Q1", "7", "AA")),
        # The length bound is what stops descriptive text becoming a code.
        ("a three-character string is not a selection code", sanitize("code_an", "ABC") is None),
        ("the dropdown's DESCRIPTION is still refused",
         sanitize("code_an", "Q1 - QSB stock 50% acquired after 08/10/1993") is None),
        ("a dash is still not a code character", sanitize("code_an", "Q-") is None),
        ("blank is a skip, not an empty code", sanitize("code_an", "  ") is None),
        # The W-2 rule this must not have loosened.
        ("the W-2's Box 12 code STILL rejects a digit — 'D 23' is not 'D'",
         sanitize("code", "D 23") is None),
        ("the pension-type list carries all seven symbols",
         set("#$%&*=@") <= r_map.PENSION_TYPE_CODES),
        ("...and a symbol Drake does NOT offer is refused by the list",
         any(s.get("rejected") and s["field_no"] == 3
             for s in r_map.build_plan({"pension_type": "!"})["skipped"])),
    ]
    return _table("Drake selection codes: symbols are real, descriptions are not", checks)


def case_r_screen_signature():
    """The 1099-R screen prints '1099-R' inside two of its own checkbox captions, so a
    signature loose enough to match those would report the screen as open from anywhere."""
    from drake_nav import screen_is_showing
    R_FORM = ["Form 1099-R - Pensions, Annuities, Retirement, Profit-Sharing, IRAs, "
              "Insurance Contracts, etc.", "Payer Information (required for e-file)",
              "1099-R for disability", "1099-R altered or handwritten"]
    MENU = ["1099|1099-R, Retirement", "INT|1099-INT, Interest Income",
            "DIV|1099-DIV, Dividend Income"]
    checks = [
        ("the 1099-R screen is recognised by its full printed heading",
         screen_is_showing(R_FORM, "1099") is True),
        ("the Data Entry MENU is not mistaken for it",
         screen_is_showing(MENU, "1099") is False),
        ("...even though the menu does contain '1099-R'",
         any("1099-R" in l for l in MENU)),
        ("the screen's own checkbox captions do not match it on their own",
         screen_is_showing(["1099-R for disability", "1099-R altered or handwritten"], "1099")
         is False),
        ("the DIV screen is not mistaken for the 1099-R screen",
         screen_is_showing(["Schedule B - Dividend Income (1099-DIV)"], "1099") is False),
        ("the 1099-R screen is not mistaken for the DIV screen",
         screen_is_showing(R_FORM, "DIV") is False),
        ("lower case still matches",
         screen_is_showing(["form 1099-r - pensions, annuities, retirement, profit-sharing"],
                           "1099") is True),
    ]
    return _table("1099-R: told apart from its own captions and the menu", checks)


def case_r_full_coverage_plan():
    """The full-coverage dummy payload reaches every one of the 73 boxes."""
    import json as _json
    import r_map
    with open("sample_1099r_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = r_map.build_plan(payload, ts="T")
    got = sorted(e["field_no"] for e in plan["entries"])
    by_no = {e["field_no"]: e for e in plan["entries"]}
    checks = [
        ("all 73 boxes are planned", len(plan["entries"]) == 73),
        ("...and they are exactly 1-73", got == list(range(1, 74))),
        ("nothing was rejected", [s for s in plan["skipped"] if s.get("rejected")] == []),
        ("no key in the payload was unrecognised", plan["unknown_keys"] == []),
        ("the plan says which screen it is for", plan["screen"] == "1099"),
        ("Box 7 is split across the two boxes",
         by_no[33]["value"] == "1" and by_no[34]["value"] == "B"),
        ("the first locality resolves to Drake's CODE, not the printed name",
         by_no[48]["value"] == "PL" and by_no[48]["resolved_from"] == "PHILADELPHIA"),
        # The second row is Ohio deliberately: New Jersey has no localities in Drake's table
        # at all, so that row could never be proven.
        ("the second locality row resolves too, against its own state",
         by_no[55]["value"] and by_no[55]["resolved_from"] == "COLUMBUS"),
        ("the four-digit Roth year fits its box", by_no[41]["value"] == "2019"),
        ("both dates are planned as MMDDYYYY",
         by_no[68]["value"] == "12312025" and by_no[69]["value"] == "06302025"),
        ("every dropdown is flagged for a human's eye",
         all(e["confirm"] for e in plan["entries"] if e["field_no"] in r_map.DROPDOWN_FIELDS)),
    ]
    return _table("1099-R: the dummy payload covers every box on the screen", checks)


def case_ssa_field_map():
    """The SSA-1099 map. Small, and interesting for what it does NOT have.

    Drake gives this screen ten boxes for a form with twenty printed values, and says why on
    the screen itself: '* No input required since there is no impact on the tax return'. The
    map's job here is to carry that distinction, not to invent boxes."""
    import ssa_map
    from form_plan import FormSpec
    spec = ssa_map.SSA_SPEC
    nums = sorted(f["field_no"] for f in ssa_map.SSA_FIELD_MAP.values())

    def refuses(**over):
        kw = dict(screen="TST", label="t", fields=dict(ssa_map.SSA_FIELD_MAP),
                  max_field=ssa_map.MAX_FIELD)
        kw.update(over)
        try:
            FormSpec(**kw)
            return False
        except RuntimeError:
            return True

    dup = dict(ssa_map.SSA_FIELD_MAP)
    dup["a_second_key_for_net_benefits"] = {"field_no": 4, "kind": "money", "label": "clash"}
    over_range = dict(ssa_map.SSA_FIELD_MAP)
    over_range["invented"] = {"field_no": 11, "kind": "money", "label": "not on the screen"}
    checks = [
        ("every field number 1-10 is mapped, with no gaps", nums == list(range(1, 11))),
        ("two keys claiming the same box refuses to import", refuses(fields=dup)),
        ("a field number above the highest measured one refuses to import",
         refuses(fields=over_range, max_field=10)),
        ("nothing is confirmed as a dropdown that is not a dropdown",
         spec.dropdowns_confirmed <= spec.dropdowns),
        ("a dropdown that is NOT confirmed still gets flagged for a human",
         all(f["field_no"] in spec.dropdowns_confirmed
             or plan_has_confirm(spec, f["field_no"])
             for f in ssa_map.SSA_FIELD_MAP.values() if f["field_no"] in spec.dropdowns)),
        # TS, not TSJ — the same shape as the 1099-R.
        ("field 1 is the TS kind, so a joint 'J' is REFUSED, not downgraded",
         ssa_map.SSA_FIELD_MAP["ts"]["kind"] == "ts"
         and __import__("form_plan").sanitize("ts", "J") is None),
        # The leading zero. '1' is not '01' and Drake's list has no '1'.
        ("field 8's codes keep their LEADING ZERO",
         ssa_map.BENEFIT_DESIGNATIONS == {"01", "02", "03", "04", "05"}),
        ("...so '1' is refused where '01' is accepted",
         (lambda bad, good: not any(e["field_no"] == 8 for e in bad["entries"])
                            and any(e["field_no"] == 8 and e["value"] == "01"
                                    for e in good["entries"]))(
             ssa_map.build_plan({"state_benefit_designation": "1"}),
             ssa_map.build_plan({"state_benefit_designation": "01"}))),
        # There is no payer to dedupe on, and that is recorded rather than papered over.
        ("this screen has NO dedupe id — the payer is the government",
         spec.dedupe_key is None),
        # Box 5 is the only number the return actually runs on.
        ("a payload with no NET BENEFITS is called out, not quietly accepted",
         any("NET BENEFITS" in w for w in
             ssa_map.build_plan({"federal_tax_withheld": "1800"})["warnings"])),
        ("...and a payload that has it is not nagged",
         not any("NET BENEFITS" in w for w in
                 ssa_map.build_plan({"net_benefits": "18000"})["warnings"])),
    ]
    return _table("SSA-1099: ten boxes, and Drake's own reason for the rest", checks)


def case_ssa_not_on_screen():
    """A value Drake has no box for must be reported BY NAME, never dropped as unknown.

    This screen is the sharpest test of that rule in the project: eleven of the values an
    extractor reads off an SSA-1099 have nowhere to go on it. Two of them — the benefits
    paid for an EARLIER year — would actively harm a return if someone decided to squeeze
    them into Box 5, because that taxes the whole back payment in the current year and
    throws away the lump-sum election."""
    import json as _json
    import ssa_map
    with open("sample_ssa1099_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = ssa_map.build_plan(payload, ts="T")
    off = {n["key"]: n["why"] for n in plan["not_on_screen"]} if plan.get("not_on_screen") else {}
    entered = {e["field_no"] for e in plan["entries"]}
    checks = [
        ("nothing in the payload came back as an UNKNOWN key", plan["unknown_keys"] == []),
        ("Box 3 is reported as not-on-this-screen, by name", "benefits_paid" in off),
        ("Box 4 likewise", "benefits_repaid" in off),
        ("...and each says WHY, in Drake's own words",
         "no impact on the tax return" in off.get("benefits_paid", "")),
        ("the Medicare split is reported, since only the total has a box",
         {"medicare_part_b", "medicare_part_c", "medicare_part_d"} <= set(off)),
        ("prior-year benefits are reported AND pointed at the LUMP SUM screen",
         "LUMP SUM" in off.get("benefits_for_prior_years", "").upper()),
        ("none of them was quietly typed into a box anyway", len(entered) == 10),
        # The beneficiary is the CLIENT, so it is acknowledged as identity rather than
        # reported as a value with no home.
        ("the beneficiary's name and SSN are acknowledged as identity, not as gaps",
         "beneficiary_ssn" in plan["identity_keys"] and "beneficiary_ssn" not in off),
    ]
    return _table("SSA-1099: a value with no box is named, not dropped", checks)


def case_ssa_screen_signature():
    """The menu's link for this screen is a PREFIX of the screen's own heading.

    'SSA|SSA-1099, Social Security' sits in the Data Entry Menu's link list, which is present
    in every window in every state. A signature stopping at 'Social Security' would match it
    and report the screen as open while a preparer is looking at the menu."""
    from drake_nav import screen_is_showing
    SSA_FORM = ["SSA-1099, Social Security Benefits Statement",
                "RRB-1099, Railroad Retirement Board Payments",
                "No input required since there is no impact on the tax return"]
    MENU = ["SSA|SSA-1099, Social Security", "1099|1099-R, Retirement",
            "INT|1099-INT, Interest Income"]
    checks = [
        ("the SSA screen is recognised by its full printed heading",
         screen_is_showing(SSA_FORM, "SSA") is True),
        ("the Data Entry MENU is not mistaken for it", screen_is_showing(MENU, "SSA") is False),
        ("...even though the menu link is a PREFIX of the heading",
         any(l.startswith("SSA|SSA-1099, Social Security") for l in MENU)),
        ("the 1099-R screen is not mistaken for it",
         screen_is_showing(["Form 1099-R - Pensions, Annuities, Retirement"], "SSA") is False),
        ("the SSA screen is not mistaken for the 1099-R screen",
         screen_is_showing(SSA_FORM, "1099") is False),
        ("lower case still matches",
         screen_is_showing(["ssa-1099, social security benefits statement"], "SSA") is True),
    ]
    return _table("SSA-1099: told apart from the menu link that prefixes it", checks)



def case_m1098_field_map():
    """The Form 1098 map. Forty-five boxes, and two new value kinds behind it."""
    import m1098_map
    from form_plan import FormSpec, sanitize
    spec = m1098_map.M1098_SPEC
    nums = sorted(f["field_no"] for f in m1098_map.M1098_FIELD_MAP.values())

    def refuses(**over):
        kw = dict(screen="TST", label="t", fields=dict(m1098_map.M1098_FIELD_MAP),
                  max_field=m1098_map.MAX_FIELD)
        kw.update(over)
        try:
            FormSpec(**kw)
            return False
        except RuntimeError:
            return True

    dup = dict(m1098_map.M1098_FIELD_MAP)
    dup["a_second_key_for_box1"] = {"field_no": 24, "kind": "money", "label": "clash"}
    over_range = dict(m1098_map.M1098_FIELD_MAP)
    over_range["invented"] = {"field_no": 46, "kind": "money", "label": "not on the screen"}
    checks = [
        ("every field number 1-45 is mapped, with no gaps", nums == list(range(1, 46))),
        ("two keys claiming the same box refuses to import", refuses(fields=dup)),
        ("a field number above the highest measured one refuses to import",
         refuses(fields=over_range, max_field=45)),
        ("nothing is confirmed as a dropdown that is not a dropdown",
         spec.dropdowns_confirmed <= spec.dropdowns),
        ("a dropdown that is NOT confirmed still gets flagged for a human",
         all(f["field_no"] in spec.dropdowns_confirmed
             or plan_has_confirm(spec, f["field_no"])
             for f in m1098_map.M1098_FIELD_MAP.values() if f["field_no"] in spec.dropdowns)),
        # TSJ, not TS: a mortgage really can be held jointly, unlike a W-2 or an SSA benefit.
        ("field 1 is the TSJ kind, so a joint 'J' is ACCEPTED",
         m1098_map.M1098_FIELD_MAP["tsj"]["kind"] == "tsj" and sanitize("tsj", "J") == "J"),
        # Field 3's list contains FOUR-character codes, which is the whole reason this kind
        # exists separately from code_an.
        ("field 3 accepts the four-character form codes 4835 and 8829",
         sanitize("code_form", "4835") == "4835" and sanitize("code_form", "8829") == "8829"),
        ("...which the two-character code_an kind would have refused",
         sanitize("code_an", "4835") is None),
        ("...and code_form still refuses a descriptive phrase",
         sanitize("code_form", "Schedule A") is None),
        ("the lender is what makes two 1098s different documents",
         spec.dedupe_key == "lender_tin"),
    ]
    return _table("Form 1098: 45 boxes, TSJ, and four-character form codes", checks)


def case_m1098_country_codes():
    """Drake's country codes are NOT ISO, and the collisions name a real country.

    This is the one place in the project where the `values` membership check buys nothing:
    ES, CH, AU, SE and AT are all valid Drake codes, so each passes, gets typed, is echoed
    back and reads off the form as a genuine selection — while meaning a country nobody
    chose. The defence is that the plan prints the NAME, so a human can see it."""
    import m1098_map
    from form_plan import sanitize
    name = m1098_map.country_name
    # Measured 2026-08-15 by reading the control. ISO code -> what Drake thinks it means.
    COLLISIONS = {"ES": ("Spain", "El Salvador"), "CH": ("Switzerland", "China"),
                  "AU": ("Australia", "Austria"), "SE": ("Sweden", "Seychelles")}
    plan = m1098_map.build_plan({"lender_country": "ES", "box1_mortgage_interest": "100"})
    good = m1098_map.build_plan({"lender_country": "CA", "box1_mortgage_interest": "100"})
    junk = m1098_map.build_plan({"lender_country": "ZZ", "box1_mortgage_interest": "100"})
    checks = [
        ("Drake's list is the 258 codes read out of the control",
         len(m1098_map.DRAKE_COUNTRIES) == 258),
        ("every ISO code that collides is a VALID Drake code naming another country",
         all(name(iso) == drake and name(iso) != real
             for iso, (real, drake) in COLLISIONS.items())),
        ("...so a membership check alone would pass all of them",
         all(iso in m1098_map.COUNTRY_CODES for iso in COLLISIONS)),
        ("the plan therefore prints the country NAME beside the code",
         any("EL SALVADOR" in w.upper() for w in plan["warnings"])),
        ("...for a correct code too, so the check is not only shown on failure",
         any("CANADA" in w.upper() for w in good["warnings"])),
        # The cheap half of the defence: a NAME can never be shortened into a code.
        ("a country name is refused rather than cut down to two letters",
         sanitize("country", "Switzerland") is None and sanitize("country", "Spain") is None),
        # A code off the list never becomes an entry at all — the planner refuses it before
        # Drake is open, which is a stronger guard than any warning about a typed value.
        ("a code that is not on Drake's list is REFUSED, not typed",
         not any(e["field_no"] == 13 for e in junk["entries"])
         and any(s["key"] == "lender_country" and s["rejected"] for s in junk["skipped"])),
        ("...and the refusal does not dump all 258 codes into the warning",
         any(w.startswith("REJECTED lender_country") and "and 246 more" in w
             for w in junk["warnings"])),
        # There is no US code: a domestic address belongs in the U.S. ONLY block.
        ("there is no United States code — a domestic address uses fields 10/11",
         "US" not in m1098_map.COUNTRY_CODES),
    ]
    return _table("Form 1098: Drake country codes are not ISO, and it is invisible", checks)


def case_m1098_not_on_screen():
    """Two of Form 1098's own numbered boxes have nowhere to go on Drake's screen."""
    import json as _json
    import m1098_map
    with open("sample_1098_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = m1098_map.build_plan(payload, ts="T")
    off = {n["key"]: n["why"] for n in plan["not_on_screen"]} if plan.get("not_on_screen") else {}
    entered = {e["field_no"] for e in plan["entries"]}
    checks = [
        ("nothing in the payload came back as an UNKNOWN key", plan["unknown_keys"] == []),
        ("Box 4, refund of overpaid interest, is reported by name",
         "box4_refund_overpaid_interest" in off),
        ("...quoting Drake's own note, which sends it to Schedule 1 line 8",
         "Schedule 1, line 8" in off.get("box4_refund_overpaid_interest", "")),
        ("Box 9, number of properties, is reported by name",
         "box9_number_of_properties" in off),
        ("the loan-limit cap is reported AND pointed at the DEDM screen",
         "DEDM" in off.get("mortgage_balance_limitation", "")),
        ("all 45 real boxes are still entered", entered == set(range(1, 46))),
        ("no value landed in a box the screen does not have",
         max(entered) <= m1098_map.MAX_FIELD),
    ]
    return _table("Form 1098: boxes 4 and 9 have no box, and both are named", checks)


def case_m1098_screen_signature():
    """This screen's number and its words BOTH appear in menu link lists, separately.

    'DOCS|1098/1099 Source Document Guide' sits on the Miscellaneous tab and carries the
    number; this screen's own link, '1098|Mortgage Interest Statement', carries the words.
    Menu links are present in every window in every state, so a signature matching either
    half would report the screen as open while a preparer is looking at a menu."""
    from drake_nav import screen_is_showing
    FORM = ["Form 1098 - Mortgage Interest", "Recipient's/Lender's Information",
            "Payer's/Borrower's Information (if different from screen 1)"]
    MENU = ["1098|Mortgage Interest Statement", "DEDM|Deductible Mortgage Interest",
            "DOCS|1098/1099 Source Document Guide", "8828|Recapture of Federal Mortgage Subsidy"]
    checks = [
        ("the 1098 screen is recognised by its printed heading",
         screen_is_showing(FORM, "1098") is True),
        ("the Data Entry MENU is not mistaken for it", screen_is_showing(MENU, "1098") is False),
        ("...even though a menu link carries the number 1098",
         any("1098" in l for l in MENU)),
        ("...and another carries the words 'Mortgage Interest'",
         any("Mortgage Interest" in l for l in MENU)),
        ("the 1099-R screen is not mistaken for it",
         screen_is_showing(["Form 1099-R - Pensions, Annuities, Retirement"], "1098") is False),
        ("the 1098 screen is not mistaken for the 1099-R screen",
         screen_is_showing(FORM, "1099") is False),
        ("lower case still matches",
         screen_is_showing(["form 1098 - mortgage interest"], "1098") is True),
    ]
    return _table("Form 1098: told apart from two different menu links", checks)


def case_m1098_full_coverage_plan():
    """The whole screen, planned from the coverage payload: 45 entries, nothing invented."""
    import json as _json
    import m1098_map
    with open("sample_1098_full.json", encoding="utf-8-sig") as f:
        payload = _json.load(f)
    plan = m1098_map.build_plan(payload, ts="T")
    entries = {e["field_no"]: e for e in plan["entries"]}
    no_box1 = m1098_map.build_plan({"box2_principal": "412500"})
    business = m1098_map.build_plan({"for_schedule": "E", "box1_mortgage_interest": "100"})
    checks = [
        ("all 45 boxes are planned", sorted(entries) == list(range(1, 46))),
        ("nothing was rejected", not plan.get("skipped")),
        ("entries come out in field-number order",
         [e["field_no"] for e in plan["entries"]] == sorted(entries)),
        ("the date boxes are normalised to Drake's MMDDYYYY",
         entries[27]["value"] == "06142019" and entries[37]["value"] == "06142019"),
        ("money keeps its cents for Drake to round, rather than being rounded here",
         entries[24]["value"] == "14321.55"),
        ("the four-character form code survives the planner", entries[3]["value"] == "A"),
        # Box 1 is what the document exists to report.
        ("a payload with no BOX 1 interest is called out, not quietly accepted",
         any("BOX 1 MORTGAGE INTEREST" in w for w in no_box1["warnings"])),
        ("...and a payload that has it is not nagged",
         not any("BOX 1 MORTGAGE INTEREST" in w for w in plan["warnings"])),
        # Schedule A is the ordinary answer; anything else is a different return.
        ("a FOR code other than A is called out as leaving Schedule A",
         any("NOT to Schedule A" in w for w in business["warnings"])),
        ("...and plain Schedule A is not nagged",
         not any("NOT to Schedule A" in w for w in plan["warnings"])),
    ]
    return _table("Form 1098: 45 of 45 planned from the coverage payload", checks)



def case_m1098_menu_tab_walk():
    """The Data Entry Menu draws ONE of its ten tabs at a time, and open_screen had only
    ever been measured on the one Drake opens with.

    That tab, General, carries 37 links. The menu carries 312. The other 275 are not hidden
    from the tree — they are absent from it — so `open_screen` reported "no screen with code
    1098 on this menu" and listed whatever tab happened to be showing. A correct refusal
    that reads exactly like a statement that the screen does not exist. Screen 1098 is on
    'Other Forms', which is how this was found.

    Selecting a tab changes what the menu DRAWS and nothing in the return, so walking them
    is cheap to be wrong about — unlike typing the code into the menu's search box, which
    is an Edit control that accepts anything at all."""
    from drake_nav import open_screen

    # Measured 2026-08-15 by selecting each tab and reading its links (menu_screens.json).
    TABS = {
        "General": ["W2|Wages", "INT|1099-INT, Interest Income", "A|Itemized Deductions Schedule"],
        "Other Forms": ["1098|Mortgage Interest Statement", "DEDM|Deductible Mortgage Interest",
                        "X|1040-X, Amended Return"],
        "Miscellaneous": ["DOCS|1098/1099 Source Document Guide", "PRNT|Print Options"],
    }
    HEADINGS = {"1098": "Form 1098 - Mortgage Interest", "W2": "Form W-2 Wage and Tax Statement"}

    class _Menu:
        """A Drake menu that shows one tab, and a screen that opens when a link is used."""

        def __init__(self, start="General"):
            self.tab = start
            self.opened = None
            self.selected = []          # tabs this run actually selected, in order

        # -- the surface open_screen talks to ------------------------------------------
        def nav_data_entry_window(self):
            return {"hwnd": 1, "title": "Data Entry (123456789 - fynn, Test)",
                    "kind": "form" if self.opened else "menu"}

        def nav_all_elements(self):
            els = []
            for i, name in enumerate(TABS):
                els.append({"automation_id": f"TAB_{i}", "name": name,
                            "control_type": "TabItem"})
            # Only the SELECTED tab's links exist in the tree. This is the whole point.
            for i, name in enumerate(TABS[self.tab]):
                els.append({"automation_id": f"LINK_0_Col0_Sel{i}", "name": name,
                            "control_type": "Button"})
            if self.opened:
                els.append({"automation_id": "Label_6", "name": HEADINGS[self.opened],
                            "control_type": "Text"})
            return els

        def nav_act(self, element, want="invoke"):
            name = str((element or {}).get("name") or "")
            if str((element or {}).get("control_type")) == "TabItem":
                self.tab = name
                self.selected.append(name)
                return {"ok": True, "how": "select", "error": None}
            self.opened = name.split("|")[0]
            return {"ok": True, "how": "invoke", "error": None}

        def press(self, _keys):
            return None

        def _detect_unexpected_dialog(self):
            return None

    quiet = lambda *a, **k: None

    # Sitting on 'States'-like tab (here: Miscellaneous) — the situation that exposed this.
    away = _Menu(start="Miscellaneous")
    got_away = open_screen(away, "1098", timeout=2.0, log=quiet)

    # Already on the right tab: the walk must not run at all.
    already = _Menu(start="Other Forms")
    got_already = open_screen(already, "1098", timeout=2.0, log=quiet)

    # A code that is on NO tab must still be refused, after all of them were searched.
    nowhere = _Menu(start="General")
    got_nowhere = open_screen(nowhere, "SCHC", timeout=2.0, log=quiet)

    # The tab walk must not disturb a screen that was always reachable.
    w2 = _Menu(start="General")
    got_w2 = open_screen(w2, "W2", timeout=2.0, log=quiet)

    checks = [
        ("a screen on another tab is found and opened", got_away["ok"] is True),
        ("...by selecting the tab it is actually on", "Other Forms" in away.selected),
        ("...and it is the 1098 screen that opened", away.opened == "1098"),
        ("a screen on the CURRENT tab opens without selecting anything",
         got_already["ok"] is True and already.selected == []),
        ("a code on no tab at all is still refused", got_nowhere["ok"] is False),
        ("...and the refusal says every tab was searched",
         "Every tab was searched" in got_nowhere["reason"]),
        ("...naming them, so 'not found' cannot be confused with 'not looked for'",
         all(t in got_nowhere["reason"] for t in TABS)),
        ("a screen on the tab Drake opens with is unaffected",
         got_w2["ok"] is True and w2.selected == []),
        # Each tab is selected at most once. A walk that re-selects is a walk that can loop
        # on a menu whose tab order changes under it.
        ("no tab is selected twice", len(away.selected) == len(set(away.selected))),
    ]
    return _table("Form 1098: the menu has ten tabs, and only one is drawn", checks)



def case_payload_target_covers_every_screen():
    """Every drivable screen must hand the agent a client SSN it RECOGNISES.

    `_payload_target` resolves whose return a document belongs to by trying a fixed list of
    key names. A screen whose extractor spells that key differently produces a payload with
    no client at all, and `_navigate_for_payload` refuses it having typed nothing — correct,
    but the screen is then completely undrivable through the watch folder, which is the only
    path the product actually uses.

    TWO SHIPPED THAT WAY. SSA-1099 emits `beneficiary_ssn` (an SSA statement names a
    beneficiary, not a recipient) and Form 1098 emits `borrower_ssn` (on a 1098 the
    RECIPIENT is the lender, so the client cannot be called that). Both passed every other
    test, both entered every box perfectly in a live run, and neither could be sent from
    the browser — because every live run so far used a hand-written sample that happened to
    carry `client_ssn`, a key no extractor produces.

    This case walks the SCREENS rather than the keys, so a seventh form cannot be added
    without an identity path."""
    import agent

    # What the backend's screenMap actually emits as the client identity, per screen. Kept
    # here rather than imported because the two repos ship separately: this is the contract
    # between them, and a test that read it from one side could not detect them drifting.
    EMITTED = {
        "W2":   ("employee_ssn",     "employee_first_name",  "employee_last_name"),
        "INT":  ("recipient_tin",    "recipient_first_name", "recipient_last_name"),
        "DIV":  ("recipient_tin",    "recipient_first_name", "recipient_last_name"),
        "1099": ("recipient_tin",    "recipient_first_name", "recipient_last_name"),
        "SSA":  ("beneficiary_ssn",  "recipient_first_name", "recipient_last_name"),
        "1098": ("borrower_ssn",     "borrower_first_name",  "borrower_last_name"),
    }

    missing_screen = sorted(set(agent._FORMS) - set(EMITTED))
    unresolved, no_name = [], []
    for screen, (ssn_key, first_key, last_key) in EMITTED.items():
        if screen not in agent._FORMS:
            continue
        t = agent._payload_target({"drake_screen": screen, ssn_key: "123456789",
                                   first_key: "TEST", last_key: "FYNN"})
        if t["ssn"] != "123456789":
            unresolved.append(f"{screen} via {ssn_key}")
        if t["first"] != "TEST" or t["last"] != "FYNN":
            no_name.append(f"{screen} via {first_key}/{last_key}")

    # The failure itself, spelled out: no identity at all must REFUSE, not default.
    blank = agent._payload_target({"drake_screen": "1098"})

    checks = [
        ("every screen the agent can drive is covered by this case", missing_screen == []),
        ("every screen's client SSN key resolves", unresolved == []),
        ("...including SSA-1099's beneficiary_ssn",
         agent._payload_target({"drake_screen": "SSA",
                                "beneficiary_ssn": "123456789"})["ssn"] == "123456789"),
        ("...and Form 1098's borrower_ssn",
         agent._payload_target({"drake_screen": "1098",
                                "borrower_ssn": "123456789"})["ssn"] == "123456789"),
        ("every screen's client NAME keys resolve", no_name == []),
        ("a payload with no identity resolves to no SSN, so navigation refuses it",
         blank["ssn"] == ""),
        ("the screen still resolves to its form map", blank["form"] is not None),
        # The dedupe id is per-form and must not fall back to the W-2's employer key.
        ("each screen's dedupe id comes from its own form definition",
         agent._payload_target({"drake_screen": "1098", "lender_tin": "12-3456789"})["ein"]
         == "12-3456789"
         and agent._payload_target({"drake_screen": "INT", "payer_tin": "99"})["ein"] == "99"),
    ]
    return _table("navigation: every screen hands over a client the agent recognises", checks)



def case_batch_queue_order():
    """A batch's order is stated in the payload, because timestamps cannot carry it.

    The watch folder used to be drained oldest-first, which is right for payloads dropped one
    at a time by a person. A BATCH writes all of them in a tight loop, and on Windows they
    land inside a single clock tick: the modification times tie, `sorted` keeps whatever
    order `glob` returned, and the run order is effectively arbitrary. Measured 2026-08-15 —
    a six-document batch written W-2 first began on the 1099-INT.

    That is not cosmetic. The agent STOPS at the first document that does not complete
    cleanly, so the order is what a person is watching, it is what "it stopped at number 3"
    means, and it is what says which documents were never attempted at all. A UI that lists
    them in one order while the agent works in another is telling someone the wrong document
    halted, and pointing them at the wrong screenshot as evidence."""
    import json as _json
    import os
    import tempfile
    from pathlib import Path
    from agent import _queue_order

    tmp = Path(tempfile.mkdtemp())

    def drop(name, seq=None, age=0.0, text=None):
        p = tmp / name
        if text is not None:
            p.write_text(text, encoding="utf-8")
        else:
            body = {"drake_screen": "W2", "client_ssn": "123456789"}
            if seq is not None:
                body["batch_seq"] = seq
            p.write_text(_json.dumps(body), encoding="utf-8")
        # Force the tie the real bug depends on: every file the same mtime.
        os.utime(p, (1_700_000_000 + age, 1_700_000_000 + age))
        return p

    # Written in one order, named in another, all with an IDENTICAL timestamp — the exact
    # shape of a batch drop.
    drop("zz-third.json", seq=3)
    drop("aa-first.json", seq=1)
    drop("mm-second.json", seq=2)
    ordered = [p.name for p in sorted(tmp.glob("*.json"), key=_queue_order)]

    # A hand-dropped payload has no seq. It must not jump the queue, and among its own kind
    # it keeps the oldest-first behaviour the folder has always had.
    tmp2 = Path(tempfile.mkdtemp())

    def drop2(name, seq=None, age=0.0):
        p = tmp2 / name
        body = {"drake_screen": "W2"}
        if seq is not None:
            body["batch_seq"] = seq
        p.write_text(_json.dumps(body), encoding="utf-8")
        os.utime(p, (1_700_000_000 + age, 1_700_000_000 + age))

    drop2("loose-newer.json", age=200)
    drop2("loose-older.json", age=100)
    drop2("batch-2.json", seq=2, age=999)
    drop2("batch-1.json", seq=1, age=999)
    mixed = [p.name for p in sorted(tmp2.glob("*.json"), key=_queue_order)]

    # A file that is not JSON must not take the sorter down. It gets picked up, fails its own
    # parse, and is reported with a report beside it — which is a result, not a crash.
    tmp3 = Path(tempfile.mkdtemp())
    drop3 = lambda n, t: (tmp3 / n).write_text(t, encoding="utf-8")
    drop3("broken.json", "{not json")
    drop3("fine.json", _json.dumps({"batch_seq": 1}))
    try:
        survived = [p.name for p in sorted(tmp3.glob("*.json"), key=_queue_order)]
        crashed = False
    except Exception:
        survived, crashed = [], True

    checks = [
        ("a batch runs in the order it stated, not the order the files were named",
         ordered == ["aa-first.json", "mm-second.json", "zz-third.json"]),
        ("...even though every file has the SAME modification time",
         len({(tmp / n).stat().st_mtime for n in ordered}) == 1),
        ("a batch runs before loose payloads that were dropped by hand",
         mixed[:2] == ["batch-1.json", "batch-2.json"]),
        ("...and those loose payloads keep the old oldest-first behaviour",
         mixed[2:] == ["loose-older.json", "loose-newer.json"]),
        ("...even though the batch's files are the NEWEST in the folder",
         (tmp2 / "batch-1.json").stat().st_mtime > (tmp2 / "loose-newer.json").stat().st_mtime),
        ("a file that is not JSON does not take the sorter down", not crashed),
        ("...it sorts last, to be picked up and reported on its own terms",
         survived == ["fine.json", "broken.json"]),
    ]
    return _table("watch folder: a batch's order is stated, not inferred from timestamps", checks)


def case_transport_keys_are_not_values():
    """`batch_seq` tells the agent WHEN to enter a payload, not what to type.

    Every key in a payload is either a box on the form, a declared not-on-this-screen value,
    an identity the agent navigates on, or an unrecognised key that gets reported. Transport
    is none of those, and a transport key reported as unrecognised would put a permanent,
    meaningless warning on every document the backend ever sends — which is how people learn
    to skim warnings, and skimmed warnings are why the whole read-back layer exists."""
    import importlib
    from w2_map import TRANSPORT_KEYS

    TRANSPORT = {"drake_screen": "W2", "doc_type": "w2", "batch_seq": 3}
    leaked = []
    for mod, screen, key, value in [
        ("w2_map", "W2", "box1_wages", "52000"),
        ("int_map", "INT", "box1_interest", "1200"),
        ("div_map", "DIV", "box1a_ordinary_dividends", "1200"),
        ("r_map", "1099", "box1_gross_distribution", "1200"),
        ("ssa_map", "SSA", "net_benefits", "18000"),
        ("m1098_map", "1098", "box1_mortgage_interest", "1200"),
    ]:
        m = importlib.import_module(mod)
        payload = {**TRANSPORT, "drake_screen": screen, key: value}
        plan = m.build_plan(payload)
        if plan["unknown_keys"]:
            leaked.append(f"{mod}: {plan['unknown_keys']}")

    # A wrong key name here would surface as "a transport key was reported as unrecognised",
    # which is a finding about the maps — and it would be a finding about this table. Every
    # probe key is checked against its map first, so the two can never be confused.
    unreal = []
    for mod, _screen, key, _v in [
        ("w2_map", "W2", "box1_wages", "52000"),
        ("int_map", "INT", "box1_interest", "1200"),
        ("div_map", "DIV", "box1a_ordinary_dividends", "1200"),
        ("r_map", "1099", "box1_gross_distribution", "1200"),
        ("ssa_map", "SSA", "net_benefits", "18000"),
        ("m1098_map", "1098", "box1_mortgage_interest", "1200"),
    ]:
        m = importlib.import_module(mod)
        table = next(v for v in vars(m).values()
                     if isinstance(v, dict)
                     and any(isinstance(x, dict) and "field_no" in x for x in v.values()))
        if key not in table:
            unreal.append(f"{mod}.{key}")

    checks = [
        ("every probe key below is a real box on its screen", unreal == []),
        ("batch_seq is transport, alongside drake_screen and doc_type",
         "batch_seq" in TRANSPORT_KEYS and "drake_screen" in TRANSPORT_KEYS),
        ("no screen reports a transport key as unrecognised", leaked == []),
        ("...and one map is asserted directly, so an empty loop cannot pass",
         importlib.import_module("m1098_map").build_plan(
             {"drake_screen": "1098", "batch_seq": 1, "box1_mortgage_interest": "1"}
         )["unknown_keys"] == []),
        # The other half: a key that IS junk must still be reported.
        ("a genuinely unknown key is still reported by name",
         "not_a_real_box" in importlib.import_module("m1098_map").build_plan(
             {"drake_screen": "1098", "batch_seq": 1, "not_a_real_box": "x"}
         )["unknown_keys"]),
    ]
    return _table("watch folder: transport keys are not values, and junk still is", checks)



def case_screen_link_from_a_form_returns_to_the_menu():
    """A screen link clicked from a FORM opens Drake's record chooser, not the screen.

    Measured 2026-08-15 on a return holding several records per screen:

        clicked from the Data Entry Menu -> the screen opens
        clicked from a form screen       -> 'Existing Forms List' opens: a chooser listing
                                            every existing record plus 'New Record'

    `open_screen` used to click the link from wherever it happened to be, on the measurement
    that every link is present and enabled in every state. They are. What they DO is not the
    same, and that is the half the measurement missed.

    It was invisible for as long as documents went in one at a time, because every one of
    those runs starts from the menu. A BATCH opens every document after the first from the
    form the previous one just finished — so the second document of every batch would meet
    the chooser, the heading would never appear, and the run would halt having entered
    exactly one document.

    Not answered by teaching the agent to drive the chooser: every row on it is a record in
    a live return, and picking the wrong one overwrites somebody's 1099 instead of adding
    one. Answered by going back to the menu, where the link does what it says."""
    from drake_nav import open_screen

    LINKS = ["W2|Wages", "INT|1099-INT, Interest Income", "DIV|1099-DIV, Dividend Income"]
    HEADINGS = {
        "W2": "Form W-2 Wage and Tax Statement",
        "INT": "Schedule B - Interest Income (1099-INT)",
    }

    class _Drake:
        """A Drake that shows the record chooser when a link is clicked from a form."""

        def __init__(self, start_kind, chooser_from_form=True):
            self.kind = start_kind
            self.open_screen_code = "INT" if start_kind == "form" else None
            self.chooser = False
            self.chooser_from_form = chooser_from_form
            self.escapes = 0

        def nav_data_entry_window(self):
            return {"hwnd": 1, "title": "Data Entry (123456789 - fynn, Test)", "kind": self.kind}

        def nav_all_elements(self):
            els = [{"automation_id": f"LINK_0_Col0_Sel{i}", "name": n, "control_type": "Button"}
                   for i, n in enumerate(LINKS)]
            els.append({"automation_id": "TAB_0", "name": "General", "control_type": "TabItem"})
            # The chooser is a separate window: the SCREEN's heading is simply absent while
            # it is up, which is exactly how the live failure presented.
            if self.open_screen_code and not self.chooser:
                els.append({"automation_id": "Label_6", "control_type": "Text",
                            "name": HEADINGS[self.open_screen_code]})
            return els

        def nav_act(self, element, want="invoke"):
            code = str((element or {}).get("name") or "").split("|")[0]
            if self.kind == "form" and self.chooser_from_form:
                self.chooser = True          # Drake asks which record; no screen opens
            else:
                self.open_screen_code = code
                self.kind = "form"
            return {"ok": True, "how": "invoke", "error": None}

        def press(self, keys):
            if any(str(k).lower() == "esc" for k in keys):
                self.escapes += 1
                self.kind = "menu"
                self.open_screen_code = None
            return None

        def _detect_unexpected_dialog(self):
            return None

    quiet = lambda *a, **k: None

    # The batch's second document: a form is open, and the next screen must still open.
    from_form = _Drake("form")
    got_form = open_screen(from_form, "W2", timeout=2.0, log=quiet)

    # The first document: already on the menu, and nothing should be closed for nothing.
    from_menu = _Drake("menu")
    got_menu = open_screen(from_menu, "W2", timeout=2.0, log=quiet)

    # If Escape cannot get back to the menu, refuse — never click into the chooser anyway.
    class _Stuck(_Drake):
        def press(self, keys):
            return None                       # Escape does nothing; still on the form

    stuck = _Stuck("form")
    got_stuck = open_screen(stuck, "W2", timeout=2.0, log=quiet)

    checks = [
        ("a screen opened from a FORM still opens", got_form["ok"] is True),
        ("...by closing the previous screen first", from_form.escapes >= 1),
        ("...so Drake's record chooser never appears", from_form.chooser is False),
        ("...and it is the requested screen that is open", from_form.open_screen_code == "W2"),
        ("from the MENU nothing is closed for nothing", from_menu.escapes == 0),
        ("...and the screen still opens", got_menu["ok"] is True),
        ("if the way back to the menu is blocked, it REFUSES", got_stuck["ok"] is False),
        ("...saying why, in terms of the chooser",
         "record chooser" in str(got_stuck.get("reason", ""))),
        ("...and nothing was clicked", stuck.chooser is False and stuck.open_screen_code == "INT"),
    ]
    return _table("navigation: a screen link from a form opens a chooser, not the screen", checks)



def case_record_chooser():
    """Drake's record chooser: read it, refuse a duplicate, never pick a row at random.

    Opening a repeatable screen that already holds records does not always open the screen —
    Drake may put up 'Existing Forms List', a grid of every existing record plus a 'New
    Record' row. Measured 2026-08-15/16: it appeared for a W-2 screen holding two records and
    NOT for an INT screen holding five, so neither "more than one record" nor "clicked from a
    form" predicts it. It is handled wherever it appears rather than predicted.

    THE CHOOSER MAKES THE DUPLICATE CHECK STRONGER, which is the only basis on which a safety
    gate is allowed to change. Until now the check could read only the ONE record that
    happened to be open: a client's third identical 1099-INT was caught if that record was in
    front of us and missed otherwise. The chooser lists every record on the screen, so the
    payer is checked against all of them before anything is opened.

    And Open is never pressed on 'whatever is selected'. Every row is a record in a live
    return; the wrong one puts this document's values on top of somebody else's."""
    import drake_nav as nav

    W2_ROWS = [
        ["#", "TS", "Employer Name", "Wages, Tips", "Federal Tax Withholding"],
        ["1", "T", "navigation test employer", "52000", "6000"],
        ["2", "T", "test employer llc", "52000", "6000"],
        ["New", "New Record", "", "", ""],
    ]

    class _Row:
        """A grid row as pywinauto really hands it back: ALREADY WRAPPED.

        Deliberately has NO `wrapper_object`. The first version of this stub had one, so
        `item.wrapper_object().select()` passed here and raised AttributeError against live
        Drake — swallowed by an outer except, leaving the row unselected, Open unpressed, and
        the run reporting that Drake's list "was answered". A stub that answers to whatever
        it is called is worse than no stub: it certifies the one thing it cannot see."""

        def __init__(self, cells):
            self.cells = cells
            self.selected = False

        def descendants(self, control_type=None):
            return [_Cell(c) for c in self.cells]

        def select(self):
            self.selected = True

    class _Cell:
        def __init__(self, t):
            self.t = t

        def window_text(self):
            return self.t

    class _Btn:
        def __init__(self, owner, name):
            self.owner, self.name = owner, name

        def wrapper_object(self):
            return self

        def invoke(self):
            self.owner.pressed = self.name

    class _Chooser:
        def __init__(self, rows):
            self.rows = [_Row(r) for r in rows]
            self.pressed = None

        def descendants(self, control_type=None):
            return self.rows if control_type == "DataItem" else []

        def child_window(self, auto_id=None, control_type=None):
            return _Btn(self, auto_id)

    class _Driver:
        def __init__(self, chooser):
            self.chooser = chooser

        class _App:
            def __init__(self, outer):
                self.outer = outer

            def window(self, handle=None):
                return self.outer.chooser

        @property
        def app(self):
            return _Driver._App(self)

    quiet = lambda *a, **k: None

    # Taking a NEW record: the New row is selected explicitly, then Open pressed.
    ch = _Chooser(W2_ROWS)
    took = nav.resolve_forms_list(_Driver(ch), 1, take_new=True, log=quiet)
    new_row_selected = ch.rows[-1].selected
    other_rows_selected = any(r.selected for r in ch.rows[:-1])

    # Refusing: Cancel, and Open is never touched.
    ch2 = _Chooser(W2_ROWS)
    left = nav.resolve_forms_list(_Driver(ch2), 1, take_new=False, log=quiet)

    # A chooser with no New row cannot be answered without overwriting somebody.
    ch3 = _Chooser(W2_ROWS[:-1])
    stuck = nav.resolve_forms_list(_Driver(ch3), 1, take_new=True, log=quiet)

    checks = [
        # The shape that caused the live failure, asserted directly so it cannot come back.
        ("a grid row is already wrapped — calling .wrapper_object() on one would fail",
         not hasattr(_Row(["New"]), "wrapper_object")),
        # --- the duplicate check, now against EVERY record on the screen ---------------
        ("a payer already on the screen is found", len(nav.forms_list_matches(W2_ROWS, "test employer llc")) == 1),
        ("...however Drake reformatted it when it stored it",
         len(nav.forms_list_matches(W2_ROWS, "Test Employer, L.L.C.")) == 1),
        ("a payer that is new to this return is not a duplicate",
         nav.forms_list_matches(W2_ROWS, "first national bank") == []),
        ("an empty id never matches anything, so a missing payer is not 'a duplicate'",
         nav.forms_list_matches(W2_ROWS, "") == []),
        ("the header row is never treated as a record",
         nav.forms_list_matches(W2_ROWS, "Employer Name") == []),
        # --- answering it ---------------------------------------------------------------
        ("taking a new record selects the NEW row", took["ok"] and new_row_selected),
        ("...and no existing record is ever selected", not other_rows_selected),
        ("...then presses Open", ch.pressed == nav.FORMS_LIST_OPEN_ID),
        ("refusing presses Cancel", left["ok"] and ch2.pressed == nav.FORMS_LIST_CANCEL_ID),
        ("...and never Open", ch2.pressed != nav.FORMS_LIST_OPEN_ID),
        ("a chooser with no 'New Record' row is REFUSED, not guessed at",
         stuck["ok"] is False and ch3.pressed is None),
        ("...saying so in terms of what it would have had to overwrite",
         "overwrite" in str(stuck.get("reason", ""))),
    ]
    return _table("Drake's record chooser: read it, and let it strengthen the duplicate check", checks)




def case_connector_entry_options():
    """Does the CONNECTOR's parser produce every option the entry path reads?

    Found the hard way, in front of a live Drake. `_run_one_payload` is shared by the folder
    watcher and the cloud connector so that neither can skip a field map, a read-back gate or
    a halt rule. Sharing the CODE without sharing the OPTIONS left the connector's parser
    defining four of the nine attributes that code reads, and the first job it ever claimed
    died instantly on `args.nav_timeout` (2026-08-17).

    What made it worse than a crash: the job was already marked running on the server, so the
    firm-wide halt guard fired and blocked everything until a human cleared it. Nothing was
    typed only because navigation happens before entry — an option read three fields into a
    W-2 would have stopped halfway through somebody's return.

    So this compares what the code READS against what the parsers PRODUCE. Purely static: no
    Drake, no server, runs anywhere."""
    import argparse
    import ast
    import io
    import os

    import agent

    src = io.open(os.path.join(os.path.dirname(os.path.abspath(agent.__file__)), "agent.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    entry_fns = {"_run_one_payload", "_navigate_for_payload", "_enter_one_payload"}
    needed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in entry_fns:
            for n in ast.walk(node):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id == "args"):
                    needed.add(n.attr)

    shared = vars(agent.entry_options_parser().parse_args([]))

    # THE REAL PARSER, imported — not a copy rebuilt here. A copy proves nothing about the
    # parser the connector actually runs: delete `parents=[...]` from connector.py and a
    # copy-based check stays green while the connector goes back to crashing on its first
    # job in front of a live Drake. That is the whole failure this case exists for.
    import connector
    produced = set(vars(connector.build_parser().parse_args(["run"])))

    missing_shared = sorted(needed - set(shared))
    missing_conn = sorted(needed - produced)

    checks = [
        ("the entry path reads the options we think it does", len(needed) >= 8),
        (f"the shared parser covers every one (missing: {missing_shared or 'none'})",
         not missing_shared),
        (f"the connector produces every one (missing: {missing_conn or 'none'})",
         not missing_conn),
        # Named explicitly: this is the one that crashed in front of a live Drake.
        ("nav_timeout, the attribute that crashed the first live run", "nav_timeout" in produced),
        ("and it carries a real default rather than None",
         isinstance(shared.get("nav_timeout"), float)),
        # Defaults must MATCH the folder watcher's. If they drift, the same document entered
        # through the two transports behaves differently — the precise failure that sharing
        # the entry path exists to prevent.
        ("zero-value money fields are skipped by default, as on the watcher",
         shared.get("include_zeros") is False),
        ("navigation — and with it the identity check — is ON by default",
         shared.get("no_navigate") is False),
        ("a client Drake has never seen is REFUSED by default, never created",
         shared.get("create") is False),
        ("Ctrl+N is injected by scancode, which is what Drake accepts",
         shared.get("toggle_method") == "scancode"),
        # A FLAG THAT PROMISES NOT TO TYPE MUST ACTUALLY NOT TYPE.
        # `run --dry-run` existed and was inert: no function in the entry path read it and
        # the driver was constructed without it, so it would have typed into a live return
        # while telling the operator it would not. That is the flag somebody reaches for
        # exactly when they are unsure, which makes a silent lie there worse than having
        # no flag at all. It is gone; this fails if it comes back without being honoured.
        ("the connector exposes no dry-run it cannot honour",
         ("dry_run" not in produced) or ("dry_run" in needed)),
    ]
    return _table("the connector inherits every option the entry path reads", checks)



def case_connector_exe_carries_every_form():
    """Does the packaged .exe carry a field map for every screen the agent claims to drive?

    `agent._load_form_map` resolves a screen to its module with
    `importlib.import_module(name)`. PyInstaller reads `import` statements and cannot see a
    module named by a string at runtime, so every field map has to be listed by hand in
    connector.spec.

    The failure mode if one is missed is unusually nasty. The build succeeds. The exe
    starts, pairs, polls, and claims a job — which marks it `running` on the server — and
    only THEN dies with ModuleNotFoundError. The job is stuck in `running`, which the
    firm-wide halt guard turns into "no further work for this firm", and the operator sees
    a Drake that never moved and a queue that stopped, with nothing on screen to explain it.

    Adding a seventh form is a two-line change in `_FORMS` that nobody would think of as a
    packaging change. This is what notices."""
    import ast
    import io
    import os

    import agent

    here = os.path.dirname(os.path.abspath(agent.__file__))
    spec_path = os.path.join(here, "connector.spec")
    if not os.path.isfile(spec_path):
        return _table("the packaged exe carries every field map",
                      [("connector.spec exists", False)])
    spec = io.open(spec_path, encoding="utf-8").read()

    # Read the declared list out of the spec rather than regexing the whole file, so a
    # module named only in a comment cannot pass for a real entry.
    declared = set()
    for node in ast.walk(ast.parse(spec)):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "FORM_MAP_MODULES"
                and isinstance(node.value, ast.List)):
            declared = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}

    required = {f["module"] for f in agent._FORMS.values()}
    missing = sorted(required - declared)
    stale = sorted(declared - required)

    # Everything the connector reaches for that PyInstaller also cannot infer.
    runtime_needed = {"agent", "drake_driver", "drake_nav", "form_plan", "win32cred",
                      "tkinter", "tray", "pystray", "PIL", "win32event", "winerror"}
    hidden_missing = sorted(m for m in runtime_needed if f"'{m}'" not in spec)

    # The single-instance guard. A windowed exe shows nothing at launch, so a person's
    # natural response to "nothing happened" is to double-click again — the first live
    # install produced twelve connectors from one confused minute of clicking. The run
    # command must check the mutex before it does anything else, including the log tee:
    # a second instance that starts writing the shared log is already a mess.
    connector_src = io.open(os.path.join(here, "connector.py"), encoding="utf-8").read()
    run_body = connector_src.split("def cmd_run(", 1)[-1]
    guard_first = run_body.find("_already_running()")
    tee_after = run_body.find("install_log_tee()")
    single_instance_ok = 0 <= guard_first < tee_after

    # The console/tray pairing. A windowed build with nothing in the tray is a process a
    # firm cannot see, cannot check and cannot stop — worse than the console it replaced.
    silent = "console=False" in spec
    has_tray = (
        "'tray'" in spec
        and "'pystray'" in spec
        and os.path.isfile(os.path.join(here, "tray.py"))
    )
    # And a log, because the console was the only record of what got typed. Without a file
    # on disk, turning the window off deletes the answer to "what did it put in the return?".
    tray_src = ""
    if os.path.isfile(os.path.join(here, "tray.py")):
        tray_src = io.open(os.path.join(here, "tray.py"), encoding="utf-8").read()
    logs_to_disk = "def install_log_tee" in tray_src
    calls_log_tee = "install_log_tee()" in io.open(
        os.path.join(here, "connector.py"), encoding="utf-8").read()

    checks = [
        ("connector.spec declares its field-map modules explicitly", bool(declared)),
        (f"every screen the agent drives has a map in the build (missing: {missing or 'none'})",
         not missing),
        (f"and the list has no modules the agent no longer uses (stale: {stale or 'none'})",
         not stale),
        ("the count matches the agent's own registry",
         len(declared) == len(required) == len(agent._FORMS)),
        (f"the lazily-imported runtime modules are listed too (missing: {hidden_missing or 'none'})",
         not hidden_missing),
        # binding.json describes Drake's UI and the exe cannot run without it.
        ("binding.json ships inside the exe", "'binding.json'" in spec),
        # The console may only go away once something has replaced it. Both directions
        # are checked: a windowed build without a tray, and a tray that ships without the
        # log that is the actual record of what a robot typed into somebody's return.
        ("the build is not silent unless a tray icon ships with it",
         (not silent) or has_tray),
        ("the log file survives the console being turned off",
         (not silent) or (logs_to_disk and calls_log_tee)),
        ("the connector installs the log tee before it prints anything", calls_log_tee),
        ("a second double-click cannot start a second keyboard", single_instance_ok),
        # The test harness must never end up inside a binary that drives a tax return.
        ("the simulator and mutation harness are excluded from the build",
         "'simulate_headsdown'" in spec and "'mutants'" in spec),
    ]
    return _table("the packaged exe carries every field map", checks)


def case_connector_network_death():
    """Does a dying network dial retry — or does it kill the connector?

    The failure this replays is not hypothetical: on the first day a person ran the
    connector unattended, the held long poll's READ timed out while the server restarted
    under a deploy, `TimeoutError` sailed past handlers written for `HTTPError`/`URLError`,
    and the whole process died with a traceback on a window nobody could see. The office's
    view of that failure mode is "nothing has gone into Drake since Tuesday".

    The contract under test: `_request` converts EVERY way a network dies into
    `ServerError`, because `ServerError` is the one exception the run loop treats as
    "wait and ask again". These are real calls through the real `_request` with only
    `urlopen` swapped — a source grep would pass on an except clause that catches the
    wrong thing.

    The last two checks cover the loop itself: whatever still escapes must hit a
    catch-all that continues, and that catch-all must come AFTER KeyboardInterrupt —
    swap the order and Ctrl+C becomes just another "unexpected error, continuing"."""
    import http.client as hc
    import io
    import json as jsonmod
    import os
    import urllib.error
    import urllib.request

    import connector

    def raises_server_error(exc) -> bool:
        real = urllib.request.urlopen

        def dying(*a, **k):
            raise exc

        urllib.request.urlopen = dying
        try:
            connector._request("https://example.invalid", "/x", timeout=1)
            return False  # no exception at all — impossible, but never "pass"
        except connector.ServerError:
            return True
        except Exception:
            return False  # the crash the exe shipped with
        finally:
            urllib.request.urlopen = real

    class _FakeResp:
        """Just enough of a response for `_request` to read a body from."""
        def __init__(self, raw: bytes):
            self._raw = raw
        def read(self) -> bytes:
            return self._raw
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def garbage_body_is_server_error() -> bool:
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _FakeResp(b"<html>hotel wifi login</html>")
        try:
            connector._request("https://example.invalid", "/x", timeout=1)
            return False
        except connector.ServerError:
            return True
        except Exception:
            return False
        finally:
            urllib.request.urlopen = real

    here = os.path.dirname(os.path.abspath(connector.__file__))
    src = io.open(os.path.join(here, "connector.py"), encoding="utf-8").read()
    loop = src.split("def cmd_run(", 1)[-1].split("def build_parser(", 1)[0]
    kb = loop.find("except KeyboardInterrupt")
    # NOT the first `except Exception` — the entry section has its own (it converts a
    # mid-document crash into a reported halt, and it sits earlier in the function). The
    # last-line net is the one that comes AFTER the KeyboardInterrupt handler.
    net = loop.find("except Exception", kb) if kb >= 0 else -1

    checks = [
        ("a read timeout retries instead of crashing (the shipped bug)",
         raises_server_error(TimeoutError("The read operation timed out"))),
        ("a connection reset retries",
         raises_server_error(ConnectionResetError(10054, "reset by peer"))),
        ("a server that hangs up mid-response retries",
         raises_server_error(hc.RemoteDisconnected("closed connection without response"))),
        ("a half-broken HTTP response retries",
         raises_server_error(hc.BadStatusLine("garbage"))),
        ("plain OS-level socket failure retries",
         raises_server_error(OSError(64, "host is down"))),
        ("and the errors it already knew still work",
         raises_server_error(urllib.error.URLError("dns says no"))),
        ("a proxy answering with HTML instead of JSON retries",
         garbage_body_is_server_error()),
        ("HTTP errors still carry their status",
         (lambda: (lambda f: f())(lambda: _http_error_status(connector)))()),
        ("the run loop has a last-line catch-all", net >= 0),
        ("...that cannot swallow Ctrl+C (KeyboardInterrupt handled first)",
         0 <= kb < net),
    ]
    return _table("a dying network dial retries, never crashes", checks)


def _http_error_status(connector) -> bool:
    """A real HTTPError still becomes a ServerError WITH its status code — the loop's
    401 branch (re-pair, do not retry forever) depends on that surviving the rewrite."""
    import io as iomod
    import urllib.error
    import urllib.request

    real = urllib.request.urlopen

    def dying(*a, **k):
        raise urllib.error.HTTPError("https://x", 401, "nope", {}, iomod.BytesIO(b"{}"))

    urllib.request.urlopen = dying
    try:
        connector._request("https://example.invalid", "/x", timeout=1)
        return False
    except connector.ServerError as e:
        return e.status == 401
    except Exception:
        return False
    finally:
        urllib.request.urlopen = real


def case_connector_tray_state():
    """Does the tray icon show a HALT, or does it show "waiting for work"?

    The icon is the only thing an office sees once the console is gone, so the one state
    it must never get wrong is the one that means somebody has to act. A halt stops work
    for the whole firm — the server hands out nothing more until a person reviews the
    document that stopped — and an icon sitting on green through that is a day lost before
    anybody thinks to look at the portal.

    `state_for` is pure and this checks the priority ORDER, not the colours: the interesting
    failure is not a wrong shade, it is a halt that loses to something cosmetic. Ordering
    bugs survive every test that checks one condition at a time, so every check here sets
    at least two conditions true at once and asserts which one wins.

    The last check is the one that pays for the rest: a state that means "act" must not be
    green, whatever future states get added."""
    import os

    try:
        import tray
    except Exception as e:
        return _table("the tray icon reports the state that matters",
                      [(f"tray.py imports ({type(e).__name__})", False)])

    s = tray.state_for

    checks = [
        # Nothing to report while there is nothing to report it for.
        ("a paired, connected, quiet machine is idle",
         s(paired=True, drake_connected=True, server_reachable=True,
           halted=False, working=False) == "idle"),

        # A halt outranks a running job: the document being entered is finished, but the
        # firm is blocked the moment it lands, and that is what a person needs to see.
        ("a halt beats work in progress",
         s(paired=True, drake_connected=True, server_reachable=True,
           halted=True, working=True) == "halted"),

        # A halt outranks Drake being closed. Reopening Drake will not clear it, and
        # "waiting for Drake" sends the office to fix the wrong thing.
        ("a halt beats Drake being closed",
         s(paired=True, drake_connected=False, server_reachable=True,
           halted=True, working=False) == "halted"),

        # ...but an unpaired machine outranks even a halt: nothing on this PC can clear a
        # halt it has no credentials to see, and re-pairing is the only useful next step.
        ("not being paired beats everything",
         s(paired=False, drake_connected=True, server_reachable=True,
           halted=True, working=True) == "unpaired"),

        # Drake closed is the operator's most common problem and must not read as a
        # network fault, which is somebody else's problem entirely.
        ("Drake closed is reported as Drake closed, not as offline",
         s(paired=True, drake_connected=False, server_reachable=False,
           halted=False, working=False) == "no-drake"),

        ("a working machine says so",
         s(paired=True, drake_connected=True, server_reachable=True,
           halted=False, working=True) == "working"),

        # Every state the function can return has to be drawable.
        ("every state it returns has a colour",
         all(st in tray.STATES for st in
             {s(p, d, r, h, w)
              for p in (True, False) for d in (True, False) for r in (True, False)
              for h in (True, False) for w in (True, False)})),

        # The point of the colour: green must never mean "somebody has to do something".
        ("no state that needs a person is green",
         all(tray.STATES[st][0] != tray.STATES["idle"][0]
             for st in ("halted", "unpaired", "no-drake", "offline"))),

        # The log path is what the tray menu opens and what the operator is told; it has
        # to be a real, absolute location rather than something relative to a working
        # directory the exe does not control when it starts at login.
        ("the log has a fixed absolute home", os.path.isabs(str(tray.LOG_PATH))),
    ]
    return _table("the tray icon reports the state that matters", checks)


def case_connector_setup_prefill():
    """Does a fresh machine's setup window open with the Fynn address already filled in?

    The bug this pins down: the setup GUI's address field opened EMPTY on a fresh machine,
    so a tax preparer had to type a Railway URL by hand — and a typo there looks exactly
    like "Fynn is down". The fix is a baked-in DEFAULT_SERVER, resolved through a chain a
    later edit could silently reorder: stored credential first (a paired machine's setup
    window must show where it actually points), then an explicit --server, then the
    default.

    THE REAL cmd_setup RUNS HERE — not a copy of its resolution line. `_ask_gui` is
    swapped for a recorder that captures the prefill and then answers "the person closed
    the window", so the command returns before it can pair, write a credential, or touch
    the network; `_cred_read` is swapped to model each machine. A copy-based check would
    stay green while cmd_setup went back to prefilling ""."""
    import connector

    def prefill(cred, argv):
        captured = {}
        real_ask, real_read = connector._ask_gui, connector._cred_read
        connector._cred_read = lambda: cred

        def fake_ask(server_default):
            captured["prefill"] = server_default
            return None  # window closed: setup must stop, side-effect free

        connector._ask_gui = fake_ask
        try:
            rc = connector.cmd_setup(connector.build_parser().parse_args(argv))
        finally:
            connector._ask_gui, connector._cred_read = real_ask, real_read
        return captured.get("prefill"), rc

    fresh, rc_fresh = prefill(None, ["setup"])
    cli, _ = prefill(None, ["setup", "--server", "https://staging.fynn.example"])
    stored, _ = prefill({"server": "https://firm.fynn.example", "token": "t"},
                        ["setup", "--server", "https://staging.fynn.example"])
    blank_cred, _ = prefill({"server": "", "token": "t"}, ["setup"])

    def pair_requires_server() -> bool:
        # The explicit CLI path stays explicit: `pair` with no --server must refuse to
        # parse, not quietly borrow the default. argparse exits(2) after printing usage;
        # the usage noise is muffled so a PASSING suite does not look like a crash.
        import contextlib
        import io
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                connector.build_parser().parse_args(["pair", "--code", "X"])
            return False
        except SystemExit:
            return True

    checks = [
        ("a fresh machine — nothing stored, no --server — prefills the baked-in default",
         fresh == connector.DEFAULT_SERVER),
        ("...and the default is a real https URL with no trailing slash",
         str(connector.DEFAULT_SERVER).startswith("https://")
         and not str(connector.DEFAULT_SERVER).endswith("/")),
        ("closing the window aborts setup without pairing", rc_fresh == 1),
        ("an explicit --server outranks the baked-in default",
         cli == "https://staging.fynn.example"),
        ("a stored credential outranks both — the window shows where this PC points",
         stored == "https://firm.fynn.example"),
        ("a credential with an EMPTY server still falls through to the default",
         blank_cred == connector.DEFAULT_SERVER),
        ("`pair` still REQUIRES --server — the explicit CLI path stays explicit",
         pair_requires_server()),
    ]
    return _table("the setup window opens already pointing at Fynn", checks)


# ---------------------------------------------------------------------------
# The connector's run loop, rehearsed
# ---------------------------------------------------------------------------
#
# WHY NONE OF THIS READS SOURCE STRINGS. Everything below used to be ONE case that pulled
# `inspect.getsource(connector.cmd_run)` apart and looked for substrings. The code it
# guards carries heavy WHY-comments — the comments themselves say "401", "parent=root",
# "_raise_above_drake" — so the COMMENTS satisfied the checks: five of six deliberate
# mutations (the ping's `return _unpaired(server)` replaced by `pass`, `parent=root`
# deleted from the messagebox, `_raise_above_drake` stubbed to an early `return`) left the
# case green, and the sixth was caught only because it deleted the prose along with the
# code. A test that reads the sentence next to the fix is a test of the sentence.
#
# So these run the REAL `cmd_run`, the real `_unpaired` closure inside it, the real
# `_flush_spool` / `_deliver_result` / `_set_aside`, the real `Tray`, the real `_tell` and
# the real `_ask_gui`. Only the edges of the machine are replaced: the network, Drake, the
# Windows credential store, the clock, the spool folder and tkinter's drawing.

_SIM_CRED = {"server": "https://sim.fynn.invalid", "token": "OLD-TOKEN",
             "agent_id": "a1", "name": "Front desk"}


_SIM_SPOOL_N = [0]


def _sim_spool_dir():
    """A throwaway spool folder — a fresh one per rehearsal.

    NEVER the real one. `cmd_run` DELETES a spool file the moment the server accepts it,
    and here the server is a fake that says yes — so pointing SPOOL_DIR at a real
    ~/.fynn-connector/spool would throw away the record of what a robot actually typed
    into somebody's tax return.

    ONE FOLDER PER REHEARSAL, not one per process: the checks about what was left on disk
    run after the LAST rehearsal in a case, so a shared folder wiped on the way in would
    quietly delete the evidence an earlier rehearsal produced — and the check would report
    "the report was not held" about a run that held it perfectly. Everything lives under a
    per-pid parent, cleared once, so two suites can run at the same time and neither leaves
    a pile behind.
    """
    import os
    import shutil
    import tempfile
    from pathlib import Path as _P
    root = _P(tempfile.gettempdir()) / f"fynn-sim-{os.getpid()}"
    _SIM_SPOOL_N[0] += 1
    if _SIM_SPOOL_N[0] == 1:
        shutil.rmtree(root, ignore_errors=True)
    d = root / f"spool-{_SIM_SPOOL_N[0]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sim_http(status: int, detail: str = "revoked"):
    """A real urllib HTTPError, so `_request` does its real classification on it.

    Handing the loop a pre-built ServerError would skip the part most worth testing: that
    a 401 arriving as an HTTP response still carries its status by the time the run loop
    reads it.
    """
    import io as _io
    import json as _json
    import urllib.error
    return urllib.error.HTTPError(
        "https://sim.fynn.invalid/x", status, detail, {},
        _io.BytesIO(_json.dumps({"error": detail}).encode("utf-8")))


def _rehearse_cmd_run(answer, *, cred=..., drake=True, report=None, setup=None,
                      spool=None, tray_factory=None, frozen=True):
    """One real run of `connector.cmd_run` with only its edges replaced. Returns a record.

    `answer(kind, url, body, rec)` IS the server: it is handed "poll", "ping" or "result"
    and returns the JSON body, raises `_sim_http(status)` for an HTTP failure, or raises
    KeyboardInterrupt — the loop's own clean exit — to end the rehearsal at a chosen point.
    Ending it from the SERVER side is deliberate: a loop that stops because the test told
    it to proves nothing about a loop whose job is to keep running.

    Everything replaced is something that would otherwise touch the machine this runs on:
    the network, Drake, Windows Credential Manager, the clock (recorded, never slept), the
    spool folder, `schtasks`, the log tee (it would append a rehearsal to a real
    connector.log and swallow the suite's own output) and `atexit` (a real registration
    fires at interpreter exit, long after the case that made it).
    """
    import contextlib
    import io
    import json as jsonmod
    import urllib.request

    import agent
    import connector
    import drake_driver as dd
    import tray as tray_mod

    rec = {"events": [], "states": [], "details": [], "toasts": [], "told": [],
           "setups": [], "auth": [], "sleeps": [], "atexit": [], "tray": None,
           "cred": (dict(_SIM_CRED) if cred is ... else cred), "rc": None, "log": "",
           "spool": None, "gave_up": set(), "ran_away": False,
           # Every payload actually handed to the entry path, and whether the fake server
           # has "closed" Drake — see _FakeDriver.window_info.
           "entries": [], "drake_gone": False}

    spool_dir = _sim_spool_dir()
    rec["spool"] = spool_dir
    for name, content in (spool or {}).items():
        (spool_dir / name).write_text(
            content if isinstance(content, str) else jsonmod.dumps(content),
            encoding="utf-8")

    # A rehearsal that cannot end is worse than one that fails: it HANGS the suite, and a
    # hung suite gets killed and read as "the run was fine, it just took too long". Every
    # edge spends from one budget; blowing it ends the run and says so in the record.
    budget = {"n": 0}

    def _spend(what):
        budget["n"] += 1
        if budget["n"] > 400:
            rec["ran_away"] = True
            raise KeyboardInterrupt(f"the loop would not stop ({what})")

    class _Resp:
        def __init__(self, body):
            self._raw = jsonmod.dumps(body or {}).encode("utf-8")

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        _spend("http")
        url = req.full_url
        kind = "result" if "/result" in url else ("ping" if "ready=0" in url else "poll")
        rec["events"].append(kind)
        # The token is recorded per request: a re-pair that does not REBIND leaves the
        # loop polling on the dead one, which looks identical from every other angle.
        rec["auth"].append(req.headers.get("Authorization", ""))
        body = jsonmod.loads(req.data.decode("utf-8")) if req.data else None
        return _Resp(answer(kind, url, body, rec))

    connects = {"n": 0}

    class _FakeDriver:
        def __init__(self, binding, key_pause=0.03):
            self.w32 = object()

        def connect(self):
            connects["n"] += 1
            _spend("drake")
            if not (drake(connects["n"]) if callable(drake) else drake):
                raise RuntimeError("Drake 2025 is not running")

        def window_info(self):
            # `rec["drake_gone"]` lets the fake SERVER close Drake — the answer callback
            # flips it while handing back a job, which is the only way to rehearse the one
            # window that matters: Drake dying inside the 25-second poll, after the
            # pre-poll liveness check passed and before a single key is sent. An empty
            # title is exactly what the real driver returns for a window that is gone.
            if rec.get("drake_gone"):
                return {}
            return {"title": "Drake 2025 Tax Software (sim)"}

        def save_screenshot(self, path):
            # Evidence, not the record. The loop must shrug this off — and a test must
            # never make it write a PNG somewhere on a real machine.
            return {"ok": False}

    class _FakeTray:
        def __init__(self, machine_name="", server="", on_quit=None):
            self.machine_name = machine_name
            self.server = server
            self._on_quit = on_quit

        def start(self):
            return True

        def stop(self):
            rec["events"].append("icon-removed")

        def set_state(self, state, detail=""):
            rec["states"].append(state)
            rec["details"].append(detail)

        def notify(self, title, message):
            rec["toasts"].append((title, message))

    def make_tray(**kw):
        rec["tray"] = (tray_factory or _FakeTray)(**kw)
        return rec["tray"]

    class _FakeTime:
        def sleep(self, secs):
            rec["sleeps"].append(secs)
            _spend("sleep")

    class _FakeAtExit:
        def register(self, fn, *a, **k):
            rec["atexit"].append(fn)
            return fn

        def unregister(self, fn):
            pass

    def fake_setup(ns):
        rec["setups"].append(
            {"server": getattr(ns, "server", None),
             "resume_in_place": bool(getattr(ns, "resume_in_place", False))})
        return 0 if setup is None else setup(rec)

    saved = {k: getattr(connector, k) for k in
             ("_already_running", "_cred_read", "_tell", "cmd_setup", "SPOOL_DIR", "time",
              "is_frozen", "_autostart_points_here", "atexit")}
    saved_gave_up = set(connector._GAVE_UP_ON)
    saved_other = (tray_mod.install_log_tee, tray_mod.Tray, dd.DrakeDriver,
                   agent.load_binding, agent._run_one_payload, urllib.request.urlopen)
    try:
        connector._already_running = lambda: False
        connector._cred_read = lambda: (dict(rec["cred"]) if rec["cred"] else None)
        connector._tell = lambda title, message, raise_it=True: rec["told"].append(
            (title, message))
        connector.cmd_setup = fake_setup
        connector.SPOOL_DIR = spool_dir
        connector.time = _FakeTime()
        connector.is_frozen = lambda: frozen
        # It shells out to schtasks. A test may not rewrite this machine's logon task.
        connector._autostart_points_here = lambda: True
        connector.atexit = _FakeAtExit()
        # Per-process memory of reports already given up on. Left dirty, one case's
        # rejected job id would make the next case's identical job invisible.
        connector._GAVE_UP_ON.clear()
        tray_mod.install_log_tee = lambda: spool_dir.parent / "sim-connector.log"
        tray_mod.Tray = make_tray
        dd.DrakeDriver = _FakeDriver
        agent.load_binding = lambda p: {"navigation": {}}
        # Recorded, not just answered: "was anything typed at all?" is the only question
        # that matters when Drake dies mid-poll, and it cannot be read off the reports.
        def _fake_entry(driver, payload, args, token):
            rec["entries"].append(payload)
            return dict(report or {"ok": True, "entered": 0})

        agent._run_one_payload = _fake_entry
        urllib.request.urlopen = fake_urlopen
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rec["rc"] = connector.cmd_run(connector.build_parser().parse_args(["run"]))
        rec["log"] = out.getvalue()
        rec["gave_up"] = set(connector._GAVE_UP_ON)
    finally:
        for k, v in saved.items():
            setattr(connector, k, v)
        connector._GAVE_UP_ON.clear()
        connector._GAVE_UP_ON.update(saved_gave_up)
        (tray_mod.install_log_tee, tray_mod.Tray, dd.DrakeDriver, agent.load_binding,
         agent._run_one_payload, urllib.request.urlopen) = saved_other
    return rec


def _sim_spool_names(rec, sub=""):
    """What is sitting in the rehearsal's spool folder afterwards, by name."""
    d = rec["spool"] / sub if sub else rec["spool"]
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def case_drake_dies_inside_the_poll():
    """Drake closes while the connector is holding the line. Does it type into the hole?

    RULE 1 SAID "DRAKE FIRST, THEN WORK", AND WAS ENFORCED ONCE — at startup, and later
    only just BEFORE the poll. But that poll holds the line for twenty-five seconds, and a
    preparer closing Drake at 5pm (or a crash, or an IT restart) lands squarely inside it.
    The job then arrives for a dead driver: the server has already recorded it `running`,
    so driving it would type a live client's wages into whatever window inherited the
    focus, and the firm would be left with a stuck job and a screen nobody can account for.

    The honest ending is the one thing that is both safe AND says so: report the job
    UNTOUCHED — nothing was typed, and nothing may be retried — go back to the waiting
    state, and tell the person, because "Drake was closed" is the one cause they can fix
    themselves in five seconds.

    THIS CASE EXISTS BECAUSE MUTATION TESTING FOUND ITS ABSENCE. Deleting the re-check left
    the whole suite green, which is the same as having no test at all."""
    import connector

    posted = []
    connects = []

    def close_drake_then_send_work(kind, url, body, rec):
        if kind == "result":
            posted.append(body)
            # The preparer reopens Drake after seeing the toast. Modelled HERE because the
            # untouched report is the last thing that happens while it is shut, and a
            # rehearsal where Drake never comes back could only ever prove the loop waits.
            rec["drake_gone"] = False
            return {}
        if kind == "poll":
            if rec["events"].count("poll") >= 2:
                raise KeyboardInterrupt   # one job is enough to judge
            # Drake goes away DURING this poll — the pre-poll check already passed, which
            # is precisely the window the old code could not see.
            rec["drake_gone"] = True
            return {"job": {"job_id": "sim-dead", "doc_type": "W-2", "doc_id": "d9",
                            "seq": 1, "payload": {"screen": "W2", "box1": "50000"}}}
        return {}

    def drake_reopens(n):
        # Drake is up at startup, and up again when the loop comes back for it — so a
        # second connect proves the dead driver was dropped rather than reused.
        connects.append(n)
        return True

    gone = _rehearse_cmd_run(close_drake_then_send_work, drake=drake_reopens)

    reports = [b.get("report", {}) for b in posted if isinstance(b, dict)]
    said = " ".join(m for _, m in gone["toasts"])

    checks = [
        ("NOTHING was typed — the entry path is never reached", gone["entries"] == []),
        ("the job is not left silently claimed: a report goes back", len(posted) == 1),
        ("...saying the run did NOT complete",
         bool(posted) and posted[0].get("ok") is False),
        ("...naming the real cause, not the document",
         any("Drake closed" in str(r.get("reason", "")) for r in reports)),
        ("...and marked untouched, so nobody hunts for half a W-2 in a live return",
         any(r.get("untouched") for r in reports)),
        ("...and claiming nothing was entered", all(r.get("entered") == 0 for r in reports)),
        ("the icon goes back to the state a person can act on",
         "no-drake" in gone["states"]),
        ("...and it is said in words, not only a colour",
         "Drake" in said and ("closed" in said or "Open Drake" in said)),
        ("the dead driver is dropped, so reopening Drake reconnects instead of reusing it",
         len(connects) >= 2),
        ("the loop survives it — this is a lost job, not a lost afternoon",
         not gone["ran_away"] and gone["events"].count("poll") >= 2),
    ]
    return _table("Drake closing mid-poll costs a job, never a return", checks)


def case_revoked_machine_is_visible():
    """When Fynn stops recognising this PC, does anybody find out — through every door?

    THE BUG THIS PINS DOWN cost a real setup session. A revoked machine printed one line to
    a log file and exited. In a windowed build that is silence: double-click, Drake flickers
    as the driver attaches, nothing else. It looks exactly like a broken download, and the
    obvious response — download it again, run it again — reproduces it forever, because a
    stored-but-rejected token still routes to `run`.

    THREE DOORS reach the same handler, because a revoked PC has to be told whether or not
    Drake happens to be open and whether or not it is holding a report:
      1. the job poll;
      2. the waiting-for-Drake liveness ping, with Drake SHUT — the state that looks most
         normal, and where a 401 used to be swallowed with everything else;
      3. a report coming back 401, which used to be filed as "Fynn refused this report",
         spooled, and retried every five seconds forever ahead of all new work, behind a
         green "connected, waiting for work" icon.

    And the fourth thing that matters is what must NOT happen: an ordinary server error is
    not a revocation. Get that wrong and one bad afternoon at the data centre tells every
    firm in the country that their PC has been removed."""
    import connector

    def refuse(kind_to_refuse, status=401):
        def answer(kind, url, body, rec):
            if kind == kind_to_refuse:
                raise _sim_http(status)
            return {}
        return answer

    # The person clicks past the dialog without re-pairing: the loop must END, not spin.
    declined = lambda rec: 1

    polled = _rehearse_cmd_run(refuse("poll"), setup=declined)
    shut = _rehearse_cmd_run(refuse("ping"), drake=False, setup=declined)

    def ping_wobbles(kind, url, body, rec):
        # Fynn unreachable is NOT a reason to stop waiting for Drake, and it is certainly
        # not a reason to tell a firm their PC was removed.
        if kind == "ping":
            if rec["events"].count("ping") >= 3:
                raise KeyboardInterrupt
            raise _sim_http(503, "bad gateway")
        return {}

    wobble_shut = _rehearse_cmd_run(ping_wobbles, drake=False, setup=declined)

    def poll_wobbles(kind, url, body, rec):
        if kind == "poll":
            if rec["events"].count("poll") >= 3:
                raise KeyboardInterrupt
            raise _sim_http(500, "boom")
        return {}

    wobble = _rehearse_cmd_run(poll_wobbles, setup=declined)

    def revoked_mid_report(kind, url, body, rec):
        if kind == "poll":
            if rec["events"].count("result"):
                # A second poll would mean the 401 on the report was shrugged off.
                raise KeyboardInterrupt
            return {"job": {"job_id": "sim-77", "doc_type": "W-2", "doc_id": "d1",
                            "seq": 1, "payload": {"screen": "W2"}}}
        if kind == "result":
            raise _sim_http(401)
        return {}

    mid = _rehearse_cmd_run(revoked_mid_report, report={"ok": True, "entered": 7},
                            setup=declined)

    # A checkout has the `setup` subcommand and a console; only the frozen exe, where the
    # person has no other way in, gets the setup window opened for them.
    checkout = _rehearse_cmd_run(refuse("poll"), setup=declined, frozen=False)

    said = " ".join(m for _, m in polled["told"])

    checks = [
        ("a 401 from the job poll puts a dialog on screen", len(polled["told"]) == 1),
        ("...worded for a preparer: what happened and where to click",
         "no longer connected" in said and "Settings" in said and "Connect a PC" in said),
        ("...and opens the setup window so they can act on what they just read",
         len(polled["setups"]) == 1),
        ("...asking it to RESUME this loop, never to start a second connector",
         polled["setups"][:1] == [{"server": _SIM_CRED["server"], "resume_in_place": True}]),
        ("...leaving the icon on 'unpaired', not on the green it was showing a moment ago",
         polled["states"] == ["starting", "idle", "unpaired"]),
        ("...and the loop ends rather than hammering a dead token",
         polled["rc"] == 1 and polled["events"] == ["poll"] and not polled["ran_away"]),

        ("with DRAKE SHUT the liveness ping says it too — the state that looks normal",
         shut["events"] == ["ping"] and len(shut["told"]) == 1
         and len(shut["setups"]) == 1 and shut["rc"] == 1),
        ("...having said 'waiting for Drake' first, then 'unpaired'",
         shut["states"] == ["starting", "no-drake", "unpaired"]),

        ("but an unreachable Fynn does NOT read as a revocation",
         not wobble_shut["told"] and not wobble_shut["setups"]
         and set(wobble_shut["states"]) == {"starting", "no-drake"}),
        ("...and it keeps waiting for Drake at the usual interval",
         wobble_shut["sleeps"][:1] == [connector.DRAKE_RETRY_SEC]
         and wobble_shut["rc"] == 0),
        ("a 500 on the poll is a bad afternoon, not a removed machine",
         not wobble["told"] and not wobble["setups"] and "offline" in wobble["states"]),

        ("a 401 on the REPORT reaches the same handler",
         len(mid["told"]) == 1 and len(mid["setups"]) == 1
         and mid["states"][-1:] == ["unpaired"]),
        ("...without burning six retries on a token the server has already refused",
         mid["events"] == ["poll", "result"]),
        ("...and the report is HELD, because it is the record of what went into a return",
         _sim_spool_names(mid) == ["sim-77.json"]),
        ("...with no toast sending anybody to a portal that never got it",
         not any("Review it in the Fynn portal" in m for _, m in mid["toasts"])),
        ("...and no wedge: the loop stops instead of retrying every five seconds",
         mid["rc"] == 1 and not mid["ran_away"]),

        ("from a terminal it still says so, but opens no window — `setup` is right there",
         len(checkout["told"]) == 1 and not checkout["setups"]
         and checkout["rc"] == 1),
    ]
    return _table("a machine Fynn no longer knows says so, through every door", checks)


def case_repair_resumes_the_loop():
    """After a successful re-pair, is anything actually running?

    THE INCIDENT THIS PINS DOWN was manufactured by the fix for the one above. The preparer
    read "This PC is no longer connected to Fynn", clicked OK, pasted a fresh code, and read
    "Connected as 'Front desk'. This PC will start the connector automatically when you log
    in. Leave Drake open on its home screen." They clicked OK — and the tray icon vanished.
    `cmd_setup` returned 0, `_unpaired` returned that 0, the run loop returned it, and the
    process exited at the exact moment they were most certain they had just fixed it.
    Nothing ran until the next sign-in, which on a desk that stays signed in is never.

    The same shape sat on the first-run path: an unpaired machine paired, was told to leave
    Drake open, and then ran nothing at all.

    So this drives the real thing three ways — a re-pair from inside the loop, a first run
    that pairs from inside `cmd_run`, and the real `cmd_setup` itself — and asks the only
    question that matters afterwards: is the loop still going, and is it going with the NEW
    token? A re-pair that leaves the old token bound ping-pongs straight back into the
    same dialog, which from the outside is indistinguishable from a re-pair that failed."""
    import connector

    def repaired(rec):
        rec["cred"] = {"server": _SIM_CRED["server"], "token": "NEW-TOKEN",
                       "agent_id": "a2", "name": "Front desk (re-paired)"}
        return 0

    def revoked_then_repaired(kind, url, body, rec):
        if kind == "result":
            # The first attempt goes out on the dead token; whatever comes after the
            # re-pair must not.
            if rec["auth"][-1] == "Bearer OLD-TOKEN":
                raise _sim_http(401)
            return {"ok": True}
        if kind == "poll":
            n = rec["events"].count("poll")
            if n == 1:
                return {"job": {"job_id": "sim-7", "doc_type": "W-2", "doc_id": "d1",
                                "seq": 1, "payload": {"screen": "W2"}}}
            if n >= 3:
                raise KeyboardInterrupt   # two clean polls after the re-pair is enough
            return {}
        return {}

    again = _rehearse_cmd_run(revoked_then_repaired, report={"ok": True, "entered": 7},
                              setup=repaired)
    after = again["auth"][2:]            # everything after poll #1 and the 401'd report

    # A machine whose credential cannot be read at all — `unpair` left the logon task
    # behind, the profile was reset, Credential Manager was wiped. This used to print one
    # line to a log nobody reads and exit within a second, at every single logon.
    def stop_at_the_first_poll(kind, url, body, rec):
        raise KeyboardInterrupt

    first_run = _rehearse_cmd_run(stop_at_the_first_poll, cred=None,
                                  setup=lambda rec: repaired(rec))
    gave_up = _rehearse_cmd_run(stop_at_the_first_poll, cred=None, setup=lambda rec: 1)

    def setup_run(*, frozen=True, resume=False):
        """The REAL cmd_setup, with pairing, the credential store and schtasks faked."""
        rec = {"told": [], "ran": [], "wrote": [], "rc": None}
        names = ("_ask_gui", "_request", "_cred_write", "_install_autostart", "_tell",
                 "is_frozen", "cmd_run", "_cred_read", "_log_location")
        saved = {n: getattr(connector, n) for n in names}
        try:
            connector._ask_gui = lambda d: (_SIM_CRED["server"], "CODE-1")
            connector._request = lambda *a, **k: {"token": "T2", "agent_id": "a2",
                                                  "name": "Front desk"}
            connector._cred_write = lambda data: rec["wrote"].append(data)
            connector._install_autostart = lambda: True
            connector._tell = lambda t, m, raise_it=True: rec["told"].append((t, m))
            connector.is_frozen = lambda: frozen
            connector.cmd_run = lambda args: (rec["ran"].append(getattr(args, "cmd", "?"))
                                              or 0)
            connector._cred_read = lambda: None
            connector._log_location = lambda: "C:\\sim\\connector.log"
            ns = connector.build_parser().parse_args(["setup"])
            ns.resume_in_place = resume
            rec["rc"] = connector.cmd_setup(ns)
        finally:
            for n, v in saved.items():
                setattr(connector, n, v)
        return rec

    exe = setup_run()
    resumed = setup_run(resume=True)
    dev = setup_run(frozen=False)
    exe_said = " ".join(m for _, m in exe["told"])
    resumed_said = " ".join(m for _, m in resumed["told"])
    dev_said = " ".join(m for _, m in dev["told"])

    checks = [
        ("a successful re-pair does NOT end the connector",
         again["rc"] == 0 and not again["ran_away"]),
        ("...the loop carries straight on, polling again",
         again["events"].count("poll") >= 2),
        ("...on the NEW token — an unrebound one ping-pongs back into the same dialog",
         after and all(a == "Bearer NEW-TOKEN" for a in after)),
        ("...and the report it was holding goes out and is cleared off the disk",
         again["events"].count("result") == 2 and _sim_spool_names(again) == []),
        ("...with the icon off the red 'unpaired' it was left on",
         again["states"][-1] == "idle"),
        ("...and the tooltip rebuilt for the name it was re-paired under",
         getattr(again["tray"], "machine_name", "") == "Front desk (re-paired)"),

        ("a machine with no credential is told, then offered the pairing window",
         len(first_run["told"]) == 1 and len(first_run["setups"]) == 1),
        ("...in place, because this run already holds the single-instance mutex",
         first_run["setups"][0]["resume_in_place"] is True),
        ("...and once it pairs, the loop actually runs",
         first_run["events"] == ["poll"] and first_run["rc"] == 0),
        ("...while a pairing that did not happen stops honestly instead",
         gave_up["rc"] == 1 and gave_up["events"] == []),

        ("setup from a double-click ENDS BY RUNNING, rather than exiting on 'connected'",
         exe["ran"] == ["run"] and exe["rc"] == 0),
        ("...and says so, instead of implying it from 'it will start when you log in'",
         "straight away" in exe_said and "NOT running" not in exe_said),
        ("setup called from inside a live loop starts no second connector",
         resumed["ran"] == [] and resumed["rc"] == 0
         and "carrying on" in resumed_said),
        ("a developer's `setup` in a checkout stays explicit, and says it is not running",
         dev["ran"] == [] and "NOT running yet" in dev_said),
    ]
    return _table("a re-pair leaves something running, on the token it just got", checks)


def case_held_report_never_blocks_the_loop():
    """One report Fynn will never accept — does the whole machine stop entering documents?

    THE PUREST CASE OF THIS AUDIT'S BUG CLASS. `_flush_spool` runs before every poll, and
    it used to hold the loop on ANY non-zero count: a job cancelled server-side (404), a
    screenshot over the size cap (413), a body the server would not take (400), or a file
    that could not even be parsed. Every five seconds, forever, ahead of all new work,
    while the icon sat on green "connected, waiting for work" — a state byte-for-byte
    identical to working. Documents queued in the portal were never entered and the
    preparer was given no reason of any kind.

    The fix has two halves and both are load-bearing: a report that can never be delivered
    is moved ASIDE (never deleted — rule 2 says it is the record of what a robot typed into
    a live return) and said out loud, while a report that is merely WAITING on a bad network
    is still retried and still holds the gate, because that one clears itself.

    The last rehearsal is the layer under the fix: a report that cannot even be moved aside
    must still stop blocking, or the wedge simply moves one folder deeper."""

    def stop_at_the_poll(kind, url, body, rec):
        # Reaching the poll AT ALL is the point of this case.
        raise KeyboardInterrupt

    def refuse_the_held_report(kind, url, body, rec):
        if kind == "result":
            raise _sim_http(404, "that job no longer exists")
        # An EMPTY poll on the pass after the rejection: nothing to do, which is the
        # moment the loop would normally paint the icon green again.
        if rec["events"].count("poll") >= 2:
            raise KeyboardInterrupt
        return {}

    payload = {"ok": True, "report": {"entered": 3, "canary": "sim-canary-9f3"}}
    dead = _rehearse_cmd_run(refuse_the_held_report, spool={"sim-404.json": payload})
    unreadable = _rehearse_cmd_run(stop_at_the_poll, spool={"sim-bad.json": "{ half a rep"})
    torn = _rehearse_cmd_run(stop_at_the_poll, spool={"sim-torn.json.part": payload})

    def wobbly(kind, url, body, rec):
        if kind == "result":
            if rec["events"].count("result") >= 8:
                raise KeyboardInterrupt
            raise _sim_http(503, "gateway timeout")
        raise KeyboardInterrupt   # a poll here would mean new work claimed ahead of it

    waiting = _rehearse_cmd_run(wobbly, spool={"sim-503.json": payload})

    def refuse_twice(kind, url, body, rec):
        if kind == "result":
            raise _sim_http(404, "that job no longer exists")
        if rec["events"].count("poll") >= 2:
            raise KeyboardInterrupt
        return {}

    # A FILE where the "rejected" folder needs to go, so the move itself cannot work.
    immovable = _rehearse_cmd_run(refuse_twice,
                                  spool={"sim-404.json": payload,
                                         "rejected": "a file in the folder's place"})

    import json as jsonmod
    kept = rejected_content = None
    if (dead["spool"] / "rejected" / "sim-404.json").is_file():
        rejected_content = jsonmod.loads(
            (dead["spool"] / "rejected" / "sim-404.json").read_text(encoding="utf-8"))
        kept = True

    checks = [
        ("a report Fynn will never accept stops blocking the poll",
         "poll" in dead["events"] and dead["rc"] == 0 and not dead["ran_away"]),
        ("...after ONE attempt, not one every five seconds forever",
         dead["events"].count("result") == 1),
        ("...moved aside rather than deleted — it is the record of what was typed",
         kept is True and rejected_content == payload
         and _sim_spool_names(dead) == []),
        ("...and said out loud, naming what the person has to do about it",
         any("could not be sent" in t and "Fynn support" in m
             for t, m in dead["toasts"])),
        ("...and the icon never goes back to green, not even with nothing left to do",
         dead["states"][:2] == ["starting", "idle"]
         and set(dead["states"][2:]) == {"error"}),

        ("a held report that cannot even be read is set aside, not retried for ever",
         unreadable["events"].count("result") == 0
         and _sim_spool_names(unreadable, "rejected") == ["sim-bad.json"]
         and "poll" in unreadable["events"]),
        ("a half-written .json.part is found at all — glob('*.json') never matched it",
         _sim_spool_names(torn, "rejected") == ["sim-torn.json.part"]
         and "poll" in torn["events"] and bool(torn["toasts"])),

        ("a report the network merely could not deliver is NOT given up on",
         _sim_spool_names(waiting) == ["sim-503.json"]
         and _sim_spool_names(waiting, "rejected") == []),
        ("...it is retried hard, the way a stranded return deserves",
         waiting["events"].count("result") >= 6),
        ("...and it does hold the gate — but says so, in amber, never in green",
         "poll" not in waiting["events"]
         and set(waiting["states"][2:]) == {"offline"}
         and any("still to go" in d for d in waiting["details"])),

        ("a report that cannot even be moved aside still stops blocking the loop",
         "poll" in immovable["events"] and immovable["rc"] == 0),
        ("...is retried once and then remembered, not attempted on every pass",
         immovable["events"].count("result") == 1
         and immovable["events"].count("poll") == 2
         and "sim-404" in immovable["gave_up"]),
        ("...is still on disk, and its contents go to the log as the last record",
         (immovable["spool"] / "sim-404.json").is_file()
         and "sim-canary-9f3" in immovable["log"]),
    ]
    return _table("a report that cannot be sent never stops the ones that can", checks)


def case_drake_closed_is_said_out_loud():
    """Drake is shut on the first real run. What does the customer actually see?

    Before this: a grey dot, hidden by default in Windows' ^ overflow flyout, the same grey
    as "starting" — so even somebody watching the icon could not tell "booting up, wait" from
    "I need you to open Drake". The instruction that would have fixed it lived in a tooltip,
    which requires knowing the icon exists, finding it, and hovering. The loop did the right
    thing every fifteen seconds and told only its log file.

    Three things have to hold, and a fourth must not break: the state gets ITS OWN colour;
    it puts WORDS on screen on the transition; it does not repeat those words so often that
    people learn to dismiss the only channel we have; and when the preparer does what they
    were asked, they are told it worked — otherwise a first run can finish a whole day
    without one positive confirmation."""
    import connector
    import tray as tray_mod

    def opens_on_the_third_look(n):
        return n >= 3

    def liveness_then_stop(kind, url, body, rec):
        # The liveness ping must be ANSWERED, not used as the brake: it is the whole
        # reason the portal can say "PC on, Drake not open" instead of "offline".
        if kind == "poll":
            raise KeyboardInterrupt
        return {}

    opened = _rehearse_cmd_run(liveness_then_stop, drake=opens_on_the_third_look)

    def ping_twice(kind, url, body, rec):
        if rec["events"].count("ping") >= 2:
            raise KeyboardInterrupt
        return {}

    quiet = _rehearse_cmd_run(ping_twice, drake=False, frozen=False)

    waiting = [(t, m) for t, m in opened["toasts"] if "waiting for Drake" in t]
    connected = [(t, m) for t, m in opened["toasts"] if "connected to Drake" in t]
    colours = {name: tray_mod.STATES[name][0]
               for name in ("no-drake", "starting", "offline", "idle")}

    checks = [
        ("Drake shut puts words on screen, not just a colour on an icon",
         len(waiting) == 1),
        ("...telling the preparer the one thing they can do about it",
         bool(waiting) and "Open Drake" in waiting[0][1]),
        ("...and the tooltip carries the same instruction",
         any("Open Drake" in d for d in opened["details"])),
        ("...said once, not on every fifteen-second pass",
         opened["states"][:3] == ["starting", "no-drake", "no-drake"]),
        ("...and repeated slowly enough to stay worth reading (>= 10 min apart)",
         connector.NO_DRAKE_SAY_EVERY * connector.DRAKE_RETRY_SEC >= 600),
        ("'waiting for Drake' does not wear the same colour as 'starting'",
         colours["no-drake"] != colours["starting"]),
        ("...nor amber, which already means 'cannot reach Fynn'",
         colours["no-drake"] != colours["offline"] != colours["idle"]),
        ("when Drake finally opens, the person who did what was asked is told it worked",
         len(connected) == 1),
        ("...and that confirmation still never claims a filing",
         bool(connected) and "never files" in connected[0][1]),
        ("...and the loop then gets on with work",
         opened["states"][-1] == "idle" and "poll" in opened["events"]),
        ("in a checkout it stays quiet — the console in front of the developer said it",
         quiet["toasts"] == [] and "no-drake" in quiet["states"]),
    ]
    return _table("Drake closed is said in words, not left to a grey dot", checks)


def case_quit_waits_for_the_loop():
    """Click Quit. The icon vanishes. Is it safe to start typing into Drake?

    IT WAS NOT. `_quit` removed the icon first and asked the loop to stop second — and the
    loop deliberately stops only at a safe point, never mid-document. So between the click
    and the actual stop there could be seconds (a poll), or many minutes (a report's retry
    chain, a document being entered), in which the only confirmation this product gives said
    "gone" while the robot was still typing into a live tax return. Somebody who then starts
    keying by hand is the second keyboard this whole design exists to prevent.

    The REAL Tray runs here, with only pystray's icon object faked, and the REAL run loop
    drives it: Quit is clicked in the middle of a report going out, exactly as it would be.
    The latch matters as much as the ordering — without it the document finishes, the loop
    calls `_state("idle")`, and the icon repaints GREEN in front of somebody who just asked
    it to stop, which is a worse lie than the one being fixed."""
    import tray as tray_mod

    class _Icon:
        """Just enough pystray to be repainted, toasted at, and removed."""
        def __init__(self):
            self.stopped = 0
            self.icon = None
            self.title = ""
            self.toasts = []

        def stop(self):
            self.stopped += 1

        def notify(self, message, title):
            self.toasts.append((title, message))

    # -- the click on its own, against the real Tray -------------------------
    icon = _Icon()
    asked = []
    t = tray_mod.Tray(machine_name="Front desk", on_quit=lambda: asked.append("stop"))
    t._icon = icon
    t.set_state("working", "entering a W-2")
    t._quit()
    stopped_at_click, said_at_click, title_at_click = icon.stopped, t._state, t._title()
    t.set_state("idle")            # the document finishes AFTER the click
    after_document = t._state
    t.stop()                       # what atexit does when the process really ends

    # -- and again, through the real run loop --------------------------------
    loop_icon = _Icon()
    # Captured BEFORE the rehearsal swaps tray_mod.Tray for its factory — reading it
    # inside would hand the factory back to itself.
    real_tray_class = tray_mod.Tray

    def tray_factory(machine_name="", server="", on_quit=None):
        real = real_tray_class(machine_name=machine_name, server=server, on_quit=on_quit)
        real._icon = loop_icon
        real.start = lambda: True   # pystray is the one part that cannot run headless
        return real

    def quit_mid_report(kind, url, body, rec):
        if kind == "result":
            rec["tray"]._quit()     # they click Quit while the report is going out
            return {"ok": True}
        if rec["events"].count("poll") > 1:
            # A second poll means the quit was not honoured at the top of the iteration.
            raise KeyboardInterrupt
        return {"job": {"job_id": "sim-9", "doc_type": "W-2", "doc_id": "d1", "seq": 1,
                        "payload": {"screen": "W2"}}}

    run = _rehearse_cmd_run(quit_mid_report, report={"ok": True, "entered": 7},
                            tray_factory=tray_factory)
    survived_the_run = loop_icon.stopped
    # What atexit would do at the real end of the process. Called here so the check below
    # is an assertion rather than a side effect hidden inside one.
    for fn in run["atexit"]:
        fn()

    checks = [
        ("clicking Quit does NOT remove the icon", stopped_at_click == 0),
        ("...it asks the loop to stop", asked == ["stop"]),
        ("...and says so in words a person can read while they wait",
         said_at_click == "stopping" and "stopping" in title_at_click),
        ("...and nothing repaints over that — not even the document that was in flight",
         after_document == "stopping"),
        ("the icon still goes when the process really ends", icon.stopped == 1),

        ("through the real loop: the icon survives the click",
         survived_the_run == 0 and run["rc"] == 0),
        ("...the document in flight is reported first",
         run["events"] == ["poll", "result"]),
        ("...the loop then stops at its safe point, without polling again",
         run["events"].count("poll") == 1 and not run["ran_away"]),
        ("...and the icon was never repainted green behind the quit",
         run["tray"]._state == "stopping"),
        ("removal is left to the process ending, which is what atexit is for",
         run["atexit"][:1] == [run["tray"].stop]),
        ("...and that removal does work when it fires", loop_icon.stopped == 1),
    ]
    return _table("Quit: the icon disappearing means stopped, never 'asked to stop'",
                  checks)


def _fake_tkinter():
    """A tkinter that draws nothing, so the real dialog code can be run headlessly.

    The alternative is real tkinter, which would open windows over whoever is running the
    suite — and would make the case unrunnable on a build machine. Everything the dialogs
    actually do to WIN THE FOREGROUND is recorded instead of performed.
    """
    import types

    rec = {"roots": [], "boxes": [], "entries": [], "after": [], "typed": [],
           "press_return": False, "explode": False}

    class _Widget:
        def __init__(self, master=None, **kw):
            self.master = master
            self.kw = dict(kw)

        def grid(self, *a, **k):
            return self

        def config(self, **k):
            self.kw.update(k)

        def focus(self):
            pass

        def bind(self, seq, fn):
            pass

    class _Entry(_Widget):
        def __init__(self, master=None, **kw):
            super().__init__(master, **kw)
            self._text = rec["typed"].pop(0) if rec["typed"] else ""
            rec["entries"].append(self)

        def insert(self, index, text):
            self._text = str(text)

        def get(self):
            return self._text

    class _Root(_Widget):
        def __init__(self, *a, **k):
            if rec["explode"]:
                raise RuntimeError("no display name and no $DISPLAY environment variable")
            super().__init__(*a, **k)
            self.raised = []
            self.bindings = {}
            self.destroyed = 0
            rec["roots"].append(self)

        def title(self, *a):
            pass

        def resizable(self, *a):
            pass

        def withdraw(self):
            pass

        def destroy(self):
            self.destroyed += 1

        def attributes(self, *a):
            self.raised.append(tuple(a))

        def lift(self):
            self.raised.append(("lift",))

        def focus_force(self):
            self.raised.append(("focus_force",))

        def after(self, ms, fn):
            rec["after"].append((ms, fn))

        def bind(self, seq, fn):
            self.bindings[seq] = fn

        def mainloop(self):
            # "The person pressed Enter" or "the person closed the window" — the two ways
            # out of the pairing dialog, and setup must do the right thing with both.
            fn = self.bindings.get("<Return>")
            if rec["press_return"] and fn is not None:
                fn(None)

    tk = types.ModuleType("tkinter")
    tk.Tk = _Root
    ttk = types.ModuleType("tkinter.ttk")
    ttk.Frame = _Widget
    ttk.Label = _Widget
    ttk.Entry = _Entry
    ttk.Button = _Widget
    mb = types.ModuleType("tkinter.messagebox")
    mb.showinfo = lambda title, message, parent=None: rec["boxes"].append(
        (title, message, parent))
    tk.ttk = ttk
    tk.messagebox = mb
    return rec, {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb}


def case_dialogs_are_raised_over_drake():
    """Does anything this program says actually appear IN FRONT of Drake?

    WITHOUT THE RAISE, EVERY WINDOW THIS PROGRAM SHOWS IS INVISIBLE IN PRACTICE. Drake runs
    maximised and takes the foreground the moment the driver attaches, so a plain `Tk()`
    opens BEHIND it: created, visible, waiting — while the process blocks on a click nobody
    knows to make and the person sees Drake flicker and nothing else. That cost a real setup
    session, and it is what makes every other message in this file worth nothing.

    The REAL `_tell`, `_ask_gui` and `_raise_above_drake` run here against a tkinter that
    draws nothing. The version of this case that read source strings could not tell the
    difference between the fix and the comment ABOVE the fix: deleting `parent=root` — which
    is the whole reason a messagebox inherits the raise, since a parentless one builds its
    own toplevel and ignores it — left the check green, because the comment two lines up
    says the words "parent=root"."""
    import contextlib
    import io
    import sys as _sys

    import connector
    import tray as tray_mod

    rec, mods = _fake_tkinter()
    saved_mods = {k: _sys.modules.get(k) for k in mods}
    saved_frozen, saved_box = connector.is_frozen, tray_mod.message_box
    boxed = []
    try:
        _sys.modules.update(mods)
        connector.is_frozen = lambda: True     # a checkout prints instead of showing a box
        tray_mod.message_box = lambda t, m: boxed.append((t, m)) or True

        # Both streams: _tell prints its own line, and the tkinter-is-gone rehearsal
        # below prints the failure to stderr. A PASSING case must not look like a crash.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            connector._tell("Fynn connector", "This PC is no longer connected to Fynn.")
            told_root = rec["roots"][-1]
            told_box = rec["boxes"][-1]
            # Let go of topmost when the window has won the front, or a pairing dialog
            # pins itself over every other window on the machine while it is open.
            dropped_before = list(told_root.raised)
            for _, fn in list(rec["after"]):
                fn()

            # The one caller that must NOT steal focus: it runs on a background thread
            # while the driver may be attached, and a topmost Fynn box can land inside an
            # evidence screenshot or a read-back OCR crop, which is a safety gate.
            connector._tell("Fynn connector", "Running in the background.", raise_it=False)
            quiet_root = rec["roots"][-1]
            quiet_box = rec["boxes"][-1]

            rec["typed"], rec["press_return"] = ["", "CODE-1"], True
            asked = connector._ask_gui("https://prefill.fynn.invalid")
            ask_root = rec["roots"][-1]
            prefilled = rec["entries"][0]._text if rec["entries"] else None

            rec["press_return"] = False
            rec["typed"] = ["", ""]
            closed = connector._ask_gui("https://prefill.fynn.invalid")

            # tkinter gone entirely. The fallback used to be print(), which in the
            # windowed build it exists for writes to a stdout that is None: a safety net
            # made of the same material as the hole.
            rec["explode"] = True
            connector._tell("Fynn", "Fynn hit a problem it did not expect.")
    finally:
        for k, v in saved_mods.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
        connector.is_frozen, tray_mod.message_box = saved_frozen, saved_box

    # The real _raise_above_drake, against a window that records instead of drawing.
    class _Root:
        def __init__(self, lift_raises=False):
            self.calls = []
            self.after = lambda ms, fn: self.calls.append(("after", ms, fn))
            self._lift_raises = lift_raises

        def attributes(self, *a):
            self.calls.append(("attributes",) + tuple(a))

        def lift(self):
            if self._lift_raises:
                raise RuntimeError("the window manager said no")
            self.calls.append(("lift",))

        def focus_force(self):
            self.calls.append(("focus_force",))

    plain = _Root()
    connector._raise_above_drake(plain)
    after_cb = [c for c in plain.calls if c[0] == "after"]
    if after_cb:
        after_cb[0][2]()
    stubborn = _Root(lift_raises=True)
    lost = False
    try:
        connector._raise_above_drake(stubborn)
    except Exception:
        lost = True

    checks = [
        ("a message box is raised over Drake before it is shown",
         ("-topmost", True) in dropped_before and ("lift",) in dropped_before),
        ("...and PARENTED to the raised window, or it ignores the raise entirely",
         told_box[2] is told_root),
        ("...carrying the words it was given",
         told_box[0] == "Fynn connector"
         and "no longer connected" in told_box[1]),
        ("...and it stops being topmost afterwards, rather than pinning itself over Drake",
         ("-topmost", False) in told_root.raised),
        ("the one notice that must not steal focus does not raise itself",
         quiet_root.raised == [] and quiet_box[2] is quiet_root),
        ("the pairing window is raised over Drake too",
         ("-topmost", True) in ask_root.raised),
        ("...opens with the Fynn address already in it",
         prefilled == "https://prefill.fynn.invalid"),
        ("...and hands back what was typed",
         asked == ("https://prefill.fynn.invalid", "CODE-1")),
        ("...while a window that was simply closed pairs nothing", closed is None),
        ("when tkinter is gone, the last resort is a box Windows draws, not print()",
         bool(boxed) and "did not expect" in boxed[-1][1]),

        ("the raise takes topmost, lifts, and takes the keyboard for the paste",
         ("attributes", "-topmost", True) in plain.calls
         and ("lift",) in plain.calls and ("focus_force",) in plain.calls),
        ("...and schedules the release of topmost rather than keeping it",
         bool(after_cb) and ("attributes", "-topmost", False) in plain.calls),
        ("a window that cannot be raised is still shown — never lost over cosmetics",
         not lost),
    ]
    return _table("every window this program shows opens in front of Drake", checks)


def case_tray_notify_is_output_only():
    """Can a toast take the connector down? The tray reports state, it never decides it —
    and a notification failure must never break the thing it reports on.

    These call the REAL Tray.notify. The icon underneath is faked three ways: absent (a
    machine with no pystray), working (to pin pystray's argument order — `notify(message,
    title)`, backwards from ours, and swapping them silently titles every toast with its
    own body), and actively broken (pystray raising out of the shell call), because those
    are the ways Windows actually behaves."""
    try:
        import tray as tray_mod
    except Exception as e:
        return _table("a toast can fail; the tray cannot",
                      [(f"tray.py imports ({type(e).__name__})", False)])

    calls = []

    class _GoodIcon:
        def notify(self, *a, **k):
            calls.append((a, k))

    class _BrokenIcon:
        def notify(self, *a, **k):
            raise RuntimeError("Shell_NotifyIcon said no")

    t = tray_mod.Tray(machine_name="SimPC")

    def survives(icon) -> bool:
        t._icon = icon
        try:
            t.notify("Fynn — document entered", "W-2 entered (7 fields).")
            return True
        except BaseException:
            return False

    no_icon_ok = survives(None) and not calls  # nobody to toast at: silent no-op
    good_ok = survives(_GoodIcon())
    passed = calls[-1][0] if calls else ()
    broken_ok = survives(_BrokenIcon())

    checks = [
        ("no icon on this machine: notify is a silent no-op", no_icon_ok),
        ("a working icon gets exactly the one toast", good_ok and len(calls) == 1),
        ("...in pystray's order — (message, title), not (title, message)",
         passed == ("W-2 entered (7 fields).", "Fynn — document entered")),
        ("an icon that RAISES does not take notify with it", broken_ok),
        ("a broken toast leaves the tray's state alone", t._state == "starting"),
    ]
    return _table("a toast can fail; the tray cannot", checks)


def case_connector_run_toasts():
    """When a document finishes, does the operator hear about it — and can that toast, at
    its worst, cost anything?

    Before this, a finished document was invisible: the operator walked back to the portal
    and guessed. So the run loop toasts after each report lands. The doctrine that guards
    it: notifications are OUTPUT ONLY. A toast must never claim more than the report said
    — which is why the ORDER of events is asserted, not just their presence — and a toast
    that fails must never break the loop it reports on.

    THE REAL cmd_run RUNS HERE. Like case_connector_network_death, only the edges are
    swapped: urlopen answers with a canned job, DrakeDriver and _run_one_payload are stubs
    (entry logic has its own hundred cases), and the injected tray's notify RAISES on
    every call — the hostile version of a toast. The iteration must complete anyway:
    report delivered first, tray state set, and the loop back at its safe stop point."""
    import contextlib
    import io
    import json as jsonmod
    import tempfile
    import urllib.request
    from pathlib import Path as _P

    import agent
    import connector
    import drake_driver as dd
    import tray as tray_mod

    def run_once(report):
        events = []   # everything observable, in the order it happened
        toasts = []
        states = []

        class _FakeTray:
            def __init__(self, machine_name="", server="", on_quit=None):
                self._on_quit = on_quit

            def start(self):
                return True

            def stop(self):
                pass

            def set_state(self, state, detail=""):
                states.append(state)

            def notify(self, title, message):
                toasts.append((title, message))
                events.append("notify")
                # Ask for the safe-point quit first, then blow up. The raise is the
                # point: the real Tray.notify never raises, so this is strictly nastier
                # than anything pystray can do, and the loop must shrug it off.
                if self._on_quit is not None:
                    self._on_quit()
                raise RuntimeError("toast backend exploded")

        class _FakeDriver:
            def __init__(self, binding, key_pause=0.03):
                self.w32 = object()

            def connect(self):
                pass

            def window_info(self):
                return {"title": "Drake 2025 (sim)"}

            def save_screenshot(self, path):
                return {"ok": False}  # no screenshot; the loop must shrug (it is evidence,
                                      # not the record) and never write a file from here

        class _Resp:
            def __init__(self, body):
                self._raw = jsonmod.dumps(body).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        polls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            if "/result" in req.full_url:
                events.append("result")
                return _Resp({"ok": True})
            events.append("poll")
            polls["n"] += 1
            if polls["n"] == 1:
                return _Resp({"job": {"job_id": "sim-1", "doc_type": "W-2", "doc_id": "d1",
                                      "seq": 1, "payload": {"screen": "W2"}}})
            if polls["n"] > 3:
                # The brake for the regression this case exists to catch: if the toast
                # (whose side effect asks for the quit) never fires, the loop would poll
                # forever and the suite would HANG instead of failing. KeyboardInterrupt
                # is the loop's own clean exit, and the missing toast fails the checks.
                raise KeyboardInterrupt
            return _Resp({})

        class _FakeTime:
            # Any sleep at all means the loop took an error detour (catch-all, offline,
            # no-Drake). Recorded rather than slept, so a broken build fails fast.
            def sleep(self, secs):
                events.append(f"sleep:{secs}")

        saved = (connector._already_running, connector._cred_read, connector.SPOOL_DIR,
                 connector.time, urllib.request.urlopen, tray_mod.install_log_tee,
                 tray_mod.Tray, dd.DrakeDriver, agent.load_binding, agent._run_one_payload)
        try:
            connector._already_running = lambda: False
            connector._cred_read = lambda: {"server": "https://example.invalid",
                                            "token": "T", "agent_id": "a1", "name": "SimPC"}
            # NEVER the real spool: cmd_run flushes it through the faked urlopen, which
            # would "deliver" — and delete — a real machine's held reports.
            connector.SPOOL_DIR = _P(tempfile.mkdtemp()) / "spool"
            connector.time = _FakeTime()
            urllib.request.urlopen = fake_urlopen
            # The tee is proven by connector_exe_forms; here it would swallow the suite's
            # own stdout and append this rehearsal to a real machine's connector.log.
            tray_mod.install_log_tee = lambda: _P("sim-connector.log")
            tray_mod.Tray = _FakeTray
            dd.DrakeDriver = _FakeDriver
            agent.load_binding = lambda p: {"navigation": {}}
            agent._run_one_payload = lambda driver, payload, args, token: dict(report)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = connector.cmd_run(connector.build_parser().parse_args(["run"]))
        finally:
            (connector._already_running, connector._cred_read, connector.SPOOL_DIR,
             connector.time, urllib.request.urlopen, tray_mod.install_log_tee,
             tray_mod.Tray, dd.DrakeDriver, agent.load_binding,
             agent._run_one_payload) = saved
        return rc, events, toasts, states

    ok_rc, ok_events, ok_toasts, ok_states = run_once({"ok": True, "entered": 7})
    bad_rc, bad_events, bad_toasts, bad_states = run_once(
        {"ok": False, "entered": 2, "reason": "read-back mismatch on field 23"})

    ok_title, ok_msg = ok_toasts[0] if ok_toasts else ("", "")
    bad_title, bad_msg = bad_toasts[0] if bad_toasts else ("", "")

    checks = [
        ("a finished document raises exactly one toast", len(ok_toasts) == 1),
        ("...titled as entered, never as filed",
         "entered" in ok_title and "filed" not in ok_title),
        ("...only AFTER the report was delivered — 'entered' means the report said so",
         "result" in ok_events and "notify" in ok_events
         and ok_events.index("result") < ok_events.index("notify")),
        ("...naming the document and the report's own field count",
         "W-2" in ok_msg and "7 fields" in ok_msg),
        ("...and pointing at the portal without claiming a filing",
         "portal" in ok_msg and "nothing is filed" in ok_msg),
        ("the toast RAISED, and the loop still reached its safe stop point", ok_rc == 0),
        ("no error detour on the way: no sleeps, never 'offline'",
         not any(e.startswith("sleep") for e in ok_events) and "offline" not in ok_states),
        ("a halted run raises the other toast, pointing at review",
         len(bad_toasts) == 1 and "review" in bad_msg and "stopped" in bad_title),
        ("...saying the firm is blocked until a person acts",
         "Nothing else will be entered" in bad_msg),
        ("the raising toast could not knock the tray off 'halted'",
         bad_states[-1:] == ["halted"]),
        ("and the halted loop also survived to its safe stop point",
         bad_rc == 0 and not any(e.startswith("sleep") for e in bad_events)),
    ]
    return _table("run-finished toasts: heard by the operator, harmless at their worst",
                  checks)


def case_chooser_duplicate_decision():
    """Is this document ALREADY on the return? Decided against every record, not just one.

    The mutation harness found this one, and it is worth saying how: the matching helper in
    drake_nav was covered, so `record_chooser` was green — but the DECISION that calls it sat
    inline in `_navigate_for_payload`, and deleting it left every test passing. A guard whose
    mutant survives has no test, however well its parts are covered.

    TWO IDENTIFIERS, and the second is not redundant. The chooser prints a NAME; the dedupe
    key is an ID. A W-2 chooser's columns are '#, TS, Employer Name, Wages…' — no EIN
    anywhere — so an id-only comparison would never fire on the screen where doubling
    somebody's wages is easiest.

    What is at stake: entering a second copy doubles a client's income on their return, and
    every read-back and form check would pass, because Drake really did accept every value."""
    from agent import _chooser_duplicates

    W2_ROWS = [
        ["#", "TS", "Employer Name", "Wages, Tips"],
        ["1", "T", "navigation test employer", "52000"],
        ["2", "T", "test employer llc", "52000"],
        ["New", "New Record", "", ""],
    ]
    INT_ROWS = [
        ["#", "Name", "Interest Income"],
        ["1", "first national test bank", "1000"],
        ["New", "New Record", ""],
    ]

    w2_target = {"ein": "12-3456789", "screen": "W2"}
    int_target = {"ein": "98-7654321", "screen": "INT"}

    checks = [
        # The name path — the only one a W-2 chooser can answer, since it prints no EIN.
        ("an employer already on the screen is caught by NAME",
         _chooser_duplicates(W2_ROWS, w2_target, {"employer_name": "TEST EMPLOYER LLC"}) != []),
        ("...however Drake reformatted it when it stored it",
         _chooser_duplicates(W2_ROWS, w2_target, {"employer_name": "Test Employer, L.L.C."}) != []),
        ("a payer new to this return is not a duplicate",
         _chooser_duplicates(W2_ROWS, w2_target, {"employer_name": "brand new employer"}) == []),
        # The id path, for the screens whose chooser does print one.
        ("a payer already on the screen is caught by ID",
         _chooser_duplicates([["#", "TIN"], ["1", "98-7654321"], ["New", "New Record"]],
                             int_target, {}) != []),
        # Each form's own name key, so a new screen cannot quietly fall through the check.
        ("the 1099 screens are checked on payer_name",
         _chooser_duplicates(INT_ROWS, int_target,
                             {"payer_name": "FIRST NATIONAL TEST BANK"}) != []),
        ("the 1098 screen is checked on lender_name",
         _chooser_duplicates([["#", "Name"], ["1", "first test mortgage bank"], ["New", "New Record"]],
                             {"ein": ""}, {"lender_name": "First Test Mortgage Bank"}) != []),
        # Nothing to compare is NOT proof of newness — but it must not invent a match either.
        ("a payload with no id and no name matches nothing",
         _chooser_duplicates(W2_ROWS, {"ein": ""}, {}) == []),
        ("an empty chooser matches nothing",
         _chooser_duplicates([], w2_target, {"employer_name": "test employer llc"}) == []),
        ("the header row is never mistaken for a record",
         _chooser_duplicates(W2_ROWS, {"ein": ""}, {"employer_name": "Employer Name"}) == []),
        ("what is returned is the matching ROW, so the refusal can name it",
         _chooser_duplicates(W2_ROWS, w2_target,
                             {"employer_name": "test employer llc"})[0][2] == "test employer llc"),
    ]
    return _table("the record chooser: is this document already on the return?", checks)


def main() -> int:
    # The suite prints '⚠' and '·', and a REDIRECTED stdout on Windows is cp1252 — which
    # raised UnicodeEncodeError inside the driver, was caught by headsdown_type's outer
    # handler, and came back as a HALT. Five cases failed that way when the suite was run
    # into a file and passed when it was run at a console: a red suite for a reason that
    # had nothing to do with the code under test. Same fix as agent.py's _utf8_console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 74)
    print("Heads-down entry — offline state-machine proof")
    print("Confirmed protocol: popup persists, alternating number -> value -> number,")
    print("with field 4 (EIN) auto-advancing and swallowing the next Ctrl+N.")
    print("Every case runs with the 'Drake Software Chat' window present.")
    print("=" * 74)

    # (label, thunk) pairs so one case or one family can be run on its own:
    #     python simulate_headsdown.py checkbox
    # The full suite takes minutes on a slow box, and the mutation harness runs it
    # once per mutant — being able to run only the family a mutant touches is the
    # difference between a 30-minute check and an all-day one.
    cases = [
        ('nav_identity_from_title', lambda: case_nav_identity_from_title()),
        ('nav_name_matching', lambda: case_nav_name_matching()),
        ('nav_row_selection', lambda: case_nav_row_selection()),
        ('nav_row_name_pairing', lambda: case_nav_row_name_pairing()),
        ('nav_record_safety', lambda: case_nav_record_safety()),
        ('nav_screen_link', lambda: case_nav_screen_link()),
        ('nav_screen_signature', lambda: case_nav_screen_signature()),
        ('nav_create_name_collision', lambda: case_nav_create_name_collision()),
        ('nav_menu_is_not_the_form', lambda: case_nav_menu_is_not_the_form()),
        ('int_field_map', lambda: case_int_field_map()),
        ('int_value_kinds', lambda: case_int_value_kinds()),
        ('int_screen_and_grid', lambda: case_int_screen_and_grid()),
        ('int_full_coverage_plan', lambda: case_int_full_coverage_plan()),
        ('div_field_map', lambda: case_div_field_map()),
        ('div_code_kinds', lambda: case_div_code_kinds()),
        ('div_screen_signature', lambda: case_div_screen_signature()),
        ('div_full_coverage_plan', lambda: case_div_full_coverage_plan()),
        ('r_field_map', lambda: case_r_field_map()),
        ('r_dist_code_split', lambda: case_r_dist_code_split()),
        ('r_screen_signature', lambda: case_r_screen_signature()),
        ('r_full_coverage_plan', lambda: case_r_full_coverage_plan()),
        ('drake_symbol_codes', lambda: case_drake_symbol_codes()),
        ('ssa_field_map', lambda: case_ssa_field_map()),
        ('ssa_not_on_screen', lambda: case_ssa_not_on_screen()),
        ('ssa_screen_signature', lambda: case_ssa_screen_signature()),
        ('m1098_field_map', lambda: case_m1098_field_map()),
        ('m1098_country_codes', lambda: case_m1098_country_codes()),
        ('m1098_not_on_screen', lambda: case_m1098_not_on_screen()),
        ('m1098_screen_signature', lambda: case_m1098_screen_signature()),
        ('m1098_full_coverage_plan', lambda: case_m1098_full_coverage_plan()),
        ('m1098_menu_tab_walk', lambda: case_m1098_menu_tab_walk()),
        ('form_dispatch', lambda: case_form_dispatch()),
        ('payload_target_screens', lambda: case_payload_target_covers_every_screen()),
        ('batch_queue_order', lambda: case_batch_queue_order()),
        ('batch_transport_keys', lambda: case_transport_keys_are_not_values()),
        ('nav_menu_first', lambda: case_screen_link_from_a_form_returns_to_the_menu()),
        ('record_chooser', lambda: case_record_chooser()),
        ('record_chooser_duplicate', lambda: case_chooser_duplicate_decision()),
        ('connector_entry_options', lambda: case_connector_entry_options()),
        ('connector_exe_forms', lambda: case_connector_exe_carries_every_form()),
        ('connector_network_death', lambda: case_connector_network_death()),
        ('connector_tray_state', lambda: case_connector_tray_state()),
        ('connector_setup_prefill', lambda: case_connector_setup_prefill()),
        ('connector_run_toasts', lambda: case_connector_run_toasts()),
        ('tray_notify_output_only', lambda: case_tray_notify_is_output_only()),
        ('revoked_machine_is_visible', lambda: case_revoked_machine_is_visible()),
        ('drake_dies_inside_the_poll', lambda: case_drake_dies_inside_the_poll()),
        ('repair_resumes_the_loop', lambda: case_repair_resumes_the_loop()),
        ('held_report_never_blocks', lambda: case_held_report_never_blocks_the_loop()),
        ('drake_closed_said_out_loud', lambda: case_drake_closed_is_said_out_loud()),
        ('quit_waits_for_the_loop', lambda: case_quit_waits_for_the_loop()),
        ('dialogs_raised_over_drake', lambda: case_dialogs_are_raised_over_drake()),
        ('caret_stranded_run_rearms', lambda: case_stranded_run_rearms_caret()),
        ('caret_no_popup_no_caret_rearms', lambda: case_no_popup_no_caret_rearms()),
        ('caret_rearm_impossible_halts', lambda: case_rearm_that_cannot_work_halts_clean()),
        ('caret_headsdown_off_halts', lambda: case_rearm_works_but_headsdown_is_off()),
        ('form_check_whole_dollars', lambda: case_form_check_whole_dollars()),
        ('locality_resolves_to_drake_code', lambda: case_locality_resolution()),
        ('full_sequence "persistent"', lambda: case_full_sequence("persistent")),
        ('full_sequence "per-jump"', lambda: case_full_sequence("per-jump")),
        ('full_sequence "persistent", autoadvance_from=23, label=" auto-advance elsewhere"', lambda: case_full_sequence("persistent", autoadvance_from=23, label=" auto-advance elsewhere")),
        ('full_sequence "persistent", autoadvance_from=None, label=" no auto-advance"', lambda: case_full_sequence("persistent", autoadvance_from=None, label=" no auto-advance")),
        ('ein_skipped', lambda: case_ein_skipped()),
        ('invalid_number', lambda: case_invalid_number()),
        ('ctrln_inert_popup_after_ein_is_recycled',
         lambda: case_inert_popup_after_ein_is_recycled()),
        ('ctrln_healthy_popup_is_never_double_toggled',
         lambda: case_healthy_popup_is_never_double_toggled()),
        ('ctrln_healthy_painted_popup_is_never_double_toggled',
         lambda: case_healthy_popup_is_never_double_toggled(
             label=" [painted popup — the confirmed shape]", popup_has_edit=False)),
        ('slow_jump_is_not_a_refusal', lambda: case_slow_jump_is_not_a_refusal()),
        ('inert_field_silently_declined', lambda: case_inert_field_silently_declined()),
        ('inert_field_that_clears_the_box', lambda: case_inert_field_that_clears_the_box()),
        ('value_silently_refused', lambda: case_value_silently_refused()),
        ('value_corrupted_in_flight', lambda: case_value_corrupted_in_flight()),
        ('number_corrupted_in_flight', lambda: case_number_corrupted_in_flight()),
        ('late_keys_append', lambda: case_late_keys_append()),
        ('empty_value_refused', lambda: case_empty_value_refused()),
        ('inherited_armed_popup', lambda: case_inherited_armed_popup()),
        ('blind_build_degrades', lambda: case_blind_build_degrades()),
        ('window_closed_midbatch', lambda: case_window_closed_midbatch()),
        ('benign_window_midrun', lambda: case_benign_window_midrun()),
        ('modal_disable', lambda: case_modal_disable()),
        ('modal_disabled_before_attach',
         lambda: case_main_frame_disabled_before_we_attached()),
        ('modal_while_popup_open', lambda: case_modal_while_popup_open()),
        ('keyboard_scope', lambda: case_keyboard_scope()),
        ('focus_never_taken', lambda: case_focus_never_taken()),
        ('anchored_title_regex', lambda: case_anchored_title_regex()),
        ('edit_class_is_not_literally_edit', lambda: case_edit_class_is_not_literally_edit()),
        ('painted_popup_enters', lambda: case_painted_popup_enters()),
        ('painted_popup_enters label=" [punctuation jitter]", surface_jitter="punct"', lambda: case_painted_popup_enters(label=" [punctuation jitter]", surface_jitter="punct")),
        ('painted_popup_enters label=" [character-level OCR noise]", surface_jitter="chars"', lambda: case_painted_popup_enters(label=" [character-level OCR noise]", surface_jitter="chars")),
        ('painted_popup_enters label=" [transient unreadable frames — the live field-1 halt]", surfa', lambda: case_painted_popup_enters(label=" [transient unreadable frames — the live field-1 halt]", surface_jitter="repaint")),
        ('painted_popup_enters label=" [OCR garbage frames]", surface_jitter="garbage"', lambda: case_painted_popup_enters(label=" [OCR garbage frames]", surface_jitter="garbage")),
        ('painted_popup_channel_dies_before_the_baseline', lambda: case_painted_popup_channel_dies_before_the_baseline()),
        ('painted_popup_unreadable', lambda: case_painted_popup_unreadable()),
        ('painted_popup_keystrokes_vanish', lambda: case_painted_popup_keystrokes_vanish()),
        ('painted_popup_value_corrupted', lambda: case_painted_popup_value_corrupted()),
        ('painted_popup_silent_refusal', lambda: case_painted_popup_silent_refusal()),
        ('painted_popup_silent_refusal label=" [character-level OCR noise]", surface_jitter="chars"', lambda: case_painted_popup_silent_refusal(label=" [character-level OCR noise]", surface_jitter="chars")),
        ('surface_token_counting', lambda: case_surface_token_counting()),
        ('checkbox_ticks_and_commits', lambda: case_checkbox_ticks_and_commits()),
        ('checkbox_token_ignored_halts', lambda: case_checkbox_token_ignored_halts()),
        ('checkbox_unreadable_halts', lambda: case_checkbox_unreadable_halts()),
        ('checkbox_already_ticked_types_nothing', lambda: case_checkbox_already_ticked_types_nothing()),
        ('checkbox_pixel_only', lambda: case_checkbox_pixel_only()),
        ('checkbox_uia_present_but_mute', lambda: case_checkbox_uia_present_but_mute()),
        ('checkbox_channels_disagree', lambda: case_checkbox_channels_disagree()),
        ('checkbox_commit_silently_refused', lambda: case_checkbox_commit_silently_refused()),
        ('checkbox_token_escalation', lambda: case_checkbox_token_escalation()),
        ('checkbox_toggle_not_double_flipped', lambda: case_checkbox_toggle_not_double_flipped()),
        ('checkbox_drift_guard', lambda: case_checkbox_drift_guard()),
        ('checkbox_map_wrong_field_is_text', lambda: case_checkbox_map_wrong_field_is_text()),
        ('checkbox_per_jump_refused', lambda: case_checkbox_per_jump_refused()),
        ('checkbox_flicker_is_not_proof', lambda: case_checkbox_flicker_is_not_proof()),
        ('checkbox_untick_needs_a_read_first',
         lambda: case_checkbox_untick_needs_a_read_first()),
        ('checkbox_glyph_table', lambda: case_checkbox_glyph_table()),
        ('checkbox_probe_measures_and_leaves_clean',
         lambda: case_checkbox_probe_measures_and_leaves_clean()),
        ('checkbox_desired_table', lambda: case_checkbox_desired_table()),
        ('known_dialog_dismissed_by_name', lambda: case_known_dialog_dismissed_by_name()),
        ('unknown_dialog_still_halts', lambda: case_unknown_dialog_still_halts()),
        ('edit_ranking_table', lambda: case_edit_ranking_table()),
        ('prompt_excludes_the_typing_box', lambda: case_prompt_excludes_the_typing_box()),
        ('classifier_table', lambda: case_classifier_table()),
        ('comparator_table', lambda: case_comparator_table()),
    ]
    for label, fn in cases:
        if only and only.lower() not in label.lower():
            continue
        try:
            fn()
        except Exception:
            # A CASE THAT CRASHES IS A FAILED CASE, NOT A MISSING ONE. A bare `fn()` here
            # meant one renamed function (an AttributeError inside a case) aborted the run
            # and the seventy cases after it never executed — with no summary line to say
            # so. Recorded through _check, so it counts red like any other failure and the
            # traceback is right there. KeyboardInterrupt still stops the suite: that one
            # is a person asking it to stop.
            import traceback
            _check(f"{label} — the case itself crashed", False, traceback.format_exc())
    if not _results:
        # An empty run must never look like a clean one — that is how a filter typo
        # turns into 'the mutant was killed' when nothing ran at all.
        print(f"NO CASES MATCHED {only!r}")
        return 1

    print("\n" + "=" * 74)
    passed = sum(1 for r in _results if r)
    print(f"{passed}/{len(_results)} cases pass")
    print("=" * 74)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
