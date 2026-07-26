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

### 1c. The make-or-break that's actually left: can the robot TYPE reliably?

All read-back is now visual, so the open question is no longer "can we read?" but
**"can we put values in the right boxes with keyboard nav, given chords break focus?"**
Prove that first — it's cheaper than OCR and decides whether Drake-RPA is viable at all:

1. Fill `field_no` (or `tab_index`) for the W-2 fields in `binding.json` (keyboard nav,
   not clicks).
2. `python agent.py selftest --binding binding.json --plan selftest.plan.json --shot after.png`
3. Open `after.png` and eyeball whether EIN / wages / withholding landed in the right
   boxes. If yes → build OCR read-back on top (below). If the robot mis-lands → RPA into
   Drake may not be viable; report back before investing further.

### 1d. `shoot` — capture the window (human floor + OCR calibration)

```
python agent.py shoot --binding binding.json --out drake.png
```

Saves a PNG of the live Drake window. Two uses: (1) the **human-verify floor** when
there's no read-back, and (2) **OCR calibration** — open the PNG, read each field's
pixel box `[x, y, w, h]` (relative to the window's top-left), and put it in
`binding.json` under that field's `"ocr_box"`. Then set
`capabilities.read_back_method: "ocr"` + `can_read_field_values: true`.

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

## Files

- `agent.py` — CLI + modes.
- `drake_driver.py` — the pywinauto driver (focus/type/read). **The parts that depend
  on your Drake build live here and in `binding.json` — expect to iterate on the VM.**
- `protocol.py` — the wire protocol + dispatcher.
- `binding.example.json` — the field-binding template (copy to `binding.json`).
- `selftest.plan.json` — the canned W-2 self-test.
