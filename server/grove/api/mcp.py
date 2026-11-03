"""The MCP streamable HTTP route at /mcp, and at /mcp/ with the same handler. docs/hosts.md.

Every method checks the bearer token first: `Authorization: Bearer <token>`, a live access token
(`grove.oauth.store.find_access_token` on its sha256 hex) whose resource is this server's
`<base>/mcp`. A request with no bearer token gets 401 with the resource metadata URL and the
default scopes in `WWW-Authenticate` (RFC 9728, A.3); a token that is not live gets 401 with
`error="invalid_token"`. Then an `Origin` header, when present, must be this server's origin.

GET offers no event stream (405), DELETE ends a session that was never stored (204), and POST
carries JSON-RPC, which `grove.mcp.transport` answers with the tools of `app.state.mcp_tools`, read
at request time. A tool runs with the token's world only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.config import ApiSettings
from grove.api.errors import INVALID_ARGUMENTS, ApiError, invalid_arguments, unauthenticated
from grove.mcp.tools import ToolContext
from grove.mcp.transport import (
    INVALID_REQUEST,
    PARSE_ERROR,
    SUPPORTED_VERSIONS,
    ParseFailed,
    ScopeDenied,
    answer,
    error_message,
    parse_body,
)
from grove.oauth.secrets import token_hash
from grove.oauth.store import TokenRow, find_access_token
from grove.store.connection import Driver

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024
# Our tokens are 47 characters. A longer value is refused with no store read.
MAX_TOKEN_LENGTH = 512
JSON_TYPE = "application/json"
# What a client with no token is told to ask for: grove.sensitive_write is granted only by name.
CHALLENGE_SCOPE = "grove.read grove.write"
ALLOWED_METHODS = "POST, DELETE"
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "mcp-protocol-version"

FORBIDDEN_ORIGIN = "forbidden_origin"
SCOPE_DENIED = "scope_denied"

_NO_STORE = {"Cache-Control": "no-store"}
# The methods a client may send; each one meets the bearer check first.
_ROUTE_METHODS = ["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"]

router = APIRouter()


def _settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def _driver(request: Request) -> Driver:
    return request.app.state.driver


def _typed(error: ApiError, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status, headers=_NO_STORE | (headers or {}))


def _rpc_refusal(code: int, message: str) -> JSONResponse:
    return JSONResponse(error_message(None, code, message), status_code=400, headers=_NO_STORE)


def missing_token_challenge(settings: ApiSettings) -> str:
    return f'Bearer resource_metadata="{settings.resource_metadata_url}", scope="{CHALLENGE_SCOPE}"'


def invalid_token_challenge(settings: ApiSettings) -> str:
    return f'Bearer error="invalid_token", resource_metadata="{settings.resource_metadata_url}"'


def insufficient_scope_challenge(settings: ApiSettings, scope: str) -> str:
    return (
        f'Bearer error="insufficient_scope", scope="{scope}", '
        f'resource_metadata="{settings.resource_metadata_url}"'
    )


def bearer_token(header: str | None) -> str | None:
    """The token of a Bearer header; "" for a Bearer header with no usable token; None when the
    request has no Bearer header at all (no header, or another scheme)."""
    if header is None:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    if not token or len(token) > MAX_TOKEN_LENGTH or any(char.isspace() for char in token):
        return ""
    return token


def _live_token(driver: Driver, token: str) -> TokenRow | None:
    return find_access_token(driver, token_hash(token), clock.now())


async def _authenticate(request: Request) -> TokenRow | JSONResponse:
    """The live token of this request, else the 401 to send."""
    settings = _settings(request)
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        challenge = missing_token_challenge(settings)
        return _typed(
            unauthenticated("an access token is required"), {"WWW-Authenticate": challenge}
        )
    row = await run_in_threadpool(_live_token, _driver(request), token) if token else None
    if row is None or row.resource != settings.mcp_url:
        challenge = invalid_token_challenge(settings)
        return _typed(
            unauthenticated("the access token is not valid"), {"WWW-Authenticate": challenge}
        )
    return row


def _origin_refused(request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin is None or origin == _settings(request).public_base_url:
        return None
    refused = ApiError(
        403, FORBIDDEN_ORIGIN, "the Origin header names another site", details={"field": "origin"}
    )
    return _typed(refused)


def _content_type_refused(request: Request) -> JSONResponse | None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == JSON_TYPE:
        return None
    refused = ApiError(
        415,
        INVALID_ARGUMENTS,
        "the body must be application/json",
        details={"field": "content-type"},
    )
    return _typed(refused)


async def _capped_body(request: Request) -> bytes | None:
    """The body, or None when it is over MAX_BODY_BYTES."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _context(request: Request, token: TokenRow) -> ToolContext:
    return ToolContext(
        driver=_driver(request),
        world_id=token.world_id,
        user_id=token.user_id,
        client_id=token.client_id,
        scopes=frozenset(token.scopes),
        grant_id=token.grant_id,
    )


async def _post(request: Request, token: TokenRow) -> Response:
    refused = _content_type_refused(request)
    if refused is not None:
        return refused
    version = request.headers.get(VERSION_HEADER)
    if version is not None and version not in SUPPORTED_VERSIONS:
        return _rpc_refusal(INVALID_REQUEST, "the MCP-Protocol-Version is not supported")
    raw = await _capped_body(request)
    if raw is None:
        return _typed(invalid_arguments("the body is too large", "body"))
    try:
        payload: Any = parse_body(raw)
    except ParseFailed:
        return _rpc_refusal(PARSE_ERROR, "the body is not valid JSON")
    tools = request.app.state.mcp_tools
    try:
        answered = await run_in_threadpool(answer, payload, tools, _context(request, token))
    except ScopeDenied as denied:
        refused_scope = ApiError(
            403,
            SCOPE_DENIED,
            "the access token does not hold the scope this tool needs",
            details={"tool": denied.tool, "scope": denied.scope},
        )
        challenge = insufficient_scope_challenge(_settings(request), denied.scope)
        return _typed(refused_scope, {"WWW-Authenticate": challenge})
    if answered.body is None:
        return Response(status_code=answered.status, headers=_NO_STORE)
    headers = dict(_NO_STORE)
    if answered.session_id is not None:
        headers[SESSION_HEADER] = answered.session_id
    return JSONResponse(answered.body, status_code=answered.status, headers=headers)


@router.api_route(MCP_PATH, methods=_ROUTE_METHODS, include_in_schema=False)
@router.api_route(f"{MCP_PATH}/", methods=_ROUTE_METHODS, include_in_schema=False)
async def mcp(request: Request) -> Response:
    token = await _authenticate(request)
    if isinstance(token, JSONResponse):
        return token
    refused = _origin_refused(request)
    if refused is not None:
        return refused
    if request.method == "POST":
        return await _post(request, token)
    if request.method == "DELETE":
        return Response(status_code=204, headers=_NO_STORE)
    no_stream = ApiError(
        405,
        INVALID_ARGUMENTS,
        "this server offers no event stream: send JSON-RPC with POST",
        details={"field": "method"},
    )
    return _typed(no_stream, {"Allow": ALLOWED_METHODS})
