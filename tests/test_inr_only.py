"""Brier accepts INR receipts only: foreign ones are rejected at upload, never stored."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from expenses import services
from expenses.models import Claim, Employee, Receipt
from tests.test_currency import EUR, GBP, USD
from tests.test_workflow import RECEIPT_TEXT

pytestmark = pytest.mark.django_db

INR_SYMBOL = RECEIPT_TEXT.replace("Grand Total:             1,174.00", "Grand Total:            ₹1,174.00")
LOCALE_ONLY = """CORNER STORE
Invoice No: INV-77
Date: 14/03/2025
Subtotal:    100.00
Sales Tax:    18.00
Total:       118.00
"""
NO_SYMBOL = RECEIPT_TEXT


@pytest.fixture
def logged_in(client):
    user = User.objects.create_user("asha", password="pw")
    Employee.objects.create(employee_code="E001", full_name="Asha R", user=user)
    client.login(username="asha", password="pw")
    return client


def submit(client, text):
    return client.post(reverse("claim_submit"), {"receipt_text": text, "description": "t"})


@pytest.mark.parametrize("text,code", [(USD, "USD"), (EUR, "EUR"), (GBP, "GBP")])
def test_foreign_receipt_is_rejected_and_nothing_is_stored(logged_in, text, code):
    response = submit(logged_in, text)
    assert response.status_code == 200                       # the form again, not a redirect
    assert f"INR receipts only. This one looks like {code}." in response.content.decode()
    assert Receipt.objects.count() == 0
    assert Claim.objects.count() == 0


def test_bare_dollar_sign_is_rejected_too(logged_in):
    """`$` alone is ambiguous between dollars, but it is never rupees."""
    text = "SHOP\nInvoice No: A-1\nDate: 14/03/2025\nTotal:  $50.00\n"
    assert submit(logged_in, text).status_code == 200
    assert Receipt.objects.count() == 0


@pytest.mark.parametrize("text", [NO_SYMBOL, INR_SYMBOL, LOCALE_ONLY])
def test_inr_or_unmarked_receipt_is_accepted_as_inr(logged_in, text):
    response = submit(logged_in, text)
    assert response.status_code == 302
    receipt = Receipt.objects.get()
    assert receipt.currency == "INR"
    assert Claim.objects.count() == 1


def test_extract_into_receipt_never_stores_a_foreign_currency():
    """Ingest and seeding go through here without a view to reject: they fall back to INR."""
    receipt = Receipt.objects.create(receipt_id="R1", text=USD)
    services.extract_into_receipt(receipt)
    receipt.refresh_from_db()
    assert receipt.currency == "INR"


def test_foreign_currency_ignores_a_bare_locale_marker():
    receipt = Receipt.objects.create(receipt_id="R2", text=LOCALE_ONLY)
    result = services.extract_into_receipt(receipt)
    assert services.foreign_currency(result) is None


def test_foreign_currency_flags_a_clear_signal():
    receipt = Receipt.objects.create(receipt_id="R3", text=EUR)
    assert services.foreign_currency(services.extract_into_receipt(receipt)) == "EUR"
