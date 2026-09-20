"""Train the optional line-role classifier from the generator's free labels."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.extraction.linemodel import LineRoleModel


class Command(BaseCommand):
    help = "Train the line-role model used by the `ml` confidence component."

    def add_arguments(self, parser):
        parser.add_argument("--data", type=str, default=None)
        parser.add_argument("--out", type=str, default=None)

    def handle(self, *args, **options):
        data = Path(options["data"] or settings.DATA_DIR)
        lines_csv = data / "receipts_lines.csv"
        if not lines_csv.exists():
            self.stderr.write(self.style.ERROR(
                f"{lines_csv} not found. Run `manage.py gen_dataset` first."))
            return

        out = Path(options["out"] or (Path(settings.MODEL_DIR) / "line_role.joblib"))
        self.stdout.write(f"Training from {lines_csv} ...")
        model = LineRoleModel.train(lines_csv, out)
        if model is None:
            self.stderr.write(self.style.ERROR(
                "Not enough labelled lines to train. Generate a larger dataset."))
            return
        self.stdout.write(self.style.SUCCESS(f"Saved {out}"))
