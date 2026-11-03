"""D3 history reads: one item's story (item_history), what changed in a span of record time
(changes_between), and what Grove stopped believing (stopped_believing).

Spec, D3 "How to test it": "Ended and refuted links are told apart in 'what changed' and in 'what
we stopped believing'." Every name here is invented.

Entries the writer has no operation for yet (an undo's inverse, the refute and the retire_record
of an item, a "corrected by" link) are planted through grove.writer.testing in the shape that
Part A of the writer-b lane writes them; each plant says so.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id
from grove.store.history_reads import changes_between, item_history, stopped_believing
from grove.store.primitives import MEANING_CORRECTED_BY
from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node
from tests.writer import helpers
from tests.writer.helpers import (
    apply,
    clock_at,
    create_item,
    create_link,
    created_id,
    iso,
    new_op_id,
    observation,
    read,
    supports,
)

OCT_20 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
OCT_25 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
OCT_27 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
OCT_28 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OCT_30 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
NOV_1 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NOV_3 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
seeded = helpers.seeded
chat = helpers.chat
# Above every sequence the writer allocates in a test world.
PLANTED_SEQUENCE = 1_000_000


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(OCT_20 + HOUR)
    with clock.using(test_clock):
        yield test_clock


def end_link(link: str, end_class: str, stop: Any, ev_id: str | None = None) -> dict[str, Any]:
    op = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": link,
        "end_class": end_class,
        "stopped_being_true": stop,
        "reason": f"the link {end_class}",
    }
    if ev_id is not None:
        op["evidence_refs"] = supports(ev_id)
    return op


def version_of(driver, world_id: str, item_id: str) -> int:
    [row] = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i.version AS v", id=item_id)
    return row["v"]


def evidence_of(driver, world_id: str, target_id: str) -> list[str]:
    rows = read(
        driver,
        world_id,
        "MATCH (e:Evidence {target_id: $id}) RETURN e.id AS id ORDER BY e.recorded, e.id",
        id=target_id,
    )
    return [row["id"] for row in rows]


def change_set_of(driver, world_id: str, item_id: str, field: str) -> str:
    [row] = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {entity_id: $id, field: $field}) RETURN e.change_set_id AS c",
        id=item_id,
        field=field,
    )
    return row["c"]


def plant_entry(
    driver,
    world_id: str,
    sequence: int,
    *,
    entity_id: str,
    field: str,
    new: Any,
    recorded_at: datetime,
    world_time: datetime | None,
    reason: str,
    **columns: Any,
) -> str:
    """One change entry with its value and its reason text, as the writer stores them. Returns
    the change set id."""
    value = plant_node(driver, world_id, "HistoryValue", value=json.dumps(new))
    reason_value = plant_node(driver, world_id, "HistoryValue", value=reason)
    change_set_id = columns.pop("change_set_id", None) or new_id(world_id)
    entry = {
        "sequence": PLANTED_SEQUENCE + sequence,
        "entity_type": "item",
        "entity_id": entity_id,
        "field": field,
        "new_value_id": value["id"],
        "reason": reason_value["id"],
        "recorded_at": recorded_at,
        "actor": "person:t",
        "commit_id": new_id(world_id),
        "change_set_id": change_set_id,
        "history_only": False,
        **columns,
    }
    if world_time is not None:
        entry["world_time"] = world_time
    plant_node(driver, world_id, "ChangeEntry", **entry)
    return change_set_id


def by_entity(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for change in changes:
        assert change["entity_id"] not in found, "one change set per entity in these spans"
        found[change["entity_id"]] = change
    return found


# What changed, and what Grove stopped believing ----------------------------------------------


def test_ended_and_refuted_told_apart(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    ops = [
        create_item("person", "Ilse Okonkwo", "a1", temp_id="$ilse"),
        create_item("organisation", "Velmora arts guild", "a1", temp_id="$guild"),
        create_item("organisation", "Rowan's print shop", "a1", temp_id="$shop"),
        create_link("$ilse", "part of", "$guild", "a1", became_true=clock_at(OCT_20)),
        create_link("$ilse", "part of", "$shop", "a1", became_true=clock_at(OCT_20)),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "a1", 1, at=OCT_20)])
    items = {created_id(receipt, nth=n) for n in range(3)}
    ended, refuted = (created_id(receipt, ">", "link", nth=n) for n in range(2))
    # On 3 November: Ilse left the guild on 28 October, and she was never part of the shop.
    frozen.set(NOV_3)
    apply(
        driver,
        w,
        [
            end_link(ended, "ended_in_the_world", clock_at(OCT_28), "b1"),
            end_link(refuted, "refuted", "unknown"),
        ],
        [observation(chat, "b1", 2, at=NOV_3)],
    )
    query = WorldQuery(driver, w)

    changes = changes_between(query, NOV_3, NOV_3 + HOUR)
    links = by_entity([change for change in changes if change["entity_type"] == "link"])
    assert set(links) == {ended, refuted}
    assert (links[ended]["kind"], links[ended]["end_class"]) == ("ended", "ended_in_the_world")
    assert (links[refuted]["kind"], links[refuted]["end_class"]) == ("ended", "refuted")
    assert links[ended]["stopped_being_true"]["earliest"] == iso(OCT_28)
    assert links[ended]["reason"] == "the link ended_in_the_world"
    assert links[ended]["recorded_at"] == NOV_3
    # The evidence row that end_link wrote for the ending is new in this span.
    others = [change for change in changes if change["entity_type"] != "link"]
    assert [(change["entity_type"], change["kind"]) for change in others] == [
        ("evidence", "created")
    ]

    stopped = stopped_believing(query, NOV_3, NOV_3 + HOUR)
    assert [(change["entity_id"], change["end_class"]) for change in stopped] == [
        (refuted, "refuted")
    ]
    assert stopped == [links[refuted]]

    # The span is on the record axis, start included and end left out.
    assert changes_between(query, NOV_3 - HOUR, NOV_3) == []
    created = changes_between(query, OCT_20 + HOUR, NOV_3)
    assert {change["kind"] for change in created} == {"created"}
    assert items | {ended, refuted} <= {change["entity_id"] for change in created}
    assert stopped_believing(query, OCT_20, NOV_3) == []


def test_changes_between_names_each_kind_and_only_refuted_is_unbelieved(
    driver, seeded, chat, frozen
):
    w = seeded["world_id"]
    ops = [
        create_item("organisation", "Rowan's print shop", "c1"),
        create_item("decision", "Ilse chose the Okonkwo hall", "c1"),
        create_item("decision", "Tamsin chose the river room", "c1"),
    ]
    told = observation(chat, "c1", 1, at=OCT_20, statement_class="decision")
    receipt = apply(driver, w, ops, [told])
    shop, hall, river = (created_id(receipt, nth=n) for n in range(3))
    frozen.set(OCT_25 + HOUR)
    renamed = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": shop,
        "version": version_of(driver, w, shop),
        "fields": {"title": "Rowan and Ilse's print shop"},
        "evidence_refs": supports("c2"),
        "reason": "the shop took a new name",
    }
    apply(driver, w, [renamed], [observation(chat, "c2", 2, at=OCT_25)])
    frozen.set(OCT_27 + HOUR)
    closed = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": shop,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(OCT_27),
        "evidence_refs": supports("c3"),
        "reason": "the shop closed",
    }
    apply(driver, w, [closed], [observation(chat, "c3", 3, at=OCT_27)])
    frozen.set(OCT_30 + HOUR)
    reopened = {
        "op": "reopen_item",
        "op_id": new_op_id(),
        "item": shop,
        "reason": "the shop opened again",
    }
    apply(driver, w, [reopened], [])

    # The undo of the rename, as Part A's undo writes it: the old value back, with inverse_of.
    renamed_in = change_set_of(driver, w, shop, "title")
    plant_entry(
        driver,
        w,
        1,
        entity_id=shop,
        field="title",
        new="Rowan's print shop",
        recorded_at=NOV_1,
        world_time=OCT_25,
        reason="the rename was a misreading",
        inverse_of=renamed_in,
    )
    # A refute of one decision and a retire_record of the other, as Part A writes them: an
    # end_class entry; spent has no world time (record axis only).
    plant_entry(
        driver,
        w,
        2,
        entity_id=hall,
        field="end_class",
        new="refuted",
        recorded_at=NOV_3,
        world_time=OCT_20,
        reason="Ilse never chose the hall",
    )
    plant_entry(
        driver,
        w,
        3,
        entity_id=river,
        field="end_class",
        new="spent",
        recorded_at=NOV_3,
        world_time=None,
        reason="its lesson lives in the venue belief",
    )
    frozen.set(NOV_3)
    first_row = evidence_of(driver, w, shop)[0]
    dropped = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": first_row,
        "end_reason": "correction",
        "reason": "the line named another shop",
    }
    apply(driver, w, [dropped], [])
    query = WorldQuery(driver, w)

    def kinds(start: datetime) -> dict[str, tuple[str, ...]]:
        return {
            entity: (change["kind"], change["end_class"] or change["end_reason"])
            for entity, change in by_entity(changes_between(query, start, start + DAY)).items()
        }

    made = kinds(OCT_20)
    assert {shop, hall, river, first_row} <= set(made)
    assert set(made.values()) == {("created", None)}
    shop_renamed = by_entity(changes_between(query, OCT_25, OCT_25 + DAY))[shop]
    assert (shop_renamed["kind"], shop_renamed["reason"]) == ("changed", "the shop took a new name")
    assert "title" in shop_renamed["fields"]
    assert kinds(OCT_27)[shop] == ("ended", "ended_in_the_world")
    assert kinds(OCT_30) == {shop: ("reopened", None)}
    undone = by_entity(changes_between(query, NOV_1, NOV_1 + DAY))
    assert set(undone) == {shop}
    assert (undone[shop]["kind"], undone[shop]["inverse_of"]) == ("undone", renamed_in)
    assert kinds(NOV_3) == {
        hall: ("ended", "refuted"),
        river: ("ended", "spent"),
        first_row: ("ended", "correction"),
    }

    # Only the refuted decision is a belief Grove gave up: not the spent record, not the
    # evidence row ended by a correction, not the shop that closed in the world.
    assert [change["entity_id"] for change in stopped_believing(query, OCT_20, NOV_3 + DAY)] == [
        hall
    ]


def test_a_span_needs_aware_times_in_order(driver, seeded):
    query = WorldQuery(driver, seeded["world_id"])
    with pytest.raises(ValueError):
        changes_between(query, datetime(2026, 11, 1, 9, 0), NOV_3)
    with pytest.raises(ValueError):
        stopped_believing(query, NOV_3, NOV_1)


# One item's story -----------------------------------------------------------------------------


def test_item_history_reads_the_item_its_links_and_its_evidence_in_sequence(
    driver, seeded, chat, frozen
):
    w = seeded["world_id"]
    ops = [
        create_item("organisation", "Rowan's print shop", "h1", temp_id="$shop"),
        create_item("organisation", "Velmora arts guild", "h1", temp_id="$guild"),
        create_item("organisation", "Tamsin's bakery", "h1", temp_id="$bakery"),
        create_link("$shop", "part of", "$guild", "h1", became_true=clock_at(OCT_20)),
        create_link("$bakery", "part of", "$guild", "h1", became_true=clock_at(OCT_20)),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "h1", 1, at=OCT_20)])
    shop, guild, bakery = (created_id(receipt, nth=n) for n in range(3))
    joined, bakery_link = (created_id(receipt, ">", "link", nth=n) for n in range(2))
    frozen.set(OCT_25 + HOUR)
    renamed = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": shop,
        "version": version_of(driver, w, shop),
        "fields": {"title": "Rowan and Ilse's print shop"},
        "evidence_refs": supports("h2"),
        "reason": "the shop took a new name",
    }
    apply(driver, w, [renamed], [observation(chat, "h2", 2, at=OCT_25)])
    frozen.set(OCT_27 + HOUR)
    first_row = evidence_of(driver, w, shop)[0]
    dropped = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": first_row,
        "end_reason": "correction",
        "reason": "the line named another shop",
    }
    apply(driver, w, [dropped], [])
    query = WorldQuery(driver, w)

    history = item_history(query, shop)
    sequences = [entry["sequence"] for entry in history]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    mine = {shop, joined, *evidence_of(driver, w, shop), *evidence_of(driver, w, joined)}
    assert {entry["entity_id"] for entry in history} == mine
    assert {entry["entity_type"] for entry in history} == {"item", "link", "evidence"}
    assert not {guild, bakery, bakery_link} & {entry["entity_id"] for entry in history}

    [made] = [e for e in history if (e["entity_id"], e["field"]) == (shop, "row")]
    assert made["new"]["table"] == "items"
    assert made["new"]["row"]["title"] == "Rowan's print shop"
    assert made["reason"] == "the message states it"
    [link_made] = [e for e in history if (e["entity_id"], e["field"]) == (joined, "row")]
    assert link_made["new"]["table"] == "links"
    assert (link_made["new"]["row"]["from_item"], link_made["new"]["row"]["to_item"]) == (
        shop,
        guild,
    )
    [title] = [e for e in history if (e["entity_id"], e["field"]) == (shop, "title")]
    assert (title["old"], title["new"]) == ("Rowan's print shop", "Rowan and Ilse's print shop")
    assert (title["reason"], title["actor"], title["world_time"]) == (
        "the shop took a new name",
        "person:t",
        OCT_25,
    )
    assert title["recorded_at"] == OCT_25 + HOUR
    assert title["history_only"] is False and title["inverse_of"] is None
    assert title["evidence_id"] in evidence_of(driver, w, shop)
    [ended] = [e for e in history if e["field"] == "end_reason"]
    assert (ended["entity_id"], ended["new"], ended["reason"]) == (
        first_row,
        "correction",
        "the line named another shop",
    )
    assert history.successors == []

    # A "corrected by" link, as Part A's correct writes it: the shop's history now names its
    # successor.
    successor = created_id(
        apply(
            driver,
            w,
            [create_item("organisation", "Rowan's print studio", "h3")],
            [observation(chat, "h3", 3, at=OCT_27)],
        )
    )
    corrected = plant_link(
        driver,
        w,
        shop,
        successor,
        meaning=MEANING_CORRECTED_BY,
        recorded=OCT_27 + HOUR,
        version=1,
        became_true_precision="instant",
        became_true_earliest=OCT_27,
        became_true_latest=OCT_27,
        active_key=f"{w}|{shop}|{MEANING_CORRECTED_BY}|{successor}",
    )["id"]
    assert item_history(query, shop).successors == [
        {"id": successor, "link_id": corrected, "meaning": MEANING_CORRECTED_BY}
    ]
    assert item_history(query, guild).successors == []
    assert item_history(query, new_id(w)) == []
