"""Deterministic duplicate rules and the two fraud-shaped patterns.

Rules run before the weighted score and short-circuit it. They exist because
some evidence is not probabilistic: the same invoice number from the same
vendor is the same bill, and no combination of weak signals should be able to
dilute that below the reviewer's attention.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from itertools import combinations
from typing import Sequence

from rapidfuzz import fuzz

from core.dedup.blocking import hamming
from core.normalize import norm_invoice_no, norm_vendor
from core.types import ClaimRecord, DuplicatePair

VENDOR_MATCH_THRESHOLD = 90

#: Perceptual hashes of *text documents* are far less distinctive than of
#: photographs: every receipt is a dark-on-light block of monospace lines, so
#: the low-frequency structure phash keys on is nearly identical across
#: unrelated bills. Measured here, a distance of 4 covers 1.2% of random pairs
#: -- roughly 1,500 spurious pairs in a 500-claim corpus. So the threshold is
#: tight AND the rule requires a corroborating field before it forces EXACT.
PHASH_IDENTICAL = 2
BAND_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH", "EXACT"]


def apply_rules(a: ClaimRecord, b: ClaimRecord,
                signals: dict[str, float]) -> tuple[str | None, list[str]]:
    """Return ``(forced_band_or_None, reasons)``."""
    reasons: list[str] = []
    forced: str | None = None

    same_vendor = _same_vendor(a, b)
    same_employee = a.employee_id == b.employee_id
    same_currency = not (a.currency and b.currency) or a.currency == b.currency
    same_amount = (a.total is not None and b.total is not None and same_currency
                   and abs(a.total - b.total) <= Decimal("0.01"))
    same_date = a.date is not None and b.date is not None and a.date == b.date
    inv_a, inv_b = norm_invoice_no(a.invoice_no), norm_invoice_no(b.invoice_no)
    same_invoice = len(inv_a) >= 4 and inv_a == inv_b

    # R1 -- the same bill, whoever submitted it.
    if same_invoice and same_vendor:
        forced = "EXACT"
        reasons.append("SAME_INVOICE_SAME_VENDOR")

    # R2 -- same person, vendor, day and amount.
    if same_employee and same_vendor and same_date and same_amount:
        forced = "EXACT"
        reasons.append("SAME_EMP_VENDOR_DATE_AMOUNT")

    # R3 -- the same photograph, corroborated. The image alone is not enough
    # (see PHASH_IDENTICAL); it must agree with the vendor or the amount.
    distance = hamming(a.phash, b.phash)
    if (distance is not None and distance <= PHASH_IDENTICAL and same_employee
            and (same_vendor or same_amount)):
        forced = "EXACT"
        reasons.append(f"IDENTICAL_IMAGE(d={distance})")

    # R4 -- everything matches except the invoice number, which is the shape of
    # a doctored resubmission rather than a coincidence.
    if (same_employee and same_vendor and same_date and same_amount
            and inv_a and inv_b and inv_a != inv_b):
        if forced != "EXACT":
            forced = "HIGH"
        reasons.append("SUSPECT_ALTERED_INVOICE_NO")

    return forced, reasons


def _same_vendor(a: ClaimRecord, b: ClaimRecord) -> bool:
    if a.gstin and b.gstin and a.gstin.upper() == b.gstin.upper():
        return True
    if a.vendor_canonical and b.vendor_canonical:
        return a.vendor_canonical == b.vendor_canonical
    va, vb = norm_vendor(a.vendor_raw), norm_vendor(b.vendor_raw)
    if not va or not vb:
        return False
    return fuzz.token_set_ratio(va, vb) >= VENDOR_MATCH_THRESHOLD


def bump_band(band: str, steps: int = 1) -> str:
    idx = BAND_ORDER.index(band) if band in BAND_ORDER else 0
    return BAND_ORDER[min(idx + steps, len(BAND_ORDER) - 1)]


def tag_resubmissions(pairs: list[DuplicatePair],
                      by_id: dict[str, ClaimRecord]) -> list[DuplicatePair]:
    """Flag claims resubmitted after the original was rejected.

    Cheap: needs only status and timestamps, both already on the record. A
    rejected claim coming back in a slightly different shape is the single most
    common reimbursement abuse, so it gets a band bump.
    """
    out: list[DuplicatePair] = []
    for pair in pairs:
        a, b = by_id.get(pair.a), by_id.get(pair.b)
        if a is None or b is None or a.employee_id != b.employee_id:
            out.append(pair)
            continue
        first, second = _order_by_submission(a, b)
        rejected = first.status.upper() in {"REJECTED", "NEEDS_INFO"}
        after = (first.decided_at is not None and second.submitted_at is not None
                 and second.submitted_at > first.decided_at)
        if rejected and after and pair.band in {"MEDIUM", "HIGH", "EXACT", "LOW"}:
            out.append(DuplicatePair(
                a=pair.a, b=pair.b, score=pair.score,
                band=bump_band(pair.band) if pair.band != "EXACT" else "EXACT",
                signals=pair.signals,
                rules_fired=[*pair.rules_fired, "RESUBMIT_AFTER_REJECT"],
                tags=[*pair.tags, "RESUBMIT_AFTER_REJECT"]))
        else:
            out.append(pair)
    return out


def _order_by_submission(a: ClaimRecord, b: ClaimRecord) -> tuple[ClaimRecord, ClaimRecord]:
    if a.submitted_at is None or b.submitted_at is None:
        return a, b
    return (a, b) if a.submitted_at <= b.submitted_at else (b, a)


def detect_split_claims(claims: Sequence[ClaimRecord], *,
                        policy_limit: Decimal | None = None,
                        window_days: int = 7,
                        max_group: int = 8) -> list[DuplicatePair]:
    """One bill submitted as several smaller claims.

    Two shapes are detected: a subset of small claims summing to another
    claim's total (the literal split), and a run of claims each sitting just
    under the approval limit (threshold gaming). Bounded by ``max_group`` so
    the combinatorial search stays cheap.
    """
    out: list[DuplicatePair] = []
    groups: dict[tuple[str, str, int], list[ClaimRecord]] = {}
    for c in claims:
        if c.total is None or c.date is None:
            continue
        vendor = c.vendor_canonical or norm_vendor(c.vendor_raw)
        if not vendor:
            continue
        groups.setdefault((c.employee_id, vendor, c.date.toordinal() // 2), []).append(c)

    for members in groups.values():
        if not 3 <= len(members) <= max_group:
            continue
        for whole in members:
            others = [m for m in members if m.claim_id != whole.claim_id]
            for size in (2, 3, 4):
                if len(others) < size:
                    break
                for combo in combinations(others, size):
                    subtotal = sum((m.total for m in combo), Decimal("0"))
                    if abs(subtotal - whole.total) <= whole.total * Decimal("0.01"):
                        for part in combo:
                            out.append(DuplicatePair(
                                a=whole.claim_id, b=part.claim_id,
                                score=0.70, band="MEDIUM", signals={},
                                rules_fired=["SPLIT_SUSPECT"],
                                tags=["SPLIT_SUSPECT"]))
                        break

    if policy_limit is not None:
        out.extend(_threshold_gaming(claims, policy_limit, window_days))
    return _dedupe(out)


def _threshold_gaming(claims: Sequence[ClaimRecord], limit: Decimal,
                      window_days: int) -> list[DuplicatePair]:
    """Three or more claims in a week, each just under the approval limit."""
    floor = limit * Decimal("0.85")
    by_employee: dict[str, list[ClaimRecord]] = {}
    for c in claims:
        if c.total is not None and floor <= c.total < limit and c.date is not None:
            by_employee.setdefault(c.employee_id, []).append(c)

    out: list[DuplicatePair] = []
    for members in by_employee.values():
        members.sort(key=lambda m: m.date)
        for i, anchor in enumerate(members):
            window = [m for m in members[i:]
                      if (m.date - anchor.date).days <= window_days]
            if len(window) >= 3:
                for other in window[1:]:
                    out.append(DuplicatePair(
                        a=anchor.claim_id, b=other.claim_id, score=0.55,
                        band="LOW", signals={}, rules_fired=["THRESHOLD_GAMING"],
                        tags=["THRESHOLD_GAMING"]))
                break
    return out


def _dedupe(pairs: list[DuplicatePair]) -> list[DuplicatePair]:
    seen: dict[tuple[str, str], DuplicatePair] = {}
    for pair in pairs:
        if pair.a == pair.b:
            continue
        existing = seen.get(pair.key)
        if existing is None or pair.score > existing.score:
            seen[pair.key] = pair
    return list(seen.values())


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
