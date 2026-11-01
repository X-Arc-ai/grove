"""The writer entry: the only execute_write for world data."""

from collections.abc import Callable
from typing import Any, TypeVar

from neo4j import Driver, ManagedTransaction, unit_of_work

from grove.store.connection import DATABASE
from grove.store.query import CYPHER_PREFIX, check_timeout, collect
from grove.store.scope import WORLD_PARAM, check_world_id, refuse_world_param, scope_cypher

T = TypeVar("T")

# One direct SET: Cypher takes the write lock before it reads, and holds it until commit, so
# sequence order is commit order and a rollback leaves no gap (S10). Reading the counter in an
# earlier statement would lose that lock.
_NEXT_SEQUENCE = (
    CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) "
    "SET w.next_sequence = w.next_sequence + 1 RETURN w.next_sequence AS sequence"
)


class UnknownWorld(RuntimeError):
    """The world has no :World row, so it has no sequence."""


class WorldTx:
    def __init__(self, tx: ManagedTransaction, world_id: str) -> None:
        self._tx = tx
        self._world_id = world_id

    def run(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        statement = CYPHER_PREFIX + scope_cypher(cypher, "write")
        result = self._tx.run(statement, {**params, WORLD_PARAM: self._world_id})
        return collect(result, self._world_id)

    def next_sequence(self) -> int:
        record = self._tx.run(_NEXT_SEQUENCE, {WORLD_PARAM: self._world_id}).single()
        if record is None:
            raise UnknownWorld(self._world_id)
        return record["sequence"]

    def unscoped(self) -> ManagedTransaction:
        """The transaction with no scope, for writer code that must commit a platform row with the
        world's rows. Nothing outside server/grove/writer/ gets a WorldTx."""
        return self._tx


class WorldWriter:
    def __init__(self, driver: Driver, world_id: str, *, timeout: float = 30.0) -> None:
        self._driver = driver
        self._world_id = check_world_id(world_id)
        self._timeout = check_timeout(timeout)

    def transaction(self, work: Callable[[WorldTx], T]) -> T:
        """The driver retries a transient failure, so `work` may run twice: it must hold no state
        outside the transaction."""

        @unit_of_work(timeout=self._timeout)
        def unit(tx: ManagedTransaction) -> T:
            return work(WorldTx(tx, self._world_id))

        with self._driver.session(database=DATABASE) as session:
            return session.execute_write(unit)
