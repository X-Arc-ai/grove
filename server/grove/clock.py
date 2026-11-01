"""The one clock provider (R2, A.6). Nothing else in the repo reads a clock."""

import os
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
    previous = _provider
    install(clock)
    try:
        yield clock
    finally:
        install(previous)
