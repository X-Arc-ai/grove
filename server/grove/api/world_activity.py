"""Requests that write into a world for a long time, and the delete of that world.

A long request checks who is calling once and then writes for minutes. The new world after a delete
keeps the old id, so an id captured before the delete still passes every check after it. Such a
request would land the old content in the new, empty world. So a delete refuses while one runs, and
none starts while a delete runs. A request also looks at its caller again inside the guard: a delete
that finished before the guard was taken ended that caller's session.

The API is one process (deploy/api.Dockerfile runs one uvicorn), so this state lives in memory.
"""

import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

from grove.api.errors import ApiError

_LOCK = threading.Lock()
_WRITING: Counter[str] = Counter()
_DELETING: set[str] = set()


@contextmanager
def world_write(world_id: str) -> Iterator[None]:
    with _LOCK:
        if world_id in _DELETING:
            raise ApiError(409, "busy", "This Grove is being deleted.", retryable=False)
        _WRITING[world_id] += 1
    try:
        yield
    finally:
        with _LOCK:
            _WRITING[world_id] -= 1
            if _WRITING[world_id] <= 0:
                del _WRITING[world_id]


@contextmanager
def world_deleting(world_id: str) -> Iterator[None]:
    with _LOCK:
        if world_id in _DELETING or _WRITING[world_id] > 0:
            raise ApiError(
                409, "busy", "An import is still running. Try again when it ends.", retryable=True
            )
        _DELETING.add(world_id)
    try:
        yield
    finally:
        with _LOCK:
            _DELETING.discard(world_id)
