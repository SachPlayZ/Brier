"""Load the generated CSV dataset into the Django-free record types.

Used by the evaluation harness and the Django ingest command alike, so both
paths exercise exactly the same extraction and dedup code.
"""
from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.degrade import degrade_text
from core.dedup.blocking import phash_of
from core.extraction.linemodel import LineRoleModel
from core.extraction.pipeline import extract_receipt
from core.extraction.vendors import VendorGazetteer
from core.ocr import is_ocr_available, ocr_image
from core.types import ClaimRecord, ExtractionResult, OcrText


def load_gazetteer(data_dir: Path) -> VendorGazetteer | None:
    path = Path(data_dir) / "vendors.csv"
    return VendorGazetteer.from_csv(path) if path.exists() else None


def read_truth(data_dir: Path) -> dict[str, dict]:
    path = Path(data_dir) / "receipts_truth.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["receipt_id"]: row for row in csv.DictReader(fh)}


def read_claims(data_dir: Path) -> list[dict]:
    path = Path(data_dir) / "claims.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def receipt_text(data_dir: Path, receipt_id: str, noise_tier: int,
                 mode: str = "simulated", seed: int = 0) -> OcrText:
    """Get receipt text in one of three modes.

    ``clean``      the exact rendered sidecar -- upper bound, not realistic
    ``simulated``  sidecar corrupted to match the tier (the default)
    ``ocr``        real Tesseract over the rendered image, when installed
    """
    data_dir = Path(data_dir)
    if mode == "ocr" and is_ocr_available():
        image = data_dir / "receipts_images" / f"{receipt_id}.png"
        if image.exists():
            return ocr_image(image)
    text = (data_dir / "receipts_text" / f"{receipt_id}.txt").read_text(encoding="utf-8")
    if mode == "clean":
        return OcrText(text, [0.97] * len(text), "sidecar")
    return degrade_text(text, noise_tier, seed=seed)


def extract_all(data_dir: Path, *, mode: str = "simulated",
                line_model: LineRoleModel | None = None,
                limit: int | None = None,
                progress=None) -> dict[str, ExtractionResult]:
    """Run extraction over every receipt in the dataset."""
    data_dir = Path(data_dir)
    gaz = load_gazetteer(data_dir)
    truth = read_truth(data_dir)
    results: dict[str, ExtractionResult] = {}

    for i, (receipt_id, row) in enumerate(truth.items()):
        if limit is not None and i >= limit:
            break
        tier = int(row.get("noise_tier") or 0)
        ocr = receipt_text(data_dir, receipt_id, tier, mode=mode, seed=i)
        results[receipt_id] = extract_receipt(ocr, gazetteer=gaz,
                                              line_model=line_model,
                                              noisy=tier >= 2)
        if progress and (i + 1) % 100 == 0:
            progress(f"extracted {i + 1}/{len(truth)}")
    return results


def build_claim_records(data_dir: Path,
                        results: dict[str, ExtractionResult],
                        *, with_phash: bool = True,
                        progress=None) -> list[ClaimRecord]:
    """Join claims to their extracted receipt fields.

    Note the records are built from *extracted* values, not ground truth -- the
    duplicate detector must work on what the pipeline actually read, including
    its mistakes, or the evaluation would be measuring an unreachable ideal.
    """
    data_dir = Path(data_dir)
    truth = read_truth(data_dir)
    phash_cache: dict[str, int | None] = {}
    records: list[ClaimRecord] = []

    for i, row in enumerate(read_claims(data_dir)):
        receipt_id = row["receipt_id"]
        result = results.get(receipt_id)
        truth_row = truth.get(receipt_id, {})

        phash = None
        if with_phash:
            if receipt_id not in phash_cache:
                image = data_dir / "receipts_images" / f"{receipt_id}.png"
                phash_cache[receipt_id] = phash_of(image) if image.exists() else None
            phash = phash_cache[receipt_id]

        text = ""
        sidecar = data_dir / "receipts_text" / f"{receipt_id}.txt"
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8")

        records.append(ClaimRecord(
            claim_id=row["claim_id"],
            employee_id=row["employee_id"],
            receipt_id=receipt_id,
            vendor_raw=_vendor_printed(result),
            vendor_canonical=_canonical(result, truth_row),
            gstin=str(result.get("gstin")) if result and result.get("gstin") else None,
            invoice_no=str(result.get("invoice_no")) if result and result.get("invoice_no") else None,
            date=result.get("date") if result else None,
            total=_decimal(result.get("total")) if result else _decimal(row.get("claimed_amount")),
            submitted_at=_datetime(row.get("submitted_at")),
            status=row.get("status", "SUBMITTED"),
            decided_at=_datetime(row.get("decided_at")),
            phash=phash,
            text=text,
        ))
        if progress and (i + 1) % 200 == 0:
            progress(f"records {i + 1}")
    return records


def _vendor_printed(result: ExtractionResult | None) -> str | None:
    """The vendor name as printed, kept separate from its canonical id."""
    if result is None:
        return None
    fr = result.fields.get("vendor")
    return fr.value if fr is not None and fr.value else None


def _canonical(result: ExtractionResult | None, truth_row: dict) -> str | None:
    """Only a gazetteer-resolved vendor counts as canonical.

    A raw header line that never matched a known vendor must stay
    uncanonicalised, otherwise dedup would compare free text against ids.
    """
    if result is None:
        return None
    value = result.get("vendor")
    if isinstance(value, str) and len(value) == 4 and value.startswith("V"):
        return value
    return None


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
