"""Synthetic dataset generation with labelled duplicate injection.

Deterministic for a given seed -- running twice produces byte-identical CSVs,
which is what makes the evaluation numbers comparable across code changes.

The duplicate plan deliberately includes *hard negatives*: pairs that look
duplicate-ish (same vendor, same day) but are genuinely distinct bills. Without
them, precision is trivially 1.0 and the whole dedup metric is meaningless.
"""
from __future__ import annotations

import csv
import datetime as dt
import random
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from core.datagen.render import load_font, render_receipt
from core.datagen.templates import ReceiptData, build_receipt, render_lines, retype_vendor
from core.datagen.vendors_seed import VENDORS, vendor_rows

DEFAULT_DUP_PLAN: dict[str, float] = {
    "photo_reuse": 0.06,       # same image, re-submitted
    "retyped": 0.04,           # same bill, different spelling/format/rounding
    "split": 0.02,             # one bill entered as several claims
    "resubmit_after_reject": 0.02,
    "hard_negative": 0.01,     # looks similar, genuinely different -- label 0
}

N_EMPLOYEES = 25
STATUSES = ["SUBMITTED", "UNDER_REVIEW", "APPROVED", "REJECTED"]


@dataclass
class GeneratedReceipt:
    receipt_id: str
    data: ReceiptData
    lines: list[tuple[str, str]]
    noise: dict
    printed_vendor: str
    image_path: str
    text_path: str


@dataclass
class GeneratedClaim:
    claim_id: str
    employee_id: str
    receipt_id: str
    submitted_at: dt.datetime
    status: str
    claimed_amount: Decimal
    category: str
    duplicate_of: str = ""
    dup_type: str = ""
    decided_at: dt.datetime | None = None


@dataclass
class Dataset:
    receipts: list[GeneratedReceipt] = field(default_factory=list)
    claims: list[GeneratedClaim] = field(default_factory=list)
    pairs: list[tuple[str, str, str, int]] = field(default_factory=list)


def generate_dataset(
    out_dir: Path,
    n_receipts: int = 400,
    n_claims: int = 500,
    seed: int = 42,
    dup_plan: dict[str, float] | None = None,
    *,
    render_images: bool = True,
    progress=None,
) -> Dataset:
    """Generate images, sidecar text and all four CSVs under ``out_dir``."""
    rng = random.Random(seed)
    plan = {**DEFAULT_DUP_PLAN, **(dup_plan or {})}
    out_dir = Path(out_dir)
    images_dir = out_dir / "receipts_images"
    text_dir = out_dir / "receipts_text"
    images_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    font = load_font()
    employees = [f"E{index:03d}" for index in range(1, N_EMPLOYEES + 1)]
    start = dt.date(2025, 1, 6)
    dataset = Dataset()
    used_invoices: set[str] = set()

    # Every honest claim must own a distinct receipt. If they shared one, the
    # corpus would contain real duplicates that the truth file does not label,
    # and the detector would be scored as wrong for correctly finding them.
    n_dup_claims = int(n_claims * sum(plan.values()))
    n_base = max(n_claims - n_dup_claims, 1)
    n_receipts = max(n_receipts, n_base)

    # ---- base receipts -----------------------------------------------------
    for i in range(1, n_receipts + 1):
        vendor = rng.choice(VENDORS)
        date = start + dt.timedelta(days=rng.randrange(0, 240))
        invoice_no = _invoice_no(vendor.canonical_id, rng, used_invoices)
        data = build_receipt(vendor, invoice_no, date, rng)
        receipt = _emit(f"R{i:05d}", data, rng, images_dir, text_dir, font,
                        noise_tier=_pick_tier(rng), render_images=render_images)
        dataset.receipts.append(receipt)
        if progress and i % 50 == 0:
            progress(f"receipts {i}/{n_receipts}")

    by_id = {r.receipt_id: r for r in dataset.receipts}

    # ---- honest claims -----------------------------------------------------
    for i in range(1, n_base + 1):
        receipt = dataset.receipts[i - 1]
        dataset.claims.append(_claim(f"C{i:05d}", rng.choice(employees), receipt, rng))

    next_index = len(dataset.claims) + 1
    originals = list(dataset.claims)

    # ---- injected duplicates ----------------------------------------------
    counts = {name: int(n_claims * share) for name, share in plan.items()}

    for _ in range(counts.get("photo_reuse", 0)):
        source = rng.choice(originals)
        original = by_id[source.receipt_id]
        if rng.random() < 0.5:
            # A genuine re-scan: same physical receipt, fresh scan noise, so the
            # file bytes differ and only the perceptual hash can connect them.
            target = _rescan(original, f"R6{next_index:04d}", rng, images_dir,
                             text_dir, font, render_images)
            dataset.receipts.append(target)
            by_id[target.receipt_id] = target
        else:
            target = original
        claim = _claim(f"C{next_index:05d}",
                       _maybe_other(source.employee_id, employees, rng, 0.25),
                       target, rng, after=source.submitted_at)
        claim.duplicate_of, claim.dup_type = source.claim_id, "PHOTO_REUSE"
        dataset.claims.append(claim)
        dataset.pairs.append((source.claim_id, claim.claim_id, "PHOTO_REUSE", 1))
        next_index += 1

    for _ in range(counts.get("retyped", 0)):
        source = rng.choice(originals)
        original = by_id[source.receipt_id]
        clone = _retype(original, f"R9{next_index:04d}", rng, images_dir, text_dir,
                        font, render_images)
        dataset.receipts.append(clone)
        by_id[clone.receipt_id] = clone
        claim = _claim(f"C{next_index:05d}", source.employee_id, clone, rng,
                       after=source.submitted_at)
        claim.duplicate_of, claim.dup_type = source.claim_id, "RETYPED"
        dataset.claims.append(claim)
        dataset.pairs.append((source.claim_id, claim.claim_id, "RETYPED", 1))
        next_index += 1

    for _ in range(counts.get("split", 0)):
        source = rng.choice(originals)
        whole = by_id[source.receipt_id]
        parts = _split_receipts(whole, next_index, rng, images_dir, text_dir,
                                font, render_images)
        for offset, part in enumerate(parts):
            dataset.receipts.append(part)
            by_id[part.receipt_id] = part
            claim = _claim(f"C{next_index + offset:05d}", source.employee_id, part, rng,
                           after=source.submitted_at)
            claim.duplicate_of, claim.dup_type = source.claim_id, "SPLIT"
            dataset.claims.append(claim)
            dataset.pairs.append((source.claim_id, claim.claim_id, "SPLIT", 1))
        next_index += len(parts)

    for _ in range(counts.get("resubmit_after_reject", 0)):
        source = rng.choice(originals)
        source.status = "REJECTED"
        source.decided_at = source.submitted_at + dt.timedelta(days=rng.randint(2, 6))
        claim = _claim(f"C{next_index:05d}", source.employee_id, by_id[source.receipt_id],
                       rng, after=source.decided_at + dt.timedelta(days=rng.randint(1, 10)))
        claim.duplicate_of, claim.dup_type = source.claim_id, "RESUBMIT_AFTER_REJECT"
        dataset.claims.append(claim)
        dataset.pairs.append((source.claim_id, claim.claim_id, "RESUBMIT_AFTER_REJECT", 1))
        next_index += 1

    # ---- hard negatives: similar-looking but genuinely distinct bills ------
    for _ in range(counts.get("hard_negative", 0)):
        source = rng.choice(originals)
        original = by_id[source.receipt_id]
        twin = _sibling_bill(original, f"R8{next_index:04d}", rng, images_dir,
                             text_dir, font, render_images, used_invoices)
        dataset.receipts.append(twin)
        by_id[twin.receipt_id] = twin
        claim = _claim(f"C{next_index:05d}", source.employee_id, twin, rng,
                       after=source.submitted_at)
        dataset.claims.append(claim)
        dataset.pairs.append((source.claim_id, claim.claim_id, "HARD_NEGATIVE", 0))
        next_index += 1

    _write_csvs(out_dir, dataset)
    return dataset


# ------------------------------------------------------------------ emission
def _emit(receipt_id: str, data: ReceiptData, rng: random.Random,
          images_dir: Path, text_dir: Path, font, *, noise_tier: int,
          render_images: bool, vendor_name: str | None = None,
          date_format: str | None = None) -> GeneratedReceipt:
    lines = render_lines(data, rng, vendor_name=vendor_name, date_format=date_format)
    text = "\n".join(line for line, _role in lines)

    text_path = text_dir / f"{receipt_id}.txt"
    text_path.write_text(text, encoding="utf-8")

    image_path = images_dir / f"{receipt_id}.png"
    noise = {"noise_tier": noise_tier, "rotation_deg": 0.0, "blur": 0.0, "jpeg_q": 0}
    if render_images:
        noise = render_receipt([line for line, _r in lines], image_path,
                               noise_tier=noise_tier, rng=rng, font=font)

    return GeneratedReceipt(
        receipt_id=receipt_id, data=data, lines=lines, noise=noise,
        printed_vendor=vendor_name or data.vendor.name,
        image_path=(Path("receipts_images") / f"{receipt_id}.png").as_posix(),
        text_path=(Path("receipts_text") / f"{receipt_id}.txt").as_posix())


def _rescan(original: GeneratedReceipt, receipt_id: str, rng: random.Random,
            images_dir: Path, text_dir: Path, font,
            render_images: bool) -> GeneratedReceipt:
    """Re-photograph the same receipt: identical content, different pixels."""
    text = "\n".join(line for line, _role in original.lines)
    (text_dir / f"{receipt_id}.txt").write_text(text, encoding="utf-8")

    image_path = images_dir / f"{receipt_id}.png"
    noise = {"noise_tier": 1, "rotation_deg": 0.0, "blur": 0.0, "jpeg_q": 0}
    if render_images:
        noise = render_receipt([line for line, _r in original.lines], image_path,
                               noise_tier=max(original.noise["noise_tier"], 1),
                               rng=rng, font=font)
    return GeneratedReceipt(
        receipt_id=receipt_id, data=original.data, lines=original.lines, noise=noise,
        printed_vendor=original.printed_vendor,
        image_path=(Path("receipts_images") / f"{receipt_id}.png").as_posix(),
        text_path=(Path("receipts_text") / f"{receipt_id}.txt").as_posix())


def _retype(original: GeneratedReceipt, receipt_id: str, rng: random.Random,
            images_dir: Path, text_dir: Path, font, render_images: bool) -> GeneratedReceipt:
    """Same bill, re-entered: different vendor spelling, date format, ±2 rupees."""
    src = original.data
    drift = Decimal(str(rng.choice([-2, -1, -0.5, 0, 0.5, 1, 2])))
    data = ReceiptData(
        vendor=src.vendor, invoice_no=src.invoice_no, date=src.date, items=src.items,
        subtotal=src.subtotal, cgst=src.cgst, sgst=src.sgst, igst=src.igst,
        cess=src.cess, round_off=src.round_off + drift, total=src.total + drift,
        template_id=src.template_id, date_format=src.date_format,
        interstate=src.interstate)
    other_format = rng.choice([f for f in ("%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d", "%d.%m.%Y")
                               if f != src.date_format])
    return _emit(receipt_id, data, rng, images_dir, text_dir, font,
                 noise_tier=_pick_tier(rng), render_images=render_images,
                 vendor_name=retype_vendor(src.vendor.name, rng),
                 date_format=other_format)


def _split_receipts(whole: GeneratedReceipt, index: int, rng: random.Random,
                    images_dir: Path, text_dir: Path, font,
                    render_images: bool) -> list[GeneratedReceipt]:
    """Break one bill's line items into two separate receipts."""
    items = whole.data.items
    if len(items) < 2:
        return []
    cut = max(1, len(items) // 2)
    parts: list[GeneratedReceipt] = []
    for offset, chunk in enumerate((items[:cut], items[cut:])):
        if not chunk:
            continue
        data = build_receipt(whole.data.vendor, f"{whole.data.invoice_no}-{offset + 1}",
                             whole.data.date, rng, interstate=whole.data.interstate)
        data.items = chunk
        subtotal = sum((i.amount for i in chunk), Decimal("0")).quantize(Decimal("0.01"))
        scale = subtotal / whole.data.subtotal if whole.data.subtotal else Decimal("0.5")
        data.subtotal = subtotal
        data.cgst = (whole.data.cgst * scale).quantize(Decimal("0.01"))
        data.sgst = (whole.data.sgst * scale).quantize(Decimal("0.01"))
        data.igst = (whole.data.igst * scale).quantize(Decimal("0.01"))
        data.round_off = Decimal("0.00")
        data.total = data.subtotal + data.cgst + data.sgst + data.igst
        parts.append(_emit(f"R7{index + offset:04d}", data, rng, images_dir, text_dir,
                           font, noise_tier=_pick_tier(rng), render_images=render_images))
    return parts


def _sibling_bill(original: GeneratedReceipt, receipt_id: str, rng: random.Random,
                  images_dir: Path, text_dir: Path, font, render_images: bool,
                  used: set[str]) -> GeneratedReceipt:
    """Same vendor, same day, different bill -- the hard negative."""
    data = build_receipt(original.data.vendor,
                         _invoice_no(original.data.vendor.canonical_id, rng, used),
                         original.data.date, rng)
    return _emit(receipt_id, data, rng, images_dir, text_dir, font,
                 noise_tier=_pick_tier(rng), render_images=render_images)


# -------------------------------------------------------------------- helpers
def _pick_tier(rng: random.Random) -> int:
    return rng.choices([0, 1, 2, 3], weights=[0.45, 0.30, 0.18, 0.07])[0]


def _invoice_no(vendor_id: str, rng: random.Random, used: set[str]) -> str:
    prefix = rng.choice(["INV", "BILL", "TI", "RCPT"])
    while True:
        candidate = f"{prefix}-{rng.randrange(2024, 2026)}-{rng.randrange(1, 99999):05d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _claim(claim_id: str, employee_id: str, receipt: GeneratedReceipt,
           rng: random.Random, *, after: dt.datetime | None = None) -> GeneratedClaim:
    base = dt.datetime.combine(receipt.data.date, dt.time(9, 0))
    submitted = base + dt.timedelta(days=rng.randint(1, 14), hours=rng.randint(0, 9))
    if after is not None and submitted <= after:
        submitted = after + dt.timedelta(days=rng.randint(1, 9))
    status = rng.choices(STATUSES, weights=[0.35, 0.15, 0.40, 0.10])[0]
    decided = (submitted + dt.timedelta(days=rng.randint(1, 8))
               if status in {"APPROVED", "REJECTED"} else None)
    return GeneratedClaim(
        claim_id=claim_id, employee_id=employee_id, receipt_id=receipt.receipt_id,
        submitted_at=submitted, status=status, claimed_amount=receipt.data.total,
        category=receipt.data.vendor.category, decided_at=decided)


def _maybe_other(employee_id: str, employees: list[str], rng: random.Random,
                 probability: float) -> str:
    if rng.random() < probability:
        return rng.choice([e for e in employees if e != employee_id])
    return employee_id


# --------------------------------------------------------------------- output
def _write_csvs(out_dir: Path, dataset: Dataset) -> None:
    out_dir = Path(out_dir)

    with (out_dir / "receipts_truth.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["receipt_id", "image_path", "text_path", "vendor_name",
                         "vendor_canonical", "gstin", "invoice_no", "date",
                         "subtotal", "cgst", "sgst", "igst", "cess", "round_off",
                         "total", "template_id", "noise_tier", "rotation_deg"])
        for r in dataset.receipts:
            d = r.data
            writer.writerow([r.receipt_id, r.image_path, r.text_path, r.printed_vendor,
                             d.vendor.canonical_id, d.vendor.gstin, d.invoice_no,
                             d.date.isoformat(), d.subtotal, d.cgst, d.sgst, d.igst,
                             d.cess, d.round_off, d.total, d.template_id,
                             r.noise["noise_tier"], r.noise["rotation_deg"]])

    with (out_dir / "receipts_lines.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["receipt_id", "line_no", "text", "role"])
        for r in dataset.receipts:
            for line_no, (text, role) in enumerate(r.lines):
                writer.writerow([r.receipt_id, line_no, text, role])

    with (out_dir / "claims.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["claim_id", "employee_id", "receipt_id", "submitted_at",
                         "status", "claimed_amount", "category", "decided_at",
                         "duplicate_of", "dup_type"])
        for c in dataset.claims:
            writer.writerow([c.claim_id, c.employee_id, c.receipt_id,
                             c.submitted_at.isoformat(), c.status, c.claimed_amount,
                             c.category, c.decided_at.isoformat() if c.decided_at else "",
                             c.duplicate_of, c.dup_type])

    with (out_dir / "duplicate_pairs.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["claim_a", "claim_b", "dup_type", "label"])
        for a, b, dup_type, label in dataset.pairs:
            writer.writerow([a, b, dup_type, label])

    with (out_dir / "vendors.csv").open("w", newline="", encoding="utf-8") as fh:
        rows = vendor_rows()
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
