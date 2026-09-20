"""Generate the separate petrol-pump slip evaluation set."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.datagen.pumpslips import generate_pump_slips


class Command(BaseCommand):
    help = ("Generate pump-slip receipts (Rate/Volume/Amount(Rs), meter numbers) into their own "
            "folder. Evaluate with: manage.py evaluate --data data/pump_slips --extraction-only")

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=60)
        parser.add_argument("--seed", type=int, default=43)
        parser.add_argument("--out", type=str, default=None)

    def handle(self, *args, **options):
        out = Path(options["out"] or settings.DATA_DIR / "pump_slips")
        n = generate_pump_slips(out, count=options["count"], seed=options["seed"])
        self.stdout.write(self.style.SUCCESS(f"Wrote {n} pump slips to {out}"))
