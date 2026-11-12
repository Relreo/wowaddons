"""CLI entry point — argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, commands
from .constants import FLAVOR_CHOICES, log
from .ui import LogFormatter, enable_ansi


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
        choices=FLAVOR_CHOICES,
        help=(
            "Override auto-detected game flavor "
            "(retail, classic, classic_era, forever, ...)"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Set up your CurseForge API key")

    p = sub.add_parser("install", help="Install addon(s)")
    p.add_argument(
        "addons", nargs="+",
        help="Addon slug(s), name(s), or CurseForge ID(s)",
    )

    p = sub.add_parser(
        "import",
        help="Install addons from an export snapshot file",
    )
    p.add_argument(
        "file",
        help="Path to a file produced by wowaddons export",
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

    p = sub.add_parser(
        "export",
        help="Write a snapshot of CurseForge-backed addons (share with import)",
    )
    p.add_argument(
        "outfile",
        nargs="?",
        help="Optional path to write (default: print to stdout)",
    )

    args = parser.parse_args()

    enable_ansi()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogFormatter())
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
        "init": commands.cmd_init,
        "install": commands.cmd_install,
        "import": commands.cmd_import,
        "update": commands.cmd_update,
        "remove": commands.cmd_remove,
        "uninstall": commands.cmd_remove,
        "list": commands.cmd_list,
        "search": commands.cmd_search,
        "info": commands.cmd_info,
        "export": commands.cmd_export,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
