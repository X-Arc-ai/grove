"""Synthetic metadata controls; these do not claim native Cypher execution."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from grove.api.errors import ApiError
from grove.runner.passes import PassSettings, stale_cutoff
from grove.store.errors import ServiceUnavailable
from grove.store.query import WorldQuery
from grove.views.session import ViewSession
from grove.views.workers import (
    AGENTS,
    MAX_COUNT,
    READ_TIMEOUT,
    _classify_activity,
    _public_run,
    _run_counts,
    worker_activity_payload,
)

NOW = datetime(2026, 11, 4, 18, 0, 0, 123456, tzinfo=UTC)
WORLD_A = "t_workers_00000000-0000-0000-0000-000000000001"
WORLD_B = "t_workers_00000000-0000-0000-0000-000000000002"
SESSION_A = ViewSession("user-a", WORLD_A, None)
SESSION_B = ViewSession("user-b", WORLD_B, None)


def run_row(agent="learner", outcome="running", **overrides):
    return {
        "agent": agent,
        "outcome": outcome,
        "started": NOW - timedelta(seconds=30),
        "finished": None if outcome == "running" else NOW,
        "time_cap": 600,
        **overrides,
    }


class SyntheticDriver:
    """Execute the read wrapper, including scope and timeout, without a native connection."""

    def __init__(self, rows=None, designated=False):
        self.rows = rows or {WORLD_A: [], WORLD_B: []}
        self.designated = designated
        self.calls = []
        self.timeouts = []
        self.designation_rows = None
        self.count_rows = None
        self.recent_rows = None
        self.failure = None
        self.sessions = {}

    def session(self, **kwargs):
        assert set(kwargs) == {"database"}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute_read(self, work):
        self.timeouts.append(work.timeout)
        return work(self)

    def run(self, query, params):
        self.calls.append((query, params.copy()))
        assert query.startswith("CYPHER 25 MATCH ")
        if "(s:Session" in query:
            assert "s.revoked_at IS NULL AND s.expires_at > $now" in query
            return self.sessions.get(params["token_hash"], [])
        if self.failure is not None:
            raise self.failure
        if "(w:World" in query:
            assert "w:World {id: $world_id}" in query
            assert "email_hash" not in query
            if self.designation_rows is not None:
                return self.designation_rows
            owner = {WORLD_A: "user-a", WORLD_B: "user-b"}.get(params["world_id"])
            return [
                {
                    "owner_matches": owner == params["user_id"],
                    "owner_exists": owner is not None,
                    "designated": self.designated,
                }
            ]
        assert "r:RunRecord {world_id: $world_id" in query
        for forbidden in ("input_ids", "proposed_operations", "receipts", "decline_ids", "summary"):
            assert forbidden not in query
        rows = self.rows.get(params["world_id"], [])
        if "count(*) AS total" in query:
            assert "LIMIT" not in query
            assert query.index("world_id: $world_id") < query.index("WITH ")
            assert "duration.inSeconds(r.started, $now).seconds < r.time_cap + $margin" in query
            if self.count_rows is not None:
                return self.count_rows
            result = []
            for agent in params["agents"]:
                classified = Counter()
                for row in rows:
                    if row["agent"] != agent or row["outcome"] != "running":
                        continue
                    start, cap = row.get("started"), row.get("time_cap")
                    valid = (
                        isinstance(start, datetime)
                        and start.tzinfo is not None
                        and type(cap) is int
                        and 0 <= cap <= params["max_count"]
                        and row.get("finished") is None
                        and start <= params["now"]
                    )
                    state = "unknown"
                    if valid:
                        cutoff = stale_cutoff(
                            row, PassSettings(lease_margin_seconds=params["margin"])
                        )
                        state = "running" if cutoff > params["now"] else "stale"
                    classified[state] += 1
                if classified:
                    result.append(
                        {
                            "agent": agent,
                            "total": classified.total(),
                            **{key: classified[key] for key in ("running", "stale", "unknown")},
                        }
                    )
            return result
        assert "ORDER BY r.started DESC, r.id DESC LIMIT $limit" in query
        assert query.index("world_id: $world_id") < query.index("LIMIT")
        assert "ELSE 'unknown'" in query
        if self.recent_rows is not None:
            return self.recent_rows
        # The fixture's input order is the specified database order. It returns only columns
        # requested by the real query. It does not evaluate arbitrary Cypher.
        return [
            {key: row.get(key) for key in ("agent", "outcome", "started", "finished")}
            for row in rows[: params["limit"]]
        ]


def payload(driver=None, session=SESSION_A, **kwargs):
    return worker_activity_payload(driver or SyntheticDriver(), session, now=NOW, **kwargs)


def test_empty_observation_is_not_process_health_or_runtime_capability():
    driver = SyntheticDriver()
    result = payload(driver)
    assert result["service_state"] == "unknown"
    assert result["service_reason"] == "no_process_heartbeat"
    assert [a["agent"] for a in result["agents"]] == list(AGENTS)
    assert all(a["activity"]["state"] == "idle" for a in result["agents"])
    assert result["agents"][1]["capability"] == {
        "state": "unavailable",
        "reason": "curator_authority_unavailable",
    }
    assert all(
        a["policy"] == {"state": "allowed", "reason": "ordinary_world_policy"}
        for a in result["agents"]
    )
    assert result["recent_runs"] == [] and result["recent_has_more"] is False
    assert result["observed_at"] == "2026-11-04T18:00:00.123456Z"
    assert driver.timeouts == [READ_TIMEOUT] * 3


def test_disabled_policy_keeps_actual_recorded_activity():
    driver = SyntheticDriver({WORLD_A: [run_row("connector")]}, designated=True)
    result = payload(driver)
    assert result["agents"][2]["policy"]["state"] == "disabled"
    assert result["agents"][2]["activity"] == {
        "state": "recorded_running",
        "running_records": 1,
        "stale_records": 0,
    }
    assert result["agents"][0]["policy"]["state"] == "limited"


@pytest.mark.parametrize("offset,expected", [(1, "recorded_running"), (0, "stale"), (-1, "stale")])
def test_exact_stale_boundary_preserves_subseconds(offset, expected):
    start = NOW - timedelta(seconds=623, microseconds=-offset)
    driver = SyntheticDriver({WORLD_A: [run_row(started=start)]})
    result = payload(driver, settings=PassSettings(lease_margin_seconds=23))
    assert result["agents"][0]["activity"]["state"] == expected


@pytest.mark.parametrize(
    "change",
    [
        {"time_cap": True},
        {"time_cap": -1},
        {"time_cap": float("inf")},
        {"time_cap": None},
        {"time_cap": MAX_COUNT + 1},
        {"started": None},
        {"started": "private-time"},
        {"started": NOW.replace(tzinfo=None)},
        {"started": NOW + timedelta(seconds=1)},
        {"finished": NOW},
    ],
)
def test_bad_running_metadata_is_unknown_not_idle(change):
    result = payload(SyntheticDriver({WORLD_A: [run_row(**change)]}))
    assert result["agents"][0]["activity"]["state"] == "unknown"


def test_recent_limit_does_not_hide_older_active_work_or_foreign_worlds():
    rows = [run_row(outcome="ok") for _ in range(25)] + [run_row("curator")]
    driver = SyntheticDriver({WORLD_A: rows, WORLD_B: [run_row("connector")] * 40})
    result = payload(driver)
    assert len(result["recent_runs"]) == 20 and result["recent_has_more"] is True
    assert result["agents"][1]["activity"]["running_records"] == 1
    assert result["agents"][2]["activity"]["running_records"] == 0
    other = payload(driver, session=SESSION_B)
    assert other["agents"][2]["activity"]["running_records"] == 40
    assert other["agents"][1]["activity"]["running_records"] == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"owner_matches": True}],
        [{"owner_matches": True, "owner_exists": True, "designated": False}] * 2,
        [{"owner_matches": False, "owner_exists": True, "designated": False}],
        [{"owner_matches": True, "owner_exists": False, "designated": False}],
        [{"owner_matches": True, "owner_exists": True, "designated": "private-marker"}],
    ],
)
def test_bad_designation_refuses_before_run_reads(rows):
    driver = SyntheticDriver()
    driver.designation_rows = rows
    with pytest.raises(ApiError, match="worker activity is unavailable") as caught:
        payload(driver)
    assert caught.value.status == 503 and len(driver.calls) == 1


@pytest.mark.parametrize(
    "key,bad",
    [
        ("total", True),
        ("running", -1),
        ("stale", float("nan")),
        ("unknown", MAX_COUNT + 1),
        ("total", 9),
    ],
)
def test_invalid_aggregate_refuses_instead_of_fabricating_empty_state(key, bad):
    row = {"agent": "learner", "total": 1, "running": 1, "stale": 0, "unknown": 0, key: bad}
    driver = SyntheticDriver()
    driver.count_rows = [row]
    with pytest.raises(ApiError):
        payload(driver)


@pytest.mark.parametrize("margin", [True, -1, float("nan"), None, MAX_COUNT + 1])
def test_invalid_configuration_refuses_before_count_query(margin):
    driver = SyntheticDriver()
    with pytest.raises(ApiError):
        _run_counts(
            WorldQuery(driver, WORLD_A),
            now=NOW,
            settings=SimpleNamespace(lease_margin_seconds=margin),
        )
    assert not driver.calls


def test_public_rows_never_echo_unknown_codes_or_extra_fields():
    row = run_row(
        agent="private-agent",
        outcome="private-error",
        input_ids=["private-id"],
        summary="private-output",
        started="private-time",
        finished="private-time",
    )
    assert _public_run(row) == {
        "agent": "unknown",
        "outcome": "unknown",
        "started_at": None,
        "finished_at": None,
    }


def test_impossible_finish_order_is_unknown_and_bad_time_is_null():
    row = run_row(outcome="ok", started=NOW, finished=NOW - timedelta(seconds=1))
    result = _public_run(row)
    assert result["outcome"] == "unknown" and result["finished_at"] is None


def test_known_success_does_not_supply_runtime_or_service_health():
    result = payload(SyntheticDriver({WORLD_A: [run_row("curator", "ok")]}))
    assert result["recent_runs"][0]["outcome"] == "ok"
    assert result["agents"][1]["capability"]["state"] == "unavailable"
    assert result["service_state"] == "unknown"


def test_store_failure_never_returns_idle():
    driver = SyntheticDriver()
    driver.failure = ServiceUnavailable("private connection detail")
    with pytest.raises(ServiceUnavailable):
        payload(driver)


def test_unknown_has_priority_over_partial_known_counts():
    assert _classify_activity({"running": 1, "stale": 1, "unknown": 1}) == {
        "state": "unknown",
        "running_records": 1,
        "stale_records": 1,
    }


@pytest.mark.parametrize("marker", ["u.demo_version", "u.demo_world_id", "d.demo_version", "d.id"])
def test_every_partial_designation_marker_is_checked_without_disclosing_values(marker):
    driver = SyntheticDriver(designated=True)
    result = payload(driver)
    query = driver.calls[0][0]
    assert marker + " IS NOT NULL" in query
    assert result["agents"][1]["policy"]["state"] == "disabled"
    assert result["agents"][2]["policy"]["state"] == "disabled"


def test_duplicate_aggregate_agent_is_refused():
    driver = SyntheticDriver()
    driver.count_rows = [
        {"agent": "learner", "total": 1, "running": 1, "stale": 0, "unknown": 0}
    ] * 2
    with pytest.raises(ApiError):
        payload(driver)


def test_excess_recent_rows_are_refused_not_silently_truncated():
    driver = SyntheticDriver()
    driver.recent_rows = [run_row()] * 22
    with pytest.raises(ApiError):
        payload(driver)


def test_invalid_recent_timestamp_is_unknown():
    result = _public_run(run_row(outcome="ok", finished="private-date"))
    assert result["outcome"] == "unknown"
    assert result["finished_at"] is None


def test_malformed_clock_is_refused_before_any_read():
    driver = SyntheticDriver()
    with pytest.raises(ApiError):
        worker_activity_payload(driver, SESSION_A, now=NOW.replace(tzinfo=None))
    assert not driver.calls
