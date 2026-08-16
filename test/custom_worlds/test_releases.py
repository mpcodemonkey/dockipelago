"""Tests for turning a download link into a concrete apworld asset."""

import unittest
from typing import Any

from test.custom_worlds.helpers import FakeHttp, release
from tools.custom_worlds.releases import (
    GitHubClient,
    ResolutionError,
    parse_github_url,
    resolve_assets,
    select_apworld_assets,
)


class TestParseGitHubUrl(unittest.TestCase):
    def test_repository_root(self) -> None:
        target = parse_github_url("https://github.com/owner/repo")
        assert target is not None
        self.assertEqual(("owner", "repo"), (target.owner, target.repo))
        self.assertIsNone(target.tag)

    def test_trailing_git_suffix_is_dropped(self) -> None:
        target = parse_github_url("https://github.com/owner/repo.git")
        assert target is not None
        self.assertEqual("repo", target.repo)

    def test_releases_page(self) -> None:
        target = parse_github_url("https://github.com/owner/repo/releases")
        assert target is not None
        self.assertEqual("repo", target.repo)
        self.assertIsNone(target.tag)

    def test_tagged_release(self) -> None:
        target = parse_github_url("https://github.com/owner/repo/releases/tag/v1.2.3")
        assert target is not None
        self.assertEqual("v1.2.3", target.tag)

    def test_direct_asset_download(self) -> None:
        url = "https://github.com/owner/repo/releases/download/v1.2.3/game.apworld"
        target = parse_github_url(url)
        assert target is not None
        self.assertEqual("game.apworld", target.asset_name)
        self.assertEqual(url, target.asset_url)
        self.assertEqual("v1.2.3", target.tag)

    def test_blob_link_becomes_a_raw_link(self) -> None:
        target = parse_github_url("https://github.com/owner/repo/blob/main/dist/game.apworld")
        assert target is not None
        self.assertEqual("https://raw.githubusercontent.com/owner/repo/main/dist/game.apworld", target.asset_url)

    def test_tree_link_is_treated_as_the_repository(self) -> None:
        target = parse_github_url("https://github.com/owner/repo/tree/main/worlds")
        assert target is not None
        self.assertEqual(("owner", "repo"), (target.owner, target.repo))
        self.assertIsNone(target.asset_url)

    def test_non_github_urls_are_rejected(self) -> None:
        self.assertIsNone(parse_github_url("https://gitlab.com/owner/repo"))
        self.assertIsNone(parse_github_url("https://github.com/owner"))


class TestReleaseQuery(unittest.TestCase):
    """A filtered releases link carries the maintainer's own answer about which releases apply."""

    MEGA_MAN_X = 'https://github.com/TheLX5/Archipelago/releases?q="Mega+Man+X"&expanded=true'

    def test_the_query_is_read_off_a_filtered_releases_link(self) -> None:
        target = parse_github_url(self.MEGA_MAN_X)
        assert target is not None
        self.assertEqual("TheLX5/Archipelago", target.slug)
        self.assertEqual("Mega Man X", target.release_query)

    def test_quoting_and_encoding_are_handled(self) -> None:
        for url, expected in (
            ("https://github.com/o/r/releases?q=%22Mega+Man+X%22", "Mega Man X"),
            ("https://github.com/o/r/releases?q=Mega+Man+X", "Mega Man X"),
            ("https://github.com/o/r/releases?q=%22Sonic%20Battle%22&expanded=true", "Sonic Battle"),
            ("https://github.com/o/r/releases?expanded=true&q=actraiser", "actraiser"),
        ):
            target = parse_github_url(url)
            assert target is not None
            self.assertEqual(expected, target.release_query, url)

    def test_a_releases_link_without_a_query_has_none(self) -> None:
        target = parse_github_url("https://github.com/o/r/releases")
        assert target is not None
        self.assertEqual("", target.release_query)

    def test_a_query_elsewhere_is_ignored(self) -> None:
        target = parse_github_url("https://github.com/o/r?q=nonsense")
        assert target is not None
        self.assertEqual("", target.release_query)

    def _releases(self) -> list[Any]:
        return [
            release("smw-3.0.1", "smw.apworld", published_at="2026-07-01T00:00:00Z"),
            release("Mega Man X3 v1.0", "mmx3.apworld", published_at="2026-06-01T00:00:00Z"),
            release("Mega Man X2 v1.1", "mmx2.apworld", published_at="2026-05-01T00:00:00Z"),
            release("Mega Man X1 v1.4", "mmx.apworld", published_at="2026-04-01T00:00:00Z"),
            release("yoshisisland-2.2", "yoshi.apworld", published_at="2026-03-01T00:00:00Z"),
        ]

    def test_the_filter_narrows_the_feed(self) -> None:
        resolution = select_apworld_assets(
            self._releases(), expected_game="Mega Man X1", release_query="Mega Man X", source="o/r"
        )
        self.assertEqual("mmx.apworld", resolution.selected[0].name)
        self.assertTrue(any("leaving 3 of 5" in note for note in resolution.notes), resolution.notes)

    def test_each_entry_in_the_series_resolves_to_its_own_release(self) -> None:
        for expected, asset in (
            ("Mega Man X1", "mmx.apworld"),
            ("Mega Man X2", "mmx2.apworld"),
            ("Mega Man X3", "mmx3.apworld"),
        ):
            resolution = select_apworld_assets(
                self._releases(), expected_game=expected, release_query="Mega Man X", source="o/r"
            )
            self.assertEqual(asset, resolution.selected[0].name, expected)

    def test_the_filter_keeps_the_newest_matching_release(self) -> None:
        # Only one game inside the filter, so the newest of it wins without any name guessing.
        releases = [
            release("smw-3.0.1", "smw.apworld", published_at="2026-07-01T00:00:00Z"),
            release("Mega Man X v2.0", "mmx.apworld", published_at="2026-06-01T00:00:00Z"),
            release("Mega Man X v1.0", "mmx.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        resolution = select_apworld_assets(
            releases, expected_game="Anything At All", release_query="Mega Man X", source="o/r"
        )
        self.assertEqual("Mega Man X v2.0", resolution.selected[0].release_tag)
        self.assertFalse(resolution.multi_game, "the filter left one game, so there is nothing to guess")

    def test_a_filter_that_matches_nothing_falls_back_to_the_whole_feed(self) -> None:
        resolution = select_apworld_assets(
            self._releases(), expected_game="Super Mario World", release_query="Renamed Long Ago", source="o/r"
        )
        self.assertTrue(any("no release matches it any more" in note for note in resolution.notes))

    def test_the_filter_rescues_a_game_the_name_matching_would_refuse(self) -> None:
        # "smw" is an abbreviation the matcher cannot connect to "Super Mario World" on its own.
        releases = [
            release("smw-3.0.1", "smw.apworld", published_at="2026-07-01T00:00:00Z"),
            release("mmx-1.0", "mmx.apworld", published_at="2026-06-01T00:00:00Z"),
        ]
        refused = select_apworld_assets(releases, expected_game="Super Mario World", source="o/r")
        self.assertEqual([], refused.selected)

        resolution = select_apworld_assets(
            releases, expected_game="Super Mario World", release_query="smw", source="o/r"
        )
        self.assertEqual("smw.apworld", resolution.selected[0].name)


class TestSelectApworldAssets(unittest.TestCase):
    def test_picks_the_newest_release_that_has_an_apworld(self) -> None:
        releases = [
            release("v2", "notes.txt", published_at="2026-02-01T00:00:00Z"),
            release("v1", "game.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        assets, _notes = _select(releases)
        self.assertEqual(["game.apworld"], [asset.name for asset in assets])
        self.assertEqual("v1", assets[0].release_tag)

    def test_orders_by_publication_date_not_list_order(self) -> None:
        releases = [
            release("old", "game.apworld", published_at="2025-01-01T00:00:00Z"),
            release("new", "game.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        assets, _notes = _select(releases)
        self.assertEqual("new", assets[0].release_tag)

    def test_drafts_are_never_used(self) -> None:
        releases = [
            release("draft", "game.apworld", published_at="2026-05-01T00:00:00Z", draft=True),
            release("v1", "game.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        assets, _notes = _select(releases)
        self.assertEqual("v1", assets[0].release_tag)

    def test_prereleases_are_skipped_by_default(self) -> None:
        releases = [
            release("v2-rc1", "game.apworld", published_at="2026-05-01T00:00:00Z", prerelease=True),
            release("v1", "game.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        assets, _notes = _select(releases)
        self.assertEqual("v1", assets[0].release_tag)

    def test_prereleases_can_be_opted_into(self) -> None:
        releases = [
            release("v2-rc1", "game.apworld", published_at="2026-05-01T00:00:00Z", prerelease=True),
            release("v1", "game.apworld", published_at="2026-01-01T00:00:00Z"),
        ]
        assets, notes = _select(releases, allow_prerelease=True)
        self.assertEqual("v2-rc1", assets[0].release_tag)
        self.assertTrue(any("pre-release" in note for note in notes))

    def test_a_prerelease_only_repository_still_resolves(self) -> None:
        releases = [release("v0.1-beta", "game.apworld", prerelease=True)]
        assets, notes = _select(releases)
        self.assertEqual("v0.1-beta", assets[0].release_tag)
        self.assertTrue(any("only pre-releases" in note for note in notes))

    def test_picks_the_asset_matching_the_repository_name(self) -> None:
        releases = [release("v1", "some_other_tool.apworld", "my_game.apworld")]
        assets, notes = _select(releases, expected_game="My Game")
        self.assertEqual(["my_game.apworld"], [asset.name for asset in assets])
        self.assertTrue(any("ignored other .apworld assets" in note for note in notes))

    def test_all_assets_keeps_every_apworld(self) -> None:
        releases = [release("v1", "a.apworld", "b.apworld")]
        assets, _notes = _select(releases, all_assets=True, expected_game="a")
        self.assertEqual({"a.apworld", "b.apworld"}, {asset.name for asset in assets})

    def test_returns_nothing_when_no_release_has_an_apworld(self) -> None:
        assets, _notes = _select([release("v1", "setup.exe")])
        self.assertEqual([], assets)

    def test_asset_metadata_is_carried_through(self) -> None:
        assets, _notes = _select([release("v1", "game.apworld")], source="owner/repo")
        asset = assets[0]
        self.assertEqual("owner/repo", asset.source)
        self.assertEqual(1024, asset.size)
        self.assertEqual("game", asset.module_name)
        self.assertEqual(
            "https://github.com/owner/repo/releases/download/v1/game.apworld", asset.download_url
        )


class TestResolveAssets(unittest.TestCase):
    def _github(self, http: FakeHttp) -> GitHubClient:
        return GitHubClient(http, token=None)  # type: ignore[arg-type]

    def test_resolves_a_repository_link_through_the_releases_api(self) -> None:
        http = FakeHttp()
        http.json_route("/repos/owner/repo/releases", [release("v1", "game.apworld")])
        assets, _notes = _resolve("https://github.com/owner/repo", self._github(http))
        self.assertEqual("game.apworld", assets[0].name)
        self.assertEqual("owner/repo", assets[0].source)

    def test_a_direct_apworld_url_needs_no_api_call(self) -> None:
        http = FakeHttp()
        assets, notes = _resolve("https://example.test/files/My%20Game.apworld", self._github(http))
        self.assertEqual("My Game.apworld", assets[0].name)
        self.assertEqual([], http.requests)
        self.assertTrue(any("directly at an .apworld" in note for note in notes))

    def test_a_release_asset_link_is_used_as_is(self) -> None:
        http = FakeHttp()
        url = "https://github.com/owner/repo/releases/download/v3/game.apworld"
        assets, _notes = _resolve(url, self._github(http))
        self.assertEqual(url, assets[0].download_url)
        self.assertEqual("v3", assets[0].release_tag)
        self.assertEqual([], http.requests)

    def test_a_tagged_link_reads_that_specific_release(self) -> None:
        http = FakeHttp()
        http.json_route("/releases/tags/v9", release("v9", "game.apworld"))
        assets, _notes = _resolve("https://github.com/owner/repo/releases/tag/v9", self._github(http))
        self.assertEqual("v9", assets[0].release_tag)

    def test_a_missing_tag_falls_back_to_the_newest_release(self) -> None:
        http = FakeHttp()
        http.route("/releases/tags/", lambda url: (_ for _ in ()).throw(_not_found(url)))
        http.json_route("/repos/owner/repo/releases", [release("v2", "game.apworld")])
        assets, notes = _resolve("https://github.com/owner/repo/releases/tag/gone", self._github(http))
        self.assertEqual("v2", assets[0].release_tag)
        self.assertTrue(any("falling back" in note for note in notes))

    def test_repository_without_releases_is_an_error(self) -> None:
        http = FakeHttp()
        http.json_route("/repos/owner/repo/releases", [])
        with self.assertRaisesRegex(ResolutionError, "no GitHub releases"):
            resolve_assets("https://github.com/owner/repo", self._github(http))

    def test_repository_whose_releases_have_no_apworld_is_an_error(self) -> None:
        http = FakeHttp()
        http.json_route("/repos/owner/repo/releases", [release("v1", "setup.exe")])
        with self.assertRaisesRegex(ResolutionError, "no .apworld asset"):
            resolve_assets("https://github.com/owner/repo", self._github(http))

    def test_a_link_we_cannot_interpret_is_an_error(self) -> None:
        with self.assertRaisesRegex(ResolutionError, "not a GitHub link"):
            resolve_assets("https://example.test/downloads", self._github(FakeHttp()))


class TestGitHubClient(unittest.TestCase):
    def test_pagination_stops_on_a_short_page(self) -> None:
        http = FakeHttp()
        http.json_route("/releases", [release("v1", "game.apworld")])
        GitHubClient(http, token=None).list_releases("owner", "repo")  # type: ignore[arg-type]
        self.assertEqual(1, len(http.requests))
        self.assertIn("page=1", http.requests[0])

    def test_pagination_continues_while_pages_are_full(self) -> None:
        http = FakeHttp()
        full_page = [release(f"v{index}", "game.apworld") for index in range(30)]
        responses = iter([full_page, full_page, [release("last", "game.apworld")]])
        http.route("/releases", lambda _url: next(responses))
        releases = GitHubClient(http, token=None).list_releases("owner", "repo")  # type: ignore[arg-type]
        self.assertEqual(61, len(releases))
        self.assertEqual(3, len(http.requests))

    def test_a_token_is_sent_as_a_bearer_header(self) -> None:
        client = GitHubClient(FakeHttp(), token="secret")  # type: ignore[arg-type]
        self.assertEqual("Bearer secret", client.headers["Authorization"])

    def test_no_authorization_header_without_a_token(self) -> None:
        client = GitHubClient(FakeHttp(), token=None)  # type: ignore[arg-type]
        client.token = None
        self.assertNotIn("Authorization", client.headers)


def _select(*args: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
    """select_apworld_assets returns a Resolution; these tests only care about two of its fields."""
    resolution = select_apworld_assets(*args, **kwargs)
    return resolution.selected, resolution.notes


def _resolve(*args: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
    resolution = resolve_assets(*args, **kwargs)
    return resolution.selected, resolution.notes


def _not_found(url: str) -> Exception:
    from tools.custom_worlds.http import HttpError

    return HttpError(url, 404, "Not Found")


if __name__ == "__main__":
    unittest.main()
