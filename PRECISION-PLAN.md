# Drake 2025 Precision Data-Entry — Engineering Plan

*How we make writing into Drake Tax highly precise (right field, every time) and solid
(a flow that never silently mis-enters and can HALT for a human). Typing itself already
works — real virtual-key + scan-code injection (`vk_packet=False`). The weak points this
plan fixes are (1) landing the caret in the EXACT field and (2) verification + flow.*

> Scope / safety: this improves **precision and honesty only**. It does NOT change the
> authorization gate — driving Drake from an external process is still "Automated Means"
> under Drake's 2026 license, so all of this is internal proof-of-correctness on our own
> VM. No e-file/file path exists or is added. `can_read_field_values` stays `false` until
> read-back is empirically confirmed.

---

## 1. The core strategy shift

**Stop making pixel clicks the primary way we hit a field.** Drake ships a first-party,
*coordinate-free* field-addressing surface we were not using at all:

- **Heads-down data entry (Ctrl+N).** Toggles a mode where every field shows a stable
  **number**. You type the number to jump the caret to that exact field — zero pixel
  math, immune to DPI / resolution / window position (exactly the failure mode of our
  hardcoded window-relative clicks). The on-screen numbers also double as an OCR anchor.
- **Macro field-jump operators `[FJ:N]` / `[FF:N]` / `[FB:N]`.** `[FJ:N]` lands the caret
  in field N *absolutely* — a single missed keystroke can't cascade the way an Enter-count
  can.
- **Selector field.** Type a screen code + Enter to open the exact screen (e.g. `W2`) with
  no menu clicks.

The one refinement to the framing: the real blocker is **anchoring**, not navigation. Our
own `selftest.seq.json` note records that opening a screen leaves *no active caret* — which
is why a pure Enter-order flow "typed into nothing." So the architecture is:

> **open screen by code → plant a *verified* caret anchor at field 1 by keyboard
> (Ctrl+N / Ctrl+Home) → advance deterministically by field-number or Enter-count →
> guard before every keystroke, read back after → HALT / re-anchor on any mismatch.**

Pixel / image-anchored clicking survives only as the anchor-of-last-resort for a single
field per screen.

**A correction in our own code:** `driver.focus()`'s docstring claimed "the heads-down
toggle is a Ctrl chord that breaks focus," conflating **Ctrl+N** (Drake's documented mode
hotkey) with the genuinely toxic **Ctrl+A/Ctrl+C** clipboard chords. Re-testing Ctrl+N in
isolation is the pivotal experiment (§6).

---

## 2. Targeting — how we land the caret precisely

### Primary: keyboard-deterministic (no coordinates)

- **A. Open the screen by code:** `{ESC}` → `W2{ENTER}` (Selector). New instance of a
  repeatable screen: `{PGDN}` / `[New]`.
- **B. Plant the field-1 anchor by keyboard** (the new load-bearing step). Test in order:
  1. **Ctrl+N** (heads-down) then `1{ENTER}` → caret in field 1, numbers now visible.
  2. If Ctrl+N misbehaves on this build, **Ctrl+Home** ("move to first field").
- **C. Advance deterministically** from the verified anchor:
  - **Field-number (most robust):** heads-down `<n>{ENTER}<value>{ENTER}`, or macro `[FJ:n]`.
    A miscount cannot cascade — each address is absolute.
  - **Enter-count (lowest effort, already supported):** type value → `{ENTER}` → repeat,
    driven by the per-field `enter_index` already recorded for W-2
    (`employer_ein=0 → employer_name=1 → box1_wages=2 → box2_fed_wh=3`).

Macro-string form (aspirational, if we stand up Drake's macro engine):
`W2>[FJ:1]12-3456789[FJ:2]ACME[FJ:3]52000[FJ:4]6000~`  (`>`=Enter, `~`=save+exit).
Tabbed screens renumber per tab — use `[NEXTTAB]`.

### Fallback: image-anchored click (not absolute pixels)

Keep `click_input(coords=…)` but stop feeding it hardcoded constants — resolve the point
at runtime against a fresh capture:
- **Template match:** `cv2.matchTemplate(scr, tpl, TM_CCOEFF_NORMED)` + `minMaxLoc`,
  confidence ≥ 0.90, center = match + tpl_size//2, search restricted to the window region.
- **OCR-label anchor:** `pytesseract.image_to_data(...)`, find the label row ("Wages"),
  apply a learned `(dx,dy)` offset to the input box.
Reserve a hardcoded `click_xy` for exactly **one** anchor field per screen.

### The DPI fix (do regardless of path)

On 125%/150% displays a DPI-**unaware** process clicks in *logical* pixels while
screenshots come back *physical* — the miss scales with the display factor. Upgrade to
**Per-Monitor-v2** as the first UI-touching line (done — see `agent.py::_set_dpi_aware`):
`user32.SetProcessDpiAwarenessContext(-4)` → fallback `shcore.SetProcessDpiAwareness(2)` →
`user32.SetProcessDPIAware()`. Then `capture_as_image`, `GetWindowRect`, `GetCursorPos`,
`SetCursorPos` all share one physical-pixel space — a screenshot pixel *is* a click pixel.

---

## 3. Verification — how we read it back (ranked)

**Tier 1 — Ground truth: Drake's own View/Print text-layer PDF (the real moat).** Drake's
PDF Printer emits a *searchable* (text-layer, not image) PDF into
`Drake Documents\<client>\<year>\`. After a screen is entered: Calculate → `Ctrl+P` →
"Drake PDF" → watch that folder for the newest PDF → `pdfplumber.extract_text()` → reconcile
each value against its **per-document facsimile/worksheet line** (not the aggregated 1040
line — Drake rounds to whole dollars and sums multiple docs). Any mismatch, or any EF
"Flagged Fields"/"required field" message in the text, is a **hard HALT**. This is the only
read-back that earns confidence 1.0 and the only e-file-readiness gate.

**Tier 2 — Tuned per-field OCR (immediate typo-catcher, confidence < 1.0, never proof).**
Upgrade `_ocr_read` (currently `--psm 7`, no whitelist, hardcoded 0.6):
- Currency: `--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789.,$-()`
- EIN/SSN: `--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789-`
- Short text: `--oem 1 --psm 7`
- Preprocess: crop +10px pad → grayscale → 4× Lanczos upscale (x-height ≥ 30px) → mild
  unsharp → try both Otsu-binarized and raw, keep higher confidence → capture the field
  **UNFOCUSED** (white-on-blue selection + caret corrupt the read). Use `image_to_data`
  for per-token confidence. HALT if min-token conf < 80, two configs disagree, or the
  normalized value ≠ intended. Better still: OCR Drake's **Ctrl+V View-mode** rendered
  form (large clean black-on-white) instead of the cramped entry box.

**Tier 3 — Human screenshot floor.** `read_back_method="screenshot"` → `needsHuman:true`.
The honest default until Tier 1/2 are proven on the VM.

---

## 4. The solid flow / state machine

States: `IDLE → ANCHOR → AT_FIELD(n) → TYPED(n) → VERIFIED(n) → … → DONE`, plus `RESYNC`
and `HALT`.

**Modal sentinel (highest-leverage safety add).** The canvas is UIA-opaque, but Drake's
*dialogs* ("fields must contain data", EF messages, Calc Results) are ordinary enumerable
top-level windows. Cache `main_hwnd` at connect; before AND after every focus/type/commit:
`GetForegroundWindow() != main_hwnd` → a foreign window stole focus → capture its text →
HALT (debounce with one short recheck to ignore transient splash/tooltips).

**Per-field transition:**
1. **ANCHOR:** `{ESC}` → code`{ENTER}` → plant field-1 caret (Ctrl+N`1{ENTER}` or Ctrl+Home).
   Verify: no modal AND screen fingerprint == expected AND (heads-down number == 1 OR
   active-field highlight at field-1 rect). Fail → HALT.
2. **NAVIGATE** to field n (Enter×(n−1) or `[FJ:n]`) — the only place desync enters, so:
3. **PRE-TYPE GUARD:** no modal; fingerprint == expected; heads-down number == n. Fail →
   don't type → RESYNC.
4. **TYPE:** clear with `{END}{BACKSPACE 40}` (never Ctrl+A/C), type escaped literal,
   `commit:false` so the caret stays for read-back before Enter advances.
5. **READ-BACK:** OCR field n, normalize, compare. Match → VERIFIED → `{ENTER}`. Mismatch → RESYNC.
6. **RESYNC (bounded, MAX=2):** re-anchor (`{ESC}` + code) and re-drive to field n. Because
   read-back catches desync at the *first* wrong field and re-anchor is one Esc away,
   recovery is cheap and a miscount can never silently propagate.
7. **HALT:** screenshot + failing reason, `needsHuman:true`, STOP.

Policy: retry only *transient* guards (wait-for-modal-cleared, wait-for-screen-loaded);
**fail-fast HALT** on any value mismatch or validation modal (never retry a business error);
cap retries then escalate to human; screenshot on every HALT.

---

## 5. Concrete implementation steps (ordered, minimal)

1. **DPI upgrade** — `agent.py::_set_dpi_aware` → PMv2 with fallbacks. *(done)*
2. **Heads-down primitives + `headsdown` command** — `drake_driver.py` +
   `agent.py::cmd_headsdown` so the pixel-free path is one command to test. *(done)*
3. **Modal sentinel** — `drake_driver.py`: cache `main_hwnd`; `modal_present()`
   (GetForegroundWindow vs handle + capture dialog text); call before/after keystrokes.
4. **Make `enter_index` real** — driver reads the per-screen `enter_index` map (declared in
   JSON, not yet consumed) to drive `focus:false` + `field_commit` sequential entry from a
   planted anchor.
5. **Anchor-verify + per-field read-back in the flow** — `agent.py::cmd_selftest`: break on
   first failure, OCR each field after typing, add the pre-type guard.
6. **Upgrade OCR read-back** — `drake_driver.py::_ocr_read`: per-type whitelist, 4× upscale,
   Otsu/raw dual-pass, `image_to_data` confidence, capture-unfocused.
7. **PDF-text oracle** — new module: Calculate → Ctrl+P → Drake PDF → watch folder → parse
   with `pdfplumber` → reconcile per-document lines → HALT.
8. **Fill binding calibration** — per Drake build, per screen: `shoot --grid` +
   `headsdown` to record `field_no`, `enter_index`, `ocr_box`, screen fingerprint, one
   anchor `click_xy`. Regenerate each tax season (field numbers shift between builds).

Neither the keyboard path (`focus:false` + `field_commit`) nor the image-anchored path
(`click_input`) is a rewrite — both already exist. The gaps are **calibration data** and
**inter-step verification**, not mechanism.

---

## 6. Immediate next physical test on the VM

**Prove the pixel-free path plants and holds a caret — one command:**

```
git pull
python agent.py headsdown --binding binding.json --screen W2 --shot heads.png
```

That opens the W-2 screen by code, presses **Ctrl+N**, and screenshots. Then tell me:
1. Did a **number** appear on each field? (heads-down works)
2. Read me the number on: **employer EIN**, **employer name**, **box 1 wages**, **box 2 fed w/h**.

Then validate entry by those numbers (substitute the 4 numbers you read):

```
python agent.py headsdown --binding binding.json --screen W2 \
  --seq "1=12-3456789,2=TEST EMPLOYER,3=52000,4=6000" --shot heads_typed.png
```

**Pass = the four values land in the right boxes with NO click.** If Ctrl+N breaks focus,
we fall back to Ctrl+Home + Enter-order. This single run validates or kills the primary path
before we build the flow on it.

---

## 7. Open questions only the VM can resolve

1. Does **Ctrl+N** break the canvas caret on *this* build, or was our "toxic Ctrl chord"
   assumption wrong? (Research says it's Drake's own hotkey and almost certainly safe.)
2. Does opening a screen leave a usable caret, or must field 1 always be planted by
   Ctrl+N / Ctrl+Home / one click?
3. Are heads-down field numbers **OCR-readable** off a screenshot reliably enough to serve
   as the pre-type guard?
4. Is the W-2 `enter_index` order (0→1→2→3) stable, and does it hold across INT/DIV/NEC/
   1099/1098 (orders unmapped)?
5. Do `[FJ:N]` macro operators fire when driven through Drake's macro engine, and are field
   numbers stable per-build?
6. Does the Drake PDF land in `Drake Documents\<client>\<year>\` with a parseable text
   layer, and does the per-document worksheet preserve individual (un-summed) entries?
7. Is there a distinct, samplable active-field highlight RGB for a fast pre-OCR focus check?

*Sources: Drake KB (heads-down `12130.htm`, macros `12129.htm`), pywinauto DPI issue #915,
Microsoft DPI-awareness-context docs, Tesseract small-text tuning. Items 1, 5, 6 are where
the KB claims most need VM confirmation before we commit code.*
