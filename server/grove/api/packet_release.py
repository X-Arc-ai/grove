"""Response-lifetime ownership for D14 leases, without a privacy implementation.

A synchronous read adapter enters the owner's lease with retain_packet_lease(). The ASGI
middleware keeps it until the complete buffered body is released, including MCP serialization.
This module does not resolve policy, read a ledger, acquire a file lock, or filter content.
"""

import threading
from contextlib import AbstractContextManager, ExitStack, nullcontext
from contextvars import ContextVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from grove.api.errors import ApiError, unavailable
from grove.reads.runtime import (
    REQUEST_SECONDS,
    Deadline,
    ResponseBudget,
    deadline,
    response_budget,
)


class _ReleaseScope:
    def __init__(self, budget: ResponseBudget | None = None) -> None:
        self._lock = threading.Lock()
        self._stack = ExitStack()
        self._closed = False
        self._sent = False
        self._budget = budget
        self.active = budget is None

    def activate(self, *, seconds: float) -> Deadline:
        with self._lock:
            if self._closed or self._sent or self._budget is None:
                raise unavailable("the packet response cannot be activated")
            current = self._budget.activate(seconds=seconds)
            self.active = True
            return current

    def require(self) -> Deadline:
        with self._lock:
            if self._closed or not self.active:
                raise unavailable("the packet response is not active")
        return deadline()

    def sent(self) -> None:
        with self._lock:
            self._sent = True

    def retain[T](self, manager: AbstractContextManager[T]) -> T:
        if self._budget is not None:
            self.require()
        # Do not hold the process mutex while the owner's bounded lease acquisition waits.
        lease = manager.__enter__()
        try:
            if self._budget is not None:
                self.require()
        except BaseException:
            manager.__exit__(None, None, None)
            raise
        with self._lock:
            if not self._closed:
                self._stack.push(manager)
                return lease
        manager.__exit__(None, None, None)
        raise unavailable("the packet response is no longer active")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            stack = self._stack.pop_all()
        stack.close()


_RELEASE_SCOPE: ContextVar[_ReleaseScope | None] = ContextVar("packet_release_scope", default=None)


async def activate_packet_response(*, seconds: float = REQUEST_SECONDS) -> Deadline:
    """Activate a read response on the owning event loop, before any response output.

    Use with require_activation=True. Repeated calls only tighten the original deadline.
    The transport must preflight the entire batch before this call or any handler execution.
    This does not validate a credential, policy, or the batch's admissibility.
    """
    current = _RELEASE_SCOPE.get()
    if current is None:
        raise unavailable("the packet release guard is not configured")
    return current.activate(seconds=seconds)


def require_packet_response() -> Deadline:
    """Require the active guard and deadline, including from a copied AnyIO worker context."""
    current = _RELEASE_SCOPE.get()
    if current is None:
        raise unavailable("the packet release guard is not configured")
    return current.require()


def retain_packet_lease[T](manager: AbstractContextManager[T]) -> T:
    """Enter one owner's lease and transfer its release to the active response boundary.

    Call this before any private capture or cache lookup. A handler without the middleware
    fails closed before it enters the lease. The middleware context reaches threadpool workers.
    """
    current = _RELEASE_SCOPE.get()
    if current is None:
        raise unavailable("the packet release guard is not configured")
    return current.retain(manager)


class PacketReleaseMiddleware:
    """Buffer selected read responses and retain leases through the final ASGI body send.

    The caller supplies exact paths. Do not enable streaming or background packet generation
    through this adapter. max_body_bytes is a refusal bound, never a truncation limit.
    Register this outside any middleware that can split a buffered response into chunks.

    Shared paths use require_activation=True. They pass through unchanged until an async route
    calls activate_packet_response before invoking any read. The route owns batch preflight and
    the lifetime of callback tasks; this wrapper owns the shared deadline and retained leases.
    Deadline checks occur before ASGI send calls. Cancellation must remain effective inside the
    transport; this wrapper cannot retract bytes after an irrevocable transport handoff.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: frozenset[str],
        max_body_bytes: int = 1_048_576,
        require_activation: bool = False,
    ) -> None:
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("the packet response bound must be positive")
        self.app = app
        self.paths = paths
        self.max_body_bytes = max_body_bytes
        self.require_activation = require_activation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        owned: _ReleaseScope | None = None
        token = None
        start: Message | None = None
        chunks: list[bytes] = []
        size = 0
        complete = False
        released = False

        async def buffered(message: Message) -> None:
            nonlocal start, size, complete, released
            assert owned is not None
            if not owned.active:
                # A shared route also serves writes/protocol calls. Preserve their streaming,
                # size and duration behavior until the route explicitly selects a read body.
                owned.sent()
                released = True
                await send(message)
                return
            if self.require_activation:
                owned.require()
            if message["type"] == "http.response.start":
                if start is not None or complete:
                    raise unavailable("the packet response protocol is invalid")
                start = dict(message)
            elif message["type"] == "http.response.body":
                if start is None or complete:
                    raise unavailable("the packet response protocol is invalid")
                body = message.get("body", b"")
                size += len(body)
                if size > self.max_body_bytes:
                    raise unavailable("the packet exceeds the response bound")
                chunks.append(body)
                complete = not message.get("more_body", False)
            else:
                raise unavailable("the packet response protocol is unsupported")

        try:
            manager = response_budget() if self.require_activation else nullcontext(None)
            async with manager as budget:
                owned = _ReleaseScope(budget)
                token = _RELEASE_SCOPE.set(owned)
                await self.app(scope, receive, buffered)
                if not owned.active:
                    return
                if start is None or not complete:
                    raise unavailable("the packet response did not complete")
                body = b"".join(chunks)
                if self.require_activation:
                    owned.require()
                # Release no partial packet: all response production and serialization succeeded.
                released = True
                owned.sent()
                await send(start)
                if self.require_activation:
                    owned.require()
                await send({"type": "http.response.body", "body": body, "more_body": False})
        except ApiError as error:
            if released or owned is None or not owned.active:
                raise
            response = JSONResponse(
                error.body(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
            await response(scope, receive, send)
        finally:
            if token is not None:
                _RELEASE_SCOPE.reset(token)
            if owned is not None:
                owned.close()
