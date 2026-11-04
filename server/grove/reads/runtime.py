"""Bounded D7 I/O and helper execution. No database writer or privacy policy lives here.

The response boundary owns request_budget through its final send. A cancelled Python callback
can finish CPU work, but retains its worker slot and cannot issue another query or publish data.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from typing import Any

from grove import clock
from grove.api.errors import ApiError
from grove.store.connection import DATABASE, AsyncDriver

REQUEST_SECONDS = 2.0
WORKERS = 2


def snapshot_busy() -> ApiError:
    return ApiError(
        503,
        "unavailable",
        "the packet cannot complete within its bound",
        details={"rule": "snapshot_busy"},
        retryable=True,
    )


@dataclass(frozen=True)
class Deadline:
    at: float
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)

    def remaining(self) -> float:
        seconds = self.at - clock.monotonic()
        if self.cancelled.is_set() or seconds <= 0:
            raise snapshot_busy()
        return seconds


_DEADLINE: ContextVar[Deadline | None] = ContextVar("packet_deadline", default=None)


def deadline() -> Deadline:
    current = _DEADLINE.get()
    if current is None:
        raise snapshot_busy()
    current.remaining()
    return current


@asynccontextmanager
async def request_budget(*, seconds: float = REQUEST_SECONDS) -> AsyncIterator[Deadline]:
    """One outer budget covers lease, acquisition, all attempts, serialization and transport."""
    if not 0 < seconds <= REQUEST_SECONDS:
        raise ValueError("the request budget must be positive and at most two seconds")
    current = _DEADLINE.get()
    if current is not None:
        current.remaining()
        yield current
        return
    current = Deadline(clock.monotonic() + seconds)
    token = _DEADLINE.set(current)
    try:
        async with asyncio.timeout(current.remaining()):
            yield current
            current.remaining()
    except TimeoutError:
        raise snapshot_busy() from None
    finally:
        current.cancelled.set()
        _DEADLINE.reset(token)


class SyncSource:
    """A run-only bridge to one owning event loop, with one outstanding query per helper."""

    def __init__(self, source: Any, loop: asyncio.AbstractEventLoop, budget: Deadline) -> None:
        self._source, self._loop, self._budget = source, loop, budget
        self._lock = threading.Lock()
        self._active = True
        self._pending: Future | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._active = False
            if self._pending is not None:
                self._pending.cancel()

    def _call(self, operation: Callable[[], Coroutine]) -> Any:
        with self._lock:
            if not self._active or self._pending is not None:
                raise snapshot_busy()
            remaining = self._budget.remaining()
            future = asyncio.run_coroutine_threadsafe(operation(), self._loop)
            self._pending = future
        try:
            result = future.result(timeout=remaining)
            with self._lock:
                if not self._active:
                    raise snapshot_busy()
                self._budget.remaining()
            return result
        except BaseException:
            future.cancel()
            raise
        finally:
            with self._lock:
                self._pending = None

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        return self._call(lambda: self._source.read(cypher, **params))

    def search(self, index: str, text: str, *, fuzzy: bool = False) -> list[dict]:
        return self._call(lambda: self._source.search(index, text, fuzzy=fuzzy))


class ReadRuntime:
    def __init__(self, driver: AsyncDriver) -> None:
        self.driver = driver
        self._pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="grove-read")
        self._lock = threading.Lock()
        self._jobs: set[Future] = set()
        self._closed = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        budget = deadline()
        if self._closed:
            raise snapshot_busy()
        session = self.driver.session(
            database=DATABASE,
            default_access_mode="READ",
            connection_acquisition_timeout=budget.remaining(),
            max_transaction_retry_time=0,
        )
        closed = False
        try:
            tx = await session.begin_transaction(timeout=budget.remaining())
            yield tx
            budget.remaining()
            await tx.rollback()
            await session.close()
            closed = True
        finally:
            if not closed:
                # Native cancel is synchronous and force-closes the socket; no unbounded cleanup.
                session.cancel()

    async def worker[T](self, function: Callable[..., T], *args: Any) -> T:
        budget = deadline()
        context = copy_context()
        with self._lock:
            if self._closed or len(self._jobs) >= WORKERS:
                raise snapshot_busy()
            future = self._pool.submit(context.run, function, *args)
            self._jobs.add(future)

        def finished(done: Future) -> None:
            with self._lock:
                self._jobs.discard(done)

        future.add_done_callback(finished)
        try:
            result = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            budget.cancelled.set()
            raise
        budget.remaining()
        return result

    async def helper[T](self, source: Any, function: Callable[[SyncSource], T]) -> T:
        bridge = SyncSource(source, asyncio.get_running_loop(), deadline())
        try:
            return await self.worker(function, bridge)
        finally:
            bridge.invalidate()

    async def close(self) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        await self.driver.close()
