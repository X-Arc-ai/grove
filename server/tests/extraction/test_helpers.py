"""Pure scheduler and real lexical-resolver integration with invented read rows."""

import pytest

from grove.extraction import ExtractionRefused, resolve_name
from grove.extraction.backfill import estimate_backfill, extraction_window
from grove.store.normalise import normalise_phrase


class ReadSource:
    def __init__(self, *, items=None, aliases=None, distinct=(), context=None, timed=0):
        self.items = items or [{"id": "item-cedar", "title": "Cedar checker", "end_class": None}]
        self.aliases = aliases or []
        self.distinct = distinct
        self.context = context
        self.timed = timed
        self.queries = []

    def read(self, query, **params):
        assert "LIMIT $extraction_limit" in query
        self.queries.append(query)
        if "DistinctPair" in query:
            return [
                {"a": first, "b": second}
                for first, second in self.distinct
                if params["id"] in {first, second}
            ]
        if "RETURN count(i) AS total" in query:
            return [{"total": len(self.items)}]
        if "RETURN count(a) AS total" in query:
            return [{"total": self.timed if "valid_from" in query else len(self.aliases)}]
        if "a.normalised_phrase = $phrase" in query:
            assert "a.valid_from <= $at" in query and "$at < a.valid_to" in query
            return [
                {"id": row["id"]}
                for row in self.aliases
                if normalise_phrase(row["phrase"]) == params["phrase"]
            ]
        if "WHERE a.target_id IN $ids" in query:
            return [row for row in self.aliases if row["id"] in params["ids"]]
        if "count(DISTINCT n) AS shared" in query:
            return (
                self.context
                if self.context is not None
                else [
                    {"id": identifier, "shared": 0, "start": None, "stop": None}
                    for identifier in params["ids"]
                ]
            )
        if "MATCH (i:Item {kind: $kind}) RETURN" in query:
            return self.items
        raise AssertionError("unexpected read query")


def resolve(source, phrase="Cedar checker", **kwargs):
    return resolve_name(source, "task", phrase, source_time="2026-10-01T09:00:00Z", **kwargs)


def test_exact_alias_precedes_lexical_resolver():
    source = ReadSource(
        aliases=[{"id": "item-cedar", "phrase": "Repair box", "status": "canonical"}]
    )
    result = resolve(source, "repair box")
    assert result.item_id == "item-cedar"
    assert result.reason == "exact_alias"
    assert not any("WHERE a.target_id IN $ids" in query for query in source.queries)


def test_real_writer_lexical_score_and_upper_threshold_are_used():
    result = resolve(ReadSource())
    assert result.item_id == "item-cedar"
    assert result.reason == "lexical_upper"
    result = resolve(ReadSource(), "Cedar checking service")
    assert result.state == "ambiguous"
    assert result.item_id is None


def test_distinct_pair_excludes_exact_alias_and_lexical_candidate():
    source = ReadSource(
        aliases=[{"id": "item-cedar", "phrase": "Repair box", "status": "canonical"}],
        distinct=(("item-prior", "item-cedar"),),
    )
    result = resolve(source, "Repair box", exclude_distinct_from="item-prior")
    assert result.state == "new"
    assert result.candidates == ()


def test_shared_neighbor_resolves_ambiguous_alias_without_arbitrary_tie():
    aliases = [{"id": item, "phrase": "Cedar", "status": "canonical"} for item in ("i1", "i2")]
    context = [
        {"id": "i1", "shared": 0, "start": None, "stop": None},
        {"id": "i2", "shared": 1, "start": None, "stop": None},
    ]
    source = ReadSource(aliases=aliases, context=context)
    result = resolve(source, "Cedar", context_items=("common-task",))
    assert (result.item_id, result.reason) == ("i2", "shared_neighbor")
    context[0]["shared"] = 1
    assert resolve(source, "Cedar", context_items=("common-task",)).state == "ambiguous"


def test_historical_time_context_uses_message_time():
    aliases = [{"id": item, "phrase": "Cedar", "status": "canonical"} for item in ("i1", "i2")]
    context = [
        {"id": "i1", "shared": 0, "start": "2027-01-01T00:00:00Z", "stop": None},
        {"id": "i2", "shared": 0, "start": None, "stop": None},
    ]
    result = resolve(ReadSource(aliases=aliases, context=context), "Cedar")
    assert (result.item_id, result.reason) == ("i2", "temporal_context")


def test_timed_lexical_alias_never_uses_unfiltered_writer_alias_as_proof():
    result = resolve(ReadSource(timed=1))
    assert result.state == "ambiguous"
    assert result.reason == "timed_alias_review"


def test_resolver_bounds_refuse_large_snapshot_before_lexical_read():
    source = ReadSource(items=[{"id": f"i{index}", "title": "Cedar"} for index in range(257)])
    with pytest.raises(ExtractionRefused, match="resolution_snapshot_too_large"):
        resolve(source)
    assert len(source.queries) == 1


def test_backfill_estimate_keeps_rerun_separate_from_measured_pass():
    result = estimate_backfill(
        {"source-one": 30, "source-two": 10},
        median_seconds=2,
        median_tokens=100,
        extraction_slots=4,
    )
    assert result.episodes == 40
    assert result.seconds_per_pass == 20
    assert result.tokens_per_pass == 4000
    assert result.reserved_passes == 2


def test_backfill_lookahead_does_not_skip_uncommitted_episode():
    episodes = ("episode-a", "episode-b", "episode-c", "episode-d")
    assert extraction_window(episodes, committed_count=1, lookahead=2) == ("episode-b", "episode-c")
    assert extraction_window(episodes, committed_count=4, lookahead=2) == ()
    with pytest.raises(ExtractionRefused, match="backfill_duplicate_episode"):
        extraction_window(("episode-a", "episode-a"), committed_count=0, lookahead=1)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, True])
def test_backfill_refuses_invalid_measurements(value):
    with pytest.raises(ExtractionRefused, match="backfill_measurement"):
        estimate_backfill(
            {"source-one": 1}, median_seconds=value, median_tokens=1, extraction_slots=1
        )
