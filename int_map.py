"""
1099-INT: extracted JSON -> Drake heads-down field numbers (screen INT).

Drake calls this screen "Schedule B - Interest Income (1099-INT)". It is the second form
this agent can drive, and it follows the W-2's rules exactly: the LLM's only job is to fill
the schema below, and everything after — which box a key belongs in, how the value is
formatted, what is skipped — is fixed table lookup and pure functions. No model decides
where a number lands.

FIELD NUMBERS: read box-by-box off a live Drake 2025 INT screen with heads-down numbering
switched on (`explore-int-form-headsdown.png`, captured 2026-08-11 on the test return
'123456789 - fynn, Test'). All 62 numbers are legible without ambiguity, which is why
MAX_FIELD is 62 and not a guess. Re-verify with `agent.py headsdown --screen INT --manual`
after a Drake update — it screenshots the numbers.

TWO THINGS ON THIS SCREEN THAT DO NOT EXIST ON THE W-2
  1. GRID MODE. Drake can display this screen as a spreadsheet instead of a form ("Use
     <F3> to switch to grid mode" is printed on it). The heads-down numbers below belong to
     the FORM. In grid mode they address nothing. `drake_nav.open_screen` measures which
     mode is showing and will not let a run start in the wrong one.
  2. TSJ, not TS. A bank account can genuinely be held jointly, so field 1 takes T, S or J.

PROVEN LIVE 2026-08-12: all 61 writable boxes entered, verified field by field, and read
back off the form — 61/61. Getting there took five runs and every one of them was a clean
halt that taught the map something:

  field 2   'F' is the FEDERAL code (0 or blank), not the multi-form code it is on other
            screens. Drake said so in its own words.
  field 15  a country CODE, not a name: 'CANADA' matched on 'CA' and the rest was dropped.
  field 26  the same, and it echoed 'CA  Canada' — which is how we know CA is Canada here
            and not California.
  field 58  bank interest state is FOUR states, MA/ME/OK/TN.
  field 61  a single letter, A-N, of Illinois bond categories.

TEN OF THESE BOXES ARE DROPDOWNS and all ten are now confirmed to take a typed code on this
build. That was worth measuring rather than assuming, because a Drake dropdown with no
matching entry ACCEPTS the typing, echoes it back in the heads-down popup, and stores
nothing — the per-field gate passes and the box stays empty. Fields 58 and 61 went further
and echoed a perfect-looking 'pa pa' / 'Us Us' that Drake then REJECTED outright. That is
why boxes with a known fixed list carry `values` here: the refusal happens in the plan,
before Drake is even open, and never depends on the echo.

Nothing here touches Drake or files anything.
"""

from __future__ import annotations

from form_plan import FormSpec, build_plan as _build, format_plan  # noqa: F401

SCREEN = "INT"

# Field 14 is the FOREIGN province/state box and it is not an edit box at all — it renders
# as "<Click to Access>", a sub-screen. The W-2 has two of these (11 and 20) and binding a
# key to one is how a 9-digit SSN once ended up pointed at a foreign postal-code box.
FORBIDDEN_FIELDS = {
    14: "the payer's FOREIGN province/state — it is a '<Click to Access>' sub-screen, not a "
        "box that can be typed into",
}

MAX_FIELD = 62

# Keys an extractor may legitimately read off a 1099-INT that have NO box on this screen.
# Reported by name rather than lumped into the generic unknown-key warning: a dropped
# identifier must be loud.
NOT_ON_THIS_SCREEN = {
    "recipient_street": "the INT screen has no recipient address block — it comes from screen 1.",
    "recipient_city": "the INT screen has no recipient address block — it comes from screen 1.",
    "recipient_state": "the INT screen has no recipient address block — it comes from screen 1.",
    "recipient_zip": "the INT screen has no recipient address block — it comes from screen 1.",
}

# kind drives sanitization and how the value is entered — see form_plan.sanitize.
INT_FIELD_MAP = {
    # --- header ---------------------------------------------------------------------
    "tsj":                          {"field_no": 1,  "kind": "tsj",      "label": "TSJ taxpayer/spouse/joint"},
    # The header "F" is a MULTI-FORM code on most Drake screens. It is not one here, and
    # assuming it was is what stopped the first live run: sending '1' made Drake answer
    # "Your entry is not VALID for field type: Federal Code", and its help window then held
    # the keyboard until a human clicked OK.
    #
    # Drake's own words for this box: "This is the Federal (F) code. It only affects the
    # Federal return. 0 (zero) - exclude item from Federal return, (blank) - include item on
    # Federal return." So there is exactly ONE thing that can be typed here, and `values`
    # makes anything else a loud refusal in the plan instead of a keystroke Drake rejects.
    "federal_code":                 {"field_no": 2,  "kind": "digits",   "label": "F federal code (0 = exclude)",
                                     "values": {"0"}, "confirm": True},
    "resident_state":               {"field_no": 3,  "kind": "state",    "label": "ST resident state"},
    # Field 4 is a CITY dropdown fed by Drake's own STATELIB\CITY.HLP table, the same
    # mechanism as the W-2's Box 20: what Drake stores is the CODE, not the name printed on
    # the document. Resolved through that table before anything is typed.
    "resident_city":                {"field_no": 4,  "kind": "text",     "label": "City (resident locality)"},
    "seller_financed_mortgage":     {"field_no": 5,  "kind": "checkbox", "label": "Seller-financed mortgage"},
    "do_not_update":                {"field_no": 6,  "kind": "checkbox", "label": "Do not update"},
    # --- payer information ------------------------------------------------------------
    "payer_tin":                    {"field_no": 7,  "kind": "tin",      "label": "Payer TIN"},
    "payer_tin_is_ssn":             {"field_no": 8,  "kind": "checkbox", "label": "Payer TIN is an SSN"},
    "payer_name":                   {"field_no": 9,  "kind": "text",     "label": "Payer name"},
    "payer_street":                 {"field_no": 10, "kind": "text",     "label": "Payer street"},
    "payer_city":                   {"field_no": 11, "kind": "text",     "label": "Payer city"},
    "payer_state":                  {"field_no": 12, "kind": "state",    "label": "Payer state"},
    "payer_zip":                    {"field_no": 13, "kind": "zip",      "label": "Payer ZIP"},
    # 14 is the foreign province SUB-SCREEN — see FORBIDDEN_FIELDS.
    # A COUNTRY CODE, not a country name — the same shape as the W-2's Box 20 locality.
    # Measured 2026-08-12: typing 'CANADA' into field 15 made Drake match on the first two
    # characters and drop the rest, so the popup read back 'CA' and the run refused to
    # commit a value it could not confirm. 'code' rejects a full name outright rather than
    # letting it be silently shortened into whatever entry those two letters happen to hit.
    "payer_foreign_country":        {"field_no": 15, "kind": "code",     "label": "Payer foreign country (code)",
                                     "confirm": True},
    "payer_foreign_postal":         {"field_no": 16, "kind": "text",     "label": "Payer foreign postal code"},
    "account_number":               {"field_no": 17, "kind": "text",     "label": "Account number"},
    "rtn":                          {"field_no": 18, "kind": "digits",   "label": "RTN (routing number)"},
    "fatca":                        {"field_no": 19, "kind": "checkbox", "label": "FATCA filing requirement"},
    # --- the 1099-INT boxes, left column (1-8) ----------------------------------------
    "box1_interest":                {"field_no": 20, "kind": "money",    "label": "Box 1 interest income"},
    "box2_early_withdrawal_penalty":{"field_no": 21, "kind": "money",    "label": "Box 2 early withdrawal penalty"},
    "box3_us_govt_interest":        {"field_no": 22, "kind": "money",    "label": "Box 3 U.S. government interest"},
    "box4_fed_wh":                  {"field_no": 23, "kind": "money",    "label": "Box 4 federal tax withheld"},
    "box5_investment_expenses":     {"field_no": 24, "kind": "money",    "label": "Box 5 investment expenses"},
    "box6_foreign_tax_paid":        {"field_no": 25, "kind": "money",    "label": "Box 6 foreign tax paid"},
    # Same dropdown, same rule as field 15.
    "box7_foreign_country":         {"field_no": 26, "kind": "code",     "label": "Box 7 foreign country (code)",
                                     "confirm": True},
    "box8_tax_exempt_interest":     {"field_no": 27, "kind": "money",    "label": "Box 8 tax-exempt interest"},
    # --- the 1099-INT boxes, right column (9-17) --------------------------------------
    "box9_private_activity_bond":   {"field_no": 28, "kind": "money",    "label": "Box 9 private activity bond int"},
    "box10_market_discount":        {"field_no": 29, "kind": "money",    "label": "Box 10 market discount"},
    "box11_bond_premium":           {"field_no": 30, "kind": "money",    "label": "Box 11 bond premium"},
    "box12_bond_premium_treasury":  {"field_no": 31, "kind": "money",    "label": "Box 12 bond premium, Treasury"},
    "box13_bond_premium_tax_exempt":{"field_no": 32, "kind": "money",    "label": "Box 13 bond premium, tax-exempt"},
    # A CUSIP is an IDENTIFIER: nine characters that name one security. No max_len, on
    # purpose — truncating it would not shorten a name, it would name a different security.
    # If Drake's box is narrower than the value, the run halts on the read-back and says so.
    "box14_cusip":                  {"field_no": 33, "kind": "text",     "label": "Box 14 tax-exempt bond CUSIP"},
    "box15_state":                  {"field_no": 34, "kind": "state",    "label": "Box 15 state"},
    "box16_state_id":               {"field_no": 35, "kind": "text",     "label": "Box 16 state ID number"},
    "box17_state_wh":               {"field_no": 36, "kind": "money",    "label": "Box 17 state tax withheld"},
    "box15_state_2":                {"field_no": 37, "kind": "state",    "label": "Box 15 state (row 2)"},
    "box16_state_id_2":             {"field_no": 38, "kind": "text",     "label": "Box 16 state ID number (row 2)"},
    "box17_state_wh_2":             {"field_no": 39, "kind": "money",    "label": "Box 17 state tax withheld (row 2)"},
    # --- "Amount that is:" — adjustments Drake carries to Schedule B -------------------
    "nominee_interest":             {"field_no": 40, "kind": "money",    "label": "Nominee interest"},
    "accrued_interest":             {"field_no": 41, "kind": "money",    "label": "Accrued interest"},
    "non_taxable_oid_interest":     {"field_no": 42, "kind": "money",    "label": "Non-taxable OID interest"},
    "foreign_interest":             {"field_no": 43, "kind": "money",    "label": "Foreign interest"},
    "form_1116_not_required":       {"field_no": 44, "kind": "checkbox", "label": "1116 NOT required"},
    "state_tax_exempt_interest":    {"field_no": 45, "kind": "money",    "label": "State tax-exempt interest"},
    "us_savings_bond_prev_reported":{"field_no": 46, "kind": "money",    "label": "US savings bond int prev reported"},
    "frozen_account_interest":      {"field_no": 47, "kind": "money",    "label": "Interest from a frozen account"},
    # --- "Amount of box 8 less box 13 above that is:" — amount OR percent --------------
    # Each row is one or the other. Sending both is not an error the map can catch, so the
    # pair is flagged in `notes` and the screenshot is what settles it.
    "resident_state_muni_amount":   {"field_no": 48, "kind": "money",    "label": "Resident state muni interest amt"},
    "resident_state_muni_pct":      {"field_no": 49, "kind": "pct",      "label": "Resident state muni interest %"},
    "other_state_muni_amount":      {"field_no": 50, "kind": "money",    "label": "Other state muni interest amt"},
    "other_state_muni_pct":         {"field_no": 51, "kind": "pct",      "label": "Other state muni interest %"},
    "other_tax_exempt_amount":      {"field_no": 52, "kind": "money",    "label": "Other tax-exempt interest amt"},
    "other_tax_exempt_pct":         {"field_no": 53, "kind": "pct",      "label": "Other tax-exempt interest %"},
    # --- Form 1116 / foreign tax credit -----------------------------------------------
    "ftc_accrued":                  {"field_no": 54, "kind": "checkbox", "label": "FTC accrued (not paid)"},
    "ftc_date_paid_or_accrued":     {"field_no": 55, "kind": "date",     "label": "FTC date paid or accrued",
                                     "confirm": True},
    "ftc_foreign_investment_expense": {"field_no": 56, "kind": "money",  "label": "FTC foreign investment expense"},
    "ftc_amount":                   {"field_no": 57, "kind": "money",    "label": "FTC amount", "confirm": True},
    # --- state-specific ----------------------------------------------------------------
    # NOT a general state dropdown — Drake lists exactly four. Its own words, measured
    # 2026-08-12: "State bank interest (direct entry) — Choose the state for which the bank
    # interest is being entered. MA ME OK TN".
    #
    # This box is why an echo is not proof. Typing 'PA' came back through the heads-down
    # popup as '58 pa pa', which is the same shape as a dropdown that really did select an
    # entry — and Drake refused it anyway. The per-field gate could not have caught that;
    # only the fixed list can, and it catches it before Drake is open.
    "bank_interest_state":          {"field_no": 58, "kind": "state",    "label": "Bank interest state (MA/ME/OK/TN)",
                                     "values": {"MA", "ME", "OK", "TN"}, "confirm": True},
    "bank_interest_amount":         {"field_no": 59, "kind": "money",    "label": "Bank interest amount"},
    "ia_taxable_interest":          {"field_no": 60, "kind": "money",    "label": "IA taxable interest income"},
    # A SINGLE-LETTER code. Drake's own list, shown when it rejects a value: "A - Illinois
    # Housing Development Authority bonds and notes / B - Tri-County River Valley
    # Development Authority bonds / C - Illinois Development Finance Authority bonds …"
    # running at least A through N.
    #
    # It took two live runs to pin down because BOTH failure modes appeared here. Sending
    # 'US GOVERNMENT' made Drake match on 'Us' and drop the rest, so the popup could not
    # echo what was sent and the run refused to commit. Sending 'US' got through the popup
    # — and Drake rejected it afterwards with its own window.
    #
    # `values` is every single letter, not the exact list: the list is Illinois-specific
    # and the window scrolled past N, so a shorter set risks refusing a code Drake would
    # have taken. One letter is what was actually measured; that is what is enforced.
    "il_schedule_m_source":         {"field_no": 61, "kind": "code",     "label": "IL Schedule M interest source (A-N)",
                                     "values": {chr(c) for c in range(ord("A"), ord("Z") + 1)},
                                     "confirm": True},
    "llc_number":                   {"field_no": 62, "kind": "digits",   "label": "LLC #"},
}

# Boxes that are a SELECTION on Drake's screen, not a free-text box. Read off the reference
# screenshot by their dropdown arrow.
DROPDOWN_FIELDS = {1, 3, 4, 12, 15, 26, 34, 37, 58, 61}

# Confirmed by a live run on 2026-08-12: each of these read back as the typed code PLUS the
# entry Drake selected ('T T', 'PA PA', 'PL PL'), and the value was then readable on the
# form. Field 26 went further and echoed the entry by name ('CA  Canada'), which is what
# settled that CA means Canada and not California on Drake's country list.
#
# A dropdown earns a place here only by being observed. Field 58 is the reason that rule is
# strict: it echoed 'pa pa' — indistinguishable from a real selection — and Drake still
# rejected the value. It is NOT here. Field 61 is simply not reached yet.
DROPDOWNS_CONFIRMED: set = {1, 3, 4, 12, 15, 26, 34, 37, 58, 61}

# field 4 -> the payload key holding the state that locality belongs to. A city code means
# nothing without its state: Drake's table is keyed on both.
LOCALITY_FIELDS = {4: "resident_state"}

NOTES = [
    "the three amount/percent pairs (48/49, 50/51, 52/53) are alternatives — Drake expects "
    "one or the other on each row. Nothing here can tell which one a document meant, so if "
    "both arrive both are typed; check those rows on the screenshot.",
    "field 57 (FTC) is a narrow box whose contents have not been read back on this build. "
    "It is entered as an amount and flagged — confirm it on the screenshot.",
]

INT_SPEC = FormSpec(
    screen=SCREEN,
    label="1099-INT — Schedule B, Interest Income",
    fields=INT_FIELD_MAP,
    max_field=MAX_FIELD,
    not_on_screen=NOT_ON_THIS_SCREEN,
    forbidden=FORBIDDEN_FIELDS,
    dropdowns=DROPDOWN_FIELDS,
    dropdowns_confirmed=DROPDOWNS_CONFIRMED,
    locality_fields=LOCALITY_FIELDS,
    # Whose return this is. The INT screen has no recipient block at all — the recipient is
    # the return — so these are navigation inputs, already proven against Drake's own window
    # title before a key was pressed, not values that failed to find a box.
    identity_keys={"recipient_tin", "recipient_ssn", "recipient_name",
                   "recipient_first_name", "recipient_last_name",
                   "client_ssn", "client_first_name", "client_last_name"},
    # One INT record is one PAYER. Entering the same payer twice doubles the client's
    # reported interest, and every read-back would verify perfectly — so the payer TIN is
    # what a second run is refused on.
    dedupe_key="payer_tin",
    record_noun="1099-INT",
    notes=NOTES,
)

INT_SCHEMA_KEYS = INT_SPEC.schema_keys


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Extracted 1099-INT JSON -> an ordered, fully-resolved entry plan.

    `ts` is the operator's --ts flag. It fills field 1 when the payload does not carry a
    TSJ of its own — whose return this is, is a property of the RETURN and is not printed
    anywhere on a 1099-INT.
    """
    extra = {}
    if ts is not None and not str((payload or {}).get("tsj") or "").strip():
        extra["tsj"] = ts
    plan = _build(payload, INT_SPEC, checkbox_token=checkbox_token,
                  include_zeros=include_zeros, skip_fields=skip_fields, extra=extra)
    if not any(e["field_no"] == 1 for e in plan["entries"]):
        plan["warnings"].insert(0,
            "TSJ (field 1) was not supplied — Drake will use its default. On a JOINT return "
            "that files a spouse's or a joint account's interest under the taxpayer alone. "
            "Pass --ts T|S (or put 'tsj' in the payload; this screen also accepts J).")
    return plan
