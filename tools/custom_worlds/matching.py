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


def normalize(value: str) -> str:
    """Reduce a name to lower-case alphanumerics: ``"Act Raiser!"`` becomes ``"actraiser"``."""
    return _NON_ALNUM.sub("", value.lower())


def tokenize(value: str) -> list[str]:
    """Split a name into meaningful words, keeping the stop words if that is all there is."""
    parts = [part for part in _TOKEN_SPLIT.split(_split_camel_case(value).lower()) if part]
    meaningful = [part for part in parts if part not in STOP_TOKENS]
    return meaningful or parts


def name_score(expected: str, value: str) -> int:
    """Score 0-100 for how strongly ``value`` names the same game as ``expected``."""
    normalized_expected, normalized_value = normalize(expected), normalize(value)
    if not normalized_expected or not normalized_value:
        return 0
    if normalized_expected == normalized_value:
        return 100
    return max(
        _token_score(tokenize(expected), tokenize(value)),
        _substring_score(normalized_expected, normalized_value),
    )


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
