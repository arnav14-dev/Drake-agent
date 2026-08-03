# Fynn Drake agent

The robot that drives Drake's on-screen data entry, box by box, like a data-entry
preparer — and **reads every box straight back** so Fynn can prove what landed.
It runs on the **Windows VM** next to Drake and talks to the Fynn backend over the
`DrakeDriver` protocol.

## It never files

There is **no file / e-file / calculate-and-transmit command** in this agent or its
protocol. It types data-entry boxes and stops. A human verifies in Drake and
executes the return. This is a hard guarantee, not a setting.

## Legal gate

Drake's license restricts automated ("robotic") data entry without written
permission. `connect` (live) mode should only be used once that authorization is in
place — on the Fynn side, live mode also stays locked until `DRAKE_AUTOMATION_AUTHORIZED`
is set. `probe` / `calibrate` / `selftest` are local feasibility tools you run on your
own trial to see whether this is even mechanically possible.

## Install (on the VM)

```
python -m pip install -r requirements.txt
copy binding.example.json binding.json
```

**Install Tesseract too.** It is not a Python package — get the UB-Mannheim Windows build,
and if it isn't on `PATH` set `tesseract_cmd` in `binding.json`. This is not optional on
Drake 2025: the heads-down popup owns no child window, so screen-reading is the only channel
that can confirm a keystroke before Enter, and without it every field halts unverified. Check
with `agent.py probe-popup` — it prints each read channel and what it returned.

Run everything in the **interactive, unlocked** Windows session (UI Automation can't
drive the GUI from a locked screen or a service). Don't touch the keyboard/mouse
while a run is in progress.

> **Windows gotchas we hit:**
> - If you create/edit `binding.json` in PowerShell or Notepad it may get a UTF-8 BOM.
>   The agent reads with `utf-8-sig` so that's handled — but if another tool complains,
>   save as "UTF-8 (without BOM)".
> - The default window match is now `Drake \d{4} Tax Software` (e.g. "Drake 2025 Tax
>   Software"), so it won't collide with **Drake Software Chat** or a browser tab. If
>   your title differs, set `app_title_re` in `binding.json`.
> - **Screenshots capture the window's *screen rectangle*, not its pixels** — so if
>   Drake is behind the editor/terminal, the capture (and OCR) grabs the *wrong* window.
>   The agent now forces Drake to the foreground (`set_focus` + a z-order raise) before
>   every capture. Still: don't touch the mouse/keyboard mid-run, and if you can, launch
>   from a plain terminal rather than the editor's integrated one (foreground rights are
>   friendlier). If an early `after.png` shows VS Code, that's this bug — re-run.
> - **The Drake Live Chat bubble is a topmost ~84x84 window in the Drake process**, so
>   `top_window()` binds to *it* — keystrokes go to the chat widget and the form stays
>   blank. The agent now picks the largest top-level Drake window that clears
>   `main_window_min` ([600, 400]) instead. `probe` and `shoot` print the bound window's
>   title + size — check it's the data-entry frame, not an 84x84 overlay.

## What we learned probing Drake Tax 2025: no programmatic read-back exists

We tested every way to read a box back on a live **Drake Tax 2025** screen. All dead:

| Vector | Result |
| --- | --- |
| **UI Automation** (`probe`) | 0 Edit controls, 0 readable values — grid is a custom canvas |
| **win32 backend** | 0 inner form fields (only the 5 outer frame panels) |
| **Clipboard** `Ctrl+A`/`Ctrl+C` (`clip`) | Nothing copies — **and** the chord breaks field focus + pops modal validators |

Drake is **visual-only**: you cannot read a field's value programmatically. So the
original "type a box, read it back exactly via UIA" moat is off the table. Read-back
now goes, in order of preference:

1. **OCR of a screenshot crop** (`read_back_method: "ocr"`) — screenshot a calibrated
   per-field box, OCR it, compare. **Approximate** (confidence < 1.0) — a typo-catcher,
   not proof. Needs `pytesseract` + the Tesseract binary + an `ocr_box` per field.
2. **Screenshot + human verify** (`read_back_method: "screenshot"`) — the robot types,
   `shoot` captures the screen, a human confirms in Fynn before executing. Always
   available; the honest floor.

**Input** also changes: because Ctrl-chords break focus, drive fields with Drake's
**native keyboard flow** (heads-down field numbers + Enter/Tab), *not* pixel clicks —
that's what `field_no` / `tab_index` in the binding are for. Pixel coordinates are used
only to define OCR crop boxes, never to click.

The "never files / human verifies before execution" guarantee is unchanged — the moat
shifts from *programmatic exact* read-back to *OCR-flag + mandatory human screenshot
gate*, and Fynn's UI shows which read-back a field got.

## Do these in order

### 1. `probe` — per-build re-check (expect all-opaque)

Open Drake on a **test return**, open the **W2** screen, then:

```
python agent.py probe --binding binding.json
```

It lists every Edit control and whether each exposes a **readable UIA value**. On the
2025 build this comes back **0 controls / all opaque** (see above). Re-run it per build
in case a future Drake exposes UIA — if it ever shows **READABLE** boxes, set
`capabilities.read_back_method: "uia"` + `can_read_field_values: true`. Otherwise → `clip`.

### 1b. `clip` — clipboard read-back (RULED OUT on Drake 2025)

We already ran this: `Ctrl+A`/`Ctrl+C` copies **nothing** from a Drake field and the
chord breaks focus + pops a modal validator. Re-run it only to re-check a *different*
build/software. On Drake 2025 skip straight to the screenshot floor + OCR.

### 1c. Can the robot TYPE into Drake? (Juno does it the same way — foreground keystrokes)

Research settled the big question: the competition (Juno) drives Drake with the **same**
foreground GUI automation — real mouse + real keystrokes — not a secret API. So typing
IS possible; earlier failures were the *accessibility/set_text* path (empty on a custom
canvas), not synthetic keystrokes. Prove it in the cheapest order:

**Step A — `typetest`: do our keys land at all?** (no calibration)

```
python agent.py typetest --binding binding.json --text 52000 --shot typed.png
```

Click a Drake box during the countdown; the agent types with genuine foreground
keystrokes. Value shows up → typing works, done — move on. Still empty → keys are being
dropped: re-run the console **as Administrator** (see the elevation warning it prints).

**Step B — `selftest.seq.json`: type a whole W-2 with NO coordinates.**

```
python agent.py selftest --binding binding.json --plan selftest.seq.json --shot seq.png
```

This uses Drake's native flow: the caret lands in the first field on screen-open, we type,
`Enter` advances to the next field, repeat. Open `seq.png` and tell me **which box each
value landed in** — that reveals the field order so we can map it exactly.

**Step C — `click_xy`: precise per-field targeting** (once A/B prove typing works)

1. `python agent.py shoot --binding binding.json --out drake.png` — open `drake.png`,
   read each W-2 field's **center pixel** `[x, y]` into `binding.json` as `"click_xy"`
   (grab the full box `[x,y,w,h]` → `"ocr_box"` too).
2. `python agent.py selftest --binding binding.json --plan selftest.plan.json --shot after.png`
3. `after.png` right? → wire OCR read-back (`read_back_method: "ocr"` +
   `can_read_field_values: true`). Mis-lands → the `click_xy` points need re-reading.

> **Note (facts that shaped this):** Drake's canvas has no focusable OS element, so
> precise focus needs a physical **click** (`click_xy`); and **Ctrl chords are toxic** —
> `Ctrl+A` (old clear) and `Ctrl+N` (old heads-down toggle) break focus + pop a modal,
> both now purged from the defaults. Also: Drake's **2026 license bans automated entry
> without written authorization** — `typetest`/`selftest` are local proofs on your own
> install; live client use needs that authorization first.

**Quick disambiguating test** (if the self-test still types nothing, run this to tell
*why* in one shot — Drake open on a W-2 screen):

```
# (1) delivery + caret: click a box by hand, then:
python -c "from pywinauto.keyboard import send_keys; send_keys('52000', pause=0.05)"
#   digits appear -> input IS delivered (elevation ruled out) and a click focuses. 
# (2) clear-chord: with the caret still there:
python -c "from pywinauto.keyboard import send_keys; send_keys('^a')"
#   modal pops / caret leaves -> confirms Ctrl+A is toxic (and re-proves delivery).
# (3) nothing lands in (1) AND ^a pops no modal in (2) -> input is being dropped:
#   elevation/UIPI — run the agent as Administrator (connect() also warns on mismatch).
```

### 1d. `shoot` — capture the window (human floor + click/OCR calibration)

```
python agent.py shoot --binding binding.json --out drake.png
```

Saves a PNG of the live Drake window (printing the bound window's title + size so you
can confirm it's the data-entry frame, not the chat overlay). Three uses: the
**human-verify floor**, **`click_xy` calibration** (each field's center point — the
click that plants a caret), and **`ocr_box` calibration** (each field's full
`[x, y, w, h]` rectangle for OCR read-back). One capture gives you all three.

### 2. `calibrate` — capture the field binding

For each screen (W2, INT, DIV, NEC, 1099, 1098):

```
python agent.py calibrate --binding binding.json --screen W2
```

It prints the screen's controls as binding stubs. Map each **logical field** (the
names already in `binding.example.json`, e.g. `box1_wages`) to the `automation_id`
you see. Tip: focus a box in Drake, re-run, and note which control's value/identity
matches. If your build has no useful `automation_id`, fall back to `field_no` (Drake
heads-down field number) or `tab_index` (Tabs from the first field).

### 3. `selftest` — prove the type→read-back loop (no Fynn needed)

With `binding.json` filled and Drake open on a test return:

```
python agent.py selftest --binding binding.json --plan selftest.plan.json
```

It runs a canned W-2 entry (EIN + wages + withholding), reading each box back **before
committing** and checking it against `_expect`. Add `--dry-run` to print the keystrokes
without touching Drake; `--slow` to watch it happen; `--shot after.png` to save a
screenshot when the run ends.

What `_expect` does depends on `capabilities.read_back_method`:
- `"ocr"` → each box is OCR'd and compared (tolerant of `$`/`,`); `PASS` = typed **and**
  OCR-verified. An OCR mismatch is a real `MISMATCH`.
- `"screenshot"` / `"none"` → there's no programmatic value, so `_expect` can't pass or
  fail — it reports **UNVERIFIED** and the run ends `TYPED OK — N field(s) UNVERIFIED`,
  telling you to confirm from the screenshot. That's the honesty gate, not a bug.

### 4. `connect` — go live with Fynn (last step)

Once probe/selftest pass and you have authorization, connect to Fynn's agent endpoint:

```
python agent.py connect --binding binding.json \
  --url  wss://<fynn-host>/drake-agent \
  --token <DRAKE_AGENT_TOKEN>
```

The agent dials **out** to Fynn (no inbound firewall hole), authenticates with the
shared token, and serves the live protocol: Fynn sends `openScreen` / `focus` /
`type` / `readField` and the agent drives Drake and replies. This matches Fynn's
`WindowsDrakeDriver` (`DRAKE_AGENT_WS_URL` / `DRAKE_AGENT_TOKEN`).

> The Fynn-side WebSocket endpoint for `connect` is the next piece to build on the
> backend; `probe` → `calibrate` → `selftest` are fully usable today and are what
> unblock everything else.

## The protocol

`{"id", "method", "params"}` → `{"id", "result"}` or `{"id", "error"}`. Methods mirror
Fynn's `DrakeDriver` seam exactly (`capabilities`, `openReturn`, `openScreen`,
`addFormInstance`, `focus`, `type`, `press`, `readField`, `screenshot`). `protocol.py`
is the single dispatch point, so `selftest` and `connect` drive Drake identically.

## `write-w2` — extracted JSON → Drake, by field number

The product path. The LLM reads the W-2 PDF and emits the schema in `sample_w2.json`;
`w2_map.py` resolves each key to a Drake **heads-down field number** and sanitizes the
value; the driver enters every field by number through the verified loop. No model
decides which box a number lands in — that's a fixed table, which is what makes the
entry auditable.

```
python agent.py write-w2 --json sample_w2.json --dry-run        # review first, anywhere
python agent.py write-w2 --binding binding.json --json sample_w2.json
```

`--dry-run` touches nothing and needs no Drake, no VM, no binding — it prints the fully
resolved plan: which box each value goes in, what was skipped (empty/zero boxes are left
alone), what was **REJECTED** (had content but wasn't valid — e.g. an unrecognized state
name), and which boxes need a human eye (dropdowns, checkboxes, identity fields). Read
that before you ever let it type.

### How the entry loop stays precise

Each field is entered through a gated state machine (`drake_driver.headsdown_type`) that
never sends a keystroke it hasn't verified the destination of:

1. The heads-down popup is a **real Win32 dialog**, not Drake's opaque canvas — so it's
   driven as one, via a second `backend="win32"` connection.
2. Its typing box is focused and focus is **proven** with `GetGUIThreadInfo` before anything
   is typed. On this build that box *is the popup* — it owns no child window (see below).
3. The field number is typed, then **read back** and asserted equal — *before* the
   irreversible Enter. `WM_GETTEXT` when there's a child Edit; otherwise the popup is read
   as a whole through the accessibility tree or OCR.
4. After the jump, the driver **observes** which popup model this build uses rather than
   assuming: popup closed → the value goes on the canvas; popup still open and prompting
   → the value goes back into the popup. A build or tax-year difference can't silently
   mis-route a value.
5. Any unexpected dialog, rejected number, or read-back mismatch **HALTs** the batch. It
   never auto-dismisses a modal and never writes on past a failure.

### The structural dialog gate (why 'Drake Software Chat' can't halt a run)

The dialog check does **not** use a title allowlist (the old version did, and the
always-present 'Drake Software Chat' window in the same process halted every run at the
first field). Windows are judged by **structure**:

- **Baseline**: every window the Drake process already has at attach (the chat overlay,
  tool panels) is recorded and benign by definition — a window never halts a run by
  *existing*.
- **Blocking** means one of two provable states: a **new dialog-shaped window** appeared
  (`#32770` — every MessageBox/validator — or an owned, captioned popup), or the **main
  frame got disabled** while the heads-down popup isn't up (something modal is pumping,
  even if it can't be enumerated; debounced once for creation/teardown churn).
- Everything else that appears mid-run (toasts, dropdown lists, tooltips, the chat bubble
  expanding) is **benign**: logged once, remembered, ignored.

On top of that sits an **HWND-scoped keystroke gate** (`_input_scope`): before Ctrl+N and
before typing a value on the canvas, the driver proves the *foreground root window* — the
place `SendInput` keys actually land — is Drake's main frame (or the heads-down popup).
If the chat window, the terminal, or anything else holds the keyboard, the batch HALTs
with that window's name instead of typing into it.

Whenever a batch halts, the full window topology is written to **`env-dump-halt.json`**
automatically (every window's class/style/owner/enabled state, its children, where the
keyboard was, and the gate's verdict per window) — so a wrong halt is diagnosable from
one file, with no manual capture round-trip. The same dump is available on demand:

```
python agent.py envdump --binding binding.json --out env-dump.json --delay 5
```

### The confirmed protocol, and what proves each step

Drake 2025 uses the **persistent command bar**: the popup stays up and alternates
`number → Enter → value → Enter → number`, with the value typed *into the popup*. The
driver detects this at runtime rather than assuming it, so a build that behaves differently
still routes correctly.

Two things make that safe, and both are new:

**Drake's own prompt is the source of truth.** A field number Drake *refuses* clears the
edit box exactly like one it *accepts* — so "the box changed" cannot tell them apart.
Boxes that are greyed out (box 9) or foreign-address-only (11/12/13) decline **silently**:
no dialog, no complaint, the caret simply doesn't move. Believing that refusal was an
acceptance is what makes the next field's *number* get typed as this field's *value*, after
which every value lands one box off and every row still reports OK. The driver now
baselines the popup's prompt text before Enter and requires it to *change* — and after the
value, requires it to come *back* to the number prompt. Compared against the captured
baseline, never a hardcoded English string, so a reworded build can't silently disable it.

**Read-backs must converge, not just match once.** Keystrokes are *posted* to Drake's queue;
`WM_GETTEXT` and `set_edit_text` are *sent* and jump ahead of it. The old "read it back, and
repair it with set_edit_text if it's wrong" could therefore read a half-arrived `520`,
repair it to `52000`, re-read clean, pass the gate — and only then have the queued `00`
arrive and append, committing **5,200,000** while reporting success. The repair is gone;
the driver now waits for two consecutive identical reads, which proves every injected key
has already been consumed.

The comparator was loosened in the same direction and is now strict: it forgives Drake's
cosmetic reformatting (`52,000` = `52000`, `12-3456789` = `123456789`, `52000.00` = `52000`)
but never a moved decimal point or a flipped sign. `322450` is not `3224.50`.

### Windows are found by handle, never by pywinauto's `title_re`

pywinauto matches `title_re` with **`re.compile(pattern).match(title)`** — anchored at the
start (`findwindows.py:274-281`). Every hand-rolled check in this driver uses `re.search`.
That difference made two parts of the driver disagree about whether the same window existed:

```
title   = 'Drake 2025 - Heads Down Data Entry'
pattern = 'Heads.?Down Data Entry'
  re.match  (pywinauto title_re) -> None          <- popup "does not exist"
  re.search (our own checks)     -> <Match>       <- popup is right there, and focused
```

So `_find_headsdown_popup` returned `None` for a popup that was on screen and had keyboard
focus, and `probe-popup` reported *"popup not open — click a Drake field first"* about a
window the operator was looking at. Worse, it silently voided `_ensure_popup_open`'s central
promise: that function can only avoid toggling an open popup back **off** if it can *detect*
that one is open — and detection always failed, so the retry loop could fire Ctrl+N into an
already-open popup and close it.

The same trap applies to `connect(title_re=...)`: the live frame is titled
`Drake 2025 - Data Entry (…)`, which `Drake \d{4} Tax Software` cannot match from the start
either. It happened to work only because `_resolve_main_window` re-scans every window of the
process afterwards.

Both are fixed by never asking pywinauto to *find* a window by title:

- `_find_popup_hwnd` enumerates top-level windows via ctypes and matches with `re.search`
  (this process first, then all processes — a popup owned by a different process was
  equally invisible before), then hands pywinauto an exact `window(handle=…)`.
  `find_elements` returns immediately for a handle criterion, bypassing every other filter.
- `_connect_uia` keeps the pywinauto path but falls back to finding the PID ourselves.

A regression case asserts the real titles behave as expected under both matchers, and that
the source never resolves a window by `title_re` again.

### …and the popup's text box is found by shape, never by class name

The same class of bug one layer down. `popup.child_window(class_name="Edit")` matches with
**exact string equality** (`findwindows.py:257`), and the only regex variant, `class_name_re`,
is the same anchored `re.match`. A control is called `Edit` only if the toolkit happens to
name it that: Delphi/C++ Builder emits `TEdit`/`TMemo`/`TMaskEdit`, .NET emits
`WindowsForms10.EDIT.app.0.378734a`, rich text emits `RichEdit20W`.

On the live build it matched nothing, so `probe-popup` returned pywinauto's criteria dict as
its "reason" and `write-w2` halted on its **first** field with *"popup edit not ready / no
handle: timed out"* — an unfindable box, reported as a timeout.

`_resolve_popup_edit` enumerates the popup's children with ctypes and ranks them:

1. the exact class the binding was calibrated to (`headsdown_popup_edit_class`)
2. any edit-shaped class — `edit`/`textbox`/`memo` as a substring, case-insensitive — with
   the focused one winning if there is more than one box
3. the child that currently **owns the keyboard**, whatever its class

A `Static` prompt label is never chosen; a disabled child is never chosen. If nothing
qualifies, the driver raises `PopupEditNotFound`, which carries the popup's whole child
topology into the halt line — class names, handles, visibility, text — because *that* is the
missing fact, and a halt saying only "timed out" costs a round trip to the Windows machine.
The same verdict is in `envdump` under `popup_edit`, and `probe-popup` now dumps
`popup_controls` via ctypes **before** resolving, so a failure to identify the box still
reports what is in there. Nothing is ever typed blind.

`WM_SETTEXT` is refused on a target that is not a text box: on a non-edit window it rewrites
the **caption**, which on the popup itself would rename the very window we find it by.

### The popup has no child window at all — Drake paints it

Confirmed on the live build: `EnumChildWindows` on the heads-down popup returns **`[]`**, and
`GetGUIThreadInfo().hwndFocus` **is the popup itself**. There is no Edit control because there
is no control — Drake draws the box onto the dialog, the same way it draws the data-entry
grid. So the popup *is* the keyboard target, and it is typed into directly.

That removes the foundation every safety gate was standing on. `WM_GETTEXT` on the popup
returns its **caption** (`Drake 2025 - Heads Down Data Entry`) — a constant that has nothing
to do with what was typed, and reading it as "the box" would report every silent refusal as
an acceptance. So the box is read *as a whole*, through the first channel that answers:

| channel | what it is | when it works |
|---|---|---|
| `win32` | `WM_GETTEXT` on a child Edit | only on builds that have one |
| `uia` | the accessibility tree, addressed by handle | if Drake exposes anything at all |
| `ocr` | screenshot the popup, Tesseract | always — a painted box is still a picture |

A reading contains the prompt *and* whatever has been typed, in one string, so verification
changed shape:

- **Token runs, not substrings.** `5` must not match inside `52000`, but `TEST EMPLOYER LLC`
  is three tokens and Drake renders `52,000` as two. A match is a run of consecutive tokens
  whose concatenation is exactly right — it can span punctuation but cannot start or end
  mid-token.
- **Counting, not presence.** Drake's value prompt names the field (*"Enter the value for
  field 1…"*). A presence test would pass on the prompt's own `1` when nothing was typed, and
  would refuse the legitimate entry of the value `1`. So the count must *exceed* what the
  same popup showed before we typed.
- **Stability on the normalised reading.** Two consecutive readings of an unchanged popup are
  not byte-identical when they come from OCR — spacing and punctuation move. Comparing raw
  text would time out on keystrokes that had in fact landed; comparing alphanumerics-only
  keeps the drain proof without failing on a stray comma.
- **The baseline is not exempt.** Every refusal check compares later readings against a
  prompt captured *before* Enter, so that capture has to converge too. The simulator's
  character-noise case caught this in this driver: one corrupted frame taken as the baseline
  inverts the logic — every *clean* frame afterwards differs from it, a silently refused
  field reads as accepted, and the value gets typed at the number prompt. `_stable_prompt`
  requires the baseline to be seen twice, and returns `''` (→ halt, on a painted popup)
  rather than hand back a reading it could not confirm.
- **A frame with no words in it is not a reading.** `_read_popup_channels` drops any channel
  whose text normalises to nothing — blank *or* punctuation-only, because OCR fails by
  returning `|_. -~` far more often than by returning nothing. This is what halted the first
  live `write-w2` at field 1: `_settle_surface` already skipped unreadable frames (so the
  field *number* verified fine), while the baseline path went through `_popup_prompt`, which
  collapses "no channel answered" into `""` — recording it as a reading of empty and
  discarding the good reading either side. Two good readings never sat next to each other,
  so a perfectly legible popup produced no baseline and the run halted with *"could not read
  the popup at all after jumping to field 1"*. `_classify_after_jump` had the same flaw one
  step later, where it would have reported a false **refusal** instead.
- **Per channel, never joined.** Judging the channels as one concatenated string makes the
  reading only as steady as the least steady channel — a jittering OCR feed stops a clean UIA
  reading from ever converging. Judging first-wins does the opposite: a channel answering
  with something useless shadows one that can actually see the typed text. Each channel is
  compared against its own pre-typing baseline, and one channel proving the keystroke is
  enough.
- **No baseline → stop before the Enter.** Not after. The number has only been typed at that
  point, so Drake is left exactly as it was found.

If **no** channel can read the popup, entry halts before the first Enter rather than typing
blind, and says that installing Tesseract is what enables the only read-back a painted box
allows. `probe-popup` reports each channel by name, what it returned, and whether it could
see the probe number — that verdict is `read_back: AVAILABLE via … / UNAVAILABLE`.

### One dialog may be answered automatically — by clicking a named button

Drake raises *"There are fields on this screen that must contain data if you are planning to
e-file this return"* whenever the screen is left with e-file-required boxes empty, which is
the normal state of a W-2 halfway through being keyed. It blocks entry, so it is on a short
allowlist (`navigation.auto_dismiss`, matched on the dialog's **text** — `Drake 2025 - Data
Entry` captions several different dialogs).

It is answered by **finding the button named `OK` and clicking that button**, never by
sending Enter. Enter presses whichever button is the dialog's default, unseen; here the two
answers are *stay on this screen and finish entering* and *leave the screen anyway*, and the
second silently moves Drake off the W-2 mid-batch, after which every remaining field number
addresses something else. No button with that name, no keystroke — it halts and lists the
buttons it did find. Every dialog not on the list still halts for a human.

**The Field-4/EIN exception** (confirmed on Drake 2025): committing the employer EIN
fires Drake's employer lookup + auto-fill, auto-advances the caret to Box 1, and
*swallows the next Ctrl+N*. That single eaten chord is what caused the original cascade —
field numbers typing onto the canvas, every value landing one box too far down.
`_ensure_popup_open` handles it by re-checking popup presence before every Ctrl+N (so it
can never toggle the mode back off) and retrying with a settle. No per-field special
case: any auto-advancing field recovers the same way.

### Testing it without the VM

```
python simulate_headsdown.py
```

A fake Drake reproducing the observed behaviours — the persistent protocol, the Field-4
swallowed chord, silent refusals, and a popup left armed by a previous run — so the state
machine is provable in seconds, anywhere, before it touches a return. 41 cases: both popup
models, the EIN-skipped batch shape, an inert box that declines silently (in both variants:
leaving the number in the edit, and clearing it), a silently refused value, corrupted and
late-arriving keystrokes, an empty value, an inherited armed popup, a build with no readable
prompt text, a popup whose text box is a `TEdit`/`WindowsForms10.EDIT…`/`RichEdit20W`, **the
confirmed painted popup with no child window** (entering a full W-2 under punctuation
jitter, character-level OCR noise, transient unreadable frames and OCR garbage frames;
refusing a silent decline; halting when nothing can read it; and halting *before* the Enter
when the channel dies mid-field), the e-file warning being dismissed by name, and the structural dialog gate — **every case runs with the 'Drake
Software Chat' window present** (the live field-4 halt).

It fakes *Drake*, not pywinauto: focus and window behaviour on the real thing is still
verified on the Windows machine.

**A fake that is easier than the real thing hides the bugs it exists to catch** — twice now.
`_FakeWin` once had an `exists()` that real `UIAWrapper` lacks, so the suite was green while
the live run died. Then the "painted" popup was modelled with a `Static` child still holding
the prompt, which quietly fed the driver clean text through a path the real build does not
have; the painted cases passed while the screen-reading path they existed to exercise was
barely used. Making the fake report `[]`, as the live probe does, failed a case immediately —
and that failure was a real defect in the driver, not in the test.

**Every guard is mutation-tested.** A green suite proved nothing the last two times — the
first fake gave `_FakeWin` an `exists()` that real `UIAWrapper` lacks, so 9/9 passed while
the live run died at field 4; the second made read-back an identity function, so every
read-back gate was dead code that could be deleted with the suite still green. So each
guard is now broken *in the source*, in an isolated copy, and the suite must go red:

| guard broken | suite result |
|---|---|
| comparator back to alnum-only (deletes `.` and `-`) | 40/41 |
| comparator drops the decimal/sign refusal | 40/41 |
| single read instead of convergence (the old repair) | 38/41 |
| empty-value guard removed | 40/41 |
| inherited-popup ownership check removed | 40/41 |
| prompt baseline removed (classify by the edit box alone) | 33/41 |
| commit proof removed (assume the value was accepted) | 40/41 |
| Ctrl+N keyboard-scope gate removed | 40/41 |
| popup edit matched by exact class name only (the live halt) | 40/41 |
| popup edit ranking accepts any child (a label becomes the target) | 40/41 |
| no children → type at the popup itself (blind entry) | 33/41 |
| painted popup: commit a value without confirming the keystroke | 40/41 |
| painted popup: Enter on an unconfirmed field number | 38/41 |
| painted popup: presence instead of counting (prompt's own digits) | 39/41 |
| prompt change accepted from a single jittery frame | 40/41 |
| prompt **baseline** taken from a single frame | 37/41 |
| informationless frame admitted as a reading (blank/punctuation) | 40/41 |
| unreadable frame recorded as a reading of `''` after the jump | 39/41 |
| no baseline → press Enter anyway and find out afterwards | 40/41 |
| auto-dismiss presses Enter blind instead of a named button | 40/41 |
| auto-dismiss treats every dialog as dismissable | 40/41 |
| prompt baseline lets edit-shaped children leak in | 40/41 |
| structural dialog gate blinded | 36/41 |

Two mutants are documented in the harness as **not** gaps rather than papered over with a
test that cannot exist: the `target.startswith(acc)` early-break in `_surface_count` is an
*equivalent* mutant (checked against the guard-free version over 200k random pairs — zero
behavioural differences; it is a speed guard), and `_is_surface_hwnd` in
`_classify_after_jump`'s final fallback chooses between two outcomes that **both halt**, so
it buys a truthful message rather than a different result.

A guard whose mutant survives has no test, whatever the suite says.

## Files

- `HANDOFF.md` — **start here if you are new to this project.** What is being built, what is
  empirically true about Drake, what is finished, and where the real blockers are.
- `mutants.py` — the mutation harness (`python mutants.py`, ~20 min).
- `agent.py` — CLI + modes.
- `drake_driver.py` — the pywinauto driver (focus/type/read). **The parts that depend
  on your Drake build live here and in `binding.json` — expect to iterate on the VM.**
- `w2_map.py` — extracted-JSON key → Drake field number + value sanitization. Pure
  functions, no Drake — importable and testable anywhere.
- `simulate_headsdown.py` — offline proof of the entry state machine.
- `protocol.py` — the wire protocol + dispatcher.
- `binding.example.json` — the field-binding template (copy to `binding.json`).
- `sample_w2.json` — the W-2 extraction schema the LLM must emit (test data).
- `selftest.plan.json` — the canned W-2 self-test.
