"""What the store layer raises."""

from neo4j.exceptions import Neo4jError

__all__ = ["Neo4jError", "ScopeError", "WorldLeak"]


class ScopeError(ValueError):
    """Cypher the world scoping refuses. Nothing was sent to the store."""


class WorldLeak(RuntimeError):
    """A result held a row of another world. The message carries ids of elements, never values."""
