"""D2 atomicity: a batch commits whole or not at all, and a failed commit is an error.

Spec, D2 "How to test it": "Atomicity: one invalid operation rolls back its whole batch, and an
injected commit failure returns an error, never a receipt."
"""

import threading
from itertools import count
from typing import Any

import pytest
from neo4j._sync.work.transaction import TransactionBase
from neo4j.exceptions import DatabaseError, IncompleteCommit, ServiceUnavailable

from grove import clock
from grove.ids import new_id
from grove.writer import (
    BatchDeclined,
    ReceiptUnavailable,
    WriteFailed,
    WriteOutcomeUnknown,
)
from grove.writer import api as api_module
from grove.writer import apply as apply_module
from grove.writer.history import Stamp
from grove.writer.tx import WorldTx, WorldWriter
from tests.writer.helpers import (
    PERSON,
    apply,
    assert_only_decline_log,
    at_minute,
    clock_at,
    count_rows,
    create_item,
    create_link,
    new_op_id,
    observation,
    raw,
    read,
    supports,
)

RECHECK = "2027-06-01T00:00:00+00:00"
_ITEM_WRITE = "CREATE (i:Item)"


def titles(driver, world_id: str) -> set[str]:
    return {row["t"] for row in read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t")}


def entries_by(driver, world_id: str, actor: str) -> int:
    rows = raw(
        driver,
        "MATCH (c:ChangeEntry {world_id: $w, actor: $a}) RETURN count(c) AS c",
        w=world_id,
        a=actor,
    )
    return rows[0]["c"]


def assert_nothing_written(driver, world_id: str, before: dict[str, int]) -> None:
    after = count_rows(driver, world_id)
    assert after == before
    # The seed wrote change entries and history values of its own; the batch may add none.
    assert after.get("ChangeEntry", 0) == before.get("ChangeEntry", 0)
    assert after.get("HistoryValue", 0) == before.get("HistoryValue", 0)
    assert entries_by(driver, world_id, PERSON.actor) == 0
    assert titles(driver, world_id) == set()


def assert_only_the_log(driver, world_id: str, before: dict[str, int], refusals: list) -> None:
    """A decline: nothing of the batch stands, and the decline's own log is all that changed."""
    assert_only_decline_log(driver, world_id, before, refusals)
    assert entries_by(driver, world_id, PERSON.actor) == 0
    assert titles(driver, world_id) == set()


def refused(driver, world_id: str, ops: list[dict], evidence: list[dict]) -> list:
    with pytest.raises(BatchDeclined) as declined:
        apply(driver, world_id, ops, evidence)
    return declined.value.refusals


def test_one_invalid_operation_last_of_five_rolls_back_the_whole_batch(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("person", "Ilse", "e1", temp_id="$ilse"),
        create_item("place", "Okonkwo boathouse", "e1", temp_id="$boathouse"),
        create_link("$rowan", "lives in", "$boathouse", "e1", recheck_by=RECHECK),
        create_link("$ilse", "admires quietly", "$boathouse", "e1", recheck_by=RECHECK),
    ]
    before = count_rows(driver, world_id)

    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])

    assert [(r.op_id, r.code) for r in refusals] == [(ops[4]["op_id"], "unknown_meaning")]
    assert_only_the_log(driver, world_id, before, refusals)


def test_a_refusal_in_the_middle_names_only_the_failing_operation(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item("person", "Rowan", "e1"),
        create_item("spaceship", "The silver hull", "e1"),
        create_item("person", "Ilse", "e1"),
        create_item("place", "Okonkwo boathouse", "e1"),
    ]
    before = count_rows(driver, world_id)

    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])

    assert [(r.op_id, r.code) for r in refusals] == [(ops[1]["op_id"], "unknown_kind")]
    assert_only_the_log(driver, world_id, before, refusals)


def test_two_failing_operations_are_both_named_in_one_decline(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item("spaceship", "The silver hull", "e1"),
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("place", "Okonkwo boathouse", "e1", temp_id="$boathouse"),
        create_link("$rowan", "admires quietly", "$boathouse", "e1"),
    ]
    before = count_rows(driver, world_id)

    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])

    assert [(r.op_id, r.code) for r in refusals] == [
        (ops[0]["op_id"], "unknown_kind"),
        (ops[3]["op_id"], "unknown_meaning"),
    ]
    assert_only_the_log(driver, world_id, before, refusals)


def test_an_operation_on_a_declined_temp_id_is_refused_as_depending_on_it(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("spaceship", "The silver hull", "e1", temp_id="$hull"),
        create_link("$hull", "part of", "$rowan", "e1"),
    ]
    before = count_rows(driver, world_id)

    refusals = {r.op_id: r for r in refused(driver, world_id, ops, [observation(chat, "e1", 1)])}

    assert set(refusals) == {ops[1]["op_id"], ops[2]["op_id"]}
    assert refusals[ops[1]["op_id"]].code == "unknown_kind"
    assert refusals[ops[2]["op_id"]].rule == "depends_on_declined"
    assert_only_the_log(driver, world_id, before, list(refusals.values()))


def test_an_injected_commit_failure_raises_write_failed_and_returns_no_receipt(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise DatabaseError("injected commit failure")

    monkeypatch.setattr(apply_module, "_before_commit", fail)
    before = count_rows(driver, world_id)

    with pytest.raises(WriteFailed):
        apply(
            driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)]
        )

    assert calls, "the hook never ran, so no commit failure was injected"
    assert_nothing_written(driver, world_id, before)


class _Crash(Exception):
    """Raised by the seam, so the transaction rolls back."""


def test_a_caller_that_owns_its_transaction_gets_the_seam_last(driver, seeded, chat, monkeypatch):
    # Sign-in calls apply_ops in its own transaction and proves its atomicity through this seam.
    world_id = seeded["world_id"]
    calls = []

    def crash() -> None:
        calls.append(1)
        raise _Crash

    monkeypatch.setattr(apply_module, "_before_commit", crash)
    before = count_rows(driver, world_id)
    stamp = Stamp(
        world_id=world_id,
        at=clock.now(world_id),
        actor=PERSON.actor,
        commit_id=new_id(world_id),
        change_set_id=new_id(world_id),
        request_id="own-tx-1",
    )

    def work(tx: WorldTx) -> None:
        apply_module.apply_ops(
            tx,
            stamp=stamp,
            ops=[create_item("person", "Rowan", "e1")],
            observations={"e1": observation(chat, "e1", 1)},
            pre_refused={},
        )

    with pytest.raises(_Crash):
        WorldWriter(driver, world_id).transaction(work)

    assert calls == [1]
    assert_nothing_written(driver, world_id, before)


def test_apply_batch_runs_the_seam_once(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]
    calls = []
    monkeypatch.setattr(apply_module, "_before_commit", lambda: calls.append(1))

    apply(driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)])

    assert calls == [1]


def test_a_commit_failure_in_a_second_batch_keeps_the_first_and_writes_nothing_more(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    calls = count()

    def fail_on_the_second_call(*args, **kwargs):
        if next(calls) == 1:
            raise DatabaseError("injected commit failure")

    monkeypatch.setattr(apply_module, "_before_commit", fail_on_the_second_call)
    first = apply(
        driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)]
    )
    assert first.state == "saved"
    after_first = count_rows(driver, world_id)

    with pytest.raises(WriteFailed):
        apply(driver, world_id, [create_item("person", "Ilse", "e2")], [observation(chat, "e2", 2)])

    assert count_rows(driver, world_id) == after_first
    assert titles(driver, world_id) == {"Rowan"}


def test_a_failure_in_a_later_statement_of_the_transaction_leaves_no_row(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    original = WorldTx.run
    seen = {"wrote": False, "failed": False}

    def run(self, cypher, /, **params):
        if seen["wrote"] and not seen["failed"]:
            seen["failed"] = True
            raise DatabaseError("injected failure after a write statement")
        rows = original(self, cypher, **params)
        # The request row's MERGE comes first in the batch: this test is about the batch's rows.
        if _ITEM_WRITE in cypher:
            seen["wrote"] = True
        return rows

    monkeypatch.setattr(WorldTx, "run", run)
    ops = [
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("place", "Okonkwo boathouse", "e1", temp_id="$boathouse"),
        create_link("$rowan", "lives in", "$boathouse", "e1", recheck_by=RECHECK),
    ]
    before = count_rows(driver, world_id)

    with pytest.raises(WriteFailed):
        apply(driver, world_id, ops, [observation(chat, "e1", 1)])

    assert seen["failed"], "no statement ran after a write, so nothing was rolled back"
    assert_nothing_written(driver, world_id, before)


def test_a_fault_in_the_writer_code_propagates_as_itself_and_writes_nothing(
    driver, seeded, chat, monkeypatch
):
    # Only a store or driver failure is WriteFailed (retryable). A bug in the writer's own code
    # must surface as itself, never as a retryable "unavailable", and must still write nothing.
    world_id = seeded["world_id"]

    class WriterBug(KeyError):
        pass

    def fail(*args, **kwargs):
        raise WriterBug("injected writer fault")

    monkeypatch.setattr(apply_module, "_before_commit", fail)
    before = count_rows(driver, world_id)

    with pytest.raises(WriterBug):
        apply(
            driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)]
        )

    assert_nothing_written(driver, world_id, before)


@pytest.mark.parametrize(
    ("status", "statement_class", "code"),
    [
        pytest.param("open", "report_state", "missing_recheck", id="end_of_batch_recheck"),
        pytest.param(
            "done", "report_happened", "terminal_needs_confirmation", id="end_of_batch_done"
        ),
    ],
)
def test_a_dependent_of_an_operation_refused_at_the_end_of_the_batch_is_named(
    driver, seeded, chat, status, statement_class, code
):
    # README: "an operation that depends on a declined one is refused with rule
    # depends_on_declined", also when the refusal comes from the checks at the end of the batch.
    world_id = seeded["world_id"]
    ops = [
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("task", "Book the venue", "e1", temp_id="$task", status=status),
        create_link("$task", "part of", "$rowan", "e1", temp_id="$part"),
        create_link("$rowan", "related to", "$task", "e1", recheck_by=RECHECK),
    ]
    before = count_rows(driver, world_id)

    evidence = [observation(chat, "e1", 1, statement_class=statement_class)]
    refusals = {r.op_id: r for r in refused(driver, world_id, ops, evidence)}

    assert set(refusals) == {ops[1]["op_id"], ops[2]["op_id"], ops[3]["op_id"]}
    assert refusals[ops[1]["op_id"]].code == code
    assert refusals[ops[2]["op_id"]].rule == "depends_on_declined"
    assert refusals[ops[3]["op_id"]].rule == "depends_on_declined"
    assert_only_the_log(driver, world_id, before, list(refusals.values()))


def _commit_then(error: Exception, monkeypatch) -> list[int]:
    """Let the real commit happen, then raise: a connection lost before the commit's reply."""
    original = TransactionBase._commit
    calls: list[int] = []

    def commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        calls.append(1)
        raise error

    monkeypatch.setattr(TransactionBase, "_commit", commit)
    return calls


def test_a_commit_with_an_unknown_outcome_is_never_a_retryable_nothing_written(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    calls = _commit_then(IncompleteCommit("injected: connection lost during commit"), monkeypatch)

    with pytest.raises(WriteOutcomeUnknown) as caught:
        apply(
            driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)]
        )

    monkeypatch.undo()
    assert calls == [1]
    error = caught.value
    assert not isinstance(error, WriteFailed)
    assert error.retryable is False
    entries = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {change_set_id: $id}) RETURN count(c) AS c",
        id=error.change_set_id,
    )
    assert entries[0]["c"] > 0, "the commit happened; the change set must be found by its id"
    assert titles(driver, world_id) == {"Rowan"}


def test_a_receipt_that_cannot_be_read_after_the_commit_says_the_batch_is_saved(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the read after the commit failed")

    monkeypatch.setattr(api_module, "read_receipt", fail)

    with pytest.raises(ReceiptUnavailable) as caught:
        apply(
            driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)]
        )

    assert caught.value.retryable is False
    assert caught.value.change_set_id
    assert titles(driver, world_id) == {"Rowan"}


def test_create_link_checks_its_endpoints_under_the_write_lock(driver, seeded, chat, monkeypatch):
    # apply.py: "An operation reads and locks the rows it touches, checks them, and only then
    # writes." An end_item on the target waits until create_link has checked and committed. The
    # world lock would serialise the two batches on its own, so it is off: the item lock is what
    # this test proves.
    monkeypatch.setattr(WorldTx, "lock_world", lambda self: None)
    world_id = seeded["world_id"]
    work = {"status": "open", "recheck_by": RECHECK}
    apply(
        driver,
        world_id,
        [
            create_item("task", "Print the invitations", "e1", temp_id="$a", **work),
            create_item("task", "Book the venue", "e1", temp_id="$b", **work),
        ],
        [observation(chat, "e1", 1)],
    )
    ids = {
        row["t"]: row["id"]
        for row in read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t, i.id AS id")
    }
    first, second = ids["Print the invitations"], ids["Book the venue"]

    paused, release = threading.Event(), threading.Event()
    original = apply_module._fits

    def gated(declared, kind):
        if threading.current_thread().name == "linker" and not paused.is_set():
            paused.set()
            release.wait(20)
        return original(declared, kind)

    monkeypatch.setattr(apply_module, "_fits", gated)
    results: dict[str, Any] = {}

    def link() -> None:
        op = create_link(first, "blocks", second, "e2", recheck_by=RECHECK)
        results["link"] = apply(driver, world_id, [op], [observation(chat, "e2", 2)])

    def end() -> None:
        op = {
            "op": "end_item",
            "op_id": new_op_id(),
            "item": second,
            "end_class": "ended_in_the_world",
            "stopped_being_true": clock_at(at_minute(3)),
            "evidence_refs": supports("e3"),
            "reason": "the venue closed",
        }
        results["end"] = apply(driver, world_id, [op], [observation(chat, "e3", 3)])

    linker = threading.Thread(target=link, name="linker")
    linker.start()
    try:
        assert paused.wait(20), "create_link never reached its endpoint checks"
        ender = threading.Thread(target=end, name="ender")
        ender.start()
        ender.join(2.0)
        ended_while_checking = not ender.is_alive()
    finally:
        release.set()
    linker.join(30)
    ender.join(30)

    assert not ended_while_checking, "end_item committed while create_link held its checks"
    assert results["link"].state == "saved"
    assert results["end"].state == "saved"
    order = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry) WHERE c.change_set_id IN $ids "
        "RETURN c.change_set_id AS id, min(c.sequence) AS first",
        ids=[results["link"].change_set_id, results["end"].change_set_id],
    )
    first_seen = {row["id"]: row["first"] for row in order}
    assert first_seen[results["link"].change_set_id] < first_seen[results["end"].change_set_id]
