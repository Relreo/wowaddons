""".toc file parsing, addon scanning, and game flavor detection."""

from __future__ import annotations

import sys
from pathlib import Path

from .constants import FLAVORS, log
from .models import ProjectData, UnmatchedAddon
from .utils import strip_wow_colors


def parse_toc(toc_path: Path | str) -> dict[str, str]:
    """Parse a WoW ``.toc`` file and return its ``##`` metadata fields."""
    meta: dict[str, str] = {}
    try:
        with open(toc_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("## "):
                    key, _, value = line[3:].partition(":")
                    if value:
                        meta[key.strip()] = value.strip()
    except OSError:
        pass
    return meta


def scan_addons() -> tuple[dict[int, ProjectData], list[UnmatchedAddon]]:
    """Scan the current directory for addon folders, reading ``.toc`` metadata.

    Addon folders starting with ``Blizzard_`` or ``.`` are skipped. Folders
    whose ``.toc`` contains an ``X-Curse-Project-ID`` are grouped by project;
    the rest are returned as unmatched.

    Returns:
        A tuple of ``(by_project, unmatched)`` where ``by_project`` maps
        CurseForge project IDs to :class:`ProjectData` and ``unmatched``
        is a list of :class:`UnmatchedAddon` dicts.
    """
    by_project: dict[int, ProjectData] = {}
    unmatched: list[UnmatchedAddon] = []

    for entry in sorted(Path.cwd().iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("Blizzard_", ".")):
            continue

        toc_files = list(entry.glob("*.toc"))
        if not toc_files:
            continue

        toc = next(
            (t for t in toc_files if t.stem == entry.name), toc_files[0]
        )
        meta = parse_toc(toc)
        title = strip_wow_colors(meta.get("Title", entry.name))
        version = meta.get("Version", "")
        pid_str = meta.get("X-Curse-Project-ID", "")

        log.debug(
            "%s: title=%r  version=%r  project_id=%r",
            entry.name, title, version, pid_str,
        )

        if pid_str and pid_str.isdigit():
            pid = int(pid_str)
            if pid in by_project:
                by_project[pid]["folders"].append(entry.name)
            else:
                by_project[pid] = ProjectData(
                    folders=[entry.name],
                    title=title,
                    version=version,
                )
        else:
            unmatched.append(UnmatchedAddon(
                folder=entry.name,
                title=title,
                version=version,
            ))

    return by_project, unmatched


def detect_flavor(override: str | None = None) -> str:
    """Auto-detect the WoW game flavor from the directory path.

    Inspects the components of the current working directory for
    ``_classic_era_``, ``_classic_``, or defaults to ``retail``.

    Args:
        override: If provided, use this flavor instead of auto-detecting.

    Returns:
        One of ``"retail"``, ``"classic"``, or ``"classic_era"``.
    """
    if override:
        if override not in FLAVORS:
            log.error(
                "Unknown flavor '%s'. Choose from: %s",
                override, ", ".join(FLAVORS),
            )
            sys.exit(1)
        return override

    lower_parts = [p.lower() for p in Path.cwd().parts]
    if "_classic_era_" in lower_parts:
        flavor = "classic_era"
    elif "_classic_" in lower_parts:
        flavor = "classic"
    else:
        flavor = "retail"
    log.debug("Detected flavor: %s", flavor)
    return flavor
