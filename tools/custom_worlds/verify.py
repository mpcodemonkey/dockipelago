"""Checking that a downloaded ``.apworld`` will actually load in this Archipelago checkout.

The checks mirror what ``worlds/__init__.py`` and ``worlds/Files.py`` do at import time, so a file
that passes here is one core will accept: a readable zip laid out the way ``zipimport`` expects,
carrying an ``archipelago.json`` whose declared version range covers our core version.

Nothing in this module imports Archipelago itself - core's ``worlds/Files.py`` pulls in third-party
dependencies, and the crawler needs to run before those are installed. The two numbers we need are
read straight out of the source instead, by :func:`detect_core_versions`.
"""

import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from .webworld import inspect_source

logger = logging.getLogger(__name__)

MANIFEST_NAME = "archipelago.json"

#: Fallbacks for when the source cannot be read; both are re-read from the checkout when possible.
FALLBACK_AP_VERSION = (0, 6, 8)
FALLBACK_CONTAINER_VERSION = 7

_VERSION_RE = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
_CONTAINER_VERSION_RE = re.compile(r"^container_version\s*:\s*int\s*=\s*(\d+)", re.MULTILINE)

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_INVALID = "invalid"

#: Statuses whose files are safe to keep in ``worlds/``.
INSTALLABLE_STATUSES = frozenset({STATUS_OK, STATUS_WARNING})

#: What to do about worlds that load but that WebHost.py would drop from the site.
WEBHOST_ERROR = "error"
WEBHOST_WARN = "warn"
WEBHOST_OFF = "off"
WEBHOST_POLICIES = (WEBHOST_ERROR, WEBHOST_WARN, WEBHOST_OFF)

#: A file's status is the worst thing found in it.
_SEVERITY = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_INCOMPATIBLE: 2, STATUS_INVALID: 3}


class CoreVersions(NamedTuple):
    """The two version numbers an apworld is checked against."""

    ap_version: tuple[int, int, int]
    container_version: int

    @property
    def ap_version_string(self) -> str:
        return ".".join(str(part) for part in self.ap_version)


@dataclass
class Manifest:
    """The contents of an apworld's ``archipelago.json``."""

    game: str | None = None
    world_version: tuple[int, ...] | None = None
    minimum_ap_version: tuple[int, ...] | None = None
    maximum_ap_version: tuple[int, ...] | None = None
    container_version: int | None = None
    compatible_version: int | None = None
    authors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    """The verdict on one ``.apworld`` file."""

    path: Path
    status: str = STATUS_OK
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: Manifest | None = None
    #: Machine-readable codes for the problems found, so a run can be diffed against a later one.
    codes: list[str] = field(default_factory=list)

    @property
    def module_name(self) -> str:
        return self.path.stem

    @property
    def installable(self) -> bool:
        return self.status in INSTALLABLE_STATUSES

    @property
    def game(self) -> str | None:
        return self.manifest.game if self.manifest else None

    def fail(self, status: str, message: str) -> None:
        self.errors.append(message)
        self._raise_status(status)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self._raise_status(STATUS_WARNING)

    def _raise_status(self, status: str) -> None:
        if _SEVERITY[status] > _SEVERITY[self.status]:
            self.status = status

    def summary(self) -> str:
        problems = self.errors + self.warnings
        return f"{self.status}: {'; '.join(problems)}" if problems else self.status


def detect_core_versions(root: Path) -> CoreVersions:
    """Read this checkout's Archipelago version and APContainer version out of the source."""
    ap_version = FALLBACK_AP_VERSION
    container_version = FALLBACK_CONTAINER_VERSION

    utils_path = root / "Utils.py"
    if utils_path.is_file():
        match = _VERSION_RE.search(utils_path.read_text(encoding="utf-8", errors="replace"))
        if match:
            parsed = parse_version(match.group(1))
            if parsed is not None and len(parsed) == 3:
                ap_version = (parsed[0], parsed[1], parsed[2])
        else:
            logger.warning("could not find __version__ in %s, assuming %s", utils_path, ap_version)

    files_path = root / "worlds" / "Files.py"
    if files_path.is_file():
        match = _CONTAINER_VERSION_RE.search(files_path.read_text(encoding="utf-8", errors="replace"))
        if match:
            container_version = int(match.group(1))
        else:
            logger.warning("could not find container_version in %s, assuming %d", files_path, container_version)

    return CoreVersions(ap_version, container_version)


def parse_version(value: str) -> tuple[int, ...] | None:
    """Parse a ``"major.minor.build"`` string the way ``Utils.tuplize_version`` does, or return None."""
    try:
        return tuple(int(piece) for piece in value.split("."))
    except (AttributeError, ValueError):
        return None


def verify_apworld(
    path: Path, versions: CoreVersions, *, webhost_check: str = WEBHOST_ERROR
) -> VerificationResult:
    """Check that ``path`` is an apworld this Archipelago checkout can load.

    ``webhost_check`` decides what to do about a world that loads but that ``WebHost.py`` would drop
    for having no ``web.tutorials``. Such a world still generates, so ``"warn"`` installs it anyway
    and ``"off"`` ignores the question entirely; the default rejects it, because a world the WebHost
    will not serve is dead weight in an image built to serve exactly that.
    """
    result = VerificationResult(path=path)

    if path.suffix != ".apworld":
        result.fail(STATUS_INVALID, f"file name does not end in .apworld: {path.name}")
        return result
    if path.name != path.name.lower():
        # Core imports these by module name; a frozen build raises on mixed case.
        result.warn(f"file name is not all lower case ({path.name}), which breaks frozen installs")

    try:
        with zipfile.ZipFile(path) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                result.fail(STATUS_INVALID, f"corrupt entry in archive: {corrupt}")
                return result
            _check_layout(archive, path, result)
            _check_webworld(archive, path, result, webhost_check=webhost_check)
            manifest_data = _read_manifest(archive, result)
    except zipfile.BadZipFile as error:
        result.fail(STATUS_INVALID, f"not a valid zip archive: {error}")
        return result
    except OSError as error:
        result.fail(STATUS_INVALID, f"could not read file: {error}")
        return result

    if manifest_data is not None:
        result.manifest = _parse_manifest(manifest_data, result)
        _check_versions(result, versions)

    return result


def _check_layout(archive: zipfile.ZipFile, path: Path, result: VerificationResult) -> None:
    """The zip must contain ``<stem>/__init__.py``: that is what ``zipimport`` looks for."""
    names = archive.namelist()
    expected = f"{path.stem}/__init__.py"
    if expected in names:
        return
    if f"{path.stem}/__init__.pyc" in names:
        result.warn("world ships only compiled bytecode (__init__.pyc), which is Python-version specific")
        return

    top_level = sorted({name.split("/", 1)[0] for name in names if "/" in name})
    if any(f"{folder}/__init__.py" in names for folder in top_level):
        wrong = ", ".join(folder for folder in top_level if f"{folder}/__init__.py" in names)
        result.fail(
            STATUS_INVALID,
            f"archive contains {wrong}/__init__.py but core will import it as '{path.stem}'; "
            "the inner folder must match the file name exactly",
        )
    else:
        result.fail(STATUS_INVALID, f"archive does not contain {expected}")


def _check_webworld(
    archive: zipfile.ZipFile,
    path: Path,
    result: VerificationResult,
    *,
    webhost_check: str,
) -> None:
    """Read the world's ``__init__.py`` and report broken WebWorld wiring.

    Only ``<stem>/__init__.py`` is looked at: a world that keeps its WebWorld in a separate module
    cannot be judged from here, and :mod:`.webworld` stays quiet rather than guessing.
    """
    entry = f"{path.stem}/__init__.py"
    try:
        source = archive.read(entry).decode("utf-8", errors="replace")
    except KeyError:
        return  # _check_layout has already reported this
    except (OSError, zipfile.BadZipFile) as error:
        result.fail(STATUS_INVALID, f"could not read {entry}: {error}")
        return

    for finding in inspect_source(source, module_name=path.stem):
        if finding.blocks_loading or webhost_check == WEBHOST_ERROR:
            result.codes.append(finding.code)
            result.fail(STATUS_INVALID, finding.detail)
        elif webhost_check == WEBHOST_WARN:
            result.codes.append(finding.code)
            result.warn(finding.detail)


def _read_manifest(archive: zipfile.ZipFile, result: VerificationResult) -> bytes | None:
    """Locate ``archipelago.json`` the same way ``APContainer.read_contents`` does."""
    names = archive.namelist()
    manifest_name = MANIFEST_NAME if MANIFEST_NAME in names else None
    if manifest_name is None:
        manifest_name = next((name for name in names if name.endswith(MANIFEST_NAME)), None)
    if manifest_name is None:
        result.warn(f"no {MANIFEST_NAME} manifest; this stops working in Archipelago 0.7.0")
        return None
    try:
        return archive.read(manifest_name)
    except (KeyError, OSError, zipfile.BadZipFile) as error:
        result.fail(STATUS_INVALID, f"could not read {manifest_name}: {error}")
        return None


def _parse_manifest(data: bytes, result: VerificationResult) -> Manifest | None:
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        result.fail(STATUS_INVALID, f"{MANIFEST_NAME} is not valid JSON: {error}")
        return None
    if not isinstance(raw, dict):
        result.fail(STATUS_INVALID, f"{MANIFEST_NAME} is not a JSON object")
        return None

    manifest = Manifest(raw=raw)

    game = raw.get("game")
    if isinstance(game, str) and game.strip():
        manifest.game = game
    else:
        result.warn(f"{MANIFEST_NAME} has no 'game' field, so core cannot tell what this world provides")

    for key in ("world_version", "minimum_ap_version", "maximum_ap_version"):
        value = raw.get(key)
        if value is None:
            continue
        parsed = parse_version(value) if isinstance(value, str) else None
        if parsed is None or len(parsed) != 3:
            result.fail(STATUS_INVALID, f"{key} is {value!r}, which core cannot parse as major.minor.build")
        else:
            setattr(manifest, key, parsed)

    for key, attribute in (("version", "container_version"), ("compatible_version", "compatible_version")):
        value = raw.get(key)
        if isinstance(value, int):
            setattr(manifest, attribute, value)
        elif value is not None:
            result.fail(STATUS_INVALID, f"{key} is {value!r}, expected an integer")

    authors = raw.get("authors")
    if isinstance(authors, list):
        manifest.authors = [str(author) for author in authors]

    return manifest


def _check_versions(result: VerificationResult, versions: CoreVersions) -> None:
    manifest = result.manifest
    if manifest is None:
        return

    if manifest.compatible_version is None:
        result.warn(
            "manifest has no 'compatible_version'; it was probably not built with the "
            "'Build APWorlds' launcher component"
        )
    elif manifest.compatible_version > versions.container_version:
        result.fail(
            STATUS_INCOMPATIBLE,
            f"packaged for APContainer version {manifest.compatible_version}, "
            f"but this checkout only handles {versions.container_version}",
        )

    if manifest.minimum_ap_version and tuple(manifest.minimum_ap_version) > versions.ap_version:
        result.fail(
            STATUS_INCOMPATIBLE,
            f"needs Archipelago >= {_version_string(manifest.minimum_ap_version)}, "
            f"this checkout is {versions.ap_version_string}",
        )

    if manifest.maximum_ap_version and tuple(manifest.maximum_ap_version) < versions.ap_version:
        result.fail(
            STATUS_INCOMPATIBLE,
            f"supports Archipelago <= {_version_string(manifest.maximum_ap_version)}, "
            f"this checkout is {versions.ap_version_string}",
        )


def _version_string(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def find_conflicts(results: list[VerificationResult], existing_worlds: set[str]) -> dict[Path, list[str]]:
    """Report clashes that only show up once several worlds are installed side by side.

    Core keys worlds by module name and by game name, and silently drops the loser in either case,
    so these are worth catching before a Docker image is built around them.
    """
    conflicts: dict[Path, list[str]] = {}

    def add(path: Path, message: str) -> None:
        conflicts.setdefault(path, []).append(message)

    by_module: dict[str, list[VerificationResult]] = {}
    by_game: dict[str, list[VerificationResult]] = {}
    for result in results:
        by_module.setdefault(result.module_name, []).append(result)
        if result.game:
            by_game.setdefault(result.game, []).append(result)

    for module_name, group in by_module.items():
        if module_name in existing_worlds:
            for result in group:
                add(result.path, f"'{module_name}' already exists in worlds/ and would shadow this file")
        if len(group) > 1:
            others = ", ".join(sorted(str(other.path.name) for other in group))
            for result in group:
                add(result.path, f"module name '{module_name}' is claimed by more than one file: {others}")

    for game, group in by_game.items():
        if len(group) > 1:
            others = ", ".join(sorted(str(other.path.name) for other in group))
            for result in group:
                add(result.path, f"game '{game}' is provided by more than one file: {others}; core loads only one")

    return conflicts
