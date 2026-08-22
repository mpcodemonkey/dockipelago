"""Building and publishing the image from the worlds a crawl just installed.

The worlds are not committed - they are third-party source that upstream's own CI would lint and
test as though it were ours, and several hundred of them would never pass. So the image cannot be
built from a checkout by itself; it has to be built where the crawl happened, right after it.

That is what this is for. ``--docker-build`` and ``--docker-push`` run at the end of a crawl, on the
tree the crawl just produced, so anybody can point the crawler at the wiki and end up with their own
image under their own account. No credentials live in the repository, and none are needed to run
anything else here.

The token is only ever handed to ``docker login`` on **stdin**. Passing it as an argument would put
it in the process list, where any other user on the machine can read it, and in the shell history of
whoever ran the command.
"""

import json
import logging
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Environment variables the credentials are read from. The token is never accepted as a flag.
USERNAME_ENV = "DOCKERHUB_USERNAME"
TOKEN_ENV = "DOCKERHUB_TOKEN"

DEFAULT_IMAGE = "dockipelago"
DEFAULT_TAG = "nightly"

_HUB_API = "https://hub.docker.com/v2"
_DESCRIPTION_TIMEOUT = 30


@dataclass
class DockerOptions:
    """What to build, what to call it, and whether to publish it."""

    build: bool = False
    push: bool = False
    describe: bool = False
    image: str = DEFAULT_IMAGE
    tag: str = DEFAULT_TAG
    username: str = ""

    @property
    def wanted(self) -> bool:
        return self.build or self.push

    @property
    def reference(self) -> str:
        return f"{self.image}:{self.tag}"


def token() -> str:
    """The Docker Hub token, from the environment only."""
    return os.environ.get(TOKEN_ENV, "")


def username(configured: str = "") -> str:
    return configured or os.environ.get(USERNAME_ENV, "")


def run(root: Path, options: DockerOptions) -> tuple[bool, str]:
    """Build, and optionally publish, the image. Returns (ok, what happened)."""
    if not options.wanted:
        return True, ""

    # Everything that can be known without doing the work is checked first: a build takes minutes,
    # and finding out afterwards that there was never a credential to push with wastes all of it.
    who, secret = username(options.username), token()
    if options.push:
        refusal = _cannot_push(options, who, secret)
        if refusal:
            return False, refusal

    if not _build(root, options.reference):
        return False, f"could not build {options.reference}"
    if not options.push:
        return True, f"built {options.reference}"

    if not _login(who, secret):
        return False, f"could not log in to Docker Hub as {who}"
    if not _push(options.reference):
        return False, f"could not push {options.reference}"

    done = f"pushed {options.reference}"
    if options.describe:
        ok, detail = _describe(root, options, who, secret)
        done += f", {detail}" if ok else f" (but {detail})"
    return True, done


def _cannot_push(options: DockerOptions, who: str, secret: str) -> str:
    """Why this could not be published, or "" if nothing stands in the way yet."""
    missing = [name for name, value in ((USERNAME_ENV, who), (TOKEN_ENV, secret)) if not value]
    if missing:
        return (
            f"cannot push {options.reference}: {' and '.join(missing)} not set. Export them and run "
            "again, or use --docker-build to build without publishing"
        )
    if "/" not in options.image:
        return (
            f"cannot push {options.reference}: an image to publish needs a namespace, as in "
            f"--docker-image {who}/{options.image}"
        )
    return ""


def _build(root: Path, reference: str) -> bool:
    return _spawn(["docker", "build", "-t", reference, "."], cwd=root)


def _push(reference: str) -> bool:
    return _spawn(["docker", "push", reference])


def _login(who: str, secret: str) -> bool:
    """Log in with the token on stdin, so it never appears in the process list."""
    logger.info("  logging in to Docker Hub as %s", who)
    try:
        done = subprocess.run(
            ["docker", "login", "--username", who, "--password-stdin"],
            input=secret,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("  docker login failed: %s", error)
        return False
    if done.returncode != 0:
        logger.error("  docker login failed: %s", (done.stderr or done.stdout).strip()[-400:])
        return False
    return True


def _spawn(command: list[str], cwd: Path | None = None) -> bool:
    """Run a docker command, letting its output through so a long build is not silent."""
    logger.info("  %s", shlex.join(command))
    try:
        return subprocess.run(command, cwd=cwd, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("  %s failed: %s", command[1], error)
        return False


def _describe(root: Path, options: DockerOptions, who: str, secret: str) -> tuple[bool, str]:
    """Replace the repository's description on Docker Hub with a freshly built one."""
    from .overview import build, detect_base, detect_repository, read_lockfile

    worlds = read_lockfile(root / "custom_worlds.lock.json")
    description = build(
        worlds,
        detect_base(root),
        image=options.image,
        tag=options.tag,
        # Whoever built this image is who the page should send a reader to, not this fork.
        repository=detect_repository(root),
    )
    try:
        jwt = _hub_token(who, secret)
        _patch_description(options.image, jwt, description)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        return False, f"the description was not updated: {error}"
    return True, f"updated the description ({len(description)} characters)"


def _hub_token(who: str, secret: str) -> str:
    """Docker Hub's own API needs a JWT; the registry token is what buys one."""
    payload = json.dumps({"username": who, "password": secret}).encode()
    request = urllib.request.Request(
        f"{_HUB_API}/users/login/", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=_DESCRIPTION_TIMEOUT) as response:
        return str(json.loads(response.read())["token"])


def _patch_description(image: str, jwt: str, description: str) -> None:
    payload = json.dumps({"full_description": description}).encode()
    request = urllib.request.Request(
        f"{_HUB_API}/repositories/{image}/",
        data=payload,
        method="PATCH",
        headers={"Content-Type": "application/json", "Authorization": f"JWT {jwt}"},
    )
    with urllib.request.urlopen(request, timeout=_DESCRIPTION_TIMEOUT):
        return
