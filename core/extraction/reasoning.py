"""Reason over the amounts as a whole: which combination of readings can be true?

Fields are first picked one at a time, each from its own labelled candidates. That is where
"Total GST : 283.00" became the total payable: as a single field it looked like a perfectly good
"total". Nothing asked whether the *set* of amounts made sense.

This step does. It scores whole combinations of total / subtotal / CGST / SGST / IGST / total tax
drawn from each field's best few candidates (plus "absent"), and prefers, in order:

1. no hard rule broken (a bill cannot be smaller than its tax; a tax is not the bill; CGST equals
   SGST; the taxable value cannot exceed the bill beyond rounding),
2. the amounts add up: taxable + taxes + round-off = total (or rate x volume = amount),
3. the candidates that read most clearly,
4. the fewest changes to the independent picks.

Independent picks that already satisfy 1 and 2 are left alone, so a clean receipt is never
rewritten. When no combination satisfies rule 1, the least trustworthy amounts are cleared and
flagged: an impossible set is never returned. Every change leaves a plain-language note on the
field (``reasoned:<field>:<sentence>``) that the UI shows.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import product

from core.types import Candidate, FieldResult

NAMES = ("total", "subtotal", "cgst", "sgst", "igst", "tax_total")
COMPONENTS = ("cgst", "sgst", "igst")
ROUNDING = Decimal("1.00")          # a bill is rounded to the rupee, so subtotal may exceed total by <1
TWIN_TOLERANCE = Decimal("0.02")
PER_FIELD = 4
MAX_COMBINATIONS = 6000
CHANGE_COST = 0.05                  # every field touched costs something, so fewer changes win ties
CORROBORATED = 0.6                  # trust added when an independent check (rate x volume) agrees


@dataclass(frozen=True)
class _Option:
    value: Decimal | None
    cand: Candidate | None
    score: float


def _options(cands: list[Candidate], limit: int) -> list[_Option]:
    seen: set[Decimal] = set()
    out: list[_Option] = []
    for cand in cands:
        if isinstance(cand.normalized, Decimal) and cand.normalized not in seen:
            seen.add(cand.normalized)
            out.append(_Option(cand.normalized, cand, cand.rank_score))
        if len(out) == limit:
            break
    out.append(_Option(None, None, 0.0))            # "not on the receipt" is always possible
    return out


def _tax_sum(values: dict[str, Decimal | None]) -> Decimal:
    components = [values[n] for n in COMPONENTS if values.get(n) is not None]
    if components:
        return sum(components, Decimal("0"))
    return values.get("tax_total") or Decimal("0")


def violations(values: dict[str, Decimal | None]) -> list[str]:
    """Hard-rule breaches, as short codes. Empty means the combination is possible."""
    out: list[str] = []
    total, subtotal = values.get("total"), values.get("subtotal")
    taxes = [v for n in (*COMPONENTS, "tax_total") if (v := values.get(n)) is not None]
    if total is not None:
        biggest = max([_tax_sum(values), *taxes], default=Decimal("0"))
        if biggest > 0 and total <= biggest:
            out.append("total_not_above_tax")
        if subtotal is not None and subtotal > total + ROUNDING:
            out.append("taxable_above_total")
    cgst, sgst = values.get("cgst"), values.get("sgst")
    if cgst is not None and sgst is not None and abs(cgst - sgst) > TWIN_TOLERANCE:
        out.append("cgst_sgst_differ")
    return out


def arithmetic(values: dict[str, Decimal | None], round_off: Decimal,
               expected: Decimal | None) -> bool | None:
    """Do the amounts add up? ``None`` when there is not enough to judge."""
    total, subtotal = values.get("total"), values.get("subtotal")
    if total is None:
        return None
    if subtotal is not None:
        gap = abs(total - (subtotal + _tax_sum(values) + round_off))
        return gap <= max(Decimal("0.02"), total * Decimal("0.005"))
    if expected is not None:
        return abs(total - expected) <= max(ROUNDING, expected * Decimal("0.005"))
    return None


_ARITH_RANK = {True: 0, None: 1, False: 2}
_SENTENCES = {
    "total": "Total amount taken from the line that reconciles with the tax and taxable value.",
    "subtotal": "Taxable value chosen so that taxable value plus tax equals the total.",
    "cgst": "CGST chosen so that it matches SGST and the total.",
    "sgst": "SGST chosen so that it matches CGST and the total.",
    "igst": "IGST chosen so that the amounts add up.",
    "tax_total": "Total tax chosen so that it matches the tax lines and the total.",
}


def resolve(fields: dict[str, FieldResult], candidates: dict[str, list[Candidate]],
            *, expected_amount: Decimal | None = None) -> list[str]:
    """Adopt the most plausible combination. Returns the names of the fields it changed."""

    def current(name: str) -> Decimal | None:
        fr = fields.get(name)
        return fr.normalized if fr is not None and isinstance(fr.normalized, Decimal) else None

    picked = {name: current(name) for name in NAMES}
    round_off = current("round_off") or Decimal("0")
    now_violations = violations(picked)
    now_arith = arithmetic(picked, round_off, expected_amount)
    if not now_violations:
        # Possible, so leave it. Amounts that merely fail to add up are NOT this step's business:
        # on damaged text that is usually one garbled figure, and swapping good values for
        # lower-ranked ones to force the sum made totals worse (measured). reconcile_amounts has
        # the careful single-field repairs for that.
        return []

    limit = PER_FIELD
    options = {n: _options(candidates.get(n, []), limit) for n in NAMES}
    while _size(options) > MAX_COMBINATIONS and limit > 2:
        limit -= 1
        options = {n: _options(candidates.get(n, []), limit) for n in NAMES}

    # A total that agrees with rate x volume is independently confirmed: it must not be the reading
    # that gets thrown away to make room for a garbled tax figure.
    if expected_amount is not None:
        slack = max(ROUNDING, expected_amount * Decimal("0.005"))
        options["total"] = [replace(o, score=o.score + CORROBORATED)
                            if o.value is not None and abs(o.value - expected_amount) <= slack else o
                            for o in options["total"]]

    # How much each independent pick was trusted. Fixing an impossible set means giving up the
    # readings we trust least, so the cost of a combination is the trust it throws away.
    trust = {n: next((o.score for o in options[n] if o.value == picked[n]), 0.0) for n in NAMES}

    best_key, best_combo = None, None
    for combo in product(*(options[n] for n in NAMES)):
        values = {n: o.value for n, o in zip(NAMES, combo)}
        bad = violations(values)
        if len(bad) > len(now_violations):
            continue
        arith = arithmetic(values, round_off, expected_amount)
        loss = sum(max(0.0, trust[n] - o.score) + CHANGE_COST
                   for n, o in zip(NAMES, combo) if values[n] != picked[n])
        key = (len(bad), round(loss, 6), _ARITH_RANK[arith], -round(sum(o.score for o in combo), 6))
        if best_key is None or key < best_key:
            best_key, best_combo = key, combo

    # Only act when it removes a violation.
    if best_combo is None or best_key[0] >= len(now_violations):
        return []

    changed: list[str] = []
    for name, option in zip(NAMES, best_combo):
        if option.value == picked[name]:
            continue
        fr = fields.get(name)
        if fr is None:
            fr = fields[name] = FieldResult(name=name)
        if option.cand is None:                      # cleared: it could not be true alongside the rest
            fr.value = fr.normalized = fr.span = fr.line_no = None
            fr.pattern_id = None
            fr.needs_review = True
            fr.warnings.append(f"reasoned:{name}:Removed because it cannot be true together with the "
                               "other amounts on this receipt. Please check it.")
        else:
            cand = option.cand
            fr.value, fr.normalized, fr.span = cand.value, cand.normalized, cand.span
            fr.line_no, fr.pattern_id = cand.line_no, cand.pattern_id
            fr.warnings.append(f"reasoned:{name}:{_SENTENCES[name]}")
        changed.append(name)
    return changed


def _size(options: dict[str, list[_Option]]) -> int:
    n = 1
    for opts in options.values():
        n *= len(opts)
    return n
