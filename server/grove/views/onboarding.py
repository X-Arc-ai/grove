"""Bounded account metadata for pending exports; these reads never repair state."""

import json
import re
from dataclasses import dataclass

from grove.store.query import PlatformQuery
from grove.views.session import ViewSession

PROVIDERS = frozenset({"chatgpt", "claude_ai", "whatsapp", "muse", "grok"})
MAX_METADATA_BYTES = 1024
MAX_REVISION = 2**53 - 1
_GENERATION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

READ_STATE = (
    "MATCH (w:World {id: $world_id}) "
    "RETURN w.owner_user_id AS account, w.pending_export_requests AS requests, "
    "w.pending_export_revision AS revision"
)


class InvalidPendingExports(ValueError):
    """Stored metadata or a provider does not meet the closed contract."""


class PendingExportDenied(PermissionError):
    """The resolved owner or durable designation forbids this operation."""


class PendingExportSessionEnded(PermissionError):
    """The actual session no longer resolves to the supplied owner and world."""


def require_provider(provider: str) -> None:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise InvalidPendingExports("unknown export provider")


def valid_generation(value: object) -> bool:
    return isinstance(value, str) and _GENERATION.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPendingExports("duplicate export provider")
        result[key] = value
    return result


def decode_state(raw: object, revision: object) -> tuple[dict[str, str], int]:
    """json_ids permits only machine codes, IDs and numbers, never free text.

    The keys here are closed provider codes and values are canonical UUIDv7 IDs. No
    arbitrary labels, filenames, addresses, timestamps or nested metadata are accepted.
    Missing properties retain their empty/zero meaning, without a read-side write.
    """
    revision = 0 if revision is None else revision
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise InvalidPendingExports("invalid export revision")
    if raw is None:
        return {}, revision
    if not isinstance(raw, str) or len(raw) > MAX_METADATA_BYTES:
        raise InvalidPendingExports("invalid export metadata")
    try:
        if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
            raise InvalidPendingExports("export metadata is too large")
        values = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidPendingExports("invalid export metadata") from None
    if (
        not isinstance(values, dict)
        or len(values) > len(PROVIDERS)
        or any(key not in PROVIDERS or not valid_generation(value) for key, value in values.items())
    ):
        raise InvalidPendingExports("invalid export metadata")
    return values, revision


def snapshot(row: dict) -> dict:
    values, revision = decode_state(row.get("requests"), row.get("revision"))
    account = row.get("account")
    if not isinstance(account, str) or not account or len(account) > 128:
        raise InvalidPendingExports("invalid export owner")
    return {"account": account, "revision": revision, "pending_exports": values}


def pending_exports(driver, session: ViewSession) -> dict:
    rows = PlatformQuery(driver).read(READ_STATE, world_id=session.world_id)
    if len(rows) != 1 or rows[0].get("account") != session.user_id:
        raise PendingExportDenied("export owner changed")
    return snapshot(rows[0])


@dataclass(frozen=True)
class PendingExportCapture:
    world_id: str
    account: str
    provider: str
    generation: str | None


def capture_pending_export(driver, world_id: str, provider: str) -> PendingExportCapture | None:
    # Registered upload providers outside this closed map gain no reminder behavior.
    if provider not in PROVIDERS:
        return None
    rows = PlatformQuery(driver).read(READ_STATE, world_id=world_id)
    if len(rows) != 1:
        raise PendingExportDenied("export world is unavailable")
    state = snapshot(rows[0])
    return PendingExportCapture(
        world_id, state["account"], provider, state["pending_exports"].get(provider)
    )
