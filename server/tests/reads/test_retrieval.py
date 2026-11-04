"""Synthetic pure D7 retrieval controls; no database, clock read, or model call."""

import math
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from grove.reads.ranking import fuse
from grove.reads.requests import Hops
from grove.reads.retrieval import (
    HopLink,
    HopNode,
    LexicalItem,
    LexicalMatch,
    RetrievalError,
    lexical_ranks,
    typed_hops,
)
from grove.store.current import link_standing, standing

WORLD = "t_hosts-b1_11111111-1111-4111-8111-111111111111"
OTHER = "t_hosts-b1_22222222-2222-4222-8222-222222222222"
AT = datetime(2026, 11, 4, 12, tzinfo=UTC)
MEANINGS = frozenset({"depends", "contains", "describes", "unused"})


def item(item_id, title="", **kwargs):
    return LexicalItem(WORLD, item_id, title, **kwargs)


def ranks(query, items, current=(), historical=(), **kwargs):
    return lexical_ranks(
        WORLD,
        query,
        items,
        [LexicalMatch(WORLD, item_id) for item_id in current],
        [LexicalMatch(WORLD, item_id) for item_id in historical],
        complete=True,
        **kwargs,
    )


def link(link_id, start, end, meaning="depends"):
    return HopLink(WORLD, link_id, start, end, meaning)


def walk(nodes, links, *, seeds=("a",), direction="out", depth=3, meanings=()):
    return typed_hops(
        WORLD,
        seeds,
        [HopNode(WORLD, value) for value in nodes],
        links,
        Hops(meanings, direction, depth),
        known_meanings=MEANINGS,
        complete=True,
    )


def test_membership_is_native_not_a_local_token_guess():
    result = ranks("lease", [item("a", "lease"), item("b", "unrelated")], ["b"])
    assert result.retrievers == (("b",), ())
    assert result.current[0].score == 0


def test_bm25_uses_nonmatching_documents_in_the_complete_local_corpus():
    result = ranks("lease", [item("a", "lease"), item("b", "rent")], ["a"])
    # Two same-length documents, one containing the term: IDF = log(1 + 1.5 / 1.5).
    assert result.current[0].score == pytest.approx(math.log(2))
    isolated = ranks("lease", [item("a", "lease")], ["a"])
    assert isolated.current[0].score != result.current[0].score


def test_other_world_corpus_has_no_effect_on_local_scores_or_order():
    documents = [item("a", "lease lease"), item("b", "lease rent")]
    before = ranks("lease", documents, ["b", "a"])
    lexical_ranks(
        OTHER,
        "lease",
        [LexicalItem(OTHER, str(n), "lease " * (n + 1)) for n in range(80)],
        [LexicalMatch(OTHER, "0")],
        [],
        complete=True,
    )
    assert ranks("lease", documents, ["b", "a"]) == before


@pytest.mark.parametrize("location", ["item", "current", "historical"])
def test_foreign_rows_refuse_even_if_unmatched_or_not_requested(location):
    documents = [item("a", "lease")]
    current, historical = [LexicalMatch(WORLD, "a")], []
    if location == "item":
        documents.append(LexicalItem(OTHER, "unmatched", "foreign text"))
    elif location == "current":
        current.append(LexicalMatch(OTHER, "a"))
    else:
        historical.append(LexicalMatch(OTHER, "a"))
    with pytest.raises(RetrievalError, match="^retrieval_world_mismatch$"):
        lexical_ranks(WORLD, "lease", documents, current, historical, complete=True)


@pytest.mark.parametrize(
    "field", ["title", "summary", "body", "property_values", "aliases", "search_terms"]
)
def test_each_authorized_current_field_contributes_to_rank(field):
    value = "lease" if field in ("title", "summary", "body") else ("lease",)
    result = ranks("lease", [item("z", **{field: value}), item("a", "other")], ["a", "z"])
    assert result.retrievers[0] == ("z", "a")
    assert result.current[0].score > 0


def test_history_has_a_separate_lower_weight_and_never_becomes_current_text():
    result = ranks("lease", [item("a", past_values=("lease",)), item("z", "lease")], ["z"], ["a"])
    assert result.retrievers == (("z",), ("a",))
    assert result.weights[0] > result.weights[1] > 0
    assert fuse(result.retrievers, frozenset({"a", "z"}), weights=result.weights) == ("z", "a")
    assert set(asdict(result.current[0])) == {"item_id", "score"}
    assert set(asdict(result.historical[0])) == {"item_id", "score"}
    current_only = ranks("lease", [item("a", past_values=("lease",))], ["a"])
    assert current_only.current[0].score == 0


def test_historical_statistics_do_not_change_current_scores():
    original = [item("a", "lease"), item("b", "rent")]
    changed = [replace(row, past_values=("lease " * 200,)) for row in original]
    assert ranks("lease", original, ["a"]).current == ranks("lease", changed, ["a"]).current


@pytest.mark.parametrize(
    "query,text", [("CAFÉ", "cafe"), ("ＦＯＯ", "foo"), ("Москва", "Москва"), ("بيت", "بيت")]
)
def test_shared_unicode_folding_and_tokens(query, text):
    assert ranks(query, [item("a", text)], ["a"]).current[0].score > 0


def test_scripts_do_not_transliterate_without_explicit_search_terms():
    native_hit = ranks("Moskva", [item("a", "Москва")], ["a"])
    assert native_hit.current[0].score == 0
    explicit = ranks("Moskva", [item("a", "Москва", search_terms=("Moskva",))], ["a"])
    assert explicit.current[0].score > 0


def test_stable_id_ties_and_deduplication_are_independent_of_input_order():
    documents = [item("z", "lease"), item("a", "lease")]
    result = ranks("lease", documents, ["z", "a", "z"], ["z", "z", "a"])
    assert result.retrievers == (("a", "z"), ("a", "z"))
    assert result == ranks("lease", documents[::-1], ["a", "z"], ["a", "z"])


def test_complete_native_candidates_reach_eligibility_before_any_ranking_page():
    # The adapter captures all native hits first. A page-sized prefix would lose the last hit.
    native = [item(f"ended-{n:03}", "lease " * 5) for n in range(60)] + [item("eligible", "lease")]
    rows = {
        row.item_id: {"end_class": "ended" if row.item_id.startswith("ended") else None}
        for row in native
    }
    eligible = [
        row
        for row in native
        if standing(rows[row.item_id], at=AT, lifecycle_class="record").is_current
    ]
    result = ranks("lease", eligible, [row.item_id for row in eligible])
    assert result.retrievers == (("eligible",), ())
    all_eligible = [replace(row, item_id=f"kept-{n:03}") for n, row in enumerate(native)]
    complete = ranks("lease", all_eligible, [row.item_id for row in all_eligible])
    assert len(complete.current) == 61


def test_source_time_eligibility_is_an_explicit_adapter_boundary():
    native = [item("future", "lease"), item("said", "lease"), item("confirmed", "lease")]
    states = {
        "future": {"became_true_earliest": AT + timedelta(days=1), "source_type": "confirmed"},
        "said": {"source_type": "said"},
        "confirmed": {"source_type": "confirmed"},
    }
    eligible = [
        row
        for row in native
        if states[row.item_id]["source_type"] == "confirmed"
        and standing(states[row.item_id], at=AT, lifecycle_class="record").is_current
    ]
    assert ranks("lease", eligible, ["confirmed"]).retrievers == (("confirmed",), ())
    # A native hit with no eligible item is a refusal, never a fallback to the original row.
    with pytest.raises(RetrievalError, match="retrieval_match_without_item"):
        ranks("lease", eligible, ["future", "confirmed"])


@pytest.mark.parametrize("complete", [False, None, 1])
def test_lexical_incomplete_inventory_refuses_instead_of_empty_result(complete):
    with pytest.raises(RetrievalError, match="incomplete_retrieval_input"):
        lexical_ranks(WORLD, "lease", [], [], [], complete=complete)


@pytest.mark.parametrize("query", [None, "", "  ", "a\x00b", "x" * 65, "x " * 513, "\ud800"])
def test_malformed_or_unbounded_queries_refuse(query):
    with pytest.raises(RetrievalError):
        lexical_ranks(WORLD, query, [], [], [], complete=True)


@pytest.mark.parametrize(
    "documents,current,historical",
    [
        ([item("a"), item("a")], [], []),
        ([item("a")], [LexicalMatch(WORLD, "missing")], []),
        ([item("a")], [], [LexicalMatch(WORLD, "missing")]),
        ([{"world_id": WORLD, "item_id": "a", "score": 1e30}], [], []),
        ([item("a")], [{"world_id": WORLD, "item_id": "a", "score": 1e30}], []),
    ],
)
def test_malformed_or_shared_score_inputs_refuse(documents, current, historical):
    with pytest.raises(RetrievalError):
        lexical_ranks(WORLD, "lease", documents, current, historical, complete=True)


def test_inputs_and_results_are_immutable_and_text_never_leaves_results():
    terms = ["lease"]
    document = item("a", search_terms=terms, past_values=["private old title"])
    terms.append("unexpected")
    assert document.search_terms == ("lease",)
    result = ranks("lease", [document], ["a"], ["a"])
    assert "lease" not in repr(result) and "private old title" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        document.title = "changed"


@pytest.mark.parametrize(
    "direction,expected",
    [
        ("out", {"a": 0, "b": 1, "c": 2}),
        ("in", {"a": 0, "d": 1, "e": 2}),
        ("both", {"a": 0, "b": 1, "d": 1, "c": 2, "e": 2}),
    ],
)
def test_direction_applies_at_every_hop(direction, expected):
    links = [link("ab", "a", "b"), link("bc", "b", "c"), link("da", "d", "a"), link("ed", "e", "d")]
    result = walk("abcde", links, direction=direction, depth=2)
    assert {row.item_id: row.depth for row in result.nodes} == expected


@pytest.mark.parametrize("depth,expected", [(1, "ab"), (2, "abc"), (3, "abcd")])
def test_depth_is_honored_and_never_exceeds_three(depth, expected):
    links = [link("ab", "a", "b"), link("bc", "b", "c"), link("cd", "c", "d"), link("de", "d", "e")]
    result = walk("abcde", links, depth=depth)
    assert "".join(row.item_id for row in result.nodes) == expected
    assert len(result.link_ids) == depth


def test_meaning_guard_applies_at_every_depth_and_keeps_parallel_edges_distinct():
    links = [
        link("ab-dep", "a", "b"),
        link("ab-desc", "a", "b", "describes"),
        link("bc-desc", "b", "c", "describes"),
        link("bd-dep", "b", "d"),
    ]
    restricted = walk("abcd", links, meanings=("depends",))
    assert [row.item_id for row in restricted.nodes] == ["a", "b", "d"]
    assert restricted.link_ids == ("ab-dep", "bd-dep")
    assert len(walk("abcd", links).link_ids) == 4


def test_known_unused_meaning_returns_only_seed_but_unknown_meaning_refuses():
    assert walk("ab", [link("ab", "a", "b")], meanings=("unused",)).link_ids == ()
    with pytest.raises(RetrievalError, match="invalid_retrieval_meanings"):
        walk("ab", [], meanings=("unknown",))


def test_cycles_self_loops_and_duplicate_seeds_terminate_with_complete_edges():
    links = [
        link("aa", "a", "a"),
        link("ab", "a", "b"),
        link("ba", "b", "a"),
        link("bc", "b", "c"),
        link("ca", "c", "a"),
    ]
    result = walk("abc", links, seeds=("a", "a"), direction="both")
    assert [(row.item_id, row.depth) for row in result.nodes] == [("a", 0), ("b", 1), ("c", 1)]
    assert result.link_ids == ("aa", "ab", "ba", "bc", "ca")
    assert walk("cba", links[::-1], direction="both") == result


def test_multi_seed_shortest_depth_is_independent_of_seed_order():
    links = [link("ab", "a", "b"), link("bc", "b", "c"), link("cd", "c", "d")]
    result = walk("abcd", links, seeds=("d", "a"), direction="both")
    assert [(row.item_id, row.depth) for row in result.nodes] == [
        ("a", 0),
        ("d", 0),
        ("b", 1),
        ("c", 1),
    ]
    assert result == walk("dcba", links[::-1], seeds=("a", "d"), direction="both")


def test_hidden_bridge_never_reappears_and_dangling_filtered_graph_refuses():
    # The caller removes the hidden node and its incident links before this helper.
    assert [row.item_id for row in walk("ac", []).nodes] == ["a"]
    with pytest.raises(RetrievalError, match="retrieval_link_without_item"):
        walk("ac", [link("ab", "a", "hidden"), link("bc", "hidden", "c")])


def test_hop_link_time_eligibility_comes_from_d3_and_include_ended_is_callers_choice():
    raw = [
        (
            {"active_key": "ab", "became_true_earliest": AT - timedelta(days=1)},
            link("ab", "a", "b"),
        ),
        ({"active_key": None, "end_class": "ended"}, link("bc", "b", "c")),
        (
            {"active_key": "bd", "became_true_earliest": AT + timedelta(days=1)},
            link("bd", "b", "d"),
        ),
    ]
    current = [edge for row, edge in raw if link_standing(row, at=AT).is_current]
    assert [row.item_id for row in walk("abcd", current).nodes] == ["a", "b"]
    with_ended = [
        edge for row, edge in raw if link_standing(row, at=AT).state in ("current", "ended")
    ]
    assert [row.item_id for row in walk("abcd", with_ended).nodes] == ["a", "b", "c"]


def test_complete_hub_returns_more_than_packet_budget_without_cutting_edges():
    neighbors = [f"n-{n:04}" for n in range(5000)]
    result = walk(["a", *neighbors], [link(value, "a", value) for value in neighbors], depth=1)
    assert len(result.nodes) == 5001
    assert len(result.link_ids) == 5000
    assert result.nodes[-1].item_id == "n-4999"


@pytest.mark.parametrize("location", ["node", "link"])
def test_foreign_hop_inventory_refuses_even_when_unreachable_or_wrong_meaning(location):
    nodes = [HopNode(WORLD, "a"), HopNode(WORLD, "b")]
    links = []
    if location == "node":
        nodes.append(HopNode(OTHER, "foreign"))
    else:
        links.append(HopLink(OTHER, "foreign-link", "b", "b", "describes"))
    with pytest.raises(RetrievalError, match="^retrieval_world_mismatch$"):
        typed_hops(
            WORLD, ["a"], nodes, links, Hops(("depends",)), known_meanings=MEANINGS, complete=True
        )


@pytest.mark.parametrize("complete", [False, None, 1])
def test_incomplete_hop_inventory_refuses(complete):
    with pytest.raises(RetrievalError, match="incomplete_retrieval_input"):
        typed_hops(WORLD, [], [], [], Hops(), known_meanings=MEANINGS, complete=complete)


@pytest.mark.parametrize(
    "hops",
    [
        Hops(depth=0),
        Hops(depth=4),
        Hops(depth=True),
        Hops(direction="sideways"),
        Hops(meanings=("depends", "depends")),
        Hops(meanings="depends"),
        Hops(meanings=(None,)),
        None,
    ],
)
def test_malformed_hop_controls_refuse(hops):
    with pytest.raises(RetrievalError):
        typed_hops(WORLD, [], [], [], hops, known_meanings=MEANINGS, complete=True)


@pytest.mark.parametrize(
    "nodes,links,seeds",
    [
        ([HopNode(WORLD, "a"), HopNode(WORLD, "a")], [], ["a"]),
        ([HopNode(WORLD, "a")], [link("x", "a", "a"), link("x", "a", "a")], ["a"]),
        ([HopNode(WORLD, "a")], [link("x", "a", "a", "unknown")], ["a"]),
        ([HopNode(WORLD, "a")], [], ["missing"]),
        ([HopNode(WORLD, "a")], [], "a"),
        ([{"world_id": WORLD, "item_id": "a"}], [], []),
    ],
)
def test_malformed_graph_refuses_without_an_empty_answer(nodes, links, seeds):
    with pytest.raises(RetrievalError):
        typed_hops(WORLD, seeds, nodes, links, Hops(), known_meanings=MEANINGS, complete=True)


def test_refusals_do_not_echo_captured_identifiers_or_text():
    with pytest.raises(RetrievalError) as error:
        ranks("lease", [LexicalItem(OTHER, "private-identifier", "private text")])
    assert "private" not in str(error.value)


@pytest.mark.parametrize("world", ["", "invalid", None, 3])
def test_invalid_world_refuses(world):
    with pytest.raises(RetrievalError, match="invalid_retrieval_world"):
        lexical_ranks(world, "lease", [], [], [], complete=True)
    with pytest.raises(RetrievalError, match="invalid_retrieval_world"):
        typed_hops(world, [], [], [], Hops(), known_meanings=MEANINGS, complete=True)


def test_empty_complete_inputs_are_valid_and_return_no_candidates():
    assert ranks("lease", [], [], []).retrievers == ((), ())
    assert walk([], [], seeds=()).nodes == ()
