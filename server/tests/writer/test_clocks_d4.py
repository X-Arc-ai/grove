"""D4's writer contract for the clocks: nothing but verify moves last confirmed.

Written from the spec's "How to test it" for D4 (the writer contract tests about the clocks, the
claim about now, the plan horizon and the period rule) and Appendix C's horizon numbers, not from
the implementation. Each claim with a last confirmed gets it through a real verify, so the tests
read the clock the writer itself wrote. All names are invented, and every time comes from a
TestClock.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.timewords import resolve
from grove.writer import BatchDeclined
from grove.writer.migrate import run_migrations
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    LEARNER,
    apply,
    assert_only_decline_log,
    clock_at,
    count_rows,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = NOW + timedelta(minutes=30)
VERIFY_AT = NOW + timedelta(hours=1)
RECHECK = "2027-03-20T09:00:00+00:00"
RENEWED = "2027-04-10T09:00:00+00:00"
DEFERRED = "2027-05-01T09:00:00+00:00"
CLOCKS = ("last_confirmed", "confirmed_against")

CHAT_LINES = [
    f"line {n}: Rowan says the Okonkwo Hall seating plan is moving along" for n in range(30)
]
PAGE_LINES = [
    "Okonkwo Hall: the seating plan for the Ilse Varga dinner is published.",
    "Okonkwo Hall: the seating plan review is under way this week.",
]
CALENDAR_LINES = ["Flight to Velmora, Ilse Varga, 12 July"]
SCREENSHOT_LINES = ["Boarding pass: Ilse Varga, Velmora flight VA 212, gate 9, 12 July 2027"]


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


@pytest.fixture
def talk(driver, w, frozen):
    # The chat export is read after every message these tests cite: a row may not speak for a
    # time after its source's read (S51). The tests' clock stays within 60 days of NOW.
    frozen.set(NOW + timedelta(days=60))
    try:
        return make_source(driver, w, "whatsapp", CHAT_LINES)
    finally:
        frozen.set(NOW)


@pytest.fixture
def pages(driver, w, frozen):
    # The page is fetched at FETCHED and stored first: a piece speaks for that read, no later.
    frozen.set(FETCHED)
    try:
        return make_source(driver, w, "web_pages", PAGE_LINES)
    finally:
        frozen.set(NOW)


def said(talk: dict, ev_id: str, line: int, at: datetime | None = None, **extra: Any) -> dict:
    return observation(talk, ev_id, line, at=at or NOW - timedelta(minutes=5), **extra)


def fetched(pages: dict, ev_id: str, at: datetime = FETCHED, **extra: Any) -> dict:
    return observation(pages, ev_id, 1, at=at, locator="whole", **extra)


def item_row(driver, w: str, item_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


def link_row(driver, w: str, link_id: str) -> dict[str, Any]:
    [row] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link_id)
    return row["l"]


def row_of(driver, w: str, claim: tuple[str, str]) -> dict[str, Any]:
    claim_type, claim_id = claim
    return item_row(driver, w, claim_id) if claim_type == "item" else link_row(driver, w, claim_id)


def clocks(row: dict[str, Any]) -> tuple[str, ...]:
    """The two truth-clock columns as text, for a byte-identical comparison."""
    return tuple(repr(row.get(name)) for name in CLOCKS)


def confirmations_logged(driver, w: str, claim_id: str) -> int:
    rows = read(
        driver,
        w,
        "MATCH (c:ChangeEntry {entity_id: $id, field: 'last_confirmed'}) RETURN count(c) AS n",
        id=claim_id,
    )
    return rows[0]["n"]


def utc(value: Any) -> datetime:
    return value.astimezone(UTC)


def judge(driver, w: str, subject: str, obs: dict, verdict: str = "supports") -> str:
    """A recorded first-judge verdict on a fetch that has no evidence row yet."""
    call = plant_node(driver, w, "ModelCall", model="judge-a", job="verify", outcome="ok", at=NOW)
    return plant_node(
        driver,
        w,
        "Judgment",
        role="first_judge",
        subject=subject,
        verdict=verdict,
        source_id=obs["source_id"],
        locator=obs["locator"],
        model_call_id=call["id"],
        at=NOW,
    )["id"]


def verify(claim: str, ev_id: str, judgment_id: str, disposition: str = "confirmed", **fields):
    return {
        "op": "verify",
        "op_id": new_op_id(),
        "claim": claim,
        "pieces": [{"ev_id": ev_id, "judgment_id": judgment_id}],
        "disposition": disposition,
        "reason": "the judge read the page again",
        **fields,
    }


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


def confirm(driver, w: str, frozen, pages: dict, claim: tuple[str, str]) -> None:
    """A real verify of the claim against a page fetched after the claim was recorded."""
    claim_type, claim_id = claim
    frozen.set(VERIFY_AT)
    piece = fetched(pages, "v1")
    judgment = judge(driver, w, f"{claim_type}:{claim_id}", piece)
    apply(
        driver, w, [verify(claim_id, "v1", judgment, recheck_by=RENEWED)], [piece], caller=LEARNER
    )
    row = row_of(driver, w, claim)
    assert utc(row["last_confirmed"]) == FETCHED
    assert row["confirmed_against"] is not None


@pytest.fixture
def confirmed_task(driver, w, frozen, talk, pages) -> tuple[str, str]:
    op = create_item(
        "task",
        "Draft the Okonkwo Hall seating plan",
        "t1",
        status="open",
        recheck_by=RECHECK,
        became_true=clock_at(NOW - timedelta(days=2), "day"),
    )
    task = created_id(apply(driver, w, [op], [said(talk, "t1", 1)]))
    confirm(driver, w, frozen, pages, ("item", task))
    return ("item", task)


@pytest.fixture
def confirmed_link(driver, w, frozen, talk, pages) -> tuple[str, str]:
    ops = [
        create_item("organisation", "Okonkwo Hall", "o1", temp_id="$hall"),
        create_item("organisation", "Okonkwo Hall Trust", "o1", temp_id="$trust"),
        create_link("$hall", "part of", "$trust", "o1"),
    ]
    link = created_id(apply(driver, w, ops, [said(talk, "o1", 2)]), ">", "link")
    confirm(driver, w, frozen, pages, ("link", link))
    return ("link", link)


# Nothing but verify moves last confirmed ------------------------------------------------------


def _update(driver, w, frozen, talk, pages, claim):
    version = item_row(driver, w, claim[1])["version"]
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": claim[1],
        "version": version,
        "fields": {"summary": "The seating plan for the Varga dinner, now in a second draft"},
        "evidence_refs": supports("u1"),
        "reason": "the message states it",
    }
    apply(driver, w, [op], [said(talk, "u1", 3, at=frozen.now())])


def _change_status(driver, w, frozen, talk, pages, claim):
    op = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": claim[1],
        "status": "in progress",
        "evidence_refs": supports("s1"),
        "reason": "the message states it",
    }
    apply(driver, w, [op], [said(talk, "s1", 4, at=frozen.now())])


def _end_then_reopen(driver, w, frozen, talk, pages, claim):
    end = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": claim[1],
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(frozen.now() - timedelta(minutes=1)),
        "evidence_refs": supports("x1"),
        "reason": "the message says the plan was dropped",
    }
    apply(driver, w, [end], [said(talk, "x1", 5, at=frozen.now())])
    ended = item_row(driver, w, claim[1])
    assert ended["end_class"] == "ended_in_the_world"
    frozen.advance(hours=1)
    reopen = {
        "op": "reopen_item",
        "op_id": new_op_id(),
        "item": claim[1],
        "reason": "the plan is back on",
        "recheck_by": DEFERRED,
    }
    apply(driver, w, [reopen], [])


def _attach_confirming_evidence(driver, w, frozen, talk, pages, claim):
    # Even a fresh confirming row attached outside a verify leaves the clock where it is.
    op = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": claim[1],
        "ev_id": "a1",
        "polarity": "supports",
    }
    # A new fetch of the page, stored first, gives the row it cites now.
    refetch = make_source(driver, w, "web_pages", PAGE_LINES)
    apply(driver, w, [op], [fetched(refetch, "a1", at=frozen.now())], caller=LEARNER)


def _end_evidence(driver, w, frozen, talk, pages, claim):
    rows = read(
        driver,
        w,
        "MATCH (e:Evidence {target_id: $id, active: true, source_type: 'confirmed'}) "
        "RETURN e.id AS id",
        id=claim[1],
    )
    assert rows
    op = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": rows[0]["id"],
        "end_reason": "correction",
        "reason": "the page was misread",
    }
    apply(driver, w, [op], [])


def _migrations_again(driver, w, frozen, talk, pages, claim):
    found = run_migrations(driver)
    assert found["applied"] == []


def _clock_advances(driver, w, frozen, talk, pages, claim):
    frozen.advance(days=90)


def _restate_link(driver, w, frozen, talk, pages, claim):
    [row] = read(
        driver,
        w,
        "MATCH (a:Item)-[l:LINK {id: $id}]->(b:Item) RETURN a.id AS a, b.id AS b",
        id=claim[1],
    )
    op = create_link(row["a"], "part of", row["b"], "r1")
    receipt = apply(driver, w, [op], [said(talk, "r1", 6, at=frozen.now())])
    # A restatement names the link that exists; it adds no second link.
    assert created_id(receipt, "=", "link") == claim[1]


ITEM_CHANGES = {
    "update_item": _update,
    "change_status": _change_status,
    "end_item_then_reopen_item": _end_then_reopen,
    "attach_evidence": _attach_confirming_evidence,
    "end_evidence": _end_evidence,
    "migrations_again": _migrations_again,
    "test_clock_advances": _clock_advances,
}


@pytest.mark.parametrize("change", list(ITEM_CHANGES))
def test_nothing_but_verify_moves_an_items_last_confirmed(
    driver, w, frozen, talk, pages, confirmed_task, change
):
    before = item_row(driver, w, confirmed_task[1])
    frozen.set(VERIFY_AT + timedelta(hours=2))
    ITEM_CHANGES[change](driver, w, frozen, talk, pages, confirmed_task)
    after = item_row(driver, w, confirmed_task[1])
    assert clocks(after) == clocks(before)
    assert confirmations_logged(driver, w, confirmed_task[1]) == 1


@pytest.mark.parametrize("change", ["restatement", "attach_evidence", "test_clock_advances"])
def test_nothing_but_verify_moves_a_links_last_confirmed(
    driver, w, frozen, talk, pages, confirmed_link, change
):
    before = link_row(driver, w, confirmed_link[1])
    frozen.set(VERIFY_AT + timedelta(hours=2))
    changes = {
        "restatement": _restate_link,
        "attach_evidence": _attach_confirming_evidence,
        "test_clock_advances": _clock_advances,
    }
    changes[change](driver, w, frozen, talk, pages, confirmed_link)
    after = link_row(driver, w, confirmed_link[1])
    assert clocks(after) == clocks(before)
    assert confirmations_logged(driver, w, confirmed_link[1]) == 1


def test_a_deferral_moves_recheck_by_and_leaves_last_confirmed(
    driver, w, frozen, pages, confirmed_task
):
    task = confirmed_task[1]
    before = item_row(driver, w, task)
    frozen.set(VERIFY_AT + timedelta(days=1))
    # The re-check fetches the page again; the fetch is stored first.
    refetch = make_source(driver, w, "web_pages", PAGE_LINES)
    piece = fetched(refetch, "d1", at=VERIFY_AT + timedelta(hours=3))
    judgment = judge(driver, w, f"item:{task}", piece, "not_about_claim")
    apply(
        driver,
        w,
        [verify(task, "d1", judgment, "deferred", recheck_by=DEFERRED)],
        [piece],
        caller=LEARNER,
    )
    after = item_row(driver, w, task)
    assert clocks(after) == clocks(before)
    assert utc(before["recheck_by"]) == datetime.fromisoformat(RENEWED)
    assert utc(after["recheck_by"]) == datetime.fromisoformat(DEFERRED)


def test_an_edit_moves_touched_at_and_no_clock(driver, w, frozen, talk, pages, confirmed_task):
    before = item_row(driver, w, confirmed_task[1])
    frozen.set(VERIFY_AT + timedelta(hours=2))
    _update(driver, w, frozen, talk, pages, confirmed_task)
    after = item_row(driver, w, confirmed_task[1])
    moved = {key for key in {*before, *after} if before.get(key) != after.get(key)}
    # Touched-at and the version are bookkeeping; the edit changes the summary and nothing that
    # measures freshness: no last confirmed, no confirmed against, no re-check by, no recorded.
    assert moved == {"summary", "touched_at", "version"}
    assert utc(after["touched_at"]) == VERIFY_AT + timedelta(hours=2)


# Last confirmed at create ---------------------------------------------------------------------


def test_a_claim_created_from_a_chat_message_has_an_empty_last_confirmed(driver, w, talk):
    hall = created_id(
        apply(driver, w, [create_item("organisation", "Okonkwo Hall", "c1")], [said(talk, "c1", 1)])
    )
    row = item_row(driver, w, hall)
    assert row.get("last_confirmed") is None and row.get("confirmed_against") is None
    assert read(driver, w, "MATCH (v:Verification {claim_id: $id}) RETURN v", id=hall) == []


def extraction_call(driver, w: str, run_id: str, at: datetime) -> str:
    return plant_node(
        driver,
        w,
        "ModelCall",
        model="extract-a",
        job="extraction",
        outcome="ok",
        run_id=run_id,
        at=at,
    )["id"]


def test_a_claim_created_from_a_page_fetched_in_this_pass_has_the_fetch_time(
    driver, w, frozen, pages
):
    # The page source was received at FETCHED, the fetch time; the extraction call of this run
    # read it.
    call = extraction_call(driver, w, "run-pass-1", FETCHED + timedelta(minutes=1))
    frozen.set(FETCHED + timedelta(minutes=2))
    page = fetched(pages, "f1", at=FETCHED)
    op = create_item("organisation", "Okonkwo Hall", "f1", confirmed_by_call=call)
    hall = created_id(apply(driver, w, [op], [page], caller=LEARNER, run_id="run-pass-1"))
    row = item_row(driver, w, hall)
    assert utc(row["last_confirmed"]) == FETCHED
    [source] = read(driver, w, "MATCH (s:Source {id: $id}) RETURN s", id=pages["source_id"])
    assert utc(row["last_confirmed"]) == utc(source["s"]["received_at"])
    [verification] = read(driver, w, "MATCH (v:Verification {claim_id: $id}) RETURN v", id=hall)
    assert row["confirmed_against"] == verification["v"]["id"]
    assert verification["v"]["disposition"] == "confirmed"


def test_a_confirmation_at_create_from_another_run_or_a_chat_line_is_refused(
    driver, w, frozen, talk, pages
):
    call = extraction_call(driver, w, "run-pass-1", FETCHED + timedelta(minutes=1))
    frozen.set(FETCHED + timedelta(minutes=2))
    page = fetched(pages, "f1", at=FETCHED)
    op = create_item("organisation", "Okonkwo Hall", "f1", confirmed_by_call=call)
    one(refused(driver, w, [op], [page], run_id="run-pass-2"), "unrecorded_verdict")
    chat_op = create_item("organisation", "Okonkwo Hall", "c1", confirmed_by_call=call)
    one(
        refused(driver, w, [chat_op], [said(talk, "c1", 1)], run_id="run-pass-1"),
        "not_confirmable",
        "creating_evidence",
    )


def test_a_load_bearing_claim_at_creation_is_refused_that_confirmation(driver, w, frozen, pages):
    call = extraction_call(driver, w, "run-pass-1", FETCHED + timedelta(minutes=1))
    frozen.set(FETCHED + timedelta(minutes=2))
    page = fetched(pages, "f1", at=FETCHED)
    ops = [
        create_item(
            "task",
            "Print the seating plan",
            "f1",
            status="open",
            recheck_by=RECHECK,
            temp_id="$print",
            confirmed_by_call=call,
        ),
        create_item(
            "task",
            "Hand out the seating plan",
            "f1",
            status="open",
            recheck_by=RECHECK,
            temp_id="$hand",
        ),
        # The batch makes the first task block the second: it is load-bearing at creation.
        create_link("$print", "blocks", "$hand", "f1", recheck_by=RECHECK),
    ]
    one(
        refused(driver, w, ops, [page], run_id="run-pass-1"),
        "second_judge_required",
        "load_bearing_at_create",
    )


# A claim about now carries a re-check by -------------------------------------------------------


def plant_state_property(driver, w: str, kind_id: str, name: str) -> None:
    """A property name of the state class: the starter set holds none."""
    plant_node(
        driver,
        w,
        "PropertyName",
        kind=kind_id,
        name=name,
        value_type="number",
        temporal_class="state",
        status="canonical",
        origin="minted",
        version=1,
    )


def screenshot_source(driver, w: str) -> dict:
    return make_source(driver, w, "screenshots", SCREENSHOT_LINES)


def boarding_pass(shots: dict, ev_id: str, at: datetime) -> dict:
    return observation(
        shots, ev_id, 1, at=at, locator="whole", statement_class="artifact", modality="image"
    )


def test_a_work_item_at_a_live_status_without_a_recheck_is_refused(driver, w, talk):
    op = create_item("task", "Draft the seating plan", "c1", status="open")
    one(refused(driver, w, [op], [said(talk, "c1", 1)]), "missing_recheck", "claim_about_now")


def test_a_bounded_period_that_began_and_has_not_ended_without_a_recheck_is_refused(
    driver, w, frozen
):
    shots = screenshot_source(driver, w)
    op = create_item(
        "trip", "Ilse's trip to Velmora", "b1", became_true=clock_at(NOW - timedelta(hours=3))
    )
    refusals = refused(driver, w, [op], [boarding_pass(shots, "b1", NOW - timedelta(hours=3))])
    one(refusals, "missing_recheck", "claim_about_now")


def test_a_record_with_a_state_property_without_a_recheck_is_refused(driver, w, seeded, talk):
    plant_state_property(driver, w, seeded["kinds"]["observation"], "queue length")
    op = create_item(
        "observation", "The queue at Okonkwo Hall", "c1", properties={"queue length": 41}
    )
    one(refused(driver, w, [op], [said(talk, "c1", 1)]), "missing_recheck", "claim_about_now")
    # With a re-check by, the same record is accepted.
    accepted = create_item(
        "observation",
        "The queue at Okonkwo Hall",
        "c1",
        properties={"queue length": 41},
        recheck_by=RECHECK,
    )
    apply(driver, w, [accepted], [said(talk, "c1", 1)])


def test_a_link_of_a_state_meaning_without_a_recheck_is_refused(driver, w, talk):
    ops = [
        create_item("person", "Ilse Varga", "c1", temp_id="$ilse"),
        create_item("place", "Velmora", "c1", temp_id="$velmora"),
        create_link("$ilse", "lives in", "$velmora", "c1"),
    ]
    one(refused(driver, w, ops, [said(talk, "c1", 1)]), "missing_recheck", "state_meaning")


def test_a_link_of_a_permanent_meaning_is_accepted_without_a_horizon(driver, w, talk):
    ops = [
        create_item("organisation", "Okonkwo Hall", "c1", temp_id="$hall"),
        create_item("organisation", "Okonkwo Hall Trust", "c1", temp_id="$trust"),
        create_link("$hall", "part of", "$trust", "c1"),
    ]
    link = created_id(apply(driver, w, ops, [said(talk, "c1", 1)]), ">", "link")
    assert link_row(driver, w, link).get("recheck_by") is None


def test_a_stopped_being_true_earlier_than_became_true_is_refused(driver, w, frozen, talk):
    op = create_item(
        "organisation", "Okonkwo Hall", "c1", became_true=clock_at(NOW - timedelta(days=10), "day")
    )
    hall = created_id(apply(driver, w, [op], [said(talk, "c1", 1)]))
    end = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": hall,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(NOW - timedelta(days=40), "day"),
        "evidence_refs": supports("x1"),
        "reason": "the message says the hall closed",
    }
    one(refused(driver, w, [end], [said(talk, "x1", 2)]), "end_before_start", "end_before_start")


@pytest.mark.parametrize(("kind", "status"), [("task", "open"), ("organisation", None)])
def test_a_state_property_on_a_work_item_or_a_lasting_thing_is_refused(
    driver, w, seeded, talk, kind, status
):
    plant_state_property(driver, w, seeded["kinds"][kind], "head count")
    fields: dict[str, Any] = {"properties": {"head count": 12}, "recheck_by": RECHECK}
    if status:
        fields["status"] = status
    op = create_item(kind, "The Okonkwo Hall dinner team", "c1", **fields)
    one(
        refused(driver, w, [op], [said(talk, "c1", 1)]),
        "state_property_on_item",
        "state_property_on_item",
    )


def test_a_record_with_two_state_properties_is_refused(driver, w, seeded, talk):
    plant_state_property(driver, w, seeded["kinds"]["observation"], "queue length")
    plant_state_property(driver, w, seeded["kinds"]["observation"], "seats free")
    op = create_item(
        "observation",
        "The queue at Okonkwo Hall",
        "c1",
        properties={"queue length": 41, "seats free": 3},
        recheck_by=RECHECK,
    )
    one(refused(driver, w, [op], [said(talk, "c1", 1)]), "state_property_on_item", "one_per_record")


# The plan horizon -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # An instant with its offset: 12 July 09:00 in Dubai is 05:00 UTC; plus one day.
        ("2027-07-12T09:00:00+04:00", datetime(2027, 7, 13, 5, 0, tzinfo=UTC)),
        # A bare date is a day: its latest instant is the day's end in UTC; plus one day.
        ("2027-07-12", datetime(2027, 7, 14, 0, 0, tzinfo=UTC) - timedelta(microseconds=1)),
        # A clock: its latest bound; plus one day.
        (
            {
                "earliest": "2027-07-12T00:00:00+00:00",
                "latest": "2027-07-12T23:59:59+00:00",
                "precision": "day",
            },
            datetime(2027, 7, 13, 23, 59, 59, tzinfo=UTC),
        ),
    ],
)
def test_a_plan_with_a_scheduled_time_and_no_recheck_gets_the_scheduled_time_plus_one_day(
    driver, w, talk, value, expected
):
    op = create_item(
        "plan",
        "Ilse flies to Velmora",
        "c1",
        status="active",
        properties={"scheduled for": value},
    )
    plan = created_id(apply(driver, w, [op], [said(talk, "c1", 1)]))
    assert utc(item_row(driver, w, plan)["recheck_by"]) == expected


def test_a_plan_keeps_a_recheck_by_its_caller_sent(driver, w, talk):
    op = create_item(
        "plan",
        "Ilse flies to Velmora",
        "c1",
        status="active",
        properties={"scheduled for": "2027-07-12"},
        recheck_by=RECHECK,
    )
    plan = created_id(apply(driver, w, [op], [said(talk, "c1", 1)]))
    assert utc(item_row(driver, w, plan)["recheck_by"]) == datetime.fromisoformat(RECHECK)


def test_a_scheduled_value_that_is_not_a_time_is_refused(driver, w, talk):
    op = create_item(
        "plan", "Ilse flies to Velmora", "c1", status="active", properties={"scheduled for": "soon"}
    )
    one(refused(driver, w, [op], [said(talk, "c1", 1)]), "invalid_arguments", "scheduled_value")


def test_a_backfilled_plan_from_a_message_nine_months_old_arrives_already_due(
    driver, w, frozen, talk
):
    # Recorded is the read day (NOW); became true and the horizon come from the message.
    sent = NOW - timedelta(days=270)
    friday = resolve("on friday", anchor=sent, timezone="Asia/Dubai")
    op = create_item(
        "plan",
        "Rowan books the Okonkwo Hall caterer",
        "c1",
        status="active",
        became_true=clock_at(sent),
        properties={"scheduled for": friday.to_json()},
    )
    plan = created_id(apply(driver, w, [op], [said(talk, "c1", 1, at=sent)]))
    row = item_row(driver, w, plan)
    assert utc(row["recorded"]) == NOW
    assert utc(row["became_true_earliest"]) == sent
    assert utc(row["recheck_by"]) == friday.latest + timedelta(days=1)
    assert utc(row["recheck_by"]) < NOW  # already due on arrival
    assert row.get("last_confirmed") is None


# The period rule --------------------------------------------------------------------------------


def test_a_trips_start_cannot_be_written_from_a_calendar_entry(driver, w, frozen):
    calendar = make_source(driver, w, "calendar", CALENDAR_LINES)
    entry = observation(
        calendar, "c1", 1, at=NOW - timedelta(days=1), locator="whole", statement_class="intention"
    )
    op = create_item(
        "trip",
        "Ilse's trip to Velmora",
        "c1",
        became_true=clock_at(NOW - timedelta(days=1), "day"),
        recheck_by=RECHECK,
    )
    one(
        refused(driver, w, [op], [entry]),
        "terminal_needs_confirmation",
        "period_start_needs_confirmation",
    )


def test_a_trips_start_cannot_be_written_from_a_chat_line(driver, w, frozen, talk):
    op = create_item(
        "trip",
        "Ilse's trip to Velmora",
        "c1",
        became_true=clock_at(NOW - timedelta(hours=2)),
        recheck_by=RECHECK,
    )
    line = said(talk, "c1", 1, statement_class="report_happened")
    one(
        refused(driver, w, [op], [line]),
        "terminal_needs_confirmation",
        "period_start_needs_confirmation",
    )
    # Nor through an update of a trip that has no start yet.
    trip = created_id(
        apply(
            driver, w, [create_item("trip", "Ilse's trip to Velmora", "c2")], [said(talk, "c2", 2)]
        )
    )
    update = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": trip,
        "version": item_row(driver, w, trip)["version"],
        "fields": {"became_true": clock_at(NOW - timedelta(hours=2)), "recheck_by": RECHECK},
        "evidence_refs": supports("c3"),
        "reason": "the message says she landed",
    }
    one(
        refused(driver, w, [update], [said(talk, "c3", 3, statement_class="report_happened")]),
        "terminal_needs_confirmation",
        "period_start_needs_confirmation",
    )
    assert item_row(driver, w, trip)["became_true_precision"] == "unknown"


def test_a_trips_start_can_be_written_from_a_boarding_pass(driver, w, frozen):
    shots = screenshot_source(driver, w)
    boarded = NOW - timedelta(hours=3)
    op = create_item(
        "trip", "Ilse's trip to Velmora", "b1", became_true=clock_at(boarded), recheck_by=RECHECK
    )
    trip = created_id(apply(driver, w, [op], [boarding_pass(shots, "b1", boarded)]))
    row = item_row(driver, w, trip)
    assert utc(row["became_true_earliest"]) == boarded
    assert (row["derived_source_type"], row["derivation"], row["modality"]) == (
        "confirmed",
        "observed",
        "image",
    )
