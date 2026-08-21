"""Building the description Docker Hub shows above the tags list.

Docker Hub's overview is a single Markdown blob with a **25,000 character limit**, and this image
ships several hundred custom worlds. A table naming each one, its version and its source repository
comes to roughly 62,000 characters at 580 worlds, so the full list cannot live here at all.

What goes in instead is what a reader of the Docker Hub page actually needs and cannot get anywhere
else: which Archipelago this was built from, how many worlds came with it, and - the part that is
genuinely particular to this image - which of those worlds had boilerplate written for them by the
crawler rather than shipping it themselves. The exhaustive list is one link away, in the lockfile,
which is generated anyway and never goes stale.

Nothing here talks to Docker Hub. This writes Markdown to a file; publishing it is the workflow's
job, which keeps the generator runnable and testable without credentials.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Docker Hub rejects a description longer than this.
DESCRIPTION_LIMIT = 25_000

#: Where the upstream project lives, for linking the commit this was built from.
UPSTREAM = "ArchipelagoMW/Archipelago"

#: What the crawler's repair codes mean to somebody reading a Docker Hub page.
_REPAIRS = {
    "web-missing": "WebWorld class",
    "web-no-tutorials": "tutorial block",
    "docs-missing": "setup guide",
}


@dataclass(frozen=True)
class Base:
    """The upstream commit this image was built on top of."""

    commit: str = ""
    subject: str = ""
    date: str = ""

    def __bool__(self) -> bool:
        return bool(self.commit)


def detect_base(root: Path, upstream_ref: str = "upstream/main") -> Base:
    """The newest upstream commit this checkout shares, or an empty Base if it cannot be told.

    A fork's merge base is the honest answer to "which Archipelago is this?", but it needs upstream's
    refs to compute, and a CI checkout has only its own. Rather than guess, this returns nothing when
    it cannot tell and the page simply omits the claim - a description that quietly names the wrong
    commit would be worse than one that names none.
    """

    def git(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = git("merge-base", "HEAD", upstream_ref)
    if not commit:
        return Base()
    return Base(
        commit=commit,
        subject=git("log", "-1", "--format=%s", commit),
        date=git("log", "-1", "--format=%ad", "--date=short", commit),
    )


def read_lockfile(path: Path) -> list[dict[str, object]]:
    """The installed worlds recorded by the last crawl, or an empty list if there is no lockfile."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    worlds = payload.get("worlds") if isinstance(payload, dict) else None
    if not isinstance(worlds, list):
        return []
    return [world for world in worlds if isinstance(world, dict) and world.get("file")]


def build(
    worlds: list[dict[str, object]],
    base: Base,
    *,
    image: str,
    repository: str,
    branch: str = "main",
    limit: int = DESCRIPTION_LIMIT,
) -> str:
    """Assemble the description, trimming the repaired list if it would not fit."""
    lockfile_url = f"https://github.com/{repository}/blob/{branch}/custom_worlds.lock.json"
    repaired = _repaired(worlds)

    head = _head(worlds, base, image=image, repository=repository, branch=branch, lockfile_url=lockfile_url)
    tail = _tail(repository, branch)

    # Only the repaired table can grow without bound, so it is the only thing that gets trimmed.
    room = limit - len(head) - len(tail)
    body, shown = _repaired_section(repaired, room, lockfile_url)
    page = head + body + tail
    if len(page) > limit:  # nothing left to give: drop the section rather than be rejected
        page = head + _repaired_summary(len(repaired), shown, lockfile_url) + tail
    return page


def _head(
    worlds: list[dict[str, object]],
    base: Base,
    *,
    image: str,
    repository: str,
    branch: str,
    lockfile_url: str,
) -> str:
    lines = [
        "# dockipelago",
        "",
        "Archipelago's WebHost, with the community's custom worlds already installed.",
        "",
        "```bash",
        f"docker pull {image}:nightly",
        "```",
        "",
        "## What is in this image",
        "",
    ]
    if base:
        commit_url = f"https://github.com/{UPSTREAM}/commit/{base.commit}"
        built_on = f"- **Archipelago** at [`{base.commit[:9]}`]({commit_url})"
        if base.subject:
            built_on += f" — {base.subject}"
        if base.date:
            built_on += f" ({base.date})"
        lines.append(built_on)
    else:
        lines.append(f"- **Archipelago**, forked from [{UPSTREAM}](https://github.com/{UPSTREAM})")
    lines += [
        f"- **{len(worlds)} custom worlds** from the wiki's "
        "[Custom games](https://archipelago.miraheze.org/wiki/Category:Custom_games) category, "
        "on top of the games Archipelago ships with.",
        "",
        "Every world is checked against this Archipelago version before it is installed, so what is "
        "here is what the WebHost will actually serve. The full list — each world with the "
        f"repository and release tag it came from — is in [`custom_worlds.lock.json`]({lockfile_url}), "
        "which is far too long for this page.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _repaired(worlds: list[dict[str, object]]) -> list[tuple[str, str, str]]:
    """Worlds the crawler had to write boilerplate for, as (game, version, what was written)."""
    rows = []
    for world in worlds:
        codes = world.get("repaired")
        if not isinstance(codes, list) or not codes:
            continue
        written = ", ".join(_REPAIRS.get(str(code), str(code)) for code in sorted(codes))
        game = str(world.get("game") or world.get("title") or "")
        rows.append((game, str(world.get("world_version") or ""), written))
    return sorted(rows)


def _repaired_section(
    repaired: list[tuple[str, str, str]], room: int, lockfile_url: str
) -> tuple[str, int]:
    """The repaired table, cut to whatever room is left, and how many rows it shows."""
    if not repaired:
        return "", 0

    intro = (
        "## Worlds this image completed\n"
        "\n"
        f"{len(repaired)} of them arrived without the boilerplate the WebHost insists on — a "
        "`WebWorld` class, a tutorial block, a setup guide — and would otherwise have been dropped "
        "from the site. The crawler wrote the missing part rather than turning the world away. "
        "Nothing else about them was changed.\n"
        "\n"
        "| Game | Version | Written for it |\n"
        "| --- | --- | --- |\n"
    )
    rows = [f"| {game} | {version} | {written} |\n" for game, version, written in repaired]

    shown, used = 0, len(intro)
    for row in rows:
        if used + len(row) > room - 200:  # keep room for the "and N more" line
            break
        used += len(row)
        shown += 1
    if shown == 0:
        return _repaired_summary(len(repaired), 0, lockfile_url), 0

    section = intro + "".join(rows[:shown])
    if shown < len(repaired):
        section += f"\n_and {len(repaired) - shown} more — see [the lockfile]({lockfile_url})._\n"
    return section + "\n", shown


def _repaired_summary(total: int, shown: int, lockfile_url: str) -> str:
    """A one-liner for when even a trimmed table will not fit."""
    if not total:
        return ""
    del shown
    return (
        "## Worlds this image completed\n"
        "\n"
        f"{total} worlds arrived without the boilerplate the WebHost insists on and had the missing "
        f"part written for them. Which ones, and what was written, is recorded in "
        f"[the lockfile]({lockfile_url}).\n"
        "\n"
    )


def _tail(repository: str, branch: str) -> str:
    return "\n".join(
        [
            "## Running it",
            "",
            "```bash",
            f"docker run --rm -p 80:80 {repository.rsplit('/', 1)[-1]}",
            "```",
            "",
            "Serves the WebHost on port 80. It is upstream's `Dockerfile` unmodified — only the "
            "contents of `worlds/` differ.",
            "",
            "## Documentation",
            "",
            f"- [Source and README](https://github.com/{repository})",
            f"- [How the worlds get here](https://github.com/{repository}/blob/{branch}/docs/"
            "custom%20worlds%20crawler.md)",
            f"- [Archipelago itself](https://github.com/{UPSTREAM})",
            "",
        ]
    )
