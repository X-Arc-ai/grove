"""D11 structural area controls. These pure tests use invented IDs and no store."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from grove.store.areas import (
    MAX_RULE_BYTES,
    AreaDefinition,
    AreaError,
    AreaGraph,
    AreaItem,
    AreaLink,
    AreaMeaning,
    PartitionRule,
    area_index,
    area_membership,
    hub_degree,
    is_hub,
    parse_partition_rule,
    partition_coverage,
)
from grove.store.primitives import MEANING_DESCRIBES, PRIMITIVE_WORLD

WORLD = "t_hosts-b1_10000000-0000-7000-8000-000000000001"
OTHER_WORLD = "t_hosts-b1_10000000-0000-7000-8000-000000000002"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PAST = datetime(2026, 10, 1, tzinfo=UTC)


def ident(number):
    return f"10000000-0000-7000-8000-{number:012x}"


def item(number):
    return AreaItem(ident(number), WORLD)


def meaning(number=100, *, consequence=False, decomposition=False, primitive=False):
    return AreaMeaning(ident(number), WORLD, primitive, consequence, decomposition)


def link(number, start, end, word=100):
    return AreaLink(ident(number), WORLD, ident(start), ident(end), ident(word))


def area(number=500, *, hub=1, by="all", meanings=(), anchors=(), **changes):
    return AreaDefinition(
        ident(number),
        WORLD,
        ident(hub),
        PartitionRule(by, tuple(sorted(map(ident, meanings))), tuple(sorted(map(ident, anchors)))),
        **changes,
    )


def graph(items, links, meanings=None, areas=None, **changes):
    meanings = meanings if meanings is not None else (meaning(),)
    areas = areas if areas is not None else (area(),)
    values = dict(
        world_id=WORLD,
        commit=9,
        captured_at=NOW,
        axis="current",
        effective_at=NOW,
        items=tuple(items),
        links=tuple(links),
        meanings=tuple(meanings),
        area_ids=frozenset(row.id for row in areas),
        eligible_item_ids=frozenset(row.id for row in items),
        eligible_link_ids=frozenset(row.id for row in links),
        authorized_item_ids=frozenset(row.id for row in items),
        authorized_link_ids=frozenset(row.id for row in links),
        authorized_area_ids=frozenset(row.id for row in areas),
        complete=True,
    )
    values.update(changes)
    return AreaGraph(**values)


def rule_json(by="all", **changes):
    return json.dumps({"version": 1, "by": by, **changes})


@pytest.mark.parametrize(
    "rule",
    [
        rule_json(),
        rule_json("meaning", meaning_ids=[ident(100)]),
        rule_json("second_level_hub", meaning_ids=[ident(100)], second_level_hub_ids=[ident(2)]),
    ],
)
def test_area_rule_accepts_three_versioned_variants(rule):
    parsed = parse_partition_rule(rule)
    assert parsed.version == 1
    assert parsed.by == json.loads(rule)["by"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "null",
        "{}",
        "[]",
        '["name"]',
        '{"version":true,"by":"all"}',
        '{"version":1.0,"by":"all"}',
        '{"version":2,"by":"all"}',
        '{"version":1,"by":"guess"}',
        '{"version":1,"by":[]}',
        '{"version":1,"by":"all","extra":1}',
        '{"version":1,"version":1,"by":"all"}',
        '{"version":1,"by":"all","meaning_ids":[]}',
        rule_json("meaning", meaning_ids=[]),
        rule_json("meaning", meaning_ids="title"),
        rule_json("meaning", meaning_ids=["works with"]),
        rule_json("meaning", meaning_ids=[ident(100), ident(100)]),
        rule_json("meaning", meaning_ids=[ident(101), ident(100)]),
        rule_json("meaning", meaning_ids=[True]),
        rule_json("second_level_hub", meaning_ids=[ident(100)], second_level_hub_ids=[]),
        rule_json("second_level_hub", meaning_ids=[ident(100)]),
        '{"version":NaN,"by":"all"}',
        '\ufeff{"version":1,"by":"all"}',
        '{"version":1,"by":"\ud800"}',
    ],
)
def test_area_rule_is_strict_and_versioned(raw):
    with pytest.raises(AreaError, match="^invalid_partition_rule$"):
        parse_partition_rule(raw)


def test_area_rule_has_a_byte_bound():
    with pytest.raises(AreaError, match="invalid_partition_rule"):
        parse_partition_rule(" " * MAX_RULE_BYTES + rule_json())


def test_area_hub_degree_uses_active_nonprimitive_links():
    rows = [item(n) for n in range(1, 16)]
    links = [link(200 + n, 1, n) for n in range(2, 14)]
    links.extend([link(250, 1, 14, 101), link(251, 1, 15), link(252, 1, 1)])
    fixed = AreaLink(ident(253), WORLD, ident(1), ident(15), MEANING_DESCRIBES)
    links.append(fixed)
    words = [
        meaning(),
        meaning(101, primitive=True),
        AreaMeaning(MEANING_DESCRIBES, PRIMITIVE_WORLD, False, True, True),
    ]
    live = frozenset(row.id for row in links if row.id != ident(251))
    capture = graph(rows, links, words, eligible_link_ids=live)
    assert hub_degree(capture, ident(1)) == 12
    assert is_hub(capture, ident(1))
    eleven = replace(capture, eligible_link_ids=live - {ident(202)})
    assert hub_degree(eleven, ident(1)) == 11
    assert not is_hub(eleven, ident(1))
    assert area_membership(area(), capture).member_count == 13


def test_area_membership_uses_declared_flags_not_names():
    rows = [item(n) for n in range(1, 6)]
    links = [link(201, 1, 2), link(202, 2, 3, 101), link(203, 4, 2, 102), link(204, 2, 5)]
    capture = graph(
        rows, links, [meaning(), meaning(101, consequence=True), meaning(102, decomposition=True)]
    )
    found = area_membership(area(), capture)
    assert found.first_hop_ids == (ident(2),)
    assert found.second_hop_ids == (ident(3), ident(4))
    assert found.path_link_ids == (ident(201), ident(202), ident(203))
    # Meanings have no name field here. The adapter cannot change the rule by renaming a word.
    no_flags = replace(capture, meanings=(meaning(), meaning(101), meaning(102)))
    assert area_membership(area(), no_flags).second_hop_ids == ()


def test_area_membership_uses_both_link_directions():
    links = (link(201, 2, 1), link(202, 1, 3), link(203, 4, 2, 101), link(204, 3, 5, 101))
    capture = graph(
        [item(n) for n in range(1, 6)], links, [meaning(), meaning(101, consequence=True)]
    )
    found = area_membership(area(), capture)
    assert found.first_hop_ids == (ident(2), ident(3))
    assert found.second_hop_ids == (ident(4), ident(5))
    assert capture.links == links
    assert capture.links[0].from_item == ident(2)


def test_area_has_no_third_hop_and_deduplicates_cycles():
    links = [
        link(201, 1, 2),
        link(202, 2, 3),
        link(203, 3, 4),
        link(204, 2, 1),
        link(205, 2, 2),
        link(206, 1, 2),
    ]
    capture = graph([item(n) for n in range(1, 5)], links, [meaning(consequence=True)])
    found = area_membership(area(), capture)
    assert found.member_ids == (ident(1), ident(2), ident(3))
    assert ident(203) not in found.path_link_ids
    assert ident(205) not in found.path_link_ids
    assert found.member_count == 3


def test_area_meaning_partition_filters_first_hop():
    selected = area(by="meaning", meanings=(100,))
    capture = graph(
        [item(n) for n in range(1, 6)],
        [link(201, 1, 2), link(202, 1, 3, 101), link(203, 2, 4, 102), link(204, 3, 5, 102)],
        [meaning(), meaning(101), meaning(102, consequence=True)],
        [selected],
    )
    assert area_membership(selected, capture).member_ids == (ident(1), ident(2), ident(4))


def test_area_second_level_partitions_share_one_hub():
    first = area(500, by="second_level_hub", meanings=(100,), anchors=(2,))
    second = area(501, by="second_level_hub", meanings=(100,), anchors=(3,))
    capture = graph(
        [item(n) for n in range(1, 5)],
        [link(201, 1, 2), link(202, 1, 3), link(203, 2, 4, 101), link(204, 3, 4, 101)],
        [meaning(), meaning(101, decomposition=True)],
        [first, second],
    )
    one, two = area_membership(first, capture), area_membership(second, capture)
    assert one.hub_item == two.hub_item == ident(1)
    assert one.first_hop_ids == (ident(2),)
    assert two.first_hop_ids == (ident(3),)
    assert one.second_hop_ids == two.second_hop_ids == (ident(4),)
    assert one.member_count == two.member_count == 3
    assert partition_coverage(ident(1), (first, second), capture).complete


def test_area_single_meaning_star_can_partition():
    leaves = range(2, 5002)
    rows = [item(1), *(item(n) for n in leaves)]
    links = [link(10000 + n, 1, n) for n in leaves]
    parts = tuple(
        area(
            20000 + start,
            by="second_level_hub",
            meanings=(100,),
            anchors=tuple(range(start, start + 100)),
        )
        for start in range(2, 5002, 100)
    )
    capture = graph(rows, links, areas=parts)
    counts = [area_membership(part, capture).member_count for part in parts]
    assert counts == [101] * 50
    assert all(not is_hub(capture, ident(n)) for n in leaves)
    coverage = partition_coverage(ident(1), parts, capture)
    assert coverage.complete and len(coverage.covered_link_ids) == 5000
    assert hub_degree(capture, ident(1)) == 5000


def test_area_partition_union_survives_new_links_and_overlapping_meanings():
    first = area(500, by="meaning", meanings=(100,))
    second = area(501, by="second_level_hub", meanings=(101,), anchors=(2,))
    capture = graph(
        [item(n) for n in range(1, 4)],
        [link(201, 1, 2), link(202, 1, 2, 101), link(203, 1, 3, 101)],
        [meaning(), meaning(101)],
        [first, second],
    )
    coverage = partition_coverage(ident(1), (first, second), capture)
    assert not coverage.complete
    assert coverage.covered_link_ids == (ident(201), ident(202))
    assert coverage.uncovered_link_ids == (ident(203),)
    updated = replace(second, partition_rule=PartitionRule("meaning", (ident(101),)))
    assert partition_coverage(ident(1), (first, updated), capture).complete


@pytest.mark.parametrize("axis", ["world", "record"])
def test_area_world_and_record_axes_use_selected_links(axis):
    selected = area(by="second_level_hub", meanings=(100,), anchors=(2, 3))
    capture = graph(
        [item(n) for n in range(1, 4)],
        [link(201, 1, 2), link(202, 1, 3)],
        areas=[selected],
        axis=axis,
        effective_at=PAST,
        eligible_item_ids=frozenset({ident(1), ident(2)}),
        eligible_link_ids=frozenset({ident(201)}),
    )
    found = area_membership(selected, capture)
    assert found.member_ids == (ident(1), ident(2))
    assert found.axis == axis and found.effective_at == PAST
    assert found.captured_at == NOW and found.commit == 9
    # The current graph can differ. No active-key inference or current definition lookup occurs.
    current = replace(
        capture,
        axis="current",
        effective_at=NOW,
        eligible_item_ids=frozenset({ident(1), ident(3)}),
        eligible_link_ids=frozenset({ident(202)}),
    )
    assert area_membership(selected, current).member_ids == (ident(1), ident(3))


@pytest.mark.parametrize("hidden_kind", ["item", "link"])
def test_area_scope_blocks_hidden_bridges_and_counts(hidden_kind):
    capture = graph(
        [item(n) for n in range(1, 4)],
        [link(201, 1, 2), link(202, 2, 3)],
        [meaning(consequence=True)],
    )
    if hidden_kind == "item":
        capture = replace(capture, authorized_item_ids=frozenset({ident(1), ident(3)}))
    else:
        capture = replace(capture, authorized_link_ids=frozenset({ident(202)}))
    found = area_membership(area(), capture)
    assert found.member_ids == (ident(1),)
    assert found.path_link_ids == () and hub_degree(capture, ident(1)) == 0


def test_area_hidden_hub_or_area_discloses_no_membership_or_count():
    capture = graph(
        [item(1), item(2)], [link(201, 1, 2)], authorized_item_ids=frozenset({ident(2)})
    )
    with pytest.raises(AreaError, match="area_unavailable"):
        area_membership(area(), capture)
    with pytest.raises(AreaError, match="area_unavailable"):
        hub_degree(capture, ident(1))
    assert area_index((area(),), capture) == ()
    visible_hub = replace(
        capture,
        authorized_item_ids=frozenset({ident(1), ident(2)}),
        authorized_area_ids=frozenset(),
    )
    with pytest.raises(AreaError, match="area_unavailable"):
        area_membership(area(), visible_hub)
    assert area_index((area(),), visible_hub) == ()


@pytest.mark.parametrize("foreign", ["item", "link", "meaning", "area"])
def test_area_cross_world_rows_are_refused(foreign):
    rows, links, words = [item(1), item(2)], [link(201, 1, 2)], [meaning()]
    if foreign == "item":
        rows[1] = replace(rows[1], world_id=OTHER_WORLD)
    elif foreign == "link":
        links[0] = replace(links[0], world_id=OTHER_WORLD)
    elif foreign == "meaning":
        words[0] = replace(words[0], world_id=OTHER_WORLD)
    if foreign == "area":
        capture = graph(rows, links, words)
        with pytest.raises(AreaError, match="area_scope_mismatch"):
            area_membership(replace(area(), world_id=OTHER_WORLD), capture)
    else:
        with pytest.raises(AreaError, match="area_scope_mismatch"):
            graph(rows, links, words)


def test_area_incomplete_graph_and_unknown_references_never_mean_empty():
    with pytest.raises(AreaError, match="incomplete_area_graph"):
        graph([item(1)], [], complete=False)
    with pytest.raises(AreaError, match="invalid_area_reference"):
        graph([item(1)], [link(201, 1, 2)])
    with pytest.raises(AreaError, match="invalid_area_reference"):
        graph([item(1)], [], authorized_item_ids=frozenset({ident(2)}))
    capture = graph([item(1)], [])
    unknown = area(by="second_level_hub", meanings=(100,), anchors=(2,))
    with pytest.raises(AreaError, match="invalid_area_reference"):
        area_membership(unknown, capture)
    unknown_word = area(by="meaning", meanings=(999,))
    with pytest.raises(AreaError, match="invalid_area_reference"):
        area_membership(unknown_word, capture)


def test_area_duplicate_graph_rows_are_refused():
    duplicate = link(201, 1, 2)
    with pytest.raises(AreaError, match="duplicate_area_graph_id"):
        graph([item(1), item(2)], [duplicate, duplicate])


def test_area_index_handles_retired_and_merged_definitions_without_guessing_history():
    current = area(500)
    old = area(501, status="merged", merged_into=current.id)
    oldest = area(502, status="merged", merged_into=old.id)
    retired = area(503, status="retired")
    rows = (current, old, oldest, retired)
    capture = graph([item(1)], [], areas=rows)
    assert len(area_index(rows, capture)) == 1
    index = area_index(rows, capture, include_ended=True)
    assert [entry.successor_area_id for entry in index] == [None, current.id, current.id, None]
    historical_rows = (replace(old, status="active", merged_into=None),)
    historical = graph([item(1)], [], areas=historical_rows, axis="record", effective_at=PAST)
    assert area_index(historical_rows, historical)[0].membership.area_id == old.id
    with pytest.raises(AreaError, match="incomplete_area_registry"):
        area_index((old,), capture)


def test_area_merge_cycle_and_foreign_successor_are_refused():
    one = area(500, status="merged", merged_into=ident(501))
    two = area(501, status="merged", merged_into=ident(500))
    capture = graph([item(1)], [], areas=(one, two))
    with pytest.raises(AreaError, match="area_merge_cycle"):
        area_index((one, two), capture, include_ended=True)
    with pytest.raises(AreaError, match="invalid_area_reference"):
        area_membership(one, graph([item(1)], [], areas=(one,)))


def test_area_hidden_successor_is_not_disclosed():
    current = area(500, hub=2)
    old = area(501, status="merged", merged_into=current.id)
    rows = (current, old)
    capture = graph(
        [item(1), item(2)],
        [],
        areas=rows,
        authorized_item_ids=frozenset({ident(1)}),
        authorized_area_ids=frozenset({old.id}),
    )
    assert area_membership(old, capture).merged_into is None
    index = area_index(rows, capture, include_ended=True)
    assert len(index) == 1
    assert index[0].successor_area_id is None and index[0].membership.merged_into is None


def test_area_hidden_merge_bridge_cannot_reveal_a_later_successor():
    current = area(500, hub=2)
    hidden = area(501, hub=3, status="merged", merged_into=current.id)
    old = area(502, status="merged", merged_into=hidden.id)
    rows = (current, hidden, old)
    capture = graph(
        [item(1), item(2), item(3)],
        [],
        areas=rows,
        authorized_item_ids=frozenset({ident(1), ident(2)}),
        authorized_area_ids=frozenset({old.id, current.id}),
    )
    index = area_index(rows, capture, include_ended=True)
    old_entry = next(entry for entry in index if entry.membership.area_id == old.id)
    assert old_entry.successor_area_id is None
    assert old_entry.membership.merged_into is None


@pytest.mark.parametrize("restriction", ["area", "hub", "time", "none"])
def test_successor_metadata_requires_complete_registry_and_visible_eligible_hub(restriction):
    current = area(500, hub=2)
    old = area(501, status="merged", merged_into=current.id)
    rows = (current, old)
    options = {}
    if restriction == "area":
        options["authorized_area_ids"] = frozenset({old.id})
    elif restriction == "hub":
        options["authorized_item_ids"] = frozenset({ident(1)})
    elif restriction == "time":
        options["eligible_item_ids"] = frozenset({ident(1)})
    capture = graph([item(1), item(2)], [], areas=rows, **options)
    assert area_membership(old, capture).merged_into is None
    indexed = area_index(rows, capture, include_ended=True)
    result = next(entry for entry in indexed if entry.membership.area_id == old.id)
    expected = current.id if restriction == "none" else None
    assert result.membership.merged_into == expected
    assert result.successor_area_id == expected


def test_area_inputs_and_outputs_are_immutable():
    item_rows, link_rows = [item(1), item(2)], [link(201, 1, 2)]
    allowed = {ident(1), ident(2)}
    capture = graph(item_rows, link_rows, authorized_item_ids=allowed)
    selected = area()
    before = area_membership(selected, capture)
    item_rows.clear()
    link_rows.clear()
    allowed.clear()
    assert area_membership(selected, capture) == before
    with pytest.raises(FrozenInstanceError):
        selected.version = 2
    with pytest.raises(FrozenInstanceError):
        before.member_ids = ()
    with pytest.raises(TypeError):
        capture._items[ident(3)] = item(3)
    assert capture.commit == 9 and selected.version == 1
