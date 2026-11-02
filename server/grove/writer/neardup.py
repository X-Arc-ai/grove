"""Near-duplicate titles (D2, Appendix C): trigram similarity computed in code (S17).

The trigrams follow the pg_trgm form: each word of the normalised phrase gets two spaces before it
and one after, and the set of its three-character windows is taken. Similarity is the size of the
shared set over the size of the union.
"""

from grove.store.normalise import normalise_phrase

# Per kind id. Appendix C's resolution level is the default: the writer flags only what would
# resolve. writer-a2 calibrates per kind from labelled pairs.
NEAR_DUPLICATE_THRESHOLD: dict[str, float] = {}
DEFAULT_THRESHOLD = 0.8
MAX_CANDIDATES_SHOWN = 5


def threshold_for(kind_id: str) -> float:
    return NEAR_DUPLICATE_THRESHOLD.get(kind_id, DEFAULT_THRESHOLD)


def trigrams(text: str) -> frozenset[str]:
    grams: set[str] = set()
    for word in normalise_phrase(text).split():
        padded = f"  {word} "
        grams.update(padded[at : at + 3] for at in range(len(padded) - 2))
    return frozenset(grams)


def similarity(a: str, b: str) -> float:
    first, second = trigrams(a), trigrams(b)
    union = first | second
    if not union:
        return 0.0
    return len(first & second) / len(union)
