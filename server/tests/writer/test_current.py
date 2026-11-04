"""D3 current(): the one function in code that says what is current (S43).

Spec, D3 "Reads return successors": "One function in code, `current()`, defined here, says what
is current: already started, not ended in any end class, not at a terminal status role of its
kind, and not overtaken by an active "corrected by" or "superseded by" successor. An unknown start
counts as started for most claims, but not for a bounded period or an event that a plan
scheduled: one whose scheduled start is still ahead, or whose occurrence no source has confirmed,
stays out of `current()` and is returned as planned, with its scheduled time (or as reported, once
a message reports that it began), and never as current (D4, D5)." D5 rule 3: "Nothing but new
evidence changes a claim's type or these states: not a clock, not a schedule".
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store import current as current_module
from grove.store.asof import item_as_of
from grove.store.current import (
    STATES,
    TERMINAL_ROLES,
    Successor,
    current_items,
    has_ended,
    link_standing,
    standing,
    standings,
)
from grove.store.primitives import MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from grove.writer import apply as apply_module
from grove.writer.testing import plant_link, plant_node
from grove.writer.tx import WorldTx, WorldWriter
from tests.writer import helpers
from tests.writer.helpers import (
    NoStore,
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

MAY = datetime(2027, 5, 4, 10, 0, tzinfo=UTC)
JUNE = datetime(2027, 6, 1, 9, 0, tzinfo=UTC)
JULY_1 = datetime(2027, 7, 1, 9, 0, tzinfo=UTC)
JULY_12 = datetime(2027, 7, 12, 0, 0, tzinfo=UTC)
LANDED = datetime(2027, 7, 12, 21, 40, tzinfo=UTC)
JULY_13 = datetime(2027, 7, 13, 9, 0, tzinfo=UTC)
JULY_20 = datetime(2027, 7, 20, 9, 0, tzinfo=UTC)
AUGUST = datetime(2027, 8, 2, 9, 0, tzinfo=UTC)
FLIGHT = "2027-07-12"
MOVED = "2027-07-19"
RECHECK = "2027-07-14T00:00:00+00:00"

# Every source here is read after the last time a test cites: an export or a fetch comes after
# the messages it holds, and a row may not speak for a time after its source's read (S51).
READ_LATE = datetime(2027, 9, 1, 9, 0, tzinfo=UTC)


def make_source(driver, world_id: str, connector_type: str, lines: list[str], **connector: Any):
    provider = clock._provider
    if not isinstance(provider, TestClock):
        return helpers.make_source(driver, world_id, connector_type, lines, **connector)
    before = provider.now()
    provider.set(READ_LATE)
    try:
        return helpers.make_source(driver, world_id, connector_type, lines, **connector)
    finally:
        provider.set(before)


@pytest.fixture
def chat(driver, seeded, frozen):
    """The shared chat fixture, read late like every source here, on the frozen clock."""
    return make_source(driver, seeded["world_id"], "whatsapp", helpers.CHAT_LINES)


CALENDAR_LINES = [
    "Flight to Velmora, Ilse Okonkwo, 12 July",
    "Okonkwo offsite dinner, 12 July",
]
PAGE_LINES = [
    "Booking 4471 confirmed: Ilse Okonkwo, flight to Velmora on 12 July",
    "Boarding pass scanned: Ilse Okonkwo, gate 9, Velmora flight, 12 July",
    "Okonkwo offsite dinner: the bill for table 12, paid",
]


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(MAY)
    with clock.using(test_clock):
        yield test_clock


def day(at: datetime) -> dict[str, str]:
    return {
        "earliest": iso(at),
        "latest": iso(at + timedelta(days=1, seconds=-1)),
        "precision": "day",
    }


def kind_ids(driver, world_id: str) -> dict[str, str]:
    rows = read(driver, world_id, "MATCH (k:Kind) RETURN k.id AS id, k.name AS name")
    return {row["name"]: row["id"] for row in rows}


def plant_scheduled(driver, world_id: str, kind_id: str, name: str, role: str = "start") -> None:
    """A property name that marks a scheduled time. The starter set holds none (truth-clock)."""
    plant_node(
        driver,
        world_id,
        "PropertyName",
        kind=kind_id,
        name=name,
        value_type="date",
        temporal_class="event",
        status="canonical",
        origin="minted",
        scheduled_role=role,
        version=1,
    )


def plant_successor(
    driver, world_id: str, old: str, new: str, meaning: str, *, active: bool = True
) -> str:
    """A link of a primitive meaning: its composite operations belong to writer-b."""
    columns: dict[str, Any] = {
        "meaning": meaning,
        "became_true_precision": "unknown",
        "recorded": MAY,
        "version": 1,
    }
    if active:
        columns["active_key"] = f"{world_id}|{old}|{meaning}|{new}"
    else:
        columns["end_class"] = "ended_in_the_world"
    return plant_link(driver, world_id, old, new, **columns)["id"]


def attach(target: str, ev_id: str) -> dict[str, Any]:
    return {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": target,
        "ev_id": ev_id,
        "polarity": "supports",
    }


def update(item: str, version: int, fields: dict[str, Any], ev_id: str) -> dict[str, Any]:
    return {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item,
        "version": version,
        "fields": fields,
        "evidence_refs": supports(ev_id),
        "reason": "the message states it",
    }


def item_row(driver, world_id: str, item_id: str) -> dict[str, Any]:
    [row] = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def entry(calendar: dict[str, str], ev_id: str, line: int) -> dict[str, Any]:
    return observation(calendar, ev_id, line, at=MAY, locator="whole", statement_class="intention")


def page(pages: dict[str, str], ev_id: str, line: int, at: datetime) -> dict[str, Any]:
    return observation(pages, ev_id, line, at=at, locator="whole", statement_class="report_state")


def read_at(test_clock: TestClock, at: datetime, driver, world_id: str, ids: list[str]) -> dict:
    test_clock.set(at)
    return standings(WorldQuery(driver, world_id), ids)


# The rule order, on rows alone -----------------------------------------------------------------

AT = JULY_13
PAST = {
    "became_true_earliest": JULY_1,
    "became_true_latest": JULY_1,
    "became_true_precision": "day",
}
FUTURE = {
    "became_true_earliest": AUGUST,
    "became_true_latest": AUGUST,
    "became_true_precision": "day",
}
UNKNOWN = {"became_true_precision": "unknown"}
SUCCESSOR = Successor("item-b", "link-ab", MEANING_CORRECTED_BY)


def evidence(source_type: str | None, derivation: str | None = "observed") -> dict[str, Any]:
    return {"derived_source_type": source_type, "derivation": derivation if source_type else None}


@pytest.mark.parametrize(
    ("row", "lifecycle", "extra", "state", "reason"),
    [
        # 1. ended, before anything else.
        ({"end_class": "spent"}, "record", {}, "ended", "end_class"),
        ({"end_class": "refuted", **PAST}, "lasting_thing", {}, "ended", "end_class"),
        ({"status_role": "cancelled", **PAST}, "work_with_state", {}, "ended", "terminal_status"),
        (
            {"stopped_being_true_precision": "before", "stopped_being_true_latest": JULY_1},
            "lasting_thing",
            {},
            "ended",
            "stop_clock",
        ),
        (
            {"end_class": "superseded"},
            "lasting_thing",
            {"successors": [SUCCESSOR]},
            "ended",
            "end_class",
        ),
        (
            {"status_role": "done", **evidence("planned")},
            "bounded_period",
            {},
            "ended",
            "terminal_status",
        ),
        # 2. overtaken, before the scheduled class.
        (
            {**UNKNOWN, **evidence("planned")},
            "bounded_period",
            {"successors": [SUCCESSOR]},
            "overtaken",
            "successor",
        ),
        ({**FUTURE}, "lasting_thing", {"successors": [SUCCESSOR]}, "overtaken", "successor"),
        # 3. the scheduled class: evidence first.
        ({**UNKNOWN, **evidence(None)}, "bounded_period", {}, "planned", "not_confirmed"),
        ({**UNKNOWN, **evidence("planned")}, "bounded_period", {}, "planned", "not_confirmed"),
        ({**PAST, **evidence("said")}, "bounded_period", {}, "planned", "not_confirmed"),
        (
            {**UNKNOWN, **evidence("reported_done")},
            "bounded_period",
            {},
            "reported",
            "reported_done",
        ),
        (
            {**PAST, **evidence("reported_done", "inferred")},
            "bounded_period",
            {},
            "planned",
            "inferred",
        ),
        ({**UNKNOWN, **evidence("confirmed")}, "bounded_period", {}, "planned", "start_unknown"),
        (
            {**PAST, **evidence("confirmed", "inferred")},
            "bounded_period",
            {},
            "planned",
            "inferred",
        ),
        ({**PAST, **evidence("confirmed")}, "bounded_period", {}, "current", "started"),
        (
            {**PAST, **evidence("confirmed", "rolled_up")},
            "bounded_period",
            {},
            "current",
            "started",
        ),
        # Evidence before the date: a planned period with a known future start is planned.
        ({**FUTURE, **evidence("planned")}, "bounded_period", {}, "planned", "not_confirmed"),
        ({**FUTURE, **evidence("confirmed")}, "bounded_period", {}, "not_started", "future_start"),
        (
            {**UNKNOWN, **evidence("planned")},
            "record",
            {"planned_origin": True},
            "planned",
            "not_confirmed",
        ),
        (
            {**UNKNOWN, **evidence("reported_done")},
            "record",
            {"planned_origin": True},
            "reported",
            "reported_done",
        ),
        (
            {**UNKNOWN, **evidence("confirmed", "inferred")},
            "record",
            {"planned_origin": True},
            "planned",
            "inferred",
        ),
        # A record needs no start: confirmed is enough.
        (
            {**UNKNOWN, **evidence("confirmed")},
            "record",
            {"planned_origin": True},
            "current",
            "started",
        ),
        (
            {**UNKNOWN, **evidence("said")},
            "record",
            {"scheduled": {"start": FLIGHT}},
            "planned",
            "not_confirmed",
        ),
        # A record with a scheduled start needs a known start, as a period does: a confirmed
        # booking does not prove the flight (spec 1078).
        (
            {**UNKNOWN, **evidence("confirmed")},
            "record",
            {"scheduled": {"start": FLIGHT}},
            "planned",
            "start_unknown",
        ),
        (
            {**UNKNOWN, **evidence("confirmed")},
            "record",
            {"planned_origin": True, "scheduled": {"start": FLIGHT}},
            "planned",
            "start_unknown",
        ),
        (
            {**PAST, **evidence("confirmed")},
            "record",
            {"scheduled": {"start": FLIGHT}},
            "current",
            "started",
        ),
        (
            {**FUTURE, **evidence("confirmed")},
            "record",
            {"scheduled": {"start": FLIGHT}},
            "not_started",
            "future_start",
        ),
        # Only the start role asks for a known start: a due date does not.
        (
            {**UNKNOWN, **evidence("confirmed")},
            "record",
            {"planned_origin": True, "scheduled": {"due": FLIGHT}},
            "current",
            "started",
        ),
        # Outside the scheduled class an unknown start counts as started.
        ({**UNKNOWN, **evidence("said")}, "record", {}, "current", "started"),
        (
            {**UNKNOWN, **evidence("said")},
            "record",
            {"scheduled": {"due": FLIGHT}},
            "current",
            "started",
        ),
        (
            {**UNKNOWN, **evidence("planned")},
            "lasting_thing",
            {"planned_origin": True},
            "current",
            "started",
        ),
        # A plan is work with a state: it became true when it was made.
        (
            {**PAST, **evidence("planned"), "status_role": "active"},
            "work_with_state",
            {"planned_origin": True},
            "current",
            "started",
        ),
        ({**PAST, "status_role": "reported_done"}, "work_with_state", {}, "current", "started"),
        # 4. not_started: a known start wholly in the future.
        ({**FUTURE, "status_role": "open"}, "work_with_state", {}, "not_started", "future_start"),
        (
            {"became_true_precision": "before", "became_true_latest": AUGUST},
            "lasting_thing",
            {},
            "current",
            "started",
        ),
        # 5. current.
        ({**UNKNOWN}, "lasting_thing", {}, "current", "started"),
        ({**UNKNOWN}, None, {}, "current", "started"),
    ],
)
def test_the_rule_order(row, lifecycle, extra, state, reason):
    found = standing(row, at=AT, lifecycle_class=lifecycle, **extra)
    assert (found.state, found.reason) == (state, reason)
    assert found.state in STATES
    assert found.is_current is (state == "current")
    assert found.successors == tuple(extra.get("successors", ()))
    assert found.scheduled == extra.get("scheduled", {})


@pytest.mark.parametrize(
    ("row", "lifecycle", "planned_origin"),
    [
        ({**UNKNOWN, **evidence("planned")}, "bounded_period", False),
        ({**UNKNOWN, **evidence("confirmed")}, "bounded_period", False),
        ({**UNKNOWN, **evidence("reported_done")}, "bounded_period", False),
        ({**PAST, **evidence("confirmed", "inferred")}, "bounded_period", False),
        ({**UNKNOWN, **evidence("reported_done")}, "record", True),
        ({**UNKNOWN, **evidence("confirmed")}, "record", False),
        ({**UNKNOWN, **evidence("confirmed")}, "record", True),
    ],
)
def test_no_date_moves_a_scheduled_state(row, lifecycle, planned_origin):
    read = {
        standing(
            row,
            at=at,
            lifecycle_class=lifecycle,
            planned_origin=planned_origin,
            scheduled={"start": FLIGHT},
        ).state
        for at in (JULY_1 - timedelta(days=60), JULY_1, JULY_13, JULY_20 + timedelta(days=400))
    }
    assert len(read) == 1 and read <= {"planned", "reported"}


def test_successors_and_the_scheduled_time_ride_on_the_state():
    found = standing(
        {"end_class": "superseded", "status_role": "cancelled"},
        at=AT,
        lifecycle_class="work_with_state",
        scheduled={"due": FLIGHT},
        successors=[SUCCESSOR],
    )
    assert found.state == "ended"
    assert found.successors == (SUCCESSOR,)
    assert found.scheduled == {"due": FLIGHT}
    assert (found.end_class, found.status_role) == ("superseded", "cancelled")


def test_a_standing_is_read_at_an_aware_time():
    with pytest.raises(ValueError):
        standing({}, at=datetime(2027, 7, 13), lifecycle_class="lasting_thing")
    with pytest.raises(ValueError):
        link_standing({"active_key": "k"}, at=datetime(2027, 7, 13))


def test_has_ended_is_rule_one():
    assert TERMINAL_ROLES == {"done", "cancelled", "ended"}
    for role in TERMINAL_ROLES:
        assert has_ended({"status_role": role})
    assert has_ended({"end_class": "spent"})
    assert has_ended({"stopped_being_true_precision": "unknown"})
    assert not has_ended({"status_role": "reported_done", "end_class": None})
    assert not has_ended({"stopped_being_true_precision": None})


def test_the_writers_guard_asks_has_ended(monkeypatch):
    # Spec line 2758: one current() function "used by every read and every guard".
    assert apply_module.has_ended is current_module.has_ended
    seen = []
    monkeypatch.setattr(apply_module, "has_ended", lambda row: seen.append(row) or "asked")
    assert apply_module._ended({"id": "item-a"}) == "asked"
    assert seen == [{"id": "item-a"}]


@pytest.mark.parametrize(
    ("row", "state", "reason"),
    [
        ({"active_key": "k", **PAST}, "current", "started"),
        ({"active_key": "k", **FUTURE}, "not_started", "future_start"),
        ({"active_key": "k", **UNKNOWN}, "current", "started"),
        (
            {"end_class": "ended_in_the_world", "stopped_being_true_earliest": JULY_1},
            "ended",
            "end_class",
        ),
        # A slot link ended at its successor's future start still stood until then.
        (
            {"end_class": "superseded", "stopped_being_true_earliest": AUGUST, **PAST},
            "current",
            "started",
        ),
        (
            {"end_class": "superseded", "stopped_being_true_earliest": AUGUST, **FUTURE},
            "not_started",
            "future_start",
        ),
        # A refuted link was never true.
        (
            {"end_class": "refuted", "stopped_being_true_earliest": AUGUST, **PAST},
            "ended",
            "end_class",
        ),
        ({"end_class": "superseded", "stopped_being_true_latest": AUGUST}, "ended", "end_class"),
        ({"active_key": None, **PAST}, "ended", "inactive"),
        ({"stopped_being_true_earliest": iso(AUGUST), **PAST}, "current", "started"),
    ],
)
def test_a_links_standing(row, state, reason):
    found = link_standing(row, at=AT)
    assert (found.state, found.reason) == (state, reason)
    assert found.successors == () and found.scheduled == {}


# The read path, on the live store --------------------------------------------------------------


def test_the_trip_reads_planned_until_a_confirmation_brings_its_start(driver, seeded, chat, frozen):
    # D5, spec 1078, and the regression test of spec 1183, with invented names.
    w = seeded["world_id"]
    kinds = kind_ids(driver, w)
    plant_scheduled(driver, w, kinds["trip"], "departs on")
    plant_scheduled(driver, w, kinds["plan"], "travel date")
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)

    # In May a booking confirmation arrives, and the calendar holds the flight on 12 July.
    booking = create_item(
        "event", "Ilse booked the Velmora flight", "b1", became_true=clock_at(MAY)
    )
    plan = create_item(
        "plan",
        "Ilse travels to Velmora",
        "c1",
        status="active",
        became_true=clock_at(MAY),
        properties={"travel date": FLIGHT},
        recheck_by=RECHECK,
    )
    trip = create_item("trip", "Ilse's trip to Velmora", "c1", properties={"departs on": FLIGHT})
    receipt = apply(
        driver, w, [booking, plan, trip], [page(pages, "b1", 1, MAY), entry(calendar, "c1", 1)]
    )
    booking_id, plan_id, trip_id = (created_id(receipt, nth=n) for n in range(3))
    ids = [booking_id, plan_id, trip_id]

    # Before 12 July and on 13 July, with no further evidence: planned, with its scheduled date.
    for at in (JULY_1, JULY_13):
        found = read_at(frozen, at, driver, w, ids)
        assert (found[trip_id].state, found[trip_id].reason) == ("planned", "not_confirmed")
        assert found[trip_id].scheduled == {"start": FLIGHT}
        assert found[plan_id].state == "current"
        assert found[plan_id].scheduled == {"start": FLIGHT}
        assert found[booking_id].state == "current"
        assert current_items(WorldQuery(driver, w), ids) == [booking_id, plan_id]

    # "landed" makes the trip reported done.
    landed = observation(chat, "l1", 7, at=LANDED, statement_class="report_happened")
    frozen.set(JULY_13)
    apply(driver, w, [attach(trip_id, "l1")], [landed])
    found = read_at(frozen, JULY_13, driver, w, ids)
    assert (found[trip_id].state, found[trip_id].reason) == ("reported", "reported_done")
    assert read_at(frozen, JULY_20, driver, w, ids)[trip_id].state == "reported"

    # The boarding pass confirms it: the trip gets its start, and the plan moves to fulfilled.
    frozen.set(JULY_13)
    version = item_row(driver, w, trip_id)["version"]
    start = update(trip_id, version, {"became_true": day(JULY_12), "recheck_by": RECHECK}, "p2")
    fulfilled = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": plan_id,
        "status": "fulfilled",
        "evidence_refs": supports("p2"),
        "reason": "the boarding pass shows the travel took place",
    }
    apply(driver, w, [start, fulfilled], [page(pages, "p2", 2, JULY_13)])
    found = read_at(frozen, JULY_13, driver, w, ids)
    assert item_row(driver, w, trip_id)["derived_source_type"] == "confirmed"
    assert (found[trip_id].state, found[trip_id].reason) == ("current", "started")
    assert found[trip_id].scheduled == {"start": FLIGHT}
    assert (found[plan_id].state, found[plan_id].reason) == ("ended", "terminal_status")
    assert found[booking_id].state == "current"
    assert current_items(WorldQuery(driver, w), ids) == [booking_id, trip_id]


def test_a_confirmed_row_on_a_trip_with_an_unknown_start_stays_planned(driver, seeded, frozen):
    w = seeded["world_id"]
    kinds = kind_ids(driver, w)
    plant_scheduled(driver, w, kinds["trip"], "departs on")
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    trip = create_item("trip", "Ilse's trip to Velmora", "c1", properties={"departs on": FLIGHT})
    trip_id = created_id(apply(driver, w, [trip], [entry(calendar, "c1", 1)]))
    frozen.set(JULY_13)
    apply(driver, w, [attach(trip_id, "p2")], [page(pages, "p2", 2, JULY_13)])

    row = item_row(driver, w, trip_id)
    assert row["derived_source_type"] == "confirmed" and row["derivation"] == "observed"
    assert row["became_true_precision"] == "unknown"
    for at in (JULY_1, JULY_13, JULY_20, AUGUST):
        found = read_at(frozen, at, driver, w, [trip_id])[trip_id]
        assert (found.state, found.reason) == ("planned", "start_unknown")
        assert found.scheduled == {"start": FLIGHT}


def test_a_booked_flight_reads_planned_until_a_confirmation_brings_its_start(
    driver, seeded, frozen
):
    # Spec 832 ("one whose scheduled start is still ahead ... stays out of current()") and the
    # problem statement of spec 1187: "A booked flight became 'confirmed travel' because its date
    # passed." The booking page confirms the booking, not the flight (spec 1078).
    w = seeded["world_id"]
    kinds = kind_ids(driver, w)
    plant_scheduled(driver, w, kinds["event"], "takes place on")
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    from_calendar = create_item(
        "event", "Flight to Velmora", "c1", properties={"takes place on": FLIGHT}
    )
    from_page = create_item(
        "event", "Ilse Okonkwo on the Velmora flight", "b1", properties={"takes place on": FLIGHT}
    )
    receipt = apply(
        driver,
        w,
        [from_calendar, from_page],
        [entry(calendar, "c1", 1), page(pages, "b1", 1, MAY)],
    )
    flight, booked = created_id(receipt, nth=0), created_id(receipt, nth=1)
    apply(driver, w, [attach(flight, "b2")], [page(pages, "b2", 1, MAY)])
    for item_id in (flight, booked):
        row = item_row(driver, w, item_id)
        assert (row["derived_source_type"], row["derivation"]) == ("confirmed", "observed")
        assert row["became_true_precision"] == "unknown"

    for at in (JUNE, JULY_1, JULY_13, AUGUST):
        found = read_at(frozen, at, driver, w, [flight, booked])
        for item_id in (flight, booked):
            assert (found[item_id].state, found[item_id].reason) == ("planned", "start_unknown")
            assert found[item_id].scheduled == {"start": FLIGHT}
        assert current_items(WorldQuery(driver, w), [flight, booked]) == []

    # A record's claim never changes, so the boarding pass records the flight with its known
    # start: that record is current.
    frozen.set(JULY_13)
    boarded = create_item(
        "event",
        "Ilse Okonkwo boarded the Velmora flight",
        "p2",
        became_true=day(JULY_12),
        properties={"takes place on": FLIGHT},
    )
    boarded_id = created_id(apply(driver, w, [boarded], [page(pages, "p2", 2, JULY_13)]))
    found = read_at(frozen, JULY_13, driver, w, [boarded_id])[boarded_id]
    assert (found.state, found.scheduled) == ("current", {"start": FLIGHT})


def test_a_moved_date_moves_what_a_read_says_and_never_the_state(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    kinds = kind_ids(driver, w)
    plant_scheduled(driver, w, kinds["trip"], "departs on")
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    trip = create_item("trip", "Ilse's trip to Velmora", "c1", properties={"departs on": FLIGHT})
    trip_id = created_id(apply(driver, w, [trip], [entry(calendar, "c1", 1)]))
    frozen.set(JUNE)
    # "We fly on the 19th now": an intention, so the row is said and proves no occurrence.
    moved = observation(chat, "m1", 3, at=JUNE, statement_class="intention")
    version = item_row(driver, w, trip_id)["version"]
    apply(
        driver, w, [update(trip_id, version, {"properties": {"departs on": MOVED}}, "m1")], [moved]
    )

    assert item_row(driver, w, trip_id)["derived_source_type"] == "said"
    for at in (JULY_13, JULY_20, AUGUST):
        found = read_at(frozen, at, driver, w, [trip_id])[trip_id]
        assert (found.state, found.scheduled) == ("planned", {"start": MOVED})


def test_an_event_a_plan_scheduled_reads_reported_and_never_current(driver, seeded, chat, frozen):
    # Spec 832: "returned as planned ... (or as reported, once a message reports that it began),
    # and never as current".
    w = seeded["world_id"]
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    dinner = create_item("event", "Okonkwo offsite dinner", "c2")
    said = create_item("event", "Rowan hosted the Okonkwo board lunch", "s1")
    receipt = apply(
        driver,
        w,
        [dinner, said],
        [
            entry(calendar, "c2", 2),
            observation(chat, "s1", 1, at=MAY, statement_class="report_happened"),
        ],
    )
    dinner_id, said_id = created_id(receipt, nth=0), created_id(receipt, nth=1)
    for at in (JULY_1, JULY_13):
        found = read_at(frozen, at, driver, w, [dinner_id, said_id])
        assert (found[dinner_id].state, found[dinner_id].reason) == ("planned", "not_confirmed")
        # A record that no plan scheduled reads current on a report.
        assert found[said_id].state == "current"

    frozen.set(JULY_13)
    for ev_id, line in (("r1", 9), ("r2", 10)):
        report = observation(chat, ev_id, line, at=LANDED, statement_class="report_happened")
        apply(driver, w, [attach(dinner_id, ev_id)], [report])
        for at in (JULY_13, JULY_20, AUGUST):
            found = read_at(frozen, at, driver, w, [dinner_id])[dinner_id]
            assert (found.state, found.reason) == ("reported", "reported_done")
    assert item_row(driver, w, dinner_id)["derived_source_type"] == "reported_done"

    frozen.set(JULY_13)
    apply(driver, w, [attach(dinner_id, "p3")], [page(pages, "p3", 3, JULY_13)])
    assert read_at(frozen, JULY_13, driver, w, [dinner_id])[dinner_id].state == "current"


def test_an_inferred_row_never_confirms(driver, seeded, chat, frozen):
    # D5 rule 1: a claim inferred from confirmed rows reads "inferred from", "never 'confirmed'".
    w = seeded["world_id"]
    pages = make_source(driver, w, "web_pages", PAGE_LINES)
    booking = create_item(
        "event", "Ilse booked the Velmora flight", "b1", became_true=clock_at(MAY)
    )
    booking_id = created_id(apply(driver, w, [booking], [page(pages, "b1", 1, MAY)]))
    [premise] = read(
        driver, w, "MATCH (e:Evidence {target_id: $id}) RETURN e.id AS id", id=booking_id
    )
    frozen.set(JULY_13)
    inferred = observation(
        chat, "i1", 4, at=JULY_13, derivation="inferred", premises=[premise["id"]]
    )
    # D5 rule 2 (S55): an inferred row cannot give the trip its start, so that write is refused.
    started = create_item(
        "trip", "Ilse's trip to Velmora", "i1", became_true=day(JULY_12), recheck_by=RECHECK
    )
    with pytest.raises(BatchDeclined) as refused:
        apply(driver, w, [started], [inferred])
    assert [(r.code, r.rule) for r in refused.value.refusals] == [
        ("terminal_needs_confirmation", "period_start_needs_confirmation")
    ]
    trip = create_item("trip", "Ilse's trip to Velmora", "i1")
    trip_id = created_id(apply(driver, w, [trip], [inferred]))
    row = item_row(driver, w, trip_id)
    assert (row["derived_source_type"], row["derivation"]) == ("confirmed", "inferred")
    for at in (JULY_13, AUGUST):
        found = read_at(frozen, at, driver, w, [trip_id])[trip_id]
        assert (found.state, found.reason) == ("planned", "inferred")

    # An observed page that shows the trip took place gives it its start, in its own batch.
    start = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": trip_id,
        "version": item_row(driver, w, trip_id)["version"],
        "fields": {"became_true": day(JULY_12), "recheck_by": RECHECK},
        "evidence_refs": [{"ev_id": "p2", "polarity": "supports"}],
        "reason": "the page shows the trip took place",
    }
    apply(driver, w, [start], [page(pages, "p2", 2, JULY_13)])
    assert read_at(frozen, JULY_13, driver, w, [trip_id])[trip_id].state == "current"


def test_an_active_successor_overtakes_and_rides_on_every_state(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    ops = [
        create_item("decision", "Ilse chose the cheaper venue", "d1"),
        create_item("decision", "Ilse chose the Okonkwo hall", "d1"),
        create_item("place", "Okonkwo hall", "d1"),
        create_item("place", "Okonkwo hall annex", "d1"),
        create_item("decision", "Tamsin chose the river room", "d1"),
        create_item("decision", "Tamsin chose the garden room", "d1"),
    ]
    told = observation(chat, "d1", 2, at=MAY, statement_class="decision")
    receipt = apply(driver, w, ops, [told])
    old, new, hall, annex, river, garden = (created_id(receipt, nth=n) for n in range(6))
    frozen.set(JUNE)
    ended = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": hall,
        "end_class": "superseded",
        "stopped_being_true": clock_at(JUNE),
        "evidence_refs": supports("x1"),
        "reason": "the annex replaced the hall",
    }
    apply(driver, w, [ended], [observation(chat, "x1", 3, at=JUNE, statement_class="report_state")])
    corrected = plant_successor(driver, w, old, new, MEANING_CORRECTED_BY)
    superseded = plant_successor(driver, w, hall, annex, MEANING_SUPERSEDED_BY)
    plant_successor(driver, w, river, garden, MEANING_CORRECTED_BY, active=False)

    query = WorldQuery(driver, w)
    found = standings(query, [old, new, hall, annex, river])
    assert (found[old].state, found[old].reason) == ("overtaken", "successor")
    assert found[old].successors == (Successor(new, corrected, MEANING_CORRECTED_BY),)
    assert (found[hall].state, found[hall].end_class) == ("ended", "superseded")
    assert found[hall].successors == (Successor(annex, superseded, MEANING_SUPERSEDED_BY),)
    # An ended successor link overtakes nothing.
    assert (found[river].state, found[river].successors) == ("current", ())
    assert (found[new].state, found[new].successors) == ("current", ())
    assert current_items(query, [old, new, hall, annex, river]) == [new, annex, river]
    # An overtaken item read alone still finds its successor: the read follows the link from the
    # old item, not back from the new one.
    alone = standings(query, [old])[old]
    assert (alone.state, alone.successors) == (
        "overtaken",
        (Successor(new, corrected, MEANING_CORRECTED_BY),),
    )
    alone = standings(query, [hall])[hall]
    assert (alone.state, alone.successors) == (
        "ended",
        (Successor(annex, superseded, MEANING_SUPERSEDED_BY),),
    )
    assert standings(query, [new])[new].successors == ()


def test_standings_inside_a_transaction_read_its_own_writes(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    ops = [
        create_item("decision", "Ilse chose the cheaper venue", "d1"),
        create_item("decision", "Ilse chose the Okonkwo hall", "d1"),
        create_item("place", "Okonkwo hall", "d1"),
    ]
    receipt = apply(
        driver, w, ops, [observation(chat, "d1", 2, at=MAY, statement_class="decision")]
    )
    old, new, hall = (created_id(receipt, nth=n) for n in range(3))
    ids = [old, new, hall]

    class RolledBack(Exception):
        pass

    seen: dict[str, Any] = {}

    def work(tx: WorldTx) -> None:
        tx.run(
            "MATCH (a:Item {id: $a}) MATCH (b:Item {id: $b}) "
            "CREATE (a)-[l:LINK]->(b) SET l += $row",
            a=old,
            b=new,
            row={
                "id": f"link-{old[-8:]}",
                "from_item": old,
                "to_item": new,
                "meaning": MEANING_CORRECTED_BY,
                "active_key": f"{w}|{old}|{MEANING_CORRECTED_BY}|{new}",
                "became_true_precision": "unknown",
            },
        )
        tx.run("MATCH (i:Item {id: $id}) SET i.end_class = 'retired'", id=hall)
        seen["inside"] = standings(tx, ids)
        seen["current"] = current_items(tx, ids)
        seen["outside"] = standings(WorldQuery(driver, w), ids)
        raise RolledBack

    with pytest.raises(RolledBack):
        WorldWriter(driver, w).transaction(work)
    assert seen["inside"][old].state == "overtaken"
    assert seen["inside"][hall].state == "ended"
    assert seen["current"] == [new]
    assert {found.state for found in seen["outside"].values()} == {"current"}
    assert current_items(WorldQuery(driver, w), ids) == ids


def test_a_guard_and_a_read_agree_on_ended(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    ops = [
        create_item("trip", "Ilse's trip to Velmora", "c1"),
        create_item("plan", "Ilse travels to Velmora", "c1", status="dropped"),
        create_item("task", "Book the Velmora hotel", "c1", status="open", recheck_by=RECHECK),
    ]
    receipt = apply(driver, w, ops, [entry(calendar, "c1", 1)])
    trip, plan, task = (created_id(receipt, nth=n) for n in range(3))
    found = standings(WorldQuery(driver, w), [trip, plan])
    assert found[trip].state == "planned"
    assert (found[plan].state, found[plan].reason) == ("ended", "terminal_status")

    said = observation(chat, "k1", 5, at=MAY, statement_class="report_state")
    blocks_trip = create_link(task, "blocks", trip, "k1", recheck_by=RECHECK)
    apply(driver, w, [blocks_trip], [said])
    blocks_plan = create_link(task, "blocks", plan, "k2", recheck_by=RECHECK)
    with pytest.raises(BatchDeclined) as declined:
        apply(driver, w, [blocks_plan], [observation(chat, "k2", 6, at=MAY)])
    assert [refusal.code for refusal in declined.value.refusals] == ["ended_target"]


def test_a_future_start_reads_not_started_and_then_current(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    opens = observation(chat, "o1", 8, at=MAY, statement_class="report_state")
    studio = create_item("organisation", "Ilse and Tamsin's studio", "o1", became_true=day(AUGUST))
    studio_id = created_id(apply(driver, w, [studio], [opens]))
    found = read_at(frozen, JULY_13, driver, w, [studio_id])[studio_id]
    assert (found.state, found.reason) == ("not_started", "future_start")
    assert read_at(frozen, AUGUST + timedelta(days=1), driver, w, [studio_id])[studio_id].is_current


def test_a_row_from_item_as_of_reads_as_it_stood(driver, seeded, chat, frozen):
    w = seeded["world_id"]
    opened = observation(chat, "o1", 11, at=MAY, statement_class="report_state")
    shop = create_item("organisation", "Rowan's print shop", "o1", became_true=day(JUNE))
    shop_id = created_id(apply(driver, w, [shop], [opened]))
    frozen.set(JULY_20)
    closed = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": shop_id,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(JULY_20),
        "evidence_refs": supports("o2"),
        "reason": "the shop closed",
    }
    apply(driver, w, [closed], [observation(chat, "o2", 12, at=JULY_20)])
    query = WorldQuery(driver, w)
    assert standings(query, [shop_id])[shop_id].state == "ended"

    def as_of(at: datetime) -> str:
        return standing(
            item_as_of(query, shop_id, at), at=at, lifecycle_class="lasting_thing"
        ).state

    assert as_of(JULY_13) == "current"
    # The as-of row holds its start as ISO text, after the time read.
    assert as_of(MAY + timedelta(days=1)) == "not_started"
    assert as_of(JULY_20 + timedelta(days=1)) == "ended"


def test_no_ids_read_nothing_and_unknown_ids_are_left_out(driver, seeded, chat, frozen):
    assert standings(NoStore(), []) == {}
    assert current_items(NoStore(), []) == []
    with pytest.raises(TypeError):
        standings(NoStore(), "an item id")
    w = seeded["world_id"]
    rowan = create_item("person", "Rowan Okonkwo", "p1")
    rowan_id = created_id(apply(driver, w, [rowan], [observation(chat, "p1", 1, at=MAY)]))
    found = standings(WorldQuery(driver, w), ["no-such-item", rowan_id, rowan_id])
    assert list(found) == [rowan_id]
    assert current_items(WorldQuery(driver, w), [rowan_id, "no-such-item", rowan_id]) == [rowan_id]
