# HANDOFF — read this first

You are picking up a project mid-flight. This file is the orientation: what is being built,
what is actually true about the target application, what is finished, what is unverified,
and where the real blockers are. `README.md` is the engineering reference — this is the map.

Written 2026-08-03, at commit `a885be6`.

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

**Drake exposes no programmatic read-back on its data-entry grid.** UI Automation reports 0
edit controls and 0 readable values; the win32 backend sees only outer frame panels; clipboard
copy returns nothing *and* the Ctrl+A/Ctrl+C chord breaks field focus and pops a modal
validator. The grid is a custom-painted canvas.

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
python simulate_headsdown.py     # 41 cases, ~90 seconds, runs anywhere (no Windows needed)
python mutants.py                # 23 mutants, ~20 minutes — breaks each guard, suite must go red
```

`simulate_headsdown.py` fakes **Drake**, not pywinauto: the driver's real decision code runs,
only the Windows primitives are replaced. `mutants.py` edits each safety guard *in the source*,
in an isolated copy, and requires the suite to fail. **A guard whose mutant survives has no
test, whatever the suite says.** Two mutants are documented in that file as deliberately not
gaps (one equivalent mutant, one message-only difference) — read the comments before adding
them back.

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

## 6. Where it stands

**Working and verified offline (41/41 cases, 23/23 mutants killed):** the full entry state
machine — popup discovery by handle, structural identification of the typing box, painted-popup
reads through UIA/OCR, token-run counting against a per-channel baseline, silent-refusal
detection, commit proof, the structural dialog gate (which stops the always-present "Drake
Software Chat" overlay from halting runs), and named-button dismissal of Drake's e-file
completeness warning.

**Verified on the live machine:** `probe-popup` passes; the popup opens, is found, and is
readable via UIA.

**The immediate next step** — this had not been run when the handoff was written:

```
git pull
python agent.py write-w2 --binding binding.json --json sample_w2.json --skip-field 4 --ts T
```

`--skip-field 4` leaves the employer EIN alone, at the founder's explicit instruction.

### Where we are lagging — the honest list

**Blocker, non-engineering: the Drake written authorization.** Until it is signed, none of
this may touch a real client return. It is the single thing standing between a working adapter
and a usable product, and no amount of code fixes it. The parallel track is CCH Axcess, which
has a real API and needs a partner conversation.

**A full W-2 has never been entered end-to-end on the live machine.** Every live run so far has
halted on field 1 or earlier. Each halt has been a genuine driver bug, found and fixed — but
the sequence past field 1 is still unproven against real Drake.

**Unconfirmed on this build, flagged at runtime rather than assumed:**
- **Dropdown fields** (1, 9, 34, 57 and others in `w2_map.DROPDOWN_FIELDS`) — typing the code
  usually selects the entry, but this is untested. **Field 1 is a dropdown**, so it is the next
  thing likely to speak up.
- **The checkbox token `X`** for Box 13 (fields 46/47/48). `1` works on some builds. False is
  never entered at all, rather than risk clearing a human's tick.
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
