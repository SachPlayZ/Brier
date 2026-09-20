"""Measure extraction accuracy and duplicate-detection quality against truth."""
from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.dataset import build_claim_records, extract_all
from core.eval.eval_dedup import evaluate_dedup, tune_weights
from core.eval.eval_extraction import evaluate_extraction, write_errors
from core.eval.report import render_markdown, write_reports
from core.ocr import is_ocr_available
from expenses import services


class Command(BaseCommand):
    help = "Evaluate extraction and duplicate detection against the ground-truth CSVs."

    def add_arguments(self, parser):
        parser.add_argument("--data", type=str, default=None)
        parser.add_argument("--mode", choices=["clean", "simulated", "ocr"],
                            default="simulated",
                            help="clean=exact sidecar (upper bound), simulated=OCR-noise "
                                 "simulation (default), ocr=real Tesseract.")
        parser.add_argument("--extraction-only", action="store_true")
        parser.add_argument("--dedup-only", action="store_true")
        parser.add_argument("--ablation", action="store_true",
                            help="Run the signal ablation table (slower).")
        parser.add_argument("--tune", action="store_true",
                            help="Random-search the dedup weights on a dev split.")
        parser.add_argument("--samples", type=int, default=200)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, **options):
        data = Path(options["data"] or settings.DATA_DIR)
        truth = data / "receipts_truth.csv"
        if not truth.exists():
            self.stderr.write(self.style.ERROR(
                f"No dataset at {data}. Run `manage.py gen_dataset` first."))
            return

        mode = options["mode"]
        if mode == "ocr" and not is_ocr_available():
            self.stdout.write(self.style.WARNING(
                "Tesseract not found; falling back to --mode simulated."))
            mode = "simulated"

        started = time.time()
        self.stdout.write(f"Extracting ({mode}) ...")
        results = extract_all(data, mode=mode, line_model=services.load_line_model(),
                              limit=options["limit"],
                              progress=lambda m: self.stdout.write(f"  {m}"))

        extraction = dedup = None
        if not options["dedup_only"]:
            extraction = evaluate_extraction(results, truth)
            write_errors(extraction, data / "eval_errors.csv")

        if not options["extraction_only"]:
            self.stdout.write("Building claim records ...")
            records = build_claim_records(data, results)
            self.stdout.write("Evaluating duplicate detection ...")
            dedup = evaluate_dedup(records, data / "duplicate_pairs.csv",
                                   run_ablation=options["ablation"])

            if options["tune"]:
                self.stdout.write(f"Tuning weights ({options['samples']} samples) ...")
                tuned = tune_weights(records, data / "duplicate_pairs.csv",
                                     n_samples=options["samples"])
                self._print_tuning(tuned)

        meta = {"mode": mode, "receipts": len(results),
                "ocr_available": is_ocr_available(),
                "elapsed_s": round(time.time() - started, 1)}
        md_path, json_path = write_reports(data / "eval_report", extraction, dedup, meta=meta)

        self.stdout.write("")
        self.stdout.write(render_markdown(extraction, dedup, meta=meta))
        self.stdout.write(self.style.SUCCESS(f"\nWrote {md_path} and {json_path}"))
        if extraction is not None:
            self.stdout.write(self.style.SUCCESS(
                f"Wrote {data / 'eval_errors.csv'} ({len(extraction.errors)} rows)"))

    def _print_tuning(self, tuned: dict) -> None:
        self.stdout.write("")
        self.stdout.write("  Weight tuning (fit on dev split, reported on held-out test):")
        self.stdout.write(f"    dev  F1: default {tuned['dev_f1_default']:.3f} "
                          f"-> tuned {tuned['dev_f1']:.3f}")
        self.stdout.write(f"    test F1: default {tuned['test_f1_default']:.3f} "
                          f"-> tuned {tuned['test_f1']:.3f}")
        self.stdout.write(f"    weights: {tuned['weights']}")
        if tuned["test_f1"] <= tuned["test_f1_default"]:
            self.stdout.write(self.style.WARNING(
                "    Tuned weights do not beat the defaults on the test split -- "
                "keeping the defaults is the honest choice."))
