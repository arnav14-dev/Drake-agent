"""Get Drake back to the Data Entry Menu when a session has left screens piled on it.

WHY THIS EXISTS. `drake_nav.open_screen` does not close the screen it was on — measurement
showed every menu link works from any state, so returning to the menu first was a step that
could fail without buying anything. True per run. Across a session it stacks: four
navigations leave four data-entry canvases on top of each other, and closing the top one
merely uncovers the next, which reads to a human as one dialog that keeps coming back.

GRID MODE IS THE PART THAT LOOKS UNFIXABLE. A grid-mode screen is not a dialog sitting on
top of a form — it IS the form, drawn as a spreadsheet. Cancelling it exits the screen and
uncovers the next instance, which is also in grid mode, which draws its own grid. Clicking
Cancel can never win: it walks the pile one at a time while Drake keeps the mode. F3 toggles
the MODE (drake_nav.GRID_MODE_TOGGLE_KEY) and ends it in one press.

That also matters beyond the cleanup. Heads-down field numbers belong to the FORM; in grid
mode they address nothing at all, so a screen left in grid mode is a screen the next write
run cannot use.

WHAT THIS WILL NOT DO. It does not kill the process, it does not press Enter blind, and it
does not answer a dialog it was not asked about. If an unexpected dialog appears — a "save
changes?" prompt is the realistic one — it STOPS and prints the dialog's text and its
buttons, because the two answers to that prompt are "keep the data" and "throw it away" and
nothing here knows which you want.

Esc on a Drake data-entry screen SAVES and closes it, so data already entered is kept.

    python recover.py            # report only: what is open, and whether anything blocks
    python recover.py --go       # leave grid mode, then Esc back to the Data Entry Menu

Nothing here types a value into a return, and this agent has no file/e-file command.
"""
from __future__ import annotations

import json
import re
import sys
import time

from drake_driver import DrakeDriver
from drake_nav import GRID_MODE_ID_PREFIX, GRID_MODE_TOGGLE_KEY

# A screen link, e.g. '1098|Mortgage Interest Statement'. Their presence is what identifies
# the Data Entry Menu — the window titles are identical for the menu and every form.
LINK_RE = re.compile(r"^[A-Z0-9]{1,6}\|")

MAX_STEPS = 12
BINDING = "binding.json"


def _canvases(d) -> list:
    """Visible data-entry windows, topmost first (Drake's own Z-order)."""
    return list(d.nav_data_entry_windows())


def _describe(d, hwnd) -> dict:
    try:
        ids = [str(e.get("automation_id") or "") for e in d.nav_elements(hwnd)]
        names = [str(e.get("name") or "").strip() for e in d.nav_elements(hwnd)]
    except Exception:
        ids, names = [], []
    return {"hwnd": hwnd,
            "menu": any(LINK_RE.match(n) for n in names),
            "grid": any(i.startswith(GRID_MODE_ID_PREFIX) for i in ids),
            "controls": len(ids)}


def _press(d, hwnd, key) -> None:
    win = d.app.window(handle=int(hwnd))
    win.set_focus()
    time.sleep(0.25)
    win.type_keys(key)
    time.sleep(0.7)


def _blocker(d):
    """A real modal in the way, or None. Reuses the driver's own classifier."""
    try:
        return d._detect_unexpected_dialog()
    except Exception:
        return None


def _buttons_of(hwnd) -> list:
    if not hwnd:
        return []
    try:
        from drake_driver import _enum_child_summaries
        return [c for c in _enum_child_summaries(int(hwnd), cap=32)
                if "button" in (c.get("class_name") or "").lower()
                and (c.get("text") or "").strip()]
    except Exception:
        return []


def report(d) -> list:
    state = [_describe(d, h) for h in _canvases(d)]
    print(f"\n{len(state)} data-entry window(s) open:")
    for s in state:
        what = "MENU" if s["menu"] else ("form (GRID MODE)" if s["grid"] else "form")
        print(f"   hwnd={s['hwnd']:<10} {what:<18} {s['controls']} controls")
    b = _blocker(d)
    print(f"blocking modal: {b['summary'] if b else 'none'}")
    return state


def leave_grid_mode(d) -> bool:
    """F3 every window that is drawn as a grid. True if the way is clear afterwards."""
    for step in range(1, MAX_STEPS + 1):
        gridded = [s for s in (_describe(d, h) for h in _canvases(d)) if s["grid"]]
        if not gridded:
            return True
        h = gridded[0]["hwnd"]
        print(f"[grid {step}] hwnd={h} is in grid mode — pressing {GRID_MODE_TOGGLE_KEY}")
        try:
            _press(d, h, "{F3}")
        except Exception as e:
            print(f"    failed: {type(e).__name__}: {e} — press F3 yourself on that window.")
            return False
        still = {s["hwnd"]: s["grid"] for s in (_describe(d, x) for x in _canvases(d))}
        if still.get(h) is True:
            print("    still grid mode. STOPPING rather than hammering it.")
            return False
        print("    out of grid mode." if h in still else "    window closed on the toggle.")
    return False


def unwind_to_menu(d) -> bool:
    """Esc each screen shut until only the Data Entry Menu is left."""
    for step in range(1, MAX_STEPS + 1):
        wins = _canvases(d)
        if not wins:
            print("\nno data-entry window left — the return is closed.")
            return True
        top = wins[0]
        if len(wins) == 1 and _describe(d, top)["menu"]:
            print(f"\nlanded on the Data Entry Menu (hwnd={top}).")
            return True

        b = _blocker(d)
        if b:
            print("\nSTOPPED — a dialog is up that this tool was not asked to answer:")
            print(f"   {b['summary']}")
            print(f"   text: {str(b.get('text') or '')[:300]}")
            print(f"   buttons: {[c.get('text') for c in _buttons_of(b.get('handle'))] or 'none readable'}")
            print("   Answer it yourself — one of its answers may discard data.")
            return False

        before = len(wins)
        print(f"[close {step}] Esc on hwnd={top}")
        try:
            _press(d, top, "{ESC}")
        except Exception as e:
            print(f"    failed: {type(e).__name__}: {e}")
            return False
        after = _canvases(d)
        if len(after) >= before and top in after:
            print(f"    did not close ({before} -> {len(after)}). STOPPING; nothing is forced.")
            return False
        print(f"    {before} -> {len(after)}")
    print(f"\nstopped after {MAX_STEPS} steps. Nothing was forced.")
    return False


def main() -> int:
    go = "--go" in sys.argv
    d = DrakeDriver(json.load(open(BINDING, encoding="utf-8-sig")))
    d.connect()
    report(d)
    if not go:
        print(f"\nREPORT ONLY — nothing was pressed. Re-run with --go to press "
              f"{GRID_MODE_TOGGLE_KEY} and Esc back to the menu.")
        return 0
    if not leave_grid_mode(d):
        return 1
    ok = unwind_to_menu(d)
    report(d)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
