#!/usr/bin/env python3
"""wowaddons - Lightweight WoW Addon Package Manager

A pip-like CLI tool for managing World of Warcraft addons via CurseForge.
Zero external dependencies. Run from your WoW AddOns folder.

Usage:
    wowaddons init
    wowaddons install deadly-boss-mods details
    wowaddons update
    wowaddons list
"""

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

__version__ = "1.0.0"

MANIFEST = "wowaddons.json"
CF_API = "https://api.curseforge.com"
WOW_GAME_ID = 1
ADDON_CLASS_ID = 6

FLAVORS = {
    "retail": 517,
    "classic": 67408,
    "classic_era": 73713,
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _ts():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# Manifest (wowaddons.json)
# ---------------------------------------------------------------------------

def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"api_key": "", "flavor": "retail", "addons": {}}


def save_manifest(data):
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CurseForge API helpers
# ---------------------------------------------------------------------------

def _api_key(manifest):
    key = os.environ.get("CURSEFORGE_API_KEY") or manifest.get("api_key", "")
    if not key:
        print("No API key configured.")
        print("Get a free key at: https://console.curseforge.com")
        print("Then run:  wowaddons init")
        sys.exit(1)
    return key


def _api(endpoint, key, params=None):
    url = f"{CF_API}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "x-api-key": key,
        "User-Agent": f"wowaddons/{__version__}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print("Error: Invalid or expired API key.")
            print("Get a new one at https://console.curseforge.com")
        elif exc.code == 404:
            print("Error: Resource not found on CurseForge.")
        else:
            print(f"Error: CurseForge API returned HTTP {exc.code}.")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Error: Network request failed - {exc.reason}")
        sys.exit(1)


def _search(key, query, flavor_id=None):
    params = {
        "gameId": WOW_GAME_ID,
        "classId": ADDON_CLASS_ID,
        "searchFilter": query,
        "sortField": 2,       # popularity
        "sortOrder": "desc",
        "pageSize": 10,
    }
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    return _api("/v1/mods/search", key, params).get("data", [])


def _get_mod(key, mod_id):
    return _api(f"/v1/mods/{mod_id}", key).get("data")


def _get_files(key, mod_id, flavor_id=None):
    params = {"pageSize": 20}
    if flavor_id:
        params["gameVersionTypeId"] = flavor_id
    return _api(f"/v1/mods/{mod_id}/files", key, params).get("data", [])


def _pick_file(files):
    """Return the latest release-quality file, falling back to any file."""
    for f in files:
        if f.get("releaseType", 1) == 1:
            return f
    return files[0] if files else None


def _download_url(file_info):
    """Get a download URL, falling back to the CDN if the API field is null."""
    url = file_info.get("downloadUrl")
    if url:
        return url
    fid = str(file_info["id"])
    fname = file_info["fileName"]
    return f"https://edge.forgecdn.net/files/{fid[:4]}/{fid[4:]}/{fname}"


def _resolve(key, identifier, flavor_id=None):
    """Resolve a slug, name, or numeric ID to a CurseForge addon dict."""
    if identifier.isdigit():
        mod = _get_mod(key, int(identifier))
        if mod:
            return mod

    results = _search(key, identifier, flavor_id)

    for r in results:
        if r.get("slug", "").lower() == identifier.lower():
            return r
    for r in results:
        if r.get("name", "").lower() == identifier.lower():
            return r

    return results[0] if results else None


# ---------------------------------------------------------------------------
# Download & extraction
# ---------------------------------------------------------------------------

def _download_extract(url, dest):
    """Download zip to temp dir, extract there, then move folders into *dest*.

    Nothing touches the destination until the download and extraction are both
    fully validated.  On failure the temp files are cleaned up automatically.
    """
    staging = tempfile.mkdtemp(prefix="wowaddons_")
    try:
        zip_path = os.path.join(staging, "addon.zip")

        req = urllib.request.Request(url, headers={"User-Agent": f"wowaddons/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(zip_path, "wb") as fp:
                    fp.write(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"  Download failed: {exc}")
            return None

        extract_dir = os.path.join(staging, "extract")
        os.makedirs(extract_dir)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                folders = sorted({n.split("/")[0] for n in zf.namelist() if n.split("/")[0]})
                if not folders:
                    print("  Error: Zip archive is empty.")
                    return None
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            print("  Error: Downloaded file is not a valid zip archive.")
            return None

        for folder in folders:
            src = os.path.join(extract_dir, folder)
            dst = os.path.join(dest, folder)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.move(src, dst)

        return folders

    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Addon directory sanity check
# ---------------------------------------------------------------------------

def _check_dir():
    cwd = Path.cwd()
    looks_ok = (
        cwd.name == "AddOns"
        or os.path.exists(MANIFEST)
        or any(p.name.startswith("Blizzard_") for p in cwd.iterdir() if p.is_dir())
    )
    if not looks_ok:
        print(f"Warning: '{cwd}' doesn't look like a WoW AddOns folder.")
        print("Typically: ...\\World of Warcraft\\_retail_\\Interface\\AddOns\\")
        print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    manifest = load_manifest()

    if args.key:
        manifest["api_key"] = args.key
    else:
        print("Get a free CurseForge API key at: https://console.curseforge.com")
        key = input("API key: ").strip()
        if not key:
            print("Aborted.")
            return
        manifest["api_key"] = key

    if args.flavor:
        manifest["flavor"] = args.flavor
    else:
        current = manifest.get("flavor", "retail")
        print(f"Game flavor ({', '.join(FLAVORS)}) [{current}]: ", end="")
        choice = input().strip().lower()
        if choice:
            if choice not in FLAVORS:
                print(f"Unknown flavor. Choose from: {', '.join(FLAVORS)}")
                sys.exit(1)
            manifest["flavor"] = choice

    save_manifest(manifest)
    print(f"Saved. flavor={manifest['flavor']}")


def cmd_install(args):
    manifest = load_manifest()
    key = _api_key(manifest)
    flavor = manifest.get("flavor", "retail")
    flavor_id = FLAVORS.get(flavor)
    addons = manifest.setdefault("addons", {})

    identifiers = list(args.addons or [])

    if args.requirements:
        path = Path(args.requirements)
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        with open(path, encoding="utf-8") as f:
            identifiers.extend(
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            )

    if not identifiers:
        print("Specify addon(s) to install, or use -r <file>.")
        sys.exit(1)

    _check_dir()

    for ident in identifiers:
        print(f"Resolving {ident}...")
        mod = _resolve(key, ident, flavor_id)
        if not mod:
            print(f"  Not found: {ident}\n")
            continue

        slug = mod["slug"]
        name = mod["name"]
        mod_id = mod["id"]

        if slug in addons:
            print(f"  {name} already installed. Use 'update' instead.\n")
            continue

        print(f"  Found: {name}  (slug: {slug}, id: {mod_id})")

        files = _get_files(key, mod_id, flavor_id)
        latest = _pick_file(files)
        if not latest:
            print(f"  No {flavor} files available.\n")
            continue

        url = _download_url(latest)
        version = latest.get("displayName", latest.get("fileName", "unknown"))
        size = latest.get("fileLength", 0)
        size_str = f" ({_fmt_size(size)})" if size else ""
        print(f"  Downloading {version}{size_str}...")

        folders = _download_extract(url, ".")
        if folders is None:
            print()
            continue

        addons[slug] = {
            "id": mod_id,
            "name": name,
            "slug": slug,
            "file_id": latest["id"],
            "version": version,
            "folders": folders,
            "installed_at": _ts(),
        }
        save_manifest(manifest)
        print(f"  Installed {name}  [{', '.join(folders)}]\n")

    print("Done.")


def cmd_update(args):
    manifest = load_manifest()
    key = _api_key(manifest)
    flavor = manifest.get("flavor", "retail")
    flavor_id = FLAVORS.get(flavor)
    addons = manifest.get("addons", {})

    if not addons:
        print("No addons installed.")
        return

    targets = addons
    if args.addons:
        targets = {}
        for a in args.addons:
            if a in addons:
                targets[a] = addons[a]
            else:
                print(f"Not installed: {a}")
        if not targets:
            return

    updated = 0
    for slug, info in targets.items():
        mod_id = info["id"]
        cur_fid = info.get("file_id", 0)

        sys.stdout.write(f"  {info['name']}... ")
        sys.stdout.flush()

        files = _get_files(key, mod_id, flavor_id)
        latest = _pick_file(files)

        if not latest:
            print("no files found.")
            continue
        if latest["id"] == cur_fid:
            print("up to date.")
            continue

        url = _download_url(latest)
        version = latest.get("displayName", latest.get("fileName", "unknown"))
        print(f"updating to {version}...")

        for folder in info.get("folders", []):
            p = Path(folder)
            if p.is_dir():
                shutil.rmtree(p)

        folders = _download_extract(url, ".")
        if folders is None:
            continue

        addons[slug].update({
            "file_id": latest["id"],
            "version": version,
            "folders": folders,
            "updated_at": _ts(),
        })
        save_manifest(manifest)
        updated += 1

    print(f"\n{updated} addon(s) updated.")


def cmd_remove(args):
    manifest = load_manifest()
    addons = manifest.get("addons", {})

    for slug in args.addons:
        if slug not in addons:
            print(f"Not installed: {slug}")
            continue

        info = addons[slug]
        for folder in info.get("folders", []):
            p = Path(folder)
            if p.is_dir():
                shutil.rmtree(p)
                print(f"  Removed {folder}/")

        del addons[slug]
        save_manifest(manifest)
        print(f"Uninstalled {info['name']}.")

    print("Done.")


def cmd_list(args):
    manifest = load_manifest()
    addons = manifest.get("addons", {})

    if not addons:
        print("No addons installed. Use 'install' to add some.")
        return

    print(f"\n {'Addon':<32} {'Version':<28} {'Folders'}")
    print(" " + "-" * 88)
    for slug, info in sorted(addons.items()):
        name = _truncate(info.get("name", slug), 30)
        ver = _truncate(info.get("version", "?"), 26)
        folders = ", ".join(info.get("folders", []))
        print(f" {name:<32} {ver:<28} {folders}")
    print(f"\n {len(addons)} addon(s) managed by wowaddons.\n")


def cmd_search(args):
    manifest = load_manifest()
    key = _api_key(manifest)
    flavor = manifest.get("flavor", "retail")
    flavor_id = FLAVORS.get(flavor)

    query = " ".join(args.query)
    print(f"Searching for '{query}' ({flavor})...\n")

    results = _search(key, query, flavor_id)
    if not results:
        print("No results.")
        return

    installed = set(manifest.get("addons", {}).keys())

    for r in results:
        slug = r["slug"]
        tag = " [installed]" if slug in installed else ""
        dl = _fmt_count(r.get("downloadCount", 0))
        summary = _truncate(r.get("summary", ""), 70)
        print(f"  {r['name']}{tag}")
        print(f"    slug: {slug}  |  id: {r['id']}  |  downloads: {dl}")
        if summary:
            print(f"    {summary}")
        print()


def cmd_info(args):
    manifest = load_manifest()
    key = _api_key(manifest)

    mod = _resolve(key, args.addon)
    if not mod:
        print(f"Not found: {args.addon}")
        return

    installed = manifest.get("addons", {}).get(mod["slug"])
    authors = ", ".join(a["name"] for a in mod.get("authors", []))

    print(f"  Name:       {mod['name']}")
    print(f"  Slug:       {mod['slug']}")
    print(f"  ID:         {mod['id']}")
    print(f"  Authors:    {authors}")
    print(f"  Downloads:  {mod.get('downloadCount', 0):,.0f}")
    print(f"  Summary:    {mod.get('summary', 'N/A')}")
    print(f"  URL:        https://www.curseforge.com/wow/addons/{mod['slug']}")
    if installed:
        print(f"  Installed:  {installed.get('version', '?')}")
        print(f"  Folders:    {', '.join(installed.get('folders', []))}")
    else:
        print(f"  Installed:  no")


def cmd_freeze(args):
    manifest = load_manifest()
    for slug in sorted(manifest.get("addons", {})):
        print(slug)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="wowaddons",
        description="Lightweight WoW Addon Package Manager (CurseForge)",
        epilog="Run from your WoW AddOns folder. Get a free API key at https://console.curseforge.com",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p = sub.add_parser("init", help="Set up API key and game flavor")
    p.add_argument("--key", help="CurseForge API key")
    p.add_argument("--flavor", choices=FLAVORS.keys(), help="retail / classic / classic_era")

    # install
    p = sub.add_parser("install", help="Install addon(s)")
    p.add_argument("addons", nargs="*", help="Addon slug(s), name(s), or CurseForge ID(s)")
    p.add_argument("-r", "--requirements", metavar="FILE", help="Install from a text file (one slug per line)")

    # update
    p = sub.add_parser("update", help="Update installed addon(s)")
    p.add_argument("addons", nargs="*", help="Specific slug(s) to update (omit for all)")

    # remove / uninstall
    for name in ("remove", "uninstall"):
        p = sub.add_parser(name, help="Remove addon(s)")
        p.add_argument("addons", nargs="+", help="Addon slug(s) to remove")

    # list
    sub.add_parser("list", help="List installed addons")

    # search
    p = sub.add_parser("search", help="Search CurseForge for addons")
    p.add_argument("query", nargs="+", help="Search terms")

    # info
    p = sub.add_parser("info", help="Show details for an addon")
    p.add_argument("addon", help="Addon slug, name, or CurseForge ID")

    # freeze
    sub.add_parser("freeze", help="Print installed addon slugs (for use with install -r)")

    args = parser.parse_args()

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
