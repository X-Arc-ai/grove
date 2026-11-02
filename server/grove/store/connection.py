"""The one place that opens a driver."""

from typing import Any

from neo4j import Driver, GraphDatabase

from grove.config import Settings, load_settings

# Code outside the store names the driver type through this module, never through neo4j.
__all__ = ["DATABASE", "Driver", "open_driver"]

# Community has exactly one user database.
DATABASE = "neo4j"


def open_driver(settings: Settings | None = None, **config: Any) -> Driver:
    """`config` passes driver options, such as the API's short connection timeouts."""
    settings = settings or load_settings()
    return GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth, **config)
