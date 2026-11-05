"""The worker record is a session-only, read-only API with a fixed shape.

This file imports nothing that only the route's own change adds, so against the code without the
route it fails on the route and not on an import.
"""

import json

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.session import current_session
from grove.store.errors import ServiceUnavailable
from grove.store.probe import DriverError, Neo4jError
from grove.views.session import SESSION_COOKIE, ViewSession, token_hash
from tests.views.worker_record_driver import (
    NOW,
    WORLD_A,
    WORLD_B,
    RecordDriver,
    freshness_row,
    run_row,
)

ROUTE = "/v1/views/workers/record"
SESSION_A = ViewSession("user-a", WORLD_A, None)
SESSION_B = ViewSession("user-b", WORLD_B, None)
ITEM_ONE = "item-one"
ITEM_TWO = "item-two"


def filled_world():
    return {
        WORLD_A: {
            "runs": {"learner": [run_row("private-run-id")]},
            "items": {"private-run-id": [ITEM_ONE, ITEM_TWO]},
            "freshness": freshness_row(),
        }
    }


def client(driver, session=SESSION_A):
    app = create_app(driver=driver)
    app.dependency_overrides[current_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda *_args: NOW)


def test_anonymous_record_refuses_before_any_store_read():
    class NoStore:
        def session(self, **_kwargs):
            raise AssertionError("an anonymous worker record must not read the store")

    response = TestClient(create_app(driver=NoStore())).get(ROUTE)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("state", ["expired", "revoked", "unknown"])
def test_invalid_session_returns_401_before_world_projection(state):
    driver = RecordDriver(filled_world())
    response = TestClient(create_app(driver=driver), cookies={SESSION_COOKIE: state}).get(ROUTE)
    assert response.status_code == 401
    assert len(driver.calls) == 1 and "(s:Session" in driver.calls[0][0]
    assert response.headers["cache-control"] == "no-store"


def test_the_route_returns_the_exact_keys_with_no_store():
    response = client(RecordDriver(filled_world())).get(ROUTE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) < 32 * 1024
    body = response.json()
    assert set(body) == {"schema", "observed_at", "observation_kind", "agents", "freshness"}
    assert body["schema"] == "grove.worker-record.v1"
    assert body["observation_kind"] == "stored_change_entries"
    assert body["observed_at"] == "2026-11-04T18:00:00.123456Z"
    assert [agent["agent"] for agent in body["agents"]] == ["learner", "curator", "connector"]
    assert all(set(agent) == {"agent", "last_run", "trail"} for agent in body["agents"])
    learner = body["agents"][0]
    assert set(learner["last_run"]) == {"outcome", "started_at", "finished_at"}
    assert set(learner["trail"]) == {
        "outcome",
        "started_at",
        "finished_at",
        "changed_items",
        "item_ids",
        "has_more",
    }
    assert set(body["freshness"]) == {
        "total_items",
        "dated_items",
        "window_days",
        "fresh_items",
        "newest_change_at",
        "oldest_change_at",
    }


def test_the_route_reads_time_through_the_clock_for_the_session_world(monkeypatch):
    asked = []
    monkeypatch.setattr(clock, "now", lambda *args: asked.append(args) or NOW)
    assert client(RecordDriver()).get(ROUTE).status_code == 200
    assert asked == [(WORLD_A,)]


def test_real_session_dependency_keeps_worlds_separate():
    worlds = filled_world()
    worlds[WORLD_B] = {
        "runs": {"connector": [run_row("other-run", "failed")]},
        "items": {"other-run": ["item-of-b"]},
    }
    driver = RecordDriver(worlds)
    for token, session in [("test-token-a", SESSION_A), ("test-token-b", SESSION_B)]:
        driver.sessions[token_hash(token)] = [
            {
                "world_id": session.world_id,
                "user_id": session.user_id,
                "self_item_id": None,
                "method": "email",
            }
        ]
    app = create_app(driver=driver)
    a = TestClient(app, cookies={SESSION_COOKIE: "test-token-a"}).get(ROUTE).json()
    b = TestClient(app, cookies={SESSION_COOKIE: "test-token-b"}).get(ROUTE).json()
    assert a["agents"][0]["trail"]["item_ids"] == [ITEM_ONE, ITEM_TWO]
    assert a["agents"][2]["trail"] is None
    assert b["agents"][0]["trail"] is None
    assert b["agents"][2]["trail"]["item_ids"] == ["item-of-b"]
    assert b["agents"][2]["last_run"]["outcome"] == "failed"
    assert WORLD_A not in json.dumps(b) and WORLD_B not in json.dumps(a)


@pytest.mark.parametrize(
    "query",
    [
        "world_id=foreign",
        "agent=learner",
        "limit=999",
        "cursor=x",
        "as_of=now",
        "private-key=private-value",
    ],
)
def test_query_parameters_cannot_select_scope_or_bounds(query):
    driver = RecordDriver(filled_world())
    response = client(driver).get(ROUTE + "?" + query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"field": "query"}
    assert "private" not in response.text
    assert not driver.calls
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "failure",
    [
        ServiceUnavailable("private-host-and-password"),
        Neo4jError("private-host-and-password"),
        DriverError("private-host-and-password"),
    ],
)
@pytest.mark.parametrize("read", ["runs", "changed", "ids", "count", "freshness"])
def test_store_failure_is_typed_unavailable_not_an_empty_record(failure, read):
    driver = RecordDriver(filled_world())
    driver.failure, driver.fail_on = failure, read
    response = client(driver).get(ROUTE)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert response.json()["error"]["retryable"] is True
    assert "private" not in response.text
    assert "agents" not in response.json() and "freshness" not in response.json()
    assert response.headers["cache-control"] == "no-store"


def test_malformed_rows_are_refused_not_an_empty_record():
    driver = RecordDriver(filled_world())
    driver.replies["freshness"] = [{"total_items": "private-count"}]
    response = client(driver).get(ROUTE)
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "worker activity is unavailable"
    assert "private" not in response.text


def test_read_does_not_start_select_recover_or_end_work(monkeypatch):
    from grove.runner import passes
    from grove.writer import runs

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the read route invoked a work mutation")

    for name in ("due_work", "start_pass", "live_run", "has_live_run"):
        monkeypatch.setattr(passes, name, forbidden)
    for name in ("abandon_stale_runs", "start_run", "set_run_inputs", "note_proposal"):
        monkeypatch.setattr(runs, name, forbidden)
    assert client(RecordDriver(filled_world())).get(ROUTE).status_code == 200


def test_run_ids_and_saved_fields_do_not_enter_the_response():
    worlds = filled_world()
    worlds[WORLD_A]["runs"]["learner"] = [
        run_row(
            "private-run-id",
            outcome="private-model-output",
            summary="private-summary",
            input_ids=["private-input"],
        )
    ]
    driver = RecordDriver(worlds)
    # Extra columns are refused by the row check. The stored values never reach the page.
    assert client(driver).get(ROUTE).status_code == 503
    worlds[WORLD_A]["runs"]["learner"] = [run_row("private-run-id", "private-model-output")]
    response = client(driver).get(ROUTE)
    assert response.status_code == 200
    assert "private" not in response.text
    assert response.json()["agents"][0]["last_run"]["outcome"] == "unknown"


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_worker_record_has_no_mutating_method(method):
    driver = RecordDriver()
    response = getattr(client(driver), method)(ROUTE)
    assert response.status_code == 405
    assert not driver.calls
