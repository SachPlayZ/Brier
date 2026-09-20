"""Receipt layouts with randomized label wording.

Four templates, each with several interchangeable phrasings for every label
("Total" / "Grand Total" / "Net Payable", "GSTIN" / "GST No."). Without this
the regexes would score beautifully on the synthetic corpus by memorising one
phrasing, and collapse on a real receipt -- the evaluation would be measuring
nothing. Each rendered line carries its role, which becomes free training data
for the line-role model.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from core.datagen.vendors_seed import CATALOG, GST_RATE, Vendor

TEMPLATE_IDS = ("retail", "restaurant", "fuel", "b2b")

LABELS = {
    "total": ["Total", "Grand Total", "Net Payable", "Total Amount", "Amount Payable", "Bill Total"],
    "subtotal": ["Sub Total", "Subtotal", "Taxable Value", "Total Before Tax", "Taxable Amount"],
    "gstin": ["GSTIN", "GST No.", "GSTIN/UIN", "GST Reg No"],
    "invoice": ["Invoice No", "Bill No", "Invoice Number", "Inv No", "Receipt No", "Cash Memo No"],
    "date": ["Date", "Invoice Date", "Bill Date", "Dated", "Date of Supply"],
    "round_off": ["Round Off", "Rounding", "Round Off Adj"],
    "header": ["TAX INVOICE", "GST INVOICE", "RETAIL INVOICE", "CASH MEMO"],
    "footer": ["THANK YOU VISIT AGAIN", "Thank you for your business",
               "*** CUSTOMER COPY ***", "Goods once sold will not be taken back",
               "Subject to local jurisdiction"],
}

DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d", "%d.%m.%Y", "%b %d, %Y"]

CENTS = Decimal("0.01")


@dataclass
class LineItem:
    description: str
    qty: int
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return (self.unit_price * self.qty).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass
class ReceiptData:
    """Everything the renderer and the truth CSV need."""

    vendor: Vendor
    invoice_no: str
    date: object                     # datetime.date
    items: list[LineItem]
    subtotal: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    cess: Decimal
    round_off: Decimal
    total: Decimal
    template_id: str
    date_format: str
    interstate: bool


def build_receipt(vendor: Vendor, invoice_no: str, date, rng: random.Random,
                  *, interstate: bool | None = None) -> ReceiptData:
    """Compose a receipt whose amounts genuinely reconcile."""
    catalog = CATALOG[vendor.category]
    n_items = rng.randint(1, 2) if vendor.template == "fuel" else rng.randint(2, 6)
    items: list[LineItem] = []
    for _ in range(n_items):
        name, low, high = rng.choice(catalog)
        qty = rng.randint(1, 4) if vendor.category != "fuel" else rng.randint(5, 45)
        price = Decimal(str(round(rng.uniform(low, high), 2)))
        items.append(LineItem(name, qty, price))

    subtotal = sum((i.amount for i in items), Decimal("0")).quantize(CENTS)
    rate = Decimal(str(GST_RATE[vendor.category]))
    interstate = rng.random() < 0.18 if interstate is None else interstate

    cgst = sgst = igst = Decimal("0.00")
    if rate > 0:
        tax = (subtotal * rate / Decimal("100")).quantize(CENTS, rounding=ROUND_HALF_UP)
        if interstate:
            igst = tax
        else:
            cgst = (tax / 2).quantize(CENTS, rounding=ROUND_HALF_UP)
            sgst = tax - cgst
    cess = Decimal("0.00")

    gross = subtotal + cgst + sgst + igst + cess
    rounded = gross.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    round_off = (rounded - gross).quantize(CENTS)
    total = gross + round_off

    return ReceiptData(
        vendor=vendor, invoice_no=invoice_no, date=date, items=items,
        subtotal=subtotal, cgst=cgst, sgst=sgst, igst=igst, cess=cess,
        round_off=round_off, total=total, template_id=vendor.template,
        date_format=rng.choice(DATE_FORMATS), interstate=interstate)


def render_lines(data: ReceiptData, rng: random.Random,
                 *, vendor_name: str | None = None,
                 date_format: str | None = None,
                 width: int = 44) -> list[tuple[str, str]]:
    """Render a receipt as ``(text, role)`` lines.

    ``vendor_name``/``date_format`` overrides exist so a duplicate can be
    re-rendered with a different spelling or date style while keeping the same
    underlying bill -- the 'retyped' duplicate class.
    """
    label = lambda key: rng.choice(LABELS[key])          # noqa: E731
    name = vendor_name or data.vendor.name
    fmt = date_format or data.date_format
    v = data.vendor
    rule = "-" * width
    out: list[tuple[str, str]] = []

    out.append((name.upper() if data.template_id != "b2b" else name, "VENDOR"))
    out.append((f"{v.street}, {v.city} {v.pincode}", "ADDRESS"))
    if rng.random() < 0.8:
        out.append((f"Ph: {v.phone}", "ADDRESS"))
    out.append((f"{label('gstin')}: {v.gstin}", "GSTIN"))
    out.append((label("header"), "OTHER"))
    out.append((rule, "OTHER"))

    meta = [(f"{label('invoice')}: {data.invoice_no}", "INVOICE"),
            (f"{label('date')}: {data.date.strftime(fmt)}", "DATE")]
    rng.shuffle(meta)
    out.extend(meta)
    if data.template_id == "restaurant" and rng.random() < 0.7:
        out.append((f"Table: {rng.randint(1, 24)}   Covers: {rng.randint(1, 8)}", "OTHER"))
    out.append((rule, "OTHER"))

    if data.template_id == "fuel":
        out.append((_pad("Product", "Litres", "Amount", width), "OTHER"))
    else:
        out.append((_pad("Item", "Qty", "Amount", width), "OTHER"))

    for item in data.items:
        out.append((_pad(item.description[:20], str(item.qty),
                         _money(item.amount), width), "ITEM"))
    out.append((rule, "OTHER"))

    if data.template_id != "fuel" and rng.random() < 0.45:
        # The classic decoy: a "Total" label that is not an amount.
        out.append((f"Total Qty: {sum(i.qty for i in data.items)}", "OTHER"))

    out.append((_kv(label("subtotal"), _money(data.subtotal), width), "SUBTOTAL"))
    rate = GST_RATE[data.vendor.category]
    if data.igst > 0:
        out.append((_kv(f"IGST @ {_rate(rate)}%", _money(data.igst), width), "TAX"))
    elif data.cgst > 0 or data.sgst > 0:
        half = _rate(rate / 2)
        out.append((_kv(f"CGST @ {half}%", _money(data.cgst), width), "TAX"))
        out.append((_kv(f"SGST @ {half}%", _money(data.sgst), width), "TAX"))
    if data.round_off != 0:
        out.append((_kv(label("round_off"), _money(data.round_off), width), "TAX"))

    symbol = "₹" if rng.random() < 0.6 else ""
    out.append((_kv(label("total"), f"{symbol}{_money(data.total)}", width), "TOTAL"))
    out.append((rule, "OTHER"))
    out.append((rng.choice(LABELS["footer"]), "FOOTER"))
    if rng.random() < 0.5:
        out.append((f"{rng.choice(['Cash', 'Card', 'UPI'])} Payment", "FOOTER"))
    return out


# --------------------------------------------------------------------- helpers
def _money(value: Decimal) -> str:
    negative = value < 0
    whole = f"{abs(value):,.2f}"
    return f"-{whole}" if negative else whole


def _rate(value: float) -> str:
    return f"{value:g}"


def _pad(left: str, middle: str, right: str, width: int) -> str:
    left = left[:22]
    return f"{left:<24}{middle:>4}{right:>{max(width - 28, 10)}}"


def _kv(key: str, value: str, width: int) -> str:
    return f"{key + ':':<26}{value:>{max(width - 26, 10)}}"


def retype_vendor(name: str, rng: random.Random) -> str:
    """A plausible re-typing of a vendor name, for the 'retyped' duplicate class."""
    variants = []
    if " Pvt Ltd" in name:
        variants.append(name.replace(" Pvt Ltd", " Private Limited"))
        variants.append(name.replace(" Pvt Ltd", ""))
    if " Ltd" in name:
        variants.append(name.replace(" Ltd", " Limited"))
    words = name.split()
    if len(words) > 1:
        variants.append(" ".join(words[:-1]))
        variants.append(name.upper())
        # A single transposed character, the way a hurried re-entry goes wrong.
        idx = rng.randrange(len(words))
        word = words[idx]
        if len(word) > 3:
            pos = rng.randrange(len(word) - 1)
            swapped = word[:pos] + word[pos + 1] + word[pos] + word[pos + 2:]
            variants.append(" ".join(words[:idx] + [swapped] + words[idx + 1:]))
    variants.append(name.replace(" and ", " & ").replace(" & ", " and "))
    return rng.choice([v for v in variants if v and v != name] or [name])
