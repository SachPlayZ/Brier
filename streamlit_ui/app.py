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
            '<h1 class="hero">Receipts in.<br><i>Risk exposed.</i></h1>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="lede">A disciplined claims desk: OCR extraction, calibrated confidence, '
            "duplicate screening, and an audit trail that explains every decision.</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="proof"><span>01</span> Field-level confidence</div>'
            '<div class="proof"><span>02</span> Explainable duplicate signals</div>'
            '<div class="proof"><span>03</span> Guarded finance approval</div>',
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
            '<div class="wordmark">BRIER<span>•</span></div>', unsafe_allow_html=True
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
        st.success("OCR online · image and scanned-PDF uploads are enabled", icon="✅")
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
                help="PDF, JPG, PNG, WEBP, GIF, BMP or TIFF · 15 MB max",
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
            f"{c.claim_id} · {c.employee.full_name or c.employee.employee_code} · {c.amount_display}"
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
        else claim.receipt.vendor_raw or "—",
        "Amount": claim.amount_display,
        "Status": claim.get_status_display(),
        "Confidence": f"{claim.receipt.confidence_pct}%",
        "Submitted": claim.submitted_at.strftime("%d %b %Y")
        if claim.submitted_at
        else "—",
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
            f"{c.claim_id} · {c.amount_display} · {c.n_open} open flags"
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
        f"{claim.employee.full_name or claim.employee.employee_code} · {claim.get_status_display()}",
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
                        "Value": f.display_value or "—",
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
                f"{FIELD_LABELS.get(field.name, field.name)} · {field.confidence_pct}%"
            ):
                if field.warnings:
                    st.caption(" · ".join(field.warnings))
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
            f"{flag.band} · {flag.score_pct}% · versus {other.claim_id} · {flag.get_status_display()}"
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
                    "Time": event.at.strftime("%d %b %Y · %H:%M"),
                    "Action": event.action.replace("_", " ").title(),
                    "From": event.from_status or "—",
                    "To": event.to_status or "—",
                    "Actor": event.actor.username if event.actor else "System",
                    "Note": event.note or "—",
                }
                for event in events
            ]
        ),
        width="stretch",
        hide_index=True,
    )


# --------------------------------------------------------------------- dashboard
def _dashboard() -> None:
    _page_title(
        "CONTROL / LIVE",
        "Finance control room",
        "The expense book, reduced to decisions that need attention.",
    )
    claims = Claim.objects.all()
    receipts = Receipt.objects.all()
    open_flags = DuplicateFlag.objects.filter(status=DuplicateFlag.OPEN)
    approved = claims.filter(status=Claim.APPROVED)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Claims", claims.count())
    m2.metric("Awaiting action", claims.filter(status__in=Claim.OPEN_STATUSES).count())
    m3.metric("Open duplicate flags", open_flags.count())
    m4.metric("Approved", approved.count())

    left, right = st.columns([1.1, 0.9], gap="large")
    with left:
        st.markdown("### Workflow volume")
        by_status = list(
            claims.values("status").annotate(count=Count("id")).order_by("status")
        )
        if by_status:
            chart = pd.DataFrame(by_status).set_index("status")
            st.bar_chart(chart, color="#0F766E")
        else:
            st.info("No claims yet.")
    with right:
        st.markdown("### Extraction health")
        total = max(receipts.count(), 1)
        st.metric(
            "Mean confidence",
            f"{sum(receipts.values_list('doc_confidence', flat=True)) / total:.1%}",
        )
        st.metric(
            "Needs manual review",
            f"{receipts.filter(needs_review=True).count() / total:.1%}",
        )
        st.metric(
            "Amounts reconcile",
            f"{receipts.filter(arithmetic_ok=True).count() / total:.1%}",
        )

    st.markdown("### Exposure awaiting resolution")
    exposure = (
        open_flags.filter(band__in=["EXACT", "HIGH"])
        .values("claim__receipt__currency")
        .annotate(total=Sum("claim__claimed_amount"))
    )
    if exposure:
        st.write(
            " · ".join(
                format_amount(
                    row["total"] or 0, row["claim__receipt__currency"] or "INR"
                )
                for row in exposure
            )
        )
    else:
        st.success("No high-risk exposure is currently open.")


# --------------------------------------------------------------------- design
def _page_title(kicker: str, title: str, subtitle: str) -> None:
    st.markdown(f'<div class="eyebrow">{escape(kicker)}</div>', unsafe_allow_html=True)
    st.markdown(f'<h1 class="page-title">{escape(title)}</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="page-subtitle">{escape(subtitle)}</p>', unsafe_allow_html=True
    )


def _styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;600&family=Newsreader:ital,opsz,wght@0,6..72,600;1,6..72,500&display=swap');
        :root { --ink:#17201f; --paper:#f4f0e8; --teal:#0f766e; --line:#cbc4b7; --acid:#d8f34a; }
        html, body, [class*="css"] { color:var(--ink); }
        .stApp { background:
          linear-gradient(rgba(23,32,31,.035) 1px, transparent 1px),
          linear-gradient(90deg, rgba(23,32,31,.025) 1px, transparent 1px), var(--paper);
          background-size:32px 32px; }
        h1,h2,h3 { font-family:"Newsreader", Georgia, serif !important; letter-spacing:-.025em; }
        .hero { font-size:clamp(4rem,8vw,7.4rem); line-height:.82; margin:.5rem 0 2rem; max-width:880px; }
        .hero i { color:var(--teal); font-weight:500; }
        .lede { max-width:690px; font-size:1.2rem; line-height:1.6; color:#4a5552; }
        .eyebrow,.panel-kicker { font:600 .72rem/1.2 "IBM Plex Mono",monospace; letter-spacing:.16em; color:var(--teal); }
        .page-title { font-size:clamp(3rem,6vw,5.4rem); line-height:.92; margin:.35rem 0 .7rem; }
        .page-subtitle { color:#5d6663; font-size:1.08rem; margin-bottom:2.4rem; }
        .proof { border-top:1px solid var(--line); padding:1rem 0; font:500 .8rem "IBM Plex Mono",monospace; max-width:680px; }
        .proof span { color:var(--teal); margin-right:1.2rem; }
        .wordmark { font:600 2rem "Newsreader",serif; letter-spacing:-.04em; }
        .wordmark span { color:var(--teal); }
        [data-testid="stSidebar"] { border-right:1px solid var(--line); background:#ebe6dc; }
        [data-testid="stMetric"] { border-top:3px solid var(--ink); padding:1rem 0; }
        [data-testid="stMetricValue"] { font-family:"Newsreader",serif; }
        .stButton>button, .stFormSubmitButton>button { border-radius:0; font:600 .76rem "IBM Plex Mono",monospace; letter-spacing:.04em; min-height:2.8rem; }
        .stButton>button[kind="primary"], .stFormSubmitButton>button[kind="primary"] { background:var(--teal); border-color:var(--teal); }
        [data-testid="stFileUploaderDropzone"], [data-testid="stDataFrame"], [data-testid="stExpander"] { border-radius:0; border-color:var(--line); }
        input, textarea { border-radius:0 !important; }
        [data-baseweb="tab-list"] { gap:1.5rem; border-bottom:1px solid var(--line); }
        [data-baseweb="tab"] { font-family:"IBM Plex Mono",monospace; }
        @media (max-width: 700px) { .hero{font-size:3.8rem}.page-title{font-size:3rem} }
        </style>
        """,
        unsafe_allow_html=True,
    )
