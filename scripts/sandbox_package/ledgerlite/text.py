"""Text helpers for category names and export file names."""

from __future__ import annotations

import re


def slugify(name: str) -> str:
    """"Eating Out" -> "eating-out", for export file names."""
    lowered = name.strip().lower()
    cleaned = re.sub(r"[^a-z0-9 ]", "", lowered)
    return cleaned.replace(" ", "-")
