"""Render receipt text to a PNG, with graded scan noise.

Noise tiers exist so the evaluation can report accuracy *by tier*: an extractor
that scores 0.97 on clean text and 0.55 on a crumpled phone photo has a very
different operational story than one that scores 0.85 on both, and a single
aggregate number hides which one you have.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\cour.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

MARGIN = 26
LINE_HEIGHT = 22
FONT_SIZE = 15


def load_font(size: int = FONT_SIZE):
    """A monospace font, falling back to PIL's bitmap font if none is installed."""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_receipt(lines: list[str], out_path: Path, *,
                   noise_tier: int = 0, rng: random.Random | None = None,
                   font=None) -> dict:
    """Render ``lines`` to ``out_path``; returns the applied noise parameters."""
    rng = rng or random.Random(0)
    font = font or load_font()

    longest = max((len(line) for line in lines), default=40)
    char_w = _char_width(font)
    width = int(longest * char_w) + 2 * MARGIN
    height = len(lines) * LINE_HEIGHT + 2 * MARGIN

    img = Image.new("L", (max(width, 320), max(height, 200)), color=252)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((MARGIN, MARGIN + i * LINE_HEIGHT), line, fill=28, font=font)

    applied = {"noise_tier": noise_tier, "rotation_deg": 0.0,
               "blur": 0.0, "jpeg_q": 0}

    if noise_tier >= 1:
        angle = rng.uniform(-1.5, 1.5)
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=252, expand=False)
        img = _gaussian_noise(img, sigma=4, rng=rng)
        applied["rotation_deg"] = round(angle, 2)

    if noise_tier >= 2:
        extra = rng.uniform(-1.5, 1.5)
        img = img.rotate(extra, resample=Image.BICUBIC, fillcolor=252, expand=False)
        img = img.filter(ImageFilter.GaussianBlur(0.8))
        img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.85, 1.15))
        applied["rotation_deg"] = round(applied["rotation_deg"] + extra, 2)
        applied["blur"] = 0.8
        applied["jpeg_q"] = 55

    if noise_tier >= 3:
        img = _sine_warp(img, rng)
        img = _shadow_gradient(img)
        img = _specks(img, rng, fraction=0.08)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = img.convert("L")
    if applied["jpeg_q"]:
        # Round-trip through JPEG so the compression artefacts are real.
        tmp = out_path.with_suffix(".jpg")
        img.save(tmp, quality=applied["jpeg_q"])
        img = Image.open(tmp).convert("L")
        tmp.unlink(missing_ok=True)
    img.save(out_path)
    return applied


# --------------------------------------------------------------------- effects
def _char_width(font) -> float:
    try:
        return font.getlength("M")
    except AttributeError:
        return 9.0


def _gaussian_noise(img: Image.Image, *, sigma: float, rng: random.Random) -> Image.Image:
    pixels = img.load()
    w, h = img.size
    # Sample sparsely: full per-pixel noise is slow and visually identical here.
    for _ in range(int(w * h * 0.12)):
        x, y = rng.randrange(w), rng.randrange(h)
        value = pixels[x, y] + int(rng.gauss(0, sigma))
        pixels[x, y] = max(0, min(255, value))
    return img


def _sine_warp(img: Image.Image, rng: random.Random) -> Image.Image:
    """A gentle vertical ripple, as if the receipt were crumpled."""
    w, h = img.size
    amplitude = rng.uniform(1.5, 3.5)
    period = rng.uniform(60, 140)
    out = Image.new("L", (w, h), color=252)
    for y in range(h):
        shift = int(amplitude * math.sin(2 * math.pi * y / period))
        row = img.crop((0, y, w, y + 1))
        out.paste(row, (shift, y))
    return out


def _shadow_gradient(img: Image.Image) -> Image.Image:
    w, h = img.size
    gradient = Image.linear_gradient("L").resize((w, h))
    gradient = ImageEnhance.Brightness(gradient).enhance(0.35)
    return Image.blend(img, Image.composite(img, gradient, img), 0.78)


def _specks(img: Image.Image, rng: random.Random, *, fraction: float) -> Image.Image:
    pixels = img.load()
    w, h = img.size
    for _ in range(int(w * h * fraction * 0.05)):
        x, y = rng.randrange(w), rng.randrange(h)
        pixels[x, y] = 255 if rng.random() < 0.5 else 0
    return img
