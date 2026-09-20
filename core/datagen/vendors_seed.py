"""Seed vendors with checksum-valid GSTINs.

The check digit is computed rather than hard-coded, so every generated GSTIN
passes the same validator the extractor uses. That matters: if the synthetic
data carried invalid GSTINs, the ``fmt`` confidence component would look broken
across the whole corpus and the evaluation would be measuring the generator's
bug rather than the extractor's quality.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.extraction.fields import gstin_check_digit

STATE_CODES = {"KA": "29", "MH": "27", "DL": "07", "TN": "33", "TG": "36",
               "GJ": "24", "WB": "19", "UP": "09"}


@dataclass(frozen=True)
class Vendor:
    canonical_id: str
    name: str
    gstin: str
    category: str
    city: str
    pincode: str
    street: str
    template: str
    phone: str

    @property
    def state_code(self) -> str:
        return self.gstin[:2]


def _gstin(state: str, pan: str, entity: str = "1") -> str:
    prefix = f"{STATE_CODES[state]}{pan}{entity}Z"
    return prefix + gstin_check_digit(prefix)


_RAW = [
    # canonical_id,  name,                         state, PAN,          category,     city,        pin,      street,                  template,   phone
    ("V001", "Sharma Traders Pvt Ltd",             "KA", "AAGCB7383J", "stationery", "Bengaluru", "560001", "14 MG Road",             "b2b",      "9845012345"),
    ("V002", "Annapurna Restaurant",               "KA", "AABCA1234M", "food",       "Bengaluru", "560034", "22 Koramangala 5th Blk", "restaurant", "9845112233"),
    ("V003", "Bharat Petroleum Outlet",            "MH", "AAACB1111P", "fuel",       "Mumbai",    "400051", "NH-8 Bandra East",       "fuel",     "9820011122"),
    ("V004", "Reliance Fresh Supermarket",         "MH", "AAACR5055K", "grocery",    "Mumbai",    "400070", "Shop 4 Kurla West",      "retail",   "9820044556"),
    ("V005", "Hotel Sunrise Residency",            "DL", "AACCH9012Q", "hotel",      "New Delhi", "110001", "9 Connaught Place",      "b2b",      "9811023456"),
    ("V006", "Kumar Medicals",                     "DL", "AAFCK3321L", "pharmacy",   "New Delhi", "110019", "44 Kalkaji Main Rd",     "retail",   "9811099887"),
    ("V007", "Saravana Bhavan",                    "TN", "AAECS7788N", "food",       "Chennai",   "600017", "18 T Nagar",             "restaurant", "9840011223"),
    ("V008", "Indian Oil Fuel Station",            "TN", "AAACI0123R", "fuel",       "Chennai",   "600096", "OMR Perungudi",          "fuel",     "9840055667"),
    ("V009", "Hyderabad Biryani House",            "TG", "AADCH4567S", "food",       "Hyderabad", "500081", "5 Gachibowli",           "restaurant", "9848012345"),
    ("V010", "Deccan Office Supplies",             "TG", "AAGCD8899T", "stationery", "Hyderabad", "500032", "12 Madhapur",            "b2b",      "9848077665"),
    ("V011", "Gujarat Enterprises",                "GJ", "AABCG2244U", "hardware",   "Ahmedabad", "380009", "7 CG Road",              "b2b",      "9825011234"),
    ("V012", "Patel Stores",                       "GJ", "AAHCP6677V", "grocery",    "Surat",     "395007", "31 Adajan",              "retail",   "9825099001"),
    ("V013", "Bengal Book Depot",                  "WB", "AACCB3456W", "stationery", "Kolkata",   "700016", "6 Park Street",          "retail",   "9830011234"),
    ("V014", "Park Street Cafe",                   "WB", "AAFCP7890X", "food",       "Kolkata",   "700017", "40 Park Street",         "restaurant", "9830044332"),
    ("V015", "Lucknow Travels Agencies",           "UP", "AAGCL1122Y", "travel",     "Lucknow",   "226001", "3 Hazratganj",           "b2b",      "9839011223"),
    ("V016", "Ganga Departmental Store",           "UP", "AABCG5566Z", "grocery",    "Kanpur",    "208001", "17 Mall Road",           "retail",   "9839066554"),
    ("V017", "Shell Select Fuel Point",            "KA", "AAACS9911A", "fuel",       "Bengaluru", "560066", "Whitefield Main Rd",     "fuel",     "9845077889"),
    ("V018", "Cafe Coffee Junction",               "KA", "AAECC2233B", "food",       "Bengaluru", "560095", "8 Indiranagar",          "restaurant", "9845033445"),
    ("V019", "Mumbai Print Solutions LLP",         "MH", "AAFCM4455C", "printing",   "Mumbai",    "400013", "21 Lower Parel",         "b2b",      "9820077331"),
    ("V020", "Apollo Pharmacy Outlet",             "TN", "AAACA6677D", "pharmacy",   "Chennai",   "600028", "Rajiv Gandhi Salai",     "retail",   "9840099887"),
]

VENDORS: list[Vendor] = [
    Vendor(canonical_id=cid, name=name, gstin=_gstin(state, pan), category=cat,
           city=city, pincode=pin, street=street, template=template, phone=phone)
    for cid, name, state, pan, cat, city, pin, street, template, phone in _RAW
]

VENDORS_BY_ID = {v.canonical_id: v for v in VENDORS}

#: Line items per category, as (description, unit_price_range).
CATALOG: dict[str, list[tuple[str, float, float]]] = {
    "stationery": [("A4 Paper Ream", 210, 320), ("Stapler", 120, 260),
                   ("Marker Pen", 25, 60), ("File Folder", 35, 90),
                   ("Notebook 200pg", 55, 130), ("Printer Ink", 480, 1200)],
    "food":       [("Masala Dosa", 80, 160), ("Filter Coffee", 30, 70),
                   ("Veg Thali", 150, 320), ("Paneer Butter Masala", 180, 340),
                   ("Butter Naan", 40, 75), ("Biryani", 180, 400)],
    "fuel":       [("Petrol", 95, 108), ("Diesel", 88, 96), ("CNG", 74, 88)],
    "grocery":    [("Toor Dal 1kg", 110, 180), ("Basmati Rice 5kg", 420, 720),
                   ("Sunflower Oil 1L", 120, 190), ("Milk 1L", 52, 70),
                   ("Atta 5kg", 220, 340), ("Sugar 1kg", 42, 60)],
    "hotel":      [("Deluxe Room Night", 2400, 6800), ("Breakfast Buffet", 350, 850),
                   ("Laundry Service", 180, 520)],
    "pharmacy":   [("Paracetamol Strip", 18, 45), ("Vitamin D3", 120, 320),
                   ("Antiseptic Liquid", 85, 190), ("Digital Thermometer", 180, 420)],
    "hardware":   [("Extension Board", 240, 560), ("HDMI Cable 2m", 180, 480),
                   ("USB Hub", 320, 890), ("Keyboard", 450, 1600)],
    "travel":     [("Airport Transfer", 650, 1800), ("Outstation Cab", 1800, 4800),
                   ("City Taxi", 180, 620)],
    "printing":   [("Business Cards 500", 480, 1200), ("Brochure Print 100", 1200, 3400),
                   ("Poster A2", 180, 460)],
}

#: GST slab per category (as a total percentage, split CGST/SGST for intra-state).
GST_RATE: dict[str, float] = {
    "stationery": 18.0, "food": 5.0, "fuel": 0.0, "grocery": 5.0,
    "hotel": 12.0, "pharmacy": 12.0, "hardware": 18.0, "travel": 5.0,
    "printing": 18.0,
}


def vendor_rows() -> list[dict[str, str]]:
    """Rows for ``vendors.csv`` / the Django ``Vendor`` table."""
    return [{"vendor_canonical": v.canonical_id, "vendor_name": v.name,
             "gstin": v.gstin, "category": v.category, "city": v.city}
            for v in VENDORS]
