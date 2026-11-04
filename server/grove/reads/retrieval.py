"""Pure D7 lexical ranks and typed hops over one complete authorized capture.

The adapter supplies the complete native-index membership, and the complete world corpus
after D3 time/current and kind/area/source filters and D14 field/row filtering. It maps alias
and PastValue hits to eligible item IDs. A page, global Lucene score, or unfiltered field is
not an input. ``complete=True`` is the adapter's assertion, not proof of these obligations.

Native membership alone determines lexical candidates. BM25 scores use only supplied text,
never shared-index statistics. Zero-score native hits remain candidates. Historical text
has a separate, lower-weight retriever; neither historical text nor search terms are claims.
The caller passes the complete ranked ID tuples and weights to ranking.fuse before paging.

For hops, the adapter supplies the complete eligible, disclosable node/link graph and resolved
meaning IDs. D3 alone selects current/as-of/ended links; this module has no clock or liveness
rule. Dangling edges refuse, so removing a hidden node cannot leave a traversable bridge.
No database, model, policy decision, pagination, or retained state enters these helpers.
"""

import math
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass

from grove.reads.requests import Hops
from grove.store.query import _ANALYZER_STOP_WORDS, _folded, _runs, lucene_terms
from grove.store.scope import check_world_id

CURRENT_WEIGHT = 1.0
HISTORICAL_WEIGHT = 0.5
_K1 = 1.2
_B = 0.75


class RetrievalError(ValueError):
    """A structural refusal that never includes captured values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: object, *, identifier: bool = False) -> str:
    if not isinstance(value, str):
        raise RetrievalError("invalid_retrieval_input")
    if identifier and (not value.strip() or len(value) > 1024):
        raise RetrievalError("invalid_retrieval_input")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RetrievalError("invalid_retrieval_input") from None
    if identifier and any(ord(char) < 32 for char in value):
        raise RetrievalError("invalid_retrieval_input")
    return value


def _sequence(value: object) -> tuple:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise RetrievalError("invalid_retrieval_input")
    return tuple(value)


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_text(part) for part in _sequence(value))


def _begin(world_id: str, complete: bool) -> None:
    if complete is not True:
        raise RetrievalError("incomplete_retrieval_input")
    try:
        check_world_id(world_id)
    except (TypeError, ValueError):
        raise RetrievalError("invalid_retrieval_world") from None


def _world(value: object, world_id: str) -> None:
    if value != world_id:
        raise RetrievalError("retrieval_world_mismatch")


@dataclass(frozen=True)
class LexicalItem:
    world_id: str
    item_id: str
    title: str = ""
    summary: str = ""
    body: str = ""
    property_values: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    past_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.world_id, identifier=True)
        _text(self.item_id, identifier=True)
        for name in ("title", "summary", "body"):
            _text(getattr(self, name))
        for name in ("property_values", "aliases", "search_terms", "past_values"):
            object.__setattr__(self, name, _strings(getattr(self, name)))


@dataclass(frozen=True)
class LexicalMatch:
    """An eligible native-index hit, already mapped from an alias/history row to its item."""

    world_id: str
    item_id: str

    def __post_init__(self) -> None:
        _text(self.world_id, identifier=True)
        _text(self.item_id, identifier=True)


@dataclass(frozen=True)
class ScoredItem:
    item_id: str
    score: float


@dataclass(frozen=True)
class LexicalRanks:
    current: tuple[ScoredItem, ...]
    historical: tuple[ScoredItem, ...]

    @property
    def retrievers(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple(row.item_id for row in self.current), tuple(
            row.item_id for row in self.historical
        )

    @property
    def weights(self) -> tuple[float, float]:
        return CURRENT_WEIGHT, HISTORICAL_WEIGHT


def _tokens(fields: Sequence[str]) -> Counter[str]:
    # Use the native-query folding/token boundaries; S34 forbids implicit transliteration.
    return Counter(
        word
        for field in fields
        for run in _runs(field)
        if (word := _folded(run)) and word not in _ANALYZER_STOP_WORDS
    )


def _matches(rows: Sequence[LexicalMatch], world_id: str, ids: set[str]) -> set[str]:
    matched: set[str] = set()
    for row in _sequence(rows):
        if not isinstance(row, LexicalMatch):
            raise RetrievalError("invalid_retrieval_input")
        _world(row.world_id, world_id)
        if row.item_id not in ids:
            raise RetrievalError("retrieval_match_without_item")
        # Several aliases or old values can match one item; that is one candidate.
        matched.add(row.item_id)
    return matched


def _bm25(
    corpus: dict[str, Counter[str]], query: tuple[str, ...], matches: set[str]
) -> tuple[ScoredItem, ...]:
    sizes = {item_id: sum(tokens.values()) for item_id, tokens in corpus.items()}
    average = sum(sizes.values()) / len(corpus) if corpus else 0.0
    frequency = Counter(term for tokens in corpus.values() for term in query if term in tokens)
    population = len(corpus)
    inverse = {
        term: math.log1p((population - frequency[term] + 0.5) / (frequency[term] + 0.5))
        for term in query
    }
    result = []
    for item_id in sorted(matches):
        tokens = corpus[item_id]
        length_factor = _K1 * (1 - _B + _B * sizes[item_id] / average) if average else _K1
        score = math.fsum(
            inverse[term] * tokens[term] * (_K1 + 1) / (tokens[term] + length_factor)
            for term in query
            if tokens[term]
        )
        result.append(ScoredItem(item_id, score))
    return tuple(sorted(result, key=lambda row: (-row.score, row.item_id)))


def lexical_ranks(
    world_id: str,
    query: str,
    items: Sequence[LexicalItem],
    current_matches: Sequence[LexicalMatch],
    historical_matches: Sequence[LexicalMatch],
    *,
    complete: bool,
) -> LexicalRanks:
    """Return every native match ranked against the complete eligible world corpus.

    Nonmatching documents still contribute to local document frequency and average length.
    Historical BM25 uses only historical fields, with the same eligible item population.
    Eligibility must precede this call, including alias validity and historical visibility.
    """
    _begin(world_id, complete)
    _text(query, identifier=True)
    try:
        lucene_terms(query, fuzzy=False)  # Same query bounds as the native membership capture.
    except ValueError:
        raise RetrievalError("invalid_retrieval_query") from None
    documents: dict[str, LexicalItem] = {}
    for item in _sequence(items):
        if not isinstance(item, LexicalItem):
            raise RetrievalError("invalid_retrieval_input")
        _world(item.world_id, world_id)
        if item.item_id in documents:
            raise RetrievalError("duplicate_retrieval_item")
        documents[item.item_id] = item
    current = _matches(current_matches, world_id, set(documents))
    historical = _matches(historical_matches, world_id, set(documents))
    terms = tuple(sorted(_tokens((query,))))
    current_corpus = {
        item_id: _tokens(
            (item.title, item.summary, item.body)
            + item.property_values
            + item.aliases
            + item.search_terms
        )
        for item_id, item in documents.items()
    }
    historical_corpus = {item_id: _tokens(item.past_values) for item_id, item in documents.items()}
    return LexicalRanks(
        _bm25(current_corpus, terms, current), _bm25(historical_corpus, terms, historical)
    )


@dataclass(frozen=True)
class HopNode:
    world_id: str
    item_id: str

    def __post_init__(self) -> None:
        _text(self.world_id, identifier=True)
        _text(self.item_id, identifier=True)


@dataclass(frozen=True)
class HopLink:
    world_id: str
    link_id: str
    from_id: str
    to_id: str
    meaning: str

    def __post_init__(self) -> None:
        for value in (self.world_id, self.link_id, self.from_id, self.to_id, self.meaning):
            _text(value, identifier=True)


@dataclass(frozen=True)
class HopVisit:
    item_id: str
    depth: int


@dataclass(frozen=True)
class HopResult:
    """Seeds at depth zero, then shortest hop depth/ID; every traversed edge ID, once."""

    nodes: tuple[HopVisit, ...]
    link_ids: tuple[str, ...]


def typed_hops(
    world_id: str,
    seed_ids: Sequence[str],
    nodes: Sequence[HopNode],
    links: Sequence[HopLink],
    hops: Hops,
    *,
    known_meanings: frozenset[str],
    complete: bool,
) -> HopResult:
    """Traverse every allowed typed edge through depth 1..3; never truncate a hub.

    Meaning and direction constraints apply at every hop. The registry's resolved meaning
    IDs include valid meanings with no links, so an unknown filter refuses instead of silently
    acting like an empty graph. Edges out of depth-boundary nodes are not traversed. Cycles,
    self-loops and parallel meanings preserve each traversed link without revisiting nodes.
    """
    _begin(world_id, complete)
    if not isinstance(hops, Hops) or hops.direction not in ("out", "in", "both"):
        raise RetrievalError("invalid_retrieval_hops")
    if type(hops.depth) is not int or not 1 <= hops.depth <= 3:
        raise RetrievalError("invalid_retrieval_hops")
    if not isinstance(known_meanings, frozenset):
        raise RetrievalError("invalid_retrieval_meanings")
    for meaning in known_meanings:
        _text(meaning, identifier=True)
    selected = _sequence(hops.meanings)
    for meaning in selected:
        _text(meaning, identifier=True)
    if len(set(selected)) != len(selected) or not set(selected) <= known_meanings:
        raise RetrievalError("invalid_retrieval_meanings")
    ids: set[str] = set()
    for node in _sequence(nodes):
        if not isinstance(node, HopNode):
            raise RetrievalError("invalid_retrieval_input")
        _world(node.world_id, world_id)
        if node.item_id in ids:
            raise RetrievalError("duplicate_retrieval_item")
        ids.add(node.item_id)
    seeds = tuple(_text(seed, identifier=True) for seed in _sequence(seed_ids))
    if not set(seeds) <= ids:
        raise RetrievalError("retrieval_seed_without_item")
    adjacency: dict[str, list[tuple[str, str]]] = {item_id: [] for item_id in ids}
    seen_links: set[str] = set()
    for link in _sequence(links):
        if not isinstance(link, HopLink):
            raise RetrievalError("invalid_retrieval_input")
        _world(link.world_id, world_id)
        if link.link_id in seen_links:
            raise RetrievalError("duplicate_retrieval_link")
        seen_links.add(link.link_id)
        if link.from_id not in ids or link.to_id not in ids:
            raise RetrievalError("retrieval_link_without_item")
        if link.meaning not in known_meanings:
            raise RetrievalError("invalid_retrieval_meanings")
        if selected and link.meaning not in selected:
            continue
        if hops.direction in ("out", "both"):
            adjacency[link.from_id].append((link.to_id, link.link_id))
        if hops.direction in ("in", "both"):
            adjacency[link.to_id].append((link.from_id, link.link_id))
    depths = dict.fromkeys(sorted(set(seeds)), 0)
    queue = deque(depths)
    traversed: set[str] = set()
    while queue:
        item_id = queue.popleft()
        if depths[item_id] == hops.depth:
            continue
        for other, link_id in sorted(adjacency[item_id]):
            traversed.add(link_id)
            if other not in depths:
                depths[other] = depths[item_id] + 1
                queue.append(other)
    return HopResult(
        tuple(
            HopVisit(item_id, depth)
            for item_id, depth in sorted(depths.items(), key=lambda pair: (pair[1], pair[0]))
        ),
        tuple(sorted(traversed)),
    )
