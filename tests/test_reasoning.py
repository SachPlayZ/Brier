"""The reasoning step: amounts are judged as a set, so an impossible reading cannot survive.

The trigger was a fuel receipt whose "Total GST : 283.00" line became the total amount (the bill was
1,855.20). Each field was picked alone and 283.00 looked like a fine total. These tests pin the rule:
whatever the label says, a total cannot be at most the tax it contains.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.extraction import reasoning as R
from core.extraction.pipeline import extract_receipt
from core.types import Candidate, FieldResult

D = Decimal

#: OCR of fuel_receipt_1.png (claim U00043), verbatim: "283 .00", "2 1572.20", "Rate(2/L) 8B".
NILGIRI_RAW = (
    "NILGIRI ENERGY\nWelcome\nSHYAMBAZAR FUEL POINT\n42/1, BARRACKPORE TRUNK RD\nKOLKATA 700002\n"
    "Tel. No.: 033-4602-7188\nInv. No: 5807219934061183\nFCC ID: 0000000041930256\nFIP No. : 03\n"
    "Nozzle No. : 02\nProduct : Diesel\nDensity : 831.6Kg/Cu.mtr\nPreset Type: Volume\n"
    "Rate(2/L) 8B 92.76\nVolume (L) : 00020.00\nAmount (%) : 1855.20\nTaxable Val : 2 1572.20\n"
    "CGST @9% Bs 141.50\nSGST @9% B ES 141.50\nTotal GST B ES 283 .00\nPay Mode : UPI\n"
    "UPI Ref: 425811037762\nVech: 00247718902. 44\nVtrd: 0002281946.815\nVehicle No: WBO2AK7314\n"
    "Mobile No : 98XXXXX412\nDate : 14/09/26\nTime : 19:42\nGST No: 19AAKFN4821K1Z7\n"
    "LST No: WB/LST/0086214\nVAT No: 19641837205\nRate inclusive of GST.\n"
    "Thank You! Drive Safe..\nVisit Again.")
#: The same slip after the cleaned OCR pass: different glyph noise, same numbers.
NILGIRI_PREP = (NILGIRI_RAW.replace("Rate(2/L) 8B 92.76", "Rate(/L) : 92.76")
                .replace("Amount (%) : 1855.20", "Amount (=) : 01855.20")
                .replace("Taxable Val : 2 1572.20", "Taxable Val : ® 1572.20")
                .replace("Total GST B ES 283 .00", "Total GST : 283.00"))
#: The older image of the same receipt (claim U00042): VAT layout.
NILGIRI_VAT = (NILGIRI_RAW.replace("Taxable Val : 2 1572.20", "Taxable Val : = 1585.64")
               .replace("CGST @9% Bs 141.50\nSGST @9% B ES 141.50\nTotal GST B ES 283 .00",
                        "VAT @17%(Incl) : = 269.56")
               .replace("Rate inclusive of GST.", "Fuel prices incl. VAT; GST\nnot applicable on fuel."))


class TestTheReportedReceipt:
    @pytest.mark.parametrize("text", [NILGIRI_RAW, NILGIRI_PREP])
    def test_total_is_the_amount_payable_not_the_gst(self, text):
        r = extract_receipt(text)
        assert r.get("total") == D("1855.20")
        assert r.get("tax_total") == D("283.00")
        assert r.get("subtotal") == D("1572.20")
        assert (r.get("cgst"), r.get("sgst")) == (D("141.50"), D("141.50"))
        assert r.arithmetic_ok is True

    def test_vat_version_of_the_same_receipt(self):
        r = extract_receipt(NILGIRI_VAT)
        assert r.get("total") == D("1855.20")
        assert (r.get("subtotal"), r.get("tax_total")) == (D("1585.64"), D("269.56"))

    def test_vendor_is_the_brand_above_the_greeting(self):
        assert extract_receipt(NILGIRI_RAW).get("vendor") == "NILGIRI ENERGY"


class TestUnseenLabels:
    """The fix must not depend on knowing the wording. Only the arithmetic is used."""

    BASE = ("SHOP\nInvoice No: A-100\nAmount (Rs) : 1855.20\nTaxable Val : 1572.20\n"
            "CGST @9% : 141.50\nSGST @9% : 141.50\n{line}")

    @pytest.mark.parametrize("line", [
        "Total Levy : 283.00", "Total Levy Payable: 283.00", "Total Charges: 283.00",
        "Total Duty: 283.00", "Grand Total Tax 283.00", "Total Tax : 283.00", "Total GST : 283.00",
        "Total VAT: 283.00"])
    def test_total_stays_the_bill(self, line):
        r = extract_receipt(self.BASE.format(line=line))
        assert r.get("total") == D("1855.20"), line
        assert r.arithmetic_ok is True


class TestHardRules:
    def test_a_bill_is_larger_than_its_tax(self):
        assert R.violations({"total": D("283"), "cgst": D("141.5"), "sgst": D("141.5")}) == ["total_not_above_tax"]
        assert R.violations({"total": D("1855.2"), "cgst": D("141.5"), "sgst": D("141.5")}) == []

    def test_a_tax_is_not_the_bill(self):
        assert "total_not_above_tax" in R.violations({"total": D("283"), "tax_total": D("283")})

    def test_taxable_value_cannot_exceed_the_bill_beyond_rounding(self):
        assert "taxable_above_total" in R.violations({"total": D("100"), "subtotal": D("150")})
        assert R.violations({"total": D("1634"), "subtotal": D("1634.21")}) == []      # rounded to the rupee

    def test_cgst_equals_sgst(self):
        assert R.violations({"cgst": D("10"), "sgst": D("30")}) == ["cgst_sgst_differ"]
        assert R.violations({"cgst": D("10.00"), "sgst": D("10.01")}) == []


def _fr(name, value):
    return FieldResult(name=name, value=str(value), normalized=D(str(value)) if value is not None else None)


def _cand(value, score, pid="x"):
    return Candidate(str(value), D(str(value)), (0, 1), 0, pid, rank_score=score)


class TestResolve:
    def fields(self, **v):
        return {n: _fr(n, val) for n, val in v.items()}

    def test_leaves_a_possible_set_alone(self):
        fields = self.fields(total=1855.20, subtotal=1572.20, cgst=141.50, sgst=141.50)
        assert R.resolve(fields, {}) == []

    def test_does_not_rewrite_amounts_that_merely_fail_to_add_up(self):
        """One garbled figure is not an impossible set: that is reconcile_amounts' job, and swapping
        good values for lower-ranked ones to force a sum made totals worse when it was measured."""
        fields = self.fields(total=3417.00, subtotal=3254.54, cgst=81.37, sgst=81.37)
        cands = {"total": [_cand("3417.00", .7), _cand("3335.90", .3)]}
        assert R.resolve(fields, cands) == []
        assert fields["total"].normalized == D("3417.00")

    def test_gives_up_the_least_trusted_reading(self):
        fields = self.fields(total=283, cgst=141.5, sgst=141.5, tax_total=283)
        cands = {"total": [_cand("283", .72), _cand("1855.20", .55)],
                 "cgst": [_cand("141.5", .84)], "sgst": [_cand("141.5", .84)], "tax_total": [_cand("283", .86)]}
        assert R.resolve(fields, cands) == ["total"]
        assert fields["total"].normalized == D("1855.20")
        assert any(w.startswith("reasoned:total:") for w in fields["total"].warnings)

    def test_clears_rather_than_return_an_impossible_set(self):
        fields = self.fields(total=283, cgst=141.5, sgst=141.5)
        cands = {"total": [_cand("283", .72)], "cgst": [_cand("141.5", .84)], "sgst": [_cand("141.5", .84)]}
        changed = R.resolve(fields, cands)
        assert changed == ["total"]                       # the lowest-trust reading goes
        assert fields["total"].normalized is None and fields["total"].needs_review
        assert R.violations({n: (f.normalized if isinstance(f.normalized, D) else None)
                             for n, f in fields.items()}) == []

    def test_unequal_cgst_sgst_keeps_the_better_read_one(self):
        fields = self.fields(cgst=141.5, sgst=1415.0)
        cands = {"cgst": [_cand("141.5", .84)], "sgst": [_cand("1415.0", .40), _cand("141.5", .35)]}
        R.resolve(fields, cands)
        assert fields["cgst"].normalized == D("141.5") and fields["sgst"].normalized == D("141.5")
