"""The single seam where the OCR decision lives.

Text-first: the synthetic generator writes an exact ``.txt`` sidecar next to
every rendered image, so the whole extraction + dedup stack is buildable and
testable with zero OCR installed. When Tesseract *is* available, ``ocr_image``
produces the same ``OcrText`` shape with real per-character confidences, and
nothing downstream changes.
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from core.types import OcrText

SIDECAR_CONF = 0.95  # native rendered text: high but not certain

#: Where Windows installers put the binary. The installers frequently do not
#: add it to PATH, and a long-running server process would not see a PATH
#: change anyway, so the binary is located explicitly rather than hoped for.
KNOWN_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
)


@lru_cache(maxsize=1)
def find_tesseract() -> str | None:
    """Locate the Tesseract binary: env override, then PATH, then known paths."""
    explicit = os.getenv("TESSERACT_CMD")
    if explicit and Path(explicit).is_file():
        return explicit
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    for candidate in KNOWN_TESSERACT_PATHS:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def is_ocr_available() -> bool:
    """True when both the Python binding and the Tesseract binary are usable."""
    try:
        import pytesseract
    except ImportError:
        return False

    binary = find_tesseract()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        # No binary, or it is present but not runnable. Either way the sidecar
        # path is used and nothing downstream changes.
        return False
    return True


def ocr_status() -> dict:
    """Diagnostics for the setup path -- surfaced by `manage.py check_ocr`."""
    try:
        import pytesseract
    except ImportError:
        return {"binding": False, "binary": None, "available": False,
                "hint": "pip install pytesseract"}
    binary = find_tesseract()
    available = is_ocr_available()
    version = None
    if available:
        try:
            version = str(pytesseract.get_tesseract_version())
        except Exception:
            version = None
    return {
        "binding": True,
        "binary": binary,
        "version": version,
        "available": available,
        "hint": None if available else
        "Install Tesseract (winget install UB-Mannheim.TesseractOCR), then "
        "restart the server. Set TESSERACT_CMD in .env if it is installed "
        "somewhere unusual.",
    }


MIN_OCR_WIDTH = 1600     # Tesseract reads small phone-screenshot receipts badly below this
MAX_OCR_WIDTH = 3200     # and slowly above it


def normalise_image(im):
    """Orientation and colour mode only: the image as a person sees it, safe for Tesseract."""
    from PIL import Image, ImageOps

    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, "white")   # transparent PNGs otherwise OCR as black
        canvas.alpha_composite(rgba)
        im = canvas.convert("RGB")
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if im.width > MAX_OCR_WIDTH:
        im = im.resize((MAX_OCR_WIDTH, round(im.height * MAX_OCR_WIDTH / im.width)), Image.LANCZOS)
    return im


def prepare_image(im):
    """Cleaner input for Tesseract: grayscale, at least 1600 px wide, contrast stretched."""
    from PIL import Image, ImageOps

    im = normalise_image(im).convert("L")
    if im.width < MIN_OCR_WIDTH:
        im = im.resize((MIN_OCR_WIDTH, round(im.height * MIN_OCR_WIDTH / im.width)), Image.LANCZOS)
    return ImageOps.autocontrast(im, cutoff=1)


def ocr_image(path, *, lang: str = "eng", psm: int = 6) -> OcrText:
    """Run Tesseract and build per-character confidences from word boxes.

    ``path`` is a file path or an already-open PIL image."""
    import pytesseract
    from PIL import Image

    binary = find_tesseract()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary

    data = pytesseract.image_to_data(
        path if hasattr(path, "convert") else Image.open(path),
        lang=lang,
        config=f"--psm {psm}",
        output_type=pytesseract.Output.DICT,
    )

    lines: list[str] = []
    confs: list[float] = []
    current_key = None
    current_words: list[tuple[str, float]] = []

    def flush() -> None:
        if not current_words:
            return
        text = " ".join(w for w, _ in current_words)
        lines.append(text)
        for i, (word, conf) in enumerate(current_words):
            if i:
                confs.append(conf)          # the joining space
            confs.extend([conf] * len(word))
        confs.append(1.0)                   # the newline

    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key != current_key:
            flush()
            current_words = []
            current_key = key
        if not word:
            continue
        try:
            conf = max(float(data["conf"][i]), 0.0) / 100.0
        except (TypeError, ValueError):
            conf = 0.0
        current_words.append((word, conf))
    flush()

    text = "\n".join(lines)
    # Drop the trailing newline confidence so char_conf stays parallel to text.
    return OcrText(text=text, char_conf=confs[: len(text)], source="tesseract")


def load_text(
    receipt_id: str,
    *,
    data_dir: Path,
    prefer: str = "sidecar",
) -> OcrText:
    """Load receipt text, preferring the sidecar and falling back to OCR.

    ``prefer='ocr'`` forces Tesseract when it is installed, which is how the
    real-OCR evaluation mode is run.
    """
    data_dir = Path(data_dir)
    sidecar = data_dir / "receipts_text" / f"{receipt_id}.txt"
    image = _find_image(data_dir / "receipts_images", receipt_id)

    if prefer == "ocr" and image and is_ocr_available():
        return ocr_image(image)
    if sidecar.exists():
        text = sidecar.read_text(encoding="utf-8")
        return OcrText(text=text, char_conf=[SIDECAR_CONF] * len(text), source="sidecar")
    if image and is_ocr_available():
        return ocr_image(image)

    raise FileNotFoundError(
        f"No text for receipt {receipt_id!r}: sidecar {sidecar} is missing and "
        f"{'the image is missing too' if not image else 'Tesseract is not installed'}. "
        "Run `manage.py gen_dataset`, or install Tesseract for real OCR."
    )


def text_from_string(raw: str) -> OcrText:
    """Wrap a plain string (e.g. a pasted receipt) as native-confidence text."""
    return OcrText(text=raw, char_conf=[SIDECAR_CONF] * len(raw), source="sidecar")


def _find_image(folder: Path, receipt_id: str) -> Path | None:
    for ext in (".png", ".jpg", ".jpeg"):
        candidate = folder / f"{receipt_id}{ext}"
        if candidate.exists():
            return candidate
    return None
