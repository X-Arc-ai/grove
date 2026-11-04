"""Worker activity is a session-only, read-only API, never a scheduler probe."""

import json

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.session import current_session
from grove.store.errors import ServiceUnavailable
from grove.views.session import SESSION_COOKIE, token_hash
from tests.views.test_worker_activity import (
    NOW,
    SESSION_A,
    SESSION_B,
    WORLD_A,
    WORLD_B,
    SyntheticDriver,
    run_row,
)


def test_anonymous_worker_status_refuses_before_any_store_read():
    class NoStore:
        def session(self, **_kwargs):
            raise AssertionError("anonymous worker activity must not read the store")

    response = TestClient(create_app(driver=NoStore())).get("/v1/views/workers")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["cache-control"] == "no-store"


def client(driver, session=SESSION_A):
    app = create_app(driver=driver)
    app.dependency_overrides[current_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda *_args: NOW)


def test_authenticated_worker_route_returns_the_fixed_metadata_contract():
    response = client(SyntheticDriver()).get("/v1/views/workers")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "schema",
        "observed_at",
        "observation_kind",
        "service_state",
        "service_reason",
        "agents",
        "recent_runs",
        "recent_limit",
        "recent_has_more",
    }
    assert response.json()["schema"] == "grove.worker-activity.v1"
    assert response.json()["recent_limit"] == 20
    assert len(response.content) < 32 * 1024


@pytest.mark.parametrize("state", ["expired", "revoked", "unknown"])
def test_invalid_session_returns_401_before_world_projection(state):
    driver = SyntheticDriver()
    response = TestClient(create_app(driver=driver), cookies={SESSION_COOKIE: state}).get(
        "/v1/views/workers"
    )
    assert response.status_code == 401
    assert len(driver.calls) == 1 and "(s:Session" in driver.calls[0][0]
    assert response.headers["cache-control"] == "no-store"


def test_real_session_dependency_keeps_worlds_separate():
    driver = SyntheticDriver({WORLD_A: [run_row("learner")], WORLD_B: [run_row("connector")] * 21})
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
    a = TestClient(app, cookies={SESSION_COOKIE: "test-token-a"}).get("/v1/views/workers").json()
    b = TestClient(app, cookies={SESSION_COOKIE: "test-token-b"}).get("/v1/views/workers").json()
    assert a["agents"][0]["activity"]["running_records"] == 1
    assert a["agents"][2]["activity"]["running_records"] == 0
    assert a["recent_has_more"] is False
    assert b["agents"][0]["activity"]["running_records"] == 0
    assert b["agents"][2]["activity"]["running_records"] == 21
    assert len(b["recent_runs"]) == 20 and b["recent_has_more"] is True
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
    driver = SyntheticDriver()
    response = client(driver).get("/v1/views/workers?" + query)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"field": "query"}
    assert "private" not in response.text
    assert not driver.calls


def test_store_failure_is_typed_unavailable_not_an_empty_feed():
    driver = SyntheticDriver()
    driver.failure = ServiceUnavailable("private-host-and-password")
    response = client(driver).get("/v1/views/workers")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert "private" not in response.text
    assert "agents" not in response.json()
    assert response.headers["cache-control"] == "no-store"


def test_malformed_designation_is_refused_not_ordinary_policy():
    driver = SyntheticDriver()
    driver.designation_rows = [{"owner_matches": True, "owner_exists": True, "designated": None}]
    response = client(driver).get("/v1/views/workers")
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "worker activity is unavailable"
    assert len(driver.calls) == 1


def test_read_does_not_start_select_recover_or_end_work(monkeypatch):
    from grove.runner import passes
    from grove.writer import runs

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the read route invoked a work mutation")

    for name in ("due_work", "start_pass", "live_run", "has_live_run"):
        monkeypatch.setattr(passes, name, forbidden)
    for name in ("abandon_stale_runs", "start_run", "set_run_inputs", "note_proposal"):
        monkeypatch.setattr(runs, name, forbidden)
    response = client(SyntheticDriver()).get("/v1/views/workers")
    assert response.status_code == 200


def test_arbitrary_saved_fields_and_codes_do_not_enter_the_response():
    row = run_row(
        agent="private-agent",
        outcome="private-model-output",
        summary="private-summary",
        input_ids=["private-input"],
        receipts=["private-receipt"],
    )
    response = client(SyntheticDriver({WORLD_A: [row]})).get("/v1/views/workers")
    assert response.status_code == 200
    assert "private" not in response.text
    assert set(response.json()["recent_runs"][0]) == {
        "agent",
        "outcome",
        "started_at",
        "finished_at",
    }


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_worker_status_has_no_mutating_method(method):
    driver = SyntheticDriver()
    response = getattr(client(driver), method)("/v1/views/workers")
    assert response.status_code == 405
    assert not driver.calls
