""".toc file parsing, addon scanning, and game flavor detection."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    FLAVOR_ALIASES,
    FLAVOR_LABELS,
    FLAVORS,
    INSTALL_FOLDERS,
    PRODUCT_FLAVORS,
    log,
)
from .models import ProjectData, UnmatchedAddon
from .utils import strip_wow_colors


@dataclass(frozen=True)
class DetectedFlavor:
    """Resolved CurseForge game flavor for the current AddOns folder."""

    name: str
    id: int
    label: str
    source: str


def parse_toc_text(text: str) -> dict[str, str]:
    """Parse the text of a WoW ``.toc`` file and return its ``##`` fields."""
    meta: dict[str, str] = {}
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if line.startswith("##"):
            key, _, value = line[2:].partition(":")
            if value:
                meta[key.strip()] = value.strip()
    return meta


def parse_toc(toc_path: Path | str) -> dict[str, str]:
    """Parse a WoW ``.toc`` file and return its ``##`` metadata fields."""
    try:
        with open(toc_path, "r", encoding="utf-8", errors="replace") as f:
            return parse_toc_text(f.read())
    except OSError:
        return {}


def find_toc(folder: Path) -> Path | None:
    """Return the primary ``.toc`` in an addon folder (``<folder>.toc`` first)."""
    toc_files = list(folder.glob("*.toc"))
    if not toc_files:
        return None
    return next((t for t in toc_files if t.stem == folder.name), toc_files[0])


def read_local_meta(folders: list[str]) -> dict[str, str]:
    """Return ``.toc`` metadata from the first of *folders* that has one."""
    for name in folders:
        toc = find_toc(Path(name))
        if toc:
            meta = parse_toc(toc)
            if meta:
                return meta
    return {}


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

        toc = find_toc(entry)
        if toc is None:
            continue

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


def _make_flavor(name: str, source: str) -> DetectedFlavor:
    return DetectedFlavor(
        name=name,
        id=FLAVORS[name],
        label=FLAVOR_LABELS[name],
        source=source,
    )


def _read_info_file(path: Path) -> list[dict[str, str]]:
    """Parse a Blizzard ``.info`` table (``Name!TYPE:n|...`` header, ``|`` rows)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = [line for line in lines if line.strip()]
    if not rows:
        return []
    columns = [col.split("!", 1)[0].strip() for col in rows[0].split("|")]
    return [dict(zip(columns, row.split("|"))) for row in rows[1:]]


def find_client_dir(start: Path) -> Path | None:
    """Return the nearest directory at or above *start* holding ``.flavor.info``."""
    for directory in (start, *start.parents):
        if (directory / ".flavor.info").is_file():
            return directory
    return None


def read_client_product(client_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(product, version)`` for a WoW client directory.

    The product code comes from the client's ``.flavor.info``; the version is
    looked up for that product in the install root's ``.build.info``.
    """
    flavor_rows = _read_info_file(client_dir / ".flavor.info")
    product = flavor_rows[0].get("Product Flavor", "").strip() if flavor_rows else ""
    if not product:
        return None, None

    version: str | None = None
    for row in _read_info_file(client_dir.parent / ".build.info"):
        if row.get("Product", "").strip() == product:
            version = row.get("Version", "").strip() or None
            if row.get("Active", "").strip() == "1":
                break
    return product, version


def flavor_from_version(version: str) -> str | None:
    """Map a client version (e.g. ``1.60.1.70291``) to a flavor name."""
    try:
        major, minor = (int(p) for p in version.split(".")[:2])
    except ValueError:
        return None
    if major == 1:
        return "forever" if minor >= 60 else "classic_era"
    return {2: "tbc", 3: "wrath", 4: "cata", 5: "classic"}.get(
        major, "retail" if major >= 6 else None
    )


def detect_flavor(override: str | None = None) -> DetectedFlavor:
    """Auto-detect the WoW game flavor for the current AddOns folder.

    Checks, in order:

    1. ``--flavor`` override.
    2. The client's version from ``.build.info`` (via the product code in the
       client folder's ``.flavor.info``) — e.g. ``1.60.x`` → WoW Forever.
    3. The product code alone (e.g. ``wow_classic_beta``).
    4. The install-folder name in the path (e.g. ``_classic_beta_``).
    5. Retail.

    Args:
        override: If provided, use this flavor instead of auto-detecting.

    Returns:
        A :class:`DetectedFlavor` with CurseForge ``gameVersionTypeId``.
    """
    if override:
        name = FLAVOR_ALIASES.get(override, override)
        if name not in FLAVORS:
            log.error(
                "Unknown flavor '%s'. Choose from: %s",
                override, ", ".join(FLAVORS),
            )
            sys.exit(1)
        log.debug("Using flavor override: %s", name)
        return _make_flavor(name, f"--flavor {override}")

    cwd = Path.cwd()
    client_dir = find_client_dir(cwd)
    if client_dir:
        product, version = read_client_product(client_dir)
        log.debug(
            "Client %s: product=%r version=%r", client_dir, product, version
        )
        if version:
            name = flavor_from_version(version)
            if name:
                return _make_flavor(name, f"{product} {version}")
        if product and product in PRODUCT_FLAVORS:
            return _make_flavor(PRODUCT_FLAVORS[product], product)

    for part in reversed(cwd.parts):
        folder = part.lower()
        name = INSTALL_FOLDERS.get(folder)
        if name:
            log.debug("Detected flavor %s from folder name %s", name, folder)
            return _make_flavor(name, f"from {folder}")

    log.debug("No WoW client info found, defaulting to retail")
    return _make_flavor("retail", "default")
