"""Upload-only tokens (A.3's short-lived tokens bound to one world, for one route).

An operator mints one on the server for one world (`python -m grove.ingest token mint`). The
token serves `POST /v1/sources/upload` only. Only its sha256 is stored; the raw token is shown
once. These are platform rows (S20): only this module writes them.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from neo4j import Driver, ManagedTransaction, unit_of_work

from grove import clock
from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.query import CYPHER_PREFIX, collect
from grove.store.scope import check_world_id

TOKEN_PREFIX = "gut_"
DEFAULT_MINUTES = 60
MAX_MINUTES = 24 * 60
_TIMEOUT = 30.0

_FIND_WORLD = "MATCH (w:World {id: $world_id}) RETURN w.owner_user_id AS owner"
_ADD_TOKEN = "CREATE (t:UploadToken) SET t += $row"
_REVOKE = (
    "MATCH (t:UploadToken {id: $id}) "
    "SET t.revoked_at = coalesce(t.revoked_at, $at) RETURN t.revoked_at AS revoked_at"
)


class UnknownWorld(LookupError):
    """No world has this id, so no token can serve it."""


@dataclass(frozen=True)
class MintedToken:
    id: str
    token: str
    world_id: str
    expires_at: datetime

    def __repr__(self) -> str:
        # The raw token never reaches a log through a repr.
        return f"MintedToken(id={self.id}, expires_at={self.expires_at.isoformat()})"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write[T](driver: Driver, work) -> T:  # type: ignore[no-untyped-def]
    @unit_of_work(timeout=_TIMEOUT)
    def unit(tx: ManagedTransaction) -> T:
        return work(tx)

    with driver.session(database=DATABASE) as session:
        return session.execute_write(unit)


def _rows(tx: ManagedTransaction, cypher: str, **params: object) -> list[dict]:
    return collect(tx.run(CYPHER_PREFIX + cypher, params), None)


def mint_upload_token(
    driver: Driver, world_id: str, *, minutes: int = DEFAULT_MINUTES
) -> MintedToken:
    """A new token for one world, valid `minutes` from the clock's now (1 to MAX_MINUTES)."""
    world_id = check_world_id(world_id)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be a whole number from 1 to {MAX_MINUTES}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = clock.now()
    row = {
        "id": new_id(),
        "world_id": world_id,
        "token_hash": token_hash(token),
        "created_at": now,
        "expires_at": now + timedelta(minutes=minutes),
    }

    def work(tx: ManagedTransaction) -> None:
        found = _rows(tx, _FIND_WORLD, world_id=world_id)
        if not found or not isinstance(found[0]["owner"], str):
            raise UnknownWorld("no world has this id")
        tx.run(CYPHER_PREFIX + _ADD_TOKEN, row={**row, "user_id": found[0]["owner"]}).consume()

    _write(driver, work)
    return MintedToken(row["id"], token, world_id, row["expires_at"])


def revoke_upload_token(driver: Driver, token_id: str) -> bool:
    """Sets revoked_at once; a second revoke keeps the first time. False for an unknown id."""
    if not isinstance(token_id, str) or not token_id:
        raise ValueError("token_id is the id that mint printed")
    at = clock.now()

    def work(tx: ManagedTransaction) -> bool:
        return bool(_rows(tx, _REVOKE, id=token_id, at=at))

    return _write(driver, work)
