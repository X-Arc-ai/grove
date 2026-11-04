"""Test data for t_ worlds, so tests outside tests/writer hold no write Cypher."""

import hashlib
import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from neo4j import Driver, Transaction

from grove import clock
from grove.ids import new_id
from grove.store.catalogue import WORLD_LABELS
from grove.store.connection import DATABASE
from grove.store.primitives import PRIMITIVE_WORLD
from grove.store.query import CYPHER_PREFIX, collect
from grove.writer.worlds import create_world

_TEST_WORLD = re.compile(r"t_[a-z0-9-]+_[0-9a-f-]{32,36}")
_WORLD_LABEL = "World"
_PLANTED_FOR = "planted_for"
_PURGE_BATCH = 1000


def refuse_real_world(world_id: str) -> None:
    """Raise ValueError unless world_id names a t_ test world."""
    if _TEST_WORLD.fullmatch(world_id) is None:
        raise ValueError("test helpers work on t_ worlds only")


def plant_node(
    driver: Driver, world_id: str, label: str, *, primitive: bool = False, **props: Any
) -> dict[str, Any]:
    """`label` may join labels with ':'. World takes the world's own row, with no world_id.

    primitive plants a row of the store-wide sentinel world and ties it to this test world, so
    the purge of the test world removes it.
    """
    refuse_real_world(world_id)
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
    refuse_real_world(world_id)
    refuse_real_world(to_world or world_id)
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


def advance_version(driver: Driver, world_id: str, entity: str, entity_id: str) -> None:
    """Simulate an intervening update of a planted item or link in a t_ world."""
    refuse_real_world(world_id)
    queries = {
        "item": "MATCH (n:Item {world_id: $world, id: $id}) ",
        "link": "MATCH ()-[n:LINK {world_id: $world, id: $id}]->() ",
    }
    if entity not in queries:
        raise ValueError("only a planted item or link can advance")
    with driver.session(database=DATABASE) as session:
        row = session.run(
            "CYPHER 25 "
            + queries[entity]
            + "SET n.version = n.version + 1 RETURN n.version AS version",
            world=world_id,
            id=entity_id,
        ).single()
    if row is None:
        raise ValueError("no planted entity in this test world")


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
    refuse_real_world(world_id)
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
    refuse_real_world(world_id)
    with driver.session(database=DATABASE) as session:
        session.run(_SET_SELF_ITEM, world_id=world_id, item_id=item_id).consume()


# A client ages only through a grant of a test world, so no real client can change here. The grant
# comes first because a client that no grant names may be deleted by any run's cleanup once it is
# old: make it fresh, consent, then age it.
_AGE_GRANTED_CLIENT = (
    "CYPHER 25 MATCH (g:OAuthGrant {client_id: $client_id, world_id: $world_id}) "
    "MATCH (c:OAuthClient {client_id: $client_id}) "
    "SET c.registered_at = $at, "
    "c.fetched_at = CASE WHEN c.fetched_at IS NULL THEN NULL ELSE $at END "
    "RETURN c.client_id AS client_id"
)


def age_granted_client(driver: Driver, world_id: str, client_id: str, at: datetime) -> None:
    """Move back the registration time, and a document's fetch time, of a client that a grant of
    this test world names. Raises ValueError when no such grant and client exist."""
    refuse_real_world(world_id)
    with driver.session(database=DATABASE) as session:
        record = session.run(
            _AGE_GRANTED_CLIENT, client_id=client_id, world_id=world_id, at=at
        ).single()
    if record is None:
        raise ValueError("no grant of this test world names that client")


# Deletes that no writer function makes, for a test that races a lock against a deleted row.
DROP_TOKEN = "MATCH (t:OAuthToken {token_hash: $token_hash}) DETACH DELETE t"
DROP_GRANT = "MATCH (g:OAuthGrant {id: $grant_id}) DETACH DELETE g"

_HELD_TAG = "held_by_test"
_SHOW_TRANSACTIONS = "CYPHER 25 SHOW TRANSACTIONS YIELD transactionId, status, metaData"
_TRANSACTION_ID = re.compile(r"neo4j-transaction-\d+")
_POLL_SECONDS = 0.02


class HeldTransaction:
    """An open write transaction, so a test can make another transaction wait on its locks and say
    when they end. The statements are the writer's own: `run` adds what its `_rows` adds."""

    def __init__(self, driver: Driver, tx: Transaction, tag: str) -> None:
        self.driver = driver
        self.tx = tx
        self._tag = tag

    def run(self, cypher: str, **params: Any) -> list[dict]:
        return collect(self.tx.run(CYPHER_PREFIX + cypher, params), None)

    def commit(self) -> None:
        self.tx.commit()

    def blocks(self, done: Callable[[], bool], *, wait_seconds: float = 20.0) -> bool:
        """True as soon as the store shows another transaction blocked by this one. False when
        `done` says the other work ended first, or after `wait_seconds`."""
        deadline = clock.monotonic() + wait_seconds
        while clock.monotonic() < deadline:
            if done():
                return False
            with self.driver.session(database=DATABASE) as session:
                rows = session.run(_SHOW_TRANSACTIONS).data()
            own = [
                row["transactionId"]
                for row in rows
                if (row["metaData"] or {}).get(_HELD_TAG) == self._tag
            ]
            if own and any(own[0] in _TRANSACTION_ID.findall(row["status"] or "") for row in rows):
                return True
            time.sleep(_POLL_SECONDS)
        return False


@contextmanager
def held_transaction(driver: Driver) -> Iterator[HeldTransaction]:
    """A write transaction that stays open until the test commits it; else it rolls back."""
    tag = new_id()
    with driver.session(database=DATABASE) as session:
        tx = session.begin_transaction(metadata={_HELD_TAG: tag})
        try:
            yield HeldTransaction(driver, tx, tag)
        finally:
            if not tx.closed():
                tx.rollback()


def purge_test_account(driver: Driver, email_hash: str) -> None:
    """The user of an address, its sessions and its codes. Purge its world with purge_test_world."""
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 OPTIONAL MATCH (u:User {email_hash: $email_hash}) "
            "OPTIONAL MATCH (s:Session {user_id: u.id}) DETACH DELETE s, u",
            email_hash=email_hash,
        ).consume()
        session.run(
            "CYPHER 25 MATCH (c:EmailCode {email_hash: $email_hash}) DETACH DELETE c",
            email_hash=email_hash,
        ).consume()


def seed_test_world(driver: Driver, world_id: str, *, owner_user_id: str) -> dict[str, Any]:
    """create_world for a t_ world: the :World row and the starter set, as a real world has."""
    refuse_real_world(world_id)
    return create_world(driver, world_id, owner_user_id=owner_user_id)


def purge_test_world(driver: Driver, world_id: str) -> None:
    refuse_real_world(world_id)
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
