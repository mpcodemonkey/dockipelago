# dockipelago

A fork of [Archipelago](https://github.com/ArchipelagoMW/Archipelago) that ships the community's
custom worlds in the box. Upstream's Docker image serves the games Archipelago itself supports; this
one additionally serves the several hundred worlds listed on the wiki's
[Custom games](https://archipelago.miraheze.org/wiki/Category:Custom_games) page.

Everything below this section is upstream's README, unchanged.

## What is different here

One directory, one tool, and the file that ties them together:

- **`worlds/`** holds the custom worlds as well as the bundled ones. The custom ones are *generated*,
  not written by hand — do not edit them, since the next crawl overwrites them.
- **`tools/crawl_custom_worlds.py`** is what puts them there, and
  **`custom_worlds.lock.json`** records what it installed, from which repository and release, so any
  world in `worlds/` can be traced back to a wiki page and a tag.

Beyond those, this section of the README and the crawler's own documentation, nothing diverges from
upstream — no patches to `WebHost.py`, `settings.py`, the `Dockerfile`, any workflow, or any bundled
world. That is deliberate: it keeps merging upstream changes cheap, and it means a world that
misbehaves here would misbehave on stock Archipelago too.

## Refreshing the custom worlds

```bash
export GITHUB_TOKEN=ghp_...          # the category is far larger than the unauthenticated rate limit

python tools/crawl_custom_worlds.py --repair --validate
```

That walks the wiki category, resolves each game's download link to a GitHub release, verifies every
`.apworld` against this checkout's Archipelago version, and installs the ones that pass into
`worlds/`. `--repair` writes the WebWorld boilerplate a world is missing rather than turning it away;
`--validate` then runs Archipelago's own start-up sequence and removes anything it rejects.

Run it on the same Python the image uses (**3.12**) — an older interpreter cannot tell a world using
newer syntax from a broken one, and the crawler will say so if it is running one.

**The results stay on your machine.** Nothing a crawl writes is committed — see
[why](#why-the-worlds-are-not-committed) below. The image is built with `COPY . .`, so `worlds/` is
baked in from the working tree: the build has to happen in the checkout that was just crawled, which
is what `--docker-build` below is for.

`--validate-generation` additionally asks every installed world for a solo seed and reports which
cannot produce one. It removes nothing, because a single failed generation is weak evidence.

Full documentation — how download links are resolved, what each check exists to prevent, the lockfile
format, and every flag — is in **[docs/custom worlds crawler.md](docs/custom%20worlds%20crawler.md)**.

## The Docker image

The `Dockerfile` and `.github/workflows/docker.yml` are upstream's, untouched. CI builds and
publishes to the GitHub Container Registry exactly as upstream does — and that image contains **no
custom worlds**, because they are not in the repository.

The image with the worlds in it is built where the crawl happened, as the last step of a crawl:

```bash
export DOCKERHUB_USERNAME=you
export DOCKERHUB_TOKEN=dckr_pat_...

python tools/crawl_custom_worlds.py --repair --validate \
    --docker-push --docker-image you/dockipelago
```

`--docker-build` stops after building, and needs no account at all — the default tag is
`dockipelago:nightly`, which is enough to run it locally:

```bash
python tools/crawl_custom_worlds.py --repair --validate --docker-build
docker run --rm -p 80:80 dockipelago:nightly
```

`--docker-tag` sets the tag, and `--docker-description` replaces the repository description on
Docker Hub with one built from the lockfile: the Archipelago commit the image was built on, how many
worlds it carries, and which of them the crawler had to write boilerplate for.

**Credentials are read from the environment only.** `DOCKERHUB_TOKEN` is deliberately not a flag —
an argument is readable in the process list by any other user on the machine, and stays in the shell
history of whoever ran it. The token reaches `docker login` on stdin and nowhere else. Nothing about
any account lives in this repository.

Anything that would stop a push is checked *before* the build starts: a missing credential or an
image name without a namespace is refused up front rather than after several minutes of work.

## Why the worlds are not committed

They are third-party source, several hundred of it, and upstream's CI treats everything here as
ours: `analyze-modified-files.yml` runs flake8 and mypy over every changed `.py`, and
`unittests.yml` runs the per-world sweeps in `test/general` over every registered world. Committing
the crawl would put thousands of files that were never written to those standards through both, and
break the checks that make merging upstream safe.

So a crawl leaves the tree clean and everyone builds their own set. The crawler writes
`worlds/.gitignore` naming exactly what it installed — including itself, so it is not a change
either — and rebuilds that list every run, and `custom_worlds.lock.json` is ignored too. `git status`
after a full crawl is empty.

---

# [Archipelago](https://archipelago.gg) ![Discord Shield](https://discordapp.com/api/guilds/731205301247803413/widget.png?style=shield) | [Install](https://github.com/ArchipelagoMW/Archipelago/releases)

Archipelago provides a generic framework for developing multiworld capability for game randomizers. In all cases,
presently, Archipelago is also the randomizer itself.

Currently, the following games are supported:

* The Legend of Zelda: A Link to the Past
* Factorio
* Subnautica
* Risk of Rain 2
* The Legend of Zelda: Ocarina of Time
* Timespinner
* Super Metroid
* Secret of Evermore
* Final Fantasy
* VVVVVV
* Raft
* Super Mario 64
* Meritous
* Super Metroid/Link to the Past combo randomizer (SMZ3)
* ChecksFinder
* Hollow Knight
* The Witness
* Sonic Adventure 2: Battle
* Starcraft 2
* Dark Souls 3
* Super Mario World
* Pokémon Red and Blue
* Hylics 2
* Overcooked! 2
* Zillion
* Lufia II Ancient Cave
* Blasphemous
* Wargroove
* Stardew Valley
* The Legend of Zelda
* The Messenger
* Kingdom Hearts 2
* The Legend of Zelda: Link's Awakening DX
* Adventure
* DLC Quest
* Noita
* Undertale
* Bumper Stickers
* Mega Man Battle Network 3: Blue Version
* Muse Dash
* DOOM 1993
* Terraria
* Lingo
* Pokémon Emerald
* DOOM II
* Shivers
* Heretic
* Landstalker: The Treasures of King Nole
* Final Fantasy Mystic Quest
* TUNIC
* Kirby's Dream Land 3
* Celeste 64
* Castlevania 64
* A Short Hike
* Yoshi's Island
* Mario & Luigi: Superstar Saga
* Bomb Rush Cyberfunk
* Aquaria
* Yu-Gi-Oh! Ultimate Masters: World Championship Tournament 2006
* A Hat in Time
* Old School Runescape
* Kingdom Hearts 1
* Mega Man 2
* Yacht Dice
* Faxanadu
* Saving Princess
* Castlevania: Circle of the Moon
* Inscryption
* Civilization VI
* The Legend of Zelda: The Wind Waker
* Jak and Daxter: The Precursor Legacy
* Super Mario Land 2: 6 Golden Coins
* shapez
* Paint
* Celeste (Open World)
* Choo-Choo Charles
* APQuest
* Satisfactory
* EarthBound
* Mega Man 3
* Gauntlet Legends

For setup and instructions check out our [tutorials page](https://archipelago.gg/tutorial/).
Downloads can be found at [Releases](https://github.com/ArchipelagoMW/Archipelago/releases), including compiled
windows binaries.

## History

Archipelago is built upon a strong legacy of brilliant hobbyists. We want to honor that legacy by showing it here.
The repositories which Archipelago is built upon, inspired by, or otherwise owes its gratitude to are:

* [bonta0's MultiWorld](https://github.com/Bonta0/ALttPEntranceRandomizer/tree/multiworld_31)
* [AmazingAmpharos' Entrance Randomizer](https://github.com/AmazingAmpharos/ALttPEntranceRandomizer)
* [VT Web Randomizer](https://github.com/sporchia/alttp_vt_randomizer)
* [Dessyreqt's alttprandomizer](https://github.com/Dessyreqt/alttprandomizer)
* [Zarby89's](https://github.com/Ijwu/Enemizer/commits?author=Zarby89)
  and [sosuke3's](https://github.com/Ijwu/Enemizer/commits?author=sosuke3) contributions to Enemizer, which make up the
  vast majority of Enemizer contributions.

We recognize that there is a strong community of incredibly smart people that have come before us and helped pave the
path. Just because one person's name may be in a repository title does not mean that only one person made that project
happen. We can't hope to perfectly cover every single contribution that lead up to Archipelago, but we hope to honor
them fairly.

### Path to the Archipelago

Archipelago was directly forked from bonta0's `multiworld_31` branch of ALttPEntranceRandomizer (this project has a
long legacy of its own, please check it out linked above) on January 12, 2020. The repository was then named to
_MultiWorld-Utilities_ to better encompass its intended function. As Archipelago matured, then known as
"Berserker's MultiWorld" by some, we found it necessary to transform our repository into a root level repository
(as opposed to a 'forked repo') and change the name (which came later) to better reflect our project.

## Running Archipelago

For most people, all you need to do is head over to
the [releases page](https://github.com/ArchipelagoMW/Archipelago/releases), then download and run the appropriate
installer, or AppImage for Linux-based systems.

If you are a developer or are running on a platform with no compiled releases available, please see our doc on
[running Archipelago from source](docs/running%20from%20source.md).

## Related Repositories

This project makes use of multiple other projects. We wouldn't be here without these other repositories and the
contributions of their developers, past and present.

* [z3randomizer](https://github.com/ArchipelagoMW/z3randomizer)
* [Enemizer](https://github.com/Ijwu/Enemizer)
* [Ocarina of Time Randomizer](https://github.com/TestRunnerSRL/OoT-Randomizer)

## Contributing

To contribute to Archipelago, including the WebHost, core program, or by adding a new game, see our
[Contributing guidelines](/docs/contributing.md).

## FAQ

For Frequently asked questions, please see the website's [FAQ Page](https://archipelago.gg/faq/en/).

## Code of Conduct

Please refer to our [code of conduct](/docs/code_of_conduct.md).
