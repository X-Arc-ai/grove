"""The writer entry: the only execute_write for world data."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields, is_dataclass
from typing import Any, TypeVar

from neo4j import Driver, ManagedTransaction, Result, Transaction, unit_of_work

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
# The same row and the same lock, with no change: taken first, before anything is read, so a read
# that takes no lock of its own (the near-duplicate check, the restatement read, the slot read)
# sees every batch that committed before this one. A rollback leaves no gap. It reads no column:
# `SET w.next_sequence = w.next_sequence` read the counter before it waited for the lock and wrote
# that stale value back after, so a batch that waited set the counter back (probed on the store).
# A marker set and removed in one statement takes the lock and changes nothing.
_LOCK_WORLD = (
    CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) "
    "SET w.lock_marker = true REMOVE w.lock_marker "
    "RETURN w.id AS id, w.read_generation AS generation"
)

_ADVANCE_GENERATION = (
    CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) "
    "SET w.read_generation = w.read_generation + 1 RETURN w.read_generation AS generation"
)
_MAX_GENERATION = 2**63 - 1


class InvalidGeneration(RuntimeError):
    """The operational capture marker is absent, malformed, or exhausted."""


class TransactionEscape(TypeError):
    """A callback must return detached values, never a live transaction or Result."""


class _UnscopedRunner:
    """The writer-only platform seam; native transactions never leave WorldTx."""

    __slots__ = ("__run",)

    def __init__(self, run: Callable[..., Result]) -> None:
        self.__run = run

    def run(self, query: str, parameters: dict | None = None, **kwargs: Any) -> Result:
        return self.__run(query, parameters, **kwargs)


class UnknownWorld(RuntimeError):
    """The world has no :World row, so it has no sequence."""


class WorldTx:
    def __init__(self, tx: ManagedTransaction, world_id: str) -> None:
        self._tx = tx
        self._world_id = world_id
        self._results: list[Result] = []
        self._locked = False
        self._closed = False
        self._generation = 0

    @property
    def world_id(self) -> str:
        return self._world_id

    def _run(self, query: str, parameters: dict | None = None, **kwargs: Any) -> Result:
        if self._closed:
            raise TransactionEscape("the writer callback has ended")
        result = self._tx.run(query, parameters, **kwargs)
        self._results.append(result)
        return result

    def run(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        statement = CYPHER_PREFIX + scope_cypher(cypher, "write")
        result = self._run(statement, {**params, WORLD_PARAM: self._world_id})
        return collect(result, self._world_id)

    def next_sequence(self) -> int:
        record = self._run(_NEXT_SEQUENCE, {WORLD_PARAM: self._world_id}).single()
        if record is None:
            raise UnknownWorld(self._world_id)
        return record["sequence"]

    def _acquire_world_lock(self) -> None:
        if self._closed:
            raise TransactionEscape("the writer callback has ended")
        if self._locked:
            return
        # This exact lock statement alone is outside mutation tracking.
        result = self._tx.run(_LOCK_WORLD, {WORLD_PARAM: self._world_id})
        record = result.single()
        result.consume()
        if record is None:
            raise UnknownWorld(self._world_id)
        generation = record["generation"]
        if type(generation) is not int or not 0 <= generation <= _MAX_GENERATION:
            raise InvalidGeneration("World.read_generation must be a nonnegative integer")
        self._generation = generation
        self._locked = True

    def lock_world(self) -> None:
        """Holds the mandatory World lock; repeated calls issue no statement."""
        self._acquire_world_lock()

    def unscoped(self) -> _UnscopedRunner:
        """A tracked run-only seam for writer-owned platform statements."""
        return _UnscopedRunner(self._run)

    def _finish(self, value: T) -> T:
        try:
            _refuse_live_value(value)
            # Do not short-circuit: every native Result must reach its summary, even if
            # an earlier statement already reported an update.
            dirty = False
            for result in self._results:
                dirty = result.consume().counters.contains_updates or dirty
            if dirty:
                if self._generation == _MAX_GENERATION:
                    raise InvalidGeneration("World.read_generation is exhausted")
                result = self._run(_ADVANCE_GENERATION, {WORLD_PARAM: self._world_id})
                record = result.single()
                result.consume()
                if record is None or record["generation"] != self._generation + 1:
                    raise InvalidGeneration("World.read_generation changed inside its writer")
            return value
        finally:
            self._closed = True


def _refuse_live_value(value: Any, seen: set[int] | None = None) -> None:
    if isinstance(
        value, (Result, Transaction, ManagedTransaction, WorldTx, _UnscopedRunner, Iterator)
    ):
        raise TransactionEscape("a writer callback returned a live transaction or Result")
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, Mapping):
        for key, child in value.items():
            _refuse_live_value(key, seen)
            _refuse_live_value(child, seen)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _refuse_live_value(getattr(value, field.name), seen)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for child in value:
            _refuse_live_value(child, seen)


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
            scoped = WorldTx(tx, self._world_id)
            scoped._acquire_world_lock()
            try:
                return scoped._finish(work(scoped))
            finally:
                scoped._closed = True

        with self._driver.session(database=DATABASE) as session:
            return session.execute_write(unit)
