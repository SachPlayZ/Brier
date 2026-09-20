"""Workflow, approval-guard and view tests against the database."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse
from django.utils import timezone

from expenses import services
from expenses.models import Claim, DuplicateFlag, Employee, Receipt, Vendor

pytestmark = pytest.mark.django_db

RECEIPT_TEXT = """SHARMA TRADERS PVT LTD
14 MG Road, Bengaluru 560001
GSTIN: 29AAGCB7383J1Z4
TAX INVOICE
Invoice No: INV-2025-00871
Date: 14/03/2025
Sub Total:                 995.00
CGST @ 9%:                  89.55
SGST @ 9%:                  89.55
Round Off:                  -0.10
Grand Total:             1,174.00
"""


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def vendor():
    return Vendor.objects.create(canonical_id="V001", name="Sharma Traders Pvt Ltd",
                                 gstin="29AAGCB7383J1Z4", category="stationery")


@pytest.fixture
def employee():
    return Employee.objects.create(employee_code="E001", full_name="Asha R")


@pytest.fixture
def finance_user():
    user = User.objects.create_user("finance", password="pw")
    user.groups.add(Group.objects.create(name="finance"))
    return user


def make_claim(claim_id: str, employee: Employee, *, text: str = RECEIPT_TEXT,
               receipt_id: str | None = None, extract: bool = True) -> Claim:
    receipt = Receipt.objects.create(receipt_id=receipt_id or f"R{claim_id}", text=text)
    if extract:
        services.extract_into_receipt(receipt)
    return Claim.objects.create(claim_id=claim_id, employee=employee, receipt=receipt,
                                claimed_amount=receipt.total or Decimal("1174.00"),
                                status=Claim.SUBMITTED,
                                submitted_at=timezone.now())


# ------------------------------------------------------------------ extraction
class TestExtractionPersistence:
    def test_fields_are_persisted_with_components(self, vendor, employee):
        claim = make_claim("C1", employee)
        fields = {f.name: f for f in claim.receipt.extracted.all()}
        assert fields["total"].value == "1174.00"
        assert fields["total"].components, "component breakdown must be stored"
        assert fields["total"].confidence > 0.5

    def test_vendor_links_to_the_canonical_record(self, vendor, employee):
        claim = make_claim("C1", employee)
        assert claim.receipt.vendor == vendor

    def test_arithmetic_flag_is_recorded(self, vendor, employee):
        claim = make_claim("C1", employee)
        assert claim.receipt.arithmetic_ok is True

    def test_unreadable_receipt_is_marked_for_review(self, vendor, employee):
        claim = make_claim("C1", employee, text="illegible smudge\n\n???")
        assert claim.receipt.needs_review is True


# -------------------------------------------------------------------- workflow
class TestWorkflow:
    def test_submit_writes_an_audit_event(self, vendor, employee):
        claim = make_claim("C1", employee)
        services.submit_claim(claim)
        assert claim.events.filter(action="SUBMIT").exists()

    def test_approve_moves_status_and_records_reviewer(self, vendor, employee, finance_user):
        claim = make_claim("C1", employee)
        services.approve(claim, finance_user, "looks fine")
        claim.refresh_from_db()
        assert claim.status == Claim.APPROVED
        assert claim.reviewer == finance_user and claim.decided_at is not None

    def test_every_transition_is_audited(self, vendor, employee, finance_user):
        claim = make_claim("C1", employee)
        services.start_review(claim, finance_user)
        services.approve(claim, finance_user)
        actions = list(claim.events.values_list("action", flat=True))
        assert actions == ["START_REVIEW", "APPROVE"]

    def test_illegal_transition_is_refused(self, vendor, employee, finance_user):
        claim = make_claim("C1", employee)
        services.approve(claim, finance_user)
        with pytest.raises(services.TransitionError):
            services.reject(claim, finance_user)

    def test_low_confidence_routes_to_needs_info(self, vendor, employee):
        claim = make_claim("C1", employee, text="blurred\nunreadable\n???")
        claim.status = Claim.DRAFT
        claim.save()
        services.submit_claim(claim)
        claim.refresh_from_db()
        assert claim.status == Claim.NEEDS_INFO


# ---------------------------------------------------------- the approval guard
class TestDuplicateGuard:
    def test_duplicate_submission_is_flagged(self, vendor, employee):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        pairs = services.screen_for_duplicates(second)
        assert pairs and pairs[0].band == "EXACT"
        assert DuplicateFlag.objects.filter(claim=second).exists()

    def test_approval_is_blocked_while_a_flag_is_open(self, vendor, employee, finance_user):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)

        with pytest.raises(services.TransitionError, match="duplicate"):
            services.approve(second, finance_user)
        second.refresh_from_db()
        assert second.status != Claim.APPROVED

    def test_dismissing_the_flag_unblocks_approval(self, vendor, employee, finance_user):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)

        flag = DuplicateFlag.objects.get(claim=second)
        services.resolve_flag(flag, finance_user, confirmed=False, note="team lunch, split bill")
        services.approve(second, finance_user)

        second.refresh_from_db()
        assert second.status == Claim.APPROVED

    def test_confirming_the_flag_rejects_the_claim(self, vendor, employee, finance_user):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)

        flag = DuplicateFlag.objects.get(claim=second)
        services.resolve_flag(flag, finance_user, confirmed=True)

        second.refresh_from_db()
        assert second.status == Claim.REJECTED
        assert second.events.filter(action="CONFIRM_DUPLICATE").exists()

    def test_distinct_claims_are_not_flagged(self, vendor, employee):
        make_claim("C1", employee)
        other_text = (RECEIPT_TEXT
                      .replace("INV-2025-00871", "INV-2025-04412")
                      .replace("1,174.00", "3,880.00")
                      .replace("14/03/2025", "02/05/2025"))
        second = make_claim("C2", employee, text=other_text, receipt_id="R2")
        pairs = services.screen_for_duplicates(second)
        assert not [p for p in pairs if p.band in {"EXACT", "HIGH"}]

    def test_groups_are_assigned(self, vendor, employee):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)
        services.recompute_groups()
        second.refresh_from_db()
        assert second.duplicate_group is not None


# ----------------------------------------------------------------------- views
class TestViews:
    def test_review_queue_requires_finance(self, vendor, employee, client):
        User.objects.create_user("bob", password="pw")
        client.login(username="bob", password="pw")
        response = client.get(reverse("review_queue"))
        assert response.status_code in (302, 403)

    def test_finance_sees_the_queue(self, vendor, employee, finance_user, client):
        make_claim("C1", employee)
        client.force_login(finance_user)
        response = client.get(reverse("review_queue"))
        assert response.status_code == 200
        assert b"C1" in response.content

    def test_claim_detail_shows_confidence_and_flags(self, vendor, employee,
                                                     finance_user, client):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)

        client.force_login(finance_user)
        response = client.get(reverse("claim_detail", args=["C2"]))
        assert response.status_code == 200
        assert b"Extracted fields" in response.content
        assert b"EXACT" in response.content

    def test_approve_via_view_is_blocked_by_the_guard(self, vendor, employee,
                                                      finance_user, client):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)

        client.force_login(finance_user)
        client.post(reverse("decide", args=["C2"]), {"action": "approve"})
        second.refresh_from_db()
        assert second.status != Claim.APPROVED

    def test_dup_compare_lists_the_signals(self, vendor, employee, finance_user, client):
        make_claim("C1", employee)
        second = make_claim("C2", employee, receipt_id="R2")
        services.screen_for_duplicates(second)
        flag = DuplicateFlag.objects.get(claim=second)

        client.force_login(finance_user)
        response = client.get(reverse("dup_compare", args=[flag.id]))
        assert response.status_code == 200
        assert b"Why these were matched" in response.content

    def test_dashboard_renders(self, vendor, employee, finance_user, client):
        make_claim("C1", employee)
        client.force_login(finance_user)
        response = client.get(reverse("dashboard"))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'class="finance-kpis"' in html
        assert 'class="finance-workspace"' in html
        assert "Financial control" in html
        assert "Recent claims" in html and "C1" in html

    def test_dashboard_chart_context_uses_real_counts(self, vendor, employee,
                                                       finance_user, client):
        make_claim("C1", employee)
        make_claim("C2", employee, receipt_id="R2")
        client.force_login(finance_user)
        response = client.get(reverse("dashboard"))
        submitted = next(row for row in response.context["status_rows"]
                         if row["key"] == Claim.SUBMITTED)
        assert submitted == {"key": "SUBMITTED", "label": "Submitted",
                             "count": 2, "pct": 100}
        assert list(response.context["recent_claims"])[0].claim_id == "C2"

    def test_submission_flow_end_to_end(self, vendor, employee, client):
        user = User.objects.create_user("asha", password="pw")
        employee.user = user
        employee.save()
        client.login(username="asha", password="pw")

        response = client.post(reverse("claim_submit"),
                               {"receipt_text": RECEIPT_TEXT, "description": "Stationery"},
                               follow=True)
        assert response.status_code == 200
        claim = Claim.objects.exclude(claim_id="").latest("id")
        assert claim.receipt.total == Decimal("1174.00")


class TestConcurrencySafety:
    """Regressions from "database is locked" under concurrent submissions."""

    def test_id_collision_is_retried(self, vendor, employee):
        """`max + 1` races: two simultaneous submits pick the same id."""
        from expenses.forms import _next_id, create_with_unique_id

        taken = _next_id(Receipt, "receipt_id", "U")
        Receipt.objects.create(receipt_id=taken, text=RECEIPT_TEXT)

        # A caller that already read the same maximum must still succeed.
        created = create_with_unique_id(Receipt, "receipt_id", "U", text=RECEIPT_TEXT)
        assert created.receipt_id != taken
        assert Receipt.objects.filter(receipt_id=created.receipt_id).count() == 1

    def test_ids_stay_unique_across_many_creates(self, vendor, employee):
        from expenses.forms import create_with_unique_id

        ids = {create_with_unique_id(Receipt, "receipt_id", "U",
                                     text=RECEIPT_TEXT).receipt_id
               for _ in range(6)}
        assert len(ids) == 6

    @pytest.mark.django_db(transaction=True)
    def test_screening_runs_outside_a_transaction(self, vendor, employee):
        """Dedup scoring is seconds of CPU; holding a write lock across it is
        what produced "database is locked". It must not be in an atomic block.

        Needs `transaction=True`: the default pytest-django fixture wraps each
        test in an atomic block, so autocommit is off no matter what the code
        under test does.
        """
        from django.db import transaction

        observed = {}
        original = services.find_duplicates_for_claim

        def spy(*args, **kwargs):
            observed["in_atomic"] = not transaction.get_autocommit()
            return original(*args, **kwargs)

        services.find_duplicates_for_claim = spy
        try:
            claim = make_claim("C1", employee)
            services.screen_for_duplicates(claim)
        finally:
            services.find_duplicates_for_claim = original

        assert observed.get("in_atomic") is False
