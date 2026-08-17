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
   ``docs`` folder. A world installed as a folder without one raises FileNotFoundError, and again
   the whole site fails to start rather than the one game.

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

#: How long to let the subprocess run. Importing several hundred worlds is not quick.
DEFAULT_TIMEOUT = 900.0


@dataclass
class WorldVerdict:
    """What Archipelago made of one installed world."""

    game: str
    module: str
    status: str
    reason: str = ""


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

    try:
        process = subprocess.run(
            [python, "-c", _SCRIPT],
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
