"""Currency detection, locale-aware amount parsing and formatting.

Receipts do not agree on how to write a number. ``1,234.56``, ``1.234,56``,
``1 234,56`` and ``1,23,456.78`` are all the same order of magnitude, and the
separators mean opposite things depending on where the receipt was printed.
Rather than guess from the currency (which is often absent), the parser reads
the *structure*: the last separator followed by one or two digits is a decimal
point, and everything else is grouping. That rule is locale-independent and
handles every convention above without being told which one applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Currency:
    code: str
    symbols: tuple[str, ...]
    name: str
    #: 'MDY' for the United States, 'DMY' almost everywhere else. Used to
    #: resolve dates like 03/04/2025, which is genuinely ambiguous otherwise.
    date_order: str = "DMY"
    decimals: int = 2


CURRENCIES: dict[str, Currency] = {
    "INR": Currency("INR", ("₹", "Rs.", "Rs", "INR"), "Indian Rupee", "DMY"),
    "USD": Currency("USD", ("$", "US$", "USD"), "US Dollar", "MDY"),
    "EUR": Currency("EUR", ("€", "EUR"), "Euro", "DMY"),
    "GBP": Currency("GBP", ("£", "GBP"), "Pound Sterling", "DMY"),
    "JPY": Currency("JPY", ("¥", "JPY"), "Japanese Yen", "DMY", decimals=0),
    "CNY": Currency("CNY", ("CNY", "RMB", "元"), "Chinese Yuan", "DMY"),
    "CAD": Currency("CAD", ("CA$", "C$", "CAD"), "Canadian Dollar", "DMY"),
    "AUD": Currency("AUD", ("A$", "AU$", "AUD"), "Australian Dollar", "DMY"),
    "SGD": Currency("SGD", ("S$", "SGD"), "Singapore Dollar", "DMY"),
    "AED": Currency("AED", ("AED", "د.إ"), "UAE Dirham", "DMY"),
    "CHF": Currency("CHF", ("CHF", "Fr."), "Swiss Franc", "DMY"),
    "ZAR": Currency("ZAR", ("ZAR", "R"), "South African Rand", "DMY"),
    "NZD": Currency("NZD", ("NZ$", "NZD"), "New Zealand Dollar", "DMY"),
    "SEK": Currency("SEK", ("SEK", "kr"), "Swedish Krona", "DMY"),
}

DEFAULT_CURRENCY = "INR"

#: Every symbol that can sit against an amount, longest first so "US$" wins
#: over "$" and "Rs." over "R".
ALL_SYMBOLS: tuple[str, ...] = tuple(sorted(
    {s for c in CURRENCIES.values() for s in c.symbols}, key=len, reverse=True))

SYMBOL_CLASS = "|".join(re.escape(s) for s in ALL_SYMBOLS)

#: Bare symbols that several currencies share. Seeing one of these is evidence
#: of *an* amount but not of which currency, so an explicit code wins.
AMBIGUOUS_SYMBOLS = {"$": ("USD", "CAD", "AUD", "SGD", "NZD"), "kr": ("SEK",),
                     "R": ("ZAR",)}

_CODE_RE = re.compile(r"\b(" + "|".join(CURRENCIES) + r")\b")

#: A symbol only counts as currency evidence when it sits against a number.
#: Single-letter symbols like ZAR's "R" otherwise match inside ordinary words --
#: "CAFE ZUR POST" was being read as a South African receipt. The gap is spaces or tabs only:
#: with `\s*` the "R" of a "Round Off" line was glued to the amount on the line above.
_SYMBOL_RE = re.compile(
    r"(?:(" + SYMBOL_CLASS + r")[ \t]*(?=[\d(])|(?<=[\d)])[ \t]*(" + SYMBOL_CLASS + r"))")

#: Country/tax markers that imply a currency when no symbol is present.
_LOCALE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:GSTIN|CGST|SGST|IGST)\b", re.I), "INR"),
    (re.compile(r"\b(?:USt-IdNr|MwSt|Mehrwertsteuer|Rechnung|Gesamtbetrag)\b", re.I), "EUR"),
    (re.compile(r"\b(?:TVA|Facture|Montant)\b", re.I), "EUR"),
    (re.compile(r"\b(?:IVA|Importe|Factura)\b", re.I), "EUR"),
    (re.compile(r"\b(?:EIN|Sales\s*Tax)\b", re.I), "USD"),
    (re.compile(r"\bVAT\s*(?:Reg(?:istration)?\.?\s*)?(?:No|Number)?\b.{0,4}\bGB", re.I), "GBP"),
    (re.compile(r"\b(?:HST|GST/HST|QST|PST)\b"), "CAD"),
)


@dataclass
class CurrencyDetection:
    code: str
    confidence: float
    evidence: str = ""
    candidates: tuple[str, ...] = field(default_factory=tuple)


def detect_currency(text: str, *, default: str = DEFAULT_CURRENCY) -> CurrencyDetection:
    """Identify the currency of a receipt.

    An explicit ISO code is decisive. A unique symbol is nearly as good. A
    shared symbol like ``$`` narrows it to a family and is then disambiguated
    by locale markers, falling back to the most common member.
    """
    text = text or ""

    code_match = _CODE_RE.search(text.upper())
    if code_match:
        return CurrencyDetection(code_match.group(1), 0.98, f"code {code_match.group(1)}")

    # Collect every symbol sitting against a number, then prefer one that
    # identifies a single currency over a shared one like "$".
    seen: list[str] = []
    for match in _SYMBOL_RE.finditer(text):
        symbol = match.group(1) or match.group(2)
        if symbol and symbol not in seen:
            seen.append(symbol)

    unambiguous = [s for s in seen
                   if len([c for c in CURRENCIES.values() if s in c.symbols]) == 1
                   and s not in AMBIGUOUS_SYMBOLS]
    if unambiguous:
        symbol = unambiguous[0]
        owner = next(c.code for c in CURRENCIES.values() if symbol in c.symbols)
        return CurrencyDetection(owner, 0.95, f"symbol {symbol}")

    if seen:
        symbol = seen[0]
        owners = [c.code for c in CURRENCIES.values() if symbol in c.symbols]
        family = AMBIGUOUS_SYMBOLS.get(symbol, tuple(owners))
        for pattern, code in _LOCALE_HINTS:
            if code in family and pattern.search(text):
                return CurrencyDetection(code, 0.85,
                                         f"symbol {symbol} + locale marker",
                                         tuple(family))
        return CurrencyDetection(family[0], 0.60, f"ambiguous symbol {symbol}",
                                 tuple(family))

    for pattern, code in _LOCALE_HINTS:
        if pattern.search(text):
            return CurrencyDetection(code, 0.70, "locale marker")

    return CurrencyDetection(default, 0.30, "default")


# ------------------------------------------------------------------- parsing
_NUMERIC_RE = re.compile(r"[\d.,   ']+")


def parse_amount(raw: str | None) -> Decimal | None:
    """Parse an amount written in any common separator convention.

    The decimal separator is identified structurally rather than by locale:
    whichever of ``.`` or ``,`` appears last, and is followed by one or two
    digits, is the decimal point. Three trailing digits mean grouping.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    for symbol in ALL_SYMBOLS:
        text = text.replace(symbol, " ")
    text = re.sub(r"[^\d.,   ']", " ", text)
    text = text.replace(" ", " ").replace(" ", " ").replace("'", "")
    text = text.strip()
    if not text:
        return None

    digits_only = re.sub(r"[.,\s]", "", text)
    if not digits_only.isdigit():
        return None

    last_dot, last_comma = text.rfind("."), text.rfind(",")
    separator_at = max(last_dot, last_comma)

    if separator_at == -1:
        whole, frac = digits_only, ""
    else:
        tail = re.sub(r"\D", "", text[separator_at + 1:])
        # One or two trailing digits => decimal point. Three => grouping.
        # A space is never a decimal separator.
        if 1 <= len(tail) <= 2 and text[separator_at] in ".,":
            whole = re.sub(r"\D", "", text[:separator_at])
            frac = tail
        else:
            whole, frac = digits_only, ""

    try:
        value = Decimal(f"{whole or '0'}.{frac}" if frac else (whole or "0"))
    except InvalidOperation:
        return None
    return -value if negative else value


def symbol_for(code: str | None) -> str:
    currency = CURRENCIES.get((code or "").upper())
    return currency.symbols[0] if currency else ""


def format_amount(value, code: str | None = None) -> str:
    """Render an amount with its symbol, grouped Western-style."""
    if value is None:
        return ""
    currency = CURRENCIES.get((code or "").upper())
    decimals = currency.decimals if currency else 2
    try:
        number = f"{Decimal(str(value)):,.{decimals}f}"
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{symbol_for(code)}{number}" if currency else number


def date_order_for(code: str | None) -> str:
    currency = CURRENCIES.get((code or "").upper())
    return currency.date_order if currency else "DMY"


def is_known(code: str | None) -> bool:
    return (code or "").upper() in CURRENCIES
