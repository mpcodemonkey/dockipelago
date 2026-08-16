"""Reading the Archipelago wiki's custom game pages.

Two jobs live here: talking to the MediaWiki API (:class:`WikiClient`) and pulling the apworld
download link out of a page (:func:`extract_download_url`).

Wiki pages are hand-written, so the download link is not always in the same place. The extractor
tries three strategies in descending order of confidence, and records which one produced the answer
so a run's report shows how much to trust each result:

1. ``infobox-param`` - a download-ish parameter of an infobox template, read from the wikitext.
2. ``infobox-row``/``section-link`` - a download-ish row of the rendered infobox, or a link that
   follows an "AP information" heading.
3. ``extlinks`` - any code-hosting link on the page, ranked. This is the guess of last resort.
"""

import logging
import re
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .http import HttpClient

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://archipelago.miraheze.org/w/api.php"
DEFAULT_CATEGORY = "Category:Custom games"

#: Infobox parameter names that hold the apworld download, most specific first.
DOWNLOAD_PARAM_NAMES: tuple[str, ...] = (
    "apworld",
    "apworld_link",
    "apworld_download",
    "download",
    "downloads",
    "download_link",
    "download_page",
    "releases",
    "release",
    "repository",
    "repo",
    "source",
    "source_code",
    "github",
    "git",
    "link",
    "url",
    "website",
)

#: Row labels / link texts that mark the download in rendered HTML.
DOWNLOAD_LABEL_RE = re.compile(r"download|apworld|repositor|source|releases?\b|github", re.IGNORECASE)

#: The heading that separates the Archipelago-specific half of a game infobox.
AP_SECTION_RE = re.compile(r"\bap\b[\s_-]*(information|info)\b|archipelago[\s_-]*information", re.IGNORECASE)

# Quotes are allowed inside the URL, because GitHub's own filtered-releases links contain them:
# /releases?q="Mega Man X"&expanded=true. A quote that really was a delimiter ends up at the end,
# where the trailing-punctuation strip removes it.
_URL_RE = re.compile(r"https?://[^\s\]\}\|<>]+")
_TRAILING_PUNCTUATION = ".,;:!?'\")"

#: Hosts that are never an apworld download, so the ranked fallback should not pick them.
IGNORED_HOSTS = frozenset({
    "archipelago.gg",
    "archipelago.miraheze.org",
    "discord.com",
    "discord.gg",
    "discordapp.com",
    "en.wikipedia.org",
    "reddit.com",
    "steamcommunity.com",
    "store.steampowered.com",
    "twitch.tv",
    "www.reddit.com",
    "www.twitch.tv",
    "www.youtube.com",
    "youtu.be",
    "youtube.com",
})

#: Repositories that are core Archipelago rather than a custom world.
IGNORED_REPOS = frozenset({"archipelagomw/archipelago"})

_CODE_HOSTS = ("github.com", "www.github.com", "gitlab.com", "www.gitlab.com", "codeberg.org")


@dataclass(frozen=True)
class WikiPage:
    """One custom game page, with everything the extractor needs."""

    title: str
    url: str
    wikitext: str = ""
    html: str = ""
    external_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownloadCandidate:
    """A link that looks like it leads to the game's apworld."""

    url: str
    strategy: str
    confidence: str
    label: str = ""


class WikiApiError(Exception):
    """The MediaWiki API reported an error."""


class WikiClient:
    """Talks to a MediaWiki install over its ``api.php`` endpoint."""

    def __init__(self, http: HttpClient, api_url: str = DEFAULT_API_URL) -> None:
        self.http = http
        self.api_url = api_url

    def page_url(self, title: str) -> str:
        base = self.api_url.rsplit("/", 1)[0].rsplit("/w", 1)[0]
        return f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

    def category_members(
        self,
        category: str = DEFAULT_CATEGORY,
        *,
        recursive: bool = False,
        max_depth: int = 2,
    ) -> list[str]:
        """Return the article titles in ``category``, optionally descending into subcategories."""
        titles: list[str] = []
        seen_titles: set[str] = set()
        seen_categories: set[str] = set()
        queue: list[tuple[str, int]] = [(category, 0)]

        while queue:
            current, depth = queue.pop(0)
            if current in seen_categories:
                continue
            seen_categories.add(current)

            for member in self._category_page(current):
                if member["ns"] == 14:  # a subcategory
                    if recursive and depth < max_depth:
                        queue.append((member["title"], depth + 1))
                elif member["ns"] == 0 and member["title"] not in seen_titles:
                    seen_titles.add(member["title"])
                    titles.append(member["title"])

        return titles

    def fetch_page(self, title: str) -> WikiPage:
        """Fetch the wikitext, rendered HTML and external links of ``title`` in one API round trip."""
        payload = self._api(
            action="parse",
            page=title,
            prop="text|wikitext|externallinks",
            redirects="1",
        )
        parsed = payload.get("parse", {})
        resolved_title = parsed.get("title", title)
        return WikiPage(
            title=resolved_title,
            url=self.page_url(resolved_title),
            wikitext=_as_text(parsed.get("wikitext")),
            html=_as_text(parsed.get("text")),
            external_links=tuple(parsed.get("externallinks") or ()),
        )

    def _category_page(self, category: str) -> Iterator[dict[str, Any]]:
        params: dict[str, str] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmlimit": "500",
            "cmtype": "page|subcat",
        }
        while True:
            payload = self._api(**params)
            yield from payload.get("query", {}).get("categorymembers", [])
            continuation = payload.get("continue")
            if not continuation:
                return
            params.update({key: str(value) for key, value in continuation.items()})

    def _api(self, **params: str) -> dict[str, Any]:
        query = {"format": "json", "formatversion": "2", **params}
        url = f"{self.api_url}?{urllib.parse.urlencode(query)}"
        payload = self.http.get_json(url)
        if not isinstance(payload, dict):
            raise WikiApiError(f"unexpected API response for {url}")
        if "error" in payload:
            error = payload["error"]
            raise WikiApiError(f"{error.get('code', 'error')}: {error.get('info', error)} ({url})")
        return payload


#: Infobox parameters that name the game, checked before falling back to the page title.
GAME_NAME_PARAMS: tuple[str, ...] = ("game", "game_name", "title", "name")

_DISAMBIGUATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def extract_game_name(page: WikiPage) -> str:
    """The name of the game this page is about.

    Used to tell one maintainer's games apart when they share a repository, so it wants to be the
    game's real name rather than the article's. The infobox usually carries it verbatim; the page
    title is the fallback, minus any ``(disambiguation)`` suffix the wiki added to make it unique.
    """
    for template in parse_templates(page.wikitext):
        if "infobox" not in template.name.lower():
            continue
        for param in GAME_NAME_PARAMS:
            value = template.params.get(param, "").strip()
            # Skip values that are wiki markup or a link rather than a plain name.
            if value and not value.startswith(("[", "{", "<")) and "://" not in value:
                return value
    return _DISAMBIGUATION_RE.sub("", page.title).strip() or page.title


def extract_download_url(page: WikiPage) -> DownloadCandidate | None:
    """Find the most plausible apworld download link on ``page``."""
    for finder in (_from_infobox_params, _from_rendered_html, _from_external_links):
        candidate = finder(page)
        if candidate is not None:
            return candidate
    return None


# --------------------------------------------------------------------------------------
# Strategy 1: infobox template parameters in the wikitext
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    """A parsed ``{{...}}`` invocation."""

    name: str
    params: dict[str, str]


def parse_templates(wikitext: str) -> list[Template]:
    """Parse top-level template invocations out of ``wikitext``.

    Nested templates are kept verbatim inside their parent's parameter values rather than being
    returned separately; infobox parameters are what we are after and those live at the top level.
    """
    templates: list[Template] = []
    for start, end in _iter_template_spans(wikitext):
        body = wikitext[start + 2:end]
        parts = _split_top_level(body, "|")
        if not parts:
            continue
        name = parts[0].strip()
        params: dict[str, str] = {}
        for index, part in enumerate(parts[1:], start=1):
            key, _, value = _partition_top_level(part, "=")
            if value is None:
                params[str(index)] = part.strip()
            else:
                params[_normalize_param(key)] = value.strip()
        templates.append(Template(name=name, params=params))
    return templates


def _from_infobox_params(page: WikiPage) -> DownloadCandidate | None:
    templates = parse_templates(page.wikitext)
    infoboxes = [template for template in templates if "infobox" in template.name.lower()]

    for candidates, confidence in ((infoboxes, "high"), (templates, "medium")):
        for param_name in DOWNLOAD_PARAM_NAMES:
            for template in candidates:
                value = template.params.get(param_name)
                if not value:
                    continue
                urls = _urls_in(value)
                if urls:
                    return DownloadCandidate(
                        url=urls[0],
                        strategy="infobox-param",
                        confidence=confidence,
                        label=param_name,
                    )
    return None


def _iter_template_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` offsets of outermost ``{{ }}`` pairs."""
    depth = 0
    start = -1
    index = 0
    while index < len(text) - 1:
        pair = text[index:index + 2]
        if pair == "{{":
            if depth == 0:
                start = index
            depth += 1
            index += 2
            continue
        if pair == "}}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield start, index
            index += 2
            continue
        index += 1


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split ``text`` on ``separator``, ignoring separators nested inside wiki syntax."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        pair = text[index:index + 2]
        if pair in ("{{", "[[", "{|"):
            depth += 1
            current.append(pair)
            index += 2
            continue
        if pair in ("}}", "]]", "|}"):
            depth = max(depth - 1, 0)
            current.append(pair)
            index += 2
            continue
        char = text[index]
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _partition_top_level(text: str, separator: str) -> tuple[str, str | None, str | None]:
    parts = _split_top_level(text, separator)
    if len(parts) < 2:
        return text, None, None
    return parts[0], separator, separator.join(parts[1:])


def _normalize_param(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _urls_in(value: str) -> list[str]:
    """Pull URLs out of a wikitext fragment, handling ``[url label]`` and ``{{URL|url}}`` forms."""
    urls: list[str] = []
    for match in _URL_RE.finditer(value):
        url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if url and url not in urls:
            urls.append(url)
    return urls


# --------------------------------------------------------------------------------------
# Strategy 2: the rendered infobox / an "AP information" section
# --------------------------------------------------------------------------------------


@dataclass
class _Link:
    href: str
    text: str
    order: int


@dataclass
class _Cell:
    is_header: bool
    text: str = ""
    links: list[_Link] = field(default_factory=list)


@dataclass
class _Row:
    cells: list[_Cell] = field(default_factory=list)
    order: int = 0


class _PageParser(HTMLParser):
    """Collects the document structure the extractor cares about: rows, links and headings."""

    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _CELL_TAGS = frozenset({"th", "td"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_Row] = []
        self.links: list[_Link] = []
        self.markers: list[tuple[str, int]] = []
        self._order = 0
        self._row: _Row | None = None
        self._cell: _Cell | None = None
        self._link: _Link | None = None
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._order += 1
        if tag == "tr":
            self._row = _Row(order=self._order)
        elif tag in self._CELL_TAGS:
            self._cell = _Cell(is_header=tag == "th")
            if self._row is None:  # a cell outside any row we saw; keep it anyway
                self._row = _Row(order=self._order)
        elif tag in self._HEADING_TAGS:
            self._heading = []
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href:
                self._link = _Link(href=href, text="", order=self._order)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self._link.text = self._link.text.strip()
            self.links.append(self._link)
            if self._cell is not None:
                self._cell.links.append(self._link)
            self._link = None
        elif tag in self._CELL_TAGS and self._cell is not None:
            self._cell.text = _collapse(self._cell.text)
            if self._row is not None:
                self._row.cells.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row.cells:
                self.rows.append(self._row)
            self._row = None
        elif tag in self._HEADING_TAGS and self._heading is not None:
            self.markers.append((_collapse("".join(self._heading)), self._order))
            self._heading = None

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link.text += data
        if self._cell is not None:
            self._cell.text += data
        if self._heading is not None:
            self._heading.append(data)

    def close(self) -> None:
        super().close()
        # Unclosed trailing markup still carries useful rows.
        if self._cell is not None and self._row is not None:
            self._cell.text = _collapse(self._cell.text)
            self._row.cells.append(self._cell)
        if self._row is not None and self._row.cells:
            self.rows.append(self._row)


def _from_rendered_html(page: WikiPage) -> DownloadCandidate | None:
    if not page.html:
        return None
    parser = _PageParser()
    parser.feed(page.html)
    parser.close()

    section_rows = _rows_by_section(parser.rows)
    # Infobox section headers ("AP information") also register as markers for the link fallback.
    markers = list(parser.markers)
    markers.extend(
        (row.cells[0].text, row.order) for row in parser.rows if len(row.cells) == 1 and row.cells[0].text
    )

    # A labelled row inside the AP information section is the intended answer.
    for section, row in section_rows:
        if not AP_SECTION_RE.search(section):
            continue
        link = _download_link_in_row(row)
        if link is not None:
            return DownloadCandidate(
                url=link.href,
                strategy="infobox-row",
                confidence="high",
                label=row.cells[0].text,
            )

    # Same row shape, but the page did not label the section.
    for _section, row in section_rows:
        link = _download_link_in_row(row)
        if link is not None:
            return DownloadCandidate(
                url=link.href,
                strategy="infobox-row",
                confidence="medium",
                label=row.cells[0].text,
            )

    # Prose layout: the first download-ish link after an "AP information" heading.
    ap_marker_order = next((order for text, order in markers if AP_SECTION_RE.search(text)), None)
    if ap_marker_order is not None:
        for link in parser.links:
            if link.order <= ap_marker_order or not _is_external(link.href):
                continue
            if DOWNLOAD_LABEL_RE.search(link.text) or _rank_url(link.href) > 0:
                return DownloadCandidate(
                    url=link.href,
                    strategy="section-link",
                    confidence="medium",
                    label=link.text,
                )
    return None


def _rows_by_section(rows: Sequence[_Row]) -> list[tuple[str, _Row]]:
    """Pair each data row with the most recent single-cell header row above it."""
    result: list[tuple[str, _Row]] = []
    section = ""
    for row in rows:
        if len(row.cells) == 1:
            section = row.cells[0].text
            continue
        result.append((section, row))
    return result


def _download_link_in_row(row: _Row) -> _Link | None:
    label = row.cells[0].text
    if not DOWNLOAD_LABEL_RE.search(label):
        return None
    for cell in row.cells[1:]:
        for link in cell.links:
            if _is_external(link.href):
                return link
    # Some infoboxes put the link on the label itself.
    for link in row.cells[0].links:
        if _is_external(link.href):
            return link
    return None


def _is_external(href: str) -> bool:
    return href.startswith(("http://", "https://", "//"))


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):  # formatversion=1 wraps these in {"*": "..."}
        return str(value.get("*", ""))
    return ""


# --------------------------------------------------------------------------------------
# Strategy 3: rank every external link on the page
# --------------------------------------------------------------------------------------


def _from_external_links(page: WikiPage) -> DownloadCandidate | None:
    best: tuple[int, str] | None = None
    for url in page.external_links:
        rank = _rank_url(url)
        if rank > 0 and (best is None or rank > best[0]):
            best = (rank, url)
    if best is None:
        return None
    return DownloadCandidate(url=best[1], strategy="extlinks", confidence="low", label="")


def _rank_url(url: str) -> int:
    """Score a link by how likely it is to be an apworld download. 0 means "not a candidate"."""
    try:
        parts = urllib.parse.urlsplit(url if "//" in url else f"//{url}")
    except ValueError:
        return 0
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if not host or host in IGNORED_HOSTS:
        return 0

    path = parts.path
    segments = [segment for segment in path.split("/") if segment]

    if path.lower().endswith(".apworld"):
        return 100

    if host in _CODE_HOSTS and len(segments) >= 2:
        owner_repo = f"{segments[0].lower()}/{segments[1].lower().removesuffix('.git')}"
        if owner_repo in IGNORED_REPOS:
            return 0
        if len(segments) >= 3 and segments[2] == "releases":
            return 90
        if len(segments) == 2:
            return 80
        return 60

    return 0
