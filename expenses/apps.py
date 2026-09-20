import importlib
import os
import threading

from django.apps import AppConfig


class ExpensesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "expenses"

    def ready(self):
        # Side-effect import: connects the signal that applies the SQLite
        # pragmas (WAL, busy timeout) to every new connection.
        importlib.import_module("expenses.db")

        if os.environ.get("RUN_MAIN") or os.environ.get("EXPENSES_WARM"):
            _warm_in_background()


def _warm_in_background() -> None:
    """Import scikit-learn off the request path.

    The TF-IDF index imports sklearn lazily on first use, which costs ~2.4s.
    Paid during a claim submission it made the first upload crawl -- and while
    that work still sat inside a write transaction it was enough to blow past
    SQLite's lock timeout. Warming it at startup keeps the first submission as
    fast as the rest.
    """

    def warm():
        try:
            importlib.import_module("sklearn.feature_extraction.text")
        except Exception:
            pass

    threading.Thread(target=warm, name="warm-sklearn", daemon=True).start()
