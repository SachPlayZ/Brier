"""Pump slips and other oddly labelled receipts.

Every case here was a live failure: a real Indian Oil slip whose meter reading became the total,
and two general defects found on the way (amounts printed without thousands separators lost their
last digits; a lost decimal point went uncorrected).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.dataset import extract_all
from core.datagen.pumpslips import generate_pump_slips
from core.eval.eval_extraction import evaluate_extraction
from core.extraction import patterns as P
from core.extraction.brands import find_brand
from core.extraction.fields import rate_times_quantity
from core.extraction.pipeline import extract_receipt
from core.extraction.vendors import VendorGazetteer

#: Tesseract's output for the slip that was misread (Downloads/69c77a5a...jpg), verbatim.
INDIAN_OIL_SLIP = (
    "Seer\nIndianOil\nWelcomes You\nSTART NELL\n178, NS C BOSE ROAD\nKOLKATA 700040\n"
    "Tel. No.: 9830036977\nInv. No: 44210663640507662\nFCC ID: 00000000028547870\nFIP No. BW)\n"
    "Nozzle No. : 03\nProduct : Petrol\nDensity : 752.7Kg/Cu.mtr\nPreset Type: Amount\n"
    "Rate(Rs/L) : 105.45\nVolume(L) +: 00005.60\nAmount(Rs) : 00590.00\nVech: 00182263484.01\n"
    "Vtrd: 0001730171.270\nVehicle No: 5852\nMobile No : Not Entered\nDate : 06/05/26\nTime : 11:28\n"
    "GST No:\nLST No:\nVAT No:\nThank You! Please Visit\nAgain..")

GAZETTEER = [("V008", "Indian Oil Fuel Station"), ("V003", "Bharat Petroleum Outlet"),
             ("V017", "Shell Select Fuel Point")]


def gaz() -> VendorGazetteer:
    return VendorGazetteer(GAZETTEER)


# ------------------------------------------------------------------ the real slip
class TestIndianOilSlip:
    def result(self, gazetteer=True):
        return extract_receipt(INDIAN_OIL_SLIP, gazetteer=gaz() if gazetteer else None)

    def test_total_is_the_amount_not_the_meter_reading(self):
        r = self.result()
        assert r.get("total") == Decimal("590.00")          # was 1730171.27 (the "Vtrd" meter)
        assert r.fields["total"].pattern_id == "total.amount_label"

    def test_invoice_number_with_a_dot_after_inv(self):
        assert self.result().get("invoice_no") == "44210663640507662"

    def test_vendor_is_the_brand_not_the_dealer(self):
        assert self.result().get("vendor") == "V008"
        assert self.result(gazetteer=False).get("vendor") == "Indian Oil"   # no gazetteer: brand name

    def test_date_currency_and_no_invented_tax_fields(self):
        r = self.result()
        assert r.get("date").isoformat() == "2026-05-06"
        assert r.currency == "INR"
        assert [r.get(k) for k in ("subtotal", "cgst", "sgst", "igst", "tax_total")] == [None] * 5

    def test_rate_times_volume_confirms_the_amount(self):
        r = self.result()
        assert r.arithmetic_ok is True                       # 105.45 x 5.60 = 590.52, within a rupee
        assert r.fields["total"].confidence >= 0.6           # not routed to manual review


# ---------------------------------------------------------------------- decoys
class TestNumbersThatAreNotMoney:
    def test_three_decimal_reading_is_not_an_amount(self):
        assert P.ANY_AMOUNT.search("Vtrd: 0001730171.270") is None

    def test_meter_reading_never_wins_the_positional_fallback(self):
        text = "SOME SHOP\nVech: 00182263484.01\nVtrd: 0001730171.270\nBalance owed 12.50"
        assert extract_receipt(text).get("total") != Decimal("1730171.27")

    def test_explicit_total_beats_an_amount_line(self):
        text = "SHOP\nAmount(Rs) : 100.00\nTotal: 590.00"
        assert extract_receipt(text).get("total") == Decimal("590.00")

    @pytest.mark.parametrize("line", [
        "Tax Amount: 89.55", "Taxable Amount: 995.00", "Amount before tax: 995.00",
        "Taxble Amount: 195.98", "CGST Amount: 44.78", "Item      Qty      Amount\nPetrol   20   100.00",
        "Amount Payab1e: 3,417.00"])
    def test_amount_label_ignores_lines_that_are_not_the_payable(self, line):
        assert P.TOTAL_AMOUNT_LABEL.search(line) is None

    def test_a_mangled_label_still_finds_the_real_amount(self):
        """'Payab1e' once yielded a total of 1 (the digit inside the word)."""
        text = "SHOP\nSubtotal:  3,254.54\nAmount Payab1e:   3,417.00"
        assert extract_receipt(text).get("total") == Decimal("3417.00")


# -------------------------------------------------------------------- variants
class TestAmountLabels:
    @pytest.mark.parametrize("label", [
        "Amount(Rs) : 00590.00", "Amount (Rs.) : 00590.00", "Amt(Rs) : 590.00", "Sale Amt 00590.00",
        "Amount (INR) : 00590.00", "Total Rs. : 590.00", "Amount Rs : 00590.00", "Amount: 590.00",
        "Net Amount: 590.00", "You Pay : 590.00"])
    def test_label_variants_and_zero_padding(self, label):
        text = f"IndianOil\nRate(Rs/L) : 100.00\nVolume(L) : 5.90\n{label}"
        assert extract_receipt(text).get("total") == Decimal("590.00")

    @pytest.mark.parametrize("label", ["Inv. No", "Inv.No", "Inv No.", "Invoice No", "Bill No.", "Bill. No"])
    def test_invoice_label_variants(self, label):
        text = f"IndianOil\n{label}: 44210663640507662\nAmount(Rs) : 590.00"
        assert extract_receipt(text).get("invoice_no") == "44210663640507662"


# ---------------------------------------------------------------------- brands
class TestBrands:
    @pytest.mark.parametrize("line,name", [
        ("IndianOil", "Indian Oil"), ("INDIAN OIL", "Indian Oil"), ("Bharat Petroleum", "Bharat Petroleum"),
        ("BPCL", "Bharat Petroleum"), ("HP", "Hindustan Petroleum"), ("Shell", "Shell"),
        ("SHLL", "Shell"), ("5hell", "Shell"), ("INDUSTAN PEROLEUM", "Hindustan Petroleum"),
        ("Nayara Energy", "Nayara Energy")])
    def test_brand_lines(self, line, name):
        assert find_brand(line).name == name

    @pytest.mark.parametrize("line", [
        "START NELL", "SHARMA TRADERS PVT LTD", "Hindustan Unilever", "Bharat Electronics", "Oil India Ltd",
        "Shelly's Snacks", "Welcomes You", "HELLO", "Reliance Fresh Supermarket", "Annapurna Restaurant"])
    def test_ordinary_names_are_not_brands(self, line):
        assert find_brand(line) is None

    def test_dealer_loses_to_the_brand(self):
        text = "SHREE GANESH FUELS\nBharat Petroleum\nPlot 5\nAmount(Rs) : 100.00"
        assert extract_receipt(text, gazetteer=gaz()).get("vendor") == "V003"


# ---------------------------------------------------- general defects found on the way
class TestAmountsWithoutThousandsSeparators:
    @pytest.mark.parametrize("text,expected", [
        ("Total: 1174.00", "1174.00"), ("Grand Total: 12345.50", "12345.50"), ("Total: 2207.00", "2207.00"),
        ("Total: Rs 15000", "15000"), ("Total: 1,174.00", "1174.00"), ("Total: 10,00,000.00", "1000000.00"),
        ("Total: 00590.00", "590.00"), ("Total: 5.60", "5.60")])
    def test_total_keeps_every_digit(self, text, expected):
        """`NUM` used to read "1174.00" as 117: the shared number pattern took its first branch."""
        assert extract_receipt(f"SHOP\nInvoice No: A-100\n{text}").get("total") == Decimal(expected)


class TestLostDecimalPoint:
    BILL = "SHOP LTD\nSubtotal:  200.00\nCGST @ 2.5%:  5.00\nSGST @ 2.5%:  5.00\nTotal:  {total}"

    def test_a_dropped_decimal_point_is_restored_when_the_bill_balances(self):
        r = extract_receipt(self.BILL.format(total="21000"))
        assert r.get("total") == Decimal("210.00")
        assert any(w.startswith("repaired:lost_decimal") for w in r.warnings)
        assert r.arithmetic_ok is True

    def test_a_real_large_total_is_left_alone(self):
        text = "SHOP LTD\nSubtotal:  20,000.00\nCGST @ 2.5%:  500.00\nSGST @ 2.5%:  500.00\nTotal:  21,000.00"
        r = extract_receipt(text)
        assert r.get("total") == Decimal("21000.00")
        assert not any("lost_decimal" in w for w in r.warnings)

    def test_cgst_and_sgst_stay_equal(self):
        """A solved-for SGST that differs from the printed CGST means another amount is off."""
        text = "SHOP LTD\nSubtotal:  1,671.25\nCGST @ 2.5%:  41.78\nTotal:  1,755.00"
        r = extract_receipt(text)
        assert r.get("sgst") is None
        assert "implausible_repair:sgst" in r.warnings


# ------------------------------------------------------------- rate x quantity
class TestRateTimesQuantity:
    def test_expected_amount(self):
        assert rate_times_quantity("Rate(Rs/L) : 105.45\nVolume(L) : 00005.60") == Decimal("590.52")

    @pytest.mark.parametrize("text", ["Volume(L) : 5.60", "Rate(Rs/L) : 105.45", "Rate: 0\nQty: 3", "nothing"])
    def test_needs_both_numbers(self, text):
        assert rate_times_quantity(text) is None

    def test_an_unlabelled_amount_is_derived_and_marked_repaired(self):
        text = "IndianOil\nRate(Rs/L) : 105.45\nVolume(L) : 00005.60\nVtrd: 0001730171.270"
        r = extract_receipt(text)
        assert r.get("total") == Decimal("590.52")
        assert r.fields["total"].pattern_id == "total.derived"
        assert any(w.startswith("repaired:") for w in r.warnings)


# ------------------------------------------------------------ the generated set
class TestPumpSlipGenerator:
    def test_same_seed_same_slips(self, tmp_path):
        generate_pump_slips(tmp_path / "a", count=8, seed=43)
        generate_pump_slips(tmp_path / "b", count=8, seed=43)
        for name in ("receipts_truth.csv", "receipts_text/P00003.txt", "vendors.csv"):
            assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()

    def test_truth_amount_is_what_the_slip_prints(self, tmp_path):
        generate_pump_slips(tmp_path, count=12, seed=7)
        import csv
        for row in csv.DictReader((tmp_path / "receipts_truth.csv").open(encoding="utf-8")):
            text = (tmp_path / "receipts_text" / f"{row['receipt_id']}.txt").read_text(encoding="utf-8")
            assert f"{Decimal(row['total']):08.2f}" in text
            assert row["invoice_no"] in text

    def test_clean_text_is_extracted_almost_perfectly(self, tmp_path):
        """No OCR damage and no glyph artefacts: what remains is layout coverage, near-complete."""
        generate_pump_slips(tmp_path, count=60, seed=5, artifacts=False)
        by = self.scores(tmp_path)
        assert by["total"].f1 >= 0.97
        assert by["vendor"].f1 >= 0.97
        assert by["invoice_no"].f1 >= 0.95
        assert by["subtotal"].f1 >= 0.95 and by["cgst"].f1 >= 0.95

    def test_the_ocr_artefacts_seen_on_real_slips_do_not_move_the_total(self, tmp_path):
        """The rupee glyph read as 2 / = / % / Bs and "283 .00": the bill must still be the bill."""
        generate_pump_slips(tmp_path, count=60, seed=5, artifacts=True)
        by = self.scores(tmp_path)
        assert by["total"].f1 >= 0.97
        assert by["subtotal"].f1 >= 0.9 and by["cgst"].f1 >= 0.9

    @staticmethod
    def scores(folder):
        results = extract_all(folder, mode="clean")
        report = evaluate_extraction(results, folder / "receipts_truth.csv")
        return {f.name: f for f in report.fields.values()}
