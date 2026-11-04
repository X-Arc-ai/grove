"""A generation bracket around every mutable packet input in one explicit read transaction.

The materializer is application code, never request-supplied Cypher. All helper reads, lexical
membership and detached inputs finish before the final marker. Privacy filtering is D14-owned.
"""

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from grove import clock
from grove.reads.runtime import ReadRuntime, deadline, snapshot_busy
from grove.store.query import CYPHER_PREFIX, guard, plain, transaction_search
from grove.store.scope import WORLD_PARAM, check_world_id, refuse_world_param, scope_cypher

ATTEMPTS = 3
_MARKER = (
    "MATCH (w:World {id: $world_id}) RETURN w.id AS id, "
    "w.read_generation AS generation, w.next_sequence AS sequence"
)


def _json_time(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture times must be aware")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("a capture contains an unsupported value")


def encode(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_time,
    ).encode("utf-8")


@dataclass(frozen=True)
class Capture:
    world_id: str
    commit: int
    history_sequence: int
    capture_id: str
    reflected_at: datetime
    payload: bytes

    def unpack(self) -> Any:
        """Each caller receives its own values; retained capture bytes are immutable."""
        return json.loads(self.payload)


class PacketSource:
    def __init__(self, tx: Any, world_id: str) -> None:
        self.__tx = tx
        self.world_id = check_world_id(world_id)
        self.__busy = False
        self.__active = True

    def invalidate(self) -> None:
        self.__active = False

    async def __run(self, cypher: str, params: dict, *, marker: bool = False) -> list[dict]:
        deadline()
        if not self.__active or self.__busy:
            raise snapshot_busy()
        self.__busy = True
        try:
            result = await self.__tx.run(CYPHER_PREFIX + cypher, params)
            rows = []
            async for record in result:
                deadline()
                row = dict(record.items())
                if not marker:
                    guard(row, self.world_id)
                rows.append(plain(row))
            await result.consume()
            deadline()
            return rows
        finally:
            self.__busy = False

    async def marker(self) -> tuple[int, int]:
        rows = await self.__run(_MARKER, {WORLD_PARAM: self.world_id}, marker=True)
        if len(rows) != 1 or rows[0].get("id") != self.world_id:
            raise snapshot_busy()
        generation, sequence = rows[0].get("generation"), rows[0].get("sequence")
        if type(generation) is not int or generation < 0:
            raise snapshot_busy()
        if type(sequence) is not int or sequence < 0:
            raise snapshot_busy()
        return generation, sequence

    async def read(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        scoped = scope_cypher(cypher, "read")
        return await self.__run(scoped, {**params, WORLD_PARAM: self.world_id})

    async def search(self, index: str, text: str, *, fuzzy: bool = False) -> list[dict]:
        return await transaction_search(
            self.__run,
            self.world_id,
            index,
            text,
            fuzzy=fuzzy,
        )


async def capture(
    runtime: ReadRuntime,
    world_id: str,
    materialize: Callable[[PacketSource, datetime], Awaitable[Any]],
) -> Capture:
    """Return detached data only after every read agrees on the same committed generation.

    The response boundary must already own a request budget and a D14 lease. This component
    supplies consistency, not authority to disclose or retain its unfiltered raw data.
    """
    world_id = check_world_id(world_id)
    for _ in range(ATTEMPTS):
        deadline()
        async with runtime.transaction() as tx:
            source = PacketSource(tx, world_id)
            try:
                before = await source.marker()
                reflected_at = clock.now(world_id)
                value = await materialize(source, reflected_at)
                payload = await runtime.worker(encode, value)
                after = await source.marker()
            finally:
                source.invalidate()
        if before == after:
            deadline()
            return Capture(
                world_id,
                before[0],
                before[1],
                secrets.token_urlsafe(24),
                reflected_at,
                payload,
            )
    raise snapshot_busy()
