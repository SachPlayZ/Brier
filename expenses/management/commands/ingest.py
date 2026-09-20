"""Load the generated dataset into the database through the real pipeline.

Deliberately runs extraction and duplicate screening rather than copying the
ground-truth columns in: the database ends up holding what the system actually
read, which is the only thing the review UI should ever show.
"""
from __future__ import annotations

import csv
import datetime as dt
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.dedup.blocking import phash_of
from core.dedup.pipeline import find_duplicates
from core.degrade import degrade_text
from expenses import services
from expenses.models import (AuditEvent, Claim, DuplicateFlag, Employee,
                             ExtractedField, Receipt, Vendor)


class Command(BaseCommand):
    help = "Load data/*.csv into the database, running extraction and dedup."

    def add_arguments(self, parser):
        parser.add_argument("--data", type=str, default=None)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--mode", choices=["clean", "simulated", "ocr"],
                            default="simulated",
                            help="Text source: exact sidecar, OCR-noise simulation, or Tesseract.")
        parser.add_argument("--flush", action="store_true",
                            help="Delete existing claims/receipts first.")
        parser.add_argument("--no-phash", action="store_true")

    def handle(self, *args, **options):
        data = Path(options["data"] or settings.DATA_DIR)
        if not (data / "receipts_truth.csv").exists():
            self.stderr.write(self.style.ERROR(
                f"No dataset at {data}. Run `manage.py gen_dataset` first."))
            return

        started = time.time()
        if options["flush"]:
            for model in (AuditEvent, DuplicateFlag, ExtractedField, Claim, Receipt):
                model.objects.all().delete()
            self.stdout.write("  flushed existing claims and receipts")

        self._load_vendors(data)
        gazetteer = services.build_gazetteer()
        line_model = services.load_line_model()
        if line_model is not None:
            self.stdout.write("  using trained line-role model")

        receipts = self._load_receipts(data, options, gazetteer, line_model)
        self._load_claims(data, receipts, options)
        self._screen(options)

        self.stdout.write(self.style.SUCCESS(
            f"Ingested {Receipt.objects.count()} receipts and {Claim.objects.count()} "
            f"claims in {time.time() - started:.0f}s."))

    # ------------------------------------------------------------------ steps
    def _load_vendors(self, data: Path) -> None:
        path = data / "vendors.csv"
        if not path.exists():
            return
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                Vendor.objects.update_or_create(
                    canonical_id=row["vendor_canonical"],
                    defaults={"name": row["vendor_name"], "gstin": row.get("gstin", ""),
                              "category": row.get("category", ""),
                              "city": row.get("city", "")})
        self.stdout.write(f"  vendors: {Vendor.objects.count()}")

    def _load_receipts(self, data: Path, options, gazetteer, line_model) -> dict[str, Receipt]:
        rows = list(csv.DictReader((data / "receipts_truth.csv").open(encoding="utf-8")))
        if options["limit"]:
            rows = rows[: options["limit"]]

        # Each receipt writes ~20 rows (the receipt plus one per extracted field).
        # Outside a transaction SQLite fsyncs every one of them, which dominates
        # the runtime; batching turns a ~12 minute ingest into well under a minute.
        receipts: dict[str, Receipt] = {}
        chunk = 100
        for start in range(0, len(rows), chunk):
            with transaction.atomic():
                for i, row in enumerate(rows[start:start + chunk], start=start):
                    receipt_id = row["receipt_id"]
                    tier = int(row.get("noise_tier") or 0)
                    text = (data / "receipts_text" / f"{receipt_id}.txt").read_text(
                        encoding="utf-8")
                    if options["mode"] == "simulated":
                        text = degrade_text(text, tier, seed=i).text

                    image = data / "receipts_images" / f"{receipt_id}.png"
                    # Stored relative to the data directory, with forward slashes: an
                    # absolute path is only valid on the machine that ran the ingest,
                    # and a backslash is a filename character on macOS and Linux.
                    rel_image = f"receipts_images/{receipt_id}.png"
                    phash = ""
                    if not options["no_phash"] and image.exists():
                        value = phash_of(image)
                        phash = str(value) if value is not None else ""

                    receipt, _ = Receipt.objects.update_or_create(
                        receipt_id=receipt_id,
                        defaults={
                            "text": text, "image_path": rel_image, "phash": phash,
                            "noise_tier": tier,
                            "template_id": row.get("template_id", ""),
                            "ocr_source": ("tesseract" if options["mode"] == "ocr"
                                           else "sidecar")})
                    services.extract_into_receipt(receipt, gazetteer=gazetteer,
                                                  line_model=line_model)
                    receipts[receipt_id] = receipt
            self.stdout.write(f"  receipts {min(start + chunk, len(rows))}/{len(rows)}")
        return receipts

    @transaction.atomic
    def _load_claims(self, data: Path, receipts: dict[str, Receipt], options) -> None:
        rows = list(csv.DictReader((data / "claims.csv").open(encoding="utf-8")))
        employees: dict[str, Employee] = {}
        created = 0
        for row in rows:
            receipt = receipts.get(row["receipt_id"])
            if receipt is None:
                continue
            code = row["employee_id"]
            if code not in employees:
                employees[code] = services.get_or_create_employee(
                    code, full_name=f"Employee {code}", department="General")
            claim, _ = Claim.objects.update_or_create(
                claim_id=row["claim_id"],
                defaults={
                    "employee": employees[code], "receipt": receipt,
                    "claimed_amount": row.get("claimed_amount") or None,
                    "category": row.get("category", ""),
                    "status": row.get("status", Claim.SUBMITTED),
                    "submitted_at": _dt(row.get("submitted_at")),
                    "decided_at": _dt(row.get("decided_at")),
                })
            created += 1
        self.stdout.write(f"  claims: {created}")

    def _screen(self, options) -> None:
        """One corpus-wide dedup pass, which is far cheaper than N incremental ones."""
        claims = list(Claim.objects.select_related("receipt", "employee", "receipt__vendor"))
        records = [services.to_claim_record(c) for c in claims]
        by_id = {c.claim_id: c for c in claims}

        pairs = find_duplicates(records, min_band="LOW",
                                policy_limit=getattr(settings, "POLICY_SPEND_LIMIT", None))
        DuplicateFlag.objects.all().delete()
        flags = []
        for pair in pairs:
            a, b = by_id.get(pair.a), by_id.get(pair.b)
            if a is None or b is None:
                continue
            # The newer claim carries the flag: it is the one under scrutiny.
            newer, older = _order(a, b)
            flags.append(DuplicateFlag(claim=newer, matched_claim=older, score=pair.score,
                                       band=pair.band, signals=pair.signals,
                                       rules_fired=pair.rules_fired, tags=pair.tags))
        DuplicateFlag.objects.bulk_create(flags, ignore_conflicts=True)
        groups = services.recompute_groups()
        self.stdout.write(f"  duplicate flags: {len(flags)} in {groups} groups")


def _order(a: Claim, b: Claim) -> tuple[Claim, Claim]:
    if a.submitted_at and b.submitted_at:
        return (a, b) if a.submitted_at >= b.submitted_at else (b, a)
    return a, b


def _dt(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
