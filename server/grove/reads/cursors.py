"""Bounded process-local retention of already filtered immutable response bytes.

The D14 owner must check a live lease on every admission and page retrieval. This module neither
implements a privacy revision nor makes an expired response from a fresh capture. Ordinary writer
generations are intentionally absent from the page-lookup binding.
"""

import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from grove import clock
from grove.api.errors import ApiError
from grove.reads.runtime import deadline, snapshot_busy

TTL_SECONDS = 300.0
MAX_BYTES = 64 * 1024 * 1024
MAX_CAPTURES = 64
MAX_PAGES = 16_384


def _expired() -> ApiError:
    return ApiError(
        503,
        "unavailable",
        "the retained packet is unavailable; restart the query",
        details={"rule": "cursor_expired"},
        retryable=True,
    )


@dataclass(frozen=True)
class CursorContext:
    world_id: str
    principal: str
    grant: str
    policy: str
    query_digest: str
    page_size: int


@dataclass(frozen=True)
class CursorBinding(CursorContext):
    capture_id: str
    privacy_revision: str

    def context(self) -> CursorContext:
        return CursorContext(
            self.world_id,
            self.principal,
            self.grant,
            self.policy,
            self.query_digest,
            self.page_size,
        )


@dataclass(frozen=True)
class PreparedPages:
    """The response serializer supplies complete bytes and the embedded next-page handles."""

    binding: CursorBinding
    handles: tuple[str, ...]
    pages: tuple[bytes, ...]
    byte_size: int

    def __post_init__(self) -> None:
        if type(self.handles) is not tuple or type(self.pages) is not tuple:
            raise TypeError("prepared pages must contain immutable tuples")
        if not self.pages or len(self.pages) != len(self.handles):
            raise ValueError("each prepared page needs one handle")
        if any(type(page) is not bytes for page in self.pages):
            raise TypeError("prepared pages must contain immutable bytes")
        if type(self.byte_size) is not int or self.byte_size != sum(map(len, self.pages)):
            raise ValueError("prepared page bytes must match the measured size")
        if len(set(self.handles)) != len(self.handles):
            raise ValueError("prepared page handles must be distinct")


def handles(count: int) -> tuple[str, ...]:
    if type(count) is not int or count < 1:
        raise ValueError("a capture must contain at least one page")
    return tuple(secrets.token_urlsafe(32) for _ in range(count))


def prepare(
    binding: CursorBinding,
    page_handles: Sequence[str],
    pages: Sequence[bytes],
) -> PreparedPages:
    """Run potentially large byte assembly in the bounded worker, before atomic admission."""
    deadline()
    page_handles, pages = tuple(page_handles), tuple(pages)
    if not pages or len(pages) != len(page_handles) or len(set(page_handles)) != len(pages):
        raise ValueError("each immutable page needs one distinct handle")
    if any(type(page) is not bytes for page in pages):
        raise TypeError("retained pages must be immutable serialized bytes")
    if any(type(handle) is not str or len(handle) != 43 for handle in page_handles):
        raise ValueError("page handles must use the opaque generated form")
    # Page handles and binding overhead are measured separately in the scale test. Refuse an
    # unbounded tiny-page index even when the payload itself fits in the byte allowance.
    if len(pages) > MAX_PAGES:
        raise snapshot_busy()
    size = sum(map(len, pages))
    if size > MAX_BYTES:
        raise snapshot_busy()
    deadline()
    return PreparedPages(binding, page_handles, pages, size)


@dataclass(frozen=True)
class _Retained:
    prepared: PreparedPages
    expires: float


class CursorStore:
    def __init__(self) -> None:
        self.incarnation = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._captures: dict[str, _Retained] = {}
        self._pages: dict[str, tuple[str, int]] = {}
        self._bytes = 0

    def _expire(self, now: float) -> None:
        for capture_id, retained in tuple(self._captures.items()):
            if retained.expires <= now:
                self._drop(capture_id)

    def _drop(self, capture_id: str) -> None:
        retained = self._captures.pop(capture_id)
        self._bytes -= retained.prepared.byte_size
        for handle in retained.prepared.handles:
            del self._pages[handle]

    def admit(
        self,
        prepared: PreparedPages,
        *,
        check_lease: Callable[[CursorBinding], None],
    ) -> bytes:
        deadline()
        check_lease(prepared.binding)
        with self._lock:
            deadline()
            now = clock.monotonic()
            self._expire(now)
            capture_id = prepared.binding.capture_id
            if capture_id in self._captures or any(h in self._pages for h in prepared.handles):
                raise snapshot_busy()
            if (
                len(self._captures) >= MAX_CAPTURES
                or self._bytes + prepared.byte_size > MAX_BYTES
                or len(self._pages) + len(prepared.pages) > MAX_PAGES
            ):
                raise snapshot_busy()
            self._captures[capture_id] = _Retained(prepared, now + TTL_SECONDS)
            self._pages.update({h: (capture_id, i) for i, h in enumerate(prepared.handles)})
            self._bytes += prepared.byte_size
            return prepared.pages[0]

    def page(
        self,
        handle: str,
        context: CursorContext,
        *,
        check_lease: Callable[[CursorBinding], None],
    ) -> bytes:
        deadline()
        # The caller acquires the current D14 lease before even this cache lookup. Its callback
        # compares the stored privacy revision; no cached policy substitutes for that decision.
        with self._lock:
            deadline()
            self._expire(clock.monotonic())
            found = self._pages.get(handle)
            if found is None:
                raise _expired()
            capture_id, index = found
            retained = self._captures[capture_id]
            if retained.prepared.binding.context() != context:
                raise _expired()
            try:
                check_lease(retained.prepared.binding)
            except Exception:
                # Refused privacy authority must not leave the old capture available locally.
                self._drop(capture_id)
                raise
            deadline()
            return retained.prepared.pages[index]

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._bytes
