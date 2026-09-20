from django.contrib import admin

from expenses.models import (AuditEvent, Claim, DuplicateFlag, Employee,
                             ExtractedField, Receipt, Vendor, VendorAlias)

admin.site.site_header = "Brier administration"
admin.site.site_title = "Brier admin"
admin.site.index_title = "Brier"


class VendorAliasInline(admin.TabularInline):
    model = VendorAlias
    extra = 0


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("canonical_id", "name", "gstin", "category", "city")
    search_fields = ("canonical_id", "name", "gstin")
    list_filter = ("category", "city")
    inlines = [VendorAliasInline]


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "full_name", "department")
    search_fields = ("employee_code", "full_name")


class ExtractedFieldInline(admin.TabularInline):
    model = ExtractedField
    extra = 0
    readonly_fields = ("name", "value", "confidence", "pattern_id", "components")


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("receipt_id", "vendor", "invoice_no", "invoice_date", "total",
                    "doc_confidence", "arithmetic_ok", "needs_review")
    list_filter = ("needs_review", "arithmetic_ok", "ocr_source", "noise_tier", "template_id")
    search_fields = ("receipt_id", "invoice_no", "gstin", "vendor_raw")
    inlines = [ExtractedFieldInline]


class DuplicateFlagInline(admin.TabularInline):
    model = DuplicateFlag
    fk_name = "claim"
    extra = 0
    readonly_fields = ("matched_claim", "score", "band", "signals", "rules_fired")


class AuditEventInline(admin.TabularInline):
    model = AuditEvent
    extra = 0
    readonly_fields = ("actor", "action", "from_status", "to_status", "note", "at")
    can_delete = False


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_id", "employee", "receipt", "claimed_amount", "status",
                    "submitted_at", "duplicate_group")
    list_filter = ("status", "category", "duplicate_group")
    search_fields = ("claim_id", "employee__employee_code", "receipt__receipt_id")
    inlines = [DuplicateFlagInline, AuditEventInline]


@admin.register(DuplicateFlag)
class DuplicateFlagAdmin(admin.ModelAdmin):
    list_display = ("claim", "matched_claim", "band", "score", "status", "created_at")
    list_filter = ("band", "status")
    search_fields = ("claim__claim_id", "matched_claim__claim_id")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("claim", "action", "from_status", "to_status", "actor", "at")
    list_filter = ("action",)
    search_fields = ("claim__claim_id",)
