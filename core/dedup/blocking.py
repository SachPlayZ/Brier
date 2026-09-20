"""Candidate generation.

Comparing every claim against every other claim is O(n^2) and does not survive
contact with a real expense system. Blocking narrows it: a claim is only
compared against claims that share at least one cheap key with it.

Blocking quality is measured directly (``blocking_stats``) and reported as a
first-class metric, because a duplicate never proposed as a candidate can never
be caught no matter how good the scorer is.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from core.normalize import norm_invoice_no, norm_vendor
from core.types import ClaimRecord

WINDOW_DAYS = 180
MAX_BLOCK = 200
AMOUNT_BUCKET = Decimal("50")     # B4 near-amount bucket width, in rupees
PHASH_BANDS = 3
PHASH_BITS = 21

#: A block containing more than this many claims is not evidence of anything --
#: receipts share layout, so phash bands and common words collide in bulk. Such
#: a block is dropped rather than trimmed: trimming keeps an arbitrary slice of
#: a meaningless group, while dropping costs nothing because any genuine pair
#: inside it also shares a selective key (invoice no, vendor+amount, date).
DEGENERATE_BLOCK = 40

#: A token is only a useful block key if it is genuinely rare in the corpus.
MAX_TOKEN_DF = 5

_TOKEN_RE = re.compile(r"[A-Za-z]{4,}")
_STOPWORDS = {"invoice", "total", "amount", "receipt", "gstin", "date", "bill",
              "thank", "visit", "again", "customer", "copy", "original", "tax"}


def phash_of(path: str | Path) -> int | None:
    """Perceptual hash of a receipt image as a 64-bit int.

    Perceptual (not cryptographic) so a re-scan, a rotation or a JPEG re-encode
    of the same physical receipt still lands close in Hamming distance.
    """
    try:
        import imagehash
        from PIL import Image

        with Image.open(path) as img:
            return int(str(imagehash.phash(img)), 16)
    except Exception:
        return None


def hamming(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


def block_keys(c: ClaimRecord) -> list[tuple[str, str]]:
    """All block keys for one claim, as ``(block_id, key)`` pairs."""
    keys: list[tuple[str, str]] = []

    inv = norm_invoice_no(c.invoice_no)
    if len(inv) >= 4:
        keys.append(("B1", inv))

    vendor = c.vendor_canonical or norm_vendor(c.vendor_raw)
    if vendor and c.total is not None:
        keys.append(("B2", f"{vendor}|{c.currency}|{int(c.total)}"))

    if c.date is not None:
        ordinal = c.date.toordinal()
        for delta in (-3, -2, -1, 0, 1, 2, 3):
            keys.append(("B3", f"{c.employee_id}|{ordinal + delta}"))

    if c.total is not None and c.date is not None:
        bucket = int(c.total / AMOUNT_BUCKET)
        week = c.date.isocalendar()[:2]
        # Amount buckets are only comparable within one currency.
        # Adjacent buckets too, so a value sitting on a boundary still meets its twin.
        for offset in (-1, 0, 1):
            keys.append(("B4", f"{c.employee_id}|{c.currency}|{bucket + offset}|{week[0]}-{week[1]}"))

    if c.phash is not None:
        for band in range(PHASH_BANDS):
            chunk = (c.phash >> (band * PHASH_BITS)) & ((1 << PHASH_BITS) - 1)
            keys.append(("B5", f"{band}:{chunk}"))

    for token in rare_tokens(c.text):
        keys.append(("B6", token))

    return keys


def rare_tokens(text: str, k: int = 3, df: dict[str, int] | None = None) -> list[str]:
    """The k rarest content tokens, used as a last-resort text block.

    Without corpus statistics every token looks rare, so a vendor's city or
    trading name -- which recurs on hundreds of receipts -- would be used as a
    block key. ``df`` filters to tokens that are actually uncommon.
    """
    tokens = {t.lower() for t in _TOKEN_RE.findall(text or "")} - _STOPWORDS
    if not tokens:
        return []
    if df is None:
        return sorted(tokens)[:k]
    eligible = [t for t in tokens if df.get(t, 1) <= MAX_TOKEN_DF]
    return sorted(eligible, key=lambda t: (df.get(t, 1), t))[:k]


def build_df(claims: Sequence[ClaimRecord]) -> dict[str, int]:
    """Document frequency per token across the claim corpus."""
    df: Counter[str] = Counter()
    for c in claims:
        df.update({t.lower() for t in _TOKEN_RE.findall(c.text or "")} - _STOPWORDS)
    return dict(df)


def build_idf(claims: Sequence[ClaimRecord]) -> dict[str, float]:
    n = max(len(claims), 1)
    return {token: math.log(n / (1 + count)) for token, count in build_df(claims).items()}


def generate_candidates(
    claims: Sequence[ClaimRecord],
    *,
    window_days: int = WINDOW_DAYS,
    max_block: int = MAX_BLOCK,
) -> set[tuple[str, str]]:
    """All candidate pairs, deduplicated and date-windowed."""
    df = build_df(claims)
    buckets: dict[tuple[str, str], list[ClaimRecord]] = defaultdict(list)
    for c in claims:
        for key in _keys_with_df(c, df):
            buckets[key].append(c)

    pairs: set[tuple[str, str]] = set()
    for (block_id, _key), members in buckets.items():
        if len(members) < 2:
            continue
        if len(members) > DEGENERATE_BLOCK:
            continue
        if len(members) > max_block:
            members = _trim_block(members, max_block)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if not _within_window(a, b, window_days):
                    continue
                pairs.add(_pair_key(a.claim_id, b.claim_id))
    return pairs


def candidates_for(new: ClaimRecord, existing: Sequence[ClaimRecord], *,
                   window_days: int = WINDOW_DAYS,
                   max_candidates: int = 400) -> list[ClaimRecord]:
    """Incremental path: everything worth comparing against one new claim."""
    wanted = set(block_keys(new))
    out: dict[str, ClaimRecord] = {}
    for other in existing:
        if other.claim_id == new.claim_id:
            continue
        if not _within_window(new, other, window_days):
            continue
        if wanted & set(block_keys(other)):
            out[other.claim_id] = other
            if len(out) >= max_candidates:
                break
    return list(out.values())


def blocking_stats(claims: Sequence[ClaimRecord],
                   candidates: set[tuple[str, str]],
                   truth_pairs: Iterable[tuple[str, str]]) -> dict[str, float]:
    """Recall and reduction -- measured independently of any scoring."""
    truth = {_pair_key(a, b) for a, b in truth_pairs}
    n = len(claims)
    total_pairs = n * (n - 1) / 2 if n > 1 else 1
    hit = len(truth & candidates)
    return {
        "n_claims": n,
        "n_candidates": len(candidates),
        "all_pairs": total_pairs,
        "reduction_ratio": round(1 - len(candidates) / total_pairs, 6) if total_pairs else 0.0,
        "blocking_recall": round(hit / len(truth), 4) if truth else 1.0,
        "missed_pairs": len(truth) - hit,
        "candidates_per_claim": round(len(candidates) * 2 / n, 2) if n else 0.0,
    }


# --------------------------------------------------------------------- helpers
def _keys_with_df(c: ClaimRecord, df: dict[str, int]) -> list[tuple[str, str]]:
    keys = [k for k in block_keys(c) if k[0] != "B6"]
    keys.extend(("B6", token) for token in rare_tokens(c.text, df=df))
    return keys


def _trim_block(members: list[ClaimRecord], limit: int) -> list[ClaimRecord]:
    """Keep the ``limit`` members closest in amount -- the most likely twins."""
    known = [m for m in members if m.total is not None]
    if len(known) < 2:
        return members[:limit]
    known.sort(key=lambda m: m.total)
    return known[:limit]


def _within_window(a: ClaimRecord, b: ClaimRecord, window_days: int) -> bool:
    if a.date is None or b.date is None:
        return True
    return abs((a.date - b.date).days) <= window_days


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def days_between(a: dt.date | None, b: dt.date | None) -> int | None:
    return None if a is None or b is None else abs((a - b).days)
