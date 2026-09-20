"""Authenticated Streamlit UI over Brier's existing Django service layer."""

from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import Case, Count, IntegerField, Max, Q, Sum, When
from django.utils import timezone

from core import object_storage
from core.currency import format_amount
from core.ocr import ocr_status
from expenses import services
from expenses.forms import SignUpForm, create_with_unique_id
from expenses.models import (
    AuditEvent,
    Claim,
    DuplicateFlag,
    Employee,
    ExtractedField,
    Receipt,
)
from expenses.uploads import ACCEPT, prepare_upload_blob, validate_receipt_upload

PAGES = {
    "dashboard": "Control room",
    "upload": "Submit invoice",
    "claims": "Claims ledger",
    "review": "Review queue",
    "claim": "Claim detail",
}

FIELD_LABELS = {
    "vendor": "Vendor",
    "date": "Invoice date",
    "invoice_no": "Invoice number",
    "gstin": "GSTIN",
    "subtotal": "Taxable value",
    "cgst": "CGST",
    "sgst": "SGST",
    "igst": "IGST",
    "tax_total": "Total tax",
    "total": "Total amount",
}

logger = logging.getLogger(__name__)


def main() -> None:
    _styles()
    user = _current_user()
    if user is None:
        _auth_screen()
        return
    finance = _is_finance(user)
    _sidebar(user, finance)
    page = st.session_state.get("page", "dashboard" if finance else "claims")
    if page == "dashboard" and finance:
        _dashboard()
    elif page == "upload":
        _upload(user)
    elif page == "review" and finance:
        _review_queue(user)
    elif page == "claim":
        _claim_detail(user, finance)
    else:
        _claims(user, finance)


# --------------------------------------------------------------------- identity
def _current_user() -> User | None:
    user_id = st.session_state.get("user_id")
    if not user_id:
        return None
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        st.session_state.pop("user_id", None)
    return user


def _is_finance(user: User) -> bool:
    return user.is_superuser or user.groups.filter(name="finance").exists()


def _auth_screen() -> None:
    left, right = st.columns([1.15, 0.85], gap="large")
    with left:
        st.markdown(
            '<div class="eyebrow">BRIER / EXPENSE INTELLIGENCE</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<h1 class="hero">Receipts in. <i>Risk exposed.</i></h1>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="lede">A disciplined claims desk: OCR extraction, calibrated confidence, '
            "duplicate screening, and an audit trail that explains every decision.</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="proof">Field-level confidence</div>'
            '<div class="proof">Explainable duplicate signals</div>'
            '<div class="proof">Guarded finance approval</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<div class="panel-kicker">SECURE WORKSPACE</div>', unsafe_allow_html=True
        )
        tabs = ["Sign in"]
        allow_signup = os.getenv("BRIER_ALLOW_SIGNUP", "0") == "1"
        if allow_signup:
            tabs.append("Create employee account")
        selected = st.tabs(tabs)
        with selected[0]:
            with st.form("login"):
                username = st.text_input("Username", autocomplete="username")
                password = st.text_input(
                    "Password", type="password", autocomplete="current-password"
                )
                submitted = st.form_submit_button("Enter workspace", width="stretch")
            if submitted:
                user = authenticate(username=username.strip(), password=password)
                if user is None or not user.is_active:
                    st.error("Username or password is incorrect.")
                else:
                    st.session_state.user_id = user.pk
                    st.session_state.page = (
                        "dashboard" if _is_finance(user) else "claims"
                    )
                    st.rerun()
        if allow_signup:
            with selected[1]:
                _signup()
        else:
            st.caption("Employee registration is invite-only on this deployment.")


def _signup() -> None:
    with st.form("signup"):
        full_name = st.text_input("Full name")
        username = st.text_input("Username", autocomplete="username")
        department = st.text_input("Department")
        password1 = st.text_input(
            "Password", type="password", autocomplete="new-password"
        )
        password2 = st.text_input(
            "Confirm password", type="password", autocomplete="new-password"
        )
        submitted = st.form_submit_button("Create account", width="stretch")
    if not submitted:
        return
    form = SignUpForm(
        {
            "full_name": full_name,
            "username": username,
            "department": department,
            "password1": password1,
            "password2": password2,
        }
    )
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                st.error(str(error))
        return
    user, _employee = form.save()
    st.session_state.user_id = user.pk
    st.session_state.page = "upload"
    st.rerun()


def _sidebar(user: User, finance: bool) -> None:
    with st.sidebar:
        st.markdown(
            '<div class="wordmark">Brier</div>', unsafe_allow_html=True
        )
        st.caption("FINANCE CONTROL SYSTEM")
        st.markdown("---")
        options = (
            ["dashboard", "review", "claims", "upload"]
            if finance
            else ["claims", "upload"]
        )
        current = st.session_state.get("page")
        for key in options:
            if st.button(
                PAGES[key],
                key=f"nav-{key}",
                width="stretch",
                type="primary" if current == key else "secondary",
            ):
                _go(key)
        st.markdown("---")
        name = user.get_full_name() or user.username
        st.markdown(f"**{escape(name)}**")
        st.caption("Finance reviewer" if finance else "Employee")
        if st.button("Sign out", width="stretch"):
            st.session_state.clear()
            st.rerun()


def _go(page: str, claim_id: str | None = None) -> None:
    st.session_state.page = page
    if claim_id is not None:
        st.session_state.claim_id = claim_id
    st.rerun()


# --------------------------------------------------------------------- upload
def _upload(user: User) -> None:
    _page_title(
        "SUBMIT / 01",
        "New invoice",
        "Upload a scan or paste the source text. Brier extracts before it files.",
    )
    status = ocr_status()
    if status["available"]:
        st.success("OCR online. Image and scanned-PDF uploads are enabled.")
    else:
        st.warning(
            "OCR is unavailable. Text-layer PDFs and pasted receipt text still work."
        )

    with st.form("invoice-upload", clear_on_submit=False):
        left, right = st.columns([1, 1], gap="large")
        with left:
            upload = st.file_uploader(
                "Invoice or receipt",
                type=_streamlit_extensions(),
                help="PDF, JPG, PNG, WEBP, GIF, BMP or TIFF. 15 MB max.",
            )
            description = st.text_input(
                "Business purpose",
                placeholder="Client lunch, Mumbai trip…",
                max_chars=300,
            )
            claimed_amount = st.number_input(
                "Fallback amount (INR)",
                min_value=0.0,
                max_value=9_999_999_999.99,
                step=0.01,
                help="Used only if no total can be extracted.",
            )
        with right:
            receipt_text = st.text_area(
                "Receipt text (optional)",
                height=260,
                placeholder="Paste text when the scan is unavailable or OCR cannot read it.",
            )
        submitted = st.form_submit_button(
            "Extract and submit", type="primary", width="stretch"
        )

    if not submitted:
        _recent_submission()
        return
    if upload is None and not receipt_text.strip():
        st.error("Attach a receipt or paste its text.")
        return

    with st.status("Reading evidence…", expanded=True) as progress:
        receipt = None
        claim = None
        try:
            blob = filename = content_type = exact_text = None
            if upload is not None:
                progress.write("Validating file signature and structure")
                validate_receipt_upload(upload)
                blob, filename, content_type, exact_text = prepare_upload_blob(upload)
            employee = _employee_for(user)
            progress.write("Extracting invoice fields and confidence")
            receipt = create_with_unique_id(
                Receipt,
                "receipt_id",
                "U",
                text=receipt_text.strip() or (exact_text or ""),
                image_filename=filename or "",
                image_content_type=content_type or "",
            )
            if blob:
                services.store_receipt_blob(receipt, blob, filename or "receipt.png", content_type or "")
            result = services.extract_into_receipt(
                receipt, line_model=services.load_line_model()
            )
            foreign = services.foreign_currency(result)
            if foreign:
                services.delete_receipt_artifact(receipt)
                receipt.delete()
                progress.update(label="Invoice refused", state="error")
                st.error(
                    f"Brier accepts INR receipts only. This one looks like {foreign}."
                )
                return
            progress.write("Screening against prior claims")
            fallback = Decimal(str(claimed_amount)) if claimed_amount else None
            claim = create_with_unique_id(
                Claim,
                "claim_id",
                "U",
                employee=employee,
                receipt=receipt,
                claimed_amount=receipt.total or fallback,
                description=description.strip(),
                category=receipt.vendor.category if receipt.vendor else "",
                status=Claim.DRAFT,
            )
            pairs = services.submit_claim(claim, user)
            services.recompute_groups()
            progress.update(label="Claim submitted", state="complete", expanded=False)
        except ValidationError as exc:
            progress.update(label="File refused", state="error")
            st.error(" ".join(exc.messages))
            return
        except (RuntimeError, InvalidOperation) as exc:
            if receipt is not None and not receipt.claims.exists():
                services.delete_receipt_artifact(receipt)
                receipt.delete()
            progress.update(label="Could not submit", state="error")
            st.error(str(exc))
            return
        except Exception:
            logger.exception("Unexpected invoice submission failure")
            if receipt is not None and not receipt.claims.exists():
                services.delete_receipt_artifact(receipt)
                receipt.delete()
            progress.update(label="Could not submit", state="error")
            if claim is not None and claim.pk:
                st.session_state.last_submitted_claim = claim.claim_id
                st.session_state.last_submitted_pairs = 0
                st.error(
                    f"{claim.claim_id} was saved, but processing did not finish. "
                    "Open it before retrying."
                )
            else:
                st.error("Submission failed before a claim was filed. Please retry.")
            return

    st.session_state.last_submitted_claim = claim.claim_id
    st.session_state.last_submitted_pairs = len(pairs)
    st.rerun()


def _recent_submission() -> None:
    claim_id = st.session_state.get("last_submitted_claim")
    if not claim_id:
        return
    pairs = int(st.session_state.get("last_submitted_pairs", 0))
    if pairs:
        st.warning(
            f"Submitted {claim_id}; {pairs} possible duplicate(s) require finance review."
        )
    else:
        st.success(f"Submitted {claim_id}; no duplicate match was found.")
    if st.button("Open claim", type="primary"):
        _go("claim", claim_id)


def _employee_for(user: User) -> Employee:
    employee = Employee.objects.filter(user=user).first()
    if employee:
        return employee
    return create_with_unique_id(
        Employee,
        "employee_code",
        "E",
        width=5,
        user=user,
        full_name=user.get_full_name(),
    )


def _streamlit_extensions() -> list[str]:
    return [
        part.strip().lstrip(".") for part in ACCEPT.split(",") if part.startswith(".")
    ]


# --------------------------------------------------------------------- ledgers
def _claims(user: User, finance: bool) -> None:
    _page_title(
        "LEDGER / 02",
        "Claims ledger",
        "A searchable record of submissions and outcomes.",
    )
    qs = Claim.objects.select_related("receipt", "employee", "receipt__vendor").defer(
        "receipt__image_blob"
    )
    if not finance:
        qs = qs.filter(employee__user=user)
    statuses = ["All"] + [label for _key, label in Claim.STATUSES]
    selected_status = st.selectbox("Status", statuses, label_visibility="collapsed")
    if selected_status != "All":
        key = next(key for key, label in Claim.STATUSES if label == selected_status)
        qs = qs.filter(status=key)
    claims = list(qs[:250])
    if not claims:
        st.info("No claims match this view.")
        return
    rows = [_claim_row(c) for c in claims]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    chosen = st.selectbox(
        "Open a claim",
        claims,
        format_func=lambda c: (
            f"{c.claim_id} / {c.employee.full_name or c.employee.employee_code} / {c.amount_display}"
        ),
    )
    if st.button("Inspect claim", type="primary"):
        _go("claim", chosen.claim_id)


def _claim_row(claim: Claim) -> dict:
    return {
        "Claim": claim.claim_id,
        "Employee": claim.employee.full_name or claim.employee.employee_code,
        "Vendor": claim.receipt.vendor.name
        if claim.receipt.vendor
        else claim.receipt.vendor_raw or "-",
        "Amount": claim.amount_display,
        "Status": claim.get_status_display(),
        "Confidence": f"{claim.receipt.confidence_pct}%",
        "Submitted": claim.submitted_at.strftime("%d %b %Y")
        if claim.submitted_at
        else "-",
    }


def _review_queue(user: User) -> None:
    _page_title(
        "REVIEW / 03",
        "Risk queue",
        "Low confidence and duplicate evidence, ordered for a human decision.",
    )
    filter_col, sort_col = st.columns(2)
    with filter_col:
        only = st.selectbox("Show", ["All open", "Flagged", "Low confidence"])
    with sort_col:
        sort = st.selectbox(
            "Order", ["Risk", "Largest amount", "Lowest confidence", "Newest"]
        )
    qs = (
        Claim.objects.filter(status__in=Claim.OPEN_STATUSES)
        .select_related("receipt", "employee", "receipt__vendor")
        .defer("receipt__image_blob")
        .annotate(
            n_open=Count("flags_as_a", filter=Q(flags_as_a__status=DuplicateFlag.OPEN)),
            risk_score=Max(
                Case(
                    When(
                        flags_as_a__status=DuplicateFlag.OPEN,
                        flags_as_a__band="EXACT",
                        then=4,
                    ),
                    When(
                        flags_as_a__status=DuplicateFlag.OPEN,
                        flags_as_a__band="HIGH",
                        then=3,
                    ),
                    When(
                        flags_as_a__status=DuplicateFlag.OPEN,
                        flags_as_a__band="MEDIUM",
                        then=2,
                    ),
                    When(
                        flags_as_a__status=DuplicateFlag.OPEN,
                        flags_as_a__band="LOW",
                        then=1,
                    ),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )
    )
    if only == "Flagged":
        qs = qs.filter(n_open__gt=0)
    elif only == "Low confidence":
        qs = qs.filter(receipt__needs_review=True)
    if sort == "Largest amount":
        qs = qs.order_by("-claimed_amount")
    elif sort == "Lowest confidence":
        qs = qs.order_by("receipt__doc_confidence")
    elif sort == "Newest":
        qs = qs.order_by("-submitted_at")
    else:
        qs = qs.order_by("-risk_score", "-claimed_amount", "-submitted_at")
    claims = list(qs[:250])
    if not claims:
        st.success("Queue clear. No open claims match this view.")
        return
    st.dataframe(
        pd.DataFrame([{**_claim_row(c), "Open flags": c.n_open} for c in claims]),
        width="stretch",
        hide_index=True,
    )
    chosen = st.selectbox(
        "Review claim",
        claims,
        format_func=lambda c: (
            f"{c.claim_id} / {c.amount_display} / {c.n_open} open flags"
        ),
    )
    if st.button("Open review", type="primary"):
        _go("claim", chosen.claim_id)


# --------------------------------------------------------------------- detail
def _claim_detail(user: User, finance: bool) -> None:
    claim_id = st.session_state.get("claim_id")
    qs = Claim.objects.select_related(
        "receipt", "employee", "receipt__vendor", "reviewer"
    )
    if not finance:
        qs = qs.filter(employee__user=user)
    claim = qs.filter(claim_id=claim_id).first()
    if claim is None:
        st.error("Claim not found or not available to this account.")
        if st.button("Back to ledger"):
            _go("claims")
        return
    receipt = claim.receipt
    _page_title(
        f"CLAIM / {claim.claim_id}",
        receipt.vendor.name
        if receipt.vendor
        else receipt.vendor_raw or "Unidentified vendor",
        f"{claim.employee.full_name or claim.employee.employee_code} / {claim.get_status_display()}",
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Claimed", claim.amount_display)
    m2.metric("Confidence", f"{receipt.confidence_pct}%")
    m3.metric("Arithmetic", "Balanced" if receipt.arithmetic_ok else "Review")
    open_flags = list(
        claim.all_flags.filter(status=DuplicateFlag.OPEN).select_related(
            "claim", "matched_claim"
        )
    )
    m4.metric("Open flags", len(open_flags))

    evidence, data = st.columns([0.9, 1.1], gap="large")
    with evidence:
        st.markdown("### Source evidence")
        image = _receipt_bytes(receipt)
        if image:
            st.image(
                image,
                caption=receipt.image_filename or receipt.receipt_id,
                width="stretch",
            )
        elif receipt.text:
            st.code(receipt.text[:5000], language=None)
        else:
            st.info("No preview is stored for this seeded receipt.")
    with data:
        st.markdown("### Extracted fields")
        fields = list(receipt.extracted.all())
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Field": FIELD_LABELS.get(f.name, f.name),
                        "Value": f.display_value or "-",
                        "Confidence": f"{f.confidence_pct}%",
                        "Review": "Yes" if f.needs_review else "No",
                    }
                    for f in fields
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        for field in fields:
            with st.expander(
                f"{FIELD_LABELS.get(field.name, field.name)} / {field.confidence_pct}%"
            ):
                if field.warnings:
                    st.caption(" / ".join(field.warnings))
                if field.components:
                    st.json(field.components, expanded=False)
                if finance:
                    _field_correction(field, user)

    if finance:
        _finance_actions(claim, user)
        _duplicate_actions(claim, user)
    _audit_trail(claim)


def _receipt_bytes(receipt: Receipt) -> bytes | None:
    if receipt.object_storage_key:
        try:
            return object_storage.get_bytes(
                receipt.object_storage_key,
                bucket=receipt.object_storage_bucket or None,
            )
        except Exception:  # noqa: BLE001 - preview failures should not break review
            logger.exception("Could not download receipt object %s", receipt.object_storage_key)
            return None
    if receipt.image_blob:
        return bytes(receipt.image_blob)
    if receipt.image:
        try:
            receipt.image.open("rb")
            return receipt.image.read()
        except (OSError, ValueError):
            return None
        finally:
            receipt.image.close()
    if receipt.image_path:
        path = Path(receipt.image_path)
        if not path.is_absolute():
            from django.conf import settings

            path = settings.DATA_DIR / path
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None


def _field_correction(field: ExtractedField, user: User) -> None:
    with st.form(f"correct-{field.pk}"):
        value = st.text_input(
            "Corrected value", value=field.display_value, max_chars=200
        )
        submitted = st.form_submit_button("Save correction")
    if submitted:
        if not value.strip():
            st.error("Corrected value cannot be blank.")
            return
        try:
            services.correct_extracted_field(field, value, user)
        except services.TransitionError as exc:
            st.error(str(exc))
            return
        st.success("Correction saved.")
        st.rerun()


def _finance_actions(claim: Claim, user: User) -> None:
    st.markdown("### Finance decision")
    blocking = list(claim.blocking_flags())
    if blocking:
        st.error(
            f"Approval blocked by {len(blocking)} unresolved high-risk duplicate flag(s)."
        )
    with st.form(f"decision-{claim.pk}"):
        action = st.selectbox(
            "Action", ["Start review", "Approve", "Request info", "Reject"]
        )
        note = st.text_input("Decision note", max_chars=300)
        submitted = st.form_submit_button("Record decision", type="primary")
    if not submitted:
        return
    try:
        if action == "Start review":
            services.start_review(claim, user)
        elif action == "Approve":
            services.approve(claim, user, note)
        elif action == "Request info":
            services.request_info(claim, user, note)
        else:
            services.reject(claim, user, note)
    except services.TransitionError as exc:
        st.error(str(exc))
        return
    st.success("Decision recorded.")
    st.rerun()


def _duplicate_actions(claim: Claim, user: User) -> None:
    flags = list(claim.all_flags.select_related("claim", "matched_claim"))
    if not flags:
        return
    st.markdown("### Duplicate evidence")
    for flag in flags:
        other = flag.matched_claim if flag.claim_id == claim.id else flag.claim
        with st.expander(
            f"{flag.band} / {flag.score_pct}% / versus {other.claim_id} / {flag.get_status_display()}"
        ):
            if flag.reasons:
                for reason in flag.reasons:
                    st.write(f"• {reason}")
            if flag.signals:
                st.bar_chart(pd.Series(flag.signals, name="strength"), horizontal=True)
            if flag.status == DuplicateFlag.OPEN and flag.claim_id == claim.id:
                with st.form(f"flag-{flag.pk}"):
                    verdict = st.radio(
                        "Verdict",
                        ["Dismiss match", "Confirm duplicate"],
                        horizontal=True,
                    )
                    note = st.text_input("Resolution note", max_chars=300)
                    submitted = st.form_submit_button("Resolve flag")
                if submitted:
                    services.resolve_flag(
                        flag, user, confirmed=verdict == "Confirm duplicate", note=note
                    )
                    services.recompute_groups()
                    st.success("Flag resolved.")
                    st.rerun()


def _audit_trail(claim: Claim) -> None:
    st.markdown("### Audit trail")
    events = list(AuditEvent.objects.filter(claim=claim).select_related("actor"))
    if not events:
        st.caption("No events recorded.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Time": event.at.strftime("%d %b %Y / %H:%M"),
                    "Action": event.action.replace("_", " ").title(),
                    "From": event.from_status or "-",
                    "To": event.to_status or "-",
                    "Actor": event.actor.username if event.actor else "System",
                    "Note": event.note or "-",
                }
                for event in events
            ]
        ),
        width="stretch",
        hide_index=True,
    )


# --------------------------------------------------------------------- dashboard
def _dashboard() -> None:
    claims = Claim.objects.all()
    receipts = Receipt.objects.all()
    open_flags = DuplicateFlag.objects.filter(status=DuplicateFlag.OPEN)
    confirmed = DuplicateFlag.objects.filter(status=DuplicateFlag.CONFIRMED)
    total_claims = claims.count()
    total_receipts = receipts.count()
    total_for_rates = max(total_receipts, 1)
    open_claims = claims.filter(status__in=Claim.OPEN_STATUSES).count()
    mean_confidence = sum(receipts.values_list("doc_confidence", flat=True)) / total_for_rates
    mean_pct = round(mean_confidence * 100)
    manual_pct = round(receipts.filter(needs_review=True).count() * 100 / total_for_rates, 1)
    reconcile_pct = round(receipts.filter(arithmetic_ok=True).count() * 100 / total_for_rates, 1)

    prevented = _dashboard_money(confirmed)
    at_risk = _dashboard_money(open_flags.filter(band__in=["EXACT", "HIGH"]))
    prevented_html = "".join(f"<span>{escape(value)}</span>" for value in prevented)
    at_risk_html = "".join(f"<span>{escape(value)}</span>" for value in at_risk)

    by_status = dict(claims.values_list("status").annotate(count=Count("id")))
    status_rows = []
    for key, label in Claim.STATUSES:
        count = by_status.get(key, 0)
        pct = round(count * 100 / (total_claims or 1))
        status_rows.append(
            f'<div class="fin-chart-row status-{key.lower()}">'
            f'<div><span>{escape(label)}</span><strong>{count:,}</strong></div>'
            f'<i style="--value:{pct}%"></i></div>'
        )

    raw_bands = list(
        DuplicateFlag.objects.values("band").annotate(count=Count("id")).order_by("-count")
    )
    max_band = max((row["count"] for row in raw_bands), default=1)
    band_rows = []
    for row in raw_bands:
        pct = round(row["count"] * 100 / max_band)
        band = escape(row["band"].title())
        band_rows.append(
            f'<div class="fin-band band-{row["band"].lower()}">'
            f'<div><span>{band}</span><strong>{row["count"]:,}</strong></div>'
            f'<i style="--value:{pct}%"></i></div>'
        )
    if not band_rows:
        band_rows.append('<div class="fin-empty">No duplicate signals yet.</div>')

    st.markdown(
        f"""
        <div class="fin-dashboard">
          <header class="fin-head fin-enter" style="--order:0">
            <div><h1>Financial control</h1><p>Claims, duplicate exposure and extraction quality in one operational view.</p></div>
            <time datetime="{timezone.localdate().isoformat()}">{timezone.localdate():%d %b %Y}</time>
          </header>
          <section class="fin-kpis">
            <article class="fin-card fin-exposure fin-enter" style="--order:1">
              <div class="fin-card-top"><div><p class="fin-label">Prevented duplicate payout</p><div class="fin-hero-value">{prevented_html}</div></div><span class="fin-icon">✓</span></div>
              <footer><span><strong>{confirmed.count():,}</strong> confirmed duplicates</span><span>Approval guard active</span></footer>
            </article>
            <article class="fin-card fin-metric fin-risk fin-enter" style="--order:2">
              <p class="fin-label">Awaiting decision</p><div class="fin-metric-value">{at_risk_html}</div><footer><span>{open_flags.count():,} open flags</span><span>Review required</span></footer>
            </article>
            <article class="fin-card fin-metric fin-enter" style="--order:3">
              <p class="fin-label">Open claims</p><div class="fin-metric-value"><span>{open_claims:,}</span></div><footer><span>Need finance action</span><span>{total_claims:,} total</span></footer>
            </article>
            <article class="fin-card fin-metric fin-docs fin-enter" style="--order:4">
              <p class="fin-label">Documents processed</p><div class="fin-metric-value"><span>{total_receipts:,}</span></div><footer><span>Mean confidence</span><strong>{mean_confidence:.3f}</strong></footer>
            </article>
          </section>
          <section class="fin-analytics">
            <article class="fin-card fin-flow fin-enter" style="--order:5">
              <header><div><h2>Claims flow</h2><p>Distribution across every decision state.</p></div></header>
              <div class="fin-chart">{"".join(status_rows)}</div>
            </article>
            <article class="fin-card fin-health fin-enter" style="--order:6">
              <header><div><h2>Extraction health</h2><p>Confidence and arithmetic checks.</p></div></header>
              <div class="fin-health-body"><div class="fin-ring" style="--score:{mean_pct}%"><div><strong>{mean_pct}%</strong><span>confidence</span></div></div>
              <div class="fin-health-stats"><div><span>Amounts reconcile</span><strong>{reconcile_pct}%</strong></div><div><span>Manual review</span><strong>{manual_pct}%</strong></div><div><span>Receipts checked</span><strong>{total_receipts:,}</strong></div></div></div>
            </article>
            <article class="fin-card fin-signals fin-enter" style="--order:7">
              <header><div><h2>Duplicate signals</h2><p>Flags grouped by detection strength.</p></div></header>
              <div class="fin-bands">{"".join(band_rows)}</div>
            </article>
          </section>
        </div>
        """,
        unsafe_allow_html=True,
    )

    recent = list(
        Claim.objects.select_related("receipt", "receipt__vendor", "employee")
        .defer("receipt__image_blob")
        .order_by("-submitted_at")[:8]
    )
    st.markdown(
        '<div class="fin-section-head fin-enter" style="--order:8"><div><h2>Recent claims</h2>'
        '<p>Latest submissions across the organization.</p></div></div>',
        unsafe_allow_html=True,
    )
    if recent:
        st.dataframe(
            pd.DataFrame([_claim_row(claim) for claim in recent]),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No claims have been submitted.")


def _dashboard_money(flags) -> list[str]:
    rows = (
        flags.values("claim__receipt__currency")
        .annotate(total=Sum("claim__claimed_amount"))
        .order_by("-total")
    )
    values = [
        format_amount(row["total"] or 0, row["claim__receipt__currency"] or "INR")
        for row in rows
    ]
    return values or ["₹0.00"]


# --------------------------------------------------------------------- design
def _page_title(_kicker: str, title: str, subtitle: str) -> None:
    st.markdown(f'<h1 class="page-title">{escape(title)}</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="page-subtitle">{escape(subtitle)}</p>', unsafe_allow_html=True
    )


def _styles() -> None:
    st.markdown(
        """
        <style>
        :root {
          --fin-page:#090b0e; --fin-surface:#0f1317; --fin-surface-2:#151a20;
          --fin-line:#252c33; --fin-line-soft:#1b2228; --fin-ink:#eef1ea;
          --fin-muted:#9aa39d; --fin-accent:#a8d96a; --fin-accent-ink:#11170d;
          --fin-danger:#ff818a; --fin-warn:#e4b95f; --fin-cool:#84a7d8;
          --fin-radius:14px; --fin-control:8px; --fin-ease:cubic-bezier(.16,1,.3,1);
          --fin-mono:"SFMono-Regular",Consolas,"Liberation Mono",monospace;
          --fin-sans:"Avenir Next","Segoe UI",system-ui,-apple-system,sans-serif;
        }
        html,body,[class*="css"] { color:var(--fin-ink); font-family:var(--fin-sans); }
        .stApp { background:var(--fin-page); }
        [data-testid="stAppViewContainer"]>.main { background:var(--fin-page); }
        [data-testid="stMainBlockContainer"] { max-width:1480px; padding-top:2rem; padding-bottom:5rem; }
        h1,h2,h3 { font-family:var(--fin-sans) !important; letter-spacing:-.035em; }
        .hero { max-width:820px; margin:.4rem 0 1.5rem; font-size:clamp(3.25rem,7vw,6.4rem); font-weight:560; line-height:.92; }
        .hero i { color:var(--fin-accent); font-style:normal; font-weight:560; }
        .lede { max-width:650px; color:var(--fin-muted); font-size:1.08rem; line-height:1.6; }
        .eyebrow,.panel-kicker { color:var(--fin-accent); font:600 .7rem/1.2 var(--fin-mono); letter-spacing:.12em; }
        .page-title { margin:.3rem 0 .5rem; font-size:clamp(2.3rem,5vw,4.2rem); font-weight:560; line-height:1; }
        .page-subtitle { max-width:680px; margin-bottom:2rem; color:var(--fin-muted); font-size:1rem; }
        .proof { max-width:650px; padding:.9rem 0; border-top:1px solid var(--fin-line); color:var(--fin-ink); font:500 .78rem var(--fin-mono); }
        .proof span { display:none; }
        .wordmark { color:var(--fin-ink); font-size:1.55rem; font-weight:650; letter-spacing:-.045em; }
        [data-testid="stSidebar"] { border-right:1px solid var(--fin-line); background:#0c0f13; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color:var(--fin-muted); }
        [data-testid="stSidebar"] hr { border-color:var(--fin-line); }
        [data-testid="stMetric"] { padding:1rem; border:1px solid var(--fin-line); border-radius:var(--fin-radius); background:var(--fin-surface); }
        [data-testid="stMetricValue"] { font-family:var(--fin-mono); letter-spacing:-.04em; }
        .stButton>button,.stFormSubmitButton>button {
          min-height:2.65rem; border-radius:var(--fin-control); border-color:var(--fin-line);
          font:600 .78rem var(--fin-sans); transition:transform .2s var(--fin-ease),border-color .2s,background-color .2s;
        }
        .stButton>button:hover,.stFormSubmitButton>button:hover { transform:translateY(-1px); border-color:var(--fin-accent); }
        .stButton>button:active,.stFormSubmitButton>button:active { transform:translateY(1px) scale(.99); }
        .stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"] { color:var(--fin-accent-ink); background:var(--fin-accent); border-color:var(--fin-accent); }
        [data-testid="stFileUploaderDropzone"],[data-testid="stDataFrame"],[data-testid="stExpander"] { border-radius:var(--fin-radius); border-color:var(--fin-line); background:var(--fin-surface); }
        input,textarea,[data-baseweb="select"]>div { border-radius:var(--fin-control) !important; border-color:#626d76 !important; }
        [data-baseweb="tab-list"] { gap:1.25rem; border-bottom:1px solid var(--fin-line); }
        [data-baseweb="tab"] { font-family:var(--fin-sans); }
        [data-testid="stAlert"] { border-radius:var(--fin-control); }

        .fin-dashboard { margin-top:.2rem; }
        .fin-head { display:flex; align-items:flex-end; justify-content:space-between; gap:1.5rem; margin-bottom:1.25rem; }
        .fin-head h1 { margin:0; color:var(--fin-ink); font-size:clamp(2rem,4vw,3rem); font-weight:560; }
        .fin-head p,.fin-section-head p { margin:.4rem 0 0; color:var(--fin-muted); font-size:.88rem; }
        .fin-head time { min-width:max-content; padding:.55rem .7rem; border:1px solid var(--fin-line); border-radius:var(--fin-control); color:var(--fin-muted); background:var(--fin-surface); font:500 .72rem var(--fin-mono); }
        .fin-kpis { display:grid; grid-template-columns:repeat(12,minmax(0,1fr)); grid-template-rows:repeat(2,minmax(140px,auto)); gap:.75rem; margin-bottom:.75rem; }
        .fin-card { position:relative; min-width:0; overflow:hidden; border:1px solid var(--fin-line); border-radius:var(--fin-radius); background:var(--fin-surface); transition:transform .3s var(--fin-ease),border-color .3s,background-color .3s; }
        .fin-card:before { content:""; position:absolute; inset:0; opacity:0; pointer-events:none; background:radial-gradient(circle at 80% 5%,rgba(168,217,106,.09),transparent 38%); transition:opacity .3s; }
        .fin-card:hover { transform:translateY(-2px); border-color:#4b5b3d; }
        .fin-card:hover:before { opacity:1; }
        .fin-card>* { position:relative; }
        .fin-exposure { grid-column:1/span 6; grid-row:1/span 2; display:flex; flex-direction:column; justify-content:space-between; padding:clamp(1.3rem,2.4vw,2rem); color:var(--fin-accent-ink); border-color:transparent; background:var(--fin-accent); }
        .fin-exposure:before { background:linear-gradient(115deg,transparent 45%,rgba(255,255,255,.2),transparent 55%); opacity:.2; transform:translateX(-120%); }
        .fin-card-top,.fin-exposure footer,.fin-metric footer { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; }
        .fin-label { margin:0; color:var(--fin-muted); font-size:.76rem; font-weight:550; }
        .fin-exposure .fin-label { color:rgba(17,23,13,.68); }
        .fin-hero-value,.fin-metric-value { display:grid; gap:.1rem; font-family:var(--fin-mono); font-variant-numeric:tabular-nums; letter-spacing:-.055em; line-height:1; }
        .fin-hero-value { margin-top:1.25rem; font-size:clamp(2.4rem,5vw,4.35rem); font-weight:520; }
        .fin-icon { width:46px;height:46px;display:grid;place-items:center;border-radius:12px;background:rgba(17,23,13,.1);font-size:1.25rem;font-weight:700; }
        .fin-exposure footer { align-items:center; margin-top:2rem; padding-top:1rem; border-top:1px solid rgba(17,23,13,.18); font-size:.75rem; }
        .fin-exposure footer strong { font:600 .9rem var(--fin-mono); }
        .fin-metric { grid-column:span 3; display:flex; flex-direction:column; justify-content:space-between; padding:1.15rem 1.2rem; }
        .fin-risk { grid-column:7/span 3; }
        .fin-metric:nth-of-type(3) { grid-column:10/span 3; }
        .fin-docs { grid-column:7/span 6; }
        .fin-metric-value { margin:1.15rem 0; color:var(--fin-ink); font-size:clamp(1.8rem,3vw,2.6rem); font-weight:520; }
        .fin-risk .fin-metric-value { color:var(--fin-danger); font-size:clamp(1.55rem,2.6vw,2.25rem); }
        .fin-metric footer { color:var(--fin-muted); font-size:.72rem; }
        .fin-metric footer strong { color:var(--fin-accent); font:520 .75rem var(--fin-mono); }

        .fin-analytics { display:grid; grid-template-columns:5fr 3fr 4fr; gap:.75rem; margin-bottom:1.1rem; }
        .fin-card>header { padding:1.2rem 1.25rem 0; }
        .fin-card>header h2,.fin-section-head h2 { margin:0; color:var(--fin-ink); font-size:.98rem; font-weight:600; letter-spacing:-.015em; }
        .fin-card>header p { margin:.28rem 0 0; color:var(--fin-muted); font-size:.73rem; }
        .fin-chart,.fin-bands { display:grid; gap:.72rem; padding:1.2rem 1.25rem 1.3rem; }
        .fin-chart-row>div,.fin-band>div,.fin-health-stats>div { display:flex; align-items:center; justify-content:space-between; gap:1rem; margin-bottom:.34rem; color:var(--fin-muted); font-size:.72rem; }
        .fin-chart-row strong,.fin-band strong,.fin-health-stats strong { color:var(--fin-ink); font:520 .75rem var(--fin-mono); }
        .fin-chart-row>i,.fin-band>i { display:block; width:var(--value); height:4px; border-radius:2px; background:var(--fin-cool); transform-origin:left; }
        .status-submitted>i,.status-under_review>i,.band-medium>i { background:var(--fin-warn); }
        .status-needs_info>i,.status-rejected>i,.band-exact>i,.band-high>i { background:var(--fin-danger); }
        .status-approved>i { background:var(--fin-accent); }
        .band-low>i { background:var(--fin-muted); }
        .fin-band>i { height:3px; }
        .fin-health-body { display:grid; align-content:space-between; min-height:315px; padding:1.2rem 1.25rem 1.3rem; }
        .fin-ring { width:min(144px,70%); aspect-ratio:1; display:grid; place-items:center; margin:.3rem auto 1.2rem; border-radius:50%; background:conic-gradient(var(--fin-accent) var(--score),#242a2f 0); }
        .fin-ring>div { width:calc(100% - 12px); aspect-ratio:1; display:grid; place-content:center; text-align:center; border-radius:50%; background:var(--fin-surface); }
        .fin-ring strong { color:var(--fin-ink); font:520 1.65rem var(--fin-mono); letter-spacing:-.05em; }
        .fin-ring span { color:var(--fin-muted); font-size:.66rem; }
        .fin-health-stats { display:grid; gap:.55rem; }
        .fin-health-stats>div { margin:0; }
        .fin-section-head { margin:1.4rem 0 .85rem; }
        .fin-empty { padding:2rem .5rem; color:var(--fin-muted); text-align:center; font-size:.75rem; }
        [data-testid="stDataFrame"] { overflow:hidden; }

        @media (prefers-reduced-motion:no-preference) {
          .fin-enter { animation:fin-enter .72s var(--fin-ease) both; animation-delay:calc(var(--order)*55ms); }
          .fin-chart-row>i,.fin-band>i { animation:fin-bar .9s var(--fin-ease) both; animation-delay:.3s; }
          .fin-exposure:before { animation:fin-sheen 1.1s var(--fin-ease) .4s both; }
          @keyframes fin-enter { from{opacity:0;transform:translateY(18px) scale(.985)} to{opacity:1;transform:none} }
          @keyframes fin-bar { from{transform:scaleX(0)} to{transform:scaleX(1)} }
          @keyframes fin-sheen { from{transform:translateX(-120%)} to{transform:translateX(120%)} }
        }
        @media (max-width:1000px) {
          .fin-exposure { grid-column:1/span 7; }
          .fin-risk { grid-column:8/span 5; }
          .fin-metric:nth-of-type(3) { grid-column:8/span 2; }
          .fin-docs { grid-column:10/span 3; }
          .fin-analytics { grid-template-columns:1fr 1fr; }
          .fin-signals { grid-column:1/-1; }
          .fin-bands { grid-template-columns:repeat(4,minmax(0,1fr)); }
        }
        @media (max-width:700px) {
          [data-testid="stMainBlockContainer"] { padding-left:1rem; padding-right:1rem; padding-top:1.2rem; }
          .hero{font-size:3.5rem}.page-title{font-size:2.7rem}
          .fin-head { align-items:flex-start; }
          .fin-head time { display:none; }
          .fin-kpis { grid-template-columns:minmax(0,1fr); grid-template-rows:auto; }
          .fin-exposure,.fin-risk,.fin-metric:nth-of-type(3),.fin-docs { grid-column:1; grid-row:auto; }
          .fin-exposure { min-height:260px; }
          .fin-metric { min-height:145px; }
          .fin-analytics { grid-template-columns:minmax(0,1fr); }
          .fin-signals { grid-column:1; }
          .fin-bands { grid-template-columns:repeat(2,minmax(0,1fr)); }
        }
        @media (max-width:450px) { .fin-bands{grid-template-columns:1fr}.fin-exposure footer{align-items:flex-start;flex-direction:column} }
        @media (prefers-reduced-motion:reduce) { .fin-card:hover,.stButton>button:hover{transform:none} }
        </style>
        """,
        unsafe_allow_html=True,
    )
