"""Extraction evaluation against ``receipts_truth.csv``.

Reported per field *and* broken out by noise tier and template, because an
aggregate F1 hides the two failure modes that actually matter: a regex overfit
to one layout, and a pipeline that collapses on noisy scans.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz

from core.eval import calibration as cal
from core.normalize import norm_invoice_no, norm_vendor
from core.types import ExtractionResult

EVAL_FIELDS = ("vendor", "gstin", "invoice_no", "date", "subtotal",
               "cgst", "sgst", "igst", "total")

TRUTH_COLUMN = {"vendor": "vendor_canonical"}

#: Error classes, so a failure can be acted on rather than merely counted.
MISSED = "MISSED"
WRONG_VALUE = "WRONG_VALUE"
PARSE_FAIL = "PARSE_FAIL"
SPURIOUS = "SPURIOUS"


@dataclass
class FieldReport:
    name: str
    n: int = 0
    extracted: int = 0
    correct: int = 0
    lenient_correct: int = 0

    @property
    def precision(self) -> float:
        return round(self.correct / self.extracted, 4) if self.extracted else 0.0

    @property
    def recall(self) -> float:
        return round(self.correct / self.n, 4) if self.n else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    @property
    def extraction_rate(self) -> float:
        return round(self.extracted / self.n, 4) if self.n else 0.0

    def as_dict(self) -> dict:
        return {"field": self.name, "n": self.n, "extracted": self.extracted,
                "extraction_rate": self.extraction_rate, "correct": self.correct,
                "precision": self.precision, "recall": self.recall, "f1": self.f1,
                "lenient_recall": round(self.lenient_correct / self.n, 4) if self.n else 0.0}


@dataclass
class ExtractionReport:
    fields: dict[str, FieldReport] = field(default_factory=dict)
    by_tier: dict[str, dict[str, FieldReport]] = field(default_factory=dict)
    by_template: dict[str, dict[str, FieldReport]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    calibration: dict = field(default_factory=dict)
    doc_confidence: list[float] = field(default_factory=list)
    n_receipts: int = 0
    arithmetic_ok_rate: float = 0.0

    def as_dict(self) -> dict:
        return {
            "n_receipts": self.n_receipts,
            "arithmetic_ok_rate": self.arithmetic_ok_rate,
            "fields": [r.as_dict() for r in self.fields.values()],
            "by_noise_tier": {tier: [r.as_dict() for r in reports.values()]
                              for tier, reports in sorted(self.by_tier.items())},
            "by_template": {tpl: [r.as_dict() for r in reports.values()]
                            for tpl, reports in sorted(self.by_template.items())},
            "calibration": self.calibration,
            "n_errors": len(self.errors),
        }


# ---------------------------------------------------------------- comparators
def _norm_amount(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


def _norm_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _norm_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return norm_invoice_no(str(value)) or None


def _norm_vendor_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if text.startswith("V") and len(text) == 4 else norm_vendor(text)


COMPARATORS: dict[str, Callable[[Any], Any]] = {
    "vendor": _norm_vendor_value, "gstin": _norm_id, "invoice_no": _norm_id,
    "date": _norm_date, "subtotal": _norm_amount, "cgst": _norm_amount,
    "sgst": _norm_amount, "igst": _norm_amount, "total": _norm_amount,
}


def _lenient_match(name: str, predicted: Any, truth: Any) -> bool:
    """Vendor gets a fuzzy second chance; every other field is exact."""
    if name != "vendor" or predicted is None or truth is None:
        return False
    return fuzz.token_set_ratio(norm_vendor(str(predicted)),
                                norm_vendor(str(truth))) >= 90


# ------------------------------------------------------------------ evaluation
def evaluate_extraction(results: dict[str, ExtractionResult],
                        truth_csv: Path,
                        *, error_limit: int = 400) -> ExtractionReport:
    """Compare extraction output against ground truth."""
    report = ExtractionReport()
    confidences: list[float] = []
    correct_flags: list[bool] = []
    arithmetic_ok = 0

    with Path(truth_csv).open(newline="", encoding="utf-8") as fh:
        truth_rows = {row["receipt_id"]: row for row in csv.DictReader(fh)}

    for receipt_id, result in results.items():
        truth = truth_rows.get(receipt_id)
        if truth is None:
            continue
        report.n_receipts += 1
        report.doc_confidence.append(result.doc_confidence)
        if result.arithmetic_ok:
            arithmetic_ok += 1
        tier = truth.get("noise_tier", "?")
        template = truth.get("template_id", "?")

        for name in EVAL_FIELDS:
            truth_raw = truth.get(TRUTH_COLUMN.get(name, name), "")
            comparator = COMPARATORS[name]
            expected = comparator(truth_raw)
            predicted_raw = result.get(name)
            predicted = comparator(predicted_raw)
            fr = result.fields.get(name)
            conf = fr.confidence if fr else 0.0

            # Zero-valued taxes are legitimately absent from the receipt text;
            # scoring them as misses would punish the extractor for being right.
            if name in {"cgst", "sgst", "igst"} and expected == "0.00":
                continue

            for bucket in (report.fields,
                           report.by_tier.setdefault(tier, {}),
                           report.by_template.setdefault(template, {})):
                bucket.setdefault(name, FieldReport(name))

            entries = [report.fields[name], report.by_tier[tier][name],
                       report.by_template[template][name]]

            if expected is None:
                if predicted is not None:
                    _log(report, error_limit, receipt_id, name, SPURIOUS,
                         truth_raw, predicted_raw, fr, tier, template)
                continue

            for entry in entries:
                entry.n += 1
            if predicted is None:
                _log(report, error_limit, receipt_id, name, MISSED,
                     truth_raw, predicted_raw, fr, tier, template)
                confidences.append(conf)
                correct_flags.append(False)
                continue

            for entry in entries:
                entry.extracted += 1
            is_correct = predicted == expected
            lenient = is_correct or _lenient_match(name, predicted_raw, truth_raw)
            for entry in entries:
                entry.correct += 1 if is_correct else 0
                entry.lenient_correct += 1 if lenient else 0
            if not is_correct:
                _log(report, error_limit, receipt_id, name, WRONG_VALUE,
                     truth_raw, predicted_raw, fr, tier, template)

            confidences.append(conf)
            correct_flags.append(is_correct)

    report.arithmetic_ok_rate = round(arithmetic_ok / report.n_receipts, 4) if report.n_receipts else 0.0
    report.calibration = {
        "n": len(confidences),
        "ece": cal.expected_calibration_error(confidences, correct_flags),
        "brier": cal.brier_score(confidences, correct_flags),
        "reliability": cal.reliability_table(confidences, correct_flags),
        "risk_coverage": cal.risk_coverage(confidences, correct_flags),
        "auto_accept_at_99": cal.threshold_for_accuracy(confidences, correct_flags, 0.99),
        "auto_accept_at_95": cal.threshold_for_accuracy(confidences, correct_flags, 0.95),
    }
    return report


def _log(report: ExtractionReport, limit: int, receipt_id: str, field_name: str,
         kind: str, truth: Any, predicted: Any, fr, tier: str, template: str) -> None:
    if len(report.errors) >= limit:
        return
    report.errors.append({
        "receipt_id": receipt_id, "field": field_name, "error": kind,
        "truth": truth, "predicted": "" if predicted is None else str(predicted),
        "pattern_id": (fr.pattern_id if fr else "") or "",
        "confidence": round(fr.confidence, 4) if fr else 0.0,
        "noise_tier": tier, "template_id": template,
    })


def write_errors(report: ExtractionReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["receipt_id", "field", "error", "truth", "predicted",
               "pattern_id", "confidence", "noise_tier", "template_id"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(report.errors)
