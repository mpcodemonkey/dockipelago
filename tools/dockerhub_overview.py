#!/usr/bin/env python3
"""Write the description Docker Hub shows for this image.

    python tools/dockerhub_overview.py --output overview.md

Reads the crawler's lockfile and this checkout's git history; talks to nothing. Publishing the
result is the workflow's job, so this stays runnable and testable without credentials.
"""

import argparse
import sys
from pathlib import Path


def run(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from tools.custom_worlds.overview import DESCRIPTION_LIMIT, build, detect_base, read_lockfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help="repository to describe")
    parser.add_argument("--output", type=Path, help="write here instead of standard output")
    parser.add_argument("--image", default="ubufugu/dockipelago", help="the image being described")
    parser.add_argument("--repository", default="mpcodemonkey/dockipelago", help="the GitHub repository")
    parser.add_argument("--branch", default="main", help="branch the linked files are read from")
    parser.add_argument(
        "--upstream-ref",
        default="upstream/main",
        help="ref to take the merge base against; the commit section is omitted if it is unavailable",
    )
    parser.add_argument("--lockfile", type=Path, help="lockfile to read (default: <root>/custom_worlds.lock.json)")
    args = parser.parse_args(argv)

    lockfile = args.lockfile or args.root / "custom_worlds.lock.json"
    worlds = read_lockfile(lockfile)
    if not worlds:
        sys.stderr.write(f"warning: no installed worlds found in {lockfile}\n")

    base = detect_base(args.root, args.upstream_ref)
    if not base:
        sys.stderr.write(
            f"warning: could not resolve {args.upstream_ref}, so the description will not name "
            "the Archipelago commit it was built from\n"
        )

    description = build(
        worlds, base, image=args.image, repository=args.repository, branch=args.branch
    )
    if len(description) > DESCRIPTION_LIMIT:  # build() trims, so this should be unreachable
        sys.stderr.write(
            f"error: description is {len(description)} characters, over Docker Hub's "
            f"{DESCRIPTION_LIMIT} limit\n"
        )
        return 1

    if args.output:
        args.output.write_text(description, encoding="utf-8")
        sys.stderr.write(f"wrote {args.output} ({len(description)} characters, limit {DESCRIPTION_LIMIT})\n")
    else:
        sys.stdout.write(description)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
