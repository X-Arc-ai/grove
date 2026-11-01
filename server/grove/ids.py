"""Row ids: UUIDv7 text with its time from the clock provider, not from the uuid module."""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from grove import clock

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def new_id(world_id: str | None = None) -> str:
    """A world under a TestClock passes its id, so the id's time follows that world's clock."""
    millis = (clock.now(world_id) - _EPOCH) // _MILLISECOND
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    return str(UUID(int=millis << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b))
