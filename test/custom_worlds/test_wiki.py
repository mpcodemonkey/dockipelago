"""Tests for reading custom game pages off the wiki."""

import unittest

from test.custom_worlds.helpers import FakeHttp, wiki_page_html
from tools.custom_worlds.wiki import (
    WikiClient,
    WikiPage,
    extract_download_url,
    parse_templates,
)


class TestParseTemplates(unittest.TestCase):
    def test_reads_named_parameters(self) -> None:
        wikitext = "{{Infobox game\n| title = Some Game\n| developer = Studio\n}}"
        templates = parse_templates(wikitext)
        self.assertEqual(1, len(templates))
        self.assertEqual("Infobox game", templates[0].name)
        self.assertEqual("Some Game", templates[0].params["title"])

    def test_normalizes_parameter_names(self) -> None:
        templates = parse_templates("{{Infobox game| Download Link = https://example.test/a.apworld}}")
        self.assertEqual("https://example.test/a.apworld", templates[0].params["download_link"])

    def test_ignores_pipes_nested_in_links_and_templates(self) -> None:
        wikitext = "{{Infobox game| genre = [[Genre|Platformer]] | download = {{URL|https://example.test/x}} }}"
        params = parse_templates(wikitext)[0].params
        self.assertEqual("[[Genre|Platformer]]", params["genre"])
        self.assertEqual("{{URL|https://example.test/x}}", params["download"])

    def test_keeps_equals_signs_inside_values(self) -> None:
        templates = parse_templates("{{Infobox|download = https://example.test/a?b=c&d=e}}")
        self.assertEqual("https://example.test/a?b=c&d=e", templates[0].params["download"])

    def test_handles_positional_parameters(self) -> None:
        templates = parse_templates("{{Cite|first|second}}")
        self.assertEqual({"1": "first", "2": "second"}, templates[0].params)

    def test_returns_each_top_level_template(self) -> None:
        templates = parse_templates("{{A|x=1}} text {{B|y=2}}")
        self.assertEqual(["A", "B"], [template.name for template in templates])

    def test_survives_unbalanced_braces(self) -> None:
        self.assertEqual([], parse_templates("{{Infobox game| title = broken"))


class TestExtractFromWikitext(unittest.TestCase):
    def _page(self, wikitext: str) -> WikiPage:
        return WikiPage(title="Some Game", url="https://wiki.test/Some_Game", wikitext=wikitext)

    def test_prefers_the_infobox_download_parameter(self) -> None:
        page = self._page(
            "{{Infobox game\n"
            "| website = https://example.test/game\n"
            "| download = [https://github.com/owner/repo Releases]\n"
            "}}"
        )
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)
        self.assertEqual("infobox-param", candidate.strategy)
        self.assertEqual("high", candidate.confidence)
        self.assertEqual("download", candidate.label)

    def test_apworld_parameter_beats_a_generic_website(self) -> None:
        page = self._page("{{Infobox game| website = https://example.test/ | apworld = https://github.com/o/r }}")
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/o/r", candidate.url)
        self.assertEqual("apworld", candidate.label)

    def test_strips_trailing_punctuation(self) -> None:
        page = self._page("{{Infobox game| download = https://github.com/owner/repo.}}")
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)

    def test_non_infobox_template_is_lower_confidence(self) -> None:
        page = self._page("{{Game details| download = https://github.com/owner/repo }}")
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("medium", candidate.confidence)


class TestExtractFromHtml(unittest.TestCase):
    def _page(self, html: str, links: tuple[str, ...] = ()) -> WikiPage:
        return WikiPage(title="Some Game", url="https://wiki.test/Some_Game", html=html, external_links=links)

    def test_reads_the_download_row_of_the_ap_section(self) -> None:
        page = self._page(wiki_page_html(download_href="https://github.com/owner/repo"))
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)
        self.assertEqual("infobox-row", candidate.strategy)
        self.assertEqual("high", candidate.confidence)

    def test_accepts_other_row_labels(self) -> None:
        page = self._page(wiki_page_html(download_label="Source code"))
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)

    def test_ignores_internal_wiki_links_in_the_row(self) -> None:
        html = """
        <table class="infobox">
        <tr><th colspan="2">AP information</th></tr>
        <tr><th>Download</th><td><a href="/wiki/APWorld">apworld</a>
            <a href="https://github.com/owner/repo">GitHub</a></td></tr>
        </table>
        """
        candidate = extract_download_url(self._page(html))
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)

    def test_falls_back_to_a_link_after_an_ap_information_heading(self) -> None:
        html = """
        <h2>Game information</h2><p><a href="https://example.test/store">Buy it</a></p>
        <h2>AP information</h2>
        <p>Grab it from <a href="https://github.com/owner/repo/releases">the releases page</a>.</p>
        """
        candidate = extract_download_url(self._page(html))
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo/releases", candidate.url)
        self.assertEqual("section-link", candidate.strategy)

    def test_unlabelled_section_still_yields_a_medium_confidence_row(self) -> None:
        html = """
        <table class="infobox">
        <tr><th>Download</th><td><a href="https://github.com/owner/repo">GitHub</a></td></tr>
        </table>
        """
        candidate = extract_download_url(self._page(html))
        assert candidate is not None
        self.assertEqual("medium", candidate.confidence)

    def test_html_is_only_used_when_the_wikitext_has_nothing(self) -> None:
        page = WikiPage(
            title="Some Game",
            url="https://wiki.test/Some_Game",
            wikitext="{{Infobox game| download = https://github.com/from/wikitext }}",
            html=wiki_page_html(download_href="https://github.com/from/html"),
        )
        candidate = extract_download_url(page)
        assert candidate is not None
        self.assertEqual("https://github.com/from/wikitext", candidate.url)


class TestExtractFromExternalLinks(unittest.TestCase):
    def _page(self, *links: str) -> WikiPage:
        return WikiPage(title="Some Game", url="https://wiki.test/Some_Game", external_links=links)

    def test_ranks_a_repository_above_unrelated_links(self) -> None:
        candidate = extract_download_url(
            self._page("https://www.youtube.com/watch?v=x", "https://github.com/owner/repo")
        )
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo", candidate.url)
        self.assertEqual("extlinks", candidate.strategy)
        self.assertEqual("low", candidate.confidence)

    def test_prefers_a_direct_apworld_link(self) -> None:
        candidate = extract_download_url(
            self._page("https://github.com/owner/repo", "https://example.test/files/game.apworld")
        )
        assert candidate is not None
        self.assertEqual("https://example.test/files/game.apworld", candidate.url)

    def test_prefers_a_releases_page_over_a_bare_repository(self) -> None:
        candidate = extract_download_url(
            self._page("https://github.com/other/tool", "https://github.com/owner/repo/releases")
        )
        assert candidate is not None
        self.assertEqual("https://github.com/owner/repo/releases", candidate.url)

    def test_skips_core_archipelago_and_chat_links(self) -> None:
        page = self._page(
            "https://github.com/ArchipelagoMW/Archipelago",
            "https://discord.gg/invite",
            "https://archipelago.gg/tutorial",
        )
        self.assertIsNone(extract_download_url(page))

    def test_returns_none_when_nothing_looks_like_a_download(self) -> None:
        self.assertIsNone(extract_download_url(self._page("https://example.test/about")))


class TestWikiClient(unittest.TestCase):
    def test_category_members_follows_continuation(self) -> None:
        http = FakeHttp()
        pages = [
            {
                "query": {"categorymembers": [{"ns": 0, "title": "Game A"}, {"ns": 14, "title": "Category:Sub"}]},
                "continue": {"cmcontinue": "page|2", "continue": "-||"},
            },
            {"query": {"categorymembers": [{"ns": 0, "title": "Game B"}]}},
        ]
        responses = iter(pages)
        http.route("list=categorymembers", lambda _url: next(responses))

        titles = WikiClient(http).category_members()  # type: ignore[arg-type]
        self.assertEqual(["Game A", "Game B"], titles)
        self.assertIn("cmcontinue=page%7C2", http.requests[1])

    def test_category_members_can_descend_into_subcategories(self) -> None:
        http = FakeHttp()

        def handler(url: str) -> dict[str, object]:
            if "Category%3ASub" in url:
                return {"query": {"categorymembers": [{"ns": 0, "title": "Game B"}]}}
            return {"query": {"categorymembers": [{"ns": 0, "title": "Game A"}, {"ns": 14, "title": "Category:Sub"}]}}

        http.route("list=categorymembers", handler)
        client = WikiClient(http)  # type: ignore[arg-type]
        self.assertEqual(["Game A", "Game B"], client.category_members(recursive=True))

    def test_subcategories_are_ignored_without_recursion(self) -> None:
        http = FakeHttp()
        http.json_route(
            "list=categorymembers",
            {"query": {"categorymembers": [{"ns": 0, "title": "Game A"}, {"ns": 14, "title": "Category:Sub"}]}},
        )
        self.assertEqual(["Game A"], WikiClient(http).category_members())  # type: ignore[arg-type]

    def test_fetch_page_collects_all_three_representations(self) -> None:
        http = FakeHttp()
        http.json_route(
            "action=parse",
            {
                "parse": {
                    "title": "Some Game",
                    "wikitext": "{{Infobox game}}",
                    "text": "<p>hi</p>",
                    "externallinks": ["https://github.com/owner/repo"],
                }
            },
        )
        page = WikiClient(http).fetch_page("Some Game")  # type: ignore[arg-type]
        self.assertEqual("{{Infobox game}}", page.wikitext)
        self.assertEqual("<p>hi</p>", page.html)
        self.assertEqual(("https://github.com/owner/repo",), page.external_links)
        self.assertEqual("https://archipelago.miraheze.org/wiki/Some_Game", page.url)

    def test_api_errors_are_raised(self) -> None:
        from tools.custom_worlds.wiki import WikiApiError

        http = FakeHttp()
        http.json_route("action=parse", {"error": {"code": "missingtitle", "info": "The page does not exist."}})
        with self.assertRaises(WikiApiError):
            WikiClient(http).fetch_page("Nope")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
