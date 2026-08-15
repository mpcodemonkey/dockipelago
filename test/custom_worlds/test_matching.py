"""Tests for telling one maintainer's games apart by name."""

import unittest

from tools.custom_worlds.matching import (
    MIN_MATCH_SCORE,
    best_score,
    matches,
    name_score,
    normalize,
    tokenize,
)


class TestNormalize(unittest.TestCase):
    def test_strips_case_and_punctuation(self) -> None:
        self.assertEqual("actraiser", normalize("Act-Raiser!"))
        self.assertEqual("thelegendofzelda", normalize("The Legend of Zelda"))

    def test_keeps_digits(self) -> None:
        self.assertEqual("runefactory5", normalize("Rune Factory 5"))


class TestTokenize(unittest.TestCase):
    def test_splits_on_separators_and_camel_case(self) -> None:
        self.assertEqual(["act", "raiser"], tokenize("ActRaiser"))
        self.assertEqual(["sonic", "battle"], tokenize("sonic_battle"))

    def test_splits_letters_from_digits(self) -> None:
        # Sequel numbers are their own token, so "Rune Factory" and "Rune Factory 5" stay distinct.
        self.assertEqual(["runefactory", "5"], tokenize("runefactory5"))
        self.assertEqual(["rune", "factory", "5"], tokenize("rune_factory_5"))

    def test_drops_noise_words(self) -> None:
        self.assertEqual(["kirby", "1"], tokenize("kirby_apworld_v1"))
        self.assertEqual(["kirby"], tokenize("kirby_apworld_archipelago"))

    def test_keeps_noise_words_when_that_is_all_there_is(self) -> None:
        self.assertEqual(["apworld"], tokenize("apworld"))


class TestNameScore(unittest.TestCase):
    def test_identical_names_score_full_marks(self) -> None:
        self.assertEqual(100, name_score("ActRaiser", "ActRaiser"))

    def test_separators_and_case_do_not_matter(self) -> None:
        for variant in ("actraiser", "act_raiser", "Act Raiser", "ACT-RAISER"):
            self.assertEqual(100, name_score("ActRaiser", variant), variant)

    def test_a_versioned_release_tag_still_matches(self) -> None:
        self.assertGreaterEqual(name_score("ActRaiser", "actraiser-v1.2.0"), MIN_MATCH_SCORE)
        self.assertGreaterEqual(name_score("Rune Factory", "runefactory-2.0.0"), MIN_MATCH_SCORE)

    def test_a_suffixed_asset_name_still_matches(self) -> None:
        self.assertGreaterEqual(name_score("Kirby Super Star", "kirby_super_star_apworld"), MIN_MATCH_SCORE)

    def test_unrelated_games_score_zero(self) -> None:
        self.assertEqual(0, name_score("ActRaiser", "sonic_battle"))
        self.assertEqual(0, name_score("ActRaiser", "runefactory"))
        self.assertEqual(0, name_score("Sonic Battle", "actraiser"))
        self.assertEqual(0, name_score("Rune Factory", "sonic_battle"))

    def test_a_shared_word_is_not_enough_on_its_own(self) -> None:
        # "Archipelago" and friends are stripped, so these have nothing real in common.
        self.assertEqual(0, name_score("ActRaiser Archipelago", "sonic_battle_archipelago"))

    def test_a_sequel_is_close_but_distinguishable(self) -> None:
        self.assertGreater(name_score("Rune Factory", "runefactory"), name_score("Rune Factory", "runefactory5"))

    def test_empty_names_score_zero(self) -> None:
        self.assertEqual(0, name_score("", "actraiser"))
        self.assertEqual(0, name_score("ActRaiser", ""))
        self.assertEqual(0, name_score("!!!", "actraiser"))

    def test_a_short_prefix_of_a_longer_name_is_not_a_match(self) -> None:
        # "Rune" appearing inside "runefactory" should not carry a match by itself.
        self.assertLess(_substring_only("Rune", "runefactory"), MIN_MATCH_SCORE)


class TestMatches(unittest.TestCase):
    def test_threshold_applies(self) -> None:
        self.assertTrue(matches("ActRaiser", "actraiser"))
        self.assertFalse(matches("ActRaiser", "sonic_battle"))

    def test_threshold_is_adjustable(self) -> None:
        self.assertFalse(matches("ActRaiser", "sonic_battle", threshold=1))


class TestBestScore(unittest.TestCase):
    def test_takes_the_strongest_of_several_candidates(self) -> None:
        self.assertEqual(100, best_score("ActRaiser", "sonic_battle", "actraiser", ""))

    def test_no_candidates_scores_zero(self) -> None:
        self.assertEqual(0, best_score("ActRaiser"))
        self.assertEqual(0, best_score("ActRaiser", "", ""))


def _substring_only(expected: str, value: str) -> int:
    from tools.custom_worlds.matching import _substring_score

    return _substring_score(normalize(expected), normalize(value))


if __name__ == "__main__":
    unittest.main()
