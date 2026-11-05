from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.config import Settings
from grove.oauth.store import TokenRow
from grove.reads.runtime import ReadRuntime
from grove.store.connection import open_driver, open_read_driver

SETTINGS = ApiSettings(public_base_url="https://grove.example")


def test_production_lifespan_owns_both_pools_and_closes_them(monkeypatch):
    sync, asynchronous = Mock(), AsyncMock()
    monkeypatch.setattr("grove.api.app.open_read_driver", lambda: asynchronous)
    app = create_app(SETTINGS, driver_factory=lambda: sync)
    with TestClient(app):
        assert app.state.driver is sync
        assert app.state.read_runtime.driver is asynchronous
        sync.close.assert_not_called()
        asynchronous.close.assert_not_awaited()
    sync.close.assert_called_once()
    asynchronous.close.assert_awaited_once()


def test_async_start_failure_still_closes_owned_sync_driver(monkeypatch):
    sync = Mock()

    def refuse():
        raise ValueError("invalid read pool configuration")

    monkeypatch.setattr("grove.api.app.open_read_driver", refuse)
    with pytest.raises(ValueError), TestClient(create_app(SETTINGS, driver_factory=lambda: sync)):
        pass
    sync.close.assert_called_once()


def test_injected_dependencies_remain_caller_owned_and_do_not_open_real_pool(monkeypatch):
    sync, asynchronous = Mock(), AsyncMock()
    factory = Mock(side_effect=AssertionError("must not open a real driver"))
    monkeypatch.setattr("grove.api.app.open_read_driver", factory)
    runtime = ReadRuntime(asynchronous)
    with TestClient(create_app(SETTINGS, driver=sync, read_runtime=runtime)) as client:
        assert client.app.state.read_runtime is runtime
    sync.close.assert_not_called()
    asynchronous.close.assert_not_awaited()
    factory.assert_not_called()


def test_async_read_factory_does_not_change_sync_factory_configuration(monkeypatch):
    sync_factory, async_factory = Mock(), Mock()
    monkeypatch.setattr("grove.store.connection.GraphDatabase.driver", sync_factory)
    monkeypatch.setattr("grove.store.connection.AsyncGraphDatabase.driver", async_factory)
    settings = Settings("bolt://localhost:7687", None, None, "development")
    open_driver(settings, connection_timeout=5.0, connection_acquisition_timeout=10.0)
    open_read_driver(settings)
    assert sync_factory.call_args.kwargs == {
        "auth": None,
        "connection_timeout": 5.0,
        "connection_acquisition_timeout": 10.0,
    }
    assert async_factory.call_args.kwargs == {
        "auth": None,
        "connection_timeout": 2.0,
        "connection_acquisition_timeout": 2.0,
        "max_transaction_retry_time": 0.0,
        "max_connection_pool_size": 2,
    }


@pytest.mark.parametrize("poll_failure", [False, True])
@pytest.mark.parametrize("read_close_failure", [False, True])
def test_owned_runtime_cleanup_preserves_poll_and_dispatcher_order(
    monkeypatch, poll_failure, read_close_failure
):
    events = []
    sync = Mock()
    sync.close.side_effect = lambda: events.append("sync_close")

    async def close_read():
        events.append("read_close")
        if read_close_failure:
            raise ValueError("synthetic read close failure")

    asynchronous = AsyncMock()
    asynchronous.close.side_effect = close_read
    monkeypatch.setattr("grove.api.app.open_read_driver", lambda: asynchronous)

    async def run_poll(driver, settings, deps, stop):
        assert driver is sync
        events.append("poll_start")
        await stop.wait()
        events.append("poll_stop")
        if poll_failure:
            raise RuntimeError("synthetic poll failure")

    class Dispatcher:
        def start(self, *, close_driver):
            assert close_driver is True
            events.append("dispatcher_start")

        def stop(self):
            events.append("dispatcher_stop")
            sync.close()

    monkeypatch.setenv("GROVE_CONNECTOR_POLL", "1")
    monkeypatch.setattr("grove.connectors.poll.run_poll", run_poll)
    app = create_app(
        SETTINGS,
        driver_factory=lambda: sync,
        explanation_dispatcher_factory=lambda _: Dispatcher(),
    )

    def lifecycle():
        with TestClient(app):
            assert app.state.read_runtime.driver is asynchronous
            sync.close.assert_not_called()

    if read_close_failure:
        with pytest.raises(ValueError, match="synthetic read close failure"):
            lifecycle()
    elif poll_failure:
        with pytest.raises(RuntimeError, match="synthetic poll failure"):
            lifecycle()
    else:
        lifecycle()
    assert events == [
        "dispatcher_start",
        "poll_start",
        "poll_stop",
        "read_close",
        "dispatcher_stop",
        "sync_close",
    ]
    sync.close.assert_called_once()
    asynchronous.close.assert_awaited_once()


def test_dispatcher_start_failure_closes_both_owned_pools(monkeypatch):
    sync, asynchronous = Mock(), AsyncMock()
    monkeypatch.setattr("grove.api.app.open_read_driver", lambda: asynchronous)
    dispatcher = Mock()
    dispatcher.start.side_effect = RuntimeError("synthetic dispatcher start failure")
    app = create_app(
        SETTINGS,
        driver_factory=lambda: sync,
        explanation_dispatcher_factory=lambda _: dispatcher,
    )
    with pytest.raises(RuntimeError, match="synthetic dispatcher start failure"), TestClient(app):
        pass
    dispatcher.stop.assert_not_called()
    sync.close.assert_called_once()
    asynchronous.close.assert_awaited_once()


def test_capture_runtime_does_not_activate_general_public_reads(monkeypatch):
    token = TokenRow(
        "token",
        "client",
        "user",
        "world",
        "grant",
        "family",
        ("grove.read",),
        SETTINGS.mcp_url,
        datetime(2026, 12, 1, tzinfo=UTC),
    )
    monkeypatch.setattr("grove.api.mcp._live_token", lambda *_: token)
    monkeypatch.setattr("grove.api.app.configuration", lambda: None)
    monkeypatch.setattr("grove.mcp.demo.require_general_access", lambda *_, **__: None)
    runtime = ReadRuntime(AsyncMock())
    app = create_app(SETTINGS, driver=object(), read_runtime=runtime, mcp_tools=())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer synthetic-test-token"}
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert listed.status_code == 200
        assert listed.json()["result"]["tools"] == []
        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"about": "Cedar"}},
            },
        )
        assert "error" in called.json()
        assert "result" not in called.json()
        for path in ("/v1/recall", "/v1/what_changed", "/v1/what_it_breaks"):
            assert client.post(path, json={}).status_code == 404
