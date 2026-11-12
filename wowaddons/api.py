"""CurseForge API helpers, download/extraction, and version comparison."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .constants import ADDON_CLASS_ID, CF_API, WOW_GAME_ID, log
from .ui import clear_line, print_dl_progress
from .utils import format_size, normalize


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_api_key(args: argparse.Namespace) -> str:
    """Retrieve the CurseForge API key from args or the environment."""
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


# ---------------------------------------------------------------------------
# Raw HTTP
# ---------------------------------------------------------------------------


def cf_get(
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
    log.debug("GET %s", url)
    req = urllib.request.Request(url, headers={
        "x-api-key": key,
        "User-Agent": f"wowaddons/{__version__}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            log.debug("Response: %s (%s bytes)", resp.status, len(body))
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            log.error("Invalid or expired API key.")
            log.error("Get a new one at https://console.curseforge.com")
            sys.exit(1)
        if exc.code == 404:
            log.warning("Not found on CurseForge: %s", endpoint)
        else:
            log.warning(
                "CurseForge API returned HTTP %s for %s", exc.code, endpoint
            )
        return None
    except urllib.error.URLError as exc:
        log.warning("Network request failed — %s", exc.reason)
        return None


# ---------------------------------------------------------------------------
# Search / lookup
# ---------------------------------------------------------------------------


def search_addons(
    key: str,
    query: str,
    flavor_id: int | None = None,
) -> list[dict[str, Any]]:
    """Search CurseForge for WoW addons matching *query*."""
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
    resp = cf_get("/v1/mods/search", key, params)
    data = resp.get("data", []) if resp else []
    log.debug("Search returned %s result(s)", len(data))
    return data


def get_mod(key: str, mod_id: int) -> dict[str, Any] | None:
    """Fetch a single mod by its CurseForge ID."""
    log.debug("Fetching mod %s", mod_id)
    resp = cf_get(f"/v1/mods/{mod_id}", key)
    return resp.get("data") if resp else None


def get_files(
    key: str,
    mod_id: int,
    flavor_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch available files for a mod from CurseForge."""
    log.debug("Fetching files for mod %s", mod_id)
    params: dict[str, Any] = {"pageSize": 20}
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    resp = cf_get(f"/v1/mods/{mod_id}/files", key, params)
    data = resp.get("data", []) if resp else []
    if flavor_id and data:
        data = [f for f in data if _file_matches_flavor(f, flavor_id)]
    log.debug("Found %s file(s)", len(data))
    return data


def _file_matches_flavor(file_entry: dict[str, Any], flavor_id: int) -> bool:
    """True if the file is tagged for *flavor_id*, or has no type metadata."""
    versions = file_entry.get("sortableGameVersions") or []
    if not versions:
        return True
    return any(sv.get("gameVersionTypeId") == flavor_id for sv in versions)


def pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the best (stable release) file from a list of CurseForge entries."""
    for file_entry in files:
        if file_entry.get("releaseType", 1) == 1:
            log.debug(
                "Selected release file: %s (id=%s)",
                file_entry.get("displayName", file_entry.get("fileName")),
                file_entry["id"],
            )
            return file_entry
    if files:
        picked = files[0]
        log.debug(
            "No release found, falling back to: %s (type=%s)",
            picked.get("displayName"), picked.get("releaseType"),
        )
        return picked
    return None


def download_url(file_info: dict[str, Any]) -> str:
    """Extract a download URL from a CurseForge file entry."""
    url = file_info.get("downloadUrl")
    if url:
        log.debug("Download URL: %s", url)
        return url
    fid = str(file_info["id"])
    fname = file_info["fileName"]
    url = f"https://edge.forgecdn.net/files/{fid[:4]}/{fid[4:]}/{fname}"
    log.debug("API downloadUrl is null, using CDN fallback: %s", url)
    return url


def search_by_slug(key: str, slug: str) -> dict[str, Any] | None:
    """Search CurseForge for an addon matching an exact slug."""
    params: dict[str, Any] = {"gameId": WOW_GAME_ID, "slug": slug}
    resp = cf_get("/v1/mods/search", key, params)
    data = resp.get("data", []) if resp else []
    return data[0] if data else None


def resolve(
    key: str,
    identifier: str,
    flavor_id: int | None = None,
) -> dict[str, Any] | None:
    """Resolve a slug, name, or numeric ID to a CurseForge addon dict."""
    if identifier.isdigit():
        log.debug("Resolving '%s' as numeric ID", identifier)
        mod = get_mod(key, int(identifier))
        if mod:
            log.debug("Resolved to: %s (slug=%s)", mod["name"], mod["slug"])
            return mod

    log.debug("Searching for '%s'", identifier)
    results = search_addons(key, identifier, flavor_id)

    for result in results:
        if result.get("slug", "").lower() == identifier.lower():
            log.debug("Exact slug match: %s", result["name"])
            return result
    for result in results:
        if result.get("name", "").lower() == identifier.lower():
            log.debug("Exact name match: %s", result["name"])
            return result

    if results:
        log.debug("No exact match, using top result: %s", results[0]["name"])
        return results[0]

    log.debug("No results for '%s'", identifier)
    return None


def match_folder(
    key: str,
    folder_name: str,
    title: str | None = None,
    flavor_id: int | None = None,
) -> dict[str, Any] | None:
    """Try to match a local addon folder to a CurseForge project.

    Attempts slug lookup first, then falls back to text search by title
    and folder name with normalized fuzzy matching.
    """
    norm_folder = normalize(folder_name)
    norm_title = normalize(title) if title else ""

    slug_guess = folder_name.lower()
    log.debug("Trying slug lookup: %s", slug_guess)
    hit = search_by_slug(key, slug_guess)
    if hit:
        log.debug(
            "Matched '%s' via slug → %s (id=%s)",
            folder_name, hit["name"], hit["id"],
        )
        return hit

    def _check(results: list[dict[str, Any]]) -> dict[str, Any] | None:
        for result in results:
            norm_slug = normalize(result.get("slug", ""))
            norm_name = normalize(result.get("name", ""))
            if norm_folder in (norm_slug, norm_name):
                log.debug(
                    "Matched '%s' → %s (slug=%s)",
                    folder_name, result["name"], result["slug"],
                )
                return result
            if norm_title and norm_title in (norm_slug, norm_name):
                log.debug(
                    "Matched title '%s' → %s (slug=%s)",
                    title, result["name"], result["slug"],
                )
                return result
        return None

    if title:
        results = search_addons(key, title, flavor_id)
        hit = _check(results) if results else None
        if hit:
            return hit

    if not title or title != folder_name:
        results = search_addons(key, folder_name, flavor_id)
        hit = _check(results) if results else None
        if hit:
            return hit

    log.debug("No CurseForge match for '%s'", folder_name)
    return None


# ---------------------------------------------------------------------------
# Download & extraction
# ---------------------------------------------------------------------------


def download_extract(
    url: str,
    dest: str,
    label: str = "",
) -> list[str] | None:
    """Download a zip archive and extract its contents to a destination.

    Streams the download with a live progress bar (when ANSI is enabled).

    Args:
        url: Direct download URL for the addon zip archive.
        dest: Destination directory to extract addon folders into.
        label: Display name used in the progress bar.

    Returns:
        A list of extracted folder names, or ``None`` on failure.
    """
    staging = tempfile.mkdtemp(prefix="wowaddons_")
    log.debug("Created staging directory: %s", staging)
    folders: list[str] = []
    try:
        zip_path = os.path.join(staging, "addon.zip")

        log.debug("Downloading: %s", url)
        req = urllib.request.Request(
            url, headers={"User-Agent": f"wowaddons/{__version__}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                chunk_size = 65536

                with open(zip_path, "wb") as fp:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        fp.write(chunk)
                        downloaded += len(chunk)
                        print_dl_progress(label or "addon", downloaded, total)

            clear_line()
            zip_size = os.path.getsize(zip_path)
            log.debug("Downloaded %s", format_size(zip_size))
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            clear_line()
            log.error("Download failed: %s", exc)
            return None

        extract_dir = os.path.join(staging, "extract")
        os.makedirs(extract_dir)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                entries = zf.namelist()
                log.debug("Zip contains %s entries", len(entries))

                extract_root = Path(extract_dir).resolve()
                for member in entries:
                    target = (extract_root / member).resolve()
                    if not str(target).startswith(str(extract_root)):
                        log.error(
                            "Zip contains path traversal entry: %s", member
                        )
                        return None

                folders = sorted(
                    {n.split("/")[0] for n in entries if n.split("/")[0]}
                )
                if not folders:
                    log.error("Zip archive is empty.")
                    return None
                for folder in folders:
                    if folder in (".", "..") or os.sep in folder:
                        log.error(
                            "Zip contains unsafe folder name: %s", folder
                        )
                        return None
                log.debug("Top-level folders: %s", folders)
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            log.error("Downloaded file is not a valid zip archive.")
            return None

        for folder in folders:
            src = os.path.join(extract_dir, folder)
            dst = os.path.join(dest, folder)
            if os.path.exists(dst):
                log.debug("Removing existing: %s", dst)
                shutil.rmtree(dst)
            log.debug("Moving %s → %s", folder, dst)
            shutil.move(src, dst)

        return folders

    finally:
        log.debug("Cleaning up staging: %s", staging)
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def needs_update(
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
       the local folder modification time.

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

    log.debug("Version mismatch: local=%r  cf=%r", local_version, display)

    cf_date_str = cf_file.get("fileDate", "")
    if cf_date_str and local_folders:
        try:
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
                            "Folder '%s' modified after CF upload "
                            "(%s > %s), treating as up to date",
                            folder,
                            mtime.strftime("%Y-%m-%d %H:%M"),
                            cf_time.strftime("%Y-%m-%d %H:%M"),
                        )
                        return False
        except (ValueError, OSError):
            pass

    return True
