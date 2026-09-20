"""Recompute duplicate flags across all claims after a weight or rule change."""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.dedup.pipeline import find_duplicates
from expenses import services
from expenses.models import Claim, DuplicateFlag


class Command(BaseCommand):
    help = "Re-run duplicate detection over every claim and rebuild the flags."

    def add_arguments(self, parser):
        parser.add_argument("--min-band", default="LOW",
                            choices=["LOW", "MEDIUM", "HIGH", "EXACT"])
        parser.add_argument("--keep-resolved", action="store_true",
                            help="Preserve reviewer decisions on existing flags.")

    def handle(self, *args, **options):
        claims = list(Claim.objects.select_related(
            "receipt", "employee", "receipt__vendor").defer("receipt__image_blob"))
        if not claims:
            self.stderr.write(self.style.ERROR("No claims. Run `manage.py ingest` first."))
            return

        resolved = {}
        if options["keep_resolved"]:
            resolved = {(f.claim_id, f.matched_claim_id): f
                        for f in DuplicateFlag.objects.exclude(status=DuplicateFlag.OPEN)}

        records = [services.to_claim_record(c) for c in claims]
        by_id = {c.claim_id: c for c in claims}
        pairs = find_duplicates(records, min_band=options["min_band"],
                                policy_limit=getattr(settings, "POLICY_SPEND_LIMIT", None))

        DuplicateFlag.objects.all().delete()
        created = 0
        for pair in pairs:
            a, b = by_id.get(pair.a), by_id.get(pair.b)
            if a is None or b is None:
                continue
            newer, older = ((a, b) if (a.submitted_at or 0) >= (b.submitted_at or 0) else (b, a))
            previous = resolved.get((newer.pk, older.pk))
            DuplicateFlag.objects.update_or_create(
                claim=newer, matched_claim=older,
                defaults={"score": pair.score, "band": pair.band, "signals": pair.signals,
                          "rules_fired": pair.rules_fired, "tags": pair.tags,
                          "status": previous.status if previous else DuplicateFlag.OPEN,
                          "resolved_by": previous.resolved_by if previous else None,
                          "resolved_at": previous.resolved_at if previous else None})
            created += 1

        groups = services.recompute_groups()
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {created} duplicate flags across {groups} groups."))
