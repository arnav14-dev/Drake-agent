"""Getting Drake to the right client and the right screen, before a single value is typed.

WHY THIS FILE EXISTS
--------------------
Until now a human opened the return and the W-2 screen, and the agent typed into whatever
was in front of it. That is safe in the narrow sense that a person chose the target, and
unsafe in the wide sense that nothing in the code ever checked. The moment the agent opens
returns by itself, "nothing ever checked" becomes the whole risk: every value this project
enters is verified, but a perfectly verified W-2 in the wrong person's return is worse than
no entry at all, and not one gate downstream would notice. Every read-back would pass.

So the rule here is narrower than "navigate": PROVE THE TARGET, OR REFUSE. This module
never guesses which client Drake landed on, never accepts a near-match on a name, and never
treats "I could not read the answer" as "the answer was yes".

WHERE THESE IDENTIFIERS CAME FROM
---------------------------------
Measured, not assumed. `agent.py explore` was run against live Drake 2025 at three states —
the home screen, the Open/Create a Return dialog, and the Data Entry Menu — and every
automation id below was read out of those dumps on 2026-08-07.

That distinction is the point of the module. `open_return()` and `open_screen()` already
existed in drake_driver.py, written from an assumption about how Drake probably works, and
never once confirmed against it. The last time this project shipped that kind of reasoning
— the caret re-arm, where Esc "would" reach the popup and a changed focus HWND "would"
prove focus moved — it failed live three times in a row. None of the constants here are
reasoned about. They were read off the screen.
"""

from __future__ import annotations

import re
from typing import Optional


# -- Drake 2025 automation ids, read from live explore dumps on 2026-08-07 -------------

# Home screen (window: 'Drake 2025 Tax Software')
OPEN_CREATE_BUTTON_ID = "MainWindow_ButtonFileOpen"
OPEN_CREATE_MENU_ID = "MainWindow_MenuItemFileOpenReturn"

# Open/Create dialog (window: 'Drake 2025 - Open / Create a Return')
CLIENT_DIALOG_TITLE_RE = r"Open\s*/\s*Create a Return"
SEARCH_BOX_ID = "ClearableWatermarkTextbox_TextBoxInput"
RESULTS_GRID_ID = "ClientSelectionUC_DataGridSearchResultsClients"
RESULT_ROW_ID_PREFIX = "ClientSelectionUC_DataGridSearchResultsClientsItem"
DIALOG_OK_ID = "FileOpenCreateWindow_ButtonOK"
DIALOG_CANCEL_ID = "FileOpenCreateWindow_ButtonCancel"
ROW_CELL_NAME_ID = "ClientName"
ROW_CELL_TYPE_ID = "ClientType"
ROW_CELL_MASKED_ID = "MaskedId"

# 'Drake 2025 - Open Return' — "<id> does not exist. Would you like to create a new return?"
CONFIRM_CREATE_TITLE_RE = r"Drake .*- Open Return\b"
CONFIRM_MSG_ID = "CustomMessageBoxWindow_TextBlockMessage"
CONFIRM_YES_ID = "CustomMessageBoxWindow_ButtonYes"
CONFIRM_NO_ID = "CustomMessageBoxWindow_ButtonNo"

# 'Drake 2025 - New Return' — return type + the name to file them under.
NEW_RETURN_TITLE_RE = r"Drake .*- New Return\b"
RETURN_TYPE_IDS = {
    "individual": "CreateReturnWindow_RadioButtonIndividual",   # 1040 — what a W-2 is
    "ccorp": "CreateReturnWindow_RadioButtonCcorp",
    "scorp": "CreateReturnWindow_RadioButtonScorp",
    "partnership": "CreateReturnWindow_RadioButtonPartnership",
    "fiduciary": "CreateReturnWindow_RadioButtonFiduciary",
    "taxexempt": "CreateReturnWindow_RadioButtonTaxExcempt",    # Drake's spelling
    "estate": "CreateReturnWindow_RadioButtonEstate",
}
NEW_FIRST_NAME_ID = "CreateReturnWindow_TextBoxFirstName"
NEW_MIDDLE_INITIAL_ID = "CreateReturnWindow_TextBoxMiddleInitial"
NEW_LAST_NAME_ID = "CreateReturnWindow_TextBoxLastName"
NEW_OK_ID = "UCButtonsControl_ButtonOk"
NEW_CANCEL_ID = "UCButtonsControl_ButtonCancel"

# Data Entry, both flavours (window: 'Drake 2025 - Data Entry (<id> - <name>) - ...')
# The separator between id and name is ' - ' (spaces on BOTH sides). The id itself may
# contain hyphens — '12-3456789' for an EIN, '123-45-6789' if Drake ever formats an SSN —
# so 'any hyphen ends the id' is wrong and silently fails to parse those titles.
DATA_ENTRY_TITLE_RE = r"Data\s*Entry\s*\(\s*(?P<id>[\d\s\-]+?)\s+-\s+(?P<name>[^)]*)\)"
MENU_SEARCH_ID = "MenuScreenWindow_TextBoxSearch"
MENU_TAB_ID = "menuTabControl"          # present ONLY on the Data Entry Menu
FORM_TAB_ID = "taxTabControl"           # present ONLY on a tax form screen
FORM_CANVAS_ID = "ucTaxForm"            # present ONLY on a tax form screen

# Screen links on the menu are Buttons labelled 'CODE|Description' — 'W2|Wages'.
SCREEN_LINK_ID_RE = r"^LINK_\d+_Col\d+_Sel\d+$"

# A label that appears on the screen ITSELF once it is open. This is the only trustworthy
# proof that clicking a screen link worked.
#
# Measured 2026-08-07: Drake keeps SEVERAL windows all titled 'Data Entry (...)' — an outer
# shell and an inner one — and UIA's descendants() crosses window boundaries, so every one
# of them reports the whole merged tree. All four structural markers (menuTabControl,
# MenuScreenWindow_TextBoxSearch, taxTabControl, ucTaxForm) are present AND visible in
# every window in every state, and so are all 37 screen-link buttons. Nothing about the
# window tells you which screen a human is actually looking at.
#
# That matters because heads-down field numbers are SCREEN-SPECIFIC: field 4 is the
# employer EIN on the W-2 screen and something else entirely on screen 1. Typing 78 values
# into the wrong screen is the failure this check exists to prevent, and it has to happen
# BEFORE the first keystroke — the read-backs afterwards would each pass, because Drake
# really did accept what it was given.
#
# A screen with no signature here is opened and reported as UNVERIFIED rather than
# claimed. Each entry was read off that screen's own printed heading in a live explore dump.
SCREEN_SIGNATURES = {
    "W2": r"Form\s+W-2\b",
    # Measured 2026-08-11 in explore-int-form-headsdown.json (Label_6). Deliberately the
    # WHOLE heading: 'Interest Income' on its own also appears in the Data Entry Menu's
    # screen-link list ('INT|1099-INT, Interest Income'), which is present in every window
    # in every state, so the short form would report the INT screen as open from the menu.
    "INT": r"Schedule\s+B\s*-\s*Interest\s+Income\s*\(1099-INT\)",
    # Measured 2026-08-12 in explore-div-form.json. Whole heading for the same reason as the
    # INT one: the Data Entry Menu's screen-link list carries 'DIV|1099-DIV, Dividend Income'
    # in every window in every state, so a short pattern would report this screen as open
    # from the menu. The two Schedule B screens also cross-link to each other ('Screen INT
    # for Interest' is printed on the DIV screen), which is a second way a loose pattern
    # would match the wrong one.
    "DIV": r"Schedule\s+B\s*-\s*Dividend\s+Income\s*\(1099-DIV\)",
    # Measured 2026-08-13 in explore-1099r-form.json. Anchored on "Form 1099-R - Pensions"
    # rather than on "1099-R" alone, which the screen also prints inside two of its own
    # checkbox captions ("1099-R for disability", "1099-R altered or handwritten") and which
    # the Data Entry Menu carries in its screen-link list in every window in every state.
    "1099": r"Form\s+1099-R\s*-\s*Pensions,\s*Annuities,\s*Retirement",
    # Measured 2026-08-15. Anchored on "Benefits Statement" for a sharper reason than usual:
    # the Data Entry Menu's link for this screen reads 'SSA|SSA-1099, Social Security', which
    # is a PREFIX of the heading. A pattern stopping at "Social Security" would match the
    # menu — present in every window in every state — and report the screen as open from it.
    "SSA": r"SSA-1099,\s*Social\s+Security\s+Benefits\s+Statement",
    # Measured 2026-08-15 in explore-1098-form.json (the screen's heading at y=113).
    # Anchored on "Form 1098 -" rather than on "1098" or "Mortgage Interest" alone, both of
    # which appear in menu link lists that are present in every window in every state:
    # 'DOCS|1098/1099 Source Document Guide' on the Miscellaneous tab carries the number,
    # and this screen's own menu link — '1098|Mortgage Interest Statement' — carries the
    # words. Neither carries the printed heading.
    "1098": r"Form\s+1098\s*-\s*Mortgage\s+Interest",
}

# Drake can draw some screens as a SPREADSHEET instead of a form — the INT screen prints
# "*Use <F3> to switch to grid mode*" on itself. The two modes share a heading and a window
# title, so the screen signature above cannot tell them apart, and the heads-down field
# numbers belong to the FORM: in grid mode they address nothing at all.
#
# Measured 2026-08-10/11 by dumping the same screen in both modes. The grid carries a
# 'ucTaxGrid…' automation id and the form carries none — the only structural difference
# between the two dumps that is not also present in both.
GRID_MODE_ID_PREFIX = "ucTaxGrid"
GRID_MODE_TOGGLE_KEY = "F3"


# ---------------------------------------------------------------------------------------
# Drake's RECORD CHOOSER.
#
# Opening a repeatable screen that already holds records does not always open the screen:
# Drake may put up 'Existing Forms List', a grid of every existing record plus a 'New Record'
# row, with Open and Cancel. Measured 2026-08-15/16 — it appeared for a W-2 screen holding
# two records and not for an INT screen holding five, so it is NOT simply "more than one
# record" and it is not only about which window the link was clicked from. Both of those
# were hypotheses this code no longer relies on: the chooser is handled wherever it appears.
#
# It was invisible for as long as documents went in one at a time. A batch meets it on its
# second document, and `open_screen` rightly refused — the screen's heading never appears
# while the chooser is up, and typing field numbers into a chooser would be worse than
# useless.
#
# It is also an OPPORTUNITY, and that is why this reads the grid rather than dismissing it.
# The chooser lists every existing record with its identifying columns; today's duplicate
# guard can only see the ONE record that happens to be open. Reading the list makes the
# duplicate check stronger, not weaker — which is the only basis on which a safety gate is
# allowed to change.
FORMS_LIST_TITLE = "Existing Forms List"
FORMS_LIST_OPEN_ID = "MultiInstanceSelectionWindow_ButtonOpen"
FORMS_LIST_CANCEL_ID = "MultiInstanceSelectionWindow_ButtonCancel"
# The row Drake pre-selects. Matched on the '#' cell, which reads 'New' for it.
FORMS_LIST_NEW_CELL = "NEW"


def forms_list_hwnd(driver):
    """The record chooser's window handle, or None."""
    from drake_driver import _enum_toplevel_windows
    try:
        pid = int(driver.pid or driver.win.element_info.process_id)
        for w in _enum_toplevel_windows(pid):
            if w.get("visible") and FORMS_LIST_TITLE.lower() in (w.get("title") or "").lower():
                return int(w["hwnd"])
    except Exception:
        pass
    return None


def read_forms_list(driver, hwnd) -> list:
    """Every row of the chooser as a list of cell strings, header first.

    Read off the DataGrid's own DataItem rows. Returned as raw cells rather than as named
    fields on purpose: the columns differ per screen (a W-2 chooser shows Employer Name and
    Wages, an INT chooser shows Name and Interest Income), and inventing a schema for each
    would be a second field map to keep in step with Drake.
    """
    rows = []
    try:
        win = driver.app.window(handle=int(hwnd))
        header = [e.window_text().strip()
                  for e in win.descendants(control_type="HeaderItem")
                  if (e.window_text() or "").strip()]
        if header:
            rows.append(header)
        for item in win.descendants(control_type="DataItem"):
            cells = []
            for c in item.descendants(control_type="Text"):
                try:
                    cells.append((c.window_text() or "").strip())
                except Exception:
                    cells.append("")
            if cells:
                rows.append(cells)
    except Exception:
        pass
    return rows


def forms_list_matches(rows: list, value: str) -> list:
    """Rows whose cells contain `value`, normalised. [] when it is new to this return.

    Compared with `_strip_name_noise`, the SAME normaliser the client identity check uses:
    the chooser prints what Drake STORED, which is not what we sent — it title-cases an
    employer name and drops punctuation — so an exact string comparison would report every
    real duplicate as new, which is the wrong direction for a duplicate check to fail in.
    """
    want = _strip_name_noise(value)
    if not want:
        return []
    out = []
    for row in rows[1:] if rows else []:
        if any(_strip_name_noise(c) == want for c in row):
            out.append(row)
    return out


def resolve_forms_list(driver, hwnd, *, take_new: bool, log=print) -> dict:
    """Answer the chooser: take the New Record row, or cancel out of it.

    Never 'whatever row is selected'. Every row here is a record in a live return, and Open
    on the wrong one puts this document's values on top of somebody else's 1099.
    """
    import time
    try:
        win = driver.app.window(handle=int(hwnd))
    except Exception as e:
        return {"ok": False, "reason": f"the record chooser could not be read: {e}"}

    if not take_new:
        try:
            win.child_window(auto_id=FORMS_LIST_CANCEL_ID, control_type="Button").wrapper_object().invoke()
            return {"ok": True, "action": "cancelled"}
        except Exception as e:
            return {"ok": False, "reason": f"could not cancel the record chooser: {e}"}

    # Select the New Record row explicitly rather than trusting Drake's pre-selection: the
    # pre-selected row is what a PERSON would see, and this code does not see it.
    #
    # `descendants()` hands back objects that are ALREADY WRAPPED. Calling .wrapper_object()
    # on one raises AttributeError, and when that was swallowed by a bare `except` the row
    # was never selected, Open was never pressed, and the run reported that Drake's list
    # "was answered" when nothing had been touched. Both shapes are accepted here so a
    # pywinauto version cannot reintroduce it, and NOTHING is swallowed: a row that will not
    # select is a refusal with the reason attached, never a silent pass.
    picked, why = False, []
    try:
        rows = win.descendants(control_type="DataItem")
    except Exception as e:
        return {"ok": False, "reason": f"the record chooser's list could not be read: {e}"}

    for item in rows:
        try:
            texts = [(c.window_text() or "").strip().upper()
                     for c in item.descendants(control_type="Text")]
        except Exception:
            continue
        if FORMS_LIST_NEW_CELL not in texts:
            continue
        target = item.wrapper_object() if hasattr(item, "wrapper_object") else item
        for how in ("select", "click_input"):
            try:
                getattr(target, how)()
                picked = True
                break
            except Exception as e:
                why.append(f"{how}: {type(e).__name__}")
        break

    if not picked:
        return {"ok": False,
                "reason": ("the record chooser's 'New Record' row could not be selected"
                           + (f" ({'; '.join(why)})" if why else
                              " — no row on it is marked 'New'")
                           + ". Nothing was opened: the alternative is picking an existing "
                             "record, which would overwrite somebody's document.")}
    time.sleep(0.25)
    try:
        btn = win.child_window(auto_id=FORMS_LIST_OPEN_ID, control_type="Button")
        btn = btn.wrapper_object() if hasattr(btn, "wrapper_object") else btn
        btn.invoke()
    except Exception as e:
        return {"ok": False, "reason": f"could not press Open on the record chooser: {e}"}
    log("  (Drake asked which record — chose a NEW one)")
    time.sleep(0.6)
    return {"ok": True, "action": "new-record"}



# -- identity --------------------------------------------------------------------------

def normalize_id(value) -> str:
    """An SSN/EIN reduced to its digits. '123-45-6789', '123456789' and ' 123 45 6789 '
    are the same taxpayer; Drake writes it differently in three different places."""
    return re.sub(r"\D", "", str(value if value is not None else ""))


def row_client_id(automation_id) -> str:
    """The taxpayer id embedded in a search-result row's automation id.

    Drake names each row 'ClientSelectionUC_DataGridSearchResultsClientsItem123456789-8'
    — the FULL, unmasked id followed by '-<row index>'. This is the only place the whole
    id is legible: the visible cell is masked to 'XXXXX6789', and matching on four digits
    would be an invitation to open the wrong return.

    Returns '' when the id does not have that shape, so an unrecognised row can never be
    mistaken for a match.
    """
    s = str(automation_id if automation_id is not None else "")
    if not s.startswith(RESULT_ROW_ID_PREFIX):
        return ""
    tail = s[len(RESULT_ROW_ID_PREFIX):]
    m = re.fullmatch(r"(\d+)-(\d+)", tail)
    return m.group(1) if m else ""


def parse_data_entry_title(title) -> Optional[dict]:
    """{'id', 'name'} out of 'Drake 2025 - Data Entry (123456789 - fynn, Test) - (...)'.

    None when the title is not a data-entry title at all. This is the authoritative
    identity check: it is Drake's own statement of whose return is open, not our record of
    which one we asked for, so it catches the case where the click landed somewhere else.
    """
    m = re.search(DATA_ENTRY_TITLE_RE, str(title if title is not None else ""))
    if not m:
        return None
    return {"id": normalize_id(m.group("id")), "name": (m.group("name") or "").strip()}


def split_drake_name(display) -> dict:
    """Drake's 'Last, First & Spouse' into {'last', 'given'}.

    'fynn, Test'                  -> last 'FYNN',     given ['TEST']
    'BLOGGER, MEDIA & NICHE'      -> last 'BLOGGER',  given ['MEDIA', 'NICHE']
    'WATERSON, MINERAL'           -> last 'WATERSON', given ['MINERAL']

    A joint return puts both given names after the comma, which is why `given` is a list:
    a W-2 belonging to either spouse is legitimately in this return, and demanding the
    first given name would refuse the spouse's W-2 for no reason.
    """
    s = re.sub(r"\s+", " ", str(display if display is not None else "")).strip().upper()
    if not s:
        return {"last": "", "given": []}
    if "," in s:
        last, _, rest = s.partition(",")
    else:
        # No comma: treat the last token as the surname, the way the backend's
        # splitEmployeeName does, so both sides of the handoff guess identically.
        parts = s.split(" ")
        last, rest = (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (s, "")
    given = [g.strip() for g in re.split(r"[&+/]| AND ", rest) if g.strip()]
    return {"last": _strip_name_noise(last), "given": [_strip_name_noise(g) for g in given]}


def _strip_name_noise(s: str) -> str:
    """Punctuation and suffixes that differ between a W-2 and a Drake client record and
    mean nothing: 'O'BRIEN' vs 'OBRIEN', 'SMITH JR' vs 'SMITH'."""
    s = re.sub(r"[.,'`\-]", "", str(s or "").upper()).strip()
    s = re.sub(r"\s+(JR|SR|II|III|IV)$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def names_match(drake_display, first_name, last_name) -> dict:
    """Is the client Drake is showing the person on this W-2? {'ok', 'why'}.

    Deliberately strict on the surname and forgiving on the given name. The surname is the
    part Drake indexes and the part a preparer reads; a mismatch there means a different
    family, and there is no benign reason for it. Given names vary legitimately — 'Bob' for
    'Robert', a middle name present on one side only, a spouse's W-2 in a joint return — so
    a given name matches if either string contains the other as a whole word.

    An UNREADABLE name is not a match. If either side is missing this returns ok=False with
    a reason, and the caller refuses; the alternative is treating "no evidence" as proof,
    which is the exact bug that made four Box 20 fields report success into an empty box.
    """
    want_last, want_first = _strip_name_noise(last_name), _strip_name_noise(first_name)
    if not want_last and not want_first:
        return {"ok": False, "why": "the payload carries no employee name to check against"}
    got = split_drake_name(drake_display)
    if not got["last"] and not got["given"]:
        return {"ok": False, "why": f"Drake's client name was unreadable ({drake_display!r})"}

    if want_last and got["last"] and want_last != got["last"]:
        return {"ok": False,
                "why": f"surname mismatch: the W-2 says {want_last!r}, "
                       f"Drake's client is {got['last']!r}"}
    if want_first and got["given"]:
        if not any(_given_matches(want_first, g) for g in got["given"]):
            return {"ok": False,
                    "why": f"given name mismatch: the W-2 says {want_first!r}, "
                           f"Drake's client is {' & '.join(got['given'])!r}"}
    return {"ok": True, "why": f"{got['last']}, {' & '.join(got['given'])}"}


def _given_matches(want: str, got: str) -> bool:
    """Whole-word containment either way, so 'ROBERT' matches 'ROBERT JAMES' and
    'TEST' matches 'TEST'. Substring matching is NOT enough — it would let 'ANN' match
    'DEANNA', which is a different person."""
    if want == got:
        return True
    wt, gt = want.split(), got.split()
    return all(w in gt for w in wt) or all(g in wt for g in gt)


# -- decisions -------------------------------------------------------------------------

def choose_client_row(rows: list, wanted_id: str) -> dict:
    """Which search-result row is this taxpayer? {'ok', 'row', 'reason', 'candidates'}.

    Matches on the FULL id from the row's automation id and nothing else. Not the masked
    cell, not the name, not "the only row showing". Two rows claiming the same id, or none,
    both refuse — Drake would happily open whichever was selected, and a wrong client here
    is not recoverable by anything later in the run.
    """
    want = normalize_id(wanted_id)
    if not want:
        return {"ok": False, "reason": "no SSN/EIN to look up", "candidates": []}
    hits = [r for r in rows if row_client_id(r.get("automation_id")) == want]
    if len(hits) == 1:
        return {"ok": True, "row": hits[0], "reason": ""}
    seen = [{"id": row_client_id(r.get("automation_id")), "name": r.get("name", "")}
            for r in rows if row_client_id(r.get("automation_id"))]
    if not hits:
        return {"ok": False, "reason": f"no client in Drake has id {want}",
                "candidates": seen, "not_found": True}
    return {"ok": False, "reason": f"{len(hits)} clients share id {want} — refusing to guess",
            "candidates": seen}


def collect_client_rows(elements: list) -> list:
    """Search-result rows paired with the client name Drake displays on each.

    The id and the name are SEPARATE elements. The row carries the full id in its
    automation id; the visible name is a 'ClientName' Text that sits inside the row's
    rectangle but is NOT its child in the UIA tree — Drake's grid is virtualised WPF and
    the cells hang off a presenter, not the row. Geometry is the only thing that relates
    them, so they are joined by geometry: the name whose vertical centre falls inside the
    row's band.

    A row whose name cannot be located keeps name='' rather than borrowing a neighbour's.
    The caller then refuses on an unreadable name, which is the correct outcome — the
    alternative is checking the wrong client's name and passing.
    """
    rows, names = [], []
    for e in elements or ():
        rect = e.get("rect")
        if not rect:
            continue
        if row_client_id(e.get("automation_id")):
            rows.append(e)
        elif e.get("automation_id") == ROW_CELL_NAME_ID and (e.get("name") or "").strip():
            names.append(e)
    out = []
    for r in rows:
        top, bottom = int(r["rect"][1]), int(r["rect"][3])
        hit = ""
        for n in names:
            mid = (int(n["rect"][1]) + int(n["rect"][3])) // 2
            if top <= mid <= bottom:
                hit = (n.get("name") or "").strip()
                break
        out.append({"automation_id": e_id(r), "name": hit, "rect": r["rect"], "_el": r.get("_el")})
    return out


def e_id(element) -> str:
    return str((element or {}).get("automation_id") or "")


def classify_data_entry_window(element_ids) -> str:
    """'menu' | 'form' | 'unknown' for a Data Entry window, by what it CONTAINS.

    Both windows are titled 'Data Entry (...)', so the title cannot tell them apart — and
    the difference matters. `canvas_windows()` accepts either, and `_focus_canvas_field()`
    picks the topmost Edit inside; on the menu the only Edit is the screen-search box at
    the bottom, so a run that started on the menu would arm its caret in a search field and
    type field numbers into it. Structure answers what the title cannot.
    """
    ids = set(element_ids or ())
    is_form = bool({FORM_TAB_ID, FORM_CANVAS_ID} & ids)
    is_menu = bool({MENU_TAB_ID, MENU_SEARCH_ID} & ids)
    if is_form and not is_menu:
        return "form"
    if is_menu and not is_form:
        return "menu"
    return "unknown"


def parse_screen_link(label) -> Optional[dict]:
    """'W2|Wages' -> {'code': 'W2', 'title': 'Wages'}. None if it is not a screen link.

    Drake packs the screen code and its description into one label with a pipe, which is
    what makes an exact code match possible: 'W2' is found as a code, never as a prefix of
    'W2G|Gambling Income'. Matching on the visible text alone would confuse those two.
    """
    s = str(label if label is not None else "")
    if "|" not in s:
        return None
    code, _, title = s.partition("|")
    code, title = code.strip().upper(), title.strip()
    if not code:
        return None
    return {"code": code, "title": title}


def choose_screen_link(links: list, code: str) -> dict:
    """The menu button that opens screen `code`. {'ok', 'link', 'reason', 'candidates'}.

    Exact code equality. 'W2' must never open 'W2G|Gambling Income', and a substring or
    startswith rule does exactly that — the two sit nine pixels apart on the General tab.
    """
    want = str(code or "").strip().upper()
    if not want:
        return {"ok": False, "reason": "no screen code given", "candidates": []}
    parsed = [(lk, parse_screen_link(lk.get("name"))) for lk in links]
    hits = [lk for lk, p in parsed if p and p["code"] == want]
    if len(hits) == 1:
        return {"ok": True, "link": hits[0], "reason": ""}
    seen = sorted({p["code"] for _lk, p in parsed if p})
    if not hits:
        return {"ok": False, "reason": f"no screen with code {want} on this menu",
                "candidates": seen}
    return {"ok": False, "reason": f"{len(hits)} buttons claim code {want} — refusing to guess",
            "candidates": seen}


RECORD_POSITION_RE = r"Record\s+(?P<index>\d+)\s+of\s+(?P<count>\d+)"


def parse_record_position(text) -> Optional[dict]:
    """'Record 1 of 1' -> {'index': 1, 'count': 1}. None when it does not say that.

    Drake keeps this in a status label with automation id 'txtInstance'. It is how a run
    can say WHICH W-2 of several it wrote, which is the difference between a report a
    preparer can check and one they have to take on faith.
    """
    m = re.search(RECORD_POSITION_RE, str(text if text is not None else ""))
    if not m:
        return None
    return {"index": int(m.group("index")), "count": int(m.group("count"))}


def _looks_numeric(value) -> bool:
    """Is this value a number Drake would have taken from a W-2 — an EIN, a ZIP, an
    amount? Four digits or more, and nothing but digits once formatting is stripped.

    Four because every identifying number on a W-2 has at least that many, while the free
    text that shares the screen ('Van Nuys', 'CA', a street) has none. A ZIP passes and is
    a deliberate false positive: it errs towards calling a record REAL, which is the
    direction that never loses data.
    """
    s = re.sub(r"[,\.\-\s$]", "", str(value if value is not None else ""))
    return s.isdigit() and len(s) >= 4


def record_kind(values: list) -> str:
    """'blank' | 'fragment' | 'w2' — is there actually a W-2 on this record?

    The distinction the first live navigate-and-fill run needed and did not have. A record
    holding City 'Van Nuys' and State 'CA' and nothing else was classified as "an existing
    W-2 for a different employer", so the run pressed Page Down — and Drake refused to
    leave an incomplete screen, put up its e-file completeness warning, and the run halted
    having done nothing.

    It was the classification that was wrong, not the halt. Those two values are not a W-2;
    they are what somebody left behind. Paging past them would have been the worse outcome
    anyway — a second W-2 record created, and an incomplete first one still sitting in the
    return for the preparer to trip over at filing time.

    A record is a real W-2 only if it carries a NUMBER — an employer EIN, or an amount.
    Free text alone cannot identify an employer and cannot be anybody's wages.
    """
    vals = [v.get("value") for v in (values or ())]
    if not vals:
        return "blank"
    return "w2" if any(_looks_numeric(v) for v in vals) else "fragment"


def plan_record_use(state: dict, employer_ein=None, *, allow_new: bool = True,
                    noun: str = "W-2", id_noun: str = "employer EIN",
                    amount_noun: str = "wages") -> dict:
    """Use this record, open a fresh one, or refuse? {'action', 'reason'}.

    action is 'use' | 'new' | 'refuse'.

    The three nouns are what makes this reusable across forms without a second copy of the
    logic. A W-2 record is one EMPLOYER identified by an EIN; a 1099-INT record is one
    PAYER identified by a TIN. The decision is identical; only the words a preparer reads
    change, and a message that says "W-2" on the 1099 screen is a message they will not
    trust.

    A W-2 screen is one employer. Three situations, three answers:

      empty record            -> 'use'. Nothing to lose.
      occupied, SAME employer -> 'refuse'. Almost certainly this W-2 already went in, and
                                 a duplicate W-2 doubles the client's reported wages. That
                                 is a bigger error than any this project has made, it does
                                 not look like a bug on the form, and no read-back would
                                 ever catch it — every value would verify perfectly.
      occupied, other employer-> 'new'. A second job is normal; Page Down is exactly what
                                 Drake's own status bar tells a preparer to press.

    An UNREADABLE form refuses. Treating "I could not look" as "it is empty" is how a
    verified run overwrites an existing W-2.
    """
    if not state or not state.get("ok"):
        return {"action": "refuse",
                "reason": f"could not read what is already on the screen "
                          f"({(state or {}).get('reason') or 'no state'}) — refusing rather "
                          f"than typing over something unseen"}
    where = (f"record {state.get('index')} of {state.get('count')}"
             if state.get("index") else "the open record")
    if state.get("populated", 0) == 0:
        return {"action": "use", "reason": f"{where} is blank"}

    kind = record_kind(state.get("values"))
    if kind == "fragment":
        # Stray text with no employer and no money. Filling it in is both safer and
        # tidier than paging past it: the incoming W-2 writes these same boxes anyway,
        # and leaving an incomplete W-2 record behind is a filing problem of its own.
        who = id_noun.rsplit(" ", 1)[0] if " " in id_noun else id_noun
        return {"action": "use",
                "reason": f"{where} holds {state['populated']} stray value(s) but no "
                          f"{who} and no amounts — not a {noun}, so it is being filled in "
                          f"rather than left behind"}

    want = normalize_id(employer_ein)
    if want:
        for v in state.get("values") or ():
            if normalize_id(v.get("value")) == want:
                return {"action": "refuse",
                        "reason": f"{where} already holds {id_noun} {want} — this {noun} "
                                  f"looks like it has already been entered, and entering it "
                                  f"again would double the client's {amount_noun}. Nothing "
                                  f"was typed."}
    if not allow_new:
        return {"action": "refuse",
                "reason": f"{where} already has {state['populated']} value(s) on it and "
                          f"opening a new record is disabled"}
    return {"action": "new",
            "reason": f"{where} already has {state['populated']} value(s) for a different "
                      f"{id_noun.rsplit(' ', 1)[0] if ' ' in id_noun else id_noun} — "
                      f"opening a new {noun} record"}


def name_collision(rows: list, first_name, last_name) -> list:
    """Clients already in Drake who carry THIS NAME. The guard on auto-create.

    Auto-creating is safe exactly as long as "no client has this SSN" means "this person is
    new". It stops being safe when the SSN is wrong: one misread digit and the agent files
    a W-2 under a brand-new empty return while the real client's return sits untouched —
    and every check downstream passes, because the values really did land where the agent
    put them. It is the one failure mode of this feature that looks like success.

    A name already on the books is the cheap tell. Nobody has to be asked anything: if
    Drake already has a 'fynn' and the SSN we were given belongs to nobody, the far more
    likely story is a bad digit than a second unrelated fynn arriving the same day. So the
    run refuses and names the client it found, which is the loud failure the wrong-SSN
    case deserves.
    """
    hits = []
    for r in rows or ():
        if names_match(r.get("name"), first_name, last_name)["ok"]:
            hits.append(r)
    return hits


def _fail(step, reason, **extra) -> dict:
    return {"ok": False, "step": step, "reason": reason, **extra}


def open_client(driver, client_id, *, first_name=None, last_name=None, create: bool = False,
                timeout: float = 10.0, log=print) -> dict:
    """Get Drake to this taxpayer's return. {'ok', 'step', 'reason', 'title', 'reused'}.

    Refuses rather than improvises at every step. It will not pick a row it is not certain
    of, will not proceed if the dialog it clicked OK on is still on screen, and will not
    hand back control until Drake's own title says the right return is open.

    A DIFFERENT return already being open is refused, not closed. Closing one is a
    mutation whose failure modes have not been measured here — Drake may prompt to save,
    and a prompt nobody answers is a wedged run. Refusing costs a person one keystroke;
    guessing costs a wrong return.
    """
    want = normalize_id(client_id)
    if not want:
        return _fail("identity", "the payload carries no employee SSN, so there is no "
                                 "return to open")

    # Already there? The common case for back-to-back payloads for one client, and it
    # skips the whole dialog.
    cur = driver.nav_data_entry_window()
    if cur["kind"] != "none":
        v = verify_open_return(cur["title"], want, first_name, last_name)
        if v["ok"]:
            log(f"  return already open: {v['reason']}")
            return {"ok": True, "step": "reused", "reason": v["reason"],
                    "title": cur["title"], "kind": cur["kind"], "reused": True}
        return _fail("wrong-return-open",
                     f"a different return is already open in Drake — {v['reason']} Close it "
                     f"in Drake and re-send; this agent will not close a return it did not "
                     f"open.", title=cur["title"])

    home = driver.nav_find_window(r"Drake .*Tax Software", timeout=2.0)
    if not home:
        return _fail("home", "could not find Drake's main window")
    els = driver.nav_elements(home)
    btn = next((e for e in els if e.get("automation_id") == OPEN_CREATE_BUTTON_ID), None)
    if btn is None:
        return _fail("home", f"Drake's home screen has no {OPEN_CREATE_BUTTON_ID} button — "
                             f"this build's layout is not the one this was measured against")
    act = driver.nav_act(btn)
    if not act["ok"]:
        return _fail("home", f"could not press Open/Create: {act['error']}")

    dlg = driver.nav_find_window(CLIENT_DIALOG_TITLE_RE, timeout=timeout)
    if not dlg:
        return _fail("dialog", "the Open/Create a Return dialog never appeared")

    dels = driver.nav_elements(dlg)
    box = next((e for e in dels if e.get("automation_id") == SEARCH_BOX_ID), None)
    if box is None:
        return _fail("dialog", f"the dialog has no {SEARCH_BOX_ID} search box")
    typed = driver.nav_type_into(box, want)
    if not typed["ok"]:
        return _fail("dialog", f"could not type the SSN: {typed['error']}")

    # Drake filters the grid as the characters arrive; re-read AFTER typing or the rows
    # are the previous search's.
    import time
    want_name = bool(str(first_name or "").strip() or str(last_name or "").strip())
    chosen, rows = None, []
    # "NOT FOUND" IS A CLAIM ABOUT DRAKE, AND ONE READ IS NOT EVIDENCE. Drake populates
    # the grid a beat AFTER the search box fills — read in that beat and an existing
    # client looks exactly like a missing one. This loop used to break on the FIRST
    # not-found, so the whole retry existed only for the happy path: a client who was
    # plainly in Drake was reported as absent, with the agent's own halt screenshot
    # showing the row that had painted moments later (live, 2026-08-23; the report's
    # empty candidates list is the fingerprint — the grid was empty, not unmatched).
    # A miss only counts once the grid has answered the same way, consecutively, for
    # long enough that "still painting" is no longer a plausible reading. The deadline
    # still bounds the whole search, so a truly absent client costs ~1.5s more, once.
    NOT_FOUND_STABLE_READS = 8  # ~1.5s+ of consecutive agreement at 0.15s per lap
    misses_in_a_row = 0
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        rows = collect_client_rows(driver.nav_elements(dlg))
        chosen = choose_client_row(rows, want)
        if chosen["ok"]:
            # The row and its NAME are separate elements, and Drake fills the grid in two
            # passes: the row (carrying the id) exists before the name cell has painted.
            # Reading in that gap gives a matching id with an empty name, which the
            # identity check then — correctly — refuses as unreadable. Waiting for the
            # name is the fix; loosening the check would not be.
            if not want_name or (chosen["row"].get("name") or "").strip():
                break
            misses_in_a_row = 0
        elif chosen.get("not_found"):
            misses_in_a_row += 1
            if misses_in_a_row >= NOT_FOUND_STABLE_READS:
                break
        else:
            # Ambiguity (duplicate ids) is decided at the deadline, not instantly —
            # half-painted grids can transiently double a row.
            misses_in_a_row = 0
        time.sleep(0.15)
    if chosen is None:
        return _fail("search", "the client list never became readable")

    if not chosen["ok"] and chosen.get("not_found") and create:
        # Before creating: is somebody with this NAME already on the books? If so the SSN
        # is far more likely to be misread than the person to be new, and creating would
        # bury a real client's W-2 in a brand-new empty return while reporting success.
        surname = str(last_name or first_name or "").strip()
        if surname:
            typed = driver.nav_type_into(box, surname)
            if typed["ok"]:
                deadline = time.time() + min(4.0, float(timeout))
                by_name: list = []
                while time.time() < deadline:
                    by_name = collect_client_rows(driver.nav_elements(dlg))
                    if by_name:
                        break
                    time.sleep(0.15)
                clash = name_collision(by_name, first_name, last_name)
                if clash:
                    who = ", ".join(f"{c['name']} ({row_client_id(c['automation_id'])})"
                                    for c in clash[:4])
                    return _fail("identity",
                                 f"no client has id {want}, but Drake already has "
                                 f"{len(clash)} client(s) with this name — {who}. That is "
                                 f"more likely a misread SSN than a new person, so nothing "
                                 f"was created. Check the SSN on the document.",
                                 candidates=chosen.get("candidates"), dialog_hwnd=dlg)
            # Put the id back so Drake's create prompt is about the right taxpayer.
            driver.nav_type_into(box, want)
            time.sleep(0.4)

        ok_btn = next((e for e in dels if e.get("automation_id") == DIALOG_OK_ID), None)
        if ok_btn is None:
            return _fail("search", f"the dialog has no {DIALOG_OK_ID} button")
        act = driver.nav_act(ok_btn)
        if not act["ok"]:
            return _fail("search", f"could not press OK to reach the create prompt: "
                                   f"{act['error']}")
        return create_client(driver, want, first_name, last_name, timeout=timeout, log=log)

    if not chosen["ok"]:
        return _fail("search", chosen["reason"], candidates=chosen.get("candidates"),
                     not_found=bool(chosen.get("not_found")), dialog_hwnd=dlg)

    row = chosen["row"]
    if first_name or last_name:
        nm = names_match(row.get("name"), first_name, last_name)
        if not nm["ok"]:
            return _fail("identity",
                         f"SSN {want} belongs to a different person in Drake — {nm['why']}. "
                         f"Nothing was opened.", dialog_hwnd=dlg)
    log(f"  matched client: {row.get('name')!r} (id {want})")

    sel = driver.nav_act(row, want="select")
    if not sel["ok"]:
        return _fail("select", f"could not select the client row: {sel['error']}")
    ok_btn = next((e for e in dels if e.get("automation_id") == DIALOG_OK_ID), None)
    if ok_btn is None:
        return _fail("select", f"the dialog has no {DIALOG_OK_ID} button")
    act = driver.nav_act(ok_btn)
    if not act["ok"]:
        return _fail("select", f"could not press OK: {act['error']}")

    # The dialog still being up means OK did not take — and every keystroke after this
    # would land in it.
    if not driver.nav_wait_gone(dlg, timeout=timeout):
        return _fail("select", "OK was pressed but the Open/Create dialog is still on "
                               "screen; nothing was opened")

    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        cur = driver.nav_data_entry_window()
        if cur["kind"] != "none":
            break
        time.sleep(0.15)
    v = verify_open_return(cur["title"], want, first_name, last_name)
    if not v["ok"]:
        return _fail("verify", v["reason"], title=cur["title"])
    log(f"  opened: {v['reason']}")
    return {"ok": True, "step": "opened", "reason": v["reason"], "title": cur["title"],
            "kind": cur["kind"], "reused": False}


def screen_is_grid(element_ids) -> bool:
    """Is this screen being drawn as Drake's SPREADSHEET grid rather than as the form?

    Structural, not textual: the grid builds controls under a 'ucTaxGrid…' automation id
    and the form builds none. The printed heading is identical in both modes, so the screen
    signature cannot answer this and something has to.

    It matters because every field number in a form map belongs to the FORM. Starting a
    heads-down run against the grid would send 61 field numbers into a screen where those
    numbers mean nothing — and the popup would echo each value back, so the per-field gate
    would pass all the way down.
    """
    pre = GRID_MODE_ID_PREFIX.lower()
    return any(str(i or "").lower().startswith(pre) for i in (element_ids or ()))


def screen_is_showing(labels, code) -> Optional[bool]:
    """Is screen `code` the one on display? True / False / None when unmeasurable.

    Answered from the screen's own printed heading, because nothing structural can answer
    it — see SCREEN_SIGNATURES. None means this screen has no measured signature and the
    caller must say so rather than claim success.
    """
    pat = SCREEN_SIGNATURES.get(str(code or "").strip().upper())
    if not pat:
        return None
    blob = " ".join(str(l or "") for l in (labels or ()))
    return bool(re.search(pat, blob, re.I))


def open_screen(driver, code, *, timeout: float = 10.0, log=print) -> dict:
    """Open a data-entry screen by its Drake code. {'ok', 'step', 'reason', 'title'}.

    Assumes a return is already open and verified — this does not re-check identity,
    because `open_client` proves it and two copies of that check would drift apart.

    The screen link is clicked wherever it is found. It does NOT first return to the Data
    Entry Menu: measurement showed all 37 links are present and enabled from every state,
    so 'get back to the menu first' was a step that could fail without buying anything.
    What replaces it is a check that the screen actually opened — read off the screen's own
    heading, before any value is typed.

    THE 37 IS ONE TAB, NOT THE MENU. The sentence above was measured on the tab Drake opens
    on — General — and it is true there. The menu has TEN tabs and draws one at a time, so
    those 37 links are 37 of 312, and the other 275 are not hidden, they are absent from the
    tree entirely. Screen 1098 lives on 'Other Forms' and was unreachable: this function
    reported "no screen with code 1098 on this menu" and listed whatever tab happened to be
    up. Correct refusal, wrong conclusion available to the reader — the screen exists.
    So when the code is not on the current tab, the tabs are selected in turn until it is.
    """
    import time
    want = str(code or "").strip().upper()
    cur = driver.nav_data_entry_window()
    if cur["kind"] == "none":
        return _fail("screen", "no return is open, so there is no screen to open")

    # BACK TO THE MENU FIRST, when a form screen is open.
    #
    # This function used to click the link from wherever it was, on the measurement that all
    # the links are present and enabled from every state. They are — but what they DO is not
    # the same from every state, and that is what the measurement missed. Measured 2026-08-15
    # on a return holding several records per screen:
    #
    #     clicked from the Data Entry Menu -> the screen opens
    #     clicked from a form screen       -> Drake opens 'Existing Forms List', a chooser
    #                                         listing every existing record plus 'New Record'
    #
    # The chooser is not a screen, so the heading never appears and this function rightly
    # refused. Which was invisible for a year of single-document runs, because every one of
    # those starts from the menu — and fatal to a batch, where every document after the first
    # is opened from the form the previous one just finished.
    #
    # Escape is the way back, and on a Drake data-entry screen it SAVES and closes: the values
    # the previous document just entered are kept. Deliberately not answered by learning to
    # drive the chooser — every row on it is a record in a live return, and picking the wrong
    # one silently overwrites somebody's 1099 instead of adding one.
    # Escape is sent TO THE CANVAS WINDOW, not through the global keyboard. `driver.press`
    # injects into whatever holds focus, and after a run that is the heads-down popup or the
    # main frame — the form never sees the key and this loop reported "still showing a form"
    # about a screen that had simply not been asked to close.
    if cur["kind"] == "form":
        for _ in range(3):
            try:
                win = driver.app.window(handle=int(cur["hwnd"]))
                win.set_focus()
                time.sleep(0.2)
                win.type_keys("{ESC}")
            except Exception:
                driver.press(["Esc"])
            time.sleep(0.55)
            cur = driver.nav_data_entry_window()
            if cur["kind"] == "menu":
                break
        if cur["kind"] != "menu":
            return _fail("screen",
                         f"could not get back to the Data Entry Menu before opening screen "
                         f"{want} — Drake is still showing {cur['kind']!r}. Clicking a screen "
                         f"link from a form makes Drake open its record chooser instead of "
                         f"the screen, so nothing was clicked and nothing was typed.")
        log("  (closed the previous screen — a screen link opens a record chooser from a form)")

    def _links():
        return [e for e in driver.nav_all_elements()
                if re.match(SCREEN_LINK_ID_RE, e_id(e)) and (e.get("name") or "")]

    def _labels():
        return [e.get("name") for e in driver.nav_all_elements()
                if e.get("control_type") == "Text"]

    def _tabs():
        return [e for e in driver.nav_all_elements()
                if e.get("control_type") == "TabItem" and (e.get("name") or "").strip()]

    # A return opened straight from a CREATE has no Data Entry Menu window yet, so there
    # are no screen links anywhere to click — they do not exist rather than being hidden.
    # Escape backs out of the open screen and makes Drake build the menu. Only done when
    # the links are genuinely absent, because on every other path they are already
    # reachable and pressing Escape would be a step that can fail for nothing.
    links = _links()
    if not links:
        for _ in range(2):
            log("  no screen menu yet (new return) — backing out to build it")
            driver.press(["Esc"])
            deadline = time.time() + min(5.0, float(timeout))
            while time.time() < deadline:
                links = _links()
                if links:
                    break
                time.sleep(0.2)
            if links:
                break
            blocker = driver._detect_unexpected_dialog()
            if blocker:
                said = " ".join(str(blocker.get("text") or "").split())[:240]
                return _fail("screen",
                             f"Drake is asking a question instead of showing the screen "
                             f"menu — {blocker.get('title')!r}: {said!r}. Answer it in "
                             f"Drake, then re-send. Nothing was typed.")
    if not links:
        return _fail("screen", "Drake never showed a screen menu, so there is no "
                               "screen list to choose from. Nothing was typed.")

    chosen = choose_screen_link(links, want)
    if not chosen["ok"]:
        # Not on the tab Drake is showing. Select each of the others and look again.
        #
        # Selecting a tab changes what the menu DRAWS and nothing in the return, so this is
        # cheap to be wrong about — unlike the alternative of typing the code into the menu's
        # search box, which is an Edit control that also accepts anything else and would put
        # a screen code somewhere unknown if the box were not the one we thought.
        tried = []
        for tab in _tabs():
            name = (tab.get("name") or "").strip()
            if not name or name in tried:
                continue
            tried.append(name)
            if not driver.nav_act(tab, want="select")["ok"]:
                continue
            time.sleep(0.45)
            retry = choose_screen_link(_links(), want)
            if retry["ok"]:
                log(f"  screen {want} is on the {name!r} tab")
                chosen = retry
                break
        if not chosen["ok"]:
            return _fail("screen",
                         f"{chosen['reason']} Every tab was searched "
                         f"({', '.join(tried) if tried else 'none found'}), so this is not "
                         f"a tab that was merely not selected — Drake is not offering this "
                         f"screen for this return.",
                         candidates=chosen.get("candidates"))
    link = chosen["link"]
    parsed = parse_screen_link(link.get("name")) or {}

    log(f"  opening screen {parsed.get('code')} — {parsed.get('title')}")
    act = driver.nav_act(link)
    if not act["ok"]:
        # A UIA element handle goes STALE whenever Drake redraws between the moment we
        # enumerated it and the moment we use it — deleting a record does it, so does any
        # screen change. The button is still there; our reference to it is not, and it
        # surfaces as NoPatternInterfaceError / COMError on every method at once.
        #
        # Re-read the tree and try the freshly-found button once. Bounded to one retry on
        # purpose: a second identical failure is a real problem, not a redraw, and looping
        # on it would just take longer to tell the truth.
        time.sleep(0.5)
        again = choose_screen_link(_links(), want)
        if again["ok"]:
            link = again["link"]
            log("  (the screen list had been redrawn — re-read it and retried)")
            act = driver.nav_act(link)
    if not act["ok"]:
        return _fail("screen", f"could not open screen {want}: {act['error']}")

    deadline = time.time() + float(timeout)
    showing = None
    while time.time() < deadline:
        # THE RECORD CHOOSER, NOT THE SCREEN. Drake may answer a screen link with 'Existing
        # Forms List' instead of the screen. Its heading never appears, so without this the
        # wait burns the whole timeout and reports "the heading never appeared" — true, and
        # useless: the caller cannot tell a chooser it must answer from a screen that failed
        # to open. Reported as its own outcome, with the rows already read, because the
        # decision (a new record, or a duplicate to refuse) belongs to whoever knows the
        # payload — not to a navigation helper.
        picker = forms_list_hwnd(driver)
        if picker:
            rows = read_forms_list(driver, picker)
            return {"ok": False, "step": "screen", "chooser": picker, "rows": rows,
                    "reason": (f"Drake asked which {want} record to open instead of opening "
                               f"the screen — its 'Existing Forms List' is up, listing "
                               f"{max(0, len(rows) - 1)} existing record(s). Nothing was "
                               f"typed.")}
        showing = screen_is_showing(_labels(), want)
        if showing is not False:
            break
        time.sleep(0.2)

    now = driver.nav_data_entry_window()
    if showing is False:
        return _fail("screen",
                     f"screen {want} was clicked but Drake is not showing it — its heading "
                     f"never appeared. Nothing was typed. Entering here would put "
                     f"{want} field numbers into whatever screen IS open.",
                     title=now["title"])

    # FORM OR GRID. The right screen can still be the wrong MODE, and the heading is the
    # same either way. F3 is Drake's own toggle — printed on the screen — and pressing it
    # proves nothing, so what counts is the re-read afterwards. If it is still a grid the
    # run refuses with nothing typed, which is also what happens if this misread a form and
    # toggled it INTO a grid: one wasted keystroke, no values, and a message that says so.
    def _ids():
        return [e_id(e) for e in driver.nav_all_elements()]

    if screen_is_grid(_ids()):
        log(f"  screen {want} is in GRID mode — pressing {GRID_MODE_TOGGLE_KEY} to switch "
            f"to the form, then checking")
        driver.press([GRID_MODE_TOGGLE_KEY])
        deadline = time.time() + min(5.0, float(timeout))
        while time.time() < deadline:
            if not screen_is_grid(_ids()):
                break
            time.sleep(0.2)
        if screen_is_grid(_ids()):
            return _fail("screen",
                         f"screen {want} is showing Drake's GRID (spreadsheet) view and "
                         f"{GRID_MODE_TOGGLE_KEY} did not switch it to the form. Heads-down "
                         f"field numbers belong to the FORM — in the grid they address "
                         f"nothing. Press {GRID_MODE_TOGGLE_KEY} in Drake to get the form "
                         f"view, then re-send. Nothing was typed.",
                         title=now["title"])
        log("  now showing the form view")

    verified = "verified by its heading" if showing else (
        f"NOT VERIFIED — no measured heading for screen {want}, so this run cannot prove "
        f"the right screen is open")
    if showing is None:
        log(f"  ! screen {want} opened but {verified}")
    return {"ok": True, "step": "screen", "verified": bool(showing),
            "reason": f"{parsed.get('code')} — {parsed.get('title')} ({verified})",
            "title": now["title"], "hwnd": now["hwnd"]}


def create_client(driver, client_id, first_name, last_name, *, middle_initial="",
                  return_type="individual", timeout: float = 15.0, log=print) -> dict:
    """Create a return for a taxpayer Drake has never seen. {'ok', 'step', 'reason'}.

    Reached only when a lookup found NOBODY with this id. Drake's own flow, measured
    2026-08-07: OK on a missing id raises 'Drake 2025 - Open Return' asking
    "<id> does not exist. Would you like to create a new return?"; Yes raises
    'Drake 2025 - New Return' wanting a return type and a first/last name; OK there creates
    the client and drops straight onto a data-entry form with the return open.

    Everything that dialog asks for, a W-2 supplies — return type is Individual/1040 for a
    W-2 by definition, and the name is on the form. Nothing is invented here. In
    particular NO FILING STATUS is set: it is not on this dialog, it is not on a W-2, and
    guessing it would change the client's refund while looking entirely normal on screen.
    The return is created incomplete, on purpose, and the caller says so.
    """
    import time
    if not (str(first_name or "").strip() or str(last_name or "").strip()):
        return _fail("create", "refusing to create a client with no name — the W-2 gave "
                               "neither a first nor a last name to file them under")

    dlg = driver.nav_find_window(CONFIRM_CREATE_TITLE_RE, timeout=timeout)
    if not dlg:
        return _fail("create", "Drake did not offer to create a new return")
    els = driver.nav_elements(dlg)
    msg = next((e.get("name") for e in els
                if e_id(e) == CONFIRM_MSG_ID), "") or ""
    # The dialog names the id it is talking about. If that is not the id we asked for,
    # something else is on screen and pressing Yes would create the wrong client.
    if normalize_id(client_id) not in normalize_id(msg):
        return _fail("create", f"Drake's create prompt is about a different id than "
                               f"{normalize_id(client_id)} — it says {msg!r}. Nothing "
                               f"was created.")
    yes = next((e for e in els if e_id(e) == CONFIRM_YES_ID), None)
    if yes is None:
        return _fail("create", "Drake's create prompt has no Yes button")
    log(f"  no client with id {normalize_id(client_id)} — creating one")
    act = driver.nav_act(yes)
    if not act["ok"]:
        return _fail("create", f"could not answer Drake's create prompt: {act['error']}")

    win = driver.nav_find_window(NEW_RETURN_TITLE_RE, timeout=timeout)
    if not win:
        return _fail("create", "Drake's New Return window never appeared")
    nels = driver.nav_elements(win)

    def _by(aid):
        return next((e for e in nels if e_id(e) == aid), None)

    rb = _by(RETURN_TYPE_IDS.get(return_type, RETURN_TYPE_IDS["individual"]))
    if rb is None:
        return _fail("create", f"the New Return window has no {return_type!r} return type")
    sel = driver.nav_act(rb, want="select")
    if not sel["ok"]:
        return _fail("create", f"could not choose the return type: {sel['error']}")

    for aid, val, what in ((NEW_FIRST_NAME_ID, first_name, "first name"),
                           (NEW_MIDDLE_INITIAL_ID, middle_initial, "middle initial"),
                           (NEW_LAST_NAME_ID, last_name, "last name")):
        if not str(val or "").strip():
            continue
        box = _by(aid)
        if box is None:
            return _fail("create", f"the New Return window has no {what} box")
        r = driver.nav_type_into(box, str(val).strip())
        if not r["ok"]:
            return _fail("create", f"could not type the {what}: {r['error']}")

    ok_btn = _by(NEW_OK_ID)
    if ok_btn is None:
        return _fail("create", "the New Return window has no OK button")
    act = driver.nav_act(ok_btn)
    if not act["ok"]:
        return _fail("create", f"could not press OK on the New Return window: {act['error']}")
    if not driver.nav_wait_gone(win, timeout=timeout):
        return _fail("create", "OK was pressed but Drake's New Return window is still up; "
                               "no client was created")

    deadline = time.time() + float(timeout)
    cur = {"kind": "none", "title": ""}
    while time.time() < deadline:
        cur = driver.nav_data_entry_window()
        if cur["kind"] != "none":
            break
        time.sleep(0.2)
    v = verify_open_return(cur["title"], client_id, first_name, last_name)
    if not v["ok"]:
        return _fail("create", f"a return was created but it is not the one expected — "
                               f"{v['reason']}", title=cur["title"])
    log(f"  created and opened: {v['reason']}")
    return {"ok": True, "step": "created", "reason": v["reason"], "title": cur["title"],
            "kind": cur["kind"], "created": True,
            "incomplete": ["filing status", "date of birth", "address on screen 1"]}


def verify_open_return(title, wanted_id, first_name=None, last_name=None) -> dict:
    """THE GATE. Is the return Drake actually has open the right one? {'ok', 'reason'}.

    Runs after navigation, against Drake's own window title, and it is the last thing
    standing between a mis-click and 78 verified values in a stranger's return. It answers
    from what Drake says is open — not from what we asked it to open, and not from whether
    the clicks appeared to work.

    The name check is advisory ONLY when the payload has no name to check with; the id
    check never is.
    """
    parsed = parse_data_entry_title(title)
    if not parsed:
        return {"ok": False, "reason": f"no return is open — Drake's window is {title!r}"}
    want = normalize_id(wanted_id)
    if not want:
        return {"ok": False, "reason": "no SSN/EIN to verify against"}
    if parsed["id"] != want:
        return {"ok": False,
                "reason": f"WRONG RETURN IS OPEN: asked for {want}, Drake has "
                          f"{parsed['id']} ({parsed['name']}). Nothing was typed."}
    if first_name or last_name:
        nm = names_match(parsed["name"], first_name, last_name)
        if not nm["ok"]:
            return {"ok": False,
                    "reason": f"the open return's id matches but the name does not — {nm['why']}. "
                              f"Nothing was typed."}
    return {"ok": True, "reason": f"{parsed['id']} ({parsed['name']})",
            "id": parsed["id"], "name": parsed["name"]}
