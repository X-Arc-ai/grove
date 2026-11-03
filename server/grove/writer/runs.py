"""The job runner's bookkeeping: run records and jobs (spec line 754).

These are not B.2 operations. The runner's API side calls them; no agent reaches them. Each row
carries the S12 pair (`version`, `touched_at`), because each changes after it is written.

One pass at a time per world: `start_run` takes the world lock, and refuses while another run of
the world is `running` and inside its time cap plus the stale margin. A run past that is marked
`abandoned` first: its process died with no end call (a kill, a stopped service), and its
requests come back through their leases.

A job is a pass that code scheduled with no request behind it: a Curator pass after a Learner pass
that changed items, a Connector pass on Appendix C's trigger. A queued job of one agent takes the
new inputs of a second trigger, so a world never holds two queued jobs of one agent. A claim
happens under the world lock, which is how two runners cannot claim one job (no SKIP LOCKED).
"""

import json
from datetime import datetime, timedelta
from typing import Any

from grove import clock
from grove.ids import new_id
from grove.store.connection import Driver
from grove.writer.history import compact
from grove.writer.tx import WorldTx, WorldWriter

RUNNING = "running"
ABANDONED = "abandoned"
JOB_QUEUED, JOB_CLAIMED, JOB_DONE, JOB_FAILED = "queued", "claimed", "done", "failed"
JOB_MAX_ATTEMPTS = 3
JOB_BACKOFF_SECONDS = (60, 300, 900)
# A proposed operations text is a text home (S16): kept 30 days. A run keeps at most this much.
MAX_PROPOSED_CHARS = 200_000
MAX_SUMMARY_CHARS = 2_000


def _now(world_id: str) -> datetime:
    return clock.now(world_id)


def _one(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None


def start_run(
    driver: Driver,
    world_id: str,
    *,
    run_id: str,
    agent: str,
    trigger: str,
    model: str,
    effort: str,
    token_cap: int,
    time_cap: int,
    stale_margin_seconds: int,
) -> dict | None:
    """The new `running` record, or None while another run of the world is live."""

    def work(tx: WorldTx) -> dict | None:
        tx.lock_world()
        now = _now(world_id)
        live = tx.run(
            "MATCH (r:RunRecord {outcome: $running}) "
            "RETURN r.id AS id, r.started AS started, r.time_cap AS time_cap",
            running=RUNNING,
        )
        for row in live:
            deadline = row["started"] + timedelta(
                seconds=(row["time_cap"] or 0) + stale_margin_seconds
            )
            if deadline > now:
                return None
            tx.run(
                "MATCH (r:RunRecord {id: $id}) WHERE r.outcome = $running "
                "SET r.outcome = $abandoned, r.finished = $now, r.version = r.version + 1, "
                "r.touched_at = $now",
                id=row["id"],
                running=RUNNING,
                abandoned=ABANDONED,
                now=now,
            )
        row = compact(
            {
                "id": run_id,
                "agent": agent,
                "trigger": trigger,
                "input_ids": [],
                "model": model,
                "effort": effort,
                "token_cap": token_cap,
                "time_cap": time_cap,
                "outcome": RUNNING,
                "started": now,
                "version": 1,
                "touched_at": now,
            }
        )
        tx.run("CREATE (r:RunRecord) SET r += $row", row=row)
        return row

    return WorldWriter(driver, world_id).transaction(work)


def set_run_inputs(driver: Driver, world_id: str, run_id: str, input_ids: list[str]) -> None:
    def work(tx: WorldTx) -> None:
        now = _now(world_id)
        tx.run(
            "MATCH (r:RunRecord {id: $id}) SET r.input_ids = $ids, r.version = r.version + 1, "
            "r.touched_at = $now",
            id=run_id,
            ids=list(input_ids),
            now=now,
        )

    WorldWriter(driver, world_id).transaction(work)


def note_proposal(
    driver: Driver,
    world_id: str,
    run_id: str,
    *,
    batch: Any,
    request_id: str | None,
    state: str,
    decline_op_ids: list[str],
) -> None:
    """One `brain propose` of a running pass: its batch (a text home, 30 days), the request it
    saved under, and the op ids the writer declined. Best effort for the caller: the writer's
    own rows are the truth, this is the run's account of them."""

    def work(tx: WorldTx) -> None:
        now = _now(world_id)
        # The SET takes the record's write lock before the read below (S1).
        found = tx.run(
            "MATCH (r:RunRecord {id: $id}) SET r.touched_at = $now "
            "RETURN r.proposed_operations AS proposed, r.receipts AS receipts, "
            "r.decline_ids AS declines",
            id=run_id,
            now=now,
        )
        if not found:
            return
        row = found[0]
        proposed = json.loads(row["proposed"]) if row["proposed"] else {"batches": []}
        entry = {"request_id": request_id, "state": state, "batch": batch}
        proposed["batches"].append(entry)
        text = json.dumps(proposed, sort_keys=True)
        if len(text) > MAX_PROPOSED_CHARS:
            proposed["batches"][-1] = {"request_id": request_id, "state": state, "batch": None}
            proposed["truncated"] = True
            text = json.dumps(proposed, sort_keys=True)
        receipts = json.loads(row["receipts"]) if row["receipts"] else []
        if state == "saved" and request_id and request_id not in receipts:
            receipts.append(request_id)
        declines = list(row["declines"] or [])
        declines.extend(op for op in decline_op_ids if op not in declines)
        tx.run(
            "MATCH (r:RunRecord {id: $id}) SET r.proposed_operations = $proposed, "
            "r.receipts = $receipts, r.decline_ids = $declines, r.version = r.version + 1",
            id=run_id,
            proposed=text,
            receipts=json.dumps(receipts),
            declines=declines,
        )

    WorldWriter(driver, world_id).transaction(work)


def finish_run(
    driver: Driver,
    world_id: str,
    run_id: str,
    *,
    outcome: str,
    tokens_in: int,
    tokens_cached: int,
    tokens_out: int,
    summary: str | None = None,
) -> dict | None:
    """Ends a running record once. A second end, or an end of an abandoned run, changes
    nothing and returns the row as it stands."""

    def work(tx: WorldTx) -> dict | None:
        now = _now(world_id)
        found = tx.run(
            "MATCH (r:RunRecord {id: $id}) SET r.touched_at = r.touched_at "
            "RETURN r.outcome AS outcome, r.proposed_operations AS proposed",
            id=run_id,
        )
        if not found:
            return None
        if found[0]["outcome"] == RUNNING:
            proposed = json.loads(found[0]["proposed"]) if found[0]["proposed"] else {"batches": []}
            if summary:
                proposed["summary"] = summary[:MAX_SUMMARY_CHARS]
            tx.run(
                "MATCH (r:RunRecord {id: $id}) SET r.outcome = $outcome, r.finished = $now, "
                "r.tokens_in = $tokens_in, r.tokens_cached = $tokens_cached, "
                "r.tokens_out = $tokens_out, r.proposed_operations = $proposed, "
                "r.version = r.version + 1, r.touched_at = $now",
                id=run_id,
                outcome=outcome,
                now=now,
                tokens_in=tokens_in,
                tokens_cached=tokens_cached,
                tokens_out=tokens_out,
                proposed=json.dumps(proposed, sort_keys=True),
            )
        return _one(tx.run("MATCH (r:RunRecord {id: $id}) RETURN r", id=run_id))

    row = WorldWriter(driver, world_id).transaction(work)
    return None if row is None else row.get("r", row)


def enqueue_job(
    driver: Driver,
    world_id: str,
    *,
    agent: str,
    trigger: str,
    input_references: list[str],
    priority: int,
    lane: str = "maintenance",
) -> str:
    """A queued job for `agent`, or the queued one it already has with these inputs added."""

    def work(tx: WorldTx) -> str:
        tx.lock_world()
        now = _now(world_id)
        queued = tx.run(
            "MATCH (j:Job {agent_or_job_type: $agent, state: $queued}) "
            "RETURN j.id AS id, j.input_references AS refs ORDER BY j.run_after, j.id LIMIT 1",
            agent=agent,
            queued=JOB_QUEUED,
        )
        if queued:
            refs = json.loads(queued[0]["refs"]) if queued[0]["refs"] else []
            refs.extend(ref for ref in input_references if ref not in refs)
            tx.run(
                "MATCH (j:Job {id: $id}) SET j.input_references = $refs, "
                "j.version = j.version + 1, j.touched_at = $now",
                id=queued[0]["id"],
                refs=json.dumps(refs),
                now=now,
            )
            return queued[0]["id"]
        job_id = new_id(world_id)
        row = {
            "id": job_id,
            "lane": lane,
            "agent_or_job_type": agent,
            "trigger": trigger,
            "input_references": json.dumps(list(dict.fromkeys(input_references))),
            "priority": priority,
            "state": JOB_QUEUED,
            "attempts": 0,
            "run_after": now,
            "version": 1,
            "touched_at": now,
        }
        tx.run("CREATE (j:Job) SET j += $row", row=row)
        return job_id

    return WorldWriter(driver, world_id).transaction(work)


def claim_job(driver: Driver, world_id: str, *, agent: str, worker: str) -> dict | None:
    """The oldest due queued job of `agent`, now claimed by `worker`; None when none is due."""

    def work(tx: WorldTx) -> dict | None:
        tx.lock_world()
        now = _now(world_id)
        due = tx.run(
            "MATCH (j:Job {agent_or_job_type: $agent, state: $queued}) WHERE j.run_after <= $now "
            "RETURN j.id AS id ORDER BY j.priority DESC, j.run_after, j.id LIMIT 1",
            agent=agent,
            queued=JOB_QUEUED,
            now=now,
        )
        if not due:
            return None
        claimed = tx.run(
            "MATCH (j:Job {id: $id}) WHERE j.state = $queued "
            "SET j.state = $claimed, j.claimed_by = $worker, j.claimed_at = $now, "
            "j.attempts = j.attempts + 1, j.version = j.version + 1, j.touched_at = $now "
            "RETURN j",
            id=due[0]["id"],
            queued=JOB_QUEUED,
            claimed=JOB_CLAIMED,
            worker=worker,
            now=now,
        )
        return claimed[0]["j"] if claimed else None

    return WorldWriter(driver, world_id).transaction(work)


def end_job(
    driver: Driver, world_id: str, job_id: str, *, worker: str, done: bool, requeue: bool = False
) -> str | None:
    """The job's state after its pass: done; or back to queued with a back-off; or failed once
    its attempts are spent. `requeue` (a pass stopped for the interactive lane) puts it back at
    once and gives back its attempt. Only the holder ends it; anything else changes nothing."""

    def work(tx: WorldTx) -> str | None:
        now = _now(world_id)
        found = tx.run(
            "MATCH (j:Job {id: $id}) SET j.touched_at = j.touched_at "
            "RETURN j.state AS state, j.claimed_by AS worker, j.attempts AS attempts",
            id=job_id,
        )
        if not found or found[0]["state"] != JOB_CLAIMED or found[0]["worker"] != worker:
            return found[0]["state"] if found else None
        attempts = found[0]["attempts"] or 0
        if requeue and not done:
            tx.run(
                "MATCH (j:Job {id: $id}) SET j.state = $state, j.claimed_by = null, "
                "j.run_after = $now, j.attempts = $attempts, j.version = j.version + 1, "
                "j.touched_at = $now",
                id=job_id,
                state=JOB_QUEUED,
                attempts=max(attempts - 1, 0),
                now=now,
            )
            return JOB_QUEUED
        if done:
            state, run_after = JOB_DONE, None
        elif attempts >= JOB_MAX_ATTEMPTS:
            state, run_after = JOB_FAILED, None
        else:
            wait = JOB_BACKOFF_SECONDS[min(attempts, len(JOB_BACKOFF_SECONDS)) - 1]
            state, run_after = JOB_QUEUED, now + timedelta(seconds=wait)
        tx.run(
            "MATCH (j:Job {id: $id}) SET j.state = $state, j.claimed_by = null, "
            "j.run_after = coalesce($run_after, j.run_after), j.version = j.version + 1, "
            "j.touched_at = $now",
            id=job_id,
            state=state,
            run_after=run_after,
            now=now,
        )
        return state

    return WorldWriter(driver, world_id).transaction(work)
