# Custom Worlds Crawler

`tools/crawl_custom_worlds.py` populates this fork's `worlds/` folder with the community worlds
listed in the Archipelago wiki's
[Custom games](https://archipelago.miraheze.org/wiki/Category:Custom_games) category, so that a
Docker image built from this checkout ships with them.

It is one pass over the category, one game at a time:

```
wiki category -> game page -> download link -> GitHub release -> .apworld -> verify -> worlds/
```

Nothing reaches `worlds/` until it has been checked against this checkout's Archipelago version and
against the other worlds in the same run.

## Quick start

```bash
# See what the wiki currently offers without writing anything.
python tools/crawl_custom_worlds.py --dry-run

# Install everything it can into worlds/.
python tools/crawl_custom_worlds.py

# Install, then confirm core actually loads the result.
python tools/crawl_custom_worlds.py --import-check
```

The script only needs the Python standard library, so it can run before Archipelago's own
dependencies are installed.

### Set a GitHub token

Unauthenticated GitHub API access is capped at 60 requests an hour, and the category holds far more
games than that. Export a token — any classic or fine-grained token with public read access will do:

```bash
export GITHUB_TOKEN=ghp_...
python tools/crawl_custom_worlds.py
```

`GH_TOKEN` and `--github-token` work too. The token is sent only to `api.github.com`; it is stripped
when a release download redirects to GitHub's storage host.

## How the download link is found

Wiki pages are written by hand, so the link is not always in the same place. Three strategies are
tried in order and the one that answered is recorded on each result, along with a confidence:

| Strategy | Confidence | What it reads |
| --- | --- | --- |
| `infobox-param` | high / medium | a `download`, `apworld`, `repository`, … parameter of an infobox template in the page's wikitext |
| `infobox-row` | high / medium | a `Download` / `Source` row of the rendered infobox, preferring the one under the **AP information** heading |
| `section-link` | medium | the first repository-looking link after an "AP information" heading, for pages that use prose instead of a table |
| `extlinks` | low | any code-hosting link on the page, ranked. A guess of last resort |

Anything resolved at `low` confidence is listed separately at the end of a run under "Download link
was guessed rather than read from the infobox", so it can be spot checked. Links to core
Archipelago, Discord, YouTube and similar are never treated as candidates.

From there the link is turned into a file: a repository URL is resolved through its releases, and a
link that already points at a tag, a release asset or an `.apworld` file is used directly.

## Repositories that publish more than one game

Plenty of maintainers look after several worlds and publish all of them from one repository, so its
release feed interleaves unrelated games. A page for ActRaiser can point at a repository whose three
most recent releases are Rune Factory, Sonic Battle and Rune Factory again — "newest release with an
`.apworld` in it" fetches the wrong game almost every time.

### When the link already says which releases apply

Some pages link to a *filtered* releases page rather than the repository, which settles the question
before any guessing starts:

```
https://github.com/TheLX5/Archipelago/releases?q="Mega+Man+X"&expanded=true
```

That `q=` is the maintainer's own answer about which releases belong to this game, and it beats
anything inferred from a name. It is read off the URL and applied as a **hard filter** before
ranking, so everything below only ever runs on what the filter left — and usually has nothing left
to decide. Matching is on the normalised release title and tag, the way the GitHub page itself
behaves, so `"Mega Man X"` finds both `Mega Man X1 v1.4` and `megamanx-1.4`.

This also reaches worlds that name-matching alone would refuse: `smw.apworld` is an abbreviation the
matcher cannot connect to a page called "Super Mario World", but a link filtered on `smw` finds it.

A filter that matches nothing is treated as stale rather than as an answer — the releases were
probably renamed — and the unfiltered feed is used instead, with a note.

### Otherwise

A repository is classified first. If every `.apworld` it publishes has the same name, it is a
one-world repository and its newest release wins, whatever anything is called. If it publishes
several, every release becomes a candidate and each is scored against the game the page is about:

1. **The asset file name** is the strongest signal — `actraiser.apworld` against ActRaiser.
2. **The release tag and title** are worth slightly less, since they often carry only a version
   number or the maintainer's own scheme — `actraiser-1.1.0` still matches, `v2.0.0` tells us
   nothing.
3. **The manifest's `game` field**, once the file has been downloaded, is authoritative. A name-based
   pick is provisional until the manifest confirms it; if it names a different game, that candidate
   is rejected and the next-best is tried.

Matching is fuzzy in the ways these names actually vary — case, separators, camel case, version
suffixes and `_apworld` suffixes are all absorbed, so `ActRaiser`, `act_raiser` and
`actraiser-v1.2.0` are one game while `sonic_battle` is plainly not. A shared fragment is not enough
on its own: a short name buried inside a longer one scores low, because most of the longer name is
then unaccounted for.

Numbered series get special treatment, because `Mega Man X1` and `Mega Man X2` share every word that
matters and would otherwise rate as nearly the same game. Version stamps are stripped first — without
that, the `1` in `Mega Man X2 v1.1` makes it look like it contains the very token that should have
singled out X1 — and then, when both names carry a series number and the numbers disagree, they are
treated as different games no matter how much else matches. A number on only one side is left alone:
a page called "Rune Factory" may well be describing "Rune Factory 5".

**A multi-game repository with nothing matching yields nothing.** The page is reported as failed,
naming the games the repository does publish, rather than installing a confident guess. That is the
whole point — a wrong world is worse than a missing one, because it silently claims a `game` name in
the datapackage that belongs to something else.

The game the page is about comes from the infobox's `game`/`title` parameter where there is one, and
otherwise from the article title minus any `(disambiguation)` suffix.

A one-world repository is deliberately *not* second-guessed: if its manifest names something other
than the page title that is naming drift, not a mixup, so it installs with a warning. Use
`--ignore-game-mismatch` to disable the rejection entirely if the filter misfires on an unusually
named world — for example one whose asset is an abbreviation, like `smw.apworld` for Super Mario
World.

When a release holds several `.apworld` assets, the best match wins and the rest are noted.
`--all-assets` installs every asset in the *chosen* release instead — other releases, meaning other
games, are still left alone, and those extra assets are exempt from the game check since asking for
all of them is explicit.

## What "verified" means

Each downloaded file is checked against the rules `worlds/__init__.py` and `worlds/Files.py` apply at
import time, so anything installed is something core will accept. Results fall into four statuses:

- **ok** — no problems.
- **warning** — installed, but worth knowing about: no `archipelago.json` (which stops working in
  Archipelago 0.7.0), no `game` field, no `compatible_version` (so it was not packaged with the
  "Build APWorlds" launcher component), a file name that is not all lower case, or a world shipping
  only compiled bytecode.
- **incompatible** — the world declares that it does not work here: `minimum_ap_version` above this
  checkout's version, `maximum_ap_version` below it, or a `compatible_version` newer than the
  APContainer format this checkout understands. Not installed.
- **invalid** — a corrupt zip, a missing or mismatched inner world folder (the zip must contain
  `<name>/__init__.py`, matching the file name exactly, because that is what `zipimport` looks for),
  or an unparseable manifest. Not installed.

The Archipelago version and APContainer version are read out of `Utils.py` and `worlds/Files.py`, so
the checks follow the checkout rather than a hard-coded number.

### WebWorld wiring

A world can satisfy every check above and still be useless here, because the manifest says nothing
about the code. These are read straight out of `<name>/__init__.py` with `ast` — the source is
parsed, never imported — and fall into two groups.

**Archipelago will not load the world at all.** Always rejected:

| In the source | What happens |
| --- | --- |
| `web = MyGameWeb` — the class, not an instance | `AutoWorldRegister` asserts `isinstance(dct["web"], WebWorld)`, so importing raises `AssertionError: WebWorld has to be instantiated.` |
| `web = MyGameWeb()` where `MyGameWeb` does not exist | `NameError` at import |
| the file is not valid Python | `SyntaxError`, which aborts `import worlds` entirely rather than being caught per-world |

**The world loads, but the WebHost drops it.** `WebHost.py` filters its world list with
`hasattr(world.web, "tutorials")`, logs

```
Following worlds not loaded as they are invalid for WebHost: {'Some Game'}
```

and removes the world from both `AutoWorldRegister.world_types` and the network data package — so
the game does not appear on the site at all. Two shapes fail it:

| In the source | Why |
| --- | --- |
| no `web` attribute | the world inherits `World.web = WebWorld()`, and the base `WebWorld` declares `tutorials` as a bare annotation with no value |
| a `WebWorld` with no `tutorials` | same outcome, one level down |

A valid WebWorld therefore needs a tutorial block:

```python
class MyGameWeb(WebWorld):
    setup = Tutorial(
        tutorial_name="Setup Guide",
        description="A guide to setting up the game",
        language="English",
        file_name="setup.md",
        link="setup/en",
        authors=["Someone"],
    )
    tutorials = [setup]
```

Because the source is parsed rather than read, a commented-out `tutorials` block is indistinguishable
from one that was never written — which is the right answer, since that is exactly how Python sees it
too. `tutorials = []` is accepted, because core accepts it. `tutorials = Tutorial(...)` without the
brackets is rejected, since the WebHost iterates the attribute.

These are refused by default: a world the WebHost will not serve is dead weight in an image built to
serve exactly that. `--webhost-check warn` installs them anyway and lists them at the end of the run,
and `--webhost-check off` skips the question entirely. Neither setting can rescue a world from the
first group.

Refusing them also means **taking back out** any copy an earlier run installed — see
[Rejected worlds](#rejected-worlds) below.

### The whole world is read, not just `__init__.py`

Splitting `World` and `WebWorld` across modules is normal — `from .web import MyGameWeb` — and a
one-file analysis cannot see whether the `tutorials` list on the other side exists. An apworld
carries its entire package, so relative imports are followed inside it:

- modules reachable from `__init__` are scanned for `World` subclasses, so a world whose class lives
  in `world.py` (or arrives via `from .world import *`) is still found;
- `web = MyGameWeb()` resolves across modules, and the `WebWorld` it names is checked wherever it
  lives, including through base classes in yet another module;
- subpackages are followed too, so `from .sub.web import MyGameWeb` works.

**Only provable problems are reported.** Names imported from outside the world, base classes in
another package, `tutorials` built by a function call, an instance built at module level, and
`MyGameWorld.web` patched on after the class body all stay quiet, because a false positive costs a
game. So does a module the world never imports: dead code cannot register a class, so a broken
`World` sitting in one is not a problem.

As a standing check, the test suite runs the analysis over every world bundled with Archipelago —
1771 modules across 82 worlds — and requires zero findings. 81 of those worlds reach the `tutorials`
check rather than being skipped, so the guarantee is about precision, not silence.

Two more checks only make sense once several worlds are installed together, and skip both sides when
they fire:

- a world whose module name would shadow one that already ships with Archipelago;
- two worlds claiming the same module name or the same `game`, where core silently loads only one.

### The import check

`--import-check` imports `worlds/` in a subprocess afterwards and reports anything in
`failed_world_loads`. This is the strongest signal available — it is exactly what the web host does
at start-up — but it **runs the downloaded worlds' module-level code**, which is third-party Python.
That is the same code the Docker image would execute when serving these games, so it is not a new
exposure; it is worth being deliberate about, which is why it is opt-in.

## Install modes

By default each apworld is **extracted**: `mygame.apworld` becomes `worlds/mygame/`. Core loads that
as an ordinary folder world, and git can diff it, review it, and store it without a binary blob per
release.

`--install-mode archive` drops the `.apworld` file into `worlds/` unchanged instead. Core loads those
too, via `zipimport`. Note that the file name is never rewritten in this mode, including its case:
core imports the world as `worlds.<file stem>` and then looks for a folder of exactly that name
inside the zip, so lower-casing the file would break it.

Archive members are validated during extraction rather than trusted — these zips come from third
parties, and one that names a path outside its own folder is rejected rather than unpacked.

## The lockfile

Every run writes `custom_worlds.lock.json`. It has two lists.

`worlds` records each installed world's wiki page, repository, release tag, asset name, SHA-256,
resolved game name and world version, so that:

- a later run can tell what actually changed, and skip re-downloading releases that did not move;
- the contents of `worlds/` can be traced back to a page and a release tag during review;
- `--prune` knows which worlds it installed, and so can remove ones that have left the category
  without ever touching a world that ships with Archipelago or one placed by hand.

### Rejected worlds

`rejected` records the releases that were turned down and why, so a broken release is downloaded
exactly once:

```json
{
  "title": "Some Game",
  "release_tag": "v1.0.0",
  "asset_name": "some_game.apworld",
  "sha256": "7a45e033…",
  "verification": "invalid",
  "codes": ["web-no-tutorials"],
  "reason": "MyGameWeb defines no 'tutorials', so WebHost.py drops it …",
  "removed": "worlds/some_game",
  "first_rejected": "2026-08-16T23:10:33Z",
  "checked": {
    "checks_version": 2,
    "archipelago_version": "0.6.8",
    "container_version": 7,
    "webhost_check": "error"
  }
}
```

On the next run the page and its release list are still checked — a new release may well have fixed
the problem — but if the newest release is one already recorded here, it is skipped without being
fetched, and reported as `known-bad`.

Each entry fingerprints the checks that produced the verdict, and stops applying by itself when any
of them moves: a newer Archipelago version, a different `--webhost-check` policy, or a bump to
`checks_version` when the checks themselves change. That last one is what lets a newly added check
reach worlds installed before it existed — their lockfile entries no longer match, so they are
re-downloaded and re-verified rather than trusted.

**A world that becomes unacceptable is removed from the output directory**, and `removed` records
where it was. Only the exact release that was rejected is deleted: if an older, working release is
what is actually installed, it stays and the run says so, because a broken new release is no reason
to lose a game that works. As everywhere else, only paths the crawler recorded installing are ever
touched. `--dry-run` reports what it would remove without removing it.

Use `--refresh` to re-download and re-check everything regardless of either list, and `--report FILE`
for a per-page JSON report including the games that failed and why.

### Partial runs

`--only` and `--limit` visit some of the category, so they are careful not to speak for the rest:
pages they did not visit keep their existing entries in both lists, and `--prune` leaves those worlds
alone. Only a full crawl treats a page's absence from the run as its absence from the wiki.

## Useful options

| Option | Effect |
| --- | --- |
| `--dry-run` | resolve, download and verify, but write nothing (and report what it would remove) |
| `--refresh` | re-download and re-check everything, ignoring both lockfile lists |
| `--only "Page Title" …` | process specific pages instead of the whole category |
| `--limit N` | stop after N pages, handy while iterating |
| `--output custom_worlds` | install somewhere other than `worlds/` |
| `--allow-prerelease` | accept pre-release GitHub releases |
| `--ignore-game-mismatch` | install even when the manifest names a different game than the page |
| `--webhost-check {error,warn,off}` | what to do about worlds the WebHost would drop (default `error`) |
| `--recursive` | descend into subcategories |
| `--strict` | exit non-zero if any game failed |
| `--keep-staging DIR` | keep the downloaded files for inspection |
| `-v` | log every request |

`python tools/crawl_custom_worlds.py --help` lists them all.

## Limitations

- A page that links to a repository without releases, or whose releases carry no `.apworld`, is
  reported as failed. Some worlds are distributed as a repository to clone rather than a release
  asset; those need to be added by hand.
- In a repository publishing several games, a world whose asset name shares nothing with the page —
  an abbreviation like `smw.apworld`, or a codename — is refused rather than guessed at. The run
  reports which games that repository does publish, so the fix is `--ignore-game-mismatch`, a
  filtered `?q=` releases link on the wiki page, or pointing the link straight at the right release.
- Only one world per page is installed unless `--all-assets` is passed.
- The crawler does not evaluate whether a world is any good, only whether core can load it. A world
  that imports cleanly can still fail during generation.
- The WebWorld analysis follows imports inside the apworld, but stops at its edge. A world whose
  `WebWorld` comes from another package, or is built at runtime rather than declared, is not judged
  — use `--import-check` to catch those.

## Tests

```bash
pytest test/custom_worlds
```

The tests run entirely offline: the wiki and the GitHub API are replaced with scripted responses and
the apworlds are built in a temporary directory, so no network access is needed and the suite does
not depend on what the wiki happens to say today.
