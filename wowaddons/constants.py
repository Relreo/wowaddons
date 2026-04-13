"""Global constants and shared logger."""

from __future__ import annotations

import logging

CF_API: str = "https://api.curseforge.com"
WOW_GAME_ID: int = 1
ADDON_CLASS_ID: int = 1
CACHE_FILE: str = ".wowaddons_cache.json"

FLAVORS: dict[str, int] = {
    "retail": 517,
    "classic": 67408,
    "classic_era": 73713,
}

log: logging.Logger = logging.getLogger("wowaddons")
