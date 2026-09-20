"""Template helpers for the UI layer."""
from __future__ import annotations

from django import template
from PIL import Image

register = template.Library()


@register.filter
def image_dims(field):
    """``(width, height)`` of an image file, or ``None`` if it cannot be read.

    Used to reserve the image's box before it loads (no layout shift). It must
    never raise: a missing or corrupt file should cost a placeholder, not the page.
    Only the header is read, so this is cheap even for large receipts.
    """
    if not field:
        return None
    try:
        with Image.open(field.path) as img:
            return img.size
    except Exception:  # missing file, non-filesystem storage, corrupt image
        return None
