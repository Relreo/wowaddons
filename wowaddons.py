#!/usr/bin/env python3
"""wowaddons - Lightweight WoW Addon Package Manager

A stateless, pip-like CLI for managing World of Warcraft addons via CurseForge.
Zero external dependencies. Zero stored state. Run from your WoW AddOns folder.

All addon metadata is read from .toc files at runtime. The API key comes from
the CURSEFORGE_API_KEY environment variable (or the --key flag). The game
flavor is auto-detected from the directory path.

Usage:
    wowaddons update
    wowaddons install deadly-boss-mods details
    wowaddons list
"""

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
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.1.0"

CF_API = "https://api.curseforge.com"
WOW_GAME_ID = 1
ADDON_CLASS_ID = 6

FLAVORS = {
    "retail": 517,
    "classic": 67408,
    "classic_era": 73713,
}

log = logging.getLogger("wowaddons")


class _LogFormatter(logging.Formatter):
    """Clean output at INFO, labeled at other levels."""
    def format(self, record):
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

def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_count(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _truncate(text, length=70):
    if not text:
        return ""
    return (text[:length] + "...") if len(text) > length else text


def _normalize(s):
    """Strip non-alphanumeric chars and lowercase for fuzzy comparison."""
    return "".join(c.lower() for c in s if c.isalnum())


# ---------------------------------------------------------------------------
# .toc parsing & addon detection
# ---------------------------------------------------------------------------

def _parse_toc(toc_path):
    """Parse a WoW .toc file and return a dict of its ## metadata fields."""
    meta = {}
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


def _scan_addons():
    """Scan cwd for addon folders, reading .toc metadata.

    Returns (by_project, unmatched):
      by_project = {project_id: {"folders": [...], "title": str, "version": str}}
      unmatched  = [{"folder": str, "title": str, "version": str}]
    """
    by_project = {}
    unmatched = []

    for entry in sorted(Path.cwd().iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("Blizzard_", ".")):
            continue

        toc_files = list(entry.glob("*.toc"))
        if not toc_files:
            continue

        toc = next((t for t in toc_files if t.stem == entry.name), toc_files[0])
        meta = _parse_toc(toc)
        title = meta.get("Title", entry.name)
        version = meta.get("Version", "")
        pid_str = meta.get("X-Curse-Project-ID", "")

        log.debug(f"{entry.name}: title={title!r}  version={version!r}  project_id={pid_str!r}")

        if pid_str and pid_str.isdigit():
            pid = int(pid_str)
            if pid in by_project:
                by_project[pid]["folders"].append(entry.name)
            else:
                by_project[pid] = {
                    "folders": [entry.name],
                    "title": title,
                    "version": version,
                }
        else:
            unmatched.append({
                "folder": entry.name,
                "title": title,
                "version": version,
            })

    return by_project, unmatched


# ---------------------------------------------------------------------------
# Flavor auto-detection
# ---------------------------------------------------------------------------

def _detect_flavor(override=None):
    """Auto-detect game flavor from directory path, or use override."""
    if override:
        if override not in FLAVORS:
            log.error(f"Unknown flavor '{override}'. Choose from: {', '.join(FLAVORS)}")
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

def _api_key(args):
    key = getattr(args, "key", None) or os.environ.get("CURSEFORGE_API_KEY", "")
    if not key:
        log.error("No API key configured. Run 'wowaddons init' to set one up.")
        sys.exit(1)
    log.debug("API key loaded")
    return key


def _api(endpoint, key, params=None):
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
            log.warning(f"CurseForge API returned HTTP {exc.code} for {endpoint}")
        return None
    except urllib.error.URLError as exc:
        log.warning(f"Network request failed - {exc.reason}")
        return None


def _search(key, query, flavor_id=None):
    params = {
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
    log.debug(f"Search returned {len(data)} result(s)")
    return data


def _get_mod(key, mod_id):
    log.debug(f"Fetching mod {mod_id}")
    resp = _api(f"/v1/mods/{mod_id}", key)
    return resp.get("data") if resp else None


def _get_files(key, mod_id, flavor_id=None):
    log.debug(f"Fetching files for mod {mod_id}")
    params = {"pageSize": 20}
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    resp = _api(f"/v1/mods/{mod_id}/files", key, params)
    data = resp.get("data", []) if resp else []
    log.debug(f"Found {len(data)} file(s)")
    return data


def _pick_file(files):
    """Return the latest release-quality file, falling back to any file."""
    for f in files:
        if f.get("releaseType", 1) == 1:
            log.debug(f"Selected release file: {f.get('displayName', f.get('fileName'))} (id={f['id']})")
            return f
    if files:
        picked = files[0]
        log.debug(f"No release found, falling back to: {picked.get('displayName')} (type={picked.get('releaseType')})")
        return picked
    return None


def _download_url(file_info):
    """Get a download URL, falling back to the CDN if the API field is null."""
    url = file_info.get("downloadUrl")
    if url:
        log.debug(f"Download URL: {url}")
        return url
    fid = str(file_info["id"])
    fname = file_info["fileName"]
    url = f"https://edge.forgecdn.net/files/{fid[:4]}/{fid[4:]}/{fname}"
    log.debug(f"API downloadUrl is null, using CDN fallback: {url}")
    return url


def _resolve(key, identifier, flavor_id=None):
    """Resolve a slug, name, or numeric ID to a CurseForge addon dict."""
    if identifier.isdigit():
        log.debug(f"Resolving '{identifier}' as numeric ID")
        mod = _get_mod(key, int(identifier))
        if mod:
            log.debug(f"Resolved to: {mod['name']} (slug={mod['slug']})")
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
        log.debug(f"No exact match, using top result: {results[0]['name']}")
        return results[0]

    log.debug(f"No results for '{identifier}'")
    return None


def _search_by_slug(key, slug):
    """Search CurseForge by exact slug match."""
    params = {"gameId": WOW_GAME_ID, "slug": slug}
    resp = _api("/v1/mods/search", key, params)
    data = resp.get("data", []) if resp else []
    return data[0] if data else None


def _match_folder(key, folder_name, title=None, flavor_id=None):
    """Try to match a local addon to CurseForge by slug, then title search."""
    norm_folder = _normalize(folder_name)
    norm_title = _normalize(title) if title else ""

    # Try exact slug lookup first (most reliable)
    slug_guess = folder_name.lower()
    log.debug(f"Trying slug lookup: {slug_guess}")
    hit = _search_by_slug(key, slug_guess)
    if hit:
        log.debug(f"Matched '{folder_name}' via slug -> {hit['name']} (id={hit['id']})")
        return hit

    def _check(results):
        for r in results:
            ns = _normalize(r.get("slug", ""))
            nn = _normalize(r.get("name", ""))
            if ns == norm_folder or nn == norm_folder:
                log.debug(f"Matched '{folder_name}' -> {r['name']} (slug={r['slug']})")
                return r
            if norm_title and (ns == norm_title or nn == norm_title):
                log.debug(f"Matched title '{title}' -> {r['name']} (slug={r['slug']})")
                return r
        return None

    # Fall back to text search by title, then folder name
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

def _download_extract(url, dest):
    """Download zip to temp dir, extract there, then move folders into *dest*.

    Nothing touches the destination until the download and extraction are both
    fully validated.  On failure the temp files are cleaned up automatically.
    """
    staging = tempfile.mkdtemp(prefix="wowaddons_")
    log.debug(f"Created staging directory: {staging}")
    try:
        zip_path = os.path.join(staging, "addon.zip")

        log.debug(f"Downloading: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": f"wowaddons/{__version__}"})
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
                folders = sorted({n.split("/")[0] for n in entries if n.split("/")[0]})
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

def _needs_update(local_version, cf_file):
    """Return True if the local addon looks outdated vs the CurseForge file."""
    if not local_version or local_version.startswith("@"):
        return False

    display = cf_file.get("displayName", "")
    fname = cf_file.get("fileName", "")
    if local_version in display or local_version in fname:
        return False

    log.debug(f"Version mismatch: local={local_version!r}  cf={display!r}")
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    existing = os.environ.get("CURSEFORGE_API_KEY", "")
    if existing:
        print(f"  CURSEFORGE_API_KEY is already set.")
        resp = input("  Overwrite it? [y/N] ").strip().lower()
        if resp != "y":
            log.info("Aborted.")
            return

    print("  Get a free CurseForge API key at: https://console.curseforge.com")
    key = input("  Paste your API key: ").strip()
    if not key:
        log.info("Aborted.")
        return

    if sys.platform == "win32":
        import subprocess
        result = subprocess.run(
            ["setx", "CURSEFORGE_API_KEY", key],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            os.environ["CURSEFORGE_API_KEY"] = key
            log.info("API key saved to your user environment variables.")
            log.info("It will be available in all new terminals automatically.")
            log.info("This terminal session is also ready to go.")
        else:
            log.error(f"setx failed: {result.stderr.strip()}")
            log.error("You can set it manually: setx CURSEFORGE_API_KEY \"your-key\"")
            sys.exit(1)
    else:
        os.environ["CURSEFORGE_API_KEY"] = key
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            rc = "~/.zshrc"
        else:
            rc = "~/.bashrc"
        log.info(f"API key set for this session.")
        log.info(f"To make it permanent, add this to {rc}:")
        log.info(f'  export CURSEFORGE_API_KEY="{key}"')


def cmd_install(args):
    key = _api_key(args)
    flavor = _detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    identifiers = list(args.addons or [])

    if args.requirements:
        path = Path(args.requirements)
        if not path.exists():
            log.error(f"File not found: {path}")
            sys.exit(1)
        log.debug(f"Reading requirements from {path}")
        with open(path, encoding="utf-8") as f:
            identifiers.extend(
                line.strip().split("#")[0].strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            )

    if not identifiers:
        log.error("Specify addon(s) to install, or use -r <file>.")
        sys.exit(1)

    by_project, _ = _scan_addons()
    installed_pids = set(by_project.keys())

    for ident in identifiers:
        log.info(f"Resolving {ident}...")
        mod = _resolve(key, ident, flavor_id)
        if not mod:
            log.warning(f"Not found: {ident}")
            continue

        name = mod["name"]
        mod_id = mod["id"]

        if mod_id in installed_pids:
            log.warning(f"{name} is already installed. Use 'update' instead.")
            continue

        log.info(f"  Found: {name}  (slug: {mod['slug']}, id: {mod_id})")

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


def cmd_update(args):
    key = _api_key(args)
    flavor = _detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    log.info("Scanning for installed addons...")
    by_project, unmatched = _scan_addons()

    if not by_project and not unmatched:
        log.error("No addons found in the current directory.")
        log.error("Make sure you're in your WoW AddOns folder:")
        log.error("  ...\\World of Warcraft\\_retail_\\Interface\\AddOns\\")
        sys.exit(1)

    if unmatched:
        log.info(f"Found {len(unmatched)} addon(s) without project IDs, searching CurseForge...")
        for u in unmatched:
            mod = _match_folder(key, u["folder"], u.get("title"), flavor_id)
            if mod:
                pid = mod["id"]
                if pid not in by_project:
                    by_project[pid] = {
                        "folders": [u["folder"]],
                        "title": mod["name"],
                        "version": u["version"],
                    }
                    log.info(f"  Matched {u['folder']} -> {mod['name']} (id: {pid})")
                else:
                    by_project[pid]["folders"].append(u["folder"])
                    log.debug(f"  {u['folder']} grouped under existing project {pid}")

    if not by_project:
        log.info("No CurseForge addons detected. Nothing to update.")
        return

    log.info(f"{len(by_project)} addon(s) to check for updates.")

    targets = by_project
    if args.addons:
        requested = set(args.addons)
        targets = {}
        for pid, data in by_project.items():
            folder_match = any(f.lower() in requested for f in data["folders"])
            id_match = str(pid) in requested
            if folder_match or id_match:
                targets[pid] = data
        if not targets:
            log.warning("None of the specified addons were found locally.")
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
            log.warning(f"No files found on CurseForge for {name} (project {pid}).")
            failed += 1
            continue

        if not _needs_update(local_ver, latest):
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

    log.info(f"\n{updated} updated, {skipped} up to date, {failed} failed.")


def cmd_remove(args):
    by_project, unmatched = _scan_addons()

    folder_to_pid = {}
    for pid, data in by_project.items():
        for folder in data["folders"]:
            folder_to_pid[folder.lower()] = pid

    for target in args.addons:
        target_lower = target.lower()

        matched_pid = None
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


def cmd_list(args):
    by_project, unmatched = _scan_addons()

    if not by_project and not unmatched:
        log.info("No addons found. Make sure you're in your WoW AddOns folder.")
        return

    if by_project:
        print(f"\n CurseForge addons ({len(by_project)}):")
        print(f" {'Title':<36} {'Version':<24} {'Folders'}")
        print(" " + "-" * 90)
        for pid, data in sorted(by_project.items(), key=lambda x: x[1]["title"].lower()):
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


def cmd_search(args):
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
    installed_pids = set(by_project.keys())

    print()
    for r in results:
        tag = " [installed]" if r["id"] in installed_pids else ""
        dl = _fmt_count(r.get("downloadCount", 0))
        summary = _truncate(r.get("summary", ""), 70)
        print(f"  {r['name']}{tag}")
        print(f"    slug: {r['slug']}  |  id: {r['id']}  |  downloads: {dl}")
        if summary:
            print(f"    {summary}")
        print()


def cmd_info(args):
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
    print(f"  URL:        https://www.curseforge.com/wow/addons/{mod['slug']}")
    if local:
        print(f"  Installed:  {local['version'] or '?'}")
        print(f"  Folders:    {', '.join(local['folders'])}")
    else:
        print(f"  Installed:  no")


def cmd_freeze(args):
    by_project, _ = _scan_addons()
    for pid, data in sorted(by_project.items(), key=lambda x: x[1]["title"].lower()):
        print(f"{pid}  # {data['title']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="wowaddons",
        description="Lightweight WoW Addon Package Manager (CurseForge)",
        epilog="Run from your WoW AddOns folder. Set CURSEFORGE_API_KEY env var or pass --key.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show debug output (API calls, temp paths, zip contents)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress all output except warnings and errors",
    )
    parser.add_argument(
        "--key", metavar="KEY",
        help="CurseForge API key (or set CURSEFORGE_API_KEY env var)",
    )
    parser.add_argument(
        "--flavor", choices=FLAVORS.keys(),
        help="Override auto-detected game flavor",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Set up your CurseForge API key")

    # install
    p = sub.add_parser("install", help="Install addon(s)")
    p.add_argument("addons", nargs="*", help="Addon slug(s), name(s), or CurseForge ID(s)")
    p.add_argument("-r", "--requirements", metavar="FILE", help="Install from a text file (one per line)")

    # update
    p = sub.add_parser("update", help="Update installed addon(s)")
    p.add_argument("addons", nargs="*", help="Folder name(s) or project ID(s) to update (omit for all)")

    # remove / uninstall
    for name in ("remove", "uninstall"):
        p = sub.add_parser(name, help="Remove addon(s)")
        p.add_argument("addons", nargs="+", help="Folder name(s) or project ID(s) to remove")

    # list
    sub.add_parser("list", help="List all detected addons")

    # search
    p = sub.add_parser("search", help="Search CurseForge for addons")
    p.add_argument("query", nargs="+", help="Search terms")

    # info
    p = sub.add_parser("info", help="Show details for an addon")
    p.add_argument("addon", help="Addon slug, name, or CurseForge ID")

    # freeze
    sub.add_parser("freeze", help="Print CurseForge project IDs (for use with install -r)")

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

    dispatch = {
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
