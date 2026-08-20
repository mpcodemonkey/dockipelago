"""Running Archipelago's own start-up checks against the installed worlds.

The static checks in :mod:`.webworld` are a pre-filter: they are fast, they need no dependencies, and
they never execute third-party code. What they cannot be is complete. A world can hand its
``WebWorld`` back from a factory function, build its ``World`` class with ``type()``, or ship an
option whose docstring the WebHost's template renderer chokes on - and no amount of reading the
source settles any of that. Every round of tightening the source analysis has been followed by
another handful of worlds that slipped past it.

So this module stops predicting and asks. It runs the same sequence ``WebHost.py`` runs at start-up,
in a subprocess, and reports a verdict per game:

1. ``import worlds`` - anything in ``failed_world_loads`` never registered at all.
2. ``hasattr(world.web, "tutorials")`` - the filter that logs "Following worlds not loaded as they
   are invalid for WebHost" and drops the world from the site and the data package.
3. ``Options.generate_yaml_templates`` - the WebHost calls this through ``create_options_files()``
   before it serves anything, and it raises on the first world it cannot render. One bad world here
   does not get dropped; it stops the whole server from starting.
4. ``copy_tutorials_files_to_static`` - the step after that, which lists every non-hidden world's
   ``docs`` folder and copies each entry. A world installed as a folder without one raises
   FileNotFoundError; one whose ``docs`` holds a subfolder raises IsADirectoryError on it. Either
   way the whole site fails to start rather than the one game.

Step 3 is why this exists at all. It is run per world, with the registry temporarily narrowed to one
game, so a single failure names the game responsible instead of aborting the sweep.

This executes the installed worlds' code, including their module-level imports. That is the same
code the Docker image runs when it serves them, so it is not a new exposure - but it is the reason
this is a separate, opt-in step rather than part of verification.
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Verdicts, worst first.
FAILED_TO_LOAD = "failed-to-load"
INVALID_FOR_WEBHOST = "invalid-for-webhost"
TEMPLATE_FAILED = "template-failed"
DOCS_MISSING = "docs-missing"
DOCS_NOT_FLAT = "docs-not-flat"

#: Verdicts from --validate-generation, which reports rather than removes.
GENERATION_FAILED = "generation-failed"
GENERATION_TIMEOUT = "generation-timeout"
UNBEATABLE = "unbeatable"
UNREACHABLE = "unreachable"

#: How long one world gets to generate before it is called a hang.
DEFAULT_WORLD_TIMEOUT = 120
#: The seed generation is attempted with. Fixed, so a run is repeatable.
GENERATION_SEED = 1
#: Seed offsets a failing world is retried with before it is believed.
CONFIRM_SEEDS = (0, 1, 2)

#: How long to let the subprocess run. Importing several hundred worlds is not quick.
DEFAULT_TIMEOUT = 900.0


@dataclass
class WorldVerdict:
    """What Archipelago made of one installed world."""

    game: str
    module: str
    status: str
    reason: str = ""
    #: How long this world took, for generation runs where that is worth knowing.
    seconds: float = 0.0


@dataclass
class ValidationReport:
    """The outcome of one validation run."""

    ok: bool = False
    error: str = ""
    registered: int = 0
    verdicts: list[WorldVerdict] = field(default_factory=list)

    @property
    def failures(self) -> list[WorldVerdict]:
        return self.verdicts

    def by_module(self) -> dict[str, WorldVerdict]:
        """Failures keyed by the ``worlds/<module>`` they came from, for removing them again."""
        return {verdict.module: verdict for verdict in self.verdicts if verdict.module}


#: Run inside the checkout. Kept as a string so the crawler needs nothing importable from core.
_SCRIPT = """
import json, os, sys, tempfile, traceback

def emit(payload):
    sys.stdout.write("\\n@@CUSTOM_WORLDS@@" + json.dumps(payload))

try:
    import worlds
    from worlds import AutoWorldRegister
except ModuleNotFoundError as missing:
    emit({"missing_dependency": missing.name or "", "error": traceback.format_exc()[-2000:]})
    raise SystemExit(0)
except Exception:
    emit({"error": "importing worlds failed:\\n" + traceback.format_exc()[-4000:]})
    raise SystemExit(0)

verdicts = []
for name, reason in sorted(worlds.failed_world_loads.items()):
    verdicts.append({
        "game": name,
        "module": name,
        "status": "failed-to-load",
        "reason": reason.strip().splitlines()[-1][:500],
    })

def module_of(world):
    path = getattr(world, "__file__", "") or ""
    parent = os.path.basename(os.path.dirname(path))
    return parent

registry = dict(AutoWorldRegister.world_types)

# The WebHost's own filter, verbatim.
invalid = {}
for game, world in registry.items():
    if not hasattr(world.web, "tutorials"):
        invalid[game] = world
        verdicts.append({
            "game": game,
            "module": module_of(world),
            "status": "invalid-for-webhost",
            "reason": "web has no 'tutorials', so WebHost.py drops it from the site",
        })

# create_options_files() renders a template per world and raises on the first that fails, which
# stops the WebHost booting. Narrow the registry to one game at a time to find every culprit.
import Options
remaining = {game: world for game, world in registry.items() if game not in invalid}
with tempfile.TemporaryDirectory() as folder:
    for game, world in remaining.items():
        AutoWorldRegister.world_types = {game: world}
        try:
            Options.generate_yaml_templates(folder, True)
        except Exception as error:
            cause = error.__cause__ or error
            verdicts.append({
                "game": game,
                "module": module_of(world),
                "status": "template-failed",
                "reason": "{}: {}".format(type(cause).__name__, cause)[:500],
            })
        finally:
            AutoWorldRegister.world_types = registry

# copy_tutorials_files_to_static() is the next thing WebHost.py calls, and it does a bare
# os.listdir() on each non-hidden world's docs folder. A world installed as a folder without one
# raises FileNotFoundError and the site never finishes starting.
for game, world in remaining.items():
    if getattr(world, "zip_path", None) or getattr(world, "hidden", False):
        continue
    folder = os.path.join(os.path.dirname(getattr(world, "__file__", "") or ""), "docs")
    if not os.path.isdir(folder):
        verdicts.append({
            "game": game,
            "module": module_of(world),
            "status": "docs-missing",
            "reason": "no docs/ folder, so copy_tutorials_files_to_static() raises at start-up",
        })
        continue
    # The same step then copyfile()s every entry, and a subfolder is an entry.
    nested = sorted(e for e in os.listdir(folder) if os.path.isdir(os.path.join(folder, e)))
    if nested:
        verdicts.append({
            "game": game,
            "module": module_of(world),
            "status": "docs-not-flat",
            "reason": "docs/ holds subfolder(s) %s, which copy_tutorials_files_to_static() tries to "
                      "copyfile() and dies on" % ", ".join(nested[:3]),
        })

emit({"registered": len(registry), "verdicts": verdicts})
"""

_MARKER = "@@CUSTOM_WORLDS@@"


def validate_installed_worlds(
    root: Path, *, python: str = sys.executable, timeout: float = DEFAULT_TIMEOUT
) -> ValidationReport:
    """Import the installed worlds and run the WebHost's start-up checks over them."""
    logger.info("Validating against Archipelago itself (this runs the installed worlds' code)")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    environment.setdefault("SKIP_REQUIREMENTS_UPDATE", "true")
    return _run(_SCRIPT, root, python, timeout, environment)


def _run(
    script: str, root: Path, python: str, timeout: float, environment: dict[str, str]
) -> ValidationReport:
    """Run one of the check scripts in a subprocess and turn what it emitted into a report."""
    try:
        process = subprocess.run(
            [python, "-c", script],
            cwd=root,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ValidationReport(error=f"validation did not finish within {timeout:.0f}s")
    except OSError as error:
        return ValidationReport(error=f"could not start the validation subprocess: {error}")

    payload = _extract(process.stdout)
    if payload is None:
        detail = (process.stderr or process.stdout).strip()[-4000:]
        return ValidationReport(error=detail or f"validation exited with status {process.returncode}")
    missing = str(payload.get("missing_dependency") or "")
    if missing:
        return ValidationReport(
            error=(
                f"Archipelago's own dependency '{missing}' is not installed for {python}, so its "
                "worlds cannot be imported. Validation runs the real Archipelago, so it needs the "
                "same environment the WebHost runs in. Either install them "
                "(python ModuleUpdate.py --yes, or pip install -r requirements.txt), or point at "
                "the interpreter that already has them with --validate-python."
            )
        )
    if payload.get("error"):
        return ValidationReport(error=str(payload["error"]))

    verdicts = [
        WorldVerdict(
            game=str(entry.get("game", "")),
            module=str(entry.get("module", "")),
            status=str(entry.get("status", "")),
            reason=str(entry.get("reason", "")),
            seconds=float(entry.get("seconds", 0.0)),
        )
        for entry in payload.get("verdicts", [])
    ]
    return ValidationReport(ok=True, registered=int(payload.get("registered", 0)), verdicts=verdicts)


def _extract(stdout: str) -> dict[str, Any] | None:
    """Pull the report off stdout, which the worlds themselves also print to."""
    marker = stdout.rfind(_MARKER)
    if marker < 0:
        return None
    try:
        payload = json.loads(stdout[marker + len(_MARKER):])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


#: Runs a solo generation per world. Separate from _SCRIPT because it executes far more of a world's
#: code - its whole generation path rather than its imports - and is opt-in for that reason.
_GENERATION_SCRIPT = """
import json, os, signal, sys, time, traceback

def emit(payload):
    sys.stdout.write("\\n@@CUSTOM_WORLDS@@" + json.dumps(payload))

try:
    import worlds
    from worlds.AutoWorld import AutoWorldRegister
    # Snapshot before test.general is imported: importing it registers Archipelago's own test
    # fixture worlds, which are not games anybody installs and would be reported as failures.
    installed = dict(AutoWorldRegister.world_types)
    from test.general import setup_solo_multiworld, gen_steps
    from Fill import distribute_items_restrictive
except ModuleNotFoundError as missing:
    emit({"missing_dependency": missing.name or "", "error": traceback.format_exc()[-2000:]})
    raise SystemExit(0)
except Exception:
    emit({"error": "preparing generation failed:\\n" + traceback.format_exc()[-4000:]})
    raise SystemExit(0)

PER_WORLD = int(os.environ.get("CW_WORLD_TIMEOUT", "120"))
SEED = int(os.environ.get("CW_SEED", "1"))

class Hang(Exception):
    pass

def _ring(signum, frame):
    raise Hang()

# A world with a pathological fill can spin indefinitely, and one hang must not cost the whole run.
armed = hasattr(signal, "SIGALRM")
if armed:
    signal.signal(signal.SIGALRM, _ring)

def module_of(world):
    return os.path.basename(os.path.dirname(getattr(world, "__file__", "") or ""))

# Hidden worlds are not games a player generates - Archipelago's own placeholder is one - and the
# WebHost keeps them off the site, so asking them for a seed says nothing useful.
registry = {game: world for game, world in installed.items() if not getattr(world, "hidden", False)}

verdicts = []
for game, world in sorted(registry.items()):
    started = time.perf_counter()
    status, reason = "", ""
    if armed:
        signal.alarm(PER_WORLD)
    try:
        multiworld = setup_solo_multiworld(world, gen_steps, seed=SEED)
        distribute_items_restrictive(multiworld)
        if not multiworld.can_beat_game():
            status, reason = "unbeatable", "generated a seed whose goal cannot be reached"
        elif not multiworld.fulfills_accessibility():
            status, reason = "unreachable", "generated a seed with locations no player can reach"
    except Hang:
        status = "generation-timeout"
        reason = "still generating after %ds" % PER_WORLD
    except Exception as error:
        status = "generation-failed"
        reason = "{}: {}".format(type(error).__name__, error)[:500]
    finally:
        if armed:
            signal.alarm(0)
    if status:
        verdicts.append({
            "game": game,
            "module": module_of(world),
            "status": status,
            "reason": reason,
            "seconds": round(time.perf_counter() - started, 2),
        })

emit({"registered": len(registry), "verdicts": verdicts})
"""


_CONFIRM_SCRIPT = _GENERATION_SCRIPT.replace(
    'registry = {game: world for game, world in installed.items() if not getattr(world, "hidden", False)}',
    'only = os.environ["CW_ONLY_GAME"]\nregistry = {g: w for g, w in installed.items() if g == only}',
)


def validate_generation(
    root: Path,
    *,
    python: str = sys.executable,
    timeout: float = DEFAULT_TIMEOUT,
    world_timeout: int = DEFAULT_WORLD_TIMEOUT,
    seed: int = GENERATION_SEED,
) -> ValidationReport:
    """Generate a solo seed for every installed world and report which ones cannot.

    This is the gap the other checks leave. A world can import, register, pass the WebHost's filters
    and appear on the site, and still fail the moment anyone actually asks it for a seed - which is
    the only thing a player ever does with it.

    No server is involved: generation is a batch process, and the WebHost only hosts a room once a
    seed exists. What it does need is the whole of a world's generation path, so this runs far more
    third-party code than :func:`validate_installed_worlds` and is opt-in separately.

    One seed, default options, one player. That makes it a smoke test rather than a proof: a world
    can pass here and still fail on another seed, under different options, or alongside other worlds.
    A clean result means "nothing obviously broken", not "correct".
    """
    logger.info("Generating a solo seed per world (this runs the worlds' generation code)")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    environment.setdefault("SKIP_REQUIREMENTS_UPDATE", "true")
    environment["CW_WORLD_TIMEOUT"] = str(world_timeout)
    environment["CW_SEED"] = str(seed)

    report = _run(_GENERATION_SCRIPT, root, python, timeout, environment)
    if not report.ok or not report.verdicts:
        return report

    # Every world in the sweep shares one interpreter, and worlds are not always tidy with global
    # state - core's own SMZ3 generates on every seed alone and still failed in the sweep, blamed
    # for something an earlier world left behind. So a failure is only believed once it survives on
    # its own, in a fresh process. Only failures pay for that, and there should not be many.
    confirmed: list[WorldVerdict] = []
    for verdict in report.verdicts:
        logger.info("  re-checking %s on its own", verdict.game)
        survived = _confirm(verdict, root, python, timeout, environment, seed)
        if survived is None:
            logger.info("  %s generated on a retry; not reported", verdict.game)
        else:
            confirmed.append(survived)
    report.verdicts = confirmed
    return report


def _confirm(
    verdict: WorldVerdict,
    root: Path,
    python: str,
    timeout: float,
    environment: dict[str, str],
    seed: int,
) -> WorldVerdict | None:
    """Re-run one world alone, over several seeds, and return its verdict only if it never generates.

    Two things make a single failure weak evidence. The sweep shares one interpreter between every
    world, and worlds are not always tidy with global state, so a failure can belong to whatever ran
    before it. And generation is not as deterministic as its seed suggests - core's own SMZ3
    generates happily on five seeds in one context and fails on the first seed in another, for
    reasons this does not attempt to settle.

    So a world is only reported when it fails alone, in a fresh process, on every seed tried. That
    keeps the report worth reading: a name in it is a world that could not produce a seed at all,
    not one that had a bad day.
    """
    for attempt, offset in enumerate(CONFIRM_SEEDS):
        alone = dict(environment, CW_ONLY_GAME=verdict.game, CW_SEED=str(seed + offset))
        retry = _run(_CONFIRM_SCRIPT, root, python, timeout, alone)
        if not retry.ok:
            # Could not re-check at all, so take the sweep at its word rather than clear the world.
            return verdict
        if not retry.verdicts:
            return None
        if attempt == len(CONFIRM_SEEDS) - 1:
            return retry.verdicts[0]
    return verdict
