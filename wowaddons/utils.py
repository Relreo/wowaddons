"""Pure formatting and string utilities."""

from __future__ import annotations

import re

_WOW_COLOR_RE = re.compile(r"\|c[0-9a-fA-F]{8}|\|r", re.IGNORECASE)


def strip_wow_colors(text: str) -> str:
    """Remove WoW UI color escape codes from a string.

    Strips ``|cAARRGGBB`` (color start) and ``|r`` (color reset) sequences
    that addon authors embed in ``.toc`` Title fields for in-game display.
    """
    return _WOW_COLOR_RE.sub("", text).strip()


def format_size(n: float) -> str:
    """Format a byte count as a human-readable size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_count(n: float) -> str:
    """Format a large number with K/M suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def truncate(text: str | None, length: int = 70) -> str:
    """Truncate a string with an ellipsis if it exceeds the given length."""
    if not text:
        return ""
    return (text[:length] + "…") if len(text) > length else text


def normalize(text: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy comparison."""
    return "".join(char.lower() for char in text if char.isalnum())
