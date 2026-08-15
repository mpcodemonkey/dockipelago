"""Tests for spotting broken WebWorld wiring in a world's ``__init__.py``.

The expected severities are not guesses: each was confirmed against this checkout by loading a world
with that mistake in it. ``web = MyWeb`` raises ``AssertionError: WebWorld has to be instantiated.``,
``web = Missing()`` raises ``NameError``, and both leave the world unregistered; a world with no
``web`` at all loads fine but its ``web.tutorials`` raises ``AttributeError``.
"""

import unittest
from pathlib import Path

from tools.custom_worlds.webworld import (
    UNPARSEABLE,
    WEB_MISSING,
    WEB_NOT_INSTANTIATED,
    WEB_UNDEFINED,
    inspect_source,
)

WORLD_HEADER = "from worlds.AutoWorld import World, WebWorld\n"


def world(body: str, *, header: str = WORLD_HEADER, web_class: bool = True) -> str:
    """Assemble a plausible world module around ``body``, which becomes the World class body."""
    web = "class MyGameWeb(WebWorld):\n    tutorials = []\n\n\n" if web_class else ""
    indented = "\n".join(f"    {line}" if line else "" for line in body.strip().splitlines())
    return f'{header}\n\n{web}class MyGameWorld(World):\n    game = "My Game"\n{indented}\n'


class WebWorldTestCase(unittest.TestCase):
    def codes(self, source: str) -> list[str]:
        return [finding.code for finding in inspect_source(source, module_name="mygame")]

    def only(self, source: str) -> object:
        findings = inspect_source(source, module_name="mygame")
        self.assertEqual(1, len(findings), f"expected exactly one finding, got {findings}")
        return findings[0]


class TestBrokenWiring(WebWorldTestCase):
    def test_assigning_the_class_instead_of_an_instance_is_fatal(self) -> None:
        finding = self.only(world("web = MyGameWeb"))
        self.assertEqual(WEB_NOT_INSTANTIATED, finding.code)  # type: ignore[attr-defined]
        self.assertTrue(finding.fatal)  # type: ignore[attr-defined]
        self.assertEqual("MyGameWorld", finding.world_class)  # type: ignore[attr-defined]
        self.assertIn("web = MyGameWeb()", finding.detail)  # type: ignore[attr-defined]

    def test_instantiating_a_name_that_does_not_exist_is_fatal(self) -> None:
        finding = self.only(world("web = MyGameWeb()", web_class=False))
        self.assertEqual(WEB_UNDEFINED, finding.code)  # type: ignore[attr-defined]
        self.assertTrue(finding.fatal)  # type: ignore[attr-defined]

    def test_assigning_a_name_that_does_not_exist_is_fatal(self) -> None:
        finding = self.only(world("web = some_web_instance", web_class=False))
        self.assertEqual(WEB_UNDEFINED, finding.code)  # type: ignore[attr-defined]

    def test_a_world_with_no_web_is_reported_but_not_fatal(self) -> None:
        finding = self.only(world("options_dataclass = None", web_class=False))
        self.assertEqual(WEB_MISSING, finding.code)  # type: ignore[attr-defined]
        self.assertFalse(finding.fatal)  # type: ignore[attr-defined]
        self.assertIn("tutorials", finding.detail)  # type: ignore[attr-defined]

    def test_a_bare_annotation_declares_nothing(self) -> None:
        self.assertEqual([WEB_MISSING], self.codes(world("web: WebWorld", web_class=False)))

    def test_unparseable_source_is_fatal(self) -> None:
        finding = self.only("class MyGameWorld(World)\n    game = 'My Game'\n")
        self.assertEqual(UNPARSEABLE, finding.code)  # type: ignore[attr-defined]
        self.assertTrue(finding.fatal)  # type: ignore[attr-defined]

    def test_every_broken_world_in_a_file_is_reported(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n    tutorials = []\n\n\n"
            + 'class OneWorld(World):\n    game = "One"\n    web = MyGameWeb\n\n\n'
            + 'class TwoWorld(World):\n    game = "Two"\n'
        )
        self.assertEqual({WEB_NOT_INSTANTIATED, WEB_MISSING}, set(self.codes(source)))


class TestCorrectWiring(WebWorldTestCase):
    def test_the_standard_shape_is_clean(self) -> None:
        self.assertEqual([], self.codes(world("web = MyGameWeb()")))

    def test_an_annotated_assignment_is_clean(self) -> None:
        source = world(
            "web: ClassVar[WebWorld] = MyGameWeb()",
            header=WORLD_HEADER + "from typing import ClassVar\n",
        )
        self.assertEqual([], self.codes(source))

    def test_a_module_level_instance_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n    tutorials = []\n\n\n"
            + "_web = MyGameWeb()\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = _web\n'
        )
        self.assertEqual([], self.codes(source))

    def test_web_patched_on_after_the_class_body_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n    tutorials = []\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n\n\n'
            + "MyGameWorld.web = MyGameWeb()\n"
        )
        self.assertEqual([], self.codes(source))

    def test_web_inherited_from_a_local_base_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n    tutorials = []\n\n\n"
            + "class BaseWorld(World):\n    web = MyGameWeb()\n\n\n"
            + 'class MyGameWorld(BaseWorld):\n    game = "My Game"\n'
        )
        self.assertEqual([], self.codes(source))

    def test_a_file_with_no_world_in_it_is_clean(self) -> None:
        self.assertEqual([], self.codes("def helper():\n    return 1\n"))

    def test_a_webworld_subclass_alone_is_not_mistaken_for_a_world(self) -> None:
        self.assertEqual([], self.codes(WORLD_HEADER + "\n\nclass MyGameWeb(WebWorld):\n    tutorials = []\n"))


class TestCrossModuleLayoutsAreLeftAlone(WebWorldTestCase):
    """Worlds that split the WebWorld into another file cannot be judged from __init__.py alone."""

    def test_instantiating_an_imported_webworld_is_clean(self) -> None:
        source = world("web = MyGameWeb()", header=WORLD_HEADER + "from .web import MyGameWeb\n", web_class=False)
        self.assertEqual([], self.codes(source))

    def test_assigning_an_imported_instance_is_clean(self) -> None:
        source = world("web = my_web", header=WORLD_HEADER + "from .web import my_web\n", web_class=False)
        self.assertEqual([], self.codes(source))

    def test_an_aliased_import_is_clean(self) -> None:
        source = world(
            "web = Renamed()",
            header=WORLD_HEADER + "from .web import MyGameWeb as Renamed\n",
            web_class=False,
        )
        self.assertEqual([], self.codes(source))

    def test_a_dotted_reference_is_clean(self) -> None:
        source = world("web = web_module.MyGameWeb()", header=WORLD_HEADER + "from . import web_module\n",
                       web_class=False)
        self.assertEqual([], self.codes(source))

    def test_a_world_inheriting_an_unseen_base_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "from .common import SharedWorld\n\n\n"
            + 'class MyGameWorld(SharedWorld, World):\n    game = "My Game"\n'
        )
        self.assertEqual([], self.codes(source))

    def test_a_web_factory_function_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\ndef build_web():\n    return WebWorld()\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = build_web()\n'
        )
        self.assertEqual([], self.codes(source))


class TestAgainstTheBundledWorlds(unittest.TestCase):
    """Every world shipped with Archipelago should pass, which is the false-positive check."""

    def test_no_findings_for_any_core_world(self) -> None:
        root = Path(__file__).resolve().parents[2] / "worlds"
        inspected = 0
        problems: dict[str, list[str]] = {}
        for init in sorted(root.glob("*/__init__.py")):
            name = init.parent.name
            if name.startswith(("_", ".")):
                continue
            inspected += 1
            findings = inspect_source(init.read_text(encoding="utf-8", errors="replace"), module_name=name)
            if findings:
                problems[name] = [finding.code for finding in findings]
        self.assertGreater(inspected, 20, "expected to find the bundled worlds")
        self.assertEqual({}, problems)


if __name__ == "__main__":
    unittest.main()
