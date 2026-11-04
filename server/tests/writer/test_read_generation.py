"""Complete-world generation: atomic, monotonic, and unchanged by defined no-ops."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from neo4j.exceptions import TransientError

from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.writer import migrate as migrate_module
from grove.writer import tx as tx_module
from grove.writer.migrate import MigrationDrift, run_migrations
from grove.writer.restore import load_snapshot, rebuild_in_place, take_snapshot
from grove.writer.testing import plant_node
from grove.writer.tx import InvalidGeneration, TransactionEscape, UnknownWorld, WorldWriter
from grove.writer.worlds import create_world


def world_properties(driver, world):
    with driver.session(database=DATABASE) as session:
        return session.run(
            "MATCH (w:World {id: $world}) RETURN properties(w) AS row", world=world
        ).single()["row"]


@pytest.fixture
def generation_world(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    return world


def test_mutations_advance_once_even_without_a_history_entry(driver, generation_world):
    world = generation_world
    writer = WorldWriter(driver, world)
    before = world_properties(driver, world)

    def mutate(tx):
        tx.run("CREATE (:Connector {id: $id, read_count: 1})", id=new_id(world))
        tx.run("CREATE (:Item {id: $id, title: 'Wren'})", id=new_id(world))
        tx.run("CREATE (:Alias {id: $id, phrase: 'Wren'})", id=new_id(world))
        tx.run("CREATE (:Area {id: $id})", id=new_id(world))
        tx.run("CREATE (:Impact {id: $id})", id=new_id(world))

    writer.transaction(mutate)
    after = world_properties(driver, world)
    assert after["read_generation"] == before["read_generation"] + 1
    assert after["next_sequence"] == before["next_sequence"]
    writer.transaction(lambda tx: tx.run("MATCH (c:Connector) SET c.read_count = 2"))
    assert world_properties(driver, world)["read_generation"] == before["read_generation"] + 2


def test_a_read_and_repeated_lock_preserve_every_world_property(driver, generation_world):
    before = world_properties(driver, generation_world)

    def read(tx):
        tx.lock_world()
        tx.lock_world()
        assert tx.run("MATCH (i:Item) RETURN i") == []
        assert tx.unscoped().run("RETURN 1 AS n").single()["n"] == 1

    WorldWriter(driver, generation_world).transaction(read)
    assert world_properties(driver, generation_world) == before


def test_sequence_and_unconsumed_platform_mutation_are_tracked(driver, generation_world):
    world = generation_world

    def mutate(tx):
        tx.next_sequence()
        tx.next_sequence()
        # Deliberately unconsumed: finalization must force execution and inspect its summary.
        tx.unscoped().run(
            "MATCH (w:World {id: $world}) SET w.test_marker = 'captured'", world=world
        )

    WorldWriter(driver, world).transaction(mutate)
    row = world_properties(driver, world)
    assert (row["read_generation"], row["next_sequence"], row["test_marker"]) == (1, 2, "captured")


def test_rollback_preserves_data_sequence_and_generation(driver, generation_world):
    world = generation_world
    before = world_properties(driver, world)

    def fail(tx):
        tx.next_sequence()
        tx.run("CREATE (:Item {id: 'rollback'})")
        raise RuntimeError("rollback control")

    with pytest.raises(RuntimeError, match="rollback control"):
        WorldWriter(driver, world).transaction(fail)
    assert world_properties(driver, world) == before
    assert (
        WorldWriter(driver, world).transaction(lambda tx: tx.run("MATCH (i:Item) RETURN i")) == []
    )


def test_concurrent_snapshot_mutations_cannot_lose_a_generation(driver, generation_world):
    def mutate(number):
        return WorldWriter(driver, generation_world).transaction(
            lambda tx: tx.run("CREATE (:Connector {id: $id})", id=new_id(generation_world))
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(mutate, range(12)))
    assert world_properties(driver, generation_world)["read_generation"] == 12


def test_missing_world_refuses_before_callback(driver, world):
    called = []
    with pytest.raises(UnknownWorld):
        WorldWriter(driver, world).transaction(lambda tx: called.append(True))
    assert called == []


@pytest.mark.parametrize("generation", [None, -1, True, "zero", 1.5])
def test_invalid_generation_refuses_before_callback(driver, world, generation):
    plant_node(driver, world, "World", next_sequence=0, read_generation=generation)
    before = world_properties(driver, world)
    called = []
    with pytest.raises(InvalidGeneration):
        WorldWriter(driver, world).transaction(lambda tx: called.append(True))
    assert called == []
    assert world_properties(driver, world) == before


def test_exhausted_generation_refuses_mutation_atomically(driver, world):
    plant_node(driver, world, "World", next_sequence=0, read_generation=2**63 - 1)
    with pytest.raises(InvalidGeneration, match="exhausted"):
        WorldWriter(driver, world).transaction(lambda tx: tx.next_sequence())
    row = world_properties(driver, world)
    assert (row["read_generation"], row["next_sequence"]) == (2**63 - 1, 0)


@pytest.mark.parametrize(
    "returned", ["tx", "facade", "result", "nested_result", "record", "iterator"]
)
def test_live_objects_cannot_escape_a_callback(driver, generation_world, returned):
    before = world_properties(driver, generation_world)

    @dataclass
    class Envelope:
        value: object

    def work(tx):
        facade = tx.unscoped()
        assert not hasattr(facade, "commit") and not hasattr(facade, "rollback")
        result = facade.run("RETURN 1 AS n")
        return {
            "tx": tx,
            "facade": facade,
            "result": result,
            "nested_result": [result],
            "record": Envelope(result),
            "iterator": iter([result]),
        }[returned]

    with pytest.raises(TransactionEscape):
        WorldWriter(driver, generation_world).transaction(work)
    assert world_properties(driver, generation_world) == before


def test_world_creation_and_rebuild_preserve_live_monotonicity(driver, world):
    create_world(driver, world, owner_user_id="u_t_owner")
    initial = world_properties(driver, world)
    assert initial["read_generation"] == 1
    create_world(driver, world, owner_user_id="another_owner")
    assert world_properties(driver, world) == initial
    snapshot = take_snapshot(driver, world)
    assert "read_generation" not in repr(snapshot)
    rebuild_in_place(driver, world)
    after = world_properties(driver, world)
    assert after["read_generation"] == 2
    assert after["next_sequence"] == initial["next_sequence"]


def test_every_result_is_consumed_even_after_an_update_and_failure_aborts():
    class SummaryResult:
        def __init__(self, updates=False, failure=False):
            self.updates, self.failure, self.consumed = updates, failure, 0

        def consume(self):
            self.consumed += 1
            if self.failure:
                raise RuntimeError("summary failed")
            return SimpleNamespace(counters=SimpleNamespace(contains_updates=self.updates))

    first, second = SummaryResult(updates=True), SummaryResult(failure=True)
    native = SimpleNamespace(run=lambda *args, **kwargs: pytest.fail("no increment after failure"))
    tx = tx_module.WorldTx(native, "world")
    tx._results.extend([first, second])
    with pytest.raises(RuntimeError, match="summary failed"):
        tx._finish(None)
    assert first.consumed == second.consumed == 1
    with pytest.raises(TransactionEscape):
        tx.unscoped().run("RETURN 1")


def test_generation_initializer_preserves_existing_values_and_checksum(
    driver, make_world, tmp_path
):
    worlds = [make_world(), make_world()]
    plant_node(driver, worlds[0], "World", next_sequence=0, read_generation=None)
    plant_node(driver, worlds[1], "World", next_sequence=3, read_generation=19)
    migration = Path(__file__).resolve().parents[2] / "migrations/0014_read_generation.cypher"
    text = migration.read_text()
    # The exact approved migration, repeated as its runner may repeat after a process exit.
    with driver.session(database=DATABASE) as session:
        for _ in range(2):
            session.run("CYPHER 25 " + text.rstrip().removesuffix(";")).consume()
    assert world_properties(driver, worlds[0])["read_generation"] == 0
    assert world_properties(driver, worlds[1])["read_generation"] == 19
    # Use an isolated ledger scope and a harmless file to prove checksum drift stops execution.
    name = "0014_read_generation.cypher"
    copied = tmp_path / name
    copied.write_text("RETURN 1;\n")
    scope = worlds[0] + "_migration"
    try:
        run_migrations(driver, tmp_path, scope=scope)
        copied.write_text("RETURN 2;\n")
        with pytest.raises(MigrationDrift):
            run_migrations(driver, tmp_path, scope=scope)
    finally:
        with driver.session(database=DATABASE) as session:
            session.run("MATCH (m:Migration {scope: $scope}) DELETE m", scope=scope).consume()


def test_driver_retry_rolls_back_the_failed_generation(driver, generation_world, monkeypatch):
    original = tx_module.WorldTx._finish
    attempts = []

    def fail_first(self, value):
        result = original(self, value)
        attempts.append(True)
        if len(attempts) == 1:
            raise TransientError("generation retry control")
        return result

    monkeypatch.setattr(tx_module.WorldTx, "_finish", fail_first)
    WorldWriter(driver, generation_world).transaction(lambda tx: tx.next_sequence())
    row = world_properties(driver, generation_world)
    assert len(attempts) == 2
    assert (row["read_generation"], row["next_sequence"]) == (1, 1)


def test_world_lock_precedes_callbacks_without_a_sequence_write(driver, generation_world):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()

    def first(tx):
        entered.set()
        assert release.wait(10)

    def second(tx):
        second_started.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(WorldWriter(driver, generation_world).transaction, first)
        assert entered.wait(10)
        blocked = pool.submit(WorldWriter(driver, generation_world).transaction, second)
        try:
            assert not second_started.wait(0.2)
        finally:
            release.set()
        held.result(timeout=10)
        blocked.result(timeout=10)
    assert world_properties(driver, generation_world)["read_generation"] == 0


def test_snapshot_load_never_restores_an_older_generation(driver, world):
    create_world(driver, world, owner_user_id="u_t_owner")
    snapshot = take_snapshot(driver, world)
    old_sequence = world_properties(driver, world)["next_sequence"]
    writer = WorldWriter(driver, world)
    for _ in range(3):
        writer.transaction(lambda tx: tx.next_sequence())
    before = world_properties(driver, world)
    assert before["next_sequence"] > old_sequence
    # Simulate the offline removal of world data, while the platform World remains live.
    with driver.session(database=DATABASE) as session:
        session.run("MATCH (n {world_id: $world}) DETACH DELETE n", world=world).consume()
    load_snapshot(driver, world, snapshot)
    after = world_properties(driver, world)
    assert after["next_sequence"] == old_sequence
    assert after["read_generation"] == before["read_generation"] + 1


class _GenerationResult:
    def __init__(self, rows=()):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def consume(self):
        return None


class _GenerationTransaction:
    def __init__(self, driver):
        self.driver = driver
        self.worlds = deepcopy(driver.worlds)
        self.scans = 0
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.committed:
            self.driver.rollbacks += 1

    def run(self, query):
        self.driver.queries.append(query)
        if query == "CYPHER 25 MATCH (w:World) RETURN w.read_generation AS generation":
            self.scans += 1
            if self.scans == 2 and self.driver.post_repair_failure:
                raise RuntimeError("synthetic final scan failure")
            return _GenerationResult(
                [{"generation": world.get("read_generation")} for world in self.worlds]
            )
        assert query == (
            "CYPHER 25 MATCH (w:World) WHERE w.read_generation IS NULL SET w.read_generation = 0"
        )
        self.driver.repairs += 1
        for world in self.worlds:
            if world.get("read_generation") is None:
                world["read_generation"] = 0
        if self.driver.post_repair_null:
            self.worlds[0]["read_generation"] = None
        if self.driver.post_repair_extra_world:
            self.worlds.append({"read_generation": 0})
        return _GenerationResult()

    def commit(self):
        self.driver.commits += 1
        if self.driver.commit_failure:
            raise RuntimeError("synthetic uncertain commit")
        self.driver.worlds = self.worlds
        self.committed = True


class _GenerationDriver:
    def __init__(self, worlds):
        self.worlds = deepcopy(worlds)
        self.queries = []
        self.transactions = 0
        self.repairs = 0
        self.commits = 0
        self.rollbacks = 0
        self.post_repair_failure = False
        self.post_repair_null = False
        self.post_repair_extra_world = False
        self.commit_failure = False

    def session(self, *, database):
        assert database == DATABASE
        return nullcontext(self)

    def begin_transaction(self, *, timeout):
        assert timeout == 60.0
        self.transactions += 1
        return _GenerationTransaction(self)

    def execute_write(self, *args, **kwargs):
        pytest.fail("reconciliation must not use managed transaction retries")


def test_quiescent_reconciliation_repairs_null_after_recorded_migration(tmp_path, monkeypatch):
    name = "0014_read_generation.cypher"
    text = (Path(__file__).resolve().parents[2] / "migrations" / name).read_text()
    (tmp_path / name).write_text(text)
    ledger = {f"grove/{name}": migrate_module.checksum(text)}
    monkeypatch.setattr(migrate_module, "_ledger", lambda *args: ledger.copy())
    monkeypatch.setattr(
        migrate_module, "_apply", lambda *args: pytest.fail("recorded migration ran again")
    )
    driver = _GenerationDriver([{"read_generation": 19, "next_sequence": 7}])
    assert run_migrations(driver, tmp_path) == {"applied": [], "skipped": [name]}
    # An old writer creates this world after 0014 already has a matching ledger row.
    driver.worlds.append({"next_sequence": 3})
    assert run_migrations(driver, tmp_path) == {"applied": [], "skipped": [name]}
    assert "read_generation" not in driver.worlds[1]
    assert migrate_module.reconcile_generations(driver) == {
        "worlds": 2,
        "initialized": 1,
        "missing": 0,
    }
    assert driver.worlds == [
        {"read_generation": 19, "next_sequence": 7},
        {"read_generation": 0, "next_sequence": 3},
    ]
    assert ledger == {f"grove/{name}": migrate_module.checksum(text)}


def test_quiescent_reconciliation_preserves_valid_counters_and_repeats():
    worlds = [{"read_generation": value, "next_sequence": 9} for value in [0, 19, 2**63 - 2]]
    driver = _GenerationDriver([*worlds, {"read_generation": None, "next_sequence": 4}])
    assert migrate_module.reconcile_generations(driver)["initialized"] == 1
    expected = [*worlds, {"read_generation": 0, "next_sequence": 4}]
    assert driver.worlds == expected
    assert migrate_module.reconcile_generations(driver) == {
        "worlds": 4,
        "initialized": 0,
        "missing": 0,
    }
    assert driver.worlds == expected
    assert driver.commits == driver.transactions == 2


@pytest.mark.parametrize("value", [-1, True, False, "0", 0.0, 1.5, [], 2**63 - 1, 2**63])
def test_quiescent_reconciliation_refuses_invalid_counters_before_repair(value):
    original = [{"next_sequence": 0}, {"read_generation": value, "next_sequence": 7}]
    driver = _GenerationDriver(original)
    with pytest.raises(migrate_module.CutoverRefused, match="invalid or exhausted"):
        migrate_module.reconcile_generations(driver)
    assert driver.worlds == original
    assert driver.repairs == driver.commits == 0
    assert driver.transactions == driver.rollbacks == 1


@pytest.mark.parametrize(
    "failure",
    ["post_repair_failure", "post_repair_null", "post_repair_extra_world", "commit_failure"],
)
def test_quiescent_reconciliation_rolls_back_failure_without_retry(failure):
    original = [{"next_sequence": 3}, {"read_generation": 19}]
    driver = _GenerationDriver(original)
    setattr(driver, failure, True)
    with pytest.raises(RuntimeError):
        migrate_module.reconcile_generations(driver)
    assert driver.worlds == original
    assert driver.transactions == driver.rollbacks == driver.repairs == 1
    assert driver.commits == (1 if failure == "commit_failure" else 0)


def test_quiescent_reconciliation_accepts_empty_store():
    driver = _GenerationDriver([])
    assert migrate_module.reconcile_generations(driver) == {
        "worlds": 0,
        "initialized": 0,
        "missing": 0,
    }
    assert driver.commits == 1
