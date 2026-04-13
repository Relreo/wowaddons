"""Local addon ID cache — avoids re-querying CurseForge on every run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import CACHE_FILE, log
from .models import ProjectData, UnmatchedAddon


def load_id_cache() -> dict[str, dict[str, Any]]:
    """Load the local addon ID cache from ``.wowaddons_cache.json``.

    Returns:
        A dict mapping folder names to ``{id, name, slug, matched_at}``,
        or an empty dict if the cache file is missing or unreadable.
    """
    cache_path = Path.cwd() / CACHE_FILE
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version") == 1:
            return data.get("entries", {})
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_id_cache(cache: dict[str, dict[str, Any]]) -> None:
    """Persist the addon ID cache to ``.wowaddons_cache.json``."""
    cache_path = Path.cwd() / CACHE_FILE
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": cache}, f, indent=2)
        log.debug("Saved %s ID cache entry(s) to %s", len(cache), CACHE_FILE)
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
