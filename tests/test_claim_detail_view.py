"""What a reviewer sees: readable labels in reading order, and the bill's arithmetic with real numbers.

"total" and "tax_total" used to appear raw and alphabetically, and a reviewer read the tax total as the
bill. The page now says "Total amount" (payable) and "Total tax" and shows the sum.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from django.urls import reverse

from expenses.models import ExtractedField
from expenses.views import amounts_check
from tests.test_reasoning import NILGIRI_RAW
from tests.test_workflow import employee, finance_user, make_claim  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db


def page(client, finance_user, claim):
    client.force_login(finance_user)
    return client.get(reverse("claim_detail", args=[claim.claim_id])).content.decode()


def test_labels_are_human_and_in_reading_order(client, employee, finance_user):
    html = page(client, finance_user, make_claim("C1", employee, text=NILGIRI_RAW))
    labels = ["Vendor", "Invoice date", "Invoice number", "GSTIN", "Taxable value", "CGST", "SGST",
              "Total tax", "Total amount"]
    positions = [html.index(f">{label}<") if f">{label}<" in html else html.index(label) for label in labels]
    assert positions == sorted(positions)
    shown = [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r'<td class="dim">(.*?)</td>', html, re.S)]
    assert not {"total", "tax_total", "subtotal"} & set(shown)        # never a raw field name as the label
    assert "Payable, tax included" in html and "not the amount to pay" in html


def test_amounts_are_shown_with_the_rupee_sign_and_the_vendor_by_name(client, employee, finance_user):
    html = page(client, finance_user, make_claim("C1", employee, text=NILGIRI_RAW))
    assert "₹1,855.20" in html and "₹283.00" in html and "₹1,572.20" in html
    assert "NILGIRI ENERGY" in html


def test_the_equation_shows_the_real_numbers(client, employee, finance_user):
    html = page(client, finance_user, make_claim("C1", employee, text=NILGIRI_RAW))
    assert "The amounts add up" in html
    equation = html[html.index('class="eq"'):]
    for part in ("Taxable value", "₹1,572.20", "CGST", "₹141.50", "SGST", "Total amount", "₹1,855.20"):
        assert part in equation


def test_reasoning_is_explained_in_words(client, employee, finance_user):
    text = ("SHOP\nInvoice No: A-100\nAmount (Rs) : 1855.20\nTaxable Val : 1572.20\nCGST @9% : 141.50\n"
            "SGST @9% : 141.50\nTotal Levy : 283.00")
    html = page(client, finance_user, make_claim("C2", employee, text=text))
    assert "Total amount taken from the line that reconciles" in html


def test_a_mismatch_says_by_how_much(client, employee, finance_user):
    claim = make_claim("C3", employee, text=NILGIRI_RAW)
    ExtractedField.objects.filter(receipt=claim.receipt, name="total").update(corrected_value="1900.00")
    html = page(client, finance_user, claim)
    assert "The parts add up to" in html and "₹44.80" in html and "Check the figures above" in html


class TestAmountsCheck:
    def fields(self, claim):
        return list(claim.receipt.extracted.all())

    def test_none_when_there_is_nothing_to_add(self, employee):
        claim = make_claim("C4", employee, text="SHOP\nInvoice No: A-1\nTotal: 500.00")
        assert amounts_check(claim.receipt, self.fields(claim)) is None

    def test_rounding_within_a_rupee_is_not_an_error(self, employee):
        text = "SHOP\nInvoice No: A-1\nSubtotal: 995.00\nCGST @9%: 89.55\nSGST @9%: 89.55\nTotal: 1,174.00"
        claim = make_claim("C5", employee, text=text)
        check = amounts_check(claim.receipt, self.fields(claim))
        assert check["ok"] or check["rounded"]
        assert Decimal("1174") == claim.receipt.total
