"""Bounded read-only adapter for the writer's D10 lexical resolver.

No alias or item is created here. Ambiguity is a value for the future durable hold
owner; it never becomes an arbitrary person ID. Thresholds remain uncalibrated defaults.
"""

from dataclasses import dataclass

from grove.extraction.contracts import ExtractionRefused, bounded_text
from grove.store.current import read_rows
from grove.store.normalise import normalise_phrase
from grove.writer.clocks import parse_instant
from grove.writer.neardup import threshold_for
from grove.writer.resolver import Source, lexical_candidates

MAX_RESOLUTION_ITEMS = 256
MAX_RESOLUTION_ALIASES = 1024
_COUNT_ITEMS = "MATCH (i:Item {kind: $kind}) RETURN count(i) AS total"
_COUNT_ALIASES = (
    "MATCH (i:Item {kind: $kind}), (a:Alias {target_type: 'item'}) "
    "WHERE a.target_id = i.id RETURN count(a) AS total"
)
_ALIASES = (
    "MATCH (a:Alias {target_type: 'item'}), (i:Item {kind: $kind}) "
    "WHERE a.target_id = i.id AND a.normalised_phrase = $phrase "
    "AND (a.status IS NULL OR NOT a.status IN ['retired', 'split']) "
    "AND (a.valid_from IS NULL OR a.valid_from <= $at) "
    "AND (a.valid_to IS NULL OR $at < a.valid_to) "
    "RETURN DISTINCT i.id AS id ORDER BY id"
)
_DISTINCT = (
    "MATCH (d:DistinctPair) WHERE d.item_a = $id OR d.item_b = $id "
    "RETURN d.item_a AS a, d.item_b AS b"
)
_CONTEXT = (
    "MATCH (i:Item) WHERE i.id IN $ids "
    "OPTIONAL MATCH (i)-[l:LINK]-(n:Item) "
    "WHERE n.id IN $neighbors AND l.active_key IS NOT NULL "
    "RETURN i.id AS id, count(DISTINCT n) AS shared, "
    "i.became_true_earliest AS start, i.stopped_being_true_latest AS stop"
)
_TIMED_ALIASES = (
    "MATCH (a:Alias {target_type: 'item'}) WHERE a.target_id IN $ids "
    "AND (a.valid_from IS NOT NULL OR a.valid_to IS NOT NULL) RETURN count(a) AS total"
)


class _BoundedSource:
    def __init__(self, source):
        self.source = source

    def read(self, query, **params):
        maximum = MAX_RESOLUTION_ALIASES if "a:Alias" in query else MAX_RESOLUTION_ITEMS
        rows = read_rows(
            self.source, query + " LIMIT $extraction_limit", extraction_limit=maximum + 1, **params
        )
        if len(rows) > maximum:
            raise ExtractionRefused("resolution_snapshot_too_large")
        return rows


@dataclass(frozen=True)
class NameResolution:
    state: str
    item_id: str | None
    candidates: tuple[str, ...]
    reason: str


def resolve_name(
    source: Source,
    kind: str,
    phrase: str,
    *,
    source_time: str,
    context_items: tuple[str, ...] = (),
    exclude_distinct_from: str | None = None,
) -> NameResolution:
    """Resolve aliases first, then the writer's lexical candidates and context.

    The caller must supply a WorldQuery for the same authorized world as its source.
    High similarity alone resolves only one candidate; competing candidates stay held.
    A role alias applies at the message's own time, including late historical messages.
    """
    bounded_text(kind)
    bounded_text(phrase)
    if not normalise_phrase(phrase):
        raise ExtractionRefused("empty_name")
    at = parse_instant(bounded_text(source_time, 64))
    source = _BoundedSource(source)
    if not isinstance(context_items, tuple) or len(context_items) > 128:
        raise ExtractionRefused("resolution_context_bounds")
    for item in context_items:
        bounded_text(item)
    excluded = set()
    if exclude_distinct_from is not None:
        bounded_text(exclude_distinct_from)
        excluded.add(exclude_distinct_from)
        for pair in read_rows(source, _DISTINCT, id=exclude_distinct_from):
            excluded.update((pair["a"], pair["b"]))
    for query, maximum in (
        (_COUNT_ITEMS, MAX_RESOLUTION_ITEMS),
        (_COUNT_ALIASES, MAX_RESOLUTION_ALIASES),
    ):
        rows = read_rows(source, query, kind=kind)
        if len(rows) != 1 or type(rows[0].get("total")) is not int:
            raise ExtractionRefused("resolution_count")
        if not 0 <= rows[0]["total"] <= maximum:
            raise ExtractionRefused("resolution_snapshot_too_large")
    aliases = tuple(
        row["id"]
        for row in read_rows(
            source,
            _ALIASES,
            kind=kind,
            phrase=normalise_phrase(phrase),
            at=at,
        )
        if row["id"] not in excluded
    )
    if len(aliases) == 1:
        return NameResolution("resolved", aliases[0], aliases, "exact_alias")
    scores = {}
    if aliases:
        candidates, reason = aliases, "ambiguous_alias"
    else:
        lexical = lexical_candidates(
            source, kind, phrase, exclude_distinct_from=exclude_distinct_from
        )
        candidates = tuple(candidate.item_id for candidate in lexical)
        scores = {candidate.item_id: candidate.score for candidate in lexical}
        reason = "lexical_band"
    if not candidates:
        return NameResolution("new", None, (), "no_candidate")
    if not aliases:
        # The shared writer resolver does not filter role aliases by validity. Until it
        # does, a lexical candidate with any timed alias cannot auto-resolve here.
        timed = read_rows(source, _TIMED_ALIASES, ids=list(candidates))
        if len(timed) != 1 or type(timed[0].get("total")) is not int:
            raise ExtractionRefused("resolution_count")
        if timed[0]["total"]:
            return NameResolution("ambiguous", None, candidates, "timed_alias_review")
    context = read_rows(source, _CONTEXT, ids=list(candidates), neighbors=list(context_items))
    if {row["id"] for row in context} != set(candidates) or len(context) != len(candidates):
        raise ExtractionRefused("resolution_snapshot_changed")
    applicable = []
    for row in context:
        starts, stops = row.get("start"), row.get("stop")
        if starts is not None and parse_instant(starts) > at:
            continue
        if stops is not None and parse_instant(stops) < at:
            continue
        applicable.append(row)
    # Missing time data does not prove incompatibility. Never turn that absence into a new item.
    if len(applicable) == 1 and len(candidates) > 1:
        selected = applicable[0]["id"]
        return NameResolution("resolved", selected, candidates, "temporal_context")
    shared = [row for row in applicable if row.get("shared", 0) > 0]
    if len(shared) == 1:
        return NameResolution("resolved", shared[0]["id"], candidates, "shared_neighbor")
    if not aliases and len(candidates) == 1 and applicable:
        if scores[candidates[0]] >= threshold_for(kind):
            return NameResolution("resolved", candidates[0], candidates, "lexical_upper")
    return NameResolution("ambiguous", None, candidates, reason)
