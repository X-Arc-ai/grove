"""The accepted first recall capability and a bridge for the synchronous MCP transport.

This module defines no principal or policy. The API installs an invocation bound to the exact
server-authenticated ToolContext; the production service must bind the D14-owned principal and
perform the owner lease/filter protocol. Merely installing this bridge grants no disclosure.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import anyio

from grove.api.errors import ApiError, unavailable
from grove.api.packet_release import require_packet_response
from grove.mcp.tools import ToolContext
from grove.reads.requests import RecallQuery, parse_recall
from grove.reads.runtime import deadline
from grove.store.enums import ENUMS


def _unsupported(field: str, rule: str = "unsupported_recall_mode") -> ApiError:
    return ApiError(
        400,
        "invalid_arguments",
        "the current recall service does not support this request",
        details={"field": field, "rule": rule},
    )


def parse_current_recall(value: object) -> RecallQuery:
    """Reject unsupported capabilities explicitly; retain the complete D7 parser unchanged."""
    query = parse_recall(value)
    if query.about is None:
        raise _unsupported("about", "about_required")
    if query.time.mode != "current" or ("time" in value and value["time"] != {"mode": "current"}):
        raise _unsupported("time")
    if query.areas:
        raise _unsupported("areas")
    if set(query.include) - {"evidence"}:
        raise _unsupported("include")
    return query


def current_recall_schema() -> dict:
    """Fresh schema for the real handler; hosts-b2 owns registration and tool descriptions."""
    strings = {
        "type": "array",
        "minItems": 1,
        "maxItems": 100,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 1024},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["about"],
        "properties": {
            "about": {"type": "string", "minLength": 1, "maxLength": 1024},
            "kinds": strings,
            "source_types": {
                **strings,
                "items": {"type": "string", "enum": list(ENUMS["source_type"])},
            },
            "hops": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meanings": {**strings, "items": dict(strings["items"])},
                    "direction": {"type": "string", "enum": ["out", "in", "both"]},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                },
            },
            "time": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mode"],
                "properties": {"mode": {"type": "string", "const": "current"}},
            },
            "include": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["evidence"]},
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
        },
    }


@dataclass
class _Invocation:
    context: ToolContext
    invoke: Callable[[RecallQuery], Awaitable[dict]]
    loop: asyncio.AbstractEventLoop
    active: bool = True
    tasks: set[asyncio.Task] = field(default_factory=set)


_INVOCATION: ContextVar[_Invocation | None] = ContextVar("mcp_recall_invocation", default=None)


@asynccontextmanager
async def bind_recall(
    context: ToolContext, invoke: Callable[[RecallQuery], Awaitable[dict]]
) -> AsyncIterator[None]:
    """Bind on the owning event loop before Starlette enters its AnyIO worker.

    The API must activate the response guard first. Its invocation closes over the owner's
    principal and service; neither can come from MCP arguments. The guard, not this binding,
    owns the deadline and lease through final serialization/send.
    """
    require_packet_response()
    if _INVOCATION.get() is not None:
        raise unavailable("the recall invocation is already bound")
    bound = _Invocation(context, invoke, asyncio.get_running_loop())
    token = _INVOCATION.set(bound)
    try:
        yield
    finally:
        bound.active = False
        _INVOCATION.reset(token)
        # AnyIO's worker bridge does not propagate a native asyncio request cancellation to
        # its separate callback task. Cancel it explicitly and let native query cleanup finish
        # before the outer response scope releases the owner's lease.
        tasks = tuple(bound.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def recall_handler(context: ToolContext, arguments: dict) -> dict:
    """Return to the application loop from the actual synchronous MCP worker."""
    bound = _INVOCATION.get()
    if bound is None or not bound.active or context is not bound.context:
        raise unavailable("the recall invocation is not configured")
    if "grove.read" not in context.scopes:
        raise ApiError(403, "scope_denied", "the recall operation requires read scope")
    require_packet_response()
    query = parse_current_recall(arguments)
    deadline()

    async def invoke() -> dict:
        if not bound.active or asyncio.get_running_loop() is not bound.loop:
            raise unavailable("the recall invocation is no longer active")
        deadline()
        task = asyncio.current_task()
        bound.tasks.add(task)
        try:
            result = await bound.invoke(query)
            deadline()
            if not bound.active or not isinstance(result, dict):
                raise unavailable("the recall operation did not return a packet")
            return result
        finally:
            bound.tasks.discard(task)

    try:
        return anyio.from_thread.run(invoke)
    except RuntimeError:
        # A direct call outside an AnyIO worker must not create a new event loop. Runtime
        # exception text can contain private driver details, so the public refusal is fixed.
        raise unavailable("the recall worker bridge is unavailable") from None
