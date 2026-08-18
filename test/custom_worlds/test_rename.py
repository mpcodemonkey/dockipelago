"""Tests for installing worlds under a prefixed module name.

The rewriting is checked against the worlds bundled with Archipelago, which is the only sample of
real world source available offline: renaming all 82 of them and requiring every rewritten file to
still compile is a stronger guarantee than any fixture.
"""

import ast
import unittest
from pathlib import Path

from tools.custom_worlds.rename import (
    DEFAULT_MODULE_PREFIX,
    prefixed,
    rewrite_imports,
    string_references,
    valid_prefix,
)

RENAMES = {"mygame": "cw_mygame", "othergame": "cw_othergame"}


class TestPrefixed(unittest.TestCase):
    def test_a_name_gets_the_prefix(self) -> None:
        self.assertEqual("cw_mygame", prefixed("mygame", "cw_"))

    def test_applying_it_twice_changes_nothing(self) -> None:
        # A re-run must not produce cw_cw_mygame.
        self.assertEqual("cw_mygame", prefixed(prefixed("mygame", "cw_"), "cw_"))

    def test_an_empty_prefix_leaves_the_name_alone(self) -> None:
        self.assertEqual("mygame", prefixed("mygame", ""))


class TestValidPrefix(unittest.TestCase):
    def test_an_identifier_safe_prefix_is_accepted(self) -> None:
        self.assertEqual("", valid_prefix(DEFAULT_MODULE_PREFIX))
        self.assertEqual("", valid_prefix(""))

    def test_a_hyphen_is_refused_with_a_reason(self) -> None:
        # worlds/cw-mygame imports fine through importlib, but "from worlds.cw-mygame.X import y"
        # is a SyntaxError - so the rewriting this module exists for could not be written.
        problem = valid_prefix("cw-")
        self.assertIn("cannot start a Python identifier", problem)

    def test_a_prefix_starting_with_a_digit_is_refused(self) -> None:
        self.assertNotEqual("", valid_prefix("1cw_"))


class TestRewriteImports(unittest.TestCase):
    def rewrite(self, source: str) -> str:
        return rewrite_imports(source, RENAMES)

    def test_an_absolute_self_import_is_repointed(self) -> None:
        self.assertEqual(
            "from worlds.cw_mygame.Bosses import all_bosses\n",
            self.rewrite("from worlds.mygame.Bosses import all_bosses\n"),
        )

    def test_the_package_itself_is_repointed(self) -> None:
        self.assertEqual("from worlds.cw_mygame import Items\n", self.rewrite("from worlds.mygame import Items\n"))

    def test_a_plain_import_is_repointed(self) -> None:
        self.assertEqual("import worlds.cw_mygame.Locations\n", self.rewrite("import worlds.mygame.Locations\n"))

    def test_a_sibling_custom_world_is_repointed_too(self) -> None:
        # Custom worlds occasionally import each other, and both sides move.
        self.assertEqual("from worlds.cw_othergame import x\n", self.rewrite("from worlds.othergame import x\n"))

    def test_a_world_that_is_not_being_renamed_is_left_alone(self) -> None:
        # Core's own worlds keep their names, so a reference to one must not be touched.
        for line in ("from worlds.alttp.Dungeons import x\n", "from worlds.AutoWorld import World\n"):
            self.assertEqual(line, self.rewrite(line))

    def test_relative_imports_are_left_alone(self) -> None:
        self.assertEqual("from .Rom import patch\n", self.rewrite("from .Rom import patch\n"))

    def test_a_docstring_mentioning_the_module_is_left_alone(self) -> None:
        source = '"""Data for worlds.mygame lives here."""\n'
        self.assertEqual(source, self.rewrite(source))

    def test_a_module_path_in_a_string_is_left_alone(self) -> None:
        # Reported by string_references instead: rewriting text would corrupt prose that matches.
        source = 'path = "worlds.mygame.data"\n'
        self.assertEqual(source, self.rewrite(source))

    def test_a_multi_line_import_is_rewritten_whole(self) -> None:
        source = "from worlds.mygame.Items import (\n    first,\n    second,\n)\n"
        self.assertIn("from worlds.cw_mygame.Items import (", self.rewrite(source))

    def test_a_name_that_merely_starts_the_same_is_left_alone(self) -> None:
        self.assertEqual("from worlds.mygame_extra import x\n", self.rewrite("from worlds.mygame_extra import x\n"))

    def test_source_that_does_not_parse_is_returned_unchanged(self) -> None:
        source = "from worlds.mygame import (\n"
        self.assertEqual(source, self.rewrite(source))

    def test_no_renames_means_no_work(self) -> None:
        source = "from worlds.mygame import x\n"
        self.assertEqual(source, rewrite_imports(source, {}))


class TestStringReferences(unittest.TestCase):
    def test_a_module_path_string_is_reported(self) -> None:
        self.assertEqual(["worlds.mygame.data"], string_references('p = "worlds.mygame.data"\n', RENAMES))

    def test_a_resource_path_is_reported(self) -> None:
        source = 'icon = "ap:worlds/mygame/assets/icon.png"\n'
        self.assertEqual(["ap:worlds/mygame/assets/icon.png"], string_references(source, RENAMES))

    def test_prose_is_not_reported(self) -> None:
        # Whitespace is what separates a path from a sentence that happens to name one.
        source = '"""Pulls data from worlds.mygame into classes."""\n'
        self.assertEqual([], string_references(source, RENAMES))

    def test_an_unrelated_world_is_not_reported(self) -> None:
        self.assertEqual([], string_references('p = "worlds.alttp.data"\n', RENAMES))


class TestAgainstTheBundledWorlds(unittest.TestCase):
    """Rename every world Archipelago ships and require the result to still be valid Python."""

    def test_every_rewritten_core_module_still_compiles(self) -> None:
        root = Path(__file__).resolve().parents[2] / "worlds"
        names = {
            item.name: prefixed(item.name, DEFAULT_MODULE_PREFIX)
            for item in sorted(root.iterdir())
            if item.is_dir() and not item.name.startswith(("_", ".")) and (item / "__init__.py").exists()
        }
        self.assertGreater(len(names), 20, "expected to find the bundled worlds")

        rewritten = 0
        for folder in sorted(root.iterdir()):
            if folder.name not in names:
                continue
            for source_file in folder.rglob("*.py"):
                source = source_file.read_text(encoding="utf-8", errors="replace")
                result = rewrite_imports(source, names)
                if result == source:
                    continue
                rewritten += 1
                # It has to parse, and it must no longer name the old module in an import.
                tree = ast.parse(result)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        head = node.module.split(".")
                        if len(head) > 1 and head[0] == "worlds":
                            self.assertNotIn(head[1], names, f"{source_file} still imports {node.module}")
        self.assertGreater(rewritten, 50, "expected the bundled worlds to exercise the rewriting")


if __name__ == "__main__":
    unittest.main()
