"""Tests for checking a downloaded apworld against this Archipelago checkout."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from test.custom_worlds.helpers import default_manifest, make_apworld
from tools.custom_worlds.verify import (
    STATUS_INCOMPATIBLE,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_WARNING,
    WEBHOST_ERROR,
    WEBHOST_OFF,
    WEBHOST_WARN,
    CoreVersions,
    VerificationResult,
    detect_core_versions,
    find_conflicts,
    parse_version,
    verify_apworld,
)

VERSIONS = CoreVersions(ap_version=(0, 6, 8), container_version=7)


class ApworldTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def verify(self, name: str = "mygame.apworld", **kwargs: object) -> VerificationResult:
        path = make_apworld(self.tmp / name, **kwargs)  # type: ignore[arg-type]
        return verify_apworld(path, VERSIONS)


class TestParseVersion(unittest.TestCase):
    def test_parses_a_three_part_version(self) -> None:
        self.assertEqual((0, 6, 8), parse_version("0.6.8"))

    def test_rejects_non_numeric_parts(self) -> None:
        self.assertIsNone(parse_version("0.6.8-rc1"))
        self.assertIsNone(parse_version("latest"))


class TestDetectCoreVersions(unittest.TestCase):
    def test_reads_the_real_checkout(self) -> None:
        root = Path(__file__).resolve().parents[2]
        versions = detect_core_versions(root)
        self.assertEqual(3, len(versions.ap_version))
        self.assertGreaterEqual(versions.ap_version, (0, 6, 0))
        self.assertGreaterEqual(versions.container_version, 5)

    def test_reads_values_out_of_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "worlds").mkdir()
            (root / "Utils.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
            (root / "worlds" / "Files.py").write_text("container_version: int = 42\n", encoding="utf-8")
            self.assertEqual(CoreVersions((1, 2, 3), 42), detect_core_versions(root))

    def test_falls_back_when_the_checkout_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            versions = detect_core_versions(Path(name))
        self.assertEqual(3, len(versions.ap_version))


class TestVerifyApworld(ApworldTestCase):
    def test_a_well_formed_apworld_passes(self) -> None:
        result = self.verify(manifest=default_manifest())
        self.assertEqual(STATUS_OK, result.status, result.summary())
        self.assertEqual([], result.errors)
        self.assertEqual("Test Game", result.game)
        self.assertEqual("mygame", result.module_name)
        self.assertTrue(result.installable)

    def test_manifest_at_the_archive_root_is_also_accepted(self) -> None:
        result = self.verify(manifest=default_manifest(), manifest_at_root=True)
        self.assertEqual(STATUS_OK, result.status, result.summary())

    def test_manifest_fields_are_parsed(self) -> None:
        result = self.verify(manifest=default_manifest(world_version="2.1.4", authors=["Someone"]))
        assert result.manifest is not None
        self.assertEqual((2, 1, 4), result.manifest.world_version)
        self.assertEqual((0, 6, 0), result.manifest.minimum_ap_version)
        self.assertEqual(["Someone"], result.manifest.authors)

    def test_wrong_extension_is_invalid(self) -> None:
        result = self.verify(name="mygame.zip", module="mygame", manifest=default_manifest())
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("does not end in .apworld", result.errors[0])

    def test_uppercase_file_name_warns(self) -> None:
        result = self.verify(name="MyGame.apworld", manifest=default_manifest())
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("lower case" in warning for warning in result.warnings))
        self.assertTrue(result.installable)

    def test_missing_inner_folder_is_invalid(self) -> None:
        result = self.verify(module="", manifest=default_manifest(), manifest_at_root=True)
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("does not contain mygame/__init__.py", result.errors[0])

    def test_mismatched_inner_folder_is_invalid_and_explains_why(self) -> None:
        result = self.verify(module="MyGame", manifest=default_manifest())
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("MyGame/__init__.py", result.errors[0])
        self.assertIn("must match the file name", result.errors[0])

    def test_bytecode_only_world_warns(self) -> None:
        path = self.tmp / "mygame.apworld"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mygame/__init__.pyc", b"\x00\x00")
            archive.writestr("mygame/archipelago.json", json.dumps(default_manifest()))
        result = verify_apworld(path, VERSIONS)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("bytecode" in warning for warning in result.warnings))

    def test_not_a_zip_is_invalid(self) -> None:
        path = self.tmp / "mygame.apworld"
        path.write_bytes(b"this is not a zip file")
        result = verify_apworld(path, VERSIONS)
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("not a valid zip", result.errors[0])

    def test_corrupt_archive_is_invalid(self) -> None:
        path = make_apworld(self.tmp / "mygame.apworld", manifest=default_manifest())
        data = bytearray(path.read_bytes())
        data[40:60] = b"\x00" * 20  # damage the compressed payload, not the central directory
        path.write_bytes(bytes(data))
        result = verify_apworld(path, VERSIONS)
        self.assertEqual(STATUS_INVALID, result.status)

    def test_missing_manifest_warns_about_0_7_0(self) -> None:
        result = self.verify(manifest=None)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("0.7.0" in warning for warning in result.warnings))
        self.assertTrue(result.installable)

    def test_unparseable_manifest_is_invalid(self) -> None:
        path = self.tmp / "mygame.apworld"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mygame/__init__.py", "")
            archive.writestr("mygame/archipelago.json", "{not json")
        result = verify_apworld(path, VERSIONS)
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("not valid JSON", result.errors[0])

    def test_missing_game_field_warns(self) -> None:
        manifest = default_manifest()
        del manifest["game"]
        result = self.verify(manifest=manifest)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("'game' field" in warning for warning in result.warnings))

    def test_unparseable_version_field_is_invalid(self) -> None:
        result = self.verify(manifest=default_manifest(world_version="v2"))
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertIn("major.minor.build", result.errors[0])

    def test_minimum_ap_version_above_core_is_incompatible(self) -> None:
        result = self.verify(manifest=default_manifest(minimum_ap_version="0.7.0"))
        self.assertEqual(STATUS_INCOMPATIBLE, result.status)
        self.assertIn("needs Archipelago >= 0.7.0", result.errors[0])
        self.assertFalse(result.installable)

    def test_minimum_ap_version_equal_to_core_is_fine(self) -> None:
        result = self.verify(manifest=default_manifest(minimum_ap_version="0.6.8"))
        self.assertEqual(STATUS_OK, result.status, result.summary())

    def test_maximum_ap_version_below_core_is_incompatible(self) -> None:
        result = self.verify(manifest=default_manifest(maximum_ap_version="0.6.4"))
        self.assertEqual(STATUS_INCOMPATIBLE, result.status)
        self.assertIn("supports Archipelago <= 0.6.4", result.errors[0])

    def test_newer_container_format_is_incompatible(self) -> None:
        result = self.verify(manifest=default_manifest(compatible_version=8))
        self.assertEqual(STATUS_INCOMPATIBLE, result.status)
        self.assertIn("APContainer version 8", result.errors[0])

    def test_older_container_format_is_fine(self) -> None:
        result = self.verify(manifest=default_manifest(compatible_version=5))
        self.assertEqual(STATUS_OK, result.status, result.summary())

    def test_missing_compatible_version_warns(self) -> None:
        manifest = default_manifest()
        del manifest["compatible_version"]
        result = self.verify(manifest=manifest)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("Build APWorlds" in warning for warning in result.warnings))

    def test_the_worst_problem_wins(self) -> None:
        # Broken layout (invalid) alongside an unsupported container format (incompatible).
        result = self.verify(name="mygame.apworld", module="other", manifest=default_manifest(compatible_version=99))
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertEqual(2, len(result.errors), result.summary())

    def test_a_warning_does_not_mask_an_incompatibility(self) -> None:
        result = self.verify(name="MyGame.apworld", module="MyGame", manifest=default_manifest(compatible_version=99))
        self.assertEqual(STATUS_INCOMPATIBLE, result.status)
        self.assertTrue(any("lower case" in warning for warning in result.warnings))


class TestWebWorldChecks(ApworldTestCase):
    """The WebWorld analysis, reached through verify_apworld rather than directly."""

    GOOD = (
        "from worlds.AutoWorld import World, WebWorld, Tutorial\n"
        "class MyGameWeb(WebWorld):\n"
        '    tutorials = [Tutorial(tutorial_name="Setup Guide", file_name="setup.md")]\n'
        'class MyGameWorld(World):\n    game = "Test Game"\n    web = MyGameWeb()\n'
    )
    NOT_INSTANTIATED = GOOD.replace("web = MyGameWeb()", "web = MyGameWeb")
    NO_TUTORIALS = GOOD.replace(
        '    tutorials = [Tutorial(tutorial_name="Setup Guide", file_name="setup.md")]\n',
        '    theme = "grass"\n',
    )
    NO_WEB = "from worlds.AutoWorld import World\n" + 'class MyGameWorld(World):\n    game = "Test Game"\n'

    def test_correct_wiring_passes(self) -> None:
        result = self.verify(manifest=default_manifest(), init_source=self.GOOD)
        self.assertEqual(STATUS_OK, result.status, result.summary())

    def test_an_uninstantiated_webworld_is_invalid(self) -> None:
        result = self.verify(manifest=default_manifest(), init_source=self.NOT_INSTANTIATED)
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertFalse(result.installable)
        self.assertTrue(any("has to be instantiated" in error for error in result.errors), result.errors)

    def test_unparseable_source_is_invalid(self) -> None:
        result = self.verify(manifest=default_manifest(), init_source="class MyGameWorld(World)\n    pass\n")
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertTrue(any("not valid Python" in error for error in result.errors), result.errors)

    def test_a_world_defining_its_webworld_elsewhere_is_left_alone(self) -> None:
        source = (
            "from worlds.AutoWorld import World\n"
            "from .web import MyGameWeb\n"
            'class MyGameWorld(World):\n    game = "Test Game"\n    web = MyGameWeb()\n'
        )
        result = self.verify(manifest=default_manifest(), init_source=source)
        self.assertEqual(STATUS_OK, result.status, result.summary())

    def test_a_bytecode_only_world_is_not_analysed(self) -> None:
        path = self.tmp / "mygame.apworld"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mygame/__init__.pyc", b"\x00\x00")
            archive.writestr("mygame/archipelago.json", json.dumps(default_manifest()))
        result = verify_apworld(path, VERSIONS)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(any("bytecode" in warning for warning in result.warnings))


class TestWebHostPolicy(ApworldTestCase):
    """What happens to worlds that load but that WebHost.py would drop from the site."""

    def verify_with(self, source: str, policy: str) -> VerificationResult:
        path = make_apworld(self.tmp / "mygame.apworld", manifest=default_manifest(), init_source=source)
        return verify_apworld(path, VERSIONS, webhost_check=policy)

    def test_no_tutorials_is_refused_by_default(self) -> None:
        result = self.verify(manifest=default_manifest(), init_source=TestWebWorldChecks.NO_TUTORIALS)
        self.assertEqual(STATUS_INVALID, result.status)
        self.assertFalse(result.installable)
        self.assertTrue(any("invalid for WebHost" in error for error in result.errors), result.errors)

    def test_no_web_is_refused_by_default(self) -> None:
        result = self.verify(manifest=default_manifest(), init_source=TestWebWorldChecks.NO_WEB)
        self.assertEqual(STATUS_INVALID, result.status)

    def test_warn_installs_it_anyway(self) -> None:
        result = self.verify_with(TestWebWorldChecks.NO_TUTORIALS, WEBHOST_WARN)
        self.assertEqual(STATUS_WARNING, result.status)
        self.assertTrue(result.installable)
        self.assertTrue(any("invalid for WebHost" in warning for warning in result.warnings))

    def test_off_says_nothing_at_all(self) -> None:
        result = self.verify_with(TestWebWorldChecks.NO_TUTORIALS, WEBHOST_OFF)
        self.assertEqual(STATUS_OK, result.status, result.summary())
        self.assertEqual([], result.warnings)

    def test_the_policy_never_rescues_a_world_that_cannot_load(self) -> None:
        for policy in (WEBHOST_ERROR, WEBHOST_WARN, WEBHOST_OFF):
            result = self.verify_with(TestWebWorldChecks.NOT_INSTANTIATED, policy)
            self.assertEqual(STATUS_INVALID, result.status, policy)

    def test_a_correct_world_passes_under_every_policy(self) -> None:
        for policy in (WEBHOST_ERROR, WEBHOST_WARN, WEBHOST_OFF):
            self.assertEqual(STATUS_OK, self.verify_with(TestWebWorldChecks.GOOD, policy).status, policy)


class TestFindConflicts(unittest.TestCase):
    def _result(self, name: str, game: str | None) -> VerificationResult:
        from tools.custom_worlds.verify import Manifest

        return VerificationResult(path=Path("/staging") / name, manifest=Manifest(game=game))

    def test_no_conflicts_when_everything_is_distinct(self) -> None:
        results = [self._result("a.apworld", "Game A"), self._result("b.apworld", "Game B")]
        self.assertEqual({}, find_conflicts(results, existing_worlds=set()))

    def test_shadowing_a_bundled_world_is_reported(self) -> None:
        results = [self._result("ror2.apworld", "Risk of Rain 2")]
        conflicts = find_conflicts(results, existing_worlds={"ror2"})
        self.assertIn(Path("/staging/ror2.apworld"), conflicts)
        self.assertIn("already exists in worlds/", conflicts[Path("/staging/ror2.apworld")][0])

    def test_two_files_claiming_the_same_game_are_both_reported(self) -> None:
        results = [self._result("a.apworld", "Same Game"), self._result("b.apworld", "Same Game")]
        conflicts = find_conflicts(results, existing_worlds=set())
        self.assertEqual(2, len(conflicts))
        for messages in conflicts.values():
            self.assertIn("provided by more than one file", messages[0])

    def test_worlds_without_a_game_name_do_not_collide(self) -> None:
        results = [self._result("a.apworld", None), self._result("b.apworld", None)]
        self.assertEqual({}, find_conflicts(results, existing_worlds=set()))


if __name__ == "__main__":
    unittest.main()
