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
from .repair import repair_apworld, repairable
from .validate import (
    ValidationReport,
    validate_generation,
    validate_installed_worlds,
)
from .verify import (
    MAX_MODULE_BYTES,
    MAX_SOURCE_BYTES,
    STATUS_INVALID,
    STATUS_OK,
    WEBHOST_ERROR,
    WEBHOST_POLICIES,
    CoreVersions,
    VerificationResult,
    detect_core_versions,
    detect_target_python,
    find_conflicts,
    verify_apworld,
)
from .webworld import module_path, patch_endings, world_game
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

#: The folder the WebHost copies each world's tutorial files out of.
DOCS_FOLDER = "docs"

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
OUTCOME_KNOWN_BAD = "known-bad"  # rejected by an earlier run, so not downloaded again

_FAILURE_OUTCOMES = frozenset({OUTCOME_FAILED, OUTCOME_SKIPPED, OUTCOME_KNOWN_BAD})

#: Bumped whenever the checks change their mind about what is acceptable. Lockfile entries written
#: by an older version are re-verified rather than trusted, so a new check reaches worlds that were
#: installed before it existed.
CHECKS_VERSION = 8


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
    #: Machine-readable codes for why a world was rejected.
    codes: list[str] = field(default_factory=list)
    #: Path removed from the output directory because what was installed there is now rejected.
    removed: str = ""
    #: Findings --repair wrote its way out of, so the lockfile records what was not the world's own.
    repaired: list[str] = field(default_factory=list)
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
    repair: bool = False
    validate: bool = False
    validate_generation: bool = False
    validate_python: str = sys.executable
    #: Python the image will run. Older than this, a SyntaxError is not proof of a broken world.
    target_python: tuple[int, int] | None = None
    webhost_check: str = WEBHOST_ERROR
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES

    @property
    def full_crawl(self) -> bool:
        """Whether this run saw the whole category, and so can speak for what is no longer in it.

        Under --only or --limit the pages that went unvisited say nothing at all, so their absence
        from the run must not be read as their absence from the wiki.
        """
        return not self.only and self.limit is None


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
        #: Filled in by report_generation, for the run report.
        self.generation: ValidationReport | None = None
        target = options.target_python
        #: Whether this interpreter can be trusted to judge a world's syntax. A world may use syntax
        #: newer than the Python running the crawler, and refusing it would cost a working world.
        self.syntax_authoritative = target is None or sys.version_info[:2] >= target
        if not self.syntax_authoritative and target is not None:
            logger.warning(
                "Running Python %d.%d but the image runs %d.%d; worlds using newer syntax will be "
                "reported rather than refused. Run the crawler on Python %d.%d for a firm answer.",
                *sys.version_info[:2], *target, *target,
            )
        previous, rejected = _read_lockfile(options.lockfile)
        self.previous = previous
        #: What earlier runs rejected, so the same broken release is not fetched again.
        self.rejected = rejected

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
                    self._claim(verified, result, asset_record)

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

        remembered = self._remembered_rejection(record)
        if remembered is not None:
            record.outcome = OUTCOME_KNOWN_BAD
            record.reason = str(remembered.get("reason") or "rejected by an earlier run")
            record.verification = str(remembered.get("verification") or STATUS_INVALID)
            record.codes = [str(code) for code in remembered.get("codes") or []]
            record.game = str(remembered.get("game") or "")
            record.sha256 = str(remembered.get("sha256") or "")
            logger.info("  known bad, not downloaded again: %s", record.reason)
            self._remove_installed(record)
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

        result = self._verify(staged)
        if self.options.repair and not result.installable and repairable(result.findings):
            result = self._repair(staged, record, result)
        record.verification = result.status
        record.errors.extend(result.errors)
        record.warnings.extend(result.warnings)
        record.game = result.game or ""
        if result.manifest and result.manifest.world_version:
            record.world_version = ".".join(str(part) for part in result.manifest.world_version)

        if not result.installable:
            record.outcome = OUTCOME_SKIPPED
            record.reason = "; ".join(result.errors) or result.status
            record.codes = list(result.codes)
            logger.warning("  %s: %s", result.status, record.reason)
            self._remove_installed(record)
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

    def _verify(self, staged: Path) -> VerificationResult:
        return verify_apworld(
            staged,
            self.versions,
            webhost_check=self.options.webhost_check,
            # A missing docs/ folder only stops the WebHost when the world is a folder it lists.
            check_docs=self.options.install_mode == INSTALL_EXTRACT,
            syntax_authoritative=self.syntax_authoritative,
        )

    def _repair(self, staged: Path, record: GameRecord, result: VerificationResult) -> VerificationResult:
        """Write the WebWorld paperwork a world is missing, and keep the result only if it works.

        The repair is a proposal, never a verdict: the rewritten world is verified again from
        scratch, and one that still fails is refused exactly as it would have been. A world is only
        repaired when every finding against it is repairable, so this never installs something with
        one problem papered over and another left in place.
        """
        patched = staged.with_name(f".repaired.{staged.name}")
        try:
            applied = repair_apworld(staged, patched, result.findings)
        except (OSError, zipfile.BadZipFile, ValueError) as error:
            logger.warning("  could not repair %s: %s", staged.name, error)
            patched.unlink(missing_ok=True)
            return result
        if not applied:
            patched.unlink(missing_ok=True)
            return result

        # Verified under the name it will be installed as, since core ties the two together.
        patched.replace(staged)
        repaired = self._verify(staged)
        if not repaired.installable:
            logger.warning("  repair did not take for %s: %s", record.title, repaired.summary()[:160])
            return repaired

        record.repaired = sorted(set(applied))
        logger.info("  repaired %s: %s", record.title, ", ".join(record.repaired))
        return repaired

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
        if not self._checked_the_same_way(previous):
            return False  # written before a check changed; re-verify rather than trust it
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

    def report_generation(self) -> tuple[bool, str]:
        """Ask every installed world for a seed, and report which ones cannot produce one.

        Report-only on purpose. A world can fail here for reasons that are not faults - needing
        non-default options, or a setting this checkout has no value for - and one solo seed with
        default options is a smoke test rather than a verdict. Removing worlds on that evidence
        would cost working games, so this says what happened and leaves the decision alone.
        """
        report = validate_generation(self.options.root, python=self.options.validate_python)
        if not report.ok:
            return False, report.error

        self.generation = report
        failures = report.verdicts
        logger.info("")
        if not failures:
            logger.info("All %d world(s) generated a solo seed", report.registered)
            return True, ""

        logger.warning(
            "%d of %d world(s) could not generate a solo seed (reported, not removed):",
            len(failures),
            report.registered,
        )
        for verdict in sorted(failures, key=lambda item: (item.status, item.game.lower())):
            logger.warning("  %-34s %-18s %s", verdict.game[:34], verdict.status, verdict.reason[:90])
        return True, f"{len(failures)} world(s) could not generate a solo seed"

    def validate_and_remove(self) -> tuple[bool, str]:
        """Run Archipelago's own start-up checks and take out whatever they reject.

        The static checks cannot see a WebWorld built by a factory, or an option the WebHost's
        template renderer chokes on. This is the backstop: whatever Archipelago itself refuses is
        removed and recorded, so it is not downloaded again either.
        """
        report = validate_installed_worlds(self.options.root, python=self.options.validate_python)
        if not report.ok:
            return False, report.error

        logger.info("Archipelago registered %d world(s)", report.registered)
        by_module = report.by_module()
        removed = 0
        for record in self.records:
            if not record.succeeded or not record.file:
                continue
            verdict = by_module.get(Path(record.file).stem)
            if verdict is None:
                continue
            record.outcome = OUTCOME_SKIPPED
            record.reason = f"{verdict.status}: {verdict.reason}"
            record.codes.append(verdict.status)
            record.verification = STATUS_INVALID
            logger.warning("  %s: %s", record.title, record.reason)
            self._remove_path(record)
            removed += 1

        unclaimed = [
            verdict
            for module, verdict in by_module.items()
            if module not in {Path(record.file).stem for record in self.records if record.file}
        ]
        for verdict in unclaimed:
            # A world this run did not install - already present, or shipped with Archipelago.
            logger.warning("  %s (%s) is rejected by Archipelago but was not installed by this run",
                           verdict.game or verdict.module, verdict.status)

        return True, f"removed {removed} world(s) Archipelago rejected" if removed else ""

    def _remove_path(self, record: GameRecord) -> None:
        """Delete what this run installed for a record, once it turns out to be unusable."""
        installed = self.options.root / record.file
        if installed.parent != self.options.output_dir or not installed.exists():
            return
        if self.options.dry_run:
            logger.warning("  would remove %s", record.file)
            return
        if installed.is_dir():
            shutil.rmtree(installed)
        else:
            installed.unlink()
        record.removed = record.file
        record.file = ""

    def _checked_the_same_way(self, entry: dict[str, Any]) -> bool:
        """Whether a lockfile entry was produced by the same checks this run is applying.

        A verdict is only worth trusting if nothing that could change it has moved: the checks
        themselves, the Archipelago version the world was measured against, and the policy for
        worlds the WebHost would drop.
        """
        checked = entry.get("checked")
        if not isinstance(checked, dict):
            return False
        return (
            checked.get("checks_version") == CHECKS_VERSION
            and checked.get("archipelago_version") == self.versions.ap_version_string
            and checked.get("container_version") == self.versions.container_version
            and checked.get("webhost_check") == self.options.webhost_check
            # Turning --repair on has to reach the worlds an earlier run turned away without it.
            and bool(checked.get("repair", False)) == self.options.repair
        )

    def _remembered_rejection(self, record: GameRecord) -> dict[str, Any] | None:
        """A previous run's verdict on this exact release, if it still applies."""
        if self.options.refresh:
            return None
        entry = self.rejected.get(_lock_key(record.title, record.asset_name))
        if entry is None or entry.get("release_tag") != record.release_tag:
            return None  # a new release may well have fixed it, so try again
        return entry if self._checked_the_same_way(entry) else None

    def _remove_installed(self, record: GameRecord) -> None:
        """Delete a previously installed copy of a world this run has just rejected.

        Only the exact release that was rejected is removed. If an older, working release is what is
        actually installed, it stays: a broken new release is no reason to lose a game that works.
        Only paths this crawler recorded installing are ever touched.
        """
        previous = self.previous.get(_lock_key(record.title, record.asset_name))
        if previous is None or not previous.get("file"):
            return
        if previous.get("release_tag") != record.release_tag:
            record.notes.append(
                f"keeping the installed {previous.get('release_tag')}, which is not the release that was rejected"
            )
            logger.info("  keeping installed %s", previous.get("release_tag"))
            return

        installed = self.options.root / str(previous["file"])
        if installed.parent != self.options.output_dir or not installed.exists():
            return
        if self.options.dry_run:
            logger.warning("  would remove %s (rejected)", previous["file"])
            return

        if installed.is_dir():
            shutil.rmtree(installed)
        else:
            installed.unlink()
        record.removed = str(previous["file"])
        logger.warning("  removed %s, which is no longer acceptable", record.removed)

    def _claim(
        self,
        verified: dict[Path, tuple[GameRecord, VerificationResult]],
        result: VerificationResult,
        record: GameRecord,
    ) -> None:
        """Record an asset against the page that resolved it, settling ties between pages.

        Two pages can land on the same file. It happens when a repository ships one world and two
        wiki pages point at it - a rename the wiki still lists under both names, or a page whose own
        release simply is not there, so the name match settles for the only asset published. Either
        way there is one world, installed once, under one module name.

        Keyed only by path, the second page used to overwrite the first in this dict and the loser
        was never installed - but it kept the provisional path from download time and was written to
        the lockfile as an installed world, pointing at something that does not exist. So the tie is
        settled openly instead: the page whose game the manifest actually names keeps the world, and
        the other is skipped with the reason spelled out.
        """
        holder = verified.get(result.path)
        if holder is None:
            verified[result.path] = (record, result)
            return

        incumbent = holder[0]
        expected = record.expected_game or record.title
        claimed = result.game or ""
        # Only a manifest that names the challenger's game, and not the incumbent's, changes hands.
        takes_over = bool(
            claimed
            and matches(expected, claimed)
            and not matches(incumbent.expected_game or incumbent.title, claimed)
        )
        loser, winner = (incumbent, record) if takes_over else (record, incumbent)
        if takes_over:
            verified[result.path] = (record, result)

        loser.outcome = OUTCOME_SKIPPED
        loser.file = ""
        loser.reason = (
            f"{result.path.name} is the same world '{winner.title}' resolved to; one file cannot be "
            f"installed as two worlds, so it is recorded there"
        )
        logger.warning("  %s: %s", loser.title, loser.reason)

    def _apply_conflicts(self, verified: dict[Path, tuple[GameRecord, VerificationResult]]) -> None:
        results = [result for _record, result in verified.values()]
        games, endings = self._existing_registrations()
        conflicts = find_conflicts(
            results,
            self._existing_world_names(),
            existing_games=games,
            existing_endings=endings,
        )
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

    def _existing_registrations(self) -> tuple[dict[str, str], dict[str, str]]:
        """The game names and patch extensions this checkout's own worlds already register.

        Both are process-wide registries that do not care which world got there first, so a custom
        world claiming either one takes a world out - and since ``worlds/`` loads alphabetically, the
        one it takes out can be Archipelago's. That is what happened to core's Gauntlet Legends: a
        custom ``gauntlet_legends`` claimed ``.apgl`` and ``gl`` failed to import behind it.

        Read from the source rather than by importing, like everything else here, and only from the
        folder worlds this checkout ships - anything a previous run installed is excluded, because
        re-installing it is the point rather than a clash.
        """
        games: dict[str, str] = {}
        endings: dict[str, str] = {}
        worlds_dir = self.options.root / "worlds"
        if not worlds_dir.is_dir():
            return games, endings
        managed = {Path(str(entry["file"])).stem for entry in self.previous.values() if entry.get("file")}

        for entry in sorted(worlds_dir.iterdir()):
            if entry.name.startswith((".", "_")) or entry.name in managed or not entry.is_dir():
                continue
            if not (entry / "__init__.py").is_file():
                continue
            modules = _read_world_folder(entry)
            for ending in patch_endings(modules):
                endings.setdefault(ending, entry.name)
            game = world_game(modules)
            if game:
                games.setdefault(game, entry.name)
        return games, endings

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
                for left_out in extract_world(path, destination):
                    record.warnings.append(
                        f"left out {left_out}: the WebHost copies each world's docs/ into one flat "
                        "folder and cannot handle a subfolder there"
                    )
                    logger.info("  %s: left out %s", record.title, left_out)
            installed.add(destination)
            record.file = str(_relative(destination, self.options.root))
            record.outcome = OUTCOME_UPDATED if existed else OUTCOME_INSTALLED

        if self.options.prune:
            self._prune(output, installed)

    def _prune(self, output: Path, installed: set[Path]) -> None:
        """Delete worlds a previous run installed that this run no longer wants.

        Only paths recorded in the lockfile are considered, so hand-placed worlds and the worlds
        that ship with Archipelago are never touched. Pages this run did not look at are left alone
        too: under --only or --limit their absence from this run says nothing about the wiki.
        """
        # Worlds this run reused without re-downloading are still wanted, so keep them too.
        keep = set(installed)
        keep.update(self.options.root / record.file for record in self.records if record.succeeded and record.file)
        processed = {record.title for record in self.records}

        for entry in self.previous.values():
            relative = str(entry.get("file") or "")
            if not relative:
                continue
            if not self.options.full_crawl and str(entry.get("title")) not in processed:
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


def write_lockfile(
    path: Path,
    records: Sequence[GameRecord],
    options: CrawlOptions,
    versions: CoreVersions,
    *,
    previously_installed: dict[str, dict[str, Any]] | None = None,
    previously_rejected: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Record what is installed, and what was rejected, so the next run can skip both.

    The rejected list is the reason a broken release is only ever downloaded once. Each entry
    fingerprints the checks that produced the verdict, so it stops applying by itself when the
    Archipelago version, the WebHost policy or the checks themselves change.
    """
    checked = {
        "checks_version": CHECKS_VERSION,
        "archipelago_version": versions.ap_version_string,
        "container_version": versions.container_version,
        "webhost_check": options.webhost_check,
        "repair": options.repair,
    }
    ordered = sorted(records, key=lambda item: item.title.lower())
    # A partial run (--only, --limit) must not erase everything it did not look at. A full one may:
    # a page missing from a full crawl really has left the category.
    processed = {record.title for record in records} if not options.full_crawl else None

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
            "repaired": record.repaired,
            "install_mode": options.install_mode,
            "sha256": record.sha256,
            "size": record.size,
            "world_version": record.world_version,
            "verification": record.verification,
            "warnings": record.warnings,
            "checked": checked,
        }
        for record in ordered
        if record.succeeded and record.file
    ]
    worlds = _merge(worlds, previously_installed or {}, processed)

    rejected = _rejected_entries(ordered, checked, previously_rejected or {})
    rejected = _merge(rejected, previously_rejected or {}, processed)

    payload = {
        "generated_at": _now(),
        "archipelago_version": versions.ap_version_string,
        "container_version": versions.container_version,
        "checks_version": CHECKS_VERSION,
        "category": options.category,
        "install_mode": options.install_mode,
        "webhost_check": options.webhost_check,
        "repair": options.repair,
        "output_dir": str(_relative(options.output_dir, options.root)),
        "worlds": worlds,
        "rejected": rejected,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d world(s), %d rejected)", path, len(worlds), len(rejected))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge(
    current: list[dict[str, Any]],
    earlier: dict[str, dict[str, Any]],
    processed: set[str] | None,
) -> list[dict[str, Any]]:
    """Add back entries for pages a partial run never visited, so it keeps the rest.

    ``processed`` is None after a full crawl, where the run's own records are the whole truth.
    """
    if processed is None:
        return current
    carried = [entry for entry in earlier.values() if str(entry.get("title")) not in processed]
    return sorted(current + carried, key=lambda entry: str(entry.get("title", "")).lower())


def _rejected_entries(
    records: Sequence[GameRecord],
    checked: dict[str, Any],
    previously_rejected: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the rejected list, keeping the date each release was first turned down."""
    entries: list[dict[str, Any]] = []
    for record in records:
        if record.outcome not in (OUTCOME_SKIPPED, OUTCOME_KNOWN_BAD) or not record.asset_name:
            continue
        earlier = previously_rejected.get(_lock_key(record.title, record.asset_name), {})
        if earlier.get("release_tag") != record.release_tag:
            earlier = {}  # a different release, so none of its history carries over
        entries.append(
            {
                "title": record.title,
                "page_url": record.page_url,
                "game": record.game,
                "repo": record.repo,
                "release_tag": record.release_tag,
                "asset_name": record.asset_name,
                "download_url": record.download_url,
                "sha256": record.sha256,
                "verification": record.verification or STATUS_INVALID,
                "codes": record.codes,
                "reason": record.reason,
                # Both are kept across runs so the lockfile still shows when a release was first
                # turned down, and that a copy of it was taken back out of the output directory.
                "removed": record.removed or str(earlier.get("removed") or ""),
                "first_rejected": earlier.get("first_rejected") or _now(),
                "checked": checked,
            }
        )
    return entries


def write_report(
    path: Path, records: Sequence[GameRecord], *, generation: "ValidationReport | None" = None
) -> None:
    """Write the per-game report, plus the generation results when that check ran.

    Kept as an object rather than a bare list once there is more than one kind of result, so a
    reader can tell a world that failed to install from one that installed and cannot generate.
    """
    games = [asdict(record) for record in sorted(records, key=lambda item: item.title.lower())]
    if generation is None:
        payload: Any = games
    else:
        payload = {
            "games": games,
            "generation": {
                "ok": generation.ok,
                "error": generation.error,
                "registered": generation.registered,
                "failures": [asdict(verdict) for verdict in generation.verdicts],
            },
        }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", path)


def log_summary(records: Sequence[GameRecord]) -> None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.outcome] = counts.get(record.outcome, 0) + 1

    logger.info("")
    tally = ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items()))
    logger.info("Summary: %s", tally or "nothing")

    removed = [record for record in records if record.removed]
    if removed:
        logger.info("")
        logger.info("Removed from the output directory, having become unacceptable:")
        for record in sorted(removed, key=lambda item: item.title.lower()):
            logger.info("  %-30s %s", record.title, record.removed)

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

    webhost = [
        record
        for record in records
        if record.succeeded and any("invalid for WebHost" in warning for warning in record.warnings)
    ]
    if webhost:
        logger.info("")
        logger.info("Installed but WebHost.py will drop these, so they will not appear on the site:")
        for record in sorted(webhost, key=lambda item: item.title.lower()):
            logger.info("  %s (%s)", record.title, record.file or record.asset_name)

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


def _read_world_folder(folder: Path) -> dict[str, str]:
    """The Python modules of a world installed as a folder, keyed the way the checks expect.

    Capped the same way the apworld reader is: a world that ships a huge generated data module has
    nothing to say about its registrations, and reading it would only slow the run down.
    """
    modules: dict[str, str] = {}
    budget = MAX_SOURCE_BYTES
    for source in sorted(folder.rglob("*.py")):
        dotted = module_path(source.relative_to(folder).as_posix())
        if dotted is None:
            continue
        try:
            size = source.stat().st_size
            if size > MAX_MODULE_BYTES or size > budget:
                continue
            modules[dotted] = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget -= size
    return modules


def extract_world(archive: Path, destination: Path) -> list[str]:
    """Unpack the ``<stem>/`` folder out of an apworld into ``destination``.

    Returns the paths left out, which is only ever the contents of subfolders of ``docs/`` - see
    :func:`_flattens_into_docs`.

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
    dropped: list[str] = []
    has_docs = False

    try:
        with zipfile.ZipFile(archive) as zip_file:
            for info in zip_file.infolist():
                if not info.filename.startswith(prefix):
                    continue
                relative = info.filename[len(prefix):]
                if not relative:
                    continue
                if relative.rstrip("/") == DOCS_FOLDER or relative.startswith(f"{DOCS_FOLDER}/"):
                    has_docs = True
                if _nested_in_docs(relative):
                    if not info.is_dir():
                        dropped.append(relative)
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

        # A world whose docs/ held nothing but subfolders would otherwise end up with no docs/ at
        # all, and a missing folder is the other way this same start-up step dies.
        if has_docs:
            (staging / DOCS_FOLDER).mkdir(parents=True, exist_ok=True)

        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return dropped


def _nested_in_docs(relative: str) -> bool:
    """Whether an archive member sits in a subfolder of ``docs/`` rather than directly in it.

    ``copy_tutorials_files_to_static()`` lists a folder world's ``docs/`` and ``shutil.copyfile``s
    every entry. A subfolder is an entry, and copying one raises::

        IsADirectoryError: [Errno 21] Is a directory: '/app/worlds/a1800/docs/images'

    which stops the WebHost starting, so these are left out rather than costing the whole site. It
    costs nothing that worked: the WebHost copies into one flat folder per game, so an image the
    markdown reaches as ``images/diagram.png`` was never going to be served from that path anyway -
    core's own zip branch flattens such files to their base name for the same reason.
    """
    parts = relative.split("/")
    return len(parts) > 2 and parts[0] == DOCS_FOLDER


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


def _read_lockfile(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the previously installed worlds and the previously rejected releases, both keyed."""
    if not path.is_file():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("ignoring unreadable lockfile %s: %s", path, error)
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    return _index(payload.get("worlds")), _index(payload.get("rejected"))


def _index(entries: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list):
        return {}
    return {
        _lock_key(str(entry.get("title")), str(entry.get("asset_name", ""))): entry
        for entry in entries
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
        "--webhost-check",
        choices=WEBHOST_POLICIES,
        default=WEBHOST_ERROR,
        help="what to do about worlds that load but that WebHost.py drops for having no "
        "web.tutorials: 'error' refuses them, 'warn' installs them anyway, 'off' skips the check",
    )
    parser.add_argument(
        "--ignore-game-mismatch",
        action="store_true",
        help="install an apworld even when its manifest names a different game than the wiki page; "
        "only useful if the game filter misfires on an unusually named world",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download and re-check everything, ignoring both the installed and rejected lists",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="remove worlds a previous run installed but this one did not",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and report without writing to --output")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="write the WebWorld paperwork a world is missing rather than refusing it: a generic "
        "tutorial block, a docs/setup_en.md to back it, and a WebWorld class where there is none. "
        "The repaired world is verified again and still refused if it does not pass",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="after installing, run Archipelago's own start-up checks in a subprocess and remove "
        "whatever they reject, including worlds that break the WebHost's option templates "
        "(this executes the downloaded third-party code)",
    )
    parser.add_argument(
        "--validate-python",
        default=sys.executable,
        help="interpreter to run --validate with; it needs Archipelago's own requirements "
        "installed (default: the one running this script)",
    )
    parser.add_argument(
        "--validate-generation",
        action="store_true",
        help="after installing, ask every world for a solo seed and report which cannot produce "
        "one. Reports only - nothing is removed, since one seed with default options is a smoke "
        "test rather than a verdict (this runs the worlds' whole generation path)",
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
        repair=args.repair,
        validate=args.validate,
        validate_generation=args.validate_generation,
        validate_python=args.validate_python,
        webhost_check=args.webhost_check,
        max_asset_bytes=args.max_asset_bytes,
    )

    options.target_python = detect_target_python(root)
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

    exit_code = 0
    if options.validate and not options.dry_run:
        passed, detail = crawler.validate_and_remove()
        if not passed:
            logger.error("validation could not run: %s", detail)
            exit_code = 1
        elif detail:
            logger.info(detail)

    if options.validate_generation and not options.dry_run:
        passed, detail = crawler.report_generation()
        if not passed:
            logger.error("generation check could not run: %s", detail)
            exit_code = 1

    log_summary(records)
    if args.report:
        write_report(args.report, records, generation=crawler.generation)

    if not options.dry_run:
        write_lockfile(
            options.lockfile,
            records,
            options,
            versions,
            previously_installed=crawler.previous,
            previously_rejected=crawler.rejected,
        )

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
