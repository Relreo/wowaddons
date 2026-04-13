"""Local addon ID cache — avoids re-querying CurseForge on every run."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import log
from .models import ProjectData, UnmatchedAddon


def _addons_root(root: Path | None) -> Path:
    return (root or Path.cwd()).resolve()


def _user_data_dir() -> Path:
    """Per-user directory for wowaddons data (stdlib only, no platformdirs)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "wowaddons"
        return Path.home() / "AppData" / "Local" / "wowaddons"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wowaddons"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "wowaddons"
    return Path.home() / ".local" / "share" / "wowaddons"


def id_cache_path(addons_root: Path | None = None) -> Path:
    """JSON file path for the ID cache of a given AddOns directory."""
    root = _addons_root(addons_root)
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return _user_data_dir() / "caches" / f"{digest}.json"


def _read_cache_entries(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version") == 1:
            entries = data.get("entries", {})
            if isinstance(entries, dict):
                return entries
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def load_id_cache(addons_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the addon ID cache for the current (or given) AddOns directory."""
    path = id_cache_path(addons_root)
    entries = _read_cache_entries(path)
    if entries:
        log.debug("Loaded ID cache from %s", path)
    return entries


def save_id_cache(
    cache: dict[str, dict[str, Any]],
    addons_root: Path | None = None,
) -> None:
    """Persist the addon ID cache for the current (or given) AddOns directory."""
    path = id_cache_path(addons_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": cache}, f, indent=2)
        log.debug("Saved %s ID cache entry(s) to %s", len(cache), path)
    except OSError as exc:
        log.debug("Could not write ID cache: %s", exc)


def apply_id_cache(
    cache: dict[str, dict[str, Any]],
    by_project: dict[int, ProjectData],
    unmatched: list[UnmatchedAddon],
) -> tuple[list[UnmatchedAddon], set[int]]:
    """Promote unmatched addons to ``by_project`` using cached IDs.

    Args:
        cache: The loaded ID cache (folder → entry).
        by_project: Mutable project dict (updated in place).
        unmatched: Addons without a ``.toc`` project ID.

    Returns:
        A tuple of ``(still_unmatched, cache_sourced_pids)`` — addons with
        no cache entry and the set of project IDs that came from cache.
    """
    still_unmatched: list[UnmatchedAddon] = []
    cache_sourced_pids: set[int] = set()

    for u in unmatched:
        entry = cache.get(u["folder"])
        if entry:
            pid: int = entry["id"]
            if pid not in by_project:
                by_project[pid] = ProjectData(
                    folders=[u["folder"]],
                    title=entry.get("name", u["title"]),
                    version=u["version"],
                )
            else:
                by_project[pid]["folders"].append(u["folder"])
            cache_sourced_pids.add(pid)
            log.debug(
                "[cache] %s → %s (id: %s)",
                u["folder"], entry.get("name"), pid,
            )
        else:
            still_unmatched.append(u)

    return still_unmatched, cache_sourced_pids


def record_match(
    cache: dict[str, dict[str, Any]],
    folder: str,
    mod_id: int,
    name: str,
    slug: str,
) -> None:
    """Write a newly discovered folder → project mapping into *cache*."""
    cache[folder] = {
        "id": mod_id,
        "name": name,
        "slug": slug,
        "matched_at": datetime.now(timezone.utc).isoformat(),
    }
