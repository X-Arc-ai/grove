"""The JSON-RPC side of the MCP streamable HTTP transport: one POST body in, one answer out.

`grove.api.mcp` checks the bearer token, the Origin, the method, the content type and the size, and
parses the body. This module answers the parsed body for one caller, whose `ToolContext` holds the
token's world only: a single message or a batch, the protocol version, `initialize`, `ping`,
`tools/list`, `tools/call` and the empty `resources/list` and `prompts/list`.

A message with no `id` (a notification) and a client's response produce no output, and a body
that yields no output is answered 202 with no body. A tool the token holds no scope for refuses the
whole body before any tool runs (`ScopeDenied`), so a batch never half runs.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from grove.api.errors import INVALID_ARGUMENTS, ApiError, store_unavailable
from grove.mcp import tools as registry
from grove.mcp.tools import Tool, ToolContext
from grove.store.errors import STORE_UNAVAILABLE
from grove.strict_json import loads_strict

__all__ = [
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "LATEST_VERSION",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "SERVER_INFO",
    "SUPPORTED_VERSIONS",
    "Answer",
    "ParseFailed",
    "Preflight",
    "ScopeDenied",
    "answer",
    "error_message",
    "index_tools",
    "parse_body",
    "preflight",
]

# Newest first: initialize answers the newest when the client's version is not here.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]
SERVER_INFO = {"name": "grove", "version": "0.1.0"}
JSONRPC = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_NO_OUTPUT = object()


class ParseFailed(ValueError):
    """The body is not one JSON value."""


class ScopeDenied(Exception):
    """The token holds no scope for a tool the body calls. Nothing ran."""

    def __init__(self, tool: Tool) -> None:
        super().__init__(f"the token lacks {tool.scope}")
        self.tool = tool.name
        self.scope = tool.scope


@dataclass(frozen=True)
class Answer:
    """What the route sends: an HTTP status and a JSON body, or no body at all (202).

    session_id is a new Mcp-Session-Id when the body held an initialize request: random, not
    stored, and not required on later requests.
    """

    status: int
    body: Any = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class Preflight:
    """Whether the checked body needs a general recall packet response.

    Callers must use the same payload, selected Tool objects and context for dispatch.
    This result neither activates a packet response nor executes any handler.
    """

    has_executable_recall: bool


@dataclass(frozen=True)
class _Request:
    id: str | int
    method: str
    params: dict[str, Any] | None
    params_valid: bool


@dataclass(frozen=True)
class _Invalid:
    id: str | int | None
    message: str


def parse_body(raw: bytes) -> Any:
    """The body as one JSON value. Raises ParseFailed for what `loads_strict` refuses: anything
    that is not strict UTF-8 JSON, or that JSON could not carry back in the answer. So no handler
    runs on a value that would make the answer fail."""
    try:
        return loads_strict(raw)
    except ValueError:
        raise ParseFailed("the body is not valid JSON") from None


def error_message(message_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id: str | int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": message_id, "result": result}


def index_tools(tools: Sequence[Tool]) -> dict[str, Tool]:
    """The tools by name. Raises ValueError when two tools share a name."""
    index: dict[str, Tool] = {}
    for tool in tools:
        if tool.name in index:
            raise ValueError(f"two MCP tools have the name {tool.name!r}")
        index[tool.name] = tool
    return index


def _valid_id(value: object) -> bool:
    # MCP: a string or an integer, never null. bool is an int in Python, and not an id.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _classify(message: object) -> _Request | _Invalid | object:
    """A request, an invalid message, or _NO_OUTPUT for a notification or a client's response."""
    if not isinstance(message, dict):
        return _Invalid(None, "a message must be a JSON object")
    message_id = message.get("id")
    known_id = message_id if _valid_id(message_id) else None
    if message.get("jsonrpc") != JSONRPC:
        return _Invalid(known_id, 'a message must have "jsonrpc": "2.0"')
    if "method" not in message:
        is_response = ("result" in message) != ("error" in message)
        if is_response and "id" in message:
            return _NO_OUTPUT
        return _Invalid(known_id, "a message must have a method, or be a response")
    if not isinstance(message["method"], str):
        return _Invalid(known_id, "method must be a string")
    if "id" not in message:
        return _NO_OUTPUT
    if known_id is None:
        return _Invalid(None, "id must be a string or an integer")
    params = message.get("params")
    if params is None and "params" not in message:
        return _Request(known_id, message["method"], None, True)
    return _Request(known_id, message["method"], params, isinstance(params, dict))


def _call_target(request: _Request, index: Mapping[str, Tool]) -> Tool | None:
    """The known tool a tools/call request names, else None (the call answers -32602)."""
    if request.method != "tools/call" or not request.params_valid or request.params is None:
        return None
    name = request.params.get("name")
    return index.get(name) if isinstance(name, str) else None


def _check_scopes(
    requests: Sequence[_Request], index: Mapping[str, Tool], context: ToolContext
) -> None:
    for request in requests:
        tool = _call_target(request, index)
        if tool is not None and tool.scope not in context.scopes:
            raise ScopeDenied(tool)


def _preflight_requests(
    requests: Sequence[_Request],
    index: Mapping[str, Tool],
    context: ToolContext,
    *,
    batch: bool,
) -> Preflight:
    # Keep scope refusal ahead of mixed-batch refusal, including known targets whose
    # arguments cannot execute. The whole body must pass before any handler starts.
    _check_scopes(requests, index, context)
    has_recall = False
    has_write = False
    for request in requests:
        tool = _call_target(request, index)
        if tool is None or not isinstance((request.params or {}).get("arguments", {}), dict):
            continue
        has_recall |= tool.name == "recall"
        has_write |= tool.scope in {"grove.write", "grove.sensitive_write"}
    if batch and has_recall and has_write:
        raise ApiError(
            400,
            INVALID_ARGUMENTS,
            "recall and write tools cannot share a batch",
            details={"rule": "mixed_read_write_batch"},
        )
    return Preflight(has_executable_recall=has_recall)


def preflight(payload: Any, tools: Sequence[Tool], context: ToolContext) -> Preflight:
    """Pure whole-body scope and mixed-batch checks before packet activation or dispatch.

    Raises ScopeDenied first for any resolved target without scope, then ApiError for a
    batch with executable recall and a write-scoped call. Only registered requests with
    object arguments execute; omitted arguments mean {}. Tool-specific fields are left
    to handlers. Malformed messages, notifications and client responses retain their
    normal treatment in answer. No input or store state is changed here.
    """
    index = index_tools(tools)
    batch = isinstance(payload, list)
    classified = [_classify(message) for message in (payload if batch else [payload])]
    requests = [item for item in classified if isinstance(item, _Request)]
    return _preflight_requests(requests, index, context, batch=batch)


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocolVersion")
    version = asked if isinstance(asked, str) and asked in SUPPORTED_VERSIONS else LATEST_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(SERVER_INFO),
        "instructions": registry.INSTRUCTIONS,
    }


def _describe(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations,
    }


def _tool_result(structured: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    text = json.dumps(structured, ensure_ascii=False, allow_nan=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _call_tool(
    request: _Request, index: Mapping[str, Tool], context: ToolContext
) -> dict[str, Any]:
    params = request.params or {}
    name = params.get("name")
    tool = index.get(name) if isinstance(name, str) else None
    if tool is None:
        return error_message(request.id, INVALID_PARAMS, "no tool has that name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return error_message(request.id, INVALID_PARAMS, "arguments must be an object")
    if tool.scope not in context.scopes:
        # _check_scopes refuses the body first; this keeps a call from ever running without it.
        raise ScopeDenied(tool)
    try:
        structured = tool.handler(context, arguments)
    except ApiError as refused:
        return _result(request.id, _tool_result(refused.body(), is_error=True))
    except STORE_UNAVAILABLE:
        return _result(request.id, _tool_result(store_unavailable().body(), is_error=True))
    if not isinstance(structured, dict):
        raise TypeError(f"the tool {tool.name!r} returned no object")
    return _result(request.id, _tool_result(structured, is_error=False))


def _respond(request: _Request, index: Mapping[str, Tool], context: ToolContext) -> dict[str, Any]:
    if not request.params_valid:
        return error_message(request.id, INVALID_PARAMS, "params must be an object")
    params = request.params or {}
    match request.method:
        case "initialize":
            return _result(request.id, _initialize(params))
        case "ping":
            return _result(request.id, {})
        case "tools/list":
            return _result(request.id, {"tools": [_describe(tool) for tool in index.values()]})
        case "tools/call":
            return _call_tool(request, index, context)
        case "resources/list":
            return _result(request.id, {"resources": []})
        case "prompts/list":
            return _result(request.id, {"prompts": []})
    return error_message(request.id, METHOD_NOT_FOUND, "no such method")


def answer(payload: Any, tools: Sequence[Tool], context: ToolContext) -> Answer:
    """The answer to one parsed POST body. Raises ScopeDenied before any tool runs when a call
    names a tool whose scope the token does not hold. A tool handler gets `context` (the token's
    world only) and the call's arguments. Mixed recall/write batches return a whole-body
    typed HTTP 400 before any handler, including protocol handlers, executes."""
    index = index_tools(tools)
    batch = isinstance(payload, list)
    if batch and not payload:
        return Answer(400, error_message(None, INVALID_REQUEST, "a batch must not be empty"))
    classified = [_classify(message) for message in (payload if batch else [payload])]
    if not batch and isinstance(classified[0], _Invalid):
        refused = classified[0]
        return Answer(400, error_message(refused.id, INVALID_REQUEST, refused.message))
    requests = [item for item in classified if isinstance(item, _Request)]
    try:
        _preflight_requests(requests, index, context, batch=batch)
    except ApiError as refused:
        return Answer(refused.status, refused.body())
    out: list[dict[str, Any]] = []
    session_id = None
    for item in classified:
        if isinstance(item, _Invalid):
            out.append(error_message(item.id, INVALID_REQUEST, item.message))
        elif isinstance(item, _Request):
            out.append(_respond(item, index, context))
            if item.method == "initialize" and item.params_valid:
                session_id = secrets.token_hex(16)
    if not out:
        return Answer(202)
    return Answer(200, out if batch else out[0], session_id)
