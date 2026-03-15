#!/usr/bin/env python3
"""Lightweight WoW Addon Package Manager.

A stateless, pip-like CLI for managing World of Warcraft addons via CurseForge.
Zero external dependencies. Zero stored state. Run from your WoW AddOns folder.

All addon metadata is read from ``.toc`` files at runtime. The API key comes
from the ``CURSEFORGE_API_KEY`` environment variable (or the ``--key`` flag).
The game flavor is auto-detected from the directory path.

Usage::

    wowaddons update
    wowaddons install deadly-boss-mods details
    wowaddons list
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, TypedDict

__version__ = "1.1.0"

CF_API: str = "https://api.curseforge.com"
WOW_GAME_ID: int = 1
ADDON_CLASS_ID: int = 1

FLAVORS: dict[str, int] = {
    "retail": 517,
    "classic": 67408,
    "classic_era": 73713,
}

log: logging.Logger = logging.getLogger("wowaddons")


class ProjectData(TypedDict):
    """Metadata for a locally installed CurseForge addon project."""

    folders: list[str]
    title: str
    version: str


class UnmatchedAddon(TypedDict):
    """Metadata for a local addon folder without a CurseForge project ID."""

    folder: str
    title: str
    version: str


class _LogFormatter(logging.Formatter):
    """Log formatter producing clean output at INFO, labeled at other levels."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record with level-appropriate prefixes.

        Args:
            record: The log record to format.

        Returns:
            The formatted log message string.
        """
        msg = record.getMessage()
        if record.levelno == logging.DEBUG:
            return f"  [DEBUG] {msg}"
        if record.levelno == logging.WARNING:
            return f"  Warning: {msg}"
        if record.levelno >= logging.ERROR:
            return f"  Error: {msg}"
        return msg


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _fmt_size(n: float) -> str:
    """Format a byte count into a human-readable size string.

    Args:
        n: The number of bytes.

    Returns:
        A formatted string such as ``"1.5 MB"`` or ``"32.0 KB"``.
    """
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_count(n: float) -> str:
    """Format a large number with K/M suffixes for compact display.

    Args:
        n: The number to format.

    Returns:
        A compact string such as ``"1.5M"`` or ``"32.1K"``.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _truncate(text: str | None, length: int = 70) -> str:
    """Truncate a string with an ellipsis if it exceeds the given length.

    Args:
        text: The string to truncate, or ``None``.
        length: Maximum allowed length before truncation.

    Returns:
        The original string, a truncated version ending in ``"..."``, or
        an empty string if ``text`` is falsy.
    """
    if not text:
        return ""
    return (text[:length] + "...") if len(text) > length else text


def _normalize(s: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy comparison.

    Args:
        s: The string to normalize.

    Returns:
        A lowercase, alphanumeric-only version of ``s``.
    """
    return "".join(c.lower() for c in s if c.isalnum())


# ---------------------------------------------------------------------------
# .toc parsing & addon detection
# ---------------------------------------------------------------------------


def _parse_toc(toc_path: Path | str) -> dict[str, str]:
    """Parse a WoW ``.toc`` file and return its ``##`` metadata fields.

    Args:
        toc_path: Path to the ``.toc`` file.

    Returns:
        A dict mapping field names to their values
        (e.g. ``{"Title": "Details", "Version": "1.2.3"}``).
    """
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


def _scan_addons() -> tuple[dict[int, ProjectData], list[UnmatchedAddon]]:
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
        meta = _parse_toc(toc)
        title = meta.get("Title", entry.name)
        version = meta.get("Version", "")
        pid_str = meta.get("X-Curse-Project-ID", "")

        log.debug(
            f"{entry.name}: title={title!r}  version={version!r}  "
            f"project_id={pid_str!r}"
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


# ---------------------------------------------------------------------------
# Flavor auto-detection
# ---------------------------------------------------------------------------


def _detect_flavor(override: str | None = None) -> str:
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
                f"Unknown flavor '{override}'. "
                f"Choose from: {', '.join(FLAVORS)}"
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
    log.debug(f"Detected flavor: {flavor}")
    return flavor


# ---------------------------------------------------------------------------
# CurseForge API helpers
# ---------------------------------------------------------------------------


def _api_key(args: argparse.Namespace) -> str:
    """Retrieve the CurseForge API key from args or the environment.

    Args:
        args: Parsed CLI arguments (may contain a ``key`` attribute).

    Returns:
        The API key string.
    """
    key = getattr(args, "key", None) or os.environ.get(
        "CURSEFORGE_API_KEY", ""
    )
    if not key:
        log.error(
            "No API key configured. Run 'wowaddons init' to set one up."
        )
        sys.exit(1)
    log.debug("API key loaded")
    return key


def _api(
    endpoint: str,
    key: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Make a GET request to the CurseForge API.

    Args:
        endpoint: API path (e.g. ``"/v1/mods/search"``).
        key: CurseForge API key.
        params: Optional query-string parameters.

    Returns:
        The parsed JSON response as a dict, or ``None`` on failure.
    """
    url = f"{CF_API}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    log.debug(f"GET {url}")
    req = urllib.request.Request(url, headers={
        "x-api-key": key,
        "User-Agent": f"wowaddons/{__version__}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            log.debug(f"Response: {resp.status} ({len(body)} bytes)")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            log.error("Invalid or expired API key.")
            log.error("Get a new one at https://console.curseforge.com")
            sys.exit(1)
        if exc.code == 404:
            log.warning(f"Not found on CurseForge: {endpoint}")
        else:
            log.warning(
                f"CurseForge API returned HTTP {exc.code} for {endpoint}"
            )
        return None
    except urllib.error.URLError as exc:
        log.warning(f"Network request failed - {exc.reason}")
        return None


def _search(
    key: str,
    query: str,
    flavor_id: int | None = None,
) -> list[dict[str, Any]]:
    """Search CurseForge for WoW addons matching a query.

    Args:
        key: CurseForge API key.
        query: Free-text search string.
        flavor_id: Optional game-version type ID to filter by flavor.

    Returns:
        A list of addon dicts from the API, possibly empty.
    """
    params: dict[str, Any] = {
        "gameId": WOW_GAME_ID,
        "classId": ADDON_CLASS_ID,
        "searchFilter": query,
        "sortField": 2,
        "sortOrder": "desc",
        "pageSize": 10,
    }
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    resp = _api("/v1/mods/search", key, params)
    data = resp.get("data", []) if resp else []
    if not data and flavor_id:
        log.debug("No results with flavor filter, retrying without it")
        params.pop("gameVersionTypeId")
        resp = _api("/v1/mods/search", key, params)
        data = resp.get("data", []) if resp else []
    log.debug(f"Search returned {len(data)} result(s)")
    return data


def _get_mod(key: str, mod_id: int) -> dict[str, Any] | None:
    """Fetch a single mod by its CurseForge ID.

    Args:
        key: CurseForge API key.
        mod_id: The CurseForge project ID.

    Returns:
        The mod data dict, or ``None`` if not found.
    """
    log.debug(f"Fetching mod {mod_id}")
    resp = _api(f"/v1/mods/{mod_id}", key)
    return resp.get("data") if resp else None


def _get_files(
    key: str,
    mod_id: int,
    flavor_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch available files for a mod from CurseForge.

    Args:
        key: CurseForge API key.
        mod_id: The CurseForge project ID.
        flavor_id: Optional game-version type ID to filter by flavor.

    Returns:
        A list of file-info dicts, possibly empty.
    """
    log.debug(f"Fetching files for mod {mod_id}")
    params: dict[str, Any] = {"pageSize": 20}
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    resp = _api(f"/v1/mods/{mod_id}/files", key, params)
    data = resp.get("data", []) if resp else []
    if not data and flavor_id:
        log.debug("No files with flavor filter, retrying without it")
        params.pop("gameVersionTypeId")
        resp = _api(f"/v1/mods/{mod_id}/files", key, params)
        data = resp.get("data", []) if resp else []
    log.debug(f"Found {len(data)} file(s)")
    return data


def _pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the best file from a list of CurseForge file entries.

    Prefers ``releaseType == 1`` (stable). Falls back to the first file
    in the list if no stable release is available.

    Args:
        files: File-info dicts from the CurseForge API.

    Returns:
        The chosen file dict, or ``None`` if ``files`` is empty.
    """
    for f in files:
        if f.get("releaseType", 1) == 1:
            log.debug(
                f"Selected release file: "
                f"{f.get('displayName', f.get('fileName'))} (id={f['id']})"
            )
            return f
    if files:
        picked = files[0]
        log.debug(
            f"No release found, falling back to: "
            f"{picked.get('displayName')} (type={picked.get('releaseType')})"
        )
        return picked
    return None


def _download_url(file_info: dict[str, Any]) -> str:
    """Extract a download URL from a CurseForge file entry.

    Falls back to a CDN URL if the API ``downloadUrl`` field is null.

    Args:
        file_info: A file-info dict from the CurseForge API.

    Returns:
        A direct download URL string.
    """
    url = file_info.get("downloadUrl")
    if url:
        log.debug(f"Download URL: {url}")
        return url
    fid = str(file_info["id"])
    fname = file_info["fileName"]
    url = f"https://edge.forgecdn.net/files/{fid[:4]}/{fid[4:]}/{fname}"
    log.debug(f"API downloadUrl is null, using CDN fallback: {url}")
    return url


def _resolve(
    key: str,
    identifier: str,
    flavor_id: int | None = None,
) -> dict[str, Any] | None:
    """Resolve a slug, name, or numeric ID to a CurseForge addon dict.

    Tries numeric-ID lookup first, then searches by text and matches on
    exact slug, exact name, or falls back to the top search result.

    Args:
        key: CurseForge API key.
        identifier: A slug, addon name, or numeric project ID string.
        flavor_id: Optional game-version type ID to filter by flavor.

    Returns:
        The matched addon dict, or ``None`` if nothing was found.
    """
    if identifier.isdigit():
        log.debug(f"Resolving '{identifier}' as numeric ID")
        mod = _get_mod(key, int(identifier))
        if mod:
            log.debug(
                f"Resolved to: {mod['name']} (slug={mod['slug']})"
            )
            return mod

    log.debug(f"Searching for '{identifier}'")
    results = _search(key, identifier, flavor_id)

    for r in results:
        if r.get("slug", "").lower() == identifier.lower():
            log.debug(f"Exact slug match: {r['name']}")
            return r
    for r in results:
        if r.get("name", "").lower() == identifier.lower():
            log.debug(f"Exact name match: {r['name']}")
            return r

    if results:
        log.debug(
            f"No exact match, using top result: {results[0]['name']}"
        )
        return results[0]

    log.debug(f"No results for '{identifier}'")
    return None


def _search_by_slug(key: str, slug: str) -> dict[str, Any] | None:
    """Search CurseForge for an addon matching an exact slug.

    Args:
        key: CurseForge API key.
        slug: The addon slug to search for.

    Returns:
        The matched addon dict, or ``None`` if not found.
    """
    params: dict[str, Any] = {"gameId": WOW_GAME_ID, "slug": slug}
    resp = _api("/v1/mods/search", key, params)
    data = resp.get("data", []) if resp else []
    return data[0] if data else None


def _match_folder(
    key: str,
    folder_name: str,
    title: str | None = None,
    flavor_id: int | None = None,
) -> dict[str, Any] | None:
    """Try to match a local addon folder to a CurseForge project.

    Attempts slug lookup first, then falls back to text search by title
    and folder name with normalized fuzzy matching.

    Args:
        key: CurseForge API key.
        folder_name: The local addon folder name.
        title: Optional title from the ``.toc`` file.
        flavor_id: Optional game-version type ID to filter by flavor.

    Returns:
        The matched addon dict, or ``None`` if no match was found.
    """
    norm_folder = _normalize(folder_name)
    norm_title = _normalize(title) if title else ""

    slug_guess = folder_name.lower()
    log.debug(f"Trying slug lookup: {slug_guess}")
    hit = _search_by_slug(key, slug_guess)
    if hit:
        log.debug(
            f"Matched '{folder_name}' via slug -> "
            f"{hit['name']} (id={hit['id']})"
        )
        return hit

    def _check(
        results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for r in results:
            ns = _normalize(r.get("slug", ""))
            nn = _normalize(r.get("name", ""))
            if ns == norm_folder or nn == norm_folder:
                log.debug(
                    f"Matched '{folder_name}' -> "
                    f"{r['name']} (slug={r['slug']})"
                )
                return r
            if norm_title and (ns == norm_title or nn == norm_title):
                log.debug(
                    f"Matched title '{title}' -> "
                    f"{r['name']} (slug={r['slug']})"
                )
                return r
        return None

    if title:
        results = _search(key, title)
        hit = _check(results) if results else None
        if hit:
            return hit

    if not title or title != folder_name:
        results = _search(key, folder_name)
        hit = _check(results) if results else None
        if hit:
            return hit

    log.debug(f"No CurseForge match for '{folder_name}'")
    return None


# ---------------------------------------------------------------------------
# Download & extraction
# ---------------------------------------------------------------------------


def _download_extract(url: str, dest: str) -> list[str] | None:
    """Download a zip archive and extract its contents to a destination.

    Downloads to a temporary staging directory, validates the zip, then
    atomically moves the top-level folders into ``dest``. Existing folders
    at the destination are removed first. On failure the temp files are
    cleaned up automatically.

    Args:
        url: Direct download URL for the addon zip archive.
        dest: Destination directory to extract addon folders into.

    Returns:
        A list of extracted folder names, or ``None`` on failure.
    """
    staging = tempfile.mkdtemp(prefix="wowaddons_")
    log.debug(f"Created staging directory: {staging}")
    try:
        zip_path = os.path.join(staging, "addon.zip")

        log.debug(f"Downloading: {url}")
        req = urllib.request.Request(
            url, headers={"User-Agent": f"wowaddons/{__version__}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(zip_path, "wb") as fp:
                    fp.write(resp.read())
            zip_size = os.path.getsize(zip_path)
            log.debug(f"Downloaded {_fmt_size(zip_size)} to staging")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            log.error(f"Download failed: {exc}")
            return None

        extract_dir = os.path.join(staging, "extract")
        os.makedirs(extract_dir)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                entries = zf.namelist()
                log.debug(f"Zip contains {len(entries)} entries")
                folders = sorted(
                    {n.split("/")[0] for n in entries if n.split("/")[0]}
                )
                if not folders:
                    log.error("Zip archive is empty.")
                    return None
                log.debug(f"Top-level folders: {folders}")
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            log.error("Downloaded file is not a valid zip archive.")
            return None

        for folder in folders:
            src = os.path.join(extract_dir, folder)
            dst = os.path.join(dest, folder)
            if os.path.exists(dst):
                log.debug(f"Removing existing: {dst}")
                shutil.rmtree(dst)
            log.debug(f"Moving {folder} -> {dst}")
            shutil.move(src, dst)

        return folders

    finally:
        log.debug(f"Cleaning up staging: {staging}")
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _needs_update(
    local_version: str,
    cf_file: dict[str, Any],
    local_folders: list[str] | None = None,
) -> bool:
    """Determine whether a local addon is outdated vs. a CurseForge file.

    Checks in order:
    1. Skips template/empty versions.
    2. If the local version string appears in the CurseForge display name
       or file name, the addon is up to date.
    3. Falls back to comparing the CurseForge file's upload date against
       the local folder modification time — if any local folder was
       modified after the file was uploaded, the addon is up to date.
       This handles addons whose ``.toc`` version doesn't match the
       CurseForge file naming convention.

    Args:
        local_version: The version string from the local ``.toc`` file.
        cf_file: A file-info dict from the CurseForge API.
        local_folders: Optional list of local folder names to check
            modification times against the CurseForge file date.

    Returns:
        ``True`` if the addon appears to need an update.
    """
    if not local_version or local_version.startswith("@"):
        return False

    display = cf_file.get("displayName", "")
    fname = cf_file.get("fileName", "")
    if local_version in display or local_version in fname:
        return False

    log.debug(f"Version mismatch: local={local_version!r}  cf={display!r}")

    cf_date_str = cf_file.get("fileDate", "")
    if cf_date_str and local_folders:
        try:
            from datetime import datetime, timezone

            cf_date = cf_date_str.replace("Z", "+00:00")
            cf_time = datetime.fromisoformat(cf_date)
            for folder in local_folders:
                p = Path(folder)
                if p.is_dir():
                    mtime = datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime > cf_time:
                        log.debug(
                            f"Folder '{folder}' modified after CF upload "
                            f"({mtime:%Y-%m-%d %H:%M} > "
                            f"{cf_time:%Y-%m-%d %H:%M}), "
                            f"treating as up to date"
                        )
                        return False
        except (ValueError, OSError):
            pass

    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    """Interactively configure the CurseForge API key.

    On Windows the key is persisted via ``setx``. On other platforms the
    user is instructed to add an export to their shell RC file.

    Args:
        args: Parsed CLI arguments (unused beyond signature consistency).
    """
    existing = os.environ.get("CURSEFORGE_API_KEY", "")
    if existing:
        print("  CURSEFORGE_API_KEY is already set.")
        resp = input("  Overwrite it? [y/N] ").strip().lower()
        if resp != "y":
            log.info("Aborted.")
            return

    print(
        "  Get a free CurseForge API key at: https://console.curseforge.com"
    )
    key = input("  Paste your API key: ").strip()
    if not key:
        log.info("Aborted.")
        return

    if sys.platform == "win32":
        import subprocess

        result = subprocess.run(
            ["setx", "CURSEFORGE_API_KEY", key],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            os.environ["CURSEFORGE_API_KEY"] = key
            log.info("API key saved to your user environment variables.")
            log.info(
                "It will be available in all new terminals automatically."
            )
            log.info("This terminal session is also ready to go.")
        else:
            log.error(f"setx failed: {result.stderr.strip()}")
            log.error(
                'You can set it manually: '
                'setx CURSEFORGE_API_KEY "your-key"'
            )
            sys.exit(1)
    else:
        os.environ["CURSEFORGE_API_KEY"] = key
        shell = os.environ.get("SHELL", "")
        rc = "~/.zshrc" if "zsh" in shell else "~/.bashrc"
        log.info("API key set for this session.")
        log.info(f"To make it permanent, add this to {rc}:")
        log.info(f'  export CURSEFORGE_API_KEY="{key}"')


def cmd_install(args: argparse.Namespace) -> None:
    """Install one or more addons from CurseForge.

    Addons can be specified as positional arguments (slugs, names, or
    numeric IDs) and/or loaded from a requirements file via ``-r``.

    Args:
        args: Parsed CLI arguments containing ``addons``, ``requirements``,
            ``flavor``, and ``key``.
    """
    key = _api_key(args)
    flavor = _detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    identifiers: list[str] = list(args.addons or [])

    if args.requirements:
        path = Path(args.requirements)
        if not path.exists():
            log.error(f"File not found: {path}")
            sys.exit(1)
        log.debug(f"Reading requirements from {path}")
        with open(path, encoding="utf-8") as f:
            identifiers.extend(
                line.strip().split("#")[0].strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            )

    if not identifiers:
        log.error("Specify addon(s) to install, or use -r <file>.")
        sys.exit(1)

    by_project, _ = _scan_addons()
    installed_pids: set[int] = set(by_project.keys())

    for ident in identifiers:
        log.info(f"Resolving {ident}...")
        mod = _resolve(key, ident, flavor_id)
        if not mod:
            log.warning(f"Not found: {ident}")
            continue

        name: str = mod["name"]
        mod_id: int = mod["id"]

        if mod_id in installed_pids:
            log.warning(
                f"{name} is already installed. Use 'update' instead."
            )
            continue

        log.info(
            f"  Found: {name}  (slug: {mod['slug']}, id: {mod_id})"
        )

        files = _get_files(key, mod_id, flavor_id)
        latest = _pick_file(files)
        if not latest:
            log.warning(f"No {flavor} files available for {name}.")
            continue

        url = _download_url(latest)
        version = latest.get("displayName", latest.get("fileName", "unknown"))
        size = latest.get("fileLength", 0)
        size_str = f" ({_fmt_size(size)})" if size else ""
        log.info(f"  Downloading {version}{size_str}...")

        folders = _download_extract(url, ".")
        if folders is None:
            continue

        log.info(f"  Installed {name}  [{', '.join(folders)}]")
        installed_pids.add(mod_id)

    log.info("Done.")


def cmd_update(args: argparse.Namespace) -> None:
    """Check for and apply updates to installed addons.

    Scans the current directory for installed addons, queries CurseForge
    for newer versions, and downloads updates as needed. Can target
    specific addons or update all.

    Args:
        args: Parsed CLI arguments containing ``addons``, ``flavor``,
            and ``key``.
    """
    key = _api_key(args)
    flavor = _detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    log.info("Scanning for installed addons...")
    by_project, unmatched = _scan_addons()

    if not by_project and not unmatched:
        log.error("No addons found in the current directory.")
        log.error("Make sure you're in your WoW AddOns folder:")
        log.error(
            "  ...\\World of Warcraft\\_retail_\\Interface\\AddOns\\"
        )
        sys.exit(1)

    _match_unmatched(key, by_project, unmatched, flavor_id)

    if not by_project:
        log.info("No CurseForge addons detected. Nothing to update.")
        return

    log.info(f"{len(by_project)} addon(s) to check for updates.")

    targets = by_project
    if args.addons:
        requested: set[str] = set(args.addons)
        targets = {}
        for pid, data in by_project.items():
            folder_match = any(
                f.lower() in requested for f in data["folders"]
            )
            id_match = str(pid) in requested
            if folder_match or id_match:
                targets[pid] = data
        if not targets:
            log.warning(
                "None of the specified addons were found locally."
            )
            return

    updated = 0
    skipped = 0
    failed = 0

    for pid, data in targets.items():
        name = data["title"]
        local_ver = data["version"]

        log.info(f"  Checking {name}...")

        files = _get_files(key, pid, flavor_id)
        latest = _pick_file(files)

        if not latest:
            log.warning(
                f"No files found on CurseForge for {name} "
                f"(project {pid})."
            )
            failed += 1
            continue

        if not _needs_update(local_ver, latest, data["folders"]):
            log.info(f"  {name} is up to date.")
            skipped += 1
            continue

        url = _download_url(latest)
        version = latest.get("displayName", latest.get("fileName", "unknown"))
        log.info(f"  Updating {name} -> {version}...")

        for folder in data["folders"]:
            p = Path(folder)
            if p.is_dir():
                log.debug(f"Removing old folder: {folder}")
                shutil.rmtree(p)

        folders = _download_extract(url, ".")
        if folders is None:
            failed += 1
            continue

        log.info(f"  Updated {name}.")
        updated += 1

    log.info(
        f"\n{updated} updated, {skipped} up to date, {failed} failed."
    )


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove one or more installed addons.

    Identifies each addon by folder name or CurseForge project ID,
    removes all associated folders, and reports what was deleted.

    Args:
        args: Parsed CLI arguments containing ``addons``.
    """
    by_project, unmatched = _scan_addons()

    folder_to_pid: dict[str, int] = {}
    for pid, data in by_project.items():
        for folder in data["folders"]:
            folder_to_pid[folder.lower()] = pid

    for target in args.addons:
        target_lower = target.lower()

        matched_pid: int | None = None
        if target.isdigit():
            pid = int(target)
            if pid in by_project:
                matched_pid = pid
        else:
            matched_pid = folder_to_pid.get(target_lower)

        if matched_pid is None:
            for u in unmatched:
                if u["folder"].lower() == target_lower:
                    p = Path(u["folder"])
                    if p.is_dir():
                        shutil.rmtree(p)
                        log.info(f"  Removed {u['folder']}/")
                    break
            else:
                log.warning(f"Not found: {target}")
            continue

        data = by_project[matched_pid]
        for folder in data["folders"]:
            p = Path(folder)
            if p.is_dir():
                shutil.rmtree(p)
                log.info(f"  Removed {folder}/")

        log.info(f"Uninstalled {data['title']}.")

    log.info("Done.")


def _match_unmatched(
    key: str,
    by_project: dict[int, ProjectData],
    unmatched: list[UnmatchedAddon],
    flavor_id: int | None = None,
) -> list[UnmatchedAddon]:
    """Try to match unmatched addon folders to CurseForge projects.

    Moves successfully matched addons from ``unmatched`` into
    ``by_project`` (mutated in place).

    Args:
        key: CurseForge API key.
        by_project: Mutable dict of known projects (updated in place).
        unmatched: List of addon folders without a project ID.
        flavor_id: Optional game-version type ID to filter by flavor.

    Returns:
        The subset of ``unmatched`` entries that could not be matched.
    """
    still_unmatched: list[UnmatchedAddon] = []
    if not unmatched:
        return still_unmatched

    log.info(
        f"Found {len(unmatched)} addon(s) without project IDs, "
        f"searching CurseForge..."
    )
    for u in unmatched:
        mod = _match_folder(key, u["folder"], u.get("title"), flavor_id)
        if mod:
            pid: int = mod["id"]
            if pid not in by_project:
                by_project[pid] = ProjectData(
                    folders=[u["folder"]],
                    title=mod["name"],
                    version=u["version"],
                )
                log.info(
                    f"  Matched {u['folder']} -> "
                    f"{mod['name']} (id: {pid})"
                )
            else:
                by_project[pid]["folders"].append(u["folder"])
                log.debug(
                    f"  {u['folder']} grouped under "
                    f"existing project {pid}"
                )
        else:
            still_unmatched.append(u)

    return still_unmatched


def cmd_list(args: argparse.Namespace) -> None:
    """List all detected addons in the current directory.

    Displays CurseForge-linked addons and untracked/custom addons in
    separate tables with title, version, and folder information.

    Args:
        args: Parsed CLI arguments (unused beyond signature consistency).
    """
    by_project, unmatched = _scan_addons()

    if not by_project and not unmatched:
        log.info(
            "No addons found. Make sure you're in your WoW AddOns folder."
        )
        return

    key = os.environ.get("CURSEFORGE_API_KEY", "") or getattr(
        args, "key", None
    )
    if key and unmatched:
        flavor = _detect_flavor(getattr(args, "flavor", None))
        flavor_id = FLAVORS.get(flavor)
        unmatched = _match_unmatched(
            key, by_project, unmatched, flavor_id
        )

    if by_project:
        print(f"\n CurseForge addons ({len(by_project)}):")
        print(f" {'Title':<36} {'Version':<24} {'Folders'}")
        print(" " + "-" * 90)
        for _pid, data in sorted(
            by_project.items(), key=lambda x: x[1]["title"].lower()
        ):
            title = _truncate(data["title"], 34)
            ver = _truncate(data["version"] or "?", 22)
            folders = ", ".join(data["folders"])
            print(f" {title:<36} {ver:<24} {folders}")

    if unmatched:
        print(f"\n Custom/untracked addons ({len(unmatched)}):")
        print(f" {'Folder':<36} {'Version':<24} {'Title'}")
        print(" " + "-" * 90)
        for u in sorted(unmatched, key=lambda x: x["folder"].lower()):
            folder = _truncate(u["folder"], 34)
            ver = _truncate(u["version"] or "?", 22)
            title = _truncate(u["title"], 30)
            print(f" {folder:<36} {ver:<24} {title}")

    total = len(by_project) + len(unmatched)
    print(f"\n {total} addon(s) detected.\n")


def cmd_search(args: argparse.Namespace) -> None:
    """Search CurseForge for addons matching a query.

    Displays results with name, slug, download count, and summary.
    Already-installed addons are tagged with ``[installed]``.

    Args:
        args: Parsed CLI arguments containing ``query``, ``flavor``,
            and ``key``.
    """
    key = _api_key(args)
    flavor = _detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    query = " ".join(args.query)
    log.info(f"Searching for '{query}' ({flavor})...")

    results = _search(key, query, flavor_id)
    if not results:
        log.info("No results.")
        return

    by_project, _ = _scan_addons()
    installed_pids: set[int] = set(by_project.keys())

    print()
    for r in results:
        tag = " [installed]" if r["id"] in installed_pids else ""
        dl = _fmt_count(r.get("downloadCount", 0))
        summary = _truncate(r.get("summary", ""), 70)
        print(f"  {r['name']}{tag}")
        print(
            f"    slug: {r['slug']}  |  id: {r['id']}  |  downloads: {dl}"
        )
        if summary:
            print(f"    {summary}")
        print()


def cmd_info(args: argparse.Namespace) -> None:
    """Display detailed information about a specific addon.

    Shows name, slug, ID, authors, download count, summary, URL, and
    local installation status.

    Args:
        args: Parsed CLI arguments containing ``addon`` and ``key``.
    """
    key = _api_key(args)

    log.info(f"Looking up {args.addon}...")
    mod = _resolve(key, args.addon)
    if not mod:
        log.warning(f"Not found: {args.addon}")
        return

    by_project, _ = _scan_addons()
    local = by_project.get(mod["id"])
    authors = ", ".join(a["name"] for a in mod.get("authors", []))

    print(f"  Name:       {mod['name']}")
    print(f"  Slug:       {mod['slug']}")
    print(f"  ID:         {mod['id']}")
    print(f"  Authors:    {authors}")
    print(f"  Downloads:  {mod.get('downloadCount', 0):,.0f}")
    print(f"  Summary:    {mod.get('summary', 'N/A')}")
    print(
        f"  URL:        https://www.curseforge.com/wow/addons/{mod['slug']}"
    )
    if local:
        print(f"  Installed:  {local['version'] or '?'}")
        print(f"  Folders:    {', '.join(local['folders'])}")
    else:
        print("  Installed:  no")


def cmd_freeze(args: argparse.Namespace) -> None:
    """Print CurseForge project IDs for all installed addons.

    Output format is compatible with ``install -r`` for backup/sharing.

    Args:
        args: Parsed CLI arguments (unused beyond signature consistency).
    """
    by_project, _ = _scan_addons()
    for pid, data in sorted(
        by_project.items(), key=lambda x: x[1]["title"].lower()
    ):
        print(f"{pid}  # {data['title']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = argparse.ArgumentParser(
        prog="wowaddons",
        description="Lightweight WoW Addon Package Manager (CurseForge)",
        epilog=(
            "Run from your WoW AddOns folder. "
            "Set CURSEFORGE_API_KEY env var or pass --key."
        ),
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show debug output (API calls, temp paths, zip contents)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress all output except warnings and errors",
    )
    parser.add_argument(
        "--key",
        metavar="KEY",
        help="CurseForge API key (or set CURSEFORGE_API_KEY env var)",
    )
    parser.add_argument(
        "--flavor",
        choices=FLAVORS.keys(),
        help="Override auto-detected game flavor",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Set up your CurseForge API key")

    p = sub.add_parser("install", help="Install addon(s)")
    p.add_argument(
        "addons", nargs="*",
        help="Addon slug(s), name(s), or CurseForge ID(s)",
    )
    p.add_argument(
        "-r", "--requirements",
        metavar="FILE",
        help="Install from a text file (one per line)",
    )

    p = sub.add_parser("update", help="Update installed addon(s)")
    p.add_argument(
        "addons", nargs="*",
        help="Folder name(s) or project ID(s) to update (omit for all)",
    )

    for name in ("remove", "uninstall"):
        p = sub.add_parser(name, help="Remove addon(s)")
        p.add_argument(
            "addons", nargs="+",
            help="Folder name(s) or project ID(s) to remove",
        )

    sub.add_parser("list", help="List all detected addons")

    p = sub.add_parser("search", help="Search CurseForge for addons")
    p.add_argument("query", nargs="+", help="Search terms")

    p = sub.add_parser("info", help="Show details for an addon")
    p.add_argument("addon", help="Addon slug, name, or CurseForge ID")

    sub.add_parser(
        "freeze",
        help="Print CurseForge project IDs (for use with install -r)",
    )

    args = parser.parse_args()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_LogFormatter())
    log.addHandler(handler)

    if args.verbose:
        log.setLevel(logging.DEBUG)
    elif args.quiet:
        log.setLevel(logging.WARNING)
    else:
        log.setLevel(logging.INFO)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch: dict[str, Any] = {
        "init": cmd_init,
        "install": cmd_install,
        "update": cmd_update,
        "remove": cmd_remove,
        "uninstall": cmd_remove,
        "list": cmd_list,
        "search": cmd_search,
        "info": cmd_info,
        "freeze": cmd_freeze,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
