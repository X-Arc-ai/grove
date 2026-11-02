"""Sending with back-off: a 429 waits until the stated reset (D15), a 5xx or a dropped connection
is tried again a few times. An egress refusal is never tried again."""

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable

import httpx

MAX_ATTEMPTS = 4
# A Modal server scaled to zero answers 503 until a container is up, and it queues nothing.
# The back-off doubles to MAX_WAIT, so 15 attempts wait about 10 minutes in all.
MODAL_ATTEMPTS = 15
MAX_WAIT = 60.0
_RETRY_STATUS = frozenset({500, 502, 503, 504})
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?")
_RESET_HEADERS = ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")


def _seconds(value: str) -> float | None:
    """A stated wait in seconds, or None when the text is not a finite wait. Below 0 counts as 0."""
    found = _parse_seconds(value.strip())
    if found is None or not math.isfinite(found):
        return None
    return max(found, 0.0)


def _parse_seconds(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        pass
    if value.endswith("ms"):
        try:
            return float(value[:-2]) / 1000
        except ValueError:
            return None
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def reset_after(response: httpx.Response) -> float | None:
    """The wait a 429 states: Retry-After in seconds, else the rate-limit reset headers."""
    stated = response.headers.get("retry-after")
    if stated is not None:
        found = _seconds(stated)
        if found is not None:
            return found
    stated_waits = (response.headers.get(name) for name in _RESET_HEADERS)
    waits = [_seconds(value) for value in stated_waits if value is not None]
    waits = [wait for wait in waits if wait is not None]
    return max(waits) if waits else None


def send(
    client: httpx.Client,
    build: Callable[[], httpx.Request],
    *,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """Send the request that `build` makes, and return the last response.

    A 429 after the last attempt is returned to the caller, which raises RateLimited with the
    reset. A transport error after the last attempt is raised.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = client.send(build())
        except httpx.TransportError:
            if attempt == attempts:
                raise
            sleep(min(2.0**attempt, MAX_WAIT))
            continue
        if response.status_code == 429 and attempt < attempts:
            stated = reset_after(response)
            response.close()
            sleep(min(stated if stated is not None else 2.0**attempt, MAX_WAIT))
            continue
        if response.status_code in _RETRY_STATUS and attempt < attempts:
            response.close()
            sleep(min(2.0**attempt, MAX_WAIT))
            continue
        return response
    raise AssertionError("unreachable")


async def asend(
    client: httpx.AsyncClient,
    build: Callable[[], httpx.Request],
    *,
    stream: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """The async form of send. With stream, the body of the returned response is still open."""
    for attempt in range(1, attempts + 1):
        try:
            response = await client.send(build(), stream=stream)
        except httpx.TransportError:
            if attempt == attempts:
                raise
            await sleep(min(2.0**attempt, MAX_WAIT))
            continue
        retry = response.status_code == 429 or response.status_code in _RETRY_STATUS
        if retry and attempt < attempts:
            stated = reset_after(response) if response.status_code == 429 else None
            await response.aclose()
            await sleep(min(stated if stated is not None else 2.0**attempt, MAX_WAIT))
            continue
        return response
    raise AssertionError("unreachable")
