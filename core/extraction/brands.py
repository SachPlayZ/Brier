"""Brand names that head a receipt while the trading name sits further down.

A petrol-pump slip prints the brand ("IndianOil") in the header and the dealer
("START NELL") next to the address. People, and this app's duplicate check, mean the
brand: every Indian Oil pump is the same vendor. The dealer name is also the line OCR
mangles most.

Matching is on letters only, lower-case, so "IndianOil", "Indian Oil" and "INDIAN OIL"
are the same. A long key may appear anywhere in the line; a short one ("hp", "bp")
must be the whole line, or "HP" would fire inside any word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

#: Similarity (0-100) at which a whole header line counts as a brand name despite OCR damage.
FUZZY_BRAND = 84


@dataclass(frozen=True)
class Brand:
    name: str                       # display name when no gazetteer entry exists
    words: tuple[str, ...]          # words a gazetteer entry must contain to be this brand
    long_keys: tuple[str, ...] = ()
    exact_keys: tuple[str, ...] = ()


BRANDS: tuple[Brand, ...] = (
    Brand("Indian Oil", ("indian", "oil"), ("indianoil", "indianoilcorporation"), ("iocl", "ioc")),
    Brand("Bharat Petroleum", ("bharat", "petroleum"), ("bharatpetroleum",), ("bpcl",)),
    Brand("Hindustan Petroleum", ("hindustan", "petroleum"), ("hindustanpetroleum",), ("hpcl", "hp")),
    Brand("Shell", ("shell",), ("shellselect", "shellindia"), ("shell",)),
    Brand("Nayara Energy", ("nayara",), ("nayara", "nayaraenergy")),
    Brand("Jio-bp", ("jio", "bp"), ("jiobp", "reliancebp"), ("jiobp",)),
)

_NON_LETTERS = re.compile(r"[^a-z]")


def find_brand(line: str) -> Brand | None:
    """The brand a header line names, or ``None``."""
    letters = _NON_LETTERS.sub("", (line or "").lower())
    if len(letters) < 2:
        return None
    for brand in BRANDS:
        if letters in brand.exact_keys or any(key in letters for key in brand.long_keys):
            return brand
    # OCR drops and swaps letters ("SHLL", "5hell", "INDUSTAN PEROLEUM"). A line that is nearly
    # the whole brand name still counts; a longer line that merely contains part of it does not.
    if 4 <= len(letters) <= 24:
        for brand in BRANDS:
            keys = (*brand.long_keys, *brand.exact_keys)
            if any(len(key) >= 5 and fuzz.ratio(letters, key) >= FUZZY_BRAND for key in keys):
                return brand
    return None
