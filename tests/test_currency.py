"""Multi-currency parsing, detection and cross-currency duplicate safety."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.currency import (CURRENCIES, detect_currency, format_amount,
                           parse_amount, date_order_for)
from core.dedup.scoring import score_pair
from core.extraction.pipeline import extract_receipt
from core.types import ClaimRecord

TODAY = dt.date(2025, 6, 1)

USD = """BLUE BOTTLE COFFEE
1355 Market St, San Francisco, CA 94103
INVOICE
Invoice No: INV-2025-88213
Date: 03/14/2025
Subtotal:                        $147.50
Sales Tax @ 8.625%:               $12.72
Total:                           $160.22
"""

EUR = """CAFE ZUR POST GmbH
Hauptstrasse 12, 10115 Berlin
RECHNUNG
Rechnung Nr: R-2025-0417
Datum: 14.03.2025
Nettobetrag:                    28,90 EUR
MwSt 19%:                        5,49 EUR
Gesamtbetrag:                   34,39 EUR
"""

GBP = """THE CORNER SHOP LTD
12 High Street, London
Invoice No: GB-2025-4471
Date: 14/03/2025
Net:                             £70.00
VAT 20%:                         £14.00
Total:                           £84.00
"""


class TestAmountParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234.56", "1234.56"),        # US / UK / India
        ("1.234,56", "1234.56"),        # Germany, Spain, Italy
        ("1 234,56", "1234.56"),        # France, Scandinavia
        ("1,23,456.78", "123456.78"),   # Indian lakh grouping
        ("34,39", "34.39"),             # bare euro decimal comma
        ("160.22", "160.22"),
        ("$147.50", "147.50"),
        ("8,40 EUR", "8.40"),
        ("(25.00)", "-25.00"),          # accounting negative
        ("1,234", "1234"),              # three trailing digits => grouping
    ])
    def test_separator_conventions(self, raw, expected):
        assert parse_amount(raw) == Decimal(expected)

    def test_rejects_non_numeric(self):
        assert parse_amount("abc") is None
        assert parse_amount("") is None


class TestCurrencyDetection:
    @pytest.mark.parametrize("text,code", [
        ("Total: $160.22\nSales Tax: $12.72", "USD"),
        ("Gesamtbetrag: 34,39 €", "EUR"),
        ("Total: £84.00", "GBP"),
        ("Grand Total: ₹1,174.00", "INR"),
        ("Total: ¥12,000", "JPY"),
        ("Total: 500.00 AUD", "AUD"),
    ])
    def test_detects(self, text, code):
        assert detect_currency(text).code == code

    def test_single_letter_symbol_needs_a_number_beside_it(self):
        """ZAR's "R" otherwise matches inside ordinary words."""
        assert detect_currency("CAFE ZUR POST GmbH\nGesamtbetrag: 34,39 €").code == "EUR"

    def test_explicit_code_beats_symbol(self):
        assert detect_currency("Total: $100.00 CAD").code == "CAD"

    def test_falls_back_to_default(self):
        detection = detect_currency("Total: 100.00")
        assert detection.code == "INR" and detection.confidence < 0.5

    def test_us_receipts_read_dates_month_first(self):
        assert date_order_for("USD") == "MDY"
        assert date_order_for("EUR") == "DMY"


class TestMultiCurrencyExtraction:
    @pytest.mark.parametrize("text,code,total,subtotal,tax", [
        (USD, "USD", "160.22", "147.50", "12.72"),
        (EUR, "EUR", "34.39", "28.90", "5.49"),
        (GBP, "GBP", "84.00", "70.00", "14.00"),
    ])
    def test_extracts_and_reconciles(self, text, code, total, subtotal, tax):
        result = extract_receipt(text, today=TODAY)
        assert result.currency == code
        assert result.get("total") == Decimal(total)
        assert result.get("subtotal") == Decimal(subtotal)
        assert result.get("tax_total") == Decimal(tax)
        assert result.arithmetic_ok is True

    def test_generic_tax_is_not_filed_as_indian_gst(self):
        """A "Sales Tax" line must not become CGST on a US receipt."""
        result = extract_receipt(USD, today=TODAY)
        assert result.get("cgst") is None
        assert result.get("sgst") is None

    def test_us_date_is_month_first(self):
        """03/14 is unambiguous, but the order must come from the currency."""
        result = extract_receipt(USD, today=TODAY)
        assert result.get("date") == dt.date(2025, 3, 14)
        assert result.meta["date_order"] == "MDY"

    def test_european_date_is_day_first(self):
        assert extract_receipt(EUR, today=TODAY).get("date") == dt.date(2025, 3, 14)


class TestCrossCurrencyDuplicates:
    def _claim(self, cid, currency, invoice):
        return ClaimRecord(
            claim_id=cid, employee_id="E1", vendor_canonical="V1", vendor_raw="Acme",
            invoice_no=invoice, total=Decimal("100.00"), currency=currency,
            date=dt.date(2025, 3, 14), text="coffee beans",
            submitted_at=dt.datetime(2025, 3, 20))

    def test_same_number_different_currency_is_not_a_duplicate(self):
        """100 USD and 100 INR are different claims, not the same one twice."""
        pair = score_pair(self._claim("A", "USD", "INV-1"),
                          self._claim("B", "INR", "INV-2"))
        assert pair.band not in {"EXACT", "HIGH"}
        assert pair.signals.get("amount") == 0.0

    def test_same_currency_still_matches(self):
        pair = score_pair(self._claim("A", "USD", "INV-1"),
                          self._claim("B", "USD", "INV-1"))
        assert pair.band == "EXACT"

    def test_same_invoice_still_matches_across_currencies(self):
        """One bill read with the currency misread is still that one bill."""
        pair = score_pair(self._claim("A", "USD", "INV-1"),
                          self._claim("B", "EUR", "INV-1"))
        assert pair.band == "EXACT"


class TestFormatting:
    @pytest.mark.parametrize("value,code,expected", [
        (Decimal("1234.5"), "USD", "$1,234.50"),
        (Decimal("34.39"), "EUR", "\u20ac34.39"),
        (Decimal("1174"), "INR", "\u20b91,174.00"),
        (Decimal("12000"), "JPY", "\u00a512,000"),
    ])
    def test_format(self, value, code, expected):
        assert format_amount(value, code) == expected

    def test_every_currency_has_a_symbol(self):
        for code, currency in CURRENCIES.items():
            assert currency.symbols, code


class TestSymbolStaysOnItsLine:
    def test_r_of_the_next_line_is_not_a_rand_symbol(self):
        """A line ending in a digit followed by "Round Off" made the receipt South African."""
        text = "Grand Total:  1,174.00\nRound Off:  -0.10\nTotal: 1,174.00"
        assert detect_currency("SGST @ 9%:  89.55\nRound Off:  -0.10").code == "INR"
        assert detect_currency(text).code == "INR"

    def test_symbol_on_the_same_line_still_counts(self):
        assert detect_currency("Total: 84.00 \u00a3").code == "GBP"
        assert detect_currency("Total: \u20ac 34,39").code == "EUR"
