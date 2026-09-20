"""Text, number and date normalization.

Two rules drive the design:

1. Newlines are preserved through normalization because line index is a
   confidence feature downstream (``pos`` component).
2. OCR digit repair (O->0, l->1, ...) is applied ONLY inside a candidate
   amount span. Applying it globally would mangle vendor names like
   "BOMBAY STORES" into "B0MBAY ST0RES".
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation

RUPEE = "\u20b9"

_CURRENCY_RE = re.compile(r"(?<![A-Za-z])(?:INR|Rs\.?|R\s?s\.?|\u20b9)(?=\s*[\d,])", re.I)
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")
_SPACE_RE = re.compile(r"[^\S\n]+")          # runs of whitespace, but not newlines
_BLANKS_RE = re.compile(r"\n{3,}")

# Applied to numeric spans only -- see fix_numeric_ocr.
_NUM_FIXES = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1",
                            "|": "1", "S": "5", "s": "5", "B": "8", "Z": "2"})

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:PVT\.?|PRIVATE|LTD\.?|LIMITED|LLP|INC\.?|CO\.?|COMPANY|"
    r"AND\s+SONS|&\s*SONS|ENTERPRISES?|TRADERS?|STORES?|AGENCIES)\b", re.I)


def normalize_text(raw: str) -> str:
    """Canonicalize currency markers, dashes and horizontal whitespace."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _DASH_RE.sub("-", text)
    text = _CURRENCY_RE.sub(RUPEE, text)
    text = _SPACE_RE.sub(" ", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def split_lines(text: str) -> list[str]:
    return text.split("\n")


def line_index_of(text: str, offset: int) -> int:
    """Line number (0-based) containing the given character offset."""
    return text.count("\n", 0, max(offset, 0))


def fix_numeric_ocr(s: str) -> str:
    """Repair common OCR digit confusions inside an already-isolated number."""
    return s.translate(_NUM_FIXES)


def to_decimal(s: str | None) -> Decimal | None:
    """Parse an amount, handling Indian lakh grouping and space grouping.

    '1,23,456.78' -> 123456.78; '1 234,56' is NOT treated as European decimal
    comma -- receipts in scope use '.' as the decimal separator.
    """
    if s is None:
        return None
    # Delegate separator interpretation to core.currency, which decides the
    # decimal point structurally rather than assuming a locale.
    from core.currency import parse_amount

    repaired = fix_numeric_ocr(_strip_currency(str(s)))
    value = parse_amount(repaired)
    if value is not None:
        return value

    # Fall back to the original conservative path for anything exotic.
    cleaned = str(s).strip()
    cleaned = re.sub(r"(?i)^\s*(?:INR|Rs\.?|R\s?s\.?)\s*", "", cleaned)
    cleaned = cleaned.replace(RUPEE, "")
    cleaned = fix_numeric_ocr(cleaned)
    cleaned = re.sub(r"[,\s]", "", cleaned)
    cleaned = re.sub(r"[^\d.\-+]", "", cleaned)
    if not cleaned or cleaned in {"-", "+", "."}:
        return None
    # Guard against multiple dots ("1.234.56") -- keep the last as decimal.
    if cleaned.count(".") > 1:
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _two_digit_year(y: int) -> int:
    return y + 2000 if y < 70 else y + 1900


def parse_date_multi(
    s: str,
    *,
    prefer: str = "DMY",
    today: dt.date | None = None,
) -> tuple[dt.date, str, bool] | None:
    """Parse a date in any of the supported receipt formats.

    Returns ``(date, fmt_id, ambiguous)``. ``ambiguous`` is True when both
    day-first and month-first readings are valid and differ -- the caller
    applies a confidence penalty for it.
    """
    s = (s or "").strip()
    today = today or dt.date.today()

    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        made = _safe_date(y, mo, d)
        return (made, "ymd", False) if made else None

    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = _two_digit_year(y)
        dmy = _safe_date(y, b, a)
        mdy = _safe_date(y, a, b)
        ambiguous = bool(dmy and mdy and dmy != mdy)
        chosen = (dmy or mdy) if prefer == "DMY" else (mdy or dmy)
        return (chosen, "dmy" if prefer == "DMY" else "mdy", ambiguous) if chosen else None

    m = re.match(r"^(\d{1,2})[-\s]?([A-Za-z]{3})[a-z]*[-,\s]?\s*(\d{2,4})$", s)
    if m and m.group(2).lower() in MONTHS:
        d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
        if y < 100:
            y = _two_digit_year(y)
        made = _safe_date(y, mo, d)
        return (made, "dMy", False) if made else None

    # Separators are loose on both sides: OCR produces "Jan'19, 2025" and
    # "Mar 14; 2025" often enough that strict punctuation loses real dates.
    m = re.match(r"^([A-Za-z]{3})[a-z]*[\s'`.,\-]{0,3}(\d{1,2})(?:st|nd|rd|th)?[,;:.\s]+(\d{4})$", s)
    if m and m.group(1).lower() in MONTHS:
        mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        made = _safe_date(y, mo, d)
        return (made, "Mdy", False) if made else None

    m = re.match(r"^(\d{2})(\d{2})(\d{4})$", s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        made = _safe_date(y, mo, d)
        return (made, "compact", True) if made else None

    return None


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def date_plausible(d: dt.date, *, today: dt.date | None = None,
                   years_back: int = 3, days_ahead: int = 7) -> bool:
    today = today or dt.date.today()
    return (today - dt.timedelta(days=365 * years_back)) <= d <= (today + dt.timedelta(days=days_ahead))


def norm_invoice_no(s: str | None) -> str:
    """Uppercase alphanumeric form used for exact invoice matching."""
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def norm_vendor(s: str | None) -> str:
    """Case/punctuation/legal-suffix-stripped vendor form for fuzzy matching."""
    if not s:
        return ""
    v = unicodedata.normalize("NFKC", str(s)).upper()
    v = v.replace("&", " AND ")
    v = _LEGAL_SUFFIX_RE.sub(" ", v)
    v = re.sub(r"[^A-Z0-9 ]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def alpha_ratio(s: str) -> float:
    if not s:
        return 0.0
    letters = sum(1 for c in s if c.isalpha())
    printable = sum(1 for c in s if not c.isspace())
    return letters / printable if printable else 0.0


def caps_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def has_legal_suffix(s: str) -> bool:
    return bool(_LEGAL_SUFFIX_RE.search(s or ""))


def _strip_currency(s: str) -> str:
    """Remove currency markers before OCR digit repair.

    Order matters: "Rs." repaired first becomes "R5.", and the stray 5 is then
    parsed as part of the number.
    """
    from core.currency import ALL_SYMBOLS

    out = s.strip()
    for symbol in ALL_SYMBOLS:
        out = out.replace(symbol, " ")
    return out
