# wowaddons - WoW Addon Package Manager

A lightweight, single-file Python CLI for managing World of Warcraft addons via CurseForge.  
Zero external dependencies. No telemetry. Only checks versions and downloads zips.

## Install

```
pip install -e "c:\Users\relre\Documents\Python Tools\wowaddons"
```

This registers the `wowaddons` command globally. The `-e` flag means editable -- any changes to the source take effect immediately without reinstalling.

To uninstall later: `pip uninstall wowaddons`

## Setup

1. **Get a free CurseForge API key** at https://console.curseforge.com  
2. **`cd` into your WoW AddOns folder**, e.g.:
   ```
   cd "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"
   ```
3. **Initialize:**
   ```
   wowaddons init
   ```
   Paste your API key and choose a flavor (`retail`, `classic`, or `classic_era`).

## Commands

| Command | Description |
|---------|-------------|
| `init` | Configure API key and game flavor |
| `install <slug ...>` | Install one or more addons by slug, name, or CurseForge ID |
| `install -r addons.txt` | Install from a file (one slug per line) |
| `update` | Update all installed addons |
| `update <slug ...>` | Update specific addon(s) |
| `remove <slug ...>` | Remove addon(s) and delete their folders |
| `list` | Show all installed addons |
| `search <query>` | Search CurseForge |
| `info <slug>` | Show details for an addon |
| `freeze` | Print installed slugs (pipe to a file for sharing) |

## Examples

```bash
# Search for an addon
wowaddons search damage meter

# Install by slug (shown in search results)
wowaddons install details

# Install multiple at once
wowaddons install deadly-boss-mods weakauras-2 details

# Update everything
wowaddons update

# Export your addon list, share it, reinstall on another machine
wowaddons freeze > my-addons.txt
wowaddons install -r my-addons.txt

# Remove an addon
wowaddons remove details
```

## How It Works

- Stores a `wowaddons.json` manifest in the current directory tracking installed addons, their CurseForge IDs, file versions, and which folders they extracted.
- On **install/update**, downloads the zip to a temp directory first, validates it, then moves clean folders into place.
- On **remove**, deletes the exact folders that were extracted during install.
- API key can also be set via the `CURSEFORGE_API_KEY` environment variable.

## Privacy

wowaddons sends **zero** information about your machine. The only network requests it makes are:

- CurseForge API queries (search, version checks) authenticated with *your* API key
- Zip file downloads from CurseForge CDN

No analytics, no tracking, no fingerprinting.
