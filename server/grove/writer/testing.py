"""Test data for t_ worlds, so tests outside tests/writer hold no write Cypher."""

import re
from typing import Any

from neo4j import Driver

from grove.ids import new_id
from grove.store.catalogue import WORLD_LABELS
from grove.store.connection import DATABASE
from grove.store.primitives import PRIMITIVE_WORLD

_TEST_WORLD = re.compile(r"t_[a-z0-9-]+_[0-9a-f-]{32,36}")
_WORLD_LABEL = "World"
_PLANTED_FOR = "planted_for"
_PURGE_BATCH = 1000


def _refuse_real_world(world_id: str) -> None:
    if _TEST_WORLD.fullmatch(world_id) is None:
        raise ValueError("test helpers work on t_ worlds only")


def plant_node(
    driver: Driver, world_id: str, label: str, *, primitive: bool = False, **props: Any
) -> dict[str, Any]:
    """`label` may join labels with ':'. World takes the world's own row, with no world_id.

    primitive plants a row of the store-wide sentinel world and ties it to this test world, so
    the purge of the test world removes it.
    """
    _refuse_real_world(world_id)
    # Every label is checked against the catalogue, so splicing it into the text is safe.
    labels = label.split(":")
    if labels == [_WORLD_LABEL]:
        row = {**props, "id": world_id}
    elif set(labels) <= WORLD_LABELS:
        row = {"id": new_id(world_id), **props, "world_id": world_id}
        if primitive:
            row |= {"world_id": PRIMITIVE_WORLD, _PLANTED_FOR: world_id}
    else:
        raise ValueError(f"not a world label: {label}")

    with driver.session(database=DATABASE) as session:
        session.run(f"CYPHER 25 CREATE (n:{label}) SET n = $row", row=row).consume()
    return row


def plant_link(
    driver: Driver,
    world_id: str,
    from_id: str,
    to_id: str,
    *,
    to_world: str | None = None,
    **props: Any,
) -> dict[str, Any]:
    """to_world plants a link that crosses worlds, which only a test of the wall may want."""
    _refuse_real_world(world_id)
    _refuse_real_world(to_world or world_id)
    row = {
        "id": new_id(world_id),
        **props,
        "world_id": world_id,
        "from_item": from_id,
        "to_item": to_id,
    }
    with driver.session(database=DATABASE) as session:
        record = session.run(
            "CYPHER 25 MATCH (a:Item {world_id: $world_id, id: $from_id}) "
            "MATCH (b:Item {world_id: $to_world, id: $to_id}) "
            "CREATE (a)-[l:LINK]->(b) SET l = $row RETURN l.id AS id",
            world_id=world_id,
            to_world=to_world or world_id,
            from_id=from_id,
            to_id=to_id,
            row=row,
        ).single()
    if record is None:
        raise ValueError("an endpoint of the link is not planted")
    return row


def purge_test_world(driver: Driver, world_id: str) -> None:
    _refuse_real_world(world_id)
    with driver.session(database=DATABASE) as session:
        for key in ("world_id", _PLANTED_FOR):
            while True:
                deleted = session.run(
                    f"CYPHER 25 MATCH (n {{{key}: $world_id}}) WITH n LIMIT $batch "
                    "DETACH DELETE n RETURN count(*) AS deleted",
                    world_id=world_id,
                    batch=_PURGE_BATCH,
                ).single()["deleted"]
                if deleted == 0:
                    break
        session.run(
            "CYPHER 25 MATCH (w:World {id: $world_id}) DETACH DELETE w", world_id=world_id
        ).consume()
