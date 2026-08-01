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
2. Its Edit control is focused and focus is **proven** with `GetGUIThreadInfo` before
   anything is typed.
3. The field number is typed, then **read back** with `WM_GETTEXT` and asserted equal —
   *before* the irreversible Enter.
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

A fake Drake that reproduces the observed behaviours — including the Field-4 swallowed
chord — so the state machine can be proven in a second, anywhere, before it touches a
return. It covers both popup models, auto-advance on other fields, that an invalid
field number halts instead of cascading, and the structural dialog gate: **every case
runs with the 'Drake Software Chat' window present** (the live field-4 halt), a benign
window appearing mid-run is ignored + logged, a disabled main frame halts structurally,
and the classifier's decision table is unit-checked. It fakes *Drake*, not pywinauto:
focus and window behaviour on the real thing is still VM-verified.

## Files

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
