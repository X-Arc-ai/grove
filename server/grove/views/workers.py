"""Bounded observations of saved run metadata, never process liveness or work admission."""

import json
from datetime import UTC, datetime
from typing import Any

from grove.api.errors import unavailable
from grove.runner.passes import PassSettings
from grove.store.errors import Neo4jError
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.session import ViewSession

AGENTS = ("learner", "curator", "connector")
OUTCOMES = frozenset(
    {"running", "ok", "failed", "killed", "timeout", "stopped", "preempted", "budget", "abandoned"}
)
RECENT_LIMIT = 20
MAX_COUNT = 2_147_483_647
MAX_BYTES = 32 * 1024
READ_TIMEOUT = 5.0

# Equivalent to demo_policy.has_designation, with only booleans returned. In particular,
# neither private account fields nor arbitrary stored marker values enter this read's result.
_DESIGNATION = (
    "MATCH (w:World {id: $world_id}) "
    "OPTIONAL MATCH (u:User {id: w.owner_user_id}) "
    "OPTIONAL MATCH (d:User {demo_world_id: w.id}) "
    "RETURN w.owner_user_id = $user_id AS owner_matches, u.id IS NOT NULL AS owner_exists, "
    "(u.demo_version IS NOT NULL OR u.demo_world_id IS NOT NULL OR "
    "d.demo_version IS NOT NULL OR d.id IS NOT NULL) AS designated LIMIT 2"
)

# Scope is injected into MATCH before grouping. No page limit can hide older running rows.
# CASE checks the native types before temporal arithmetic. Whole elapsed seconds preserve
# the runner's integer-second deadline, including subsecond start times, without date overflow.
_COUNTS = (
    "MATCH (r:RunRecord {outcome: 'running'}) WHERE r.agent IN $agents "
    "WITH r.agent AS agent, CASE "
    "WHEN r.started IS :: ZONED DATETIME NOT NULL AND r.time_cap IS :: INTEGER NOT NULL "
    "AND r.time_cap >= 0 AND r.time_cap <= $max_count AND r.finished IS NULL "
    "AND r.started <= $now "
    "THEN CASE WHEN duration.inSeconds(r.started, $now).seconds < r.time_cap + $margin "
    "THEN 'running' ELSE 'stale' END ELSE 'unknown' END AS observation "
    "RETURN agent, count(*) AS total, "
    "sum(CASE WHEN observation = 'running' THEN 1 ELSE 0 END) AS running, "
    "sum(CASE WHEN observation = 'stale' THEN 1 ELSE 0 END) AS stale, "
    "sum(CASE WHEN observation = 'unknown' THEN 1 ELSE 0 END) AS unknown"
)
_RECENT = (
    "MATCH (r:RunRecord) "
    "RETURN CASE WHEN r.agent IN $agents THEN r.agent ELSE 'unknown' END AS agent, "
    "CASE WHEN r.outcome IN $outcomes THEN r.outcome ELSE 'unknown' END AS outcome, "
    "r.started AS started, r.finished AS finished "
    "ORDER BY r.started DESC, r.id DESC LIMIT $limit"
)


def _refuse() -> None:
    raise unavailable("worker activity is unavailable")


def _moment(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(UTC) if value.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _designation_policy(driver: Any, session: ViewSession) -> bool:
    rows = PlatformQuery(driver, timeout=READ_TIMEOUT).read(
        _DESIGNATION, world_id=session.world_id, user_id=session.user_id
    )
    if (
        len(rows) != 1
        or not isinstance(rows[0], dict)
        or set(rows[0]) != {"owner_matches", "owner_exists", "designated"}
    ):
        _refuse()
    row = rows[0]
    if any(type(value) is not bool for value in row.values()):
        _refuse()
    if not row["owner_matches"] or not row["owner_exists"]:
        _refuse()
    return row["designated"]


def _run_counts(world: WorldQuery, *, now: datetime, settings: PassSettings) -> dict[str, dict]:
    margin = settings.lease_margin_seconds
    if type(margin) is not int or not 0 <= margin <= MAX_COUNT:
        _refuse()
    rows = world.read(_COUNTS, agents=list(AGENTS), now=now, margin=margin, max_count=MAX_COUNT)
    if len(rows) > len(AGENTS):
        _refuse()
    result = {agent: {"running": 0, "stale": 0, "unknown": 0} for agent in AGENTS}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "agent",
            "total",
            "running",
            "stale",
            "unknown",
        }:
            _refuse()
        agent = row["agent"]
        if not isinstance(agent, str) or agent not in AGENTS or agent in seen:
            _refuse()
        seen.add(agent)
        if any(
            type(row[key]) is not int or not 0 <= row[key] <= MAX_COUNT
            for key in ("total", "running", "stale", "unknown")
        ):
            _refuse()
        counts = {key: row[key] for key in ("running", "stale", "unknown")}
        if sum(counts.values()) != row["total"] or row["total"] == 0:
            _refuse()
        result[agent] = counts
    return result


def _classify_activity(counts: dict[str, int]) -> dict[str, Any]:
    state = "idle"
    if counts["unknown"]:
        state = "unknown"
    elif counts["running"]:
        state = "recorded_running"
    elif counts["stale"]:
        state = "stale"
    return {"state": state, "running_records": counts["running"], "stale_records": counts["stale"]}


def _public_run(row: dict) -> dict[str, Any]:
    agent = row.get("agent")
    outcome = row.get("outcome")
    known_agent = isinstance(agent, str) and agent in AGENTS
    known_outcome = isinstance(outcome, str) and outcome in OUTCOMES
    started = _moment(row.get("started"))
    finished = _moment(row.get("finished"))
    malformed = not known_agent or not known_outcome or started is None
    if row.get("finished") is not None and finished is None:
        malformed = True
    if started is not None and finished is not None and finished < started:
        finished, malformed = None, True
    if outcome == "running" and finished is not None:
        malformed = True
    if known_outcome and outcome != "running" and finished is None:
        malformed = True
    return {
        "agent": agent if known_agent else "unknown",
        "outcome": outcome if not malformed else "unknown",
        "started_at": _stamp(started),
        "finished_at": _stamp(finished),
    }


def _recent_runs(world: WorldQuery) -> tuple[list[dict], bool]:
    rows = world.read(
        _RECENT, limit=RECENT_LIMIT + 1, agents=list(AGENTS), outcomes=sorted(OUTCOMES)
    )
    if len(rows) > RECENT_LIMIT + 1 or any(not isinstance(row, dict) for row in rows):
        _refuse()
    return [_public_run(row) for row in rows[:RECENT_LIMIT]], len(rows) > RECENT_LIMIT


def worker_activity_payload(
    driver: Any, session: ViewSession, *, now: datetime, settings: PassSettings | None = None
) -> dict[str, Any]:
    """Three read transactions, not a common snapshot, admission check or complete job census."""
    moment = _moment(now)
    if moment is None:
        _refuse()
    world = WorldQuery(driver, session.world_id, timeout=READ_TIMEOUT)
    try:
        designated = _designation_policy(driver, session)
        counts = _run_counts(world, now=moment, settings=settings or PassSettings())
        recent, has_more = _recent_runs(world)
    except Neo4jError:
        # A query failure must not turn into an empty or apparently idle account.
        raise unavailable("worker activity is unavailable") from None
    capabilities = (
        ("limited", "legacy_learner_only"),
        ("unavailable", "curator_authority_unavailable"),
        ("limited", "legacy_connector_only"),
    )
    agents = []
    for agent, (state, reason) in zip(AGENTS, capabilities, strict=True):
        policy = {"state": "allowed", "reason": "ordinary_world_policy"}
        if designated:
            policy = (
                {"state": "limited", "reason": "demo_learner_only"}
                if agent == "learner"
                else {"state": "disabled", "reason": "demo_autonomous_disabled"}
            )
        agents.append(
            {
                "agent": agent,
                "policy": policy,
                "capability": {"state": state, "reason": reason},
                "activity": _classify_activity(counts[agent]),
            }
        )
    payload = {
        "schema": "grove.worker-activity.v1",
        "observed_at": _stamp(moment),
        "observation_kind": "stored_run_records",
        "service_state": "unknown",
        "service_reason": "no_process_heartbeat",
        "agents": agents,
        "recent_runs": recent,
        "recent_limit": RECENT_LIMIT,
        "recent_has_more": has_more,
    }
    if len(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()) > MAX_BYTES:
        _refuse()
    return payload
