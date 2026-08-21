"""Tests for the description published to Docker Hub.

The thing that matters here is the size limit. Docker Hub rejects a description over 25,000
characters, the game list grows every time the crawler runs, and a rejected API call would be
noticed long after the build that caused it - so the generator has to stay inside the limit on its
own, whatever it is handed.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools.custom_worlds.overview import (
    DESCRIPTION_LIMIT,
    UPSTREAM,
    Base,
    build,
    detect_base,
    read_lockfile,
)

IMAGE = "ubufugu/dockipelago"
REPOSITORY = "mpcodemonkey/dockipelago"
BASE = Base("fe5b49e1899b32bcb9f65e91cb7f74d6aa6d0ff8", "Core: limit depth of received JSON (#6378)", "2026-08-10")


def worlds(count: int, *, repaired: int = 0) -> list[dict[str, object]]:
    made: list[dict[str, object]] = []
    for index in range(count):
        world: dict[str, object] = {
            "file": f"worlds/game{index}",
            "game": f"Game Number {index}",
            "world_version": "1.2.3",
        }
        if index < repaired:
            world["repaired"] = ["web-missing", "docs-missing"]
        made.append(world)
    return made


def page(entries: list[dict[str, object]], *, limit: int = DESCRIPTION_LIMIT) -> str:
    return build(entries, BASE, image=IMAGE, repository=REPOSITORY, limit=limit)


class TestStayingInsideTheLimit(unittest.TestCase):
    def test_a_realistic_library_fits(self) -> None:
        self.assertLessEqual(len(page(worlds(600))), DESCRIPTION_LIMIT)

    def test_it_fits_even_when_every_world_needed_repairing(self) -> None:
        # The worst case the data allows, and the only section that grows without bound.
        self.assertLessEqual(len(page(worlds(600, repaired=600))), DESCRIPTION_LIMIT)

    def test_it_fits_at_sizes_the_library_may_yet_reach(self) -> None:
        for count in (1000, 2000, 5000):
            with self.subTest(worlds=count):
                self.assertLessEqual(len(page(worlds(count, repaired=count))), DESCRIPTION_LIMIT)

    def test_the_table_is_trimmed_rather_than_the_page_overflowing(self) -> None:
        trimmed = page(worlds(400, repaired=400), limit=8000)
        self.assertLessEqual(len(trimmed), 8000)
        self.assertIn("more — see", trimmed, "a trimmed table has to say so")
        self.assertLess(trimmed.count("| Game Number"), 400)

    def test_it_falls_back_to_a_summary_when_no_table_fits(self) -> None:
        squeezed = page(worlds(400, repaired=400), limit=1800)
        self.assertLessEqual(len(squeezed), 1800)
        self.assertNotIn("| Game Number", squeezed)
        self.assertIn("recorded in", squeezed)

    def test_the_sections_that_matter_survive_every_squeeze(self) -> None:
        # Whatever else is dropped, the reader still learns what this image is and what it is built on.
        for limit in (25000, 8000, 3000, 1800):
            with self.subTest(limit=limit):
                text = page(worlds(400, repaired=400), limit=limit)
                self.assertIn("docker pull ubufugu/dockipelago:nightly", text)
                self.assertIn(BASE.commit[:9], text)
                self.assertIn("400 custom worlds", text)


class TestWhatItSays(unittest.TestCase):
    def test_the_upstream_commit_is_named_and_linked(self) -> None:
        text = page(worlds(3))
        self.assertIn(f"https://github.com/{UPSTREAM}/commit/{BASE.commit}", text)
        self.assertIn(BASE.subject, text)
        self.assertIn(BASE.date, text)

    def test_an_unknown_base_is_omitted_rather_than_guessed(self) -> None:
        # Naming the wrong commit would be worse than naming none.
        text = build(worlds(3), Base(), image=IMAGE, repository=REPOSITORY)
        self.assertNotIn("/commit/", text)
        self.assertIn("forked from", text)

    def test_the_world_count_is_reported(self) -> None:
        self.assertIn("42 custom worlds", page(worlds(42)))

    def test_the_full_list_is_linked_rather_than_included(self) -> None:
        text = page(worlds(600))
        self.assertIn("custom_worlds.lock.json", text)
        self.assertNotIn("| Game Number 500 |", text)

    def test_repairs_are_named_in_plain_words(self) -> None:
        text = page(worlds(3, repaired=1))
        self.assertIn("WebWorld class", text)
        self.assertIn("setup guide", text)
        self.assertNotIn("web-missing", text, "the raw code is jargon on a public page")

    def test_a_library_that_needed_no_repairs_says_nothing_about_them(self) -> None:
        self.assertNotIn("completed", page(worlds(5)))

    def test_an_unrecognised_repair_code_is_still_shown(self) -> None:
        entries = worlds(1)
        entries[0]["repaired"] = ["something-new"]
        self.assertIn("something-new", page(entries))


class TestReadingTheLockfile(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "custom_worlds.lock.json"

    def write(self, payload: object) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_only_installed_worlds_are_counted(self) -> None:
        self.write({"worlds": [{"file": "worlds/a", "game": "A"}, {"game": "Skipped", "file": ""}]})
        self.assertEqual(1, len(read_lockfile(self.path)))

    def test_a_lockfile_without_repairs_is_read_fine(self) -> None:
        # Lockfiles written before --repair existed have no such field.
        self.write({"worlds": [{"file": "worlds/a", "game": "A", "world_version": "1.0.0"}]})
        self.assertNotIn("completed", page(read_lockfile(self.path)))

    def test_a_missing_lockfile_is_not_an_error(self) -> None:
        self.assertEqual([], read_lockfile(self.path.with_name("absent.json")))

    def test_unreadable_json_is_not_an_error(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual([], read_lockfile(self.path))


class TestDetectingTheBase(unittest.TestCase):
    def test_a_ref_that_does_not_exist_yields_nothing(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertFalse(detect_base(root, "definitely/not/a/ref"))

    def test_a_directory_that_is_not_a_repository_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(detect_base(Path(folder)))


if __name__ == "__main__":
    unittest.main()
