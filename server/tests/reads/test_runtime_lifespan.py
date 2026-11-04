from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.config import Settings
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
