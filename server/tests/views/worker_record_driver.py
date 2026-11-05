"""A synthetic read driver for the worker record. It does not claim native Cypher execution.

It runs the real read wrapper, with its scope and timeout, and answers each query by its shape.
tests/views/test_worker_record_live.py runs the same reads as real Cypher.
"""

from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 11, 4, 18, 0, 0, 123456, tzinfo=UTC)
WORLD_A = "t_workers_00000000-0000-0000-0000-000000000001"
WORLD_B = "t_workers_00000000-0000-0000-0000-000000000002"
NO_ITEMS = {
    "total_items": 0,
    "dated_items": 0,
    "fresh_items": 0,
    "newest_change_at": None,
    "oldest_change_at": None,
}


def run_row(run_id, outcome="ok", **overrides):
    return {
        "id": run_id,
        "outcome": outcome,
        "started": NOW - timedelta(hours=1),
        "finished": None if outcome == "running" else NOW - timedelta(minutes=30),
        **overrides,
    }


def freshness_row(**overrides):
    return {
        "total_items": 3,
        "dated_items": 2,
        "fresh_items": 1,
        "newest_change_at": NOW - timedelta(days=1),
        "oldest_change_at": NOW - timedelta(days=40),
        **overrides,
    }


def read_name(query):
    """Which read of the worker record a query is, by the words only that read holds."""
    for marker, name in (
        ("(r:RunRecord", "runs"),
        ("UNWIND $run_ids", "changed"),
        ("count(DISTINCT item_id)", "count"),
        ("ORDER BY newest DESC", "ids"),
        ("AS total_items", "freshness"),
    ):
        if marker in query:
            return name
    raise AssertionError("a read this driver does not know")


class RecordDriver:
    """worlds: {world id: {"runs": {agent: rows}, "items": {run id: ids}, "freshness": row}}."""

    def __init__(self, worlds=None):
        self.worlds = worlds or {}
        self.calls = []
        self.timeouts = []
        self.sessions = {}
        self.replies = {}
        self.failure = None
        self.fail_on = None

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
        assert query.startswith("CYPHER 25 ")
        if "(s:Session" in query:
            assert "s.revoked_at IS NULL AND s.expires_at > $now" in query
            return self.sessions.get(params["token_hash"], [])
        name = read_name(query)
        if self.failure is not None and self.fail_on in (None, name):
            raise self.failure
        if name in self.replies:
            return self.replies[name]
        world = self.worlds.get(params["world_id"], {})
        items = world.get("items", {})
        if name == "runs":
            return world.get("runs", {}).get(params["agent"], [])[: params["limit"]]
        if name == "changed":
            return [{"run_id": run_id} for run_id in params["run_ids"] if items.get(run_id)]
        if name == "count":
            return [{"changed_items": len(items[params["run_id"]])}]
        if name == "ids":
            return [{"item_id": item} for item in items[params["run_id"]][: params["limit"]]]
        return [world.get("freshness", NO_ITEMS)]
