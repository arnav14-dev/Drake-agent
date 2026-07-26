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

## Probe result (2026-07): Drake exposes no UIA field values → Plan B

On a live **Drake Tax 2025** W-2 screen, `probe` found **0 Edit controls** and **0
readable values** via UI Automation; the win32 backend saw only the 5 outer frame
panels. Drake draws its data-entry grid on a **custom canvas** — individual boxes are
not OS accessibility elements. So the original "read the box back via UIA" moat is
**off the table**. Read-back now goes, in order of preference:

1. **Clipboard copy-back** — focus a field, `Ctrl+A`/`Ctrl+C`, read the clipboard.
   EXACT if Drake fields support copy. Test with `clip` (below). ← try this first
2. **OCR of a screenshot crop** — approximate; needs Tesseract + per-field pixel boxes.
   Only if clipboard fails (not built yet — report back first).
3. **Screenshot + human verify** — the robot types + captures each screen; the human
   confirms in Fynn before executing. Always available; the honest floor.

The "never files / human verifies before execution" guarantee is unchanged — the moat
just shifts from *programmatic exact* read-back to *whichever of the above your build
supports*, and Fynn's UI shows which.

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

### 1b. `clip` — the NEW make-or-break test (clipboard read-back)

Since UIA is opaque, this is the test that decides whether we get **exact** read-back.
Open Drake on a test return with a field that already holds a value, then:

```
python agent.py clip --binding binding.json
```

You get a few seconds to **click into a Drake field**; the agent then sends
`Ctrl+A`/`Ctrl+C` and prints the clipboard. (It uses a countdown, not an Enter prompt,
so your console never steals focus from Drake.)
- **Your value prints** → EXACT clipboard read-back works. Set
  `capabilities.read_back_method: "clipboard"` + `can_read_field_values: true`. Continue.
- **Blank** → Drake doesn't support copy from that field. Stop and report back before we
  invest in the OCR path; leave the caps false so Fynn won't claim a read it can't do.

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
committing** and checking it against `_expect`. `PASS` means the robot can type and
verify a box end-to-end. Add `--dry-run` to print the keystrokes without touching Drake;
`--slow` to watch it happen.

The read-back only works once `capabilities.read_back_method` is set (from `clip`, e.g.
`"clipboard"`). Left `"none"`, every `readField` honestly returns no value and the
`_expect` checks report MISMATCH — that's the honesty gate, not a bug.

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
