"""Harnesses for D2's durability and concurrency tests: a pass killed as a real process, and a
store-side wait for a transaction that is blocked on a lock.

The kill worker runs in a child process. The parent test makes the world and the source; the
child opens its own driver, runs one pass under a TestClock set to the parent's time, and sends
itself SIGKILL from inside a seam. No Python cleanup runs: no rollback is sent and no connection
is closed, so the store must see the socket die and roll back the open transaction on its own.

    python -m tests.writer.harness <seam> <mode> <world_id> <source_id> <request_id> <at>

`mode` is `worker` (the host enqueues, a queue worker claims and applies) or `direct` (the person
calls apply_batch with their own request id and no queue).
"""

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from grove import clock
from grove.clock import TestClock
from grove.store.connection import DATABASE, open_driver
from grove.store.query import WorldQuery
from grove.writer import (
    Receipt,
    RequestState,
    apply_batch,
    apply_request,
    claim_next_request,
    enqueue_request,
)
from grove.writer import api as api_module
from grove.writer import apply as apply_module
from tests.writer.helpers import HOST, LEARNER, PERSON, observation, supports

SEAMS = ("after_enqueue", "before_commit", "after_commit")
MODES = ("worker", "direct")
RECHECK = "2031-06-01T00:00:00+00:00"
SERVER = Path(__file__).resolve().parents[2]

# The words of one remember request: three items, then two links between them.
SOURCE_LINES = [
    "task: Book the venue for the Okonkwo offsite",
    "person: Tamsin",
    "task: Print the name cards for Rowan and Ilse",
    "link: 1 assigned to 2",
    "link: 3 part of 1",
]
ONE_EFFECT = {"items": 3, "links": 2, "evidence": 5}

# A child that returns from its pass never reached its seam.
_EXIT_NO_SEAM = 3
_open: dict[str, Any] = {}


# The pass ---------------------------------------------------------------------------------------


def extract(driver, world_id: str, source_id: str) -> dict[str, Any]:
    """The pass's extraction: a pure function of the stored source and its episode. Its op ids
    come from the line numbers, so every pass over one request proposes the same batch, with the
    same fingerprint."""
    query = WorldQuery(driver, world_id)
    [source] = query.read("MATCH (s:Source {id: $id}) RETURN s.content AS content", id=source_id)
    [episode] = query.read(
        "MATCH (e:Episode) WHERE $id IN e.source_ids RETURN e.id AS id", id=source_id
    )
    cited = {"episode_id": episode["id"], "source_id": source_id}
    evidence: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    start = 0
    for number, line in enumerate(source["content"].split("\n"), start=1):
        ev_id = f"e{number}"
        # A source of the AI host has no lines: each line of the words is a char span.
        span = f"char:{start}-{start + len(line)}"
        evidence.append(observation(cited, ev_id, number, locator=span))
        start += len(line) + 1
        head, _, rest = line.partition(": ")
        common = {
            "op_id": f"x{number}",
            "became_true": "unknown",
            "evidence_refs": supports(ev_id),
            "reason": "the request states it",
        }
        if head == "link":
            first, *words, second = rest.split(" ")
            ops.append(
                {
                    "op": "create_link",
                    "from": f"$l{first}",
                    "meaning": " ".join(words),
                    "to": f"$l{second}",
                    "recheck_by": RECHECK,
                    **common,
                }
            )
            continue
        op = {"op": "create_item", "temp_id": f"$l{number}", "kind": head, "title": rest, **common}
        if head == "task":
            op |= {"status": "open", "recheck_by": RECHECK}
        ops.append(op)
    return {"evidence": evidence, "ops": ops}


def work_one(driver, world_id: str, *, worker: str) -> RequestState | None:
    """One queue worker pass: claim the oldest due request, extract, apply."""
    claim = claim_next_request(driver, world_id, LEARNER, worker=worker)
    if claim is None:
        return None
    sent = extract(driver, world_id, claim.source_id)
    return apply_request(driver, world_id, LEARNER, claim.id, sent)


def direct_apply(driver, world_id: str, source_id: str, request_id: str) -> Receipt:
    """The person's own call from the app, with their idempotency key and no queue."""
    return apply_batch(
        driver, world_id, PERSON, extract(driver, world_id, source_id), request_id=request_id
    )


# The child ------------------------------------------------------------------------------------


def _die(seam: str, seen: dict[str, Any] | None = None) -> None:
    print(json.dumps({"seam": seam, **(seen or {})}, sort_keys=True), flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def _inside_the_open_transaction() -> dict[str, Any]:
    """What the batch wrote before the kill, read in its own transaction: the proof that the
    kill came mid-apply, with the batch and its `saved` written and not committed."""
    tx = _open["tx"]
    items = tx.run("MATCH (i:Item) RETURN count(i) AS n")[0]["n"]
    links = tx.run("MATCH (:Item)-[l:LINK]->(:Item) RETURN count(l) AS n")[0]["n"]
    status = tx.run("MATCH (r:Request {id: $id}) RETURN r.status AS s", id=_open["request_id"])
    return {"items": items, "links": links, "status": status[0]["s"]}


def _arm(seam: str, request_id: str) -> None:
    if seam == "before_commit":
        apply_ops = apply_module.apply_ops

        def keep_the_transaction(tx, **parts):
            _open.update(tx=tx, request_id=request_id)
            return apply_ops(tx, **parts)

        apply_module.apply_ops = keep_the_transaction
        apply_module._before_commit = lambda: _die(seam, _inside_the_open_transaction())
    elif seam == "after_commit":
        api_module._after_commit = lambda: _die(seam)


def child(seam: str, mode: str, world_id: str, source_id: str, request_id: str, at: str) -> int:
    test_clock = TestClock()
    test_clock.set(datetime.fromisoformat(at))
    clock.install(test_clock)
    _arm(seam, request_id)
    with open_driver() as driver:
        if mode == "direct":
            direct_apply(driver, world_id, source_id, request_id)
            return _EXIT_NO_SEAM
        enqueue_request(driver, world_id, HOST, request_id, source_id=source_id)
        if seam == "after_enqueue":
            _die(seam)
        work_one(driver, world_id, worker="killed-worker")
    return _EXIT_NO_SEAM


# The parent -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Killed:
    returncode: int
    reports: list[dict[str, Any]]
    stderr: str


def run_killed(
    seam: str, mode: str, world_id: str, source_id: str, request_id: str, at: datetime
) -> Killed:
    """Runs the child and waits for it. The child inherits the parent's environment, so it
    reaches the same store with the same credentials."""
    if seam not in SEAMS or mode not in MODES:
        raise ValueError(f"no such seam or mode: {seam} {mode}")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.writer.harness",
            seam,
            mode,
            world_id,
            source_id,
            request_id,
            at.isoformat(),
        ],
        cwd=SERVER,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    reports = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    return Killed(done.returncode, reports, done.stderr)


# A transaction blocked on a lock, as the store reports it ---------------------------------------

_SHOW = (
    "SHOW TRANSACTIONS YIELD transactionId, status, currentQuery, parameters "
    "WHERE parameters.world_id = $w AND status STARTS WITH 'Blocked' "
    "RETURN transactionId AS id, currentQuery AS query"
)
# 20 seconds at most: a failed wait must end before the 30 second timeout of the transaction a
# gate holds open.
_POLL_SECONDS = 0.05
_POLLS = 400


def blocked(driver, world_id: str, fragment: str) -> list[str]:
    """The transactions of this world that wait for a lock in a statement holding `fragment`.
    The store names every waiter, so a test asserts a wait from the store, not from a timer."""
    with driver.session(database=DATABASE) as session:
        rows = session.run(_SHOW, w=world_id).data()
    return [row["id"] for row in rows if fragment in row["query"]]


def wait_until_blocked(
    driver, world_id: str, fragment: str, count: int = 1, gave_up: threading.Event | None = None
) -> bool:
    """True once `count` transactions wait in a statement holding `fragment`. False when
    `gave_up` is set first (a thread that should wait finished instead), or after the polls."""
    pause = threading.Event()
    for _ in range(_POLLS):
        if len(blocked(driver, world_id, fragment)) >= count:
            return True
        if gave_up is not None and gave_up.is_set():
            return False
        pause.wait(_POLL_SECONDS)
    return False


if __name__ == "__main__":
    sys.exit(child(*sys.argv[1:]))
