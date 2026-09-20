"""Deterministic OCR-noise simulation.

The text sidecar is the *exact* rendered text, so extraction against it is
trivially perfect and the confidence scores have nothing to discriminate --
every field comes back correct at ~0.9. That makes the calibration metrics
degenerate and the whole per-field confidence feature unfalsifiable.

This module injects the errors a real OCR engine makes, keyed to the same noise
tier used to render the image, and lowers the per-character confidences to
match. It gives an honest evaluation signal on a machine with no Tesseract
installed, and ``manage.py evaluate --ocr`` still runs the real thing when the
binary is available.
"""
from __future__ import annotations

import random

from core.types import OcrText

#: Confusions a real engine actually makes, both directions.
CONFUSIONS: dict[str, str] = {
    "0": "O", "O": "0", "1": "l", "l": "1", "I": "1", "5": "S", "S": "5",
    "8": "B", "B": "8", "2": "Z", "Z": "2", "6": "G", "G": "6",
    "rn": "m", "m": "rn", "cl": "d", "D": "O", "Q": "O", "U": "V",
}

#: Per-tier: (char error rate, mean char confidence, confidence spread).
TIER_PROFILE: dict[int, tuple[float, float, float]] = {
    0: (0.000, 0.97, 0.02),
    1: (0.006, 0.92, 0.06),
    2: (0.022, 0.82, 0.12),
    3: (0.055, 0.68, 0.18),
}


def degrade_text(text: str, noise_tier: int, *, seed: int = 0) -> OcrText:
    """Return ``text`` corrupted as a scan of the given tier would read.

    Digits inside amounts are corrupted at a reduced rate: OCR engines are
    noticeably better on the large, well-spaced numerals in a totals block than
    on dense body text, and pretending otherwise would make the arithmetic
    check fire far more often than it does in practice.
    """
    rate, mean_conf, spread = TIER_PROFILE.get(noise_tier, TIER_PROFILE[0])
    rng = random.Random(seed)

    if rate <= 0:
        return OcrText(text, [mean_conf] * len(text), "sidecar")

    out: list[str] = []
    confs: list[float] = []

    for ch in text:
        if ch == "\n":
            out.append(ch)
            confs.append(1.0)
            continue

        local_rate = rate * (0.45 if ch.isdigit() else 1.0)
        roll = rng.random()

        if roll < local_rate:
            replacement = CONFUSIONS.get(ch)
            if replacement is None:
                # No known confusion: drop the character or smudge it to a space.
                if rng.random() < 0.5 and not ch.isspace():
                    confs.append(max(0.05, mean_conf - 0.45))
                    out.append(" ")
                continue
            out.append(replacement)
            confs.append(max(0.05, rng.gauss(mean_conf - 0.35, spread)))
        elif roll < local_rate * 1.15:
            out.append(ch)
            confs.append(_clamp(rng.gauss(mean_conf, spread)))
            out.append(ch)                      # a doubled character
            confs.append(max(0.05, mean_conf - 0.3))
        else:
            out.append(ch)
            confs.append(_clamp(rng.gauss(mean_conf, spread)))

    return OcrText("".join(out), confs, "sidecar")


def _clamp(value: float) -> float:
    return max(0.05, min(1.0, value))
