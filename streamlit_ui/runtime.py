"""Boot Django safely inside Streamlit's rerun execution model."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

SECRET_NAMES = (
    "DATABASE_URL",
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_ALLOWED_HOSTS",
    "TESSERACT_CMD",
    "BRIER_ADMIN_USERNAME",
    "BRIER_ADMIN_PASSWORD",
    "BRIER_ADMIN_EMAIL",
    "BRIER_ALLOW_SIGNUP",
    "BRIER_LOCAL_DEMO",
)


def copy_secrets(secrets: Mapping | None) -> None:
    """Copy root Streamlit secrets into environment before Django loads."""
    if secrets is None:
        return
    for name in SECRET_NAMES:
        try:
            value = secrets[name]
        except (KeyError, TypeError, FileNotFoundError):
            continue
        if value is not None and name not in os.environ:
            os.environ[name] = str(value)


def boot(secrets: Mapping | None = None, *, migrate: bool = True) -> None:
    """Configure Django, migrate the database, and provision finance access."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    copy_secrets(secrets)
    _validate_environment()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("EXPENSES_WARM", "1")

    import django

    django.setup()
    if migrate:
        from django.core.management import call_command

        call_command("migrate", interactive=False, verbosity=0)
    provision_finance_user()


def _validate_environment() -> None:
    """Fail closed instead of deploying production against the tracked demo DB."""
    if os.getenv("BRIER_LOCAL_DEMO") == "1":
        return
    if os.getenv("DJANGO_DEBUG") != "0":
        raise RuntimeError(
            "Streamlit deployment requires DJANGO_DEBUG=0. Set BRIER_LOCAL_DEMO=1 only locally."
        )
    secret_key = os.getenv("DJANGO_SECRET_KEY", "")
    if len(secret_key) < 50 or secret_key.startswith("dev-insecure"):
        raise RuntimeError(
            "DJANGO_SECRET_KEY must be a random production value of at least 50 characters."
        )
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("A PostgreSQL DATABASE_URL is required when DJANGO_DEBUG=0.")


def provision_finance_user() -> bool:
    """Create the initial finance user from deployment secrets, once.

    Existing passwords are never reset by an app restart. Password changes
    therefore remain under the account owner's control.
    """
    username = os.getenv("BRIER_ADMIN_USERNAME", "").strip()
    password = os.getenv("BRIER_ADMIN_PASSWORD", "")
    if not username and not password:
        return False
    if not username or not password:
        raise RuntimeError("Set both BRIER_ADMIN_USERNAME and BRIER_ADMIN_PASSWORD.")
    if len(password) < 12:
        raise RuntimeError("BRIER_ADMIN_PASSWORD must be at least 12 characters.")

    from django.contrib.auth.models import Group, User
    from django.db import transaction

    marker_name = "brier_bootstrapped_finance"
    with transaction.atomic():
        group, _ = Group.objects.get_or_create(name="finance")
        marker, _ = Group.objects.get_or_create(name=marker_name)
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": os.getenv("BRIER_ADMIN_EMAIL", ""), "is_staff": True},
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
            user.groups.add(marker)
        elif not user.groups.filter(name=marker_name).exists():
            raise RuntimeError(
                f"Refusing to promote existing user {username!r}; choose a new bootstrap username."
            )
        changed = []
        if not user.is_staff:
            user.is_staff = True
            changed.append("is_staff")
        if changed:
            user.save(update_fields=changed)
        user.groups.add(group)
    return created
