"""Who may use the general tools: the owner of an ordinary world, and never a demo world.

Designation is durable account metadata (`grove.demo_policy`), so this check reads the store and
holds with the demo configuration removed. No argument or client header can change its answer.
"""

from typing import Any

from grove.api.errors import ApiError
from grove.demo_policy import (
    DESIGNATION_QUERY,
    DemoDenied,
    configured_email_hash,
    has_designation,
)
from grove.store.query import PlatformQuery

ACCOUNT_REFUSED = "demo_access_refused"
# The token's account owns the world, and the token's grant and client are real and of that owner.
# `find_access_token` proves a live token. It does not compare the user with the world's owner.
_OWNER_AND_GRANT = (
    "MATCH (w:World {id: $world_id}), (u:User {id: $user_id}), "
    "(g:OAuthGrant {id: $grant_id}), (c:OAuthClient {client_id: $client_id}) "
    "WHERE w.owner_user_id = u.id AND g.user_id = u.id AND g.world_id = w.id "
    "AND g.client_id = c.client_id AND g.revoked_at IS NULL RETURN g.id AS id"
)


def demo_refusal() -> ApiError:
    """The account is the wrong one for this tool set. The token's scopes are not the cause."""
    return ApiError(403, ACCOUNT_REFUSED, "the restricted demo is not available")


def token_refusal() -> ApiError:
    return ApiError(401, "unauthenticated", "the access token is not valid")


def _restricted(row: dict[str, Any]) -> bool:
    if has_designation(row):
        return True
    try:
        configured = configured_email_hash()
    except DemoDenied:
        return True
    return configured is not None and row.get("email_hash") == configured


def require_general_access(
    driver: Any, *, world_id: str, user_id: str, grant_id: str, client_id: str
) -> None:
    """Refuse a demo world with 403 and a token that does not bind the world's owner with 401.

    A corrupt or partial designation stays restricted. So does the configured address with no
    designation, because that account is the prepared account in a broken state.
    """
    query = PlatformQuery(driver)
    designation = query.read(DESIGNATION_QUERY, world_id=world_id)
    if len(designation) > 1 or (designation and _restricted(designation[0])):
        raise demo_refusal()
    bound = query.read(
        _OWNER_AND_GRANT,
        world_id=world_id,
        user_id=user_id,
        grant_id=grant_id,
        client_id=client_id,
    )
    if len(bound) != 1:
        raise token_refusal()
