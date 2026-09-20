"""Receipts arrive as PDF, JPG, PNG, WEBP, GIF, BMP or TIFF: all must be read, none may be trusted blindly.

The type comes from the file's bytes, not its name. Everything ends up as a picture on the Receipt
(preview, duplicate hash, OCR); a PDF's own text layer is used when it has one.
"""
from __future__ import annotations

import io
import random
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from core import ocr, pdfio
from core.datagen.render import load_font, render_receipt
from expenses import uploads
from expenses.models import Claim, Employee, Receipt

BILL = ["SHARMA TRADERS PVT LTD", "14 MG Road, Bengaluru 560001", "GSTIN: 29AAGCB7383J1Z4", "TAX INVOICE",
        "Invoice No: INV-2025-00871", "Date: 14/03/2025", "Sub Total:                 995.00",
        "CGST @ 9%:                  89.55", "SGST @ 9%:                  89.55", "Round Off:                  -0.10",
        "Grand Total:             1,174.00", "Thank you for your business"]
EXPECTED_TOTAL = Decimal("1174.00")

needs_ocr = pytest.mark.skipif(not ocr.is_ocr_available(), reason="Tesseract is not installed")


# ------------------------------------------------------------------- file builders
def png_bytes(tmp_path) -> bytes:
    path = tmp_path / "bill.png"
    render_receipt(BILL, path, noise_tier=0, rng=random.Random(1), font=load_font(18))
    return path.read_bytes()


def convert(png: bytes, fmt: str) -> bytes:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    return buffer.getvalue()


def text_pdf(lines) -> bytes:
    """A one-page PDF whose text is real text (a "digital" receipt), written by hand."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 11 Tf 14 TL 40 800 Td " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode(),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def image_pdf(png: bytes) -> bytes:
    buffer = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buffer, format="PDF")
    return buffer.getvalue()


# ------------------------------------------------------------------- sniffing + validation
class TestSniff:
    @pytest.mark.parametrize("head,kind", [
        (b"%PDF-1.7", "pdf"), (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"), (b"GIF89a", "gif"), (b"BM6\x00", "bmp"),
        (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"\x00\x00\x00\x18ftypheic", "heic")])
    def test_kinds(self, head, kind):
        assert uploads.sniff(head.ljust(16, b"\0")) == kind

    def test_unknown(self):
        assert uploads.sniff(b"MZ\x90\x00" + b"\0" * 12) is None


class TestValidation:
    def upload(self, data, name="receipt.jpg"):
        return SimpleUploadedFile(name, data)

    def test_a_renamed_program_is_refused(self):
        with pytest.raises(ValidationError, match="not a supported receipt"):
            uploads.validate_receipt_upload(self.upload(b"MZ\x90\x00" + b"\0" * 200, "receipt.jpg"))

    def test_heic_gets_a_useful_message(self):
        with pytest.raises(ValidationError, match="HEIC"):
            uploads.validate_receipt_upload(self.upload(b"\x00\x00\x00\x18ftypheic" + b"\0" * 64))

    def test_a_truncated_image_is_refused(self, tmp_path):
        with pytest.raises(ValidationError, match="could not be opened"):
            uploads.validate_receipt_upload(self.upload(png_bytes(tmp_path)[:100], "r.png"))

    def test_a_corrupt_pdf_is_refused(self):
        with pytest.raises(ValidationError, match="could not be opened"):
            uploads.validate_receipt_upload(self.upload(b"%PDF-1.4\nnot really", "r.pdf"))

    def test_too_large(self, monkeypatch, tmp_path):
        monkeypatch.setattr(uploads, "MAX_BYTES", 500)
        with pytest.raises(ValidationError, match="limit"):
            uploads.validate_receipt_upload(self.upload(png_bytes(tmp_path), "r.png"))

    def test_the_name_does_not_matter(self, tmp_path):
        """A PNG called .jpg is still a PNG."""
        uploads.validate_receipt_upload(self.upload(png_bytes(tmp_path), "photo.jpg"))

    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"])
    def test_every_image_format_passes(self, tmp_path, fmt):
        uploads.validate_receipt_upload(self.upload(convert(png_bytes(tmp_path), fmt), "r.bin"))

    def test_pdfs_pass(self, tmp_path):
        uploads.validate_receipt_upload(self.upload(text_pdf(BILL), "r.pdf"))
        uploads.validate_receipt_upload(self.upload(image_pdf(png_bytes(tmp_path)), "r.pdf"))


# ------------------------------------------------------------------- normalising
class TestPrepare:
    def test_pdf_becomes_a_png_and_keeps_its_text_layer(self):
        image, text = uploads.prepare_upload(SimpleUploadedFile("bill.pdf", text_pdf(BILL)))
        assert image.name == "bill.png" and image.read(8) == b"\x89PNG\r\n\x1a\n"
        assert "Grand Total" in text and "1,174.00" in text

    def test_image_only_pdf_has_no_text_layer(self, tmp_path):
        image, text = uploads.prepare_upload(SimpleUploadedFile("scan.pdf", image_pdf(png_bytes(tmp_path))))
        assert text == "" and image.name == "scan.png"

    @pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
    def test_browser_unfriendly_formats_become_png(self, tmp_path, fmt):
        image, text = uploads.prepare_upload(SimpleUploadedFile("r." + fmt.lower(), convert(png_bytes(tmp_path), fmt)))
        assert image.name == "r.png" and text == ""

    @pytest.mark.parametrize("fmt,name", [("JPEG", "r.jpg"), ("PNG", "r.png"), ("WEBP", "r.webp")])
    def test_web_formats_are_kept_as_uploaded(self, tmp_path, fmt, name):
        upload = SimpleUploadedFile(name, convert(png_bytes(tmp_path), fmt))
        image, _ = uploads.prepare_upload(upload)
        assert image is upload

    def test_transparent_png_is_flattened_onto_white(self):
        rgba = Image.new("RGBA", (40, 40), (0, 0, 0, 0))        # black text on transparency reads as black on black
        flat = ocr.normalise_image(rgba)
        assert flat.mode == "RGB" and flat.getpixel((5, 5)) == (255, 255, 255)

    def test_small_images_are_upscaled_for_ocr(self):
        small = Image.new("L", (400, 900), 255)
        assert ocr.prepare_image(small).width >= ocr.MIN_OCR_WIDTH


class TestPdfText:
    def test_text_layer(self):
        text = pdfio.text_layer(text_pdf(BILL))
        assert "SHARMA TRADERS" in text and "1,174.00" in text

    def test_a_scan_has_none(self, tmp_path):
        assert pdfio.text_layer(image_pdf(png_bytes(tmp_path))) == ""


# ------------------------------------------------------------------- end to end
pytestmark_db = pytest.mark.django_db


@pytest.fixture
def employee_client(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"                     # never write test uploads into the real media
    user = User.objects.create_user("asha", password="pw")
    Employee.objects.create(employee_code="E001", full_name="Asha R", user=user)
    client.login(username="asha", password="pw")
    return client


def submit(client, name, data):
    return client.post(reverse("claim_submit"), {"image": SimpleUploadedFile(name, data), "description": "t"})


@pytest.mark.django_db
class TestSubmitEachFormat:
    def test_pdf_with_a_text_layer_needs_no_ocr(self, employee_client):
        response = submit(employee_client, "bill.pdf", text_pdf(BILL))
        assert response.status_code == 302, response.content[:300]
        receipt = Receipt.objects.get()
        assert receipt.total == EXPECTED_TOTAL and receipt.invoice_no == "INV-2025-00871"
        assert receipt.image.name.endswith(".png")               # the preview is the rendered page
        assert Claim.objects.count() == 1

    @needs_ocr
    @pytest.mark.parametrize("fmt,name", [("PNG", "b.png"), ("JPEG", "b.jpg"), ("WEBP", "b.webp"),
                                          ("GIF", "b.gif"), ("BMP", "b.bmp"), ("TIFF", "b.tif")])
    def test_image_formats_are_read(self, employee_client, tmp_path, fmt, name):
        response = submit(employee_client, name, convert(png_bytes(tmp_path), fmt))
        assert response.status_code == 302, response.content[:300]
        assert Receipt.objects.get().total == EXPECTED_TOTAL

    @needs_ocr
    def test_scanned_pdf_is_read_by_ocr(self, employee_client, tmp_path):
        response = submit(employee_client, "scan.pdf", image_pdf(png_bytes(tmp_path)))
        assert response.status_code == 302, response.content[:300]
        assert Receipt.objects.get().total == EXPECTED_TOTAL

    def test_a_refused_file_creates_nothing_and_says_why(self, employee_client):
        response = submit(employee_client, "virus.jpg", b"MZ\x90\x00" + b"\0" * 300)
        assert response.status_code == 200
        assert "not a supported receipt" in response.content.decode()
        assert Receipt.objects.count() == 0 and Claim.objects.count() == 0
