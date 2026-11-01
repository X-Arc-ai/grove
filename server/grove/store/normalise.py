"""The one normal form of a phrase. The seed and the resolver share it, so they cannot drift."""

import unicodedata

_MARK = "Mn"
# ASCII punctuation also sits in these three symbol categories. Other symbols, emoji among them,
# stay: a phrase made of one must not normalise to nothing.
_PUNCTUATION_SYMBOLS = frozenset({"Sc", "Sk", "Sm"})


def _is_punctuation(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith("P") or category in _PUNCTUATION_SYMBOLS


def normalise_phrase(phrase: str) -> str:
    """Case, accents, underscores, hyphens, punctuation and spacing fall away (lines 1386, 1589).

    NFKD runs on both sides of casefold: it can expose a capital (a compatibility character), and
    casefold can expose an accent. One pass of either would leave the result not idempotent.
    """
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", phrase).casefold())
    kept = (
        " " if _is_punctuation(char) else char
        for char in folded
        if unicodedata.category(char) != _MARK
    )
    return " ".join("".join(kept).split())
