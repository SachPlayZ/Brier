"""No extraction may contain an impossible set of amounts, whatever the text.

The sweep that found the "Total GST became the total" class of error: run the extractor over every
kind of text we can generate, at every OCR-damage tier, and require ZERO violations of the hard
rules. A new wording that breaks a rule fails here instead of reaching a reviewer.

(Amounts that merely fail to add up are allowed: on damaged text that is one garbled figure, and it
is flagged. What is never allowed is a set that cannot be true.)
"""
from __future__ import annotations

import pytest

from core.dataset import extract_all
from core.datagen.generate import generate_dataset
from core.datagen.pumpslips import generate_pump_slips
from core.extraction import reasoning as R
from core.extraction.pipeline import extract_receipt
from core.types import ExtractionResult

from decimal import Decimal


def violations_of(result: ExtractionResult) -> list[str]:
    values = {n: (result.get(n) if isinstance(result.get(n), Decimal) else None) for n in R.NAMES}
    return R.violations(values)


def sweep(results: dict[str, ExtractionResult]) -> dict[str, list[str]]:
    return {rid: bad for rid, r in results.items() if (bad := violations_of(r))}


@pytest.fixture(scope="module")
def main_corpus(tmp_path_factory):
    folder = tmp_path_factory.mktemp("main")
    generate_dataset(folder, n_receipts=90, n_claims=110, seed=7, render_images=False)
    return folder


def test_main_generator_at_every_noise_tier(main_corpus):
    results = extract_all(main_corpus, mode="simulated")
    assert len(results) >= 90
    assert sweep(results) == {}


def test_main_generator_clean(main_corpus):
    assert sweep(extract_all(main_corpus, mode="clean")) == {}


@pytest.mark.parametrize("artifacts", [True, False])
def test_pump_slips(tmp_path, artifacts):
    generate_pump_slips(tmp_path, count=90, seed=11, artifacts=artifacts)
    assert sweep(extract_all(tmp_path, mode="simulated")) == {}


@pytest.mark.parametrize("line", [
    "Total GST : 283.00", "Total Tax: 283.00", "Total Levy : 283.00", "Total VAT 283.00", "Total Cess: 283.00",
    "TOTAL TAXES 283.00", "Tax Total : 283.00", "Total Tax Amount : 283.00", "Sub Total : 1572.20"])
def test_decoy_totals_on_a_tax_bill(line):
    text = ("SHOP\nInvoice No: A-100\nAmount (Rs) : 1855.20\nTaxable Val : 1572.20\n"
            f"CGST @9% : 141.50\nSGST @9% : 141.50\n{line}")
    assert violations_of(extract_receipt(text)) == []
