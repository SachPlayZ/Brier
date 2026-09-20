"""Generate the synthetic receipt/claim dataset."""
from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.datagen.generate import generate_dataset


class Command(BaseCommand):
    help = "Generate synthetic receipt images, sidecar text and ground-truth CSVs."

    def add_arguments(self, parser):
        parser.add_argument("--receipts", type=int, default=400)
        parser.add_argument("--claims", type=int, default=500)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--out", type=str, default=None)
        parser.add_argument("--no-images", action="store_true",
                            help="Skip PNG rendering (much faster; text only).")

    def handle(self, *args, **options):
        out = Path(options["out"] or settings.DATA_DIR)
        started = time.time()
        self.stdout.write(f"Generating into {out} (seed={options['seed']}) ...")

        dataset = generate_dataset(
            out, n_receipts=options["receipts"], n_claims=options["claims"],
            seed=options["seed"], render_images=not options["no_images"],
            progress=lambda msg: self.stdout.write(f"  {msg}"))

        labelled = sum(1 for *_rest, label in dataset.pairs if label == 1)
        self.stdout.write(self.style.SUCCESS(
            f"Done in {time.time() - started:.0f}s: {len(dataset.receipts)} receipts, "
            f"{len(dataset.claims)} claims, {labelled} labelled duplicate pairs "
            f"({len(dataset.pairs) - labelled} hard negatives)."))
