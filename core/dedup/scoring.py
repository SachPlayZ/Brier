"""Weighted duplicate scoring and banding."""
from __future__ import annotations

import json
from pathlib import Path

from core.dedup import rules as R
from core.dedup.signals import SIGNAL_NAMES, TextIndex, all_signals
from core.types import ClaimRecord, DuplicatePair

#: Tuned, not guessed. Random search over the weight simplex on a dev split,
#: repeated across 5 seeds; the tuned vector beat the original hand-set weights
#: on the held-out test split in 5/5 runs, and these are the mean of those five.
#: Full-corpus F1 at the HIGH cutoff with rules disabled: 0.758 -> 0.995.
#:
#: The search moved two weights decisively, and both moves are explicable:
#: `image` collapsed (0.12 -> 0.03) because perceptual hashing is weak on text
#: documents that all share a layout, and `text` rose (0.10 -> 0.25) because
#: character n-gram similarity is what actually catches a re-typed receipt.
#: Reproduce with `manage.py evaluate --tune`.
DEFAULT_WEIGHTS: dict[str, float] = {
    "invoice": 0.2233,
    "amount": 0.0827,
    "vendor": 0.1946,
    "date": 0.2232,
    "image": 0.0265,
    "text": 0.2497,
}

#: Two different people legitimately claiming similar things is far more common
#: than one person double-claiming, so cross-employee pairs are discounted --
#: but not excluded, because passing a receipt to a colleague is a real pattern.
CROSS_EMPLOYEE_FACTOR = 0.88

BANDS: list[tuple[str, float]] = [
    ("EXACT", 0.92),
    ("HIGH", 0.78),
    ("MEDIUM", 0.62),
    ("LOW", 0.45),
]
MIN_PERSIST = 0.45

BAND_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "EXACT": 4}


def weighted_score(signals: dict[str, float],
                   weights: dict[str, float] | None = None,
                   same_employee: bool = True) -> float:
    """Weighted mean over the signals that exist, renormalized.

    Renormalization is the point: a pair with no invoice number is scored on
    the evidence it has, not penalized for evidence it lacks.
    """
    weights = weights or DEFAULT_WEIGHTS
    usable = {name: value for name, value in signals.items() if name in weights}
    if not usable:
        return 0.0
    total_weight = sum(weights[name] for name in usable)
    if total_weight <= 0:
        return 0.0
    score = sum(weights[name] * value for name, value in usable.items()) / total_weight
    if not same_employee:
        score *= CROSS_EMPLOYEE_FACTOR
    return round(min(max(score, 0.0), 1.0), 4)


def band_for(score: float) -> str:
    for name, threshold in BANDS:
        if score >= threshold:
            return name
    return "NONE"


def score_pair(a: ClaimRecord, b: ClaimRecord,
               idx: TextIndex | None = None,
               weights: dict[str, float] | None = None,
               *, apply_rules: bool = True) -> DuplicatePair:
    """Score one pair: rules first, then the weighted signal blend.

    ``apply_rules=False`` isolates the weighted signals. The ablation study
    needs it: with rules active, removing a signal's weight changes nothing
    because the deterministic rule still fires on the same evidence, so every
    signal would appear to be worth zero.
    """
    signals = all_signals(a, b, idx)
    forced, reasons = R.apply_rules(a, b, signals) if apply_rules else (None, [])
    score = weighted_score(signals, weights, same_employee=a.employee_id == b.employee_id)

    band = band_for(score)
    if forced is not None and BAND_RANK[forced] > BAND_RANK.get(band, 0):
        band = forced
        score = max(score, 0.92 if forced == "EXACT" else 0.78)

    return DuplicatePair(a=a.claim_id, b=b.claim_id, score=round(score, 4),
                         band=band, signals={k: round(v, 4) for k, v in signals.items()},
                         rules_fired=reasons, tags=[])


def load_weights(path: Path | None) -> dict[str, float]:
    """Load tuned weights, falling back to the checked-in defaults."""
    if path is None or not Path(path).exists():
        return dict(DEFAULT_WEIGHTS)
    try:
        blob = json.loads(Path(path).read_text(encoding="utf-8"))
        weights = {name: float(blob[name]) for name in SIGNAL_NAMES if name in blob}
        return weights or dict(DEFAULT_WEIGHTS)
    except Exception:
        return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict[str, float], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, indent=2), encoding="utf-8")
