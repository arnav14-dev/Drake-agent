"""
1099-DIV: extracted JSON -> Drake heads-down field numbers (screen DIV).

Drake calls this screen "Schedule B - Dividend Income (1099-DIV)". It is the third form this
agent can drive, and it follows the same rules as the W-2 and the INT: the LLM's only job is
to fill the schema below, and everything after — which box a key belongs in, how the value
is formatted, what is skipped — is fixed table lookup and pure functions. No model decides
where a number lands.

FIELD NUMBERS: read STRUCTURALLY off a live Drake 2025 DIV screen on 2026-08-12 (test return
'123456789 - fynn, Test', dump `explore-div-form.json`, screenshot `div-screen.png`). Every
control on this screen carries an automation id that ENCODES its heads-down number and its
kind — `Textbox_23`, `Dropdown_22`, `CheckboxTextRight_17`. That is a measurement, not a
reading of pixels, and it is why MAX_FIELD is 73 and the dropdown set below is exact.

The method was validated against int_map.py before it was trusted here: run against the INT
dump it reproduces that map's 62 boxes and its ten dropdowns {1,3,4,12,15,26,34,37,58,61}
exactly — the same ten it took four live halts to discover one at a time.

WHAT THIS SCREEN DOES THAT THE INT SCREEN DOES NOT

  1. FOUR COLUMNS, not one. Boxes 1a/1b/2a repeat across `Total`, `Foreign Amount`,
     `Foreign Percent` and `Nominee Amount`, and 2b/2c/2d repeat under Nominee. So one
     printed box can be up to four different field numbers, and they are NOT adjacent —
     Box 1a is field 18 (Total), 43 (Foreign Amt), 47 (Foreign %) and 51 (Nominee).
  2. Box 2c has TWO controls: a dropdown (22) for which kind of Section 1202 stock, then
     the amount (23). Nothing on the INT screen looks like this.
  3. The numbering DIVERGES FROM INT AT FIELD 5. On the INT screen field 5 is
     "Seller-financed mortgage"; here it is "Do not update". Nothing carries over.

PROVEN LIVE 2026-08-12: all 72 writable boxes entered, verified field by field, and read
back off the form — 72/72, with all ten dropdowns selecting a real entry. It took two runs,
and the first halted CLEAN at field 68 having taught the map something no amount of reading
would have: that box holds three characters, and Drake took '601' from the '6010' it was
sent. See the comment on field 68 — the same box exists on the INT screen and was mis-mapped
there too.

DROPDOWNS: ten of them, all now confirmed. That was worth measuring rather than assuming,
because a Drake dropdown with no matching entry ACCEPTS the typing, echoes it back in the
heads-down popup, and stores nothing; on the INT screen two boxes went further and echoed a
perfect-looking 'pa pa' that Drake then REJECTED outright. So the echo is not proof, and
boxes whose list is known carry `values` here — the refusal then happens in the plan, before
Drake is even open, and never depends on the echo.

WHAT EACH DROPDOWN ACCEPTS WAS READ OUT OF THE CONTROL, not inferred. On 2026-08-12 each
combo was expanded through UIA and its entries recorded (`div_dropdown_items.json`). That is
a read, not a keystroke, and it answers in one pass the question that cost the INT screen
four separate live halts. It also caught a defect in the INT map — see field 71.

Nothing here touches Drake or files anything.
"""

from __future__ import annotations

from form_plan import FormSpec, build_plan as _build, format_plan  # noqa: F401

SCREEN = "DIV"

# Field 13 is the payer's FOREIGN province/state and it is not an edit box at all — it
# renders as "<Click to Access>", a sub-screen. The INT screen has the same trap at field 14
# and the W-2 has two of them; binding a key to one is how a 9-digit SSN once ended up
# pointed at a foreign postal-code box.
FORBIDDEN_FIELDS = {
    13: "the payer's FOREIGN province/state — it is a '<Click to Access>' sub-screen, not a "
        "box that can be typed into",
}

MAX_FIELD = 73

# Keys an extractor may legitimately read off a 1099-DIV that have NO box on this screen.
# Reported by name rather than lumped into the generic unknown-key warning: a dropped
# identifier must be loud.
NOT_ON_THIS_SCREEN = {
    "recipient_street": "the DIV screen has no recipient address block — it comes from screen 1.",
    "recipient_city": "the DIV screen has no recipient address block — it comes from screen 1.",
    "recipient_state": "the DIV screen has no recipient address block — it comes from screen 1.",
    "recipient_zip": "the DIV screen has no recipient address block — it comes from screen 1.",
}

# kind drives sanitization and how the value is entered — see form_plan.sanitize.
DIV_FIELD_MAP = {
    # --- header -----------------------------------------------------------------------
    "tsj":                          {"field_no": 1,  "kind": "tsj",      "label": "TSJ taxpayer/spouse/joint"},
    # The header "F" is a MULTI-FORM code on most Drake screens and is NOT one on Schedule B:
    # on the INT screen sending '1' here made Drake answer "Your entry is not VALID for field
    # type: Federal Code", and its help window then held the keyboard until a human clicked
    # OK. Drake's own words there: "0 (zero) - exclude item from Federal return, (blank) -
    # include item on Federal return."
    #
    # Carried across to this screen because it is the same Schedule B header, but NOT yet
    # measured here. `values` is the safe direction either way: if Drake accepts more, we
    # lose only the ability to send it; if it accepts only 0, the plan refuses loudly instead
    # of the run halting on a validation window.
    "federal_code":                 {"field_no": 2,  "kind": "digits",   "label": "F federal code (0 = exclude)",
                                     "values": {"0"}, "confirm": True},
    "resident_state":               {"field_no": 3,  "kind": "state",    "label": "ST resident state"},
    # Field 4 is a CITY dropdown fed by Drake's own STATELIB\CITY.HLP table, the same
    # mechanism as the W-2's Box 20: what Drake stores is the CODE, not the name printed on
    # the document. Resolved through that table before anything is typed. Measured as
    # DISABLED until field 3 holds a state — the locality is keyed on both.
    "resident_city":                {"field_no": 4,  "kind": "text",     "label": "City (resident locality)"},
    "do_not_update":                {"field_no": 5,  "kind": "checkbox", "label": "Do not update"},
    # --- payer information --------------------------------------------------------------
    "payer_tin":                    {"field_no": 6,  "kind": "tin",      "label": "Payer TIN"},
    "payer_tin_is_ssn":             {"field_no": 7,  "kind": "checkbox", "label": "Payer TIN is an SSN"},
    "payer_name":                   {"field_no": 8,  "kind": "text",     "label": "Payer name"},
    "payer_street":                 {"field_no": 9,  "kind": "text",     "label": "Payer street"},
    "payer_city":                   {"field_no": 10, "kind": "text",     "label": "Payer city"},
    "payer_state":                  {"field_no": 11, "kind": "state",    "label": "Payer state"},
    "payer_zip":                    {"field_no": 12, "kind": "zip",      "label": "Payer ZIP"},
    # 13 is the foreign province SUB-SCREEN — see FORBIDDEN_FIELDS.
    # A COUNTRY CODE, not a country name. Measured on the INT screen 2026-08-12: typing
    # 'CANADA' made Drake match on the first two characters and drop the rest, so the popup
    # read back 'CA' and the run refused to commit a value it could not confirm. 'code'
    # rejects a full name outright rather than letting it be silently shortened into whatever
    # entry those two letters happen to hit.
    #
    # The list was read out of this control on 2026-08-12: 259 entries, and they are the IRS
    # country codes, NOT ISO 3166. They differ on real countries — Australia is AS (ISO: AU),
    # Algeria is AG (ISO: DZ), Austria is AU (ISO: AT). So an ISO code from an extractor can
    # land on a DIFFERENT COUNTRY that Drake will happily accept. 'CA   Canada' is in the
    # list and there is no California in it, which is what settles that CA means Canada here.
    # No `values` set: 259 codes belong in a lookup, not in a map, and the wrong-country risk
    # is about the SOURCE of the code, which a membership test cannot see.
    "payer_foreign_country":        {"field_no": 14, "kind": "code",     "label": "Payer foreign country (code)",
                                     "confirm": True},
    "payer_foreign_postal":         {"field_no": 15, "kind": "text",     "label": "Payer foreign postal code"},
    "account_number":               {"field_no": 16, "kind": "text",     "label": "Account number"},
    "fatca":                        {"field_no": 17, "kind": "checkbox", "label": "Box 11 FATCA filing requirement"},
    # --- the 1099-DIV boxes, "Total" column ---------------------------------------------
    "box1a_ordinary_dividends":     {"field_no": 18, "kind": "money",    "label": "Box 1a ordinary dividends"},
    "box1b_qualified_dividends":    {"field_no": 19, "kind": "money",    "label": "Box 1b qualified dividends"},
    "box2a_total_capital_gain":     {"field_no": 20, "kind": "money",    "label": "Box 2a total capital gain distr"},
    "box2b_unrecaptured_1250":      {"field_no": 21, "kind": "money",    "label": "Box 2b 25% rate (unrecap 1250)"},
    # Box 2c is TWO controls: this dropdown says WHICH KIND of Section 1202 (qualified small
    # business) stock the gain came from, and that code is what decides how much of the gain
    # is excluded from tax. Getting it wrong is a wrong tax, not a cosmetic slip.
    #
    # Drake's list, read out of the control itself on 2026-08-12:
    #     Q1 - QSB stock 50% acquired after 08/10/1993 (Default)
    #     Q3 - QSB stock 75% acquired from 02/18/2009 to 09/27/2010
    #     Q4 - QSB stock 100% acquired after 09/27/2010
    # There is no Q2. Anything else is refused in the plan, before Drake is open.
    # kind is `code_an`, not `code`: these codes carry a DIGIT, and `code` is the W-2's Box
    # 12 sanitizer which rejects digits on purpose ('D 23' must not become 'D').
    "box2c_section_1202_type":      {"field_no": 22, "kind": "code_an",
                                     "label": "Box 2c Section 1202 stock type (Q1/Q3/Q4)",
                                     "values": {"Q1", "Q3", "Q4"}, "confirm": True},
    "box2c_section_1202_gain":      {"field_no": 23, "kind": "money",    "label": "Box 2c Section 1202 gain"},
    "box2d_collectibles_gain":      {"field_no": 24, "kind": "money",    "label": "Box 2d collectibles (28%) gain"},
    "box2e_section_897_ordinary":   {"field_no": 25, "kind": "money",    "label": "Box 2e Section 897 ordinary div"},
    "box2f_section_897_capital":    {"field_no": 26, "kind": "money",    "label": "Box 2f Section 897 capital gain"},
    "box3_nondividend_distributions": {"field_no": 27, "kind": "money",  "label": "Box 3 nondividend distributions"},
    "box4_fed_wh":                  {"field_no": 28, "kind": "money",    "label": "Box 4 federal tax withheld"},
    "box5_section_199a":            {"field_no": 29, "kind": "money",    "label": "Box 5 Section 199A dividends"},
    "box6_investment_expenses":     {"field_no": 30, "kind": "money",    "label": "Box 6 investment expenses"},
    "box7_foreign_tax_paid":        {"field_no": 31, "kind": "money",    "label": "Box 7 foreign tax paid"},
    # Same country-code dropdown, same rule as field 14.
    "box8_foreign_country":         {"field_no": 32, "kind": "code",     "label": "Box 8 foreign country (code)",
                                     "confirm": True},
    "box9_cash_liquidation":        {"field_no": 33, "kind": "money",    "label": "Box 9 cash liquidation distr"},
    "box10_noncash_liquidation":    {"field_no": 34, "kind": "money",    "label": "Box 10 noncash liquidation distr"},
    "box12_exempt_interest_dividends": {"field_no": 35, "kind": "money", "label": "Box 12 exempt-interest dividends"},
    "box13_private_activity_bond":  {"field_no": 36, "kind": "money",    "label": "Box 13 private activity bond div"},
    # --- state rows (the screen prints two, and both are numbered 14/15/16) -------------
    "box14_state":                  {"field_no": 37, "kind": "state",    "label": "Box 14 state"},
    "box15_state_id":               {"field_no": 38, "kind": "text",     "label": "Box 15 state ID number"},
    "box16_state_wh":               {"field_no": 39, "kind": "money",    "label": "Box 16 state tax withheld"},
    "box14_state_2":                {"field_no": 40, "kind": "state",    "label": "Box 14 state (row 2)"},
    "box15_state_id_2":             {"field_no": 41, "kind": "text",     "label": "Box 15 state ID number (row 2)"},
    "box16_state_wh_2":             {"field_no": 42, "kind": "money",    "label": "Box 16 state tax withheld (row 2)"},
    # --- "Foreign Amount" column — the foreign-source part of each box, for Form 1116 ----
    "box1a_foreign_amount":         {"field_no": 43, "kind": "money",    "label": "Box 1a foreign amount"},
    "box1b_foreign_amount":         {"field_no": 44, "kind": "money",    "label": "Box 1b foreign amount"},
    "box2a_foreign_amount":         {"field_no": 45, "kind": "money",    "label": "Box 2a foreign amount"},
    "box6_foreign_amount":          {"field_no": 46, "kind": "money",    "label": "Box 6 foreign amount"},
    # --- "Foreign Percent" column — the alternative to the amount, per row --------------
    "box1a_foreign_pct":            {"field_no": 47, "kind": "pct",      "label": "Box 1a foreign percent"},
    "box1b_foreign_pct":            {"field_no": 48, "kind": "pct",      "label": "Box 1b foreign percent"},
    "box2a_foreign_pct":            {"field_no": 49, "kind": "pct",      "label": "Box 2a foreign percent"},
    "box6_foreign_pct":             {"field_no": 50, "kind": "pct",      "label": "Box 6 foreign percent"},
    # --- "Nominee Amount" column — the part belonging to someone else -------------------
    "box1a_nominee":                {"field_no": 51, "kind": "money",    "label": "Box 1a nominee amount"},
    "box1b_nominee":                {"field_no": 52, "kind": "money",    "label": "Box 1b nominee amount"},
    "box2a_nominee":                {"field_no": 53, "kind": "money",    "label": "Box 2a nominee amount"},
    "box2b_nominee":                {"field_no": 54, "kind": "money",    "label": "Box 2b nominee amount"},
    "box2c_nominee":                {"field_no": 55, "kind": "money",    "label": "Box 2c nominee amount"},
    "box2d_nominee":                {"field_no": 56, "kind": "money",    "label": "Box 2d nominee amount"},
    # --- "Amount that is:" — state treatment of box 1a ----------------------------------
    # Drake prints its own warning between these two: "Do NOT include the 'U.S. Government
    # dividend' portion on the 'NOT taxable on state' line." Nothing here can enforce that —
    # it is a property of the two amounts, not of either one — so it is in NOTES.
    "state_nontaxable_dividends":   {"field_no": 57, "kind": "money",    "label": "Box 1a NOT taxable on the state"},
    "us_govt_dividends":            {"field_no": 58, "kind": "money",    "label": "Box 1a U.S. Government dividends"},
    "restricted_dividends":         {"field_no": 59, "kind": "money",    "label": "Restricted dividends in box 1a"},
    # --- "Amount of Box 12 above that is:" — amount OR percent --------------------------
    # Each row is one or the other. Sending both is not an error the map can catch, so the
    # pair is flagged in `notes` and the screenshot is what settles it.
    "resident_state_muni_amount":   {"field_no": 60, "kind": "money",    "label": "Resident state muni interest amt"},
    "resident_state_muni_pct":      {"field_no": 61, "kind": "pct",      "label": "Resident state muni interest %"},
    "other_state_muni_amount":      {"field_no": 62, "kind": "money",    "label": "Other state muni interest amt"},
    "other_state_muni_pct":         {"field_no": 63, "kind": "pct",      "label": "Other state muni interest %"},
    "other_tax_exempt_amount":      {"field_no": 64, "kind": "money",    "label": "Other tax-exempt interest amt"},
    "other_tax_exempt_pct":         {"field_no": 65, "kind": "pct",      "label": "Other tax-exempt interest %"},
    # --- Form 1116 / foreign tax credit -------------------------------------------------
    "ftc_accrued":                  {"field_no": 66, "kind": "checkbox", "label": "FTC accrued (not paid)"},
    "ftc_date_paid_or_accrued":     {"field_no": 67, "kind": "date",     "label": "FTC date paid or accrued",
                                     "confirm": True},
    # MEASURED, not inferred: this box is 38 pixels wide where every amount box on this
    # screen is 127, and it is the same width as the LLC # box. The live run 2026-08-12 sent
    # '6010' and Drake's popup echoed '601' — it stopped accepting characters at three, and
    # the run halted on the read-back rather than commit a number ten times too small.
    #
    # So it is NOT an amount, which is what both this map and the INT one used to call it.
    # Drake prints only "FTC" beside it, inside the "Form 1116 / FTC Information" group, and
    # a 3-character box there is a form NUMBER — which Form 1116 this foreign tax belongs to
    # when a return carries several. That reading is inferred from the box's size, position
    # and group; what is MEASURED is the three characters and that an amount does not fit.
    "ftc_form_1116_code":           {"field_no": 68, "kind": "digits",
                                     "label": "FTC — Form 1116 number (3-char box, NOT an amount)",
                                     "max_len": 3, "confirm": True},
    "form_1116_not_required":       {"field_no": 69, "kind": "checkbox", "label": "1116 NOT required"},
    # --- state-specific ------------------------------------------------------------------
    "ia_taxable_dividend":          {"field_no": 70, "kind": "money",    "label": "IA taxable dividend amount"},
    # Illinois Schedule M bond categories. The full list was read out of this control on
    # 2026-08-12: THIRTY-TWO codes, A through Z plus AA, BB, CC, DD, EE and FF — the
    # two-letter ones being bonds issued by Guam, Puerto Rico, the Virgin Islands, American
    # Samoa and the Northern Marianas.
    #
    # Worth stating plainly, because the INT map got this wrong: it was written from the list
    # Drake printed in a REJECTION window, that window scrolled, and the codes past Z were
    # never seen. So it constrained this box to single letters and would have refused a
    # client's perfectly valid Puerto Rico bond code — our own planner rejecting a value
    # Drake would have taken. A list read from the control does not have that failure mode.
    "il_schedule_m_source":         {"field_no": 71, "kind": "code",
                                     "label": "IL Schedule M interest source (A-Z, AA-FF)",
                                     "values": ({chr(c) for c in range(ord("A"), ord("Z") + 1)}
                                                | {"AA", "BB", "CC", "DD", "EE", "FF"}),
                                     "confirm": True},
    "tn_nontaxable_nondividend":    {"field_no": 72, "kind": "money",
                                     "label": "Box 3 nondividend NOT taxable to TN"},
    "llc_number":                   {"field_no": 73, "kind": "digits",   "label": "LLC #"},
}

# Boxes that are a SELECTION on Drake's screen, not a free-text box. Derived structurally
# from the `Dropdown_<n>` automation ids in explore-div-form.json — measured, not eyeballed
# off a screenshot for a dropdown arrow.
DROPDOWN_FIELDS = {1, 3, 4, 11, 14, 22, 32, 37, 40, 71}

# Confirmed by the live run on 2026-08-12: every one of the ten read back as the typed code
# and was then READABLE ON THE FORM. Two of them said so in Drake's own words — field 32
# echoed 'CA   Canada' and field 71 echoed 'A A', the shape of a dropdown that really did
# select an entry.
#
# A dropdown earns a place here only by being OBSERVED on the form, never by its list having
# been read. Field 58 on the INT screen is why that rule is strict: it echoed a
# perfect-looking 'pa pa' and Drake rejected the value anyway.
DROPDOWNS_CONFIRMED: set = {1, 3, 4, 11, 14, 22, 32, 37, 40, 71}

# field 4 -> the payload key holding the state that locality belongs to. A city code means
# nothing without its state: Drake's table is keyed on both, and the control is disabled
# until the state is set.
LOCALITY_FIELDS = {4: "resident_state"}

NOTES = [
    "this screen has FOUR columns (Total / Foreign Amount / Foreign Percent / Nominee "
    "Amount), so one printed box is up to four field numbers and they are not adjacent — "
    "Box 1a is fields 18, 43, 47 and 51. Check the columns on the screenshot, not just the "
    "row.",
    "the foreign amount/percent pairs (43/47, 44/48, 45/49, 46/50) are alternatives — Drake "
    "expects one or the other on each row. Nothing here can tell which one a document "
    "meant, so if both arrive both are typed.",
    "the three amount/percent pairs (60/61, 62/63, 64/65) are alternatives in the same way.",
    "Drake prints its own warning on this screen: do NOT include the U.S. Government "
    "dividend portion (field 58) in the 'NOT taxable on the state' amount (field 57). That "
    "is a property of the two amounts together, so nothing here enforces it.",
    "field 22 (Box 2c Section 1202 stock type) decides whether 50%, 75% or 100% of the gain "
    "is excluded, so a wrong code is a wrong tax. Drake offers Q1, Q3 and Q4 only — there "
    "is no Q2 — and which one applies depends on WHEN the stock was acquired, which is not "
    "printed on a 1099-DIV. It is a preparer's determination, not an extraction.",
    "fields 14 and 32 take IRS country codes, NOT ISO 3166 — Australia is AS, Austria is "
    "AU, Algeria is AG. An ISO code can land on a real but DIFFERENT country that Drake "
    "accepts without complaint, so the code's source matters more than its shape.",
    "field 68 (FTC) holds THREE characters — measured live, Drake took '601' from '6010'. "
    "It is not an amount box, and anything longer than three characters is refused rather "
    "than cut to fit, because cutting a number changes it.",
]

DIV_SPEC = FormSpec(
    screen=SCREEN,
    label="1099-DIV — Schedule B, Dividend Income",
    fields=DIV_FIELD_MAP,
    max_field=MAX_FIELD,
    not_on_screen=NOT_ON_THIS_SCREEN,
    forbidden=FORBIDDEN_FIELDS,
    dropdowns=DROPDOWN_FIELDS,
    dropdowns_confirmed=DROPDOWNS_CONFIRMED,
    locality_fields=LOCALITY_FIELDS,
    # Whose return this is. The DIV screen has no recipient block at all — the recipient is
    # the return — so these are navigation inputs, already proven against Drake's own window
    # title before a key was pressed, not values that failed to find a box.
    identity_keys={"recipient_tin", "recipient_ssn", "recipient_name",
                   "recipient_first_name", "recipient_last_name",
                   "client_ssn", "client_first_name", "client_last_name"},
    # One DIV record is one PAYER. Entering the same payer twice doubles the client's
    # reported dividends, and every read-back would verify perfectly — so the payer TIN is
    # what a second run is refused on.
    dedupe_key="payer_tin",
    record_noun="1099-DIV",
    notes=NOTES,
)

DIV_SCHEMA_KEYS = DIV_SPEC.schema_keys


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Extracted 1099-DIV JSON -> an ordered, fully-resolved entry plan.

    `ts` is the operator's --ts flag. It fills field 1 when the payload does not carry a TSJ
    of its own — whose return this is, is a property of the RETURN and is not printed
    anywhere on a 1099-DIV.
    """
    extra = {}
    if ts is not None and not str((payload or {}).get("tsj") or "").strip():
        extra["tsj"] = ts
    plan = _build(payload, DIV_SPEC, checkbox_token=checkbox_token,
                  include_zeros=include_zeros, skip_fields=skip_fields, extra=extra)
    if not any(e["field_no"] == 1 for e in plan["entries"]):
        plan["warnings"].insert(0,
            "TSJ (field 1) was not supplied — Drake will use its default. On a JOINT return "
            "that files a spouse's or a joint account's dividends under the taxpayer alone. "
            "Pass --ts T|S (or put 'tsj' in the payload; this screen also accepts J).")
    return plan
