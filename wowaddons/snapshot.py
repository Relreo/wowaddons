"""Export/import snapshot file format for sharing addon lists."""

from __future__ import annotations

from pathlib import Path

# First line identifies our format; extra # lines are comments for humans.
EXPORT_FORMAT_LINE = "# wowaddons-export 1"


def parse_snapshot_file(path: Path) -> list[str]:
    """Read identifiers from an export file or legacy freeze-style list.

    Blank lines and lines whose first non-whitespace character is ``#`` are
    skipped. Each other line contributes the segment before ``#`` (trimmed),
    typically a numeric CurseForge project ID (same as ``install`` accepts
    slugs/names if present in older hand-edited files).

    Order is preserved; duplicates are dropped (first wins).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            key = raw.split("#", 1)[0].strip()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(key)
    return ordered
