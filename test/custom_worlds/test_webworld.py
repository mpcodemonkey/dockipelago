"""Tests for spotting broken WebWorld wiring in a world's ``__init__.py``.

The expected severities are not guesses: each was confirmed against this checkout by loading a world
with that mistake in it. ``web = MyWeb`` raises ``AssertionError: WebWorld has to be instantiated.``,
``web = Missing()`` raises ``NameError``, and both leave the world unregistered; a world with no
``web`` at all, or one whose WebWorld has no ``tutorials``, loads fine but is dropped by
``WebHost.py``, whose filter is literally ``hasattr(world.web, "tutorials")``.
"""

import unittest
from pathlib import Path

from tools.custom_worlds.webworld import (
    SEVERITY_LOAD,
    SEVERITY_WEBHOST,
    TUTORIALS_NOT_A_LIST,
    UNPARSEABLE,
    WEB_MISSING,
    WEB_NO_TUTORIALS,
    WEB_NOT_INSTANTIATED,
    WEB_UNDEFINED,
    inspect_source,
)

WORLD_HEADER = "from worlds.AutoWorld import World, WebWorld, Tutorial\n"
TUTORIALS = '    tutorials = [Tutorial(tutorial_name="Setup Guide", file_name="setup.md")]\n'


def world(body: str, *, header: str = WORLD_HEADER, web_class: bool = True) -> str:
    """Assemble a plausible world module around ``body``, which becomes the World class body."""
    web = f"class MyGameWeb(WebWorld):\n{TUTORIALS}\n\n" if web_class else ""
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
        self.assertEqual(SEVERITY_LOAD, finding.severity)  # type: ignore[attr-defined]
        self.assertEqual("MyGameWorld", finding.world_class)  # type: ignore[attr-defined]
        self.assertIn("web = MyGameWeb()", finding.detail)  # type: ignore[attr-defined]

    def test_instantiating_a_name_that_does_not_exist_is_fatal(self) -> None:
        finding = self.only(world("web = MyGameWeb()", web_class=False))
        self.assertEqual(WEB_UNDEFINED, finding.code)  # type: ignore[attr-defined]
        self.assertEqual(SEVERITY_LOAD, finding.severity)  # type: ignore[attr-defined]

    def test_assigning_a_name_that_does_not_exist_is_fatal(self) -> None:
        finding = self.only(world("web = some_web_instance", web_class=False))
        self.assertEqual(WEB_UNDEFINED, finding.code)  # type: ignore[attr-defined]

    def test_a_world_with_no_web_is_reported_but_not_fatal(self) -> None:
        finding = self.only(world("options_dataclass = None", web_class=False))
        self.assertEqual(WEB_MISSING, finding.code)  # type: ignore[attr-defined]
        self.assertEqual(SEVERITY_WEBHOST, finding.severity)  # type: ignore[attr-defined]
        self.assertIn("invalid for WebHost", finding.detail)  # type: ignore[attr-defined]

    def test_a_bare_annotation_declares_nothing(self) -> None:
        self.assertEqual([WEB_MISSING], self.codes(world("web: WebWorld", web_class=False)))

    def test_unparseable_source_is_fatal(self) -> None:
        finding = self.only("class MyGameWorld(World)\n    game = 'My Game'\n")
        self.assertEqual(UNPARSEABLE, finding.code)  # type: ignore[attr-defined]
        self.assertEqual(SEVERITY_LOAD, finding.severity)  # type: ignore[attr-defined]

    def test_every_broken_world_in_a_file_is_reported(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n" + TUTORIALS + "\n\n"
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
            + "\n\nclass MyGameWeb(WebWorld):\n" + TUTORIALS + "\n\n"
            + "_web = MyGameWeb()\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = _web\n'
        )
        self.assertEqual([], self.codes(source))

    def test_web_patched_on_after_the_class_body_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n" + TUTORIALS + "\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n\n\n'
            + "MyGameWorld.web = MyGameWeb()\n"
        )
        self.assertEqual([], self.codes(source))

    def test_web_inherited_from_a_local_base_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n" + TUTORIALS + "\n\n"
            + "class BaseWorld(World):\n    web = MyGameWeb()\n\n\n"
            + 'class MyGameWorld(BaseWorld):\n    game = "My Game"\n'
        )
        self.assertEqual([], self.codes(source))

    def test_a_file_with_no_world_in_it_is_clean(self) -> None:
        self.assertEqual([], self.codes("def helper():\n    return 1\n"))

    def test_a_webworld_subclass_alone_is_not_mistaken_for_a_world(self) -> None:
        self.assertEqual([], self.codes(WORLD_HEADER + "\n\nclass MyGameWeb(WebWorld):\n" + TUTORIALS))


class TestTutorials(WebWorldTestCase):
    """A WebWorld only counts for the WebHost once it carries a tutorials list."""

    def build(self, web_body: str) -> str:
        return (
            WORLD_HEADER
            + "\n\nclass MyGameWeb(WebWorld):\n"
            + web_body
            + '\n\nclass MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )

    def test_a_full_tutorial_block_is_clean(self) -> None:
        source = self.build(
            "    setup = Tutorial(\n"
            '        tutorial_name = "Setup Guide",\n'
            '        description = "A guide to setting up the game",\n'
            '        language = "English",\n'
            '        file_name = "setup.md",\n'
            '        link = "setup/en",\n'
            '        authors = ["Someone"]\n'
            "    )\n"
            "    tutorials = [setup]\n"
        )
        self.assertEqual([], self.codes(source))

    def test_a_webworld_without_tutorials_is_reported(self) -> None:
        finding = self.only(self.build('    theme = "grass"\n'))
        self.assertEqual(WEB_NO_TUTORIALS, finding.code)  # type: ignore[attr-defined]
        self.assertEqual(SEVERITY_WEBHOST, finding.severity)  # type: ignore[attr-defined]
        self.assertIn("invalid for WebHost", finding.detail)  # type: ignore[attr-defined]
        self.assertIn("Tutorial", finding.detail)  # type: ignore[attr-defined]

    def test_a_commented_out_tutorial_block_counts_as_absent(self) -> None:
        # Parsing gives this for free: commented code is simply not in the tree.
        source = self.build(
            '    # setup = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n'
            "    # tutorials = [setup]\n"
            '    theme = "grass"\n'
        )
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(source))

    def test_a_bare_tutorials_annotation_counts_as_absent(self) -> None:
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(self.build("    tutorials: list\n")))

    def test_an_empty_list_is_accepted_because_core_accepts_it(self) -> None:
        # WebHost.py only asks hasattr(web, "tutorials"), so an empty list passes.
        self.assertEqual([], self.codes(self.build("    tutorials = []\n")))

    def test_a_lone_tutorial_without_brackets_is_reported(self) -> None:
        source = self.build('    tutorials = Tutorial(tutorial_name="Setup Guide", file_name="setup.md")\n')
        finding = self.only(source)
        self.assertEqual(TUTORIALS_NOT_A_LIST, finding.code)  # type: ignore[attr-defined]
        self.assertEqual(SEVERITY_WEBHOST, finding.severity)  # type: ignore[attr-defined]

    def test_a_string_is_reported(self) -> None:
        self.assertEqual([TUTORIALS_NOT_A_LIST], self.codes(self.build('    tutorials = "setup"\n')))

    def test_a_tuple_is_accepted(self) -> None:
        self.assertEqual([], self.codes(self.build('    tutorials = (Tutorial(file_name="setup.md"),)\n')))

    def test_a_module_level_list_is_accepted(self) -> None:
        source = (
            WORLD_HEADER
            + '\n\n_tutorials = [Tutorial(file_name="setup.md")]\n\n\n'
            + "class MyGameWeb(WebWorld):\n    tutorials = _tutorials\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        self.assertEqual([], self.codes(source))

    def test_tutorials_from_a_factory_call_is_left_alone(self) -> None:
        # build_tutorials() may well return a list; only a bare Tutorial(...) is provably wrong.
        source = (
            WORLD_HEADER
            + "\n\ndef build_tutorials():\n    return []\n\n\n"
            + "class MyGameWeb(WebWorld):\n    tutorials = build_tutorials()\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        self.assertEqual([], self.codes(source))

    def test_tutorials_inherited_from_a_local_base_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "\n\nclass BaseWeb(WebWorld):\n" + TUTORIALS + "\n\n"
            + 'class MyGameWeb(BaseWeb):\n    theme = "grass"\n\n\n'
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        self.assertEqual([], self.codes(source))

    def test_tutorials_inherited_from_an_unseen_base_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + "from .common import SharedWeb\n\n\n"
            + 'class MyGameWeb(SharedWeb):\n    theme = "grass"\n\n\n'
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        self.assertEqual([], self.codes(source))

    def test_tutorials_patched_on_after_the_class_is_clean(self) -> None:
        source = (
            WORLD_HEADER
            + '\n\nclass MyGameWeb(WebWorld):\n    theme = "grass"\n\n\n'
            + "MyGameWeb.tutorials = []\n\n\n"
            + 'class MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        self.assertEqual([], self.codes(source))

    def test_an_imported_webworld_is_not_checked_for_tutorials(self) -> None:
        source = world("web = MyGameWeb()", header=WORLD_HEADER + "from .web import MyGameWeb\n", web_class=False)
        self.assertEqual([], self.codes(source))


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
