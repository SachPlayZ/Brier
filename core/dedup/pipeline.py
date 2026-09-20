"""Duplicate-detection orchestrator: block, score, rule-tag, cluster."""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from core.dedup import rules as R
from core.dedup.blocking import candidates_for, generate_candidates
from core.dedup.scoring import BAND_RANK, MIN_PERSIST, score_pair
from core.dedup.signals import TextIndex
from core.types import ClaimRecord, DuplicatePair


def find_duplicates(
    claims: Sequence[ClaimRecord],
    *,
    min_band: str = "LOW",
    weights: dict[str, float] | None = None,
    policy_limit: Decimal | None = None,
    include_patterns: bool = True,
) -> list[DuplicatePair]:
    """Full-corpus pass. Used by ``rescore`` and the evaluation harness."""
    by_id = {c.claim_id: c for c in claims}
    index = TextIndex(claims)
    candidates = generate_candidates(claims)

    pairs: list[DuplicatePair] = []
    for a_id, b_id in candidates:
        a, b = by_id.get(a_id), by_id.get(b_id)
        if a is None or b is None:
            continue
        pair = score_pair(a, b, index, weights)
        if pair.score >= MIN_PERSIST or pair.rules_fired:
            pairs.append(pair)

    if include_patterns:
        pairs = R.tag_resubmissions(pairs, by_id)
        pairs = _merge(pairs, R.detect_split_claims(claims, policy_limit=policy_limit))

    threshold = BAND_RANK.get(min_band, 1)
    return sorted((p for p in pairs if BAND_RANK.get(p.band, 0) >= threshold),
                  key=lambda p: -p.score)


def find_duplicates_for_claim(
    new: ClaimRecord,
    existing: Sequence[ClaimRecord],
    *,
    min_band: str = "LOW",
    weights: dict[str, float] | None = None,
    index: TextIndex | None = None,
) -> list[DuplicatePair]:
    """Incremental path, run at submission time against prior claims."""
    neighbours = candidates_for(new, existing)
    if not neighbours:
        return []
    index = index or TextIndex([new, *neighbours])

    pairs = [score_pair(new, other, index, weights) for other in neighbours]
    by_id = {c.claim_id: c for c in (new, *neighbours)}
    pairs = R.tag_resubmissions(pairs, by_id)

    threshold = BAND_RANK.get(min_band, 1)
    return sorted((p for p in pairs if BAND_RANK.get(p.band, 0) >= threshold),
                  key=lambda p: -p.score)


def cluster(pairs: Sequence[DuplicatePair],
            bands: tuple[str, ...] = ("EXACT", "HIGH")) -> dict[str, int]:
    """Union-find over strong pairs, giving each claim a duplicate-group id.

    Finance resolves a *group* rather than n(n-1)/2 pairs: three submissions of
    one receipt should be one decision, not three.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for pair in pairs:
        if pair.band in bands:
            union(pair.a, pair.b)

    roots: dict[str, int] = {}
    out: dict[str, int] = {}
    for claim_id in parent:
        root = find(claim_id)
        if root not in roots:
            roots[root] = len(roots) + 1
        out[claim_id] = roots[root]
    return out


def _merge(base: list[DuplicatePair], extra: list[DuplicatePair]) -> list[DuplicatePair]:
    """Fold pattern-detected pairs in, keeping the stronger verdict per pair."""
    merged: dict[tuple[str, str], DuplicatePair] = {p.key: p for p in base}
    for pair in extra:
        existing = merged.get(pair.key)
        if existing is None:
            merged[pair.key] = pair
            continue
        merged[pair.key] = DuplicatePair(
            a=existing.a, b=existing.b,
            score=max(existing.score, pair.score),
            band=existing.band if BAND_RANK.get(existing.band, 0) >= BAND_RANK.get(pair.band, 0)
            else pair.band,
            signals=existing.signals,
            rules_fired=list(dict.fromkeys([*existing.rules_fired, *pair.rules_fired])),
            tags=list(dict.fromkeys([*existing.tags, *pair.tags])))
    return list(merged.values())
