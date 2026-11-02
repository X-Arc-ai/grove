"""The OAuth rows as the routes read them: read transactions only, through PlatformQuery.

grove.writer.oauth writes these rows (S41). A token is looked up by its hash
(grove.oauth.secrets.token_hash), never by its value.
"""

from dataclasses import dataclass
from datetime import datetime

from grove.store.connection import Driver
from grove.store.errors import ScopeError
from grove.store.query import PlatformQuery
from grove.store.scope import check_world_id

ACCESS = "access"

_FIND_CLIENT = (
    "MATCH (c:OAuthClient {client_id: $client_id}) "
    "RETURN c.client_id AS client_id, c.host AS host, "
    "c.registration_type AS registration_type, c.redirect_uris AS redirect_uris, "
    "c.token_endpoint_auth_method AS auth_method, c.client_secret_hash AS secret_hash, "
    "c.registered_at AS registered_at, c.fetched_at AS fetched_at"
)
# Live: an access token, not revoked, not expired, its grant of the same client and world not
# revoked, and its world still there. A token holds only what the grant still grants: a later
# consent with fewer scopes takes the others from the tokens that the grant has already issued.
_FIND_ACCESS_TOKEN = (
    "MATCH (t:OAuthToken {token_hash: $token_hash}) "
    "WHERE t.token_type = $access AND t.revoked_at IS NULL AND t.expires_at > $now "
    "MATCH (g:OAuthGrant {id: t.grant_id}) "
    "WHERE g.revoked_at IS NULL AND g.world_id = t.world_id AND g.client_id = t.client_id "
    "MATCH (w:World {id: t.world_id}) "
    "RETURN t.id AS id, t.client_id AS client_id, t.user_id AS user_id, "
    "t.world_id AS world_id, t.grant_id AS grant_id, t.family_id AS family_id, "
    "[s IN t.scopes WHERE s IN g.scopes_granted] AS scopes, "
    "t.resource AS resource, t.expires_at AS expires_at"
)
_LIST_GRANTS = (
    "MATCH (g:OAuthGrant {world_id: $world_id}) WHERE g.revoked_at IS NULL "
    "OPTIONAL MATCH (c:OAuthClient {client_id: g.client_id}) "
    "RETURN g.id AS grant_id, g.client_id AS client_id, g.user_id AS user_id, "
    "c.host AS host, c.registration_type AS registration_type, "
    "g.scopes_granted AS scopes, g.granted_at AS granted_at "
    "ORDER BY g.granted_at DESC, g.id DESC"
)


@dataclass(frozen=True)
class ClientRow:
    client_id: str
    host: str | None
    registration_type: str
    redirect_uris: tuple[str, ...]
    auth_method: str
    secret_hash: str | None
    registered_at: datetime | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class TokenRow:
    id: str
    client_id: str
    user_id: str
    world_id: str
    grant_id: str
    family_id: str
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: datetime


@dataclass(frozen=True)
class GrantRow:
    grant_id: str
    client_id: str
    user_id: str
    host: str | None
    registration_type: str | None
    scopes: tuple[str, ...]
    granted_at: datetime


def find_client(driver: Driver, client_id: str) -> ClientRow | None:
    if not isinstance(client_id, str) or not client_id:
        return None
    rows = PlatformQuery(driver).read(_FIND_CLIENT, client_id=client_id)
    if not rows:
        return None
    row = rows[0]
    return ClientRow(
        client_id=row["client_id"],
        host=row["host"],
        registration_type=row["registration_type"],
        redirect_uris=tuple(row["redirect_uris"] or ()),
        auth_method=row["auth_method"],
        secret_hash=row["secret_hash"],
        registered_at=row["registered_at"],
        fetched_at=row["fetched_at"],
    )


def find_access_token(driver: Driver, token_hash: str, now: datetime) -> TokenRow | None:
    """The live access token of that hash, else None: refresh tokens, revoked or expired tokens,
    tokens of a revoked grant, tokens of a world that is gone and tokens with no scope left are
    not live."""
    rows = PlatformQuery(driver).read(
        _FIND_ACCESS_TOKEN, token_hash=token_hash, access=ACCESS, now=now
    )
    if not rows:
        return None
    row = rows[0]
    if not row["scopes"]:
        return None
    try:
        world_id = check_world_id(row["world_id"])
    except ScopeError:
        return None
    return TokenRow(
        id=row["id"],
        client_id=row["client_id"],
        user_id=row["user_id"],
        world_id=world_id,
        grant_id=row["grant_id"],
        family_id=row["family_id"],
        scopes=tuple(row["scopes"] or ()),
        resource=row["resource"],
        expires_at=row["expires_at"],
    )


def list_grants(driver: Driver, world_id: str) -> list[GrantRow]:
    """The live grants of a world, newest first, each with its client's host and type."""
    rows = PlatformQuery(driver).read(_LIST_GRANTS, world_id=world_id)
    return [
        GrantRow(
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            user_id=row["user_id"],
            host=row["host"],
            registration_type=row["registration_type"],
            scopes=tuple(row["scopes"] or ()),
            granted_at=row["granted_at"],
        )
        for row in rows
    ]
