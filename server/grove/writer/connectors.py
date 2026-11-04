"""Connection identity, sealed credentials, OAuth state and sync leases (A.4)."""

import json
from datetime import timedelta
from typing import Any

from grove import clock
from grove.demo_policy import designation_in_tx, has_designation
from grove.ids import new_id
from grove.ingest.connector import message_key
from grove.store.query import WorldQuery
from grove.writer.errors import InvalidRecord
from grove.writer.tx import WorldTx, WorldWriter

LEASE_SECONDS = 600
_FIELDS = frozenset(
    {
        "credential_ciphertext",
        "oauth_state_hash",
        "oauth_expires_at",
        "connection_status",
        "settings",
        "scope",
        "sync_cursor",
        "event_versions",
    }
)


class ConnectionBusy(Exception):
    """A connection is busy, or a reader no longer has current sync authority."""


def connection(driver, world: str, connection_id: str) -> dict:
    rows = WorldQuery(driver, world).read(
        "MATCH (c:Connector {id: $id}) RETURN c", id=connection_id
    )
    if not rows or rows[0]["c"].get("provider") not in ("google_calendar", "git"):
        raise InvalidRecord("not_found", "connector", "no such source connection")
    return rows[0]["c"]


def ensure_connection(driver, world: str, provider: str, external_id: str) -> str:
    if provider not in ("google_calendar", "git"):
        raise InvalidRecord("invalid_arguments", "provider", "unknown source connection")
    identity = message_key("instance", external_id)

    def work(tx: WorldTx):
        tx.lock_world()
        _refuse_demo(tx)
        rows = tx.run(
            "MATCH (c:Connector {provider: $provider, instance_key: $key}) RETURN c.id AS id",
            provider=provider,
            key=identity,
        )
        if rows:
            return rows[0]["id"]
        at = clock.now(world)
        chosen = new_id(world)
        tx.run(
            "CREATE (c:Connector) SET c += $row",
            row={
                "id": chosen,
                "provider": provider,
                "instance_key": identity,
                "type": "calendar" if provider == "google_calendar" else "git",
                "auth_reference": chosen,
                "connection_status": "pending",
                "batch_rule": "event_revision.v1" if provider == "google_calendar" else "commit.v1",
                "expected_read_interval": 300,
                "read_count": 0,
                "failed_count": 0,
                "denied_count": 0,
                "settings": "{}",
                "scope": "[]",
                "version": 1,
                "touched_at": at,
            },
        )
        return chosen

    return WorldWriter(driver, world).transaction(work)


def _refuse_demo(tx: WorldTx) -> None:
    if has_designation(designation_in_tx(tx) or {}):
        raise InvalidRecord(
            "scope_denied", "restricted_demo", "source connections are disabled for this world"
        )


def _clears_authority(columns: dict) -> bool:
    clearing = {
        "credential_ciphertext",
        "oauth_state_hash",
        "oauth_expires_at",
        "sync_cursor",
        "event_versions",
    }
    return all(
        (key in clearing and value is None)
        or (key == "connection_status" and value == "disconnected")
        for key, value in columns.items()
    )


def _locked(tx: WorldTx, connection_id: str) -> dict:
    tx.lock_world()
    rows = tx.run("MATCH (c:Connector {id: $id}) RETURN c", id=connection_id)
    if not rows:
        raise InvalidRecord("not_found", "connector", "no such source connection")
    return rows[0]["c"]


def _set(tx: WorldTx, connection_id: str, columns: dict):
    tx.run(
        "MATCH (c:Connector {id: $id}) SET c += $columns, c.version = c.version + 1",
        id=connection_id,
        columns={**columns, "touched_at": clock.now(tx.world_id)},
    )


def update_connection(driver, world: str, connection_id: str, **columns: Any) -> None:
    if set(columns) - _FIELDS:
        raise ValueError("unsupported connection field")

    def work(tx: WorldTx):
        row = _locked(tx, connection_id)
        if not _clears_authority(columns):
            _refuse_demo(tx)
        until = row.get("sync_until")
        if until and until > clock.now(world):
            raise ConnectionBusy()
        # A read may still run after its lease expired. A newer user mutation
        # revokes that owner, so it cannot restore credentials after disconnect.
        _set(tx, connection_id, {**columns, "sync_owner": None, "sync_until": None})

    WorldWriter(driver, world).transaction(work)


def consume_state(driver, world: str, connection_id: str, state_hash: str) -> dict | None:
    def work(tx: WorldTx):
        row = _locked(tx, connection_id)
        _refuse_demo(tx)
        until = row.get("oauth_expires_at")
        if row.get("oauth_state_hash") != state_hash or not until or until <= clock.now(world):
            return None
        _set(
            tx,
            connection_id,
            {"oauth_state_hash": None, "oauth_expires_at": None, "credential_ciphertext": None},
        )
        return row

    return WorldWriter(driver, world).transaction(work)


def start_sync(driver, world: str, connection_id: str) -> tuple[dict, str]:
    def work(tx: WorldTx):
        row = _locked(tx, connection_id)
        _refuse_demo(tx)
        now = clock.now(world)
        if row.get("sync_until") and row["sync_until"] > now:
            raise ConnectionBusy()
        if row.get("connection_status") != "connected":
            raise InvalidRecord("invalid_arguments", "connection", "source is not connected")
        owner = new_id(world)
        _set(
            tx,
            connection_id,
            {
                "sync_owner": owner,
                "sync_until": now + timedelta(seconds=LEASE_SECONDS),
                "last_attempted_read": now,
            },
        )
        return row, owner

    return WorldWriter(driver, world).transaction(work)


def require_sync(tx: WorldTx, connection_id: str, owner: str) -> None:
    """Fence a source transaction against revocation under the same lock as disconnect.

    Take the world lock before any connector lock. The check and the source commit must
    share this transaction; a prior check cannot authorize a later commit.
    """
    row = _locked(tx, connection_id)
    _refuse_demo(tx)
    until = row.get("sync_until")
    if (
        not owner
        or row.get("sync_owner") != owner
        or row.get("connection_status") != "connected"
        or not until
        or until <= clock.now(tx.world_id)
    ):
        raise ConnectionBusy()


def check_sync(driver, world: str, connection_id: str, owner: str) -> None:
    """Admit one request (or accept its response) in database order with disconnect.

    The committed admission is the request's start, not its first network byte. A request
    admitted before revocation can finish, but its response needs a fresh check and every
    later source transaction must call require_sync. No network I/O runs in this retried unit.
    """
    WorldWriter(driver, world).transaction(lambda tx: require_sync(tx, connection_id, owner))


def finish_sync(
    driver,
    world: str,
    connection_id: str,
    owner: str,
    *,
    outcome: str,
    cursor: str | None = None,
    versions: dict | None = None,
    credential: str | None = None,
) -> None:
    if outcome not in ("took_in", "nothing_new", "kept_out", "failed", "denied"):
        raise ValueError("invalid outcome")

    def work(tx: WorldTx):
        row = _locked(tx, connection_id)
        if outcome not in ("failed", "denied") or credential is not None:
            _refuse_demo(tx)
        if row.get("sync_owner") != owner or row.get("connection_status") != "connected":
            raise ConnectionBusy()
        if outcome not in ("failed", "denied"):
            require_sync(tx, connection_id, owner)
        columns = {"sync_owner": None, "sync_until": None, "last_outcome": outcome}
        if outcome in ("failed", "denied"):
            field = "denied_count" if outcome == "denied" else "failed_count"
            columns[field] = (row.get(field) or 0) + 1
        else:
            columns["last_successful_read"] = clock.now(world)
            columns["sync_cursor"] = cursor
            if versions is not None:
                columns["event_versions"] = json.dumps(versions, separators=(",", ":"))
        if credential:
            columns["credential_ciphertext"] = credential
        _set(tx, connection_id, columns)

    WorldWriter(driver, world).transaction(work)


def disconnect(driver, world: str, connection_id: str) -> None:
    update_connection(
        driver,
        world,
        connection_id,
        connection_status="disconnected",
        credential_ciphertext=None,
        oauth_state_hash=None,
        oauth_expires_at=None,
        sync_cursor=None,
        event_versions=None,
    )
