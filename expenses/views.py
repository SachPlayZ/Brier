"""Views for submission, review and duplicate resolution."""
from __future__ import annotations

import json
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.utils import timezone

from core.extraction import confidence as C
from expenses import services
from expenses.forms import (ClaimSubmitForm, FieldCorrectionForm, ReviewDecisionForm,
                            SignUpForm, create_with_unique_id)
from expenses.models import Claim, DuplicateFlag, Employee, ExtractedField, Receipt
from expenses.uploads import prepare_upload

FINANCE_GROUP = "finance"


def is_finance(user) -> bool:
    return user.is_authenticated and (user.is_superuser
                                      or user.groups.filter(name=FINANCE_GROUP).exists())


finance_required = user_passes_test(is_finance)


def home_for(user) -> str:
    """Where a signed-in user belongs: reviewers get the overview, employees their claims."""
    return "dashboard" if is_finance(user) else "claim_list"


class CustomLoginView(LoginView):
	template_name = "expenses/login.html"

	def dispatch(self, request, *args, **kwargs):
		if request.user.is_authenticated:
			return redirect(home_for(request.user))
		return super().dispatch(request, *args, **kwargs)

	def get_default_redirect_url(self):
		"""Send each role to its own home, the same as `dispatch` already does.

		`LOGIN_REDIRECT_URL` is a single value, so without this a reviewer signing
		in lands on the employee claim list. An explicit `?next=` still wins,
		because `get_success_url` prefers it over this.
		"""
		return resolve_url(home_for(self.request.user))


def signup(request):
    """Create an employee account, sign the new user in, and send them to their first claim."""
    if request.user.is_authenticated:
        return redirect(home_for(request.user))
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user, employee = form.save()
        login(request, user)
        messages.success(request, f"Welcome to Brier, {user.first_name}. "
                                  f"Your employee code is {employee.employee_code}.")
        return redirect("claim_submit")
    return render(request, "expenses/signup.html", {"form": form})


# ------------------------------------------------------------------ landing
LANDING_FIELDS = {"vendor": "Vendor", "date": "Date", "total": "Total", "gstin": "GSTIN",
                  "invoice_no": "Invoice number"}


def landing_metrics(path=None) -> dict | None:
    """Measured numbers for the landing page, read from the evaluation report.

    They come from ``data/eval_report.json`` at request time so the page can never drift
    from what was measured (a copied constant would silently go stale). A missing or
    unreadable report, or a section the report does not contain, is simply left out.
    """
    path = path or settings.BASE_DIR / "data" / "eval_report.json"
    try:
        return _metrics_from(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Unreadable, not JSON, or JSON in a shape this does not understand. The
        # landing page is the front door for every logged-out visitor, so a report
        # written by an older version of `evaluate` must drop the numbers, not
        # return a 500. The template already renders without them.
        return None


def _metrics_from(report: dict) -> dict | None:
    extraction = report.get("extraction") or {}
    by_field = {f["field"]: f for f in extraction.get("fields") or []}
    if not by_field:
        return None
    tiers = extraction.get("by_noise_tier") or {}

    def tier_f1(tier: str, name: str):
        for row in tiers.get(tier) or []:
            if row.get("field") == name:
                return row.get("f1")
        return None

    rows = [{"name": name, "label": label, "overall": by_field[name]["f1"],
             "clean": tier_f1("0", name), "photo": tier_f1("3", name)}
            for name, label in LANDING_FIELDS.items() if name in by_field]
    rate = extraction.get("arithmetic_ok_rate")
    return {
        "receipts": extraction.get("n_receipts") or (report.get("meta") or {}).get("receipts"),
        "mode": (report.get("meta") or {}).get("mode"),
        "vendor_f1": (by_field.get("vendor") or {}).get("f1"),
        "total_f1": (by_field.get("total") or {}).get("f1"),
        "arithmetic_pct": round(rate * 100, 1) if rate is not None else None,
        "ece": (extraction.get("calibration") or {}).get("ece"),
        "rows": rows,
    }


def landing(request):
    return render(request, "expenses/landing.html", {"metrics": landing_metrics()})


# ------------------------------------------------------------------ employee
@login_required
def claim_list(request):
    claims = (Claim.objects.select_related("receipt", "employee", "receipt__vendor")
              .defer("receipt__image_blob")
              .annotate(n_flags=Count("flags_as_a", filter=Q(flags_as_a__status=DuplicateFlag.OPEN))))
    finance = is_finance(request.user)
    employee = getattr(request.user, "employee", None)
    # Only reviewers may widen the list. Everyone else sees their own claims, and a login
    # with no employee record sees none, never "all".
    mine = not finance or request.GET.get("scope", "mine") == "mine"
    if mine:
        claims = claims.filter(employee=employee) if employee is not None else claims.none()
    status = request.GET.get("status")
    if status:
        claims = claims.filter(status=status)

    return render(request, "expenses/claim_list.html", {
        "claims": claims[:200], "statuses": Claim.STATUSES,
        "current_status": status, "scope": "mine" if mine else "all",
        "is_finance": finance,
    })


@login_required
def claim_submit(request):
    """Upload or paste a receipt, extract it, then screen for duplicates."""
    if request.method != "POST":
        return render(request, "expenses/claim_submit.html", {"form": ClaimSubmitForm()})

    form = ClaimSubmitForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, "expenses/claim_submit.html", {"form": form})

    employee = getattr(request.user, "employee", None)
    if employee is None:
        employee = create_with_unique_id(
            Employee, "employee_code", "E", width=5, user=request.user,
            full_name=request.user.get_full_name())

    upload = form.cleaned_data.get("image")
    image, exact_text = prepare_upload(upload) if upload else (None, "")
    receipt = create_with_unique_id(
        Receipt, "receipt_id", "U",
        text=form.cleaned_data.get("receipt_text") or exact_text,
        image=image)

    try:
        result = services.extract_into_receipt(receipt)
    except RuntimeError as exc:
        receipt.delete()
        messages.error(request, str(exc))
        return render(request, "expenses/claim_submit.html", {"form": form})

    foreign = services.foreign_currency(result)
    if foreign:
        receipt.image.delete(save=False)
        receipt.delete()
        messages.error(request, f"Brier handles INR receipts only. This one looks like {foreign}.")
        return render(request, "expenses/claim_submit.html", {"form": form})

    claim = create_with_unique_id(
        Claim, "claim_id", "U",
        employee=employee, receipt=receipt,
        claimed_amount=receipt.total or form.cleaned_data.get("claimed_amount"),
        description=form.cleaned_data.get("description", ""),
        category=receipt.vendor.category if receipt.vendor else "",
        status=Claim.DRAFT)

    pairs = services.submit_claim(claim, request.user)
    services.recompute_groups()

    if pairs:
        messages.warning(request, f"{len(pairs)} possible duplicate(s) found. "
                                  "Finance will review before approval.")
    else:
        messages.success(request, f"{claim.claim_id} submitted. No duplicates found.")
    return redirect("claim_detail", claim_id=claim.claim_id)


@login_required
def claim_detail(request, claim_id: str):
    claims = (Claim.objects.select_related("receipt", "employee", "receipt__vendor")
              .defer("receipt__image_blob"))
    if not is_finance(request.user):
        claims = claims.filter(employee__user=request.user)     # 404, not 403: do not confirm ids exist
    claim = get_object_or_404(claims, claim_id=claim_id)
    fields = list(claim.receipt.extracted.all())

    return render(request, "expenses/claim_detail.html", {
        "claim": claim,
        "fields": _with_explanations(fields, claim.receipt),
        "amounts": amounts_check(claim.receipt, fields),
        "flags": claim.all_flags.select_related("claim", "matched_claim"),
        "events": claim.events.select_related("actor"),
        "is_finance": is_finance(request.user),
        "blocking": list(claim.blocking_flags()),
        "decision_form": ReviewDecisionForm(),
        "correction_form": FieldCorrectionForm(),
    })


# ------------------------------------------------------------------- finance
@login_required
@finance_required
def review_queue(request):
    """Everything awaiting a decision, worst first."""
    claims = (Claim.objects.filter(status__in=Claim.OPEN_STATUSES)
              .select_related("receipt", "employee", "receipt__vendor")
              .defer("receipt__image_blob")
              .annotate(n_open=Count("flags_as_a",
                                     filter=Q(flags_as_a__status=DuplicateFlag.OPEN))))

    band = request.GET.get("band")
    if band:
        claims = claims.filter(flags_as_a__band=band,
                               flags_as_a__status=DuplicateFlag.OPEN).distinct()
    if request.GET.get("only") == "flagged":
        claims = claims.filter(n_open__gt=0)
    if request.GET.get("only") == "lowconf":
        claims = claims.filter(receipt__needs_review=True)

    sort = request.GET.get("sort", "risk")
    ordering = {"amount": "-claimed_amount", "confidence": "receipt__doc_confidence",
                "date": "-submitted_at"}.get(sort)
    claims = list(claims.order_by(ordering) if ordering else claims)
    if sort == "risk":
        claims.sort(key=_risk_key, reverse=True)

    return render(request, "expenses/review_queue.html", {
        "claims": claims[:200], "band": band, "sort": sort,
        "only": request.GET.get("only", ""),
        "bands": ["EXACT", "HIGH", "MEDIUM", "LOW"], "is_finance": True,
    })


def _risk_key(claim: Claim) -> tuple:
    rank = {"EXACT": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    worst = max((rank.get(f.band, 0) for f in claim.flags_as_a.all()
                 if f.status == DuplicateFlag.OPEN), default=0)
    return (worst, float(claim.claimed_amount or 0))


@login_required
@finance_required
def dup_compare(request, flag_id: int):
    """Side-by-side comparison with the matching signals spelled out."""
    flag = get_object_or_404(
        DuplicateFlag.objects.select_related(
            "claim__receipt", "matched_claim__receipt",
            "claim__employee", "matched_claim__employee"), pk=flag_id)

    if request.method == "POST":
        action = request.POST.get("action")
        note = request.POST.get("note", "")
        if action in {"confirm", "dismiss"}:
            services.resolve_flag(flag, request.user, confirmed=action == "confirm",
                                  note=note)
            messages.success(request, f"Flag {'confirmed' if action == 'confirm' else 'dismissed'}.")
            return redirect("claim_detail", claim_id=flag.claim.claim_id)

    left_fields = {f.name: f for f in flag.claim.receipt.extracted.all()}
    right_fields = {f.name: f for f in flag.matched_claim.receipt.extracted.all()}
    names = sorted(set(left_fields) | set(right_fields))

    rows = []
    for name in names:
        left, right = left_fields.get(name), right_fields.get(name)
        lv = left.display_value if left else ""
        rv = right.display_value if right else ""
        if not lv and not rv:
            verdict = "absent"          # neither receipt carries this field
        elif lv == rv:
            verdict = "identical"
        else:
            verdict = "differs"
        rows.append({"name": name, "left": left, "right": right,
                     "verdict": verdict, "same": verdict == "identical"})

    return render(request, "expenses/dup_compare.html", {
        "flag": flag, "rows": rows, "is_finance": True,
        "signals": _signal_rows(flag.signals or {}),
    })


SIGNAL_LABELS = {
    "invoice": "Invoice number", "amount": "Amount", "vendor": "Vendor name",
    "date": "Date", "image": "Receipt image (perceptual hash)",
    "text": "Receipt text similarity",
}


def _signal_rows(signals: dict) -> list[dict]:
    """Signals as display rows, strongest first, with percentages precomputed."""
    rows = []
    for name, value in sorted(signals.items(), key=lambda kv: -kv[1]):
        value = float(value)
        rows.append({
            "name": name,
            "label": SIGNAL_LABELS.get(name, name),
            "value": round(value, 3),
            "pct": int(round(value * 100)),
            "band": "high" if value >= 0.75 else "medium" if value >= 0.4 else "low",
        })
    return rows


@login_required
@finance_required
def decide(request, claim_id: str):
    """Approve / reject / request info, honouring the duplicate guard."""
    claim = get_object_or_404(Claim, claim_id=claim_id)
    if request.method != "POST":
        return redirect("claim_detail", claim_id=claim_id)

    form = ReviewDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid decision.")
        return redirect("claim_detail", claim_id=claim_id)

    action = form.cleaned_data["action"]
    note = form.cleaned_data.get("note", "")
    try:
        if action == "review":
            services.start_review(claim, request.user)
        elif action == "approve":
            services.approve(claim, request.user, note)
            messages.success(request, f"{claim.claim_id} approved.")
        elif action == "reject":
            services.reject(claim, request.user, note)
            messages.success(request, f"{claim.claim_id} rejected.")
        elif action == "info":
            services.request_info(claim, request.user, note)
    except services.TransitionError as exc:
        messages.error(request, str(exc))
    return redirect("claim_detail", claim_id=claim_id)


@login_required
@finance_required
def correct_field(request, field_id: int):
    """Record a reviewer's manual correction of a low-confidence field."""
    field = get_object_or_404(ExtractedField.objects.select_related("receipt"), pk=field_id)
    if request.method != "POST":
        return redirect("claim_list")

    form = FieldCorrectionForm(request.POST)
    if form.is_valid():
        try:
            services.correct_extracted_field(field, form.cleaned_data["value"], request.user)
            messages.success(request, f"{field.name} corrected.")
        except services.TransitionError as exc:
            messages.error(request, str(exc))

    claim = field.receipt.claims.first()
    return redirect("claim_detail", claim_id=claim.claim_id) if claim else redirect("claim_list")


def dashboard(request):
    """Status counts, exposure prevented, and current extraction health (reviewers only).

    `/` is the front door, so a visitor who is not signed in gets the landing page
    rather than a login form. Deep links stay `@login_required` and still bounce
    through `/login/?next=...`, which is what someone following a link expects.
    """
    if not request.user.is_authenticated:
        return redirect("landing")
    if not is_finance(request.user):
        return redirect("claim_list")
    by_status = dict(Claim.objects.values_list("status").annotate(n=Count("id")))
    n_claims = Claim.objects.count()
    flags = DuplicateFlag.objects.all()

    confirmed = flags.filter(status=DuplicateFlag.CONFIRMED)
    open_flags = flags.filter(status=DuplicateFlag.OPEN)

    # Money is grouped by currency rather than summed. Adding rupees to dollars
    # would produce a confident, meaningless number on the finance dashboard.
    prevented_by_currency = _money_by_currency(confirmed)
    at_risk_by_currency = _money_by_currency(
        open_flags.filter(band__in=["EXACT", "HIGH"]))

    receipts = Receipt.objects.all()
    n_receipts = receipts.count() or 1
    raw_band_counts = list(flags.values("band").annotate(n=Count("id")).order_by("-n"))
    max_band_count = max((row["n"] for row in raw_band_counts), default=1)
    band_counts = [
        {**row, "pct": round(row["n"] * 100 / max_band_count)}
        for row in raw_band_counts
    ]
    status_rows = [
        {
            "key": key,
            "label": label,
            "count": by_status.get(key, 0),
            "pct": round(by_status.get(key, 0) * 100 / (n_claims or 1)),
        }
        for key, label in Claim.STATUSES
    ]
    recent_claims = list(
        Claim.objects.select_related("receipt", "receipt__vendor", "employee")
        .only(
            "claim_id", "claimed_amount", "status", "submitted_at",
            "employee__employee_code", "receipt__currency", "receipt__vendor_raw",
            "receipt__vendor__name",
        )
        .annotate(
            n_open_flags=Count(
                "flags_as_a", filter=Q(flags_as_a__status=DuplicateFlag.OPEN)
            )
        )
        .order_by("-submitted_at")[:6]
    )
    mean_confidence = round(
        sum(receipts.values_list("doc_confidence", flat=True)) / n_receipts, 3)

    return render(request, "expenses/dashboard.html", {
        "status_rows": status_rows,
        "n_claims": n_claims,
        "n_receipts": receipts.count(),
        "n_flags": flags.count(),
        "n_open_flags": open_flags.count(),
        "n_confirmed": confirmed.count(),
        "prevented_by_currency": prevented_by_currency,
        "at_risk_by_currency": at_risk_by_currency,
        "band_counts": band_counts,
        "needs_review_pct": round(
            receipts.filter(needs_review=True).count() * 100 / n_receipts, 1),
        "arithmetic_pct": round(
            receipts.filter(arithmetic_ok=True).count() * 100 / n_receipts, 1),
        "mean_confidence": mean_confidence,
        "mean_confidence_pct": round(mean_confidence * 100),
        "open_claims": Claim.objects.filter(status__in=Claim.OPEN_STATUSES).count(),
        "recent_claims": recent_claims,
        "is_finance": is_finance(request.user),
        "now": timezone.now(),
    })


def _money_by_currency(flag_qs) -> list[dict]:
    """Sum flagged amounts per currency, largest first."""
    from core.currency import format_amount

    rows = (flag_qs.values("claim__receipt__currency")
            .annotate(total=Sum("claim__claimed_amount"))
            .order_by("-total"))
    out = []
    for row in rows:
        code = row["claim__receipt__currency"] or "INR"
        total = row["total"] or Decimal("0")
        out.append({"currency": code, "total": total,
                    "display": format_amount(total, code)})
    return out


#: What a reader should see, in reading order. "total" and "tax_total" used to appear raw and in
#: alphabetical order, and a reviewer took the tax total for the bill.
FIELD_ORDER = ("vendor", "date", "invoice_no", "gstin", "subtotal", "cgst", "sgst", "igst",
               "tax_total", "total")
FIELD_LABELS = {
    "vendor": "Vendor", "date": "Invoice date", "invoice_no": "Invoice number", "gstin": "GSTIN",
    "subtotal": "Taxable value", "cgst": "CGST", "sgst": "SGST", "igst": "IGST",
    "tax_total": "Total tax", "total": "Total amount",
}
FIELD_HINTS = {
    "subtotal": "Before tax", "tax_total": "All tax added up, not the amount to pay",
    "total": "Payable, tax included",
}
_MONEY_FIELDS = {"subtotal", "cgst", "sgst", "igst", "tax_total", "total"}
_NOTE_TEXT = {
    "repaired": "Worked out from the other amounts, because it was not printed clearly.",
    "validator_failed": "Failed its format check, so it may be misread or mistyped.",
    "cgst_sgst_mismatch": "CGST and SGST should be equal, but they are not.",
    "ambiguous_dmy": "The day and month could be either way round.",
}


def _notes(field) -> list[str]:
    """Plain-language reasons a reviewer should look at this field."""
    notes: list[str] = []
    for warning in field.warnings or []:
        code = warning.split(":", 1)[0]
        if code == "reasoned":
            notes.append(warning.split(":", 2)[2] if warning.count(":") >= 2 else "Checked against the other amounts.")
        elif code in _NOTE_TEXT and _NOTE_TEXT[code] not in notes:
            notes.append(_NOTE_TEXT[code])
    return notes


def _display(receipt, field) -> str:
    """The value as a person reads it: money with its symbol and grouping, vendor by name."""
    value = field.display_value
    if not value:
        return ""
    if field.name in _MONEY_FIELDS:
        amount = _decimal_or_none(value)
        return receipt.money(amount) if amount is not None else value
    if field.name == "vendor":
        return receipt.vendor.name if receipt.vendor else (receipt.vendor_raw or value)
    return value


def _decimal_or_none(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception:  # noqa: BLE001 - anything that is not a number
        return None


def _with_explanations(fields, receipt=None) -> list[dict]:
    """Fields in reading order with a human label, a readable value, plain notes, and the
    weighted component breakdown each score came from."""
    def rank(field) -> int:
        return FIELD_ORDER.index(field.name) if field.name in FIELD_ORDER else len(FIELD_ORDER)

    out = []
    for field in sorted(fields, key=rank):
        rows = [{"name": name, "label": C.component_label(name),
                 "value": round(value, 3), "contribution": round(contribution, 3)}
                for name, value, contribution in _explain(field)]
        out.append({"field": field, "components": rows,
                    "label": FIELD_LABELS.get(field.name, field.name),
                    "hint": FIELD_HINTS.get(field.name, ""),
                    "display": _display(receipt, field) if receipt is not None else field.display_value,
                    "notes": _notes(field)})
    return out


def amounts_check(receipt, fields) -> dict | None:
    """The bill's own arithmetic with the real numbers: taxable + tax = total, or the gap.

    ``None`` when the receipt does not give enough to add up. Uses corrected values, so a
    reviewer's fix is reflected immediately."""
    values = {f.name: _decimal_or_none(f.display_value) for f in fields}
    total, subtotal = values.get("total"), values.get("subtotal")
    if total is None:
        return None
    components = [(FIELD_LABELS[n], values[n]) for n in ("cgst", "sgst", "igst") if values.get(n) is not None]
    if not components and values.get("tax_total") is not None:
        components = [(FIELD_LABELS["tax_total"], values["tax_total"])]
    terms = ([(FIELD_LABELS["subtotal"], subtotal)] if subtotal is not None else []) + components
    if len(terms) < 2:
        return None
    expected = sum((amount for _label, amount in terms), Decimal("0"))
    gap = total - expected
    tolerance = max(Decimal("0.02"), (total * Decimal("0.005")).quantize(Decimal("0.01")))
    return {"terms": [{"label": label, "amount": receipt.money(amount)} for label, amount in terms],
            "sum": receipt.money(expected), "total": receipt.money(total),
            "ok": abs(gap) <= tolerance,
            "rounded": tolerance < abs(gap) <= Decimal("1.00"),
            "gap": receipt.money(abs(gap)), "over": gap < 0}


def _explain(field) -> list[tuple[str, float, float]]:
    weights = C.FIELD_WEIGHTS.get(field.name, {})
    components = field.components or {}
    rows = [(name, float(components.get(name, 0.0)),
             weight * float(components.get(name, 0.0)))
            for name, weight in weights.items()]
    return sorted(rows, key=lambda r: -r[2])
