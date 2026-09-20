"""Labelled regexes and their specificity tiers.

Every pattern carries an id; the id maps to a specificity tier which becomes the
``pat`` confidence component. That mapping is the reason a value found via
"Grand Total: Rs 1,234.00" scores higher than the largest number on the page.

``GAP`` deliberately excludes ``\n`` so a label never binds to a value on a
different line -- the single most common source of wrong extractions.
"""
from __future__ import annotations

import re

from core.currency import SYMBOL_CLASS

RUPEE = "\u20b9"

#: A number in any separator convention: 1,234.56 / 1.234,56 / 1 234,56 /
#: 1,23,456.78 / 34,39. Interpretation is left to core.currency.parse_amount --
#: the pattern only has to recognise the shape.
# The grouped form needs at least one separator group (`+`, not `*`). With `*` it also matched a bare
# "1174.00" as "117" (alternation takes the first branch that matches, not the longest), so every
# amount printed without thousands separators lost its last digits.
# The plain form also takes ONE space before a two-digit decimal part: OCR reads "283.00" as "283 .00".
NUM = r"\d{1,3}(?:[.,\s\u00a0]\d{2,3})+(?:[.,]\d{1,2})?|\d+(?:[ ]?[.,]\d{2}(?!\d)|[.,]\d{1,2})?"

#: The symbol may lead ("$12.00") or trail ("12,00 EUR").
CUR = r"(?:" + SYMBOL_CLASS + r")"
# OCR often reads the rupee glyph as a lone digit: "Taxable Val : 2 1572.20". A single "2" and a space
# in front of a decimal amount is that glyph, not part of the number.
STRAY_GLYPH = r"(?:2[ \t]+(?=\d[\d,]*[.,]\d{2}(?!\d)))?"
AMT = STRAY_GLYPH + r"(?:" + CUR + r"\s*)?(" + NUM + r")(?:\s*" + CUR + r")?"

#: The label-to-value gap must not swallow a digit or a currency symbol.
GAP = r"(?:(?!" + CUR + r")[^\d\n]){0,18}"
_F = re.I | re.M

# --- amounts ----------------------------------------------------------------
# OCR drops stray punctuation inside labels ("Amount: Payable"), so the gap
# between label words tolerates it.
W = r"[\s:.]*"

# The lookahead must exclude "before tax": GAP is 18 characters wide, which is
# enough for "total" to skip straight over "Before Tax:" and capture the
# taxable value as the grand total. Wide column spacing hid this on the clean
# renders; OCR collapses the whitespace and exposes it.
# "Total GST", "Total Tax", "Total Discount", "Total Volume" are other fields' labels, never the bill:
# without the lookahead below, "Total GST: 283.00" was read as the total payable. "Sub Total" is the
# subtotal's label, so it is not a total either.
TOTAL = re.compile(
    r"(?<!sub )(?<!sub-)\b(?:grand" + W + r"total|total(?:" + W + r"(?:amount|amt|payable|due))?"
    r"|net" + W + r"(?:amount|payable)|amount" + W + r"(?:payable|due)"
    r"|bill" + W + r"total|balance" + W + r"due"
    # Non-English equivalents seen on euro-zone and Latin-American receipts.
    r"|gesamtbetrag|gesamtsumme|endbetrag|montant" + W + r"total|total" + W + r"ttc"
    r"|importe" + W + r"total|totale|totaal|summa|sum" + W + r"total)\b"
    r"(?!\s*(?:qty|items?|quantity|units|nos|pcs|before\s*tax|excl"
    r"|gst|tax|vat|cgst|sgst|igst|cess|disc|discount|saving|savings|volume|litres?|ltrs?|round|in\s*words))"
    r"\s*[:\-]?" + GAP + AMT, _F)

SUBTOTAL = re.compile(
    r"\b(?:sub\s*-?\s*total|taxable\s*(?:val(?:ue)?|amount|amt)|total\s*before\s*tax"
    r"|(?:net\s*)?amount\s*before\s*tax"
    r"|nettobetrag|nettosumme|zwischensumme|netto|montant" + W + r"ht|total" + W + r"ht"
    r"|base" + W + r"imponible|subtotaal)\b\.?\s*[:\-]?" + GAP + AMT, _F)

# `(?![A-Za-z0-9])` rather than `\b`: underscore is a word character, so a
# trailing `\b` fails on "SGST_@ 9%" -- which is exactly what OCR produces when
# it smears the gap between the label and the rate.
# `SEP` absorbs the underscores and stray punctuation OCR sprinkles between a
# tax label and its rate. Without it the optional rate group fails to match and
# the rate itself ("9") gets captured as the tax amount.
SEP = r"[\s_.,:;|-]*"
# The percent marker is REQUIRED (`%` or the `x` OCR often makes of it). Made
# optional, this group happily reads "SGST: 89.55" as rate 89.5 and amount 5.
_RATE = r"(?:" + SEP + r"[@]?" + SEP + r"(\d{1,2}(?:\.\d+)?)\s*[%x])?"

CGST = re.compile(r"\bC\s?GST(?![A-Za-z0-9])" + _RATE + SEP + GAP + AMT, _F)
SGST = re.compile(r"\b(?:S\s?GST|UTGST)(?![A-Za-z0-9])" + _RATE + SEP + GAP + AMT, _F)
IGST = re.compile(r"\bI\s?GST(?![A-Za-z0-9])" + _RATE + SEP + GAP + AMT, _F)
CESS = re.compile(r"\bCESS\b\s*[:\-]?" + GAP + AMT, _F)
# Generic single-line tax, for every jurisdiction that does not split it the
# way India does. Without this, a "Sales Tax" line is simply not read, and
# reconciliation silently attributes the gap to CGST -- arithmetically right,
# semantically nonsense.
TAX_TOTAL = re.compile(
    # "Total Before Tax" is a SUBTOTAL label. Without these guards the bare
    # "tax" alternative captures the taxable value as if it were the tax.
    r"\b(?<!before )(?<!excluding )(?<!excl )(?<!pre )(?<!net of )"
    r"(?:total" + W + r"(?:gst|tax)|tax" + W + r"amount|gst" + W + r"amount"
    r"|sales" + W + r"tax|use" + W + r"tax|vat|v\.a\.t\.|hst|pst|qst"
    r"|mwst|mehrwertsteuer|ust|umsatzsteuer|tva|iva|btw|moms|imposto"
    r"|tax)(?![A-Za-z])" + _RATE + SEP + r"[:\-]?" + GAP + AMT, _F)
ROUND_OFF = re.compile(
    r"\b(?:round(?:ing)?\s*(?:off|ed)?)\b\s*[:\-]?\s*([+-]?\s?\d+\.?\d{0,2})", _F)

# Any currency-marked or 2dp amount -- used for positional fallbacks only.
# The lookbehinds and the trailing `(?!\d)` keep it off longer numbers: "0001730171.270" (a pump
# meter reading, three decimals) used to match as 1730171.27 and win as the "largest amount".
ANY_AMOUNT = re.compile(
    r"(?:" + CUR + r"\s*)?(?<![\d,])(?<!\d\.)"
    r"(\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2}(?!\d)|\d+[.,]\d{2}(?!\d))"
    r"(?:\s*" + CUR + r")?", _F)

# The payable amount on slips that never print the word "Total": "Amount(Rs) : 00590.00",
# "Sale Amt", "Amount (INR)", "Total Rs.". Separators stay on the line (`[ \t]`, not `\s`), or the
# "Amount" header of an item table would bind to the first row underneath it. It ranks below an
# explicit "Total", and never fires on "Tax Amount", "Taxable Amount" or "Amount before tax".
_CCY_WORD = r"(?:rs\.?|inr|" + RUPEE + r")"
# The label must open its line (optionally "Sale"/"Net"/"Bill" first): "Taxble Amount: 195.98" or
# "Tax Amount: 89.55" then never qualify, however OCR spells the word before it. The number may not
# be glued to a letter, or a mangled label ("Amount Payab1e: 3,417.00") yields the stray "1".
TOTAL_AMOUNT_LABEL = re.compile(
    r"^[ \t]*(?:(?:sale|net|bill|fuel)[ \t]+)?"
    r"(?:(?:amount|amt)(?:[ \t]*\([^)\n]{1,5}\)|[ \t]+" + _CCY_WORD + r"(?![A-Za-z]))?"
    r"|total[ \t]+" + _CCY_WORD + r"(?![A-Za-z])|you[ \t]+pay|amount[ \t]+paid)"
    r"(?![ \t]*(?:before|excl|qty|quantity|tax\b|gst\b))"
    r"[ \t]*[:\-]?" + GAP + r"(?<![A-Za-z])" + AMT, _F)

# A line whose number is never money: pump/nozzle/vehicle numbers, meter readings, ids, phone
# numbers, quantities and unit rates. Applied to the text in front of a number, and only when
# choosing an amount that carries no label of its own.
NON_MONEY_LINE = re.compile(
    r"\b(?:vtrd|vech|veh(?:icle)?|fcc|fip|nozzle|density|tel|ph(?:one)?|mob(?:ile)?|pin(?:code)?"
    r"|inv(?:oice)?|bill\s*no|token|txn|rrn|ref(?:erence)?|auth|card|volume|qty|quantity|litres?|ltrs?"
    r"|rate|odo(?:meter)?|pump|dispenser|batch|serial|id)\b", re.I)

# --- identifiers ------------------------------------------------------------
GSTIN_STRICT = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
# The separator allows '.' -- "GST No.: 27AAF..." is extremely common and a
# bare [:\-] class misses every one of them. The capture is widened to 14-17
# characters so a dropped or inserted glyph can still be repaired downstream;
# the checksum is what ultimately accepts or rejects the value.
GSTIN_LABELLED = re.compile(
    r"\b(?:GSTIN|GST\s*(?:No|Reg(?:n)?\.?\s*No)?|GST\s*ID)[\s:.\-]*([0-9A-Z]{14,17})\b", _F)

# `[\s.]*` after the keyword: "Inv. No", "Inv.No" and "Bill. No" are as common as "Inv No".
INVOICE_LABELLED = re.compile(
    r"\b(?:tax\s*invoice|invoice|inv|bill|receipt|cash\s*memo|memo|doc(?:ument)?)[\s.]*"
    r"(?:no\.?|num(?:ber)?|#|nr|id)\s*[:\-#]?\s*([A-Z0-9][A-Z0-9\-/]{2,24})\b", _F)
INVOICE_FORMATTED = re.compile(r"\b((?:INV|BILL|RCPT|TI)[-/]?[A-Z0-9]{0,4}[-/]?\d{3,8})\b", _F)

# --- dates ------------------------------------------------------------------
DATE_LABEL = re.compile(
    r"\b(?:invoice\s*date|bill\s*date|date\s*of\s*(?:supply|issue)|dated?|dt)\b\s*[:\-]?\s*", _F)

DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("d_ymd", re.compile(r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b")),
    ("d_dmy", re.compile(r"\b(\d{1,2}[-/.]\d{1,2}[-/.](?:\d{4}|\d{2}))\b")),
    ("d_dMy", re.compile(r"\b(\d{1,2}[-\s]?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-,\s]?\s*\d{2,4})\b", re.I)),
    # The separator is deliberately loose: OCR turns "Jan 19, 2025" into
    # "Jan'19, 2025" or "Jan.19, 2025" often enough to matter.
    ("d_Mdy", re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s'`.,\-]{0,3}\d{1,2}(?:st|nd|rd|th)?[,;:.\s]+\d{4})\b", re.I)),
    ("d_compact", re.compile(r"\b(\d{8})\b")),
]

TIME = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\s*(?:AM|PM)?\b", re.I)

# --- vendor header heuristics ----------------------------------------------
STOPLINES = re.compile(
    r"\b(?:TAX\s+INVOICE|GST\s+INVOICE|RETAIL\s+INVOICE|CASH\s+MEMO|RECEIPT"
    r"|ORIGINAL\s+FOR\s+(?:RECIPIENT|BUYER)|DUPLICATE|TRIPLICATE|CUSTOMER\s+COPY"
    r"|THANK\s+YOU|VISIT\s+AGAIN|SUBJECT\s+TO)\b", re.I)
PHONE = re.compile(r"\b[6-9]\d{9}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
PINCODE = re.compile(r"\b\d{6}\b")
LEGAL_SUFFIX = re.compile(
    r"\b(?:PVT\.?\s*LTD|PRIVATE\s+LIMITED|LIMITED|LTD\.?|LLP|&\s*SONS|AND\s+SONS"
    r"|ENTERPRISES?|TRADERS?|STORES?|MART|SUPERMARKET|RESTAURANT|HOTEL|FUELS?"
    r"|PETROLEUM|PHARMACY|MEDICALS?|AGENCIES|DEPARTMENTAL)\b", re.I)

# --- specificity tiers ------------------------------------------------------
# S1 labelled + currency symbol | S2 labelled | S3 keyword nearby/wrapped
# S4 unlabelled positional      | S5 desperation fallback
TIER_SCORES = {"S1": 1.00, "S2": 0.85, "S3": 0.70, "S4": 0.45, "S5": 0.20}

PATTERN_TIERS: dict[str, str] = {
    "total.labelled": "S2", "total.amount_label": "S3", "total.positional": "S4", "total.max": "S5",
    "subtotal.labelled": "S2", "subtotal.derived": "S4",
    "cgst.labelled": "S2", "sgst.labelled": "S2", "igst.labelled": "S2",
    "cess.labelled": "S2", "tax_total.labelled": "S2", "round_off.labelled": "S2",
    "cgst.derived": "S4", "sgst.derived": "S4", "igst.derived": "S4",
    "gstin.strict": "S1", "gstin.labelled": "S2", "gstin.repaired": "S3",
    "invoice_no.labelled": "S2", "invoice_no.formatted": "S3", "invoice_no.bare": "S4",
    "date.labelled": "S2", "date.bare": "S3",
    "vendor.gstin_lookup": "S1", "vendor.brand": "S2", "vendor.header": "S3", "vendor.fallback": "S4",
}


def tier_of(pattern_id: str | None) -> str:
    if not pattern_id:
        return "S5"
    return PATTERN_TIERS.get(pattern_id, "S4")


def specificity(pattern_id: str | None, *, has_currency: bool = False) -> float:
    """Score for the ``pat`` component. A currency symbol promotes S2 to S1."""
    tier = tier_of(pattern_id)
    if tier == "S2" and has_currency:
        tier = "S1"
    return TIER_SCORES[tier]


INVOICE_STOPWORDS = {"GST", "TAX", "ORIGINAL", "DUPLICATE", "TRIPLICATE",
                     "INVOICE", "RECEIPT", "BILL", "MEMO", "NO", "DATE", "COPY"}
