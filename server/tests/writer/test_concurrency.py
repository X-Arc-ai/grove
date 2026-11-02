"""D2 "Concurrency" (spec line 774) and the writer's locks under set interleavings.

Line 774: "Concurrency: when two agents update one item at once, both changes land or one retries,
and no update is lost." Line 2029: "A maintenance run that proposes a change to an item the person
has changed since the run began fails the writer's version check, re-reads and tries once more."

Every race here is set with threading.Event pauses inside an open transaction, never with timing.
A wait is proven by the store, which names each transaction blocked on a lock and the statement it
waits in (harness.wait_until_blocked). The world lock (S40) orders every batch of a world; the
tests of the item and link locks, the belts under it, patch it to a no-op.
"""

import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove import clock
from grove.clock import TestClock
from grove.writer import (
    BACKOFF_SECONDS,
    BatchDeclined,
    Receipt,
    WriteFailed,
    apply_batch,
    apply_request,
    claim_next_request,
    committed_for_request,
    enqueue_request,
)
from grove.writer import apply as apply_module
from grove.writer.tx import WorldTx
from tests.writer.harness import wait_until_blocked
from tests.writer.helpers import (
    CURATOR,
    HOST,
    LEARNER,
    PERSON,
    at_minute,
    batch,
    clock_at,
    create_item,
    create_link,
    created_id,
    new_op_id,
    observation,
    raw,
    read,
    request_key,
    request_row,
    supports,
)

T = datetime(2031, 3, 4, 9, 0, tzinfo=UTC)
RECHECK = "2031-06-01T00:00:00+00:00"

# The statements a waiter is blocked in, as the store prints them.
WORLD_LOCK = "SET w.lock_marker = true"
SEQUENCE = "SET w.next_sequence = w.next_sequence + 1"
ITEM_LOCK = "SET i.touched_at = $at"
LINK_LOCK = "SET l.touched_at = $at"
ANY_STATEMENT = ""


@pytest.fixture
def frozen():
    test_clock = TestClock()
    test_clock.set(T)
    with clock.using(test_clock):
        yield test_clock


def no_world_lock(monkeypatch) -> None:
    monkeypatch.setattr(WorldTx, "lock_world", lambda self: None)


def no_item_locks(monkeypatch) -> None:
    """create_link locks both end items before it reads the link, and the store's commit of a
    change to a link takes that link's end items too: an end_link that commits meanwhile waits for
    the reader, the store aborts the younger transaction as a deadlock, and the driver runs it
    again from the start (probed on the store). The reads under the link's lock are the belt behind
    that; this makes them the only one."""

    def read_only(run, item_id):
        return apply_module._one(run, apply_module._READ_ITEM, "item", item_id, lock=False)

    monkeypatch.setattr(apply_module, "_lock_item", read_only)


def attempts(monkeypatch) -> Counter:
    """How many times each thread ran its batch: the driver runs a transaction again after a
    transient failure, and a second run reads everything afresh."""
    seen: Counter = Counter()
    apply_ops = apply_module.apply_ops

    def counted(tx, **parts):
        seen[threading.current_thread().name] += 1
        return apply_ops(tx, **parts)

    monkeypatch.setattr(apply_module, "apply_ops", counted)
    return seen


class Gate:
    """Holds the thread of this name at a seam, inside its open transaction, until the test opens
    the gate. Every other thread passes. `seen` keeps what the held thread had read."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reached = threading.Event()
        self.opened = threading.Event()
        self.seen: Any = None

    def __call__(self, seen: Any = None) -> None:
        if threading.current_thread().name == self.name and not self.reached.is_set():
            self.seen = seen
            self.reached.set()
            assert self.opened.wait(120), "the test never opened the gate"

    def after(self, call: Callable[..., Any]) -> Callable[..., Any]:
        """`call`, then the gate: the thread holds what `call` read and locked."""

        def gated(*args: Any, **kwargs: Any) -> Any:
            found = call(*args, **kwargs)
            self(found)
            return found

        return gated

    def before(self, call: Callable[..., Any], cypher: str) -> Callable[..., Any]:
        """The gate, then `call`, when the thread is about to run this statement through
        apply._one: the thread has read with no lock and does not hold this lock yet."""

        def gated(run: Any, statement: str, *args: Any, **kwargs: Any) -> Any:
            if statement == cypher:
                self(args)
            return call(run, statement, *args, **kwargs)

        return gated


class Run:
    """A call on its own named thread. `done` (and `also`, when given) is set when it ends."""

    def __init__(
        self, name: str, call: Callable[[], Any], also: threading.Event | None = None
    ) -> None:
        self.done = threading.Event()
        self._also = also
        self._result: Any = None
        self._error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(call,), name=name, daemon=True)
        self.thread.start()

    def _run(self, call: Callable[[], Any]) -> None:
        try:
            self._result = call()
        except BaseException as error:
            self._error = error
        finally:
            self.done.set()
            if self._also is not None:
                self._also.set()

    def outcome(self) -> Any:
        """The receipt or state, or the BatchDeclined; any other error is raised here."""
        assert self.done.wait(120), f"{self.thread.name} never finished"
        if isinstance(self._error, BatchDeclined):
            return self._error
        if self._error is not None:
            raise self._error
        return self._result


def task(chat: dict[str, str], title: str, line: int, **fields: Any) -> dict[str, Any]:
    ev = f"e{line}"
    op = create_item("task", title, ev, status="open", recheck_by=RECHECK, **fields)
    return batch([op], [observation(chat, ev, line)])


def made(driver, world_id: str, chat: dict[str, str], *kinds_titles: tuple[str, str]) -> list[str]:
    """Items of these kinds and titles in one batch; their ids in order."""
    ops = []
    for kind, title in kinds_titles:
        work = {"status": "open", "recheck_by": RECHECK} if kind == "task" else {}
        ops.append(create_item(kind, title, "e1", **work))
    receipt = apply_batch(driver, world_id, PERSON, batch(ops, [observation(chat, "e1", 1)]))
    return [created_id(receipt, nth=n) for n in range(len(kinds_titles))]


def update(
    chat: dict[str, str], item_id: str, version: int, field: str, value: str, line: int
) -> dict[str, Any]:
    """One update_item at the version the agent read, cited to one line of the chat."""
    ev = f"u{line}"
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item_id,
        "version": version,
        "fields": {field: value},
        "evidence_refs": supports(ev),
        "reason": "the message states it",
    }
    return batch([op], [observation(chat, ev, line)])


def end_link(link_id: str, ev: str, minute: int) -> dict[str, Any]:
    return {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": link_id,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(minute)),
        "evidence_refs": supports(ev),
        "reason": "the message says it ended",
    }


def item_row(driver, world_id: str, item_id: str) -> dict[str, Any]:
    return read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)[0]["i"]


def link_row(driver, world_id: str, link_id: str) -> dict[str, Any]:
    rows = read(driver, world_id, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link_id)
    return rows[0]["l"]


def the_links(driver, world_id: str, meaning: str) -> list[dict[str, Any]]:
    return [
        row["l"]
        for row in read(
            driver,
            world_id,
            "MATCH (:Item)-[l:LINK]->(:Item) MATCH (m:Meaning {id: l.meaning}) "
            "WHERE m.name = $name RETURN l ORDER BY l.recorded, l.id",
            name=meaning,
        )
    ]


def sequence(driver, world_id: str) -> int:
    return raw(driver, "MATCH (w:World {id: $w}) RETURN w.next_sequence AS n", w=world_id)[0]["n"]


# Two agents, one item --------------------------------------------------------------------------


@pytest.mark.parametrize("world_lock", ["on", "off"])
def test_two_agents_update_one_item_the_loser_retries_and_both_changes_stand(
    driver, seeded, chat, frozen, monkeypatch, world_lock
):
    world_id = seeded["world_id"]
    waits_in = WORLD_LOCK
    if world_lock == "off":
        # The item lock alone must order the two: the loser waits for it, then reads the version.
        no_world_lock(monkeypatch)
        waits_in = ITEM_LOCK
    [venue] = made(driver, world_id, chat, ("task", "Book the venue for the Okonkwo offsite"))
    first, second = request_key(), request_key()
    enqueue_request(driver, world_id, HOST, first, source_id=chat["source_id"])
    frozen.advance(seconds=1)
    enqueue_request(driver, world_id, HOST, second, source_id=chat["source_id"])
    assert claim_next_request(driver, world_id, LEARNER, worker="agent-a").id == first
    assert claim_next_request(driver, world_id, CURATOR, worker="agent-b").id == second
    # Both agents read the item before either writes.
    seen = item_row(driver, world_id, venue)["version"]
    summary = update(chat, venue, seen, "summary", "Rowan holds the deposit", 5)
    body = update(chat, venue, seen, "body", "Ilse checks the floor plan", 6)

    gate = Gate("agent-a")
    monkeypatch.setattr(apply_module, "_before_commit", gate)
    agent_a = Run("agent-a", lambda: apply_request(driver, world_id, LEARNER, first, summary))
    assert gate.reached.wait(60)
    agent_b = Run("agent-b", lambda: apply_request(driver, world_id, CURATOR, second, body))
    waited = wait_until_blocked(driver, world_id, waits_in, gave_up=agent_b.done)
    gate.opened.set()
    won, lost = agent_a.outcome(), agent_b.outcome()

    assert waited, "agent b did not wait for agent a's lock"
    assert won.api_state == "saved"
    assert (lost.api_state, lost.status, lost.last_error_code) == (
        "saving",
        "queued",
        "version_conflict",
    )
    row = request_row(driver, world_id, second)
    assert (row["status"], row["attempts"], row["last_error_code"]) == (
        "queued",
        1,
        "version_conflict",
    )
    assert row["retry_at"] == frozen.now(world_id) + timedelta(seconds=BACKOFF_SECONDS[0])
    assert row.get("claimed_by") is None and row.get("lease_until") is None
    logged = read(
        driver, world_id, "MATCH (d:Decline {request_id: $id}) RETURN d.rule AS rule", id=second
    )
    assert [entry["rule"] for entry in logged] == ["version_conflict"]
    stored = item_row(driver, world_id, venue)
    assert (stored["version"], stored["summary"], stored.get("body")) == (
        seen + 1,
        "Rowan holds the deposit",
        None,
    )

    # The back-off holds the loser until its retry time; then a new pass re-reads and lands.
    assert claim_next_request(driver, world_id, CURATOR, worker="agent-b") is None
    frozen.set(row["retry_at"])
    again = claim_next_request(driver, world_id, CURATOR, worker="agent-b")
    assert (again.id, again.attempts, again.last_error_code) == (second, 2, "version_conflict")
    fresh = item_row(driver, world_id, venue)["version"]
    retried = update(chat, venue, fresh, "body", "Ilse checks the floor plan", 6)
    landed = apply_request(driver, world_id, CURATOR, second, retried)

    assert landed.api_state == "saved"
    stored = item_row(driver, world_id, venue)
    assert (stored["version"], stored["summary"], stored["body"]) == (
        seen + 2,
        "Rowan holds the deposit",
        "Ilse checks the floor plan",
    )
    changes = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {entity_id: $id}) WHERE c.field IN ['summary', 'body'] "
        "RETURN c.field AS field, c.request_id AS request ORDER BY c.sequence",
        id=venue,
    )
    assert [(entry["field"], entry["request"]) for entry in changes] == [
        ("summary", first),
        ("body", second),
    ]


# N replays of one request ------------------------------------------------------------------------


@pytest.mark.parametrize("world_lock", ["on", "off"])
def test_n_threads_replaying_one_request_give_one_effect(
    driver, seeded, chat, monkeypatch, world_lock
):
    # With no world lock, the request row's MERGE alone orders the deliveries of one id.
    if world_lock == "off":
        no_world_lock(monkeypatch)
    world_id, request_id = seeded["world_id"], request_key()
    sent = batch(
        [
            create_item("person", "Tamsin", "e3", temp_id="$t"),
            create_item(
                "task",
                "Print the name cards for Rowan and Ilse",
                "e3",
                temp_id="$c",
                status="open",
                recheck_by=RECHECK,
            ),
            create_link("$c", "assigned to", "$t", "e3", recheck_by=RECHECK),
        ],
        [observation(chat, "e3", 3)],
    )
    start = sequence(driver, world_id)
    gate = Gate("replay-0")
    monkeypatch.setattr(apply_module, "_before_commit", gate)

    def deliver() -> Receipt:
        return apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    runs = [Run("replay-0", deliver)]
    assert gate.reached.wait(60)
    one_ended = threading.Event()
    runs += [Run(f"replay-{n}", deliver, also=one_ended) for n in range(1, 6)]
    # None found a saved row before the lock, so each must wait for the first one's commit.
    waited = wait_until_blocked(driver, world_id, ANY_STATEMENT, count=5, gave_up=one_ended)
    gate.opened.set()
    receipts = [run.outcome() for run in runs]

    assert waited, "a replay finished while the first delivery held its transaction"
    assert all(isinstance(receipt, Receipt) for receipt in receipts), receipts
    assert {receipt.change_set_id for receipt in receipts} == {receipts[0].change_set_id}
    assert all(receipt == receipts[0] for receipt in receipts)
    titles = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t ORDER BY t")
    assert [row["t"] for row in titles] == ["Print the name cards for Rowan and Ilse", "Tamsin"]
    assert len(the_links(driver, world_id, "assigned to")) == 1
    entries = committed_for_request(driver, world_id, request_id)
    assert {entry["change_set_id"] for entry in entries} == {receipts[0].change_set_id}
    assert sequence(driver, world_id) - start == len(entries)
    assert request_row(driver, world_id, request_id)["attempts"] == 1


# Two near-duplicate creates at once ---------------------------------------------------------------


def test_two_near_duplicate_creates_at_once_one_commits_and_the_other_is_refused(
    driver, seeded, chat, monkeypatch
):
    # The near-duplicate check takes no lock of its own: the world lock is what orders the two.
    world_id = seeded["world_id"]
    gate = Gate("first")
    monkeypatch.setattr(apply_module, "_near_duplicates", gate.after(apply_module._near_duplicates))

    first = Run(
        "first",
        lambda: apply_batch(
            driver, world_id, PERSON, task(chat, "Fix the Ilse invoice mismatch", 1)
        ),
    )
    assert gate.reached.wait(60)
    second = Run(
        "second",
        lambda: apply_batch(driver, world_id, PERSON, task(chat, "Ilse invoice mismatch fix", 2)),
    )
    waited = wait_until_blocked(driver, world_id, WORLD_LOCK, gave_up=second.done)
    gate.opened.set()
    saved, refused = first.outcome(), second.outcome()

    assert isinstance(saved, Receipt)
    assert isinstance(refused, BatchDeclined), "both near-duplicates committed"
    assert waited, "the second create ran while the first held its near-duplicate check"
    [refusal] = refused.refusals
    assert (refusal.code, refusal.rule) == ("near_duplicate", "near_duplicate")
    assert [entry["item_id"] for entry in refusal.details["candidates"]] == [created_id(saved)]
    titles = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS t")
    assert [row["t"] for row in titles] == ["Fix the Ilse invoice mismatch"]


# The world lock and the sequence (S10, S40) -------------------------------------------------------


def sequences(driver, world_id: str) -> list[int]:
    rows = read(driver, world_id, "MATCH (c:ChangeEntry) RETURN c.sequence AS s ORDER BY s")
    return [row["s"] for row in rows]


def test_a_batch_that_waited_for_the_world_lock_takes_the_next_free_sequence(
    driver, seeded, chat, monkeypatch
):
    # Regression: the lock was `SET w.next_sequence = w.next_sequence`, which reads the value
    # before it waits and writes it back after, so the waiter set the counter back and its first
    # change entry took a sequence in use: a ConstraintError, and the batch did not commit.
    world_id = seeded["world_id"]
    gate = Gate("first")
    monkeypatch.setattr(apply_module, "_before_commit", gate)

    first = Run(
        "first", lambda: apply_batch(driver, world_id, PERSON, task(chat, "Book the venue", 1))
    )
    assert gate.reached.wait(60)
    second = Run(
        "second",
        lambda: apply_batch(driver, world_id, PERSON, task(chat, "Print the name cards", 2)),
    )
    waited = wait_until_blocked(driver, world_id, WORLD_LOCK, gave_up=second.done)
    gate.opened.set()
    one, two = first.outcome(), second.outcome()

    assert isinstance(one, Receipt) and isinstance(two, Receipt)
    taken = sequences(driver, world_id)
    assert len(taken) == len(set(taken))
    assert taken == list(range(taken[0], taken[0] + len(taken)))
    assert sequence(driver, world_id) == taken[-1]
    assert waited, "the second batch did not wait for the world lock"


def test_a_commit_with_no_change_entry_after_a_wait_leaves_the_sequence_where_it_was(
    driver, seeded, chat, monkeypatch
):
    # The worse half of the same regression: a waiter that commits no change entry kept the
    # counter it set back, and every later batch of the world collided with a used sequence.
    world_id = seeded["world_id"]
    [venue] = made(driver, world_id, chat, ("task", "Book the venue"))
    # The message is on the item already: the batch commits and writes no change entry.
    known = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": venue,
        "ev_id": "e1",
        "polarity": "supports",
    }
    gate = Gate("first")
    monkeypatch.setattr(apply_module, "_before_commit", gate)

    first = Run(
        "first",
        lambda: apply_batch(driver, world_id, PERSON, task(chat, "Print the name cards", 2)),
    )
    assert gate.reached.wait(60)
    second = Run(
        "second",
        lambda: apply_batch(driver, world_id, PERSON, batch([known], [observation(chat, "e1", 1)])),
    )
    waited = wait_until_blocked(driver, world_id, WORLD_LOCK, gave_up=second.done)
    gate.opened.set()
    one, two = first.outcome(), second.outcome()

    assert isinstance(one, Receipt) and isinstance(two, Receipt) and two.lines == ()
    assert sequence(driver, world_id) == sequences(driver, world_id)[-1]
    later = apply_batch(driver, world_id, PERSON, task(chat, "Draft the seating plan", 3))
    assert later.state == "saved"
    taken = sequences(driver, world_id)
    assert len(taken) == len(set(taken))
    assert waited, "the second batch did not wait for the world lock"


# The second interleaving harness: the link re-reads, with no world lock --------------------------
#
# create_link reads the active link with no lock, and the slot's holders too; the reads again
# under the link's lock (the restatement in create_link, and _end_prior) make sure a link that a
# concurrent end_link ended is neither restated nor ended a second time (D1). With the world lock
# off, the first two tests run with the item locks as they are; the last two take those off too,
# so the reads under the link's lock are all that stands, and the held thread runs its batch once.


def restatement_cast(driver, world_id: str, chat: dict[str, str]) -> tuple[str, str, str]:
    venue, cards = made(
        driver,
        world_id,
        chat,
        ("task", "Book the venue for the Okonkwo offsite"),
        ("task", "Print the name cards for Rowan and Ilse"),
    )
    held = apply_batch(
        driver,
        world_id,
        PERSON,
        batch([create_link(cards, "part of", venue, "e3")], [observation(chat, "e3", 3)]),
    )
    return venue, cards, created_id(held, ">", "link")


def restate(driver, world_id: str, chat: dict[str, str], venue: str, cards: str, key: str):
    return apply_batch(
        driver,
        world_id,
        PERSON,
        batch([create_link(cards, "part of", venue, "e5")], [observation(chat, "e5", 5)]),
        request_id=key,
    )


def end(driver, world_id: str, chat: dict[str, str], link_id: str) -> Receipt:
    return apply_batch(
        driver, world_id, PERSON, batch([end_link(link_id, "e4", 4)], [observation(chat, "e4", 4)])
    )


def assert_a_new_link_and_none_restated(
    driver, world_id: str, link_id: str, venue: str, cards: str, key: str, restated: Receipt
) -> None:
    old = link_row(driver, world_id, link_id)
    assert (old.get("active_key"), old["end_class"]) == (None, "ended_in_the_world")
    on_old = read(
        driver,
        world_id,
        "MATCH (e:Evidence {target_id: $id}) RETURN e.request_id AS request",
        id=link_id,
    )
    assert key not in {row["request"] for row in on_old}
    assert not [line for line in restated.lines if line.entity_id == link_id]
    new = link_row(driver, world_id, created_id(restated, ">", "link"))
    assert new["active_key"] == f"{world_id}|{cards}|{old['meaning']}|{venue}"
    assert (new["from_item"], new["to_item"]) == (cards, venue)


def reassignment_cast(driver, world_id: str, chat: dict[str, str]) -> tuple[str, str, str]:
    venue, tamsin, rowan = made(
        driver,
        world_id,
        chat,
        ("task", "Book the venue for the Okonkwo offsite"),
        ("person", "Tamsin"),
        ("person", "Rowan"),
    )
    held = apply_batch(
        driver,
        world_id,
        PERSON,
        batch(
            [create_link(venue, "assigned to", tamsin, "e3", recheck_by=RECHECK)],
            [observation(chat, "e3", 3)],
        ),
    )
    return venue, rowan, created_id(held, ">", "link")


def reassign(driver, world_id: str, chat: dict[str, str], venue: str, rowan: str) -> Receipt:
    return apply_batch(
        driver,
        world_id,
        PERSON,
        batch(
            [create_link(venue, "assigned to", rowan, "e5", recheck_by=RECHECK)],
            [observation(chat, "e5", 5)],
        ),
    )


def assert_the_end_stands(
    driver, world_id: str, prior: str, rowan: str, ended: Receipt, reassigned: Receipt
) -> None:
    old = link_row(driver, world_id, prior)
    assert (old.get("active_key"), old["end_class"]) == (None, "ended_in_the_world")
    assert old["stopped_being_true_earliest"] == at_minute(4)
    ends = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {entity_id: $id}) WHERE c.field IN ['end_class', "
        "'stopped_being_true'] RETURN c.change_set_id AS change_set",
        id=prior,
    )
    assert {row["change_set"] for row in ends} == {ended.change_set_id}
    assert [line.symbol for line in reassigned.lines] == [">"]
    new = link_row(driver, world_id, created_id(reassigned, ">", "link"))
    assert (new["to_item"], new.get("end_class")) == (rowan, None)
    assert [
        link["id"] for link in the_links(driver, world_id, "assigned to") if link.get("active_key")
    ] == [new["id"]]


def test_a_restatement_that_waited_for_a_concurrent_end_link_makes_a_new_link(
    driver, seeded, chat, monkeypatch
):
    no_world_lock(monkeypatch)
    world_id = seeded["world_id"]
    venue, cards, link_id = restatement_cast(driver, world_id, chat)
    gate = Gate("ender")
    monkeypatch.setattr(apply_module, "_before_commit", gate)
    restating = request_key()

    ender = Run("ender", lambda: end(driver, world_id, chat, link_id))
    assert gate.reached.wait(60)
    restater = Run("restater", lambda: restate(driver, world_id, chat, venue, cards, restating))
    waited = wait_until_blocked(driver, world_id, LINK_LOCK, gave_up=restater.done)
    gate.opened.set()
    ended, restated = ender.outcome(), restater.outcome()

    assert isinstance(ended, Receipt) and isinstance(restated, Receipt)
    assert_a_new_link_and_none_restated(
        driver, world_id, link_id, venue, cards, restating, restated
    )
    assert waited, "the restatement did not wait for the link's lock"


def test_a_reassignment_that_waited_for_a_concurrent_end_link_leaves_that_end_standing(
    driver, seeded, chat, monkeypatch
):
    no_world_lock(monkeypatch)
    world_id = seeded["world_id"]
    venue, rowan, prior = reassignment_cast(driver, world_id, chat)
    gate = Gate("ender")
    monkeypatch.setattr(apply_module, "_before_commit", gate)

    ender = Run("ender", lambda: end(driver, world_id, chat, prior))
    assert gate.reached.wait(60)
    reassigner = Run("reassigner", lambda: reassign(driver, world_id, chat, venue, rowan))
    # It read the prior as the slot's holder with no lock; the ender holds the world's sequence.
    waited = wait_until_blocked(driver, world_id, SEQUENCE, gave_up=reassigner.done)
    gate.opened.set()
    ended, reassigned = ender.outcome(), reassigner.outcome()

    assert isinstance(ended, Receipt) and isinstance(reassigned, Receipt)
    assert_the_end_stands(driver, world_id, prior, rowan, ended, reassigned)
    assert waited, "the reassignment did not wait for the end_link's commit"


def test_the_restatement_read_under_the_link_lock_alone_sees_an_end_that_committed_since(
    driver, seeded, chat, monkeypatch
):
    no_world_lock(monkeypatch)
    no_item_locks(monkeypatch)
    world_id = seeded["world_id"]
    venue, cards, link_id = restatement_cast(driver, world_id, chat)
    ran = attempts(monkeypatch)
    gate = Gate("restater")
    monkeypatch.setattr(
        apply_module, "_one", gate.before(apply_module._one, apply_module._LOCK_LINK)
    )
    restating = request_key()

    restater = Run("restater", lambda: restate(driver, world_id, chat, venue, cards, restating))
    # It found the link active with no lock, and stops before it takes the link's lock.
    assert gate.reached.wait(60)
    assert gate.seen == ("link", link_id)
    ended = end(driver, world_id, chat, link_id)
    gate.opened.set()
    restated = restater.outcome()

    assert isinstance(restated, Receipt)
    assert ran["restater"] == 1, "the batch ran again: its read under the lock went untested"
    assert_a_new_link_and_none_restated(
        driver, world_id, link_id, venue, cards, restating, restated
    )
    assert ended.change_set_id != restated.change_set_id


def test_the_end_prior_read_under_the_link_lock_alone_leaves_an_end_that_committed_since(
    driver, seeded, chat, monkeypatch
):
    no_world_lock(monkeypatch)
    no_item_locks(monkeypatch)
    world_id = seeded["world_id"]
    venue, rowan, prior = reassignment_cast(driver, world_id, chat)
    ran = attempts(monkeypatch)
    gate = Gate("reassigner")
    monkeypatch.setattr(apply_module, "_slot_priors", gate.after(apply_module._slot_priors))

    reassigner = Run("reassigner", lambda: reassign(driver, world_id, chat, venue, rowan))
    # It read the prior as the slot's holder with no lock, and stops before it writes.
    assert gate.reached.wait(60)
    assert [row["id"] for row in gate.seen] == [prior]
    ended = end(driver, world_id, chat, prior)
    gate.opened.set()
    reassigned = reassigner.outcome()

    assert isinstance(reassigned, Receipt)
    assert ran["reassigner"] == 1, "the batch ran again: its read under the lock went untested"
    assert_the_end_stands(driver, world_id, prior, rowan, ended, reassigned)


# A stress loop: the watch for link_not_found -----------------------------------------------------

THREADS = 4
ROUNDS = 4
# A link a receipt returned may be ended by another thread before this one ends it: a later
# assignment of the same task supersedes it.
ALLOWED_RULES = frozenset({"already_ended"})
_READ_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l.id AS id"


def _link_of(receipt: Receipt) -> str:
    """The link the batch made or restated, never a prior link it ended."""
    return next(
        line.entity_id
        for line in receipt.lines
        if line.entity_type == "link" and line.symbol in (">", "=")
    )


@pytest.mark.parametrize("world_lock", ["on", "off"])
def test_a_stress_loop_of_link_writes_never_loses_a_link_a_receipt_returned(
    driver, seeded, chat, monkeypatch, world_lock
):
    # writer-a1 saw a link_not_found once under parallel load, for a link id a receipt had just
    # returned. Threads create, restate and end links, read every link a receipt
    # names back at once, and end the link a receipt had just named.
    if world_lock == "off":
        no_world_lock(monkeypatch)
    world_id = seeded["world_id"]
    tasks = made(
        driver,
        world_id,
        chat,
        ("task", "Book the venue for the Okonkwo offsite"),
        ("task", "Print the name cards for Rowan and Ilse"),
        ("task", "Draft the seating plan for Tamsin"),
    )
    people = made(driver, world_id, chat, ("person", "Okonkwo"), ("person", "Ilse"))
    outcomes: list[tuple[str, str, Any]] = []
    returned: set[str] = set()
    unread: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    def step(name: str, what: str, op: dict[str, Any], ev: str, line: int) -> Receipt | None:
        key = f"mk-stress-{name}-{what}-{line}"
        sent = batch([op], [observation(chat, ev, 1 + line % 40, message_key=key)])
        try:
            receipt = apply_batch(driver, world_id, PERSON, sent)
        except (BatchDeclined, WriteFailed) as error:
            with lock:
                outcomes.append((name, what, error))
            return None
        links = [line.entity_id for line in receipt.lines if line.entity_type == "link"]
        missing = [link for link in links if not read(driver, world_id, _READ_LINK, id=link)]
        with lock:
            outcomes.append((name, what, receipt))
            returned.update(links)
            unread.extend((name, what, link) for link in missing)
        return receipt

    def worker(n: int) -> None:
        name = f"stress-{n}"
        for r in range(ROUNDS):
            line = n * ROUNDS * 4 + r * 4
            work, person = tasks[(n + r) % 3], people[(n + r) % 2]
            other = tasks[(n + r + 1) % 3]
            op = create_link(work, "assigned to", person, "s", recheck_by=RECHECK)
            assigned = step(name, "assign", op, "s", line)
            op = create_link(work, "assigned to", person, "s", recheck_by=RECHECK)
            step(name, "restate", op, "s", line + 1)
            step(name, "part", create_link(work, "part of", other, "s"), "s", line + 2)
            if assigned is not None:
                step(name, "end", end_link(_link_of(assigned), "s", 30), "s", line + 3)

    threads = [
        threading.Thread(target=worker, args=(n,), name=f"stress-{n}") for n in range(THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300)

    assert not any(thread.is_alive() for thread in threads)
    refusals = [
        (name, what, refusal.op, refusal.code, refusal.rule)
        for name, what, outcome in outcomes
        if isinstance(outcome, BatchDeclined)
        for refusal in outcome.refusals
    ]
    lost = [entry for entry in refusals if entry[4] == "link_not_found"]
    assert not lost, f"link_not_found under parallel load: {lost}"
    assert unread == [], "a link a receipt named did not read back at once"
    # The store's message names the constraint a failed commit broke.
    failed = [
        (name, what, getattr(outcome.__cause__, "message", repr(outcome.__cause__)))
        for name, what, outcome in outcomes
        if isinstance(outcome, WriteFailed)
    ]
    assert failed == []
    assert [entry for entry in refusals if entry[4] not in ALLOWED_RULES] == []
    # Every step ran to an outcome: an assignment is never refused, so every round ends a link.
    assert len(outcomes) == THREADS * ROUNDS * 4

    # Every link a receipt returned is in the store, and each slot has one holder at most.
    stored = {
        row["id"]
        for row in read(driver, world_id, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l.id AS id")
    }
    assert returned <= stored
    holders = read(
        driver,
        world_id,
        "MATCH (t:Item)-[l:LINK]->(:Item) MATCH (m:Meaning {id: l.meaning}) "
        "WHERE m.name = 'assigned to' AND l.active_key IS NOT NULL "
        "RETURN t.id AS task, count(l) AS n",
    )
    assert all(row["n"] == 1 for row in holders), holders
