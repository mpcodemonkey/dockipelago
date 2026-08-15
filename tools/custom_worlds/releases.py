"""Turning a wiki download link into a concrete ``.apworld`` file to fetch.

Most custom worlds ship their apworld as a GitHub release asset, so the common path is
"repository URL -> newest release -> ``*.apworld`` asset". A few pages link straight at a file, and
that is handled too.

The wrinkle is that a repository does not necessarily belong to one game. Maintainers who look after
several worlds often publish all of them from a single repository, interleaved in one release feed,
so "newest release with an .apworld in it" reliably fetches whichever game that maintainer touched
most recently rather than the one the wiki page is about. Selection is therefore driven by
:mod:`.matching` against the expected game name, and a repository known to publish several games
yields nothing at all rather than a confident guess.
"""

import logging
import os
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .http import HttpClient, HttpError
from .matching import MIN_MATCH_SCORE, best_score, name_score, normalize

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

#: How many pages of releases to walk before giving up on finding an apworld asset.
MAX_RELEASE_PAGES = 3
RELEASES_PER_PAGE = 30

#: How many runner-up assets to keep, for when the winner turns out to declare a different game.
MAX_ALTERNATES = 4

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


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


@dataclass(frozen=True)
class AssetCandidate:
    """One apworld found in the release feed, with how well it matches the game we want."""

    asset: ApworldAsset
    score: int
    age: int  # 0 is the newest release; used only to break ties between equal scores

    @property
    def stem(self) -> str:
        return self.asset.module_name


@dataclass
class Resolution:
    """What a download link resolved to."""

    selected: list[ApworldAsset] = field(default_factory=list)
    #: Runner-up assets, best first, to fall back on if the winner declares a different game.
    alternates: list[ApworldAsset] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: True when the repository publishes worlds for more than one game.
    multi_game: bool = False
    expected_game: str = ""
    #: Set when nothing was selected, explaining why.
    failure: str = ""


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
    expected_game: str = "",
    allow_prerelease: bool = False,
    all_assets: bool = False,
) -> Resolution:
    """Resolve ``url`` to the apworld asset(s) to download for ``expected_game``."""
    # GitHub links are parsed first even when they end in .apworld, so a release asset link keeps
    # the repository and tag it came from instead of degrading to an anonymous file download.
    target = parse_github_url(url)
    if target is None:
        if url.lower().split("?")[0].endswith(".apworld"):
            name = urllib.parse.unquote(url.split("?", maxsplit=1)[0].rsplit("/", 1)[-1])
            host = urllib.parse.urlsplit(url).netloc
            return Resolution(
                selected=[ApworldAsset(name=name, download_url=url, source=host)],
                notes=["linked directly at an .apworld file"],
            )
        raise ResolutionError(f"{url} is not a GitHub link and is not a direct .apworld download")

    if target.asset_url and target.asset_name:
        asset = ApworldAsset(
            name=target.asset_name,
            download_url=target.asset_url,
            source=target.slug,
            release_tag=target.tag or "",
        )
        return Resolution(selected=[asset], notes=["linked directly at a release asset"])

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

    resolution = select_apworld_assets(
        releases,
        expected_game=expected_game or target.repo,
        allow_prerelease=allow_prerelease,
        all_assets=all_assets,
        source=target.slug,
    )
    resolution.notes[:0] = notes
    if not resolution.selected:
        raise ResolutionError(resolution.failure or f"no .apworld asset in the releases of {target.slug}")
    return resolution


def select_apworld_assets(
    releases: Iterable[dict[str, Any]],
    *,
    expected_game: str = "",
    allow_prerelease: bool = False,
    all_assets: bool = False,
    source: str = "",
) -> Resolution:
    """Pick the apworld asset(s) a repository publishes for ``expected_game``.

    A repository that only ever publishes one world is easy: take its newest release. A repository
    that publishes several - one maintainer, several unrelated games - is the case this exists for.
    There, every release is a candidate and the newest is usually the *wrong* game, so candidates
    are ranked by how well their asset name, release tag and release title match the game the page
    is about, and nothing is installed unless something actually matches.
    """
    notes: list[str] = []
    usable = [release for release in releases if not release.get("draft")]
    usable.sort(key=_release_sort_key, reverse=True)

    stable = [release for release in usable if not release.get("prerelease")]
    ordered = usable if allow_prerelease else stable
    if not allow_prerelease and not stable and usable:
        notes.append("only pre-releases available, using the newest one")
        ordered = usable

    candidates = _candidates(ordered, expected_game, source)
    if not candidates:
        return Resolution(notes=notes)

    published_games = sorted({normalize(candidate.stem) for candidate in candidates})
    multi_game = len(published_games) > 1

    if multi_game:
        winner = _pick_from_multi_game_repo(candidates, expected_game, source, published_games, notes)
        if winner is None:
            return Resolution(
                notes=notes,
                multi_game=True,
                expected_game=expected_game,
                failure=(
                    f"{source or 'the repository'} publishes apworlds for several games "
                    f"({', '.join(published_games)}) and none of them matches '{expected_game}'"
                ),
            )
    else:
        # One world, so the newest release of it is what we want regardless of what it is called.
        winner = candidates[0]

    selected = _assets_to_install(winner, candidates, all_assets=all_assets, notes=notes)
    if winner.asset.prerelease:
        notes.append(f"release {winner.asset.release_tag} is marked as a pre-release")

    chosen_urls = {asset.download_url for asset in selected}
    alternates = [
        candidate.asset
        for candidate in candidates
        if candidate.asset.download_url not in chosen_urls and candidate.score > 0
    ]
    return Resolution(
        selected=selected,
        alternates=alternates[:MAX_ALTERNATES],
        notes=notes,
        multi_game=multi_game,
        expected_game=expected_game,
    )


def _candidates(releases: Sequence[dict[str, Any]], expected_game: str, source: str) -> list[AssetCandidate]:
    """Every .apworld across every release, scored against the expected game, best first.

    Ties are broken towards the newer release, so an unscored repository still behaves like the old
    "newest release wins" rule.
    """
    candidates: list[AssetCandidate] = []
    for age, release in enumerate(releases):
        for asset in release.get("assets") or []:
            name = str(asset.get("name", ""))
            if not name.lower().endswith(".apworld"):
                continue
            built = ApworldAsset(
                name=name,
                download_url=str(asset.get("browser_download_url", "")),
                source=source,
                size=asset.get("size"),
                release_tag=str(release.get("tag_name", "")),
                release_name=str(release.get("name") or ""),
                published_at=str(release.get("published_at") or ""),
                prerelease=bool(release.get("prerelease")),
            )
            candidates.append(AssetCandidate(asset=built, score=_score(expected_game, built), age=age))

    candidates.sort(key=lambda candidate: (candidate.score, -candidate.age), reverse=True)
    return candidates


def _pick_from_multi_game_repo(
    candidates: Sequence[AssetCandidate],
    expected_game: str,
    source: str,
    published_games: Sequence[str],
    notes: list[str],
) -> "AssetCandidate | None":
    """Choose the best-matching candidate, or nothing if the repository has no world for this game."""
    matching = [candidate for candidate in candidates if candidate.score >= MIN_MATCH_SCORE]
    if not matching:
        return None

    winner = matching[0]
    notes.append(
        f"{source or 'the repository'} publishes apworlds for {len(published_games)} games "
        f"({', '.join(published_games)}); picked {winner.asset.name} from release "
        f"{winner.asset.release_tag or '(untagged)'} as the match for '{expected_game}'"
    )
    return winner


def _assets_to_install(
    winner: AssetCandidate,
    candidates: Sequence[AssetCandidate],
    *,
    all_assets: bool,
    notes: list[str],
) -> list[ApworldAsset]:
    """The winning asset, or every apworld in the winning release under ``--all-assets``."""
    others = sorted(
        (
            candidate.asset
            for candidate in candidates
            if candidate.asset.release_tag == winner.asset.release_tag
            and candidate.asset.download_url != winner.asset.download_url
        ),
        key=lambda asset: asset.name,
    )
    if all_assets:
        # The winner stays first: callers rely on it being the asset the game filter chose.
        return [winner.asset, *others]

    if others:
        names = ", ".join(asset.name for asset in others)
        notes.append(f"ignored other .apworld assets in the same release: {names}")
    return [winner.asset]


def _score(expected_game: str, asset: ApworldAsset) -> int:
    """How strongly this asset looks like it belongs to ``expected_game``.

    The file name is the most reliable signal, so a match on the release tag or title alone is
    discounted - those often carry only a version number, or the maintainer's own naming scheme.
    """
    if not expected_game:
        return 0
    stem_score = name_score(expected_game, asset.module_name)
    context_score = best_score(expected_game, asset.release_tag, asset.release_name)
    return max(stem_score, int(context_score * 0.8))


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
