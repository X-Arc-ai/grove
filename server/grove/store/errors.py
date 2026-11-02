"""What the store layer raises."""

from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

__all__ = ["STORE_UNAVAILABLE", "Neo4jError", "ScopeError", "WorldLeak"]

# The store cannot be reached right now; a caller may try again.
STORE_UNAVAILABLE = (ServiceUnavailable, SessionExpired)


class ScopeError(ValueError):
    """Cypher the world scoping refuses. Nothing was sent to the store."""


class WorldLeak(RuntimeError):
    """A result held a row of another world. The message carries ids of elements, never values."""
