"""The one place that opens a driver."""

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, Driver, GraphDatabase

from grove.config import Settings, load_settings

# Code outside the store names the driver type through this module, never through neo4j.
__all__ = ["DATABASE", "AsyncDriver", "Driver", "open_driver", "open_read_driver"]

# Community has exactly one user database.
DATABASE = "neo4j"


def open_driver(settings: Settings | None = None, **config: Any) -> Driver:
    """`config` passes driver options, such as the API's short connection timeouts."""
    settings = settings or load_settings()
    return GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth, **config)


def open_read_driver(settings: Settings | None = None) -> AsyncDriver:
    """D7 owns this separate pool; synchronous writers retain their original configuration."""
    settings = settings or load_settings()
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=settings.neo4j_auth,
        connection_timeout=2.0,
        connection_acquisition_timeout=2.0,
        max_transaction_retry_time=0.0,
        max_connection_pool_size=2,
    )
