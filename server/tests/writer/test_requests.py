"""D2 durable queue: the request row, its state machine and the replay rules (S41).

Spec, D2 "The durable queue": "A remember request is written to a requests table, with an
idempotency key, before any model is called. Its states are queued, applying, saved (receipt
attached), declined (reasons attached) and failed (retried with back-off, then surfaced in
health)." One request commits once: `saved` moves in the same transaction as that commit.
"""

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from neo4j._sync.work.transaction import TransactionBase
from neo4j.exceptions import DatabaseError, IncompleteCommit, Neo4jError, ServiceUnavailable

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id
from grove.writer import (
    BACKOFF_SECONDS,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    BatchDeclined,
    Caller,
    Receipt,
    ReceiptUnavailable,
    RequestState,
    WriteFailed,
    WriteOutcomeUnknown,
    apply_batch,
    apply_request,
    claim_next_request,
    committed_for_request,
    enqueue_request,
    fail_request,
    finish_request,
    pending_requests,
    render_receipt,
    request_state,
    short_id,
    surfaced_requests,
    worlds_with_due_requests,
    write_request_row,
)
from grove.writer import api as api_module
from grove.writer import apply as apply_module
from grove.writer import requests as requests_module
from grove.writer.testing import plant_node, purge_test_world
from grove.writer.tx import UnknownWorld, WorldTx, WorldWriter
from grove.writer.worlds import create_world
from tests.writer.helpers import (
    CHAT_LINES,
    HOST,
    LEARNER,
    PERSON,
    READ_ONLY,
    NoStore,
    batch,
    count_rows,
    create_item,
    make_source,
    observation,
    raw,
    read,
    request_key,
    request_row,
    stale_update,
    unknown_kind,
    world_dump,
)

T = datetime(2031, 3, 4, 9, 0, tzinfo=UTC)


class Killed(Exception):
    """Stands for a process dying at a seam: nothing after the raise runs."""


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(T)
    with clock.using(test_clock):
        yield test_clock


key = request_key
row_of = request_row


def one_item(chat: dict[str, str], title: str = "Rowan", line: int = 1, **fields: Any) -> dict:
    ev = f"e{line}"
    return batch([create_item("person", title, ev, **fields)], [observation(chat, ev, line)])


def titles(driver, world_id: str) -> list[str]:
    return sorted(row["t"] for row in read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t"))


def item_at_version_two(driver, world_id: str, chat: dict[str, str]) -> str:
    receipt = apply_batch(driver, world_id, PERSON, one_item(chat, "Tamsin", 2))
    item_id = receipt.lines[0].entity_id
    apply_batch(driver, world_id, PERSON, stale_update(chat, item_id, 3))
    return item_id


def enqueued(driver, world_id: str, chat: dict[str, str], request_id: str | None = None) -> str:
    request_id = request_id or key()
    enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])
    return request_id


def claimed(driver, world_id: str, chat: dict[str, str], worker: str = "pass-1") -> str:
    request_id = enqueued(driver, world_id, chat)
    claim = claim_next_request(driver, world_id, LEARNER, worker=worker)
    assert claim is not None and claim.id == request_id
    return request_id


def shape_fault(chat: dict[str, str]) -> dict:
    return batch(
        [{"op": "create_item", "op_id": "o-shape", "kind": "person"}],
        [observation(chat, "e1", 1)],
    )


def source_writer(chat: dict[str, str], calls: list[int], before=None):
    """What screening plugs in: writes one source inside the enqueue's transaction."""

    def write(tx: WorldTx) -> str:
        calls.append(1)
        if before is not None:
            before()
        source_id = new_id(tx.world_id)
        at = clock.now(tx.world_id)
        tx.run(
            "CREATE (s:Source) SET s += $row",
            row={
                "id": source_id,
                "connector_id": chat["connector_id"],
                "external_locator": f"remember-{source_id}",
                "received_at": at,
                "content": "an invented remember about the venue",
                "parser_version": "test-1",
                "untrusted": False,
                "forget_state": "kept",
                "version": 1,
                "touched_at": at,
            },
        )
        return source_id

    return write


# Enqueue --------------------------------------------------------------------------------------


def test_an_enqueue_writes_a_queued_row_that_reads_saving(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    request_id = key()

    state = enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])

    assert state.status == "queued"
    assert state.api_state == "saving"
    assert state.receipt is None
    assert state.source_id == chat["source_id"]
    assert state.to_api() == {"state": "saving", "request_id": request_id}
    row = row_of(driver, world_id, request_id)
    assert row["caller"] == HOST.actor
    assert row["attempts"] == 0 and row["repairs"] == 0 and row["version"] == 1
    assert row["created_at"] == T and row["touched_at"] == T
    assert row["content_key"] == chat["source_id"]
    assert "fresh_marker" not in row


def test_a_second_enqueue_with_the_same_content_writes_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    before = world_dump(driver, world_id)

    state = enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])

    assert state.status == "queued"
    assert world_dump(driver, world_id) == before


def test_the_words_are_stored_once(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    calls: list[int] = []
    sources = count_rows(driver, world_id)["Source"]

    first = enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        write_source=source_writer(chat, calls),
        content_key="hash-of-the-words",
    )
    second = enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        write_source=source_writer(chat, calls),
        content_key="hash-of-the-words",
    )

    assert calls == [1]
    assert count_rows(driver, world_id)["Source"] == sources + 1
    assert second.source_id == first.source_id
    assert read(
        driver, world_id, "MATCH (s:Source {id: $id}) RETURN s.id AS id", id=first.source_id
    )


def test_concurrent_enqueues_give_one_row_and_one_source(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    sources = count_rows(driver, world_id)["Source"]
    inside, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def hold() -> None:
        inside.set()
        assert release.wait(60)

    results: dict[str, RequestState] = {}

    def enqueue(name: str, before=None) -> None:
        results[name] = enqueue_request(
            driver,
            world_id,
            HOST,
            request_id,
            write_source=source_writer(chat, calls, before),
            content_key="hash-of-the-words",
        )

    first = threading.Thread(target=enqueue, args=("first", hold))
    first.start()
    assert inside.wait(60)
    second = threading.Thread(target=enqueue, args=("second",))
    second.start()
    second.join(3.0)
    waited = second.is_alive()
    release.set()
    first.join(90)
    second.join(90)

    assert waited, "the second enqueue did not wait for the row of the first"
    assert calls == [1]
    assert results["first"].source_id == results["second"].source_id
    assert count_rows(driver, world_id)["Source"] == sources + 1
    assert len(read(driver, world_id, "MATCH (r:Request) RETURN r.id AS id")) == 1


def test_other_content_under_a_used_key_is_refused_and_the_row_stays(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    calls: list[int] = []
    enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        write_source=source_writer(chat, calls),
        content_key="hash-of-the-words",
    )
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as refused:
        enqueue_request(
            driver,
            world_id,
            HOST,
            request_id,
            write_source=source_writer(chat, calls),
            content_key="hash-of-other-words",
        )

    assert [(r.code, r.rule) for r in refused.value.refusals] == [
        ("invalid_arguments", "request_key_reused")
    ]
    assert calls == [1]
    assert world_dump(driver, world_id) == before


def test_an_enqueue_under_a_key_a_direct_batch_used_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)

    with pytest.raises(BatchDeclined) as refused:
        enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])

    assert refused.value.refusals[0].rule == "request_key_reused"


def test_an_unknown_source_is_not_found_and_leaves_no_row(driver, seeded):
    world_id = seeded["world_id"]
    request_id = key()
    before = count_rows(driver, world_id)

    with pytest.raises(BatchDeclined) as refused:
        enqueue_request(driver, world_id, HOST, request_id, source_id=new_id(world_id))

    assert refused.value.refusals[0].code == "not_found"
    assert count_rows(driver, world_id) == before


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="neither"),
        pytest.param({"source_id": "s1", "write_source": lambda tx: "s1"}, id="both"),
        pytest.param({"write_source": lambda tx: "s1"}, id="write_source_with_no_key"),
        pytest.param({"source_id": "s1", "content_key": "k"}, id="source_id_with_a_key"),
        pytest.param({"source_id": "s1", "said_at": "not a time"}, id="said_at"),
        pytest.param({"source_id": "s1", "about": "one id"}, id="about"),
        pytest.param({"source_id": "s1", "verbatim": "yes"}, id="verbatim"),
        pytest.param({"source_id": "s1", "host_message_id": " "}, id="host_message_id"),
    ],
)
def test_an_enqueue_with_bad_arguments_never_reaches_the_store(arguments):
    world_id = f"t_writer-a2_{uuid4()}"
    with pytest.raises(ValueError):
        enqueue_request(NoStore(), world_id, HOST, key(), **arguments)


def test_write_request_row_commits_with_the_callers_transaction(driver, seeded, chat):
    world_id = seeded["world_id"]
    kept, dropped = key(), key()
    writer = WorldWriter(driver, world_id)

    def rolled_back(tx: WorldTx) -> None:
        write_request_row(tx, HOST, dropped, source_id=chat["source_id"])
        raise Killed

    with pytest.raises(Killed):
        writer.transaction(rolled_back)
    assert row_of(driver, world_id, dropped) is None

    def committed(tx: WorldTx) -> tuple[dict, bool]:
        return write_request_row(tx, HOST, kept, source_id=chat["source_id"], run_id="run-1")

    row, fresh = writer.transaction(committed)
    assert fresh and row["source_id"] == chat["source_id"] and row["run_id"] == "run-1"
    before = world_dump(driver, world_id)

    def again(tx: WorldTx) -> tuple[dict, bool]:
        return write_request_row(tx, HOST, kept, source_id=chat["source_id"])

    row, fresh = writer.transaction(again)
    assert not fresh and row["id"] == kept
    assert world_dump(driver, world_id) == before


def test_the_remember_fields_survive_to_the_claim(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    said = datetime(2026, 10, 28, 9, 15, tzinfo=UTC)
    enqueue_request(
        driver,
        world_id,
        HOST,
        request_id,
        source_id=chat["source_id"],
        verbatim=True,
        host_message_id="msg-77",
        said_at=said.isoformat(),
        about=["id-of-rowan", "id-of-ilse"],
        run_id="run-9",
    )

    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-1")

    assert claim.verbatim is True
    assert claim.host_message_id == "msg-77"
    assert claim.said_at == said
    assert claim.about == ["id-of-rowan", "id-of-ilse"]
    assert claim.run_id == "run-9"
    assert claim.source_id == chat["source_id"]
    assert claim.caller == HOST.actor
    assert claim.attempts == 1


# Claim ----------------------------------------------------------------------------------------


def test_a_claim_moves_the_oldest_due_row_to_applying(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    older = enqueued(driver, world_id, chat)
    frozen.advance(seconds=1)
    newer = enqueued(driver, world_id, chat)
    version = row_of(driver, world_id, older)["version"]

    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-1", lease_seconds=60)

    assert claim.id == older
    row = row_of(driver, world_id, older)
    assert row["status"] == "applying"
    assert row["attempts"] == 1
    assert row["version"] == version + 1
    assert row["claimed_by"] == "pass-1"
    assert row["lease_until"] == T + timedelta(seconds=61)
    assert row_of(driver, world_id, newer)["status"] == "queued"
    assert request_state(driver, world_id, older).api_state == "saving"


def test_two_racing_claims_give_one_winner(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    paused, release = threading.Event(), threading.Event()
    original = requests_module._claim_one

    def gated(tx, *args, **kwargs):
        found = original(tx, *args, **kwargs)
        if threading.current_thread().name == "first" and not paused.is_set():
            paused.set()
            assert release.wait(60)
        return found

    monkeypatch.setattr(requests_module, "_claim_one", gated)
    results: dict[str, Any] = {}

    def claim(worker: str) -> None:
        results[worker] = claim_next_request(driver, world_id, LEARNER, worker=worker)

    first = threading.Thread(target=claim, args=("pass-1",), name="first")
    first.start()
    assert paused.wait(60)
    second = threading.Thread(target=claim, args=("pass-2",), name="second")
    second.start()
    second.join(3.0)
    waited = second.is_alive()
    release.set()
    first.join(90)
    second.join(90)

    assert waited, "the second claim did not wait for the row's lock"
    assert results["pass-1"].id == request_id
    assert results["pass-2"] is None
    row = row_of(driver, world_id, request_id)
    assert row["attempts"] == 1
    assert row["claimed_by"] == "pass-1"


def test_a_loser_of_a_claim_moves_on_to_the_next_row(driver, seeded, chat, frozen, monkeypatch):
    world_id = seeded["world_id"]
    busy = enqueued(driver, world_id, chat)
    frozen.advance(seconds=1)
    free = enqueued(driver, world_id, chat)
    original = requests_module._claim_one

    def lost_the_first(tx, request_id, *args):
        if request_id == busy:
            raise Neo4jError._hydrate_neo4j(
                code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
                message="injected: the lock wait ended",
            )
        return original(tx, request_id, *args)

    monkeypatch.setattr(requests_module, "_claim_one", lost_the_first)

    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-1")

    assert claim.id == free
    assert row_of(driver, world_id, busy)["status"] == "queued"


def test_a_store_fault_during_a_claim_is_a_write_failure(driver, seeded, chat, monkeypatch):
    # A fault that is no lock wait must not read as "nothing due".
    world_id = seeded["world_id"]
    enqueued(driver, world_id, chat)

    def broken(*args):
        raise DatabaseError("injected: the store fault")

    monkeypatch.setattr(requests_module, "_claim_one", broken)

    with pytest.raises(WriteFailed):
        claim_next_request(driver, world_id, LEARNER, worker="pass-1")


def test_a_claim_with_nothing_due_writes_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    before = world_dump(driver, world_id)

    assert claim_next_request(driver, world_id, LEARNER, worker="pass-1") is None

    assert world_dump(driver, world_id) == before


def test_a_lease_brings_a_killed_pass_back(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat, "pass-1")

    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2") is None
    assert pending_requests(driver, world_id) == []
    assert request_state(driver, world_id, request_id).api_state == "saving"

    frozen.advance(seconds=LEASE_SECONDS - 1)
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2") is None

    frozen.advance(seconds=1)
    assert [state.request_id for state in pending_requests(driver, world_id)] == [request_id]
    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-2")
    assert claim.id == request_id
    assert claim.attempts == 2

    with pytest.raises(BatchDeclined) as lost:
        finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")
    assert [(r.code, r.rule) for r in lost.value.refusals] == [("invalid_arguments", "lease_lost")]
    assert row_of(driver, world_id, request_id)["claimed_by"] == "pass-2"


def test_a_finish_that_races_a_new_claim_waits_and_loses_the_lease(
    driver, seeded, chat, frozen, monkeypatch
):
    # With no lock before the holder check, the old worker reads its own name and saves over the
    # claim of the new one.
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat, "pass-1")
    frozen.advance(seconds=LEASE_SECONDS)
    paused, release = threading.Event(), threading.Event()
    original = requests_module._claim_one

    def gated(tx, *args, **kwargs):
        found = original(tx, *args, **kwargs)
        paused.set()
        assert release.wait(60)
        return found

    monkeypatch.setattr(requests_module, "_claim_one", gated)
    outcome: dict[str, Any] = {}

    def finish() -> None:
        try:
            outcome["state"] = finish_request(
                driver, world_id, LEARNER, request_id, worker="pass-1"
            )
        except BatchDeclined as declined:
            outcome["declined"] = declined

    claimer = threading.Thread(
        target=lambda: claim_next_request(driver, world_id, LEARNER, worker="pass-2")
    )
    claimer.start()
    assert paused.wait(60)
    finisher = threading.Thread(target=finish)
    finisher.start()
    finisher.join(3.0)
    waited = finisher.is_alive()
    release.set()
    claimer.join(90)
    finisher.join(90)

    assert waited, "the finish did not wait for the row's lock"
    assert outcome["declined"].refusals[0].rule == "lease_lost"
    row = row_of(driver, world_id, request_id)
    assert row["status"] == "applying"
    assert row["claimed_by"] == "pass-2"


def test_only_a_row_an_enqueue_wrote_can_be_claimed(driver, seeded, chat):
    world_id = seeded["world_id"]
    item_id = item_at_version_two(driver, world_id, chat)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, stale_update(chat, item_id), request_id=key())
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=key())
    plant_node(driver, world_id, "Request", id=key(), status="queued")
    plant_node(driver, world_id, "Request", id=key(), status="applying")

    assert claim_next_request(driver, world_id, LEARNER, worker="pass-1") is None
    assert pending_requests(driver, world_id) == []


def test_an_applying_row_with_no_lease_is_due(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    plant_node(
        driver,
        world_id,
        "Request",
        id=request_id,
        status="applying",
        source_id=chat["source_id"],
        attempts=1,
    )

    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-1")

    assert claim.id == request_id
    assert claim.attempts == 2


@pytest.mark.parametrize("worker", ["", "has space", None])
def test_a_worker_is_a_code(worker):
    with pytest.raises(ValueError):
        claim_next_request(NoStore(), f"t_writer-a2_{uuid4()}", LEARNER, worker=worker)


@pytest.mark.parametrize("lease", [0, -1, True, "300"])
def test_a_lease_is_a_positive_number_of_seconds(lease):
    with pytest.raises(ValueError):
        claim_next_request(
            NoStore(), f"t_writer-a2_{uuid4()}", LEARNER, worker="pass-1", lease_seconds=lease
        )


# Back-off and surfacing -----------------------------------------------------------------------


def test_the_constants_the_ruling_names():
    assert MAX_ATTEMPTS == 5
    assert BACKOFF_SECONDS == (30, 60, 120, 240)
    assert LEASE_SECONDS == 300


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(0, 30), (1, 30), (2, 60), (3, 120), (4, 240), (5, 240), (9, 240)]
)
def test_the_back_off_table(attempts, seconds):
    assert requests_module.next_retry(attempts, T) == T + timedelta(seconds=seconds)


def test_a_version_conflict_backs_off_and_then_surfaces(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    item_id = item_at_version_two(driver, world_id, chat)
    request_id = enqueued(driver, world_id, chat)
    stale = stale_update(chat, item_id)

    for attempt, seconds in zip(range(1, MAX_ATTEMPTS + 1), (30, 60, 120, 240, 240), strict=True):
        claim = claim_next_request(driver, world_id, LEARNER, worker=f"pass-{attempt}")
        assert claim.id == request_id
        assert claim.attempts == attempt
        if attempt > 1:
            assert claim.last_error_code == "version_conflict"

        state = apply_request(driver, world_id, LEARNER, request_id, stale)

        assert state.status == "queued"
        assert state.api_state == "saving"
        assert state.last_error_code == "version_conflict"
        now = clock.now(world_id)
        assert state.retry_at == now + timedelta(seconds=seconds)
        row = row_of(driver, world_id, request_id)
        assert "claimed_by" not in row and "lease_until" not in row
        assert claim_next_request(driver, world_id, LEARNER, worker="early") is None
        frozen.advance(seconds=seconds - 1)
        assert claim_next_request(driver, world_id, LEARNER, worker="early") is None
        frozen.advance(seconds=1)

    assert claim_next_request(driver, world_id, LEARNER, worker="late") is None
    row = row_of(driver, world_id, request_id)
    assert row["status"] == "failed"
    assert row["last_error_code"] == "attempts_exhausted"
    assert row["attempts"] == MAX_ATTEMPTS
    assert "retry_at" not in row and "lease_until" not in row and "claimed_by" not in row
    state = request_state(driver, world_id, request_id)
    assert state.api_state == "failed"
    assert [entry.request_id for entry in surfaced_requests(driver, world_id)] == [request_id]
    assert pending_requests(driver, world_id) == []


def test_a_claim_surfaces_a_spent_row_and_moves_to_the_next(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    spent = key()
    plant_node(
        driver,
        world_id,
        "Request",
        id=spent,
        status="queued",
        source_id=chat["source_id"],
        attempts=MAX_ATTEMPTS,
        created_at=T - timedelta(hours=1),
        retry_at=T - timedelta(minutes=1),
        lease_until=T,
        claimed_by="pass-0",
    )
    fresh = enqueued(driver, world_id, chat)

    claim = claim_next_request(driver, world_id, LEARNER, worker="pass-1")

    assert claim.id == fresh
    row = row_of(driver, world_id, spent)
    assert row["status"] == "failed" and row["last_error_code"] == "attempts_exhausted"
    assert "retry_at" not in row and "lease_until" not in row and "claimed_by" not in row


# The commit -----------------------------------------------------------------------------------


def test_a_commit_moves_saved_in_the_same_transaction(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    sent = one_item(chat)
    seen = {}

    def before_commit() -> None:
        # Another session sees neither the batch nor the status: both wait for this commit.
        seen["row"] = row_of(driver, world_id, request_id)
        seen["titles"] = titles(driver, world_id)

    monkeypatch.setattr(apply_module, "_before_commit", before_commit)

    state = apply_request(driver, world_id, LEARNER, request_id, sent, run_id="run-7")

    assert seen["row"]["status"] == "applying"
    assert seen["titles"] == []
    assert state.api_state == "saved"
    row = row_of(driver, world_id, request_id)
    assert row["status"] == "saved"
    assert row["commit_id"] == state.receipt.commit_id
    assert row["change_set_id"] == state.receipt.change_set_id
    canonical = json.dumps(sent, sort_keys=True, ensure_ascii=False, default=str)
    assert row["fingerprint"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert row["committed_at"] is not None
    assert row["run_id"] == "run-7"
    assert row["attempts"] == 1, "the claim counted this attempt"
    for gone in (
        "declined_fingerprint",
        "retry_at",
        "lease_until",
        "claimed_by",
        "last_error_code",
    ):
        assert gone not in row


def test_a_failed_batch_rolls_the_request_back_with_it(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    before = row_of(driver, world_id, request_id)

    def kill() -> None:
        raise Killed

    monkeypatch.setattr(apply_module, "_before_commit", kill)

    with pytest.raises(Killed):
        apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert row_of(driver, world_id, request_id) == before
    assert titles(driver, world_id) == []


def test_a_direct_batch_mints_a_request_id_and_stamps_it_everywhere(driver, seeded, chat):
    world_id = seeded["world_id"]

    receipt = apply_batch(driver, world_id, PERSON, one_item(chat))

    assert receipt.request_id
    row = row_of(driver, world_id, receipt.request_id)
    assert row["status"] == "saved"
    assert row["attempts"] == 1, "a saved row shows at least one attempt"
    assert row["caller"] == PERSON.actor
    assert "source_id" not in row
    entries = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN e.request_id AS request_id",
        id=receipt.change_set_id,
    )
    assert entries and {entry["request_id"] for entry in entries} == {receipt.request_id}
    head = render_receipt(driver, world_id, receipt).splitlines()[0]
    assert head.endswith(f"request {short_id(receipt.request_id)}")


def test_a_direct_batch_under_a_key_stamps_that_key(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()

    receipt = apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)

    assert receipt.request_id == request_id
    assert committed_for_request(driver, world_id, request_id)
    state = request_state(driver, world_id, request_id)
    assert state.api_state == "saved"
    assert state.receipt == receipt


@pytest.mark.parametrize(
    "bad",
    ["", " ", "has space", "rowan/salary", "x" * 129, 7, "naïve"],
    ids=["empty", "blank", "space", "slash", "too_long", "number", "accent"],
)
def test_a_request_id_holds_no_text_at_every_entry_point(bad):
    world_id = f"t_writer-a2_{uuid4()}"
    store = NoStore()
    batch_ = {"ops": [], "evidence": []}
    entries = [
        lambda: apply_batch(store, world_id, PERSON, batch_, request_id=bad),
        lambda: enqueue_request(store, world_id, HOST, bad, source_id="s1"),
        lambda: apply_request(store, world_id, LEARNER, bad, batch_),
        lambda: finish_request(store, world_id, LEARNER, bad, worker="pass-1"),
        lambda: fail_request(store, world_id, LEARNER, bad, "boom", worker="pass-1"),
        lambda: request_state(store, world_id, bad),
        lambda: committed_for_request(store, world_id, bad),
        lambda: write_request_row(store, HOST, bad, source_id="s1"),
    ]
    for entry in entries:
        with pytest.raises(ValueError):
            entry()


@pytest.mark.parametrize("good", ["7f3c90d1", "client-1:abc_DEF.2", "x" * 128, "K:g2"])
def test_the_forms_a_host_and_a_backfill_use_are_accepted(driver, seeded, chat, good):
    world_id = seeded["world_id"]
    receipt = apply_batch(driver, world_id, PERSON, one_item(chat), request_id=good)
    assert receipt.request_id == good


# The replay rules -----------------------------------------------------------------------------


def test_a_replay_of_a_saved_request_writes_nothing_and_reads_the_receipt(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    first = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = world_dump(driver, world_id)

    second = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert second == first
    assert world_dump(driver, world_id) == before


def test_the_replay_rules_hold_again_under_the_lock(driver, seeded, chat, monkeypatch):
    # A stale pre-flight read: the row saved after it. The transaction must answer the same.
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    first = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = world_dump(driver, world_id)
    monkeypatch.setattr(api_module, "read_row", lambda *args: None)

    second = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    with pytest.raises(BatchDeclined) as reused:
        apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 2), request_id=request_id)

    assert second == first
    assert reused.value.refusals[0].rule == "request_key_reused"
    assert world_dump(driver, world_id) == before


def test_the_decline_replays_hold_again_under_the_lock(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = key()
    sent = unknown_kind(chat)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat, 2), request_id=request_id)
    before = world_dump(driver, world_id)
    monkeypatch.setattr(api_module, "read_row", lambda *args: None)

    with pytest.raises(BatchDeclined) as spent:
        apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 3), request_id=request_id)

    assert spent.value.refusals[0].rule == "repair_spent"
    assert world_dump(driver, world_id) == before


def test_a_replay_of_a_saved_request_does_not_wait_for_the_world_lock(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    first = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    paused, release = threading.Event(), threading.Event()

    def hold() -> None:
        if threading.current_thread().name == "holder":
            paused.set()
            assert release.wait(60)

    monkeypatch.setattr(apply_module, "_before_commit", hold)
    results: dict[str, Any] = {}
    holder = threading.Thread(
        target=lambda: apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 2)),
        name="holder",
    )
    holder.start()
    assert paused.wait(60)
    replay = threading.Thread(
        target=lambda: results.setdefault(
            "receipt", apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
        )
    )
    replay.start()
    replay.join(15.0)
    queued_behind_it = replay.is_alive()
    release.set()
    holder.join(90)
    replay.join(90)

    assert not queued_behind_it, "a replay of a saved request waited for the world lock"
    assert results["receipt"] == first


def test_n_replays_of_one_request_give_one_effect(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    start = threading.Barrier(6)
    receipts: list[Receipt] = []

    def replay() -> None:
        start.wait(60)
        receipts.append(apply_batch(driver, world_id, PERSON, sent, request_id=request_id))

    threads = [threading.Thread(target=replay) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert len(receipts) == 6
    assert titles(driver, world_id) == ["Rowan"]
    assert {receipt.change_set_id for receipt in receipts} == {receipts[0].change_set_id}
    assert row_of(driver, world_id, request_id)["attempts"] == 1


def test_different_content_on_a_saved_key_is_refused_and_not_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as refused:
        apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 2), request_id=request_id)

    assert [(r.code, r.rule) for r in refused.value.refusals] == [
        ("invalid_arguments", "request_key_reused")
    ]
    assert world_dump(driver, world_id) == before


def test_a_shape_fault_on_a_saved_key_is_refused_and_the_row_stays_saved(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as refused:
        apply_batch(driver, world_id, PERSON, shape_fault(chat), request_id=request_id)

    assert refused.value.refusals[0].rule == "request_key_reused"
    assert world_dump(driver, world_id) == before
    assert row_of(driver, world_id, request_id)["status"] == "saved"


def test_a_caller_kind_refusal_on_a_saved_key_leaves_the_row_saved(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as refused:
        apply_batch(driver, world_id, HOST, one_item(chat, "Ilse", 2), request_id=request_id)

    assert refused.value.refusals[0].rule == "request_key_reused"
    assert world_dump(driver, world_id) == before


def test_a_decline_then_a_replay_of_it_writes_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    sent = unknown_kind(chat)
    with pytest.raises(BatchDeclined) as first:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as second:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert [(r.op_id, r.op, r.code, r.rule) for r in second.value.refusals] == [
        (r.op_id, r.op, r.code, r.rule) for r in first.value.refusals
    ]
    assert second.value.retryable is False
    assert world_dump(driver, world_id) == before
    assert row_of(driver, world_id, request_id)["repairs"] == 0


def test_a_repair_that_commits_saves_and_clears_the_decline(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    declined = row_of(driver, world_id, request_id)
    assert declined["status"] == "declined"
    assert declined["last_error_code"] == "unknown_kind"
    assert declined["attempts"] == 1

    receipt = apply_batch(
        driver, world_id, PERSON, one_item(chat, "Ilse", 2), request_id=request_id
    )

    row = row_of(driver, world_id, request_id)
    assert row["status"] == "saved"
    assert row["attempts"] == 2
    assert "declined_fingerprint" not in row and "last_error_code" not in row
    assert json.loads(row["receipt"])["state"] == "saved", "the commit cleared the stored decline"
    state = request_state(driver, world_id, request_id)
    assert state.api_state == "saved"
    assert state.receipt == receipt
    assert state.declines == []


def test_a_failed_repair_spends_the_repair(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    repair = unknown_kind(chat, 2, "airship")
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, repair, request_id=request_id)
    row = row_of(driver, world_id, request_id)
    assert row["repairs"] == 1
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as spent:
        apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 3), request_id=request_id)

    assert [(r.code, r.rule) for r in spent.value.refusals] == [
        ("invalid_arguments", "repair_spent")
    ]
    assert world_dump(driver, world_id) == before
    assert titles(driver, world_id) == []

    with pytest.raises(BatchDeclined) as again:
        apply_batch(driver, world_id, PERSON, repair, request_id=request_id)
    assert again.value.refusals[0].code == "unknown_kind", "the repair's own replay still replays"
    assert world_dump(driver, world_id) == before


def test_a_shape_faulted_repair_spends_the_repair_too(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    with pytest.raises(BatchDeclined) as faulted:
        apply_batch(driver, world_id, PERSON, shape_fault(chat), request_id=request_id)
    assert faulted.value.refusals[0].rule == "missing_field"
    assert row_of(driver, world_id, request_id)["repairs"] == 1

    with pytest.raises(BatchDeclined) as spent:
        apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 3), request_id=request_id)

    assert spent.value.refusals[0].rule == "repair_spent"


def test_a_conflict_on_a_repair_goes_back_to_the_queue_with_the_repair_used(driver, seeded, chat):
    world_id = seeded["world_id"]
    item_id = item_at_version_two(driver, world_id, chat)
    request_id = enqueued(driver, world_id, chat)
    claim_next_request(driver, world_id, LEARNER, worker="pass-1")
    first = apply_request(driver, world_id, LEARNER, request_id, unknown_kind(chat))
    assert first.status == "declined"

    state = apply_request(driver, world_id, LEARNER, request_id, stale_update(chat, item_id))

    assert state.status == "queued"
    row = row_of(driver, world_id, request_id)
    assert row["repairs"] == 1
    assert "declined_fingerprint" not in row and "receipt" not in row
    declined_again = apply_request(driver, world_id, LEARNER, request_id, unknown_kind(chat, 6))
    assert declined_again.status == "declined"
    with pytest.raises(BatchDeclined) as spent:
        apply_request(driver, world_id, LEARNER, request_id, one_item(chat, "Ilse", 7))
    assert spent.value.refusals[0].rule == "repair_spent"


# Finish and fail ------------------------------------------------------------------------------


def test_finish_saves_a_pass_that_found_nothing_to_write(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)

    state = finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")

    assert state.api_state == "saved"
    assert state.receipt.commit_id is None
    assert state.receipt.change_set_id is None
    assert state.receipt.lines == ()
    assert state.to_api()["receipt"] == {
        "state": "saved",
        "commit_id": None,
        "change_set_id": None,
        "request_id": request_id,
        "lines": [],
    }
    row = row_of(driver, world_id, request_id)
    assert json.loads(row["receipt"])["state"] == "saved"
    assert "lease_until" not in row and "claimed_by" not in row
    assert "commit_id" not in row and "fingerprint" not in row and "committed_at" not in row
    assert committed_for_request(driver, world_id, request_id) == []
    lines = render_receipt(driver, world_id, state.receipt).splitlines()
    assert lines[0] == f"saved (0), request {short_id(request_id)}"
    assert short_id(chat["source_id"]) in lines[1]


def test_finish_on_a_queued_row_changes_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    before = world_dump(driver, world_id)

    state = finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")

    assert state.status == "queued"
    assert state.api_state == "saving"
    assert world_dump(driver, world_id) == before


def test_finish_on_a_saved_row_returns_its_state(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    first = finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")
    before = world_dump(driver, world_id)

    second = finish_request(driver, world_id, LEARNER, request_id, worker="pass-2")

    assert second == first
    assert world_dump(driver, world_id) == before


def test_finish_by_a_worker_that_does_not_hold_the_lease_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat, "pass-1")
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as lost:
        finish_request(driver, world_id, LEARNER, request_id, worker="pass-2")

    assert [(r.code, r.rule) for r in lost.value.refusals] == [("invalid_arguments", "lease_lost")]
    assert world_dump(driver, world_id) == before


def test_a_batch_after_a_finish_is_a_reused_key(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")

    with pytest.raises(BatchDeclined) as reused:
        apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert reused.value.refusals[0].rule == "request_key_reused"
    assert titles(driver, world_id) == []


def test_finish_and_fail_on_an_unknown_request_are_not_found(driver, seeded):
    world_id = seeded["world_id"]
    for call in (
        lambda: finish_request(driver, world_id, LEARNER, key(), worker="pass-1"),
        lambda: fail_request(driver, world_id, LEARNER, key(), "boom", worker="pass-1"),
    ):
        with pytest.raises(BatchDeclined) as missing:
            call()
        assert missing.value.refusals[0].code == "not_found"


def test_fail_with_a_retry_waits_and_reads_saving(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)

    state = fail_request(driver, world_id, LEARNER, request_id, "model_failed", worker="pass-1")

    assert state.status == "failed"
    assert state.api_state == "saving"
    assert state.retry_at == T + timedelta(seconds=30)
    assert state.last_error_code == "model_failed"
    row = row_of(driver, world_id, request_id)
    assert "lease_until" not in row and "claimed_by" not in row
    assert surfaced_requests(driver, world_id) == []
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2") is None
    frozen.advance(seconds=30)
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2").attempts == 2


def test_fail_with_no_retry_surfaces(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)

    state = fail_request(
        driver, world_id, LEARNER, request_id, "contamination", worker="pass-1", retryable=False
    )

    assert state.api_state == "failed"
    assert state.retry_at is None
    assert state.to_api() == {
        "state": "failed",
        "request_id": request_id,
        "last_error_code": "contamination",
    }
    assert [entry.request_id for entry in surfaced_requests(driver, world_id)] == [request_id]
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2") is None


def test_fail_at_the_last_attempt_surfaces_even_with_a_retry(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    plant_node(
        driver,
        world_id,
        "Request",
        id=request_id,
        status="applying",
        source_id=chat["source_id"],
        attempts=MAX_ATTEMPTS,
        claimed_by="pass-1",
    )

    state = fail_request(driver, world_id, LEARNER, request_id, "boom", worker="pass-1")

    assert state.api_state == "failed"


def test_fail_by_a_worker_that_does_not_hold_the_lease_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat, "pass-1")
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as lost:
        fail_request(driver, world_id, LEARNER, request_id, "boom", worker="pass-2")

    assert lost.value.refusals[0].rule == "lease_lost"
    assert world_dump(driver, world_id) == before


def test_fail_leaves_a_row_that_is_not_applying_unchanged(driver, seeded, chat):
    world_id = seeded["world_id"]
    queued = enqueued(driver, world_id, chat)
    saved = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=saved)
    declined = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat, 2), request_id=declined)
    before = world_dump(driver, world_id)

    for request_id, status in ((queued, "queued"), (saved, "saved"), (declined, "declined")):
        state = fail_request(driver, world_id, LEARNER, request_id, "boom", worker="pass-1")
        assert state.status == status

    assert world_dump(driver, world_id) == before


@pytest.mark.parametrize("code", ["", "has space", None])
def test_a_failure_code_is_a_code(code):
    with pytest.raises(ValueError):
        fail_request(NoStore(), f"t_writer-a2_{uuid4()}", LEARNER, key(), code, worker="pass-1")


# Store failures and unknown outcomes ----------------------------------------------------------


def _fail_commit(monkeypatch) -> None:
    def fail() -> None:
        raise DatabaseError("injected commit failure")

    monkeypatch.setattr(apply_module, "_before_commit", fail)


def _commit_then_lose_the_reply(monkeypatch) -> None:
    """The batch's commit lands and its reply is lost. A read commits too, so the seam arms it."""
    original = TransactionBase._commit
    armed: list[int] = []

    def commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if armed:
            armed.clear()
            raise IncompleteCommit("injected: connection lost during commit")

    monkeypatch.setattr(apply_module, "_before_commit", lambda: armed.append(1))
    monkeypatch.setattr(TransactionBase, "_commit", commit)


def test_a_store_failure_on_an_enqueued_row_waits_for_a_retry(
    driver, seeded, chat, frozen, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    _fail_commit(monkeypatch)

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat), run_id="run-3")

    assert state.api_state == "saving"
    assert state.status == "failed"
    assert state.last_error_code == "unavailable"
    assert state.retry_at == T + timedelta(seconds=30)
    row = row_of(driver, world_id, request_id)
    assert row["attempts"] == 1, "the claim counted this attempt"
    assert row["run_id"] == "run-3"
    assert "claimed_by" not in row and "lease_until" not in row
    assert titles(driver, world_id) == []


def test_a_store_failure_on_a_queued_row_counts_an_attempt(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    _fail_commit(monkeypatch)

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert state.status == "failed"
    assert state.attempts == 1


def test_a_store_failure_on_a_direct_call_writes_no_row_and_names_the_id(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    _fail_commit(monkeypatch)
    before = world_dump(driver, world_id)

    with pytest.raises(WriteFailed) as failed:
        apply_batch(driver, world_id, PERSON, one_item(chat))

    assert failed.value.request_id
    assert world_dump(driver, world_id) == before


def test_a_store_failure_leaves_a_row_with_no_source_alone(driver, seeded, chat, monkeypatch):
    # Only an enqueue makes a row a retry can bring back: this one would read "saving" for ever.
    world_id = seeded["world_id"]
    request_id = key()
    plant_node(driver, world_id, "Request", id=request_id, status="queued", touched_at=T)
    before = row_of(driver, world_id, request_id)
    _fail_commit(monkeypatch)

    with pytest.raises(WriteFailed):
        apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)

    assert row_of(driver, world_id, request_id) == before


def test_a_store_failure_leaves_a_declined_row_declined(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    apply_request(driver, world_id, LEARNER, request_id, unknown_kind(chat))
    before = row_of(driver, world_id, request_id)
    _fail_commit(monkeypatch)

    with pytest.raises(WriteFailed):
        apply_batch(driver, world_id, LEARNER, one_item(chat, "Ilse", 2), request_id=request_id)

    assert row_of(driver, world_id, request_id) == before


def test_a_failed_write_of_the_failure_is_harmless(driver, seeded, chat, frozen, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    before = row_of(driver, world_id, request_id)

    def down(self, work):
        raise ServiceUnavailable("injected: the store is down")

    monkeypatch.setattr(WorldWriter, "transaction", down)

    with pytest.raises(WriteFailed):
        apply_batch(driver, world_id, LEARNER, one_item(chat), request_id=request_id)

    monkeypatch.undo()
    assert row_of(driver, world_id, request_id) == before
    frozen.advance(seconds=LEASE_SECONDS)
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2").id == request_id


def test_an_unknown_commit_outcome_is_safe_to_replay(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    _commit_then_lose_the_reply(monkeypatch)

    with pytest.raises(WriteOutcomeUnknown) as unknown:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    monkeypatch.undo()
    assert unknown.value.request_id == request_id
    assert unknown.value.retryable is False
    before = count_rows(driver, world_id)

    receipt = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert receipt.change_set_id == unknown.value.change_set_id
    assert titles(driver, world_id) == ["Rowan"]
    assert count_rows(driver, world_id) == before


def test_an_unreadable_receipt_is_safe_to_replay(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the read after the commit failed")

    monkeypatch.setattr(api_module, "read_receipt", fail)

    with pytest.raises(ReceiptUnavailable) as unavailable:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    monkeypatch.undo()
    assert unavailable.value.request_id == request_id
    receipt = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    assert receipt.change_set_id == unavailable.value.change_set_id
    assert titles(driver, world_id) == ["Rowan"]


def test_a_replay_reports_an_unreadable_decline_as_a_write_failure(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = key()
    sent = unknown_kind(chat)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the read failed")

    monkeypatch.setattr(requests_module, "candidates_by_op", fail)

    with pytest.raises(WriteFailed) as failed:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert failed.value.request_id == request_id


def test_a_replay_reports_an_unreadable_receipt_as_itself(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)

    def kill() -> None:
        raise Killed

    monkeypatch.setattr(api_module, "_after_commit", kill)
    with pytest.raises(Killed):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    monkeypatch.undo()

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the read failed")

    monkeypatch.setattr(requests_module, "read_receipt", fail)

    with pytest.raises(ReceiptUnavailable) as unavailable:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert unavailable.value.request_id == request_id
    assert unavailable.value.commit_id


def test_a_store_failure_in_the_queue_is_a_write_failure(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)

    def down(self, work):
        raise ServiceUnavailable("injected: the store is down")

    monkeypatch.setattr(WorldWriter, "transaction", down)
    calls = {
        "enqueue": lambda: enqueue_request(
            driver, world_id, HOST, key(), source_id=chat["source_id"]
        ),
        "finish": lambda: finish_request(driver, world_id, LEARNER, request_id, worker="pass-1"),
        "fail": lambda: fail_request(
            driver, world_id, LEARNER, request_id, "boom", worker="pass-1"
        ),
    }

    for name, call in calls.items():
        with pytest.raises(WriteFailed) as failed:
            call()
        assert failed.value.retryable is True, name
        assert failed.value.request_id, name


def test_a_store_failure_during_a_claim_is_a_write_failure(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    enqueued(driver, world_id, chat)

    def down(self, work):
        raise ServiceUnavailable("injected: the store is down")

    monkeypatch.setattr(WorldWriter, "transaction", down)

    with pytest.raises(WriteFailed):
        claim_next_request(driver, world_id, LEARNER, worker="pass-1")


def test_apply_request_says_saving_when_the_store_is_down_for_its_reads(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    _fail_commit(monkeypatch)
    reads: list[int] = []
    original = requests_module.read_row

    def second_read_fails(*args):
        reads.append(1)
        if len(reads) > 1:
            raise ServiceUnavailable("injected: the read failed")
        return original(*args)

    monkeypatch.setattr(requests_module, "read_row", second_read_fails)

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert len(reads) == 2
    assert state.api_state == "saving"
    assert state.last_error_code == "unavailable"


def test_a_read_failure_before_apply_request_is_a_write_failure(driver, seeded, monkeypatch):
    world_id = seeded["world_id"]

    def down(*args):
        raise ServiceUnavailable("injected: the read failed")

    monkeypatch.setattr(requests_module, "read_row", down)

    with pytest.raises(WriteFailed) as failed:
        apply_request(driver, world_id, LEARNER, "k-1", {"ops": []})

    assert failed.value.request_id == "k-1"


def test_a_read_failure_before_the_transaction_is_a_write_failure(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]

    def down(*args):
        raise ServiceUnavailable("injected: the read failed")

    monkeypatch.setattr(api_module, "read_row", down)

    with pytest.raises(WriteFailed) as failed:
        apply_batch(driver, world_id, PERSON, one_item(chat), request_id="k-1")

    assert failed.value.request_id == "k-1"
    assert failed.value.retryable is True


# Receipts on the row -------------------------------------------------------------------------


def test_a_kill_after_the_commit_leaves_saved_with_no_receipt_and_a_replay_attaches(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = key()
    sent = one_item(chat)
    seen = {}

    def kill() -> None:
        seen["row"] = row_of(driver, world_id, request_id)
        raise Killed

    monkeypatch.setattr(api_module, "_after_commit", kill)
    with pytest.raises(Killed):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    monkeypatch.undo()

    assert seen["row"]["status"] == "saved", "the seam runs after the commit"
    row = row_of(driver, world_id, request_id)
    assert "receipt" not in row
    before = world_dump(driver, world_id)

    state = request_state(driver, world_id, request_id)

    assert state.api_state == "saved"
    assert state.receipt.change_set_id == row["change_set_id"]
    assert [line.symbol for line in state.receipt.lines] == ["+"]
    assert world_dump(driver, world_id) == before, "request_state is a pure read"

    replayed = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert replayed == state.receipt
    attached = row_of(driver, world_id, request_id)
    assert Receipt.from_json(json.loads(attached["receipt"])) == replayed
    assert count_rows(driver, world_id)["Item"] == 1
    assert attached["version"] == row["version"] + 1


def test_an_attach_writes_only_on_a_saved_row_with_no_receipt(driver, seeded, chat):
    world_id = seeded["world_id"]
    queued = enqueued(driver, world_id, chat)
    saved = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=saved)
    before = world_dump(driver, world_id)

    for request_id in (queued, saved):
        receipt = Receipt("saved", "c", "s", request_id)
        requests_module.attach_receipt(driver, world_id, receipt)

    assert world_dump(driver, world_id) == before


def test_a_failed_attach_never_fails_the_call(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = key()
    original = WorldWriter.transaction
    calls: list[int] = []

    def second_fails(self, work):
        calls.append(1)
        if len(calls) == 2:
            raise ServiceUnavailable("injected: the attach failed")
        return original(self, work)

    monkeypatch.setattr(WorldWriter, "transaction", second_fails)

    receipt = apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)

    assert len(calls) == 2
    assert receipt.state == "saved"
    assert "receipt" not in row_of(driver, world_id, request_id)


def test_a_stored_receipt_holds_ids_and_codes_only(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    title = "Okonkwo boathouse keeper"

    apply_batch(driver, world_id, PERSON, one_item(chat, title), request_id=request_id)

    assert title not in row_of(driver, world_id, request_id)["receipt"]


def test_request_state_is_a_pure_read_in_every_status(driver, seeded, chat):
    world_id = seeded["world_id"]
    queued = enqueued(driver, world_id, chat)
    saved = key()
    apply_batch(driver, world_id, PERSON, one_item(chat), request_id=saved)
    declined = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat, 2), request_id=declined)
    before = world_dump(driver, world_id)

    assert request_state(driver, world_id, queued).status == "queued"
    assert request_state(driver, world_id, saved).status == "saved"
    assert request_state(driver, world_id, declined).status == "declined"
    assert request_state(driver, world_id, key()) is None

    assert world_dump(driver, world_id) == before


def test_api_state_is_never_saved_with_no_receipt():
    no_receipt = RequestState(request_id="k", status="saved")
    assert no_receipt.api_state == "saving"
    assert "receipt" not in no_receipt.to_api()
    receipt = Receipt("saved", "c", "s", "k")
    assert RequestState(request_id="k", status="saved", receipt=receipt).api_state == "saved"
    assert RequestState(request_id="k", status="applying", receipt=receipt).api_state == "saving"


@pytest.mark.parametrize(
    ("status", "retry_at", "expected"),
    [
        ("queued", None, "saving"),
        ("queued", T, "saving"),
        ("applying", None, "saving"),
        ("failed", T, "saving"),
        ("failed", None, "failed"),
        ("declined", None, "declined"),
    ],
)
def test_api_state_by_status(status, retry_at, expected):
    assert RequestState(request_id="k", status=status, retry_at=retry_at).api_state == expected


def test_a_stored_decline_on_a_saved_row_is_no_receipt(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    receipt = apply_batch(driver, world_id, PERSON, one_item(chat), request_id=request_id)
    decline = json.dumps({"state": "declined", "batch_size": 1, "refusals": []})
    raw(
        driver,
        "MATCH (r:Request {world_id: $w, id: $id}) SET r.receipt = $receipt",
        w=world_id,
        id=request_id,
        receipt=decline,
    )

    state = request_state(driver, world_id, request_id)

    assert state.api_state == "saved"
    assert state.receipt == receipt


def test_to_api_of_a_saved_and_a_declined_request(driver, seeded, chat):
    world_id = seeded["world_id"]
    saved = key()
    receipt = apply_batch(driver, world_id, PERSON, one_item(chat), request_id=saved)
    declined = key()
    sent = unknown_kind(chat, 2)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=declined)

    assert request_state(driver, world_id, saved).to_api() == {
        "state": "saved",
        "request_id": saved,
        "receipt": receipt.to_json(),
    }
    assert request_state(driver, world_id, declined).to_api() == {
        "state": "declined",
        "request_id": declined,
        "declines": [
            {
                "op_id": sent["ops"][0]["op_id"],
                "op": "create_item",
                "code": "unknown_kind",
                "rule": "unknown_kind",
                "candidates": [],
            }
        ],
        "last_error_code": "unknown_kind",
    }


def test_a_saved_row_shows_its_receipt_after_a_second_enqueue(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    receipt = apply_batch(driver, world_id, LEARNER, one_item(chat), request_id=request_id)

    state = enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])

    assert state.api_state == "saved"
    assert state.receipt == receipt


# apply_request --------------------------------------------------------------------------------


def test_apply_request_needs_a_row_an_enqueue_wrote(driver, seeded, chat):
    world_id = seeded["world_id"]
    before = count_rows(driver, world_id)

    with pytest.raises(BatchDeclined) as missing:
        apply_request(driver, world_id, LEARNER, key(), one_item(chat))

    assert [(r.code, r.rule) for r in missing.value.refusals] == [
        ("not_found", "request_not_found")
    ]
    assert count_rows(driver, world_id) == before


def test_apply_request_turns_a_decline_into_a_state(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    sent = unknown_kind(chat)

    state = apply_request(driver, world_id, LEARNER, request_id, sent)

    assert state.api_state == "declined"
    assert [(d["op_id"], d["code"]) for d in state.declines] == [
        (sent["ops"][0]["op_id"], "unknown_kind")
    ]
    assert state.last_error_code == "unknown_kind"
    assert state == request_state(driver, world_id, request_id)
    row = row_of(driver, world_id, request_id)
    assert "claimed_by" not in row and "lease_until" not in row and "retry_at" not in row


def test_apply_request_keeps_a_decline_when_its_log_failed(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    sent = unknown_kind(chat)

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the log failed")

    monkeypatch.setattr(api_module, "log_decline", fail)

    state = apply_request(driver, world_id, LEARNER, request_id, sent)

    assert state.api_state == "declined"
    assert state.declines[0]["code"] == "unknown_kind"
    assert row_of(driver, world_id, request_id)["status"] == "applying"


def test_apply_request_says_saving_for_an_unknown_outcome(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)
    _commit_then_lose_the_reply(monkeypatch)

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert state.api_state == "saving"
    assert state.receipt is None
    assert state.last_error_code == "outcome_unknown"
    assert state.to_api() == {
        "state": "saving",
        "request_id": request_id,
        "last_error_code": "outcome_unknown",
    }


def test_apply_request_says_saving_for_an_unreadable_receipt(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = claimed(driver, world_id, chat)

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the read after the commit failed")

    monkeypatch.setattr(api_module, "read_receipt", fail)

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert state.api_state == "saving"
    assert state.last_error_code == "receipt_unavailable"
    assert "receipt" not in state.to_api()
    monkeypatch.undo()
    assert request_state(driver, world_id, request_id).api_state == "saved"


def test_apply_request_lets_the_refusals_of_the_key_and_the_caller_through(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)
    apply_request(driver, world_id, LEARNER, request_id, one_item(chat))
    with pytest.raises(BatchDeclined) as reused:
        apply_request(driver, world_id, LEARNER, request_id, one_item(chat, "Ilse", 2))
    assert reused.value.refusals[0].rule == "request_key_reused"

    other = enqueued(driver, world_id, chat)
    with pytest.raises(BatchDeclined) as denied:
        apply_request(driver, world_id, HOST, other, one_item(chat, "Ilse", 3))
    assert denied.value.refusals[0].code == "scope_denied"

    third = enqueued(driver, world_id, chat)
    apply_request(driver, world_id, LEARNER, third, unknown_kind(chat, 4))
    apply_request(driver, world_id, LEARNER, third, unknown_kind(chat, 5))
    with pytest.raises(BatchDeclined) as spent:
        apply_request(driver, world_id, LEARNER, third, one_item(chat, "Okonkwo", 6))
    assert spent.value.refusals[0].rule == "repair_spent"


def test_apply_request_lets_a_writer_fault_through(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    request_id = enqueued(driver, world_id, chat)

    class WriterBug(KeyError):
        pass

    def fail() -> None:
        raise WriterBug("injected writer fault")

    monkeypatch.setattr(apply_module, "_before_commit", fail)

    with pytest.raises(WriterBug):
        apply_request(driver, world_id, LEARNER, request_id, one_item(chat))

    assert row_of(driver, world_id, request_id)["status"] == "queued"


def test_run_id_is_set_on_every_attempt(driver, seeded, chat):
    world_id = seeded["world_id"]
    item_id = item_at_version_two(driver, world_id, chat)
    request_id = enqueued(driver, world_id, chat)

    apply_request(
        driver, world_id, LEARNER, request_id, stale_update(chat, item_id), run_id="run-1"
    )
    assert row_of(driver, world_id, request_id)["run_id"] == "run-1"

    apply_request(driver, world_id, LEARNER, request_id, unknown_kind(chat), run_id="run-2")
    assert row_of(driver, world_id, request_id)["run_id"] == "run-2"

    state = apply_request(driver, world_id, LEARNER, request_id, one_item(chat), run_id="run-3")
    assert state.run_id == "run-3"
    assert row_of(driver, world_id, request_id)["run_id"] == "run-3"
    entries = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {request_id: $id}) RETURN e.run_id AS run",
        id=request_id,
    )
    assert entries and {entry["run"] for entry in entries} == {"run-3"}


# Scope ----------------------------------------------------------------------------------------


def test_any_caller_with_a_write_scope_may_enqueue(driver, seeded, chat):
    world_id = seeded["world_id"]
    for caller in (HOST, PERSON, LEARNER):
        state = enqueue_request(driver, world_id, caller, key(), source_id=chat["source_id"])
        assert state.status == "queued"


@pytest.mark.parametrize(
    "caller",
    [
        HOST,
        PERSON,
        READ_ONLY,
        Caller(kind="update", scopes=frozenset({"grove.read", "grove.write"}), actor="agent:t"),
        Caller(kind="writer", scopes=frozenset({"grove.read", "grove.propose"}), actor="w:t"),
    ],
    ids=["host", "person", "read_only", "worker_with_no_propose", "writer_kind"],
)
def test_only_a_queue_worker_works_the_queue(caller):
    world_id = f"t_writer-a2_{uuid4()}"
    store = NoStore()
    calls = [
        lambda: claim_next_request(store, world_id, caller, worker="pass-1"),
        lambda: finish_request(store, world_id, caller, key(), worker="pass-1"),
        lambda: fail_request(store, world_id, caller, key(), "boom", worker="pass-1"),
    ]
    for call in calls:
        with pytest.raises(BatchDeclined) as denied:
            call()
        assert [(r.code, r.rule) for r in denied.value.refusals] == [
            ("scope_denied", "queue_worker")
        ]


@pytest.mark.parametrize("kind", ["update", "cleanup", "consolidation"])
def test_each_worker_kind_may_claim(driver, seeded, chat, kind):
    world_id = seeded["world_id"]
    worker = Caller(kind=kind, scopes=frozenset({"grove.propose"}), actor=f"agent:{kind}")
    request_id = enqueued(driver, world_id, chat)
    assert claim_next_request(driver, world_id, worker, worker="pass-1").id == request_id


# Reads for later lanes ------------------------------------------------------------------------


def test_committed_for_request_lists_ids_and_codes_in_order(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    receipt = apply_batch(driver, world_id, PERSON, one_item(chat, "Rowan"), request_id=request_id)
    other = key()
    apply_batch(driver, world_id, PERSON, one_item(chat, "Ilse", 2), request_id=other)

    entries = committed_for_request(driver, world_id, request_id)

    assert entries
    sequences = [entry["sequence"] for entry in entries]
    assert sequences == sorted(sequences)
    assert {entry["change_set_id"] for entry in entries} == {receipt.change_set_id}
    assert {entry["commit_id"] for entry in entries} == {receipt.commit_id}
    assert receipt.lines[0].entity_id in {entry["entity_id"] for entry in entries}
    assert "Rowan" not in json.dumps(entries, default=str)
    assert committed_for_request(driver, world_id, key()) == []


def test_pending_lists_only_what_a_claim_could_take(driver, seeded, chat, frozen):
    world_id = seeded["world_id"]
    due = enqueued(driver, world_id, chat)
    frozen.advance(seconds=1)
    held = enqueued(driver, world_id, chat)
    plant_node(
        driver,
        world_id,
        "Request",
        id=key(),
        status="queued",
        source_id=chat["source_id"],
        retry_at=clock.now(world_id) + timedelta(hours=1),
    )
    plant_node(
        driver,
        world_id,
        "Request",
        id=key(),
        status="failed",
        source_id=chat["source_id"],
    )
    plant_node(
        driver,
        world_id,
        "Request",
        id=key(),
        status="applying",
        source_id=chat["source_id"],
        lease_until=clock.now(world_id) + timedelta(hours=1),
    )

    found = [state.request_id for state in pending_requests(driver, world_id)]

    assert found[:2] == [due, held]
    assert len(found) == 2
    assert [s.request_id for s in pending_requests(driver, world_id, limit=1)] == [due]
    assert len(surfaced_requests(driver, world_id)) == 1


def test_worlds_with_due_requests_names_only_the_worlds_with_work(driver, seeded, chat, make_world):
    world_id = seeded["world_id"]
    assert world_id not in worlds_with_due_requests(driver, limit=1000)

    request_id = enqueued(driver, world_id, chat)
    other = make_world()
    create_world(driver, other, owner_user_id="u_t_owner")
    enqueued(driver, other, make_source(driver, other, "whatsapp", CHAT_LINES))
    found = worlds_with_due_requests(driver, limit=1000)
    assert world_id in found and other in found
    assert len(worlds_with_due_requests(driver, limit=1)) == 1

    claim_next_request(driver, world_id, LEARNER, worker="pass-1")
    assert world_id not in worlds_with_due_requests(driver, limit=1000)

    finish_request(driver, world_id, LEARNER, request_id, worker="pass-1")
    assert world_id not in worlds_with_due_requests(driver, limit=1000)


def test_a_world_row_outside_the_id_form_does_not_stop_the_watcher(driver, seeded, chat):
    world_id = seeded["world_id"]
    enqueued(driver, world_id, chat)
    # A form the test helpers allow and check_world_id refuses: no dashes in the UUID.
    odd = f"t_writer-a2_{uuid4().hex}"
    plant_node(driver, odd, "World")
    try:
        assert world_id in worlds_with_due_requests(driver, limit=1000)
    finally:
        purge_test_world(driver, odd)


@pytest.mark.parametrize("limit", [0, -1, True, "5"])
def test_a_limit_is_a_positive_whole_number(limit):
    world_id = f"t_writer-a2_{uuid4()}"
    for call in (
        lambda: pending_requests(NoStore(), world_id, limit=limit),
        lambda: surfaced_requests(NoStore(), world_id, limit=limit),
        lambda: worlds_with_due_requests(NoStore(), limit=limit),
    ):
        with pytest.raises(ValueError):
            call()


# The world lock -------------------------------------------------------------------------------


def test_the_world_lock_is_the_first_thing_a_batch_takes(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    order: list[str] = []
    lock, run = WorldTx.lock_world, WorldTx.run

    def locked(self) -> None:
        order.append("lock_world")
        lock(self)

    def ran(self, cypher, /, **params):
        order.append("run")
        return run(self, cypher, **params)

    monkeypatch.setattr(WorldTx, "lock_world", locked)
    monkeypatch.setattr(WorldTx, "run", ran)

    apply_batch(driver, world_id, PERSON, one_item(chat))

    assert order[0] == "lock_world"
    assert order.count("lock_world") == 1


def test_the_world_lock_keeps_two_near_duplicate_creates_apart(driver, seeded, chat, monkeypatch):
    # The near-duplicate check takes no lock: with no world lock, each batch misses the other.
    world_id = seeded["world_id"]
    paused, release = threading.Event(), threading.Event()
    original = apply_module._near_duplicates

    def gated(*args, **kwargs):
        found = original(*args, **kwargs)
        if threading.current_thread().name == "first" and not paused.is_set():
            paused.set()
            assert release.wait(60)
        return found

    monkeypatch.setattr(apply_module, "_near_duplicates", gated)
    results: dict[str, Any] = {}

    def create(name: str, title: str, line: int) -> None:
        try:
            results[name] = apply_batch(driver, world_id, PERSON, one_task(chat, title, line))
        except BatchDeclined as declined:
            results[name] = declined

    first = threading.Thread(
        target=create, args=("first", "Fix the Ilse invoice mismatch", 1), name="first"
    )
    first.start()
    assert paused.wait(60)
    second = threading.Thread(
        target=create, args=("second", "Ilse invoice mismatch fix", 2), name="second"
    )
    second.start()
    second.join(3.0)
    waited = second.is_alive()
    release.set()
    first.join(90)
    second.join(90)

    assert waited, "the second batch did not wait for the world lock"
    assert results["first"].state == "saved"
    assert isinstance(results["second"], BatchDeclined)
    assert results["second"].refusals[0].code == "near_duplicate"
    assert titles(driver, world_id) == ["Fix the Ilse invoice mismatch"]


def one_task(chat: dict[str, str], title: str, line: int) -> dict:
    ev = f"e{line}"
    op = create_item("task", title, ev, status="open", recheck_by="2027-06-01T00:00:00+00:00")
    return batch([op], [observation(chat, ev, line)])


def test_the_world_lock_changes_no_sequence_and_a_rollback_leaves_no_gap(driver, seeded, chat):
    world_id = seeded["world_id"]

    def sequence() -> int:
        rows = raw(driver, "MATCH (w:World {id: $w}) RETURN w.next_sequence AS n", w=world_id)
        return rows[0]["n"]

    start = sequence()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat))
    assert sequence() == start

    def only_the_lock(tx: WorldTx) -> None:
        tx.lock_world()

    WorldWriter(driver, world_id).transaction(only_the_lock)
    assert sequence() == start


def test_the_world_lock_needs_a_world_row(driver, world):
    def locked(tx: WorldTx) -> None:
        assert tx.world_id == world
        tx.lock_world()

    with pytest.raises(UnknownWorld):
        WorldWriter(driver, world).transaction(locked)


def test_a_batch_in_a_world_with_no_row_writes_nothing(driver, world):
    src = {"episode_id": str(uuid4()), "source_id": str(uuid4())}
    sent = batch([create_item("person", "Rowan", "e1")], [observation(src, "e1", 1)])

    with pytest.raises(UnknownWorld):
        apply_batch(driver, world, PERSON, sent)

    assert count_rows(driver, world) == {"LINK": 0}
