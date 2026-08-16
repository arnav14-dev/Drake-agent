"""
W-2: extracted JSON -> Drake heads-down field numbers.

This is the deterministic layer between the LLM that reads a W-2 PDF and the robot that
types it into Drake. The LLM's ONLY job is to fill the schema below (`W2_SCHEMA_KEYS`);
everything after that — which Drake box a key belongs in, how the value is formatted, what
gets skipped — is fixed table lookup and pure functions. No model decides where a number
lands, which is what makes the entry auditable.

Field numbers are Drake's HEADS-DOWN numbers (Ctrl+N reveals them on screen), verified
field-by-field against a screenshot of the live Drake 2025 W-2 screen on 2026-08-02. They
are per-tax-year: re-verify with `agent.py headsdown --manual` (no --seq) after a Drake
update, which screenshots the numbers.

THE MAP IS THE SAFETY MECHANISM. Drake exposes no programmatic read-back on this build
(UIA, win32 and clipboard are all empirically dead), so a wrong field number writes a
plausible-looking value into the wrong box of a real return with nothing to catch it.
`_validate_map()` below therefore enforces at IMPORT time that no key points at a
foreign-only, inert, duplicate, or unconfirmed number.

Nothing here touches Drake or files anything — it is importable and testable anywhere,
which is why the whole plan can be reviewed with `--dry-run` on any machine.
"""

from __future__ import annotations

import os
from pathlib import Path

# Fields that exist on the screen but must NEVER be written by this map:
#   FOREIGN_ONLY  render only under a foreign address; 11 and 20 are "<Click to Access>"
#                 sub-screens, not edit boxes at all.
#   INERT         box 9 is greyed out on the modern W-2.
# Field 13 is the reason this list exists: it is the EMPLOYER's foreign Postal code, and
# was previously mis-bound to the employee SSN — which would have typed a 9-digit SSN
# into a foreign postal-code box on a domestic employer, invisibly.
FOREIGN_ONLY_FIELDS = {11, 12, 13, 20, 21, 22}
INERT_FIELDS = {31}
# Numbers at/above 88 are not legible with certainty on the reference screenshot, so the
# map refuses to bind them rather than guess.
MAX_CONFIRMED_FIELD = 87

# Keys an extractor may legitimately produce that have NO box on the W-2 screen. They are
# reported by name instead of being lumped into the generic "unknown key" warning — a
# dropped SSN must be loud, not silent.
# Keys that are TRANSPORT, not values on any form. They tell the agent how to handle the
# payload rather than what to type, so reporting them as unrecognised would put a permanent,
# meaningless warning on every document the backend sends.
#
#   drake_screen  which form this payload is for — it is what chose this map
#   doc_type      the extractor's own name for the document
#   batch_seq     where this payload sits in a batch's ORDER. The agent stops at the first
#                 document that does not complete cleanly, so the order is what "it stopped
#                 at number 3" means and which documents were never attempted.
TRANSPORT_KEYS = frozenset({"drake_screen", "doc_type", "batch_seq"})

NOT_ON_THIS_SCREEN = {
    "employee_ssn": "the W-2 screen has no SSN box — the SSN comes from screen 1 "
                    "(demographics), which is why the employee block is captioned "
                    "'if different from screen 1'. Verify it on screen 1 by eye.",
}

# kind drives sanitization + how the value is entered:
#   money    strip $ , and whitespace; drop a trailing .00 (whole dollars); skipped when zero
#   ein/ssn  digits only — Drake re-formats them itself
#   zip      digits, keeping a 5+4 hyphen
#   state    2-letter code, upper-cased
#   code     box 12 letter code (A..HH), 1-2 letters, no digits
#   year     box 12 prior-year designation, 2 digits
#   ts       taxpayer/spouse selector, "T" or "S"
#   text     names/addresses — upper-cased for Drake's convention, whitespace collapsed
#   checkbox boolean -> the checkbox token (binding: headsdown_checkbox_true, default "X");
#            FALSE is SKIPPED, never "unchecked", so we can never clear a human's entry
#
# LAYOUT NOTE, so a domestic key is never pointed at a foreign box again:
#   employer  9/10 = US state/ZIP     11/12/13 = FOREIGN province/country/postal
#   employee 18/19 = US state/ZIP     20/21/22 = FOREIGN province/country/postal
W2_FIELD_MAP = {
    # --- header ---------------------------------------------------------------
    # Whose W-2 is this? Drake defaults to "T". On a joint return an unset TS files the
    # spouse's W-2 under the taxpayer, stacking both SS wage bases on one SSN.
    "ts":                      {"field_no": 1,  "kind": "ts",       "label": "TS taxpayer/spouse"},
    # --- employer -------------------------------------------------------------
    # Field 4 is special: committing an EIN fires Drake's employer lookup + auto-fill and
    # auto-advances the caret to Box 1, dropping heads-down mode. The driver recovers by
    # retrying Ctrl+N (see _ensure_popup_open) — no ordering hack needed here.
    "employer_ein":            {"field_no": 4,  "kind": "ein",      "label": "Employer EIN"},
    "employer_name":           {"field_no": 5,  "kind": "text",     "label": "Employer name"},
    "employer_name_cont":      {"field_no": 6,  "kind": "text",     "label": "Employer name (cont)"},
    "employer_street":         {"field_no": 7,  "kind": "text",     "label": "Employer street"},
    "employer_city":           {"field_no": 8,  "kind": "text",     "label": "Employer city"},
    "employer_state":          {"field_no": 9,  "kind": "state",    "label": "Employer state"},
    "employer_zip":            {"field_no": 10, "kind": "zip",      "label": "Employer ZIP"},
    # --- employee (14-22; override block, only if different from screen 1) -----
    # There is NO employee SSN box here — see NOT_ON_THIS_SCREEN.
    "employee_first_name":     {"field_no": 14, "kind": "text",     "label": "Employee first name",
                                "confirm": True},
    "employee_last_name":      {"field_no": 15, "kind": "text",     "label": "Employee last name",
                                "confirm": True},
    "employee_street":         {"field_no": 16, "kind": "text",     "label": "Employee street"},
    "employee_city":           {"field_no": 17, "kind": "text",     "label": "Employee city"},
    "employee_state":          {"field_no": 18, "kind": "state",    "label": "Employee state"},
    "employee_zip":            {"field_no": 19, "kind": "zip",      "label": "Employee ZIP"},
    # --- boxes 1-11 -----------------------------------------------------------
    "box1_wages":              {"field_no": 23, "kind": "money",    "label": "Box 1 wages"},
    "box2_fed_wh":             {"field_no": 24, "kind": "money",    "label": "Box 2 federal W/H"},
    "box3_ss_wages":           {"field_no": 25, "kind": "money",    "label": "Box 3 SS wages"},
    "box4_ss_wh":              {"field_no": 26, "kind": "money",    "label": "Box 4 SS tax W/H"},
    "box5_med_wages":          {"field_no": 27, "kind": "money",    "label": "Box 5 Medicare wages"},
    "box6_med_wh":             {"field_no": 28, "kind": "money",    "label": "Box 6 Medicare tax W/H"},
    "box7_ss_tips":            {"field_no": 29, "kind": "money",    "label": "Box 7 SS tips"},
    "box8_alloc_tips":         {"field_no": 30, "kind": "money",    "label": "Box 8 allocated tips"},
    "box10_dep_care":          {"field_no": 32, "kind": "money",    "label": "Box 10 dependent care"},
    "box11_nonqual":           {"field_no": 33, "kind": "money",    "label": "Box 11 nonqualified plans"},
    # --- box 12 (four slots of code / amount / year) --------------------------
    # The YEAR column matters: a prior-year designation ("D 23 19500") without its year is
    # measured against the CURRENT year's deferral limit.
    "box12a_code":             {"field_no": 34, "kind": "code",     "label": "Box 12a code"},
    "box12a_amount":           {"field_no": 35, "kind": "money",    "label": "Box 12a amount"},
    "box12a_year":             {"field_no": 36, "kind": "year",     "label": "Box 12a year",
                                "confirm": True},
    "box12b_code":             {"field_no": 37, "kind": "code",     "label": "Box 12b code"},
    "box12b_amount":           {"field_no": 38, "kind": "money",    "label": "Box 12b amount"},
    "box12b_year":             {"field_no": 39, "kind": "year",     "label": "Box 12b year",
                                "confirm": True},
    "box12c_code":             {"field_no": 40, "kind": "code",     "label": "Box 12c code"},
    "box12c_amount":           {"field_no": 41, "kind": "money",    "label": "Box 12c amount"},
    "box12c_year":             {"field_no": 42, "kind": "year",     "label": "Box 12c year",
                                "confirm": True},
    "box12d_code":             {"field_no": 43, "kind": "code",     "label": "Box 12d code"},
    "box12d_amount":           {"field_no": 44, "kind": "money",    "label": "Box 12d amount"},
    "box12d_year":             {"field_no": 45, "kind": "year",     "label": "Box 12d year",
                                "confirm": True},
    # --- box 13 checkboxes ----------------------------------------------------
    "box13_statutory":         {"field_no": 46, "kind": "checkbox", "label": "Box 13 statutory employee",
                                "confirm": True},
    "box13_retirement":        {"field_no": 47, "kind": "checkbox", "label": "Box 13 retirement plan",
                                "confirm": True},
    # --- bottom-of-screen flags (85/86/87) -------------------------------------
    # Read off the live screen 2026-08-04 and legible without ambiguity. They are Drake
    # bookkeeping flags rather than W-2 boxes, and each one MEANS something on the return,
    # so all three carry confirm=True: a stray tick on "Corrected W-2" is a claim about the
    # document, not a typo. FALSE is never entered (see _clean_checkbox), so an extractor
    # that says nothing about them leaves them exactly as the preparer left them.
    "w2_altered_handwritten":  {"field_no": 85, "kind": "checkbox", "confirm": True,
                                "label": "W-2 altered or handwritten (nonstandard)"},
    "w2_corrected":            {"field_no": 86, "kind": "checkbox", "confirm": True,
                                "label": "Corrected W-2"},
    "w2_do_not_update":        {"field_no": 87, "kind": "checkbox", "confirm": True,
                                "label": "Do not update to next year"},
    "box13_third_party_sick":  {"field_no": 48, "kind": "checkbox", "label": "Box 13 third-party sick pay",
                                "confirm": True},
    # --- box 14 "Other" (4 rows x 2 columns: 49/50, 51/52, 53/54, 55/56) -------
    # Drives state add-backs and credits (NY IRC 414(h), NY/NJ/CA SDI/FLI), so it is worth
    # entering — but WHICH column is description and which is amount is an assumption from
    # the screen layout, NOT confirmed by entry. Every one carries confirm=True and
    # build_plan emits a loud warning. Verify the first one you enter by eye.
    "box14_1_desc":            {"field_no": 49, "kind": "text",     "label": "Box 14 line 1 description",
                                "confirm": True, "max_len": 8},
    "box14_1_amount":          {"field_no": 50, "kind": "money",    "label": "Box 14 line 1 amount",
                                "confirm": True},
    "box14_2_desc":            {"field_no": 51, "kind": "text",     "label": "Box 14 line 2 description",
                                "confirm": True, "max_len": 8},
    "box14_2_amount":          {"field_no": 52, "kind": "money",    "label": "Box 14 line 2 amount",
                                "confirm": True},
    "box14_3_desc":            {"field_no": 53, "kind": "text",     "label": "Box 14 line 3 description",
                                "confirm": True, "max_len": 8},
    "box14_3_amount":          {"field_no": 54, "kind": "money",    "label": "Box 14 line 3 amount",
                                "confirm": True},
    "box14_4_desc":            {"field_no": 55, "kind": "text",     "label": "Box 14 line 4 description",
                                "confirm": True, "max_len": 8},
    "box14_4_amount":          {"field_no": 56, "kind": "money",    "label": "Box 14 line 4 amount",
                                "confirm": True},
    # --- boxes 15-20, state/local (4 rows of 7: 57-63, 64-70, 71-77, 78-84) ----
    # A multi-state or PA-local W-2 needs rows 2-4; without them everything past row 1 was
    # silently unrepresentable (W2_SCHEMA_KEYS is derived, so the extractor was never asked).
    "box15_state":             {"field_no": 57, "kind": "state",    "label": "Box 15 state"},
    "box15_state_id":          {"field_no": 58, "kind": "text",     "label": "Box 15 employer state ID"},
    "box16_state_wages":       {"field_no": 59, "kind": "money",    "label": "Box 16 state wages"},
    "box17_state_wh":          {"field_no": 60, "kind": "money",    "label": "Box 17 state income tax"},
    "box18_local_wages":       {"field_no": 61, "kind": "money",    "label": "Box 18 local wages"},
    "box19_local_wh":          {"field_no": 62, "kind": "money",    "label": "Box 19 local income tax"},
    "box20_locality":          {"field_no": 63, "kind": "text",     "label": "Box 20 locality name", "max_len": 9},
    "box15_state_2":           {"field_no": 64, "kind": "state",    "label": "Box 15 state (row 2)"},
    "box15_state_id_2":        {"field_no": 65, "kind": "text",     "label": "Box 15 state ID (row 2)"},
    "box16_state_wages_2":     {"field_no": 66, "kind": "money",    "label": "Box 16 state wages (row 2)"},
    "box17_state_wh_2":        {"field_no": 67, "kind": "money",    "label": "Box 17 state tax (row 2)"},
    "box18_local_wages_2":     {"field_no": 68, "kind": "money",    "label": "Box 18 local wages (row 2)"},
    "box19_local_wh_2":        {"field_no": 69, "kind": "money",    "label": "Box 19 local tax (row 2)"},
    "box20_locality_2":        {"field_no": 70, "kind": "text",     "label": "Box 20 locality (row 2)", "max_len": 9},
    "box15_state_3":           {"field_no": 71, "kind": "state",    "label": "Box 15 state (row 3)"},
    "box15_state_id_3":        {"field_no": 72, "kind": "text",     "label": "Box 15 state ID (row 3)"},
    "box16_state_wages_3":     {"field_no": 73, "kind": "money",    "label": "Box 16 state wages (row 3)"},
    "box17_state_wh_3":        {"field_no": 74, "kind": "money",    "label": "Box 17 state tax (row 3)"},
    "box18_local_wages_3":     {"field_no": 75, "kind": "money",    "label": "Box 18 local wages (row 3)"},
    "box19_local_wh_3":        {"field_no": 76, "kind": "money",    "label": "Box 19 local tax (row 3)"},
    "box20_locality_3":        {"field_no": 77, "kind": "text",     "label": "Box 20 locality (row 3)", "max_len": 9},
    "box15_state_4":           {"field_no": 78, "kind": "state",    "label": "Box 15 state (row 4)"},
    "box15_state_id_4":        {"field_no": 79, "kind": "text",     "label": "Box 15 state ID (row 4)"},
    "box16_state_wages_4":     {"field_no": 80, "kind": "money",    "label": "Box 16 state wages (row 4)"},
    "box17_state_wh_4":        {"field_no": 81, "kind": "money",    "label": "Box 17 state tax (row 4)"},
    "box18_local_wages_4":     {"field_no": 82, "kind": "money",    "label": "Box 18 local wages (row 4)"},
    "box19_local_wh_4":        {"field_no": 83, "kind": "money",    "label": "Box 19 local tax (row 4)"},
    "box20_locality_4":        {"field_no": 84, "kind": "text",     "label": "Box 20 locality (row 4)", "max_len": 9},
}

# The exact key set the LLM extractor may emit. Includes the keys that have no box here, so
# the SSN is still extracted (and cross-checked against screen 1) rather than thrown away.
W2_SCHEMA_KEYS = tuple(W2_FIELD_MAP.keys()) + tuple(NOT_ON_THIS_SCREEN)

# Fields whose number is a dropdown/selection on Drake's screen rather than a free-text box.
DROPDOWN_FIELDS = {1, 9, 18, 34, 37, 40, 43, 57, 63, 64, 70, 71, 77, 78, 84}

# Dropdowns CONFIRMED to take a typed code on this build — live run 2026-08-03, which read
# each of them back as the code plus the entry Drake selected ('T T', 'TX TX', 'D D'). They
# are still worth a human's eye on a NEW build, but they are no longer unknowns, and saying
# so keeps the plan's warnings pointed at what actually is one. 57 joined them on the
# 2026-08-04 end-to-end run ('57 TX TX').
DROPDOWNS_CONFIRMED = {1, 9, 34, 57}

# Fields whose typed value is CONFIRMED not to stick on this build, whatever is typed.
# Empty on purpose: Box 20 locality (63/70/77/84) lived here until 2026-08-05, when the
# cause turned out to be the VALUE and not the field — see the locality section below.
# The mechanism stays because the next such field will want it.
HAND_ENTRY_FIELDS: dict[int, str] = {}

# --- Box 20 locality --------------------------------------------------------------------
# Box 20 is not a free-text box and never was. It is a dropdown whose entries come from
# Drake's OWN lookup table — STATELIB\CITY.HLP, three columns, "ST,City Code,City Name" —
# and the value Drake stores is the CODE, not the name printed on the W-2.
#
# That is the entire reason four locality fields reported OK and wrote nothing on
# 2026-08-04. We typed 'PHILA', 'NYC' and 'MARION'. The codes are 'PL', 'NY' and '49'.
# Drake matched none of them, selected nothing, and a dropdown with nothing selected
# stores nothing — while the heads-down popup still echoed the characters back, which is
# why the per-field gate passed. The gate was right about what it could see; the value
# was wrong. Nothing about the gate is relaxed here — the entry is now simply correct.
#
# Only 11 states appear in the table at all (CA DE IN KY MI MO NY OH OR PA). For any other
# state the list is genuinely EMPTY: no local income tax Drake tracks, no code to select,
# nothing a human could pick either. Those rows are reported, never attempted.
#
# The table is read from the LIVE install, so it tracks Drake's updates instead of drifting
# against a copy. If it cannot be found, resolution is disabled and every locality is handed
# to a human WITH that reason — it never falls back to typing an unvalidated string.

# locality field -> the payload key holding the Box 15 state on the SAME row. The list is
# per-state, so a locality means nothing without knowing which row's state it belongs to.
LOCALITY_FIELDS = {63: "box15_state", 70: "box15_state_2",
                   77: "box15_state_3", 84: "box15_state_4"}

CITY_HLP_CANDIDATES = (
    r"C:\DRAKE25\STATELIB\CITY.HLP",
    r"C:\Drake25\STATELIB\CITY.HLP",
)

# Real W-2s print the locality's NAME, and a few common ones are written in a form no
# prefix match reaches. Hand-curated, deliberately tiny, consulted only after both code
# and name matching have failed. Anything not here that does not match falls through to a
# rejection that names the codes Drake does list — a wrong guess is worse than a question.
LOCALITY_ALIASES = {
    ("NY", "NYC"): "NY",
    ("NY", "NEW YORK"): "NY",
    ("PA", "PHL"): "PL",
    ("MO", "KCMO"): "KC",
    ("MO", "STL"): "SL",
    ("MI", "DET"): "DT",
}

_LOCALITY_TABLE: dict[str, dict[str, str]] | None = None
_LOCALITY_SOURCE: str | None = None

# Box 14's description/amount column assignment is inferred from the screen layout, not
# from a confirmed entry. Kept as data so the warning can name the exact fields.
BOX14_DESC_FIELDS = {49, 51, 53, 55}
BOX14_AMOUNT_FIELDS = {50, 52, 54, 56}


def _validate_map() -> None:
    """Fail at IMPORT if the map could write into a box it must never touch.

    This exists because a wrong field number is the one error class with no downstream
    defense: there is no read-back on this build, the value looks plausible in the wrong
    box, and the operator sees a green run. Making that class of mistake un-importable is
    cheaper than catching it after a keystroke.
    """
    nums = [(k, s["field_no"]) for k, s in W2_FIELD_MAP.items()]
    forbidden = FOREIGN_ONLY_FIELDS | INERT_FIELDS
    bad = sorted((k, n) for k, n in nums if n in forbidden)
    if bad:
        raise RuntimeError(
            "w2_map: key(s) bound to a FOREIGN-ONLY or INERT Drake field — these boxes are "
            f"invisible, a sub-screen, or dead on a domestic W-2: {bad}")
    seen: dict[int, str] = {}
    for k, n in nums:
        if n in seen:
            raise RuntimeError(f"w2_map: field {n} is claimed by both {seen[n]!r} and {k!r}")
        seen[n] = k
    out_of_range = sorted((k, n) for k, n in nums if not 1 <= n <= MAX_CONFIRMED_FIELD)
    if out_of_range:
        raise RuntimeError(
            f"w2_map: field number(s) outside the CONFIRMED range 1-{MAX_CONFIRMED_FIELD} "
            f"(higher numbers are not legible on the reference screen): {out_of_range}")
    overlap = set(W2_FIELD_MAP) & set(NOT_ON_THIS_SCREEN)
    if overlap:
        raise RuntimeError(f"w2_map: key(s) both mapped and marked not-on-screen: {sorted(overlap)}")


_validate_map()


def _clean_money(v) -> str | None:
    """'$52,000.00' -> '52000'; '3224.50' -> '3224.50'; 0 / '0.00' -> None (skip).
    A blank box IS zero on a tax form, so writing an explicit 0 is noise — and every
    keystroke we don't send is one that can't go wrong."""
    s = str(v).strip().replace("$", "").replace(",", "").replace(" ", "")
    if s in ("", "-", "."):
        return None
    neg = s.startswith("(") and s.endswith(")")  # accounting negatives
    if neg:
        s = s[1:-1]
    try:
        amt = float(s)
    except ValueError:
        return None
    if amt == 0:
        return None
    if neg:
        amt = -amt
    # Whole dollars stay whole; real cents are preserved.
    return str(int(amt)) if amt == int(amt) else f"{amt:.2f}"


def _digits(v) -> str | None:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return s or None


def _clean_zip(v) -> str | None:
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch == "-").strip("-")
    return s or None


def _clean_text(v) -> str | None:
    s = " ".join(str(v).split()).upper()
    return s or None


# Full state names -> USPS codes. Without this, truncating to two letters silently
# corrupts: 'Texas' -> 'TE', 'Ohio' -> 'OH' (right by luck), 'Maine' -> 'MA' (MASSACHUSETTS —
# wrong state, right-looking code, and a wrong-state W-2 is a wrong return). An unrecognized
# multi-letter name is rejected (None) rather than truncated.
_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICTOFCOLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEWHAMPSHIRE": "NH", "NEWJERSEY": "NJ",
    "NEWMEXICO": "NM", "NEWYORK": "NY", "NORTHCAROLINA": "NC", "NORTHDAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODEISLAND": "RI", "SOUTHCAROLINA": "SC", "SOUTHDAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WESTVIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTORICO": "PR", "GUAM": "GU", "VIRGINISLANDS": "VI", "AMERICANSAMOA": "AS",
    "NORTHERNMARIANAISLANDS": "MP",
    # Overseas military. AP is not hypothetical — it is what the reference Drake screen
    # had sitting in field 9; without these an overseas W-2's state box is silently skipped.
    "ARMEDFORCESAMERICAS": "AA", "ARMEDFORCESEUROPE": "AE", "ARMEDFORCESPACIFIC": "AP",
}
_STATE_CODES = set(_STATE_NAMES.values())


def _clean_state(v) -> str | None:
    s = "".join(ch for ch in str(v) if ch.isalpha()).upper()
    if not s:
        return None
    if len(s) == 2:
        return s if s in _STATE_CODES else None
    return _STATE_NAMES.get(s)  # unknown long name -> None (skipped + reported), never truncated


def _clean_code(v) -> str | None:
    """Box 12 letter code (A..HH): 1-2 letters, no digits.

    A raw code carrying digits ('D 23') is REJECTED rather than stripped to 'D': the 23 is
    a prior-year designation that belongs in the year field (36/39/42/45), and silently
    dropping it measures the whole amount against the current year's deferral limit."""
    raw = str(v).strip()
    if any(ch.isdigit() for ch in raw):
        return None
    s = "".join(ch for ch in raw if ch.isalpha()).upper()
    return s if 1 <= len(s) <= 2 else None


def _clean_year(v) -> str | None:
    """Box 12 prior-year designation -> two digits ('2023' or '23' -> '23')."""
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) == 4 and s[:2] in ("19", "20"):
        return s[2:]
    return s if len(s) == 2 else None


def _clean_ts(v) -> str | None:
    """Taxpayer/spouse selector -> 'T' or 'S'."""
    s = "".join(ch for ch in str(v) if ch.isalpha()).upper()
    if s in ("T", "S"):
        return s
    if s in ("TAXPAYER", "PRIMARY", "SELF"):
        return "T"
    if s == "SPOUSE":
        return "S"
    return None


def _clean_checkbox(v, token: str) -> str | None:
    """True -> the check token. False/absent -> None (SKIP).

    Never emits an 'uncheck' keystroke: if the extractor is wrong about a false, the worst
    case is a box a human still has to tick — not one we silently cleared."""
    if isinstance(v, str):
        truthy = v.strip().lower() in ("true", "yes", "y", "1", "x", "checked")
    else:
        truthy = bool(v)
    return token if truthy else None


def _has_content(v) -> bool:
    """Did the extractor actually give us something? False for None/''/whitespace and for
    a False boolean (an unticked checkbox is an answer, not a missing value)."""
    if v is None or isinstance(v, bool):
        return False
    return bool(str(v).strip())


def _is_zeroish(v) -> bool:
    """A money value that legitimately means zero ('0', '0.00', '$0') — an empty box, not
    a parse failure."""
    s = str(v).strip().replace("$", "").replace(",", "").replace(" ", "")
    s = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    try:
        return float(s) == 0
    except ValueError:
        return False


def sanitize(kind: str, value, *, checkbox_token: str = "X") -> str | None:
    """Value as Drake should receive it, or None meaning SKIP this field entirely."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if kind == "money":
        return _clean_money(value)
    if kind in ("ein", "ssn"):
        return _digits(value)
    if kind == "zip":
        return _clean_zip(value)
    if kind == "state":
        return _clean_state(value)
    if kind == "code":
        return _clean_code(value)
    if kind == "year":
        return _clean_year(value)
    if kind == "ts":
        return _clean_ts(value)
    if kind == "checkbox":
        return _clean_checkbox(value, checkbox_token)
    return _clean_text(value)


def _city_hlp_path() -> Path | None:
    """Where Drake's locality table lives. DRAKE_CITY_HLP overrides, for a non-default install."""
    env = os.environ.get("DRAKE_CITY_HLP")
    for p in ([env] if env else []) + list(CITY_HLP_CANDIDATES):
        try:
            if p and Path(p).is_file():
                return Path(p)
        except OSError:
            continue
    return None


def load_locality_table(force: bool = False) -> dict[str, dict[str, str]]:
    """Parse CITY.HLP into {'PA': {'PL': 'Philadelphia', ...}, ...}. {} if not found.

    Cached, because build_plan asks once per locality field and the file does not change
    mid-run. `force=True` re-reads it (after a Drake update, or in tests).
    """
    global _LOCALITY_TABLE, _LOCALITY_SOURCE
    if _LOCALITY_TABLE is not None and not force:
        return _LOCALITY_TABLE
    table: dict[str, dict[str, str]] = {}
    path = _city_hlp_path()
    if path is not None:
        try:
            # latin-1 never raises on a byte, which matters for a vendor file we do not
            # control: a stray high byte in one city name must not cost us the other 900.
            text = path.read_text(encoding="latin-1")
        except OSError:
            text = ""
        for line in text.splitlines():
            parts = line.split(",")
            if len(parts) < 3:
                continue
            st = parts[0].strip().upper()
            code = parts[1].strip().upper()
            name = ",".join(parts[2:]).strip()
            if len(st) != 2 or st == "ST" or not code or not name:
                continue                      # drops the "ST,City Code,City Name" header
            table.setdefault(st, {})[code] = name
        _LOCALITY_SOURCE = str(path)
    _LOCALITY_TABLE = table
    return table


def locality_source() -> str | None:
    """Path the locality table was read from, for the plan to cite. None if not found."""
    load_locality_table()
    return _LOCALITY_SOURCE


def _normalize_locality(raw) -> str:
    """'  Marion County ' -> 'MARION'. Only suffixes that are unambiguously noise are cut:
    ' CITY' is NOT, because Michigan really does list 'Albion City' and 'Portland City'."""
    s = " ".join(str(raw).strip().upper().split())
    for suffix in (" COUNTY", " CO."):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def resolve_locality(state, raw) -> dict:
    """Resolve a Box 20 locality to the CODE Drake's dropdown stores.

    Returns {"code", "name", "how", "reason", "candidates"}. `code` is None when the value
    cannot be resolved, and `reason` then says why in terms a preparer can act on. It never
    guesses: an ambiguous match is a rejection that lists what Drake actually offers.
    """
    out = {"code": None, "name": None, "how": None, "reason": None, "candidates": []}
    table = load_locality_table()
    if not table:
        out["reason"] = ("Drake's locality table (STATELIB\\CITY.HLP) was not found on this "
                         "machine, so the code behind a Box 20 name cannot be looked up. Set "
                         "DRAKE_CITY_HLP to its path, or pick this locality from the dropdown "
                         "by hand.")
        return out
    st = str(state or "").strip().upper()
    if not st:
        out["reason"] = ("the Box 15 state on this row is empty. Drake's locality list is "
                         "per-state, so there is no list to match against — fill Box 15 on "
                         "this row first.")
        return out
    entries = table.get(st) or {}
    if not entries:
        out["reason"] = (f"{st} has no localities in Drake's table — it levies no local income "
                         f"tax Drake tracks, so the Box 20 dropdown on that row is EMPTY. There "
                         f"is nothing to select there, by robot or by hand.")
        return out

    def _hit(code, how):
        out.update({"code": code, "name": entries[code], "how": how})
        return out

    def _ambiguous(codes, why):
        shown = sorted(codes)[:12]
        out["candidates"] = [f"{c} ({entries[c]})" for c in shown]
        more = f" (+{len(codes) - len(shown)} more)" if len(codes) > len(shown) else ""
        out["reason"] = f"{why} Drake lists: {', '.join(out['candidates'])}{more}."
        return out

    text = _normalize_locality(raw)
    if text in entries:                                        # already the code
        return _hit(text, "code")
    by_name = [c for c, n in entries.items() if n.strip().upper() == text]
    if len(by_name) == 1:
        return _hit(by_name[0], "name")
    if len(by_name) > 1:
        # Several codes genuinely share one name (California lists four for Voluntary Plan
        # DI). Picking one at random would be a coin flip on the return.
        return _ambiguous(by_name, f"{text!r} matches {len(by_name)} different codes in {st}.")
    alias = LOCALITY_ALIASES.get((st, text))
    if alias and alias in entries:
        return _hit(alias, "alias")
    prefix = [c for c, n in entries.items() if n.strip().upper().startswith(text)]
    if len(prefix) == 1:
        return _hit(prefix[0], "prefix")
    if len(prefix) > 1:
        return _ambiguous(prefix, f"{text!r} is the start of {len(prefix)} locality names in {st}.")
    out["reason"] = (f"{text!r} is not a locality Drake lists for {st} ({len(entries)} on file), "
                     f"so its dropdown has nothing to select and would store nothing. Check the "
                     f"spelling against Box 20 on the W-2, or pick it by hand.")
    return out


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Turn extracted W-2 JSON into an ordered, fully-resolved entry plan.

    Returns {"entries", "skipped", "skipped_by_request", "not_on_screen", "unknown_keys",
             "warnings"}.

    Entries are sorted by field number. When the EIN (4) is present that puts it first,
    which is deliberate — Drake's employer auto-fill runs before we write the employer
    name/address, so our extracted values land on top of whatever it filled in. When the
    EIN is skipped (`skip_fields={4}`) no auto-fill happens at all and the employer block
    is written clean, which is MORE deterministic, not less.

    `skip_fields` is an operator instruction ("do not touch this box"), reported separately
    from fields skipped because they were empty — the two mean completely different things
    and must never look alike in the printed plan.

    Nothing is dropped silently: a key that isn't in the map, a key with no box on this
    screen, or a value that sanitizes to nothing is reported so `--dry-run` shows the
    complete picture before anything is typed.
    """
    skip = {int(n) for n in (skip_fields or [])}
    entries, skipped, by_request, not_on_screen = [], [], [], []
    hand_entry = []
    unknown, warnings = [], []
    payload = dict(payload or {})
    if ts is not None:
        payload["ts"] = ts

    for key, raw in payload.items():
        # Transport, not a value: `drake_screen` is how a payload names the form it is for,
        # and it is what chose this map in the first place. Reporting it as an unrecognised
        # key would put a permanent, meaningless warning on every W-2 the backend sends.
        if key.startswith("_") or key in TRANSPORT_KEYS:
            continue
        if key in NOT_ON_THIS_SCREEN:
            not_on_screen.append({"key": key, "raw": raw, "why": NOT_ON_THIS_SCREEN[key]})
            continue
        spec = W2_FIELD_MAP.get(key)
        if spec is None:
            unknown.append(key)
            continue
        # Box 20 resolves against Drake's own locality table before anything else, because
        # what gets typed is the CODE and the name on the W-2 is only the way in. An empty
        # box still falls through to the ordinary "nothing to enter" path below.
        if spec["field_no"] in LOCALITY_FIELDS and _has_content(raw):
            row_state = payload.get(LOCALITY_FIELDS[spec["field_no"]])
            res = resolve_locality(row_state, raw)
            row = {"key": key, "field_no": spec["field_no"], "label": spec["label"],
                   "kind": spec["kind"], "value": res["code"], "raw": raw,
                   "confirm": True, "trimmed_from": None,
                   "resolved_from": _clean_text(raw), "locality_name": res["name"]}
            cap = spec.get("max_len")
            if res["code"] and cap and len(res["code"]) > int(cap):
                # A code is not text: cutting it short does not shorten a name, it names a
                # DIFFERENT locality. Refuse rather than truncate.
                res = {**res, "code": None,
                       "reason": f"Drake's code for it ({res['code']!r}) is longer than the "
                                 f"{cap}-character box, so it cannot be typed without becoming "
                                 f"a different code."}
                row["value"] = None
            if res["code"] is None:
                row["value"] = _clean_text(raw)
                row["why"] = res["reason"]
                hand_entry.append(row)
                continue
            warnings.append(
                f"field {spec['field_no']} ({spec['label']}): {_clean_text(raw)!r} resolved to "
                f"Drake's locality code {res['code']!r} ({res['name']}) for "
                f"{str(row_state).strip().upper()}, matched by {res['how']}. The CODE is what is "
                f"typed and what Drake stores — the box will then show Drake's own name for it.")
            if spec["field_no"] in skip:
                by_request.append(row)
            else:
                entries.append(row)
            continue
        val = sanitize(spec["kind"], raw, checkbox_token=checkbox_token)
        # Drake enforces a maximum length on some boxes and simply STOPS accepting
        # characters — measured live on field 49 (Box 14 description), which caps at 8:
        # 'UNION DUES' settled as 'UNION DU' and the run halted rather than commit a value
        # it could not confirm. Trimming to the box's real capacity is what a preparer does
        # by hand; doing it SILENTLY is not, so every trim is reported.
        trimmed_from = None
        maxlen = spec.get("max_len")
        if val is not None and maxlen and len(val) > int(maxlen):
            trimmed_from, val = val, val[:int(maxlen)]
        if val is None and include_zeros and spec["kind"] == "money":
            val = "0" if str(raw).strip() not in ("", "None") else None
        if val is None:
            # Distinguish "the box is genuinely empty/zero" (fine, leave it alone) from
            # "the extractor gave us something we could not turn into a valid value"
            # (e.g. an unrecognized state name, a non-numeric amount). The second is a
            # data problem a human must see — it must never look like a blank field.
            rejected = _has_content(raw) and spec["kind"] not in ("money", "checkbox")
            if not rejected and spec["kind"] == "money":
                rejected = _has_content(raw) and _clean_money(raw) is None and not _is_zeroish(raw)
            skipped.append({"key": key, "field_no": spec["field_no"], "label": spec["label"],
                            "raw": raw, "rejected": rejected})
            if rejected:
                warnings.append(f"REJECTED {key} = {raw!r} — not a valid {spec['kind']} for "
                                f"field {spec['field_no']} ({spec['label']}). It was NOT entered; "
                                f"fix the extraction or enter that box by hand.")
            continue
        row = {
            "key": key,
            "field_no": spec["field_no"],
            "label": spec["label"],
            "kind": spec["kind"],
            "value": val,
            "raw": raw,
            # Flags a human should eyeball on the first run of a new Drake build.
            "confirm": bool(spec.get("confirm")) or spec["field_no"] in DROPDOWN_FIELDS
                       or trimmed_from is not None,
            "trimmed_from": trimmed_from,
        }
        if trimmed_from is not None:
            warnings.append(f"field {spec['field_no']} ({spec['label']}): {trimmed_from!r} was "
                            f"TRUNCATED to {val!r} — Drake's box holds {maxlen} characters. "
                            f"The short form is what will be on the return; check it still "
                            f"says what it needs to.")
        if spec["field_no"] in skip:
            by_request.append(row)
            continue
        if spec["field_no"] in HAND_ENTRY_FIELDS:
            row["why"] = HAND_ENTRY_FIELDS[spec["field_no"]]
            hand_entry.append(row)
            continue
        entries.append(row)

    entries.sort(key=lambda e: e["field_no"])
    by_request.sort(key=lambda e: e["field_no"])

    for n in sorted(skip - {e["field_no"] for e in by_request}):
        warnings.append(f"--skip-field {n} was given but no extracted value maps to field {n} "
                        f"— nothing to skip there (check the number).")
    if by_request:
        warnings.append(f"{len(by_request)} field(s) held back BY REQUEST (not empty — you asked "
                        f"for them to be left alone): "
                        f"{', '.join(str(e['field_no']) for e in by_request)}.")
    for e in not_on_screen:
        warnings.append(f"{e['key']} = {e['raw']!r} was extracted but NOT entered: {e['why']}")
    if not payload.get("ts"):
        warnings.append("TS (field 1) was not supplied — Drake will use its default 'T' "
                        "(taxpayer). On a JOINT return that files a spouse's W-2 under the "
                        "taxpayer and stacks both SS wage bases on one SSN. Pass --ts T|S.")
    if unknown:
        warnings.append(f"{len(unknown)} key(s) not in the W-2 field map were IGNORED: "
                        f"{', '.join(sorted(unknown))}")
    dropdowns = sorted({e["field_no"] for e in entries if e["field_no"] in DROPDOWN_FIELDS})
    unconfirmed = [n for n in dropdowns if n not in DROPDOWNS_CONFIRMED]
    confirmed = [n for n in dropdowns if n in DROPDOWNS_CONFIRMED]
    if unconfirmed:
        warnings.append(f"field(s) {unconfirmed} are dropdowns on Drake's screen whose typed "
                        f"code has NOT been confirmed to select an entry on this build — "
                        f"check those boxes on the screenshot.")
    if confirmed:
        warnings.append(f"field(s) {confirmed} are dropdowns; typing the code IS confirmed to "
                        f"select the entry on this build (read back as code + entry). Still "
                        f"worth an eye after a Drake version bump.")
    if any(e["kind"] == "checkbox" for e in entries):
        warnings.append(f"checkbox field(s) — Drake's heads-down popup shows the TICK BOX "
                        f"itself for these, not a text box, so the tick is read back off the "
                        f"widget before it is committed. {checkbox_token!r} is tried first "
                        f"(binding: navigation.headsdown_checkbox_true), then the other "
                        f"tokens; if none of them flips the tick the run HALTs with the box "
                        f"untouched. Run `agent.py probe-checkbox` once per build to learn "
                        f"which token this one takes.")
    box14 = sorted({e["field_no"] for e in entries
                    if e["field_no"] in BOX14_DESC_FIELDS | BOX14_AMOUNT_FIELDS})
    if box14:
        warnings.append(f"Box 14 field(s) {box14}: which column is DESCRIPTION "
                        f"({sorted(BOX14_DESC_FIELDS)}) and which is AMOUNT "
                        f"({sorted(BOX14_AMOUNT_FIELDS)}) is inferred from the screen layout "
                        f"and NOT confirmed by entry — verify these boxes by eye first.")
    if any(e["field_no"] in LOCALITY_FIELDS for e in entries + hand_entry):
        src = locality_source()
        warnings.append(
            f"Box 20 locality codes were resolved against Drake's own table at {src} — the "
            f"live file, so it tracks Drake's updates rather than a copy of them."
            if src else
            "Drake's locality table (STATELIB\\CITY.HLP) was NOT found, so no Box 20 value "
            "could be resolved to the code Drake stores. Every locality is left for a human. "
            "Set DRAKE_CITY_HLP if Drake is installed somewhere other than C:\\DRAKE25.")
    for e in hand_entry:
        warnings.append(f"field {e['field_no']} ({e['label']}) = {e['value']!r} will NOT be "
                        f"typed: {e['why']}")
    return {"entries": entries, "skipped": skipped, "skipped_by_request": by_request,
            "not_on_screen": not_on_screen, "hand_entry": hand_entry,
            "unknown_keys": unknown, "warnings": warnings}


def format_plan(plan: dict) -> str:
    """The plan as a human-readable table — what `--dry-run` prints for review BEFORE any
    keystroke reaches Drake."""
    lines = ["", f"{len(plan['entries'])} field(s) will be entered, in field-number order:", ""]
    lines.append(f"  {'fld':>4}  {'label':<32} {'value':<24} source key")
    lines.append(f"  {'-'*4}  {'-'*32} {'-'*24} {'-'*24}")
    for e in plan["entries"]:
        mark = " *" if e["confirm"] else "  "
        val = e["value"]
        # A locality's code is meaningless on its own — show what it came from, so the
        # reviewer can check the resolution and not just the two characters being typed.
        if e.get("resolved_from") and e["resolved_from"] != val:
            val = f"{val} <- {e['resolved_from']}"
        lines.append(f"  {e['field_no']:>4}{mark}{e['label']:<32} {val:<24} {e['key']}")
    if any(e["confirm"] for e in plan["entries"]):
        lines.append("")
        lines.append("  * = verify this box by eye on the screenshot (dropdown, checkbox, or identity field)")
    if plan.get("skipped_by_request"):
        lines.append("")
        lines.append(f"  HELD BACK BY REQUEST ({len(plan['skipped_by_request'])}) — these had a "
                     f"value; you asked for the box to be left alone:")
        for e in plan["skipped_by_request"]:
            lines.append(f"      field {e['field_no']:<4} {e['label']:<32} would have been {e['value']!r}")
    if plan.get("not_on_screen"):
        lines.append("")
        lines.append(f"  NOT ON THIS SCREEN ({len(plan['not_on_screen'])}) — extracted, but the "
                     f"W-2 screen has no box for it:")
        for e in plan["not_on_screen"]:
            lines.append(f"      {e['key']} = {e['raw']!r}")
            lines.append(f"        {e['why']}")
    if plan["skipped"]:
        lines.append("")
        lines.append(f"  skipped ({len(plan['skipped'])}) — empty or zero, so the box is left alone:")
        for s in plan["skipped"]:
            mark = "  !! REJECTED " if s.get("rejected") else "    "
            lines.append(f"{mark}field {s['field_no']:<4} {s['label']:<32} raw={s['raw']!r}")
    for w in plan["warnings"]:
        lines.append("")
        lines.append(f"  NOTE: {w}")
    return "\n".join(lines) + "\n"
