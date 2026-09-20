"""Pairwise duplicate signals.

Each signal is independent, returns ``0..1``, and returns ``None`` when it
cannot be computed for this pair (a missing invoice number, no image, ...).

``None`` is not zero. A missing invoice number means "no evidence", whereas
zero means "positive evidence they differ". Imputing zero for missing data
would manufacture evidence of innocence, so ``scoring.py`` renormalizes the
weights over the signals that actually exist.
"""
from __future__ import annotations

from decimal import Decimal

from rapidfuzz import fuzz

from core.dedup.blocking import hamming
from core.normalize import norm_invoice_no, norm_vendor
from core.types import ClaimRecord

#: Below this, a perceptual-hash similarity is indistinguishable from noise.
#: Measured on this corpus (see the eval report's phash section): unrelated
#: receipt pairs have a median Hamming distance of 12 and 15% of them fall
#: within 8, because receipts share layout and phash keys on low-frequency
#: structure. A floor of 0.90 (distance 6) keeps ~60% of true duplicates while
#: admitting only ~5% of random pairs -- and even then it is only ever a
#: weighted signal, never decisive on its own.
PHASH_NOISE_FLOOR = 0.90

SIGNAL_NAMES = ("invoice", "amount", "vendor", "date", "image", "text")


def s_invoice(a: ClaimRecord, b: ClaimRecord) -> float | None:
    ia, ib = norm_invoice_no(a.invoice_no), norm_invoice_no(b.invoice_no)
    if len(ia) < 4 or len(ib) < 4:
        return None
    if ia == ib:
        return 1.0
    ratio = fuzz.ratio(ia, ib)
    if ratio >= 90:
        return 0.8          # one OCR character apart -- still the same bill
    return 0.0


def s_vendor(a: ClaimRecord, b: ClaimRecord) -> float | None:
    if a.gstin and b.gstin:
        if a.gstin.upper() == b.gstin.upper():
            return 1.0      # a checksummed identifier beats any name similarity
    if a.vendor_canonical and b.vendor_canonical:
        return 1.0 if a.vendor_canonical == b.vendor_canonical else 0.0
    va, vb = norm_vendor(a.vendor_raw), norm_vendor(b.vendor_raw)
    if not va or not vb:
        return None
    return fuzz.token_set_ratio(va, vb) / 100.0


def s_amount(a: ClaimRecord, b: ClaimRecord) -> float | None:
    if a.total is None or b.total is None:
        return None
    # 100 USD and 100 INR are not the same claim. Comparing the bare numbers
    # across currencies would make every round figure look like a duplicate.
    if a.currency and b.currency and a.currency != b.currency:
        return 0.0
    diff = abs(a.total - b.total)
    if diff <= Decimal("0.01"):
        return 1.0
    larger = max(a.total, b.total)
    if larger == 0:
        return 1.0
    relative = diff / larger
    if diff <= Decimal("1.00") or relative <= Decimal("0.005"):
        return 0.95         # rounding drift or a re-typed paise value
    if relative >= Decimal("0.05"):
        return 0.0
    # Linear decay from 0.95 at 0.5% to 0.0 at 5%.
    return float(Decimal("0.95") * (Decimal("0.05") - relative) / Decimal("0.045"))


def s_date(a: ClaimRecord, b: ClaimRecord) -> float | None:
    if a.date is None or b.date is None:
        return None
    days = abs((a.date - b.date).days)
    for limit, score in ((0, 1.0), (1, 0.90), (3, 0.75), (7, 0.50), (30, 0.20)):
        if days <= limit:
            return score
    return 0.0


def s_image(a: ClaimRecord, b: ClaimRecord) -> float | None:
    distance = hamming(a.phash, b.phash)
    if distance is None:
        return None
    similarity = 1 - distance / 64
    return similarity if similarity >= PHASH_NOISE_FLOOR else 0.0


class TextIndex:
    """Cached TF-IDF index over the claim corpus for text-similarity scoring.

    Character n-grams rather than words: they survive OCR noise and catch a
    re-typed receipt whose wording shifted but whose content did not.
    """

    def __init__(self, claims=()):
        self._row: dict[str, int] = {}
        self._matrix = None
        self._vectorizer = None
        texts = [(c.claim_id, c.text or "") for c in claims]
        texts = [(cid, t) for cid, t in texts if t.strip()]
        if len(texts) < 2:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            self._vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                               min_df=1, max_features=50000)
            self._matrix = self._vectorizer.fit_transform([t for _cid, t in texts])
            self._row = {cid: i for i, (cid, _t) in enumerate(texts)}
        except Exception:
            self._matrix = None

    def similarity(self, a: ClaimRecord, b: ClaimRecord) -> float | None:
        if self._matrix is None:
            return None
        ia, ib = self._row.get(a.claim_id), self._row.get(b.claim_id)
        if ia is None or ib is None:
            return None
        va, vb = self._matrix[ia], self._matrix[ib]
        return float(va.multiply(vb).sum())      # rows are L2-normalized

    def cosine(self, text_a: str, text_b: str) -> float | None:
        """Ad-hoc similarity for text not in the index (a freshly uploaded claim)."""
        if self._vectorizer is None or not text_a.strip() or not text_b.strip():
            return None
        try:
            m = self._vectorizer.transform([text_a, text_b])
            return float(m[0].multiply(m[1]).sum())
        except Exception:
            return None


def all_signals(a: ClaimRecord, b: ClaimRecord,
                idx: TextIndex | None = None) -> dict[str, float]:
    """Every computable signal for a pair. Missing signals are simply absent."""
    raw = {
        "invoice": s_invoice(a, b),
        "amount": s_amount(a, b),
        "vendor": s_vendor(a, b),
        "date": s_date(a, b),
        "image": s_image(a, b),
        "text": _text_signal(a, b, idx),
    }
    return {name: value for name, value in raw.items() if value is not None}


def _text_signal(a: ClaimRecord, b: ClaimRecord, idx: TextIndex | None) -> float | None:
    if idx is None:
        return None
    value = idx.similarity(a, b)
    if value is None:
        value = idx.cosine(a.text or "", b.text or "")
    return value
