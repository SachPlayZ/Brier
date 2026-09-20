"""Report whether OCR is wired up, and prove it on a real image."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.ocr import ocr_image, ocr_status


class Command(BaseCommand):
    help = "Diagnose the OCR setup and optionally run it on one image."

    def add_arguments(self, parser):
        parser.add_argument("--image", type=str, default=None,
                            help="Image to OCR as a smoke test.")

    def handle(self, *args, **options):
        status = ocr_status()
        self.stdout.write(f"  pytesseract installed : {status['binding']}")
        self.stdout.write(f"  tesseract binary      : {status.get('binary') or 'not found'}")
        self.stdout.write(f"  tesseract version     : {status.get('version') or '-'}")

        if not status["available"]:
            self.stdout.write(self.style.WARNING("  OCR NOT available"))
            self.stdout.write(f"  {status['hint']}")
            self.stdout.write("  Image uploads will be rejected; pasting receipt "
                              "text still works.")
            return

        self.stdout.write(self.style.SUCCESS("  OCR available"))

        image = options["image"]
        if image is None:
            sample = sorted((Path(settings.DATA_DIR) / "receipts_images").glob("*.png"))
            image = str(sample[0]) if sample else None
        if image is None:
            return

        self.stdout.write(f"\nOCR of {image}:")
        result = ocr_image(image)
        mean_conf = (sum(result.char_conf) / len(result.char_conf)) if result.char_conf else 0.0
        for line in result.text.splitlines()[:14]:
            self.stdout.write(f"  | {line}")
        self.stdout.write(f"  mean character confidence: {mean_conf:.3f}")
