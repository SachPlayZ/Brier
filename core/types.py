"""Shared dataclasses for the extraction and dedup cores.

Deliberately dependency-free (stdlib only) so `core` stays importable and
testable without Django, and so the Django layer can convert its models into
these at a single seam (``expenses/services.py``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OcrText:
    """Receipt text plus per-character confidence.

    ``char_conf`` is parallel to ``text`` so a matched span can be scored
    directly. ``source`` is 'sidecar' (native rendered text) or 'tesseract'.
    """

    text: str
    char_conf: list[float]
    source: str

    def span_conf(self, span: tuple[int, int] | None) -> float:
        if span is None or not self.char_conf:
            return 0.95 if self.source == "sidecar" else 0.5
        lo, hi = span
        window = self.char_conf[lo:hi]
        if not window:
            return 0.95 if self.source == "sidecar" else 0.5
        return sum(window) / len(window)


@dataclass(frozen=True)
class Candidate:
    """One possible value for a field, before scoring picks a winner."""

    value: str
    normalized: Any
    span: tuple[int, int]
    line_no: int
    pattern_id: str
    rank_score: float = 0.0


@dataclass
class FieldResult:
    name: str
    value: str | None = None
    normalized: Any = None
    span: tuple[int, int] | None = None
    line_no: int | None = None
    pattern_id: str | None = None
    confidence: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    needs_review: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.normalized is not None


@dataclass
class ExtractionResult:
    fields: dict[str, FieldResult] = field(default_factory=dict)
    doc_confidence: float = 0.0
    currency: str = "INR"
    arithmetic_ok: bool | None = None
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        fr = self.fields.get(name)
        return default if fr is None or fr.normalized is None else fr.normalized

    def conf(self, name: str) -> float:
        fr = self.fields.get(name)
        return 0.0 if fr is None else fr.confidence

    def to_row(self) -> dict[str, Any]:
        """Flat dict for CSV export and the evaluation harness."""
        row: dict[str, Any] = {"doc_confidence": round(self.doc_confidence, 4),
                               "arithmetic_ok": self.arithmetic_ok}
        for name, fr in self.fields.items():
            row[name] = fr.normalized
            row[f"{name}_conf"] = round(fr.confidence, 4)
        return row


@dataclass(frozen=True)
class ClaimRecord:
    """Django-free view of a claim. Built by ``expenses.services.to_claim_record``."""

    claim_id: str
    employee_id: str
    vendor_raw: str | None = None
    vendor_canonical: str | None = None
    gstin: str | None = None
    invoice_no: str | None = None
    date: dt.date | None = None
    total: Decimal | None = None
    currency: str = ""
    submitted_at: dt.datetime | None = None
    status: str = "SUBMITTED"
    decided_at: dt.datetime | None = None
    phash: int | None = None
    text: str = ""
    receipt_id: str | None = None


@dataclass(frozen=True)
class DuplicatePair:
    a: str
    b: str
    score: float
    band: str
    signals: dict[str, float] = field(default_factory=dict)
    rules_fired: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.a, self.b) if self.a <= self.b else (self.b, self.a)

    def reasons(self) -> list[str]:
        """Human-readable explanation of why this pair was flagged."""
        out = list(self.rules_fired)
        labels = {
            "invoice": "invoice no",
            "amount": "amount",
            "vendor": "vendor name",
            "date": "date",
            "image": "receipt image",
            "text": "receipt text",
        }
        for name, value in sorted(self.signals.items(), key=lambda kv: -kv[1]):
            if value >= 0.75:
                out.append(f"{labels.get(name, name)} match ({value:.2f})")
        return out
