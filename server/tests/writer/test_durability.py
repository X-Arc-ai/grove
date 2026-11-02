"""D2 "Durability": a pass killed as a real process still yields exactly one effect.

Spec line 774: "Durability: killing the caller after the enqueue, and again mid-apply, still
yields exactly one effect, whose receipt matches the rows." The pass runs in a child process
(tests/writer/harness.py) that sends itself SIGKILL from inside a seam: right after
enqueue_request, inside the open transaction (`apply._before_commit`), and after the commit before
the receipt (`api._after_commit`). A fresh worker in this process then finishes the request, and
one more replay changes nothing.
"""

import signal
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.writer import (
    LEASE_SECONDS,
    Receipt,
    apply_request,
    committed_for_request,
    request_state,
)
from grove.writer.requests import batch_fingerprint
from tests.writer.harness import (
    ONE_EFFECT,
    SOURCE_LINES,
    Killed,
    direct_apply,
    extract,
    run_killed,
    work_one,
)
from tests.writer.helpers import (
    LEARNER,
    make_source,
    raw,
    read,
    request_key,
    request_row,
    world_dump,
)

T = datetime(2031, 3, 4, 9, 0, tzinfo=UTC)
NOTHING = {"items": 0, "links": 0, "evidence": 0}
TITLES = sorted(line.partition(": ")[2] for line in SOURCE_LINES if not line.startswith("link"))
# Every change entry one pass writes: a create entry per row, and nothing else.
ENTRIES = {
    ("item", "row"): ONE_EFFECT["items"],
    ("link", "row"): ONE_EFFECT["links"],
    ("evidence", "row"): ONE_EFFECT["evidence"],
}


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(T)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def remembered(driver, seeded):
    """The request's words, stored once as a source of the AI host's connector."""
    return make_source(driver, seeded["world_id"], "ai_host", SOURCE_LINES)


def assert_killed(killed: Killed, seam: str) -> dict[str, Any]:
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    [report] = killed.reports
    assert report["seam"] == seam
    return report


def count(driver, world_id: str, cypher: str) -> int:
    return read(driver, world_id, cypher)[0]["n"]


def sequence(driver, world_id: str) -> int:
    return raw(driver, "MATCH (w:World {id: $w}) RETURN w.next_sequence AS n", w=world_id)[0]["n"]


def effect(driver, world_id: str) -> dict[str, int]:
    """What the world holds of the request: the source and the starter set write none of it."""
    return {
        "items": count(driver, world_id, "MATCH (i:Item) RETURN count(i) AS n"),
        "links": count(driver, world_id, "MATCH (:Item)-[l:LINK]->(:Item) RETURN count(l) AS n"),
        "evidence": count(driver, world_id, "MATCH (e:Evidence) RETURN count(e) AS n"),
    }


def assert_one_effect(
    driver, world_id: str, request_id: str, receipt: Receipt, *, attempts: int, start: int
) -> None:
    """`start` is the world's next_sequence before the first pass ran."""
    assert effect(driver, world_id) == ONE_EFFECT
    titles = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t ORDER BY t")
    assert [row["t"] for row in titles] == TITLES

    # One commit: every change entry of the request, and every evidence row, is of that commit.
    entries = committed_for_request(driver, world_id, request_id)
    assert {(entry["commit_id"], entry["change_set_id"]) for entry in entries} == {
        (receipt.commit_id, receipt.change_set_id)
    }
    assert Counter((entry["entity_type"], entry["field"]) for entry in entries) == ENTRIES
    # A killed pass took sequences in its open transaction: the rollback gave every one back.
    assert [entry["sequence"] for entry in entries] == list(
        range(start + 1, start + 1 + len(entries))
    )
    assert sequence(driver, world_id) == start + len(entries)
    others = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry) WHERE c.request_id IS NOT NULL "
        "RETURN DISTINCT c.change_set_id AS id",
    )
    assert [row["id"] for row in others] == [receipt.change_set_id]
    stamped = read(driver, world_id, "MATCH (e:Evidence) RETURN DISTINCT e.request_id AS id")
    assert [row["id"] for row in stamped] == [request_id]

    # The receipt matches the rows: one line per item and per link, each naming a stored row.
    items = {row["id"] for row in read(driver, world_id, "MATCH (i:Item) RETURN i.id AS id")}
    links = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK]->(b:Item) MATCH (m:Meaning {id: l.meaning}) "
        "RETURN l.id AS id, a.title AS a, m.name AS meaning, b.title AS b",
    )
    assert {line.entity_id for line in receipt.lines if line.symbol == "+"} == items
    assert {line.entity_id for line in receipt.lines if line.symbol == ">"} == {
        row["id"] for row in links
    }
    assert len(receipt.lines) == ONE_EFFECT["items"] + ONE_EFFECT["links"]
    assert sorted((row["a"], row["meaning"], row["b"]) for row in links) == [
        ("Book the venue for the Okonkwo offsite", "assigned to", "Tamsin"),
        (
            "Print the name cards for Rowan and Ilse",
            "part of",
            "Book the venue for the Okonkwo offsite",
        ),
    ]

    row = request_row(driver, world_id, request_id)
    assert row["status"] == "saved"
    assert (row["commit_id"], row["change_set_id"]) == (receipt.commit_id, receipt.change_set_id)
    assert row["attempts"] == attempts


def replay(driver, world_id: str, request_id: str, source_id: str, mode: str) -> Receipt:
    """A second delivery of the same request: the same id and a fresh extraction."""
    if mode == "direct":
        return direct_apply(driver, world_id, source_id, request_id)
    state = apply_request(
        driver, world_id, LEARNER, request_id, extract(driver, world_id, source_id)
    )
    assert state.api_state == "saved"
    return state.receipt


def assert_a_replay_changes_nothing(
    driver, world_id: str, request_id: str, source_id: str, mode: str, receipt: Receipt
) -> None:
    # The extraction is a pure function of the stored source: a later pass proposes the batch
    # that committed, so the replay below meets the saved fingerprint, not request_key_reused.
    stored = request_row(driver, world_id, request_id)["fingerprint"]
    assert batch_fingerprint(extract(driver, world_id, source_id)) == stored
    before = world_dump(driver, world_id)
    assert work_one(driver, world_id, worker="late-worker") is None
    assert replay(driver, world_id, request_id, source_id, mode) == receipt
    assert world_dump(driver, world_id) == before


# (a) killed right after the enqueue -----------------------------------------------------------


def test_a_kill_right_after_the_enqueue_leaves_a_queued_request_that_saves_once(
    driver, seeded, remembered, frozen
):
    world_id, source_id, request_id = seeded["world_id"], remembered["source_id"], request_key()
    start = sequence(driver, world_id)

    killed = run_killed("after_enqueue", "worker", world_id, source_id, request_id, T)

    assert_killed(killed, "after_enqueue")
    row = request_row(driver, world_id, request_id)
    assert (row["status"], row["attempts"], row["source_id"]) == ("queued", 0, source_id)
    assert effect(driver, world_id) == NOTHING
    assert request_state(driver, world_id, request_id).api_state == "saving"

    state = work_one(driver, world_id, worker="fresh-worker")

    assert state.api_state == "saved"
    assert_one_effect(driver, world_id, request_id, state.receipt, attempts=1, start=start)
    assert_a_replay_changes_nothing(
        driver, world_id, request_id, source_id, "worker", state.receipt
    )


# (b) killed mid-apply, inside the open transaction ---------------------------------------------


def test_a_worker_killed_inside_the_open_transaction_is_rolled_back_and_its_lease_brings_it_back(
    driver, seeded, remembered, frozen
):
    world_id, source_id, request_id = seeded["world_id"], remembered["source_id"], request_key()
    start = sequence(driver, world_id)

    killed = run_killed("before_commit", "worker", world_id, source_id, request_id, T)

    # The kill came with the batch and its `saved` written in the open transaction.
    report = assert_killed(killed, "before_commit")
    assert (report["items"], report["links"], report["status"]) == (3, 2, "saved")
    assert effect(driver, world_id) == NOTHING
    row = request_row(driver, world_id, request_id)
    assert (row["status"], row["attempts"], row["claimed_by"]) == ("applying", 1, "killed-worker")
    assert row["lease_until"] == T + timedelta(seconds=LEASE_SECONDS)
    # A live lease is the dead pass's until it runs out: no second pass runs beside it.
    assert work_one(driver, world_id, worker="fresh-worker") is None

    frozen.set(T + timedelta(seconds=LEASE_SECONDS + 1))
    state = work_one(driver, world_id, worker="fresh-worker")

    assert state.api_state == "saved"
    assert_one_effect(driver, world_id, request_id, state.receipt, attempts=2, start=start)
    assert_a_replay_changes_nothing(
        driver, world_id, request_id, source_id, "worker", state.receipt
    )


def test_a_direct_caller_killed_inside_the_open_transaction_leaves_no_row_and_its_retry_saves_once(
    driver, seeded, remembered, frozen
):
    world_id, source_id, request_id = seeded["world_id"], remembered["source_id"], request_key()
    start = sequence(driver, world_id)

    killed = run_killed("before_commit", "direct", world_id, source_id, request_id, T)

    report = assert_killed(killed, "before_commit")
    assert (report["items"], report["links"], report["status"]) == (3, 2, "saved")
    assert effect(driver, world_id) == NOTHING
    assert request_row(driver, world_id, request_id) is None

    receipt = direct_apply(driver, world_id, source_id, request_id)

    assert_one_effect(driver, world_id, request_id, receipt, attempts=1, start=start)
    assert_a_replay_changes_nothing(driver, world_id, request_id, source_id, "direct", receipt)


# (c) killed after the commit, before the receipt -----------------------------------------------


@pytest.mark.parametrize("mode", ["worker", "direct"])
def test_a_kill_after_the_commit_leaves_saved_and_no_pass_runs_again(
    driver, seeded, remembered, frozen, mode
):
    world_id, source_id, request_id = seeded["world_id"], remembered["source_id"], request_key()
    start = sequence(driver, world_id)

    killed = run_killed("after_commit", mode, world_id, source_id, request_id, T)

    assert_killed(killed, "after_commit")
    row = request_row(driver, world_id, request_id)
    assert row["status"] == "saved" and row.get("receipt") is None
    # The state reads "saved" from the change set and writes nothing; no claim takes the row.
    before = world_dump(driver, world_id)
    state = request_state(driver, world_id, request_id)
    assert state.api_state == "saved"
    frozen.set(T + timedelta(seconds=LEASE_SECONDS + 1))
    assert work_one(driver, world_id, worker="fresh-worker") is None
    assert world_dump(driver, world_id) == before
    assert_one_effect(driver, world_id, request_id, state.receipt, attempts=1, start=start)

    # A second delivery reads the receipt and attaches it; one more changes nothing.
    receipt = replay(driver, world_id, request_id, source_id, mode)
    assert receipt == state.receipt
    assert request_row(driver, world_id, request_id)["receipt"]
    assert_a_replay_changes_nothing(driver, world_id, request_id, source_id, mode, receipt)
