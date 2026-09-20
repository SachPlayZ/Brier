"""Unit tests for blocking, signals, rules, scoring and clustering."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from core.dedup import rules as R
from core.dedup import signals as S
from core.dedup.blocking import blocking_stats, generate_candidates, hamming
from core.dedup.pipeline import cluster, find_duplicates, find_duplicates_for_claim
from core.dedup.scoring import band_for, score_pair, weighted_score
from core.types import ClaimRecord, DuplicatePair

DAY = dt.date(2025, 3, 14)


def claim(cid: str, *, emp="E001", vendor="V001", invoice="INV-2025-001",
          total="1174.00", date=DAY, phash=None, status="SUBMITTED",
          submitted=None, decided=None, text="receipt text", vendor_raw="Sharma Traders",
          gstin="29AAGCB7383J1Z4") -> ClaimRecord:
    return ClaimRecord(
        claim_id=cid, employee_id=emp, vendor_canonical=vendor, vendor_raw=vendor_raw,
        gstin=gstin, invoice_no=invoice,
        total=Decimal(total) if total is not None else None, date=date, phash=phash,
        status=status, text=text,
        submitted_at=submitted or dt.datetime(2025, 3, 20, 9, 0),
        decided_at=decided)


# ------------------------------------------------------------------- signals
class TestSignals:
    def test_identical_invoice(self):
        assert S.s_invoice(claim("A"), claim("B")) == 1.0

    def test_one_character_typo_still_matches(self):
        assert S.s_invoice(claim("A"), claim("B", invoice="INV-2025-O01")) >= 0.8

    def test_different_invoice_scores_zero(self):
        assert S.s_invoice(claim("A"), claim("B", invoice="INV-2025-999")) == 0.0

    def test_missing_invoice_returns_none_not_zero(self):
        """None means 'no evidence'; zero would mean 'evidence they differ'."""
        assert S.s_invoice(claim("A", invoice=None), claim("B")) is None

    def test_exact_amount(self):
        assert S.s_amount(claim("A"), claim("B")) == 1.0

    def test_rounding_drift_scores_high(self):
        assert S.s_amount(claim("A"), claim("B", total="1174.50")) >= 0.9

    def test_far_amount_scores_zero(self):
        assert S.s_amount(claim("A"), claim("B", total="4000.00")) == 0.0

    def test_amount_decays_monotonically(self):
        near = S.s_amount(claim("A"), claim("B", total="1180.00"))
        far = S.s_amount(claim("A"), claim("B", total="1210.00"))
        assert near > far

    @pytest.mark.parametrize("days,expected", [(0, 1.0), (1, 0.90), (3, 0.75), (7, 0.50)])
    def test_date_proximity(self, days, expected):
        other = claim("B", date=DAY + dt.timedelta(days=days))
        assert S.s_date(claim("A"), other) == expected

    def test_same_gstin_forces_vendor_match(self):
        other = claim("B", vendor="V999", vendor_raw="Something Else Entirely")
        assert S.s_vendor(claim("A"), other) == 1.0

    def test_image_similarity_is_noise_gated(self):
        """Unrelated hashes sit near 50% agreement and must score zero."""
        a, b = claim("A", phash=0), claim("B", phash=(1 << 32) - 1)
        assert S.s_image(a, b) == 0.0

    def test_near_identical_image_scores_high(self):
        a, b = claim("A", phash=0), claim("B", phash=0b11)
        assert S.s_image(a, b) > 0.9

    def test_missing_signals_are_absent_not_zero(self):
        a = claim("A", invoice=None, phash=None)
        b = claim("B", invoice=None, phash=None)
        found = S.all_signals(a, b)
        assert "invoice" not in found and "image" not in found


# --------------------------------------------------------------------- rules
class TestRules:
    def test_same_invoice_same_vendor_is_exact(self):
        band, reasons = R.apply_rules(claim("A"), claim("B", emp="E002"), {})
        assert band == "EXACT" and "SAME_INVOICE_SAME_VENDOR" in reasons

    def test_same_employee_vendor_date_amount_is_exact(self):
        band, reasons = R.apply_rules(claim("A", invoice=None),
                                      claim("B", invoice=None), {})
        assert band == "EXACT" and "SAME_EMP_VENDOR_DATE_AMOUNT" in reasons

    def test_altered_invoice_number_is_flagged_high(self):
        band, reasons = R.apply_rules(claim("A"), claim("B", invoice="INV-2025-777"), {})
        assert "SUSPECT_ALTERED_INVOICE_NO" in reasons and band in {"HIGH", "EXACT"}

    def test_identical_image_alone_is_not_enough(self):
        """phash on text documents is weak evidence and needs corroboration."""
        a = claim("A", phash=0, invoice=None, vendor="V001", total="100.00")
        b = claim("B", phash=0, invoice=None, vendor="V999", total="9999.00",
                  vendor_raw="Totally Different Co", gstin="27AAPFU0939F1ZV")
        band, reasons = R.apply_rules(a, b, {})
        assert not any("IDENTICAL_IMAGE" in r for r in reasons)

    def test_identical_image_with_corroboration_is_exact(self):
        a = claim("A", phash=0, invoice=None)
        b = claim("B", phash=0, invoice=None)
        band, reasons = R.apply_rules(a, b, {})
        assert band == "EXACT"

    def test_unrelated_claims_fire_no_rules(self):
        other = claim("B", emp="E002", vendor="V009", invoice="BILL-9",
                      total="77.00", date=DAY + dt.timedelta(days=90),
                      gstin="27AAPFU0939F1ZV", vendor_raw="Other Vendor")
        band, reasons = R.apply_rules(claim("A"), other, {})
        assert band is None and reasons == []

    def test_resubmission_after_rejection_is_tagged(self):
        rejected = claim("A", status="REJECTED",
                         submitted=dt.datetime(2025, 3, 20),
                         decided=dt.datetime(2025, 3, 25))
        resubmitted = claim("B", submitted=dt.datetime(2025, 4, 2))
        pair = DuplicatePair(a="A", b="B", score=0.7, band="MEDIUM")
        tagged = R.tag_resubmissions([pair], {"A": rejected, "B": resubmitted})
        assert "RESUBMIT_AFTER_REJECT" in tagged[0].tags

    def test_split_claim_detection(self):
        whole = claim("W", invoice="INV-1", total="1000.00")
        part1 = claim("P1", invoice="INV-2", total="600.00")
        part2 = claim("P2", invoice="INV-3", total="400.00")
        found = R.detect_split_claims([whole, part1, part2])
        assert any("SPLIT_SUSPECT" in p.rules_fired for p in found)


# ------------------------------------------------------------------- scoring
class TestScoring:
    def test_missing_signals_renormalize_rather_than_penalise(self):
        full = weighted_score({"invoice": 1.0, "amount": 1.0, "vendor": 1.0,
                               "date": 1.0, "image": 1.0, "text": 1.0})
        partial = weighted_score({"amount": 1.0, "vendor": 1.0, "date": 1.0})
        assert full == pytest.approx(1.0)
        assert partial == pytest.approx(1.0)

    def test_cross_employee_is_discounted(self):
        signals = {"amount": 1.0, "vendor": 1.0, "date": 1.0}
        assert (weighted_score(signals, same_employee=False)
                < weighted_score(signals, same_employee=True))

    def test_no_signals_scores_zero(self):
        assert weighted_score({}) == 0.0

    @pytest.mark.parametrize("score,band", [
        (0.99, "EXACT"), (0.85, "HIGH"), (0.70, "MEDIUM"), (0.50, "LOW"), (0.10, "NONE")])
    def test_banding(self, score, band):
        assert band_for(score) == band

    def test_rule_overrides_weak_score(self):
        pair = score_pair(claim("A"), claim("B", emp="E002"))
        assert pair.band == "EXACT"

    def test_pair_exposes_human_readable_reasons(self):
        pair = score_pair(claim("A"), claim("B"))
        assert pair.reasons()


# ------------------------------------------------------------------ blocking
class TestBlocking:
    def test_duplicates_become_candidates(self):
        claims = [claim("A"), claim("B"),
                  claim("C", emp="E009", vendor="V009", invoice="ZZ-1",
                        total="12.00", date=DAY + dt.timedelta(days=120),
                        gstin="27AAPFU0939F1ZV", vendor_raw="Elsewhere", text="other")]
        pairs = generate_candidates(claims)
        assert ("A", "B") in pairs

    def test_blocking_stats_reports_recall_and_reduction(self):
        claims = [claim("A"), claim("B")]
        stats = blocking_stats(claims, generate_candidates(claims), [("A", "B")])
        assert stats["blocking_recall"] == 1.0
        assert 0.0 <= stats["reduction_ratio"] <= 1.0

    def test_hamming(self):
        assert hamming(0b1010, 0b1000) == 1
        assert hamming(None, 5) is None


# ------------------------------------------------------------------ pipeline
class TestPipeline:
    def test_finds_the_duplicate_and_ignores_the_stranger(self):
        claims = [claim("A"), claim("B"),
                  claim("C", emp="E009", vendor="V009", invoice="ZZ-1", total="12.00",
                        date=DAY + dt.timedelta(days=120), gstin="27AAPFU0939F1ZV",
                        vendor_raw="Elsewhere", text="unrelated")]
        found = {p.key for p in find_duplicates(claims, min_band="MEDIUM")}
        assert ("A", "B") in found
        assert not any("C" in key for key in found)

    def test_incremental_matches_the_batch_path(self):
        existing = [claim("A")]
        found = find_duplicates_for_claim(claim("B"), existing, min_band="MEDIUM")
        assert found and found[0].band == "EXACT"

    def test_clustering_groups_three_copies_together(self):
        pairs = [DuplicatePair("A", "B", 1.0, "EXACT"),
                 DuplicatePair("B", "C", 1.0, "EXACT")]
        groups = cluster(pairs)
        assert groups["A"] == groups["B"] == groups["C"]

    def test_weak_pairs_do_not_join_a_cluster(self):
        pairs = [DuplicatePair("A", "B", 1.0, "EXACT"),
                 DuplicatePair("C", "D", 0.5, "LOW")]
        groups = cluster(pairs)
        assert "C" not in groups or groups.get("C") != groups["A"]
