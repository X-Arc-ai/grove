"""Sign-in's writes (S20, S40): users, sessions, email codes and the person's own item.

An address is never stored in any row, log or error: a row holds its salted hash. The same hash
comes from a Google sign-in and from an email code, so one person has one world by either method.

The own item is made once, even when two first sign-ins race and after a crash between any two
steps. Everything before its transaction may run twice and leaves at most a spare source row. Its
transaction takes the world's lock first and reads World.self_item_id again under that lock, so the
second caller stops there.
"""

import hashlib
import hmac
import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from neo4j import Driver, ManagedTransaction, unit_of_work
from neo4j.exceptions import ConstraintError

from grove import clock
from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.query import CYPHER_PREFIX, PlatformQuery, collect
from grove.writer.apply import apply_ops
from grove.writer.caller import Caller
from grove.writer.errors import BatchDeclined
from grove.writer.history import Stamp
from grove.writer.ops import shape_refusals
from grove.writer.sources import record_episode, record_source
from grove.writer.tx import WorldTx, WorldWriter
from grove.writer.worlds import create_world

METHODS = ("google", "email")
NEUTRAL_TITLE = "You"
MAX_TITLE_LENGTH = 120
PARSER_VERSION = "signin.v1"
CONNECTOR_TYPE = "person_in_views"
OWN_ITEM_REASON = "the person's own item, made at first sign-in"

_CALLER = Caller(kind="person", scopes={"grove.read", "grove.write"}, actor="person:signin")
_EMAIL_SALT = "grove-email-v1:"
# A fixed namespace: the world id follows from the user id, so two first sign-ins make one world.
_WORLD_NAMESPACE = UUID("6f0b6a54-3d0e-4c55-9a53-5d1c7c2e8f10")
_MESSAGE_KEY = "signin:first"
_CUT_RULE = "signin"
_EV_ID = "e1"
_FIRST_VERSION = 1
_ATTEMPTS = 3
_TIMEOUT = 30.0
_EXPIRED_ROWS_KEPT = timedelta(days=1)
_REMOVED_CATEGORIES = ("Cc", "Cs")

_ENSURE_USER = (
    "MERGE (u:User {email_hash: $email_hash}) "
    "ON CREATE SET u.id = $id, u.created_at = $at "
    "RETURN u.id AS id"
)
# The session contract of docs/views.md: the oldest world of the user.
_OLDEST_WORLD = (
    "MATCH (w:World {owner_user_id: $user_id}) "
    "RETURN w.id AS id, w.self_item_id AS self_item_id "
    "ORDER BY w.created_at, w.id LIMIT 1"
)
_READ_OWN_ITEM = CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) RETURN w.self_item_id AS own"
_SET_OWN_ITEM = CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) SET w.self_item_id = $item_id"
_FIND_CONNECTOR = "MATCH (c:Connector {type: $type}) RETURN c.id AS id ORDER BY c.id LIMIT 1"
_ADD_CONNECTOR = "CREATE (c:Connector) SET c += $row"
_NEW_ITEM = (
    "MATCH (e:ChangeEntry {change_set_id: $change_set_id, entity_type: $entity_type, field: $field}"
    ") RETURN e.entity_id AS id"
)
_ADD_SESSION = "CREATE (s:Session) SET s = $row"
_REVOKE_SESSION = (
    "MATCH (s:Session {token_hash: $token_hash}) WHERE s.revoked_at IS NULL SET s.revoked_at = $at"
)
# Each write below locks the rows, so two calls for one address run one after the other. The lock
# reads no column, as in tx.py: `SET c.attempts = c.attempts` read the count before it waited and
# wrote that stale value back after, so a start at the limit erased a wrong try that committed
# during the wait, and the limit of tries on a code did not hold.
_LOCK_CODES = (
    "MATCH (c:EmailCode {email_hash: $email_hash}) "
    "SET c.lock_marker = true REMOVE c.lock_marker "
    "RETURN c.created_at AS created_at"
)
_END_EARLIER_CODES = (
    "MATCH (c:EmailCode {email_hash: $email_hash}) WHERE c.used_at IS NULL SET c.used_at = $now"
)
_DROP_OLD_CODES = (
    "MATCH (c:EmailCode {email_hash: $email_hash}) WHERE c.expires_at < $before DELETE c"
)
_ADD_CODE = "CREATE (c:EmailCode) SET c = $row"
_NEWEST_LIVE_CODE = (
    "MATCH (c:EmailCode {email_hash: $email_hash}) WHERE c.used_at IS NULL AND c.expires_at > $now "
    "RETURN c.id AS id ORDER BY c.created_at DESC, c.id DESC LIMIT 1"
)
_LOCK_CODE = "MATCH (c:EmailCode {id: $id}) SET c.attempts = c.attempts + 1"
_READ_CODE = (
    "MATCH (c:EmailCode {id: $id}) "
    "RETURN c.used_at AS used_at, c.attempts AS attempts, c.code_hash AS code_hash"
)
_USE_CODE = "MATCH (c:EmailCode {id: $id}) SET c.used_at = $now"


@dataclass(frozen=True)
class Account:
    user_id: str
    world_id: str
    self_item_id: str
    created: bool


@dataclass(frozen=True)
class CodeStored:
    row_id: str


@dataclass(frozen=True)
class TooManyCodes:
    """The address is at its limit; nothing was written."""

    retry_after_seconds: int


def _write[T](driver: Driver, work: Callable[[ManagedTransaction], T]) -> T:
    """One write transaction on platform rows. The driver may run `work` twice."""

    @unit_of_work(timeout=_TIMEOUT)
    def unit(tx: ManagedTransaction) -> T:
        return work(tx)

    with driver.session(database=DATABASE) as session:
        return session.execute_write(unit)


def _rows(tx: ManagedTransaction, cypher: str, **params: Any) -> list[dict]:
    return collect(tx.run(CYPHER_PREFIX + cypher, params), None)


def email_hash(address: str) -> str:
    return hashlib.sha256((_EMAIL_SALT + address.strip().lower()).encode("utf-8")).hexdigest()


def code_hash(row_id: str, hashed_address: str, code: str) -> str:
    return hashlib.sha256(f"{row_id}:{hashed_address}:{code}".encode()).hexdigest()


def default_world_id(user_id: str) -> str:
    return str(uuid5(_WORLD_NAMESPACE, user_id))


def own_title(display_name: object) -> str:
    """The name with control characters and lone surrogates removed and cut, else the neutral word.

    A surrogate is removed too: JSON can write one, and the store's UTF-8 cannot hold it.
    """
    if not isinstance(display_name, str):
        return NEUTRAL_TITLE
    kept = "".join(c for c in display_name if unicodedata.category(c) not in _REMOVED_CATEGORIES)
    return kept.strip()[:MAX_TITLE_LENGTH].strip() or NEUTRAL_TITLE


# The account ---------------------------------------------------------------------------------


def _ensure_user(driver: Driver, hashed_address: str) -> tuple[str, bool]:
    fresh = new_id()

    def work(tx: ManagedTransaction) -> str:
        return _rows(tx, _ENSURE_USER, email_hash=hashed_address, id=fresh, at=clock.now())[0]["id"]

    for attempt in range(_ATTEMPTS):
        try:
            user_id = _write(driver, work)
            return user_id, user_id == fresh
        except ConstraintError:
            # A create that lost a race on the key. The next run finds the winner's row.
            if attempt == _ATTEMPTS - 1:
                raise
    raise AssertionError("the loop returns or raises")


def _ensure_connector(driver: Driver, world_id: str) -> str:
    """One connector per world: the world's lock makes the second caller find the first's row."""

    def work(tx: WorldTx) -> str:
        tx.lock_world()
        found = tx.run(_FIND_CONNECTOR, type=CONNECTOR_TYPE)
        if found:
            return found[0]["id"]
        row = {
            "id": new_id(world_id),
            "type": CONNECTOR_TYPE,
            "version": _FIRST_VERSION,
            "touched_at": clock.now(world_id),
        }
        tx.run(_ADD_CONNECTOR, row=row)
        return row["id"]

    return WorldWriter(driver, world_id).transaction(work)


def _record_first_episode(driver: Driver, world_id: str, connector_id: str, source_id: str) -> str:
    at = clock.now(world_id)
    for attempt in range(_ATTEMPTS):
        try:
            return record_episode(
                driver,
                world_id,
                connector_id=connector_id,
                source_ids=[source_id],
                first_message_key=_MESSAGE_KEY,
                last_message_key=_MESSAGE_KEY,
                start_time=at,
                end_time=at,
                cut_rule=_CUT_RULE,
            )
        except ConstraintError:
            # Two callers made the episode at once. The next run adds this source to the winner's.
            if attempt == _ATTEMPTS - 1:
                raise
    raise AssertionError("the loop returns or raises")


def _own_item_batch(
    title: str, source_id: str, episode_id: str, at: datetime
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    observation = {
        "ev_id": _EV_ID,
        "episode_id": episode_id,
        "source_id": source_id,
        "locator": "whole",
        "message_key": _MESSAGE_KEY,
        "source_time": at.isoformat(),
        "statement_class": "report_state",
        "derivation": "observed",
        "speaker_kind": "person",
        "modality": "text",
    }
    op = {
        "op": "create_item",
        "op_id": "own_item",
        "kind": "person",
        "title": title,
        "became_true": "unknown",
        "evidence_refs": [{"ev_id": _EV_ID, "polarity": "supports"}],
        "reason": OWN_ITEM_REASON,
    }
    return [op], {_EV_ID: observation}


def _create_own_item(
    driver: Driver, world_id: str, title: str, source_id: str, episode_id: str
) -> str:
    """The item and World.self_item_id commit together, or neither does."""

    def work(tx: WorldTx) -> str:
        tx.lock_world()
        managed = tx.unscoped()
        # A read of its own, after the lock: an earlier read may predate another caller's commit.
        record = managed.run(_READ_OWN_ITEM, world_id=world_id).single()
        if record is not None and record["own"]:
            return record["own"]
        at = clock.now(world_id)
        stamp = Stamp(
            world_id=world_id,
            at=at,
            actor=_CALLER.actor,
            commit_id=new_id(world_id),
            change_set_id=new_id(world_id),
        )
        ops, observations = _own_item_batch(title, source_id, episode_id, at)
        faults, by_index = shape_refusals({"ops": ops, "evidence": list(observations.values())})
        if faults:
            raise BatchDeclined(faults, batch_size=len(ops))
        apply_ops(tx, stamp=stamp, ops=ops, observations=observations, pre_refused=by_index)
        item_id = tx.run(
            _NEW_ITEM, change_set_id=stamp.change_set_id, entity_type="item", field="row"
        )[0]["id"]
        managed.run(_SET_OWN_ITEM, world_id=world_id, item_id=item_id).consume()
        return item_id

    return WorldWriter(driver, world_id).transaction(work)


def _ensure_own_item(driver: Driver, world_id: str, *, method: str, title: str) -> str:
    connector_id = _ensure_connector(driver, world_id)
    # A pointer with no text: there is nothing to screen, and a source this short says only how the
    # person arrived. A racer may leave a spare one; it cites nothing and says nothing.
    pointer = f"signin:{method}"
    source_id = record_source(
        driver,
        world_id,
        connector_id=connector_id,
        external_locator=pointer,
        content_pointer=pointer,
        parser_version=PARSER_VERSION,
    )
    episode_id = _record_first_episode(driver, world_id, connector_id, source_id)
    try:
        return _create_own_item(driver, world_id, title, source_id, episode_id)
    except BatchDeclined:
        if title == NEUTRAL_TITLE:
            raise
        return _create_own_item(driver, world_id, NEUTRAL_TITLE, source_id, episode_id)


def ensure_account(
    driver: Driver,
    hashed_address: str,
    *,
    method: str,
    display_name: str | None = None,
    world_id_for: Callable[[str], str] = default_world_id,
) -> Account:
    """The user, its oldest world and its own item, made when absent. world_id_for must give one id
    per user, so that two first sign-ins make one world."""
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    user_id, created = _ensure_user(driver, hashed_address)
    platform = PlatformQuery(driver, timeout=_TIMEOUT)
    worlds = platform.read(_OLDEST_WORLD, user_id=user_id)
    if not worlds:
        create_world(driver, world_id_for(user_id), owner_user_id=user_id)
        worlds = platform.read(_OLDEST_WORLD, user_id=user_id)
    world_id = worlds[0]["id"]
    self_item_id = worlds[0]["self_item_id"] or _ensure_own_item(
        driver, world_id, method=method, title=own_title(display_name)
    )
    return Account(user_id, world_id, self_item_id, created)


# Sessions ------------------------------------------------------------------------------------


def create_session(
    driver: Driver, user_id: str, token_hash: str, *, method: str, expires_at: datetime
) -> str:
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    row = {
        "id": new_id(),
        "user_id": user_id,
        "token_hash": token_hash,
        "method": method,
        "created_at": clock.now(),
        "expires_at": expires_at,
    }

    def work(tx: ManagedTransaction) -> None:
        tx.run(CYPHER_PREFIX + _ADD_SESSION, row=row).consume()

    _write(driver, work)
    return row["id"]


def revoke_session(driver: Driver, token_hash: str, at: datetime) -> None:
    """Sets revoked_at when absent and changes nothing else: logout keeps the world."""

    def work(tx: ManagedTransaction) -> None:
        tx.run(CYPHER_PREFIX + _REVOKE_SESSION, token_hash=token_hash, at=at).consume()

    _write(driver, work)


# Email codes ---------------------------------------------------------------------------------


def store_email_code(
    driver: Driver,
    hashed_address: str,
    code_hash_of: Callable[[str], str],
    *,
    now: datetime,
    ttl: timedelta,
    max_per_window: int,
    window: timedelta,
) -> CodeStored | TooManyCodes:
    """One transaction. code_hash_of takes the new row's id, which salts the hash."""

    def work(tx: ManagedTransaction) -> CodeStored | TooManyCodes:
        rows = _rows(tx, _LOCK_CODES, email_hash=hashed_address)
        recent = sorted(r["created_at"] for r in rows if r["created_at"] > now - window)
        if len(recent) >= max_per_window:
            # The row whose leaving brings the count below the limit: the oldest one, unless a
            # race once took the address past the limit.
            leaves_at = recent[len(recent) - max_per_window] + window
            return TooManyCodes(max(1, math.ceil((leaves_at - now).total_seconds())))
        _rows(tx, _END_EARLIER_CODES, email_hash=hashed_address, now=now)
        _rows(tx, _DROP_OLD_CODES, email_hash=hashed_address, before=now - _EXPIRED_ROWS_KEPT)
        row_id = new_id()
        row = {
            "id": row_id,
            "email_hash": hashed_address,
            "code_hash": code_hash_of(row_id),
            "attempts": 0,
            "created_at": now,
            "expires_at": now + ttl,
        }
        _rows(tx, _ADD_CODE, row=row)
        return CodeStored(row_id)

    return _write(driver, work)


def try_email_code(
    driver: Driver, hashed_address: str, code: str, *, now: datetime, max_attempts: int
) -> bool:
    """One transaction that always commits its attempt: a wrong try returns False, never raises."""

    def work(tx: ManagedTransaction) -> bool:
        found = _rows(tx, _NEWEST_LIVE_CODE, email_hash=hashed_address, now=now)
        if not found:
            return False
        row_id = found[0]["id"]
        _rows(tx, _LOCK_CODE, id=row_id)
        # A read of its own, after the lock: a racer may have used the code since the first read.
        row = _rows(tx, _READ_CODE, id=row_id)[0]
        if row["used_at"] is not None:
            return False
        if row["attempts"] > max_attempts:
            _rows(tx, _USE_CODE, id=row_id, now=now)
            return False
        wanted = code_hash(row_id, hashed_address, code)
        if not hmac.compare_digest(row["code_hash"], wanted):
            return False
        _rows(tx, _USE_CODE, id=row_id, now=now)
        return True

    return _write(driver, work)
