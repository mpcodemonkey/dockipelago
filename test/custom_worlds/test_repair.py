"""Tests for writing the WebWorld paperwork a world is missing.

Repair edits third-party source, so what is checked here is as much what it leaves alone as what it
writes: the world's own lines have to come back unchanged, and a repair that does not actually fix
the world must not be kept.
"""

import ast
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.custom_worlds.repair import (
    GENERATED_WEB_CLASS,
    REPAIRABLE,
    SETUP_DOC,
    repair_apworld,
    repairable,
)
from tools.custom_worlds.verify import detect_core_versions, verify_apworld
from tools.custom_worlds.webworld import (
    DOCS_MISSING,
    UNPARSEABLE,
    WEB_MISSING,
    WEB_NO_TUTORIALS,
    WebWorldFinding,
    inspect_world,
)

VERSIONS = detect_core_versions(Path(__file__).resolve().parents[2])

WORLD_BODY = '    game = "{game}"\n    item_name_to_id = {{}}\n    location_name_to_id = {{}}\n'

NO_WEB = (
    '"""A world with no WebWorld at all."""\n'
    "from worlds.AutoWorld import World\n"
    "\n"
    "from .Items import ITEMS\n"
    "\n"
    "\n"
    "class DemoWorld(World):\n"
    '    """The world\'s own docstring."""\n'
    "\n" + WORLD_BODY.format(game="Demo")
)

NO_TUTORIALS = (
    "from worlds.AutoWorld import World, WebWorld\n"
    "\n"
    "\n"
    "class DemoWeb(WebWorld):\n"
    '    theme = "grass"\n'
    "\n"
    "\n"
    "class DemoWorld(World):\n" + WORLD_BODY.format(game="Demo") + "    web = DemoWeb()\n"
)


def finding(code: str, module: str = "", target: str = "") -> WebWorldFinding:
    return WebWorldFinding(code=code, detail="", severity="webhost", module=module, target_class=target)


class TestRepairable(unittest.TestCase):
    def test_the_three_paperwork_findings_are_repairable(self) -> None:
        self.assertEqual({DOCS_MISSING, WEB_NO_TUTORIALS, WEB_MISSING}, set(REPAIRABLE))

    def test_a_world_with_only_repairable_findings_qualifies(self) -> None:
        self.assertTrue(repairable([finding(WEB_MISSING), finding(DOCS_MISSING)]))

    def test_anything_else_disqualifies_the_whole_world(self) -> None:
        # Repairing half of it would install something still broken, which beats a clear refusal.
        self.assertFalse(repairable([finding(WEB_MISSING), finding(UNPARSEABLE)]))

    def test_a_world_with_nothing_wrong_is_not_repaired(self) -> None:
        self.assertFalse(repairable([]))

    def test_a_warning_does_not_stand_in_the_way(self) -> None:
        self.assertTrue(repairable([finding(WEB_MISSING), finding("bad-escape")]))


class RepairTestCase(unittest.TestCase):
    """Builds a real apworld, repairs it, and asks the checks what they make of the result."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def build(self, name: str, init: str, *, docs: bool = False, extra: dict[str, str] | None = None) -> Path:
        path = self.tmp / f"{name}.apworld"
        manifest = {
            "game": "Demo",
            "world_version": "1.0.0",
            "minimum_ap_version": "0.6.0",
            "version": 7,
            "compatible_version": 7,
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}/__init__.py", init)
            for relative, content in (extra or {}).items():
                archive.writestr(f"{name}/{relative}", content)
            if docs:
                archive.writestr(f"{name}/docs/{SETUP_DOC}", "# existing guide\n")
            archive.writestr(f"{name}/archipelago.json", json.dumps(manifest))
        return path

    def repair(self, source: Path) -> tuple[list[str], Path]:
        """Repair a world into a file named the same way, since core ties the two together."""
        before = verify_apworld(source, VERSIONS)
        patched = self.tmp / "out" / source.name
        patched.parent.mkdir(exist_ok=True)
        applied = repair_apworld(source, patched, before.findings)
        return applied, patched

    def contents(self, archive_path: Path) -> dict[str, str]:
        with zipfile.ZipFile(archive_path) as archive:
            return {
                name.split("/", 1)[1]: archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if not name.endswith("/")
            }


class TestRepairingAWorldWithNoWeb(RepairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.build("demo", NO_WEB, extra={"Items.py": "ITEMS = []\n"})
        self.applied, self.patched = self.repair(self.source)

    def test_it_reports_what_it_answered(self) -> None:
        self.assertEqual({WEB_MISSING, DOCS_MISSING}, set(self.applied))

    def test_the_world_now_passes_verification(self) -> None:
        result = verify_apworld(self.patched, VERSIONS)
        self.assertTrue(result.installable, result.summary())
        self.assertEqual([], result.codes)

    def test_web_is_assigned_directly_after_the_docstring(self) -> None:
        source = self.contents(self.patched)["__init__.py"]
        body = ast.parse(source).body
        world = next(n for n in body if isinstance(n, ast.ClassDef) and n.name == "DemoWorld")
        self.assertIsInstance(world.body[0], ast.Expr)  # the world's own docstring, still first
        assignment = world.body[1]
        self.assertIsInstance(assignment, ast.Assign)
        self.assertEqual(f"web = {GENERATED_WEB_CLASS}()", ast.unparse(assignment))

    def test_the_generated_class_is_defined_before_the_world(self) -> None:
        body = ast.parse(self.contents(self.patched)["__init__.py"]).body
        names = [node.name for node in body if isinstance(node, ast.ClassDef)]
        self.assertLess(names.index(GENERATED_WEB_CLASS), names.index("DemoWorld"))

    def test_the_imports_are_added(self) -> None:
        source = self.contents(self.patched)["__init__.py"]
        self.assertIn("from worlds.AutoWorld import WebWorld", source)
        self.assertIn("from BaseClasses import Tutorial", source)

    def test_a_setup_guide_backs_the_tutorial(self) -> None:
        self.assertIn(f"docs/{SETUP_DOC}", self.contents(self.patched))

    def test_the_worlds_own_lines_are_untouched(self) -> None:
        # Only additions: every original line has to come back, in order.
        original = self.contents(self.source)["__init__.py"].splitlines()
        repaired = self.contents(self.patched)["__init__.py"].splitlines()
        position = -1
        for line in original:
            position = repaired.index(line, position + 1)

    def test_other_files_are_carried_over_unchanged(self) -> None:
        self.assertEqual("ITEMS = []\n", self.contents(self.patched)["Items.py"])

    def test_the_original_is_left_alone(self) -> None:
        self.assertEqual(NO_WEB, self.contents(self.source)["__init__.py"])


class TestRepairingAWorldWithNoTutorials(RepairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.build("demo", NO_TUTORIALS)
        self.applied, self.patched = self.repair(self.source)

    def test_it_reports_what_it_answered(self) -> None:
        self.assertEqual({WEB_NO_TUTORIALS, DOCS_MISSING}, set(self.applied))

    def test_the_world_now_passes_verification(self) -> None:
        result = verify_apworld(self.patched, VERSIONS)
        self.assertTrue(result.installable, result.summary())

    def test_the_tutorial_goes_into_the_worlds_own_webworld(self) -> None:
        # No second WebWorld class: the one it has is the one that gains the tutorials.
        body = ast.parse(self.contents(self.patched)["__init__.py"]).body
        names = [node.name for node in body if isinstance(node, ast.ClassDef)]
        self.assertNotIn(GENERATED_WEB_CLASS, names)
        web = next(n for n in body if isinstance(n, ast.ClassDef) and n.name == "DemoWeb")
        assigned = {
            target.id
            for statement in web.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("tutorials", assigned)
        self.assertIn("theme", assigned, "the world's own attribute survives")

    def test_the_tutorial_names_the_file_that_gets_created(self) -> None:
        contents = self.contents(self.patched)
        self.assertIn(f'"{SETUP_DOC}"', contents["__init__.py"])
        self.assertIn(f"docs/{SETUP_DOC}", contents)


class TestRepairLimits(RepairTestCase):
    def test_an_existing_setup_guide_is_not_overwritten(self) -> None:
        source = self.build("demo", NO_TUTORIALS, docs=True)
        _applied, patched = self.repair(source)
        self.assertEqual("# existing guide\n", self.contents(patched)[f"docs/{SETUP_DOC}"])

    def test_a_world_with_nothing_repairable_is_left_alone(self) -> None:
        source = self.build("demo", "class Broken(World)\n    oops\n")
        before = verify_apworld(source, VERSIONS)
        self.assertFalse(repairable(before.findings))

    def test_the_repaired_source_still_parses(self) -> None:
        for init in (NO_WEB, NO_TUTORIALS):
            with self.subTest(init=init[:30]):
                source = self.build("demo", init, extra={"Items.py": "ITEMS = []\n"})
                _applied, patched = self.repair(source)
                ast.parse(self.contents(patched)["__init__.py"])

    def test_a_world_whose_webworld_lives_elsewhere_is_repaired_there(self) -> None:
        init = (
            "from worlds.AutoWorld import World\n"
            "from .web import DemoWeb\n"
            "\n\n"
            "class DemoWorld(World):\n" + WORLD_BODY.format(game="Demo") + "    web = DemoWeb()\n"
        )
        web_module = 'from worlds.AutoWorld import WebWorld\n\n\nclass DemoWeb(WebWorld):\n    theme = "g"\n'
        source = self.build("demo", init, extra={"web.py": web_module})
        applied, patched = self.repair(source)
        self.assertIn(WEB_NO_TUTORIALS, applied)
        contents = self.contents(patched)
        self.assertIn("tutorials = [setup_en]", contents["web.py"])
        self.assertNotIn("tutorials", contents["__init__.py"])
        self.assertTrue(verify_apworld(patched, VERSIONS).installable)


class TestAgainstTheBundledWorlds(unittest.TestCase):
    """Core's worlds are all correct, so none of them should look repairable."""

    def test_no_bundled_world_is_considered_repairable(self) -> None:
        root = Path(__file__).resolve().parents[2] / "worlds"
        checked = 0
        for world_dir in sorted(root.iterdir()):
            if not world_dir.is_dir() or world_dir.name.startswith(("_", ".")):
                continue
            if not (world_dir / "__init__.py").exists():
                continue
            checked += 1
            modules = {}
            for source_file in world_dir.rglob("*.py"):
                from tools.custom_worlds.webworld import module_path

                dotted = module_path(source_file.relative_to(world_dir).as_posix())
                if dotted is not None and source_file.stat().st_size <= 2 * 1024 * 1024:
                    modules[dotted] = source_file.read_text(encoding="utf-8", errors="replace")
            files = [item.relative_to(world_dir).as_posix() for item in world_dir.rglob("*")]
            findings = inspect_world(modules, module_name=world_dir.name, files=files)
            self.assertFalse(repairable(findings), f"{world_dir.name} looks like it needs repairing")
        self.assertGreater(checked, 20, "expected to find the bundled worlds")


if __name__ == "__main__":
    unittest.main()
