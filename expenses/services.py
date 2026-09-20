"""The only seam between Django and the pure-Python core.

Every status transition goes through this module so that the audit trail and
the duplicate guard cannot be bypassed by a view that forgets to call them.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.currency import DEFAULT_CURRENCY
from core.dedup.blocking import phash_of
from core.dedup.pipeline import cluster, find_duplicates_for_claim
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

#: `tax_total` is the generic tax line every non-Indian receipt carries; without
#: it a reviewer looking at a US or EU claim sees no tax at all.
PERSISTED_FIELDS = ("vendor", "gstin", "invoice_no", "date", "subtotal",
                    "cgst", "sgst", "igst", "tax_total", "total")


class TransitionError(Exception):
    """Raised when a workflow transition is not allowed."""


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

    if receipt.image and not receipt.phash:
        try:
            value = phash_of(receipt.image.path)
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
    if receipt.image and is_ocr_available():
        return read_receipt(receipt.image.path, gazetteer=gazetteer, line_model=line_model)
    sidecar = Path(settings.DATA_DIR) / "receipts_text" / f"{receipt.receipt_id}.txt"
    if sidecar.exists():
        return text_from_string(sidecar.read_text(encoding="utf-8"))
    if receipt.image:
        raise RuntimeError(
            "Tesseract is not installed, so an uploaded image cannot be read. "
            "Install Tesseract, or paste the receipt text into the form.")
    raise RuntimeError(f"No text available for receipt {receipt.receipt_id}.")


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
              .select_related("receipt", "employee", "receipt__vendor"))
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


def recompute_groups() -> int:
    """Recluster all claims into duplicate groups. Returns the group count."""
    pairs = [DuplicatePair(a=f.claim.claim_id, b=f.matched_claim.claim_id,
                           score=f.score, band=f.band, signals=f.signals or {},
                           rules_fired=f.rules_fired or [], tags=f.tags or [])
             for f in DuplicateFlag.objects.select_related("claim", "matched_claim")
             .exclude(status=DuplicateFlag.DISMISSED)]
    groups = cluster(pairs)
    if not groups:
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
