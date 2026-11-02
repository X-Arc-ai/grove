"""The session the views read. Signin owns the rows; the views only read them.

The contract: the cookie `grove_session` holds a token. `:Session.token_hash` is the lowercase
sha256 hex of that token. A session is valid while `revoked_at` is absent and `expires_at` is later
than the clock's now. `:Session.user_id` leads to the `:World` whose `owner_user_id` it is, the
oldest by `created_at` when there are several. `World.self_item_id`, which may be absent, names the
person's own item.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from grove.store.errors import ScopeError
from grove.store.query import PlatformQuery
from grove.store.scope import check_world_id

SESSION_COOKIE = "grove_session"
MAX_TOKEN_LENGTH = 512

_FIND = (
    "MATCH (s:Session {token_hash: $token_hash}) "
    "WHERE s.revoked_at IS NULL AND s.expires_at > $now "
    "MATCH (w:World {owner_user_id: s.user_id}) "
    "RETURN s.user_id AS user_id, w.id AS world_id, w.self_item_id AS self_item_id "
    "ORDER BY w.created_at, w.id LIMIT 1"
)


@dataclass(frozen=True)
class ViewSession:
    user_id: str
    world_id: str
    self_item_id: str | None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_session(platform: PlatformQuery, token: str | None, now: datetime) -> ViewSession | None:
    """The signed-in person's world, or None for no token, an unknown, expired or revoked one."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    rows = platform.read(_FIND, token_hash=token_hash(token), now=now)
    if not rows:
        return None
    row = rows[0]
    try:
        world_id = check_world_id(row["world_id"])
    except ScopeError:
        return None
    self_item_id = row["self_item_id"]
    return ViewSession(
        user_id=row["user_id"],
        world_id=world_id,
        self_item_id=self_item_id if isinstance(self_item_id, str) else None,
    )
