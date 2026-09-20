"""Per-field candidate generation and hard validators.

Every extractor returns a *list* of candidates rather than one answer. Scoring
(``confidence.py``) picks the winner, and the margin between the best and the
runner-up becomes the ``uniq`` confidence component -- which is only possible
because the losing candidates are kept.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from core.extraction import patterns as P
from core.extraction.brands import find_brand
from core.extraction.vendors import VendorGazetteer
from core.normalize import (
    RUPEE,
    alpha_ratio,
    caps_ratio,
    date_plausible,
    has_legal_suffix,
    line_index_of,
    norm_invoice_no,
    parse_date_multi,
    to_decimal,
)
from core.types import Candidate

_GSTIN_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AMOUNT_MAX = Decimal("10000000")

AMOUNT_PATTERNS: dict[str, re.Pattern[str]] = {
    "total": P.TOTAL,
    "subtotal": P.SUBTOTAL,
    "cgst": P.CGST,
    "sgst": P.SGST,
    "igst": P.IGST,
    "cess": P.CESS,
    "tax_total": P.TAX_TOTAL,
}
_RATE_FIELDS = {"cgst", "sgst", "igst"}


# --------------------------------------------------------------------- amounts
def extract_amount_field(text: str, lines: list[str], field: str) -> list[Candidate]:
    """Candidates for one amount field, best-effort and unranked."""
    out: list[Candidate] = []
    pattern = AMOUNT_PATTERNS.get(field)
    if pattern is not None:
        for m in pattern.finditer(text):
            group = m.lastindex or 1
            raw = m.group(group)
            value = to_decimal(raw)
            if value is None or not (0 <= value < AMOUNT_MAX):
                continue
            span = m.span(group)
            out.append(Candidate(
                value=m.group(0).strip(),
                normalized=value,
                span=span,
                line_no=line_index_of(text, m.start()),
                pattern_id=f"{field}.labelled",
                rank_score=0.5 + _currency_bonus(text, span),
            ))

    if field == "round_off":
        for m in P.ROUND_OFF.finditer(text):
            value = to_decimal(m.group(1))
            if value is None:
                continue
            out.append(Candidate(m.group(0).strip(), value, m.span(1),
                                 line_index_of(text, m.start()),
                                 "round_off.labelled", rank_score=0.5))

    if field == "total":
        out.extend(_amount_label_candidates(text, out))
        if not out:
            out.extend(_positional_total(text, lines))
    return out


def _amount_label_candidates(text: str, existing: list[Candidate]) -> list[Candidate]:
    """"Amount(Rs) : 00590.00" style totals, ranked under an explicit "Total"."""
    seen = {c.span for c in existing}
    out: list[Candidate] = []
    for m in P.TOTAL_AMOUNT_LABEL.finditer(text):
        raw = m.group(m.lastindex or 1)
        value = to_decimal(raw)
        span = m.span(m.lastindex or 1)
        if value is None or not (0 < value < AMOUNT_MAX) or span in seen:
            continue
        seen.add(span)
        out.append(Candidate(m.group(0).strip(), value, span, line_index_of(text, m.start()),
                             "total.amount_label", rank_score=0.4 + _currency_bonus(text, span)))
    return out


def _on_non_money_line(text: str, pos: int) -> bool:
    """True when the label in front of ``pos`` names a meter, id, phone, quantity or rate."""
    return bool(P.NON_MONEY_LINE.search(text[text.rfind("\n", 0, pos) + 1:pos]))


def _positional_total(text: str, lines: list[str]) -> list[Candidate]:
    """Fallback: the largest 2dp amount in the last third of the receipt."""
    if not lines:
        return []
    cutoff = int(len(text) * 0.66)
    best: Candidate | None = None
    for m in P.ANY_AMOUNT.finditer(text):
        if m.start() < cutoff or _on_non_money_line(text, m.start()):
            continue
        value = to_decimal(m.group(1))
        if value is None or value <= 0:
            continue
        if best is None or value > best.normalized:
            best = Candidate(m.group(0).strip(), value, m.span(1),
                             line_index_of(text, m.start()), "total.positional",
                             rank_score=0.3)
    if best is not None:
        return [best]

    amounts = [(to_decimal(m.group(1)), m) for m in P.ANY_AMOUNT.finditer(text)
               if not _on_non_money_line(text, m.start())]
    amounts = [(v, m) for v, m in amounts if v is not None]
    if not amounts:
        return []
    value, m = max(amounts, key=lambda vm: vm[0])
    return [Candidate(m.group(0).strip(), value, m.span(1),
                      line_index_of(text, m.start()), "total.max", rank_score=0.1)]


def _currency_bonus(text: str, span: tuple[int, int]) -> float:
    return 0.1 if RUPEE in text[max(0, span[0] - 4): span[0]] else 0.0


def has_currency_marker(text: str, span: tuple[int, int] | None) -> bool:
    """True when a rupee symbol immediately precedes the matched value."""
    if span is None:
        return False
    return RUPEE in text[max(0, span[0] - 4): span[0]]


def parsed_rate(text: str, span: tuple[int, int] | None) -> float | None:
    """Recover the ``@x%`` rate printed near a tax amount, if any."""
    if span is None:
        return None
    window = text[max(0, span[0] - 40): span[1]]
    m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", window)
    return _safe_float(m.group(1)) if m else None


def _safe_float(s: str | None) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------ date
def extract_date(text: str, lines: list[str], *,
                 today: dt.date | None = None,
                 date_order: str = "DMY") -> list[Candidate]:
    """Date candidates, preferring those adjacent to an explicit date label."""
    out: list[Candidate] = []
    label_ends = [m.end() for m in P.DATE_LABEL.finditer(text)]
    seen: set[tuple[int, int]] = set()

    for pattern_name, pattern in P.DATE_PATTERNS:
        for m in pattern.finditer(text):
            if m.span(1) in seen:
                continue
            raw = m.group(1)
            labelled = any(0 <= m.start(1) - end <= 3 for end in label_ends)
            if pattern_name == "d_compact" and not labelled:
                continue      # eight bare digits is far too loose without a label
            parsed = parse_date_multi(raw, today=today, prefer=date_order)
            if parsed is None:
                continue
            date, _fmt_id, ambiguous = parsed
            if not date_plausible(date, today=today):
                continue
            seen.add(m.span(1))
            out.append(Candidate(
                value=raw,
                normalized=date,
                span=m.span(1),
                line_no=line_index_of(text, m.start(1)),
                pattern_id="date.labelled" if labelled else "date.bare",
                rank_score=(0.4 if labelled else 0.0) + (0.0 if ambiguous else 0.1),
            ))
            if ambiguous:
                AMBIGUOUS_SPANS.add((text, m.span(1)))
    return out


# Side table so date ambiguity survives into scoring without widening Candidate.
AMBIGUOUS_SPANS: set[tuple[str, tuple[int, int]]] = set()


def date_is_ambiguous(text: str, span: tuple[int, int] | None) -> bool:
    return span is not None and (text, span) in AMBIGUOUS_SPANS


# ----------------------------------------------------------------------- gstin
def gstin_checksum_ok(g: str | None) -> bool:
    """Official GSTIN mod-36 check-digit validation, plus state-code range."""
    if not g:
        return False
    g = g.upper()
    if len(g) != 15 or not re.fullmatch(r"[0-9A-Z]{15}", g):
        return False
    if not g[:2].isdigit() or not 1 <= int(g[:2]) <= 38:
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        idx = _GSTIN_CHARSET.find(ch)
        if idx < 0:
            return False
        product = idx * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_CHARSET[(36 - total % 36) % 36] == g[14]


def gstin_check_digit(first14: str) -> str:
    """Compute the 15th character for a 14-character GSTIN prefix."""
    total = 0
    for i, ch in enumerate(first14[:14].upper()):
        product = _GSTIN_CHARSET.find(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_CHARSET[(36 - total % 36) % 36]


#: A GSTIN is typed position by position: 2 digits (state), 5 letters (PAN
#: prefix), 4 digits, 1 letter, 1 alphanumeric, a literal 'Z', 1 alphanumeric.
GSTIN_SHAPE = "DDLLLLLDDDDLAZA"          # D digit, L letter, A alphanumeric

#: Glyph pairs OCR actually swaps, both directions.
CONFUSIONS: dict[str, tuple[str, ...]] = {
    "0": ("O", "Q", "D"), "O": ("0",), "Q": ("0",), "D": ("0",),
    "1": ("I", "L"), "I": ("1",), "L": ("1",),
    "5": ("S",), "S": ("5",),
    "8": ("B",), "B": ("8",),
    "2": ("Z",), "Z": ("2",),
    "4": ("A",), "A": ("4",),
    "6": ("G",), "G": ("6",),
    "7": ("T",), "T": ("7",),
}
MAX_GSTIN_CANDIDATES = 4096


def _options_at(ch: str, kind: str) -> list[str]:
    """Characters this position could legitimately hold, original first."""
    if kind == "Z":
        return ["Z"]        # position 13 is a literal 'Z', never anything else
    allowed = (str.isdigit if kind == "D" else
               str.isalpha if kind == "L" else str.isalnum)
    out = [ch] if allowed(ch) else []
    out.extend(alt for alt in CONFUSIONS.get(ch, ()) if allowed(alt) and alt != ch)
    return out or [ch]


def repair_gstin(value: str) -> str | None:
    """Recover a GSTIN from OCR damage, or None if unrecoverable.

    Every position of a GSTIN has a known type, so an OCR confusion is often
    mechanically correctable. Where a position is ambiguous the alternatives
    are searched, and the mod-36 checksum decides -- so a wrong guess cannot
    pass as valid. The search is bounded; a GSTIN with many corrupt characters
    is correctly given up on rather than guessed at.

    A length of 16 or 17 means OCR split a glyph into two (``B1Z7`` read as
    ``B12Z7``), so each single deletion is tried as well.
    """
    value = (value or "").upper()
    if not 15 <= len(value) <= 17:
        return None
    if len(value) > 15:
        for i in range(len(value)):
            recovered = _repair_exact(value[:i] + value[i + 1:])
            if recovered:
                return recovered
        return None
    return _repair_exact(value)


def _repair_exact(value: str) -> str | None:
    """Glyph-confusion search over a 15-character candidate."""
    if len(value) != 15:
        return None
    per_position = [_options_at(ch, kind) for ch, kind in zip(value, GSTIN_SHAPE)]
    total = 1
    for options in per_position:
        total *= len(options)
    if total > MAX_GSTIN_CANDIDATES:
        return None

    from itertools import product

    for combo in product(*per_position):
        candidate = "".join(combo)
        if gstin_checksum_ok(candidate):
            return candidate
    return None


def extract_gstin(text: str, lines: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for pattern_id, pattern in (("gstin.strict", P.GSTIN_STRICT),
                                ("gstin.labelled", P.GSTIN_LABELLED)):
        for m in pattern.finditer(text):
            value = m.group(1).upper()
            if value in seen:
                continue
            seen.add(value)
            valid = gstin_checksum_ok(value)
            pattern_used = pattern_id
            if not valid:
                repaired = repair_gstin(value)
                if repaired and repaired != value:
                    value, valid, pattern_used = repaired, True, "gstin.repaired"
            if not valid and len(value) != 15:
                # The 14-17 char capture exists only to give the repair a
                # chance at a split or dropped glyph. If repair failed, a
                # wrong-length string is certainly not a GSTIN, and asserting
                # it would be worse than reporting nothing.
                continue
            out.append(Candidate(value, value, m.span(1),
                                 line_index_of(text, m.start(1)), pattern_used,
                                 rank_score=0.5 if valid else 0.0))
    return out


# ------------------------------------------------------------------ invoice no
def invoice_no_valid(value: str, *, gstin: str | None = None,
                     today: dt.date | None = None) -> bool:
    """Reject the things that look like invoice numbers but are not."""
    v = (value or "").strip().upper()
    if len(norm_invoice_no(v)) < 3:
        return False
    if v in P.INVOICE_STOPWORDS:
        return False
    if gstin and norm_invoice_no(v) == norm_invoice_no(gstin):
        return False
    if parse_date_multi(v, today=today) is not None:
        return False
    if re.fullmatch(r"[\d,]+\.\d{2}", v):      # a bare currency amount
        return False
    if not re.search(r"\d", v):                # invoice numbers always carry a digit
        return False
    return True


def extract_invoice_no(text: str, lines: list[str], *,
                       gstin: str | None = None,
                       today: dt.date | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for pattern_id, pattern in (("invoice_no.labelled", P.INVOICE_LABELLED),
                                ("invoice_no.formatted", P.INVOICE_FORMATTED)):
        for m in pattern.finditer(text):
            value = m.group(1).strip().upper().rstrip("-/")
            if value in seen or not invoice_no_valid(value, gstin=gstin, today=today):
                continue
            seen.add(value)
            out.append(Candidate(value, value, m.span(1),
                                 line_index_of(text, m.start(1)), pattern_id,
                                 rank_score=0.3 if pattern_id.endswith("labelled") else 0.1))
    return out


# ---------------------------------------------------------------------- vendor
def _vendor_line_rejected(line: str) -> bool:
    if not line or len(line) < 3:
        return True
    if P.STOPLINES.search(line):
        return True
    if P.PHONE.search(line) or P.EMAIL.search(line) or P.URL.search(line):
        return True
    if P.PINCODE.search(line):
        return True
    if alpha_ratio(line) < 0.6:
        return True
    if len(line.split()) > 7:
        return True
    return False


def extract_vendor(lines: list[str], gaz: VendorGazetteer | None,
                   gstin: str | None = None) -> list[Candidate]:
    """Scored search over the header block, with a GSTIN shortcut.

    A known GSTIN resolves the vendor outright (tier S1). Otherwise header lines
    are scored on position, capitalisation, legal suffix and gazetteer fit.
    """
    if gaz is not None and gstin:
        canonical = gaz.by_gstin(gstin)
        if canonical:
            printed = gaz.name_for(canonical) or canonical
            return [Candidate(printed, canonical, (0, 0), 0,
                              "vendor.gstin_lookup", rank_score=1.0)]

    header_idx = [i for i, line in enumerate(lines[:6]) if line.strip()]
    gstin_line = next((i for i, line in enumerate(lines) if P.GSTIN_STRICT.search(line)), None)
    if gstin_line is not None and gstin_line > 0:
        header_idx.append(gstin_line - 1)

    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1

    out: list[Candidate] = []
    for i in sorted(set(header_idx)):
        line = lines[i].strip()
        if _vendor_line_rejected(line):
            continue
        canonical, ratio = (gaz.snap(line) if gaz else (None, 0.5))
        score = (0.35 * (1 - min(i, 5) / 6)
                 + 0.20 * caps_ratio(line)
                 + 0.20 * (1.0 if has_legal_suffix(line) else 0.0)
                 + 0.25 * ratio)
        out.append(Candidate(line, canonical or line,
                             (offsets[i], offsets[i] + len(lines[i])), i,
                             "vendor.header" if canonical else "vendor.fallback",
                             rank_score=score))

    # A brand nobody has listed: on a fuel slip the line above the greeting ("NILGIRI ENERGY /
    # Welcome / SHYAMBAZAR FUEL POINT") is the brand and the fuel-point line under it is the dealer.
    greeted = _greeted_brand(lines, offsets)
    if greeted is not None and find_brand_in(lines, header_idx) is None:
        out.append(greeted)

    # A brand in the header ("IndianOil") is the vendor even when the dealer's own name sits
    # nearer the address: every pump of a brand is one vendor, and the dealer line is the one
    # OCR mangles. Resolved to a known vendor when the gazetteer has one.
    for i in sorted(set(header_idx)):
        brand = find_brand(lines[i])
        if brand is None:
            continue
        canonical = gaz.find_by_words(brand.words) if gaz else None
        out.append(Candidate(lines[i].strip(), canonical or brand.name,
                             (offsets[i], offsets[i] + len(lines[i])), i, "vendor.brand",
                             rank_score=1.0))
        break
    return out


_GREETING = re.compile(r"^\W*(?:welcomes?(?:\s+you)?|happy\s+motoring)\W*$", re.I)
_FUEL_DEALER = re.compile(r"\b(?:fuels?|petrol|petroleum|filling\s+station|service\s+station|pump|gas)\b", re.I)


def find_brand_in(lines: list[str], indices) -> object | None:
    return next((b for b in (find_brand(lines[i]) for i in indices) if b is not None), None)


def _greeted_brand(lines: list[str], offsets: list[int]) -> Candidate | None:
    """The name printed above "Welcome" when a fuel dealer line follows it."""
    for i in range(1, min(len(lines), 6)):
        if not _GREETING.match(lines[i].strip()):
            continue
        name = lines[i - 1].strip()
        words = name.split()
        if (len(name) < 3 or not 1 <= len(words) <= 4 or alpha_ratio(name) < 0.85
                or P.PHONE.search(name) or _vendor_line_rejected(name)):
            continue
        if any(_FUEL_DEALER.search(later) for later in lines[i + 1:i + 5]):
            return Candidate(name, name, (offsets[i - 1], offsets[i - 1] + len(lines[i - 1])), i - 1,
                             "vendor.brand", rank_score=0.95)
    return None


def vendor_fuzz(candidate: Candidate, gaz: VendorGazetteer | None) -> float:
    """The ``fuzz`` confidence component for a vendor candidate."""
    if gaz is None or gaz.is_empty:
        return 0.5
    if candidate.pattern_id == "vendor.gstin_lookup":
        return 1.0
    if candidate.pattern_id == "vendor.brand":
        known = isinstance(candidate.normalized, str) and re.fullmatch(r"V\d{3}", candidate.normalized)
        return 1.0 if known else 0.7
    _canonical, ratio = gaz.snap(candidate.value)
    return ratio


# -------------------------------------------------------------- reconciliation
#: India splits GST into components; most jurisdictions print one tax line.
#: ``tax_total`` is the generic one and is used only when no component was
#: found, so the two never double-count.
TAX_COMPONENTS = ("cgst", "sgst", "igst", "cess")
TAX_FIELDS = TAX_COMPONENTS


def tax_sum_of(fields: dict) -> tuple[Decimal, bool]:
    """Total tax and whether it came from the itemised components.

    Falling back to ``tax_total`` is what lets a "Sales Tax" or "VAT" line
    participate in reconciliation. Without it the gap between subtotal and
    total is unexplained, and the repair logic attributes it to CGST -- which
    is arithmetically consistent and completely wrong.
    """
    components = [_val(fields, name) for name in TAX_COMPONENTS]
    found = [v for v in components if v is not None]
    if found:
        return sum(found, Decimal("0")), True
    generic = _val(fields, "tax_total")
    return (generic, False) if generic is not None else (Decimal("0"), False)


def amount_tolerance(total: Decimal | None) -> Decimal:
    """Absolute tolerance for the arithmetic check: 2 paise or 0.5%."""
    if total is None:
        return Decimal("0.02")
    return max(Decimal("0.02"), (total * Decimal("0.005")).quantize(Decimal("0.01")))


def reconcile_amounts(fields: dict) -> tuple[bool | None, list[str]]:
    """Check ``subtotal + taxes + round_off == total``, repairing one gap.

    When exactly one component is missing, it is solved for algebraically and
    marked ``repaired:<field>`` -- which carries both a lower specificity tier
    and a multiplicative confidence penalty, so a repaired value never
    masquerades as a directly-read one. Returns ``None`` when there is not
    enough information to judge.
    """
    warnings: list[str] = []

    def val(name: str) -> Decimal | None:
        fr = fields.get(name)
        return fr.normalized if fr is not None and isinstance(fr.normalized, Decimal) else None

    subtotal, total = val("subtotal"), val("total")
    taxes = {name: val(name) for name in TAX_COMPONENTS}
    round_off = val("round_off") or Decimal("0")
    tax_sum, itemised = tax_sum_of(fields)

    cgst, sgst = taxes["cgst"], taxes["sgst"]
    if cgst is not None and sgst is not None and abs(cgst - sgst) > Decimal("0.02"):
        warnings.append("cgst_sgst_mismatch")
        for name in ("cgst", "sgst"):
            fields[name].warnings.append("cgst_sgst_mismatch")

    if subtotal is not None and total is not None:
        if abs(total - (subtotal + tax_sum + round_off)) <= amount_tolerance(total):
            return True, warnings

        # A currency glyph misread as a digit inflates the total by an order of
        # magnitude ("Rs 18,554.00" -> "118,554.00"). Arithmetic is the oracle:
        # if dropping the leading digit makes the bill balance, that is what
        # happened. Checked before any repair, because otherwise the repair
        # would be fitted to the corrupted total.
        fixed = _strip_leading_glyph_digit(total, subtotal + tax_sum + round_off,
                                           subtotal)
        if fixed is not None:
            _force(fields, "total", fixed, warnings, "repaired:currency_glyph")
            total = fixed
            if abs(total - (subtotal + tax_sum + round_off)) <= amount_tolerance(total):
                return True, warnings

        # The other common OCR loss: the decimal point. "250.00" reads as "25000".
        restored = _restore_lost_decimal(total, subtotal + tax_sum + round_off)
        if restored is not None:
            _force(fields, "total", restored, warnings, "repaired:lost_decimal")
            return True, warnings

        gap = total - subtotal - tax_sum - round_off
        if gap > 0:
            # Attribute an unexplained gap to the generic tax line unless the
            # receipt itself is itemising Indian GST. Filling CGST on a receipt
            # that never mentioned GST invents a jurisdiction.
            missing = [n for n in TAX_COMPONENTS if taxes[n] is None]
            if itemised and missing:
                target = "igst" if missing == ["igst"] else missing[0]
            else:
                target = "tax_total"
            if _set_repaired(fields, target, gap, warnings):
                return True, warnings
        return False, warnings

    if total is not None and subtotal is None and tax_sum:
        _set_repaired(fields, "subtotal", total - tax_sum - round_off, warnings)
        return True, warnings
    if subtotal is not None and total is None:
        _set_repaired(fields, "total", subtotal + tax_sum + round_off, warnings)
        return True, warnings

    return None, warnings


_RATE_WORD = re.compile(r"\brate\b", re.I)
_QTY_WORD = re.compile(r"\b(?:volume|qty|quantity|litres?|ltrs?)\b", re.I)
_LINE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _number_on_line(text: str, word: re.Pattern[str]) -> Decimal | None:
    """The value printed on the first line that names ``word`` and carries a number.

    The LAST number on the line: OCR turns the rupee glyph of "Rate(₹/L)" into a digit
    ("Rate(2/L) 8B 92.76"), and the first number would then be that 2.
    """
    for line in text.splitlines():
        if not word.search(line):
            continue
        numbers = _LINE_NUMBER.findall(line)
        decimals = [n for n in numbers if "." in n]
        picked = (decimals or numbers or [None])[-1]
        if picked:
            return to_decimal(picked)
    return None


def rate_times_quantity(text: str) -> Decimal | None:
    """Expected payable from a "Rate ... Volume/Qty ..." pair, when the receipt prints both.

    A pump slip has no subtotal or tax to reconcile, so this is its arithmetic: the amount is
    the rate times the volume, give or take the rupee the pump rounds away.
    """
    rate, qty = _number_on_line(text, _RATE_WORD), _number_on_line(text, _QTY_WORD)
    if rate is None or qty is None or rate <= 0 or qty <= 0:
        return None
    return (rate * qty).quantize(Decimal("0.01"))


def reconcile_rate_quantity(text: str, fields: dict, candidates: list[Candidate],
                            warnings: list[str]) -> bool | None:
    """Check (and if needed fix) the total against rate x quantity. ``None`` = cannot judge.

    Three outcomes: the chosen total already fits (arithmetic is fine); another candidate fits
    and the chosen one does not (switch to it); no real total was found at all, only a
    positional guess or nothing (derive it, marked ``repaired`` so it carries a penalty).
    """
    expected = rate_times_quantity(text)
    if expected is None:
        return None
    tolerance = max(Decimal("1.00"), expected * Decimal("0.005"))
    fr = fields.get("total")
    current = fr.normalized if fr is not None and isinstance(fr.normalized, Decimal) else None
    if current is not None and abs(current - expected) <= tolerance:
        return True
    for cand in candidates:
        if isinstance(cand.normalized, Decimal) and abs(cand.normalized - expected) <= tolerance:
            fr.value, fr.normalized, fr.span = cand.value, cand.normalized, cand.span
            fr.line_no, fr.pattern_id = cand.line_no, cand.pattern_id
            return True
    weak = current is None or (fr.pattern_id or "").startswith(("total.positional", "total.max"))
    if weak and _set_repaired(fields, "total", expected, warnings):
        return True
    return None


#: Highest believable effective tax rate: GST tops out at 28%, plus cess.
MAX_TAX_FRACTION = Decimal("0.45")


def repair_is_plausible(name: str, value: Decimal, fields: dict) -> bool:
    """Sanity-check a value solved for algebraically before accepting it.

    Without this, reconciliation will happily invent a tax of 102,822 on a
    subtotal of 15,723 purely to make a misread total balance -- and then
    report ``arithmetic_ok=True``, which is far worse than reporting a
    mismatch. A repair that implies an absurd tax rate is evidence that some
    *other* field is wrong, not that this one was missing.
    """
    if value is None or value < 0:
        return False
    subtotal = _val(fields, "subtotal")
    total = _val(fields, "total")

    if name in TAX_COMPONENTS or name == "tax_total":
        base = subtotal if subtotal is not None else total
        if base is None or base <= 0:
            return False
        # CGST and SGST are equal by law. A solved-for half that disagrees with the half that
        # was read means a third amount (usually a lost round-off line) is off, not that this
        # half is different, so leave it empty rather than invent a wrong figure.
        twin = {"cgst": "sgst", "sgst": "cgst"}.get(name)
        other = _val(fields, twin) if twin else None
        if other is not None and abs(value - other) > Decimal("0.02"):
            return False
        return value <= base * MAX_TAX_FRACTION
    if name == "subtotal":
        if total is not None and not (0 < value <= total):
            return False
        # A subtotal whose printed GST is under 1% of it means the total it was solved from is
        # inflated (a lost decimal point: 250.00 read as 25000), not that the bill is untaxed.
        tax_sum, itemised = tax_sum_of(fields)
        return not (itemised and tax_sum > 0 and tax_sum < value * Decimal("0.01"))
    if name == "total":
        return subtotal is None or value >= subtotal
    return True


def _val(fields: dict, name: str) -> Decimal | None:
    fr = fields.get(name)
    return fr.normalized if fr is not None and isinstance(fr.normalized, Decimal) else None


#: A total can exceed its taxable value by the GST rate plus cess, never more.
#: Anything beyond this multiple of the subtotal is not a real total.
MAX_TOTAL_MULTIPLE = Decimal("1.5")


def _strip_leading_glyph_digit(total: Decimal, expected: Decimal,
                               subtotal: Decimal | None = None) -> Decimal | None:
    """Drop a spurious leading digit from ``total`` when the arithmetic says so.

    A currency glyph misread as a digit multiplies the total by roughly ten.
    Two tests, in order of strength: the stripped value reconciles exactly, or
    -- when some taxes were too mangled to read and an exact check is
    impossible -- the original is impossibly large relative to the subtotal
    while the stripped value falls in the only band GST allows.
    """
    digits = f"{int(total)}"
    if len(digits) < 2 or total <= 0:
        return None
    candidate = Decimal(digits[1:] or "0") + (total - int(total))
    if candidate <= 0:
        return None

    if expected > 0 and total > expected:
        tolerance = max(Decimal("0.02"), expected * Decimal("0.005"))
        if abs(candidate - expected) <= tolerance:
            return candidate

    if subtotal is not None and subtotal > 0:
        impossible = total > subtotal * MAX_TOTAL_MULTIPLE
        plausible = subtotal <= candidate <= subtotal * MAX_TOTAL_MULTIPLE
        if impossible and plausible:
            return candidate
    return None


def _restore_lost_decimal(total: Decimal, expected: Decimal) -> Decimal | None:
    """Put a lost decimal point back when doing so is what balances the bill.

    A whole-number total far above what the other amounts add up to, whose hundredth *does*
    balance, was almost certainly printed with paise. Needs the reconciliation to agree, so a
    genuine 25,000 total on a 25,000 bill is never touched.
    """
    if expected <= 0 or total != total.to_integral_value() or total < expected * 5:
        return None
    candidate = (total / 100).quantize(Decimal("0.01"))
    tolerance = max(Decimal("0.02"), expected * Decimal("0.005"))
    return candidate if abs(candidate - expected) <= tolerance else None


def _set_repaired(fields: dict, name: str, value: Decimal | None,
                  warnings: list[str]) -> bool:
    """Fill a missing field from the others. Returns whether it was accepted."""
    if value is None or value < 0:
        return False
    if not repair_is_plausible(name, value, fields):
        warnings.append(f"implausible_repair:{name}")
        return False
    _force(fields, name, value, warnings, f"repaired:{name}")
    return True


def _force(fields: dict, name: str, value: Decimal, warnings: list[str],
           warning: str) -> None:
    from core.types import FieldResult

    fr = fields.get(name) or FieldResult(name=name)
    fr.normalized = value.quantize(Decimal("0.01"))
    fr.value = str(fr.normalized)
    fr.pattern_id = f"{name}.derived"
    fr.warnings.append(warning)
    fields[name] = fr
    warnings.append(warning)
