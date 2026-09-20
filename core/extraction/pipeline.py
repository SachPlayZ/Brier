"""The extraction orchestrator.

    text -> candidates -> line-model re-rank -> reconciliation -> scoring

Reconciliation runs *before* final scoring because the ``arith`` component and
the ``repaired:`` penalty both depend on the cross-field outcome, and both feed
back into every amount field's confidence.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from core.currency import date_order_for, detect_currency
from core.extraction import confidence as C
from core.extraction import fields as F
from core.extraction import reasoning as R
from core.extraction.linemodel import LineRoleModel
from core.extraction.vendors import VendorGazetteer
from core.normalize import normalize_text, split_lines
from core.ocr import load_text, text_from_string
from core.types import Candidate, ExtractionResult, FieldResult, OcrText

#: Order matters -- gstin and subtotal are extracted before the fields whose
#: validators and rate checks depend on them.
FIELD_ORDER = ("gstin", "vendor", "invoice_no", "date", "subtotal",
               "cgst", "sgst", "igst", "cess", "tax_total", "round_off", "total")

AMOUNT_FIELDS = ("subtotal", "cgst", "sgst", "igst", "cess",
                 "tax_total", "round_off", "total")


def extract_receipt(
    source: str | Path | OcrText,
    *,
    gazetteer: VendorGazetteer | None = None,
    line_model: LineRoleModel | None = None,
    today: dt.date | None = None,
    noisy: bool = False,
) -> ExtractionResult:
    """Extract every field from one receipt.

    ``source`` may be an :class:`OcrText`, a path to a text/image file, or a raw
    string of receipt text -- so the same function serves the web upload path,
    the batch ingest and the test suite.
    """
    ocr = _as_ocr_text(source)
    text = normalize_text(ocr.text)
    lines = split_lines(text)

    # Rebuild char confidences against the normalized text. Normalization only
    # collapses whitespace, so a proportional remap keeps spans meaningful.
    ocr = _remap_conf(ocr, text)

    # Currency first: it decides how ambiguous dates are read (03/04/2025 is
    # 3 April on a euro receipt and 4 March on a US one) and how amounts are
    # displayed downstream.
    detection = detect_currency(text)
    date_order = date_order_for(detection.code)

    line_probs = line_model.predict_proba(lines) if line_model is not None else None
    ctx = C.ScoringContext(text=text, lines=lines, ocr=ocr, gazetteer=gazetteer,
                           line_probs=line_probs, noise_penalty=noisy,
                           date_order=date_order)

    candidates: dict[str, list[Candidate]] = {}
    result_fields: dict[str, FieldResult] = {}

    for name in FIELD_ORDER:
        cands = _candidates_for(name, text, lines, ctx, gazetteer, today)
        cands = _rerank(name, cands, ctx)
        candidates[name] = cands
        result_fields[name] = _pick(name, cands)
        ctx.fields = result_fields

    # Each amount was picked on its own. Now ask whether the set can be true (a total equal to the
    # GST cannot), and take the combination that can.
    expected_amount = F.rate_times_quantity(text)
    R.resolve(result_fields, candidates, expected_amount=expected_amount)

    arithmetic_ok, warnings = F.reconcile_amounts(result_fields)
    if arithmetic_ok is None:
        # No subtotal or tax to reconcile (a pump slip): rate x volume is the arithmetic.
        arithmetic_ok = F.reconcile_rate_quantity(text, result_fields,
                                                  candidates.get("total", []), warnings)
    # The repairs above can fill a field from the others. Whatever they did, an impossible set is
    # never returned: check the rules once more and, if anything changed, judge the sums again.
    if R.resolve(result_fields, candidates, expected_amount=expected_amount):
        arithmetic_ok = R.arithmetic(
            {n: (fr.normalized if isinstance(fr.normalized, Decimal) else None)
             for n, fr in result_fields.items() if n in R.NAMES},
            result_fields["round_off"].normalized
            if isinstance(result_fields.get("round_off") and result_fields["round_off"].normalized, Decimal)
            else Decimal("0"),
            expected_amount)
    ctx.arithmetic_ok = arithmetic_ok

    # Second pass: now that reconciliation has run, score every field for real.
    for name in FIELD_ORDER:
        fr = result_fields[name]
        if fr.normalized is None:
            fr.confidence = 0.0
            fr.needs_review = True
            continue
        winner = _as_candidate(fr)
        uniq = C.uniqueness(candidates[name], winner)
        _conf, comps = C.score_field(name, winner, ctx, uniq=uniq)
        fr.components = comps
        C.finalize_confidence(name, fr, ctx)

    result = ExtractionResult(
        fields=result_fields,
        arithmetic_ok=arithmetic_ok,
        warnings=warnings,
        meta={"ocr_source": ocr.source, "n_lines": len(lines), "n_chars": len(text),
              "currency": detection.code,
              "currency_confidence": round(detection.confidence, 3),
              "currency_evidence": detection.evidence,
              "date_order": date_order},
    )
    result.currency = detection.code
    result.doc_confidence = C.doc_confidence(result_fields, arithmetic_ok)

    # Feed the gazetteer's GSTIN memory so later receipts resolve exactly.
    if gazetteer is not None:
        gstin = result.get("gstin")
        vendor = result.get("vendor")
        if gstin and vendor and F.gstin_checksum_ok(str(gstin)):
            gazetteer.learn_gstin(str(gstin), str(vendor))
    return result


def extract_batch(sources: Iterable[str | Path | OcrText], **kwargs) -> list[ExtractionResult]:
    return [extract_receipt(src, **kwargs) for src in sources]


def extract_receipt_id(receipt_id: str, *, data_dir: Path,
                       prefer: str = "sidecar", **kwargs) -> ExtractionResult:
    """Convenience wrapper used by the ingest and evaluate commands."""
    return extract_receipt(load_text(receipt_id, data_dir=data_dir, prefer=prefer), **kwargs)


# --------------------------------------------------------------------- helpers
def _candidates_for(name: str, text: str, lines: list[str],
                    ctx: C.ScoringContext, gaz: VendorGazetteer | None,
                    today: dt.date | None) -> list[Candidate]:
    if name == "gstin":
        return F.extract_gstin(text, lines)
    if name == "vendor":
        gstin = ctx.fields.get("gstin")
        return F.extract_vendor(lines, gaz,
                                str(gstin.normalized) if gstin and gstin.normalized else None)
    if name == "invoice_no":
        gstin = ctx.fields.get("gstin")
        return F.extract_invoice_no(
            text, lines,
            gstin=str(gstin.normalized) if gstin and gstin.normalized else None,
            today=today)
    if name == "date":
        return F.extract_date(text, lines, today=today,
                             date_order=ctx.date_order)
    return F.extract_amount_field(text, lines, name)


def _rerank(name: str, cands: list[Candidate], ctx: C.ScoringContext) -> list[Candidate]:
    """Blend each candidate's own rank with its provisional confidence.

    This is where the line-role model earns its keep: it breaks ties that regex
    specificity alone gets wrong (the classic being "Total Qty: 3" outranking
    "Total: 1,234.00" when the label regex matches both).
    """
    if not cands:
        return cands
    rescored: list[Candidate] = []
    for cand in cands:
        provisional, _comps = C.score_field(name, cand, ctx, uniq=1.0)
        rescored.append(Candidate(cand.value, cand.normalized, cand.span,
                                  cand.line_no, cand.pattern_id,
                                  rank_score=0.5 * cand.rank_score + 0.5 * provisional))
    rescored.sort(key=lambda c: -c.rank_score)
    return rescored


def _pick(name: str, cands: list[Candidate]) -> FieldResult:
    fr = FieldResult(name=name, candidates=cands)
    if not cands:
        fr.needs_review = True
        return fr
    best = cands[0]
    fr.value = best.value
    fr.normalized = best.normalized
    fr.span = best.span
    fr.line_no = best.line_no
    fr.pattern_id = best.pattern_id
    return fr


def _as_candidate(fr: FieldResult) -> Candidate:
    """Rebuild a candidate from a (possibly repaired) field result."""
    return Candidate(value=fr.value or "", normalized=fr.normalized,
                     span=fr.span or (0, 0), line_no=fr.line_no or 0,
                     pattern_id=fr.pattern_id, rank_score=1.0)


def _as_ocr_text(source: str | Path | OcrText) -> OcrText:
    if isinstance(source, OcrText):
        return source
    if isinstance(source, Path):
        return _from_path(source)
    text = str(source)
    if not text.strip() or "\n" in text or len(text) > 260:
        return text_from_string(text)
    try:
        candidate = Path(text)
        if candidate.is_file():
            return _from_path(candidate)
    except (OSError, ValueError):
        pass
    return text_from_string(text)


def _from_path(path: Path) -> OcrText:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        from core.ocr import is_ocr_available, ocr_image

        sidecar = path.parent.parent / "receipts_text" / f"{path.stem}.txt"
        if sidecar.exists():
            return text_from_string(sidecar.read_text(encoding="utf-8"))
        if is_ocr_available():
            return ocr_image(path)
        raise FileNotFoundError(
            f"{path.name}: no text sidecar at {sidecar} and Tesseract is not installed.")
    return text_from_string(path.read_text(encoding="utf-8"))


def _remap_conf(ocr: OcrText, normalized: str) -> OcrText:
    """Stretch the original per-character confidences over normalized text."""
    if not ocr.char_conf:
        return OcrText(normalized, [], ocr.source)
    if len(ocr.char_conf) == len(normalized):
        return OcrText(normalized, ocr.char_conf, ocr.source)
    src = ocr.char_conf
    n, m = len(normalized), len(src)
    remapped = [src[min(int(i * m / n), m - 1)] for i in range(n)]
    return OcrText(normalized, remapped, ocr.source)


def to_decimal_or_none(value) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
