"""Global constants and shared logger."""

from __future__ import annotations

import logging

CF_API: str = "https://api.curseforge.com"
WOW_GAME_ID: int = 1
ADDON_CLASS_ID: int = 1

# CurseForge gameVersionTypeId values (WowUp curseforge-v2 / CF API).
FLAVORS: dict[str, int] = {
    "retail": 517,
    "classic_era": 67408,  # Vanilla / Anniversary / SoD (_classic_era_)
    "tbc": 73246,
    "wrath": 73713,
    "cata": 77522,
    "classic": 79434,  # MoP Classic — current _classic_ client
    "forever": 88568,  # WoW Forever (_classic_beta_)
}

FLAVOR_LABELS: dict[str, str] = {
    "retail": "Retail",
    "classic": "MoP Classic",
    "classic_era": "Classic Era",
    "tbc": "TBC Classic",
    "wrath": "Wrath Classic",
    "cata": "Cataclysm Classic",
    "forever": "WoW Forever",
}

FLAVOR_ALIASES: dict[str, str] = {
    "classic_beta": "forever",
    "wow_forever": "forever",
    "mop": "classic",
    "vanilla": "classic_era",
    "era": "classic_era",
}

# Battle.net product codes (from a client's .flavor.info) → canonical flavor.
# Used when .build.info has no version for the product.
PRODUCT_FLAVORS: dict[str, str] = {
    "wow": "retail",
    "wowt": "retail",
    "wowxptr": "retail",
    "wow_beta": "retail",
    "wow_classic": "classic",
    "wow_classic_ptr": "classic",
    "wow_classic_era": "classic_era",
    "wow_classic_era_ptr": "classic_era",
    "wow_anniversary": "tbc",
    "wow_classic_beta": "forever",
}

# Exact WoW install-folder names (path components) → canonical flavor.
# Last-resort fallback when no .flavor.info is found.
INSTALL_FOLDERS: dict[str, str] = {
    "_retail_": "retail",
    "_ptr_": "retail",
    "_xptr_": "retail",
    "_beta_": "retail",
    "_classic_": "classic",
    "_classic_ptr_": "classic",
    "_classic_era_": "classic_era",
    "_classic_era_ptr_": "classic_era",
    "_classic_beta_": "forever",
    "_forever_": "forever",
}

FLAVOR_CHOICES: tuple[str, ...] = tuple(FLAVORS) + tuple(FLAVOR_ALIASES)

log: logging.Logger = logging.getLogger("wowaddons")
