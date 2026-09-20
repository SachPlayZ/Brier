"""Forms for claim submission, review decisions and field corrections."""
from __future__ import annotations

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.models import User
from django.db import transaction

from expenses.models import Claim, Employee, Receipt
from expenses.uploads import ACCEPT, validate_receipt_upload


class ClaimSubmitForm(forms.Form):
    """A receipt arrives as a PDF or image, as pasted text, or both."""

    image = forms.FileField(required=False, validators=[validate_receipt_upload],
                            widget=forms.ClearableFileInput(
                                attrs={"class": "form-control", "accept": ACCEPT}))
    receipt_text = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"class": "form-control font-monospace", "rows": 12,
               "placeholder": "Paste the receipt text here if you have no image, "
                              "or if OCR is unavailable."}))
    description = forms.CharField(required=False, max_length=300, widget=forms.TextInput(
        attrs={"class": "form-control", "placeholder": "Client lunch, Mumbai trip, ..."}))
    claimed_amount = forms.DecimalField(required=False, max_digits=12, decimal_places=2,
                                        widget=forms.NumberInput(
                                            attrs={"class": "form-control", "step": "0.01"}))

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("image") and not (cleaned.get("receipt_text") or "").strip():
            raise forms.ValidationError("Attach a receipt image or paste the receipt text.")
        return cleaned

    def next_receipt_id(self) -> str:
        return _next_id(Receipt, "receipt_id", "U")

    def next_claim_id(self) -> str:
        return _next_id(Claim, "claim_id", "U")


class SignUpForm(forms.Form):
    """Self-serve registration. Always creates an *employee*: the finance reviewer role is a
    group membership only an admin can grant, so nothing here can escalate privileges."""

    full_name = forms.CharField(max_length=120, label="Full name",
                                widget=forms.TextInput(attrs={"autocomplete": "name", "autofocus": True}))
    username = forms.CharField(max_length=150, label="Username",
                               widget=forms.TextInput(attrs={"autocomplete": "username"}),
                               help_text="Letters, digits and @ . + - _ only.")
    department = forms.CharField(max_length=60, required=False, label="Department (optional)",
                                 widget=forms.TextInput(attrs={"autocomplete": "organization-title"}))
    password1 = forms.CharField(label="Password", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Confirm password", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def full_clean(self):
        super().full_clean()
        for name in self.errors:                      # mark bad inputs for styling and screen readers
            if name in self.fields:
                attrs = self.fields[name].widget.attrs
                attrs["class"] += " is-invalid"
                attrs["aria-invalid"] = "true"

    def clean_full_name(self):
        return " ".join(self.cleaned_data["full_name"].split())

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User._meta.get_field("username").run_validators(username)
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
        elif p1:
            candidate = User(username=cleaned.get("username", ""),
                             first_name=(cleaned.get("full_name") or "").split(" ")[0])
            try:
                password_validation.validate_password(p1, candidate)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self) -> tuple[User, Employee]:
        """Create the login and its Employee record together, or neither."""
        data = self.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(
                username=data["username"], password=data["password1"],
                first_name=data["full_name"].split(" ")[0])
            employee = create_with_unique_id(
                Employee, "employee_code", "E", width=3, user=user,
                full_name=data["full_name"], department=data["department"])
        return user, employee


class ReviewDecisionForm(forms.Form):
    ACTIONS = [("review", "Start review"), ("approve", "Approve"),
               ("reject", "Reject"), ("info", "Request info")]
    action = forms.ChoiceField(choices=ACTIONS)
    note = forms.CharField(required=False, max_length=300, widget=forms.TextInput(
        attrs={"class": "form-control form-control-sm", "placeholder": "Reason (optional)"}))


class FieldCorrectionForm(forms.Form):
    value = forms.CharField(max_length=200, widget=forms.TextInput(
        attrs={"class": "form-control form-control-sm"}))


def _next_id(model, field: str, prefix: str, *, offset: int = 0, width: int = 5) -> str:
    """Sequential id in the user-submitted namespace, distinct from seeded data.

    ``max + 1`` inherently races: two simultaneous submissions read the same
    maximum and pick the same id. Callers must create through
    :func:`create_with_unique_id`, which retries with a bumped ``offset``.
    """
    latest = (model.objects.filter(**{f"{field}__startswith": prefix})
              .order_by(f"-{field}").values_list(field, flat=True).first())
    number = int(latest[1:]) + 1 if latest and latest[1:].isdigit() else 1
    return f"{prefix}{number + offset:0{width}d}"


def create_with_unique_id(model, field: str, prefix: str, *, attempts: int = 8, width: int = 5,
                          **fields):
    """Create ``model`` with a generated id, retrying past concurrent collisions."""
    from django.db import IntegrityError, transaction

    last_error: Exception | None = None
    for offset in range(attempts):
        candidate = _next_id(model, field, prefix, offset=offset, width=width)
        try:
            with transaction.atomic():
                return model.objects.create(**{field: candidate}, **fields)
        except IntegrityError as exc:
            if field not in str(exc) and "UNIQUE" not in str(exc).upper():
                raise
            last_error = exc
    raise last_error if last_error else RuntimeError("could not allocate an id")
