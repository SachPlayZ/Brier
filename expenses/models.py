"""Data model for receipts, claims, duplicate flags and the audit trail.

No PostgreSQL-only field types are used, so the same migrations run on SQLite
(dev) and Postgres (prod). Structured payloads use ``JSONField``, which Django
maps to a text column on SQLite and to native JSON on Postgres.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

CONFIDENCE_HIGH = 0.8216   # mirrors settings.CONFIDENCE_AUTO_ACCEPT (empirical)
CONFIDENCE_LOW = 0.60


class Vendor(models.Model):
    """Canonical vendor. Doubles as the extraction gazetteer source."""

    canonical_id = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=200)
    normalized_name = models.CharField(max_length=200, db_index=True, blank=True)
    gstin = models.CharField(max_length=15, blank=True, db_index=True)
    category = models.CharField(max_length=40, blank=True)
    city = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        from core.normalize import norm_vendor

        self.normalized_name = norm_vendor(self.name)
        super().save(*args, **kwargs)


class VendorAlias(models.Model):
    """A printed spelling seen for a vendor -- feeds fuzzy matching."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=200)

    class Meta:
        unique_together = [("vendor", "alias")]


class Employee(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                null=True, blank=True, related_name="employee")
    employee_code = models.CharField(max_length=16, unique=True)
    full_name = models.CharField(max_length=120, blank=True)
    department = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["employee_code"]

    def __str__(self) -> str:
        return f"{self.employee_code} {self.full_name}".strip()


class Receipt(models.Model):
    """One physical receipt, plus everything extraction read from it."""

    OCR_SOURCES = [("sidecar", "Native text"), ("tesseract", "Tesseract OCR"),
                   ("manual", "Manually entered")]

    receipt_id = models.CharField(max_length=32, unique=True)
    image = models.ImageField(upload_to="receipts/", blank=True, null=True)
    image_path = models.CharField(max_length=300, blank=True)
    text = models.TextField(blank=True)
    ocr_source = models.CharField(max_length=20, choices=OCR_SOURCES, default="sidecar")
    phash = models.CharField(max_length=20, blank=True, db_index=True)

    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="receipts")
    vendor_raw = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=20, blank=True)
    invoice_no = models.CharField(max_length=40, blank=True, db_index=True)
    invoice_date = models.DateField(null=True, blank=True, db_index=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cgst = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sgst = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    igst = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="INR", db_index=True)
    currency_confidence = models.FloatField(default=0.0)

    doc_confidence = models.FloatField(default=0.0)
    arithmetic_ok = models.BooleanField(null=True, blank=True)
    needs_review = models.BooleanField(default=False)
    noise_tier = models.IntegerField(default=0)
    template_id = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["invoice_no", "total"])]

    def __str__(self) -> str:
        return self.receipt_id

    @property
    def phash_int(self) -> int | None:
        try:
            return int(self.phash) if self.phash else None
        except ValueError:
            return None

    @property
    def confidence_pct(self) -> int:
        return int(round(self.doc_confidence * 100))

    @property
    def symbol(self) -> str:
        from core.currency import symbol_for

        return symbol_for(self.currency)

    def money(self, value) -> str:
        """Format an amount in this receipt's currency."""
        from core.currency import format_amount

        return format_amount(value, self.currency)

    @property
    def confidence_band(self) -> str:
        if self.doc_confidence >= CONFIDENCE_HIGH:
            return "high"
        return "medium" if self.doc_confidence >= CONFIDENCE_LOW else "low"


class ExtractedField(models.Model):
    """One extracted field with its confidence breakdown.

    Stored per field rather than as a blob on the receipt so the review UI can
    show, sort and filter on individual field confidence -- and so a reviewer's
    manual correction is recorded against the specific field it fixes.
    """

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="extracted")
    name = models.CharField(max_length=32)
    value = models.CharField(max_length=200, blank=True)
    confidence = models.FloatField(default=0.0)
    components = models.JSONField(default=dict, blank=True)
    pattern_id = models.CharField(max_length=40, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    needs_review = models.BooleanField(default=False)
    corrected_value = models.CharField(max_length=200, blank=True)
    corrected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True)

    class Meta:
        unique_together = [("receipt", "name")]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.receipt_id}.{self.name}"

    @property
    def display_value(self) -> str:
        return self.corrected_value or self.value

    @property
    def found(self) -> bool:
        return bool(self.display_value)

    @property
    def band(self) -> str:
        """Traffic-light band used by the template.

        'absent' is distinct from 'low': a receipt with no IGST line is not a
        low-confidence reading, and colouring it red would send reviewers
        chasing fields that were never there.
        """
        if self.corrected_value:
            return "corrected"
        if not self.found:
            return "absent"
        if self.confidence >= CONFIDENCE_HIGH:
            return "high"
        return "medium" if self.confidence >= CONFIDENCE_LOW else "low"

    @property
    def confidence_pct(self) -> int:
        return int(round(self.confidence * 100))


class Claim(models.Model):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    NEEDS_INFO = "NEEDS_INFO"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    STATUSES = [(s, s.replace("_", " ").title()) for s in
                (DRAFT, SUBMITTED, UNDER_REVIEW, NEEDS_INFO, APPROVED, REJECTED)]

    OPEN_STATUSES = (SUBMITTED, UNDER_REVIEW, NEEDS_INFO)

    claim_id = models.CharField(max_length=32, unique=True)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="claims")
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="claims")
    claimed_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=300, blank=True)
    category = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=SUBMITTED,
                              db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="reviewed_claims")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    duplicate_group = models.IntegerField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return self.claim_id

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def currency(self) -> str:
        return self.receipt.currency or "INR"

    @property
    def amount_display(self) -> str:
        from core.currency import format_amount

        return format_amount(self.claimed_amount, self.currency)

    def blocking_flags(self):
        """Open duplicate flags severe enough to stop approval."""
        from django.conf import settings as dj_settings

        bands = getattr(dj_settings, "BLOCKING_DUP_BANDS", ("EXACT", "HIGH"))
        return self.flags_as_a.filter(status=DuplicateFlag.OPEN, band__in=bands)

    @property
    def all_flags(self):
        from django.db.models import Q

        return DuplicateFlag.objects.filter(Q(claim=self) | Q(matched_claim=self))


class DuplicateFlag(models.Model):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    STATUSES = [(OPEN, "Open"), (CONFIRMED, "Confirmed duplicate"),
                (DISMISSED, "Dismissed")]

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="flags_as_a")
    matched_claim = models.ForeignKey(Claim, on_delete=models.CASCADE,
                                      related_name="flags_as_b")
    score = models.FloatField(default=0.0)
    band = models.CharField(max_length=10, db_index=True)
    signals = models.JSONField(default=dict, blank=True)
    rules_fired = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default=OPEN, db_index=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("claim", "matched_claim")]
        ordering = ["-score"]
        indexes = [models.Index(fields=["band", "status"])]

    def __str__(self) -> str:
        return f"{self.claim_id}~{self.matched_claim_id} {self.band}"

    @property
    def reasons(self) -> list[str]:
        """Human-readable explanation, reusing the core formatting."""
        from core.types import DuplicatePair

        pair = DuplicatePair(a=str(self.claim_id), b=str(self.matched_claim_id),
                             score=self.score, band=self.band,
                             signals=self.signals or {},
                             rules_fired=self.rules_fired or [],
                             tags=self.tags or [])
        return pair.reasons()

    @property
    def score_pct(self) -> int:
        return int(round(self.score * 100))


class AuditEvent(models.Model):
    """Append-only trail. Every status change writes exactly one row."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, blank=True)
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    note = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at"]

    def __str__(self) -> str:
        return f"{self.claim_id} {self.action}"
