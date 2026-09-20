"""Read a receipt FILE (any supported format) into text, keeping the reading that makes the most sense.

One OCR pass is a guess. Tesseract reads a phone screenshot best one way and a scan best another,
so this tries up to three readings and keeps the fuller, more consistent one (see ``_score``):

    1. the PDF's own text layer (exact, when the PDF has one),
    2. the image as it is (orientation and colour mode fixed),
    3. the image cleaned for OCR (grayscale, upscaled, contrast stretched).

It stops at the first reading that reconciles with high confidence, so a clear receipt costs one
OCR pass. Measured on real slips, the cleaned pass fixes "283 .00" and "Rate(2/L)"; the plain pass
is better on others, which is why neither is used blindly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

from core import ocr, pdfio
from core.extraction.linemodel import LineRoleModel
from core.extraction.pipeline import extract_receipt
from core.extraction.vendors import VendorGazetteer
from core.types import OcrText

#: A reading this complete and this consistent is not worth a second OCR pass.
GOOD_ENOUGH_FIELDS = 6
GOOD_ENOUGH_MEAN = 0.70
#: A later reading must beat the earlier one by this much to replace it (ties keep the plain pass).
MARGIN = 0.05


def _score(text: OcrText, gazetteer, line_model) -> tuple[float, bool]:
    """How good a reading is, and whether it is good enough to stop.

    Not ``doc_confidence``: that is the minimum over vendor, date and total, so a reading with
    every amount right and one missing date scores 0 and lost to a poorer, emptier one (measured
    on real scans). The score is the total confidence of everything found, which rewards reading
    MORE of the receipt correctly, plus agreement of the amounts.
    """
    result = extract_receipt(text, gazetteer=gazetteer, line_model=line_model)
    found = [fr.confidence for fr in result.fields.values() if fr.normalized is not None]
    mean = sum(found) / len(found) if found else 0.0
    # "Adds up" only counts when there was something to add: a subtotal that equals the total with
    # every tax missing "reconciles" trivially, and was preferred to a fuller, slightly-off reading.
    amounts = sum(1 for n in ("subtotal", "cgst", "sgst", "igst", "tax_total", "total")
                  if result.fields.get(n) is not None and result.fields[n].normalized is not None)
    arithmetic = {True: 0.30 if amounts >= 3 else 0.05, None: 0.0, False: -0.10}[result.arithmetic_ok]
    good = (result.arithmetic_ok is True and len(found) >= GOOD_ENOUGH_FIELDS and mean >= GOOD_ENOUGH_MEAN)
    return sum(found) / 10 + arithmetic, good


def _readings(path: Path) -> Iterator[Callable[[], OcrText]]:
    """Lazy readings, best-first, so an early good one saves the rest."""
    from PIL import Image

    if pdfio.is_pdf(path):
        layer = pdfio.text_layer(path)
        if layer:
            yield lambda: ocr.text_from_string(layer)
        if not ocr.is_ocr_available():
            return
        image = pdfio.stitch(pdfio.render_pages(path))
    else:
        if not ocr.is_ocr_available():
            return
        image = Image.open(path)
        image.load()
    yield lambda: ocr.ocr_image(ocr.normalise_image(image))
    yield lambda: ocr.ocr_image(ocr.prepare_image(image))


def read_receipt(path, *, gazetteer: VendorGazetteer | None = None,
                 line_model: LineRoleModel | None = None) -> OcrText:
    """Text of the best reading of ``path``. Raises ``RuntimeError`` when nothing can be read."""
    path = Path(path)
    best: tuple[float, OcrText] | None = None
    for reading in _readings(path):
        text = reading()
        if not text.text.strip():
            continue
        score, good = _score(text, gazetteer, line_model)
        if best is None or score > best[0] + MARGIN:
            best = (score, text)
        if good:
            break
    if best is None:
        if not ocr.is_ocr_available():
            raise RuntimeError("Tesseract is not installed, so an uploaded image cannot be read. "
                               "Install Tesseract, or paste the receipt text into the form.")
        raise RuntimeError("No text could be read from this file. Try a clearer photo or scan, "
                           "or paste the receipt text.")
    return best[1]
