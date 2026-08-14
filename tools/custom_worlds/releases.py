"""Turning a wiki download link into a concrete ``.apworld`` file to fetch.

Most custom worlds ship their apworld as a GitHub release asset, so the common path is
"repository URL -> newest release -> ``*.apworld`` asset". A few pages link straight at a file, and
that is handled too.
"""

import logging
import os
import re
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, HttpError

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

#: How many pages of releases to walk before giving up on finding an apworld asset.
MAX_RELEASE_PAGES = 3
RELEASES_PER_PAGE = 30

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class ResolutionError(Exception):
    """The download link could not be turned into an apworld asset."""


@dataclass(frozen=True)
class GitHubTarget:
    """A GitHub repository, optionally narrowed to a tag or a specific asset."""

    owner: str
    repo: str
    tag: str | None = None
    asset_url: str | None = None
    asset_name: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class ApworldAsset:
    """A downloadable ``.apworld``."""

    name: str
    download_url: str
    source: str
    size: int | None = None
    release_tag: str = ""
    release_name: str = ""
    published_at: str = ""
    prerelease: bool = False

    @property
    def module_name(self) -> str:
        """The ``worlds.<name>`` module this file will be imported as."""
        return self.name[: -len(".apworld")] if self.name.endswith(".apworld") else self.name


class GitHubClient:
    """The sliver of the GitHub REST API the crawler needs."""

    def __init__(self, http: HttpClient, *, token: str | None = None, api_url: str = DEFAULT_API_URL) -> None:
        self.http = http
        self.api_url = api_url.rstrip("/")
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def list_releases(self, owner: str, repo: str, *, max_pages: int = MAX_RELEASE_PAGES) -> list[dict[str, Any]]:
        """Return releases newest-first, following pagination up to ``max_pages``."""
        releases: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            url = f"{self.api_url}/repos/{owner}/{repo}/releases?per_page={RELEASES_PER_PAGE}&page={page}"
            payload = self.http.get_json(url, headers=self.headers)
            if not isinstance(payload, list):
                raise ResolutionError(f"unexpected releases payload for {owner}/{repo}")
            releases.extend(payload)
            if len(payload) < RELEASES_PER_PAGE:
                break
        return releases

    def get_release_by_tag(self, owner: str, repo: str, tag: str) -> dict[str, Any]:
        url = f"{self.api_url}/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag)}"
        payload = self.http.get_json(url, headers=self.headers)
        if not isinstance(payload, dict):
            raise ResolutionError(f"unexpected release payload for {owner}/{repo}@{tag}")
        return payload

    def download(self, asset: ApworldAsset, *, max_bytes: int) -> bytes:
        # Release assets redirect to a signed storage URL; HttpClient drops the token on the way.
        return self.http.get(asset.download_url, headers=self.headers, max_bytes=max_bytes)


def parse_github_url(url: str) -> GitHubTarget | None:
    """Interpret a GitHub URL, whatever shape the wiki linked to."""
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.lower() not in _GITHUB_HOSTS:
        return None
    segments = [urllib.parse.unquote(segment) for segment in parts.path.split("/") if segment]
    if len(segments) < 2:
        return None

    owner, repo = segments[0], segments[1].removesuffix(".git")
    rest = segments[2:]

    if len(rest) >= 4 and rest[0] == "releases" and rest[1] == "download":
        # /owner/repo/releases/download/<tag>/<file>
        return GitHubTarget(owner, repo, tag=rest[2], asset_url=url, asset_name=rest[-1])
    if len(rest) >= 3 and rest[0] == "releases" and rest[1] == "tag":
        return GitHubTarget(owner, repo, tag=rest[2])
    if len(rest) >= 3 and rest[0] in ("blob", "raw") and rest[-1].lower().endswith(".apworld"):
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{'/'.join(rest[1:])}"
        return GitHubTarget(owner, repo, asset_url=raw_url, asset_name=rest[-1])
    return GitHubTarget(owner, repo)


def resolve_assets(
    url: str,
    github: GitHubClient,
    *,
    allow_prerelease: bool = False,
    all_assets: bool = False,
    hints: Sequence[str] = (),
) -> tuple[list[ApworldAsset], list[str]]:
    """Resolve ``url`` to the apworld asset(s) to download, plus notes about the choices made."""
    # GitHub links are parsed first even when they end in .apworld, so a release asset link keeps
    # the repository and tag it came from instead of degrading to an anonymous file download.
    target = parse_github_url(url)
    if target is None:
        if url.lower().split("?")[0].endswith(".apworld"):
            name = urllib.parse.unquote(url.split("?", maxsplit=1)[0].rsplit("/", 1)[-1])
            host = urllib.parse.urlsplit(url).netloc
            return [ApworldAsset(name=name, download_url=url, source=host)], ["linked directly at an .apworld file"]
        raise ResolutionError(f"{url} is not a GitHub link and is not a direct .apworld download")

    if target.asset_url and target.asset_name:
        asset = ApworldAsset(
            name=target.asset_name,
            download_url=target.asset_url,
            source=target.slug,
            release_tag=target.tag or "",
        )
        return [asset], ["linked directly at a release asset"]

    notes: list[str] = []
    if target.tag:
        releases = [_fetch_release_by_tag(github, target, notes)]
    else:
        try:
            releases = github.list_releases(target.owner, target.repo)
        except HttpError as error:
            raise ResolutionError(f"could not list releases for {target.slug}: {error}") from error
        if not releases:
            raise ResolutionError(f"{target.slug} has no GitHub releases")

    assets, selection_notes = select_apworld_assets(
        releases,
        allow_prerelease=allow_prerelease,
        all_assets=all_assets,
        hints=[*hints, target.repo],
        source=target.slug,
    )
    notes.extend(selection_notes)
    if not assets:
        raise ResolutionError(f"no .apworld asset in the releases of {target.slug}")
    return assets, notes


def select_apworld_assets(
    releases: Iterable[dict[str, Any]],
    *,
    allow_prerelease: bool = False,
    all_assets: bool = False,
    hints: Sequence[str] = (),
    source: str = "",
) -> tuple[list[ApworldAsset], list[str]]:
    """Pick the apworld asset(s) from a repository's releases, newest usable release first."""
    notes: list[str] = []
    usable = [release for release in releases if not release.get("draft")]
    usable.sort(key=_release_sort_key, reverse=True)

    stable = [release for release in usable if not release.get("prerelease")]
    ordered = usable if allow_prerelease else stable
    if not allow_prerelease and not stable and usable:
        notes.append("only pre-releases available, using the newest one")
        ordered = usable

    for release in ordered:
        candidates = [
            asset for asset in release.get("assets") or [] if str(asset.get("name", "")).lower().endswith(".apworld")
        ]
        if not candidates:
            continue

        chosen = candidates if all_assets else [_best_asset(candidates, hints)]
        skipped = [asset["name"] for asset in candidates if asset not in chosen]
        if skipped:
            notes.append(f"ignored other .apworld assets in the same release: {', '.join(sorted(skipped))}")
        if release.get("prerelease"):
            notes.append(f"release {release.get('tag_name', '')} is marked as a pre-release")

        return [
            ApworldAsset(
                name=str(asset["name"]),
                download_url=str(asset.get("browser_download_url", "")),
                source=source,
                size=asset.get("size"),
                release_tag=str(release.get("tag_name", "")),
                release_name=str(release.get("name") or ""),
                published_at=str(release.get("published_at") or ""),
                prerelease=bool(release.get("prerelease")),
            )
            for asset in chosen
        ], notes

    return [], notes


def _fetch_release_by_tag(github: GitHubClient, target: GitHubTarget, notes: list[str]) -> dict[str, Any]:
    assert target.tag is not None
    try:
        return github.get_release_by_tag(target.owner, target.repo, target.tag)
    except HttpError as error:
        notes.append(f"tag {target.tag} could not be read ({error}); falling back to the newest release")
        releases = github.list_releases(target.owner, target.repo, max_pages=1)
        if not releases:
            raise ResolutionError(f"{target.slug} has no GitHub releases") from error
        return releases[0]


def _release_sort_key(release: dict[str, Any]) -> tuple[str, str]:
    return (
        str(release.get("published_at") or release.get("created_at") or ""),
        str(release.get("tag_name") or ""),
    )


def _best_asset(assets: Sequence[dict[str, Any]], hints: Sequence[str]) -> dict[str, Any]:
    """Prefer the asset whose name looks like the repository or page it came from."""
    normalized_hints = [_normalize(hint) for hint in hints if hint]

    def score(asset: dict[str, Any]) -> tuple[int, int]:
        stem = _normalize(str(asset.get("name", "")).removesuffix(".apworld"))
        best = 0
        for hint in normalized_hints:
            if not stem or not hint:
                continue
            if stem == hint:
                best = max(best, 3)
            elif stem in hint or hint in stem:
                best = max(best, 2)
        # Shorter names win ties: "game.apworld" over "game_debug_build.apworld".
        return best, -len(str(asset.get("name", "")))

    return max(assets, key=score)


def _normalize(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())
