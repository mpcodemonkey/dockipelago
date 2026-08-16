"""Tests for telling one maintainer's games apart by name."""

import unittest

from tools.custom_worlds.matching import (
    MIN_MATCH_SCORE,
    best_score,
    matches,
    name_score,
    normalize,
    strip_version,
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
        self.assertEqual(["kirby"], tokenize("kirby_apworld_v1"))
        self.assertEqual(["kirby"], tokenize("kirby_apworld_archipelago"))

    def test_keeps_noise_words_when_that_is_all_there_is(self) -> None:
        self.assertEqual(["apworld"], tokenize("apworld"))


class TestStripVersion(unittest.TestCase):
    def test_removes_a_v_prefixed_version(self) -> None:
        self.assertEqual(["mega", "man", "x", "2"], tokenize("Mega Man X2 v1.1"))

    def test_removes_a_dotted_version(self) -> None:
        self.assertEqual(["runefactory"], tokenize("runefactory-2.0.0"))

    def test_removes_a_version_after_an_underscore(self) -> None:
        # "_" is a word character, so this only works because separators are normalised first.
        self.assertEqual(["mmx"], tokenize("mmx_v1.4"))

    def test_keeps_a_bare_series_number(self) -> None:
        # The "5" in "Rune Factory 5" is the name, not a version.
        self.assertEqual(["rune", "factory", "5"], tokenize("Rune Factory 5"))
        self.assertIn("2", tokenize("Mega Man X2"))

    def test_leaves_a_plain_name_alone(self) -> None:
        self.assertEqual("ActRaiser", strip_version("ActRaiser"))


class TestSeriesNumbers(unittest.TestCase):
    """Numbered entries in one series share nearly every word, so the number has to decide."""

    def test_different_entries_never_match(self) -> None:
        self.assertEqual(0, name_score("Mega Man X1", "Mega Man X2"))
        self.assertEqual(0, name_score("Mega Man X1", "Mega Man X3 v1.0"))
        self.assertEqual(0, name_score("Rune Factory 4", "rune_factory_5"))

    def test_the_same_entry_still_matches(self) -> None:
        self.assertEqual(100, name_score("Mega Man X2", "mega_man_x2"))
        self.assertGreaterEqual(name_score("Mega Man X1", "Mega Man X1 v1.2"), MIN_MATCH_SCORE)

    def test_a_version_number_is_not_mistaken_for_a_series_number(self) -> None:
        # Without version stripping the "1" in "v1.1" would make this look like X1.
        self.assertEqual(0, name_score("Mega Man X1", "Mega Man X2 v1.1"))

    def test_a_number_on_only_one_side_is_not_disqualifying(self) -> None:
        # A page called "Rune Factory" may well be describing "Rune Factory 5".
        self.assertGreater(name_score("Rune Factory", "runefactory5"), 0)
        self.assertGreater(name_score("Mega Man X1", "Mega Man X"), 0)


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
