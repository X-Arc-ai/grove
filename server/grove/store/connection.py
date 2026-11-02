"""The one place that opens a driver."""

from neo4j import Driver, GraphDatabase

from grove.config import Settings, load_settings

# Code outside the store names the driver type through this module, never through neo4j.
__all__ = ["DATABASE", "Driver", "open_driver"]

# Community has exactly one user database.
DATABASE = "neo4j"


def open_driver(settings: Settings | None = None) -> Driver:
    settings = settings or load_settings()
    return GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
