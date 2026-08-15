"""
SSA-1099: extracted JSON -> Drake heads-down field numbers (screen SSA).

Drake calls this screen "SSA-1099, Social Security Benefits Statement / RRB-1099, Railroad
Retirement Board Payments" — one screen serving both forms. It is the fifth form this agent
can drive, and it follows the same rules as the others.

FIELD NUMBERS: read STRUCTURALLY off a live Drake 2025 screen on 2026-08-15 (test return
'123456789 - fynn, Test', dump `explore-ssa-form.json`, screenshot `ssa-screen.png`). TEN
boxes, 1..10, no gaps and none forbidden.

TEN BOXES FOR A FORM WITH TWENTY, AND DRAKE SAYS WHY

This screen is mostly LABELS. Name, Beneficiary's SSN, Benefits (Box 3), Benefits repaid
(Box 4), Description of amount, Address and Claim number are all printed with an asterisk
and no box to type in, under a legend Drake prints on the screen itself:

    * No input required since there is no impact on the tax return

That is Drake stating which parts of an SSA-1099 change the return and which do not. Only
three numbers do: NET BENEFITS (Box 5), the MEDICARE PREMIUMS deducted from them, and the
FEDERAL TAX WITHHELD (Box 6). Box 3 and Box 4 matter only through Box 5, which is their
difference and which the form prints.

So the extraction reading twenty things off the page is not wasted, but most of it lands
somewhere other than this screen — the Medicare split feeds the Schedule A medical
deduction, and BENEFITS PAID FOR AN EARLIER YEAR feed the LUMP SUM screen, which this one
links to by name in its top corner. Those are declared in NOT_ON_THIS_SCREEN by name, with
the reason, rather than being dropped as unknown keys.

TWO THINGS TO KNOW

  1. TS, NOT TSJ — Drake offers T and S only. Social Security belongs to one person, and a
     married couple's two SSA-1099s are two records on this screen, one each.
  2. Field 8's codes are TWO CHARACTERS WITH A LEADING ZERO: 01..05. '1' is not '01' and
     Drake's list does not contain it.

PROVEN LIVE 2026-08-15: all 10 boxes entered, verified field by field, and read back off
the form — 10/10 on the first run, with all three dropdowns selecting a real entry. The
eleven values this screen has no box for were reported by name, each with its reason, and
none of them was typed anywhere.

A Drake dropdown with no matching entry ACCEPTS the typing, echoes it back in the
heads-down popup, and stores nothing — so the echo is never proof. All three lists below
were read out of the controls before anything was typed.

Nothing here touches Drake or files anything.
"""

from __future__ import annotations

from form_plan import FormSpec, build_plan as _build, format_plan  # noqa: F401

SCREEN = "SSA"

FORBIDDEN_FIELDS: dict = {}

MAX_FIELD = 10

# Field 8, read out of the control 2026-08-15. State-return use only.
BENEFIT_DESIGNATIONS = {"01", "02", "03", "04", "05"}

# Keys an extractor legitimately reads off an SSA-1099 that have NO box on this screen.
# Drake's own asterisk legend is the authority for most of them, and it is quoted rather than
# paraphrased: these are not gaps in the map, they are boxes Drake deliberately does not
# offer. Reported BY NAME so a dropped value is never silent.
_NO_IMPACT = ("Drake prints this on the screen with no box and the legend '* No input "
              "required since there is no impact on the tax return'.")
NOT_ON_THIS_SCREEN = {
    "beneficiary_name": _NO_IMPACT,
    "beneficiary_ssn": _NO_IMPACT,
    "benefits_paid": f"SSA-1099 Box 3. {_NO_IMPACT} Only Box 5, the net, reaches the return.",
    "benefits_repaid": f"SSA-1099 Box 4. {_NO_IMPACT} Only Box 5, the net, reaches the return.",
    "address": _NO_IMPACT,
    "claim_number": _NO_IMPACT,
    "paid_by_check_or_direct_deposit": _NO_IMPACT,
    "attorney_fees": _NO_IMPACT,
    # These DO change the return — just not from this screen.
    "medicare_part_b": "the SSA screen takes the Medicare TOTAL (field 5). The split by part "
                       "is for the Schedule A medical deduction, on a different screen.",
    "medicare_part_c": "as medicare_part_b — the total is what this screen takes.",
    "medicare_part_d": "as medicare_part_b — the total is what this screen takes.",
    "benefits_for_prior_years": "benefits paid THIS year for an EARLIER year go on Drake's "
                                "LUMP SUM screen, which this screen links to in its top "
                                "corner. Entering them here would tax them all in this year "
                                "and lose the lump-sum election.",
    "prior_years_covered": "as benefits_for_prior_years — the LUMP SUM screen wants it.",
}

# kind drives sanitization and how the value is entered — see form_plan.sanitize.
SSA_FIELD_MAP = {
    # T or S ONLY. `ts` REJECTS 'J' rather than downgrading it — a benefit belongs to one
    # person, and a couple's two statements are two records on this screen.
    "ts":                       {"field_no": 1,  "kind": "ts",       "label": "TS taxpayer/spouse"},
    # The header "F" — the Federal code, as on the Schedule B screens. Carried across on the
    # same assumption and NOT measured here; `values` is the safe direction either way.
    "federal_code":             {"field_no": 2,  "kind": "digits",   "label": "F federal code (0 = exclude)",
                                 "values": {"0"}, "confirm": True},
    "resident_state":           {"field_no": 3,  "kind": "state",    "label": "ST resident state"},
    # THE NUMBER THAT MATTERS. Box 5 is Box 3 minus Box 4 and it is what the taxable-benefit
    # calculation runs on. It is a printed box on the form, never computed here: a Box 5 that
    # disagrees with 3 minus 4 is a document a human has to look at, not an arithmetic error
    # to silently correct.
    "net_benefits":             {"field_no": 4,  "kind": "money",    "label": "Net benefits (SSA Box 5)"},
    # The TOTAL of the Medicare premiums deducted from the benefit. Worth having: they never
    # appear on a bank statement, because they are taken out before the money is paid.
    "medicare_premiums":        {"field_no": 5,  "kind": "money",    "label": "Medicare premiums deducted"},
    # An ELECTION, not a fact: it moves the Medicare premiums to the self-employed health
    # insurance deduction instead of Schedule A. Only right for a client with self-employment
    # income, so nothing extracts it.
    "medicare_as_sehi":         {"field_no": 6,  "kind": "checkbox",
                                 "label": "Treat Medicare premiums as SE health insurance"},
    "federal_tax_withheld":     {"field_no": 7,  "kind": "money",    "label": "Federal tax withheld (SSA Box 6)"},
    # State returns only. Codes are TWO characters with a leading zero — '01' not '1'.
    "state_benefit_designation": {"field_no": 8, "kind": "code_an",
                                  "label": "Designate benefits as (01-05, state use)",
                                  "values": BENEFIT_DESIGNATIONS, "confirm": True},
    "tax_treaty_exempt":        {"field_no": 9,  "kind": "checkbox",
                                 "label": "Treaty country resident — benefits not taxable"},
    "workers_comp_reduction":   {"field_no": 10, "kind": "money",
                                 "label": "Benefits reduced by workers' compensation"},
}

# Derived structurally from the `Dropdown_<n>` automation ids.
DROPDOWN_FIELDS = {1, 3, 8}

# Confirmed by the live run on 2026-08-15: all three read back as the typed code and were
# then readable on the form, field 8 keeping its leading zero ('02', not '2').
#
# A dropdown earns a place here only by being OBSERVED on the form, never by its list
# having been read. Field 58 on the INT screen is why: it echoed a perfect-looking 'pa pa'
# and Drake rejected the value anyway.
DROPDOWNS_CONFIRMED: set = {1, 3, 8}

LOCALITY_FIELDS: dict = {}

NOTES = [
    "this screen serves BOTH the SSA-1099 and the RRB-1099 (railroad). The box numbers "
    "differ between them — federal tax withheld is SSA Box 6 but RRB Box 10 — and Drake "
    "prints both labels on the same box. An RRB-1099 is not an SSA-1099 and its Tier 1 "
    "benefits are the part that belongs here.",
    "field 1 is TS, not TSJ. A married couple's two statements are two records on this "
    "screen, one each — not one record marked joint.",
    "field 8's codes carry a LEADING ZERO ('01', not '1'), and they are for state returns "
    "only.",
    "Box 3 and Box 4 have no box on this screen. Only Box 5 — their difference, printed on "
    "the form — reaches the return, and Drake says so on the screen itself.",
    "benefits paid this year FOR AN EARLIER year belong on Drake's LUMP SUM screen, not "
    "here. Putting them in Box 5 taxes them all in the current year and throws away the "
    "lump-sum election, which is often worth a great deal to a client who got back pay.",
]

SSA_SPEC = FormSpec(
    screen=SCREEN,
    label="SSA-1099 — Social Security Benefits",
    fields=SSA_FIELD_MAP,
    max_field=MAX_FIELD,
    not_on_screen=NOT_ON_THIS_SCREEN,
    forbidden=FORBIDDEN_FIELDS,
    dropdowns=DROPDOWN_FIELDS,
    dropdowns_confirmed=DROPDOWNS_CONFIRMED,
    locality_fields=LOCALITY_FIELDS,
    identity_keys={"recipient_tin", "recipient_ssn", "recipient_name",
                   "recipient_first_name", "recipient_last_name",
                   "beneficiary_ssn", "beneficiary_name",
                   "client_ssn", "client_first_name", "client_last_name"},
    # There is no payer on an SSA-1099 — the payer is the government. What makes a second
    # record legitimate is a DIFFERENT PERSON (taxpayer vs spouse), which is field 1, so
    # there is no id to dedupe on. Left None deliberately: inventing one would either refuse
    # a spouse's genuine second statement or fail to catch a real duplicate.
    dedupe_key=None,
    record_noun="SSA-1099",
    notes=NOTES,
)

SSA_SCHEMA_KEYS = SSA_SPEC.schema_keys


def build_plan(payload: dict, *, checkbox_token: str = "X", include_zeros: bool = False,
               skip_fields=None, ts=None) -> dict:
    """Extracted SSA-1099 JSON -> an ordered, fully-resolved entry plan.

    `ts` is the operator's --ts flag and it matters more here than on most screens: this
    form is one person's, the screen takes T or S only, and a couple's two statements are
    two records. Getting it wrong files one spouse's benefits under the other.
    """
    payload = dict(payload or {})
    extra = {}
    if ts is not None and not str(payload.get("ts") or "").strip():
        extra["ts"] = ts
    plan = _build(payload, SSA_SPEC, checkbox_token=checkbox_token,
                  include_zeros=include_zeros, skip_fields=skip_fields, extra=extra)
    if not any(e["field_no"] == 1 for e in plan["entries"]):
        plan["warnings"].insert(0,
            "TS (field 1) was not supplied — Drake will use its default. On a joint return "
            "both spouses often receive their own SSA-1099, and defaulting files one under "
            "the other. Pass --ts T|S; this screen does NOT accept J.")
    # Box 5 is the whole point of the screen. A payload that carries the Medicare or the
    # withholding but not the net benefit is a mis-read document, not a light one.
    if not any(e["field_no"] == 4 for e in plan["entries"]) and plan["entries"]:
        plan["warnings"].insert(0,
            "NET BENEFITS (field 4, SSA Box 5) was not supplied. It is the only number on "
            "this form the taxable-benefit calculation runs on — without it the return "
            "reports no Social Security at all, however much else was entered.")
    return plan
