"""The writer entry, live."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.errors import Neo4jError, ScopeError, WorldLeak
from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node
from grove.writer.tx import UnknownWorld, WorldWriter

WRITERS = 8
TRANSACTIONS = 25


def raw_one(driver, cypher, **params):
    with driver.session(database=DATABASE) as session:
        return session.run(cypher, **params).single()


def count_items(driver, world):
    return raw_one(driver, "MATCH (i:Item {world_id: $w}) RETURN count(i) AS c", w=world)["c"]


def sequences(driver, world):
    with driver.session(database=DATABASE) as session:
        rows = session.run(
            "MATCH (c:ChangeEntry {world_id: $w}) RETURN c.sequence AS s ORDER BY s", w=world
        )
        return [row["s"] for row in rows]


@pytest.fixture
def world_row(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    return world


def test_created_nodes_and_links_carry_the_world(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    a, b, link = new_id(), new_id(), new_id()

    def work(tx):
        return tx.run(
            "CREATE (a:Item {id: $a}) CREATE (b:Item {id: $b}) "
            "CREATE (a)-[l:LINK {id: $link}]->(b) RETURN a, l",
            a=a,
            b=b,
            link=link,
        )

    rows = WorldWriter(driver, world).transaction(work)

    assert rows[0]["a"]["world_id"] == world
    assert rows[0]["l"]["id"] == link
    for item in (a, b):
        row = raw_one(driver, "MATCH (i:Item {id: $id}) RETURN i.world_id AS w", id=item)
        assert row["w"] == world
    row = raw_one(driver, "MATCH ()-[l:LINK {id: $id}]->() RETURN l.world_id AS w", id=link)
    assert row["w"] == world


def test_merge_stamps_and_finds_the_same_row(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    item = new_id()

    def work(tx):
        return tx.run("MERGE (i:Item {id: $id}) RETURN i", id=item)

    writer = WorldWriter(driver, world)
    writer.transaction(work)
    writer.transaction(work)

    assert count_items(driver, world) == 1


def test_an_update_reaches_only_the_own_world(driver, make_world):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    plant_node(driver, a, "Item", title="same")
    other = plant_node(driver, b, "Item", title="same")

    WorldWriter(driver, a).transaction(
        lambda tx: tx.run("MATCH (i:Item {title: 'same'}) SET i.title = 'changed'")
    )

    row = raw_one(driver, "MATCH (i:Item {id: $id}) RETURN i.title AS t", id=other["id"])
    assert row["t"] == "same"


def test_a_writer_cannot_touch_the_other_worlds_link(driver, make_world):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    first = plant_node(driver, b, "Item")["id"]
    second = plant_node(driver, b, "Item")["id"]
    link = plant_link(driver, b, first, second)

    WorldWriter(driver, a).transaction(
        lambda tx: tx.run("MATCH ()-[l:LINK {id: $id}]->() DELETE l", id=link["id"])
    )

    row = raw_one(driver, "MATCH ()-[l:LINK {id: $id}]->() RETURN count(l) AS c", id=link["id"])
    assert row["c"] == 1


def test_a_failed_work_function_rolls_the_whole_batch_back(driver, world):
    plant_node(driver, world, "World", next_sequence=0)

    def work(tx):
        tx.run("CREATE (:Item {id: $id})", id=new_id())
        tx.run("CREATE (:Item {id: $id})", id=new_id())
        raise RuntimeError("the work failed")

    with pytest.raises(RuntimeError, match="the work failed"):
        WorldWriter(driver, world).transaction(work)

    assert count_items(driver, world) == 0


def test_a_refused_statement_rolls_the_batch_back(driver, world):
    plant_node(driver, world, "World", next_sequence=0)

    def work(tx):
        tx.run("CREATE (:Item {id: $id})", id=new_id())
        tx.run("MATCH (i:Item) SET i.world_id = 'elsewhere'")

    with pytest.raises(ScopeError):
        WorldWriter(driver, world).transaction(work)

    assert count_items(driver, world) == 0


def test_a_broken_constraint_rolls_the_batch_back(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    same = new_id()

    def work(tx):
        tx.run("CREATE (:Item {id: $id})", id=new_id())
        tx.run("CREATE (:Item {id: $id})", id=same)
        tx.run("CREATE (:Item {id: $id})", id=same)

    with pytest.raises(Exception, match="already exists"):
        WorldWriter(driver, world).transaction(work)

    assert count_items(driver, world) == 0


def test_a_caller_world_id_is_refused(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    with pytest.raises(ScopeError):
        WorldWriter(driver, world).transaction(
            lambda tx: tx.run("CREATE (:Item {id: $id})", id=new_id(), world_id="elsewhere")
        )
    assert count_items(driver, world) == 0


def test_a_nested_world_id_in_a_row_map_is_refused(driver, world):
    plant_node(driver, world, "World", next_sequence=0)
    item = plant_node(driver, world, "Item")["id"]
    with pytest.raises(ScopeError):
        WorldWriter(driver, world).transaction(
            lambda tx: tx.run(
                "MATCH (i:Item {id: $id}) SET i += $row",
                id=item,
                row={"title": "x", "world_id": "elsewhere"},
            )
        )
    row = raw_one(driver, "MATCH (i:Item {id: $id}) RETURN i.world_id AS w", id=item)
    assert row["w"] == world


def test_a_write_results_guard_sees_another_world(driver, make_world):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    start = plant_node(driver, a, "Item")["id"]
    end = plant_node(driver, b, "Item")["id"]
    plant_link(driver, a, start, end, to_world=b)

    with pytest.raises(WorldLeak):
        WorldWriter(driver, a).transaction(
            lambda tx: tx.run("MATCH (i:Item)-[:LINK]->(x) RETURN x")
        )


def test_bad_world_ids_are_refused(driver):
    for bad in ("", "a b", "a'b"):
        with pytest.raises(ScopeError):
            WorldWriter(driver, bad)


def test_next_sequence_starts_at_one_and_counts_up(driver, world_row):
    writer = WorldWriter(driver, world_row)
    assert writer.transaction(lambda tx: tx.next_sequence()) == 1
    assert writer.transaction(lambda tx: (tx.next_sequence(), tx.next_sequence())) == (2, 3)


def test_next_sequence_on_a_world_with_no_row_fails(driver, world):
    with pytest.raises(UnknownWorld):
        WorldWriter(driver, world).transaction(lambda tx: tx.next_sequence())


def _write_entry(tx):
    sequence = tx.next_sequence()
    tx.run("CREATE (:ChangeEntry {sequence: $sequence})", sequence=sequence)
    return sequence


def test_next_sequence_is_gapless_and_ordered_under_concurrent_writers(driver, world_row):
    def writer_thread(_):
        writer = WorldWriter(driver, world_row)
        return [writer.transaction(_write_entry) for _ in range(TRANSACTIONS)]

    # A constraint conflict is a client error: the driver never retries it, so it raises here.
    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        per_writer = list(pool.map(writer_thread, range(WRITERS)))

    total = WRITERS * TRANSACTIONS
    for own in per_writer:
        assert own == sorted(own)
        assert len(set(own)) == len(own)
    handed_out = sorted(value for own in per_writer for value in own)
    assert handed_out == list(range(1, total + 1))
    assert sequences(driver, world_row) == list(range(1, total + 1))
    row = raw_one(driver, "MATCH (w:World {id: $id}) RETURN w.next_sequence AS n", id=world_row)
    assert row["n"] == total


def test_the_sequence_lock_holds_until_commit(driver, world_row):
    """Sequence order is commit order: a second writer waits for the first to commit."""
    first_has_sequence = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    results = {}

    def first():
        def work(tx):
            value = tx.next_sequence()
            first_has_sequence.set()
            assert release_first.wait(20)
            return value

        results["first"] = WorldWriter(driver, world_row).transaction(work)

    def second():
        assert first_has_sequence.wait(20)
        results["second"] = WorldWriter(driver, world_row).transaction(
            lambda tx: tx.next_sequence()
        )
        second_done.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    assert first_has_sequence.wait(20)
    waited = second_done.wait(1.0)
    release_first.set()
    for thread in threads:
        thread.join(30)

    assert waited is False
    assert results == {"first": 1, "second": 2}


def test_the_timeout_ends_a_writer_blocked_on_the_sequence_lock(driver, world_row):
    has_lock = threading.Event()
    release = threading.Event()

    def holder():
        def work(tx):
            tx.next_sequence()
            has_lock.set()
            assert release.wait(20)

        WorldWriter(driver, world_row).transaction(work)

    thread = threading.Thread(target=holder)
    thread.start()
    assert has_lock.wait(20)
    try:
        with pytest.raises(Neo4jError) as failed:
            WorldWriter(driver, world_row, timeout=0.2).transaction(lambda tx: tx.next_sequence())
    finally:
        release.set()
        thread.join(30)

    assert failed.value.code == "Neo.ClientError.Transaction.LockClientStopped"
    row = raw_one(driver, "MATCH (w:World {id: $id}) RETURN w.next_sequence AS n", id=world_row)
    assert row["n"] == 1


def test_a_rollback_leaves_no_gap(driver, world_row):
    writer = WorldWriter(driver, world_row)
    assert writer.transaction(_write_entry) == 1

    def fails(tx):
        _write_entry(tx)
        raise RuntimeError("the work failed")

    with pytest.raises(RuntimeError):
        writer.transaction(fails)

    assert writer.transaction(_write_entry) == 2
    assert sequences(driver, world_row) == [1, 2]


def test_two_worlds_have_independent_sequences(driver, make_world):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    plant_node(driver, b, "World", next_sequence=0)

    for _ in range(3):
        WorldWriter(driver, a).transaction(_write_entry)
    WorldWriter(driver, b).transaction(_write_entry)

    assert sequences(driver, a) == [1, 2, 3]
    assert sequences(driver, b) == [1]
    assert WorldQuery(driver, b).read("MATCH (c:ChangeEntry) RETURN c.sequence AS s") == [{"s": 1}]


# Fix round 2: names spelled like keywords and lone carriage returns hid write clauses.

WRITE_REPROS = [
    "MATCH (a:Item {id:$id}) WITH a ORDER BY a.ends WITH 1 AS z MATCH (a {id:$bid}) SET a.x=7",
    "MATCH (a:Item {id:$id}) WITH a, 1 AS ends WITH a, ends WITH 1 AS z MATCH (a {id:$bid}) "
    "SET a.x=7",
    "MATCH (a:Item {id:$id}) WITH a, a.n AS Starts WITH a MATCH (b {id:$bid}) SET b.x=7",
    "WITH 1 AS as MATCH (n {id:$bid}) DETACH DELETE n",
    "WITH 1 AS AS MATCH (n {id:$bid}) DETACH DELETE n",
    "with 1 as As match (n {id:$bid}) detach delete n",
    "WITH 1 AS as CREATE (n:Item {id:$nid, title:'REVIEWNEW'})",
    "MATCH (n:Item {id:$id}) WITH n, 1 AS as SET n.world_id = 'review-moved'",
    "MATCH (n:Item {id:$id}) WITH n, 1 AS as REMOVE n.world_id",
    "MATCH (n:Item {id:$id}) WITH n, 1 AS as SET n = {title: 'x'}",
    "MATCH (a:Item {id:$id}) WHERE true OR a.as MATCH (b {id:$bid}) SET b.x=7",
    "MATCH (a:Item {id:$id}) // c\rMATCH (b {id:$bid}) SET b.x=7 //\n",
    "MATCH (a:Item {id:$id}) // c\rMATCH (b {id:$bid}) DETACH DELETE b",
    "MATCH (a:Item {id:$id})\t//\tc\rCREATE (n:Item {id:$nid}) SET n.world_id = 'review-moved'",
]


class _RolledBack(Exception):
    pass


def _snapshot(driver, ids):
    with driver.session(database=DATABASE) as session:
        rows = session.run(
            "MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id, properties(i) AS p ORDER BY id",
            ids=ids,
        )
        return [(row["id"], row["p"]) for row in rows]


@pytest.mark.parametrize("cypher", WRITE_REPROS)
def test_a_hidden_write_clause_is_refused_and_changes_nothing(driver, make_world, cypher):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    mine = plant_node(driver, a, "Item", title="own", n=1)["id"]
    theirs = plant_node(driver, b, "Item", title="theirs", n=1)["id"]
    plant_link(driver, b, theirs, plant_node(driver, b, "Item")["id"])
    fresh = new_id()
    before = _snapshot(driver, [mine, theirs, fresh])

    def work(tx):
        tx.run(cypher, id=mine, bid=theirs, nid=fresh)
        # Reached only when the scoper let the text through: undo it, whatever it did.
        raise _RolledBack

    with pytest.raises(ScopeError):
        WorldWriter(driver, a).transaction(work)

    assert _snapshot(driver, [mine, theirs, fresh]) == before


def test_the_keywords_still_write_in_the_own_world_only(driver, make_world):
    a, b = make_world(), make_world()
    plant_node(driver, a, "World", next_sequence=0)
    plant_node(driver, a, "Item", title="own heron", n=1)
    theirs = plant_node(driver, b, "Item", title="own heron", n=1)["id"]

    def work(tx):
        tx.run("MATCH (i:Item) WHERE i.title STARTS WITH 'own' SET i.x = 1")
        tx.run("match (i:Item) where i.title ends with 'heron' set i.y = 2")
        tx.run("MATCH (i:Item) WITH i, i.n AS m WITH i, m SET i.z = m // done\rSET i.w = 3")
        return tx.run("MATCH (i:Item) RETURN i.x AS x, i.y AS y, i.z AS z, i.w AS w")

    assert WorldWriter(driver, a).transaction(work) == [{"x": 1, "y": 2, "z": 1, "w": 3}]
    other = raw_one(driver, "MATCH (i:Item {id: $id}) RETURN properties(i) AS p", id=theirs)
    assert set(other["p"]) == {"id", "world_id", "title", "n"}


@pytest.mark.parametrize("timeout", [0, -1, True, None, "5", float("nan"), float("inf")])
def test_a_writer_refuses_a_timeout_that_is_not_a_positive_number(timeout):
    with pytest.raises(ScopeError, match="timeout"):
        WorldWriter(object(), "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", timeout=timeout)
