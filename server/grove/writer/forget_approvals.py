"""D14 person approvals: one target, one world, one use, five minutes.

The API mints only after a signed-in person confirms. A host can consume an approval but
cannot mint one. Only token and target digests are stored, never the target's text. Consumption
belongs in the same WorldTx as the cascade, so a rollback restores the approval too.
"""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from grove import clock
from grove.ids import new_id
from grove.store.connection import Driver
from grove.store.query import CYPHER_PREFIX
from grove.writer.errors import InvalidRecord
from grove.writer.tx import WorldTx, WorldWriter

LIFETIME = timedelta(minutes=5)
CONTENT_FIELDS = frozenset({"title", "summary", "body", "properties", "search_terms", "status"})
TOKEN = re.compile(r"gfa_[A-Za-z0-9_-]{43}")
_OWNER = CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) RETURN w.owner_user_id AS owner"


def _invalid() -> InvalidRecord:
    return InvalidRecord("invalid_arguments", "forget_target", "the forget target is not valid")


def _opaque(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_target(target: Any) -> dict[str, Any]:
    """Return a fresh canonical target. Reject extra fields and content-shaped identifiers."""
    if not isinstance(target, dict):
        raise _invalid()
    kind = target.get("type")
    keys = set(target)
    valid = False
    if kind == "item":
        valid = keys == {"type", "item_id"} and _opaque(target.get("item_id"))
    elif kind == "field_value":
        valid = (keys == {"type", "value_id"} and _opaque(target.get("value_id"))) or (
            keys == {"type", "item_id", "field"}
            and _opaque(target.get("item_id"))
            and isinstance(target.get("field"), str)
            and target["field"] in CONTENT_FIELDS
        )
    elif kind == "named_value":
        value = target.get("value")
        valid = (
            keys == {"type", "value"}
            and isinstance(value, str)
            and bool(value.strip())
            and len(value) <= 512
            and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
        )
    elif kind == "material":
        valid = (
            keys == {"type", "material_id", "revision"}
            and _opaque(target.get("material_id"))
            and type(target.get("revision")) is int
            and 1 <= target["revision"] <= 2**31 - 1
        )
    elif kind == "source_span":
        locator = target.get("locator")
        valid = (
            keys in ({"type", "source_id"}, {"type", "source_id", "locator"})
            and _opaque(target.get("source_id"))
            and (
                "locator" not in target
                or (
                    isinstance(locator, str)
                    and bool(locator.strip())
                    and len(locator) <= 4096
                    and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in locator)
                )
            )
        )
    if not valid:
        raise _invalid()
    return dict(sorted(target.items()))


def target_digest(target: Any) -> str:
    normalized = validate_target(target)
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _refused() -> InvalidRecord:
    return InvalidRecord(
        "approval_required", "forget_approval", "a new approval from the person is required"
    )


def _require_owner(tx: WorldTx, actor: str) -> None:
    row = tx.unscoped().run(_OWNER, world_id=tx.world_id).single()
    if row is None or row["owner"] != actor:
        raise _refused()


@dataclass(frozen=True)
class MintedApproval:
    id: str
    token: str = field(repr=False)
    expires_at: datetime


def mint_approval(
    driver: Driver,
    world_id: str,
    actor: str,
    target: Any,
    *,
    material_session=None,
    session_hash=None,
) -> MintedApproval:
    digest = target_digest(target)
    token = "gfa_" + secrets.token_urlsafe(32)
    now = clock.now(world_id)
    row = {
        "id": new_id(world_id),
        "user_id": actor,
        "token_hash": _token_hash(token),
        "target_digest": digest,
        "created_at": now,
        "expires_at": now + LIFETIME,
    }

    def work(tx: WorldTx) -> None:
        tx.lock_world()
        _require_owner(tx, actor)
        if target.get("type") == "material":
            from grove.writer.materials import check_revision, check_session

            if material_session is None or session_hash is None:
                raise _refused()
            check_session(tx, material_session, session_hash)
            check_revision(tx, target)
        tx.run("CREATE (a:ForgetApproval) SET a += $row", row=row)

    if target.get("type") == "material":
        from grove.writer.forget_ledger import checked_under_lock, locked_ledger

        with locked_ledger(None) as root:

            def checked_work(tx):
                checked_under_lock(
                    root, world_id, [r["f"] for r in tx.run("MATCH (f:ForgetLedger) RETURN f")]
                )
                work(tx)

            WorldWriter(driver, world_id).transaction(checked_work)
    else:
        WorldWriter(driver, world_id).transaction(work)
    return MintedApproval(row["id"], token, row["expires_at"])


def consume_approval(tx: WorldTx, token: str, digest: str, actor: str) -> str:
    """Consume only in the cascade transaction. All refusal cases share one safe error."""
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise _refused()
    tx.lock_world()
    _require_owner(tx, actor)
    rows = tx.run(
        "MATCH (a:ForgetApproval {token_hash: $token_hash}) "
        "WHERE a.user_id = $actor AND a.target_digest = $digest "
        "AND a.used_at IS NULL AND a.expires_at > $now "
        "SET a.used_at = $now RETURN a.id AS id",
        token_hash=_token_hash(token),
        actor=actor,
        digest=digest,
        now=clock.now(tx.world_id),
    )
    if len(rows) != 1:
        raise _refused()
    return rows[0]["id"]
