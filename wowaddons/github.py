"""GitHub-hosted addon support: spec parsing, remote lookup, and install.

An addon can be installed from any public GitHub repository, given anything
you can copy from the repo page: the browser URL, the *Code* menu's clone
URL (HTTPS or SSH), or just ``owner/repo``.

Two kinds of remote are supported, tried in this order:

1. **Releases** — the newest GitHub release with a ``.zip`` asset (what the
   BigWigs packager produces). The release tag is the remote version.
2. **Branch source** — the repository itself is the addon. The remote
   version is the ``## Version`` in the repo's ``.toc``. If that is a
   packager placeholder such as ``@project-version@``, the git tree SHA is
   compared instead.

Naming a branch (``.../tree/<branch>``) always uses branch source.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .addons import parse_toc_text, read_local_meta
from .api import download_extract, download_file, safe_extract
from .constants import log
from .utils import strip_wow_colors

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_CODELOAD = "https://codeload.github.com"

_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]*"
_REPO = r"[A-Za-z0-9._-]+"

_URL_RE = re.compile(
    r"^(?:(?:https?|ssh)://(?:git@)?(?:www\.)?github\.com/"
    r"|git@github\.com:"
    r"|(?:www\.)?github\.com/)"
    rf"(?P<owner>{_OWNER})/(?P<repo>{_REPO})(?P<rest>[/?#].*)?$",
    re.IGNORECASE,
)
_SHORT_RE = re.compile(rf"^(?P<owner>{_OWNER})/(?P<repo>{_REPO})$")
_TREE_RE = re.compile(r"^/tree/(?P<ref>[^?#]+)")

# Flavor suffixes used in packager asset names (``Name-v1.2-<suffix>.zip``).
_ASSET_FLAVOR_TOKENS: dict[str, frozenset[str]] = {
    "retail": frozenset({"retail", "mainline"}),
    "classic": frozenset({"mists", "mop"}),
    "classic_era": frozenset({"classic", "vanilla", "era"}),
    "tbc": frozenset({"bcc", "tbc", "bc"}),
    "wrath": frozenset({"wrath", "wotlk", "wotlkc"}),
    "cata": frozenset({"cata", "cataclysm"}),
    "forever": frozenset({"forever"}),
}
_ALL_FLAVOR_TOKENS = frozenset().union(*_ASSET_FLAVOR_TOKENS.values())

# ``Foo_Mainline.toc`` / ``Foo-Classic.toc`` → ``Foo``
_TOC_SUFFIX_RE = re.compile(
    r"[_-](mainline|vanilla|classic|tbc|bcc|wrath|wotlkc|cata|mists|standard|retail)$",
    re.IGNORECASE,
)


class GitHubError(Exception):
    """A GitHub lookup or install failed; the message is user-presentable."""


@dataclass(frozen=True)
class RepoSpec:
    """A GitHub repository, optionally pinned to a branch or tag."""

    owner: str
    repo: str
    ref: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        """Stable lower-case identifier used for the local source record."""
        return self.slug.lower()

    @property
    def url(self) -> str:
        base = f"https://github.com/{self.slug}"
        return f"{base}/tree/{self.ref}" if self.ref else base


@dataclass
class RemoteInfo:
    """What GitHub currently offers for a repository."""

    source: str  # "release" or "branch"
    version: str  # compared against the local .toc ## Version
    ref: str
    tag: str | None = None
    tree_sha: str | None = None
    asset_url: str | None = None
    asset_size: int = 0
    asset_name: str | None = None
    flavor_mismatch: bool = False  # asset is tagged for a different flavor

    @property
    def is_template(self) -> bool:
        """True if *version* is empty or an unexpanded packager placeholder."""
        return is_template_version(self.version)

    @property
    def describe(self) -> str:
        if self.source == "release":
            return f"release {self.tag}"
        return f"branch {self.ref}" if self.ref != "HEAD" else "default branch"


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def parse_github_spec(text: str) -> RepoSpec | None:
    """Parse something copied from GitHub into a :class:`RepoSpec`.

    Accepted forms::

        https://github.com/owner/repo
        https://github.com/owner/repo.git        (Code → HTTPS)
        git@github.com:owner/repo.git            (Code → SSH)
        https://github.com/owner/repo/tree/branch
        github.com/owner/repo
        owner/repo

    Returns ``None`` if *text* does not look like a GitHub repository.
    """
    text = text.strip()
    ref: str | None = None

    match = _URL_RE.match(text)
    if match:
        rest = match.group("rest") or ""
        tree = _TREE_RE.match(rest)
        if tree:
            ref = urllib.parse.unquote(tree.group("ref")).rstrip("/") or None
    else:
        match = _SHORT_RE.match(text)
        if not match:
            return None

    repo = match.group("repo")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not repo or repo in (".", ".."):
        return None
    return RepoSpec(match.group("owner"), repo, ref)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _headers(api: bool) -> dict[str, str]:
    headers = {"User-Agent": f"wowaddons/{__version__}"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, api: bool) -> str:
    log.debug("GET %s", url)
    req = urllib.request.Request(url, headers=_headers(api))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise GitHubError(
                "not found on GitHub (check the URL; private repos are "
                "not supported)"
            ) from exc
        if exc.code in (403, 429) and exc.headers.get(
            "X-RateLimit-Remaining"
        ) == "0":
            raise GitHubError(
                "GitHub API rate limit reached — set a GITHUB_TOKEN "
                "environment variable or try again later"
            ) from exc
        raise GitHubError(f"GitHub returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"network request failed — {exc.reason}") from exc


def _api_get(path: str) -> Any:
    return json.loads(_get(f"{GITHUB_API}{path}", api=True))


def _quote_ref(ref: str) -> str:
    return urllib.parse.quote(ref, safe="/")


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def is_template_version(version: str) -> bool:
    """True if *version* is blank or an unexpanded ``@token@`` placeholder."""
    return not version or "@" in version


def normalize_version(version: str) -> str:
    """Lower-case and drop a leading ``v`` so ``v1.2`` equals ``1.2``."""
    version = version.strip().lower()
    return version[1:] if version.startswith("v") and version[1:2].isdigit() else version


# ---------------------------------------------------------------------------
# Remote lookup
# ---------------------------------------------------------------------------


def _pick_asset(
    assets: list[dict[str, Any]], flavor: str
) -> tuple[dict[str, Any], bool] | None:
    """Choose the release ``.zip`` that best fits *flavor*.

    Returns:
        ``(asset, fits)`` — *fits* is ``False`` when every zip is tagged for
        a different flavor — or ``None`` if the release has no zip at all.
    """
    zips = [
        a for a in assets
        if a.get("name", "").lower().endswith(".zip")
        and a.get("state", "uploaded") == "uploaded"
    ]
    zips = [a for a in zips if "nolib" not in a["name"].lower()] or zips
    if not zips:
        return None

    def tokens(asset: dict[str, Any]) -> set[str]:
        stem = asset["name"].lower()[:-4]
        return set(re.split(r"[-_.\s]+", stem))

    wanted = _ASSET_FLAVOR_TOKENS.get(flavor, frozenset())
    for asset in zips:
        if tokens(asset) & wanted:
            return asset, True
    for asset in zips:
        if not tokens(asset) & _ALL_FLAVOR_TOKENS:
            return asset, True
    return zips[0], False


def _latest_release(spec: RepoSpec, flavor: str) -> RemoteInfo | None:
    """Newest release with a zip (stable preferred over pre-release), if any."""
    releases = _api_get(f"/repos/{spec.slug}/releases?per_page=20")
    usable: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    for release in releases:
        if release.get("draft"):
            continue
        picked = _pick_asset(release.get("assets") or [], flavor)
        if picked:
            usable.append((release, picked[0], picked[1]))

    if not usable:
        return None
    stable = [item for item in usable if not item[0].get("prerelease")]
    release, asset, fits = (stable or usable)[0]
    tag = release["tag_name"]
    log.debug("Latest release for %s: %s (%s)", spec.slug, tag, asset["name"])
    return RemoteInfo(
        source="release",
        version=tag,
        ref=tag,
        tag=tag,
        asset_url=asset["browser_download_url"],
        asset_size=int(asset.get("size") or 0),
        asset_name=asset["name"],
        flavor_mismatch=not fits,
    )


def _toc_stem_base(path: str) -> str:
    return _TOC_SUFFIX_RE.sub("", Path(path).stem)


def _primary_toc(paths: list[str]) -> str:
    """Pick the representative ``.toc`` (un-suffixed, then shortest) of *paths*."""
    def rank(path: str) -> tuple[bool, int, str]:
        return (Path(path).stem != _toc_stem_base(path), len(path), path)

    return sorted(paths, key=rank)[0]


def _branch_remote(spec: RepoSpec) -> RemoteInfo:
    """Describe the branch head: layout via the git tree, version via raw TOC."""
    ref = spec.ref or "HEAD"
    tree = _api_get(f"/repos/{spec.slug}/git/trees/{_quote_ref(ref)}?recursive=1")

    tocs = [
        e["path"] for e in tree.get("tree", [])
        if e.get("type") == "blob"
        and e["path"].lower().endswith(".toc")
        and e["path"].count("/") <= 1
        and not any(part.startswith(".") for part in e["path"].split("/"))
    ]
    if not tocs:
        raise GitHubError(
            "no .toc file found — this doesn't look like a WoW addon repo"
        )

    root_tocs = [p for p in tocs if "/" not in p]
    toc_path = _primary_toc(root_tocs or tocs)
    raw_url = f"{GITHUB_RAW}/{spec.slug}/{_quote_ref(ref)}/{urllib.parse.quote(toc_path)}"
    meta = parse_toc_text(_get(raw_url, api=False))
    version = meta.get("Version", "")
    log.debug(
        "Remote TOC %s: version=%r tree=%s", toc_path, version, tree.get("sha")
    )
    return RemoteInfo(
        source="branch",
        version=version,
        ref=ref,
        tree_sha=tree.get("sha"),
    )


def fetch_remote(spec: RepoSpec, flavor: str) -> RemoteInfo:
    """Look up what GitHub currently offers for *spec*.

    Raises:
        GitHubError: The repo can't be reached or isn't an addon.
    """
    if spec.ref is None:
        release = _latest_release(spec, flavor)
        if release:
            return release
    return _branch_remote(spec)


def fetch_repo_summary(spec: RepoSpec) -> dict[str, Any]:
    """Return GitHub's repo metadata (description, stars, default branch...)."""
    return _api_get(f"/repos/{spec.slug}")


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def parse_pkgmeta(text: str) -> tuple[str | None, list[str]]:
    """Read ``package-as`` and the ``ignore`` list from a ``.pkgmeta`` file."""
    package_as: str | None = None
    ignore: list[str] = []
    in_ignore = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            key, _, value = stripped.partition(":")
            key = key.strip()
            in_ignore = key == "ignore"
            if key == "package-as":
                package_as = value.strip().strip("'\"") or None
        elif in_ignore and stripped.startswith("-"):
            ignore.append(stripped[1:].strip().strip("'\""))
    return package_as, ignore


def _valid_folder_name(name: str | None) -> bool:
    return bool(name) and name not in (".", "..") and not re.search(r"[\\/:*?\"<>|]", name)


def _make_ignore(root: Path, patterns: list[str]) -> Callable[[str, list[str]], set[str]]:
    """Build a ``shutil.copytree`` ignore callback from ``.pkgmeta`` patterns."""
    cleaned = [p.strip().rstrip("/") for p in patterns if p.strip()]

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel_dir = Path(directory).relative_to(root).as_posix()
        skipped: set[str] = set()
        for name in names:
            if name.startswith("."):
                skipped.add(name)
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if any(fnmatch.fnmatch(rel, pat) for pat in cleaned):
                skipped.add(name)
        return skipped

    return ignore


def _layout_repo(
    root: Path,
    out: Path,
    repo_name: str,
) -> list[str]:
    """Copy the addon folder(s) found in an extracted repo into *out*.

    * ``.toc`` at the repo root → the whole repo is one addon, named by
      ``.pkgmeta`` ``package-as`` or else the ``.toc`` name.
    * otherwise each top-level folder containing a ``.toc`` is an addon.

    Returns the folder names created under *out*.
    """
    out.mkdir(parents=True, exist_ok=True)
    root_tocs = sorted(root.glob("*.toc"))

    if root_tocs:
        package_as: str | None = None
        ignore: list[str] = []
        pkgmeta = root / ".pkgmeta"
        if pkgmeta.is_file():
            package_as, ignore = parse_pkgmeta(
                pkgmeta.read_text(encoding="utf-8", errors="replace")
            )
        name = package_as
        if not _valid_folder_name(name):
            name = _toc_stem_base(_primary_toc([t.name for t in root_tocs]))
        if not _valid_folder_name(name):
            name = repo_name
        shutil.copytree(root, out / name, ignore=_make_ignore(root, ignore))
        return [name]

    names: list[str] = []
    for child in sorted(root.iterdir()):
        if (
            child.is_dir()
            and not child.name.startswith(".")
            and next(child.glob("*.toc"), None) is not None
        ):
            shutil.copytree(
                child, out / child.name, ignore=_make_ignore(child, [])
            )
            names.append(child.name)
    return names


def _install_branch(spec: RepoSpec, dest: str, label: str) -> list[str] | None:
    """Download the repo archive and install the addon folder(s) it holds."""
    ref = _quote_ref(spec.ref or "HEAD")
    url = f"{GITHUB_CODELOAD}/{spec.slug}/zip/{ref}"

    staging = tempfile.mkdtemp(prefix="wowaddons_")
    log.debug("Created staging directory: %s", staging)
    try:
        zip_path = os.path.join(staging, "repo.zip")
        if not download_file(url, zip_path, label):
            return None

        extract_dir = os.path.join(staging, "extract")
        os.makedirs(extract_dir)
        tops = safe_extract(zip_path, extract_dir)
        if tops is None:
            return None

        # GitHub archives wrap everything in a single "<repo>-<ref>/" folder.
        extracted = Path(extract_dir)
        root = extracted / tops[0] if len(tops) == 1 else extracted
        if not root.is_dir():
            root = extracted

        out = Path(staging) / "out"
        folders = _layout_repo(root, out, spec.repo)
        if not folders:
            log.error("No addon folder (.toc) found in %s.", spec.slug)
            return None

        for folder in folders:
            dst = os.path.join(dest, folder)
            if os.path.exists(dst):
                log.debug("Removing existing: %s", dst)
                shutil.rmtree(dst)
            log.debug("Moving %s → %s", folder, dst)
            shutil.move(str(out / folder), dst)
        return folders
    except OSError as exc:
        log.error("Install failed: %s", exc)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_remote(
    spec: RepoSpec,
    remote: RemoteInfo,
    dest: str = ".",
    label: str = "",
) -> list[str] | None:
    """Download *remote* and place its addon folder(s) in *dest*.

    Returns:
        The installed folder names, or ``None`` on failure.
    """
    label = label or spec.repo
    if remote.source == "release" and remote.asset_url:
        return download_extract(remote.asset_url, dest, label=label)
    return _install_branch(spec, dest, label)


# ---------------------------------------------------------------------------
# Source records
# ---------------------------------------------------------------------------


def make_source_entry(
    spec: RepoSpec,
    remote: RemoteInfo,
    folders: list[str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the record stored for a GitHub-installed addon."""
    now = datetime.now(timezone.utc).isoformat()
    meta = read_local_meta(folders)
    return {
        "owner": spec.owner,
        "repo": spec.repo,
        "ref": spec.ref,
        "source": remote.source,
        "tag": remote.tag,
        "tree_sha": remote.tree_sha,
        "title": strip_wow_colors(meta.get("Title") or spec.repo),
        "installed_version": meta.get("Version", ""),
        "folders": folders,
        "installed_at": (previous or {}).get("installed_at", now),
        "updated_at": now,
    }


def entry_spec(entry: dict[str, Any]) -> RepoSpec:
    """Rebuild the :class:`RepoSpec` from a stored source record."""
    return RepoSpec(entry["owner"], entry["repo"], entry.get("ref"))


def is_outdated(
    entry: dict[str, Any],
    remote: RemoteInfo,
    local_version: str,
) -> bool:
    """Decide whether the installed addon is behind *remote*.

    The local ``.toc`` version is compared with the remote version. When
    the remote version is only a placeholder (branch source) the git tree
    SHA recorded at install time is compared instead.

    For releases, a ``.toc`` version that differs from the tag (a hand-written
    ``1.2`` under tag ``v1.2.0``) is fine as long as the recorded tag is
    current *and* the folder still has the ``.toc`` version it was installed
    with — otherwise it would re-install on every run.
    """
    if remote.source == "release":
        if entry.get("source") != "release":
            return True
        local = normalize_version(local_version)
        if local and local == normalize_version(remote.version):
            return False
        if entry.get("tag") != remote.tag:
            return True
        installed = entry.get("installed_version")
        return installed is not None and local != normalize_version(installed)

    if entry.get("source") != "branch":
        return True
    if remote.is_template:
        return entry.get("tree_sha") != remote.tree_sha
    return normalize_version(local_version) != normalize_version(remote.version)
