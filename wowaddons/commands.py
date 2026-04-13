"""All CLI command implementations."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .addons import detect_flavor, scan_addons
from .api import (
    download_extract,
    download_url,
    get_api_key,
    get_files,
    match_folder,
    needs_update,
    pick_file,
    resolve,
    search_addons,
)
from .cache import apply_id_cache, load_id_cache, record_match, save_id_cache
from .constants import FLAVORS, log
from .models import ProjectData, UnmatchedAddon
from .ui import AnsiCodes, colorize
from .utils import format_count, format_size, truncate


# ---------------------------------------------------------------------------
# Shared match helpers
# ---------------------------------------------------------------------------


def match_unmatched(
    key: str,
    by_project: dict[int, ProjectData],
    unmatched: list[UnmatchedAddon],
    flavor_id: int | None = None,
    cache: dict[str, dict[str, Any]] | None = None,
) -> list[UnmatchedAddon]:
    """Try to match unmatched addon folders to CurseForge projects via API.

    Successfully matched addons are moved from ``unmatched`` into
    ``by_project`` (mutated in place). New matches are written back to
    ``cache`` if provided.

    Returns:
        The subset of ``unmatched`` entries that could not be matched.
    """
    still_unmatched: list[UnmatchedAddon] = []
    if not unmatched:
        return still_unmatched

    total = len(unmatched)
    print(
        f"  {colorize('●', AnsiCodes.YELLOW)} "
        f"{colorize(str(total), AnsiCodes.WHITE + AnsiCodes.BOLD)} addon(s) without "
        f"project IDs — searching CurseForge..."
    )

    for i, addon in enumerate(unmatched, 1):
        counter = colorize(f"[{i}/{total}]", AnsiCodes.GRAY)
        print(f"    {counter} {colorize(addon['folder'], AnsiCodes.WHITE)}...", end="", flush=True)
        mod = match_folder(key, addon["folder"], addon.get("title"), flavor_id)
        if mod:
            pid: int = mod["id"]
            arrow = colorize("→", AnsiCodes.GRAY)
            name_str = colorize(mod["name"], AnsiCodes.GREEN)
            id_str = colorize(f"(id: {pid})", AnsiCodes.GRAY)
            check = colorize("✓", AnsiCodes.GREEN)
            folder_str = colorize(addon["folder"], AnsiCodes.WHITE)
            print(
                f"\r    {check} {folder_str} {arrow} {name_str} {id_str}"
                f"          "
            )
            if pid not in by_project:
                by_project[pid] = ProjectData(
                    folders=[addon["folder"]],
                    title=mod["name"],
                    version=addon["version"],
                )
            else:
                by_project[pid]["folders"].append(addon["folder"])
                log.debug(
                    "  %s grouped under existing project %s",
                    addon["folder"], pid,
                )
            if cache is not None:
                record_match(cache, addon["folder"], pid, mod["name"], mod.get("slug", ""))
        else:
            dash = colorize("–", AnsiCodes.GRAY)
            folder_str = colorize(addon["folder"], AnsiCodes.GRAY)
            no_match = colorize("no match", AnsiCodes.GRAY)
            print(f"\r    {dash} {folder_str} {no_match}          ")
            still_unmatched.append(addon)

    return still_unmatched


def _find_parent_project(
    folder_name: str,
    by_project: dict[int, ProjectData],
) -> int | None:
    """Return the project ID if *folder_name* is a sub-package of a known project.

    A folder is a sub-package when its name starts with a known project's
    folder name followed by ``_`` (e.g. ``BigWigs_Core`` → ``BigWigs``).
    """
    for pid, data in by_project.items():
        for known_folder in data["folders"]:
            if folder_name.startswith(known_folder + "_"):
                return pid
    return None


def group_sub_packages(
    by_project: dict[int, ProjectData],
    unmatched: list[UnmatchedAddon],
) -> tuple[list[UnmatchedAddon], int]:
    """Silently group sub-package folders under their parent project.

    Checks each unmatched folder against every known project folder. If the
    folder name starts with ``<known_folder>_`` it is treated as a bundled
    sub-package and merged into that project without any API calls.

    Args:
        by_project: Known projects dict, mutated in place.
        unmatched: Addon folders without a project ID.

    Returns:
        ``(still_unmatched, grouped_count)`` — folders that could not be
        grouped and the number that were silently absorbed.
    """
    remaining: list[UnmatchedAddon] = []
    grouped = 0
    for addon in unmatched:
        parent_pid = _find_parent_project(addon["folder"], by_project)
        if parent_pid is not None:
            by_project[parent_pid]["folders"].append(addon["folder"])
            log.debug(
                "Sub-package '%s' grouped under project %s (no API call)",
                addon["folder"], parent_pid,
            )
            grouped += 1
        else:
            remaining.append(addon)
    return remaining, grouped


def _install_mod(
    mod: dict[str, Any],
    key: str,
    flavor: str,
    flavor_id: int | None,
    installed_pids: set[int],
    cache: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Download and install a single addon from its CurseForge mod dict.

    All folders extracted from the zip are recorded in *cache* (if provided)
    so that sub-packages are recognised on the next run without API calls.

    Returns:
        ``True`` if the addon was installed successfully.
    """
    name: str = mod["name"]
    mod_id: int = mod["id"]
    slug: str = mod.get("slug", "")

    if mod_id in installed_pids:
        print(
            f"  {colorize('⚠', AnsiCodes.YELLOW)} {colorize(name, AnsiCodes.WHITE)} is already installed. "
            "Use 'update' instead."
        )
        return False

    slug_str = colorize(f"slug: {slug}  id: {mod_id}", AnsiCodes.GRAY)
    print(f"  {colorize('✓', AnsiCodes.GREEN)} Found: {colorize(name, AnsiCodes.WHITE)}  {slug_str}")

    files = get_files(key, mod_id, flavor_id)
    latest = pick_file(files)
    if not latest:
        print(f"  {colorize('✗', AnsiCodes.RED)} No {flavor} files available for {name}.")
        return False

    url = download_url(latest)
    version = latest.get("displayName", latest.get("fileName", "unknown"))
    size = latest.get("fileLength", 0)
    size_str = colorize(f"  {format_size(size)}", AnsiCodes.GRAY) if size else ""
    print(f"  {colorize('↓', AnsiCodes.CYAN)} Downloading {colorize(version, AnsiCodes.WHITE)}{size_str}...")

    folders = download_extract(url, ".", label=name)
    if folders is None:
        return False

    if cache is not None:
        for folder in folders:
            record_match(cache, folder, mod_id, name, slug)

    folders_str = colorize(f"[{', '.join(folders)}]", AnsiCodes.GRAY)
    print(f"  {colorize('✓', AnsiCodes.GREEN)} Installed {colorize(name, AnsiCodes.WHITE)}  {folders_str}")
    installed_pids.add(mod_id)
    return True


def _update_stale(
    key: str,
    stale_unmatched: list[UnmatchedAddon],
    flavor_id: int | None,
    cache: dict[str, dict[str, Any]],
) -> tuple[int, int, int]:
    """Re-match stale cache entries and attempt to update them.

    Returns:
        A ``(updated, skipped, failed)`` count tuple.
    """
    print(
        f"\n  {colorize('▸', AnsiCodes.YELLOW)} Re-matching "
        f"{len(stale_unmatched)} stale cache entry(s)...\n"
    )
    temp_proj: dict[int, ProjectData] = {}
    match_unmatched(key, temp_proj, stale_unmatched, flavor_id, cache)

    updated = skipped = failed = 0
    for pid, data in temp_proj.items():
        name = data["title"]
        local_ver = data["version"]
        name_str = colorize(truncate(name, 40), AnsiCodes.WHITE)
        files = get_files(key, pid, flavor_id)
        latest = pick_file(files)

        if not latest:
            print(f"  {colorize('✗', AnsiCodes.RED)} {name_str} — still no files.")
            failed += 1
            continue

        if not needs_update(local_ver, latest, data["folders"]):
            print(
                f"  {colorize('✓', AnsiCodes.GREEN)} {name_str}  "
                f"{colorize('up to date', AnsiCodes.GRAY)}"
            )
            skipped += 1
            continue

        new_ver = latest.get("displayName", latest.get("fileName", "?"))
        size = latest.get("fileLength", 0)
        size_str = colorize(f"({format_size(size)})", AnsiCodes.GRAY) if size else ""
        print(
            f"  {colorize('↑', AnsiCodes.CYAN)} {name_str}  "
            f"{colorize(new_ver, AnsiCodes.CYAN)}  {size_str}"
        )

        for folder in data["folders"]:
            path = Path(folder)
            if path.is_dir():
                shutil.rmtree(path)

        url = download_url(latest)
        folders_list = download_extract(url, ".", label=name)
        if folders_list is None:
            print(f"  {colorize('✗', AnsiCodes.RED)} Failed to download {name}.")
            failed += 1
        else:
            print(
                f"  {colorize('✓', AnsiCodes.GREEN)} {name_str}  "
                f"{colorize('updated →', AnsiCodes.GREEN)} {colorize(new_ver, AnsiCodes.WHITE)}"
            )
            updated += 1

    return updated, skipped, failed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(_args: argparse.Namespace) -> None:
    """Interactively configure the CurseForge API key."""
    existing = os.environ.get("CURSEFORGE_API_KEY", "")
    if existing:
        print(colorize("  CURSEFORGE_API_KEY is already set.", AnsiCodes.YELLOW))
        resp = input("  Overwrite it? [y/N] ").strip().lower()
        if resp != "y":
            log.info("Aborted.")
            return

    print(
        "  Get a free CurseForge API key at: "
        + colorize("https://console.curseforge.com", AnsiCodes.CYAN)
    )
    key = input("  Paste your API key: ").strip()
    if not key:
        log.info("Aborted.")
        return

    if sys.platform == "win32":
        result = subprocess.run(
            ["setx", "CURSEFORGE_API_KEY", key],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            os.environ["CURSEFORGE_API_KEY"] = key
            print(colorize("  ✓ API key saved to your user environment variables.", AnsiCodes.GREEN))
            print("  It will be available in all new terminals automatically.")
            print("  This terminal session is also ready to go.")
        else:
            log.error("setx failed: %s", result.stderr.strip())
            log.error(
                'You can set it manually: '
                'setx CURSEFORGE_API_KEY "your-key"'
            )
            sys.exit(1)
    else:
        os.environ["CURSEFORGE_API_KEY"] = key
        shell = os.environ.get("SHELL", "")
        rc = "~/.zshrc" if "zsh" in shell else "~/.bashrc"
        print(colorize("  ✓ API key set for this session.", AnsiCodes.GREEN))
        print(f"  To make it permanent, add this to {rc}:")
        print('  export CURSEFORGE_API_KEY="<your-key>"')


def cmd_install(args: argparse.Namespace) -> None:
    """Install one or more addons from CurseForge."""
    key = get_api_key(args)
    flavor = detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    identifiers: list[str] = list(args.addons or [])

    if args.requirements:
        path = Path(args.requirements)
        if not path.exists():
            log.error("File not found: %s", path)
            sys.exit(1)
        log.debug("Reading requirements from %s", path)
        with open(path, encoding="utf-8") as f:
            identifiers.extend(
                line.strip().split("#")[0].strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            )

    if not identifiers:
        log.error("Specify addon(s) to install, or use -r <file>.")
        sys.exit(1)

    by_project, _ = scan_addons()
    installed_pids: set[int] = set(by_project.keys())
    cache = load_id_cache()
    cache_changed = False

    for ident in identifiers:
        print(f"\n  {colorize('▸', AnsiCodes.CYAN)} Resolving {colorize(ident, AnsiCodes.WHITE)}...")
        mod = resolve(key, ident, flavor_id)
        if not mod:
            print(f"  {colorize('✗', AnsiCodes.RED)} Not found: {ident}")
            continue

        if _install_mod(mod, key, flavor, flavor_id, installed_pids, cache):
            cache_changed = True

    if cache_changed:
        save_id_cache(cache)

    print(f"\n  {colorize('Done.', AnsiCodes.GREEN + AnsiCodes.BOLD)}\n")


def cmd_update(args: argparse.Namespace) -> None:
    """Check for and apply updates to installed addons."""
    key = get_api_key(args)
    flavor = detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    print(f"\n  {colorize('▸', AnsiCodes.CYAN)} Scanning for installed addons...")
    by_project, unmatched = scan_addons()

    if not by_project and not unmatched:
        log.error("No addons found in the current directory.")
        log.error("Make sure you're in your WoW AddOns folder:")
        log.error("  ...\\World of Warcraft\\_retail_\\Interface\\AddOns\\")
        sys.exit(1)

    cache: dict[str, dict[str, Any]] = {}
    cache_changed = False
    cache_sourced_pids: set[int] = set()

    if unmatched:
        cache = load_id_cache()
        still_unmatched, cache_sourced_pids = apply_id_cache(
            cache, by_project, unmatched
        )
        log.debug(
            "Cache resolved %s of %s unmatched addon(s)",
            len(unmatched) - len(still_unmatched), len(unmatched),
        )

        still_unmatched, sub_pkg_count = group_sub_packages(by_project, still_unmatched)
        if sub_pkg_count:
            log.debug(
                "Grouped %s sub-package folder(s) under parent projects "
                "(no API calls needed)",
                sub_pkg_count,
            )

        if still_unmatched:
            still_unmatched = match_unmatched(
                key, by_project, still_unmatched, flavor_id, cache
            )
            cache_changed = True

    if not by_project:
        print(
            f"  {colorize('–', AnsiCodes.GRAY)} No CurseForge addons detected. "
            "Nothing to update."
        )
        return

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
            log.warning("None of the specified addons were found locally.")
            return

    total_count = len(targets)
    print(
        f"\n  {colorize('▸', AnsiCodes.CYAN)} Checking "
        f"{colorize(str(total_count), AnsiCodes.WHITE + AnsiCodes.BOLD)} addon(s) for updates...\n"
    )

    updated = 0
    skipped = 0
    failed = 0
    stale_unmatched: list[UnmatchedAddon] = []

    for i, (pid, data) in enumerate(targets.items(), 1):
        name = data["title"]
        local_ver = data["version"]
        counter = colorize(f"[{i}/{total_count}]", AnsiCodes.GRAY)
        name_str = colorize(truncate(name, 40), AnsiCodes.WHITE)
        print(f"  {counter} {name_str}", end="", flush=True)

        files = get_files(key, pid, flavor_id)
        latest = pick_file(files)

        if not latest:
            if pid in cache_sourced_pids:
                print(
                    f"\r  {counter} {name_str} "
                    f"{colorize('⚠ cache stale — re-searching...', AnsiCodes.YELLOW)}"
                    f"          "
                )
                for folder in data["folders"]:
                    cache.pop(folder, None)
                    stale_unmatched.append(
                        UnmatchedAddon(
                            folder=folder,
                            title=name,
                            version=local_ver,
                        )
                    )
                cache_changed = True
            else:
                print(
                    f"\r  {counter} {name_str} "
                    f"{colorize('✗ no files on CurseForge', AnsiCodes.RED)}"
                    f"          "
                )
                failed += 1
            continue

        if not needs_update(local_ver, latest, data["folders"]):
            ver_str = colorize(
                truncate(latest.get("displayName", local_ver), 30), AnsiCodes.GRAY
            )
            print(
                f"\r  {counter} {name_str}  "
                f"{colorize('✓', AnsiCodes.GREEN)} {colorize('up to date', AnsiCodes.GRAY)}  "
                f"{ver_str}          "
            )
            skipped += 1
            continue

        new_ver = latest.get("displayName", latest.get("fileName", "?"))
        size = latest.get("fileLength", 0)
        size_str = colorize(f"({format_size(size)})", AnsiCodes.GRAY) if size else ""
        print(
            f"\r  {counter} {name_str}  "
            f"{colorize('↑', AnsiCodes.CYAN)} {colorize(new_ver, AnsiCodes.CYAN)}  {size_str}          "
        )

        for folder in data["folders"]:
            path = Path(folder)
            if path.is_dir():
                log.debug("Removing old folder: %s", folder)
                shutil.rmtree(path)

        url = download_url(latest)
        folders_list = download_extract(url, ".", label=name)
        if folders_list is None:
            print(f"  {colorize('✗', AnsiCodes.RED)} Failed to download {name}.")
            failed += 1
            continue

        for folder in folders_list:
            record_match(cache, folder, pid, name, "")
        cache_changed = True

        print(
            f"  {colorize('✓', AnsiCodes.GREEN)} {name_str}  "
            f"{colorize('updated →', AnsiCodes.GREEN)} {colorize(new_ver, AnsiCodes.WHITE)}"
        )
        updated += 1

    if stale_unmatched:
        s_upd, s_skip, s_fail = _update_stale(
            key, stale_unmatched, flavor_id, cache
        )
        cache_changed = True
        updated += s_upd
        skipped += s_skip
        failed += s_fail

    if cache_changed:
        save_id_cache(cache)

    u_str = colorize(str(updated), AnsiCodes.GREEN + AnsiCodes.BOLD if updated else AnsiCodes.GRAY)
    s_str = colorize(str(skipped), AnsiCodes.GRAY)
    f_str = colorize(str(failed), AnsiCodes.RED + AnsiCodes.BOLD if failed else AnsiCodes.GRAY)
    print(
        f"\n  {u_str} updated   "
        f"{s_str} up to date   "
        f"{f_str} failed\n"
    )


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove one or more installed addons."""
    by_project, unmatched = scan_addons()

    folder_to_pid: dict[str, int] = {}
    for pid, data in by_project.items():
        for folder in data["folders"]:
            folder_to_pid[folder.lower()] = pid

    print()
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
            for addon in unmatched:
                if addon["folder"].lower() == target_lower:
                    path = Path(addon["folder"])
                    if path.is_dir():
                        shutil.rmtree(path)
                        print(
                            f"  {colorize('✓', AnsiCodes.GREEN)} Removed "
                            f"{colorize(addon['folder'] + '/', AnsiCodes.WHITE)}"
                        )
                    break
            else:
                print(f"  {colorize('✗', AnsiCodes.RED)} Not found: {target}")
            continue

        data = by_project[matched_pid]
        for folder in data["folders"]:
            path = Path(folder)
            if path.is_dir():
                shutil.rmtree(path)
                print(
                    f"  {colorize('✓', AnsiCodes.GREEN)} Removed "
                    f"{colorize(folder + '/', AnsiCodes.WHITE)}"
                )

        print(
            f"  {colorize('✓', AnsiCodes.GREEN)} Uninstalled "
            f"{colorize(data['title'], AnsiCodes.WHITE)}"
        )

    print(f"\n  {colorize('Done.', AnsiCodes.GREEN + AnsiCodes.BOLD)}\n")


def cmd_list(args: argparse.Namespace) -> None:
    """List all detected addons in the current directory."""
    by_project, unmatched = scan_addons()

    if not by_project and not unmatched:
        print(
            f"  {colorize('–', AnsiCodes.GRAY)} No addons found. "
            "Make sure you're in your WoW AddOns folder."
        )
        return

    key = os.environ.get("CURSEFORGE_API_KEY", "") or getattr(
        args, "key", None
    )
    if key and unmatched:
        cache = load_id_cache()
        flavor = detect_flavor(getattr(args, "flavor", None))
        flavor_id = FLAVORS.get(flavor)
        still_unmatched, _ = apply_id_cache(cache, by_project, unmatched)
        still_unmatched, _ = group_sub_packages(by_project, still_unmatched)
        unmatched = match_unmatched(
            key, by_project, still_unmatched, flavor_id, cache
        )
        save_id_cache(cache)

    if by_project:
        header = colorize(f"  CurseForge addons ({len(by_project)})", AnsiCodes.CYAN + AnsiCodes.BOLD)
        print(f"\n{header}")
        col_title = colorize(f"  {'Title':<36}", AnsiCodes.GRAY)
        col_ver = colorize(f"{'Version':<24}", AnsiCodes.GRAY)
        col_folders = colorize("Folders", AnsiCodes.GRAY)
        print(f"{col_title} {col_ver} {col_folders}")
        print(colorize("  " + "─" * 88, AnsiCodes.GRAY))
        for _pid, data in sorted(
            by_project.items(), key=lambda x: x[1]["title"].lower()
        ):
            title = truncate(data["title"], 34)
            ver = truncate(data["version"] or "?", 22)
            folder_list = data["folders"]
            overflow = len(folder_list) - 3
            folders = ", ".join(folder_list[:3])
            if overflow > 0:
                folders += colorize(f"  +{overflow} more", AnsiCodes.GRAY)
            print(
                f"  {colorize(f'{title:<36}', AnsiCodes.WHITE)}"
                f" {colorize(f'{ver:<24}', AnsiCodes.GRAY)}"
                f" {colorize(folders, AnsiCodes.GRAY)}"
            )

    if unmatched:
        header = colorize(
            f"  Custom/untracked addons ({len(unmatched)})",
            AnsiCodes.YELLOW + AnsiCodes.BOLD,
        )
        print(f"\n{header}")
        col_folder = colorize(f"  {'Folder':<36}", AnsiCodes.GRAY)
        col_ver = colorize(f"{'Version':<24}", AnsiCodes.GRAY)
        col_title = colorize("Title", AnsiCodes.GRAY)
        print(f"{col_folder} {col_ver} {col_title}")
        print(colorize("  " + "─" * 88, AnsiCodes.GRAY))
        for addon in sorted(unmatched, key=lambda x: x["folder"].lower()):
            folder = truncate(addon["folder"], 34)
            ver = truncate(addon["version"] or "?", 22)
            title = truncate(addon["title"], 30)
            print(
                f"  {colorize(f'{folder:<36}', AnsiCodes.WHITE)}"
                f" {colorize(f'{ver:<24}', AnsiCodes.GRAY)}"
                f" {colorize(title, AnsiCodes.GRAY)}"
            )

    total = len(by_project) + len(unmatched)
    print(f"\n  {colorize(str(total), AnsiCodes.WHITE + AnsiCodes.BOLD)} addon(s) detected.\n")


def cmd_search(args: argparse.Namespace) -> None:
    """Search CurseForge for addons matching a query."""
    key = get_api_key(args)
    flavor = detect_flavor(getattr(args, "flavor", None))
    flavor_id = FLAVORS.get(flavor)

    query = " ".join(args.query)
    print(
        f"\n  {colorize('▸', AnsiCodes.CYAN)} Searching for "
        f"{colorize(repr(query), AnsiCodes.WHITE)} "
        f"{colorize(f'({flavor})', AnsiCodes.GRAY)}..."
    )

    results = search_addons(key, query, flavor_id)
    if not results:
        print(f"  {colorize('–', AnsiCodes.GRAY)} No results found.")
        return

    by_project, _ = scan_addons()
    installed_pids: set[int] = set(by_project.keys())

    print()
    for i, result in enumerate(results, 1):
        is_installed = result["id"] in installed_pids
        tag = colorize(" [installed]", AnsiCodes.GREEN) if is_installed else ""
        dl = format_count(result.get("downloadCount", 0))
        summary = truncate(result.get("summary", ""), 70)
        num = colorize(f"  {i}.", AnsiCodes.CYAN + AnsiCodes.BOLD)
        name_str = colorize(result["name"], AnsiCodes.WHITE + AnsiCodes.BOLD)
        meta = colorize(
            f"     slug: {result['slug']}  |  id: {result['id']}  |  ↓ {dl}",
            AnsiCodes.GRAY,
        )
        print(f"{num} {name_str}{tag}")
        print(meta)
        if summary:
            print(colorize(f"     {summary}", AnsiCodes.GRAY))
        print()

    print(colorize("  " + "─" * 50, AnsiCodes.GRAY))
    try:
        choice = input(
            colorize("  Enter a number to install, or press Enter to exit: ", AnsiCodes.WHITE)
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not choice or choice.lower() == "q":
        return

    if not choice.isdigit() or not 1 <= int(choice) <= len(results):
        print(f"  {colorize('⚠', AnsiCodes.YELLOW)} Invalid selection: {choice}")
        return

    selected = results[int(choice) - 1]
    print()
    _install_mod(selected, key, flavor, flavor_id, installed_pids)
    print(f"\n  {colorize('Done.', AnsiCodes.GREEN + AnsiCodes.BOLD)}\n")


def cmd_info(args: argparse.Namespace) -> None:
    """Display detailed information about a specific addon."""
    key = get_api_key(args)

    print(f"\n  {colorize('▸', AnsiCodes.CYAN)} Looking up {colorize(args.addon, AnsiCodes.WHITE)}...")
    mod = resolve(key, args.addon)
    if not mod:
        print(f"  {colorize('✗', AnsiCodes.RED)} Not found: {args.addon}")
        return

    by_project, _ = scan_addons()
    local = by_project.get(mod["id"])
    authors = ", ".join(a["name"] for a in mod.get("authors", []))

    def row(label: str, value: str) -> None:
        print(f"  {colorize(label + ':', AnsiCodes.GRAY):<28} {value}")

    print()
    row("Name", colorize(mod["name"], AnsiCodes.WHITE + AnsiCodes.BOLD))
    row("Slug", colorize(mod["slug"], AnsiCodes.CYAN))
    row("ID", colorize(str(mod["id"]), AnsiCodes.WHITE))
    row("Authors", authors)
    row("Downloads", colorize(format_count(mod.get("downloadCount", 0)), AnsiCodes.WHITE))
    row("Summary", truncate(mod.get("summary", "N/A"), 65))
    row(
        "URL",
        colorize(
            f"https://www.curseforge.com/wow/addons/{mod['slug']}",
            AnsiCodes.CYAN,
        ),
    )
    if local:
        row("Installed", colorize(local["version"] or "?", AnsiCodes.GREEN))
        row("Folders", colorize(", ".join(local["folders"]), AnsiCodes.GRAY))
    else:
        row("Installed", colorize("no", AnsiCodes.GRAY))
    print()


def cmd_freeze(_args: argparse.Namespace) -> None:
    """Print CurseForge project IDs for all installed addons."""
    by_project, _ = scan_addons()
    for pid, data in sorted(
        by_project.items(), key=lambda x: x[1]["title"].lower()
    ):
        print(f"{pid}  # {data['title']}")
