"""The Singapore regression test (D5, spec 1183), through the real writer, with invented names.

Spec 1183: "A booking confirmation in May and a calendar entry for 12 July. On 13 July, with no
further evidence, 'Did I go to Singapore?' returns 'planned for 12 July, booked on <date>; nothing
confirms the trip happened'. With a boarding pass ingested, it returns 'yes, confirmed against the
boarding pass'." Spec 1078 walks the same trip: the booking is a record confirmed by the artifact,
the plan carries 12 July as its scheduled date, the trip has no became-true time, "On 13 July the
plan's horizon lapses and the lane offers it; the date itself changes nothing", "landed" makes the
trip reported done, and the boarding pass gives the trip its start and moves the plan to fulfilled.
D5 rule 3: "Nothing but new evidence changes a claim's type or these states: not a clock".

The destination is the invented word Velmora, every person is invented, and every time comes from
a TestClock. A source is named only by its connector type in words.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store.current import standings
from grove.store.query import WorldQuery
from grove.store.words import NOTHING_CONFIRMS, clock_words, occurrence
from grove.writer import record_connector, record_episode, record_source
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

MAY = datetime(2027, 5, 4, 10, 0, tzinfo=UTC)
BOOKED = MAY - timedelta(minutes=5)
JUNE = datetime(2027, 6, 1, 9, 0, tzinfo=UTC)
JULY_1 = datetime(2027, 7, 1, 9, 0, tzinfo=UTC)
BOARDED = datetime(2027, 7, 12, 6, 30, tzinfo=UTC)
LANDED = datetime(2027, 7, 12, 21, 40, tzinfo=UTC)
JULY_13 = datetime(2027, 7, 13, 9, 0, tzinfo=UTC)
JULY_20 = datetime(2027, 7, 20, 9, 0, tzinfo=UTC)
# The calendar entry: the plan is scheduled for 12 July at 08:00, so its horizon is 13 July 08:00
# (Appendix C: the scheduled time plus one day). The trip starts on the bare day 12 July.
FLIGHT_AT = "2027-07-12T08:00:00+00:00"
PLAN_RECHECK = datetime(2027, 7, 13, 8, 0, tzinfo=UTC)
FLIGHT_DAY = "2027-07-12"
BOOKING_TITLE = "Velmora flight booking 4471"
TRIP_TITLE = "Ilse's trip to Velmora"

BOOKING_LINES = ["Booking 4471 confirmed: Ilse Okonkwo, flight to Velmora on 12 July, seat 14C"]
BOOKING_KEY = "mk-carrier-4471"
CALENDAR_LINES = ["Flight to Velmora, Ilse Okonkwo, 12 July 08:00"]
CHAT_LINES = ["landed", "an invented line about the Okonkwo hotel"]
PASS_LINES = ["Boarding pass: Ilse Okonkwo, gate 9, Velmora flight, 12 July 08:00, scanned"]


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(MAY)
    with clock.using(test_clock):
        yield test_clock


def automated_source(driver, w: str, lines: list[str]) -> dict[str, str]:
    """A notice that arrived in a chat, whose own metadata names the automated sender: the code
    that parsed it set the speaker, so the writer types the row by it (D5, S46)."""
    connector_id = record_connector(driver, w, "whatsapp")
    source_id = record_source(
        driver,
        w,
        connector_id=connector_id,
        external_locator="carrier-notice-4471.txt",
        content="\n".join(lines),
        parser_version="test-1",
        speakers={BOOKING_KEY: {"kind": "automated", "sender": "carrier-notice"}},
    )
    episode_id = record_episode(
        driver,
        w,
        connector_id=connector_id,
        source_ids=[source_id],
        first_message_key=BOOKING_KEY,
        last_message_key=BOOKING_KEY,
        start_time=BOOKED,
        end_time=BOOKED,
        cut_rule="chat_window_90m",
    )
    return {"connector_id": connector_id, "source_id": source_id, "episode_id": episode_id}


def item_row(driver, w: str, item_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def held(driver, w: str, ids: list[str]) -> dict[str, tuple[str, str, Any]]:
    """Each item's state, its reason and its derived source type, read at the clock's now."""
    found = standings(WorldQuery(driver, w), ids)
    return {
        item_id: (found[item_id].state, found[item_id].reason, row["derived_source_type"])
        for item_id in ids
        if (row := item_row(driver, w, item_id))
    }


def answer(driver, w: str, item_id: str) -> dict[str, Any]:
    return occurrence(WorldQuery(driver, w), item_id)


def utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC)


def test_the_booked_trip_is_planned_until_a_boarding_pass_confirms_it(driver, seeded, frozen):
    w = seeded["world_id"]

    # May: a booking confirmation from the carrier's automated sender, and the calendar entry.
    booking_src = automated_source(driver, w, BOOKING_LINES)
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    notice = observation(
        booking_src,
        "b1",
        1,
        at=BOOKED,
        statement_class="report_happened",
        message_key=BOOKING_KEY,
    )
    entry = observation(
        calendar, "c1", 1, at=MAY, locator="whole", statement_class="intention", message_key="cal-1"
    )
    ops = [
        create_item("event", BOOKING_TITLE, "b1", temp_id="$booking", became_true=clock_at(BOOKED)),
        create_item(
            "plan",
            "Ilse travels to Velmora",
            "c1",
            temp_id="$plan",
            status="active",
            became_true=clock_at(MAY),
            properties={"scheduled for": FLIGHT_AT},
        ),
        create_item("trip", TRIP_TITLE, "c1", temp_id="$trip", properties={"starts": FLIGHT_DAY}),
        create_link("$booking", "part of", "$plan", "b1"),
        create_link("$trip", "part of", "$plan", "c1"),
    ]
    receipt = apply(driver, w, ops, [notice, entry])
    booking, plan, trip = (created_id(receipt, nth=n) for n in range(3))
    ids = [booking, plan, trip]

    booking_row, plan_row, trip_row = (item_row(driver, w, item_id) for item_id in ids)
    assert booking_row["derived_source_type"] == "confirmed"
    assert plan_row["derived_source_type"] == trip_row["derived_source_type"] == "planned"
    assert trip_row["became_true_precision"] == "unknown"
    # The writer set the plan's horizon from its scheduled time: 12 July 08:00 plus one day.
    assert utc(plan_row["recheck_by"]) == PLAN_RECHECK

    # Before the date: planned, naming the booking with its May date.
    frozen.set(JULY_1)
    before = held(driver, w, ids)
    assert before[trip] == ("planned", "not_confirmed", "planned")
    assert before[plan] == ("current", "started", "planned")
    assert before[booking] == ("current", "started", "confirmed")
    planned_words = f"planned for 12 Jul; {BOOKING_TITLE}: confirmed 4 May; {NOTHING_CONFIRMS}"
    early = answer(driver, w, trip)
    assert (early["state"], early["words"]) == ("planned", planned_words)
    assert early["scheduled"]["words"] == "12 Jul"
    assert early["scheduled"]["from"] == trip
    assert early["confirmed_by"] is None
    trip_words_before = clock_words(WorldQuery(driver, w), trip)
    plan_words_before = clock_words(WorldQuery(driver, w), plan)

    # 13 July, with no further evidence: the date passed and changed nothing.
    frozen.set(JULY_13)
    assert held(driver, w, ids) == before
    late = answer(driver, w, trip)
    assert late["state"] == "planned"
    assert late["words"] == planned_words
    assert late["confirmed_by"] is None
    assert "yes" not in late["words"]
    # The plan is due: its horizon lapsed, and a read flags it with both dates.
    assert utc(item_row(driver, w, plan)["recheck_by"]) < clock.now(w)
    plan_words = clock_words(WorldQuery(driver, w), plan)
    assert plan_words["re-check by"] == "was due 13 Jul"
    assert plan_words["last confirmed"] == (
        "never confirmed, from the calendar of 4 May; re-check was due 13 Jul"
    )
    planned_plan = answer(driver, w, plan)
    assert (planned_plan["state"], planned_plan["words"]) == ("current", planned_words)
    assert planned_plan["scheduled"]["from"] == plan
    # The source types and the states read the same as before the clock moved.
    trip_words = clock_words(WorldQuery(driver, w), trip)
    for key in ("source type", "state", "became true"):
        assert trip_words[key] == trip_words_before[key]
        assert plan_words[key] == plan_words_before[key]
    assert trip_words["state"] == "planned"
    assert trip_words["source type"] == "planned (planned in the calendar)"
    # A booking proves the booking, not the trip.
    booked = answer(driver, w, booking)
    assert booked["words"] == "yes, confirmed against a chat message of 4 May"

    # "landed", in a chat: reported, not confirmed.
    chat = make_source(driver, w, "whatsapp", CHAT_LINES)
    landed = observation(chat, "l1", 1, at=LANDED, statement_class="report_happened")
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": trip,
        "ev_id": "l1",
        "polarity": "supports",
    }
    apply(driver, w, [attach], [landed])
    reported = answer(driver, w, trip)
    assert reported["state"] == "reported"
    assert reported["words"] == "reported, not confirmed: a chat message of 12 Jul"
    assert reported["confirmed_by"] is None
    assert held(driver, w, [trip])[trip] == ("reported", "reported_done", "reported_done")
    frozen.set(JULY_20)
    assert answer(driver, w, trip)["words"] == reported["words"]
    assert answer(driver, w, plan)["state"] == "current"

    # The boarding pass, ingested a week later: a screenshot of the artifact gives the trip its
    # start and the plan fulfilled, in one batch. The clock only moves forward.
    screenshots = make_source(driver, w, "screenshots", PASS_LINES)
    boarding = observation(
        screenshots,
        "p1",
        1,
        at=BOARDED,
        locator="whole",
        statement_class="artifact",
        modality="image",
        message_key="shot-1",
    )
    start = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": trip,
        "version": item_row(driver, w, trip)["version"],
        "fields": {
            "became_true": {
                "earliest": iso(datetime(2027, 7, 12, tzinfo=UTC)),
                "latest": iso(datetime(2027, 7, 12, 23, 59, 59, tzinfo=UTC)),
                "precision": "day",
            }
        },
        "evidence_refs": supports("p1"),
        "reason": "the boarding pass shows the trip began on 12 July",
    }
    fulfilled = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": plan,
        "status": "fulfilled",
        "evidence_refs": supports("p1"),
        "reason": "the boarding pass shows the travel took place",
    }
    apply(driver, w, [start, fulfilled], [boarding])

    after = held(driver, w, ids)
    assert after[trip] == ("current", "started", "confirmed")
    assert after[plan][:2] == ("ended", "terminal_status")
    went = answer(driver, w, trip)
    assert went["state"] == "current"
    assert went["words"] == "yes, confirmed against a screenshot of 12 Jul"
    assert went["confirmed_by"]["source"] == "a screenshot"
    assert utc(went["confirmed_by"]["at"]) == BOARDED
    assert clock_words(WorldQuery(driver, w), trip)["became true"] == "12 Jul"
    done = answer(driver, w, plan)
    assert (done["state"], done["words"]) == (
        "ended",
        "yes, confirmed against a screenshot of 12 Jul",
    )


def test_no_date_alone_ever_says_it_happened(driver, seeded, frozen):
    # D5 rule 3 with a calendar entry and nothing else: every later day reads planned.
    w = seeded["world_id"]
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    entry = observation(
        calendar, "c1", 1, at=MAY, locator="whole", statement_class="intention", message_key="cal-1"
    )
    trip = created_id(
        apply(
            driver,
            w,
            [create_item("trip", TRIP_TITLE, "c1", properties={"starts": FLIGHT_DAY})],
            [entry],
        )
    )
    for at in (JUNE, JULY_1, JULY_13, JULY_20):
        frozen.set(at)
        found = answer(driver, w, trip)
        assert found["state"] == "planned"
        assert found["words"] == f"planned for 12 Jul; {NOTHING_CONFIRMS}"
        assert found["confirmed_by"] is None


def test_an_id_that_is_no_item_raises(driver, seeded, frozen):
    with pytest.raises(LookupError):
        answer(driver, seeded["world_id"], "no-such-item")
