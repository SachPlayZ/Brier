"""Receipt uploads in any common format: check what the file really is, then make it usable.

The type is decided by the file's first bytes, not its extension or the browser's claim, so a
renamed executable is refused and a PDF saved as ".jpg" still works.

Every accepted file ends up as a picture in ``Receipt.image`` (preview, perceptual-hash duplicate
check, OCR): a PDF becomes its rendered pages stacked into one PNG, and its own text layer, when it
has one, is returned as exact text. JPEG, PNG and WEBP are kept as uploaded.
"""
from __future__ import annotations

import io
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from core import pdfio

MAX_BYTES = 15 * 1024 * 1024
ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff,image/*,application/pdf"
FRIENDLY = "a PDF, JPG, PNG, WEBP, GIF, BMP or TIFF"

_KEEP_AS_IS = {"jpeg", "png", "webp"}       # browsers show these; everything else is converted to PNG


def sniff(head: bytes) -> str | None:
    """What the bytes say the file is."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"GIF8",):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heim"):
        return "heic"
    return None


def validate_receipt_upload(upload) -> None:
    """Form validator: refuse anything that is not a readable receipt file."""
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    kind = sniff(head)
    if kind == "heic":
        raise ValidationError("HEIC photos are not supported yet. Export the photo as JPG or PNG and "
                              "upload that.")
    if kind is None:
        raise ValidationError(f"This file is not a supported receipt. Upload {FRIENDLY}.")
    if upload.size > MAX_BYTES:
        raise ValidationError(f"This file is {upload.size / 1048576:.1f} MB. The limit is "
                              f"{MAX_BYTES // 1048576} MB.")
    data = upload.read()
    upload.seek(0)
    try:
        if kind == "pdf":
            if pdfio.page_count(data) < 1:
                raise ValueError("no pages")
        else:
            from PIL import Image

            Image.open(io.BytesIO(data)).verify()
    except Exception as exc:                          # noqa: BLE001 - any parser failure means unreadable
        raise ValidationError(f"This {kind.upper()} file could not be opened ({exc.__class__.__name__}). "
                              "It may be damaged or password protected.") from exc


def prepare_upload(upload):
    """``(image_file, exact_text)`` ready to store on a Receipt. ``exact_text`` is "" unless the
    upload is a PDF with a text layer."""
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    kind = sniff(data[:16])
    stem = Path(getattr(upload, "name", "receipt")).stem or "receipt"

    if kind == "pdf":
        pages = pdfio.render_pages(data)
        return _png(pdfio.stitch(pages), stem), pdfio.text_layer(data)
    if kind in _KEEP_AS_IS:
        return upload, ""

    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))    # first frame of a GIF or TIFF
    return _png(image.convert("RGB"), stem), ""


def _png(image, stem: str) -> ContentFile:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return ContentFile(buffer.getvalue(), name=f"{stem}.png")
