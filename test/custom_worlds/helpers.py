"""Fixtures shared by the crawler tests: a scripted HTTP client and an apworld builder."""

import json
import urllib.parse
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tools.custom_worlds.http import HttpError

Route = Callable[[str], Any]


class FakeHttp:
    """Stands in for :class:`tools.custom_worlds.http.HttpClient`.

    Routes are matched by substring, in insertion order, so a test can register a broad handler and
    then override a specific URL by registering it first.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, Route]] = []
        self.requests: list[str] = []

    def route(self, fragment: str, handler: Route | Any) -> "FakeHttp":
        if not callable(handler):
            value = handler
            handler = lambda _url: value  # noqa: E731 - a constant response
        self.routes.append((fragment, handler))
        return self

    def json_route(self, fragment: str, payload: Any) -> "FakeHttp":
        return self.route(fragment, json.dumps(payload).encode("utf-8"))

    def get(self, url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int | None = None) -> bytes:
        self.requests.append(url)
        for fragment, handler in self.routes:
            if fragment in url:
                result = handler(url)
                if isinstance(result, HttpError):
                    raise result
                body = result if isinstance(result, bytes) else json.dumps(result).encode("utf-8")
                if max_bytes is not None and len(body) > max_bytes:
                    raise HttpError(url, None, "too large")
                return body
        raise HttpError(url, 404, "no route registered in the test")

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        return json.loads(self.get(url, headers=headers))


def make_apworld(
    path: Path,
    *,
    module: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    manifest_at_root: bool = False,
    init_source: str = "# test world\n",
    extra_files: Mapping[str, str] | None = None,
    docs: bool = True,
) -> Path:
    """Write a syntactically valid ``.apworld`` at ``path`` and return it.

    ``docs`` ships the ``docs/`` folder a published world carries, because the WebHost lists that
    folder at start-up for any world with tutorials. It defaults on so a fixture looks like a real
    world; the tests for the missing-folder case turn it off.
    """
    module = module if module is not None else path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if module:
            archive.writestr(f"{module}/__init__.py", init_source)
            supplied = any(name.startswith(f"{module}/docs/") for name in (extra_files or {}))
            if docs and not supplied:
                archive.writestr(f"{module}/docs/setup_en.md", "# Setup\n")
        if manifest is not None:
            location = "archipelago.json" if manifest_at_root else f"{module}/archipelago.json"
            archive.writestr(location, json.dumps(manifest))
        for name, content in (extra_files or {}).items():
            archive.writestr(name, content)
    return path


def default_manifest(game: str = "Test Game", **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "game": game,
        "world_version": "1.0.0",
        "minimum_ap_version": "0.6.0",
        "version": 7,
        "compatible_version": 7,
    }
    manifest.update(overrides)
    return manifest


def release(
    tag: str,
    *asset_names: str,
    published_at: str = "2026-01-01T00:00:00Z",
    prerelease: bool = False,
    draft: bool = False,
    repo: str = "owner/repo",
) -> dict[str, Any]:
    """Build a GitHub release payload of the shape the API returns."""
    return {
        "tag_name": tag,
        "name": tag,
        "published_at": published_at,
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "name": name,
                "size": 1024,
                "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
            }
            for name in asset_names
        ],
    }


def wiki_page_html(
    *,
    title: str = "Some Game",
    download_href: str = "https://github.com/owner/repo",
    section: str = "AP information",
    download_label: str = "Download",
) -> str:
    """Render the infobox shape a MediaWiki game page produces."""
    return f"""
    <div class="mw-parser-output">
    <table class="infobox">
      <tbody>
        <tr><th colspan="2" class="infobox-title">{title}</th></tr>
        <tr><th colspan="2">Game information</th></tr>
        <tr><th>Developer</th><td>Some Studio</td></tr>
        <tr><th>Release date</th><td>2011</td></tr>
        <tr><th colspan="2">{section}</th></tr>
        <tr><th>Maintainer</th><td><a href="/wiki/User:Someone">Someone</a></td></tr>
        <tr><th>{download_label}</th>
            <td><a rel="nofollow" class="external text" href="{download_href}">GitHub</a></td></tr>
      </tbody>
    </table>
    <p>{title} is a game. See the <a href="/wiki/Custom_games">custom games</a> list.</p>
    </div>
    """


def api_url_for(**params: str) -> str:
    return "https://archipelago.miraheze.org/w/api.php?" + urllib.parse.urlencode(params)
