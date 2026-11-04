"""The read side of upload-only tokens: which world a presented token may upload to, if any."""

from dataclasses import dataclass
from datetime import datetime

from grove.store.errors import ScopeError
from grove.store.query import PlatformQuery
from grove.store.scope import check_world_id
from grove.writer.upload_tokens import TOKEN_PREFIX, token_hash

MAX_TOKEN_LENGTH = 256

_FIND = (
    "MATCH (t:UploadToken {token_hash: $token_hash}) "
    "WHERE t.revoked_at IS NULL AND t.expires_at > $now "
    "MATCH (w:World {id: t.world_id}) "
    "RETURN t.id AS id, t.world_id AS world_id, t.user_id AS user_id, "
    "w.owner_user_id AS owner"
)


@dataclass(frozen=True)
class UploadGrant:
    token_id: str
    world_id: str
    user_id: str


def find_upload_grant(
    platform: PlatformQuery, raw: str | None, now: datetime
) -> UploadGrant | None:
    """The grant of a live token, or None: no token, another kind of token, an unknown, expired
    or revoked one, or a world whose owner changed since the mint."""
    if not raw or len(raw) > MAX_TOKEN_LENGTH or not raw.startswith(TOKEN_PREFIX):
        return None
    rows = platform.read(_FIND, token_hash=token_hash(raw), now=now)
    if not rows:
        return None
    row = rows[0]
    if row["owner"] != row["user_id"]:
        return None
    try:
        world_id = check_world_id(row["world_id"])
    except ScopeError:
        return None
    return UploadGrant(token_id=row["id"], world_id=world_id, user_id=row["user_id"])
