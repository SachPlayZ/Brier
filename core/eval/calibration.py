"""Confidence calibration metrics.

A confidence score is only useful if it is *calibrated*: of the fields scored
0.9, about 90% should actually be right. These functions test that claim, which
is what turns the confidence number from decoration into something finance can
set a policy on.
"""
from __future__ import annotations

from typing import Sequence


def reliability_table(confidences: Sequence[float], correct: Sequence[bool],
                      bins: int = 10) -> list[dict]:
    """Bucket predictions by confidence and compare mean confidence to accuracy."""
    rows: list[dict] = []
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        picked = [(c, ok) for c, ok in zip(confidences, correct)
                  if (low <= c < high) or (b == bins - 1 and c == 1.0)]
        if not picked:
            rows.append({"bin": f"{low:.1f}-{high:.1f}", "n": 0,
                         "mean_conf": None, "accuracy": None, "gap": None})
            continue
        mean_conf = sum(c for c, _ in picked) / len(picked)
        accuracy = sum(1 for _, ok in picked if ok) / len(picked)
        rows.append({"bin": f"{low:.1f}-{high:.1f}", "n": len(picked),
                     "mean_conf": round(mean_conf, 4),
                     "accuracy": round(accuracy, 4),
                     "gap": round(accuracy - mean_conf, 4)})
    return rows


def expected_calibration_error(confidences: Sequence[float],
                               correct: Sequence[bool], bins: int = 10) -> float:
    """ECE: sample-weighted mean gap between confidence and accuracy."""
    n = len(confidences)
    if n == 0:
        return 0.0
    total = 0.0
    for row in reliability_table(confidences, correct, bins):
        if row["n"]:
            total += (row["n"] / n) * abs(row["gap"])
    return round(total, 4)


def brier_score(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    if not confidences:
        return 0.0
    return round(sum((c - (1.0 if ok else 0.0)) ** 2
                     for c, ok in zip(confidences, correct)) / len(confidences), 4)


def risk_coverage(confidences: Sequence[float],
                  correct: Sequence[bool]) -> list[dict]:
    """Accuracy when only the most-confident X% of predictions are accepted."""
    paired = sorted(zip(confidences, correct), key=lambda p: -p[0])
    n = len(paired)
    if n == 0:
        return []
    rows = []
    for coverage in (0.5, 0.8, 0.9, 1.0):
        take = max(int(n * coverage), 1)
        subset = paired[:take]
        accuracy = sum(1 for _c, ok in subset if ok) / take
        rows.append({"coverage": coverage, "n": take,
                     "accuracy": round(accuracy, 4),
                     "min_confidence": round(subset[-1][0], 4)})
    return rows


def threshold_for_accuracy(confidences: Sequence[float], correct: Sequence[bool],
                           target: float = 0.99,
                           min_coverage: float = 0.05) -> dict | None:
    """The confidence cutoff above which accuracy reaches ``target``.

    This is what should set the auto-accept threshold -- an empirical cutoff
    derived from measured accuracy, rather than a number somebody guessed.
    """
    paired = sorted(zip(confidences, correct), key=lambda p: -p[0])
    n = len(paired)
    if n == 0:
        return None
    correct_so_far = 0
    best: dict | None = None
    for i, (conf, ok) in enumerate(paired, start=1):
        correct_so_far += 1 if ok else 0
        accuracy = correct_so_far / i
        if accuracy >= target and i / n >= min_coverage:
            best = {"threshold": round(conf, 4), "coverage": round(i / n, 4),
                    "accuracy": round(accuracy, 4), "n": i}
    return best
