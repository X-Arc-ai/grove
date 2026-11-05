"""Tool selection by account, and the restricted demo's final ASGI release under OAuth authority.

The designated account keeps its restricted pair and its release guard. Every other live token
leaves the guard before its first await on the body, and the route then checks it against the
store. A demo world never gets the general tools, with or without the demo configuration.
"""

import asyncio
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.errors import ApiError, unavailable
from grove.config import ConfigError
from grove.demo_guard import DemoConfiguration, ReleaseFile, configuration
from grove.mcp.access import demo_refusal, require_general_access, token_refusal
from grove.oauth.secrets import token_hash
from grove.oauth.store import TokenRow, find_access_token
from grove.store.errors import Neo4jError
from grove.store.query import PlatformQuery

MAX_RELEASE_BYTES = 1_048_576
BODY_TIMEOUT_SECONDS = 10.0
_AUTHORITY_KEY = "grove.demo.authority"


async def _request_body(receive) -> bytes | None:
    """Bound network intake before any authority lock can block a revoke."""
    from grove.api.mcp import MAX_BODY_BYTES

    parts, size = [], 0
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                if message["type"] != "http.request":
                    raise ApiError(400, "invalid_arguments", "the request body is invalid")
                part = message.get("body", b"")
                size += len(part)
                if size > MAX_BODY_BYTES:
                    raise ApiError(413, "invalid_arguments", "the request body is too large")
                parts.append(part)
                if not message.get("more_body", False):
                    return b"".join(parts)
    except TimeoutError:
        raise ApiError(
            408, "invalid_arguments", "the request body did not arrive in time"
        ) from None


def tools_for_request(request: Request, token: TokenRow):
    """The one shared route hook. No argument or client header can set this authority.

    Only the release guard sets the demo authority, after it proves the designation. Every other
    request meets the store check here, so a missing key never means an unrestricted world.
    """
    authority = request.scope.get(_AUTHORITY_KEY)
    if authority is not None:
        if authority != token:
            raise demo_refusal()
        return request.app.state.demo_tools
    require_general_access(
        request.app.state.driver,
        world_id=token.world_id,
        user_id=token.user_id,
        grant_id=token.grant_id,
        client_id=token.client_id,
    )
    return request.app.state.mcp_tools


def _authority(scope: dict, config: DemoConfiguration, hashed_token: str) -> TokenRow:
    from grove.demo_policy import DemoDenied, require_designation

    app = scope["app"]
    if configuration() != config:
        raise unavailable("the demo configuration changed")
    token = find_access_token(app.state.driver, hashed_token, clock.now())
    if token is None or token.resource != app.state.settings.mcp_url:
        raise token_refusal()
    try:
        require_designation(app.state.driver, token.world_id, token.user_id)
    except DemoDenied:
        raise demo_refusal() from None
    # The published token lookup checks grant/world/client/scopes. Also bind the owner and
    # require the real registered client. ToolContext alone does not retain these checks.
    rows = PlatformQuery(app.state.driver).read(
        "MATCH (g:OAuthGrant {id: $grant_id}), (c:OAuthClient {client_id: $client_id}) "
        "WHERE g.user_id = $user_id AND g.world_id = $world_id "
        "AND g.client_id = c.client_id AND g.revoked_at IS NULL RETURN g.id AS id",
        grant_id=token.grant_id,
        client_id=token.client_id,
        user_id=token.user_id,
        world_id=token.world_id,
    )
    if len(rows) != 1:
        raise token_refusal()
    return token


def _ordinary_token(scope: dict, raw_header: bytes) -> bool:
    from grove.api.mcp import bearer_token

    raw_token = bearer_token(raw_header.decode("latin-1"))
    if not raw_token:
        return False
    app = scope["app"]
    token = find_access_token(app.state.driver, token_hash(raw_token), clock.now())
    if token is None or token.resource != app.state.settings.mcp_url:
        return False
    require_general_access(
        app.state.driver,
        world_id=token.world_id,
        user_id=token.user_id,
        grant_id=token.grant_id,
        client_id=token.client_id,
    )
    return True


async def _is_ordinary(scope: dict) -> bool:
    """True only for one live token of an ordinary owner. Two Authorization headers stay guarded:
    this code and the route could read different ones. Any failure takes the guarded path, which
    reads the store again and answers as it always did."""
    headers = [value for key, value in scope.get("headers", ()) if key == b"authorization"]
    if len(headers) != 1:
        return False
    try:
        return await run_in_threadpool(_ordinary_token, scope, headers[0])
    except Exception:
        return False


class DemoReleaseMiddleware:
    """Own the guard until the worker ends and the complete response body is released.

    Native request cancellation abandons output, not the worker's ownership. A cancelled
    store may already have committed; its original idempotency key remains the retry key.
    """

    def __init__(self, app, config: DemoConfiguration | None) -> None:
        self.app = app
        self.config = config
        self.active: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send) -> None:
        if self.config is None or scope["type"] != "http" or scope["path"] not in ("/mcp", "/mcp/"):
            await self.app(scope, receive, send)
            return
        if await _is_ordinary(scope):
            # Outside the shielded task, so a cancelled read cannot outlive its response scope.
            await self.app(scope, receive, send)
            return
        abandoned = asyncio.Event()
        task = asyncio.create_task(self._guarded(scope, receive, send, abandoned))
        self.active.add(task)

        def finished(done):
            self.active.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned.set()
            raise

    async def _guarded(self, scope, receive, send, abandoned: asyncio.Event) -> None:
        from grove.api.mcp import bearer_token

        guard = None
        released = False
        try:
            body = await _request_body(receive)
            if body is None or abandoned.is_set():
                return
            replayed = False

            async def replay_receive():
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}

            guard = ReleaseFile(self.config)
            while not guard.try_shared():
                if abandoned.is_set():
                    return
                await asyncio.sleep(0.01)
            headers = dict(scope.get("headers", ()))
            raw_token = bearer_token(headers.get(b"authorization", b"").decode("latin-1"))
            # Preserve the ordinary transport's exact unauthenticated challenge.
            token = None
            if raw_token:
                token = await run_in_threadpool(
                    _authority, scope, self.config, token_hash(raw_token)
                )
                scope[_AUTHORITY_KEY] = token
            start: dict[str, Any] | None = None
            parts: list[bytes] = []
            size = 0
            complete = False

            async def capture(message):
                nonlocal start, size, complete
                if message["type"] == "http.response.start":
                    start = message
                elif message["type"] == "http.response.body":
                    part = message.get("body", b"")
                    size += len(part)
                    if size > MAX_RELEASE_BYTES:
                        raise unavailable("the demo response exceeds its limit")
                    parts.append(part)
                    complete = not message.get("more_body", False)

            await self.app(scope, replay_receive, capture)
            if abandoned.is_set():
                return
            if start is None or not complete:
                raise unavailable("the demo response is incomplete")
            guard.check()
            if token is not None:
                current = await run_in_threadpool(
                    _authority, scope, self.config, token_hash(raw_token)
                )
                if current != token:
                    raise ApiError(401, "unauthenticated", "the access token changed")
            if abandoned.is_set():
                return
            released = True
            await send(start)
            guard.check()
            if token is not None:
                current = await run_in_threadpool(
                    _authority, scope, self.config, token_hash(raw_token)
                )
                if current != token or abandoned.is_set():
                    raise ApiError(401, "unauthenticated", "the access token changed")
            await send({"type": "http.response.body", "body": b"".join(parts), "more_body": False})
        except (ApiError, ConfigError, Neo4jError, OSError) as error:
            if released:
                raise
            if not abandoned.is_set():
                refused = (
                    error if isinstance(error, ApiError) else unavailable("the demo is unavailable")
                )
                headers = {"Cache-Control": "no-store"}
                if refused.status == 401:
                    settings = scope["app"].state.settings
                    headers["WWW-Authenticate"] = (
                        'Bearer error="invalid_token", '
                        f'resource_metadata="{settings.resource_metadata_url}"'
                    )
                await JSONResponse(refused.body(), status_code=refused.status, headers=headers)(
                    scope, receive, send
                )
        finally:
            scope.pop(_AUTHORITY_KEY, None)
            if guard is not None:
                guard.close()
