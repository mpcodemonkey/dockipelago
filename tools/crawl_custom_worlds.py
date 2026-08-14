#!/usr/bin/env python3
"""Entry point for the custom games crawler.

    python tools/crawl_custom_worlds.py --help

The implementation lives in ``tools/custom_worlds/``; this file only makes the repository root
importable so the script works from any working directory.
"""

import sys
from pathlib import Path


def run() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.custom_worlds.crawl import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
