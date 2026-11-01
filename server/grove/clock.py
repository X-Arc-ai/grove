"""The one clock provider (R2, A.6). Nothing else in the repo reads a clock."""

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Protocol


class ClockNotSet(RuntimeError):
    """A TestClock has no time for this world. It never falls back to the system clock."""


class Clock(Protocol):
    def now(self, world_id: str | None = None) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self, world_id: str | None = None) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a clock takes aware datetimes only")
    return at.astimezone(UTC)


class TestClock:
    """Time per world. A world's own time wins over the store-wide default."""

    __test__ = False

    def __init__(self) -> None:
        self._default: datetime | None = None
        self._worlds: dict[str, datetime] = {}
        self._ticks = 0.0

    def now(self, world_id: str | None = None) -> datetime:
        at = self._worlds.get(world_id, self._default) if world_id is not None else self._default
        if at is None:
            raise ClockNotSet(f"no time set for world {world_id!r}")
        return at

    def monotonic(self) -> float:
        return self._ticks

    def set(self, at: datetime, world_id: str | None = None) -> None:
        at = _as_utc(at)
        if world_id is None:
            self._default = at
        else:
            self._worlds[world_id] = at

    def advance(self, *, world_id: str | None = None, **timedelta_kwargs: float) -> datetime:
        delta = timedelta(**timedelta_kwargs)
        if delta < timedelta(0):
            raise ValueError("time moves forward; use set to go back")
        at = self.now(world_id) + delta
        self.set(at, world_id)
        self._ticks += delta.total_seconds()
        return at


_provider: Clock = SystemClock()
# using() swaps the one process-wide provider, so two threads in two blocks would restore each
# other's clock out of order. The owner thread and its depth say who may enter.
_using_lock = threading.Lock()
_using_owner: int | None = None
_using_depth = 0


def now(world_id: str | None = None) -> datetime:
    return _provider.now(world_id)


def monotonic() -> float:
    return _provider.monotonic()


def today(world_id: str | None = None) -> date:
    return now(world_id).date()


def install(clock: Clock) -> None:
    global _provider
    if os.environ.get("GROVE_ENV") == "production" and not isinstance(clock, SystemClock):
        raise RuntimeError("production runs on the system clock")
    _provider = clock


@contextmanager
def using(clock: Clock) -> Iterator[Clock]:
    """Install `clock` for the block, then restore the previous provider.

    The provider is process-wide: every thread reads the clock of the block, which is what a pool
    worker under the block needs. One thread holds blocks at a time. It may nest them, and they
    restore in order; another thread that enters while a block is held gets RuntimeError.
    """
    global _using_owner, _using_depth
    me = threading.get_ident()
    with _using_lock:
        if _using_owner is not None and _using_owner != me:
            raise RuntimeError("clock.using is process-wide and another thread holds a block")
        previous = _provider
        install(clock)
        _using_owner = me
        _using_depth += 1
    try:
        yield clock
    finally:
        with _using_lock:
            _using_depth -= 1
            if _using_depth == 0:
                _using_owner = None
            install(previous)
