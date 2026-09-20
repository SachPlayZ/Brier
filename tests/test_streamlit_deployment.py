"""Deployment adapter tests; UI rendering is covered by the headless smoke check."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from expenses import services, uploads
from expenses.models import AuditEvent, Claim, Employee, ExtractedField, Receipt
from streamlit_ui.runtime import (
    _validate_environment,
    copy_secrets,
    provision_finance_user,
)


def png_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_prepare_upload_blob_is_browser_ready():
    upload = SimpleUploadedFile("invoice.png", png_bytes())
    data, name, mime, text = uploads.prepare_upload_blob(upload)
    assert data.startswith(b"\x89PNG")
    assert name == "invoice.png"
    assert mime == "image/png"
    assert text == ""


def test_upload_rejects_excessive_image_dimensions(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_IMAGE_PIXELS", 100)
    upload = SimpleUploadedFile("invoice.png", png_bytes())
    with pytest.raises(ValidationError, match="dimensions are too large"):
        uploads.validate_receipt_upload(upload)


def test_database_image_gets_a_temporary_local_path():
    receipt = Receipt(
        receipt_id="R-BLOB",
        image_blob=png_bytes(),
        image_filename="invoice.png",
        image_content_type="image/png",
    )
    with services._receipt_file(receipt) as path:
        assert path.exists()
        assert path.read_bytes().startswith(b"\x89PNG")
        saved_path = Path(path)
    assert not saved_path.exists()


def test_copy_secrets_does_not_override_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "existing")
    copy_secrets({"DATABASE_URL": "replacement", "DJANGO_SECRET_KEY": "secret"})
    assert __import__("os").environ["DATABASE_URL"] == "existing"
    assert __import__("os").environ["DJANGO_SECRET_KEY"] == "secret"


def test_production_fails_closed_without_postgres(monkeypatch):
    monkeypatch.delenv("BRIER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("DJANGO_DEBUG", "0")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "x" * 50)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        _validate_environment()


def test_streamlit_fails_closed_when_all_deployment_vars_are_missing(monkeypatch):
    for name in (
        "BRIER_LOCAL_DEMO",
        "DJANGO_DEBUG",
        "DJANGO_SECRET_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="DJANGO_DEBUG=0"):
        _validate_environment()


@pytest.mark.django_db
def test_finance_bootstrap_is_idempotent_and_does_not_reset_password(monkeypatch):
    monkeypatch.setenv("BRIER_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("BRIER_ADMIN_PASSWORD", "initial-secret-123")
    assert provision_finance_user() is True
    user = User.objects.get(username="reviewer")
    assert user.check_password("initial-secret-123")
    assert user.groups.filter(name="finance").exists()

    user.set_password("changed-by-owner-456")
    user.save(update_fields=["password"])
    assert provision_finance_user() is False
    user.refresh_from_db()
    assert user.check_password("changed-by-owner-456")
    assert Group.objects.filter(name="finance").exists()


@pytest.mark.django_db
def test_finance_bootstrap_rejects_short_password(monkeypatch):
    monkeypatch.setenv("BRIER_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("BRIER_ADMIN_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="at least 12"):
        provision_finance_user()


@pytest.mark.django_db
def test_finance_bootstrap_refuses_existing_unmarked_user(monkeypatch):
    User.objects.create_user("reviewer", password="known-old-password")
    monkeypatch.setenv("BRIER_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("BRIER_ADMIN_PASSWORD", "new-secure-password")
    with pytest.raises(RuntimeError, match="Refusing to promote"):
        provision_finance_user()


@pytest.mark.django_db
def test_employee_allocation_never_reuses_another_users_row():
    from streamlit_ui.app import _employee_for

    first = User.objects.create_user("same-prefix-user-alpha")
    second = User.objects.create_user("same-prefix-user-beta")
    employee_a = _employee_for(first)
    employee_b = _employee_for(second)
    assert employee_a.pk != employee_b.pk
    assert employee_a.user_id == first.pk
    assert employee_b.user_id == second.pk


@pytest.mark.django_db
def test_field_correction_updates_operational_total_and_audit():
    reviewer = User.objects.create_user("finance-reviewer")
    employee = Employee.objects.create(employee_code="E001")
    receipt = Receipt.objects.create(
        receipt_id="R001", total="100.00", needs_review=True
    )
    field = ExtractedField.objects.create(
        receipt=receipt, name="total", value="100.00", needs_review=True
    )
    claim = Claim.objects.create(
        claim_id="C001",
        employee=employee,
        receipt=receipt,
        claimed_amount="100.00",
        status=Claim.SUBMITTED,
    )

    services.correct_extracted_field(field, "125.50", reviewer)

    receipt.refresh_from_db()
    claim.refresh_from_db()
    field.refresh_from_db()
    assert receipt.total == Decimal("125.50")
    assert claim.claimed_amount == Decimal("125.50")
    assert receipt.needs_review is False
    assert field.corrected_value == "125.50"
    assert AuditEvent.objects.filter(claim=claim, action="CORRECT_FIELD").exists()
