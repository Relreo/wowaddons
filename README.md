# wowaddons

A lightweight Python CLI for managing World of Warcraft addons via CurseForge
and GitHub. No third-party dependencies — just Python 3.8+.

## First-time setup

1. Install [Python](https://www.python.org/downloads/) 3.8 or newer.
2. Install wowaddons (see [Installation](#installation) below).
3. Create a free CurseForge API key at <https://console.curseforge.com>.
4. Run `wowaddons init` and paste the key when prompted.
5. On Windows, open a **new** terminal so `CURSEFORGE_API_KEY` is picked up
   (or use `wowaddons --key YOUR_KEY ...` for a single command).

For addon commands, `cd` into the right **Interface\AddOns** folder first
(retail, Classic, WoW Forever, etc.). The game flavor is taken from that
path so search/install/update pull the matching CurseForge files.

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

Examples:

```
cd "C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns"
cd "C:\Program Files (x86)\World of Warcraft\_classic_\Interface\AddOns"
cd "C:\Program Files (x86)\World of Warcraft\_classic_era_\Interface\AddOns"
cd "C:\Program Files (x86)\World of Warcraft\_classic_beta_\Interface\AddOns"
```

`_classic_beta_` is the WoW Forever beta client. Commands print the detected
game (e.g. `Game: WoW Forever (from _classic_beta_)`) so you can confirm it
before searching.

### Commands

| Command | Description |
|---------|-------------|
| `init` | Save your CurseForge API key (one-time setup) |
| `install <slug ...>` | Install one or more addons by slug, name, or CurseForge project ID, **or from a GitHub repo** (URL, clone URL, or `owner/repo`) |
| `import <file>` | Install every project ID / GitHub URL listed in an `export` snapshot file |
| `export` | Print a snapshot to stdout (CurseForge- and GitHub-backed addons) |
| `export <file>` | Write that snapshot to a file |
| `update` | Check all installed addons for updates and download what's new |
| `update <name ...>` | Update specific addon(s) by folder name or project ID |
| `remove <name ...>` | Remove addon(s) and all their related folders |
| `uninstall <name ...>` | Alias for `remove` |
| `list` | List all detected addons with versions and folder names |
| `search <query>` | Search CurseForge and optionally install a result interactively |
| `info <slug or repo>` | Show details for an addon (authors, downloads, install status, URL); works for GitHub repos too |

### Flags

| Flag | Description |
|------|-------------|
| `-v`, `--verbose` | Show debug output (API calls, temp paths, version comparisons) |
| `-q`, `--quiet` | Suppress everything except warnings and errors |
| `--key KEY` | Pass API key directly instead of using the env var |
| `--flavor FLAVOR` | Override auto-detected game flavor (`retail`, `classic`, `classic_era`, `forever`, `tbc`, `wrath`, `cata`) |
| `-V`, `--version` | Print the version and exit |

## Installing addons from GitHub

Anything you can copy from a repo's page works — the browser URL, the
**Code** menu's HTTPS/SSH clone URL, or just `owner/repo`:

```
wowaddons install https://github.com/CHogan0973/BookOfDogs
wowaddons install https://github.com/oliome/FriendlyNameplateGuild.git
wowaddons install CHogan0973/BookOfDogs git@github.com:oliome/FriendlyNameplateGuild.git
wowaddons install https://github.com/someone/addon/tree/dev   # pin a branch
```

GitHub addons need **no CurseForge API key**. CurseForge and GitHub
identifiers can be mixed in one `install`.

**What gets installed**

1. If the repo has a GitHub **release** with a `.zip` (what the
   [BigWigs packager](https://github.com/BigWigsMods/packager) produces), the
   newest release is used — stable over pre-release, and the zip matching your
   game flavor when there are several (e.g. `-forever`, `-retail`).
2. Otherwise the repo itself is the addon: its source is downloaded, the
   folder is named from `.pkgmeta` `package-as` (or the `.toc` name), and
   dotfiles plus `.pkgmeta` `ignore:` entries are left out. If the repo
   instead holds addon folders (each with a `.toc`), those are installed as-is.
3. A `/tree/<branch>` URL always uses that branch's source.

**How updates work** — `wowaddons update` checks each GitHub addon against
its repo:

- **Release-based:** compares the latest release tag with your `.toc`
  `## Version` (and the tag recorded at install).
- **Branch-based:** compares the repo's `.toc` `## Version` with yours. If the
  repo's `.toc` only has a packager placeholder like `@project-version@`, it
  compares the repo's git tree SHA recorded at install time instead.

If the version differs, the addon is re-downloaded and replaced. `update
<folder or owner/repo>` limits it to specific addons, and `remove` / `list` /
`export` handle GitHub addons too. Install records live in
`%LOCALAPPDATA%\wowaddons\github\` (one file per AddOns folder).

GitHub's unauthenticated API allows 60 requests/hour (each addon check uses
1–2). Set a `GITHUB_TOKEN` environment variable for a higher limit. Private
repos are not supported.

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
  Each AddOns folder path gets its own cache file (by hash). Successful installs
  via `install`, `import`, or interactive `search` record extracted folder names
  in that cache; `update` and `list` can extend it when matching unknown folders.
- Sub-packages (e.g. `BigWigs_Core` under `BigWigs`) are automatically grouped
  under their parent project without any API calls
- Game flavor is auto-detected from the client's own install files: the
  product code in the client folder's `.flavor.info`, and that product's
  version in `World of Warcraft\.build.info` (e.g. `1.60.x` → WoW Forever,
  `5.5.x` → MoP Classic). If those are missing it falls back to the install
  folder name (`_retail_`, `_classic_beta_`, ...). Override with `--flavor`
  if needed. Search and downloads stay on that flavor and do not fall back to
  retail files.
- Updates compare the local `.toc` version against the latest CurseForge file
  name, then fall back to file modification time if versions don't match cleanly
- Downloads go to a temp directory first, the zip is validated (zip-slip
  protection), then clean folders are moved into place replacing the old ones

## Privacy

Only sends your API key (as the `x-api-key` header) and `wowaddons/<version>`
as the User-Agent. GitHub requests send only the User-Agent (plus
`GITHUB_TOKEN` if you set one, to `api.github.com` only). No local paths,
system info, or telemetry are transmitted.
