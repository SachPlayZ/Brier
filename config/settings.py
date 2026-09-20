"""Django settings for the expense-claim project.

Database is driven entirely by DATABASE_URL so SQLite (dev) and PostgreSQL
(prod) are a one-line switch. No Postgres-only model fields are used anywhere.
"""
from pathlib import Path
import os

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"] if DEBUG else os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "expenses",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "expenses.context_processors.roles",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
    )
}

if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    # Persistent connections plus SQLite's file locking cause "database is
    # locked" under concurrent requests; a fresh connection per request avoids
    # holding a stale lock. WAL and the busy timeout are set per connection in
    # expenses/db.py.
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["timeout"] = 30
    # The important one. A DEFERRED transaction (SQLite's default) takes a read
    # lock at BEGIN and upgrades to a write lock on the first write -- and that
    # upgrade cannot wait, because two upgrading readers would deadlock. SQLite
    # therefore returns "database is locked" *immediately*, ignoring the busy
    # timeout entirely. IMMEDIATE takes the write lock up front, so concurrent
    # writers queue on the timeout instead of failing.
    DATABASES["default"]["OPTIONS"]["transaction_mode"] = "IMMEDIATE"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "claim_list"
LOGOUT_REDIRECT_URL = "login"

# --- project-specific -------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"

# Confidence routing thresholds.
#
# AUTO_ACCEPT is empirical, not guessed: `manage.py evaluate` sorts every field
# by confidence and finds the cutoff at which accuracy reaches 99%. On the
# seed-42 corpus that is 0.8216, covering 86% of fields at 99.0% accuracy.
# Re-run evaluate after changing the extractor and update this.
CONFIDENCE_AUTO_ACCEPT = 0.8216
CONFIDENCE_NEEDS_REVIEW = 0.60

# Bands that block approval until a reviewer resolves the flag.
BLOCKING_DUP_BANDS = ("EXACT", "HIGH")

# Policy spend limit used by the threshold-gaming split-claim heuristic (rupees).
POLICY_SPEND_LIMIT = 5000

# Brier reads Indian tax receipts (GSTIN, CGST, SGST, IGST), so only these currencies are
# accepted. core/currency.py still detects the others: that is how a foreign receipt is
# recognised and rejected at upload. Widening this tuple is the only change needed to accept more.
ALLOWED_CURRENCIES = ("INR",)
