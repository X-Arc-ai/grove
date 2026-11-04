"""Calendar confirmation requires connector-owned facts, not an extracted date or timezone."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.clock import TestClock
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined, provenance, record_connector, record_episode
from grove.writer.calendar_facts import MAX_CALENDAR_EVENTS, calendar_facts_json
from grove.writer.errors import InvalidRecord
from grove.writer.sources import add_screened_source
from grove.writer.tx import WorldWriter
from tests.writer.helpers import LEARNER, apply, create_item, created_id, observation

NOW = datetime(2027, 3, 1, 9, tzinfo=UTC)
PAST = datetime(2027, 2, 25, 9, tzinfo=UTC)
FUTURE = datetime(2027, 3, 10, 9, tzinfo=UTC)
BOUNDARY = datetime(2027, 2, 19, 21, tzinfo=UTC)
KEY = "calendar-event"


@pytest.fixture
def frozen():
    timer = TestClock()
    timer.set(NOW)
    with clock.using(timer):
        yield timer


@pytest.fixture
def w(frozen, seeded):
    return seeded["world_id"]


def facts(at=PAST, zone="UTC", status="confirmed", updated=PAST):
    return {
        KEY: {
            "scheduled_at": at.isoformat() if at else None,
            "timezone": zone,
            "status": status,
            "updated_at": updated.isoformat() if updated else None,
        }
    }


def screened(at=PAST, key=KEY):
    # Text contradicts the trusted facts deliberately; only code-held fields are authoritative.
    return Screener(ScreeningConfig()).screen_source(
        "calendar-export", [Message(key, "calendar-owner", "Orion session, completed in UTC", at)]
    )


def source(driver, w, at=FUTURE, *, binding=True, zone="UTC", status="confirmed", settings=None):
    connector = record_connector(
        driver, w, "calendar", settings=settings or {"log_from": "2027-02-01"}
    )
    options = {"calendar_events": facts(at, zone, status)} if binding else {}
    stored = add_screened_source(
        WorldWriter(driver, w),
        screened(at),
        connector_id=connector,
        parser_version="calendar-test-1",
        **options,
    )
    episode = record_episode(
        driver,
        w,
        connector_id=connector,
        source_ids=[stored["source_id"]],
        first_message_key=KEY,
        last_message_key=KEY,
        start_time=at or NOW,
        end_time=at or NOW,
        cut_rule="calendar-event",
    )
    return {"source_id": stored["source_id"], "episode_id": episode}


def obs(src, at=PAST, zone="UTC", *, key=KEY, locator=None):
    return observation(
        src,
        "c1",
        1,
        at=at,
        message_key=key,
        locator=locator or "key:" + key,
        statement_class="report_happened",
        person_timezone=zone,
    )


def typed(driver, w, row):
    item = created_id(
        apply(driver, w, [create_item("event", "Orion session", "c1")], [row], caller=LEARNER)
    )
    [stored] = WorldQuery(driver, w).read("MATCH (e:Evidence {target_id: $id}) RETURN e", id=item)
    return item, stored["e"]


def done(driver, w, row):
    return apply(
        driver,
        w,
        [create_item("task", "Finish Orion session", "c1", status="done")],
        [row],
        caller=LEARNER,
    )


def test_future_source_cannot_confirm_via_old_observation_time(driver, w):
    src = source(driver, w, binding=False)
    _, row = typed(driver, w, obs(src))
    assert row["source_type"] == "planned"


def test_future_source_cannot_mark_task_done_via_old_observation_time(driver, w):
    src = source(driver, w, binding=False)
    with pytest.raises(BatchDeclined) as caught:
        done(driver, w, obs(src))
    assert {r.rule for r in caught.value.refusals} == {"terminal_needs_confirmation"}


@pytest.mark.parametrize(
    "scheduled,supplied,status,trusted_zone,supplied_zone,settings,expected",
    [
        (FUTURE, FUTURE, "confirmed", "UTC", "UTC", None, "planned"),
        (FUTURE, PAST, "confirmed", "UTC", "UTC", None, "planned"),
        (PAST, PAST, "confirmed", "UTC", "UTC", None, "confirmed"),
        (PAST, PAST - timedelta(days=2), "confirmed", "UTC", "UTC", None, "planned"),
        (PAST, PAST, "cancelled", "UTC", "UTC", None, "planned"),
        (FUTURE, PAST, "cancelled", "UTC", "UTC", None, "planned"),
        (None, PAST, "cancelled", "UTC", "UTC", None, "planned"),
        (PAST, PAST, "tentative", "UTC", "UTC", None, "planned"),
        (PAST, PAST, "confirmed", "UTC", "Asia/Dubai", None, "planned"),
        (BOUNDARY, BOUNDARY, "confirmed", "UTC", "UTC", {"log_from": "2027-02-20"}, "planned"),
        (
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "UTC",
            "Asia/Dubai",
            {"log_from": "2027-02-20"},
            "planned",
        ),
        (
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "Asia/Dubai",
            "Asia/Dubai",
            {"log_from": "2027-02-20"},
            "confirmed",
        ),
        (
            BOUNDARY,
            BOUNDARY,
            "confirmed",
            "Asia/Dubai",
            None,
            {"log_from": "2027-02-20"},
            "confirmed",
        ),
        (PAST, PAST, "confirmed", "UTC", "UTC", {"unlogged": True}, "planned"),
        (PAST, PAST, "confirmed", "UTC", "UTC", {"logged_days": ["2027-02-25"]}, "confirmed"),
        (PAST, PAST, "confirmed", "UTC", "UTC", {"logged_days": ["2027-02-24"]}, "planned"),
    ],
)
def test_bound_event_controls_confirmation_and_completion(
    driver, w, scheduled, supplied, status, trusted_zone, supplied_zone, settings, expected
):
    src = source(driver, w, scheduled, status=status, zone=trusted_zone, settings=settings)
    row = obs(src, supplied, supplied_zone)
    _, stored = typed(driver, w, row)
    assert stored["source_type"] == expected
    if expected == "confirmed":
        receipt = done(driver, w, row)
        assert receipt.state == "saved"
    else:
        with pytest.raises(BatchDeclined) as caught:
            done(driver, w, row)
        assert {r.rule for r in caught.value.refusals} == {"terminal_needs_confirmation"}


@pytest.mark.parametrize("key,locator", [(KEY, "whole"), ("another-key", "key:" + KEY)])
def test_an_observation_cannot_borrow_another_calendar_binding(driver, w, key, locator):
    src = source(driver, w, PAST)
    _, stored = typed(driver, w, obs(src, key=key, locator=locator))
    assert stored["source_type"] == "planned"


def test_a_clock_advance_does_not_reclassify_a_calendar_row(driver, w, frozen):
    src = source(driver, w)
    item, row = typed(driver, w, obs(src, FUTURE))
    frozen.set(FUTURE + timedelta(days=1))
    [result] = WorldQuery(driver, w).read(
        "MATCH (i:Item {id: $id}) RETURN i.derived_source_type AS type", id=item
    )
    assert row["source_type"] == result["type"] == "planned"


@pytest.mark.parametrize("reuse", ["evidence_id", "same_message"])
def test_old_unbound_confirmation_cannot_be_cited_again(driver, w, monkeypatch, reuse):
    src = source(driver, w, binding=False)
    with monkeypatch.context() as old_writer:
        old_writer.setattr(provenance, "calendar_logged", lambda *a, **k: True)
        item, row = typed(driver, w, obs(src))
    assert row["source_type"] == "confirmed"
    if reuse == "evidence_id":
        op = create_item(
            "task",
            "Finish Orion session",
            "unused",
            status="done",
            evidence_refs=[{"evidence_id": row["id"], "polarity": "supports"}],
        )
        evidence = []
    else:
        op = {
            "op": "attach_evidence",
            "op_id": "again",
            "target": item,
            "ev_id": "c1",
            "polarity": "supports",
        }
        evidence = [obs(src)]
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [op], evidence, caller=LEARNER)
    assert {r.rule for r in caught.value.refusals} == {"calendar_binding"}


@pytest.mark.parametrize(
    "change",
    [
        {"status": "invalid"},
        {"timezone": "No/Such_Zone"},
        {"scheduled_at": None},
        {"scheduled_at": FUTURE.isoformat()},
        {"scheduled_at": "2027-02-25"},
        {"updated_at": "x" * 65},
        {"unexpected": "field"},
    ],
)
def test_calendar_facts_reject_invalid_or_unbound_fields(change):
    supplied = facts()
    supplied[KEY].update(change)
    with pytest.raises(InvalidRecord, match="calendar_facts"):
        calendar_facts_json(supplied, screened().messages)


def test_calendar_facts_reject_missing_duplicate_and_excess_messages():
    messages = screened().messages
    for supplied, retained in [
        (facts(), ()),
        (facts(), (*messages, *messages)),
        ({str(n): facts()[KEY] for n in range(MAX_CALENDAR_EVENTS + 1)}, messages),
    ]:
        with pytest.raises(InvalidRecord, match="calendar_facts"):
            calendar_facts_json(supplied, retained)


def test_calendar_facts_keep_schedule_and_revision_separate(driver, w):
    src = source(driver, w, FUTURE)
    [stored] = WorldQuery(driver, w).read(
        "MATCH (s:Source {id: $id}) RETURN s.calendar_events AS facts", id=src["source_id"]
    )
    value = json.loads(stored["facts"])[KEY]
    assert value["scheduled_at"] == FUTURE.isoformat()
    assert value["updated_at"] == PAST.isoformat()
