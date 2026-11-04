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
ERROR_RESERVE_SECONDS = 0.05
ERROR_RESERVE_FRACTION = 0.1
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
    at: float | None
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def remaining(self) -> float:
        with self._lock:
            if self.at is None or self.cancelled.is_set():
                raise snapshot_busy()
            seconds = self.at - clock.monotonic()
            if seconds <= 0:
                raise snapshot_busy()
            return seconds

    def _tighten(self, at: float) -> None:
        # The shared handle stays the same in copied worker contexts. Its bound may only
        # move earlier; callers cannot replace or revive an expired/cancelled deadline.
        with self._lock:
            now = clock.monotonic()
            if self.cancelled.is_set() or at <= now or (self.at is not None and self.at <= now):
                raise snapshot_busy()
            object.__setattr__(self, "at", at if self.at is None else min(self.at, at))


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


class ResponseBudget:
    """One deferred deadline owned by the outer ASGI task, never by a tool worker.

    Activation belongs on this event loop before a read handler runs. Reading the active handle
    is safe in copied contexts. A trusted owner can shorten it to a credential/capture lifetime.
    The owner still must validate that lifetime and the final transport handoff.
    """

    def __init__(self, current: Deadline, timeout: asyncio.Timeout) -> None:
        self.current = current
        self._timeout = timeout
        self._loop = asyncio.get_running_loop()
        self._work_when: float | None = None
        self._hard_when: float | None = None
        self._invalidation: asyncio.TimerHandle | None = None

    @property
    def active(self) -> bool:
        return self.current.at is not None

    def activate(self, *, seconds: float = REQUEST_SECONDS) -> Deadline:
        if asyncio.get_running_loop() is not self._loop:
            raise snapshot_busy()
        if isinstance(seconds, bool) or not 0 < seconds <= REQUEST_SECONDS:
            raise ValueError("the response budget must be positive and at most two seconds")
        self.current._tighten(clock.monotonic() + seconds)
        # asyncio owns its clock domain. Convert the remaining duration through its public
        # timeout factory so a test Clock need not have the event loop's monotonic origin.
        remaining = self.current.remaining()
        hard_when = asyncio.timeout(remaining).when()
        assert hard_when is not None
        self._hard_when = hard_when if self._hard_when is None else min(self._hard_when, hard_when)
        reserve = min(ERROR_RESERVE_SECONDS, remaining * ERROR_RESERVE_FRACTION)
        work_when = self._hard_when - reserve
        self._work_when = work_when if self._work_when is None else min(self._work_when, work_when)
        if self._invalidation is not None:
            self._invalidation.cancel()
        self._invalidation = self._loop.call_at(self._work_when, self.current.cancelled.set)
        self._timeout.reschedule(self._work_when)
        return self.current

    def close(self) -> None:
        self.current.cancelled.set()
        if self._invalidation is not None:
            self._invalidation.cancel()

    def error_remaining(self) -> float:
        """Check only the original hard end; this never authorizes further private work.

        The private handle is already cancelled and its leases closed before safe error delivery.
        Keep the clock check as well as asyncio's fixed timeout for deterministic clock tests.
        """
        at = self.current.at
        remaining = 0 if at is None else at - clock.monotonic()
        if remaining <= 0:
            raise snapshot_busy()
        return remaining

    @asynccontextmanager
    async def error_delivery(self) -> AsyncIterator[None]:
        """Deliver a safe error within the original hard end, never a new duration."""
        self.error_remaining()
        timeout = asyncio.timeout_at(self._hard_when)
        try:
            async with timeout:
                yield
                self.error_remaining()
        except TimeoutError:
            if timeout.expired():
                raise snapshot_busy() from None
            raise


@asynccontextmanager
async def response_budget() -> AsyncIterator[ResponseBudget]:
    """Install an inactive handle without imposing a read timeout on an unrelated request."""
    parent = _DEADLINE.get()
    if parent is not None:
        parent.remaining()
    current = Deadline(parent.at if parent is not None else None)
    token = _DEADLINE.set(current)
    budget = None
    try:
        async with asyncio.timeout(None) as timeout:
            budget = ResponseBudget(current, timeout)
            if parent is not None:
                budget.activate(seconds=min(parent.remaining(), REQUEST_SECONDS))
            yield budget
            if budget.active:
                current.remaining()
    except TimeoutError:
        if timeout.expired():
            raise snapshot_busy() from None
        raise
    finally:
        current.cancelled.set()
        if budget is not None:
            budget.close()
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
