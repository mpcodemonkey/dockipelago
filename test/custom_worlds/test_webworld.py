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
    BAD_ESCAPE,
    DOCS_MISSING,
    PRESETS_NOT_NESTED,
    ROOT,
    SETTINGS_UNRESOLVABLE,
    SEVERITY_LOAD,
    SEVERITY_NOTE,
    SEVERITY_WEBHOST,
    TUTORIALS_NOT_A_LIST,
    UNPARSEABLE,
    WEB_MISSING,
    WEB_NO_TUTORIALS,
    WEB_NOT_INSTANTIATED,
    WEB_UNDEFINED,
    inspect_source,
    inspect_world,
    module_path,
    patch_endings,
    world_game,
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


class TestModulePath(unittest.TestCase):
    def test_init_is_the_package_root(self) -> None:
        self.assertEqual(ROOT, module_path("__init__.py"))

    def test_a_sibling_module(self) -> None:
        self.assertEqual("web", module_path("web.py"))

    def test_a_subpackage(self) -> None:
        self.assertEqual("sub", module_path("sub/__init__.py"))
        self.assertEqual("sub.web", module_path("sub/web.py"))

    def test_non_python_files_are_not_modules(self) -> None:
        self.assertIsNone(module_path("archipelago.json"))
        self.assertIsNone(module_path("docs/setup_en.md"))


class TestAcrossModules(unittest.TestCase):
    """Worlds that split World and WebWorld across files, which is most non-trivial ones.

    Each layout here was confirmed against this checkout: with tutorials the world is served, and
    without them WebHost.py drops it, exactly as the findings say.
    """

    NO_TUTORIALS = 'from worlds.AutoWorld import WebWorld\nclass GameWeb(WebWorld):\n    theme = "grass"\n'
    WITH_TUTORIALS = (
        "from worlds.AutoWorld import WebWorld\n"
        "from BaseClasses import Tutorial\n"
        'class GameWeb(WebWorld):\n    tutorials = [Tutorial("Setup", "d", "en", "s.md", "s/en", ["me"])]\n'
    )

    def codes(self, modules: dict[str, str]) -> list[str]:
        return [finding.code for finding in inspect_world(modules, module_name="mygame")]

    def test_a_webworld_in_another_module_is_checked(self) -> None:
        modules = {
            ROOT: 'from worlds.AutoWorld import World\nfrom .web import GameWeb\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "web": self.NO_TUTORIALS,
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))
        modules["web"] = self.WITH_TUTORIALS
        self.assertEqual([], self.codes(modules))

    def test_a_world_class_in_another_module_is_found(self) -> None:
        modules = {
            ROOT: "from .world import GameWorld\n",
            "world": 'from worlds.AutoWorld import World, WebWorld\nclass GameWeb(WebWorld):\n    theme = "g"\n'
                     'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))

    def test_a_star_imported_world_module_is_found(self) -> None:
        modules = {
            ROOT: "from .world import *\n",
            "world": 'from worlds.AutoWorld import World, WebWorld\nclass GameWeb(WebWorld):\n    theme = "g"\n'
                     'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))

    def test_a_webworld_base_in_another_module_is_followed(self) -> None:
        modules = {
            ROOT: 'from worlds.AutoWorld import World\nfrom .base import BaseWeb\n'
                  'class GameWeb(BaseWeb):\n    theme = "g"\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "base": "from worlds.AutoWorld import WebWorld\nclass BaseWeb(WebWorld):\n    pass\n",
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))

        modules["base"] = (
            "from worlds.AutoWorld import WebWorld\nfrom BaseClasses import Tutorial\n"
            'class BaseWeb(WebWorld):\n    tutorials = [Tutorial("S", "d", "en", "s.md", "s/en", ["m"])]\n'
        )
        self.assertEqual([], self.codes(modules))

    def test_a_nested_package_is_followed(self) -> None:
        modules = {
            ROOT: 'from worlds.AutoWorld import World\nfrom .sub.web import GameWeb\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "sub": "",
            "sub.web": self.NO_TUTORIALS,
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))

    def test_an_import_from_outside_the_world_stays_quiet(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World\nfrom some_other_package import GameWeb\n"
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
        }
        self.assertEqual([], self.codes(modules))

    def test_a_module_the_world_never_imports_is_ignored(self) -> None:
        # Dead code cannot register a class, so a broken world in it is not a problem.
        modules = {
            ROOT: "from worlds.AutoWorld import World, WebWorld\nfrom BaseClasses import Tutorial\n"
                  'class GameWeb(WebWorld):\n    tutorials = [Tutorial("S", "d", "en", "s.md", "s/en", ["m"])]\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "old_unused": 'from worlds.AutoWorld import World\nclass Dead(World):\n    game = "Dead"\n',
        }
        self.assertEqual([], self.codes(modules))

    def test_a_star_import_from_outside_silences_undefined_names(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World\nfrom some_other_package import *\n"
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
        }
        self.assertEqual([], self.codes(modules))

    def test_a_world_with_no_init_module_is_ignored(self) -> None:
        self.assertEqual([], self.codes({"web": self.NO_TUTORIALS}))

    def test_a_broken_module_the_world_imports_is_reported(self) -> None:
        # Python will parse it too, and raise where we did, so the world cannot load at all.
        modules = {
            ROOT: 'from worlds.AutoWorld import World\nfrom .web import GameWeb\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "web": "class GameWeb(WebWorld)\n    broken\n",
        }
        findings = inspect_world(modules, module_name="mygame")
        self.assertEqual([UNPARSEABLE], [f.code for f in findings])
        self.assertEqual(SEVERITY_LOAD, findings[0].severity)
        self.assertIn("mygame/web.py", findings[0].detail)

    def test_a_broken_module_the_world_never_imports_is_ignored(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World, WebWorld\nfrom BaseClasses import Tutorial\n"
                  'class GameWeb(WebWorld):\n    tutorials = [Tutorial("S", "d", "en", "s.md", "s/en", ["m"])]\n'
                  'class GameWorld(World):\n    game = "G"\n    web = GameWeb()\n',
            "scratch": "this is not python at all !!!\n",
        }
        self.assertEqual([], self.codes(modules))


class TestByteOrderMark(WebWorldTestCase):
    """A leading BOM used to make ast.parse raise, which silently skipped the whole module.

    Both sulfur and gta_sa reached a real Archipelago install this way: the checks saw an empty
    package, found nothing to complain about, and the world was installed anyway.
    """

    def test_a_bom_does_not_hide_a_broken_world(self) -> None:
        finding = self.only("﻿" + world("web = MyGameWeb"))
        self.assertEqual(WEB_NOT_INSTANTIATED, finding.code)  # type: ignore[attr-defined]

    def test_a_bom_on_a_healthy_world_is_still_clean(self) -> None:
        self.assertEqual([], self.codes("﻿" + world("web = MyGameWeb()")))


class TestModuleQualifiedReferences(WebWorldTestCase):
    """``web = web_world.MyGameWeb()`` is as common as the bare-name form, and used to be missed."""

    def codes(self, modules: dict[str, str]) -> list[str]:  # type: ignore[override]
        return [finding.code for finding in inspect_world(modules, module_name="mygame")]

    WEB_MODULE = (
        "from worlds.AutoWorld import WebWorld\nfrom BaseClasses import Tutorial\n"
        'class MyGameWeb(WebWorld):\n    tutorials = [Tutorial("S", "d", "en", "s.md", "s/en", ["m"])]\n'
    )

    def test_a_qualified_instance_is_accepted(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World\nfrom . import web_world\n"
                  'class MyGameWorld(World):\n    game = "G"\n    web = web_world.MyGameWeb()\n',
            "web_world": self.WEB_MODULE,
        }
        self.assertEqual([], self.codes(modules))

    def test_a_qualified_class_is_still_not_instantiated(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World\nfrom . import web_world\n"
                  'class MyGameWorld(World):\n    game = "G"\n    web = web_world.MyGameWeb\n',
            "web_world": self.WEB_MODULE,
        }
        self.assertEqual([WEB_NOT_INSTANTIATED], self.codes(modules))

    def test_a_qualified_webworld_without_tutorials_is_caught(self) -> None:
        modules = {
            ROOT: "from worlds.AutoWorld import World\nfrom . import web_world\n"
                  'class MyGameWorld(World):\n    game = "G"\n    web = web_world.MyGameWeb()\n',
            "web_world": "from worlds.AutoWorld import WebWorld\n"
                         'class MyGameWeb(WebWorld):\n    theme = "grass"\n',
        }
        self.assertEqual([WEB_NO_TUTORIALS], self.codes(modules))


class TestOptionsPresets(WebWorldTestCase):
    """A flat ``options_presets`` stops the WebHost booting, so it is worth catching statically."""

    def preset(self, body: str) -> list[str]:
        source = (
            "from worlds.AutoWorld import World, WebWorld, Tutorial\n"
            f"class MyGameWeb(WebWorld):\n{TUTORIALS}{body}"
            '\n\nclass MyGameWorld(World):\n    game = "My Game"\n    web = MyGameWeb()\n'
        )
        return self.codes(source)

    def test_a_preset_mapped_to_a_scalar_is_reported(self) -> None:
        codes = self.preset('    options_presets = {"Dragun": 3}\n')
        self.assertEqual([PRESETS_NOT_NESTED], codes)

    def test_a_preset_mapped_to_a_dict_is_accepted(self) -> None:
        codes = self.preset('    options_presets = {"Dragun": {"goal": 3}}\n')
        self.assertEqual([], codes)

    def test_a_preset_built_elsewhere_is_left_alone(self) -> None:
        codes = self.preset("    options_presets = build_presets()\n")
        self.assertEqual([], codes)

    def test_a_preset_holding_an_option_object_is_left_alone(self) -> None:
        # Enter The Gungeon's real mistake looked like this, but a name we cannot resolve to a
        # scalar is not evidence of one - only a literal is.
        codes = self.preset('    options_presets = {"Dragun": DragunGoal.default}\n')
        self.assertEqual([], codes)


class TestDocsFolder(WebWorldTestCase):
    """WebHost.py lists each world's docs/ at start-up, and one missing folder stops the site."""

    GOOD = world("web = MyGameWeb()")

    def codes_with(self, files: list[str] | None) -> list[str]:
        return [f.code for f in inspect_world({ROOT: self.GOOD}, module_name="mygame", files=files)]

    def test_tutorials_without_a_docs_folder_are_reported(self) -> None:
        finding = inspect_world({ROOT: self.GOOD}, module_name="mygame", files=["__init__.py"])[0]
        self.assertEqual(DOCS_MISSING, finding.code)
        self.assertEqual(SEVERITY_WEBHOST, finding.severity)
        self.assertIn("os.listdir", finding.detail)

    def test_a_docs_folder_satisfies_it(self) -> None:
        self.assertEqual([], self.codes_with(["__init__.py", "docs/setup_en.md"]))

    def test_a_caller_that_cannot_list_the_package_stays_quiet(self) -> None:
        # No file list means no evidence, and a guess here would delete a working world.
        self.assertEqual([], self.codes_with(None))

    def test_a_hidden_world_needs_no_docs(self) -> None:
        source = world("web = MyGameWeb()\nhidden = True")
        findings = inspect_world({ROOT: source}, module_name="mygame", files=["__init__.py"])
        self.assertEqual([], [f.code for f in findings])

    def test_a_world_with_no_tutorials_is_reported_for_that_instead(self) -> None:
        # It never reaches the docs copy, so naming docs too would just be noise.
        source = world("options_dataclass = None", web_class=False)
        findings = inspect_world({ROOT: source}, module_name="mygame", files=["__init__.py"])
        self.assertEqual([WEB_MISSING], [f.code for f in findings])


class TestSettingsAnnotation(WebWorldTestCase):
    """core resolves a string 'settings' annotation by stripping exactly one layer of brackets."""

    FUTURE = "from __future__ import annotations\nfrom typing import ClassVar\n"

    def annotate(self, annotation: str, *, future: bool = True) -> list[str]:
        header = (self.FUTURE if future else "from typing import ClassVar\n") + WORLD_HEADER
        source = world(f"web = MyGameWeb()\nsettings: {annotation}", header=header)
        return self.codes(source)

    def test_a_doubly_nested_annotation_is_reported(self) -> None:
        self.assertEqual([SETTINGS_UNRESOLVABLE], self.annotate("ClassVar[type[MySettings]]"))

    def test_the_ordinary_shape_is_accepted(self) -> None:
        self.assertEqual([], self.annotate("ClassVar[MySettings]"))

    def test_a_bare_class_is_accepted(self) -> None:
        self.assertEqual([], self.annotate("MySettings"))

    def test_a_dotted_annotation_is_reported_only_as_a_string(self) -> None:
        # core's sc2 writes exactly this and is fine, because without the future import the
        # annotation is an object and typing.get_args resolves it. Only the string branch breaks.
        self.assertEqual([], self.annotate("ClassVar[settings.MySettings]", future=False))
        self.assertEqual([SETTINGS_UNRESOLVABLE], self.annotate("ClassVar[settings.MySettings]"))

    def test_an_explicit_string_annotation_counts_without_the_future_import(self) -> None:
        self.assertEqual([SETTINGS_UNRESOLVABLE], self.annotate('"ClassVar[type[MySettings]]"', future=False))


class TestInvalidEscapes(WebWorldTestCase):
    def test_a_bad_escape_is_reported_as_a_note(self) -> None:
        source = world("web = MyGameWeb()\n" + r'link = "setup\en"')
        findings = inspect_source(source, module_name="mygame")
        self.assertEqual([BAD_ESCAPE], [f.code for f in findings])
        self.assertEqual(SEVERITY_NOTE, findings[0].severity)
        self.assertIn("__init__.py line", findings[0].detail)

    def test_a_real_escape_is_not_reported(self) -> None:
        self.assertEqual([], self.codes(world("web = MyGameWeb()\n" + r'text = "a\nb\\c"')))

    def test_a_raw_string_is_not_reported(self) -> None:
        self.assertEqual([], self.codes(world('web = MyGameWeb()\npath = r"setup\\en"')))


class TestPatchEndings(unittest.TestCase):
    """AutoPatchRegister keys patch extensions globally, on the class's own 'game' attribute."""

    def test_a_registered_ending_is_found(self) -> None:
        modules = {ROOT: 'class P:\n    game = "G"\n    patch_file_ending = ".apgl"\n'}
        self.assertEqual({".apgl"}, patch_endings(modules))

    def test_a_class_without_its_own_game_never_registers(self) -> None:
        # core checks "game" in dct, so an inherited game does not put the class in the registry.
        modules = {ROOT: 'class Base:\n    game = "G"\nclass P(Base):\n    patch_file_ending = ".apx"\n'}
        self.assertEqual(set(), patch_endings(modules))

    def test_zip_is_ignored(self) -> None:
        # core raises on ".zip" before the registry, so it can never be the thing two worlds share.
        modules = {ROOT: 'class P:\n    game = "G"\n    patch_file_ending = ".zip"\n'}
        self.assertEqual(set(), patch_endings(modules))

    def test_a_module_the_world_never_imports_still_counts(self) -> None:
        # Reached or not, the class registers as soon as its module is executed.
        modules = {ROOT: "", "Rom": 'class P:\n    game = "G"\n    patch_file_ending = ".apx"\n'}
        self.assertEqual({".apx"}, patch_endings(modules))

    def test_core_declares_the_endings_it_actually_registers(self) -> None:
        root = Path(__file__).resolve().parents[2] / "worlds"
        found: dict[str, set[str]] = {}
        for world_dir in sorted(root.iterdir()):
            if not world_dir.is_dir() or world_dir.name.startswith(("_", ".")):
                continue
            if not (world_dir / "__init__.py").exists():
                continue
            endings = patch_endings(_package_of(world_dir))
            if endings:
                found[world_dir.name] = endings
        self.assertEqual({".apgl"}, found.get("gl"), "core's Gauntlet Legends registers .apgl")
        # Core boots, so nothing it ships may collide with anything else it ships.
        seen: dict[str, str] = {}
        for name, endings in found.items():
            for ending in endings:
                self.assertNotIn(ending, seen, f"{name} and {seen.get(ending)} both claim {ending}")
                seen[ending] = name


class TestWorldGame(unittest.TestCase):
    def test_the_registered_game_is_read(self) -> None:
        self.assertEqual("My Game", world_game({ROOT: world("web = MyGameWeb()")}))

    def test_a_game_built_at_runtime_is_not_guessed_at(self) -> None:
        source = world("web = MyGameWeb()", header=WORLD_HEADER).replace('game = "My Game"', "game = make_name()")
        self.assertEqual("", world_game({ROOT: source}))


class TestAgainstTheBundledWorlds(unittest.TestCase):
    """Every world shipped with Archipelago should pass, which is the false-positive check."""

    def test_no_findings_for_any_core_world(self) -> None:
        root = Path(__file__).resolve().parents[2] / "worlds"
        inspected = 0
        problems: dict[str, list[str]] = {}
        for world_dir in sorted(root.iterdir()):
            name = world_dir.name
            if not world_dir.is_dir() or name.startswith(("_", ".")):
                continue
            if not (world_dir / "__init__.py").exists():
                continue
            inspected += 1
            # With the real file list, so the docs/ check is exercised against real worlds too.
            files = [item.relative_to(world_dir).as_posix() for item in world_dir.rglob("*")]
            findings = inspect_world(_package_of(world_dir), module_name=name, files=files)
            if findings:
                problems[name] = [finding.code for finding in findings]
        self.assertGreater(inspected, 20, "expected to find the bundled worlds")
        self.assertEqual({}, problems)


def _package_of(world_dir: Path) -> dict[str, str]:
    """Every Python module of a world on disk, keyed the way inspect_world expects."""
    modules: dict[str, str] = {}
    for source in world_dir.rglob("*.py"):
        dotted = module_path(source.relative_to(world_dir).as_posix())
        if dotted is not None and source.stat().st_size <= 2 * 1024 * 1024:
            modules[dotted] = source.read_text(encoding="utf-8", errors="replace")
    return modules


if __name__ == "__main__":
    unittest.main()
