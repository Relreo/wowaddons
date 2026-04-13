# wowaddons

A lightweight Python CLI for managing World of Warcraft addons via CurseForge.
No third-party dependencies — just Python 3.8+.

## First-time setup

1. Install [Python](https://www.python.org/downloads/) 3.8 or newer.
2. Install wowaddons (see [Installation](#installation) below).
3. Create a free CurseForge API key at <https://console.curseforge.com>.
4. Run `wowaddons init` and paste the key when prompted.
5. On Windows, open a **new** terminal so `CURSEFORGE_API_KEY` is picked up
   (or use `wowaddons --key YOUR_KEY ...` for a single command).

For addon commands, `cd` into the right **Interface\AddOns** folder first
(retail, Classic, etc.).

## Installation

### From GitHub (easiest for sharing)

```
pip install git+https://github.com/YOURNAME/wowaddons.git
```

### From a local clone

```
git clone https://github.com/YOURNAME/wowaddons.git
pip install wowaddons/
```

To uninstall:

```
pip uninstall wowaddons
```

## Usage

Example (retail):

```
cd "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"
```

### Commands

| Command | Description |
|---------|-------------|
| `init` | Save your CurseForge API key (one-time setup) |
| `install <slug ...>` | Install one or more addons by slug, name, or CurseForge project ID |
| `import <file>` | Install every project ID listed in an `export` snapshot file |
| `export` | Print a snapshot to stdout (CurseForge-backed addons only) |
| `export <file>` | Write that snapshot to a file |
| `update` | Check all installed addons for updates and download what's new |
| `update <name ...>` | Update specific addon(s) by folder name or project ID |
| `remove <name ...>` | Remove addon(s) and all their related folders |
| `uninstall <name ...>` | Alias for `remove` |
| `list` | List all detected addons with versions and folder names |
| `search <query>` | Search CurseForge and optionally install a result interactively |
| `info <slug>` | Show details for an addon (authors, downloads, install status, URL) |

### Flags

| Flag | Description |
|------|-------------|
| `-v`, `--verbose` | Show debug output (API calls, temp paths, version comparisons) |
| `-q`, `--quiet` | Suppress everything except warnings and errors |
| `--key KEY` | Pass API key directly instead of using the env var |
| `--flavor FLAVOR` | Override auto-detected game flavor (`retail`, `classic`, `classic_era`) |
| `-V`, `--version` | Print the version and exit |

## Export and import (sharing)

**Export** lists every addon that can be tied to CurseForge right now:

- Folders whose `.toc` has `X-Curse-Project-ID`
- Plus folders without that tag but already matched in your **ID cache**
  (for example after a successful `update` or `list`)

Purely local or unmatched addons are not included.

Share the file with someone who has their own API key; they put it in their
AddOns folder and run **import**:

```
wowaddons export my-addons.txt
# send my-addons.txt

# recipient, from their AddOns folder:
wowaddons import my-addons.txt
```

You can also pipe or redirect: `wowaddons export > my-addons.txt`.

## How it works

- Scans the current directory for addon folders, reading each `.toc` file for
  `Title`, `Version`, and `X-Curse-Project-ID`
- Addons that don't embed a project ID are matched to CurseForge via slug and
  fuzzy name search, then cached under your user data directory (Windows:
  `%LOCALAPPDATA%\wowaddons\caches\`) so future runs don't need extra API calls.
  Each AddOns folder path gets its own cache file (by hash).
- Sub-packages (e.g. `BigWigs_Core` under `BigWigs`) are automatically grouped
  under their parent project without any API calls
- Game flavor is auto-detected from the directory path (`_retail_`, `_classic_`,
  `_classic_era_`); override with `--flavor` if needed
- Updates compare the local `.toc` version against the latest CurseForge file
  name, then fall back to file modification time if versions don't match cleanly
- Downloads go to a temp directory first, the zip is validated (zip-slip
  protection), then clean folders are moved into place replacing the old ones

## Privacy

Only sends your API key (as the `x-api-key` header) and `wowaddons/<version>`
as the User-Agent. No local paths, system info, or telemetry are transmitted.
