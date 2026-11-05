"""What a tool is and what its handler gets. It imports nothing from Grove, so `grove.mcp.tools`
can build the default set from handler modules that need these two types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["Tool", "ToolContext"]


@dataclass(frozen=True)
class ToolContext:
    """The caller of one tool call: the token's world, person, host, scopes and grant."""

    driver: Any
    world_id: str
    user_id: str
    client_id: str
    scopes: frozenset[str]
    grant_id: str


@dataclass(frozen=True)
class Tool:
    """One MCP tool. `scope` is the scope a call needs: grove.read, grove.write or
    grove.sensitive_write. The handler returns the structured result, or raises ApiError."""

    name: str
    title: str
    description: str
    input_schema: dict
    annotations: dict
    scope: str
    handler: Callable[[ToolContext, dict], dict]
