"""
1099-R: extracted JSON -> Drake heads-down field numbers (screen 1099).

Drake calls this screen "Form 1099-R - Pensions, Annuities, Retirement, Profit-Sharing,
IRAs, Insurance Contracts, etc." It is the fourth form this agent can drive, and it follows
the same rules as the others: the LLM's only job is to fill the schema below, and everything
after — which box a key belongs in, how the value is formatted, what is skipped — is fixed
table lookup and pure functions. No model decides where a number lands.

FIELD NUMBERS: read STRUCTURALLY off a live Drake 2025 screen on 2026-08-13 (test return
'123456789 - fynn, Test', dump `explore-1099r-form.json`, screenshot `r-screen.png`). Every
control carries an automation id encoding its heads-down number and its kind. 73 boxes,
1..73, no gaps and none forbidden — unlike the INT and DIV screens, the foreign province box
here is an ordinary text box rather than a "<Click to Access>" sub-screen.

FOUR THINGS ON THIS SCREEN THAT THE OTHERS DO NOT HAVE

  1. TS, NOT TSJ. Field 1 offers T and S only — no J. A pension belongs to one person, so
     there is no joint option, and the frontend must not offer one either.
  2. BOX 7 IS TWO BOXES. The distribution code is fields 33 AND 34, each a separate
     single-character dropdown offering the same 29 codes. A code printed as '1B' is '1' in
     one and 'B' in the other. Anything that hands over "1B" as one string has to SPLIT it —
     see `split_distribution_code` below. Getting this wrong is not cosmetic: the code
     decides whether the 10% early-withdrawal penalty applies, whether the money is a
     non-taxable rollover, or whether it is a death benefit.
  3. AN OVERRIDE BLOCK. Fields 16-24 are the recipient's name and address "if different from
     screen 1", and they are OVERRIDE boxes — Drake prints '=' beside each one. A value there
     replaces what the return already holds. That is a preparer's decision, not an
     extraction's, which is why they are declared not-on-the-document on the bridge.
  4. SYMBOL CODES. Field 3 (pension type) offers 44 codes and seven of them are symbols:
     @ # * % & $ = . They are real Drake codes for AZ, CT, PA, NY and MD.

PROVEN LIVE 2026-08-14: all 73 boxes entered, verified field by field, and read back off the
form — 73/73 on the first run, no halts, with all thirteen dropdowns selecting a real entry
and all thirteen checkboxes ticked from clear.

That it took one run rather than the INT screen's six is not luck. Every dropdown list here
was READ OUT OF THE CONTROL before anything was typed, which is what turned the INT screen's
four separate live halts into a single measurement pass. A Drake dropdown with no matching
entry ACCEPTS the typing, echoes it back in the heads-down popup, and stores nothing; on the
INT screen two boxes went further and echoed a perfect-looking 'pa pa' that Drake then
REJECTED outright. So the echo is never proof — the form check is.

Nothing here touches Drake or files anything.
"""

from __future__ import annotations

from form_plan import FormSpec, build_plan as _build, format_plan  # noqa: F401

SCREEN = "1099"

# Nothing on this screen is a sub-screen or a greyed-out box: all 73 numbered controls are
# writable. Kept (empty) so the import-time check still runs and so the reason is on record.
FORBIDDEN_FIELDS: dict = {}

MAX_FIELD = 73

# Box 7 distribution codes, read out of BOTH dropdowns (33 and 34) on 2026-08-13. Twenty-nine
# of them; I, O, V, X, Y and Z are not offered.
DIST_CODES = {"1", "2", "3", "4", "5", "6", "7", "8", "9",
              "A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M", "N",
              "P", "Q", "R", "S", "T", "U", "W"}

# Field 3 "Pension type", read out of the control: 44 codes, seven of them symbols. Note that
# Drake lists 'Z' TWICE — once as "City Government" and once as "KS - KPERS" — so the code
# alone does not identify the entry. Nothing here can resolve that; it is a preparer's box.
PENSION_TYPE_CODES = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") | set("#$%&*=@")

# Field 66, read out of the control.
ROLLOVER_CODES = {"C", "G", "S", "X"}

# Keys an extractor may legitimately read off a 1099-R that have NO box on this screen.
NOT_ON_THIS_SCREEN: dict = {}

# kind drives sanitization and how the value is entered — see form_plan.sanitize.
R_FIELD_MAP = {
    # --- header -----------------------------------------------------------------------
    # T or S ONLY. `ts` is the W-2's kind and it REJECTS 'J' rather than downgrading it,
    # which is exactly right here: a pension is one person's.
    "ts":                           {"field_no": 1,  "kind": "ts",       "label": "TS taxpayer/spouse"},
    # The header "F" is a MULTI-FORM code on most Drake screens and is NOT one on Schedule B,
    # where sending '1' made Drake answer "Your entry is not VALID for field type: Federal
    # Code". Carried across on the same assumption and NOT yet measured here; `values` is the
    # safe direction either way.
    "federal_code":                 {"field_no": 2,  "kind": "digits",   "label": "F federal code (0 = exclude)",
                                     "values": {"0"}, "confirm": True},
    # Drake's own state-treatment code for the distribution — 44 entries covering AL, AR, AZ,
    # CT, HI, IA, KS, LA, MD, MI, NC, NY and PA rules, plus G/M/S/Z/R/C for the payer type.
    # Nothing on a 1099-R implies it; it is a preparer's classification.
    "pension_type":                 {"field_no": 3,  "kind": "code_an",  "label": "Pension type (Drake code)",
                                     "values": PENSION_TYPE_CODES, "confirm": True},
    "corrected_1099r":              {"field_no": 4,  "kind": "checkbox", "label": "Corrected 1099-R"},
    # --- payer information (required for e-file) ----------------------------------------
    "payer_tin":                    {"field_no": 5,  "kind": "tin",      "label": "Payer TIN"},
    "payer_name":                   {"field_no": 6,  "kind": "text",     "label": "Payer name"},
    "payer_name_cont":              {"field_no": 7,  "kind": "text",     "label": "Payer name continued"},
    "payer_street":                 {"field_no": 8,  "kind": "text",     "label": "Payer street"},
    "payer_city":                   {"field_no": 9,  "kind": "text",     "label": "Payer city"},
    "payer_state":                  {"field_no": 10, "kind": "state",    "label": "Payer state"},
    "payer_zip":                    {"field_no": 11, "kind": "zip",      "label": "Payer ZIP"},
    # An ordinary text box on THIS screen. On the INT and DIV screens the equivalent is a
    # "<Click to Access>" sub-screen that cannot be typed into at all.
    "payer_foreign_province":       {"field_no": 12, "kind": "text",     "label": "Payer foreign province/state"},
    # An IRS country CODE, not a name, and not ISO 3166 — the same 259-entry list as the DIV
    # screen, where Australia is AS and Austria is AU.
    "payer_foreign_country":        {"field_no": 13, "kind": "code",     "label": "Payer foreign country (code)",
                                     "confirm": True},
    "payer_foreign_postal":         {"field_no": 14, "kind": "text",     "label": "Payer foreign postal code"},
    "payer_phone":                  {"field_no": 15, "kind": "digits",   "label": "Payer phone"},
    # --- recipient OVERRIDE block (fields 16-24) ----------------------------------------
    # "Recipient's Name and Address (if different from screen 1)". Drake prints '=' beside
    # each of these: a value here REPLACES what the return already holds. Writing one when
    # the addresses agree is a change nobody asked for, so they are never bridged from an
    # extraction — they exist here so the screen can be proven end to end.
    "recipient_first_name_override": {"field_no": 16, "kind": "text",    "label": "Recipient first name (OVERRIDE)",
                                      "confirm": True},
    "recipient_last_name_override": {"field_no": 17, "kind": "text",     "label": "Recipient last name (OVERRIDE)",
                                     "confirm": True},
    "recipient_street_override":    {"field_no": 18, "kind": "text",     "label": "Recipient street (OVERRIDE)",
                                     "confirm": True},
    "recipient_city_override":      {"field_no": 19, "kind": "text",     "label": "Recipient city (OVERRIDE)",
                                     "confirm": True},
    "recipient_state_override":     {"field_no": 20, "kind": "state",    "label": "Recipient state (OVERRIDE)",
                                     "confirm": True},
    "recipient_zip_override":       {"field_no": 21, "kind": "zip",      "label": "Recipient ZIP (OVERRIDE)",
                                     "confirm": True},
    "recipient_foreign_province_override": {"field_no": 22, "kind": "text",
                                            "label": "Recipient foreign province (OVERRIDE)", "confirm": True},
    "recipient_foreign_country_override": {"field_no": 23, "kind": "code",
                                           "label": "Recipient foreign country (OVERRIDE)", "confirm": True},
    "recipient_foreign_postal_override": {"field_no": 24, "kind": "text",
                                          "label": "Recipient foreign postal (OVERRIDE)", "confirm": True},
    # --- the 1099-R boxes ----------------------------------------------------------------
    "box1_gross_distribution":      {"field_no": 25, "kind": "money",    "label": "Box 1 gross distribution"},
    "box2a_taxable":                {"field_no": 26, "kind": "money",    "label": "Box 2a taxable amount"},
    "box2b_taxable_not_determined": {"field_no": 27, "kind": "checkbox", "label": "Box 2b taxable amt not determined"},
    "box2b_total_distribution":     {"field_no": 28, "kind": "checkbox", "label": "Box 2b total distribution"},
    "box3_capital_gain":            {"field_no": 29, "kind": "money",    "label": "Box 3 capital gain in box 2a"},
    "box4_fed_wh":                  {"field_no": 30, "kind": "money",    "label": "Box 4 federal tax withheld"},
    "box5_employee_contrib":        {"field_no": 31, "kind": "money",    "label": "Box 5 employee contrib/ins prem"},
    "box6_unrealized_appreciation": {"field_no": 32, "kind": "money",    "label": "Box 6 unrealized appreciation"},
    # BOX 7 IS TWO SEPARATE SINGLE-CHARACTER DROPDOWNS. See the module docstring and
    # `split_distribution_code`.
    "box7_dist_code":               {"field_no": 33, "kind": "code_an",  "label": "Box 7 distribution code (1st)",
                                     "values": DIST_CODES, "confirm": True},
    "box7_dist_code_2":             {"field_no": 34, "kind": "code_an",  "label": "Box 7 distribution code (2nd)",
                                     "values": DIST_CODES, "confirm": True},
    "box7_ira_sep_simple":          {"field_no": 35, "kind": "checkbox", "label": "Box 7 IRA/SEP/SIMPLE"},
    "box8_other_amount":            {"field_no": 36, "kind": "money",    "label": "Box 8 other, amount"},
    "box8_other_pct":               {"field_no": 37, "kind": "pct",      "label": "Box 8 other, percent"},
    "box9a_pct_of_total":           {"field_no": 38, "kind": "pct",      "label": "Box 9a percent of total distr"},
    "box9b_total_employee_contrib": {"field_no": 39, "kind": "money",    "label": "Box 9b total employee contrib"},
    "box10_irr_within_5_years":     {"field_no": 40, "kind": "money",    "label": "Box 10 allocable to IRR in 5 yrs"},
    # A four-digit YEAR in a 51-pixel box. `max_len` refuses anything longer rather than
    # trimming it, because cutting a number changes it — measured on the DIV screen, where
    # Drake took '601' from '6010'.
    "box11_first_year_roth":        {"field_no": 41, "kind": "digits",   "label": "Box 11 first year of Roth contrib",
                                     "max_len": 4, "confirm": True},
    "box12_fatca":                  {"field_no": 42, "kind": "checkbox", "label": "Box 12 FATCA filing requirement"},
    # --- state and local rows (the screen prints two of each) ----------------------------
    "box14_state_wh":               {"field_no": 43, "kind": "money",    "label": "Box 14 state tax withheld"},
    "box15_state":                  {"field_no": 44, "kind": "state",    "label": "Box 15 state"},
    "box15_payer_state_number":     {"field_no": 45, "kind": "text",     "label": "Box 15 payer state number"},
    "box16_state_distribution":     {"field_no": 46, "kind": "money",    "label": "Box 16 state distribution"},
    "box17_local_wh":               {"field_no": 47, "kind": "money",    "label": "Box 17 local tax withheld"},
    # A locality DROPDOWN fed by Drake's own STATELIB\CITY.HLP table, the same mechanism as
    # the W-2's Box 20: what Drake stores is the CODE, not the name printed on the document.
    # Measured as empty until its state is set — the table is keyed on both.
    "box18_locality_name":          {"field_no": 48, "kind": "text",     "label": "Box 18 locality name"},
    "box19_local_distribution":     {"field_no": 49, "kind": "money",    "label": "Box 19 local distribution"},
    "box14_state_wh_2":             {"field_no": 50, "kind": "money",    "label": "Box 14 state tax withheld (row 2)"},
    "box15_state_2":                {"field_no": 51, "kind": "state",    "label": "Box 15 state (row 2)"},
    "box15_payer_state_number_2":   {"field_no": 52, "kind": "text",     "label": "Box 15 payer state no. (row 2)"},
    "box16_state_distribution_2":   {"field_no": 53, "kind": "money",    "label": "Box 16 state distr (row 2)"},
    "box17_local_wh_2":             {"field_no": 54, "kind": "money",    "label": "Box 17 local tax withheld (row 2)"},
    "box18_locality_name_2":        {"field_no": 55, "kind": "text",     "label": "Box 18 locality name (row 2)"},
    "box19_local_distribution_2":   {"field_no": 56, "kind": "money",    "label": "Box 19 local distr (row 2)"},
    "account_number":               {"field_no": 57, "kind": "text",     "label": "Account number"},
    # --- "Additional Information for this Distribution" — Drake's own elections -----------
    # Every one of these is a preparer's determination about how the distribution is TREATED,
    # not a fact printed on the form. Several change the tax directly.
    "disability_1099r":             {"field_no": 58, "kind": "checkbox", "label": "1099-R for disability"},
    "disability_reported_as_wages": {"field_no": 59, "kind": "checkbox", "label": "...reported as wages on the 1040"},
    "carry_to_form_5329":           {"field_no": 60, "kind": "checkbox", "label": "Carry to Form 5329 (10% penalty)"},
    "exclude_reported_on_4972":     {"field_no": 61, "kind": "checkbox", "label": "Exclude — reported on Form 4972"},
    "exclude_reported_on_8606":     {"field_no": 62, "kind": "checkbox", "label": "Exclude — reported on Form 8606"},
    "altered_or_handwritten":       {"field_no": 63, "kind": "checkbox", "label": "1099-R altered or handwritten"},
    "do_not_update":                {"field_no": 64, "kind": "checkbox", "label": "Do not update screen to 2026"},
    "no_distribution_received":     {"field_no": 65, "kind": "checkbox", "label": "No distribution received"},
    # --- rollover and conversion ----------------------------------------------------------
    "rollover_type":                {"field_no": 66, "kind": "code_an",
                                     "label": "Rollover/conversion (C/G/S/X)",
                                     "values": ROLLOVER_CODES, "confirm": True},
    "partial_rollover_amount":      {"field_no": 67, "kind": "money",    "label": "Partial rollover/conversion amt"},
    "box13_date_of_payment":        {"field_no": 68, "kind": "date",     "label": "Box 13 date of payment",
                                     "confirm": True},
    "date_of_retirement":           {"field_no": 69, "kind": "date",     "label": "Date of retirement",
                                     "confirm": True},
    # --- state exclusion (percent OR amount on each row) ----------------------------------
    # Measured by box width: 64 pixels is the percent column, 128 the amount column.
    "state_exclude_pct":            {"field_no": 70, "kind": "pct",      "label": "Portion to exclude on state, %"},
    "state_exclude_amount":         {"field_no": 71, "kind": "money",    "label": "Portion to exclude on state, amt"},
    "state_not_qualifying_pct":     {"field_no": 72, "kind": "pct",      "label": "Portion NOT qualifying, %"},
    "state_not_qualifying_amount":  {"field_no": 73, "kind": "money",    "label": "Portion NOT qualifying, amt"},
}

# Boxes that are a SELECTION on Drake's screen, not a free-text box. Derived structurally
# from the `Dropdown_<n>` and `DropdownOverride_<n>` automation ids.
DROPDOWN_FIELDS = {1, 3, 10, 13, 20, 23, 33, 34, 44, 48, 51, 55, 66}

# Confirmed by the live run on 2026-08-14: all thirteen read back as the typed code and were
# then READABLE ON THE FORM, including both halves of Box 7 (field 33 showing '1', field 34
# showing 'B') and both locality boxes (48 'PL', 55 'COLUMBUS').
#
# A dropdown earns a place here only by being OBSERVED on the form, never by its list having
# been read. Field 58 on the INT screen is why that rule is strict: it echoed a
# perfect-looking 'pa pa' and Drake rejected the value anyway.
DROPDOWNS_CONFIRMED: set = {1, 3, 10, 13, 20, 23, 33, 34, 44, 48, 51, 55, 66}

# field -> the payload key holding the state that locality belongs to. A locality code means
# nothing without its state: Drake's table is keyed on both.
LOCALITY_FIELDS = {48: "box15_state", 55: "box15_state_2"}

NOTES = [
    "BOX 7 IS TWO BOXES on this screen (fields 33 and 34), each a single character. A code "
    "printed as '1B' is '1' in the first and 'B' in the second — use "
    "r_map.split_distribution_code() rather than sending the pair as one string. The code "
    "decides whether the 10% early-withdrawal penalty applies, so a dropped second "
    "character is a wrong tax, not a cosmetic loss.",
    "field 1 is TS, not TSJ — Drake offers T and S only. A pension belongs to one person.",
    "fields 16-24 are OVERRIDE boxes for the recipient's name and address: a value there "
    "REPLACES what the return already holds from screen 1. Drake prints '=' beside each. "
    "They are only for a document whose address genuinely differs.",
    "the amount/percent pairs (36/37, 70/71, 72/73) are alternatives — Drake expects one or "
    "the other on each row. Nothing here can tell which one a document meant.",
    "fields 58-65 are Drake's own elections about how the distribution is TREATED (Form "
    "4972, Form 8606, Form 5329 and the 10% penalty). None is printed on a 1099-R and "
    "several change the tax directly — they are a preparer's determination.",
    "field 3 (pension type) has 44 codes and Drake lists 'Z' TWICE, once as City Government "
    "and once as KS-KPERS. The code alone does not identify the entry; check it by eye.",
]

R_SPEC = FormSpec(
    screen=SCREEN,
    label="1099-R — Pensions, Annuities, Retirement",
    fields=R_FIELD_MAP,
    max_field=MAX_FIELD,
    not_on_screen=NOT_ON_THIS_SCREEN,
    forbidden=FORBIDDEN_FIELDS,
    dropdowns=DROPDOWN_FIELDS,
    dropdowns_confirmed=DROPDOWNS_CONFIRMED,
    locality_fields=LOCALITY_FIELDS,
    # Whose return this is. The recipient block on this screen is an OVERRIDE of screen 1,
    # not the identity itself, so these remain navigation inputs — already proven against
    # Drake's own window title before a key is pressed.
    identity_keys={"recipient_tin", "recipient_ssn", "recipient_name",
                   "recipient_first_name", "recipient_last_name",
                   "client_ssn", "client_first_name", "client_last_name"},
    # One 1099-R record is one PAYER. Entering the same one twice doubles the client's
    # reported pension income, and every read-back would verify perfectly.
    dedupe_key="payer_tin",
    record_noun="1099-R",
    notes=NOTES,
)

R_SCHEMA_KEYS = R_SPEC.schema_keys


def split_distribution_code(raw):
    """'1B' -> ('1', 'B'). Box 7 is two single-character dropdowns, not one two-character box.

    Returns (first, second) with second None when only one code was given, or (None, None)
    when the input is not a usable code. Refuses rather than guesses: a three-character
    string is not two codes, and silently keeping the first two would drop a code that
    changes how the distribution is taxed.
    """
    s = "".join(str(raw or "").split()).upper()
    if not s:
        return None, None
    if len(s) == 1:
        return (s, None) if s in DIST_CODES else (None, None)
    if len(s) == 2 and s[0] in DIST_CODES and s[1] in DIST_CODES:
        return s[0], s[1]
    return None, None


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Extracted 1099-R JSON -> an ordered, fully-resolved entry plan.

    `ts` is the operator's --ts flag. It fills field 1 when the payload does not carry one of
    its own — whose return this is, is a property of the RETURN and is not printed on a
    1099-R. This screen takes T or S only.

    A combined `box7_dist_code` such as '1B' is split across fields 33 and 34 here, so a
    caller that only knows the printed code does not have to know the screen's shape.
    """
    payload = dict(payload or {})
    extra = {}
    if ts is not None and not str(payload.get("ts") or "").strip():
        extra["ts"] = ts

    warn_split = None
    combined = str(payload.get("box7_dist_code") or "").strip()
    if len(combined.replace(" ", "")) > 1 and not str(payload.get("box7_dist_code_2") or "").strip():
        first, second = split_distribution_code(combined)
        if first:
            payload["box7_dist_code"] = first
            if second:
                payload["box7_dist_code_2"] = second
            warn_split = (f"Box 7 arrived as {combined!r} and was split across the two boxes "
                          f"Drake has: field 33 = {first!r}"
                          + (f", field 34 = {second!r}" if second else "") + ".")
        else:
            warn_split = (f"Box 7 arrived as {combined!r}, which is not one or two of Drake's "
                          f"distribution codes. It was NOT entered — key it by hand.")
            payload.pop("box7_dist_code", None)

    plan = _build(payload, R_SPEC, checkbox_token=checkbox_token,
                  include_zeros=include_zeros, skip_fields=skip_fields, extra=extra)
    if warn_split:
        plan["warnings"].insert(0, warn_split)
    if not any(e["field_no"] == 1 for e in plan["entries"]):
        plan["warnings"].insert(0,
            "TS (field 1) was not supplied — Drake will use its default. On a JOINT return "
            "that files a spouse's pension under the taxpayer. Pass --ts T|S; this screen "
            "does NOT accept J.")
    return plan
