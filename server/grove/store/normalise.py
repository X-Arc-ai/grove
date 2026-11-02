"""The one normal form of a phrase. The seed and the resolver share it, so they cannot drift."""

import unicodedata

# Combining marks (accents) and format characters fall away. A format character (a zero-width
# space or joiner, a soft hyphen, a byte order mark, a direction mark) is invisible, so two
# phrases that read the same must not differ by one. The screening lane drops them for its own
# matching too.
_DROPPED = frozenset({"Mn", "Cf"})
# ASCII punctuation also sits in these three symbol categories. Other symbols, emoji among them,
# stay: a phrase made of one must not normalise to nothing.
_PUNCTUATION_SYMBOLS = frozenset({"Sc", "Sk", "Sm"})


def _is_punctuation(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith("P") or category in _PUNCTUATION_SYMBOLS


def normalise_phrase(phrase: str) -> str:
    """Case, accents, underscores, hyphens, punctuation, invisible format characters and spacing
    fall away (lines 1386, 1589).

    Line 1386 also names script, and this build does not fold it (ruling S34): `Москва` and
    `Moskva` stay two phrases, because transliteration needs a language model or a table the build
    does not have. A change belongs to the resolver, which must also refuse an empty phrase.

    NFKD runs on both sides of casefold: it can expose a capital (a compatibility character), and
    casefold can expose an accent. One pass of either would leave the result not idempotent.
    """
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", phrase).casefold())
    kept = (
        " " if _is_punctuation(char) else char
        for char in folded
        if unicodedata.category(char) not in _DROPPED
    )
    return " ".join("".join(kept).split())
