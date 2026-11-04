"""The bounded, invented-data demo designation and input policy.

Designation is durable account metadata. Removing runtime configuration never turns a demo
world into an unrestricted intake world. Configuration is also required before disclosure.
"""

import os
import re
from typing import Any

from grove.store.query import CYPHER_PREFIX, PlatformQuery, collect

DEMO_VERSION = "cedar-v1"
DEMO_TEXT = "The Cedar demo is in room 4 at 14:00 UTC."
DEMO_INPUTS = (DEMO_TEXT,)
DEMO_SEED_CLIENT = "demo-seed-v1"
RICH_CORPUS_SHA256 = "88091dfafeafa6f50c49b5cadcb8f32a131a10a5e586656c2b5b95f1dfcf3ecb"
_HASH = re.compile(r"[0-9a-f]{64}")

DESIGNATION_QUERY = (
    "MATCH (w:World {id: $world_id}) "
    "OPTIONAL MATCH (u:User {id: w.owner_user_id}) "
    "OPTIONAL MATCH (d:User {demo_world_id: w.id}) "
    "RETURN w.id AS world_id, w.owner_user_id AS user_id, w.self_item_id AS self_item_id, "
    "u.email_hash AS email_hash, u.demo_version AS demo_version, "
    "u.demo_world_id AS demo_world_id, d.id AS designated_user_id, "
    "d.demo_version AS claimed_demo_version"
)
OLDEST_WORLD_QUERY = (
    "MATCH (w:World {owner_user_id: $user_id}) "
    "RETURN w.id AS id ORDER BY w.created_at, w.id LIMIT 1"
)


class DemoDenied(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the restricted demo operation is not permitted")


def configured_email_hash() -> str | None:
    value = os.environ.get("GROVE_DEMO_EMAIL_HASH")
    if value is None or value == "":
        return None
    if _HASH.fullmatch(value) is None:
        raise DemoDenied()
    return value


def has_designation(row: dict[str, Any]) -> bool:
    """Partial or corrupt durable metadata stays restricted."""
    return any(
        row.get(key) is not None
        for key in ("demo_version", "demo_world_id", "claimed_demo_version", "designated_user_id")
    )


def designation_in_tx(tx: Any) -> dict[str, Any] | None:
    rows = collect(tx.unscoped().run(CYPHER_PREFIX + DESIGNATION_QUERY, world_id=tx.world_id), None)
    if len(rows) > 1:
        raise DemoDenied()
    return rows[0] if rows else None


def is_demo_world(driver: Any, world_id: str) -> bool:
    rows = PlatformQuery(driver).read(DESIGNATION_QUERY, world_id=world_id)
    return bool(rows and has_designation(rows[0]))


def require_designation(driver: Any, world_id: str, user_id: str | None = None) -> dict[str, Any]:
    allowed = configured_email_hash()
    if allowed is None:
        raise DemoDenied()
    query = PlatformQuery(driver)
    rows = query.read(DESIGNATION_QUERY, world_id=world_id)
    if len(rows) != 1:
        raise DemoDenied()
    row = rows[0]
    if (
        row.get("email_hash") != allowed
        or row.get("demo_version") != DEMO_VERSION
        or row.get("demo_world_id") != world_id
        or not row.get("user_id")
        or row.get("designated_user_id") != row.get("user_id")
        or row.get("claimed_demo_version") != DEMO_VERSION
        or (user_id is not None and row["user_id"] != user_id)
    ):
        raise DemoDenied()
    oldest = query.read(OLDEST_WORLD_QUERY, user_id=row["user_id"])
    if len(oldest) != 1 or oldest[0]["id"] != world_id:
        raise DemoDenied()
    return row


def require_designation_in_tx(tx: Any) -> dict[str, Any]:
    row = designation_in_tx(tx)
    if (
        row is None
        or configured_email_hash() is None
        or row.get("email_hash") != configured_email_hash()
        or row.get("demo_version") != DEMO_VERSION
        or row.get("demo_world_id") != tx.world_id
        or not row.get("user_id")
        or row.get("designated_user_id") != row.get("user_id")
        or row.get("claimed_demo_version") != DEMO_VERSION
    ):
        raise DemoDenied()
    oldest = collect(
        tx.unscoped().run(CYPHER_PREFIX + OLDEST_WORLD_QUERY, user_id=row["user_id"]), None
    )
    if len(oldest) != 1 or oldest[0]["id"] != tx.world_id:
        raise DemoDenied()
    return row


def require_corpus_identity() -> str:
    """The additive fixture never expands the Cedar-only public intake policy."""
    from grove.demo.corpus import VERSION, digest

    if VERSION != "rich-demo-v1" or digest() != RICH_CORPUS_SHA256:
        raise DemoDenied()
    return RICH_CORPUS_SHA256
