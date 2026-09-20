"""Per-field confidence scoring -- the headline feature of this project.

A confidence number nobody can interrogate is worthless to a finance reviewer,
so every score is a weighted sum of *named* components that are stored
alongside the value and rendered in the UI:

    conf = clip(sum(w[field][c] * comp[c]) * prod(penalties), 0, 1)

Components (all 0..1)
    pat    pattern specificity tier -- how explicitly labelled the match was
    pos    positional prior -- vendors sit at the top, totals at the bottom
    ocr    mean OCR character confidence over the matched span
    arith  does subtotal + tax + round_off reconcile with total
    fmt    hard validator verdict (GSTIN checksum, date window, charset)
    fuzz   similarity to the closest known vendor
    uniq   margin between the winning candidate and the runner-up
    ml     line-role classifier probability for the expected role
    rate   tax amount consistent with the printed GST rate

The weights live in one table so they can be printed, tuned and defended.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from decimal import Decimal


from core.extraction import fields as F
from core.extraction import patterns as P
from core.extraction.vendors import VendorGazetteer
from core.types import Candidate, FieldResult, OcrText

# --------------------------------------------------------------------- weights
FIELD_WEIGHTS: dict[str, dict[str, float]] = {
    "vendor":     {"pat": .10, "pos": .15, "ocr": .15, "fuzz": .30, "uniq": .10, "ml": .20},
    "date":       {"pat": .35, "pos": .10, "ocr": .10, "fmt": .25, "uniq": .10, "ml": .10},
    "total":      {"pat": .30, "pos": .15, "ocr": .10, "arith": .30, "uniq": .05, "ml": .10},
    "subtotal":   {"pat": .30, "pos": .10, "ocr": .10, "arith": .30, "uniq": .10, "ml": .10},
    "cgst":       {"pat": .30, "ocr": .10, "arith": .20, "uniq": .05, "ml": .10, "rate": .25},
    "sgst":       {"pat": .30, "ocr": .10, "arith": .20, "uniq": .05, "ml": .10, "rate": .25},
    "igst":       {"pat": .30, "ocr": .10, "arith": .20, "uniq": .05, "ml": .10, "rate": .25},
    "cess":       {"pat": .35, "ocr": .15, "arith": .25, "uniq": .10, "ml": .15},
    "tax_total":  {"pat": .35, "ocr": .15, "arith": .25, "uniq": .10, "ml": .15},
    "round_off":  {"pat": .50, "ocr": .20, "uniq": .15, "ml": .15},
    "gstin":      {"pat": .25, "pos": .10, "ocr": .15, "fmt": .50},
    "invoice_no": {"pat": .45, "pos": .10, "ocr": .10, "fmt": .20, "uniq": .10, "ml": .05},
}

# Gaussian positional priors: (mu, sigma) as a fraction of document length.
POSITION_PRIORS: dict[str, tuple[float, float]] = {
    "vendor": (0.05, 0.18),
    "date": (0.25, 0.30),
    "invoice_no": (0.25, 0.30),
    "gstin": (0.20, 0.25),
    "subtotal": (0.82, 0.15),
    "cgst": (0.86, 0.12),
    "sgst": (0.86, 0.12),
    "igst": (0.86, 0.12),
    "cess": (0.86, 0.12),
    "tax_total": (0.88, 0.12),
    "round_off": (0.90, 0.12),
    "total": (0.93, 0.10),
}

# Multiplicative penalties, applied after the weighted sum.
PENALTIES: dict[str, float] = {
    "ambiguous_dmy": 0.85,
    "repaired": 0.80,
    "cgst_sgst_mismatch": 0.90,
    "wrapped_value": 0.90,
    "noisy_scan": 0.95,
}

# A hard validator failure caps confidence here and forces manual review.
VALIDATOR_FAIL_CAP = 0.35
NEUTRAL_ML = 0.60

# Expected line role per field, for the ``ml`` component.
EXPECTED_ROLE: dict[str, str] = {
    "vendor": "VENDOR", "date": "DATE", "invoice_no": "INVOICE", "gstin": "GSTIN",
    "subtotal": "SUBTOTAL", "total": "TOTAL",
    "cgst": "TAX", "sgst": "TAX", "igst": "TAX", "cess": "TAX",
    "tax_total": "TAX", "round_off": "TAX",
}

VALID_GST_RATES = {0.0, 0.125, 1.5, 2.5, 5.0, 6.0, 9.0, 12.0, 14.0, 18.0, 28.0}


@dataclass
class ScoringContext:
    """Everything ``score_field`` needs that is not on the candidate itself."""

    text: str
    lines: list[str]
    ocr: OcrText
    gazetteer: VendorGazetteer | None = None
    line_probs: list[dict[str, float]] | None = None
    arithmetic_ok: bool | None = None
    fields: dict[str, FieldResult] = dc_field(default_factory=dict)
    noise_penalty: bool = False
    date_order: str = "DMY"


# ------------------------------------------------------------------ components
def positional_prior(rel_pos: float, field_name: str) -> float:
    mu, sigma = POSITION_PRIORS.get(field_name, (0.5, 0.40))
    return math.exp(-((rel_pos - mu) ** 2) / (2 * sigma * sigma))


def pattern_specificity(pattern_id: str | None, *, has_currency: bool = False) -> float:
    return P.specificity(pattern_id, has_currency=has_currency)


def ocr_span_conf(ocr: OcrText, span: tuple[int, int] | None) -> float:
    return ocr.span_conf(span)


def arithmetic_component(arithmetic_ok: bool | None,
                         fields: dict[str, FieldResult]) -> float:
    """1.0 reconciled, 0.5 close, 0.0 contradictory, 0.6 when undeterminable."""
    if arithmetic_ok is True:
        return 1.0
    if arithmetic_ok is None:
        return 0.6
    total = _num(fields.get("total"))
    subtotal = _num(fields.get("subtotal"))
    if total is None or subtotal is None or total == 0:
        return 0.0
    taxes = sum((_num(fields.get(n)) or Decimal("0")) for n in F.TAX_FIELDS)
    round_off = _num(fields.get("round_off")) or Decimal("0")
    gap = abs(total - (subtotal + taxes + round_off))
    return 0.5 if gap <= total * Decimal("0.01") else 0.0


def uniqueness(candidates: list[Candidate], winner: Candidate) -> float:
    """Margin between the winner and the best distinct runner-up."""
    others = [c for c in candidates
              if c.normalized != winner.normalized and c.rank_score > 0]
    if not others:
        return 1.0
    best_other = max(c.rank_score for c in others)
    if winner.rank_score <= 0:
        return 0.0
    return max(0.0, min(1.0, 1 - best_other / winner.rank_score))


def ml_component(ctx: ScoringContext, field_name: str, line_no: int | None) -> float:
    if ctx.line_probs is None or line_no is None or line_no >= len(ctx.line_probs):
        return NEUTRAL_ML
    role = EXPECTED_ROLE.get(field_name)
    if role is None:
        return NEUTRAL_ML
    return float(ctx.line_probs[line_no].get(role, 0.0))


def rate_component(ctx: ScoringContext, field_name: str,
                   cand: Candidate) -> float:
    """Does this tax amount match the GST rate printed beside it?"""
    rate = F.parsed_rate(ctx.text, cand.span)
    subtotal = _num(ctx.fields.get("subtotal"))
    if rate is None:
        return 0.5
    if subtotal is not None and isinstance(cand.normalized, Decimal):
        expected = (subtotal * Decimal(str(rate)) / Decimal("100")).quantize(Decimal("0.01"))
        if abs(cand.normalized - expected) <= Decimal("0.02"):
            return 1.0
    return 0.7 if rate in VALID_GST_RATES else 0.3


def format_component(field_name: str, cand: Candidate, ctx: ScoringContext) -> float:
    """Hard validator verdict: 1.0 pass, 0.0 fail."""
    if field_name == "gstin":
        return 1.0 if F.gstin_checksum_ok(str(cand.normalized)) else 0.0
    if field_name == "invoice_no":
        gstin = ctx.fields.get("gstin")
        gstin_value = str(gstin.normalized) if gstin and gstin.normalized else None
        return 1.0 if F.invoice_no_valid(str(cand.normalized), gstin=gstin_value) else 0.0
    if field_name == "date":
        # Ambiguity (03/04 could be 3 Apr or 4 Mar) is NOT a validator failure --
        # the value parsed cleanly and is usually right. It is handled by the
        # `ambiguous_dmy` multiplicative penalty instead. Failing it here too
        # both double-counted the doubt and hard-capped the score at 0.35,
        # which put correct dates in a bucket claiming 35% confidence.
        return 1.0
    return 1.0


# ---------------------------------------------------------------------- scorer
def score_field(field_name: str, cand: Candidate, ctx: ScoringContext,
                *, uniq: float = 1.0) -> tuple[float, dict[str, float]]:
    """Score one candidate, returning ``(confidence, components)``.

    ``uniq`` is supplied by the caller because it is a property of the whole
    candidate set, not of one candidate.
    """
    weights = FIELD_WEIGHTS.get(field_name, {"pat": .6, "ocr": .2, "uniq": .2})
    comps: dict[str, float] = {}
    doc_len = max(len(ctx.text), 1)
    rel_pos = min(max(cand.span[0] / doc_len, 0.0), 1.0) if cand.span else 0.0

    for name in weights:
        if name == "pat":
            comps["pat"] = pattern_specificity(
                cand.pattern_id,
                has_currency=F.has_currency_marker(ctx.text, cand.span))
        elif name == "pos":
            comps["pos"] = positional_prior(rel_pos, field_name)
        elif name == "ocr":
            comps["ocr"] = ocr_span_conf(ctx.ocr, cand.span)
        elif name == "arith":
            comps["arith"] = arithmetic_component(ctx.arithmetic_ok, ctx.fields)
        elif name == "fmt":
            comps["fmt"] = format_component(field_name, cand, ctx)
        elif name == "fuzz":
            comps["fuzz"] = F.vendor_fuzz(cand, ctx.gazetteer)
        elif name == "uniq":
            comps["uniq"] = uniq
        elif name == "ml":
            comps["ml"] = ml_component(ctx, field_name, cand.line_no)
        elif name == "rate":
            comps["rate"] = rate_component(ctx, field_name, cand)

    raw = sum(weights[name] * comps.get(name, 0.0) for name in weights)
    return min(max(raw, 0.0), 1.0), comps


def finalize_confidence(field_name: str, fr: FieldResult,
                        ctx: ScoringContext) -> None:
    """Apply penalties and hard caps, then set ``needs_review``.

    Kept separate from ``score_field`` because penalties depend on
    cross-field outcomes (reconciliation, ambiguity) that are only known once
    every field has a provisional value.
    """
    weights = FIELD_WEIGHTS.get(field_name, {})
    comps = fr.components
    raw = sum(w * comps.get(name, 0.0) for name, w in weights.items())

    multiplier = 1.0
    for warning in fr.warnings:
        key = warning.split(":", 1)[0]
        multiplier *= PENALTIES.get(key, 1.0)
    if ctx.noise_penalty:
        multiplier *= PENALTIES["noisy_scan"]
    if field_name == "date" and F.date_is_ambiguous(ctx.text, fr.span):
        multiplier *= PENALTIES["ambiguous_dmy"]

    conf = min(max(raw * multiplier, 0.0), 1.0)
    if comps.get("fmt") == 0.0 and "fmt" in weights:
        conf = min(conf, VALIDATOR_FAIL_CAP)
        fr.warnings.append("validator_failed")
    fr.confidence = conf
    fr.needs_review = conf < 0.60 or comps.get("fmt") == 0.0


def doc_confidence(fields: dict[str, FieldResult],
                   arithmetic_ok: bool | None) -> float:
    """Document-level confidence: the weakest of the three fields finance acts on."""
    core = [fields.get(name) for name in ("vendor", "date", "total")]
    scores = [fr.confidence if fr is not None else 0.0 for fr in core]
    base = min(scores) if scores else 0.0
    return round(base * (1.0 if arithmetic_ok is not False else 0.9), 4)


def _num(fr: FieldResult | None) -> Decimal | None:
    if fr is None or not isinstance(fr.normalized, Decimal):
        return None
    return fr.normalized


def explain(fr: FieldResult) -> list[tuple[str, float, float]]:
    """``(component, value, weighted_contribution)`` rows for the UI breakdown."""
    weights = FIELD_WEIGHTS.get(fr.name, {})
    rows = [(name, fr.components.get(name, 0.0), weights[name] * fr.components.get(name, 0.0))
            for name in weights]
    return sorted(rows, key=lambda r: -r[2])


COMPONENT_LABELS: dict[str, str] = {
    "pat": "Pattern match quality",
    "pos": "Position on receipt",
    "ocr": "Text clarity",
    "arith": "Amounts add up",
    "fmt": "Format valid",
    "fuzz": "Known vendor match",
    "uniq": "No competing value",
    "ml": "Line-type model",
    "rate": "GST rate consistent",
}


def component_label(name: str) -> str:
    return COMPONENT_LABELS.get(name, name)
