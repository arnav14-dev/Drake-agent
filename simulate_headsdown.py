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
        ("a screen with no measured signature returns None, not True",
         screen_is_showing(["anything at all"], "1099") is None),
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


def plan_has_confirm(spec, field_no) -> bool:
    """Would a plan mark this field 'verify by eye'? Asked of the PLAN, not the table —
    that flag is what a preparer actually sees, and it is set by build_plan, not declared."""
    import form_plan
    key = next(k for k, f in spec.fields.items() if f["field_no"] == field_no)
    field = spec.fields[key]
    kind = field["kind"]
    probe = {"checkbox": True, "pct": "10", "date": "12/31/2025", "money": "100",
             "state": "PA", "code": "CA", "tsj": "T"}.get(kind, "0" if kind == "digits" else "X")
    # A box with a fixed list has to be probed with something ON the list, or the plan
    # rightly refuses it and there is no entry left to inspect for the flag.
    if field.get("values"):
        probe = sorted(field["values"])[0]
    plan = form_plan.build_plan({key: probe}, spec)
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
         unknown is not None and "W2" in unknown and "INT" in unknown),
        ("both mapped screens expose the same planning entry points",
         all(callable(getattr(ag._load_form_map(s)[0], fn))
             for s in ("W2", "INT") for fn in ("build_plan", "format_plan"))),
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
        ('form_dispatch', lambda: case_form_dispatch()),
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
        fn()
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
