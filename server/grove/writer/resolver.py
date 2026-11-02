"""The reuse-first resolver (D8, lines 1587-1596): a proposed word reaches one registry word, or
none, or several, and the resolver never picks among several (line 1386).

D2 resolves aliases once, before every other check, so the writer's strict wrappers on Registry
call resolve_word and refuse what does not resolve. Extraction and the clean-up agent use the soft
forms: resolve_link_phrase, which never lets an unmapped phrase reach a meaning that carries
consequences (line 1596), and lexical_candidates, layer 3 of D10's ladder (line 2010). The ladder's
other joins (shared neighbours, overlapping time) and the mint path belong to other lanes.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grove.store.current import has_ended, read_rows
from grove.store.normalise import normalise_phrase
from grove.store.query import WorldQuery
from grove.writer.errors import Refused
from grove.writer.neardup import CANDIDATE_THRESHOLD, similarity
from grove.writer.tx import WorldTx

if TYPE_CHECKING:
    from grove.writer.registry import Registry

RESOLVED = "resolved"
NONE = "none"
AMBIGUOUS = "ambiguous"
WORD_TYPES = frozenset({"kind", "meaning", "property_name"})
# Present tense only: "was owned by" stays unmatched, because a link's clocks say when (D1).
_COPULAS = frozenset({"is", "are"})
_UNUSABLE = frozenset({"retired", "split"})

# The title, and the columns has_ended reads.
_ITEMS_OF_KIND = (
    "MATCH (i:Item {kind: $kind}) RETURN i.id AS id, i.title AS title, i.end_class AS end_class, "
    "i.status_role AS status_role, "
    "i.stopped_being_true_precision AS stopped_being_true_precision"
)
_ITEM_ALIASES = (
    "MATCH (a:Alias {target_type: 'item'}) WHERE a.target_id IN $ids "
    "RETURN a.target_id AS id, a.phrase AS phrase"
)
_DISTINCT_PAIRS = (
    "MATCH (d:DistinctPair) WHERE d.item_a = $id OR d.item_b = $id "
    "RETURN d.item_a AS a, d.item_b AS b"
)

# A WorldTx reads inside the batch's transaction, so it sees the batch's own writes.
Source = WorldTx | WorldQuery


def usable(row: dict[str, Any]) -> bool:
    """A retired or a split word stays in the registry for its history, and resolves nothing."""
    return row.get("status") not in _UNUSABLE


def _empty_phrase(phrase_of: str) -> Refused:
    # S34: "___" and "..." normalise to nothing, and nothing must never match a word.
    return Refused(
        "invalid_arguments",
        "empty_phrase",
        "the phrase is empty once case, spacing and punctuation fall away",
        {"phrase_of": phrase_of},
    )


# Words ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """`via` is id, name, normalised_name, alias, or one of the last two after a leading copula.
    `candidates` holds the usable targets of an ambiguous phrase, as {id, name, reversed}."""

    outcome: str
    target: dict[str, Any] | None
    reversed: bool
    via: str | None
    phrase: str
    normalised: str
    candidates: tuple[dict[str, Any], ...] = ()


def _words(registry: "Registry", word_type: str, kind_id: str | None) -> dict[str, dict]:
    if word_type == "kind":
        rows = registry.kinds.values()
    elif word_type == "meaning":
        rows = registry.meanings.values()
    else:
        rows = (row for row in registry.property_names if row.get("kind") == kind_id)
    return {row["id"]: row for row in rows if usable(row)}


def _decided(
    choices: list[tuple[dict[str, Any], bool]], via: str, phrase: str, normalised: str
) -> Resolution:
    if len(choices) == 1:
        target, reversed_ = choices[0]
        return Resolution(RESOLVED, target, reversed_, via, phrase, normalised)
    candidates = tuple(
        {"id": row["id"], "name": row.get("name"), "reversed": reversed_}
        for row, reversed_ in sorted(choices, key=lambda choice: choice[0]["id"])
    )
    return Resolution(AMBIGUOUS, None, False, via, phrase, normalised, candidates)


def _matched(
    registry: "Registry",
    word_type: str,
    words: dict[str, dict],
    wanted: str,
    suffix: str,
    phrase: str,
    normalised: str,
) -> Resolution | None:
    """The normalised name first, then the aliases: a name wins over an alias."""
    named = [row for row in words.values() if normalise_phrase(row.get("name") or "") == wanted]
    if named:
        choices = [(row, False) for row in named]
        return _decided(choices, "normalised_name" + suffix, phrase, normalised)
    # Only an alias of this type whose own row and whose target are both usable counts, so a
    # canonical alias of a retired word reaches nothing. For a property name, `words` holds only
    # the rows of one kind, so an alias of another kind's property is no match.
    targets: dict[str, tuple[dict[str, Any], bool]] = {}
    for alias in registry.aliases:
        if (
            alias.get("target_type") == word_type
            and alias.get("normalised_phrase") == wanted
            and usable(alias)
            and alias.get("target_id") in words
        ):
            targets[alias["target_id"]] = (words[alias["target_id"]], bool(alias.get("reversed")))
    if targets:
        return _decided(list(targets.values()), "alias" + suffix, phrase, normalised)
    return None


def resolve_word(
    registry: "Registry", word_type: str, phrase: str, *, kind_id: str | None = None
) -> Resolution:
    """One phrase against the registry, in D8's order: an exact id or name, the normalised name,
    an alias; and only when none matched, the same again with a leading "is" or "are" dropped.

    Raises Refused (invalid_arguments, empty_phrase) for a phrase that normalises to nothing.
    A primitive word is no row of the world's registry, so it never resolves here; the strict
    wrappers on Registry refuse it by name first.
    """
    if word_type not in WORD_TYPES:
        raise ValueError(f"no word type {word_type!r}")
    if (word_type == "property_name") != (kind_id is not None):
        raise ValueError("a property name resolves per kind: kind_id is for property names only")
    normalised = normalise_phrase(phrase)
    if not normalised:
        raise _empty_phrase(word_type)
    words = _words(registry, word_type, kind_id)
    if phrase in words:
        return Resolution(RESOLVED, words[phrase], False, "id", phrase, normalised)
    for row in words.values():
        if row.get("name") == phrase:
            return Resolution(RESOLVED, row, False, "name", phrase, normalised)
    found = _matched(registry, word_type, words, normalised, "", phrase, normalised)
    if found is not None:
        return found
    first, _, rest = normalised.partition(" ")
    if first in _COPULAS and rest:
        found = _matched(registry, word_type, words, rest, "_after_copula", phrase, normalised)
        if found is not None:
            return found
    return Resolution(NONE, None, False, None, phrase, normalised)


# Link phrases --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkWord:
    """The meaning a proposed link phrase is written with. `original_phrase` is set only when the
    phrase rides on the generic meaning; `candidates` are the meanings an ambiguous phrase named."""

    meaning: dict[str, Any]
    reversed: bool
    original_phrase: str | None
    candidates: tuple[dict[str, Any], ...]
    outcome: str


def generic_meaning(registry: "Registry") -> dict[str, Any] | None:
    """The generic connection meaning, found by its declared field (S25), never by its name.
    Two usable rows would make the fallback a pick, so then there is none."""
    rows = [
        row
        for row in registry.meanings.values()
        if row.get("generic") is True and usable(row) and not row.get("carries_consequences")
    ]
    return rows[0] if len(rows) == 1 else None


def resolve_link_phrase(registry: "Registry", phrase: str) -> LinkWord | None:
    """A resolved phrase is written with its meaning. An unmapped or an ambiguous one rides on the
    generic meaning with its phrase kept, for the consolidation agent to re-type; it never falls
    to a meaning that carries consequences (line 1596). None when the world has no generic row.
    The mint path is ontology's, and create_link itself still refuses an unknown meaning (D1)."""
    found = resolve_word(registry, "meaning", phrase)
    if found.target is not None:
        return LinkWord(found.target, found.reversed, None, (), RESOLVED)
    generic = generic_meaning(registry)
    if generic is None:
        return None
    return LinkWord(generic, False, phrase, found.candidates, found.outcome)


# Lexical candidates --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """`score` is rounded to three places, as the decline log stores a candidate's score."""

    item_id: str
    score: float
    ended: bool


def title_scores(source: Source, phrase: str, titles: dict[str, str | None]) -> dict[str, float]:
    """Each item's best trigram similarity to the phrase over its title and its item aliases
    (Appendix C). The writer's near-duplicate gate and the candidate band both score with this,
    so one pair never gets two scores."""
    scores = {item_id: similarity(phrase, title or "") for item_id, title in titles.items()}
    if scores:
        for alias in read_rows(source, _ITEM_ALIASES, ids=list(scores)):
            alias_score = similarity(phrase, alias["phrase"] or "")
            scores[alias["id"]] = max(scores[alias["id"]], alias_score)
    return scores


def _left_out(source: Source, item_id: str | None) -> set[str]:
    if item_id is None:
        return set()
    left_out = {item_id}
    for pair in read_rows(source, _DISTINCT_PAIRS, id=item_id):
        left_out.update((pair["a"], pair["b"]))
    return left_out


def lexical_candidates(
    source: Source, kind_id: str, phrase: str, *, exclude_distinct_from: str | None = None
) -> list[Candidate]:
    """Every item of the kind at trigram similarity CANDIDATE_THRESHOLD or more, best first.

    An ended item comes back flagged, never dropped: a late fact needs its ended item (D10, a
    March message about a task that ended in July). With `exclude_distinct_from`, that item and
    each item it forms a DistinctPair with are left out (D10 layer 5: the pair is never raised
    again). Raises Refused (invalid_arguments, empty_phrase) for a phrase that normalises to
    nothing.
    """
    if not normalise_phrase(phrase):
        raise _empty_phrase("item")
    rows = read_rows(source, _ITEMS_OF_KIND, kind=kind_id)
    left_out = _left_out(source, exclude_distinct_from)
    kept = {row["id"]: row for row in rows if row["id"] not in left_out}
    scores = title_scores(source, phrase, {item_id: row["title"] for item_id, row in kept.items()})
    found = sorted(
        ((score, item_id) for item_id, score in scores.items() if score >= CANDIDATE_THRESHOLD),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [
        Candidate(item_id, round(score, 3), has_ended(kept[item_id])) for score, item_id in found
    ]
