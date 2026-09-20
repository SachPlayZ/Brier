"""PDF receipts: the embedded text layer when there is one, otherwise rendered pages for OCR.

pypdfium2 (Apache/BSD, ships PDFium, no external binary). Only the first few pages are read: a
receipt is a page or two, and a 200-page PDF is not a receipt.
"""
from __future__ import annotations

from pathlib import Path

MAX_PAGES = 3
RENDER_DPI = 250
MIN_TEXT_CHARS = 40          # fewer than this and the "text layer" is a scanned page with a stamp


def is_pdf(path_or_bytes) -> bool:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return bytes(path_or_bytes[:5]) == b"%PDF-"
    try:
        with open(path_or_bytes, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _open(source):
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(bytes(source) if isinstance(source, (bytes, bytearray)) else str(Path(source)))


def page_count(source) -> int:
    pdf = _open(source)
    try:
        return len(pdf)
    finally:
        pdf.close()


def text_layer(source, *, max_pages: int = MAX_PAGES) -> str:
    """The text a PDF carries itself, page by page. Empty for a scan."""
    pdf = _open(source)
    try:
        parts = []
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            textpage = page.get_textpage()
            parts.append(textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n"))
            textpage.close()
            page.close()
    finally:
        pdf.close()
    lines = [line.rstrip() for part in parts for line in part.split("\n")]
    text = "\n".join(line for line in lines if line.strip())
    return text if len(text.replace("\n", "").strip()) >= MIN_TEXT_CHARS else ""


def render_pages(source, *, dpi: int = RENDER_DPI, max_pages: int = MAX_PAGES):
    """Pages as PIL images."""
    pdf = _open(source)
    try:
        images = []
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            images.append(page.render(scale=dpi / 72).to_pil().convert("RGB"))
            page.close()
        return images
    finally:
        pdf.close()


def stitch(images):
    """Stack pages top to bottom into one image (a receipt that runs onto a second page)."""
    from PIL import Image

    if len(images) == 1:
        return images[0]
    width = max(im.width for im in images)
    canvas = Image.new("RGB", (width, sum(im.height for im in images)), "white")
    y = 0
    for im in images:
        canvas.paste(im, (0, y))
        y += im.height
    return canvas
