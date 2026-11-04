"""D14 source tombstones: opaque locator digests, never forgotten words.

The upload pipeline reads this gate before screening. The source writer repeats it under
the world lock, so a concurrent forget cannot race a checked upload into storage. A locator
digest has a versioned, length-unambiguous JSON input. Old raw external locators remain
readable so an earlier ledger still prevents re-import after an upgrade.
"""

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from grove.writer.errors import InvalidRecord
from grove.writer.tx import WorldTx

_LOCATORS = "MATCH (f:ForgetLedger) RETURN f.locators AS locators"
_PREFIX = "sha256:"
_VERSION = "grove.forget.locator.v1"


class ForgottenSource(InvalidRecord):
    """A checked source or message was already forgotten. No source content was written."""

    def __init__(self) -> None:
        super().__init__("scope_denied", "forgotten_source", "the source was already forgotten")


def _digest(*parts: str) -> str:
    if any(not isinstance(part, str) for part in parts):
        raise TypeError("source locator components must be text")
    value = json.dumps([_VERSION, *parts], ensure_ascii=True, separators=(",", ":"))
    return _PREFIX + hashlib.sha256(value.encode("ascii")).hexdigest()


def locator_key(external_locator: str) -> str:
    """The permanent whole-source key, independent of a replacement connector's row id."""
    return _digest("source", external_locator)


def message_locator_key(external_locator: str, message_key: str) -> str:
    """The permanent key for one message inside a source locator."""
    return _digest("message", external_locator, message_key)


def _held(reader: Any) -> frozenset[str]:
    read = reader.run if isinstance(reader, WorldTx) else reader.read
    result: set[str] = set()
    for row in read(_LOCATORS):
        locators = row["locators"]
        if locators is None:
            continue
        # Older ledgers could store one locator rather than a list.
        if isinstance(locators, str):
            locators = [locators]
        if not isinstance(locators, list | tuple) or any(
            not isinstance(locator, str) for locator in locators
        ):
            raise InvalidRecord("unavailable", "forget_ledger", "the source gate is unavailable")
        result.update(locators)
    return frozenset(result)


def source_is_forgotten(reader: Any, external_locator: str) -> bool:
    """Read-only whole-source check. The caller binds the reader to the target world."""
    held = _held(reader)
    return external_locator in held or locator_key(external_locator) in held


def blocked_message_keys(
    reader: Any, external_locator: str, message_keys: Iterable[str]
) -> frozenset[str]:
    """The input keys that the latest ledger excludes; no content reaches this function."""
    held = _held(reader)
    keys = frozenset(message_keys)
    if external_locator in held or locator_key(external_locator) in held:
        return keys
    return frozenset(key for key in keys if message_locator_key(external_locator, key) in held)


def refuse_forgotten_source(
    tx: WorldTx, external_locator: str, message_keys: Iterable[str] = ()
) -> None:
    """Check under the same lock as the cascade, before the first source write.

    A whole-source tombstone rejects an empty screened source too. Callers that take a
    narrower row lock must take the world lock first, to preserve the writer's lock order.
    """
    tx.lock_world()
    held = _held(tx)
    if (
        external_locator in held
        or locator_key(external_locator) in held
        or any(message_locator_key(external_locator, key) in held for key in message_keys)
    ):
        raise ForgottenSource()
