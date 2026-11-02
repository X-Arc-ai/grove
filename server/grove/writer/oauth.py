"""OAuth's writes (S20, S41): clients, a person's consent, codes and tokens.

The only writer of :OAuthClient, :OAuthGrant, :AuthCode and :OAuthToken. Every function takes the
driver first and runs one write transaction. Times come from the caller (`at`), never from a clock
read here, so a route reads the clock once per request.

A check that guards a change runs after a direct SET on the row it reads, the first form under
"What A.5 asks, what Neo4j gives" in docs/store-map.md: a filter in MATCH reads with no lock, so
two redeems of one code would both see it unused. The lock write sets a property to its own value,
never the checked one, and the read after it is a statement of its own, so it sees what a racer
committed before the lock was granted.

Lock order: a code before its grant (redeem_code, issue_tokens), a client before its grant
(grant_consent). rotate_refresh locks a token before its grant and revoke_grant the grant before
its tokens; when those two meet, Neo4j ends one with a transient deadlock error and the driver runs
it again, so the second sees what the first committed.

A caller passes hashes (grove.oauth.secrets.token_hash), never a code, a token or a secret.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work
from neo4j.exceptions import ConstraintError

from grove.ids import new_id
from grove.oauth import (
    EXPIRED_CODE_KEPT,
    MAX_CLIENTS_DELETED_PER_REGISTRATION,
    UNUSED_DYNAMIC_CLIENT_KEPT,
)
from grove.store.connection import DATABASE
from grove.store.enums import ENUMS
from grove.store.query import CYPHER_PREFIX, collect

DYNAMIC = "dynamic"
METADATA_DOCUMENT = "metadata_document"
REGISTRATION_TYPES: tuple[str, ...] = ENUMS["registration_type"]
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
PUBLIC_CLIENT = "none"
ACCESS = "access"
REFRESH = "refresh"

_TIMEOUT = 30.0
_ATTEMPTS = 3

# Clients ----------------------------------------------------------------------------------------

_ADD_CLIENT = "CREATE (c:OAuthClient) SET c = $row"
# The client id of a document is its URL, one row for every person who uses that host. Only the
# first fetch sets the type and the registration time; every fetch refreshes what the document says.
_ENSURE_DOCUMENT_CLIENT = (
    "MERGE (c:OAuthClient {client_id: $client_id}) "
    "ON CREATE SET c.registration_type = $registration_type, c.registered_at = $at "
    "SET c.host = $host, c.redirect_uris = $redirect_uris, "
    "c.token_endpoint_auth_method = $auth_method, c.fetched_at = $at "
    "RETURN c.registration_type AS registration_type"
)
# Old dynamic clients that no grant names, oldest first. The filter comes before the limit, so old
# clients with grants never fill it. Each is locked, and the drop checks for a grant again in a
# statement of its own, so a consent that committed first keeps its client. A client with no grant
# holds no code or token: store_code and issue_tokens both need a grant.
_LOCK_OLD_DYNAMIC_CLIENTS = (
    "MATCH (c:OAuthClient {registration_type: $registration_type}) "
    "WHERE c.registered_at < $before "
    "AND NOT EXISTS { MATCH (g:OAuthGrant {client_id: c.client_id}) } "
    "WITH c ORDER BY c.registered_at, c.client_id LIMIT $limit "
    "SET c.registration_type = c.registration_type "
    "RETURN c.client_id AS client_id"
)
_DROP_UNUSED_CLIENTS = (
    "UNWIND $client_ids AS client_id "
    "MATCH (c:OAuthClient {client_id: client_id}) "
    "WHERE NOT EXISTS { MATCH (g:OAuthGrant {client_id: client_id}) } "
    "DETACH DELETE c "
    "RETURN count(*) AS deleted"
)
_LOCK_CLIENT = (
    "MATCH (c:OAuthClient {client_id: $client_id}) "
    "SET c.registration_type = c.registration_type "
    "RETURN c.client_id AS client_id"
)

# Grants -----------------------------------------------------------------------------------------

# MERGE on the key of the (client_id, world_id) constraint: a second consent from the same world
# waits for the first and finds its row.
_ENSURE_GRANT = (
    "MERGE (g:OAuthGrant {client_id: $client_id, world_id: $world_id}) "
    "ON CREATE SET g.id = $id "
    "SET g.user_id = $user_id, g.scopes_granted = $scopes, g.granted_at = $at "
    "REMOVE g.revoked_at "
    "RETURN g.id AS id"
)
_LOCK_GRANT = (
    "MATCH (g:OAuthGrant {id: $grant_id}) SET g.granted_at = g.granted_at RETURN g.id AS id"
)
_READ_GRANT = (
    "MATCH (g:OAuthGrant {id: $grant_id}) "
    "RETURN g.client_id AS client_id, g.world_id AS world_id, g.revoked_at AS revoked_at, "
    "g.scopes_granted AS scopes_granted"
)
_REVOKE_GRANT = (
    "MATCH (g:OAuthGrant {id: $grant_id, world_id: $world_id}) "
    "SET g.revoked_at = coalesce(g.revoked_at, $at) "
    "RETURN g.id AS id"
)

# Codes ------------------------------------------------------------------------------------------

_DROP_OLD_CODES = "MATCH (c:AuthCode {client_id: $client_id}) WHERE c.expires_at < $before DELETE c"
_ADD_CODE = "CREATE (c:AuthCode) SET c = $row"
_LOCK_CODE = (
    "MATCH (c:AuthCode {code_hash: $code_hash}) SET c.created_at = c.created_at RETURN c.id AS id"
)
_LOCK_CODE_BY_ID = "MATCH (c:AuthCode {id: $id}) SET c.created_at = c.created_at RETURN c.id AS id"
_READ_CODE = "MATCH (c:AuthCode {id: $id}) RETURN c {.*} AS row"
_USE_CODE = "MATCH (c:AuthCode {id: $id}) SET c.used_at = $at"
# A second redeem keeps the time of the first reuse.
_MARK_REUSED = "MATCH (c:AuthCode {id: $id}) SET c.reused_at = coalesce(c.reused_at, $at)"

# Tokens -----------------------------------------------------------------------------------------

_ADD_TOKEN = "CREATE (t:OAuthToken) SET t = $row"
_LOCK_TOKEN = (
    "MATCH (t:OAuthToken {token_hash: $token_hash}) "
    "SET t.created_at = t.created_at RETURN t.id AS id"
)
_READ_TOKEN = "MATCH (t:OAuthToken {id: $id}) RETURN t {.*} AS row"
_SPEND_REFRESH = "MATCH (t:OAuthToken {id: $id}) SET t.used_at = $at, t.revoked_at = $at"
_REVOKE_ONE = "MATCH (t:OAuthToken {id: $id}) WHERE t.revoked_at IS NULL SET t.revoked_at = $at"
_REVOKE_FAMILY = (
    "MATCH (t:OAuthToken {family_id: $family_id}) WHERE t.revoked_at IS NULL SET t.revoked_at = $at"
)
_REVOKE_GRANT_TOKENS = (
    "MATCH (t:OAuthToken {grant_id: $grant_id}) WHERE t.revoked_at IS NULL SET t.revoked_at = $at"
)


class GrantRevoked(RuntimeError):
    """issue_tokens found no live grant of that id for that client and world, or no redeemed code
    of that grant behind the family; nothing was written.

    A revoked grant issues no token, and neither does a code that was redeemed twice: its second
    redeem may come between the first redeem and its issue."""


class UnknownClient(LookupError):
    """grant_consent found no client of that id. Nothing was written."""


class ScopeNotGranted(ValueError):
    """rotate_refresh was asked for a scope the refresh token does not hold, or for none.

    Nothing was written: the refresh token stays usable (RFC 6749 section 6, invalid_scope)."""


@dataclass(frozen=True)
class RedeemedCode:
    id: str
    client_id: str
    user_id: str
    world_id: str
    grant_id: str
    code_challenge: str
    redirect_uri: str
    redirect_given: bool
    scopes: tuple[str, ...]
    resource: str | None


@dataclass(frozen=True)
class IssuedFamily:
    family_id: str
    grant_id: str
    world_id: str
    scopes: tuple[str, ...]
    resource: str | None


def _write[T](driver: Driver, work: Callable[[ManagedTransaction], T]) -> T:
    """One write transaction on platform rows. The driver may run `work` twice."""

    @unit_of_work(timeout=_TIMEOUT)
    def unit(tx: ManagedTransaction) -> T:
        return work(tx)

    with driver.session(database=DATABASE) as session:
        return session.execute_write(unit)


def _rows(tx: ManagedTransaction, cypher: str, **params: Any) -> list[dict]:
    return collect(tx.run(CYPHER_PREFIX + cypher, params), None)


def _codes(name: str, values: Sequence[str]) -> list[str]:
    """A non-empty list of non-empty strings, in order, each once. S16: no empty list is stored."""
    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        raise ValueError(f"{name} must be a non-empty list")
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{name} must hold non-empty strings only")
    return list(dict.fromkeys(values))


def _present(row: dict[str, Any]) -> dict[str, Any]:
    """Null is no property value: an absent value stays absent on the row."""
    return {key: value for key, value in row.items() if value is not None}


def _scopes_of(row: dict[str, Any], key: str = "scopes") -> tuple[str, ...]:
    return tuple(row.get(key) or ())


# Clients ----------------------------------------------------------------------------------------


def register_client(
    driver: Driver,
    *,
    client_id: str,
    host: str | None,
    redirect_uris: list[str],
    registration_type: str,
    auth_method: str,
    secret_hash: str | None,
    at: datetime,
) -> None:
    """A dynamic client (CREATE) or a metadata document's copy (MERGE on client_id).

    The same transaction deletes up to 100 dynamic clients that no grant names and that registered
    more than 7 days before `at`. A client id that exists raises ConstraintError for a dynamic
    client; a metadata document's row is refreshed with what the document says and fetched_at=at.
    """
    if registration_type not in REGISTRATION_TYPES:
        raise ValueError(f"registration_type must be one of {REGISTRATION_TYPES}")
    if auth_method not in AUTH_METHODS:
        raise ValueError(f"auth_method must be one of {AUTH_METHODS}")
    if (secret_hash is None) != (auth_method == PUBLIC_CLIENT):
        raise ValueError("a public client has no secret, and a confidential client has one")
    if registration_type == METADATA_DOCUMENT and auth_method != PUBLIC_CLIENT:
        raise ValueError("a metadata document client is a public client")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("client_id must be a non-empty string")
    uris = _codes("redirect_uris", redirect_uris)

    def work(tx: ManagedTransaction) -> None:
        if registration_type == DYNAMIC:
            row = {
                "client_id": client_id,
                "host": host,
                "registration_type": registration_type,
                "redirect_uris": uris,
                "token_endpoint_auth_method": auth_method,
                "client_secret_hash": secret_hash,
                "registered_at": at,
            }
            _rows(tx, _ADD_CLIENT, row=_present(row))
        else:
            found = _rows(
                tx,
                _ENSURE_DOCUMENT_CLIENT,
                client_id=client_id,
                registration_type=registration_type,
                host=host,
                redirect_uris=uris,
                auth_method=auth_method,
                at=at,
            )
            if found[0]["registration_type"] != METADATA_DOCUMENT:
                raise ValueError("a dynamic client holds that client id")
        _drop_unused_clients(tx, before=at - UNUSED_DYNAMIC_CLIENT_KEPT)

    _write(driver, work)


def _drop_unused_clients(tx: ManagedTransaction, *, before: datetime) -> int:
    locked = _rows(
        tx,
        _LOCK_OLD_DYNAMIC_CLIENTS,
        registration_type=DYNAMIC,
        before=before,
        limit=MAX_CLIENTS_DELETED_PER_REGISTRATION,
    )
    if not locked:
        return 0
    client_ids = [row["client_id"] for row in locked]
    return _rows(tx, _DROP_UNUSED_CLIENTS, client_ids=client_ids)[0]["deleted"]


def delete_unused_client(driver: Driver, *, client_id: str) -> bool:
    """Delete one client that no grant names, of either type. False when a grant names it or it is
    absent. A client with no grant holds no code or token. The test purge calls it after it has
    purged the client's test worlds, whose grants, codes and tokens go with them."""

    def work(tx: ManagedTransaction) -> bool:
        if not _rows(tx, _LOCK_CLIENT, client_id=client_id):
            return False
        return _rows(tx, _DROP_UNUSED_CLIENTS, client_ids=[client_id])[0]["deleted"] == 1

    return _write(driver, work)


# Consent ----------------------------------------------------------------------------------------


def grant_consent(
    driver: Driver,
    *,
    client_id: str,
    user_id: str,
    world_id: str,
    scopes: list[str],
    at: datetime,
) -> str:
    """The grant of (client_id, world_id), made or found: user_id, scopes_granted and granted_at
    are set and revoked_at is cleared. Returns the grant id. Raises UnknownClient when no client
    has the id; the client's lock keeps a cleanup from deleting it under the new grant."""
    granted = _codes("scopes", scopes)
    fresh = new_id()

    def work(tx: ManagedTransaction) -> str:
        if not _rows(tx, _LOCK_CLIENT, client_id=client_id):
            raise UnknownClient("no client has that id")
        return _rows(
            tx,
            _ENSURE_GRANT,
            client_id=client_id,
            world_id=world_id,
            id=fresh,
            user_id=user_id,
            scopes=granted,
            at=at,
        )[0]["id"]

    for attempt in range(_ATTEMPTS):
        try:
            return _write(driver, work)
        except ConstraintError:
            # A create that lost a race on the (client_id, world_id) key. The next run finds it.
            if attempt == _ATTEMPTS - 1:
                raise
    raise AssertionError("the loop returns or raises")


# Codes ------------------------------------------------------------------------------------------


def store_code(
    driver: Driver,
    *,
    code_hash: str,
    client_id: str,
    user_id: str,
    world_id: str,
    grant_id: str,
    code_challenge: str,
    redirect_uri: str,
    redirect_given: bool,
    scopes: list[str],
    resource: str | None,
    at: datetime,
    expires_at: datetime,
) -> str:
    """One :AuthCode row; returns its id, which becomes the family id of its tokens. The same
    transaction deletes this client's codes that expired more than 1 day before `at`."""
    if not isinstance(redirect_given, bool):
        raise ValueError("redirect_given must be a boolean")
    if expires_at <= at:
        raise ValueError("a code must expire after it is made")
    row = {
        "id": new_id(),
        "code_hash": code_hash,
        "client_id": client_id,
        "user_id": user_id,
        "world_id": world_id,
        "grant_id": grant_id,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "redirect_given": redirect_given,
        "scopes": _codes("scopes", scopes),
        "resource": resource,
        "created_at": at,
        "expires_at": expires_at,
    }

    def work(tx: ManagedTransaction) -> None:
        _rows(tx, _DROP_OLD_CODES, client_id=client_id, before=at - EXPIRED_CODE_KEPT)
        _rows(tx, _ADD_CODE, row=_present(row))

    _write(driver, work)
    return row["id"]


def redeem_code(driver: Driver, *, code_hash: str, at: datetime) -> RedeemedCode | None:
    """The code's row, once. None for an unknown or expired code. A code already used gives None,
    sets the code's reused_at and revokes every token whose family_id is the code's id (RFC 6749
    section 4.1.2), in a transaction that commits. issue_tokens refuses a family whose code has
    reused_at, so a reuse that comes before the winner's issue still leaves no live token."""

    def work(tx: ManagedTransaction) -> RedeemedCode | None:
        locked = _rows(tx, _LOCK_CODE, code_hash=code_hash)
        if not locked:
            return None
        code_id = locked[0]["id"]
        row = _rows(tx, _READ_CODE, id=code_id)[0]["row"]
        if row.get("used_at") is not None:
            _rows(tx, _MARK_REUSED, id=code_id, at=at)
            _rows(tx, _REVOKE_FAMILY, family_id=code_id, at=at)
            return None
        if row["expires_at"] <= at:
            return None
        _rows(tx, _USE_CODE, id=code_id, at=at)
        return RedeemedCode(
            id=code_id,
            client_id=row["client_id"],
            user_id=row["user_id"],
            world_id=row["world_id"],
            grant_id=row["grant_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_given=bool(row.get("redirect_given", False)),
            scopes=_scopes_of(row),
            resource=row.get("resource"),
        )

    return _write(driver, work)


# Tokens -----------------------------------------------------------------------------------------


def _family_code_holds(
    tx: ManagedTransaction, family_id: str, grant_id: str, client_id: str, world_id: str
) -> bool:
    """Locks the code that began the family, then reads it, as redeem_code does: a second redeem
    that commits first is seen here, and one that comes later waits for this transaction and then
    revokes what it made. The code must be redeemed, redeemed once, and of this grant, client and
    world. The code comes before the grant in every transaction that takes both locks."""
    if not _rows(tx, _LOCK_CODE_BY_ID, id=family_id):
        return False
    code = _rows(tx, _READ_CODE, id=family_id)[0]["row"]
    return (
        code.get("used_at") is not None
        and code.get("reused_at") is None
        and code.get("grant_id") == grant_id
        and code.get("client_id") == client_id
        and code.get("world_id") == world_id
    )


def _live_grant(tx: ManagedTransaction, grant_id: str, client_id: str, world_id: str) -> bool:
    """Locks the grant, then reads it: a revoke that commits first is seen, and one that comes
    later waits for this transaction and then revokes what it made."""
    if not _rows(tx, _LOCK_GRANT, grant_id=grant_id):
        return False
    grant = _rows(tx, _READ_GRANT, grant_id=grant_id)[0]
    return (
        grant["revoked_at"] is None
        and grant["client_id"] == client_id
        and grant["world_id"] == world_id
    )


def _granted_now(tx: ManagedTransaction, grant_id: str) -> tuple[str, ...]:
    """The scopes of the person's last consent. Call it after _live_grant, which locks the row."""
    return _scopes_of(_rows(tx, _READ_GRANT, grant_id=grant_id)[0], "scopes_granted")


def _token_row(
    *,
    token_hash: str,
    token_type: str,
    client_id: str,
    user_id: str,
    world_id: str,
    grant_id: str,
    family_id: str,
    scopes: list[str],
    resource: str | None,
    at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    return _present(
        {
            "id": new_id(),
            "token_hash": token_hash,
            "token_type": token_type,
            "client_id": client_id,
            "user_id": user_id,
            "world_id": world_id,
            "grant_id": grant_id,
            "family_id": family_id,
            "scopes": scopes,
            "resource": resource,
            "created_at": at,
            "expires_at": expires_at,
        }
    )


def _add_pair(
    tx: ManagedTransaction,
    *,
    access_hash: str,
    refresh_hash: str,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
    **shared: Any,
) -> None:
    for token_hash, token_type, expires_at in (
        (access_hash, ACCESS, access_expires_at),
        (refresh_hash, REFRESH, refresh_expires_at),
    ):
        row = _token_row(
            token_hash=token_hash, token_type=token_type, expires_at=expires_at, **shared
        )
        _rows(tx, _ADD_TOKEN, row=row)


def issue_tokens(
    driver: Driver,
    *,
    access_hash: str,
    refresh_hash: str,
    client_id: str,
    user_id: str,
    world_id: str,
    grant_id: str,
    family_id: str,
    scopes: list[str],
    resource: str | None,
    at: datetime,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
) -> None:
    """An access token and a refresh token in one transaction, both of the grant's world. family_id
    is the id of the code that redeem_code returned. Raises GrantRevoked, with nothing written, when
    the grant is revoked, absent, or of another client or world, and when that code is absent, not
    redeemed, redeemed twice, or of another grant, client or world."""
    granted = _codes("scopes", scopes)

    def work(tx: ManagedTransaction) -> None:
        if not _family_code_holds(tx, family_id, grant_id, client_id, world_id):
            raise GrantRevoked(
                "the family's code is absent, unredeemed, reused or of another grant"
            )
        if not _live_grant(tx, grant_id, client_id, world_id):
            raise GrantRevoked("the grant is revoked or absent")
        _add_pair(
            tx,
            access_hash=access_hash,
            refresh_hash=refresh_hash,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            client_id=client_id,
            user_id=user_id,
            world_id=world_id,
            grant_id=grant_id,
            family_id=family_id,
            scopes=granted,
            resource=resource,
            at=at,
        )

    _write(driver, work)


def rotate_refresh(
    driver: Driver,
    *,
    refresh_hash: str,
    client_id: str,
    new_access_hash: str,
    new_refresh_hash: str,
    scopes: list[str] | None,
    at: datetime,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
) -> IssuedFamily | None:
    """Spend one refresh token for a new pair in the same family and grant.

    None for an unknown token, an access token, another client's token, an expired one, or a
    revoked grant. A refresh token already used or revoked gives None and revokes its whole family
    (RFC 9700 section 4.14.2), in a transaction that commits. `scopes` narrows the new pair to a
    subset of the old scopes; None keeps them. Raises ScopeNotGranted, with nothing written, for a
    scope the old token does not hold."""

    def work(tx: ManagedTransaction) -> IssuedFamily | None:
        locked = _rows(tx, _LOCK_TOKEN, token_hash=refresh_hash)
        if not locked:
            return None
        old = _rows(tx, _READ_TOKEN, id=locked[0]["id"])[0]["row"]
        if old.get("token_type") != REFRESH or old.get("client_id") != client_id:
            return None
        if old.get("used_at") is not None or old.get("revoked_at") is not None:
            _rows(tx, _REVOKE_FAMILY, family_id=old["family_id"], at=at)
            return None
        if old["expires_at"] <= at:
            return None
        if not _live_grant(tx, old["grant_id"], client_id, old["world_id"]):
            return None
        # A later consent with fewer scopes narrows this family too; none left is no refresh.
        still_granted = _granted_now(tx, old["grant_id"])
        held = tuple(scope for scope in _scopes_of(old) if scope in still_granted)
        if not held:
            return None
        if scopes is None:
            granted = list(held)
        else:
            try:
                granted = _codes("scopes", scopes)
            except ValueError:
                raise ScopeNotGranted("a refresh asks for at least one scope") from None
            if not set(granted) <= set(held):
                raise ScopeNotGranted("a refresh may only narrow the scopes")
        _rows(tx, _SPEND_REFRESH, id=old["id"], at=at)
        _add_pair(
            tx,
            access_hash=new_access_hash,
            refresh_hash=new_refresh_hash,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            client_id=client_id,
            user_id=old["user_id"],
            world_id=old["world_id"],
            grant_id=old["grant_id"],
            family_id=old["family_id"],
            scopes=granted,
            resource=old.get("resource"),
            at=at,
        )
        return IssuedFamily(
            family_id=old["family_id"],
            grant_id=old["grant_id"],
            world_id=old["world_id"],
            scopes=tuple(granted),
            resource=old.get("resource"),
        )

    return _write(driver, work)


def revoke_token(driver: Driver, *, token_hash: str, client_id: str, at: datetime) -> None:
    """RFC 7009 for a token of this client: an access token alone, a refresh token with its whole
    family. Another client's token and an unknown one change nothing."""

    def work(tx: ManagedTransaction) -> None:
        locked = _rows(tx, _LOCK_TOKEN, token_hash=token_hash)
        if not locked:
            return
        row = _rows(tx, _READ_TOKEN, id=locked[0]["id"])[0]["row"]
        if row.get("client_id") != client_id:
            return
        if row.get("token_type") == REFRESH and row.get("family_id"):
            _rows(tx, _REVOKE_FAMILY, family_id=row["family_id"], at=at)
        else:
            _rows(tx, _REVOKE_ONE, id=row["id"], at=at)

    _write(driver, work)


def revoke_grant(driver: Driver, *, grant_id: str, world_id: str, at: datetime) -> bool:
    """The person's revoke of a host: the grant's revoked_at and every live token of the grant, in
    one transaction. False, with nothing written, when no grant of that world has the id. A grant
    revoked before keeps its first revoked_at."""

    def work(tx: ManagedTransaction) -> bool:
        if not _rows(tx, _REVOKE_GRANT, grant_id=grant_id, world_id=world_id, at=at):
            return False
        _rows(tx, _REVOKE_GRANT_TOKENS, grant_id=grant_id, at=at)
        return True

    return _write(driver, work)
