"""Response-lifetime ownership for D14 leases, without a privacy implementation.

A synchronous read adapter enters the owner's lease with retain_packet_lease(). The ASGI
middleware keeps it until the complete buffered body is released, including MCP serialization.
This module does not resolve policy, read a ledger, acquire a file lock, or filter content.
"""

import threading
from contextlib import AbstractContextManager, ExitStack
from contextvars import ContextVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from grove.api.errors import ApiError, unavailable


class _ReleaseScope:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stack = ExitStack()
        self._closed = False

    def retain[T](self, manager: AbstractContextManager[T]) -> T:
        # Do not hold the process mutex while the owner's bounded lease acquisition waits.
        lease = manager.__enter__()
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
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: frozenset[str],
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("the packet response bound must be positive")
        self.app = app
        self.paths = paths
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        owned = _ReleaseScope()
        token = _RELEASE_SCOPE.set(owned)
        start: Message | None = None
        chunks: list[bytes] = []
        size = 0
        complete = False
        released = False

        async def buffered(message: Message) -> None:
            nonlocal start, size, complete
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
            await self.app(scope, receive, buffered)
            if start is None or not complete:
                raise unavailable("the packet response did not complete")
            body = b"".join(chunks)
            # Release no partial packet: all response production and serialization succeeded.
            released = True
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
        except ApiError as error:
            if released:
                raise
            response = JSONResponse(
                error.body(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
            await response(scope, receive, send)
        finally:
            _RELEASE_SCOPE.reset(token)
            owned.close()
