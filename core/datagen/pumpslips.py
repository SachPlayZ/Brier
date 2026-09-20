"""Petrol-pump slips: receipt layouts the main generator does not cover.

A pump slip has no item table. What it has is a column of numbers that are *not* money: pump and
nozzle numbers, a 17-digit FCC id, a vehicle-tester reading ("Vtrd: 0001730171.270") and a meter
number, all zero-padded and several with three decimals. The payable amount hides behind
"Amount(Rs)" rather than "Total", and some slips add a GST block whose "Total GST" line is a TAX,
not the bill. Three layouts, modelled on real slips Brier misread:

    plain   Rate / Volume / Amount only (an Indian Oil slip: the meter reading became the total)
    gst     the above plus Taxable Val / CGST / SGST / Total GST (the tax total became the total)
    vat     the above plus Taxable Val / VAT @17% (Incl)

Some brands are unknown to any list ("NILGIRI ENERGY / Welcome / SHYAMBAZAR FUEL POINT"), and
`artifacts` reproduces what Tesseract really did to these slips: the rupee glyph read as "2", "=",
"%" or "Bs", and a space before the decimal point ("283 .00").

This is a separate set with its own random stream. The main generator shares one RNG across every
receipt, so changing its fuel layout would change all of them, the seeded database, the trained
line model and the published accuracy numbers.
"""
from __future__ import annotations

import csv
import datetime as dt
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from core.datagen.vendors_seed import STATE_CODES, _gstin
from core.extraction.fields import gstin_check_digit

CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Brand:
    canonical_id: str
    name: str                      # the vendor as the gazetteer knows it
    printed: tuple[str, ...]       # how the brand appears in the slip header
    city: str


#: The first three exist in the main dataset's vendor table; the last two are new here.
BRANDS = (
    Brand("V008", "Indian Oil Fuel Station", ("IndianOil", "Indian Oil", "INDIAN OIL", "IndianOil"), "Chennai"),
    Brand("V003", "Bharat Petroleum Outlet", ("Bharat Petroleum", "BHARAT PETROLEUM", "BPCL"), "Mumbai"),
    Brand("V017", "Shell Select Fuel Point", ("Shell", "SHELL"), "Bengaluru"),
    Brand("V021", "Hindustan Petroleum Outlet", ("HP", "Hindustan Petroleum", "HINDUSTAN PETROLEUM"), "Kolkata"),
    Brand("V022", "Nayara Energy Outlet", ("Nayara Energy", "NAYARA ENERGY"), "Ahmedabad"),
)

#: Brands on no list. The vendor is whatever is printed above the greeting.
UNKNOWN_BRANDS = ("NILGIRI ENERGY", "VAYU FUELS", "KAVERI PETRO", "SAHYADRI ENERGY", "Konark Oil", "GANGA PETROCHEM")

DEALERS = ("START NELL", "SHREE GANESH FUELS", "KUMAR SERVICE STATION", "NEW BHARAT AUTO FUELS",
           "SAI KRUPA PETROLEUM", "MAA TARA FILLING STATION", "GREEN VALLEY FUEL POINT",
           "RAJDHANI SERVICE CENTRE", "ANNAPURNA FUEL CENTRE", "NATIONAL AUTO SERVICE")
#: Dealers whose name says what they are, for the unknown-brand slips.
FUEL_DEALERS = tuple(d for d in DEALERS if any(k in d for k in ("FUEL", "PETROL", "FILLING", "SERVICE STATION")))
FUEL_DEALERS += ("SHYAMBAZAR FUEL POINT", "BARRACKPORE FUEL POINT", "PARK CIRCUS FILLING STATION")

STREETS = ("178, N S C BOSE ROAD", "NH-48 NEAR TOLL PLAZA", "PLOT 12, SECTOR 5", "M G ROAD",
           "24, RING ROAD", "GT ROAD, OPP BUS STAND", "SURVEY NO 88, HIGHWAY", "17, STATION ROAD",
           "42/1, BARRACKPORE TRUNK RD")

CITIES = (("KOLKATA", "700040", "WB"), ("MUMBAI", "400051", "MH"), ("CHENNAI", "600096", "TN"),
          ("BENGALURU", "560066", "KA"), ("NEW DELHI", "110019", "DL"), ("AHMEDABAD", "380009", "GJ"),
          ("HYDERABAD", "500081", "TG"), ("LUCKNOW", "226001", "UP"))

#: What the payable-amount line is called on different pump firmware.
AMOUNT_LABELS = ("Amount(Rs)", "Amount(Rs)", "Amount (Rs.)", "Amt(Rs)", "Sale Amt", "Amount (INR)",
                 "Total Rs.", "Total Amount", "Amount Rs", "Amount(₹)", "Amount (₹)")
INVOICE_LABELS = ("Inv. No", "Inv. No", "Inv No.", "Invoice No", "Bill No.", "Inv.No")
PRODUCTS = ("Petrol", "Diesel", "Power", "XP95", "Speed")
NOISE_HEADS = ("Seer", "Sees", "ee", "", "", "")
LAYOUTS = ("plain", "gst", "vat")
#: The rupee glyph as Tesseract actually returned it on these slips.
GLYPH_MISREADS = ("2", "=", "%", "®", "Bs", "B ES", "Z")


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randrange(10)) for _ in range(n))


def _pan(rng: random.Random) -> str:
    letters = "ABCDEFGHJKLMNPRSTUVW"
    return "".join(rng.choice(letters) for _ in range(5)) + _digits(rng, 4) + rng.choice(letters)


def _money_cell(rng: random.Random, value: Decimal, artifacts: bool, *, width: int = 8) -> str:
    """``value`` as printed in a rupee column, with the damage OCR does to it when asked."""
    text = f"{value:.2f}"
    if not artifacts or rng.random() > 0.45:
        return f"₹ {text:>{width}}"
    kind = rng.choice(("glyph", "glyph", "space", "both"))
    if kind in ("space", "both"):
        text = text.replace(".", " .")
    glyph = rng.choice(GLYPH_MISREADS) if kind in ("glyph", "both") else "₹"
    return f"{glyph} {text}"


def build_slip(rng: random.Random, n: int, *, artifacts: bool = True) -> tuple[list[str], dict[str, str]]:
    """One slip as ``(lines, truth)``. The amounts genuinely relate: rate x volume ~ amount, and
    taxable + GST = amount on the tax layouts."""
    layout = rng.choices(LAYOUTS, weights=[4, 4, 2])[0]
    unknown = rng.random() < 0.3
    brand = None if unknown else rng.choice(BRANDS)
    dealer = rng.choice(FUEL_DEALERS if unknown else DEALERS)
    city, pin, state = rng.choice(CITIES)
    rate = Decimal(str(round(rng.uniform(86, 108), 2)))
    if rng.random() < 0.6:                                  # preset by amount: whole rupees
        amount = Decimal(rng.choice([100, 200, 300, 500, 590, 750, 1000, 1500, 2000, 2500, 3000]))
        volume = (amount / rate).quantize(CENTS, rounding=ROUND_HALF_UP)
        preset = "Amount"
    else:                                                   # preset by volume: amount is rate x volume
        volume = Decimal(str(round(rng.uniform(2, 45), 2)))
        amount = (rate * volume).quantize(CENTS, rounding=ROUND_HALF_UP)
        preset = "Volume"

    date = dt.date(2025, 1, 6) + dt.timedelta(days=rng.randrange(0, 560))
    date_text = date.strftime(rng.choice(["%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y"]))
    invoice = _digits(rng, rng.randint(12, 17))
    invoice_label = rng.choice(INVOICE_LABELS)

    gstin = ""
    if layout == "gst" or rng.random() < 0.3:
        prefix = f"{STATE_CODES.get(state, '27')}{_pan(rng)}1Z"
        gstin = prefix + gstin_check_digit(prefix)

    # The tax block. The amount is tax-inclusive ("Rate inclusive of GST"), so taxable = amount / 1.18.
    taxable = cgst = sgst = tax_total = None
    if layout == "gst":
        taxable = (amount / Decimal("1.18")).quantize(CENTS, rounding=ROUND_HALF_UP)
        tax_total = amount - taxable
        cgst = (tax_total / 2).quantize(CENTS, rounding=ROUND_HALF_UP)
        sgst = tax_total - cgst if rng.random() < 0.5 else cgst
        tax_total = cgst + sgst
        taxable = amount - tax_total
    elif layout == "vat":
        taxable = (amount / Decimal("1.17")).quantize(CENTS, rounding=ROUND_HALF_UP)
        tax_total = amount - taxable

    lines: list[str] = []
    head = rng.choice(NOISE_HEADS)
    if head:
        lines.append(head)
    lines.append(rng.choice(UNKNOWN_BRANDS) if unknown else rng.choice(brand.printed))
    lines.append("Welcome" if unknown else rng.choice(["Welcomes You", "Welcome", "Happy Motoring", ""]))
    lines = [line for line in lines if line]
    lines += [dealer, rng.choice(STREETS), f"{city} {pin}", f"Tel. No.: 98{_digits(rng, 8)}", ""]
    lines.append(f"{invoice_label}: {invoice}")
    if rng.random() < 0.8:
        lines.append(f"FCC ID: {_digits(rng, 17)}")
    rate_label = rng.choice(["Rate(Rs/L)", "Rate(₹/L)", "Rate(2/L)" if artifacts else "Rate(Rs/L)"])
    lines += [f"FIP No. : {rng.randint(1, 8):02d}", f"Nozzle No. : {rng.randint(1, 8):02d}",
              f"Product : {rng.choice(PRODUCTS)}",
              f"Density : {rng.uniform(730, 850):.1f}Kg/Cu.mtr", f"Preset Type: {preset}",
              f"{rate_label} : {rate}", f"Volume(L) : {volume:08.2f}"]
    label = rng.choice(AMOUNT_LABELS)
    lines.append(f"{label} : {amount:08.2f}" if rng.random() < 0.8 else f"{label} {amount:08.2f}")
    lines.append("-" * 26)
    if layout == "gst":
        lines += [f"Taxable Val  : {_money_cell(rng, taxable, artifacts)}",
                  f"CGST @9%     : {_money_cell(rng, cgst, artifacts)}",
                  f"SGST @9%     : {_money_cell(rng, sgst, artifacts)}",
                  f"Total GST    : {_money_cell(rng, tax_total, artifacts)}"]
    elif layout == "vat":
        lines += [f"Taxable Val : {_money_cell(rng, taxable, artifacts)}",
                  f"VAT @17%(Incl) : {_money_cell(rng, tax_total, artifacts)}"]
    if layout != "plain":
        lines += [f"Pay Mode     : {rng.choice(['UPI', 'Card', 'Cash'])}", f"UPI Ref: {_digits(rng, 12)}",
                  "-" * 26]
    if rng.random() < 0.85:                                  # the meter readings that are not money
        lines.append(f"Vech: {_digits(rng, 11)}.{_digits(rng, 2)}")
        lines.append(f"Vtrd: {_digits(rng, 10)}.{_digits(rng, 3)}")
    lines += ["", f"Vehicle No: {rng.randint(1000, 9999)}",
              rng.choice(["Mobile No : Not Entered", f"Mobile No : 9{_digits(rng, 9)}"]), ""]
    lines += [f"Date : {date_text}", f"Time : {rng.randint(5, 22):02d}:{rng.randint(0, 59):02d}", ""]
    lines += [f"GST No: {gstin}", "LST No:", "VAT No:", ""]
    if layout != "plain":
        lines += ["Rate inclusive of GST." if layout == "gst" else "Fuel prices incl. VAT.", ""]
    lines += ["Thank You! Please Visit", "Again.."]

    if unknown:
        vendor_name = next(l for l in lines if l in UNKNOWN_BRANDS)
        canonical = vendor_name
    else:
        vendor_name, canonical = brand.name, brand.canonical_id
    truth = {
        "receipt_id": f"P{n:05d}", "vendor_name": vendor_name, "vendor_canonical": canonical,
        "gstin": gstin, "invoice_no": invoice, "date": date.isoformat(),
        "subtotal": str(taxable) if taxable is not None else "",
        "cgst": str(cgst) if cgst is not None else "", "sgst": str(sgst) if sgst is not None else "",
        "igst": "", "cess": "", "round_off": "", "total": str(amount), "template_id": f"pump-{layout}",
    }
    return lines, truth


def generate_pump_slips(out_dir: Path, count: int = 60, seed: int = 43, *, artifacts: bool = True) -> int:
    """Write ``receipts_text/``, ``receipts_truth.csv`` and ``vendors.csv`` under ``out_dir``."""
    rng = random.Random(seed)
    out_dir = Path(out_dir)
    text_dir = out_dir / "receipts_text"
    text_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for n in range(1, count + 1):
        lines, truth = build_slip(rng, n, artifacts=artifacts)
        (text_dir / f"{truth['receipt_id']}.txt").write_text("\n".join(lines), encoding="utf-8")
        truth["noise_tier"] = rng.choices([0, 1, 2, 3], weights=[4, 3, 2, 1])[0]
        rows.append(truth)

    columns = ["receipt_id", "image_path", "text_path", "vendor_name", "vendor_canonical", "gstin",
               "invoice_no", "date", "subtotal", "cgst", "sgst", "igst", "cess", "round_off", "total",
               "template_id", "noise_tier", "rotation_deg"]
    with (out_dir / "receipts_truth.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "image_path": "", "rotation_deg": "0.0",
                             "text_path": f"receipts_text/{row['receipt_id']}.txt"})

    with (out_dir / "vendors.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["vendor_canonical", "vendor_name", "gstin", "category", "city"])
        for brand in BRANDS:
            gstin = _gstin("MH", "AAAC" + brand.canonical_id[-1] + "0123R")
            writer.writerow([brand.canonical_id, brand.name, gstin, "fuel", brand.city])
    return count
