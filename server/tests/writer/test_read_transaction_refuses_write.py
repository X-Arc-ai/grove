"""Every read runs in a read transaction, and Neo4j refuses a write there."""

import pytest

from grove.store.connection import DATABASE
from grove.store.errors import Neo4jError
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer.testing import plant_node

ACCESS_MODE = "Neo.ClientError.Statement.AccessMode"
# Never a real name: if a read transaction ever let a schema write through, no real schema moves.
NAME_ON_PURPOSE = "t_foundation_read_refusal"


def count_marked(driver, marker):
    with driver.session(database=DATABASE) as session:
        return session.run(
            "MATCH (n {marker: $marker}) RETURN count(n) AS c", marker=marker
        ).single()["c"]


def schema_names(driver):
    with driver.session(database=DATABASE) as session:
        names = {row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name")}
        return names | {row["name"] for row in session.run("SHOW INDEXES YIELD name")}


def test_raw_data_write_in_a_read_transaction_is_refused(driver, world):
    def work(tx):
        tx.run("CREATE (:Item {world_id: $w, marker: $w})", w=world).consume()

    with driver.session(database=DATABASE) as session:
        with pytest.raises(Neo4jError) as refused:
            session.execute_read(work)

    assert refused.value.code == ACCESS_MODE
    assert count_marked(driver, world) == 0


@pytest.mark.parametrize(
    "statement",
    [
        f"CREATE CONSTRAINT {NAME_ON_PURPOSE} FOR (n:Item) REQUIRE n.marker IS UNIQUE",
        f"CREATE INDEX {NAME_ON_PURPOSE} FOR (n:Item) ON (n.marker)",
        f"DROP CONSTRAINT {NAME_ON_PURPOSE} IF EXISTS",
    ],
)
def test_raw_schema_write_in_a_read_transaction_is_refused(driver, migrated, statement):
    before = schema_names(driver)

    def work(tx):
        tx.run(statement).consume()

    with driver.session(database=DATABASE) as session:
        with pytest.raises(Neo4jError) as refused:
            session.execute_read(work)

    assert refused.value.code == ACCESS_MODE
    assert schema_names(driver) == before


@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (:Item {marker: $marker})",
        "MERGE (:Item {marker: $marker})",
    ],
)
def test_world_query_read_with_a_create_clause_fails_and_leaves_no_row(driver, world, cypher):
    with pytest.raises(Neo4jError) as refused:
        WorldQuery(driver, world).read(cypher, marker=world)
    assert refused.value.code == ACCESS_MODE
    assert count_marked(driver, world) == 0


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (i:Item {id: $id}) SET i.title = 'changed'",
        "MATCH (i:Item {id: $id}) REMOVE i.title",
        "MATCH (i:Item {id: $id}) DELETE i",
        "MATCH (i:Item {id: $id}) DETACH DELETE i",
    ],
)
def test_world_query_read_with_an_update_clause_fails_and_changes_nothing(driver, world, cypher):
    row = plant_node(driver, world, "Item", title="kept", marker=world)

    with pytest.raises(Neo4jError) as refused:
        WorldQuery(driver, world).read(cypher, id=row["id"])

    assert refused.value.code == ACCESS_MODE
    rows = WorldQuery(driver, world).read("MATCH (i:Item {id: $id}) RETURN i", id=row["id"])
    assert rows[0]["i"]["title"] == "kept"


def test_platform_query_read_with_a_write_clause_fails(driver, world):
    plant_node(driver, world, "World", next_sequence=3)

    with pytest.raises(Neo4jError) as refused:
        PlatformQuery(driver).read("MATCH (w:World {id: $id}) SET w.next_sequence = 9", id=world)

    assert refused.value.code == ACCESS_MODE
    rows = PlatformQuery(driver).read("MATCH (w:World {id: $id}) RETURN w", id=world)
    assert rows[0]["w"]["next_sequence"] == 3
