"""The API side of a pass: what is due, a pass's grant, its input packet, and its end.

The runner service holds no store credential, so the API does each step for it:

1. `due_work` lists the worlds with work: queued requests for the Learner first (the interactive
   lane), then due jobs for the Curator and the Connector.
2. `start_pass` opens a run record under the world lock (one pass at a time per world), claims
   the Learner's requests with the run id as the worker name and a lease of the time cap plus a
   margin, writes an episode for each claimed source that has none (D10: before any model call),
   and mints the pass's two tokens.
3. `end_pass` hands back each claimed request that the pass did not settle: `fail_request` with
   a retry, so the next pass applies it once (the writer's idempotency keeps it to one effect).
   It adds the run's tokens from `model_calls`, ends the run record, and schedules the Curator
   after a Learner pass that changed items, and the Connector on Appendix C's trigger.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from grove import clock
from grove.demo_policy import is_demo_world
from grove.ids import new_id
from grove.models.config import load_catalog
from grove.models.gateway_auth import mint as mint_gateway_token
from grove.runner.tokens import AGENTS, check_runner_token_hash, mint_pass_token
from grove.store.connection import Driver
from grove.store.errors import ScopeError
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer import Caller, claim_next_request, fail_request, record_episode
from grove.writer import runs as run_rows
from grove.writer.clocks import parse_instant
from grove.writer.requests import LEASE_SECONDS

# Appendix C line 3269: ten changed items since the Connector's last pass, or a new contradiction.
CONNECTOR_CHANGED_ITEMS = 10
NOT_PROPOSED = "not_proposed"
EPISODE_CUT_RULE = "one_request"
_PRIORITY = {"curator": 20, "connector": 10}


@dataclass(frozen=True)
class PassSettings:
    """The caps of one pass (D15, 3.5 rule 8). The time cap is the watchdog's limit."""

    time_cap_seconds: int = 600
    lease_margin_seconds: int = 120
    token_cap: int = 300_000
    learner_requests: int = 5
    # A test on the shared dev store lists only its own worlds. Production leaves it unset.
    only_worlds: frozenset[str] | None = None

    @property
    def lease_seconds(self) -> int:
        return self.time_cap_seconds + self.lease_margin_seconds

    @property
    def token_ttl_seconds(self) -> int:
        return self.lease_seconds


@dataclass(frozen=True)
class RunnerAuth:
    """The API's half of the runner's credentials. Either unset turns the runner routes off."""

    gateway_secret: bytes | None = field(default=None, repr=False)
    runner_token_sha256: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "RunnerAuth":
        env = os.environ if env is None else env
        secret = env.get("GROVE_GATEWAY_SECRET") or None
        if secret is not None and len(secret.encode("utf-8")) < 32:
            secret = None
        return cls(
            gateway_secret=secret.encode("utf-8") if secret else None,
            runner_token_sha256=check_runner_token_hash(env.get("GROVE_RUNNER_TOKEN_SHA256")),
        )


def caller_for(agent: str) -> Caller:
    kind, _ = AGENTS[agent]
    return Caller(
        kind=kind, scopes=frozenset({"grove.read", "grove.propose"}), actor=f"agent:{agent}"
    )


def _worlds(driver: Driver) -> list[str]:
    rows = PlatformQuery(driver).read("MATCH (w:World) RETURN w.id AS id ORDER BY w.id")
    return [row["id"] for row in rows]


_DUE_REQUEST = (
    "MATCH (r:Request) WHERE r.source_id IS NOT NULL AND ("
    "(r.status = 'queued' AND (r.retry_at IS NULL OR r.retry_at <= $now)) OR "
    "(r.status = 'failed' AND r.retry_at IS NOT NULL AND r.retry_at <= $now) OR "
    "(r.status = 'applying' AND (r.lease_until IS NULL OR r.lease_until <= $now))) "
    "RETURN r.id AS id LIMIT 1"
)
_DUE_JOBS = (
    "MATCH (j:Job {state: 'queued'}) WHERE j.run_after <= $now "
    "RETURN DISTINCT j.agent_or_job_type AS agent"
)
_LIVE_RUNS = (
    "MATCH (r:RunRecord {outcome: 'running'}) "
    "RETURN r.id AS id, r.agent AS agent, r.started AS started, r.time_cap AS time_cap"
)


def live_run(query: WorldQuery, now: datetime, settings: "PassSettings") -> dict | None:
    """The run that reads `running` inside its time cap plus the margin. One past that died with
    no end, and `start_run` marks it abandoned when the next pass starts."""
    for row in query.read(_LIVE_RUNS):
        if stale_cutoff(row, settings) > now:
            return row
    return None


def has_live_run(query: WorldQuery, now: datetime, settings: "PassSettings") -> bool:
    return live_run(query, now, settings) is not None


def budget_reached(query: WorldQuery, now: datetime) -> bool:
    """Every pass job waits at the ceiling (jobs.json), so a world whose budget for today is
    reached starts no pass: its requests stay queued and spend no attempt until the reset."""
    rows = query.read(
        "MATCH (b:Budget {day: $day}) RETURN b.state AS state", day=now.astimezone(UTC).date()
    )
    return bool(rows) and rows[0]["state"] == "reached"


def due_work(
    driver: Driver, *, limit: int = 100, settings: "PassSettings | None" = None
) -> list[dict[str, str]]:
    """One row per world with work and no live run: `{world_id, agent}`, the Learner first.

    The interactive lane comes first (spec 2327, 3008) while one pass runs per world: a world
    with a due request and a live Curator or Connector pass gets `{world_id, agent: learner,
    preempt: <run id>}`. The runner stops that pass (outcome `preempted`, its job back in the
    queue with no attempt spent), and the Learner starts on a later poll, inside the 60 s."""
    settings = settings or PassSettings()
    due: list[dict[str, str]] = []
    for world_id in _worlds(driver):
        if settings.only_worlds is not None and world_id not in settings.only_worlds:
            continue
        try:
            query = WorldQuery(driver, world_id)
        except ScopeError:
            continue
        now = clock.now(world_id)
        if budget_reached(query, now):
            continue
        demo_world = is_demo_world(driver, world_id)
        if demo_world and not query.read(_DUE_REQUEST, now=now):
            # Leave autonomous jobs and stale runs intact for an explicit continuation.
            continue
        rows = query.read(_LIVE_RUNS)
        live = next((row for row in rows if stale_cutoff(row, settings) > now), None)
        if live is None and rows:
            # A run that died with no end: free its job now, not only when a pass starts.
            run_rows.abandon_stale_runs(
                driver, world_id, stale_margin_seconds=settings.lease_margin_seconds
            )
        if live is not None:
            if live["agent"] != "learner" and query.read(_DUE_REQUEST, now=now):
                due.append({"world_id": world_id, "agent": "learner", "preempt": live["id"]})
            continue
        if query.read(_DUE_REQUEST, now=now):
            due.append({"world_id": world_id, "agent": "learner"})
        elif not demo_world:
            _ensure_connector_job(driver, world_id)
            agents = {row["agent"] for row in query.read(_DUE_JOBS, now=now)}
            for agent in ("curator", "connector"):
                if agent in agents:
                    due.append({"world_id": world_id, "agent": agent})
                    break
        if len(due) >= limit:
            break
    return due


@dataclass(frozen=True)
class PassGrant:
    world_id: str
    run_id: str
    agent: str
    input_ids: list[str]
    job_id: str | None
    pass_token: str = field(repr=False)
    gateway_token: str = field(repr=False)
    time_cap_seconds: int

    def to_api(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "run_id": self.run_id,
            "agent": self.agent,
            "input_ids": list(self.input_ids),
            "job_id": self.job_id,
            "pass_token": self.pass_token,
            "gateway_token": self.gateway_token,
            "time_cap_seconds": self.time_cap_seconds,
        }


class PassBusy(Exception):
    """Another pass of the world is live."""


def _ensure_episode(
    driver: Driver, world_id: str, source_id: str, run_id: str, at: datetime
) -> str:
    """The episode that holds this source, written before any model call (D10, line 1989)."""
    query = WorldQuery(driver, world_id)
    found = query.read(
        "MATCH (e:Episode) WHERE $source IN e.source_ids RETURN e.id AS id ORDER BY e.id LIMIT 1",
        source=source_id,
    )
    if found:
        return found[0]["id"]
    rows = query.read(
        "MATCH (s:Source {id: $id}) RETURN s.connector_id AS connector, s.content AS content, "
        "s.received_at AS received",
        id=source_id,
    )
    if not rows:
        raise LookupError("the request's source is not in this world")
    messages = _messages(rows[0]["content"])
    times = [m["at"] for m in messages if m.get("at")]
    if not times:
        # No message carries its own time: the window runs from the time the host said the words
        # (the request's said_at) to the source's read, because the row is dated at one of them
        # and a row may not be dated outside its episode (S51).
        said = query.read(
            "MATCH (r:Request {source_id: $id}) RETURN r.said_at AS said_at "
            "ORDER BY r.created_at, r.id LIMIT 1",
            id=source_id,
        )
        received = rows[0]["received"] or at
        candidates = [received]
        if said and said[0]["said_at"] is not None and said[0]["said_at"] <= received:
            candidates.append(said[0]["said_at"])
        times = [moment.isoformat() for moment in candidates]
    # ISO text with different offsets does not sort in chronological order.
    instants = [parse_instant(moment) for moment in times]
    start = min(instants)
    end = max(instants)
    first = messages[0]["key"] if messages and messages[0].get("key") else "whole"
    last = messages[-1]["key"] if messages and messages[-1].get("key") else first
    return record_episode(
        driver,
        world_id,
        connector_id=rows[0]["connector"],
        source_ids=[source_id],
        # One episode per request: a message key alone repeats across requests of one host.
        first_message_key=f"{source_id}:{first}",
        last_message_key=f"{source_id}:{last}",
        start_time=start,
        end_time=end,
        cut_rule=EPISODE_CUT_RULE,
        run_id=run_id,
    )


def _messages(content: object) -> list[dict[str, Any]]:
    if not isinstance(content, str):
        return []
    try:
        rows = json.loads(content)
    except ValueError:
        return [{"key": None, "sender": None, "text": content, "at": None}]
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def start_pass(
    driver: Driver,
    auth: RunnerAuth,
    world_id: str,
    *,
    agent: str | None = None,
    settings: PassSettings | None = None,
) -> PassGrant | None:
    """A grant for the world's next pass, or None when nothing is due. PassBusy while another
    pass of the world is live."""
    if auth.gateway_secret is None:
        raise RuntimeError("GROVE_GATEWAY_SECRET is not set")
    settings = settings or PassSettings()
    if agent is None:
        agent = _next_agent(driver, world_id)
        if agent is None:
            return None
    if agent not in AGENTS:
        raise ValueError("unknown agent")
    if agent != "learner" and is_demo_world(driver, world_id):
        return None
    catalog = load_catalog()
    _, job = AGENTS[agent]
    job_spec = catalog.job(job)
    model = catalog.model_for(job_spec).id
    effort = job_spec.effort
    run_id = new_id(world_id)
    started = run_rows.start_run(
        driver,
        world_id,
        run_id=run_id,
        agent=agent,
        trigger="queue" if agent == "learner" else "job",
        model=model,
        effort=effort or "low",
        token_cap=settings.token_cap,
        time_cap=settings.time_cap_seconds,
        stale_margin_seconds=settings.lease_margin_seconds,
    )
    if started is None:
        raise PassBusy(world_id)
    input_ids: list[str] = []
    job_id: str | None = None
    if agent == "learner":
        caller = caller_for(agent)
        for _ in range(settings.learner_requests):
            claimed = claim_next_request(
                driver, world_id, caller, worker=run_id, lease_seconds=settings.lease_seconds
            )
            if claimed is None:
                break
            input_ids.append(claimed.id)
            _ensure_episode(driver, world_id, claimed.source_id, run_id, started["started"])
    else:
        claimed_job = run_rows.claim_job(driver, world_id, agent=agent, worker=run_id)
        if claimed_job is not None:
            job_id = claimed_job["id"]
            input_ids = json.loads(claimed_job.get("input_references") or "[]")
    if not input_ids and job_id is None:
        run_rows.finish_run(
            driver,
            world_id,
            run_id,
            outcome="nothing_due",
            tokens_in=0,
            tokens_cached=0,
            tokens_out=0,
        )
        return None
    run_rows.set_run_inputs(driver, world_id, run_id, input_ids if agent == "learner" else [job_id])
    return PassGrant(
        world_id=world_id,
        run_id=run_id,
        agent=agent,
        input_ids=input_ids,
        job_id=job_id,
        pass_token=mint_pass_token(
            auth.gateway_secret,
            world_id=world_id,
            run_id=run_id,
            agent=agent,
            ttl_seconds=settings.token_ttl_seconds,
        ),
        gateway_token=mint_gateway_token(
            auth.gateway_secret,
            catalog,
            world_id=world_id,
            run_id=run_id,
            job=job,
            ttl_seconds=settings.token_ttl_seconds,
            token_cap=settings.token_cap,
        ),
        time_cap_seconds=settings.time_cap_seconds,
    )


def _next_agent(driver: Driver, world_id: str) -> str | None:
    query = WorldQuery(driver, world_id)
    now = clock.now(world_id)
    if query.read(_DUE_REQUEST, now=now):
        return "learner"
    _ensure_connector_job(driver, world_id)
    agents = {row["agent"] for row in query.read(_DUE_JOBS, now=now)}
    for agent in ("curator", "connector"):
        if agent in agents:
            return agent
    return None


def run_record(driver: Driver, world_id: str, run_id: str) -> dict[str, Any] | None:
    rows = WorldQuery(driver, world_id).read("MATCH (r:RunRecord {id: $id}) RETURN r", id=run_id)
    return rows[0]["r"] if rows else None


_RUN_USAGE = (
    "MATCH (c:ModelCall {run_id: $run_id}) RETURN coalesce(sum(c.input_tokens), 0) AS tokens_in, "
    "coalesce(sum(c.cached_tokens), 0) AS tokens_cached, "
    "coalesce(sum(c.output_tokens), 0) AS tokens_out"
)
_RUN_CHANGED = (
    "MATCH (c:ChangeEntry {run_id: $run_id}) WHERE c.entity_type = 'item' "
    "RETURN DISTINCT c.entity_id AS id"
)
_APPLYING_FOR = (
    "MATCH (r:Request) WHERE r.id IN $ids AND r.status = 'applying' AND r.claimed_by = $worker "
    "RETURN r.id AS id"
)


def end_pass(
    driver: Driver,
    world_id: str,
    run_id: str,
    *,
    outcome: str,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Ends the pass once. Returns the ended run record, or None for an unknown run."""
    record = run_record(driver, world_id, run_id)
    if record is None:
        return None
    if record.get("outcome") != run_rows.RUNNING:
        return record
    agent = record["agent"]
    query = WorldQuery(driver, world_id)
    if agent == "connector" and outcome == "ok":
        # An agent's prose cannot mark an unresolved queue complete. The guarded writer batch
        # resolves or escalates every open row atomically; skipped work retries as a failed job.
        open_rows = query.read("MATCH (c:Contradiction {status: 'open'}) RETURN c.id AS id LIMIT 1")
        pending_support = query.read(
            "MATCH (s:SupportChanged) WHERE s.taken_at IS NULL RETURN s.belief_id AS id LIMIT 1"
        )
        due_retest = query.read(
            "MATCH (b:Belief {externally_testable: true}) MATCH (i:Item {id: b.item_id}) "
            "WHERE b.state <> 'superseded' AND i.recheck_by <= $now RETURN i.id AS id LIMIT 1",
            now=clock.now(world_id),
        )
        saved = query.read(
            "MATCH (r:Request {run_id: $run, status: 'saved'}) RETURN r.id AS id LIMIT 1",
            run=run_id,
        )
        from grove.consolidation.cascades import pending_proposals, pending_reviews
        from grove.consolidation.packet import successful_cursor

        cascades_pending = pending_proposals(query) or pending_reviews(
            query, since=successful_cursor(query), now=clock.now(world_id)
        )
        if open_rows or pending_support or due_retest or cascades_pending or not saved:
            outcome = "failed"
            summary = "The Connector left required work unresolved or committed no guarded batch."
    if agent == "learner":
        caller = caller_for(agent)
        code = NOT_PROPOSED if outcome == "ok" else f"pass_{outcome}"
        for row in query.read(
            _APPLYING_FOR, ids=list(record.get("input_ids") or []), worker=run_id
        ):
            fail_request(driver, world_id, caller, row["id"], code, worker=run_id, retryable=True)
    else:
        for job_id in record.get("input_ids") or []:
            run_rows.end_job(
                driver,
                world_id,
                job_id,
                worker=run_id,
                done=outcome == "ok",
                requeue=outcome in ("preempted", "stopped"),
            )
    usage = query.read(_RUN_USAGE, run_id=run_id)[0]
    ended = run_rows.finish_run(
        driver,
        world_id,
        run_id,
        outcome=outcome,
        tokens_in=usage["tokens_in"],
        tokens_cached=usage["tokens_cached"],
        tokens_out=usage["tokens_out"],
        summary=summary,
    )
    _schedule_after(driver, world_id, agent, run_id)
    return ended


def _schedule_after(driver: Driver, world_id: str, agent: str, run_id: str) -> None:
    """The cycle order of section 4: the Curator after a Learner pass that changed items, on the
    items it touched; the Connector on Appendix C's trigger."""
    if is_demo_world(driver, world_id):
        return
    query = WorldQuery(driver, world_id)
    changed = [row["id"] for row in query.read(_RUN_CHANGED, run_id=run_id)]
    if agent == "learner" and changed:
        run_rows.enqueue_job(
            driver,
            world_id,
            agent="curator",
            trigger="after_update",
            input_references=changed,
            priority=_PRIORITY["curator"],
        )
    if agent != "connector":
        _ensure_connector_job(driver, world_id)


def connector_due(driver: Driver, world_id: str) -> bool:
    """D12's P0 triggers, including drift and due retests on an otherwise idle world."""
    from grove.consolidation.packet import job_trigger, work

    if is_demo_world(driver, world_id):
        return False
    query = WorldQuery(driver, world_id)
    if query.read(
        "MATCH (j:Job {agent_or_job_type: 'connector'}) "
        "WHERE j.state IN ['queued', 'claimed'] RETURN j.id AS id LIMIT 1"
    ):
        return False
    pending = work(driver, world_id)
    if not pending["triggers"]:
        return False
    failed = query.read(
        "MATCH (j:Job {agent_or_job_type: 'connector', state: 'failed'}) "
        "RETURN j.trigger AS trigger ORDER BY j.touched_at DESC LIMIT 1"
    )
    return not failed or failed[0]["trigger"] != job_trigger(pending)


def _ensure_connector_job(driver: Driver, world_id: str) -> None:
    from grove.consolidation.packet import job_trigger, work

    if is_demo_world(driver, world_id):
        return
    if connector_due(driver, world_id):
        pending = work(driver, world_id)
        run_rows.enqueue_job(
            driver,
            world_id,
            agent="connector",
            trigger=job_trigger(pending),
            input_references=[row["id"] for row in pending["changed"]],
            priority=_PRIORITY["connector"],
        )


def stale_cutoff(record: dict[str, Any], settings: PassSettings) -> datetime:
    return record["started"] + timedelta(
        seconds=(record.get("time_cap") or 0) + settings.lease_margin_seconds
    )


__all__ = [
    "CONNECTOR_CHANGED_ITEMS",
    "LEASE_SECONDS",
    "NOT_PROPOSED",
    "PassBusy",
    "PassGrant",
    "PassSettings",
    "RunnerAuth",
    "caller_for",
    "connector_due",
    "due_work",
    "end_pass",
    "run_record",
    "start_pass",
]
