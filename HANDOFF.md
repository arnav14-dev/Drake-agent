# HANDOFF — read this first

You are picking up a project mid-flight. This file is the orientation: what is being built,
what is actually true about the target application, what is finished, what is unverified,
and where the real blockers are. `README.md` is the engineering reference — this is the map.

Written 2026-08-03 at commit `a885be6`; updated 2026-08-04, the day a full W-2 went into
Drake end to end for the first time — 20 of 20 fields (see §6).

---

## 1. What is being built

**Fynn** — an AI tax preparer. Ingest source documents (W-2, 1099, K-1, brokerage, P&L) →
extract → a canonical **Tax Fact Graph** → reconcile and review-by-exception → **populate
professional tax software** → read back and verify.

The bet is "one Tax Fact Graph, many adapters." The product is not the automation; it is the
precision and the audit trail. The founder's bar, stated plainly and worth keeping:
*no urgency, rock solid, never lie to the user about what it doesn't know.*

This repo, `drake-agent/`, is **one adapter**: the piece that drives **Drake Tax 2025**'s
on-screen data entry on a Windows machine. It is Python + pywinauto and runs next to Drake.

Wider context you should not have to rediscover:

- Fynn's shipping product today is post-entry **QuickBooks bookkeeping** (`fynn-backend/`,
  `fynn-frontend/` in the parent directory). The tax-prep work is a pivot, not a rewrite.
- The benchmark competitor (**Juno**) drives Drake with **the same GUI RPA** — confirmed from
  their own help docs. There is no secret sanctioned Drake write API. We are not on the wrong
  surface.
- **CCH Axcess has a real write API** (Open Integration Platform, "Tax Transfer"). That is the
  genuine API door, partner-gated, no self-serve sandbox. It is the strategic path; Drake is
  the tactical one.

## 2. Two hard constraints — do not relax these

**The agent never files.** There is no file / e-file / calculate-and-transmit command in the
agent or in `protocol.py`, by design. It types data-entry boxes and stops. A human verifies in
Drake and executes. This is a guarantee, not a setting. If you are asked to add a submit path,
push back and confirm explicitly first.

**Live use on real client returns is legally gated.** Drake's TY2026 license (updated
2026-07-10) bans "Automated Means" — RPA and AI agents — without Drake's prior written
authorization. Everything here runs against a **trial install with test returns**. The Drake
path is a business-development gate first and an engineering problem second. On the Fynn side,
live mode also stays locked behind `DRAKE_AUTOMATION_AUTHORIZED`.

## 3. How Drake actually works — hard-won, empirically confirmed

Every line here was established against the live application, usually after a wrong assumption
cost a run. Treat it as fact and do not re-litigate it without new evidence.

**THE CANVAS IS READABLE — the founding measurement was taken on the wrong window.**
CONFIRMED 2026-08-04. "UIA reports 0 edit controls and 0 readable values" was measured
against the MAIN FRAME (`Drake 2025 Tax Software`). Drake hosts a return's data-entry screen
in a SEPARATE top-level window, and walking *that* window's UIA tree returns **253 elements
with real values** — `12-3456789`, `test employer llc`, `Addison`, `52000`. `canvas_windows()`
finds it; `read_canvas_values()` reads it; `audit_canvas()` compares a finished run against it
and `write-w2` prints the result as FORM CHECK.

This matters more than any other line in this file. Every gate before it verified what the
POPUP echoed — which is what we typed, not what Drake kept. See the locality entry below for
what that difference costs. The next piece of work is a field-number → control map so the
check can say a value is in the RIGHT box, not merely somewhere on the form; the presence
test already catches "never landed at all", which is the dangerous case.

**Box 20 locality (63/70/77/84) stores a CODE from Drake's own table, not the name on the
W-2.** RESOLVED 2026-08-05. For a day this was recorded here as "typing into Box 20 stores
NOTHING" — the dropdown took the keystrokes, the popup echoed them (`84 Phila Phila`, with
Drake's own title-casing, indistinguishable from the confirmed dropdown behaviour on
1/9/34/57), the Enter committed, and the box stayed empty. **The founder caught it by eye
from a screenshot while the run reported all four OK.**

The field was never the problem. The VALUE was. Drake ships `STATELIB\CITY.HLP`, a plain
`ST,City Code,City Name` table, and the dropdown stores the *code*: `PA,PL` is Philadelphia,
`NY,NY` is New York City, `IN,49` is Marion County. We typed `PHILA`, `NYC`, `MARION`. Drake
matched none of them, selected nothing, and a dropdown with nothing selected saves nothing.
Every gate was reporting truthfully about what it could see.

`w2_map` now reads that table from the LIVE install and resolves a Box 20 string to its code
— by code, by exact name, by a tiny curated alias set, or by unique name prefix, in that
order — and REFUSES on anything ambiguous or unknown rather than guessing, because a
wrong-but-valid code types cleanly, reads back cleanly, and looks exactly like a right one.
Only 11 states appear in the table (CA DE IN KY MI MO NY OH OR PA); for any other state the
list is genuinely empty and the row is reported, never attempted. Confirmed live: OH
`COLUMBUS`→`COLUMBUS`, IN `MARION COUNTY`→`49`, NY `NYC`→`NY`, PA `Philadelphia`→`PL`, all
four visible on the form afterwards, and Drake grew an "Ohio" tab in response.

`HAND_ENTRY_FIELDS` is now empty. The mechanism stays for the next such field.

**Some boxes silently CAP their length.** Box 14 descriptions (49/51/53/55) hold 8 characters,
Box 20 locality 9. Drake simply stops accepting keys, so `UNION DUES` settles as `UNION DU` and
the settle gate halts rather than commit a truncated value. `max_len` in the map trims to the
real capacity and every trim is REPORTED in the plan — trimming silently would be the lie.
A locality CODE is the exception: it is refused rather than trimmed, because cutting a code
short does not shorten a name, it names a different locality.

**Heads-down arms off an ACTIVE CARET, not window focus.** Ctrl+N is a silent no-op with no
field active — no popup, no error. A finished run leaves the canvas focused with no caret
and the popup still on screen, which is exactly the state a second back-to-back run starts
in: on 2026-08-05 run 1 wrote 78/78 and run 2 halted on field 1 having typed nothing. The
halt is safe, but consecutive UNATTENDED W-2s do not work without a re-arm step. `{ESC}`
then `{TAB}` then Ctrl+N recovers it — proven live on the stuck state, keyboard-only, no
coordinates, and Tab moves between boxes rather than altering one. Deliberately NOT built
in: the founder prefers to clear the popup by hand and be asked first. Build it when the
backend starts feeding W-2s unattended.

**Two identical runs produce an identical form.** 2026-08-05, first time this was ever
tested: the same 78-field payload run twice with no edits between, compared box-by-box off
the canvas rather than by OK count — 72 non-empty boxes, zero differences. Also confirms the
checkbox read matters: on run 2 all six boxes arrived already ticked and the driver typed
NOTHING at them, because `X` toggles and a keystroke would have cleared them.

**Drake exposes no programmatic read-back on its data-entry grid** — TRUE ONLY OF THE MAIN
FRAME, and superseded by the entry above. On the main frame UI Automation reports 0 edit
controls and 0 readable values, the win32 backend sees only outer frame panels, and clipboard
copy returns nothing *and* the Ctrl+A/Ctrl+C chord breaks field focus and pops a modal
validator. All of that is still true; it is simply not true of the data-entry window.

**Entry goes through heads-down mode (Ctrl+N).** Drake shows a number next to every field; you
address a field by typing its number. This is coordinate-free — immune to DPI, resolution and
window position, unlike pixel clicks. The protocol, confirmed on this build, is a
**persistent command bar**:

```
Ctrl+N → type field NUMBER → Enter → the same popup asks for the VALUE
       → type value → Enter → back to the number prompt → repeat
```

**Exception — field 4 (employer EIN).** Committing it fires Drake's employer lookup and
auto-fill, auto-advances the caret to Box 1 (field 23), and **swallows the next Ctrl+N**. This
single eaten chord is what originally made every field number type onto the canvas and every
value land one box too far down. It is handled generically (re-check popup presence before
every Ctrl+N, retry with a settle), not with a special case.

**A checkbox field's value stage is not a text box.** CONFIRMED live 2026-08-03 on field 47
(Box 13 "Retirement plan"): the popup keeps its field-number box and puts a **real checkbox
widget** beside it, captioned with the field's name. The token flips the tick — but a tick is
a *glyph*, so the text gates, which require the typed token to appear in the popup's text one
more time than before, can never be satisfied. That is what halted the first 20-field run
after 16 clean fields. The X had landed and the box was ticked on screen; the driver was
right to refuse to commit what it could not read. Checkboxes now go through `_enter_checkbox`,
which reads the **tick** (UIA Toggle state, or the accent-blue glyph on screen) instead of
counting characters. See §6 for what is still unverified about it.

**A jump can land LATE, and a late jump is not a refusal.** Committing a field can fire
Drake's own work — an employer-database lookup, a ZIP validation — which blocks its UI thread,
so the popup sits on the field-number prompt for a while and *then* moves to the value prompt.
Nothing distinguishes that from a silently declined number except waiting long enough. At a
2.5s budget the live run of 2026-08-04 called field 14 refused and stopped, while the
screenshot taken moments later showed the popup already sitting on field 14's value box. The
budget is now `navigation.headsdown_jump_timeout` (default 8s) and is a **busy** budget, not a
refusal test: it costs nothing on a healthy field, because the loop returns the instant the
prompt moves. The halt message reports the observation, not the conclusion.

**Drake rewrites values after you commit them.** Field 8 (employer city) was entered as
`DALLAS`, read back as `dallas` at commit, and the after-screenshot shows **`Addison`** — the
USPS city for the ZIP we entered in field 10 (75001 is Addison, TX). Drake validated the ZIP
and corrected the city. That is Drake being *right*, and our sample data being internally
inconsistent — but it is proof of a general rule: **a verified commit is not a permanent
value.** No read-back can catch this, because the canvas exposes nothing; the after-screenshot
is the only check there is, which is why every run takes one and why the run tells you to look
at it.

**Drake nests its screens, and the frames behind get `WS_DISABLED`.** Opening a return's
data-entry screen creates a NEW top-level window (a fresh hwnd each time) and puts the disabled
bit on the main frame behind it. That is Drake at rest, not a modal — but it is bit-identical
to one, and the structural gate read it as *"main frame DISABLED by a modal — window not
identified"* and refused to type a single character (live, 2026-08-04: both commands halted at
the first gate, the screenshot at the halt showed a clean W-2 screen with no dialog anywhere,
and every window in the process was already in the baseline). The gate now records the frame's
disabled bit **at attach** alongside the baseline hwnds: inherited modality is furniture, the
same rule that keeps the chat overlay from halting runs. A modal that arrives LATER still
disables the frame and is still caught.

**The heads-down popup owns NO child windows.** `EnumChildWindows` returns `[]` and
`GetGUIThreadInfo().hwndFocus` **is the popup itself**. Drake paints the input box onto the
dialog, exactly as it paints the grid. So the popup *is* the keyboard target, and
`WM_GETTEXT` on it returns its **caption** — a constant with nothing to do with what was
typed. The box is therefore read *as a whole*, through whichever channel answers: a child
`Edit` if one exists, else **UIA by handle**, else **OCR** of the popup's rectangle.

On the live machine **UIA works** (`read_back: AVAILABLE via uia`), so Tesseract is currently a
fallback rather than a blocker — but keep it installed, because the OCR channel is the only one
that cannot be taken away.

## 4. Durable rules learned the expensive way

These each cost a live run. They generalise beyond this repo.

**pywinauto's finder criteria are exact or anchored — never identify a window or a control by
name.** `title_re` and `class_name_re` match with `re.match` (anchored at the start,
`findwindows.py:274-281`), and `class_name=` is plain string equality (`:257`). So
`r"Heads.?Down Data Entry"` never matched `"Drake 2025 - Heads Down Data Entry"`, and
`class_name="Edit"` finds nothing on a toolkit that names its control `TEdit` or
`WindowsForms10.EDIT.app.0.x`. Enumerate with ctypes, pick by **structure**, then hand
pywinauto a `handle=`.

**Keystrokes are POSTED; `WM_GETTEXT` and `set_edit_text` are SENT and jump the queue.** This
killed the "read it back, and if it's wrong repair it" pattern: it could read a half-arrived
`520`, "repair" it to `52000`, re-read cleanly, pass the gate — and only then would the still
-queued `00` arrive and append, committing 5,200,000 while reporting success. Every gate now
waits for the box to **stop changing** (two consecutive agreeing readings) as a drain proof.

**No evidence is not evidence of emptiness.** A failed read must never be collapsed into `""`.
This one produced the most recent live halt: an unreadable frame recorded as *a reading of
empty* discarded the good reading either side of it, so a perfectly legible popup could never
produce a baseline. Related: OCR fails by returning punctuation (`|_. -~`) far more often than
by returning nothing, so a frame with no alphanumerics in it is also not a reading.

**A refusal is silent and looks exactly like success.** Drake declines an invalid field number
or value with a beep — no dialog, and the box clears identically either way. The *only* tell is
that its prompt text does not move. Everything is therefore verified against a **captured
baseline** rather than a hardcoded English string, and the baseline itself must converge
before it is trusted.

**Never press a key you cannot prove the destination of.** Focus is proven with
`GetGUIThreadInfo` (the cross-process truth of who owns the keyboard) *and* by checking the
foreground root window, before anything is typed.

## 5. Testing — this is not optional here

A green suite has been actively misleading **three times** on this project, so the standard is
higher than usual.

```
python simulate_headsdown.py            # 61 cases, ~9 min on the laptop, runs anywhere
python simulate_headsdown.py checkbox   # one family, ~1 min — what you use while iterating
python mutants.py checkbox              # the 8 checkbox guards, ~15 min
python mutants.py                       # all 31 guards. HOURS at 9 min a suite — plan for it
```

Both harnesses take a substring filter, and they are worth using: the full suite is minutes,
not the "90 seconds" an earlier version of this file claimed, and the full mutation run is
`31 x suite`. Each checkbox mutant runs only the checkbox family; running *fewer* cases can
only turn a kill into a reported GAP, never a survivor into a false kill, so the shortcut
cannot flatter a guard. A filter that matches nothing exits 1 rather than reporting a pass on
an empty run.

`simulate_headsdown.py` fakes **Drake**, not pywinauto: the driver's real decision code runs,
only the Windows primitives are replaced. `mutants.py` edits each safety guard *in the source*,
in an isolated copy, and requires the suite to fail. **A guard whose mutant survives has no
test, whatever the suite says.** Three mutants are documented in that file as deliberately not
gaps (one equivalent mutant, one message-only difference, one channel semantic the simulator
replaces at the boundary) — read the comments before adding them back.

Worth knowing what a *real* gap looks like, because both of the ones found while writing the
checkbox path passed a plausible-looking test first. The untick guard had no case at all until
one was written for it. The convergence guard had a case that looked right — one flickering
frame right after the token — and the mutant **survived** it, because the re-prove step just
before the Enter caught that particular lie on its own. Only a channel that jitters on *every
other read*, arrival included, actually needs the two-agreeing-readings rule.

The recurring failure mode to watch for: **a fake that is more generous than the real thing
hides exactly the bugs it exists to catch.**

- `_FakeWin` once had an `exists()` that a real `UIAWrapper` lacks → suite green, live run dead.
- Read-back was once an identity function → every read-back gate was dead code.
- The "painted" popup was once given a `Static` child holding the prompt → every painted case
  passed while the screen-reading path they existed to test was barely used. Making it report
  `[]`, as the live probe does, failed a case immediately — and that failure was a real driver
  bug.
- An assertion once checked `fake.log` for `"ENTER"`, but the persistent model logs what an
  Enter *did*, never the key itself, so it passed whether or not one had been sent. **A test
  that cannot fail is worse than no test.**
- The checkbox fake renders the field's caption and *never* the token, and its pixel channel
  returns `True` or `None` but never `False` — because the real one cannot tell an unticked
  checkbox from a text box. A fake that answered `False` there would make an untick look
  verifiable when it is not.

**Run the suite at a console, or with output redirected — both now work.** Five cases used to
fail only when stdout was a file: Windows gives a redirected stream the ANSI codepage, so
printing `⚠` raised `UnicodeEncodeError`, which `headsdown_type`'s outer handler reported as a
HALT with a charmap error in place of the real reason. That would have done the same to a real
run logged to a file. Fixed in `_say()` and `_utf8_console()`; keep it that way.

## 6. Where it stands

**Working and verified offline (61/61 cases; the 8 checkbox guards all mutation-killed):** the full entry state
machine — popup discovery by handle, structural identification of the typing box, painted-popup
reads through UIA/OCR, token-run counting against a per-channel baseline, silent-refusal
detection, commit proof, the structural dialog gate (which stops the always-present "Drake
Software Chat" overlay from halting runs), named-button dismissal of Drake's e-file
completeness warning, and the checkbox path (tick read back off the widget, token escalation
verified between tokens, build-drift guard both ways).

**A FULL W-2 HAS NOW BEEN ENTERED END TO END ON THE LIVE MACHINE — 2026-08-04, 20 of 20
fields, verified box by box against the after-screenshot.** No cascade, no error dialog, no
mis-entry. This is the thing that had never happened.

```
1 TS=T   5 employer name  7 street  8 city  9 state=TX  10 ZIP  14/15 employee name
23 wages  24 fed W/H  25 SS wages  26 SS W/H  27 Medicare wages  28 Medicare W/H
34 box12a code=D  35 box12a amount  47 Box 13 retirement plan (TICKED)
57 state=TX  58 state ID  59 state wages
```

Settled by that run, each previously an unknown:

- **The persistent command-bar protocol holds for a whole run.**
- **Dropdowns take a typed code.** 1, 9, 34 and 57 all read back as the code *and* the entry
  Drake selected (`T T`, `TX TX`, `D D`). `w2_map.DROPDOWNS_CONFIRMED` records which.
- **The checkbox path works live**: `checkbox ticked — confirmed via uia+pixel`. Both
  channels independently agreed — the accessibility Toggle state *and* the glyph on screen.
- **`X` TOGGLES, it does not set** (`probe-checkbox`, measured). Sending it at an already-ticked
  box would CLEAR it. The entry path types nothing when the box already holds the wanted state,
  which is the only reason this run is not silently wrong.

**The run to repeat:**

```
python agent.py probe-checkbox --binding binding.json --field 47   # once, per build
python agent.py write-w2 --binding binding.json --json sample_w2.json --skip-field 4 --ts T
```

`--skip-field 4` leaves the employer EIN alone, at the founder's explicit instruction. Both
commands leave the heads-down popup open — press **Esc** in Drake between runs, or the next
one refuses to start (correctly: an inherited popup's state is unknown).

### Where we are lagging — the honest list

**Blocker, non-engineering: the Drake written authorization.** Until it is signed, none of
this may touch a real client return. It is the single thing standing between a working adapter
and a usable product, and no amount of code fixes it. The parallel track is CCH Axcess, which
has a real API and needs a partner conversation.

**A full W-2 has now been entered end to end (2026-08-04, 20/20).** What is still unproven is
REPETITION and BREADTH: one clean run on one test return with one employer is not the same as
a reliable adapter. Nothing here has been run twice in a row without a code change in between.

**Unconfirmed on this build, flagged at runtime rather than assumed:**
- **The dropdowns past 57**: the rest of `w2_map.DROPDOWN_FIELDS` (63/64/70/71/77/78/84 and the
  box-12 codes 37/40/43). Typing the code is confirmed on 1, 9, 34 and 57, so these are likely
  fine — but likely is not confirmed.
- **Multiple W-2s, and any employer whose EIN is NOT skipped.** Every live run so far has used
  `--skip-field 4`; the EIN auto-fill path is handled in code and proven offline, never live.
- Fields mapped but never actually entered: box 14, box 12 years, state rows 2-4.
- Whether Drake's numeric boxes accept `.` and `-` as typed.

**Only the W-2 screen is mapped.** `w2_map.py` covers 75 fields with an import-time validator
that refuses foreign-only, inert, duplicate and out-of-range bindings. Every other form —
1099, K-1, Schedule C — is unmapped, and the field numbers are per-build, so mapping is manual
calibration work.

**Field numbers are build-specific.** They come from a screenshot of the live heads-down
screen. A Drake version bump can move them, and there is no automated way to detect that.

## 7. Working agreements with this user

- They are **not an engineer**. Explain in terms of what happens and what it costs, not in
  terms of the call stack. They are direct, they will tell you when something is not working,
  and they push hard for results — one message opened with *"make it work in one go or I'll
  drop this project."* Match that with real progress, not reassurance.
- **They often diagnose the halt themselves and prescribe a fix.** Those diagnoses have been
  roughly half right every time — the right area, the wrong mechanism. Read the actual code
  path before implementing a prescribed fix, and if it would not work, say so plainly and
  explain why. Twice a prescribed fix would have moved the failure one line later; once it
  would have removed every safety gate. They respond well to being told this directly.
- **Do not weaken a safety gate to make a run proceed.** The failure mode this whole design
  exists to prevent is silent: one field lands wrong, every later value shifts one box, and
  every row reports OK. Halting is the correct behaviour. Fix the read, never the gate.
- Report faithfully. If something is unverified, say so — that is the product's whole pitch.

## 8. Orientation for your first hour

1. `README.md` — the engineering reference. The sections on the anchored-regex trap, finding
   the box by shape, and the painted popup are the ones that matter.
2. `drake_driver.py` — all the safety logic. Start at `headsdown_type`, which is the entry
   state machine and is documented step by step in its docstring.
3. `simulate_headsdown.py` — run it. It is the fastest way to understand the protocol.
4. `w2_map.py` — extraction keys → Drake field numbers. Pure functions, no Drake, testable
   anywhere.
5. `agent.py` — the CLI. `write-w2 --dry-run` works on any machine and prints exactly which
   box each value would land in.
6. `PRECISION-PLAN.md` — the precision/verification design rationale.

`git log` is worth reading in full. Each commit message explains a real failure and why the
fix is what it is; that history is the reason the current design looks paranoid.
