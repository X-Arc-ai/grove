"""Regression tests for the controller's reconcile of the fourth delta verifier (R8).

V15: an export read five days after its episode ended took a line dated one second before the
read, so an old line read as fresh and escaped its staleness limit. A row is now dated inside the
window of the episode it cites, which the connector's code cut from the messages' own times.

The planned calendar row: a status change that cites a calendar entry dated next week took next
week as its world time, so an as-of read of now disagreed with the head until then. A scheduled
date is a field, never a clock (D4): an operation's world time is never later than its batch.

All names are invented.
"""

from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store.asof import item_as_of
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from tests.writer.helpers import (
    LEARNER,
    apply,
    create_item,
    created_id,
    make_source,
    new_op_id,
    observation,
    read,
)

NOW = datetime(2027, 4, 5, 9, 0, tzinfo=UTC)
RECHECK = "2027-05-01T09:00:00+00:00"
CHAT = [f"line {n}: Rowan writes about the Kestrel ferry timetable" for n in range(10)]


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(NOW)
    with clock.using(test_clock):
        yield test_clock


def refusals(driver, w: str, ops: list[dict], evidence: list[dict]) -> list[tuple[str, str]]:
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, ops, evidence, caller=LEARNER)
    return [(r.code, r.rule) for r in caught.value.refusals]


def item_row(driver, w: str, item_id: str) -> dict:
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def test_a_line_dated_after_its_episode_ended_is_refused(driver, seeded, frozen):
    """V15 with the verifier's numbers: the export is read at NOW, its episode ended five days
    before, and a line is dated one second before the read."""
    w = seeded["world_id"]
    chat = make_source(
        driver,
        w,
        "whatsapp",
        CHAT,
        episode_start=NOW - timedelta(days=6),
        episode_end=NOW - timedelta(days=5),
    )
    fresh_looking = observation(chat, "e1", 1, at=NOW - timedelta(seconds=1))
    op = create_item("organisation", "Kestrel ferries", "e1")
    assert refusals(driver, w, [op], [fresh_looking]) == [("invalid_arguments", "outside_episode")]
    # Dated inside the window, by the message's own time, the same line is accepted.
    honest = observation(chat, "e2", 1, at=NOW - timedelta(days=5, hours=2))
    created_id(apply(driver, w, [create_item("organisation", "Kestrel ferries", "e2")], [honest]))


def test_a_line_dated_before_its_episode_began_is_refused(driver, seeded, frozen):
    w = seeded["world_id"]
    chat = make_source(
        driver,
        w,
        "whatsapp",
        CHAT,
        episode_start=NOW - timedelta(days=6),
        episode_end=NOW - timedelta(days=5),
    )
    early = observation(chat, "e1", 1, at=NOW - timedelta(days=30))
    op = create_item("organisation", "Kestrel ferries", "e1")
    assert refusals(driver, w, [op], [early]) == [("invalid_arguments", "outside_episode")]


def test_a_calendar_entry_for_next_week_never_dates_a_status_change_ahead(driver, seeded, frozen):
    """The head and an as-of read of now agree: the status moved now, when the entry was read."""
    w = seeded["world_id"]
    chat = make_source(driver, w, "whatsapp", CHAT)
    calendar = make_source(driver, w, "calendar", ["Ferry timetable review, next Monday"])
    task = created_id(
        apply(
            driver,
            w,
            [
                create_item(
                    "task", "Review the Kestrel timetable", "t1", status="open", recheck_by=RECHECK
                )
            ],
            [observation(chat, "t1", 1, at=NOW - timedelta(hours=1))],
        )
    )
    entry = observation(
        calendar,
        "c1",
        1,
        at=NOW + timedelta(days=7),
        locator="whole",
        statement_class="intention",
    )
    change = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task,
        "status": "in progress",
        "evidence_refs": [{"ev_id": "c1", "polarity": "supports"}],
        "reason": "the review is on the calendar",
    }
    apply(driver, w, [change], [entry], caller=LEARNER)
    head = item_row(driver, w, task)
    assert head["status"] == "in progress"
    assert head["status_since"] == NOW
    as_of_now = item_as_of(WorldQuery(driver, w), task, NOW)
    assert as_of_now["status"] == head["status"]


def test_a_plan_from_a_calendar_entry_for_next_week_exists_now(driver, seeded, frozen):
    """A plan became true when it was made (D4): read now, it holds now, and its date is a field."""
    w = seeded["world_id"]
    calendar = make_source(driver, w, "calendar", ["Kestrel ferry to Velmora, next Monday"])
    entry = observation(
        calendar,
        "c1",
        1,
        at=NOW + timedelta(days=7),
        locator="whole",
        statement_class="intention",
    )
    plan = created_id(
        apply(
            driver,
            w,
            [
                create_item(
                    "plan",
                    "Take the Kestrel ferry to Velmora",
                    "c1",
                    status="proposed",
                    properties={"scheduled for": (NOW + timedelta(days=7)).isoformat()},
                )
            ],
            [entry],
            caller=LEARNER,
        )
    )
    held = item_as_of(WorldQuery(driver, w), plan, NOW)
    assert held is not None and held["status"] == "proposed"
    row = item_row(driver, w, plan)
    assert row["recheck_by"] == NOW + timedelta(days=8)
