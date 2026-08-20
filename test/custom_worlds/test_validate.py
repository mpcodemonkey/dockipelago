"""Tests for the Archipelago-backed validation steps."""

import unittest
from pathlib import Path
from unittest import mock

from tools.custom_worlds.validate import (
    _CONFIRM_SCRIPT,
    _GENERATION_SCRIPT,
    CONFIRM_SEEDS,
    GENERATION_FAILED,
    ValidationReport,
    WorldVerdict,
    _confirm,
)


class TestGenerationScripts(unittest.TestCase):
    """The confirm script is built by rewriting the sweep script, which can silently stop matching."""

    def test_the_confirm_script_really_narrows_the_registry(self) -> None:
        # If the line it rewrites is ever reworded, the replacement no-ops and "confirming" a
        # failure becomes another full sweep - reproducing the contamination it exists to rule out.
        self.assertNotEqual(_GENERATION_SCRIPT, _CONFIRM_SCRIPT)
        self.assertIn("CW_ONLY_GAME", _CONFIRM_SCRIPT)
        self.assertNotIn("CW_ONLY_GAME", _GENERATION_SCRIPT)

    def test_both_scripts_are_valid_python(self) -> None:
        for name, script in (("sweep", _GENERATION_SCRIPT), ("confirm", _CONFIRM_SCRIPT)):
            with self.subTest(script=name):
                compile(script, f"<{name}>", "exec")

    def test_the_sweep_skips_hidden_worlds_and_test_fixtures(self) -> None:
        # Archipelago's own placeholder world is hidden, and importing test.general registers
        # fixture worlds; neither is a game anyone installs.
        self.assertIn('getattr(world, "hidden", False)', _GENERATION_SCRIPT)
        self.assertIn("installed = dict(AutoWorldRegister.world_types)", _GENERATION_SCRIPT)
        snapshot = _GENERATION_SCRIPT.index("installed = dict(AutoWorldRegister.world_types)")
        test_import = _GENERATION_SCRIPT.index("from test.general import")
        self.assertLess(snapshot, test_import, "the snapshot has to precede the test.general import")


class TestConfirmingAFailure(unittest.TestCase):
    """A world is only reported when it fails alone, on every seed tried."""

    def setUp(self) -> None:
        self.verdict = WorldVerdict(game="Some Game", module="some", status=GENERATION_FAILED, reason="boom")
        self.seeds: list[str] = []

    def confirm(self, results: list[ValidationReport]) -> WorldVerdict | None:
        answers = iter(results)

        def fake_run(script, root, python, timeout, environment):  # type: ignore[no-untyped-def]
            self.seeds.append(environment["CW_SEED"])
            return next(answers)

        with mock.patch("tools.custom_worlds.validate._run", fake_run):
            return _confirm(self.verdict, Path("."), "python", 1.0, {}, 1)

    def clean(self) -> ValidationReport:
        return ValidationReport(ok=True, registered=1, verdicts=[])

    def failed(self) -> ValidationReport:
        return ValidationReport(ok=True, registered=1, verdicts=[self.verdict])

    def test_a_world_that_generates_on_the_first_retry_is_cleared(self) -> None:
        self.assertIsNone(self.confirm([self.clean()]))
        self.assertEqual(1, len(self.seeds), "no more seeds are tried once one works")

    def test_a_world_that_generates_on_a_later_seed_is_cleared(self) -> None:
        self.assertIsNone(self.confirm([self.failed(), self.clean()]))

    def test_a_world_that_never_generates_is_reported(self) -> None:
        confirmed = self.confirm([self.failed() for _ in CONFIRM_SEEDS])
        self.assertIsNotNone(confirmed)
        self.assertEqual(len(CONFIRM_SEEDS), len(self.seeds), "every seed is tried before reporting")

    def test_each_retry_uses_a_different_seed(self) -> None:
        self.confirm([self.failed() for _ in CONFIRM_SEEDS])
        self.assertEqual(len(set(self.seeds)), len(self.seeds))

    def test_a_retry_that_cannot_run_leaves_the_world_reported(self) -> None:
        # Failing to re-check is not evidence the world is fine.
        confirmed = self.confirm([ValidationReport(ok=False, error="no interpreter")])
        self.assertIs(self.verdict, confirmed)


if __name__ == "__main__":
    unittest.main()
