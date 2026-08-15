"""End-to-end tests for the crawler, with the wiki and GitHub replaced by scripted responses."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test.custom_worlds.helpers import FakeHttp, default_manifest, make_apworld, release, wiki_page_html
from tools.custom_worlds.crawl import (
    INSTALL_ARCHIVE,
    INSTALL_EXTRACT,
    OUTCOME_FAILED,
    OUTCOME_INSTALLED,
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
from tools.custom_worlds.verify import CoreVersions
from tools.custom_worlds.wiki import WikiClient

VERSIONS = CoreVersions(ap_version=(0, 6, 8), container_version=7)


class CrawlTestCase(unittest.TestCase):
    """Builds a throwaway Archipelago checkout and a wiki/GitHub pair that serve one game."""

    install_mode = INSTALL_EXTRACT

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
        )

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
        }
        settings.update(overrides)
        return CrawlOptions(**settings)

    def crawl(self, options: CrawlOptions | None = None, *, write_lock: bool = True) -> list[GameRecord]:
        options = options or self.options()
        crawler = Crawler(
            options,
            WikiClient(self.http),  # type: ignore[arg-type]
            GitHubClient(self.http, token=None),  # type: ignore[arg-type]
            VERSIONS,
        )
        records = crawler.run(self.staging)
        if write_lock and not options.dry_run:
            write_lockfile(options.lockfile, records, options, VERSIONS)
        return records

    def record_for(self, records: list[GameRecord], title: str) -> GameRecord:
        return next(record for record in records if record.title == title)

    def world_path(self, stem: str, *, output: str = "worlds") -> Path:
        """Where a world with this module name ends up under the current install mode."""
        if self.install_mode == INSTALL_ARCHIVE:
            return self.root / output / f"{stem}.apworld"
        return self.root / output / stem

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
        self.assertEqual(expected, (self.root / "worlds" / "mygame" / "__init__.py").read_bytes())
        self.assertTrue((self.root / "worlds" / "mygame" / "archipelago.json").is_file())

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
        bundled = self.root / "worlds" / "mygame"
        bundled.mkdir()
        (bundled / "__init__.py").write_text("", encoding="utf-8")
        self.add_game("Some Game")
        records = self.crawl()
        record = self.record_for(records, "Some Game")
        self.assertEqual(OUTCOME_SKIPPED, record.outcome)
        self.assertIn("already exists in worlds/", record.reason)
        self.assertFalse((bundled / "archipelago.json").exists())

    def test_two_pages_shipping_the_same_game_are_both_skipped(self) -> None:
        self.add_game("Game A", repo="a/a", asset_name="a.apworld", manifest=default_manifest("Shared Game"))
        self.add_game("Game B", repo="b/b", asset_name="b.apworld", manifest=default_manifest("Shared Game"))
        records = self.crawl()
        for record in records:
            self.assertEqual(OUTCOME_SKIPPED, record.outcome)
            self.assertIn("provided by more than one file", record.reason)
        self.assertEqual([], self.lock["worlds"])

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


class TestArchiveMode(CrawlTestCase):
    """The same pipeline, but leaving the .apworld file intact instead of unpacking it."""

    install_mode = INSTALL_ARCHIVE

    def test_installs_the_apworld_file_itself(self) -> None:
        source = self.add_game("Some Game")
        records = self.crawl()
        self.assertEqual(OUTCOME_INSTALLED, self.record_for(records, "Some Game").outcome)
        installed = self.root / "worlds" / "mygame.apworld"
        self.assertEqual(source.read_bytes(), installed.read_bytes())

    def test_nothing_is_unpacked(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        self.assertFalse((self.root / "worlds" / "mygame").exists())

    def test_a_tampered_file_is_re_downloaded(self) -> None:
        self.add_game("Some Game")
        self.crawl()
        (self.root / "worlds" / "mygame.apworld").write_bytes(b"corrupted")
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
        self.assertTrue((self.root / "worlds" / "MyGame.apworld").is_file())


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
