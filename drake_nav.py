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


def plan_record_use(state: dict, employer_ein=None, *, allow_new: bool = True) -> dict:
    """Use this record, open a fresh one, or refuse? {'action', 'reason'}.

    action is 'use' | 'new' | 'refuse'.

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
        return {"action": "use",
                "reason": f"{where} holds {state['populated']} stray value(s) but no "
                          f"employer and no amounts — not a W-2, so it is being filled in "
                          f"rather than left behind"}

    want = normalize_id(employer_ein)
    if want:
        for v in state.get("values") or ():
            if normalize_id(v.get("value")) == want:
                return {"action": "refuse",
                        "reason": f"{where} already holds employer EIN {want} — this W-2 "
                                  f"looks like it has already been entered, and entering it "
                                  f"again would double the client's wages. Nothing was typed."}
    if not allow_new:
        return {"action": "refuse",
                "reason": f"{where} already has {state['populated']} value(s) on it and "
                          f"opening a new record is disabled"}
    return {"action": "new",
            "reason": f"{where} already has {state['populated']} value(s) for a different "
                      f"employer — opening a new W-2 record"}


def _fail(step, reason, **extra) -> dict:
    return {"ok": False, "step": step, "reason": reason, **extra}


def open_client(driver, client_id, *, first_name=None, last_name=None,
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
    chosen, rows = None, []
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        rows = collect_client_rows(driver.nav_elements(dlg))
        chosen = choose_client_row(rows, want)
        if chosen["ok"] or chosen.get("not_found"):
            break
        time.sleep(0.15)
    if chosen is None:
        return _fail("search", "the client list never became readable")
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


def open_screen(driver, code, *, timeout: float = 10.0, log=print) -> dict:
    """Open a data-entry screen by its Drake code. {'ok', 'step', 'reason', 'title'}.

    Assumes a return is already open and verified — this does not re-check identity,
    because `open_client` is what proves it and doing it twice in two places is how the
    two copies drift apart.
    """
    want = str(code or "").strip().upper()
    cur = driver.nav_data_entry_window()
    if cur["kind"] == "none":
        return _fail("screen", "no return is open, so there is no menu to open a screen from")
    if cur["kind"] == "form":
        log(f"  a form screen is already open; returning to the menu")
        driver.press(["Esc"])
        import time
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            cur = driver.nav_data_entry_window()
            if cur["kind"] == "menu":
                break
            time.sleep(0.15)
    if cur["kind"] != "menu":
        return _fail("screen", f"Drake is not showing the Data Entry Menu "
                               f"(it is showing a {cur['kind']!r} window), so the screen "
                               f"list cannot be read")

    els = driver.nav_elements(cur["hwnd"])
    links = [e for e in els if re.match(SCREEN_LINK_ID_RE, e_id(e)) and (e.get("name") or "")]
    chosen = choose_screen_link(links, want)
    if not chosen["ok"]:
        return _fail("screen", chosen["reason"], candidates=chosen.get("candidates"))
    link = chosen["link"]
    parsed = parse_screen_link(link.get("name")) or {}
    log(f"  opening screen {parsed.get('code')} — {parsed.get('title')}")

    act = driver.nav_act(link)
    if not act["ok"]:
        return _fail("screen", f"could not open screen {want}: {act['error']}")

    import time
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        now = driver.nav_data_entry_window()
        if now["kind"] == "form":
            return {"ok": True, "step": "screen", "reason": f"{parsed.get('code')} — "
                    f"{parsed.get('title')}", "title": now["title"], "hwnd": now["hwnd"]}
        time.sleep(0.15)
    return _fail("screen", f"screen {want} was clicked but no data-entry form appeared")


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
