"""Duplicate-detection evaluation.

Blocking recall is reported separately from, and before, scoring quality. A
precision/recall figure computed only over candidate pairs is self-congratulatory:
it silently excludes every duplicate that blocking never proposed. Measuring the
two stages apart is the only way to know which one to fix.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from core.dedup.blocking import blocking_stats, generate_candidates
from core.dedup.pipeline import find_duplicates
from core.dedup.scoring import BAND_RANK, DEFAULT_WEIGHTS, band_for, weighted_score
from core.dedup.signals import SIGNAL_NAMES, TextIndex, all_signals
from core.types import ClaimRecord, DuplicatePair

BANDS_DESC = ["EXACT", "HIGH", "MEDIUM", "LOW"]


@dataclass
class DedupReport:
    blocking: dict = field(default_factory=dict)
    by_band: list[dict] = field(default_factory=list)
    by_dup_type: list[dict] = field(default_factory=list)
    ablation: list[dict] = field(default_factory=list)
    pr_curve: list[dict] = field(default_factory=list)
    workload: dict = field(default_factory=dict)
    average_precision: float = 0.0
    n_pairs_flagged: int = 0

    def as_dict(self) -> dict:
        return {
            "blocking": self.blocking,
            "by_band": self.by_band,
            "by_dup_type": self.by_dup_type,
            "signal_ablation": self.ablation,
            "pr_curve": self.pr_curve,
            "workload": self.workload,
            "average_precision": self.average_precision,
            "n_pairs_flagged": self.n_pairs_flagged,
        }


def load_truth_pairs(path: Path, *, close: bool = True
                     ) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str],
                                set[tuple[str, str]]]:
    """Return ``(positive_pairs, dup_type_by_pair, labelled_negative_pairs)``.

    Duplicate-ness is an equivalence relation, but the generator only records
    the edges it injected: if a receipt is claimed three times it writes
    (A,B) and (A,C) and never (B,C). Scoring against those raw edges would
    count the detector's entirely correct discovery of B~C as a false positive.
    ``close=True`` takes the transitive closure so the ground truth is
    self-consistent.
    """
    positives: set[tuple[str, str]] = set()
    negatives: set[tuple[str, str]] = set()
    types: dict[tuple[str, str], str] = {}
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = _key(row["claim_a"], row["claim_b"])
            types[key] = row.get("dup_type", "")
            (positives if row.get("label") == "1" else negatives).add(key)

    if close:
        positives = _transitive_closure(positives, types)
    return positives, types, negatives - positives


def _transitive_closure(pairs: set[tuple[str, str]],
                        types: dict[tuple[str, str], str]) -> set[tuple[str, str]]:
    """Expand duplicate edges into full equivalence classes."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)

    closed: set[tuple[str, str]] = set()
    for members in groups.values():
        members.sort()
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                key = _key(a, b)
                closed.add(key)
                types.setdefault(key, "TRANSITIVE")
    return closed


def evaluate_dedup(claims: Sequence[ClaimRecord], truth_csv: Path,
                   *, weights: dict[str, float] | None = None,
                   run_ablation: bool = True) -> DedupReport:
    positives, types, _negatives = load_truth_pairs(truth_csv)
    report = DedupReport()

    candidates = generate_candidates(claims)
    report.blocking = blocking_stats(claims, candidates, positives)

    pairs = find_duplicates(claims, min_band="LOW", weights=weights)
    report.n_pairs_flagged = len(pairs)
    flagged = {p.key: p for p in pairs}

    for band in BANDS_DESC:
        cutoff = BAND_RANK[band]
        predicted = {key for key, p in flagged.items() if BAND_RANK.get(p.band, 0) >= cutoff}
        report.by_band.append({"band": f"{band}+", **_prf(predicted, positives)})

    # Per-duplicate-type recall at the actionable threshold (MEDIUM and above).
    actionable = {key for key, p in flagged.items() if BAND_RANK.get(p.band, 0) >= 2}
    by_type: dict[str, list[int]] = {}
    for key in positives:
        dup_type = types.get(key, "UNKNOWN")
        hit, total = by_type.setdefault(dup_type, [0, 0])
        by_type[dup_type] = [hit + (1 if key in actionable else 0), total + 1]
    report.by_dup_type = [
        {"dup_type": name, "found": hit, "total": total,
         "recall": round(hit / total, 4) if total else 0.0}
        for name, (hit, total) in sorted(by_type.items())]

    report.pr_curve, report.average_precision = _pr_curve(pairs, positives)
    n_claims = max(len(claims), 1)
    top50 = sorted(pairs, key=lambda p: -p.score)[:50]
    report.workload = {
        "pairs_per_1000_claims": round(len(actionable) * 1000 / n_claims, 2),
        "precision_at_top_50": round(
            sum(1 for p in top50 if p.key in positives) / max(len(top50), 1), 4),
        "reviewer_pairs_medium_plus": len(actionable),
    }

    if run_ablation:
        report.ablation = _ablation(claims, positives, weights or DEFAULT_WEIGHTS)
    return report


def _prf(predicted: set, positives: set) -> dict:
    tp = len(predicted & positives)
    fp = len(predicted - positives)
    fn = len(positives - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4)}


def _pr_curve(pairs: Sequence[DuplicatePair], positives: set) -> tuple[list[dict], float]:
    ranked = sorted(pairs, key=lambda p: -p.score)
    tp = 0
    rows: list[dict] = []
    ap = 0.0
    n_positive = max(len(positives), 1)
    for i, pair in enumerate(ranked, start=1):
        if pair.key in positives:
            tp += 1
            ap += (tp / i) / n_positive
        if i % max(len(ranked) // 20, 1) == 0 or i == len(ranked):
            rows.append({"k": i, "score": pair.score,
                         "precision": round(tp / i, 4),
                         "recall": round(tp / n_positive, 4)})
    return rows, round(ap, 4)


def _ablation(claims: Sequence[ClaimRecord], positives: set,
              weights: dict[str, float]) -> list[dict]:
    """Zero each signal in turn and report the F1 it was worth.

    Rules are disabled throughout, so this measures the weighted signal blend
    alone. With rules active every delta is zero -- the rule fires on the same
    evidence whatever the weights say -- which tells you nothing.
    """
    baseline = _f1_at_high(claims, positives, weights)
    rows = [{"signal": "(baseline, rules off)", "f1": baseline, "delta": 0.0}]
    for name in SIGNAL_NAMES:
        if name not in weights:
            continue
        trimmed = {k: v for k, v in weights.items() if k != name}
        if not trimmed:
            continue
        f1 = _f1_at_high(claims, positives, trimmed)
        rows.append({"signal": name, "f1": f1, "delta": round(f1 - baseline, 4)})
    return rows


def _f1_at_high(claims: Sequence[ClaimRecord], positives: set,
                weights: dict[str, float]) -> float:
    """F1 at the HIGH cutoff from the weighted signals only, rules disabled."""
    return _score_weights(claims, positives, weights)


# ------------------------------------------------------------- weight tuning
def precompute_signals(claims: Sequence[ClaimRecord]
                       ) -> list[tuple[tuple[str, str], dict[str, float], bool]]:
    """Signals for every candidate pair: ``(pair_key, signals, same_employee)``.

    Signals do not depend on the weights, so the expensive work -- blocking and
    building the TF-IDF index -- is done once and reused across every weight
    vector the search tries.
    """
    index = TextIndex(claims)
    by_id = {c.claim_id: c for c in claims}
    out = []
    for a_id, b_id in generate_candidates(claims):
        a, b = by_id.get(a_id), by_id.get(b_id)
        if a is None or b is None:
            continue
        out.append((_key(a_id, b_id), all_signals(a, b, index),
                    a.employee_id == b.employee_id))
    return out


def tune_weights(claims: Sequence[ClaimRecord], truth_csv: Path,
                 *, n_samples: int = 200, seed: int = 0,
                 dev_fraction: float = 0.6) -> dict:
    """Random search over the weight simplex, fit on a dev split only.

    The test split is never scored during the search: reporting a tuned number
    on the data it was tuned against would be meaningless.
    """
    positives, _types, _neg = load_truth_pairs(truth_csv)
    rng = random.Random(seed)

    ids = sorted({c.claim_id for c in claims})
    rng.shuffle(ids)
    cut = int(len(ids) * dev_fraction)
    dev_ids, test_ids = set(ids[:cut]), set(ids[cut:])

    dev_pairs = precompute_signals([c for c in claims if c.claim_id in dev_ids])
    test_pairs = precompute_signals([c for c in claims if c.claim_id in test_ids])
    dev_truth = {k for k in positives if k[0] in dev_ids and k[1] in dev_ids}
    test_truth = {k for k in positives if k[0] in test_ids and k[1] in test_ids}

    best = dict(DEFAULT_WEIGHTS)
    best_f1 = _f1_from_signals(dev_pairs, dev_truth, best)

    for _ in range(n_samples):
        raw = {name: rng.random() for name in SIGNAL_NAMES}
        total = sum(raw.values()) or 1.0
        candidate = {name: round(value / total, 4) for name, value in raw.items()}
        f1 = _f1_from_signals(dev_pairs, dev_truth, candidate)
        if f1 > best_f1:
            best, best_f1 = candidate, f1

    return {
        "weights": best,
        "dev_f1": best_f1,
        "dev_f1_default": _f1_from_signals(dev_pairs, dev_truth, DEFAULT_WEIGHTS),
        "test_f1": _f1_from_signals(test_pairs, test_truth, best),
        "test_f1_default": _f1_from_signals(test_pairs, test_truth, DEFAULT_WEIGHTS),
        "n_dev_claims": len(dev_ids), "n_test_claims": len(test_ids),
        "n_dev_pairs": len(dev_truth), "n_test_pairs": len(test_truth),
    }


def _f1_from_signals(pairs, truth: set, weights: dict[str, float]) -> float:
    """F1 at the HIGH cutoff over precomputed signals, rules disabled."""
    if not pairs or not truth:
        return 0.0
    predicted = {key for key, signals, same_emp in pairs
                 if BAND_RANK.get(band_for(weighted_score(signals, weights, same_emp)), 0) >= 3}
    return _prf(predicted, truth)["f1"]


def _score_weights(claims: Sequence[ClaimRecord], truth: set,
                   weights: dict[str, float]) -> float:
    """F1 at the HIGH cutoff for a claim list, rules disabled."""
    if not claims or not truth:
        return 0.0
    return _f1_from_signals(precompute_signals(claims), truth, weights)


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)
