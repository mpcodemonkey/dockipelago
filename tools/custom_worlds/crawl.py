"""Crawl the Archipelago wiki's custom games and install their apworlds into this checkout.

The run is a pipeline, one game at a time:

    wiki category -> game page -> download link -> GitHub release -> .apworld -> verify -> worlds/

Everything lands in a staging directory first, so a file only reaches ``worlds/`` once it has been
checked against this checkout's Archipelago version and against the other worlds in the same run.
The result is written to ``custom_worlds.lock.json`` so that a later run can tell what changed, and
so the contents of ``worlds/`` can be traced back to a page and a release tag.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .http import HttpClient, HttpError
from .matching import matches
from .releases import ApworldAsset, GitHubClient, Resolution, ResolutionError, resolve_assets
from .verify import (
    STATUS_OK,
    CoreVersions,
    VerificationResult,
    detect_core_versions,
    find_conflicts,
    verify_apworld,
)
from .wiki import (
    DEFAULT_API_URL,
    DEFAULT_CATEGORY,
    DownloadCandidate,
    WikiApiError,
    WikiClient,
    extract_download_url,
    extract_game_name,
)

logger = logging.getLogger("custom_worlds")

DEFAULT_LOCKFILE = "custom_worlds.lock.json"
DEFAULT_OUTPUT = "worlds"
DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024

#: How an apworld ends up in the output directory.
#: ``extract`` unpacks ``<name>/`` out of the zip into ``worlds/<name>/`` - core loads that as a
#: normal folder world, and git can diff it. ``archive`` drops the ``.apworld`` file in as-is.
INSTALL_EXTRACT = "extract"
INSTALL_ARCHIVE = "archive"

# Outcomes for a single game.
OUTCOME_INSTALLED = "installed"
OUTCOME_UPDATED = "updated"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_RESOLVED = "resolved"  # --dry-run only

_FAILURE_OUTCOMES = frozenset({OUTCOME_FAILED, OUTCOME_SKIPPED})


@dataclass
class GameRecord:
    """What happened to one wiki page during a run."""

    title: str
    page_url: str = ""
    #: The game the page is about, which is not always the article title.
    expected_game: str = ""
    #: True when the source repository publishes worlds for more than one game.
    shared_repo: bool = False
    outcome: str = OUTCOME_FAILED
    reason: str = ""
    download_url: str = ""
    strategy: str = ""
    confidence: str = ""
    repo: str = ""
    release_tag: str = ""
    asset_name: str = ""
    file: str = ""
    sha256: str = ""
    size: int = 0
    game: str = ""
    world_version: str = ""
    verification: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outcome not in _FAILURE_OUTCOMES


@dataclass
class CrawlOptions:
    """Everything the crawl needs that is not a network client."""

    root: Path
    output_dir: Path
    lockfile: Path
    category: str = DEFAULT_CATEGORY
    recursive: bool = False
    limit: int | None = None
    only: tuple[str, ...] = ()
    allow_prerelease: bool = False
    all_assets: bool = False
    refresh: bool = False
    dry_run: bool = False
    prune: bool = False
    install_mode: str = INSTALL_EXTRACT
    ignore_game_mismatch: bool = False
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES


class Crawler:
    """Runs the pipeline and collects a record per game."""

    def __init__(
        self,
        options: CrawlOptions,
        wiki: WikiClient,
        github: GitHubClient,
        versions: CoreVersions,
    ) -> None:
        self.options = options
        self.wiki = wiki
        self.github = github
        self.versions = versions
        self.records: list[GameRecord] = []
        self.previous = _read_lockfile(options.lockfile)

    def run(self, staging: Path) -> list[GameRecord]:
        titles = self._titles()
        logger.info("Processing %d custom game page(s)", len(titles))

        verified: dict[Path, tuple[GameRecord, VerificationResult]] = {}
        for index, title in enumerate(titles, start=1):
            logger.info("[%d/%d] %s", index, len(titles), title)
            record = GameRecord(title=title, page_url=self.wiki.page_url(title))
            self.records.append(record)

            resolution = self._resolve(title, record)
            # A release may ship several worlds under --all-assets; each gets its own record, the
            # first reusing the page's record so the common one-world case reads unchanged.
            for position, asset in enumerate(resolution.selected):
                asset_record = record if position == 0 else _sibling_record(record)
                if position > 0:
                    self.records.append(asset_record)
                # Only the primary asset is checked against the expected game: anything after it
                # is there because --all-assets asked for everything in the release.
                result = self._fetch(asset, asset_record, staging, resolution, confirm_game=position == 0)
                if result is not None:
                    verified[result.path] = (asset_record, result)

        self._apply_conflicts(verified)
        if not self.options.dry_run:
            self._install(verified)
        return self.records

    # -- pipeline steps ----------------------------------------------------------------

    def _titles(self) -> list[str]:
        if self.options.only:
            titles = list(self.options.only)
        else:
            titles = self.wiki.category_members(self.options.category, recursive=self.options.recursive)
            logger.info("%s lists %d page(s)", self.options.category, len(titles))
        if self.options.limit is not None:
            titles = titles[: self.options.limit]
        return titles

    def _resolve(self, title: str, record: GameRecord) -> Resolution:
        """Find the page's download link and turn it into the asset(s) to fetch."""
        candidate = self._find_link(title, record)
        if candidate is None:
            return Resolution()

        try:
            resolution = resolve_assets(
                candidate.url,
                self.github,
                expected_game=record.expected_game or title,
                allow_prerelease=self.options.allow_prerelease,
                all_assets=self.options.all_assets,
            )
        except (ResolutionError, HttpError) as error:
            record.outcome = OUTCOME_FAILED
            record.reason = str(error)
            logger.warning("  no apworld: %s", error)
            return Resolution()

        record.shared_repo = resolution.multi_game
        record.notes.extend(resolution.notes)
        for note in resolution.notes:
            logger.info("  %s", note)
        return resolution

    def _fetch(
        self,
        asset: ApworldAsset,
        record: GameRecord,
        staging: Path,
        resolution: Resolution,
        *,
        confirm_game: bool = True,
    ) -> VerificationResult | None:
        """Download and verify an asset, moving on to a runner-up if it turns out to be another game.

        The manifest is the only place a world states which game it implements, and it is not
        readable until the file has been fetched. So when a repository publishes several games, a
        name-based pick is treated as provisional and confirmed against the manifest here.
        """
        queue = [asset, *resolution.alternates] if resolution.multi_game and confirm_game else [asset]
        for attempt, current in enumerate(queue):
            result = self._download_and_verify(current, record, staging)
            if result is None:
                return None
            if not confirm_game:
                return result
            if self._game_is_right(current, record, result, resolution, has_fallback=attempt < len(queue) - 1):
                return result
        return None

    def _download_and_verify(
        self, asset: ApworldAsset, record: GameRecord, staging: Path
    ) -> VerificationResult | None:
        record.repo = asset.source
        record.release_tag = asset.release_tag
        record.asset_name = asset.name

        if self._can_reuse(record):
            record.outcome = OUTCOME_UNCHANGED
            logger.info("  unchanged (%s %s)", asset.source, asset.release_tag or asset.name)
            return None

        try:
            payload = self.github.download(asset, max_bytes=self.options.max_asset_bytes)
        except HttpError as error:
            record.outcome = OUTCOME_FAILED
            record.reason = f"download failed: {error}"
            logger.warning("  download failed: %s", error)
            return None

        record.size = len(payload)
        record.sha256 = hashlib.sha256(payload).hexdigest()

        file_name = _safe_file_name(asset.name)
        if file_name is None:
            record.outcome = OUTCOME_SKIPPED
            record.reason = f"release asset has an unusable file name: {asset.name!r}"
            logger.warning("  %s", record.reason)
            return None

        # Deliberately not lower-cased: core imports the world as worlds.<file stem> and zipimport
        # then looks for a folder of exactly that name inside the zip, so renaming would break it.
        staged = staging / file_name
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)

        result = verify_apworld(staged, self.versions)
        record.verification = result.status
        record.errors.extend(result.errors)
        record.warnings.extend(result.warnings)
        record.game = result.game or ""
        if result.manifest and result.manifest.world_version:
            record.world_version = ".".join(str(part) for part in result.manifest.world_version)

        if not result.installable:
            record.outcome = OUTCOME_SKIPPED
            record.reason = "; ".join(result.errors) or result.status
            logger.warning("  %s: %s", result.status, record.reason)
            return None

        record.outcome = OUTCOME_RESOLVED
        record.file = str(_relative(self.options.output_dir, self.options.root) / staged.name)
        for warning in result.warnings:
            logger.info("  warning: %s", warning)
        logger.info(
            "  %s from %s %s%s",
            staged.name,
            asset.source,
            asset.release_tag or "(direct link)",
            f" - {result.game}" if result.game else "",
        )
        return result

    def _game_is_right(
        self,
        asset: ApworldAsset,
        record: GameRecord,
        result: VerificationResult,
        resolution: Resolution,
        *,
        has_fallback: bool,
    ) -> bool:
        """Confirm the downloaded world implements the game the page is about."""
        expected = record.expected_game or record.title
        if self.options.ignore_game_mismatch or not result.game or not expected:
            return True
        if matches(expected, result.game):
            return True

        detail = f"{asset.name} declares game '{result.game}', but the page is about '{expected}'"
        if not resolution.multi_game:
            # One world in the repository, so this is naming drift rather than the wrong game.
            record.warnings.append(detail)
            logger.info("  note: %s", detail)
            return True

        record.notes.append(f"rejected {detail}")
        logger.warning("  wrong game: %s", detail)
        if not has_fallback:
            record.outcome = OUTCOME_FAILED
            record.reason = (
                f"{asset.source} publishes apworlds for several games and none of them declares "
                f"'{expected}'; last tried {asset.name}, which is '{result.game}'"
            )
        return False

    def _find_link(self, title: str, record: GameRecord) -> DownloadCandidate | None:
        try:
            page = self.wiki.fetch_page(title)
        except (HttpError, WikiApiError) as error:
            record.outcome = OUTCOME_FAILED
            record.reason = f"could not read the wiki page: {error}"
            logger.warning("  wiki page failed: %s", error)
            return None

        record.page_url = page.url
        record.expected_game = extract_game_name(page)
        candidate = extract_download_url(page)
        if candidate is None:
            record.outcome = OUTCOME_FAILED
            record.reason = "no download link found on the page"
            logger.warning("  no download link found")
            return None

        record.download_url = candidate.url
        record.strategy = candidate.strategy
        record.confidence = candidate.confidence
        if candidate.confidence == "low":
            record.warnings.append(f"download link was guessed from the page's external links: {candidate.url}")
        return candidate

    def _can_reuse(self, record: GameRecord) -> bool:
        """True when the previous run already installed this exact release and it is still in place."""
        if self.options.refresh or self.options.dry_run:
            return False
        previous = self.previous.get(_lock_key(record.title, record.asset_name))
        if previous is None or previous.get("release_tag") != record.release_tag:
            return False
        if previous.get("install_mode", INSTALL_ARCHIVE) != self.options.install_mode:
            return False

        installed = self.options.root / str(previous.get("file", ""))
        if self.options.install_mode == INSTALL_ARCHIVE:
            if not installed.is_file():
                return False
            # Cheap tamper check: an edited or truncated file gets re-downloaded.
            if hashlib.sha256(installed.read_bytes()).hexdigest() != previous.get("sha256"):
                return False
        elif not (installed.is_dir() and (installed / "__init__.py").is_file()):
            return False

        record.file = str(previous["file"])
        record.sha256 = str(previous.get("sha256") or "")
        record.size = int(previous.get("size") or 0)
        record.game = str(previous.get("game") or "")
        record.world_version = str(previous.get("world_version") or "")
        record.verification = str(previous.get("verification") or STATUS_OK)
        return True

    def _apply_conflicts(self, verified: dict[Path, tuple[GameRecord, VerificationResult]]) -> None:
        results = [result for _record, result in verified.values()]
        conflicts = find_conflicts(results, self._existing_world_names())
        for path, messages in conflicts.items():
            record = verified[path][0]
            record.outcome = OUTCOME_SKIPPED
            record.reason = "; ".join(messages)
            record.errors.extend(messages)
            logger.warning("conflict for %s: %s", path.name, record.reason)
            del verified[path]

    def _existing_world_names(self) -> set[str]:
        """Module names already provided by this checkout, which a new apworld must not shadow.

        Worlds a previous run installed are excluded: re-installing them is the point, not a clash.
        """
        worlds_dir = self.options.root / "worlds"
        if not worlds_dir.is_dir():
            return set()
        managed = {Path(str(entry["file"])).stem for entry in self.previous.values() if entry.get("file")}
        names = set()
        for entry in worlds_dir.iterdir():
            if entry.name.startswith((".", "_")) or entry.stem in managed:
                continue
            if entry.is_dir() and (entry / "__init__.py").exists():
                names.add(entry.name)
            elif entry.is_file() and entry.suffix == ".apworld":
                names.add(entry.stem)
        return names

    def _install(self, verified: dict[Path, tuple[GameRecord, VerificationResult]]) -> None:
        output = self.options.output_dir
        output.mkdir(parents=True, exist_ok=True)
        installed: set[Path] = set()
        for path, (record, _result) in verified.items():
            if self.options.install_mode == INSTALL_ARCHIVE:
                destination = output / path.name
                existed = destination.exists()
                shutil.copy2(path, destination)
            else:
                destination = output / path.stem
                existed = destination.exists()
                extract_world(path, destination)
            installed.add(destination)
            record.file = str(_relative(destination, self.options.root))
            record.outcome = OUTCOME_UPDATED if existed else OUTCOME_INSTALLED

        if self.options.prune:
            self._prune(output, installed)

    def _prune(self, output: Path, installed: set[Path]) -> None:
        """Delete worlds a previous run installed that this run no longer wants.

        Only paths recorded in the lockfile are considered, so hand-placed worlds and the worlds
        that ship with Archipelago are never touched.
        """
        # Worlds this run reused without re-downloading are still wanted, so keep them too.
        keep = set(installed)
        keep.update(self.options.root / record.file for record in self.records if record.succeeded and record.file)

        for entry in self.previous.values():
            relative = str(entry.get("file") or "")
            if not relative:
                continue
            stale = self.options.root / relative
            if stale in keep or stale.parent != output or not stale.exists():
                continue
            logger.info("pruning %s (no longer listed on the wiki)", relative)
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def write_lockfile(path: Path, records: Sequence[GameRecord], options: CrawlOptions, versions: CoreVersions) -> None:
    """Record what is installed so the next run can diff against it."""
    worlds = [
        {
            "title": record.title,
            "page_url": record.page_url,
            "expected_game": record.expected_game,
            "game": record.game,
            "repo": record.repo,
            "release_tag": record.release_tag,
            "asset_name": record.asset_name,
            "download_url": record.download_url,
            "file": record.file,
            "shared_repo": record.shared_repo,
            "install_mode": options.install_mode,
            "sha256": record.sha256,
            "size": record.size,
            "world_version": record.world_version,
            "verification": record.verification,
            "warnings": record.warnings,
        }
        for record in sorted(records, key=lambda item: item.title.lower())
        if record.succeeded and record.file
    ]
    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "archipelago_version": versions.ap_version_string,
        "container_version": versions.container_version,
        "category": options.category,
        "install_mode": options.install_mode,
        "output_dir": str(_relative(options.output_dir, options.root)),
        "worlds": worlds,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d world(s))", path, len(worlds))


def write_report(path: Path, records: Sequence[GameRecord]) -> None:
    payload = [asdict(record) for record in sorted(records, key=lambda item: item.title.lower())]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", path)


def log_summary(records: Sequence[GameRecord]) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.outcome] = counts.get(record.outcome, 0) + 1

    logger.info("")
    tally = ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items()))
    logger.info("Summary: %s", tally or "nothing")

    problems = [record for record in records if not record.succeeded]
    if problems:
        logger.info("")
        logger.info("Games that produced no usable apworld:")
        width = max(len(record.title) for record in problems)
        for record in sorted(problems, key=lambda item: item.title.lower()):
            logger.info("  %-*s  %-8s %s", width, record.title, record.outcome, record.reason)

    guessed = [record for record in records if record.succeeded and record.confidence == "low"]
    if guessed:
        logger.info("")
        logger.info("Download link was guessed rather than read from the infobox (worth spot checking):")
        for record in sorted(guessed, key=lambda item: item.title.lower()):
            logger.info("  %s -> %s", record.title, record.download_url)

    shared = [record for record in records if record.succeeded and record.shared_repo and record.asset_name]
    if shared:
        logger.info("")
        logger.info("Picked out of a repository that publishes several games (worth spot checking):")
        for record in sorted(shared, key=lambda item: item.title.lower()):
            logger.info(
                "  %-30s %s -> %s (%s)",
                record.title,
                record.repo,
                record.asset_name,
                record.game or "no game in manifest",
            )


def run_import_check(root: Path, python: str = sys.executable) -> tuple[bool, str]:
    """Import ``worlds`` in a subprocess and report which worlds core refused to load.

    This runs the downloaded worlds' module-level code, which is third-party Python. Only use it on
    apworlds you are willing to execute - the same code runs when the Docker image serves them.
    """
    script = (
        "import json, sys\n"
        "import worlds\n"
        "json.dump({\n"
        "    'games': sorted(worlds.AutoWorldRegister.world_types),\n"
        "    'failed': {name: reason.splitlines()[-1] for name, reason in worlds.failed_world_loads.items()},\n"
        "}, sys.stdout)\n"
    )
    logger.info("Running the import check (this executes the downloaded worlds)")
    process = subprocess.run(
        [python, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
        check=False,
    )
    if process.returncode != 0:
        return False, (process.stderr or process.stdout).strip()[-4000:]

    try:
        payload = json.loads(process.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"could not parse the import check output: {process.stdout[-2000:]}"

    failed: dict[str, str] = payload.get("failed", {})
    logger.info("Import check: %d world(s) registered", len(payload.get("games", [])))
    if failed:
        for name, reason in sorted(failed.items()):
            logger.warning("  %s failed to load: %s", name, reason)
        return False, f"{len(failed)} world(s) failed to load"
    return True, ""


def extract_world(archive: Path, destination: Path) -> None:
    """Unpack the ``<stem>/`` folder out of an apworld into ``destination``.

    Archive members are validated rather than trusted: a zip can name entries that escape the
    directory it is unpacked into, and these files come from third parties. Extraction happens in a
    sibling temporary directory so a failure part-way through cannot leave a half-written world in
    ``worlds/``.
    """
    prefix = f"{archive.stem}/"
    staging = destination.with_name(f".{destination.name}.extracting")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        with zipfile.ZipFile(archive) as zip_file:
            for info in zip_file.infolist():
                if not info.filename.startswith(prefix):
                    continue
                relative = info.filename[len(prefix):]
                if not relative:
                    continue
                target = _safe_join(staging, relative)
                if target is None:
                    raise ValueError(f"{archive.name} contains an unsafe archive path: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)

        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _safe_join(base: Path, relative: str) -> Path | None:
    """Join ``relative`` onto ``base``, refusing anything that would land outside it."""
    if relative.startswith("/") or "\\" in relative or "\x00" in relative:
        return None
    base_str = os.path.abspath(base)
    candidate = os.path.normpath(os.path.join(base_str, relative))
    if candidate != base_str and not candidate.startswith(base_str + os.sep):
        return None
    return Path(candidate)


def _safe_file_name(name: str) -> str | None:
    """Reject asset names that would write outside the staging directory."""
    candidate = name.strip()
    if not candidate or candidate in (".", "..") or "/" in candidate or "\\" in candidate or "\x00" in candidate:
        return None
    return candidate


def _relative(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _sibling_record(record: GameRecord) -> GameRecord:
    """A second record for the same wiki page, for releases that ship more than one world."""
    return GameRecord(
        title=record.title,
        page_url=record.page_url,
        expected_game=record.expected_game,
        shared_repo=record.shared_repo,
        download_url=record.download_url,
        strategy=record.strategy,
        confidence=record.confidence,
        warnings=list(record.warnings),
    )


def _lock_key(title: str, asset_name: str) -> str:
    """Lockfile identity. A page contributes one entry per asset, not one per page."""
    return f"{title}\x1f{asset_name}"


def _read_lockfile(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("ignoring unreadable lockfile %s: %s", path, error)
        return {}
    worlds = payload.get("worlds") if isinstance(payload, dict) else None
    if not isinstance(worlds, list):
        return {}
    return {
        _lock_key(str(entry.get("title")), str(entry.get("asset_name", ""))): entry
        for entry in worlds
        if isinstance(entry, dict) and entry.get("title")
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl_custom_worlds",
        description=(
            "Crawl the Archipelago wiki's custom games category, download each game's .apworld "
            "from its GitHub release, verify it against this checkout, and install it into worlds/."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--root", type=Path, default=_repository_root(), help="Archipelago checkout to install into")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="directory under --root to install apworlds into")
    parser.add_argument("--lockfile", default=DEFAULT_LOCKFILE, help="lockfile path, relative to --root")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help="wiki category to crawl")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="MediaWiki api.php endpoint")
    parser.add_argument("--recursive", action="store_true", help="also crawl subcategories")
    parser.add_argument("--limit", type=int, help="only process the first N pages")
    parser.add_argument("--only", nargs="+", default=(), metavar="PAGE", help="process just these page titles")
    parser.add_argument("--allow-prerelease", action="store_true", help="accept pre-release GitHub releases")
    parser.add_argument(
        "--all-assets",
        action="store_true",
        help="install every .apworld in a release instead of the best-matching one",
    )
    parser.add_argument(
        "--install-mode",
        choices=(INSTALL_EXTRACT, INSTALL_ARCHIVE),
        default=INSTALL_EXTRACT,
        help="'extract' unpacks each apworld into worlds/<name>/ so git can diff it; "
        "'archive' drops the .apworld file in unchanged",
    )
    parser.add_argument(
        "--ignore-game-mismatch",
        action="store_true",
        help="install an apworld even when its manifest names a different game than the wiki page; "
        "only useful if the game filter misfires on an unusually named world",
    )
    parser.add_argument("--refresh", action="store_true", help="re-download even when the lockfile says nothing moved")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove worlds a previous run installed but this one did not",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and report without writing to --output")
    parser.add_argument(
        "--import-check",
        action="store_true",
        help="after installing, import worlds/ in a subprocess to confirm core loads them "
        "(this executes the downloaded third-party code)",
    )
    parser.add_argument("--report", type=Path, help="write a detailed JSON report to this path")
    parser.add_argument("--github-token", default=None, help="GitHub token (defaults to $GITHUB_TOKEN / $GH_TOKEN)")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    parser.add_argument(
        "--max-asset-bytes", type=int, default=DEFAULT_MAX_ASSET_BYTES, help="refuse apworld downloads larger than this"
    )
    parser.add_argument(
        "--keep-staging",
        type=Path,
        help="keep downloaded files in this directory instead of a temporary one",
    )
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any game failed")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    root = args.root.resolve()
    options = CrawlOptions(
        root=root,
        output_dir=(root / args.output).resolve(),
        lockfile=(root / args.lockfile).resolve(),
        category=args.category,
        recursive=args.recursive,
        limit=args.limit,
        only=tuple(args.only),
        allow_prerelease=args.allow_prerelease,
        all_assets=args.all_assets,
        refresh=args.refresh,
        dry_run=args.dry_run,
        prune=args.prune,
        install_mode=args.install_mode,
        ignore_game_mismatch=args.ignore_game_mismatch,
        max_asset_bytes=args.max_asset_bytes,
    )

    versions = detect_core_versions(root)
    logger.info(
        "Archipelago %s (APContainer version %d) at %s",
        versions.ap_version_string,
        versions.container_version,
        root,
    )

    http = HttpClient(timeout=args.timeout)
    crawler = Crawler(options, WikiClient(http, args.api_url), GitHubClient(http, token=args.github_token), versions)

    try:
        records = _run_with_staging(crawler, args.keep_staging)
    except (HttpError, WikiApiError) as error:
        logger.error("could not read the wiki category: %s", error)
        return 2

    log_summary(records)
    if args.report:
        write_report(args.report, records)

    exit_code = 0
    if not options.dry_run:
        write_lockfile(options.lockfile, records, options, versions)
        if args.import_check:
            passed, detail = run_import_check(root)
            if not passed:
                logger.error("import check failed: %s", detail)
                exit_code = 1

    if args.strict and any(not record.succeeded for record in records):
        exit_code = 1
    return exit_code


def _run_with_staging(crawler: Crawler, keep_staging: Path | None) -> list[GameRecord]:
    if keep_staging is not None:
        keep_staging.mkdir(parents=True, exist_ok=True)
        return crawler.run(keep_staging)
    with tempfile.TemporaryDirectory(prefix="custom-worlds-") as staging:
        return crawler.run(Path(staging))


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _configure_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True)
