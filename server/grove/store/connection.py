"""The one place that opens a driver."""

from neo4j import Driver, GraphDatabase

from grove.config import Settings, load_settings

# Community has exactly one user database.
DATABASE = "neo4j"


def open_driver(settings: Settings | None = None) -> Driver:
    settings = settings or load_settings()
    return GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth)
