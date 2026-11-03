"""D3 "Two time axes, kept apart": the world axis, world_as_of (what was true at T, with everything
Grove knows now).

Spec, D3 "How to test it": "A link ended with a past world end time keeps 'what was true on the
1st' and 'what Grove believed on the 1st' distinct and correct." The record axis half is
known_as_of (Part C of the writer-b lane). Every name here is invented.

Rows the writer has no operation for yet (the successor links of correct and supersede, and a
history-only stop clock of a late terminal status) are planted through grove.writer.testing, as
those operations will write them; each plant says so.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id
from grove.store.asof import item_as_of, world_as_of
from grove.store.axes import AsOf
from grove.store.current import Successor
from grove.store.history_reads import changes_between
from grove.store.primitives import MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY
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
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

OCT_20 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
OCT_22 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
OCT_25 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
OCT_27 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
OCT_28 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
OCT_30 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
NOV_1 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NOV_3 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
NOV_4 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
NOV_5 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
RECHECK = "2026-12-01T00:00:00+00:00"
# The writer's fixtures: a seeded world and a chat source to cite.
seeded = helpers.seeded
chat = helpers.chat
# Above every sequence the writer allocates in a test world: a planted entry sorts last, as an
# entry written after every batch of the test does.
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


def end_item(item: str, end_class: str, stop: Any, ev_id: str) -> dict[str, Any]:
    return {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": item,
        "end_class": end_class,
        "stopped_being_true": stop,
        "evidence_refs": supports(ev_id),
        "reason": f"the item {end_class}",
    }


def update(item: str, version: int, fields: dict[str, Any], ev_id: str, reason: str) -> dict:
    return {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item,
        "version": version,
        "fields": fields,
        "evidence_refs": supports(ev_id),
        "reason": reason,
    }


def change_status(item: str, status: str, ev_id: str) -> dict[str, Any]:
    return {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": item,
        "status": status,
        "evidence_refs": supports(ev_id),
        "reason": f"the message says {status}",
    }


def version_of(driver, world_id: str, item_id: str) -> int:
    [row] = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i.version AS v", id=item_id)
    return row["v"]


def instant_clock(at: datetime) -> dict[str, Any]:
    """The columns of a clock at precision instant, as the composite operations write a
    successor link's start (writer-b design, section 2)."""
    return {
        "became_true_precision": "instant",
        "became_true_earliest": at,
        "became_true_latest": at,
    }


def plant_successor(driver, world_id: str, old: str, new: str, meaning: str, **columns: Any) -> str:
    """A "corrected by" or "superseded by" link, as correct and supersede (Part A) will write it:
    the base has no operation that writes a primitive meaning."""
    row = {
        "meaning": meaning,
        "recorded": NOV_3,
        "version": 1,
        **columns,
    }
    if "end_class" not in row:
        row["active_key"] = f"{world_id}|{old}|{meaning}|{new}"
    return plant_link(driver, world_id, old, new, **row)["id"]


def two_items_and_a_link(driver, w: str, src: dict[str, str]) -> tuple[str, str, str]:
    """An organisation that is part of another since 20 October, all recorded that day."""
    ops = [
        create_item("organisation", "Okonkwo print studio", "a1", temp_id="$studio"),
        create_item("organisation", "Velmora arts guild", "a1", temp_id="$guild"),
        create_link("$studio", "part of", "$guild", "a1", became_true=clock_at(OCT_20)),
    ]
    receipt = apply(driver, w, ops, [observation(src, "a1", 1, at=OCT_20)])
    return created_id(receipt, nth=0), created_id(receipt, nth=1), created_id(receipt, ">", "link")


# The ending of a link -------------------------------------------------------------------------


def test_past_world_end_keeps_true_then_and_believed_then_apart(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    studio, guild, link = two_items_and_a_link(driver, w, chat)
    # On 3 November Grove learns that the studio left the guild on 28 October.
    frozen.set(NOV_3)
    told = observation(chat, "b1", 2, at=NOV_3)
    apply(driver, w, [end_link(link, "ended_in_the_world", clock_at(OCT_28), "b1")], [told])
    query = WorldQuery(driver, w)

    on_the_1st = world_as_of(query, NOV_1)
    assert isinstance(on_the_1st, AsOf)
    assert (on_the_1st.axis, on_the_1st.at) == ("world", NOV_1)
    # What was true on the 1st: the link had ended; both items it joined still stood.
    assert link not in on_the_1st.links
    assert {studio, guild} <= set(on_the_1st.items)
    assert on_the_1st.items[studio].standing.state == "current"
    # Before its world end the link was true.
    before = world_as_of(query, OCT_25).links[link]
    assert before.standing.state == "current"
    assert before.row["became_true_earliest"] == iso(OCT_20)

    # The two questions differ because the ending was recorded on 3 November, after the 1st, and
    # placed in the world on 28 October.
    [ending] = [
        change
        for change in changes_between(query, NOV_3, NOV_3 + HOUR)
        if change["entity_id"] == link
    ]
    assert (ending["kind"], ending["end_class"]) == ("ended", "ended_in_the_world")
    assert ending["recorded_at"] == NOV_3
    assert ending["stopped_being_true"]["earliest"] == iso(OCT_28)
    # TODO(writer-b Part C): known_as_of(query, NOV_1) must still hold the link, current: on the
    # 1st Grove had not recorded its end. That is the record-axis half of this D3 line.


def test_world_as_of_drops_a_refuted_link_always_and_an_ended_one_after_its_stop(
    driver, seeded, chat, frozen
):
    w = seeded["world_id"]
    ops = [
        create_item("person", "Ilse Okonkwo", "a1", temp_id="$ilse"),
        create_item("organisation", "Velmora arts guild", "a1", temp_id="$guild"),
        create_item("organisation", "Rowan's print shop", "a1", temp_id="$shop"),
        create_link("$ilse", "part of", "$guild", "a1", became_true=clock_at(OCT_20)),
        create_link("$ilse", "part of", "$shop", "a1", became_true=clock_at(OCT_20)),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "a1", 1, at=OCT_20)])
    items = [created_id(receipt, nth=n) for n in range(3)]
    ended, refuted = (created_id(receipt, ">", "link", nth=n) for n in range(2))
    frozen.set(NOV_3)
    apply(
        driver,
        w,
        [
            end_link(ended, "ended_in_the_world", clock_at(OCT_28), "b1"),
            # A stop clock after every time read below: only "never true" keeps it out.
            end_link(refuted, "refuted", clock_at(NOV_5)),
        ],
        [observation(chat, "b1", 2, at=NOV_3)],
    )
    query = WorldQuery(driver, w)

    for at, held in ((OCT_22, {ended}), (OCT_25, {ended}), (OCT_30, set()), (NOV_4, set())):
        found = world_as_of(query, at)
        assert set(found.links) == held, at
        assert set(items) <= set(found.items), at
        assert all(row.standing.state == "current" for row in found.links.values())
    # The refuted link is in the store, ended, with its stop clock: the read leaves it out.
    [row] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=refuted)
    assert row["l"]["end_class"] == "refuted" and row["l"]["stopped_being_true_earliest"] == NOV_5


# The content and the ending of an item --------------------------------------------------------


def test_item_content_as_of_follows_the_replay(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    shop = create_item("organisation", "Rowan's print shop", "c1")
    shop_id = created_id(apply(driver, w, [shop], [observation(chat, "c1", 1, at=OCT_20)]))
    frozen.set(OCT_25 + HOUR)
    renamed = update(
        shop_id,
        version_of(driver, w, shop_id),
        {"title": "Rowan and Ilse's print shop"},
        "c2",
        "the shop took a new name",
    )
    apply(driver, w, [renamed], [observation(chat, "c2", 2, at=OCT_25)])
    query = WorldQuery(driver, w)

    assert world_as_of(query, OCT_22).items[shop_id].row["title"] == "Rowan's print shop"
    assert world_as_of(query, OCT_27).items[shop_id].row["title"] == "Rowan and Ilse's print shop"
    # Before the message that first named it, the shop was no item of the world.
    assert shop_id not in world_as_of(query, OCT_20 - HOUR).items
    [head] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i.title AS t", id=shop_id)
    assert head["t"] == "Rowan and Ilse's print shop"


def test_an_item_ended_in_the_past_reads_ended_after_its_stop(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    ops = [
        create_item("organisation", "Rowan's print shop", "c1"),
        create_item("place", "Okonkwo hall", "c1"),
        create_item("place", "Okonkwo hall annex", "c1"),
        create_item("organisation", "Velmora ferry company", "c1"),
    ]
    receipt = apply(driver, w, ops, [observation(chat, "c1", 1, at=OCT_20)])
    shop, hall, annex, ferry = (created_id(receipt, nth=n) for n in range(4))
    # Recorded on 3 November, with evidence of that day: both stopped on 28 October. The ferry
    # stopped at a time no one gave, told by a message of 28 October: with no stop clock, the
    # end entry's world time places the end.
    frozen.set(NOV_3)
    told = observation(chat, "c2", 2, at=NOV_3)
    ops = [
        end_item(shop, "ended_in_the_world", clock_at(OCT_28), "c2"),
        end_item(hall, "superseded", clock_at(OCT_28), "c2"),
        end_item(ferry, "ended_in_the_world", "unknown", "c3"),
    ]
    apply(driver, w, ops, [told, observation(chat, "c3", 3, at=OCT_28)])
    superseded = plant_successor(
        driver, w, hall, annex, MEANING_SUPERSEDED_BY, **instant_clock(OCT_28)
    )
    query = WorldQuery(driver, w)

    before = world_as_of(query, OCT_25)
    assert before.items[shop].standing.state == "current"
    assert "end_class" not in before.items[shop].row
    assert "stopped_being_true_precision" not in before.items[shop].row
    assert (before.items[hall].standing.state, before.items[hall].standing.successors) == (
        "current",
        (),
    )
    assert before.items[ferry].standing.state == "current"

    after = world_as_of(query, NOV_1)
    # The end entries' world time is 3 November: the replay alone would still read it current.
    assert "end_class" not in item_as_of(query, shop, NOV_1)
    assert shop not in after.items
    assert ferry not in after.items
    # An overtaken item stays, ended, with its successor on the standing.
    kept = after.items[hall]
    assert (kept.standing.state, kept.standing.end_class) == ("ended", "superseded")
    assert kept.standing.successors == (Successor(annex, superseded, MEANING_SUPERSEDED_BY),)
    assert kept.row["end_class"] == "superseded"
    assert kept.row["stopped_being_true_earliest"] == iso(OCT_28)
    assert annex in after.items


def plant_ended_item(
    driver, world_id: str, kind: str, title: str, end_class: str, ended_at: datetime | None, n: int
) -> str:
    """An item made on 20 October and ended on 3 November, head and entries, as Part A's refute
    (end class refuted, no stop clock, the end entry at the world time of its evidence) and
    retire_record (end class spent and lesson_in, both entries with no world time) write it. The
    base has neither operation, and a test outside tests/writer holds no write Cypher, so the
    whole item is planted."""
    columns = {"kind": kind, "title": title, "became_true_precision": "unknown", "version": 1}
    item = plant_node(driver, world_id, "Item", **columns, recorded=OCT_20, end_class=end_class)
    made = {"table": "items", "row": {**columns, "id": item["id"], "world_id": world_id}}

    def entry(sequence: int, field: str, value: Any, recorded_at: datetime, at: datetime | None):
        stored = plant_node(driver, world_id, "HistoryValue", value=json.dumps(value))
        row = {
            "sequence": PLANTED_SEQUENCE + 10 * n + sequence,
            "entity_type": "item",
            "entity_id": item["id"],
            "field": field,
            "new_value_id": stored["id"],
            "recorded_at": recorded_at,
            "actor": "person:t",
            "commit_id": new_id(world_id),
            "change_set_id": new_id(world_id),
            "history_only": False,
        }
        if at is not None:
            row["world_time"] = at
        plant_node(driver, world_id, "ChangeEntry", **row)

    entry(0, "row", made, OCT_20 + HOUR, OCT_20)
    entry(1, "end_class", end_class, NOV_3, ended_at)
    if end_class == "spent":
        entry(2, "lesson_in", new_id(world_id), NOV_3, None)
    return item["id"]


def test_a_refuted_item_never_stands_and_a_spent_one_stands_on_the_world_axis(
    driver, seeded, chat, frozen
):
    w = seeded["world_id"]
    shop = create_item("organisation", "Rowan's print shop", "r1")
    shop_id = created_id(apply(driver, w, [shop], [observation(chat, "r1", 1, at=OCT_20)]))
    [head] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i.kind AS kind", id=shop_id)
    # Refuted on 3 November by a message of 1 November: it was never true, before that too.
    refuted = plant_ended_item(driver, w, head["kind"], "Rowan's second shop", "refuted", NOV_1, 1)
    # Spent on 3 November: Grove stopped serving it, and its claim did not stop being true.
    spent = plant_ended_item(driver, w, head["kind"], "Ilse's guild", "spent", None, 2)
    query = WorldQuery(driver, w)

    for at in (OCT_22, OCT_25, NOV_4):
        found = world_as_of(query, at)
        assert refuted not in found.items, at
        assert found.items[spent].standing.state == "current", at
        assert "end_class" not in found.items[spent].row, at
        assert "lesson_in" not in found.items[spent].row, at
        assert found.items[shop_id].standing.state == "current", at
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i.end_class AS c", id=refuted)
    assert row["c"] == "refuted"


def test_a_replayed_stop_clock_drops_while_the_status_is_not_terminal(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    task = create_item("task", "Book the Velmora hall", "s1", status="open", recheck_by=RECHECK)
    task_id = created_id(apply(driver, w, [task], [observation(chat, "s1", 1, at=OCT_20)]))
    frozen.set(OCT_30 + HOUR)
    working = change_status(task_id, "in progress", "s2")
    apply(driver, w, [working], [observation(chat, "s2", 2, at=OCT_30)])
    # On 3 November a message of 25 October says it was cancelled then: older than the head's
    # status, so it goes to history only.
    frozen.set(NOV_3)
    cancelled = change_status(task_id, "cancelled", "s3")
    apply(driver, w, [cancelled], [observation(chat, "s3", 3, at=OCT_25)])
    # Its stop clock as a history-only entry: what change_status writes on this path in Part A
    # of the lane design (section 2); the base refuses it (rule stopped_on_history_only).
    stop = plant_node(
        driver,
        w,
        "HistoryValue",
        value=json.dumps({"precision": "before", "latest": iso(OCT_25)}),
    )
    plant_node(
        driver,
        w,
        "ChangeEntry",
        sequence=PLANTED_SEQUENCE,
        entity_type="item",
        entity_id=task_id,
        field="stopped_being_true",
        new_value_id=stop["id"],
        recorded_at=NOV_3,
        world_time=OCT_25,
        actor="person:t",
        commit_id=new_id(w),
        change_set_id=new_id(w),
        history_only=True,
    )
    query = WorldQuery(driver, w)

    opened = world_as_of(query, OCT_22).items[task_id]
    assert (opened.standing.state, opened.row["status"]) == ("current", "open")
    # Cancelled on 25 October: the status and its stop clock end it.
    assert task_id not in world_as_of(query, OCT_27).items
    # From 30 October the task was in progress: the replayed stop clock of 25 October is dropped.
    assert item_as_of(query, task_id, NOV_1)["stopped_being_true_precision"] == "before"
    working_then = world_as_of(query, NOV_1).items[task_id]
    assert (working_then.standing.state, working_then.row["status"]) == ("current", "in progress")
    assert "stopped_being_true_precision" not in working_then.row
    assert "stopped_being_true_latest" not in working_then.row


# Successors and evidence on the world axis ----------------------------------------------------


def test_successors_ride_on_an_overtaken_item_from_the_successor_links_start(
    driver, seeded, chat, frozen
):
    w = seeded["world_id"]
    ops = [
        create_item("decision", "Ilse chose the cheaper venue", "d1"),
        create_item("decision", "Ilse chose the Okonkwo hall", "d1"),
        create_item("decision", "Tamsin chose the river room", "d1"),
    ]
    told = observation(chat, "d1", 1, at=OCT_20, statement_class="decision")
    receipt = apply(driver, w, ops, [told])
    old, new, other = (created_id(receipt, nth=n) for n in range(3))
    corrected = plant_successor(driver, w, old, new, MEANING_CORRECTED_BY, **instant_clock(OCT_28))
    # A refuted successor link (an undone correction) never overtakes.
    plant_successor(
        driver,
        w,
        old,
        other,
        MEANING_SUPERSEDED_BY,
        became_true_precision="unknown",
        end_class="refuted",
    )
    query = WorldQuery(driver, w)

    before = world_as_of(query, OCT_25)
    assert (before.items[old].standing.state, before.items[old].standing.successors) == (
        "current",
        (),
    )
    assert corrected not in before.links

    after = world_as_of(query, NOV_1)
    expected = (Successor(new, corrected, MEANING_CORRECTED_BY),)
    assert (after.items[old].standing.state, after.items[old].standing.successors) == (
        "overtaken",
        expected,
    )
    assert after.links[corrected].standing.state == "current"
    assert after.items[new].standing.successors == ()

    alone = world_as_of(query, NOV_1, item_ids=[old])
    assert set(alone.items) == {old}
    assert alone.items[old].standing.successors == expected
    assert set(alone.links) == {corrected}


def test_evidence_fields_and_planned_origin_follow_the_time_read(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    calendar = make_source(
        driver,
        w,
        "calendar",
        ["Ilse flies to Velmora, 28 October", "Okonkwo dinner, 30 October"],
    )
    pages = make_source(driver, w, "web_pages", ["Boarding pass scanned: Ilse, Velmora flight"])

    def entry(ev_id: str, line: int, at: datetime) -> dict[str, Any]:
        return observation(
            calendar, ev_id, line, at=at, locator="whole", statement_class="intention"
        )

    ops = [
        create_item("trip", "Ilse's trip to Velmora", "p1"),
        create_item("event", "Rowan hosted the Okonkwo dinner", "r1"),
    ]
    reported = observation(chat, "r1", 1, at=OCT_20, statement_class="report_happened")
    receipt = apply(driver, w, ops, [entry("p1", 1, OCT_20), reported])
    trip_id, dinner_id = created_id(receipt, nth=0), created_id(receipt, nth=1)
    frozen.set(OCT_30 + HOUR)
    start = {
        "earliest": iso(OCT_28),
        "latest": iso(OCT_28 + timedelta(days=1, seconds=-1)),
        "precision": "day",
    }
    boarded = update(
        trip_id,
        version_of(driver, w, trip_id),
        {"became_true": start, "recheck_by": RECHECK},
        "p2",
        "the boarding pass shows the flight",
    )
    confirmed = observation(pages, "p2", 1, at=OCT_30, locator="whole")
    # A calendar entry of 30 October on the dinner: from then on a plan scheduled it (D5).
    scheduled = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": dinner_id,
        "ev_id": "p3",
        "polarity": "supports",
    }
    apply(driver, w, [boarded, scheduled], [confirmed, entry("p3", 2, OCT_30)])
    [head] = read(
        driver, w, "MATCH (i:Item {id: $id}) RETURN i.derived_source_type AS t", id=trip_id
    )
    assert head["t"] == "confirmed"
    query = WorldQuery(driver, w)

    # On 25 October only the calendar entry had spoken: planned, not "confirmed with no start".
    then = world_as_of(query, OCT_25).items[trip_id]
    assert (then.standing.state, then.standing.reason) == ("planned", "not_confirmed")
    assert (then.row["derived_source_type"], then.row["derivation"]) == ("planned", "observed")
    now = world_as_of(query, NOV_1).items[trip_id]
    assert (now.standing.state, now.standing.reason) == ("current", "started")
    assert now.row["derived_source_type"] == "confirmed"

    # The dinner had no planned origin on 25 October, so its report made it current then.
    dinner_then = world_as_of(query, OCT_25).items[dinner_id]
    assert (dinner_then.standing.state, dinner_then.standing.reason) == ("current", "started")
    dinner_now = world_as_of(query, NOV_1).items[dinner_id]
    assert (dinner_now.standing.state, dinner_now.standing.reason) == ("reported", "reported_done")


def test_an_as_of_time_needs_its_offset(driver, seeded):
    with pytest.raises(ValueError):
        world_as_of(WorldQuery(driver, seeded["world_id"]), datetime(2026, 11, 1, 9, 0))
