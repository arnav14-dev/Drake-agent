"""
Form 1098 Mortgage Interest: extracted JSON -> Drake heads-down field numbers (screen 1098).

Drake calls this screen "Form 1098 - Mortgage Interest". It is the sixth form this agent can
drive, and it follows the same rules as the others.

FIELD NUMBERS: read STRUCTURALLY off a live Drake 2025 screen on 2026-08-15 (test return
'123456789 - fynn, Test', dump `explore-1098-form.json`, screenshot `1098-screen.png`).
FORTY-FIVE boxes, 1..45, no gaps and none forbidden.

THE SCREEN WAS ON A TAB NOBODY HAD LOOKED AT

Every dump this project had taken of the Data Entry Menu captured the tab Drake opens on —
General, 37 links. The menu has TEN tabs and 312 links, and screen 1098 is on 'Other Forms'.
So `open_screen` reported "no screen with code 1098 on this menu" and listed whatever tab
happened to be showing: a correct refusal that read like a statement the screen did not
exist. It is fixed in drake_nav.open_screen, which now selects each tab in turn.

TWO OF FORM 1098'S BOXES HAVE NO BOX HERE, AND DRAKE SAYS WHY FOR ONE OF THEM

Box 4 (refund of overpaid interest) is PRINTED on the screen with an asterisk and no input
control, under Drake's own note:

    * Preparer must determine if the amount in box 4 of Form 1098 is taxable. To enter a
      taxable amount on Form 1040, use Schedule 1, line 8

Box 9 (number of properties securing the mortgage) is not printed at all. Both are declared
in NOT_ON_THIS_SCREEN by name, with the reason, rather than being dropped as unknown keys.

THE COUNTRY DROPDOWNS ARE THE MOST DANGEROUS BOXES ON THIS SCREEN

Fields 13 and 22 take a Drake country code, and Drake's 258 codes are NOT ISO. Five of ten
common ISO codes checked are valid Drake codes naming a DIFFERENT REAL COUNTRY — ES is El
Salvador and not Spain, CH is China and not Switzerland, AU is Austria and not Australia, SE
is Seychelles and not Sweden, AT is Ashmore And Cartier Islands and not Austria.

This is the one place in this codebase where a `values` membership check buys nothing: each
of those passes it, gets typed, is echoed back, and reads off the form as a real selection.
Nothing downstream can tell the wrong country was chosen. So DRAKE_COUNTRIES below maps code
-> name, both country fields are `confirm`, and build_plan prints the NAME beside the code —
the name being the only thing a human reviewing the plan can actually check. There is no code
for the United States: a domestic address belongs in the U.S. ONLY block, fields 10/11.

PROVEN LIVE 2026-08-15: all 45 boxes entered, verified field by field, and read back off the
form — 45/45 on the first run, with all eight dropdowns selecting a real entry. The four keys
this screen has no box for were reported by name, each with its reason, and none of them was
typed anywhere.

Field 43 is the one control on this screen with no precedent: a RadioButton, not a CheckBox.
It took the same 'X' token as the four real checkboxes, but Drake exposed no checkbox to
accessibility for it, so the tick was confirmed by the SCREEN GLYPH alone. That is a weaker
proof than the other four got and it is recorded as such rather than rounded up.

A Drake dropdown with no matching entry ACCEPTS the typing, echoes it back in the heads-down
popup, and stores nothing — so the echo is never proof. All eight lists below were read out
of the controls before anything was typed.

Nothing here touches Drake or files anything.
"""

from __future__ import annotations

from typing import Optional

from form_plan import FormSpec, build_plan as _build, format_plan  # noqa: F401

SCREEN = "1098"

FORBIDDEN_FIELDS: dict = {}

MAX_FIELD = 45

# Field 3, read out of the control 2026-08-15. Which schedule this mortgage interest belongs
# to. 'A' (Schedule A, itemised deductions) is the ordinary answer; the others say the
# property is business or rental, and the interest leaves Schedule A entirely.
FOR_SCHEDULE_CODES = {"A", "C", "E", "F", "4835", "8829"}

# Fields 13 and 22, read out of the control 2026-08-15 — 258 codes. Kept as code -> NAME
# rather than as a bare set, because naming the country is the only check a human can make
# against the silent ISO collisions described above.
DRAKE_COUNTRIES = {
    'AA': 'Aruba', 'AC': 'Antigua And Barbuda', 'AE': 'United Arab Emirates',
    'AF': 'Afghanistan', 'AG': 'Algeria', 'AJ': 'Azerbaijan', 'AL': 'Albania', 'AM': 'Armenia',
    'AN': 'Andorra', 'AO': 'Angola', 'AQ': 'American Samoa', 'AR': 'Argentina',
    'AS': 'Australia', 'AT': 'Ashmore And Cartier Islands', 'AU': 'Austria', 'AV': 'Anguilla',
    'AX': 'Akrotiri', 'AY': 'Antarctica', 'BA': 'Bahrain', 'BB': 'Barbados', 'BC': 'Botswana',
    'BD': 'Bermuda', 'BE': 'Belgium', 'BF': 'Bahamas', 'BG': 'Bangladesh', 'BH': 'Belize',
    'BK': 'Bosnia-Herzegovina', 'BL': 'Bolivia', 'BM': 'Burma', 'BN': 'Benin', 'BO': 'Belarus',
    'BP': 'Solomon Islands', 'BQ': 'Navassa Island', 'BR': 'Brazil', 'BT': 'Bhutan',
    'BU': 'Bulgaria', 'BV': 'Bouvet Island', 'BX': 'Brunei', 'BY': 'Burundi', 'CA': 'Canada',
    'CB': 'Cambodia', 'CD': 'Chad', 'CE': 'Sri Lanka', 'CF': 'Congo (Brazzaville)',
    'CG': 'Congo (Kinshasa)', 'CH': 'China', 'CI': 'Chile', 'CJ': 'Cayman Islands',
    'CK': 'Cocos Islands', 'CM': 'Cameroon', 'CN': 'Comoros', 'CO': 'Colombia',
    'CQ': 'Northern Mariana Islands', 'CR': 'Coral Sea Islands', 'CS': 'Costa Rica',
    'CT': 'Central African Republic', 'CU': 'Cuba', 'CV': 'Cape Verde', 'CW': 'Cook Islands',
    'CY': 'Cyprus', 'DA': 'Denmark', 'DJ': 'Djibouti', 'DO': 'Dominica', 'DQ': 'Jarvis Island',
    'DR': 'Dominican Republic', 'DX': 'Dhekelia', 'EC': 'Ecuador', 'EG': 'Egypt',
    'EI': 'Ireland', 'EK': 'Equatorial Guinea', 'EN': 'Estonia', 'ER': 'Eritrea',
    'ES': 'El Salvador', 'ET': 'Ethiopia', 'EZ': 'Czech Republic', 'FI': 'Finland',
    'FJ': 'Fiji', 'FK': 'Falkland Islands', 'FM': 'Federal States Of Micronesia',
    'FO': 'Faroe Islands', 'FP': 'French Polynesia', 'FQ': 'Baker Island', 'FR': 'France',
    'FS': 'French Southern And Antarctic Lands', 'GA': 'The Gambia', 'GB': 'Gabon',
    'GG': 'Georgia', 'GH': 'Ghana', 'GI': 'Gibraltar', 'GJ': 'Grenada', 'GK': 'Guernsey',
    'GL': 'Greenland', 'GM': 'Germany', 'GQ': 'Guam', 'GR': 'Greece', 'GT': 'Guatemala',
    'GV': 'Guinea', 'GY': 'Guyana', 'HA': 'Haiti', 'HK': 'Hong Kong',
    'HM': 'Heard Island And Mcdonald Islands', 'HO': 'Honduras', 'HQ': 'Howland Island',
    'HR': 'Croatia', 'HU': 'Hungary', 'IC': 'Iceland', 'ID': 'Indonesia', 'IM': 'Isle Of Man',
    'IN': 'India', 'IO': 'British Indian Ocean Territory', 'IP': 'Clipperton Island',
    'IR': 'Iran', 'IS': 'Israel', 'IT': 'Italy', 'IV': "Cote D'ivoire", 'IZ': 'Iraq',
    'JA': 'Japan', 'JE': 'Jersey', 'JM': 'Jamaica', 'JN': 'Jan Mayen', 'JO': 'Jordan',
    'JQ': 'Johnston Atoll', 'KE': 'Kenya', 'KG': 'Kyrgyzstan', 'KN': 'North Korea',
    'KQ': 'Kingman Reef', 'KR': 'Kiribati', 'KS': 'South Korea', 'KT': 'Christmas Island',
    'KU': 'Kuwait', 'KV': 'Kosovo', 'KZ': 'Kazakhstan', 'LA': 'Laos', 'LE': 'Lebanon',
    'LG': 'Latvia', 'LH': 'Lithuania', 'LI': 'Liberia', 'LO': 'Slovakia', 'LQ': 'Palmyra Atoll',
    'LS': 'Liechtenstein', 'LT': 'Lesotho', 'LU': 'Luxembourg', 'LY': 'Libya',
    'MA': 'Madagascar', 'MC': 'Macau', 'MD': 'Moldova', 'MG': 'Mongolia', 'MH': 'Montserrat',
    'MI': 'Malawi', 'MJ': 'Montenegro', 'MK': 'Macedonia', 'ML': 'Mali', 'MN': 'Monaco',
    'MO': 'Morocco', 'MP': 'Mauritius', 'MQ': 'Midway Islands', 'MR': 'Mauritania',
    'MT': 'Malta', 'MU': 'Oman', 'MV': 'Maldives', 'MX': 'Mexico', 'MY': 'Malaysia',
    'MZ': 'Mozambique', 'NC': 'New Caledonia', 'NE': 'Niue', 'NF': 'Norfolk Island',
    'NG': 'Niger', 'NH': 'Vanuatu', 'NI': 'Nigeria', 'NL': 'Netherlands', 'NN': 'Sint Maarten',
    'NO': 'Norway', 'NP': 'Nepal', 'NR': 'Nauru', 'NS': 'Suriname', 'NU': 'Nicaragua',
    'NZ': 'New Zealand', 'OC': 'Other Country', 'OD': 'South Sudan', 'PA': 'Paraguay',
    'PC': 'Pitcairn Islands', 'PE': 'Peru', 'PF': 'Paracel Islands', 'PG': 'Spratly Islands',
    'PK': 'Pakistan', 'PL': 'Poland', 'PM': 'Panama', 'PO': 'Portugal',
    'PP': 'Papua-New Guinea', 'PS': 'Palau', 'PU': 'Guinea-Bissau', 'QA': 'Qatar',
    'RI': 'Serbia', 'RM': 'Marshall Islands', 'RN': 'Saint Martin', 'RO': 'Romania',
    'RP': 'Philippines', 'RQ': 'Puerto Rico', 'RS': 'Russia', 'RW': 'Rwanda',
    'SA': 'Saudi Arabia', 'SB': 'St. Pierre And Miquelon', 'SC': 'St. Kitts And Nevis',
    'SE': 'Seychelles', 'SF': 'South Africa', 'SG': 'Senegal', 'SH': 'St. Helena',
    'SI': 'Slovenia', 'SL': 'Sierra Leone', 'SM': 'San Marino', 'SN': 'Singapore',
    'SO': 'Somalia', 'SP': 'Spain', 'ST': 'St. Lucia Island', 'SU': 'Sudan', 'SV': 'Svalbard',
    'SW': 'Sweden', 'SX': 'South Georgia And South Sandwich Is', 'SY': 'Syria',
    'SZ': 'Switzerland', 'TB': 'Saint Barthelemy', 'TD': 'Trinidad And Tobago',
    'TH': 'Thailand', 'TI': 'Tajikistan', 'TK': 'Turks And Caicos Islands', 'TL': 'Tokelau',
    'TN': 'Tonga', 'TO': 'Togo', 'TP': 'Sao Tome And Principe', 'TS': 'Tunisia',
    'TT': 'East Timor', 'TU': 'Turkey', 'TV': 'Tuvalu', 'TW': 'Taiwan', 'TX': 'Turkmenistan',
    'TZ': 'Tanzania', 'UC': 'Curacao', 'UG': 'Uganda', 'UK': 'United Kingdom', 'UP': 'Ukraine',
    'UV': 'Burkina Faso', 'UY': 'Uruguay', 'UZ': 'Uzbekistan',
    'VC': 'St. Vincent And The Grenadines', 'VE': 'Venezuela', 'VI': 'British Virgin Islands',
    'VM': 'Vietnam', 'VQ': 'Virgin Islands', 'VT': 'Holy See', 'WA': 'Namibia',
    'WF': 'Wallis And Futuna', 'WI': 'Western Sahara', 'WQ': 'Wake Island', 'WS': 'Samoa',
    'WZ': 'Swaziland', 'YM': 'Yemen', 'ZA': 'Zambia', 'ZI': 'Zimbabwe',
}

COUNTRY_CODES = frozenset(DRAKE_COUNTRIES)


# Keys an extractor legitimately reads off a Form 1098 that have NO box on this screen.
# Reported BY NAME so a dropped value is never silent.
NOT_ON_THIS_SCREEN = {
    "box4_refund_overpaid_interest":
        "Form 1098 Box 4. Drake PRINTS box 4 on this screen with an asterisk and no input "
        "box, under its own note: '* Preparer must determine if the amount in box 4 of Form "
        "1098 is taxable. To enter a taxable amount on Form 1040, use Schedule 1, line 8'. "
        "A refund of interest deducted in an earlier year is taxable only if that deduction "
        "actually reduced tax, which needs the prior year's return — so Drake asks a human "
        "and this agent must not answer for them.",
    "box9_number_of_properties":
        "Form 1098 Box 9. Drake does not print box 9 on this screen at all. It matters only "
        "when one mortgage secures several properties, which changes how the interest is "
        "split, and Drake handles that through the 'Splitting Interest Between Schedules' "
        "link on this screen rather than through a box.",
    # These DO change the return — just not from this screen.
    "mortgage_balance_limitation":
        "the acquisition-debt cap is worked out on Drake's DEDM screen ('Deductible Mortgage "
        "Interest', Other Forms tab), which this screen links to as 'Loan Limit Worksheet'. "
        "Its RESULT is what belongs in field 25 here, not the cap itself.",
    "prior_year_interest":
        "not a Form 1098 value. Prior-year figures come from the return Drake rolled over.",
}

# kind drives sanitization and how the value is entered — see form_plan.sanitize.
M1098_FIELD_MAP = {
    # --- header -----------------------------------------------------------------------
    # TSJ, not TS: Drake offers 'J  Belongs to each spouse equally', read out of the control.
    # A mortgage genuinely can be held jointly, unlike a W-2 or a Social Security benefit.
    "tsj":                    {"field_no": 1,  "kind": "tsj",  "label": "TSJ taxpayer/spouse/joint"},
    # The list also carries '0  Suppress State', which is not a state — it is an instruction
    # to leave this document off the state return. `state` refuses it, and that is the safe
    # direction: suppressing a state return is a decision, not an extraction.
    "resident_state":         {"field_no": 2,  "kind": "state", "label": "ST resident state"},
    # WHERE THE INTEREST LANDS. 'A' is Schedule A. C/E/F/4835/8829 mean the property is
    # business or rental and the interest leaves Schedule A altogether — a different return.
    # Never inferred: nothing on a Form 1098 says what the property is used for.
    "for_schedule":           {"field_no": 3,  "kind": "code_form",
                               "label": "For — which schedule/form (A, C, E, F, 4835, 8829)",
                               "values": FOR_SCHEDULE_CODES, "confirm": True},
    "multi_form_number":      {"field_no": 4,  "kind": "digits", "max_len": 2,
                               "label": "Multi-form number (1-99; blank means 1)",
                               "confirm": True},
    "not_issued_in_taxpayer_name": {"field_no": 5, "kind": "checkbox",
                               "label": "1098 not issued in taxpayer's name"},

    # --- Recipient's/Lender's Information ----------------------------------------------
    "lender_tin":             {"field_no": 6,  "kind": "tin",   "label": "Lender Fed ID#"},
    "lender_name":            {"field_no": 7,  "kind": "text",  "label": "Lender name"},
    "lender_address":         {"field_no": 8,  "kind": "text",  "label": "Lender address"},
    "lender_city":            {"field_no": 9,  "kind": "text",  "label": "Lender city"},
    "lender_state":           {"field_no": 10, "kind": "state", "label": "Lender state (U.S. only)"},
    "lender_zip":             {"field_no": 11, "kind": "zip",   "label": "Lender ZIP (U.S. only)"},
    "lender_province":        {"field_no": 12, "kind": "text",
                               "label": "Lender province/state (foreign only)"},
    "lender_country":         {"field_no": 13, "kind": "country",
                               "label": "Lender country code (foreign only)",
                               "values": COUNTRY_CODES, "confirm": True},
    "lender_postal_code":     {"field_no": 14, "kind": "text",
                               "label": "Lender postal code (foreign only)"},

    # --- Payer's/Borrower's Information, if different from screen 1 --------------------
    # Filled in only when the borrower is NOT the client whose return this is. Normally the
    # borrower IS the client, Drake already has them on screen 1, and these stay empty.
    "borrower_first_name":    {"field_no": 15, "kind": "text",  "label": "Borrower first name"},
    "borrower_last_name":     {"field_no": 16, "kind": "text",  "label": "Borrower last name"},
    "borrower_address":       {"field_no": 17, "kind": "text",  "label": "Borrower address"},
    "borrower_city":          {"field_no": 18, "kind": "text",  "label": "Borrower city"},
    "borrower_state":         {"field_no": 19, "kind": "state", "label": "Borrower state (U.S. only)"},
    "borrower_zip":           {"field_no": 20, "kind": "zip",   "label": "Borrower ZIP (U.S. only)"},
    "borrower_province":      {"field_no": 21, "kind": "text",
                               "label": "Borrower province/state (foreign only)"},
    "borrower_country":       {"field_no": 22, "kind": "country",
                               "label": "Borrower country code (foreign only)",
                               "values": COUNTRY_CODES, "confirm": True},
    "borrower_postal_code":   {"field_no": 23, "kind": "text",
                               "label": "Borrower postal code (foreign only)"},

    # --- the numbered Form 1098 boxes ---------------------------------------------------
    "box1_mortgage_interest": {"field_no": 24, "kind": "money",
                               "label": "Box 1 mortgage interest received"},
    # NOT a Form 1098 box. Drake's own adjustment: what is actually DEDUCTIBLE when it
    # differs from box 1 — the output of the DEDM loan-limit worksheet. An extractor must
    # never fill this from the document, because the document cannot know it.
    "deductible_amount_override": {"field_no": 25, "kind": "money",
                               "label": "Deductible amount, if different from box 1",
                               "confirm": True},
    "box2_principal":         {"field_no": 26, "kind": "money",
                               "label": "Box 2 outstanding mortgage principal as of 1/1/2025"},
    "box3_origination_date":  {"field_no": 27, "kind": "date",
                               "label": "Box 3 mortgage origination date", "confirm": True},
    "box5_mip":               {"field_no": 28, "kind": "money",
                               "label": "Box 5 mortgage insurance premiums"},
    "box6_points":            {"field_no": 29, "kind": "money",
                               "label": "Box 6 points paid on purchase of principal residence"},
    "box7_property_address_same_as_borrower": {"field_no": 30, "kind": "checkbox",
                               "label": "Box 7 property address same as borrower"},
    "box8_property_address":  {"field_no": 31, "kind": "text",
                               "label": "Box 8 address of property securing mortgage"},
    "box8_property_city":     {"field_no": 32, "kind": "text",  "label": "Box 8 property city"},
    "box8_property_state":    {"field_no": 33, "kind": "state", "label": "Box 8 property state"},
    "box8_property_zip":      {"field_no": 34, "kind": "zip",   "label": "Box 8 property ZIP"},
    "box8_property_description": {"field_no": 35, "kind": "text",
                               "label": "Box 8 description of property"},
    "box10_other":            {"field_no": 36, "kind": "text",  "label": "Box 10 other"},
    "box11_acquisition_date": {"field_no": 37, "kind": "date",
                               "label": "Box 11 mortgage acquisition date", "confirm": True},

    # --- Additional information ---------------------------------------------------------
    "account_number":         {"field_no": 38, "kind": "text",  "label": "Account number (optional)"},
    # Often printed on a lender's 1098 as an escrow figure even though it is not a numbered
    # box. It is a Schedule A item in its own right, and it is NOT mortgage interest.
    "real_estate_taxes_paid": {"field_no": 39, "kind": "money", "label": "Real estate taxes paid"},
    "primary_residence":      {"field_no": 40, "kind": "checkbox", "label": "Primary residence"},
    "taxes_for_state_property_credit": {"field_no": 41, "kind": "money",
                               "label": "Taxes that qualify for state property tax credit",
                               "confirm": True},
    # Free text, several lines, and Drake says on the screen that it is E-FILED: "Explain
    # below if the taxpayer paid and is deducting more mortgage interest than shown. This
    # information will be e-filed." It goes straight to the IRS, so nothing is generated for
    # it — a human writes this sentence or it stays empty.
    "more_interest_explanation": {"field_no": 42, "kind": "text",
                               "label": "Explanation for deducting more interest than shown "
                                        "(E-FILED TO THE IRS)", "confirm": True},
    # A RadioButton (CheckGroupStartTextRight_43), not a CheckBox like the other four ticks
    # on this screen. Whether it takes the same 'X' token is UNMEASURED — hence `confirm`.
    "not_used_to_buy_build_improve": {"field_no": 43, "kind": "checkbox",
                               "label": "Some loans not used to buy, build or improve the home",
                               "confirm": True},
    # State Use Only box.
    "state_nondeductible_home_equity_interest": {"field_no": 44, "kind": "money",
                               "label": "Federally nondeductible home equity interest (state use)",
                               "confirm": True},
    "do_not_update_to_2026":  {"field_no": 45, "kind": "checkbox",
                               "label": "Do not update to 2026"},
}

# Derived structurally from the `Dropdown_<n>` automation ids.
DROPDOWN_FIELDS = {1, 2, 3, 10, 13, 19, 22, 33}

# Confirmed by the live run on 2026-08-15: all eight read back as the typed code and were
# then readable on the form — including both country boxes, 13 as CA and 22 as GM.
#
# A dropdown earns a place here only by being OBSERVED on the form, never by its list having
# been read. Field 58 on the INT screen is why: it echoed a perfect-looking 'pa pa' and Drake
# rejected the value anyway.
DROPDOWNS_CONFIRMED: set = {1, 2, 3, 10, 13, 19, 22, 33}

# No locality box on this screen — nothing here is keyed to Drake's CITY.HLP table.
LOCALITY_FIELDS: dict = {}

NOTES = [
    "screen 1098 lives on the Data Entry Menu's 'Other Forms' tab, not 'General'. Every "
    "other screen this agent drives is on General, and open_screen could only see the tab "
    "that happened to be showing until this form was added.",
    "field 3 decides which schedule the interest lands on. 'A' is the ordinary answer; "
    "C, E, F, 4835 and 8829 take it off Schedule A entirely and onto a business or rental "
    "form. Nothing printed on a Form 1098 says which is right — the preparer knows what the "
    "property is used for and the document does not.",
    "Box 4 (refund of overpaid interest) and Box 9 (number of properties) have no box on "
    "this screen. Box 4 is printed with Drake's own note sending it to Schedule 1 line 8.",
    "fields 13 and 22 take DRAKE country codes, which are not ISO. ES is El Salvador, not "
    "Spain. CH is China, not Switzerland. AU is Austria, not Australia. Each of those is a "
    "real code, so it passes every check and reads back correctly — the wrong country is "
    "invisible after the fact. The plan prints the country NAME for this reason.",
    "field 25 is not a Form 1098 box. It is what is actually DEDUCTIBLE when the loan "
    "exceeds the acquisition-debt cap, which is computed on the DEDM screen. Filling it "
    "from the document would claim a deduction nobody worked out.",
    "field 42 is e-filed to the IRS verbatim — Drake says so on the screen. It is prose "
    "written by a preparer, never generated.",
]

M1098_SPEC = FormSpec(
    screen=SCREEN,
    label="Form 1098 — Mortgage Interest",
    fields=M1098_FIELD_MAP,
    max_field=MAX_FIELD,
    not_on_screen=NOT_ON_THIS_SCREEN,
    forbidden=FORBIDDEN_FIELDS,
    dropdowns=DROPDOWN_FIELDS,
    dropdowns_confirmed=DROPDOWNS_CONFIRMED,
    locality_fields=LOCALITY_FIELDS,
    # The BORROWER is who this return belongs to. `borrower_name` arrives combined and the
    # backend emitter splits it into the two boxes 15/16 on the way through, so all three
    # spellings have to be accepted or the split one is reported as an unknown key.
    identity_keys={"recipient_tin", "recipient_ssn", "recipient_name",
                   "borrower_name", "borrower_ssn", "payer_ssn", "payer_tin",
                   "client_ssn", "client_first_name", "client_last_name"},
    # The LENDER is what makes two 1098s different documents. A client refinancing mid-year
    # genuinely has two, from two lenders; the same lender twice is the same statement keyed
    # twice, which would double their mortgage interest deduction.
    dedupe_key="lender_tin",
    record_noun="Form 1098",
    notes=NOTES,
)

M1098_SCHEMA_KEYS = M1098_SPEC.schema_keys


def country_name(code) -> Optional[str]:
    """The country Drake will select for this code, or None if it is not on Drake's list."""
    return DRAKE_COUNTRIES.get(str(code or "").strip().upper())


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Extracted Form 1098 JSON -> an ordered, fully-resolved entry plan."""
    payload = dict(payload or {})
    extra = {}
    if ts is not None and not str(payload.get("tsj") or "").strip():
        extra["tsj"] = ts
    plan = _build(payload, M1098_SPEC, checkbox_token=checkbox_token,
                  include_zeros=include_zeros, skip_fields=skip_fields, extra=extra)

    # Box 1 is the whole point of the document. A payload carrying the principal or the
    # points but not the interest is a mis-read document, not a light one.
    if not any(e["field_no"] == 24 for e in plan["entries"]) and plan["entries"]:
        plan["warnings"].insert(0,
            "BOX 1 MORTGAGE INTEREST (field 24) was not supplied. It is the number this "
            "whole form exists to report — without it the return claims no mortgage "
            "interest at all, however much else was entered.")

    # Name the country beside the code. Drake's codes are not ISO and five common ISO codes
    # name a different real country, so the code alone tells a reviewer nothing.
    for e in plan["entries"]:
        if e["field_no"] in (13, 22):
            name = country_name(e.get("value"))
            whose = "lender" if e["field_no"] == 13 else "borrower"
            # `name` cannot be None here: the field carries `values=COUNTRY_CODES`, so the
            # planner has already refused anything off the list before it became an entry.
            # That refusal is the stronger guard — it happens before Drake is even open —
            # and this is only the half that catches a code which IS on the list and still
            # wrong. Asserting rather than branching, so that removing `values` fails loudly
            # instead of quietly reaching an untested path.
            assert name, f"field {e['field_no']} passed a country code not on Drake's list"
            plan["warnings"].append(
                f"{whose} country {e['value']!r} selects {name.upper()} in Drake. Drake's "
                f"codes are NOT ISO — check this is the country on the document.")

    # A 1098 that is not going to Schedule A is a different return. Say so rather than let
    # it pass as an ordinary itemised deduction.
    for e in plan["entries"]:
        if e["field_no"] == 3 and str(e.get("value") or "").upper() != "A":
            plan["warnings"].insert(0,
                f"FOR = {e['value']!r} sends this mortgage interest to a business or rental "
                f"form, NOT to Schedule A. Nothing printed on a Form 1098 says which is "
                f"right — confirm the property's use before this is entered.")
    return plan
