"""
The form-agnostic planning engine: extracted JSON -> Drake heads-down field numbers.

`w2_map.py` proved the shape of this: a fixed table of key -> field number + kind, pure
sanitizers, and a plan that reports everything it dropped. That worked, so the second form
does not get to invent its own version of it. This module is that engine with the W-2
specifics lifted out; a form is now a `FormSpec` (see `int_map.py`) and nothing else.

WHY THIS IS NOT YET SHARED WITH w2_map.py
    The W-2 path is proven live and covered by the mutation harness. Re-pointing it at a
    brand-new engine on the same day the second form arrives would put the one working
    pipeline at risk to tidy up code. So `w2_map.build_plan` stays exactly as it is, and
    this engine carries the forms added from here on. They merge once INT has run live and
    the mutants for both are green — not before. The sanitizers are IMPORTED from w2_map
    rather than copied, so the two planners cannot disagree about what a value means.

Nothing here touches Drake or files anything. It is importable and testable anywhere, which
is why a whole form can be reviewed with `--dry-run` on a machine that has no Drake at all.
"""

from __future__ import annotations

from typing import Optional

# These are generic — money is money on every form — and they live in w2_map for historical
# reasons only. IMPORTED, never copied: two implementations of "what does this value mean"
# is exactly the drift that puts a wrong number in a real return.
from w2_map import (  # noqa: F401
    sanitize as _w2_sanitize,
    _clean_money,
    _clean_text,
    _digits,
    _has_content,
    _is_zeroish,
    resolve_locality,
    locality_source,
)


# --- kinds this engine adds on top of the W-2 set ---------------------------------------
#   digits   a routing/account number Drake re-formats itself
#   tin      payer EIN or SSN — digits only, same as 'ein'
#   pct      a percentage box (0-100); '12.5%' -> '12.5'
#   date     a Drake date box, normalised to MMDDYYYY
#   tsj      taxpayer / spouse / JOINT. The W-2 selector is TS; every 1099 screen is TSJ,
#            because a bank account really can be held jointly. Using 'ts' here would
#            reject every joint account on the grounds that a W-2 cannot be joint.
# Everything else falls through to w2_map.sanitize: money, ein, ssn, zip, state, code,
# year, ts, checkbox, text.

def _clean_tsj(v) -> Optional[str]:
    """Taxpayer / spouse / joint selector -> 'T', 'S' or 'J'."""
    s = "".join(ch for ch in str(v) if ch.isalpha()).upper()
    if s in ("T", "S", "J"):
        return s
    if s in ("TAXPAYER", "PRIMARY", "SELF"):
        return "T"
    if s == "SPOUSE":
        return "S"
    if s in ("JOINT", "BOTH"):
        return "J"
    return None


def _clean_pct(v) -> Optional[str]:
    """'100%' -> '100'; '12.50' -> '12.5'. Outside 0-100 is REJECTED, not clamped.

    A percentage box that reads 150 is a misread document or a value that belongs in the
    AMOUNT column beside it — both are things a human has to look at, and clamping to 100
    would turn either into a plausible-looking number nobody chose.
    """
    s = str(v).strip().replace("%", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        pct = float(s)
    except ValueError:
        return None
    if pct < 0 or pct > 100:
        return None
    return str(int(pct)) if pct == int(pct) else f"{pct:g}"


def _clean_date(v) -> Optional[str]:
    """A date box -> MMDDYYYY. '12/31/2025', '2025-12-31' and '12312025' all land there.

    UNCONFIRMED on this Drake build — no INT run has read one back yet. Every spec that
    uses this kind marks the field `confirm`, so the plan says so and the form check after
    the run is what settles it. Rejecting an ambiguous date is deliberate: a date that
    Drake mis-parses is a wrong date on a return, and there is no read-back that would
    tell the difference between 01/02 and 02/01.
    """
    s = str(v).strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    # ISO first — the only unambiguous written form.
    if len(s) >= 10 and s[4] in "-/" and s[7] in "-/" and len(digits) == 8:
        y, m, d = digits[:4], digits[4:6], digits[6:]
    elif len(digits) == 8:
        m, d, y = digits[:2], digits[2:4], digits[4:]
    elif len(digits) == 6:
        m, d, y = digits[:2], digits[2:4], digits[4:]
        y = f"20{y}" if int(y) < 70 else f"19{y}"
    else:
        return None
    if not (1 <= int(m) <= 12 and 1 <= int(d) <= 31 and 1900 <= int(y) <= 2100):
        return None
    return f"{m}{d}{y}"


# Kinds whose value is a QUANTITY, where cutting characters off the end changes the number
# rather than shortening a description. A `text` box may be trimmed to fit and reported; one
# of these may not.
NUMERIC_KINDS = frozenset({"money", "digits", "pct", "tin", "ein", "ssn", "zip", "year"})


# Characters that are a Drake SELECTION CODE in their own right, beyond letters and digits.
# Measured 2026-08-13 by reading the 1099-R screen's "Pension type" list out of the control:
# it offers 44 codes and SEVEN of them are symbols — '@' Arizona, '#' Connecticut,
# '*' Pennsylvania ESOP, '%' New York, '&' and '$' and '=' Maryland.
#
# Worth stating plainly, because refusing them would be the same defect as the IL Schedule M
# list that only knew A-Z: our own planner turning away a value Drake accepts.
DRAKE_CODE_SYMBOLS = frozenset("#$%&*=@")


def _clean_code_alnum(v) -> Optional[str]:
    """A Drake dropdown SELECTION CODE: one or two characters, letters, digits or symbols.

    Covers Q1/Q3/Q4 on the DIV screen, the 1099-R distribution codes (which are single
    characters mixing digits and letters — 1, 7, G), and the pension-type symbols above.

    Deliberately NOT a loosening of the `code` kind. That one is the W-2's Box 12 code and it
    REJECTS anything carrying a digit on purpose: 'D 23' has a prior-year designation that
    belongs in its own box, and stripping it to 'D' measures the whole amount against the
    current year's deferral limit. The rule is right there and wrong here, so these stay two
    kinds rather than one kind with an exception.

    The LENGTH BOUND is what keeps this honest. Every Drake selection code measured so far is
    one or two characters, so a longer string is not a code — it is the descriptive text
    printed beside it ('Q1 - QSB stock 50% acquired after 08/10/1993'), and compressing that
    into something code-shaped is how a value nobody chose ends up in a box. Longer input
    returns None and becomes a loud skip.
    """
    s = str(v).strip().upper()
    if not 1 <= len(s) <= 2:
        return None
    return s if all(ch.isalnum() or ch in DRAKE_CODE_SYMBOLS for ch in s) else None


def _clean_code_form(v) -> Optional[str]:
    """A Drake code naming a SCHEDULE OR FORM: 'A', 'C', 'E', 'F', '4835', '8829'.

    Separate from `code_an` only because of length. That kind caps at two characters, and
    the cap is what stops a descriptive sentence being squeezed into something code-shaped
    — but this list genuinely contains four-character codes, so the same cap would refuse
    Form 4835 and Form 8829 outright. Four is the measured maximum for the 1098 screen's
    "For:" selector, read out of the control on 2026-08-15.

    No symbols. `code_an` allows seven of them because Drake's pension-type list really
    uses them; no form number does, and allowing them here would only widen what gets
    through without matching anything real.
    """
    s = str(v).strip().upper()
    if not 1 <= len(s) <= 4:
        return None
    return s if s.isalnum() else None


def _clean_country(v) -> Optional[str]:
    """A Drake COUNTRY CODE — exactly two letters. A country NAME is refused.

    DRAKE'S CODES ARE NOT ISO, AND THE COLLISIONS ARE SILENT. Measured 2026-08-15 by reading
    the 1098 screen's country list (258 codes) out of the control. Of ten common ISO codes
    checked, five are valid Drake codes naming a DIFFERENT REAL COUNTRY:

        ISO ES Spain       -> Drake ES is El Salvador   (Spain is SP)
        ISO CH Switzerland -> Drake CH is China         (Switzerland is SZ)
        ISO AU Australia   -> Drake AU is Austria       (Australia is AS)
        ISO SE Sweden      -> Drake SE is Seychelles    (Sweden is SW)
        ISO AT Austria     -> Drake AT is Ashmore And Cartier Islands

    This is the one case in this codebase where the `values` membership check cannot help:
    every code above IS on Drake's list, so it passes, gets typed, is echoed back, and reads
    back off the form as a real selection. Nothing downstream can tell that the wrong country
    was chosen. That is why every country field is marked `confirm` and why the plan prints
    the country NAME Drake will select beside the code — the name is the only thing a human
    reviewing the plan can actually check.

    Refusing a NAME here is the cheap half of the defence: no rule shortens 'Switzerland'
    into 'SZ', so anything longer than a code must never reach the comparison looking
    code-shaped.
    """
    s = "".join(str(v).split()).upper()
    return s if len(s) == 2 and s.isalpha() else None


def sanitize(kind: str, value, *, checkbox_token: str = "X") -> Optional[str]:
    """Value as Drake should receive it, or None meaning SKIP this field entirely."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if kind in ("digits", "tin"):
        return _digits(value)
    if kind == "tsj":
        return _clean_tsj(value)
    if kind == "pct":
        return _clean_pct(value)
    if kind == "date":
        return _clean_date(value)
    if kind == "code_an":
        return _clean_code_alnum(value)
    if kind == "code_form":
        return _clean_code_form(value)
    if kind == "country":
        return _clean_country(value)
    return _w2_sanitize(kind, value, checkbox_token=checkbox_token)


class FormSpec:
    """One Drake data-entry screen, described completely enough to plan an entry.

    `fields`         key -> {field_no, kind, label, max_len?, confirm?}
    `not_on_screen`  key -> why. Keys an extractor may legitimately produce that this
                     screen has no box for. Reported BY NAME, never lumped into the
                     generic unknown-key warning — a dropped identifier must be loud.
    `forbidden`      field_no -> why. Boxes that exist but must never be written: a
                     "<Click to Access>" sub-screen, a greyed-out box, a foreign-only
                     field on a domestic document. Enforced at import.
    `dropdowns`      field numbers that are a selection on Drake's screen, not a text box.
                     Typing into one that has no matching entry echoes in the popup and
                     stores NOTHING (measured on the W-2's Box 20), so every dropdown is
                     flagged for a human's eye until it is in `dropdowns_confirmed`.
    `locality_fields` field_no -> the payload key holding the STATE for that row. Those
                     boxes store a CODE out of Drake's own STATELIB\\CITY.HLP table, not
                     the name printed on the document.
    `identity_keys`  keys that say WHOSE return this document belongs to. They are how the
                     agent finds the client, they are already verified against Drake's own
                     window title by the time anything is typed, and they have no box on
                     this screen. Acknowledged in one line rather than reported as unknown
                     keys — "IGNORED" is the wrong word for the value the whole run was
                     targeted with.
    """

    def __init__(self, *, screen: str, label: str, fields: dict, max_field: int,
                 not_on_screen: Optional[dict] = None, forbidden: Optional[dict] = None,
                 dropdowns: Optional[set] = None, dropdowns_confirmed: Optional[set] = None,
                 locality_fields: Optional[dict] = None, hand_entry: Optional[dict] = None,
                 identity_keys: Optional[set] = None,
                 dedupe_key: Optional[str] = None, record_noun: str = "record",
                 notes: Optional[list] = None):
        self.screen = screen.upper()
        self.label = label
        self.fields = fields
        self.max_field = int(max_field)
        self.not_on_screen = not_on_screen or {}
        self.forbidden = forbidden or {}
        self.dropdowns = set(dropdowns or ())
        self.dropdowns_confirmed = set(dropdowns_confirmed or ())
        self.locality_fields = locality_fields or {}
        self.hand_entry = hand_entry or {}
        self.identity_keys = set(identity_keys or ())
        # The value that identifies THIS document among others already on the screen — the
        # employer EIN on a W-2, the payer TIN on a 1099. Entering the same one twice
        # doubles a client's income and no read-back would ever catch it, because every
        # value would verify perfectly.
        self.dedupe_key = dedupe_key
        self.record_noun = record_noun
        self.notes = list(notes or ())
        self.validate()

    @property
    def schema_keys(self) -> tuple:
        return tuple(self.fields.keys()) + tuple(self.not_on_screen)

    def validate(self) -> None:
        """Fail at IMPORT if this spec could write into a box it must never touch.

        A wrong field number is the one error class with no downstream defense: the value
        looks plausible in the wrong box and the operator sees a green run. Making that
        class of mistake un-importable is cheaper than catching it after a keystroke.
        """
        where = f"{self.screen} field map"
        nums = [(k, s["field_no"]) for k, s in self.fields.items()]
        bad = sorted((k, n) for k, n in nums if n in self.forbidden)
        if bad:
            detail = "; ".join(f"{k} -> {n}: {self.forbidden[n]}" for k, n in bad)
            raise RuntimeError(f"{where}: key(s) bound to a FORBIDDEN Drake field — {detail}")
        seen: dict = {}
        for k, n in nums:
            if n in seen:
                raise RuntimeError(f"{where}: field {n} is claimed by both {seen[n]!r} and {k!r}")
            seen[n] = k
        out_of_range = sorted((k, n) for k, n in nums if not 1 <= n <= self.max_field)
        if out_of_range:
            raise RuntimeError(
                f"{where}: field number(s) outside the CONFIRMED range 1-{self.max_field} "
                f"(higher numbers were not legible on the reference screen): {out_of_range}")
        overlap = set(self.fields) & set(self.not_on_screen)
        if overlap:
            raise RuntimeError(f"{where}: key(s) both mapped and marked not-on-screen: "
                               f"{sorted(overlap)}")
        unknown_kinds = sorted({s["kind"] for s in self.fields.values()} - {
            "money", "ein", "ssn", "tin", "digits", "zip", "state", "code", "code_an",
            "code_form", "country", "year", "ts", "tsj", "checkbox", "text", "pct", "date"})
        if unknown_kinds:
            raise RuntimeError(f"{where}: unknown value kind(s) {unknown_kinds} — sanitize() "
                               f"would silently fall through to plain text")
        stray = sorted(n for n in self.locality_fields if n not in seen)
        if stray:
            raise RuntimeError(f"{where}: locality_fields names field(s) {stray} that no key "
                               f"maps to")


def build_plan(payload: dict, spec: FormSpec, *, checkbox_token: str = "X",
               include_zeros: bool = False, skip_fields=None, extra=None) -> dict:
    """Turn one extracted document into an ordered, fully-resolved entry plan.

    Returns {"screen", "entries", "skipped", "skipped_by_request", "not_on_screen",
             "hand_entry", "unknown_keys", "warnings"} — the same shape `w2_map.build_plan`
    returns, because `agent.py` consumes it and there must be exactly one plan format.

    Nothing is dropped silently: a key that isn't in the map, a key with no box on this
    screen, or a value that sanitizes to nothing is reported, so `--dry-run` shows the
    complete picture before anything is typed.
    """
    skip = {int(n) for n in (skip_fields or [])}
    entries, skipped, by_request, not_on_screen = [], [], [], []
    hand_entry, unknown, warnings, identity = [], [], [], []
    payload = dict(payload or {})
    for k, v in (extra or {}).items():
        if v is not None:
            payload[k] = v

    for key, raw in payload.items():
        if key.startswith("_") or key in ("drake_screen", "doc_type"):
            continue
        if key in spec.identity_keys:
            identity.append(key)
            continue
        if key in spec.not_on_screen:
            not_on_screen.append({"key": key, "raw": raw, "why": spec.not_on_screen[key]})
            continue
        field = spec.fields.get(key)
        if field is None:
            unknown.append(key)
            continue
        no = field["field_no"]

        # A locality box stores a CODE out of Drake's own table; the name printed on the
        # document is only the way in. Resolved BEFORE sanitization because the name that
        # arrives is not the string that gets typed.
        if no in spec.locality_fields and _has_content(raw):
            row_state = payload.get(spec.locality_fields[no])
            res = resolve_locality(row_state, raw)
            row = {"key": key, "field_no": no, "label": field["label"], "kind": field["kind"],
                   "value": res["code"], "raw": raw, "confirm": True, "trimmed_from": None,
                   "resolved_from": _clean_text(raw), "locality_name": res["name"]}
            cap = field.get("max_len")
            if res["code"] and cap and len(res["code"]) > int(cap):
                # A code is not text: cutting it short does not shorten a name, it names a
                # DIFFERENT locality. Refuse rather than truncate.
                res = {**res, "code": None,
                       "reason": f"Drake's code for it ({res['code']!r}) is longer than the "
                                 f"{cap}-character box, so it cannot be typed without "
                                 f"becoming a different code."}
                row["value"] = None
            if res["code"] is None:
                row["value"] = _clean_text(raw)
                row["why"] = res["reason"]
                hand_entry.append(row)
                continue
            warnings.append(
                f"field {no} ({field['label']}): {_clean_text(raw)!r} resolved to Drake's "
                f"locality code {res['code']!r} ({res['name']}) for "
                f"{str(row_state).strip().upper()}, matched by {res['how']}. The CODE is what "
                f"is typed and what Drake stores — the box will then show Drake's own name.")
            (by_request if no in skip else entries).append(row)
            continue

        val = sanitize(field["kind"], raw, checkbox_token=checkbox_token)

        # Boxes that accept a FIXED SET of codes and nothing else. Drake answers a value
        # outside the set with its own validation window — which then holds the keyboard
        # until a human clicks OK, so the run halts several fields later with a message
        # about focus rather than about the value that caused it. Measured 2026-08-12 on
        # the INT screen's field 2 (Federal code, '0' or blank).
        #
        # Refusing here costs nothing: it happens before Drake is even open.
        allowed = field.get("values")
        if val is not None and allowed and val not in allowed:
            skipped.append({"key": key, "field_no": no, "label": field["label"], "raw": raw,
                            "rejected": True})
            # Print the list, but not a list nobody will read. The 1098 country boxes have
            # 258 legal codes, and spelling all of them into a warning buries the one thing
            # the reader needs — which value was refused — under four lines of noise. A
            # warning that is skimmed is a warning that did not happen.
            opts = sorted(allowed)
            shown = (f"{opts[:12]} and {len(opts) - 12} more" if len(opts) > 12 else f"{opts}")
            warnings.append(
                f"REJECTED {key} = {raw!r} — field {no} ({field['label']}) accepts only "
                f"{shown} on this screen, and Drake refuses anything else with a "
                f"validation window that then holds the keyboard. It was NOT entered.")
            continue

        # Drake enforces a maximum length on some boxes and simply STOPS accepting
        # characters (measured live on the W-2's Box 14, which caps at 8). Trimming to the
        # box's real capacity is what a preparer does by hand; doing it SILENTLY is not.
        trimmed_from = None
        maxlen = field.get("max_len")
        if val is not None and maxlen and len(val) > int(maxlen):
            if field["kind"] in NUMERIC_KINDS:
                # Cutting a NUMBER to fit does not shorten it, it changes it: 6010 becomes
                # 601. Measured live on the DIV screen 2026-08-12, where field 68 is a
                # 3-character box — Drake took '601' from '6010' and the run halted on the
                # read-back because the echo did not match what was sent.
                #
                # That halt is what a wrong value looks like when it is caught. A trim would
                # have made it look like a success with a warning attached, and the warning
                # would sit under a value that is off by a factor of ten.
                skipped.append({"key": key, "field_no": no, "label": field["label"],
                                "raw": raw, "rejected": True})
                warnings.append(
                    f"REJECTED {key} = {raw!r} — field {no} ({field['label']}) holds only "
                    f"{maxlen} character(s) and this is a {field['kind']}. Cutting it to fit "
                    f"would enter {val[:int(maxlen)]!r}, a different number. It was NOT "
                    f"entered; check the box and key it by hand.")
                continue
            trimmed_from, val = val, val[:int(maxlen)]
        if val is None and include_zeros and field["kind"] == "money":
            val = "0" if str(raw).strip() not in ("", "None") else None
        if val is None:
            # "The box is genuinely empty/zero" and "the extractor gave us something we
            # could not turn into a valid value" are completely different outcomes and must
            # never look alike. The second is a data problem a human has to see.
            rejected = _has_content(raw) and field["kind"] not in ("money", "checkbox")
            if not rejected and field["kind"] == "money":
                rejected = _has_content(raw) and _clean_money(raw) is None and not _is_zeroish(raw)
            skipped.append({"key": key, "field_no": no, "label": field["label"], "raw": raw,
                            "rejected": rejected})
            if rejected:
                warnings.append(f"REJECTED {key} = {raw!r} — not a valid {field['kind']} for "
                                f"field {no} ({field['label']}). It was NOT entered; fix the "
                                f"extraction or enter that box by hand.")
            continue

        row = {"key": key, "field_no": no, "label": field["label"], "kind": field["kind"],
               "value": val, "raw": raw,
               "confirm": bool(field.get("confirm")) or no in spec.dropdowns
                          or trimmed_from is not None,
               "trimmed_from": trimmed_from}
        if trimmed_from is not None:
            warnings.append(f"field {no} ({field['label']}): {trimmed_from!r} was TRUNCATED to "
                            f"{val!r} — Drake's box holds {maxlen} characters. The short form "
                            f"is what will be on the return; check it still says what it needs to.")
        if no in skip:
            by_request.append(row)
            continue
        if no in spec.hand_entry:
            row["why"] = spec.hand_entry[no]
            hand_entry.append(row)
            continue
        entries.append(row)

    entries.sort(key=lambda e: e["field_no"])
    by_request.sort(key=lambda e: e["field_no"])

    for n in sorted(skip - {e["field_no"] for e in by_request}):
        warnings.append(f"--skip-field {n} was given but no extracted value maps to field {n} "
                        f"— nothing to skip there (check the number).")
    if by_request:
        warnings.append(f"{len(by_request)} field(s) held back BY REQUEST (not empty — you "
                        f"asked for them to be left alone): "
                        f"{', '.join(str(e['field_no']) for e in by_request)}.")
    for e in not_on_screen:
        warnings.append(f"{e['key']} = {e['raw']!r} was extracted but NOT entered: {e['why']}")
    if identity:
        warnings.append(f"{', '.join(sorted(identity))} identify the CLIENT, not this "
                        f"document — they are what the agent opened the right return with "
                        f"(and checked against Drake's own window title). Screen "
                        f"{spec.screen} has no box for them.")
    if unknown:
        warnings.append(f"{len(unknown)} key(s) not in the {spec.screen} field map were "
                        f"IGNORED: {', '.join(sorted(unknown))}")

    dropdowns = sorted({e["field_no"] for e in entries if e["field_no"] in spec.dropdowns})
    unconfirmed = [n for n in dropdowns if n not in spec.dropdowns_confirmed]
    confirmed = [n for n in dropdowns if n in spec.dropdowns_confirmed]
    if unconfirmed:
        warnings.append(f"field(s) {unconfirmed} are dropdowns on Drake's screen whose typed "
                        f"code has NOT been confirmed to select an entry on this build. A "
                        f"dropdown with no match ACCEPTS the typing, echoes it in the popup, "
                        f"and stores nothing — so the per-field gate passes and the box stays "
                        f"empty. The FORM CHECK after the run is what catches it; read those "
                        f"boxes on the screenshot.")
    if confirmed:
        warnings.append(f"field(s) {confirmed} are dropdowns; typing the code IS confirmed to "
                        f"select the entry on this build. Still worth an eye after a Drake "
                        f"version bump.")
    if any(e["kind"] == "date" for e in entries):
        warnings.append("date field(s): the format Drake accepts here has NOT been confirmed "
                        "on this build. MMDDYYYY is what is typed; check the box on the "
                        "screenshot reads the date you meant before trusting it.")
    if any(e["kind"] == "checkbox" for e in entries):
        warnings.append(f"checkbox field(s) — Drake's heads-down popup shows the TICK BOX "
                        f"itself for these, not a text box, so the tick is read back off the "
                        f"widget before it is committed. {checkbox_token!r} is tried first, "
                        f"then the other tokens; if none flips the tick the run HALTs with "
                        f"the box untouched.")
    if any(e["field_no"] in spec.locality_fields for e in entries + hand_entry):
        src = locality_source()
        warnings.append(
            f"locality codes were resolved against Drake's own table at {src} — the live "
            f"file, so it tracks Drake's updates rather than a copy of them."
            if src else
            "Drake's locality table (STATELIB\\CITY.HLP) was NOT found, so no locality could "
            "be resolved to the code Drake stores. Every one is left for a human. Set "
            "DRAKE_CITY_HLP if Drake is installed somewhere other than C:\\DRAKE25.")
    for note in spec.notes:
        warnings.append(note)
    for e in hand_entry:
        warnings.append(f"field {e['field_no']} ({e['label']}) = {e['value']!r} will NOT be "
                        f"typed: {e['why']}")

    return {"screen": spec.screen, "entries": entries, "skipped": skipped,
            "skipped_by_request": by_request, "not_on_screen": not_on_screen,
            "hand_entry": hand_entry, "unknown_keys": unknown, "identity_keys": identity,
            "warnings": warnings}


def format_plan(plan: dict) -> str:
    """The plan as a human-readable table — what `--dry-run` prints for review BEFORE any
    keystroke reaches Drake."""
    screen = plan.get("screen") or ""
    head = f"{len(plan['entries'])} field(s) will be entered on screen {screen}, in field-number order:"
    lines = ["", head, ""]
    lines.append(f"  {'fld':>4}  {'label':<38} {'value':<24} source key")
    lines.append(f"  {'-'*4}  {'-'*38} {'-'*24} {'-'*24}")
    for e in plan["entries"]:
        mark = " *" if e["confirm"] else "  "
        val = e["value"]
        # A locality's code is meaningless on its own — show what it came from, so the
        # reviewer can check the resolution and not just the two characters being typed.
        if e.get("resolved_from") and e["resolved_from"] != val:
            val = f"{val} <- {e['resolved_from']}"
        lines.append(f"  {e['field_no']:>4}{mark}{e['label']:<38} {val:<24} {e['key']}")
    if any(e["confirm"] for e in plan["entries"]):
        lines.append("")
        lines.append("  * = verify this box by eye on the screenshot (dropdown, checkbox, "
                     "date, or identity field)")
    if plan.get("skipped_by_request"):
        lines.append("")
        lines.append(f"  HELD BACK BY REQUEST ({len(plan['skipped_by_request'])}) — these had "
                     f"a value; you asked for the box to be left alone:")
        for e in plan["skipped_by_request"]:
            lines.append(f"      field {e['field_no']:<4} {e['label']:<38} would have been {e['value']!r}")
    if plan.get("not_on_screen"):
        lines.append("")
        lines.append(f"  NOT ON THIS SCREEN ({len(plan['not_on_screen'])}) — extracted, but "
                     f"screen {screen} has no box for it:")
        for e in plan["not_on_screen"]:
            lines.append(f"      {e['key']} = {e['raw']!r}")
            lines.append(f"        {e['why']}")
    if plan["skipped"]:
        lines.append("")
        lines.append(f"  skipped ({len(plan['skipped'])}) — empty or zero, so the box is left alone:")
        for s in plan["skipped"]:
            mark = "  !! REJECTED " if s.get("rejected") else "    "
            lines.append(f"{mark}field {s['field_no']:<4} {s['label']:<38} raw={s['raw']!r}")
    for w in plan["warnings"]:
        lines.append("")
        lines.append(f"  NOTE: {w}")
    return "\n".join(lines) + "\n"
