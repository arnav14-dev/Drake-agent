"""
W-2: extracted JSON -> Drake heads-down field numbers.

This is the deterministic layer between the LLM that reads a W-2 PDF and the robot that
types it into Drake. The LLM's ONLY job is to fill the schema below (`W2_SCHEMA_KEYS`);
everything after that — which Drake box a key belongs in, how the value is formatted, what
gets skipped — is fixed table lookup and pure functions. No model decides where a number
lands, which is what makes the entry auditable.

Field numbers are Drake's HEADS-DOWN numbers (Ctrl+N reveals them on screen), read off the
Drake 2025 build on 2026-08-01. They are per-tax-year: re-verify with
`agent.py headsdown --manual` (no --seq) after a Drake update, which screenshots the numbers.

Nothing here touches Drake or files anything — it is importable and testable anywhere,
which is why the whole plan can be reviewed with `--dry-run` on any machine.
"""

from __future__ import annotations

# kind drives sanitization + how the value is entered:
#   money    strip $ , and whitespace; drop a trailing .00 (whole dollars); skipped when zero
#   ein/ssn  digits only — Drake re-formats them itself
#   zip      digits, keeping a 5+4 hyphen
#   state    2-letter code, upper-cased
#   code     short alpha code (box 12 D/DD/W…), upper-cased
#   text     names/addresses — upper-cased for Drake's convention, whitespace collapsed
#   checkbox boolean -> the checkbox token (binding: headsdown_checkbox_true, default "X");
#            FALSE is SKIPPED, never "unchecked", so we can never clear a human's entry
W2_FIELD_MAP = {
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
    # --- employee -------------------------------------------------------------
    "employee_ssn":            {"field_no": 13, "kind": "ssn",      "label": "Employee SSN",
                                "confirm": True},
    "employee_first_name":     {"field_no": 14, "kind": "text",     "label": "Employee first name",
                                "confirm": True},
    "employee_last_name":      {"field_no": 15, "kind": "text",     "label": "Employee last name",
                                "confirm": True},
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
    # --- box 12 (four slots) --------------------------------------------------
    "box12a_code":             {"field_no": 34, "kind": "code",     "label": "Box 12a code"},
    "box12a_amount":           {"field_no": 35, "kind": "money",    "label": "Box 12a amount"},
    "box12b_code":             {"field_no": 37, "kind": "code",     "label": "Box 12b code"},
    "box12b_amount":           {"field_no": 38, "kind": "money",    "label": "Box 12b amount"},
    "box12c_code":             {"field_no": 40, "kind": "code",     "label": "Box 12c code"},
    "box12c_amount":           {"field_no": 41, "kind": "money",    "label": "Box 12c amount"},
    "box12d_code":             {"field_no": 43, "kind": "code",     "label": "Box 12d code"},
    "box12d_amount":           {"field_no": 44, "kind": "money",    "label": "Box 12d amount"},
    # --- box 13 checkboxes ----------------------------------------------------
    "box13_statutory":         {"field_no": 46, "kind": "checkbox", "label": "Box 13 statutory employee",
                                "confirm": True},
    "box13_retirement":        {"field_no": 47, "kind": "checkbox", "label": "Box 13 retirement plan",
                                "confirm": True},
    "box13_third_party_sick":  {"field_no": 48, "kind": "checkbox", "label": "Box 13 third-party sick pay",
                                "confirm": True},
    # --- boxes 15-20, state/local row 1 --------------------------------------
    # Rows repeat every 7 fields: row 1 = 57-63, row 2 = 64-70, row 3 = 71-77, row 4 = 78-84.
    "box15_state":             {"field_no": 57, "kind": "state",    "label": "Box 15 state"},
    "box15_state_id":          {"field_no": 58, "kind": "text",     "label": "Box 15 employer state ID"},
    "box16_state_wages":       {"field_no": 59, "kind": "money",    "label": "Box 16 state wages"},
    "box17_state_wh":          {"field_no": 60, "kind": "money",    "label": "Box 17 state income tax"},
    "box18_local_wages":       {"field_no": 61, "kind": "money",    "label": "Box 18 local wages"},
    "box19_local_wh":          {"field_no": 62, "kind": "money",    "label": "Box 19 local income tax"},
    "box20_locality":          {"field_no": 63, "kind": "text",     "label": "Box 20 locality name"},
}

# The exact key set the LLM extractor must emit. Anything else is reported, never guessed at.
W2_SCHEMA_KEYS = tuple(W2_FIELD_MAP.keys())

# Fields whose number is a dropdown/selection on Drake's screen rather than a free-text box.
# Typing the code usually selects it, but this has NOT been confirmed on the VM — the plan
# flags them so a human looks at those boxes first.
DROPDOWN_FIELDS = {9, 34, 37, 40, 43, 57, 63}


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
    s = "".join(ch for ch in str(v) if ch.isalnum()).upper()
    return s or None


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
    if kind == "checkbox":
        return _clean_checkbox(value, checkbox_token)
    return _clean_text(value)


def build_plan(payload: dict, *, checkbox_token: str = "X",
               include_zeros: bool = False) -> dict:
    """Turn extracted W-2 JSON into an ordered, fully-resolved entry plan.

    Returns {"entries": [...], "skipped": [...], "unknown_keys": [...], "warnings": [...]}.

    Entries are sorted by field number, which puts the EIN (4) first — deliberate, so
    Drake's employer auto-fill runs BEFORE we write the employer name/address, and our
    extracted values land on top of whatever it filled in.

    Nothing is dropped silently: a key that isn't in the map, or a value that sanitizes to
    nothing, is reported so `--dry-run` shows the complete picture before anything is typed.
    """
    entries, skipped, unknown, warnings = [], [], [], []
    for key, raw in (payload or {}).items():
        if key.startswith("_"):
            continue
        spec = W2_FIELD_MAP.get(key)
        if spec is None:
            unknown.append(key)
            continue
        val = sanitize(spec["kind"], raw, checkbox_token=checkbox_token)
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
        entries.append({
            "key": key,
            "field_no": spec["field_no"],
            "label": spec["label"],
            "kind": spec["kind"],
            "value": val,
            "raw": raw,
            # Flags a human should eyeball on the first run of a new Drake build.
            "confirm": bool(spec.get("confirm")) or spec["field_no"] in DROPDOWN_FIELDS,
        })
    entries.sort(key=lambda e: e["field_no"])
    if unknown:
        warnings.append(f"{len(unknown)} key(s) not in the W-2 field map were IGNORED: "
                        f"{', '.join(sorted(unknown))}")
    dropdowns = sorted({e["field_no"] for e in entries if e["field_no"] in DROPDOWN_FIELDS})
    if dropdowns:
        warnings.append(f"field(s) {dropdowns} are dropdowns on Drake's screen — typing the "
                        f"code usually selects it, but this is UNCONFIRMED on this build; "
                        f"check those boxes on the screenshot.")
    if any(e["kind"] == "checkbox" for e in entries):
        warnings.append(f"checkbox fields are entered as {checkbox_token!r} "
                        f"(binding: navigation.headsdown_checkbox_true) — UNCONFIRMED on this "
                        f"build; if Drake rejects it the run HALTs rather than mis-entering.")
    return {"entries": entries, "skipped": skipped, "unknown_keys": unknown,
            "warnings": warnings}


def format_plan(plan: dict) -> str:
    """The plan as a human-readable table — what `--dry-run` prints for review BEFORE any
    keystroke reaches Drake."""
    lines = ["", f"{len(plan['entries'])} field(s) will be entered, in field-number order:", ""]
    lines.append(f"  {'fld':>4}  {'label':<32} {'value':<24} source key")
    lines.append(f"  {'-'*4}  {'-'*32} {'-'*24} {'-'*24}")
    for e in plan["entries"]:
        mark = " *" if e["confirm"] else "  "
        lines.append(f"  {e['field_no']:>4}{mark}{e['label']:<32} {e['value']:<24} {e['key']}")
    if any(e["confirm"] for e in plan["entries"]):
        lines.append("")
        lines.append("  * = verify this box by eye on the screenshot (dropdown, checkbox, or identity field)")
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
