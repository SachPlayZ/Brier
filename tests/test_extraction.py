"""Unit tests for normalization, field extraction and confidence scoring."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.degrade import degrade_text
from core.extraction import confidence as C
from core.extraction import fields as F
from core.extraction.pipeline import extract_receipt
from core.extraction.vendors import VendorGazetteer
from core.normalize import (norm_invoice_no, norm_vendor, normalize_text,
                            parse_date_multi, to_decimal)

TODAY = dt.date(2025, 6, 1)

RECEIPT = """SHARMA TRADERS PVT LTD
14 MG Road, Bengaluru 560001
GSTIN: 29AAGCB7383J1Z4
TAX INVOICE
Invoice No: INV-2025-00871
Date: 14/03/2025
----------------------------------
Item              Qty      Amount
A4 Paper Ream       3      450.00
Stapler             1      185.00
Marker Pen         12      360.00
----------------------------------
Total Qty: 16
Sub Total:                 995.00
CGST @ 9%:                  89.55
SGST @ 9%:                  89.55
Round Off:                  -0.10
Grand Total:             1,174.00
THANK YOU VISIT AGAIN
"""


# ------------------------------------------------------------------ normalize
class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("1,23,456.78", Decimal("123456.78")),      # Indian lakh grouping
        ("1,234.56", Decimal("1234.56")),
        ("₹ 990.00", Decimal("990.00")),
        ("Rs.1 200.50", Decimal("1200.50")),
        ("45", Decimal("45")),
        ("-0.10", Decimal("-0.10")),
        ("", None),
        ("abc", None),
    ])
    def test_to_decimal(self, raw, expected):
        assert to_decimal(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("14/03/2025", dt.date(2025, 3, 14)),
        ("14-03-2025", dt.date(2025, 3, 14)),
        ("2025-03-14", dt.date(2025, 3, 14)),
        ("14-Mar-2025", dt.date(2025, 3, 14)),
        ("Mar 14, 2025", dt.date(2025, 3, 14)),
        ("14.03.25", dt.date(2025, 3, 14)),
    ])
    def test_parse_date_formats(self, raw, expected):
        parsed = parse_date_multi(raw, today=TODAY)
        assert parsed is not None and parsed[0] == expected

    def test_ambiguous_date_is_flagged(self):
        _date, _fmt, ambiguous = parse_date_multi("03/04/2025", today=TODAY)
        assert ambiguous is True

    def test_unambiguous_date_not_flagged(self):
        _date, _fmt, ambiguous = parse_date_multi("14/03/2025", today=TODAY)
        assert ambiguous is False

    def test_newlines_survive_normalization(self):
        assert normalize_text("A   B\n\nC  D").count("\n") >= 1

    def test_vendor_normalization_strips_legal_suffix(self):
        assert norm_vendor("Sharma Traders Pvt Ltd") == norm_vendor("SHARMA TRADERS")

    def test_invoice_normalization(self):
        assert norm_invoice_no("inv-2025/00871") == "INV202500871"


# --------------------------------------------------------------------- gstin
class TestGstin:
    def test_valid_gstin_passes(self):
        assert F.gstin_checksum_ok("27AAPFU0939F1ZV")

    def test_wrong_check_digit_fails(self):
        assert not F.gstin_checksum_ok("27AAPFU0939F1ZA")

    def test_bad_state_code_fails(self):
        assert not F.gstin_checksum_ok("99AAPFU0939F1ZV")

    def test_check_digit_round_trip(self):
        prefix = "29AAGCB7383J1Z"
        assert F.gstin_checksum_ok(prefix + F.gstin_check_digit(prefix))


# ---------------------------------------------------------------- extraction
class TestExtraction:
    @pytest.fixture
    def result(self):
        return extract_receipt(RECEIPT, today=TODAY)

    def test_total_beats_the_total_qty_decoy(self, result):
        """'Total Qty: 16' must not win over 'Grand Total: 1,174.00'."""
        assert result.get("total") == Decimal("1174.00")

    def test_core_fields(self, result):
        assert result.get("subtotal") == Decimal("995.00")
        assert result.get("cgst") == Decimal("89.55")
        assert result.get("sgst") == Decimal("89.55")
        assert result.get("date") == dt.date(2025, 3, 14)
        assert result.get("invoice_no") == "INV-2025-00871"
        assert result.get("gstin") == "29AAGCB7383J1Z4"

    def test_arithmetic_reconciles(self, result):
        assert result.arithmetic_ok is True

    def test_invoice_no_is_not_the_gstin(self, result):
        assert result.get("invoice_no") != result.get("gstin")

    def test_vendor_snaps_to_gazetteer(self):
        gaz = VendorGazetteer([("V001", "Sharma Traders Pvt Ltd")])
        result = extract_receipt(RECEIPT, gazetteer=gaz, today=TODAY)
        assert result.get("vendor") == "V001"

    def test_gstin_lookup_short_circuits_vendor(self):
        gaz = VendorGazetteer([("V001", "Sharma Traders Pvt Ltd")])
        gaz.learn_gstin("29AAGCB7383J1Z4", "V001")
        result = extract_receipt(RECEIPT, gazetteer=gaz, today=TODAY)
        assert result.fields["vendor"].pattern_id == "vendor.gstin_lookup"
        assert result.conf("vendor") > 0.8

    def test_missing_subtotal_is_repaired(self):
        text = RECEIPT.replace("Sub Total:                 995.00", "")
        result = extract_receipt(text, today=TODAY)
        assert result.get("subtotal") == Decimal("995.00")
        assert any("repaired" in w for w in result.fields["subtotal"].warnings)

    def test_repaired_value_is_penalised(self):
        text = RECEIPT.replace("Sub Total:                 995.00", "")
        repaired = extract_receipt(text, today=TODAY).conf("subtotal")
        direct = extract_receipt(RECEIPT, today=TODAY).conf("subtotal")
        assert repaired < direct

    def test_invalid_gstin_is_capped_and_flagged(self):
        text = RECEIPT.replace("29AAGCB7383J1Z4", "29AAGCB7383J1ZQ")
        result = extract_receipt(text, today=TODAY)
        field = result.fields["gstin"]
        assert field.confidence <= C.VALIDATOR_FAIL_CAP
        assert field.needs_review is True

    def test_empty_text_yields_no_fields(self):
        result = extract_receipt("", today=TODAY)
        assert result.get("total") is None
        assert result.doc_confidence == 0.0


# --------------------------------------------------------------- confidence
class TestConfidence:
    def test_weights_sum_to_one(self):
        for name, weights in C.FIELD_WEIGHTS.items():
            assert round(sum(weights.values()), 6) == 1.0, name

    def test_positional_prior_peaks_at_expected_place(self):
        # Totals live at the bottom of a receipt, vendors at the top.
        assert C.positional_prior(0.95, "total") > C.positional_prior(0.10, "total")
        assert C.positional_prior(0.03, "vendor") > C.positional_prior(0.90, "vendor")

    def test_labelled_pattern_scores_above_fallback(self):
        assert C.pattern_specificity("total.labelled") > C.pattern_specificity("total.max")

    def test_currency_symbol_promotes_tier(self):
        assert (C.pattern_specificity("total.labelled", has_currency=True)
                > C.pattern_specificity("total.labelled", has_currency=False))

    def test_components_are_recorded_for_every_found_field(self):
        result = extract_receipt(RECEIPT, today=TODAY)
        for name in ("total", "date", "invoice_no"):
            assert result.fields[name].components, name

    def test_confidence_degrades_with_noise(self):
        """The whole point of the score: dirtier input must score lower."""
        clean = extract_receipt(RECEIPT, today=TODAY).doc_confidence
        noisy = extract_receipt(degrade_text(RECEIPT, 3, seed=7),
                                today=TODAY, noisy=True).doc_confidence
        assert noisy < clean

    def test_doc_confidence_is_the_weakest_core_field(self):
        result = extract_receipt(RECEIPT, today=TODAY)
        weakest = min(result.conf(n) for n in ("vendor", "date", "total"))
        assert result.doc_confidence <= weakest + 1e-6


# ------------------------------------------------------- real-OCR corruptions
class TestOcrCorruptions:
    """Failure modes found by running actual Tesseract over rendered receipts.

    Each of these was a live bug: the currency glyph inflating a claim sixfold,
    a nonsense repair validating it, an apostrophe hiding the date, and an
    underscore hiding the tax.
    """

    def test_currency_glyph_read_as_digit_is_corrected(self):
        """'Rs 18,554.00' OCRs as '118,554.00' -- a 6x inflated claim."""
        text = RECEIPT.replace("Grand Total:             1,174.00",
                               "Grand Total:            11,174.00")
        result = extract_receipt(text, today=TODAY)
        assert result.get("total") == Decimal("1174.00")
        assert any("currency_glyph" in w for w in result.warnings)

    def test_implausible_repair_is_refused(self):
        """A tax solved for algebraically must not exceed a believable rate."""
        text = RECEIPT.replace("CGST @ 9%:                  89.55", "")
        text = text.replace("SGST @ 9%:                  89.55", "")
        text = text.replace("Grand Total:             1,174.00",
                            "Grand Total:            95,174.00")
        result = extract_receipt(text, today=TODAY)
        # 94k of tax on a 995 subtotal is not a missing field, it is a bad read.
        assert result.arithmetic_ok is not True
        assert result.get("cgst") is None

    def test_apostrophe_in_month_day_date(self):
        text = RECEIPT.replace("Date: 14/03/2025", "Bill Date: Mar'14, 2025")
        assert extract_receipt(text, today=TODAY).get("date") == dt.date(2025, 3, 14)

    def test_underscore_after_tax_label(self):
        """'SGST_@ 9%' -- underscore is a word char, so \b fails after SGST."""
        text = RECEIPT.replace("SGST @ 9%:                  89.55",
                               "SGST_@ 9%:                  89.55")
        assert extract_receipt(text, today=TODAY).get("sgst") == Decimal("89.55")

    def test_unlabelled_tax_rate_not_read_as_amount(self):
        """'SGST: 89.55' must not parse 89.5 as the rate and 5 as the amount."""
        text = RECEIPT.replace("SGST @ 9%:                  89.55",
                               "SGST:                       89.55")
        assert extract_receipt(text, today=TODAY).get("sgst") == Decimal("89.55")


class TestGstinRepair:
    @pytest.mark.parametrize("garbled,expected", [
        ("27AAACRSOSSK1Z7", "27AAACR5055K1Z7"),   # S/5 and O/0
        ("Z9AAGCB73B3J1Z4", "29AAGCB7383J1Z4"),   # Z/2 and B/8
        ("29AAGCB7383J1Z4", "29AAGCB7383J1Z4"),   # already valid
    ])
    def test_recovers_glyph_confusions(self, garbled, expected):
        assert F.repair_gstin(garbled) == expected

    def test_gives_up_rather_than_guessing(self):
        """Too corrupt to recover must return None, never a plausible lie."""
        assert F.repair_gstin("27AAFCHAASSCIZM") is None

    def test_never_returns_a_checksum_invalid_value(self):
        for garbled in ("27AAACRSOSSK1Z7", "Z9AAGCB73B3J1Z4", "27AAFCHAASSCIZM"):
            repaired = F.repair_gstin(garbled)
            assert repaired is None or F.gstin_checksum_ok(repaired)

    def test_literal_z_position_is_never_altered(self):
        """Position 13 is a literal 'Z'; letting it flip invents valid GSTINs."""
        repaired = F.repair_gstin("27AAFCH4455C1ZM")
        assert repaired is None or repaired[13] == "Z"


class TestLabelBoundaries:
    """Labels that OCR damage makes ambiguous.

    These failed only under real OCR: wide column spacing on the clean renders
    kept the value beyond the label-to-value gap, so the bug stayed hidden
    until Tesseract collapsed the whitespace.
    """

    def test_total_before_tax_is_not_the_total(self):
        """The 18-char gap lets `total` skip over "Before Tax" and steal it."""
        text = RECEIPT.replace("Sub Total:                 995.00",
                               "Total Before Tax: 995.00")
        result = extract_receipt(text, today=TODAY)
        assert result.get("total") == Decimal("1174.00")
        assert result.get("subtotal") == Decimal("995.00")

    def test_punctuation_inside_a_label(self):
        """OCR turns "Amount Payable" into "Amount: Payable"."""
        text = RECEIPT.replace("Grand Total:             1,174.00",
                               "Amount: Payable: 1,174.00")
        assert extract_receipt(text, today=TODAY).get("total") == Decimal("1174.00")

    def test_total_qty_decoy_still_rejected(self):
        assert extract_receipt(RECEIPT, today=TODAY).get("total") == Decimal("1174.00")

    def test_gstin_label_with_a_period(self):
        """'GST No.: ...' -- a bare colon/dash separator misses every one."""
        text = RECEIPT.replace("GSTIN: 29AAGCB7383J1Z4", "GST No.: 29AAGCB7383J1Z4")
        assert extract_receipt(text, today=TODAY).get("gstin") == "29AAGCB7383J1Z4"

    def test_gstin_with_a_split_glyph_is_repaired(self):
        """16 characters means OCR split one glyph into two."""
        text = RECEIPT.replace("29AAGCB7383J1Z4", "29AAGCB7383J12Z4")
        assert extract_receipt(text, today=TODAY).get("gstin") == "29AAGCB7383J1Z4"
