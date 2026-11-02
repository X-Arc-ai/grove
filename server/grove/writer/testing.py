"""Test data for t_ worlds, so tests outside tests/writer hold no write Cypher."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.store.catalogue import WORLD_LABELS
from grove.store.connection import DATABASE
from grove.store.primitives import PRIMITIVE_WORLD
from grove.writer.worlds import create_world

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


_SESSION_LIFETIME = timedelta(hours=1)
# The :World row of a test world: made on first use, its owner set on every call. The id is the
# key of the uniqueness constraint, so two calls for one world find one row.
_OWN_WORLD = (
    "CYPHER 25 MERGE (w:World {id: $world_id}) "
    "ON CREATE SET w.created_at = $at, w.next_sequence = 0 "
    "SET w.owner_user_id = $user_id"
)
_PLANT_USER = (
    "CYPHER 25 MERGE (u:User {id: $user_id}) "
    "ON CREATE SET u.created_at = $at, u.planted_for = $world_id"
)
_PLANT_SESSION = "CYPHER 25 CREATE (s:Session) SET s = $row"
_SET_SELF_ITEM = "CYPHER 25 MATCH (w:World {id: $world_id}) SET w.self_item_id = $item_id"


def session_token_hash(token: str) -> str:
    """What the signin contract stores for a session cookie: the lowercase sha256 hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def plant_session(
    driver: Driver,
    world_id: str,
    *,
    user_id: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    self_item_id: str | None = None,
) -> str:
    """Plant a :User that owns this test world and a :Session of that user; return the raw token.

    The token is fresh random text; only its hash is stored. expires_at defaults to one hour after
    the clock's now. The user and the session carry planted_for, so the purge removes them.
    """
    _refuse_real_world(world_id)
    at = clock.now()
    user_id = user_id or new_id()
    token = secrets.token_urlsafe(32)
    row = {
        "id": new_id(),
        "user_id": user_id,
        "token_hash": session_token_hash(token),
        "created_at": at,
        "expires_at": expires_at or at + _SESSION_LIFETIME,
        _PLANTED_FOR: world_id,
    }
    if revoked_at is not None:
        row["revoked_at"] = revoked_at
    with driver.session(database=DATABASE) as session:
        session.run(_PLANT_USER, user_id=user_id, at=at, world_id=world_id).consume()
        session.run(_OWN_WORLD, world_id=world_id, user_id=user_id, at=at).consume()
        session.run(_PLANT_SESSION, row=row).consume()
    if self_item_id is not None:
        set_self_item(driver, world_id, self_item_id)
    return token


def set_self_item(driver: Driver, world_id: str, item_id: str | None) -> None:
    """Set or clear World.self_item_id, the column signin owns, on a test world's row."""
    _refuse_real_world(world_id)
    with driver.session(database=DATABASE) as session:
        session.run(_SET_SELF_ITEM, world_id=world_id, item_id=item_id).consume()


def seed_test_world(driver: Driver, world_id: str, *, owner_user_id: str) -> dict[str, Any]:
    """create_world for a t_ world: the :World row and the starter set, as a real world has."""
    _refuse_real_world(world_id)
    return create_world(driver, world_id, owner_user_id=owner_user_id)


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
