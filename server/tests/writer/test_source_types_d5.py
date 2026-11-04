"""D5's writer contract and ingest tests: every claim knows what its source can prove.

Written from the spec's "How to test it" for D5 (the writer contract tests and the ingest tests
per connector, at the writer level), the D5 tables of what each connector yields and confirms,
and B.3's relay fields, not from the implementation. Speakers come from a source's metadata
through record_source(speakers=...), as a connector's code stores them. All names are invented,
and every time comes from a TestClock.
"""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.connection import DATABASE
from grove.store.current import standings
from grove.store.query import WorldQuery
from grove.writer import (
    BatchDeclined,
    enqueue_request,
    record_connector,
    record_episode,
    record_source,
)
from grove.writer.sources import add_screened_source
from grove.writer.testing import plant_node, set_self_item
from grove.writer.tx import WorldWriter
from tests.writer.helpers import (
    HOST,
    LEARNER,
    PERSON,
    apply,
    assert_only_decline_log,
    clock_at,
    count_rows,
    create_item,
    create_link,
    created_id,
    new_op_id,
    observation,
    read,
    request_key,
    supports,
)

NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)
RECHECK = "2027-03-20T09:00:00+00:00"
RENEWED = "2027-04-10T09:00:00+00:00"
JULY_1 = datetime(2027, 7, 1, 9, 0, tzinfo=UTC)
AUGUST = datetime(2027, 8, 2, 9, 0, tzinfo=UTC)
FLIGHT = "2027-07-12"
GIT_TOKEN = "c0ffee42"

_keys = count(1)


# Fixtures and builders ------------------------------------------------------------------------


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(NOW)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def w(frozen, seeded):
    return seeded["world_id"]


def source(
    driver,
    w: str,
    connector_type: str,
    lines: list[str],
    *,
    speakers: dict[str, dict[str, str]] | None = None,
    document: bool = False,
    **connector: Any,
) -> dict[str, str]:
    """A stored source with the speakers its connector's code read from its metadata."""
    connector_id = record_connector(driver, w, connector_type, **connector)
    source_id = record_source(
        driver,
        w,
        connector_id=connector_id,
        external_locator=f"d5-{next(_keys)}.txt",
        content="\n".join(lines),
        parser_version="test-1",
        speakers=speakers,
        document=document,
    )
    episode_id = record_episode(
        driver,
        w,
        connector_id=connector_id,
        source_ids=[source_id],
        first_message_key=f"d5-first-{next(_keys)}",
        last_message_key=f"d5-last-{next(_keys)}",
        # Wide, so these tests date rows freely; a row may not be dated outside its episode.
        start_time=NOW - timedelta(days=3650),
        end_time=NOW + timedelta(days=3650),
        cut_rule="chat_window_90m",
    )
    return {"connector_id": connector_id, "source_id": source_id, "episode_id": episode_id}


@pytest.fixture
def talk(driver, w):
    lines = [f"line {n}: Rowan writes about the Okonkwo Hall seating plan" for n in range(20)]
    return source(driver, w, "whatsapp", lines)


def line(src: dict, ev_id: str, n: int, at: datetime | None = None, **extra: Any) -> dict:
    return observation(src, ev_id, n, at=at or NOW - timedelta(minutes=5), **extra)


def whole(src: dict, ev_id: str, at: datetime | None = None, **extra: Any) -> dict:
    return observation(src, ev_id, 1, at=at or NOW - timedelta(minutes=5), locator="whole", **extra)


def item_row(driver, w: str, item_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def link_row(driver, w: str, link_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link_id)
    return row["l"]


def evidence_on(driver, w: str, target_id: str) -> list[dict[str, Any]]:
    rows = read(
        driver, w, "MATCH (e:Evidence {target_id: $id}) RETURN e ORDER BY e.id", id=target_id
    )
    return [row["e"] for row in rows]


def refused(driver, w: str, ops: list[dict], evidence: list[dict], caller=LEARNER, **kw) -> list:
    before = count_rows(driver, w)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, ops, evidence, caller=caller, **kw)
    assert_only_decline_log(driver, w, before, caught.value.refusals)
    return caught.value.refusals


def one(refusals, code: str, rule: str | None = None):
    found = [r for r in refusals if r.code == code and (rule is None or r.rule == rule)]
    assert found, f"no {code}/{rule} among {[(r.code, r.rule) for r in refusals]}"
    return found[0]


def utc(value: Any) -> datetime:
    return value.astimezone(UTC)


def typed(driver, w: str, obs: dict, title: str = "An invented record", **fields) -> dict:
    """The evidence row the writer stores for one observation, on a new record."""
    item = created_id(
        apply(driver, w, [create_item("event", title, obs["ev_id"], **fields)], [obs])
    )
    [row] = evidence_on(driver, w, item)
    return {**row, "item": item}


def judge(
    driver,
    w: str,
    subject: str,
    obs: dict | None = None,
    verdict: str = "supports",
    *,
    role: str = "first_judge",
    model: str = "judge-a",
    evidence_id: str | None = None,
) -> str:
    call = plant_node(driver, w, "ModelCall", model=model, job="verify", outcome="ok", at=NOW)
    named = (
        {"evidence_id": evidence_id}
        if evidence_id is not None
        else {"source_id": obs["source_id"], "locator": obs["locator"]}
    )
    return plant_node(
        driver,
        w,
        "Judgment",
        role=role,
        subject=subject,
        verdict=verdict,
        model_call_id=call["id"],
        at=NOW,
        **named,
    )["id"]


def verify(claim: str, pieces: list[dict], disposition: str = "confirmed", **fields) -> dict:
    return {
        "op": "verify",
        "op_id": new_op_id(),
        "claim": claim,
        "pieces": pieces,
        "disposition": disposition,
        "reason": "the judge read the source",
        **fields,
    }


def change_status(item: str, status: str, *ev_ids: str) -> dict:
    return {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": item,
        "status": status,
        "evidence_refs": supports(*ev_ids),
        "reason": "the message states it",
    }


def attach(target: str, ev_id: str) -> dict:
    return {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": target,
        "ev_id": ev_id,
        "polarity": "supports",
    }


def open_task(driver, w: str, talk: dict, title: str = "Send the Okonkwo Hall seating plan") -> str:
    op = create_item("task", title, "t0", status="open", recheck_by=RECHECK)
    return created_id(apply(driver, w, [op], [line(talk, "t0", 1)]))


# The proof rule: done, and the reported done state ---------------------------------------------


@pytest.mark.parametrize("statement_class", ["report_happened", "promise"])
def test_done_with_only_said_or_reported_done_evidence_is_refused_and_reported_done_is_taken(
    driver, w, talk, statement_class
):
    task = open_task(driver, w, talk)
    done = line(talk, "d1", 2, statement_class=statement_class)
    refusal = one(
        refused(driver, w, [change_status(task, "done", "d1")], [done]),
        "terminal_needs_confirmation",
        "terminal_needs_confirmation",
    )
    assert refusal.details.get("alternative") == "reported done"
    apply(driver, w, [change_status(task, "reported done", "d1")], [done])
    row = item_row(driver, w, task)
    assert (row["status"], row["status_role"]) == ("reported done", "reported_done")


def test_a_confirming_artifact_in_the_same_transaction_allows_done(driver, w, talk):
    task = open_task(driver, w, talk)
    files = source(
        driver,
        w,
        "notes_files_releases",
        ["Signed hand-over: the seating plan, sent"],
        document=True,
    )
    # The whole stored file is the artifact (S51); a line of a note never is.
    artifact = whole(files, "f1", statement_class="artifact")
    done = line(talk, "d1", 2, statement_class="report_happened")
    apply(driver, w, [attach(task, "f1"), change_status(task, "done", "d1")], [artifact, done])
    row = item_row(driver, w, task)
    assert (row["status_role"], row["derived_source_type"]) == ("done", "confirmed")


# A source type is never written ----------------------------------------------------------------


@pytest.mark.parametrize("where", ["observation", "create_item", "update_item"])
def test_a_source_type_cannot_be_written_directly(driver, w, talk, where):
    obs = line(talk, "s1", 2)
    if where == "observation":
        ops = [create_item("event", "Rowan booked Okonkwo Hall", "s1")]
        obs["source_type"] = "confirmed"
    elif where == "create_item":
        ops = [
            create_item("event", "Rowan booked Okonkwo Hall", "s1", derived_source_type="confirmed")
        ]
    else:
        hall = created_id(
            apply(
                driver,
                w,
                [create_item("organisation", "Okonkwo Hall", "s0")],
                [line(talk, "s0", 1)],
            )
        )
        ops = [
            {
                "op": "update_item",
                "op_id": new_op_id(),
                "item": hall,
                "version": 1,
                "fields": {"derived_source_type": "confirmed"},
                "evidence_refs": supports("s1"),
                "reason": "the message states it",
            }
        ]
    one(refused(driver, w, ops, [obs]), "caller_source_type", "caller_source_type")


# Rule 3: a clock changes no type and no state ---------------------------------------------------


def test_moving_the_clock_past_a_plans_date_changes_no_claims_type_or_state(driver, w, frozen):
    calendar = source(driver, w, "calendar", ["Flight to Velmora, Ilse Varga, 12 July"])
    entry = whole(calendar, "c1", at=NOW, statement_class="intention")
    ops = [
        create_item(
            "plan",
            "Ilse flies to Velmora",
            "c1",
            status="active",
            became_true=clock_at(NOW),
            properties={"scheduled for": FLIGHT},
        ),
        create_item("trip", "Ilse's trip to Velmora", "c1", properties={"starts": FLIGHT}),
    ]
    receipt = apply(driver, w, ops, [entry])
    ids = [created_id(receipt, nth=0), created_id(receipt, nth=1)]

    def snapshot(at: datetime) -> tuple[dict, dict]:
        frozen.set(at)
        rows = {item: item_row(driver, w, item) for item in ids}
        found = standings(WorldQuery(driver, w), ids)
        return rows, {item: (found[item].state, found[item].reason) for item in ids}

    rows_before, states_before = snapshot(JULY_1)
    rows_after, states_after = snapshot(AUGUST)
    assert rows_after == rows_before  # every column, the source type among them
    assert states_after == states_before
    assert [rows_before[item]["derived_source_type"] for item in ids] == ["planned", "planned"]
    assert states_before[ids[1]][0] == "planned"


# Rule 4: speakers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "caller_speaker"),
    [
        (None, "ai_host"),
        ({"kind": "automated", "sender": "venue-bot", "locator": "line:2"}, "person"),
    ],
)
def test_a_decision_whose_speaker_is_not_the_decider_is_refused(driver, w, meta, caller_speaker):
    speakers = {"m-2": meta} if meta else None
    chat = source(
        driver, w, "whatsapp", ["Rowan: ok", "We go with Okonkwo Hall"], speakers=speakers
    )
    decision = line(
        chat, "d1", 2, message_key="m-2", statement_class="decision", speaker_kind=caller_speaker
    )
    op = create_item("decision", "The dinner is at Okonkwo Hall", "d1")
    one(refused(driver, w, [op], [decision]), "invalid_arguments", "decider_not_speaker")


def test_a_decision_spoken_by_the_person_is_said_with_the_person_as_speaker(driver, w, talk):
    decision = line(talk, "d1", 3, statement_class="decision")
    row = typed(driver, w, decision, "The dinner is at Okonkwo Hall")
    assert (row["source_type"], row["speaker_kind"]) == ("said", "person")


def test_a_forwarded_messages_speaker_is_its_original_author(driver, w, talk):
    rowan = created_id(
        apply(driver, w, [create_item("person", "Rowan Okonkwo", "p1")], [line(talk, "p1", 1)])
    )
    chat = source(
        driver,
        w,
        "whatsapp",
        ["Forwarded: the hall is ours for the 12th"],
        speakers={"m-f": {"kind": "forwarded", "original_author": rowan, "locator": "line:1"}},
    )
    # The caller's own classification says person; the metadata wins.
    forwarded = line(chat, "f1", 1, message_key="m-f", statement_class="report_state")
    row = typed(driver, w, forwarded, "Okonkwo Hall is held for the 12th")
    assert (row["speaker_kind"], row["original_author"]) == ("forwarded", rowan)


def test_a_forwarded_message_without_its_original_author_is_refused(driver, w, talk):
    by_caller = line(talk, "f1", 2, speaker_kind="forwarded")
    op = create_item("event", "Okonkwo Hall is held for the 12th", "f1")
    one(refused(driver, w, [op], [by_caller]), "invalid_arguments", "original_author_required")
    chat = source(
        driver,
        w,
        "whatsapp",
        ["Forwarded: the hall is ours"],
        speakers={"m-f": {"kind": "forwarded", "locator": "line:1"}},
    )
    by_meta = line(chat, "f2", 1, message_key="m-f")
    op = create_item("event", "Okonkwo Hall is held for the 12th", "f2")
    one(refused(driver, w, [op], [by_meta]), "invalid_arguments", "original_author_required")


# Capabilities ----------------------------------------------------------------------------------


def plant_unclassed_kind(driver, w: str) -> None:
    """A kind with no lifecycle class: no default row of D5's table keys on it."""
    plant_node(
        driver,
        w,
        "Kind",
        name="keepsake",
        status="canonical",
        origin="minted",
        status_vocabulary="[]",
        version=1,
    )


@pytest.mark.parametrize("connector", ["web_pages", "screenshots"])
def test_a_claim_no_specific_row_and_no_default_covers_cannot_be_confirmed(
    driver, w, frozen, talk, connector
):
    plant_unclassed_kind(driver, w)
    keepsake = created_id(
        apply(
            driver,
            w,
            [create_item("keepsake", "The brass key of Okonkwo Hall", "k1")],
            [line(talk, "k1", 1)],
        )
    )
    # The lane fetches the source at LATER and stores it first; the piece speaks for no later.
    frozen.set(LATER)
    src = source(driver, w, connector, ["The brass key of Okonkwo Hall, on its hook"])
    piece = whole(src, "p1", at=NOW + timedelta(minutes=30), statement_class="artifact")
    if connector == "screenshots":
        piece["modality"] = "image"
    pieces = [{"ev_id": "p1", "judgment_id": judge(driver, w, f"item:{keepsake}", piece)}]
    ops = [verify(keepsake, pieces, recheck_by=RENEWED)]
    one(refused(driver, w, ops, [piece]), "not_confirmable", "no_capability")


def test_a_minted_words_capability_row_applies_only_once_the_second_model_accepted_it(
    driver, w, seeded, frozen, talk
):
    # Git confirms no lasting thing by default; a row minted with a word would let it.
    hall = created_id(
        apply(driver, w, [create_item("organisation", "Okonkwo Hall", "o1")], [line(talk, "o1", 1)])
    )
    row = plant_node(
        driver,
        w,
        "Capability",
        connector_type="git",
        registry_id=seeded["kinds"]["organisation"],
        capability="confirms",
        origin="minted_with_a_word",
        reason="a commit to the venue registry confirms the venue",
        version=1,
    )
    frozen.set(LATER)
    git = source(driver, w, "git", [f"commit {GIT_TOKEN} Register Okonkwo Hall as a venue"])
    commit = observation(
        git,
        "g1",
        1,
        at=NOW + timedelta(minutes=30),
        locator=f"key:{GIT_TOKEN}",
        message_key=GIT_TOKEN,
        statement_class="report_happened",
    )
    judgment = judge(driver, w, f"item:{hall}", commit)
    ops = [verify(hall, [{"ev_id": "g1", "judgment_id": judgment}])]

    def accept_with(verdict: str) -> None:
        call = plant_node(driver, w, "ModelCall", model="judge-b", job="field_fit", outcome="ok")
        fit = plant_node(
            driver,
            w,
            "Judgment",
            role="field_fit",
            subject=f"capability:{row['id']}",
            verdict=verdict,
            model_call_id=call["id"],
            at=NOW,
        )
        with driver.session(database=DATABASE) as session:
            session.run(
                "CYPHER 25 MATCH (c:Capability {world_id: $w, id: $id}) "
                "SET c.accepted_judgment_id = $judgment",
                w=w,
                id=row["id"],
                judgment=fit["id"],
            ).consume()

    one(refused(driver, w, ops, [commit]), "not_confirmable", "no_capability")
    accept_with("does_not_fit")
    one(refused(driver, w, ops, [commit]), "not_confirmable", "no_capability")
    accept_with("fits")
    apply(driver, w, ops, [commit], caller=LEARNER)
    assert utc(item_row(driver, w, hall)["last_confirmed"]) == NOW + timedelta(minutes=30)
    [verification] = read(driver, w, "MATCH (v:Verification {claim_id: $id}) RETURN v", id=hall)
    assert f'"capability_row": "{row["id"]}"' in verification["v"]["evidence_pieces"]


def test_an_identity_source_confirms_a_name_and_is_refused_for_where_someone_lives(
    driver, w, frozen, talk
):
    ops = [
        create_item("person", "Ilse Varga", "s1", temp_id="$ilse"),
        create_item("place", "Velmora", "s1", temp_id="$velmora"),
        create_link("$ilse", "lives in", "$velmora", "s1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, w, ops, [line(talk, "s1", 1)])
    ilse, link = created_id(receipt, nth=0), created_id(receipt, ">", "link")
    frozen.set(LATER)
    identity = source(driver, w, "identity", ["profile: Ilse Varga, of Velmora"])
    profile = whole(identity, "id1", at=NOW + timedelta(minutes=30), statement_class="report_state")

    pieces = [{"ev_id": "id1", "judgment_id": judge(driver, w, f"link:{link}", profile)}]
    one(
        refused(driver, w, [verify(link, pieces, recheck_by=RENEWED)], [profile]),
        "not_confirmable",
        "no_capability",
    )
    pieces = [{"ev_id": "id1", "judgment_id": judge(driver, w, f"item:{ilse}", profile)}]
    apply(driver, w, [verify(ilse, pieces)], [profile], caller=LEARNER)
    assert utc(item_row(driver, w, ilse)["last_confirmed"]) == NOW + timedelta(minutes=30)
    assert link_row(driver, w, link).get("last_confirmed") is None


def test_an_inferred_claim_carries_derivation_inferred_and_verify_refuses_it(
    driver, w, frozen, talk
):
    pages = source(driver, w, "web_pages", ["Okonkwo Hall: the Varga dinner booking is confirmed"])
    booking = created_id(
        apply(
            driver,
            w,
            [create_item("event", "The Varga dinner is booked", "b1")],
            [whole(pages, "b1", at=NOW)],
        )
    )
    [premise] = evidence_on(driver, w, booking)
    frozen.set(LATER)
    inferred = line(talk, "i1", 4, at=NOW, derivation="inferred", premises=[premise["id"]])
    dinner = created_id(
        apply(driver, w, [create_item("event", "The Varga dinner takes place", "i1")], [inferred])
    )
    row = item_row(driver, w, dinner)
    assert row["derivation"] == "inferred"
    [stored] = evidence_on(driver, w, dinner)
    assert (stored["derivation"], stored["premises"]) == ("inferred", [premise["id"]])
    judgment = judge(driver, w, f"item:{dinner}", evidence_id=stored["id"])
    ops = [verify(dinner, [{"evidence_id": stored["id"], "judgment_id": judgment}])]
    one(refused(driver, w, ops, []), "inferred_evidence", "inferred_piece")


# Ingest, per connector -------------------------------------------------------------------------


CALENDAR_LINES = ["Okonkwo Hall dinner", "Velmora flight", "Board meeting", "Caterer tasting"]


def bound_calendar(driver, w, at, zone, settings):
    connector = record_connector(driver, w, "calendar", settings=settings)
    screened = Screener(ScreeningConfig()).screen_source(
        "calendar-export", [Message("calendar-entry", None, CALENDAR_LINES[0], at)]
    )
    stored = add_screened_source(
        WorldWriter(driver, w),
        screened,
        connector_id=connector,
        parser_version="test-1",
        calendar_events={
            "calendar-entry": {
                "scheduled_at": at.isoformat(),
                "timezone": zone or "UTC",
                "status": "confirmed",
            }
        },
    )
    episode = record_episode(
        driver,
        w,
        connector_id=connector,
        source_ids=[stored["source_id"]],
        first_message_key="calendar-entry",
        last_message_key="calendar-entry",
        start_time=at,
        end_time=at,
        cut_rule="calendar-event",
    )
    return {"source_id": stored["source_id"], "episode_id": episode}


def test_a_future_calendar_entry_is_planned(driver, w):
    calendar = source(driver, w, "calendar", CALENDAR_LINES)
    entry = whole(calendar, "c1", at=NOW + timedelta(days=10), statement_class="intention")
    row = typed(driver, w, entry, "The Okonkwo Hall dinner")
    assert row["source_type"] == "planned"
    assert item_row(driver, w, row["item"])["derived_source_type"] == "planned"


def test_a_past_calendar_entry_is_planned_without_the_log_setting(driver, w):
    calendar = source(driver, w, "calendar", CALENDAR_LINES)
    entry = whole(calendar, "c1", at=NOW - timedelta(days=3), statement_class="intention")
    assert typed(driver, w, entry)["source_type"] == "planned"


@pytest.mark.parametrize(
    ("at", "zone", "expected"),
    [
        (datetime(2027, 2, 25, 12, 0, tzinfo=UTC), None, "confirmed"),  # after the start date
        (datetime(2027, 2, 20, 0, 30, tzinfo=UTC), None, "confirmed"),  # on the start date
        (datetime(2027, 2, 19, 12, 0, tzinfo=UTC), None, "planned"),  # before it
        # 21:00 UTC on 19 Feb is 01:00 on 20 Feb in Dubai: the person's day decides.
        (datetime(2027, 2, 19, 21, 0, tzinfo=UTC), "Asia/Dubai", "confirmed"),
        (NOW + timedelta(days=2), None, "planned"),  # a future entry, even under the log
    ],
)
def test_a_past_calendar_entry_is_confirmed_with_the_log_setting_from_its_start_date(
    driver, w, at, zone, expected
):
    calendar = bound_calendar(driver, w, at, zone, {"log_from": "2027-02-20"})
    extra = {"person_timezone": zone} if zone else {}
    entry = observation(
        calendar,
        "c1",
        1,
        at=at,
        statement_class="report_happened",
        message_key="calendar-entry",
        locator="key:calendar-entry",
        **extra,
    )
    assert typed(driver, w, entry)["source_type"] == expected


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2027, 2, 10, 12, 0, tzinfo=UTC), "confirmed"),
        (datetime(2027, 2, 11, 12, 0, tzinfo=UTC), "planned"),
    ],
)
def test_a_past_calendar_entry_on_a_logged_day_is_confirmed(driver, w, at, expected):
    calendar = bound_calendar(driver, w, at, "UTC", {"logged_days": ["2027-02-10"]})
    entry = observation(
        calendar,
        "c1",
        1,
        at=at,
        statement_class="report_happened",
        message_key="calendar-entry",
        locator="key:calendar-entry",
    )
    assert typed(driver, w, entry)["source_type"] == expected


def test_a_reminder_dated_in_the_past_is_planned(driver, w):
    reminders = source(driver, w, "reminders", ["Call the Okonkwo Hall caterer"])
    reminder = whole(reminders, "r1", at=NOW - timedelta(days=5), statement_class="intention")
    assert typed(driver, w, reminder, "Call the caterer")["source_type"] == "planned"
    reported = whole(reminders, "r2", at=NOW - timedelta(days=5), statement_class="report_happened")
    assert typed(driver, w, reported, "The caterer was called")["source_type"] == "planned"


def test_a_chat_line_saying_done_is_reported_done(driver, w, talk):
    done = line(talk, "d1", 5, statement_class="report_happened")
    op = create_item(
        "task", "Send the seating plan", "d1", status="reported done", recheck_by=RECHECK
    )
    task = created_id(apply(driver, w, [op], [done]))
    [row] = evidence_on(driver, w, task)
    assert row["source_type"] == "reported_done"
    assert item_row(driver, w, task)["derived_source_type"] == "reported_done"


def test_a_booking_confirmation_yields_a_confirmed_booking_and_a_planned_trip(driver, w, frozen):
    mail = source(
        driver,
        w,
        "whatsapp",
        ["Velmora Air: booking VA212 confirmed for Ilse Varga, flight on 12 July"],
        speakers={"m-b": {"kind": "automated", "sender": "velmora-air", "locator": "line:1"}},
    )
    booked = line(
        mail,
        "b1",
        1,
        at=NOW - timedelta(hours=1),
        message_key="m-b",
        statement_class="report_happened",
    )
    ops = [
        create_item("event", "Ilse booked flight VA212", "b1", became_true=clock_at(NOW)),
        create_item("trip", "Ilse's trip to Velmora", "b1", properties={"starts": FLIGHT}),
    ]
    receipt = apply(driver, w, ops, [booked])
    booking, trip = created_id(receipt, nth=0), created_id(receipt, nth=1)
    [row] = evidence_on(driver, w, booking)
    assert (row["source_type"], row["speaker_kind"], row["sender"]) == (
        "confirmed",
        "automated",
        "velmora-air",
    )
    assert item_row(driver, w, booking)["derived_source_type"] == "confirmed"
    for at in (JULY_1, AUGUST):
        frozen.set(at)
        found = standings(WorldQuery(driver, w), [booking, trip])
        assert found[booking].state == "current"
        assert found[trip].state == "planned"
    # A booking does not prove the trip: it cannot give the trip its start.
    frozen.set(NOW)
    started = create_item(
        "trip",
        "The VA212 journey",
        "b2",
        became_true=clock_at(datetime(2027, 7, 12, 8, 0, tzinfo=UTC)),
        recheck_by=RECHECK,
    )
    again = line(
        mail,
        "b2",
        1,
        at=NOW - timedelta(hours=1),
        message_key="m-b",
        statement_class="report_happened",
    )
    one(
        refused(driver, w, [started], [again]),
        "terminal_needs_confirmation",
        "period_start_needs_confirmation",
    )


def test_a_screenshot_of_a_receipt_is_confirmed_with_modality_image(driver, w):
    shots = source(driver, w, "screenshots", ["Okonkwo Hall receipt 0912: dinner for 12, paid"])
    shot = whole(shots, "s1", statement_class="artifact", modality="image")
    row = typed(driver, w, shot, "The Varga dinner was paid")
    assert (row["source_type"], row["modality"]) == ("confirmed", "image")
    item = item_row(driver, w, row["item"])
    assert (item["derived_source_type"], item["modality"]) == ("confirmed", "image")


def test_a_delivery_notification_has_an_automated_speaker(driver, w):
    notes = source(
        driver,
        w,
        "whatsapp",
        ["Parcel desk: the table linen for Okonkwo Hall was delivered"],
        speakers={"m-d": {"kind": "automated", "sender": "parcel-desk", "locator": "line:1"}},
    )
    note = line(notes, "n1", 1, message_key="m-d", statement_class="report_happened")
    row = typed(driver, w, note, "The table linen was delivered")
    assert (row["speaker_kind"], row["sender"], row["source_type"]) == (
        "automated",
        "parcel-desk",
        "confirmed",
    )


def test_a_voice_note_has_modality_voice_and_its_sender_as_speaker(driver, w):
    voice = source(
        driver,
        w,
        "voice_notes",
        ["Rowan: the caterer can do the 12th at Okonkwo Hall"],
        speakers={"m-v": {"kind": "third_party", "sender": "rowan-okonkwo", "locator": "whole"}},
    )
    note = whole(voice, "v1", message_key="m-v", statement_class="report_state", modality="voice")
    row = typed(driver, w, note, "The caterer can do the 12th")
    assert (row["modality"], row["speaker_kind"], row["sender"]) == (
        "voice",
        "third_party",
        "rowan-okonkwo",
    )
    assert row["source_type"] == "reported_done"


# The AI host -----------------------------------------------------------------------------------


def host_source(
    driver,
    w: str,
    text: str,
    relay: str,
    *,
    caller=HOST,
    verbatim: bool | None = None,
    host_message_id: str | None = None,
) -> dict[str, str]:
    src = source(driver, w, "ai_host", [text])
    enqueue_request(
        driver,
        w,
        caller,
        request_key(),
        source_id=src["source_id"],
        relay=relay,
        verbatim=verbatim,
        host_message_id=host_message_id,
    )
    return src


@pytest.mark.parametrize(
    ("statement_class", "expected"), [("report_happened", "reported_done"), ("decision", "said")]
)
def test_a_remember_with_the_persons_words_is_the_persons_and_typed_as_a_chat_line(
    driver, w, talk, statement_class, expected
):
    src = host_source(
        driver,
        w,
        "I moved the Varga dinner to Okonkwo Hall",
        "remember",
        verbatim=True,
        host_message_id="hm-101",
    )
    relayed = whole(src, "h1", statement_class=statement_class, speaker_kind="ai_host")
    row = typed(driver, w, relayed, "The Varga dinner is at Okonkwo Hall")
    assert (row["speaker_kind"], row["source_type"]) == ("person", expected)
    # Typed as the same words in a chat line are.
    chat = typed(driver, w, line(talk, "c1", 6, statement_class=statement_class))
    assert chat["source_type"] == row["source_type"]


def test_a_remember_without_the_persons_words_is_the_hosts_and_never_confirms(
    driver, w, frozen, talk
):
    src = host_source(driver, w, "They finished the seating plan", "remember")
    task = open_task(driver, w, talk)
    relayed = whole(src, "h1", at=NOW, statement_class="report_happened")
    one(
        refused(driver, w, [change_status(task, "done", "h1")], [relayed]),
        "terminal_needs_confirmation",
    )
    row = typed(driver, w, relayed, "The seating plan is finished")
    assert (row["speaker_kind"], row["source_type"]) == ("ai_host", "said")
    frozen.set(LATER)
    pieces = [{"ev_id": "h1", "judgment_id": judge(driver, w, f"item:{task}", relayed)}]
    one(
        refused(driver, w, [verify(task, pieces, recheck_by=RENEWED)], [relayed]),
        "not_confirmable",
        "source_type",
    )
    one(
        refused(driver, w, [verify(task, pieces, recheck_by=RENEWED)], [relayed], caller=HOST),
        "verbatim_required",
    )


@pytest.mark.parametrize(
    ("relay", "verbatim", "host_message_id"),
    [
        ("answer", None, None),
        ("answer", True, None),
        ("correction", None, "hm-7"),
        ("correction", False, "hm-7"),
    ],
)
def test_an_answer_or_a_correction_from_a_host_without_the_persons_words_is_refused(
    driver, w, relay, verbatim, host_message_id
):
    src = source(driver, w, "ai_host", ["She still lives in Velmora"])
    before = count_rows(driver, w)
    with pytest.raises(BatchDeclined) as caught:
        enqueue_request(
            driver,
            w,
            HOST,
            request_key(),
            source_id=src["source_id"],
            relay=relay,
            verbatim=verbatim,
            host_message_id=host_message_id,
        )
    assert [(r.code, r.rule) for r in caught.value.refusals] == [
        ("verbatim_required", "verbatim_required")
    ]
    assert count_rows(driver, w) == before


def test_the_writer_refuses_an_answer_relayed_without_the_persons_words(driver, w):
    # A request row whose relay is an answer, with no verbatim words: the writer refuses to read
    # the host's text as the person's answer.
    src = host_source(driver, w, "She still lives in Velmora", "answer", caller=PERSON)
    relayed = whole(src, "h1", statement_class="report_state")
    op = create_item("event", "Ilse lives in Velmora", "h1")
    one(refused(driver, w, [op], [relayed]), "verbatim_required", "verbatim_required")


@pytest.fixture
def own(driver, w, talk) -> str:
    """The person's own item, named on the :World row."""
    ilse = created_id(
        apply(driver, w, [create_item("person", "Ilse Varga", "p1")], [line(talk, "p1", 1)])
    )
    set_self_item(driver, w, ilse)
    return ilse


def test_a_relayed_answer_in_the_persons_words_confirms_where_they_live(
    driver, w, frozen, talk, own
):
    ops = [
        create_item("place", "Velmora", "s1", temp_id="$velmora"),
        create_link(own, "lives in", "$velmora", "s1", recheck_by=RECHECK),
    ]
    link = created_id(apply(driver, w, ops, [line(talk, "s1", 2)]), ">", "link")
    assert link_row(driver, w, link).get("last_confirmed") is None

    frozen.set(LATER)
    src = host_source(
        driver,
        w,
        "Yes, I still live in Velmora",
        "answer",
        verbatim=True,
        host_message_id="hm-201",
    )
    answer = whole(src, "a1", at=LATER, statement_class="report_state")
    subject = f"link:{link}"
    first = judge(driver, w, subject, answer, model="judge-a")
    # A state link on the person's own item is load-bearing: a second model agrees.
    second = judge(driver, w, subject, answer, role="second_judge", model="judge-b")
    op = verify(
        link, [{"ev_id": "a1", "judgment_id": first}], second_judge=[second], recheck_by=RENEWED
    )
    apply(driver, w, [op], [answer], caller=HOST)
    row = link_row(driver, w, link)
    assert utc(row["last_confirmed"]) == LATER
    stored = [e for e in evidence_on(driver, w, link) if e["source_id"] == src["source_id"]]
    assert [(e["speaker_kind"], e["source_type"]) for e in stored] == [("person", "confirmed")]


def test_the_same_answer_about_a_colleagues_work_stays_reported_done(driver, w, frozen, talk, own):
    ops = [
        create_item("person", "Kofi Mensah", "s1", temp_id="$kofi"),
        create_item(
            "task",
            "Send the Okonkwo Hall deck",
            "s1",
            status="open",
            recheck_by=RECHECK,
            temp_id="$deck",
        ),
        create_link("$deck", "assigned to", "$kofi", "s1", recheck_by=RECHECK),
    ]
    deck = created_id(apply(driver, w, ops, [line(talk, "s1", 2)]), nth=1)

    frozen.set(LATER)
    src = host_source(
        driver, w, "Kofi sent the deck yesterday", "answer", verbatim=True, host_message_id="hm-301"
    )
    answer = whole(src, "a1", at=LATER, statement_class="report_happened")
    refusal = one(
        refused(driver, w, [change_status(deck, "done", "a1")], [answer]),
        "terminal_needs_confirmation",
    )
    assert refusal.details.get("alternative") == "reported done"
    pieces = [{"ev_id": "a1", "judgment_id": judge(driver, w, f"item:{deck}", answer)}]
    one(
        refused(driver, w, [verify(deck, pieces, recheck_by=RENEWED)], [answer], caller=HOST),
        "not_confirmable",
        "no_capability",
    )
    apply(driver, w, [change_status(deck, "reported done", "a1")], [answer])
    row = item_row(driver, w, deck)
    assert row["status_role"] == "reported_done"
    assert row.get("last_confirmed") is None
