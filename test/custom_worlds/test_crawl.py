"""End-to-end tests for the crawler, with the wiki and GitHub replaced by scripted responses."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

from test.custom_worlds.helpers import FakeHttp, default_manifest, make_apworld, release, wiki_page_html
from tools.custom_worlds.crawl import (
    INSTALL_ARCHIVE,
    INSTALL_EXTRACT,
    OUTCOME_FAILED,
    OUTCOME_INSTALLED,
    OUTCOME_KNOWN_BAD,
    OUTCOME_RESOLVED,
    OUTCOME_SKIPPED,
    OUTCOME_UNCHANGED,
    OUTCOME_UPDATED,
    Crawler,
    CrawlOptions,
    GameRecord,
    extract_world,
    write_lockfile,
)
from tools.custom_worlds.releases import GitHubClient
from tools.custom_worlds.rename import DEFAULT_MODULE_PREFIX, prefixed
from tools.custom_worlds.validate import (
    INVALID_FOR_WEBHOST,
    TEMPLATE_FAILED,
    ValidationReport,
    WorldVerdict,
)
from tools.custom_worlds.verify import CoreVersions
from tools.custom_worlds.wiki import WikiClient

VERSIONS = CoreVersions(ap_version=(0, 6, 8), container_version=7)

#: A world with nothing wrong with it, as the baseline every rejection fixture is derived from.
GOOD_WORLD = (
    "from worlds.AutoWorld import World, WebWorld, Tutorial\n"
    "class MyGameWeb(WebWorld):\n"
    '    setup = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n'
    "    tutorials = [setup]\n"
    'class MyGameWorld(World):\n    game = "Some Game Game"\n    web = MyGameWeb()\n'
)


class CrawlTestCase(unittest.TestCase):
    """Builds a throwaway Archipelago checkout and a wiki/GitHub pair that serve one game."""

    install_mode = INSTALL_EXTRACT
    module_prefix = DEFAULT_MODULE_PREFIX

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name) / "checkout"
        (self.root / "worlds").mkdir(parents=True)
        (self.root / "Utils.py").write_text('__version__ = "0.6.8"\n', encoding="utf-8")
        (self.root / "worlds" / "Files.py").write_text("container_version: int = 7\n", encoding="utf-8")
        self.staging = Path(self._temp.name) / "staging"
        self.staging.mkdir()
        self.assets = Path(self._temp.name) / "assets"
        self.assets.mkdir()

        self.http = FakeHttp()
        self.pages: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, list[dict[str, Any]]] = {}
        self.http.route("list=categorymembers", self._serve_category)
        self.http.route("action=parse", self._serve_page)
        # Asset downloads before the API route: both live under /releases/ on their own hosts.
        self.http.route("/releases/download/", self._serve_asset)
        self.http.route("/repos/", self._serve_releases)

    # -- fixture wiring ----------------------------------------------------------------

    def add_game(
        self,
        title: str,
        *,
        repo: str = "owner/repo",
        asset_name: str = "mygame.apworld",
        tag: str = "v1.0.0",
        module: str | None = None,
        manifest: dict[str, Any] | None = None,
        download_href: str | None = None,
        init_source: str = "# test world\n",
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        href = download_href if download_href is not None else f"https://github.com/{repo}"
        self.pages[title] = {
            "title": title,
            "wikitext": f"{{{{Infobox game\n| title = {title}\n| download = [{href} Download]\n}}}}",
            "text": wiki_page_html(title=title, download_href=href),
            "externallinks": [href],
        }
        self.releases.setdefault(repo, []).append(release(tag, asset_name, repo=repo))
        return make_apworld(
            self.assets / asset_name,
            module=module if module is not None else Path(asset_name).stem,
            manifest=default_manifest(f"{title} Game") if manifest is None else manifest,
            init_source=init_source,
            extra_files=extra_files,
        )

    def add_page(self, title: str, *, repo: str) -> None:
        """A second wiki page pointing at a repository that already has its release and asset."""
        href = f"https://github.com/{repo}"
        self.pages[title] = {
            "title": title,
            "wikitext": f"{{{{Infobox game\n| title = {title}\n| download = [{href} Download]\n}}}}",
            "text": wiki_page_html(title=title, download_href=href),
            "externallinks": [href],
        }

    def _serve_category(self, _url: str) -> dict[str, Any]:
        members = [{"ns": 0, "title": title} for title in self.pages]
        return {"query": {"categorymembers": members}}

    def _serve_page(self, url: str) -> dict[str, Any]:
        for title, payload in self.pages.items():
            if title.replace(" ", "+") in url or title.replace(" ", "%20") in url:
                return {"parse": payload}
        raise AssertionError(f"no fixture page for {url}")

    def _serve_releases(self, url: str) -> list[dict[str, Any]]:
        for repo, payload in self.releases.items():
            if f"/repos/{repo}/releases" in url:
                return payload
        return []

    def _serve_asset(self, url: str) -> bytes:
        # .../releases/download/<tag>/<name>. A "<tag>__<name>" fixture lets two releases serve
        # different bytes under the same asset name.
        *_, tag, name = url.split("/")
        for candidate in (self.assets / f"{tag}__{name}", self.assets / name):
            if candidate.is_file():
                return candidate.read_bytes()
        raise AssertionError(f"no fixture asset for {url}")

    # -- running -----------------------------------------------------------------------

    def options(self, **overrides: Any) -> CrawlOptions:
        settings: dict[str, Any] = {
            "root": self.root,
            "output_dir": self.root / "worlds",
            "lockfile": self.root / "custom_worlds.lock.json",
            "install_mode": self.install_mode,
            "module_prefix": self.module_prefix,
        }
        settings.update(overrides)
        return CrawlOptions(**settings)

    def crawl(
        self,
        options: CrawlOptions | None = None,
        *,
        write_lock: bool = True,
        versions: CoreVersions = VERSIONS,
    ) -> list[GameRecord]:
        options = options or self.options()
        crawler = Crawler(
            options,
            WikiClient(self.http),  # type: ignore[arg-type]
            GitHubClient(self.http, token=None),  # type: ignore[arg-type]
            versions,
        )
        records = crawler.run(self.staging)
        if write_lock and not options.dry_run:
            write_lockfile(
                options.lockfile,
                records,
                options,
                versions,
                previously_installed=crawler.previous,
                previously_rejected=crawler.rejected,
            )
        return records

    def record_for(self, records: list[GameRecord], title: str) -> GameRecord:
        return next(record for record in records if record.title == title)

    def world_path(self, stem: str, *, output: str = "worlds") -> Path:
        """Where a world with this module name ends up, under the install mode and module prefix."""
        name = prefixed(stem, self.module_prefix)
        if self.install_mode == INSTALL_ARCHIVE:
            return self.root / output / f"{name}.apworld"
        return self.root / output / name

    def assert_installed(self, stem: str, *, output: str = "worlds") -> None:
        path = self.world_path(stem, output=output)
        self.assertTrue(path.exists(), f"{path} was not installed")
        if self.install_mode == INSTALL_EXTRACT:
            self.assertTrue((path / "__init__.py").is_file(), f"{path} has no __init__.py")

    def assert_not_installed(self, stem: str) -> None:
        self.assertFalse(self.world_path(stem).exists(), f"{self.world_path(stem)} should not exist")

    def relative(self, stem: str) -> str:
        return str(self.world_path(stem).relative_to(self.root))

    @property
    def lock(self) -> dict[str, Any]:
        return json.loads((self.root / "custom_worlds.lock.json").read_text(encoding="utf-8"))


class TestHappyPath(CrawlTestCase):
    def test_installs_the_apworld_into_worlds(self) -> None:
        self.add_game("Some Game")
        records = self.crawl()

        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assert_installed("mygame")
        self.assertEqual(self.relative("mygame"), record.file)
        self.assertEqual("Some Game Game", record.game)
        self.assertEqual("owner/repo", record.repo)
        self.assertEqual("v1.0.0", record.release_tag)
        self.assertEqual(64, len(record.sha256))

    def test_installs_several_games(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.add_game("Game B", repo="b/b", asset_name="game_b.apworld")
        records = self.crawl()
        self.assertEqual(2, len(records))
        self.assertTrue(all(record.outcome == OUTCOME_INSTALLED for record in records))
        self.assert_installed("game_a")
        self.assert_installed("game_b")

    def test_writes_a_lockfile_describing_what_was_installed(self) -> None:
        self.add_game("Some Game")
        self.crawl()

        lock = self.lock
        self.assertEqual("0.6.8", lock["archipelago_version"])
        self.assertEqual(7, lock["container_version"])
        self.assertEqual("worlds", lock["output_dir"])
        self.assertEqual(1, len(lock["worlds"]))
        entry = lock["worlds"][0]
        self.assertEqual("Some Game", entry["title"])
        self.assertEqual(self.relative("mygame"), entry["file"])
        self.assertEqual(self.install_mode, entry["install_mode"])
        self.assertEqual("v1.0.0", entry["release_tag"])
        self.assertEqual("1.0.0", entry["world_version"])

    def test_lockfile_entries_are_sorted_for_stable_diffs(self) -> None:
        self.add_game("Zebra Game", repo="z/z", asset_name="zebra.apworld")
        self.add_game("Alpha Game", repo="a/a", asset_name="alpha.apworld")
        self.crawl()
        self.assertEqual(["Alpha Game", "Zebra Game"], [entry["title"] for entry in self.lock["worlds"]])

    def test_extraction_reproduces_the_archive_contents(self) -> None:
        import zipfile

        source = self.add_game("Some Game")
        self.crawl()
        with zipfile.ZipFile(source) as archive:
            expected = archive.read("mygame/__init__.py")
        self.assertEqual(expected, (self.world_path("mygame") / "__init__.py").read_bytes())
        self.assertTrue((self.world_path("mygame") / "archipelago.json").is_file())

    def test_extraction_leaves_no_temporary_directory_behind(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        leftovers = [entry.name for entry in (self.root / "worlds").iterdir() if entry.name.startswith(".")]
        self.assertEqual([], leftovers)


class TestIncrementalRuns(CrawlTestCase):
    def test_a_second_run_downloads_nothing_new(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        before = len(self.http.requests)

        records = self.crawl()
        self.assertEqual(OUTCOME_UNCHANGED, self.record_for(records, "Some Game").outcome)
        # The page and the release list are still checked; the asset itself is not re-fetched.
        self.assertNotIn(
            "https://github.com/owner/repo/releases/download/v1.0.0/mygame.apworld",
            self.http.requests[before:],
        )

    def test_a_new_release_is_picked_up(self) -> None:
        self.add_game("Some Game")
        self.crawl()

        self.releases["owner/repo"].append(release("v2.0.0", "mygame.apworld", published_at="2026-06-01T00:00:00Z"))
        make_apworld(
            self.assets / "mygame.apworld",
            module="mygame",
            manifest=default_manifest("Some Game Game", world_version="2.0.0"),
        )
        records = self.crawl()

        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_UPDATED, record.outcome)
        self.assertEqual("v2.0.0", record.release_tag)
        self.assertEqual("2.0.0", self.lock["worlds"][0]["world_version"])

    def test_refresh_forces_a_re_download(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        records = self.crawl(self.options(refresh=True))
        self.assertEqual(OUTCOME_UPDATED, self.record_for(records, "Some Game").outcome)

    def test_a_deleted_world_is_restored(self) -> None:
        import shutil

        self.add_game("Some Game")
        self.crawl()
        target = self.world_path("mygame")
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        self.crawl()
        self.assert_installed("mygame")

    def test_switching_install_mode_reinstalls(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        other = INSTALL_ARCHIVE if self.install_mode == INSTALL_EXTRACT else INSTALL_EXTRACT
        records = self.crawl(self.options(install_mode=other))
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)

    def test_prune_removes_worlds_that_left_the_category(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.add_game("Game B", repo="b/b", asset_name="game_b.apworld")
        self.crawl()

        del self.pages["Game B"]
        self.crawl(self.options(prune=True))

        self.assert_installed("game_a")
        self.assertFalse(self.world_path("game_b").exists())

    def test_prune_never_touches_bundled_worlds(self) -> None:
        bundled = self.root / "worlds" / "ror2"
        bundled.mkdir()
        (bundled / "__init__.py").write_text("", encoding="utf-8")
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.crawl(self.options(prune=True))
        self.assertTrue((bundled / "__init__.py").is_file())


class TestFailures(CrawlTestCase):
    def test_a_page_without_a_download_link_is_reported(self) -> None:
        self.pages["Linkless Game"] = {
            "title": "Linkless Game",
            "wikitext": "{{Infobox game| title = Linkless Game }}",
            "text": "<p>Nothing to see here.</p>",
            "externallinks": [],
        }
        records = self.crawl()
        record = self.record_for(records, "Linkless Game")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("no download link", record.reason)

    def test_a_repository_without_an_apworld_is_reported(self) -> None:
        self.add_game("Some Game")
        self.releases["owner/repo"] = [release("v1.0.0", "setup.exe")]
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("no .apworld asset", record.reason)

    def test_an_incompatible_apworld_is_not_installed(self) -> None:
        self.add_game("Some Game", manifest=default_manifest("Some Game", minimum_ap_version="0.9.0"))
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("needs Archipelago >= 0.9.0", record.reason)
        self.assert_not_installed("mygame")

    def test_a_broken_archive_is_not_installed(self) -> None:
        self.add_game("Some Game")
        (self.assets / "mygame.apworld").write_bytes(b"not a zip")
        records = self.crawl()
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Some Game").outcome)
        self.assert_not_installed("mygame")

    def test_one_failure_does_not_stop_the_rest(self) -> None:
        self.add_game("Good Game", repo="a/a", asset_name="good.apworld")
        self.add_game("Bad Game", repo="b/b", asset_name="bad.apworld")
        (self.assets / "bad.apworld").write_bytes(b"not a zip")
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Good Game").outcome)
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Bad Game").outcome)
        self.assertEqual(1, len(self.lock["worlds"]))

    def test_a_world_that_would_shadow_a_bundled_one_is_skipped(self) -> None:
        # The name that has to be free is the one the world is installed under, prefix included.
        bundled = self.world_path("mygame")
        bundled.mkdir()
        (bundled / "__init__.py").write_text("", encoding="utf-8")
        self.add_game("Some Game")
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("already exists in worlds/", record.reason)
        self.assertFalse((bundled / "archipelago.json").exists())

    def test_absolute_self_imports_are_repointed_at_the_new_name(self) -> None:
        """A world that spells its own package out has to keep working once the folder moves.

        11 of the 82 worlds bundled with Archipelago write imports this way, so it is normal source
        rather than an oddity, and every such line is a ModuleNotFoundError after a rename.
        """
        self.add_game(
            "Some Game",
            init_source="from worlds.mygame.Rules import RULE\n" + GOOD_WORLD,
            extra_files={
                "mygame/Rules.py": "from worlds.mygame.Items import X\nRULE = X\n",
                "mygame/Items.py": 'X = "x"\n',
            },
        )
        self.crawl()

        installed = self.world_path("mygame")
        self.assertEqual(
            "from worlds.cw_mygame.Rules import RULE",
            (installed / "__init__.py").read_text(encoding="utf-8").splitlines()[0],
        )
        self.assertEqual(
            "from worlds.cw_mygame.Items import X",
            (installed / "Rules.py").read_text(encoding="utf-8").splitlines()[0],
        )

    def test_a_module_path_written_as_a_string_is_flagged(self) -> None:
        # Rewriting text would corrupt prose that matches, so these are reported instead.
        self.add_game("Some Game", init_source='DATA = "worlds/mygame/data"\n' + GOOD_WORLD)
        record = self.record_for(self.crawl(), "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome)
        self.assertTrue(
            any("names a module path as a string" in warning for warning in record.warnings),
            record.warnings,
        )

    def test_the_prefix_keeps_a_world_out_of_a_bundled_name(self) -> None:
        # A custom world named like a core one is exactly what the prefix is for: unprefixed this
        # shadows core's mygame, prefixed it lands beside it and both load.
        bundled = self.root / "worlds" / "mygame"
        bundled.mkdir()
        (bundled / "__init__.py").write_text("", encoding="utf-8")
        self.add_game("Some Game")
        record = self.record_for(self.crawl(), "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome)
        self.assertEqual("worlds/cw_mygame", record.file)
        self.assertTrue((bundled / "__init__.py").is_file(), "core's own world is untouched")

    def test_two_pages_shipping_the_same_game_are_both_skipped(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="a.apworld", manifest=default_manifest("Shared Game"))
        self.add_game("Game B", repo="b/b", asset_name="b.apworld", manifest=default_manifest("Shared Game"))
        records = self.crawl()
        for record in records:
            self.assertEqual(OUTCOME_SKIPPED, record.outcome)
            self.assertIn("provided by more than one file", record.reason)
        self.assertEqual([], self.lock["worlds"])

    def test_two_pages_resolving_to_one_file_record_it_once(self) -> None:
        """A repo shipping one world that two pages point at, as Wargroove 2 / Votipelago did.

        Both pages resolve the same asset. Only one world can be installed, and the lockfile has to
        say so: the page that loses used to be written as installed, pointing at a path that was
        never created.
        """
        self.add_game("Wargroove 2", repo="fly/ap", asset_name="wargroove2.apworld",
                      manifest=default_manifest("Wargroove 2"))
        self.add_page("Votipelago", repo="fly/ap")
        records = self.crawl()

        self.assert_installed("wargroove2")
        winner = self.record_for(records, "Wargroove 2")
        loser = self.record_for(records, "Votipelago")
        self.assertEqual(OUTCOME_SKIPPED, loser.outcome)
        self.assertIn("same world 'Wargroove 2'", loser.reason)
        self.assertEqual("", loser.file)

        titles = [entry["title"] for entry in self.lock["worlds"]]
        self.assertEqual(["Wargroove 2"], titles)
        self.assertEqual(self.relative("wargroove2"), winner.file)

    def test_the_page_the_manifest_names_keeps_the_world(self) -> None:
        # Whichever order the pages come in, the world belongs to the page its manifest agrees with.
        self.add_game("Votipelago", repo="fly/ap", asset_name="wargroove2.apworld",
                      manifest=default_manifest("Wargroove 2"))
        self.add_page("Wargroove 2", repo="fly/ap")
        records = self.crawl()

        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Votipelago").outcome)
        self.assertEqual([entry["title"] for entry in self.lock["worlds"]], ["Wargroove 2"])

    def test_a_world_with_tutorials_but_no_docs_folder_is_refused(self) -> None:
        # WebHost.py os.listdir()s each world's docs/ at start-up, so a missing one is fatal to the
        # whole site rather than to the one game.
        self.add_game("Duck Life 4", asset_name="ducklife4.apworld", init_source=GOOD_WORLD)
        make_apworld(
            self.assets / "ducklife4.apworld",
            module="ducklife4",
            manifest=default_manifest("Duck Life 4 Game"),
            init_source=GOOD_WORLD,
            docs=False,
        )
        record = self.record_for(self.crawl(), "Duck Life 4")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("ships no 'docs/' folder", record.reason)
        self.assert_not_installed("ducklife4")

    def test_a_world_claiming_a_core_patch_extension_is_skipped(self) -> None:
        """What took out core's own Gauntlet Legends: a custom world claiming '.apgl'.

        AutoPatchRegister keys extensions globally, so the second class to claim one raises and its
        world fails to import - and worlds/ loads alphabetically, so the loser can be core's.
        """
        (self.root / "worlds" / "gl").mkdir(parents=True)
        (self.root / "worlds" / "gl" / "__init__.py").write_text(
            'from worlds.AutoWorld import World\nclass GLWorld(World):\n    game = "Gauntlet Legends"\n',
            encoding="utf-8",
        )
        (self.root / "worlds" / "gl" / "Rom.py").write_text(
            'class GLPatch:\n    game = "Gauntlet Legends"\n    patch_file_ending = ".apgl"\n',
            encoding="utf-8",
        )
        self.add_game(
            "Gauntlet Legends AP",
            asset_name="gauntlet_legends.apworld",
            manifest=default_manifest("Gauntlet Legends Custom"),
            init_source="from .Rom import GLPatch\n" + GOOD_WORLD.replace("My Game", "Gauntlet Legends Custom"),
            extra_files={
                "gauntlet_legends/Rom.py":
                    'class GLPatch:\n    game = "Gauntlet Legends Custom"\n    patch_file_ending = ".apgl"\n',
            },
        )
        records = self.crawl()

        record = self.record_for(records, "Gauntlet Legends AP")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("'.apgl' is already registered by worlds/gl", record.reason)
        self.assert_not_installed("gauntlet_legends")

    def test_a_world_claiming_a_core_game_name_is_skipped(self) -> None:
        (self.root / "worlds" / "gl").mkdir(parents=True)
        (self.root / "worlds" / "gl" / "__init__.py").write_text(
            'from worlds.AutoWorld import World\nclass GLWorld(World):\n    game = "Gauntlet Legends"\n',
            encoding="utf-8",
        )
        self.add_game("Gauntlet Legends", asset_name="glap.apworld",
                      manifest=default_manifest("Gauntlet Legends"))
        record = self.record_for(self.crawl(), "Gauntlet Legends")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("already provided by worlds/gl", record.reason)

    def test_changing_the_prefix_moves_the_world_and_removes_the_old_copy(self) -> None:
        """The migration every existing install goes through the first time a prefix is set.

        Leaving the old folder behind would be worse than not renaming at all: both copies load, and
        the unprefixed one still occupies the name the prefix exists to vacate.
        """
        self.add_game("Some Game")
        self.crawl(self.options(module_prefix=""))
        self.assertTrue((self.root / "worlds" / "mygame" / "__init__.py").is_file())

        records = self.crawl(self.options(module_prefix="cw_"))
        record = self.record_for(records, "Some Game")
        self.assertEqual("worlds/cw_mygame", record.file)
        self.assertEqual("worlds/mygame", record.removed)
        self.assertTrue((self.root / "worlds" / "cw_mygame" / "__init__.py").is_file())
        self.assertFalse((self.root / "worlds" / "mygame").exists(), "the old copy must not linger")
        self.assertEqual(["worlds/cw_mygame"], [entry["file"] for entry in self.lock["worlds"]])

    def test_the_prefix_is_fingerprinted_so_a_rerun_does_not_reuse_the_old_path(self) -> None:
        self.add_game("Some Game")
        self.crawl(self.options(module_prefix=""))
        # Same prefix again is genuinely unchanged; a different one has to re-install.
        again = self.record_for(self.crawl(self.options(module_prefix="")), "Some Game")
        self.assertEqual(OUTCOME_UNCHANGED, again.outcome)
        moved = self.record_for(self.crawl(self.options(module_prefix="cw_")), "Some Game")
        self.assertIn(moved.outcome, (OUTCOME_INSTALLED, OUTCOME_UPDATED))

    def test_a_wiki_page_that_fails_to_load_is_reported(self) -> None:
        self.pages["Broken Page"] = {}
        self.http.routes.insert(0, ("Broken+Page", self._raise_page_error))
        records = self.crawl()
        record = self.record_for(records, "Broken Page")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("could not read the wiki page", record.reason)

    @staticmethod
    def _raise_page_error(url: str) -> bytes:
        from tools.custom_worlds.http import HttpError

        raise HttpError(url, 500, "Internal Server Error")


class TestOptions(CrawlTestCase):
    def test_dry_run_resolves_without_writing(self) -> None:
        self.add_game("Some Game")
        records = self.crawl(self.options(dry_run=True))
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_RESOLVED, record.outcome)
        self.assertEqual("Some Game Game", record.game)
        self.assert_not_installed("mygame")
        self.assertFalse((self.root / "custom_worlds.lock.json").exists())

    def test_limit_stops_after_n_pages(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.add_game("Game B", repo="b/b", asset_name="game_b.apworld")
        self.assertEqual(1, len(self.crawl(self.options(limit=1))))

    def test_only_bypasses_the_category_listing(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.add_game("Game B", repo="b/b", asset_name="game_b.apworld")
        records = self.crawl(self.options(only=("Game B",)))
        self.assertEqual(["Game B"], [record.title for record in records])
        self.assertFalse(any("categorymembers" in request for request in self.http.requests))

    def test_output_can_be_redirected_elsewhere(self) -> None:
        self.add_game("Some Game")
        self.crawl(self.options(output_dir=self.root / "custom_worlds"))
        self.assert_installed("mygame", output="custom_worlds")
        self.assert_not_installed("mygame")

    def test_an_oversized_asset_is_refused(self) -> None:
        self.add_game("Some Game")
        records = self.crawl(self.options(max_asset_bytes=10))
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("download failed", record.reason)

    def test_a_low_confidence_link_is_flagged(self) -> None:
        self.pages["Guessy Game"] = {
            "title": "Guessy Game",
            "wikitext": "Some prose with no infobox.",
            "text": "<p>Some prose.</p>",
            "externallinks": ["https://github.com/owner/repo"],
        }
        self.releases["owner/repo"] = [release("v1.0.0", "mygame.apworld")]
        make_apworld(self.assets / "mygame.apworld", module="mygame", manifest=default_manifest("Guessy Game"))
        records = self.crawl()
        record = self.record_for(records, "Guessy Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome)
        self.assertEqual("low", record.confidence)
        self.assertTrue(any("guessed" in warning for warning in record.warnings))


class TestMultipleAssets(CrawlTestCase):
    """A single release that ships more than one world for the same page."""

    def setUp(self) -> None:
        super().setUp()
        href = "https://github.com/owner/bundle_game"
        self.pages["Bundle Game"] = {
            "title": "Bundle Game",
            "wikitext": f"{{{{Infobox game| game = Bundle Game | download = [{href} Download] }}}}",
            "text": wiki_page_html(title="Bundle Game", download_href=href),
            "externallinks": [href],
        }
        self.releases["owner/bundle_game"] = [
            release("v1.0.0", "bundle_game.apworld", "bundle_game_extras.apworld", repo="owner/bundle_game")
        ]
        make_apworld(
            self.assets / "bundle_game.apworld", module="bundle_game", manifest=default_manifest("Bundle Game")
        )
        make_apworld(
            self.assets / "bundle_game_extras.apworld",
            module="bundle_game_extras",
            manifest=default_manifest("Bundle Game Extras"),
        )

    def test_only_the_best_match_is_installed_by_default(self) -> None:
        records = self.crawl()
        self.assertEqual(1, len(records))
        self.assert_installed("bundle_game")
        self.assert_not_installed("bundle_game_extras")
        self.assertEqual(1, len(self.lock["worlds"]))

    def test_all_assets_installs_every_world_in_the_release(self) -> None:
        records = self.crawl(self.options(all_assets=True))
        self.assertEqual(2, len(records))
        self.assertTrue(all(record.outcome == OUTCOME_INSTALLED for record in records), records)
        self.assert_installed("bundle_game")
        self.assert_installed("bundle_game_extras")

    def test_each_asset_gets_its_own_lockfile_entry(self) -> None:
        self.crawl(self.options(all_assets=True))
        entries = self.lock["worlds"]
        self.assertEqual(["Bundle Game", "Bundle Game"], [entry["title"] for entry in entries])
        self.assertEqual(
            {"bundle_game.apworld", "bundle_game_extras.apworld"}, {entry["asset_name"] for entry in entries}
        )

    def test_a_second_run_leaves_both_alone(self) -> None:
        self.crawl(self.options(all_assets=True))
        records = self.crawl(self.options(all_assets=True))
        self.assertEqual([OUTCOME_UNCHANGED, OUTCOME_UNCHANGED], [record.outcome for record in records])

    def test_extra_assets_inherit_the_pages_link_provenance(self) -> None:
        records = self.crawl(self.options(all_assets=True, dry_run=True))
        self.assertEqual({"infobox-param"}, {record.strategy for record in records})
        self.assertEqual({"https://github.com/owner/bundle_game"}, {record.download_url for record in records})


class TestSharedRepository(CrawlTestCase):
    """One maintainer publishing several unrelated games out of a single repository.

    Modelled on the real case: an ActRaiser page pointing at a repository whose release feed also
    carries Sonic Battle and Rune Factory, with those two released more recently.
    """

    REPO = "maintainer/apworlds"

    def setUp(self) -> None:
        super().setUp()
        self.releases[self.REPO] = [
            release("runefactory-2.0.0", "runefactory.apworld", published_at="2026-06-01T00:00:00Z", repo=self.REPO),
            release("sonicbattle-1.4.0", "sonic_battle.apworld", published_at="2026-05-01T00:00:00Z", repo=self.REPO),
            release("actraiser-1.1.0", "actraiser.apworld", published_at="2026-01-01T00:00:00Z", repo=self.REPO),
        ]
        for stem, game in (
            ("runefactory", "Rune Factory"),
            ("sonic_battle", "Sonic Battle"),
            ("actraiser", "ActRaiser"),
        ):
            make_apworld(self.assets / f"{stem}.apworld", module=stem, manifest=default_manifest(game))

    def add_page(self, title: str) -> None:
        href = f"https://github.com/{self.REPO}"
        self.pages[title] = {
            "title": title,
            "wikitext": f"{{{{Infobox game| game = {title} | download = [{href} Download] }}}}",
            "text": wiki_page_html(title=title, download_href=href),
            "externallinks": [href],
        }

    def test_picks_the_game_the_page_is_about_not_the_newest_release(self) -> None:
        self.add_page("ActRaiser")
        records = self.crawl()
        record = self.record_for(records, "ActRaiser")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertEqual("actraiser.apworld", record.asset_name)
        self.assertEqual("actraiser-1.1.0", record.release_tag)
        self.assertEqual("ActRaiser", record.game)
        self.assert_installed("actraiser")
        self.assert_not_installed("runefactory")
        self.assert_not_installed("sonic_battle")

    def test_each_page_gets_its_own_game(self) -> None:
        for title in ("ActRaiser", "Sonic Battle", "Rune Factory"):
            self.add_page(title)
        records = self.crawl()
        installed = {record.title: record.game for record in records}
        self.assertEqual({"ActRaiser": "ActRaiser", "Sonic Battle": "Sonic Battle", "Rune Factory": "Rune Factory"},
                         installed)

    def test_the_lockfile_flags_worlds_from_a_shared_repository(self) -> None:
        self.add_page("ActRaiser")
        self.crawl()
        self.assertTrue(self.lock["worlds"][0]["shared_repo"])

    def test_the_note_explains_the_choice(self) -> None:
        self.add_page("Sonic Battle")
        records = self.crawl()
        record = self.record_for(records, "Sonic Battle")
        self.assertTrue(
            any("publishes apworlds for 3 games" in note for note in record.notes), record.notes
        )

    def test_a_game_the_repository_does_not_publish_is_refused(self) -> None:
        self.add_page("Chrono Trigger")
        records = self.crawl()
        record = self.record_for(records, "Chrono Trigger")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("none of them matches", record.reason)
        self.assertEqual([], self.lock["worlds"])

    def test_nothing_is_installed_when_no_game_matches(self) -> None:
        self.add_page("Chrono Trigger")
        self.crawl()
        for stem in ("actraiser", "runefactory", "sonic_battle"):
            self.assert_not_installed(stem)

    def add_mislabelled_release(self) -> None:
        """A newer release whose asset is named actraiser.apworld but contains Rune Factory.

        The file name alone cannot distinguish it from the genuine article, so only the manifest
        inside the downloaded file gives the game away.
        """
        self.releases[self.REPO].insert(
            0,
            release("bad-3.0.0", "actraiser.apworld", published_at="2026-07-01T00:00:00Z", repo=self.REPO),
        )
        make_apworld(
            self.assets / "bad-3.0.0__actraiser.apworld",
            module="actraiser",
            manifest=default_manifest("Rune Factory"),
        )

    def test_a_misleading_file_name_is_caught_by_the_manifest(self) -> None:
        self.add_page("ActRaiser")
        self.add_mislabelled_release()
        records = self.crawl()
        record = self.record_for(records, "ActRaiser")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertEqual("actraiser-1.1.0", record.release_tag)
        self.assertEqual("ActRaiser", record.game)
        self.assertTrue(any("rejected" in note for note in record.notes), record.notes)

    def test_giving_up_after_exhausting_the_alternates_is_reported(self) -> None:
        self.add_page("ActRaiser")
        self.add_mislabelled_release()
        # Now the genuine release lies about its game too, so nothing in the repository qualifies.
        make_apworld(
            self.assets / "actraiser.apworld", module="actraiser", manifest=default_manifest("Sonic Battle")
        )
        records = self.crawl()
        record = self.record_for(records, "ActRaiser")
        self.assertEqual(OUTCOME_FAILED, record.outcome)
        self.assertIn("none of them declares", record.reason)
        self.assert_not_installed("actraiser")

    def test_ignore_game_mismatch_takes_the_name_based_pick(self) -> None:
        self.add_page("ActRaiser")
        self.add_mislabelled_release()
        records = self.crawl(self.options(ignore_game_mismatch=True))
        record = self.record_for(records, "ActRaiser")
        self.assertEqual("bad-3.0.0", record.release_tag)
        self.assertEqual("Rune Factory", record.game)

    def test_a_single_game_repository_is_not_second_guessed(self) -> None:
        # Only one world here, so a name that does not match the page is naming drift, not a mixup.
        self.releases["solo/repo"] = [release("v1", "thegame.apworld", repo="solo/repo")]
        make_apworld(self.assets / "thegame.apworld", module="thegame", manifest=default_manifest("Some Old Game"))
        href = "https://github.com/solo/repo"
        self.pages["Totally Different Title"] = {
            "title": "Totally Different Title",
            "wikitext": f"{{{{Infobox game| download = [{href} Download] }}}}",
            "text": wiki_page_html(title="Totally Different Title", download_href=href),
            "externallinks": [href],
        }
        records = self.crawl()
        record = self.record_for(records, "Totally Different Title")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertTrue(any("declares game" in warning for warning in record.warnings), record.warnings)


class TestWebWorldRejection(CrawlTestCase):
    """Worlds Archipelago cannot load, or the WebHost will not serve, never reach worlds/."""

    GOOD = GOOD_WORLD
    NOT_INSTANTIATED = GOOD.replace("web = MyGameWeb()", "web = MyGameWeb")
    NO_TUTORIALS = GOOD.replace(
        '    setup = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n    tutorials = [setup]\n',
        '    theme = "grass"\n',
    )
    COMMENTED_TUTORIALS = GOOD.replace(
        '    setup = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n    tutorials = [setup]\n',
        '    # setup = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n'
        "    # tutorials = [setup]\n"
        '    theme = "grass"\n',
    )
    NO_WEB = "from worlds.AutoWorld import World\n" + 'class MyGameWorld(World):\n    game = "Some Game Game"\n'

    def test_a_correctly_wired_world_installs(self) -> None:
        self.add_game("Some Game", init_source=self.GOOD)
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)
        self.assert_installed("mygame")

    def test_an_uninstantiated_webworld_is_not_installed(self) -> None:
        self.add_game("Some Game", init_source=self.NOT_INSTANTIATED)
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("has to be instantiated", record.reason)
        self.assert_not_installed("mygame")
        self.assertEqual([], self.lock["worlds"])

    def test_a_webworld_without_tutorials_is_not_installed(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("invalid for WebHost", record.reason)
        self.assert_not_installed("mygame")

    def test_a_commented_out_tutorial_block_is_not_installed(self) -> None:
        self.add_game("Some Game", init_source=self.COMMENTED_TUTORIALS)
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        # Reached via the tutorials check, not by the file failing to parse.
        self.assertIn("defines no 'tutorials'", record.reason)
        self.assert_not_installed("mygame")

    def test_a_world_with_no_web_is_not_installed(self) -> None:
        self.add_game("Some Game", init_source=self.NO_WEB)
        records = self.crawl()
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Some Game").outcome)
        self.assert_not_installed("mygame")

    def test_warn_installs_it_with_the_reason_recorded(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        records = self.crawl(self.options(webhost_check="warn"))
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertTrue(any("invalid for WebHost" in warning for warning in record.warnings), record.warnings)
        self.assertEqual("warning", record.verification)
        self.assert_installed("mygame")
        self.assertTrue(any("invalid for WebHost" in w for w in self.lock["worlds"][0]["warnings"]))

    def test_off_installs_it_silently(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        records = self.crawl(self.options(webhost_check="off"))
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome)
        self.assertEqual([], record.warnings)

    def test_no_policy_rescues_a_world_that_cannot_load(self) -> None:
        self.add_game("Some Game", init_source=self.NOT_INSTANTIATED)
        for policy in ("error", "warn", "off"):
            records = self.crawl(self.options(webhost_check=policy, refresh=True))
            self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Some Game").outcome, policy)

    def test_one_broken_world_does_not_stop_the_others(self) -> None:
        self.add_game("Good Game", repo="a/a", asset_name="good.apworld", init_source=self.GOOD)
        self.add_game("Bad Game", repo="b/b", asset_name="bad.apworld", init_source=self.NOT_INSTANTIATED)
        self.add_game("Webless Game", repo="c/c", asset_name="webless.apworld", init_source=self.NO_TUTORIALS)
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Good Game").outcome)
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Bad Game").outcome)
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Webless Game").outcome)
        self.assert_installed("good")
        self.assert_not_installed("bad")
        self.assert_not_installed("webless")


class TestRejectionMemory(CrawlTestCase):
    """A release rejected once is remembered, so it is never downloaded a second time."""

    GOOD = TestWebWorldRejection.GOOD
    NO_TUTORIALS = TestWebWorldRejection.NO_TUTORIALS

    def asset_downloads(self) -> list[str]:
        return [request for request in self.http.requests if "/releases/download/" in request]

    def rejected(self) -> list[dict[str, Any]]:
        return self.lock["rejected"]

    def test_a_rejection_is_written_to_the_lockfile(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()

        entries = self.rejected()
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("Some Game", entry["title"])
        self.assertEqual("v1.0.0", entry["release_tag"])
        self.assertEqual("mygame.apworld", entry["asset_name"])
        self.assertEqual("invalid", entry["verification"])
        self.assertEqual(["web-no-tutorials"], entry["codes"])
        self.assertIn("invalid for WebHost", entry["reason"])
        self.assertEqual(64, len(entry["sha256"]))
        self.assertTrue(entry["first_rejected"])

    def test_a_second_run_does_not_download_it_again(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        self.assertEqual(1, len(self.asset_downloads()))

        self.http.requests.clear()
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_KNOWN_BAD, record.outcome)
        self.assertIn("invalid for WebHost", record.reason)
        self.assertEqual([], self.asset_downloads())
        self.assert_not_installed("mygame")

    def test_the_rejection_survives_into_the_next_lockfile(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        first = self.rejected()[0]["first_rejected"]
        self.crawl()
        entry = self.rejected()[0]
        self.assertEqual(first, entry["first_rejected"], "the original date should be kept")
        self.assertEqual(["web-no-tutorials"], entry["codes"])

    def test_a_new_release_is_tried_again(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()

        # The maintainer ships a fixed release; the memo is for the old tag only.
        self.releases["owner/repo"].append(
            release("v2.0.0", "mygame.apworld", published_at="2026-06-01T00:00:00Z")
        )
        make_apworld(
            self.assets / "v2.0.0__mygame.apworld",
            module="mygame",
            manifest=default_manifest("Some Game Game"),
            init_source=self.GOOD,
        )
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assert_installed("mygame")
        self.assertEqual([], self.rejected())

    def test_refresh_re_checks_a_remembered_rejection(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        self.http.requests.clear()
        records = self.crawl(self.options(refresh=True))
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Some Game").outcome)
        self.assertEqual(1, len(self.asset_downloads()), "refresh should fetch it again")

    def test_changing_the_webhost_policy_re_checks_it(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        self.http.requests.clear()
        records = self.crawl(self.options(webhost_check="off"))
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)
        self.assertEqual(1, len(self.asset_downloads()))
        self.assert_installed("mygame")

    def test_a_new_archipelago_version_re_checks_it(self) -> None:
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        self.http.requests.clear()
        newer = CoreVersions(ap_version=(0, 7, 0), container_version=7)
        records = self.crawl(versions=newer)
        self.assertEqual(OUTCOME_SKIPPED, self.record_for(records, "Some Game").outcome)
        self.assertEqual(1, len(self.asset_downloads()), "a core upgrade could change the verdict")

    def test_a_world_that_is_fine_leaves_no_rejection(self) -> None:
        self.add_game("Some Game", init_source=self.GOOD)
        self.crawl()
        self.assertEqual([], self.rejected())


class TestRemovingRejectedWorlds(CrawlTestCase):
    """A world already in worlds/ that is no longer acceptable gets taken out again."""

    GOOD = TestWebWorldRejection.GOOD
    NO_TUTORIALS = TestWebWorldRejection.NO_TUTORIALS

    def install_then_break(self) -> None:
        """Install a world under a lenient policy, then make the checks reject the same release."""
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl(self.options(webhost_check="off"))
        self.assert_installed("mygame")

    def test_a_previously_installed_world_is_removed(self) -> None:
        self.install_then_break()
        records = self.crawl()  # default policy rejects it
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertEqual(self.relative("mygame"), record.removed)
        self.assert_not_installed("mygame")

    def test_the_removal_is_recorded_in_the_lockfile(self) -> None:
        self.install_then_break()
        self.crawl()
        self.assertEqual([], self.lock["worlds"])
        entry = self.lock["rejected"][0]
        self.assertEqual(self.relative("mygame"), entry["removed"])

    def test_the_removal_is_still_recorded_on_later_runs(self) -> None:
        self.install_then_break()
        self.crawl()
        self.crawl()
        entry = self.lock["rejected"][0]
        self.assertEqual(self.relative("mygame"), entry["removed"], "the removal should not be forgotten")

    def test_the_removed_world_is_not_downloaded_again(self) -> None:
        self.install_then_break()
        self.crawl()
        self.http.requests.clear()
        records = self.crawl()
        self.assertEqual(OUTCOME_KNOWN_BAD, self.record_for(records, "Some Game").outcome)
        self.assertEqual([], [r for r in self.http.requests if "/releases/download/" in r])
        self.assert_not_installed("mygame")

    def test_a_dry_run_removes_nothing(self) -> None:
        self.install_then_break()
        self.crawl(self.options(dry_run=True))
        self.assert_installed("mygame")

    def test_a_working_older_release_is_kept_when_the_newest_is_broken(self) -> None:
        # v1 is fine and installed; v2 is broken. Losing a working game would be a bad trade.
        self.add_game("Some Game", init_source=self.GOOD)
        self.crawl()
        self.assert_installed("mygame")

        self.releases["owner/repo"].append(
            release("v2.0.0", "mygame.apworld", published_at="2026-06-01T00:00:00Z")
        )
        make_apworld(
            self.assets / "v2.0.0__mygame.apworld",
            module="mygame",
            manifest=default_manifest("Some Game Game"),
            init_source=self.NO_TUTORIALS,
        )
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertEqual("", record.removed)
        self.assert_installed("mygame")
        self.assertTrue(any("keeping the installed v1.0.0" in note for note in record.notes), record.notes)

    def test_worlds_not_installed_by_the_crawler_are_never_touched(self) -> None:
        bystander = self.root / "worlds" / "mygame"
        bystander.mkdir()
        (bystander / "__init__.py").write_text("# hand placed\n", encoding="utf-8")
        self.add_game("Some Game", init_source=self.NO_TUTORIALS)
        self.crawl()
        self.assertEqual("# hand placed\n", (bystander / "__init__.py").read_text(encoding="utf-8"))


class TestPartialRuns(CrawlTestCase):
    """--only and --limit must not make the crawler forget everything they skipped."""

    def setUp(self) -> None:
        super().setUp()
        self.add_game("Game A", repo="a/a", asset_name="game_a.apworld")
        self.add_game("Game B", repo="b/b", asset_name="game_b.apworld")
        self.crawl()

    def test_only_keeps_the_other_worlds_in_the_lockfile(self) -> None:
        self.crawl(self.options(only=("Game A",)))
        self.assertEqual(["Game A", "Game B"], [entry["title"] for entry in self.lock["worlds"]])

    def test_only_with_prune_does_not_delete_the_other_worlds(self) -> None:
        self.crawl(self.options(only=("Game A",), prune=True))
        self.assert_installed("game_a")
        self.assert_installed("game_b")

    def test_limit_keeps_the_other_worlds(self) -> None:
        self.crawl(self.options(limit=1, prune=True))
        self.assertEqual(2, len(self.lock["worlds"]))
        self.assert_installed("game_a")
        self.assert_installed("game_b")

    def test_a_full_run_still_prunes_what_left_the_category(self) -> None:
        del self.pages["Game B"]
        self.crawl(self.options(prune=True))
        self.assert_installed("game_a")
        self.assertFalse(self.world_path("game_b").exists())
        self.assertEqual(["Game A"], [entry["title"] for entry in self.lock["worlds"]])

    def test_a_partial_run_keeps_another_pages_rejection(self) -> None:
        self.add_game("Game C", repo="c/c", asset_name="game_c.apworld",
                      init_source=TestWebWorldRejection.NO_TUTORIALS)
        self.crawl()
        self.assertEqual(["Game C"], [entry["title"] for entry in self.lock["rejected"]])

        self.crawl(self.options(only=("Game A",)))
        self.assertEqual(["Game C"], [entry["title"] for entry in self.lock["rejected"]])


class TestFilteredReleasesLink(CrawlTestCase):
    """A wiki page linking to a filtered releases page, as Mega Man X1 does.

    The link is https://github.com/TheLX5/Archipelago/releases?q="Mega+Man+X"&expanded=true, and the
    repository is a whole Archipelago fork: dozens of games, with Mega Man X releases nowhere near
    the top of the feed.
    """

    REPO = "TheLX5/Archipelago"

    def setUp(self) -> None:
        super().setUp()
        self.releases[self.REPO] = [
            release("smw-3.0.1", "smw.apworld", published_at="2026-07-01T00:00:00Z", repo=self.REPO),
            release("Mega Man X3 v1.0", "mmx3.apworld", published_at="2026-06-01T00:00:00Z", repo=self.REPO),
            release("Mega Man X2 v1.1", "mmx2.apworld", published_at="2026-05-01T00:00:00Z", repo=self.REPO),
            release("Mega Man X1 v1.4", "mmx.apworld", published_at="2026-04-01T00:00:00Z", repo=self.REPO),
            release("yoshi-2.2", "yoshi.apworld", published_at="2026-03-01T00:00:00Z", repo=self.REPO),
        ]
        for stem, game in (
            ("smw", "Super Mario World"),
            ("mmx3", "Mega Man X3"),
            ("mmx2", "Mega Man X2"),
            ("mmx", "Mega Man X1"),
            ("yoshi", "Yoshi's Island"),
        ):
            make_apworld(self.assets / f"{stem}.apworld", module=stem, manifest=default_manifest(game))

    def add_page(self, title: str, href: str) -> None:
        self.pages[title] = {
            "title": title,
            "wikitext": f"{{{{Infobox game| game = {title} | download = [{href} Download] }}}}",
            "text": wiki_page_html(title=title, download_href=href),
            "externallinks": [href],
        }

    def filtered(self, query: str) -> str:
        return f'https://github.com/{self.REPO}/releases?q="{query.replace(" ", "+")}"&expanded=true'

    def test_the_link_filter_picks_the_right_game(self) -> None:
        self.add_page("Mega Man X1", self.filtered("Mega Man X"))
        records = self.crawl()
        record = self.record_for(records, "Mega Man X1")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertEqual("mmx.apworld", record.asset_name)
        self.assertEqual("Mega Man X1 v1.4", record.release_tag)
        self.assertEqual("Mega Man X1", record.game)
        self.assert_installed("mmx")

    def test_each_page_in_the_series_gets_its_own_release(self) -> None:
        for title in ("Mega Man X1", "Mega Man X2", "Mega Man X3"):
            self.add_page(title, self.filtered("Mega Man X"))
        records = self.crawl()
        self.assertEqual(
            {"Mega Man X1": "Mega Man X1", "Mega Man X2": "Mega Man X2", "Mega Man X3": "Mega Man X3"},
            {record.title: record.game for record in records},
        )
        for stem in ("mmx", "mmx2", "mmx3"):
            self.assert_installed(stem)

    def test_the_filter_is_noted_for_review(self) -> None:
        self.add_page("Mega Man X1", self.filtered("Mega Man X"))
        records = self.crawl()
        record = self.record_for(records, "Mega Man X1")
        self.assertTrue(any("filters releases by 'Mega Man X'" in note for note in record.notes), record.notes)

    def test_an_abbreviated_asset_name_is_reachable_through_the_filter(self) -> None:
        # Without the filter "smw" is an abbreviation the matcher refuses to connect to the page.
        self.add_page("Super Mario World", f"https://github.com/{self.REPO}")
        refused = self.crawl()
        self.assertEqual(OUTCOME_FAILED, self.record_for(refused, "Super Mario World").outcome)

        self.pages.clear()
        self.add_page("Super Mario World", self.filtered("smw"))
        records = self.crawl(self.options(refresh=True))
        record = self.record_for(records, "Super Mario World")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertEqual("smw.apworld", record.asset_name)

    def test_an_unfiltered_link_still_needs_the_name_to_match(self) -> None:
        self.add_page("Mega Man X1", f"https://github.com/{self.REPO}")
        records = self.crawl()
        record = self.record_for(records, "Mega Man X1")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assertEqual("Mega Man X1", record.game)


class TestMultiModuleWorlds(CrawlTestCase):
    """Worlds that keep their WebWorld in a separate module, which one file cannot judge."""

    WORLD_INIT = (
        "from worlds.AutoWorld import World\n"
        "from .web import GameWeb\n"
        'class GameWorld(World):\n    game = "Some Game Game"\n    web = GameWeb()\n'
    )
    NO_TUTORIALS = 'from worlds.AutoWorld import WebWorld\nclass GameWeb(WebWorld):\n    theme = "grass"\n'
    WITH_TUTORIALS = (
        "from worlds.AutoWorld import WebWorld\n"
        "from BaseClasses import Tutorial\n"
        'class GameWeb(WebWorld):\n    tutorials = [Tutorial("Setup", "d", "en", "s.md", "s/en", ["me"])]\n'
    )

    def add_split_game(self, web_source: str) -> None:
        self.add_game("Some Game", init_source=self.WORLD_INIT)
        # Rebuild the apworld with the extra module the World imports.
        make_apworld(
            self.assets / "mygame.apworld",
            module="mygame",
            manifest=default_manifest("Some Game Game"),
            init_source=self.WORLD_INIT,
            extra_files={"mygame/web.py": web_source},
        )

    def test_a_split_world_without_tutorials_is_refused(self) -> None:
        self.add_split_game(self.NO_TUTORIALS)
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("invalid for WebHost", record.reason)
        self.assert_not_installed("mygame")

    def test_a_split_world_with_tutorials_installs(self) -> None:
        self.add_split_game(self.WITH_TUTORIALS)
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome, record.reason)
        self.assert_installed("mygame")

    def test_the_extra_module_survives_installation(self) -> None:
        self.add_split_game(self.WITH_TUTORIALS)
        self.crawl()
        self.assertTrue((self.world_path("mygame") / "web.py").is_file())


class TestValidation(CrawlTestCase):
    """The backstop: whatever Archipelago itself rejects is removed and remembered."""

    def setUp(self) -> None:
        super().setUp()
        self.add_game("Good Game", repo="a/a", asset_name="good.apworld")
        self.add_game("Bad Game", repo="b/b", asset_name="bad.apworld")
        self.report = ValidationReport(
            ok=True,
            registered=2,
            verdicts=[
                WorldVerdict(
                    game="Bad Game Game",
                    # validate.py reads this off the installed folder, so it carries the prefix.
                    module=prefixed("bad", DEFAULT_MODULE_PREFIX),
                    status=TEMPLATE_FAILED,
                    reason="AttributeError: 'list' object has no attribute 'update'",
                ),
            ],
        )

    def crawl_with_validation(self, report: ValidationReport, **options: Any) -> list[GameRecord]:
        settings = self.options(validate=True, **options)
        crawler = Crawler(
            settings,
            WikiClient(self.http),  # type: ignore[arg-type]
            GitHubClient(self.http, token=None),  # type: ignore[arg-type]
            VERSIONS,
        )
        records = crawler.run(self.staging)
        with mock.patch("tools.custom_worlds.crawl.validate_installed_worlds", return_value=report):
            self.passed, self.detail = crawler.validate_and_remove()
        write_lockfile(
            settings.lockfile,
            records,
            settings,
            VERSIONS,
            previously_installed=crawler.previous,
            previously_rejected=crawler.rejected,
        )
        return records

    def test_a_world_archipelago_rejects_is_removed(self) -> None:
        records = self.crawl_with_validation(self.report)
        self.assertTrue(self.passed, self.detail)
        record = self.record_for(records, "Bad Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("template-failed", record.reason)
        self.assertEqual(self.relative("bad"), record.removed)
        self.assert_not_installed("bad")

    def test_the_rest_are_left_alone(self) -> None:
        records = self.crawl_with_validation(self.report)
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Good Game").outcome)
        self.assert_installed("good")

    def test_the_rejection_is_recorded_so_it_is_not_downloaded_again(self) -> None:
        self.crawl_with_validation(self.report)
        rejected = self.lock["rejected"]
        self.assertEqual(["Bad Game"], [entry["title"] for entry in rejected])
        self.assertIn(TEMPLATE_FAILED, rejected[0]["codes"])
        self.assertEqual(["Good Game"], [entry["title"] for entry in self.lock["worlds"]])

        self.http.requests.clear()
        records = self.crawl()
        self.assertEqual(OUTCOME_KNOWN_BAD, self.record_for(records, "Bad Game").outcome)
        self.assertEqual([], [r for r in self.http.requests if "/releases/download/bad" in r])

    def test_nothing_is_removed_when_validation_cannot_run(self) -> None:
        broken = ValidationReport(ok=False, error="ModuleNotFoundError: no module named 'schema'")
        records = self.crawl_with_validation(broken)
        self.assertFalse(self.passed)
        self.assertIn("schema", self.detail)
        for title in ("Good Game", "Bad Game"):
            self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, title).outcome)
        self.assert_installed("good")
        self.assert_installed("bad")

    def test_a_verdict_for_a_world_this_run_did_not_install_is_only_reported(self) -> None:
        report = ValidationReport(
            ok=True,
            registered=1,
            verdicts=[WorldVerdict(game="Some Core Game", module="alttp", status=INVALID_FOR_WEBHOST)],
        )
        records = self.crawl_with_validation(report)
        self.assertTrue(self.passed)
        for title in ("Good Game", "Bad Game"):
            self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, title).outcome)

    def test_a_dry_run_removes_nothing(self) -> None:
        # A dry run installs nothing, so there is nothing for validation to take back out either.
        records = self.crawl(self.options(dry_run=True, validate=True))
        self.assertEqual(OUTCOME_RESOLVED, self.record_for(records, "Bad Game").outcome)


class TestArchiveMode(CrawlTestCase):
    """The same pipeline, but leaving the .apworld file intact instead of unpacking it."""

    install_mode = INSTALL_ARCHIVE

    def test_a_missing_docs_folder_does_not_matter_here(self) -> None:
        # The zip branch of copy_tutorials_files_to_static() copies whatever is under docs/ and is
        # content to find nothing, so this is only a problem for worlds installed as folders.
        self.add_game("Duck Life 4", asset_name="ducklife4.apworld", init_source=GOOD_WORLD)
        make_apworld(
            self.assets / "ducklife4.apworld",
            module="ducklife4",
            manifest=default_manifest("Duck Life 4 Game"),
            init_source=GOOD_WORLD,
            docs=False,
        )
        record = self.record_for(self.crawl(), "Duck Life 4")
        self.assertEqual(OUTCOME_INSTALLED, record.outcome)
        self.assert_installed("ducklife4")

    def test_installs_the_apworld_repacked_under_the_prefixed_name(self) -> None:
        # Core imports worlds.<file stem> and then wants a folder of exactly that name inside the
        # zip, so renaming the file means renaming the folder inside it too.
        source = self.add_game("Some Game")
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)
        installed = self.world_path("mygame")
        with zipfile.ZipFile(installed) as archive:
            names = archive.namelist()
            self.assertIn("cw_mygame/__init__.py", names)
            self.assertFalse([name for name in names if name.startswith("mygame/")])
            with zipfile.ZipFile(source) as original:
                self.assertEqual(
                    sorted(name.split("/", 1)[1] for name in original.namelist()),
                    sorted(name.split("/", 1)[1] for name in names),
                )

    def test_nothing_is_unpacked(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        self.assertFalse((self.root / "worlds" / prefixed("mygame", self.module_prefix)).exists())

    def test_a_tampered_file_is_re_downloaded(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        self.world_path("mygame").write_bytes(b"corrupted")
        records = self.crawl()
        self.assertEqual(OUTCOME_UPDATED, self.record_for(records, "Some Game").outcome)

    def test_a_second_run_is_a_no_op(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        records = self.crawl()
        self.assertEqual(OUTCOME_UNCHANGED, self.record_for(records, "Some Game").outcome)

    def test_mixed_case_file_names_are_preserved(self) -> None:
        # Core imports worlds.<file stem> and zipimport then wants a folder of exactly that name,
        # so the crawler must not helpfully lower-case the file.
        self.add_game("Some Game", asset_name="MyGame.apworld", module="MyGame")
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)
        self.assertTrue((self.root / "worlds" / "cw_MyGame.apworld").is_file())


class TestExtractWorld(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def test_extracts_only_the_world_folder(self) -> None:
        archive = make_apworld(
            self.tmp / "mygame.apworld",
            manifest=default_manifest(),
            extra_files={"README.md": "not part of the world"},
        )
        destination = self.tmp / "out" / "mygame"
        extract_world(archive, destination)
        self.assertTrue((destination / "__init__.py").is_file())
        self.assertTrue((destination / "archipelago.json").is_file())
        self.assertFalse((destination / "README.md").exists())

    def test_nested_files_survive(self) -> None:
        archive = make_apworld(
            self.tmp / "mygame.apworld",
            manifest=default_manifest(),
            extra_files={"mygame/data/items.json": "[]", "mygame/docs/setup_en.md": "# setup"},
        )
        destination = self.tmp / "mygame"
        extract_world(archive, destination)
        self.assertEqual("[]", (destination / "data" / "items.json").read_text(encoding="utf-8"))
        self.assertEqual("# setup", (destination / "docs" / "setup_en.md").read_text(encoding="utf-8"))

    def test_replacing_an_existing_world_removes_stale_files(self) -> None:
        destination = self.tmp / "mygame"
        destination.mkdir()
        (destination / "stale.py").write_text("old", encoding="utf-8")
        archive = make_apworld(self.tmp / "mygame.apworld", manifest=default_manifest())
        extract_world(archive, destination)
        self.assertFalse((destination / "stale.py").exists())
        self.assertTrue((destination / "__init__.py").is_file())

    def test_path_traversal_is_refused(self) -> None:
        archive = make_apworld(
            self.tmp / "mygame.apworld",
            manifest=default_manifest(),
            extra_files={"mygame/../../escaped.py": "pwned"},
        )
        destination = self.tmp / "out" / "mygame"
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            extract_world(archive, destination)
        self.assertFalse((self.tmp / "escaped.py").exists())
        self.assertFalse(destination.exists())

    def test_absolute_paths_are_refused(self) -> None:
        archive = make_apworld(
            self.tmp / "mygame.apworld",
            manifest=default_manifest(),
            extra_files={"mygame//etc/passwd": "pwned"},
        )
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            extract_world(archive, self.tmp / "out" / "mygame")


if __name__ == "__main__":
    unittest.main()
