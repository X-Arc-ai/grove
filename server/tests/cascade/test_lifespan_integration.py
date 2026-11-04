"""Both background services share a driver until connector polling stops."""

from threading import Event

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.cascade import ExplanationDispatcher
from grove.connectors import poll
from tests.cascade.test_api import SETTINGS


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("poll_failure", [False, True])
def test_poll_stops_before_dispatcher_releases_shared_driver(monkeypatch, injected, poll_failure):
    events = []
    poll_started, dispatcher_started = Event(), Event()

    class Store:
        def close(self):
            events.append("close")

    store = Store()

    async def run_poll(driver, settings, deps, stop):
        assert driver is store
        poll_started.set()
        await stop.wait()
        assert "close" not in events
        events.append("poll_stop")
        if poll_failure:
            raise RuntimeError("synthetic poll stop failure")

    def inventory(driver):
        assert driver is store
        dispatcher_started.set()
        return ()

    dispatcher = ExplanationDispatcher(store, inventory=inventory)
    monkeypatch.setenv("GROVE_CONNECTOR_POLL", "1")
    monkeypatch.setattr(poll, "run_poll", run_poll)
    app = create_app(
        SETTINGS,
        driver=store if injected else None,
        driver_factory=lambda: store,
        explanation_dispatcher_factory=lambda _: dispatcher,
    )

    def lifecycle():
        with TestClient(app):
            assert poll_started.wait(1)
            assert dispatcher_started.wait(1)
            assert events == []

    if poll_failure:
        with pytest.raises(RuntimeError, match="synthetic poll stop failure"):
            lifecycle()
    else:
        lifecycle()
    assert dispatcher.status == "stopped"
    assert events == (["poll_stop"] if injected else ["poll_stop", "close"])
