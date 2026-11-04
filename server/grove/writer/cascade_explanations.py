"""D6 cache and request writes. Provider calls happen before these transactions."""

from datetime import timedelta

from neo4j import Driver

from grove import clock
from grove.cascade_jobs import JOB, load_explanation, packet_fingerprint
from grove.config import ConfigError
from grove.ids import new_id
from grove.models.explain_break import Explanation, _one_sentence
from grove.screening import Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer.history import Stamp, add_entry, add_text
from grove.writer.runs import (
    JOB_CLAIMED,
    JOB_FAILED,
    JOB_MAX_ATTEMPTS,
    _put_back,
    _set_job,
    claim_job,
    enqueue_job,
)
from grove.writer.tx import WorldTx, WorldWriter

EXPLANATION_LEASE_SECONDS = 1800


def claim_explanation_job(driver: Driver, world_id: str, *, worker: str) -> dict | None:
    """Recover only expired explanation claims, then take a claim with a unique holder token.

    The lease is renewed between model calls. A late holder cannot end a newer claim, even
    when both dispatcher processes use the same worker name. Cache writes remain idempotent
    if an individual model request exceeds the lease and both holders finish it.
    """

    def recover(tx: WorldTx) -> None:
        tx.lock_world()
        now = clock.now(world_id)
        expired = tx.run(
            "MATCH (j:Job {agent_or_job_type: $agent, state: $claimed}) "
            "WHERE j.claimed_at IS NULL OR j.claimed_at <= $before "
            "RETURN j.id AS id, j.attempts AS attempts",
            agent=JOB,
            claimed=JOB_CLAIMED,
            before=now - timedelta(seconds=EXPLANATION_LEASE_SECONDS),
        )
        for job in expired:
            attempts = job["attempts"] or 0
            if attempts >= JOB_MAX_ATTEMPTS:
                _set_job(tx, job["id"], state=JOB_FAILED, now=now)
            else:
                _put_back(tx, job["id"], JOB, attempts=attempts, run_after=now, now=now)

    WorldWriter(driver, world_id).transaction(recover)
    return claim_job(driver, world_id, agent=JOB, worker=f"{worker}:{new_id(world_id)}")


def renew_explanation_job(driver: Driver, world_id: str, job_id: str, *, worker: str) -> bool:
    """Renew this holder only. False means a replacement holder owns any remaining work."""

    def work(tx: WorldTx) -> bool:
        tx.lock_world()
        rows = tx.run(
            "MATCH (j:Job {id: $id, agent_or_job_type: $agent, state: $claimed, "
            "claimed_by: $worker}) SET j.claimed_at = $now, "
            "j.version = j.version + 1, j.touched_at = $now RETURN j.id AS id",
            id=job_id,
            agent=JOB,
            claimed=JOB_CLAIMED,
            worker=worker,
            now=clock.now(world_id),
        )
        return bool(rows)

    return WorldWriter(driver, world_id).transaction(work)


def request_explanation(driver: Driver, world_id: str, mark_id: str) -> str | None:
    """Explicit background enqueue, separate from read_explanation and the read response."""
    rows = WorldQuery(driver, world_id).read(
        "MATCH (m:Impact {link_id: $id}) RETURN m.explanation AS explanation",
        id=mark_id,
    )
    if not rows or rows[0]["explanation"] is not None:
        return None
    return enqueue_job(
        driver,
        world_id,
        agent=JOB,
        trigger="explanation_requested",
        input_references=[mark_id],
        priority=0,
    )


def cache_explanation(
    driver: Driver,
    world_id: str,
    mark_id: str,
    result: Explanation,
    *,
    expected_version: int,
    expected_packet: str,
    screener: Screener | None = None,
) -> str:
    """Return saved, cached, stale, missing, or refused. A model race writes at most once."""
    if not isinstance(result, Explanation) or result.by != "spark" or not result.model_call_id:
        return "refused"
    if not _one_sentence(result.text):
        return "refused"
    try:
        checked = (screener or Screener(ScreeningConfig.from_environment())).screen_text(
            result.text
        )
    except ConfigError:
        return "refused"
    if (
        checked.counts
        or checked.dropped_whole
        or len(checked.messages) != 1
        or checked.messages[0].text != result.text
    ):
        return "refused"

    def work(tx: WorldTx) -> str:
        tx.lock_world()
        loaded = load_explanation(tx, mark_id)
        if loaded is None:
            return "missing"
        if loaded.mark.get("explanation") is not None:
            return "cached"
        if (
            loaded.mark.get("version", 0) != expected_version
            or loaded.packet is None
            or packet_fingerprint(loaded.packet) != expected_packet
        ):
            return "stale"
        now = clock.now(world_id)
        stamp = Stamp(
            world_id,
            now,
            "cascade-explanation",
            new_id(world_id),
            new_id(world_id),
            caller_kind="writer",
        )
        value_id = add_text(tx, stamp, result.text)
        tx.run(
            "MATCH (m:Impact {link_id: $id}) SET m.explanation = $value, "
            "m.explained_by = $by, m.version = coalesce(m.version, 0) + 1, "
            "m.touched_at = $now",
            id=mark_id,
            value=value_id,
            by=result.by,
            now=now,
        )
        add_entry(
            tx,
            stamp,
            entity_type="link",
            entity_id=mark_id,
            field="cascade_explanation",
            new={
                "value_id": value_id,
                "model_call_id": result.model_call_id,
                "explained_by": result.by,
            },
            world_time=None,
        )
        return "saved"

    return WorldWriter(driver, world_id).transaction(work)
