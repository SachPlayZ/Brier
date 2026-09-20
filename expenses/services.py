"""The only seam between Django and the pure-Python core.

Every status transition goes through this module so that the audit trail and
the duplicate guard cannot be bypassed by a view that forgets to call them.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import object_storage
from core.currency import DEFAULT_CURRENCY
from core.dedup.blocking import phash_of
from core.dedup.pipeline import cluster, find_duplicates, find_duplicates_for_claim
from core.extraction.linemodel import LineRoleModel
from core.extraction.pipeline import extract_receipt
from core.extraction.vendors import VendorGazetteer
from core.ocr import is_ocr_available, text_from_string
from core.reader import read_receipt
from core.types import ClaimRecord, DuplicatePair, ExtractionResult
from expenses.models import (
    AuditEvent,
    Claim,
    DuplicateFlag,
    Employee,
    ExtractedField,
    Receipt,
    Vendor,
)

logger = logging.getLogger(__name__)

#: `tax_total` is the generic tax line every non-Indian receipt carries; without
#: it a reviewer looking at a US or EU claim sees no tax at all.
PERSISTED_FIELDS = ("vendor", "gstin", "invoice_no", "date", "subtotal",
                    "cgst", "sgst", "igst", "tax_total", "total")


class TransitionError(Exception):
    """Raised when a workflow transition is not allowed."""


def store_receipt_blob(receipt: Receipt, data: bytes, filename: str, content_type: str) -> None:
    """Persist upload bytes in private object storage or the local DB fallback.

    The fallback keeps local development and existing deployments working while
    a bucket is being provisioned. Production Streamlit deployments should set
    ``BRIER_OBJECT_STORAGE_BUCKET`` so new evidence leaves the database row.
    """
    if not data:
        return
    if object_storage.enabled():
        key = object_storage.key_for(receipt.receipt_id, filename)
        try:
            etag = object_storage.put_bytes(data, key=key, content_type=content_type)
        except Exception as exc:  # noqa: BLE001 - convert provider errors to a safe UI error
            raise RuntimeError(
                "Receipt storage is unavailable. Check the S3 bucket, endpoint, and credentials."
            ) from exc
        receipt.image_blob = None
        receipt.object_storage_bucket = object_storage.config_from_env().bucket
        receipt.object_storage_key = key
        receipt.object_storage_etag = etag
    else:
        receipt.image_blob = data
        receipt.object_storage_bucket = ""
        receipt.object_storage_key = ""
        receipt.object_storage_etag = ""
    receipt.image_filename = filename or "receipt.png"
    receipt.image_content_type = content_type or "application/octet-stream"
    receipt.save(update_fields=[
        "image_blob", "image_filename", "image_content_type",
        "object_storage_bucket", "object_storage_key", "object_storage_etag",
    ])


def delete_receipt_artifact(receipt: Receipt) -> None:
    """Best-effort cleanup for an upload that is being discarded."""
    if not receipt.object_storage_key:
        return
    try:
        object_storage.delete(
            receipt.object_storage_key,
            bucket=receipt.object_storage_bucket or None,
        )
    except Exception:  # noqa: BLE001 - never hide the original workflow failure
        logger.exception("Could not delete receipt object %s", receipt.object_storage_key)


# --------------------------------------------------------------------- core IO
def build_gazetteer() -> VendorGazetteer:
    """Gazetteer built from the Vendor table, including learned GSTINs."""
    entries: list[tuple[str, str]] = []
    for vendor in Vendor.objects.all().prefetch_related("aliases"):
        entries.append((vendor.canonical_id, vendor.name))
        entries.extend((vendor.canonical_id, alias.alias) for alias in vendor.aliases.all())
    gaz = VendorGazetteer(entries)
    for vendor in Vendor.objects.exclude(gstin=""):
        gaz.learn_gstin(vendor.gstin, vendor.canonical_id)
    return gaz


def load_line_model() -> LineRoleModel | None:
    return LineRoleModel.load(Path(settings.MODEL_DIR) / "line_role.joblib")


def to_claim_record(claim: Claim) -> ClaimRecord:
    """Django model -> the Django-free record the dedup core operates on."""
    receipt = claim.receipt
    return ClaimRecord(
        claim_id=claim.claim_id,
        employee_id=claim.employee.employee_code,
        receipt_id=receipt.receipt_id,
        vendor_raw=receipt.vendor_raw or (receipt.vendor.name if receipt.vendor else None),
        vendor_canonical=receipt.vendor.canonical_id if receipt.vendor else None,
        gstin=receipt.gstin or None,
        invoice_no=receipt.invoice_no or None,
        date=receipt.invoice_date,
        total=receipt.total if receipt.total is not None else claim.claimed_amount,
        currency=receipt.currency or "",
        submitted_at=claim.submitted_at,
        status=claim.status,
        decided_at=claim.decided_at,
        phash=receipt.phash_int,
        text=receipt.text or "",
    )


def foreign_currency(result: ExtractionResult) -> str | None:
    """The currency code when a receipt is clearly in a currency Brier does not accept.

    Only a real currency signal counts: an ISO code, a symbol, or a symbol plus a locale marker.
    A bare locale marker ("Sales Tax") is not enough, because Indian bills print that too.
    """
    code = (result.currency or "").upper()
    if not code or code in settings.ALLOWED_CURRENCIES:
        return None
    evidence = str(result.meta.get("currency_evidence", ""))
    if evidence in ("", "default", "locale marker"):
        return None
    # A bare letter symbol ("R", "kr") is too weak to turn an Indian bill away; "$" never means rupees.
    if evidence.startswith("ambiguous symbol") and not evidence.endswith("$"):
        return None
    return code


def extract_into_receipt(receipt: Receipt, *, gazetteer: VendorGazetteer | None = None,
                         line_model: LineRoleModel | None = None) -> ExtractionResult:
    """Run extraction and persist every field with its confidence breakdown."""
    gazetteer = gazetteer or build_gazetteer()
    source = _receipt_source(receipt, gazetteer, line_model)
    result = extract_receipt(source, gazetteer=gazetteer, line_model=line_model)

    receipt.text = source.text
    receipt.ocr_source = source.source
    # Foreign currencies never reach the database: an uncertain one falls back to INR here,
    # and a clear one is rejected by the caller through foreign_currency().
    receipt.currency = result.currency if result.currency in settings.ALLOWED_CURRENCIES else DEFAULT_CURRENCY
    receipt.currency_confidence = float(result.meta.get("currency_confidence", 0.0))
    receipt.doc_confidence = result.doc_confidence
    receipt.arithmetic_ok = result.arithmetic_ok
    receipt.needs_review = result.doc_confidence < settings.CONFIDENCE_NEEDS_REVIEW

    receipt.vendor_raw = _field_value(result, "vendor") or ""
    canonical = result.get("vendor")
    if isinstance(canonical, str) and canonical.startswith("V") and len(canonical) == 4:
        receipt.vendor = Vendor.objects.filter(canonical_id=canonical).first()
    receipt.gstin = str(result.get("gstin") or "")
    receipt.invoice_no = str(result.get("invoice_no") or "")
    receipt.invoice_date = result.get("date")
    for name in ("subtotal", "cgst", "sgst", "igst", "total"):
        setattr(receipt, name, _decimal(result.get(name)))

    if _has_receipt_image(receipt) and not receipt.phash:
        try:
            with _receipt_file(receipt) as path:
                value = phash_of(path)
            receipt.phash = str(value) if value is not None else ""
        except (ValueError, OSError):
            receipt.phash = ""
    receipt.save()

    for name in PERSISTED_FIELDS:
        fr = result.fields.get(name)
        if fr is None:
            continue
        ExtractedField.objects.update_or_create(
            receipt=receipt, name=name,
            defaults={
                "value": "" if fr.normalized is None else str(fr.normalized),
                "confidence": fr.confidence,
                "components": {k: round(v, 4) for k, v in fr.components.items()},
                "pattern_id": fr.pattern_id or "",
                "warnings": fr.warnings,
                "needs_review": fr.needs_review,
            })
    return result


def _receipt_source(receipt: Receipt, gazetteer=None, line_model=None):
    if receipt.text:
        return text_from_string(receipt.text)
    if _has_receipt_image(receipt) and is_ocr_available():
        with _receipt_file(receipt) as path:
            return read_receipt(path, gazetteer=gazetteer, line_model=line_model)
    sidecar = Path(settings.DATA_DIR) / "receipts_text" / f"{receipt.receipt_id}.txt"
    if sidecar.exists():
        return text_from_string(sidecar.read_text(encoding="utf-8"))
    if _has_receipt_image(receipt):
        raise RuntimeError(
            "Tesseract is not installed, so an uploaded image cannot be read. "
            "Install Tesseract, or paste the receipt text into the form.")
    raise RuntimeError(f"No text available for receipt {receipt.receipt_id}.")


@contextmanager
def _receipt_file(receipt: Receipt):
    """Yield a local image path for local, database, or object-backed storage."""
    if receipt.object_storage_key:
        suffix = Path(receipt.image_filename or ".png").suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(object_storage.get_bytes(
                receipt.object_storage_key,
                bucket=receipt.object_storage_bucket or None,
            ))
            temporary_path = Path(handle.name)
        try:
            yield temporary_path
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        return
    if receipt.image_blob:
        suffix = Path(receipt.image_filename or ".png").suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(bytes(receipt.image_blob))
            temporary_path = Path(handle.name)
        try:
            yield temporary_path
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
        return
    if receipt.image:
        yield Path(receipt.image.path)
        return
    raise FileNotFoundError(f"Receipt {receipt.receipt_id} has no image.")


def _has_receipt_image(receipt: Receipt) -> bool:
    return bool(receipt.image or receipt.image_blob or receipt.object_storage_key)


# ------------------------------------------------------------------- workflow
ALLOWED: dict[str, set[str]] = {
    Claim.DRAFT: {Claim.SUBMITTED},
    Claim.SUBMITTED: {Claim.UNDER_REVIEW, Claim.NEEDS_INFO, Claim.APPROVED, Claim.REJECTED},
    Claim.UNDER_REVIEW: {Claim.APPROVED, Claim.REJECTED, Claim.NEEDS_INFO},
    Claim.NEEDS_INFO: {Claim.SUBMITTED, Claim.UNDER_REVIEW, Claim.REJECTED},
    Claim.APPROVED: set(),
    Claim.REJECTED: {Claim.SUBMITTED},
}


def _transition(claim: Claim, to_status: str, actor, action: str, note: str = "") -> Claim:
    if to_status not in ALLOWED.get(claim.status, set()):
        raise TransitionError(
            f"Cannot move {claim.claim_id} from {claim.status} to {to_status}.")
    from_status = claim.status
    claim.status = to_status
    if to_status in {Claim.APPROVED, Claim.REJECTED}:
        claim.decided_at = timezone.now()
        claim.reviewer = actor if _is_user(actor) else None
        claim.decision_note = note
    claim.save()
    AuditEvent.objects.create(claim=claim, actor=actor if _is_user(actor) else None,
                              action=action, from_status=from_status,
                              to_status=to_status, note=note)
    return claim


def _is_user(actor) -> bool:
    return bool(actor and getattr(actor, "is_authenticated", False))


def submit_claim(claim: Claim, actor=None, *, run_dedup: bool = True) -> list[DuplicatePair]:
    """Submit a claim and immediately screen it for duplicates.

    Deliberately NOT wrapped in a single transaction. Duplicate screening is
    seconds of pure-Python work (blocking, fuzzy matching, TF-IDF) and holding
    a write lock across it makes SQLite throw "database is locked" on any
    concurrent request. The scoring touches no rows, so it runs outside; only
    the writes are atomic, and they are short.
    """
    with transaction.atomic():
        claim.submitted_at = claim.submitted_at or timezone.now()
        claim.save(update_fields=["submitted_at"])
        if claim.status == Claim.DRAFT:
            _transition(claim, Claim.SUBMITTED, actor, "SUBMIT")
        else:
            AuditEvent.objects.create(
                claim=claim, actor=actor if _is_user(actor) else None,
                action="SUBMIT", from_status=claim.status, to_status=claim.status)

    pairs: list[DuplicatePair] = []
    if run_dedup:
        pairs = screen_for_duplicates(claim)

    if claim.receipt.doc_confidence < settings.CONFIDENCE_NEEDS_REVIEW:
        # Too little was read reliably to act on; ask for a better copy rather
        # than pushing a guess into the approval queue.
        with transaction.atomic():
            _transition(claim, Claim.NEEDS_INFO, actor, "LOW_CONFIDENCE",
                        f"Document confidence {claim.receipt.doc_confidence:.2f} "
                        "below threshold")
    return pairs


def screen_for_duplicates(claim: Claim) -> list[DuplicatePair]:
    """Compare one claim against prior claims and persist any flags.

    Reads, then scores outside any transaction, then writes. Keeping the scoring
    out of the write path is what stops one submission from locking the database
    for everyone else.
    """
    record = to_claim_record(claim)
    others = (Claim.objects.exclude(pk=claim.pk)
              .select_related("receipt", "employee", "receipt__vendor")
              .defer("receipt__image_blob"))
    existing = [to_claim_record(c) for c in others]

    pairs = find_duplicates_for_claim(record, existing, min_band="LOW")

    with transaction.atomic():
        persist_flags(claim, pairs)
    return pairs


def persist_flags(claim: Claim, pairs: list[DuplicatePair]) -> list[DuplicateFlag]:
    """Write duplicate pairs as flags hanging off the newer claim."""
    by_claim_id = {c.claim_id: c for c in
                   Claim.objects.filter(claim_id__in=[p.b if p.a == claim.claim_id else p.a
                                                      for p in pairs])}
    flags: list[DuplicateFlag] = []
    for pair in pairs:
        other_id = pair.b if pair.a == claim.claim_id else pair.a
        other = by_claim_id.get(other_id)
        if other is None:
            continue
        flag, _created = DuplicateFlag.objects.update_or_create(
            claim=claim, matched_claim=other,
            defaults={"score": pair.score, "band": pair.band, "signals": pair.signals,
                      "rules_fired": pair.rules_fired, "tags": pair.tags})
        flags.append(flag)
    return flags


def start_review(claim: Claim, actor=None) -> Claim:
    return _transition(claim, Claim.UNDER_REVIEW, actor, "START_REVIEW")


def approve(claim: Claim, actor=None, note: str = "") -> Claim:
    """Approve a claim -- refused while a severe duplicate flag is unresolved.

    This is the guard the whole project exists for, so it lives in the service
    layer rather than in a view: no code path can approve around it.
    """
    blocking = list(claim.blocking_flags())
    if blocking:
        bands = ", ".join(sorted({f.band for f in blocking}))
        raise TransitionError(
            f"{claim.claim_id} has {len(blocking)} unresolved duplicate flag(s) "
            f"({bands}). Confirm or dismiss them before approving.")
    return _transition(claim, Claim.APPROVED, actor, "APPROVE", note)


def reject(claim: Claim, actor=None, note: str = "") -> Claim:
    return _transition(claim, Claim.REJECTED, actor, "REJECT", note)


def request_info(claim: Claim, actor=None, note: str = "") -> Claim:
    return _transition(claim, Claim.NEEDS_INFO, actor, "REQUEST_INFO", note)


def resolve_flag(flag: DuplicateFlag, actor, *, confirmed: bool, note: str = "") -> DuplicateFlag:
    """Finance's verdict on a flag. Confirming rejects the duplicate claim."""
    flag.status = DuplicateFlag.CONFIRMED if confirmed else DuplicateFlag.DISMISSED
    flag.resolved_by = actor if _is_user(actor) else None
    flag.resolved_at = timezone.now()
    flag.resolution_note = note
    flag.save()

    AuditEvent.objects.create(
        claim=flag.claim, actor=actor if _is_user(actor) else None,
        action="CONFIRM_DUPLICATE" if confirmed else "DISMISS_DUPLICATE",
        from_status=flag.claim.status, to_status=flag.claim.status,
        note=note or f"vs {flag.matched_claim.claim_id}")

    if confirmed and flag.claim.status in Claim.OPEN_STATUSES:
        reject(flag.claim, actor,
               note=note or f"Confirmed duplicate of {flag.matched_claim.claim_id}")
    return flag


def correct_extracted_field(field: ExtractedField, value: str, actor) -> ExtractedField:
    """Apply a reviewed value to both the evidence row and operational fields."""
    value = value.strip()
    if not value:
        raise TransitionError("Corrected value cannot be blank.")
    if len(value) > 200:
        raise TransitionError("Corrected value is too long.")

    receipt = field.receipt
    scalar_fields: list[str] = []
    try:
        if field.name == "vendor":
            from core.normalize import norm_vendor
            receipt.vendor_raw = value
            receipt.vendor = Vendor.objects.filter(normalized_name=norm_vendor(value)).first()
            scalar_fields.extend(["vendor_raw", "vendor"])
        elif field.name == "date":
            receipt.invoice_date = dt.date.fromisoformat(value)
            scalar_fields.append("invoice_date")
        elif field.name == "invoice_no":
            receipt.invoice_no = value
            scalar_fields.append("invoice_no")
        elif field.name == "gstin":
            receipt.gstin = value
            scalar_fields.append("gstin")
        elif field.name in {"subtotal", "cgst", "sgst", "igst", "total"}:
            setattr(receipt, field.name, Decimal(value.replace(",", "")))
            scalar_fields.append(field.name)
    except (ValueError, InvalidOperation) as exc:
        raise TransitionError(f"{field.name} has an invalid value.") from exc

    with transaction.atomic():
        field.corrected_value = value
        field.corrected_by = actor if _is_user(actor) else None
        field.needs_review = False
        field.save(update_fields=["corrected_value", "corrected_by", "needs_review"])
        receipt.needs_review = receipt.extracted.filter(needs_review=True).exists()
        receipt.save(update_fields=[*scalar_fields, "needs_review"])
        claims = list(receipt.claims.all())
        if field.name == "total":
            receipt.claims.update(claimed_amount=receipt.total)
        for claim in claims:
            AuditEvent.objects.create(
                claim=claim, actor=actor if _is_user(actor) else None,
                action="CORRECT_FIELD", from_status=claim.status, to_status=claim.status,
                note=f"{field.name} corrected")

    if field.name in {"vendor", "gstin", "invoice_no", "date", "total"}:
        rescore_all_flags()
    return field


def rescore_all_flags() -> tuple[int, int]:
    """Rebuild duplicate evidence while preserving prior human resolutions."""
    claims = list(Claim.objects.select_related(
        "receipt", "employee", "receipt__vendor").defer("receipt__image_blob"))
    if not claims:
        return 0, 0
    resolved = {(f.claim_id, f.matched_claim_id): f
                for f in DuplicateFlag.objects.exclude(status=DuplicateFlag.OPEN)}
    pairs = find_duplicates(
        [to_claim_record(c) for c in claims], min_band="LOW",
        policy_limit=getattr(settings, "POLICY_SPEND_LIMIT", None))
    by_id = {claim.claim_id: claim for claim in claims}
    with transaction.atomic():
        DuplicateFlag.objects.all().delete()
        created = 0
        for pair in pairs:
            a, b = by_id.get(pair.a), by_id.get(pair.b)
            if a is None or b is None:
                continue
            newer, older = ((a, b) if (a.submitted_at or timezone.now()) >=
                            (b.submitted_at or timezone.now()) else (b, a))
            previous = resolved.get((newer.pk, older.pk))
            DuplicateFlag.objects.create(
                claim=newer, matched_claim=older, score=pair.score, band=pair.band,
                signals=pair.signals, rules_fired=pair.rules_fired, tags=pair.tags,
                status=previous.status if previous else DuplicateFlag.OPEN,
                resolved_by=previous.resolved_by if previous else None,
                resolved_at=previous.resolved_at if previous else None,
                resolution_note=previous.resolution_note if previous else "")
            created += 1
    return created, recompute_groups()


def recompute_groups() -> int:
    """Recluster all claims into duplicate groups. Returns the group count."""
    pairs = [DuplicatePair(a=f.claim.claim_id, b=f.matched_claim.claim_id,
                           score=f.score, band=f.band, signals=f.signals or {},
                           rules_fired=f.rules_fired or [], tags=f.tags or [])
             for f in DuplicateFlag.objects.select_related("claim", "matched_claim")
             .exclude(status=DuplicateFlag.DISMISSED)]
    groups = cluster(pairs)
    if not groups:
        Claim.objects.exclude(duplicate_group=None).update(duplicate_group=None)
        return 0
    by_id = {c.claim_id: c for c in Claim.objects.filter(claim_id__in=groups.keys())}
    updated = []
    for claim_id, group in groups.items():
        claim = by_id.get(claim_id)
        if claim is not None and claim.duplicate_group != group:
            claim.duplicate_group = group
            updated.append(claim)
    if updated:
        Claim.objects.bulk_update(updated, ["duplicate_group"])
    Claim.objects.exclude(claim_id__in=groups.keys()).exclude(
        duplicate_group=None).update(duplicate_group=None)
    return len(set(groups.values()))


# --------------------------------------------------------------------- helpers
def get_or_create_employee(code: str, **extra) -> Employee:
    employee, _ = Employee.objects.get_or_create(employee_code=code, defaults=extra)
    return employee


def _field_value(result: ExtractionResult, name: str) -> str | None:
    fr = result.fields.get(name)
    return fr.value if fr is not None else None


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def as_date(value) -> dt.date | None:
    return value if isinstance(value, dt.date) else None
