# wowaddons - WoW Addon Package Manager

A lightweight, stateless Python CLI for managing World of Warcraft addons via CurseForge.  
Zero external dependencies. Zero stored state. No telemetry.

## Install the tool

```
pip install -e "c:\Users\relre\Documents\Python Tools\wowaddons"
```

To uninstall: `pip uninstall wowaddons`

## Setup

1. Get a free CurseForge API key at https://console.curseforge.com
2. Set it as an environment variable (persists across sessions):
   ```
   setx CURSEFORGE_API_KEY "your-key-here"
   ```
3. Open a **new** terminal for the variable to take effect.

## Usage

Navigate to your WoW AddOns folder and run commands:

```
cd "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"
```

| Command | Description |
|---------|-------------|
| `update` | Scan all addons, check CurseForge for updates, download what's new |
| `update <folder>` | Update a specific addon |
| `install <slug ...>` | Install new addon(s) by slug, name, or CurseForge ID |
| `install -r addons.txt` | Install from a file (one per line, `freeze` format) |
| `remove <folder>` | Remove an addon and all its related folders |
| `list` | List all detected addons (CurseForge + custom) |
| `search <query>` | Search CurseForge |
| `info <slug>` | Show details for an addon |
| `freeze` | Print project IDs (for sharing/backup, works with `install -r`) |

### Flags

| Flag | Description |
|------|-------------|
| `-v` | Verbose: show API calls, temp paths, version comparisons |
| `-q` | Quiet: suppress everything except warnings and errors |
| `--key KEY` | Pass API key directly (alternative to env var) |
| `--flavor` | Override auto-detected flavor (retail/classic/classic_era) |

## How It Works

- Reads `.toc` files at runtime for addon metadata (`Title`, `Version`, `X-Curse-Project-ID`)
- Groups folders sharing the same CurseForge project ID (e.g., Details + Details_DataStorage)
- Compares local `.toc` version against CurseForge's latest file to decide what needs updating
- Downloads to a temp directory, validates the zip, then moves clean folders into place
- Game flavor is auto-detected from the directory path (`_retail_`, `_classic_`, `_classic_era_`)
- **No files are created or stored** -- all state is derived at runtime

## Privacy

Only sends your API key and `wowaddons/<version>` as User-Agent. No local paths, system info, or telemetry.
