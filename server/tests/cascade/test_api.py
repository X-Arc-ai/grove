"""D6 explicit enqueue, session scope, CSRF checks, and the post-commit lifespan hook."""

from threading import Event

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api import cascade
from grove.api.app import create_app
from grove.api.cascade import ExplanationDispatcher
from grove.api.config import ApiSettings
from grove.cascade_jobs import explain_pending, packet_fingerprint
from grove.store.d3_fingerprint import live_fingerprint
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE
from grove.writer.cascade_explanations import cache_explanation, request_explanation
from grove.writer.testing import plant_session
from tests.cascade.test_explanation_cache import (
    SCREEN,
    Layer,
    frozen,  # noqa: F401  Shared scoped graph fixtures.
    loaded,
    result,
)
from tests.cascade.test_explanation_cache import (
    graph as graph_fixture,
)

SETTINGS = ApiSettings(public_base_url="https://grove.example")
graph = graph_fixture


def client_for(driver, world_id):
    return TestClient(
        create_app(SETTINGS, driver=driver),
        cookies={SESSION_COOKIE: plant_session(driver, world_id)},
    )


def route(graph):
    return f"/v1/cascades/{graph['mark']}/explanation"


def jobs(driver, world):
    return WorldQuery(driver, world).read("MATCH (j:Job) RETURN j ORDER BY j.id")


def test_explanation_route_requires_session(driver, graph):
    before = live_fingerprint(driver, graph["world"])
    with TestClient(create_app(SETTINGS, driver=driver)) as client:
        response = client.post(route(graph), json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["cache-control"] == "no-store"
    assert live_fingerprint(driver, graph["world"]) == before


@pytest.mark.parametrize("origin", [None, SETTINGS.public_base_url])
def test_explicit_request_queues_without_a_model_call(driver, graph, monkeypatch, origin):
    def forbidden(*args, **kwargs):
        pytest.fail("the request must not construct a model layer")

    monkeypatch.setattr(cascade, "ModelLayer", forbidden)
    with client_for(driver, graph["world"]) as client:
        response = client.post(route(graph), json={}, headers={"Origin": origin} if origin else {})
        repeated = client.post(route(graph), json={})
    assert response.status_code == 200 and repeated.json() == response.json()
    assert response.json()["mark_id"] == graph["mark"]
    assert response.json()["by"] == "template" and response.json()["needs_explanation"]
    assert response.headers["cache-control"] == "no-store"
    [job] = jobs(driver, graph["world"])
    assert job["j"]["agent_or_job_type"] == "explain_break"
    assert job["j"]["state"] == "queued" and job["j"]["attempts"] == 0


def test_cached_response_does_not_enqueue(driver, graph):
    outcome = result(driver, graph)
    assert (
        cache_explanation(
            driver,
            graph["world"],
            graph["mark"],
            outcome,
            expected_version=1,
            expected_packet=packet_fingerprint(loaded(driver, graph).packet),
            screener=SCREEN,
        )
        == "saved"
    )
    with client_for(driver, graph["world"]) as client:
        before = live_fingerprint(driver, graph["world"])
        response = client.post(route(graph), json={})
    assert response.status_code == 200
    assert response.json() == {
        "mark_id": graph["mark"],
        "text": outcome.text,
        "by": "spark",
        "needs_explanation": False,
    }
    assert live_fingerprint(driver, graph["world"]) == before
    assert not jobs(driver, graph["world"])


def test_another_session_world_cannot_request_the_mark(driver, graph, make_world):
    other = make_world()
    with client_for(driver, other) as client:
        before = {world: live_fingerprint(driver, world) for world in (other, graph["world"])}
        response = client.post(route(graph), json={})
    assert response.status_code == 404
    assert {world: live_fingerprint(driver, world) for world in before} == before
    assert not jobs(driver, other) and not jobs(driver, graph["world"])


@pytest.mark.parametrize("origin", ["https://other.example", "null", "https://grove.example.evil"])
def test_foreign_origin_is_refused_without_a_job(driver, graph, origin):
    with client_for(driver, graph["world"]) as client:
        before = live_fingerprint(driver, graph["world"])
        response = client.post(route(graph), json={}, headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    assert response.headers["cache-control"] == "no-store"
    assert live_fingerprint(driver, graph["world"]) == before


@pytest.mark.parametrize(
    "content, media_type",
    [
        (b"{}", None),
        (b"{}", "text/plain"),
        (b"{}", "application/x-www-form-urlencoded"),
        (b"not json", "application/json"),
        (b'{"world_id":"other"}', "application/json"),
        (b"[]", "application/json"),
        (b" " * 1025, "application/json"),
    ],
)
def test_only_an_empty_json_object_can_enqueue(driver, graph, content, media_type):
    with client_for(driver, graph["world"]) as client:
        before = live_fingerprint(driver, graph["world"])
        response = client.post(
            route(graph),
            content=content,
            headers={"Content-Type": media_type} if media_type else {},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert live_fingerprint(driver, graph["world"]) == before


def test_injected_driver_has_no_implicit_dispatcher(driver, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an injected driver must not start a dispatcher without the explicit seam")

    monkeypatch.setattr(cascade, "ExplanationDispatcher", forbidden)
    with TestClient(create_app(SETTINGS, driver=driver)):
        pass
    driver.verify_connectivity()


def test_explicit_test_dispatcher_processes_queued_work_after_commit(driver, graph):
    request_explanation(driver, graph["world"], graph["mark"])
    completed, closed = Event(), Event()

    class ClosingLayer(Layer):
        def close(self):
            closed.set()

    layer = ClosingLayer(graph)

    def dispatch(*args, **kwargs):
        report = explain_pending(*args, **kwargs, screener=SCREEN)
        assert report.saved == 1
        completed.set()

    def factory(store):
        return ExplanationDispatcher(
            store,
            layer_factory=lambda _: layer,
            inventory=lambda _: (graph["world"],),
            dispatch=dispatch,
            interval=30,
        )

    with TestClient(create_app(SETTINGS, driver=driver, explanation_dispatcher_factory=factory)):
        assert completed.wait(3)
    assert closed.is_set() and layer.calls == 1
    assert loaded(driver, graph).cached is not None
    driver.verify_connectivity()


def test_dispatcher_does_not_construct_layer_without_pending_work():
    def forbidden(_):
        pytest.fail("no pending work must mean no model client")

    dispatcher = ExplanationDispatcher(object(), inventory=lambda _: (), layer_factory=forbidden)
    dispatcher.run_once()
    assert dispatcher.status == "idle"


def test_missing_key_leaves_requested_job_retryable(driver, graph, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    request_explanation(driver, graph["world"], graph["mark"])
    before = live_fingerprint(driver, graph["world"])
    dispatcher = ExplanationDispatcher(driver, inventory=lambda _: (graph["world"],))
    dispatcher.run_once()
    dispatcher.run_once()
    assert dispatcher.status == "unavailable"
    assert live_fingerprint(driver, graph["world"]) == before
    [row] = jobs(driver, graph["world"])
    assert row["j"]["state"] == "queued" and row["j"]["attempts"] == 0


class OwnedDriver:
    def __init__(self):
        self.closed = Event()

    def close(self):
        self.closed.set()


def test_owned_driver_starts_default_dispatcher_and_closes_after_stop(monkeypatch):
    store, visited = OwnedDriver(), Event()

    def factory(driver):
        assert driver is store

        def inventory(_):
            visited.set()
            return ()

        return ExplanationDispatcher(driver, inventory=inventory)

    monkeypatch.setattr(cascade, "ExplanationDispatcher", factory)
    with TestClient(create_app(SETTINGS, driver_factory=lambda: store)):
        assert visited.wait(1)
        assert not store.closed.is_set()
    assert store.closed.is_set()


def test_shutdown_does_not_wait_for_active_provider_and_resources_close_after_it():
    store = OwnedDriver()
    entered, release, model_closed = Event(), Event(), Event()

    class SlowLayer:
        def close(self):
            model_closed.set()

    def slow_dispatch(*args, **kwargs):
        entered.set()
        assert release.wait(3)

    def factory(driver):
        return ExplanationDispatcher(
            driver,
            inventory=lambda _: ("synthetic-world",),
            layer_factory=lambda _: SlowLayer(),
            dispatch=slow_dispatch,
        )

    try:
        with TestClient(
            create_app(
                SETTINGS,
                driver_factory=lambda: store,
                explanation_dispatcher_factory=factory,
            )
        ):
            assert entered.wait(1)
            started = clock.monotonic()
        assert clock.monotonic() - started < 1
        assert not store.closed.is_set()
    finally:
        release.set()
    assert model_closed.wait(1) and store.closed.wait(1)


def test_dispatcher_retries_after_a_failed_pass_without_logging_exception_text(caplog):
    visited, succeeded = Event(), Event()

    def inventory(_):
        if not visited.is_set():
            visited.set()
            raise RuntimeError("synthetic private provider content")
        succeeded.set()
        return ()

    dispatcher = ExplanationDispatcher(object(), inventory=inventory, interval=0.01)
    dispatcher.start()
    try:
        assert succeeded.wait(1)
    finally:
        dispatcher.stop()
    assert "synthetic private provider content" not in caplog.text
    assert dispatcher.status == "stopped"


def test_dispatcher_start_failure_closes_the_owned_driver():
    store = OwnedDriver()

    class RefusedDispatcher:
        def start(self, **kwargs):
            raise RuntimeError("synthetic start refusal")

    app = create_app(
        SETTINGS,
        driver_factory=lambda: store,
        explanation_dispatcher_factory=lambda _: RefusedDispatcher(),
    )
    with pytest.raises(RuntimeError, match="synthetic start refusal"), TestClient(app):
        pass
    assert store.closed.is_set()


def test_driver_close_error_does_not_escape_the_dispatcher(capsys):
    class RefusedClose:
        def close(self):
            raise RuntimeError("synthetic private store content")

    dispatcher = ExplanationDispatcher(RefusedClose(), inventory=lambda _: ())
    dispatcher.start(close_driver=True)
    dispatcher.stop()
    assert dispatcher.status == "unavailable"
    assert "synthetic private store content" not in capsys.readouterr().err
