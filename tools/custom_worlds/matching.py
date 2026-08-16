"""Deciding whether a name refers to the game a wiki page is about.

Several maintainers publish apworlds for a handful of unrelated games out of one repository, so
"newest release with an .apworld in it" picks the wrong game as often as the right one. Choosing
correctly means comparing an asset name, a release tag or a manifest's ``game`` field against the
game the page is actually about, which is a fuzzy-matching problem: the same game shows up as
``ActRaiser``, ``actraiser``, ``act_raiser``, ``ActRaiser-v1.2.0`` and ``ActRaiser.apworld``.

:func:`name_score` returns 0-100. Two independent views are taken and the better one wins:

* **tokens** - split both names into words and measure the overlap. This is what catches
  ``Sonic Battle`` against ``sonic_battle`` and, importantly, rejects ``Rune Factory`` against
  ``sonic_battle`` outright.
* **substring** - compare the alphanumeric-only forms, scored by how much of the longer name the
  shorter one accounts for. This is what catches ``ActRaiser`` against ``actraiser-v1.2.0``, where
  tokenisation cannot help because the game name is a single run-together word.
"""

import re

#: Words that carry no identity, so they should not prop up a match on their own.
STOP_TOKENS = frozenset({
    "a",
    "an",
    "ap",
    "apworld",
    "apworlds",
    "archipelago",
    "beta",
    "build",
    "final",
    "for",
    "of",
    "randomizer",
    "release",
    "rc",
    "the",
    "v",
    "ver",
    "version",
    "world",
    "worlds",
})

#: The score at or above which two names are treated as the same game.
MIN_MATCH_SCORE = 50

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")

#: Version stamps, which say nothing about which game this is: "v1.2", "1.2.3", "v3", "-rc2".
#: Only dotted numbers and v-prefixed ones count, so a bare trailing number - the "2" in
#: "Mega Man X2" - survives as part of the name.
_VERSION = re.compile(r"\bv\d+(?:\.\d+)*\b|\b\d+(?:\.\d+)+\b|\brc\d+\b", re.IGNORECASE)
_SEPARATORS = re.compile(r"[_\-]+")


def normalize(value: str) -> str:
    """Reduce a name to lower-case alphanumerics: ``"Act Raiser!"`` becomes ``"actraiser"``."""
    return _NON_ALNUM.sub("", value.lower())


def strip_version(value: str) -> str:
    """Drop version stamps, so ``"Mega Man X2 v1.1"`` compares as ``"Mega Man X2"``.

    Without this the digits in a version leak into the comparison and every entry in a series looks
    like every other one: "Mega Man X2 v1.1" appears to contain a "1", which is exactly the token
    that should have singled out "Mega Man X1".
    """
    # Separators become spaces first: "_" is a word character, so without this the \b in the
    # pattern never fires on the very common "mygame_v1.2" shape.
    return _VERSION.sub(" ", _SEPARATORS.sub(" ", value))


def tokenize(value: str) -> list[str]:
    """Split a name into meaningful words, keeping the stop words if that is all there is."""
    cleaned = _split_camel_case(strip_version(value)).lower()
    parts = [part for part in _TOKEN_SPLIT.split(cleaned) if part]
    meaningful = [part for part in parts if part not in STOP_TOKENS]
    return meaningful or parts


def name_score(expected: str, value: str) -> int:
    """Score 0-100 for how strongly ``value`` names the same game as ``expected``."""
    normalized_expected, normalized_value = normalize(expected), normalize(value)
    if not normalized_expected or not normalized_value:
        return 0
    if normalized_expected == normalized_value:
        return 100

    expected_tokens, value_tokens = tokenize(expected), tokenize(value)
    if _different_entries_in_a_series(expected_tokens, value_tokens):
        return 0
    return max(
        _token_score(expected_tokens, value_tokens),
        _substring_score(normalized_expected, normalized_value),
    )


def _different_entries_in_a_series(expected: list[str], value: list[str]) -> bool:
    """Whether two names are numbered entries in the same series, but not the same entry.

    "Mega Man X1" and "Mega Man X2" share almost every word, so overlap alone rates them as nearly
    the same game. The number is the whole point of the name, and when both sides carry one and they
    disagree, they are different games however much else matches. A number on only one side is left
    alone: a page called "Rune Factory" may well be describing "Rune Factory 5".
    """
    expected_number, value_number = _series_number(expected), _series_number(value)
    return expected_number is not None and value_number is not None and expected_number != value_number


def _series_number(tokens: list[str]) -> str | None:
    """The last purely numeric token, which is where a series number sits in these names."""
    return next((token for token in reversed(tokens) if token.isdigit()), None)


def matches(expected: str, value: str, *, threshold: int = MIN_MATCH_SCORE) -> bool:
    """Whether ``value`` names the same game as ``expected``."""
    return name_score(expected, value) >= threshold


def best_score(expected: str, *values: str) -> int:
    """The strongest score ``expected`` achieves against any of ``values``."""
    return max((name_score(expected, value) for value in values if value), default=0)


def _token_score(expected: list[str], value: list[str]) -> int:
    """Weigh how much of the expected name appears, and how much else came along with it."""
    expected_set, value_set = set(expected), set(value)
    if not expected_set or not value_set:
        return 0
    shared = expected_set & value_set
    if not shared:
        return 0
    coverage = len(shared) / len(expected_set)
    precision = len(shared) / len(value_set)
    return round(100 * (0.7 * coverage + 0.3 * precision))


def _substring_score(expected: str, value: str) -> int:
    """Score containment by how much of the longer name the shorter one accounts for.

    ``actraiser`` inside ``actraiserv120`` scores well; ``sonic`` inside ``sonicbattle`` does not,
    because most of the longer name is unaccounted for and it is probably a different game.
    """
    shorter, longer = sorted((expected, value), key=len)
    if shorter not in longer:
        return 0
    return round(100 * len(shorter) / len(longer))


def _split_camel_case(value: str) -> str:
    """Insert separators at camel-case boundaries so ``ActRaiser`` tokenises as ``act raiser``."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
