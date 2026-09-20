"""Vendor gazetteer: fuzzy name snapping plus GSTIN memory.

The gazetteer is the bridge between free-text vendor lines and canonical
vendor records. In Django it is built from the ``Vendor`` table; in the eval
harness it is built from a CSV. Either way the matching logic is identical.

A learned GSTIN->vendor mapping beats fuzzy name matching outright: the GSTIN
is a checksummed identifier, so once seen it resolves the vendor exactly even
when the printed name is garbled.
"""
from __future__ import annotations

import csv
from pathlib import Path

from rapidfuzz import fuzz, process

from core.normalize import norm_vendor

SNAP_THRESHOLD = 88


class VendorGazetteer:
    def __init__(self, entries: list[tuple[str, str]]):
        """``entries`` is a list of (canonical_id, printed_name)."""
        self._by_norm: dict[str, str] = {}
        self._names: list[str] = []
        self._name_to_id: dict[str, str] = {}
        self._gstin: dict[str, str] = {}
        for canonical_id, name in entries:
            self.add(canonical_id, name)

    def add(self, canonical_id: str, name: str) -> None:
        key = norm_vendor(name)
        if not key:
            return
        self._by_norm.setdefault(key, canonical_id)
        if key not in self._name_to_id:
            self._names.append(key)
            self._name_to_id[key] = canonical_id

    @classmethod
    def from_csv(cls, path: Path) -> "VendorGazetteer":
        """Load from a CSV with ``vendor_canonical``/``vendor_name`` columns.

        Aliases are picked up automatically: every distinct printed name seen
        for a canonical id becomes a match target.
        """
        entries: list[tuple[str, str]] = []
        gstins: list[tuple[str, str]] = []
        with Path(path).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cid = row.get("vendor_canonical") or row.get("canonical_id")
                name = row.get("vendor_name") or row.get("name")
                if cid and name:
                    entries.append((cid, name))
                if cid and row.get("gstin"):
                    gstins.append((row["gstin"], cid))
        gaz = cls(entries)
        for gstin, cid in gstins:
            gaz.learn_gstin(gstin, cid)
        return gaz

    def snap(self, name: str, *, threshold: int = SNAP_THRESHOLD) -> tuple[str | None, float]:
        """Snap a printed name to a canonical id.

        Returns ``(canonical_id_or_None, similarity_0_to_1)``. The similarity is
        returned even when below threshold -- it feeds the ``fuzz`` confidence
        component, so a near-miss still scores better than a total miss.
        """
        key = norm_vendor(name)
        if not key:
            return None, 0.0
        if key in self._by_norm:
            return self._by_norm[key], 1.0
        if not self._names:
            return None, 0.5
        match = process.extractOne(key, self._names, scorer=fuzz.token_set_ratio)
        if match is None:
            return None, 0.0
        matched_name, score, _ = match
        ratio = score / 100.0
        return (self._name_to_id[matched_name] if score >= threshold else None), ratio

    def find_by_words(self, words: tuple[str, ...]) -> str | None:
        """Canonical id of the first entry whose name contains every word (a brand lookup)."""
        for key in self._names:
            if all(word in key.lower().split() for word in words):
                return self._name_to_id[key]
        return None

    def learn_gstin(self, gstin: str, canonical_id: str) -> None:
        if gstin and canonical_id:
            self._gstin[gstin.upper()] = canonical_id

    def by_gstin(self, gstin: str | None) -> str | None:
        return self._gstin.get(gstin.upper()) if gstin else None

    def name_for(self, canonical_id: str) -> str | None:
        for key, cid in self._name_to_id.items():
            if cid == canonical_id:
                return key
        return None

    def __len__(self) -> int:
        return len(self._names)

    @property
    def is_empty(self) -> bool:
        return not self._names
