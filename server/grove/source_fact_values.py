"""Bounded, memory-only text fact comparisons. These records grant no authority.

This module deliberately does not import ingest or screening. A match selects the
whole original message; admission, authenticated intent and erasure belong elsewhere.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache, wraps
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from grove.source_facts import (
    DOCUMENT_TEXT,
    WHATSAPP_TEXT,
    LocalParts,
    TextSourceFacts,
    check_text_transport,
)

FactKind = Literal["local_candidate", "instant", "source_offset", "source_timezone"]
VERSION = "text_fact_values.v1"
MAX_MESSAGES = 1_000
MAX_KEY_CHARS = 1_024
MAX_KEY_BYTES = 1_024_000
MAX_RECORD_BYTES = 4_000_000
MAX_PROJECTION_BYTES = 2_048
_KINDS = ("local_candidate", "instant", "source_offset", "source_timezone")
_ERRORS = ("invalid_text_facts", "unsupported_fact_selector", "fact_projection_limit")
_LOCAL = re.compile(
    r"([0-9]{4}|[0-9]{2})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2}))?\Z"
)
_OFFSET = re.compile(r"([+-])([0-9]{2}):([0-9]{2})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class FactInterfaceError(ValueError):
    """Closed diagnostics, never a selector, key, timezone or candidate."""

    def __init__(self, code: str = "invalid_text_facts") -> None:
        self.code = code if type(code) is str and code in _ERRORS else _ERRORS[0]
        super().__init__(self.code)


def _safe(code="invalid_text_facts"):
    def decorate(function):
        @wraps(function)
        def checked(*args, **kwargs):
            failure = code
            try:
                return function(*args, **kwargs)
            except FactInterfaceError as error:
                failure = error.code
            except (
                ValueError,
                TypeError,
                AttributeError,
                OverflowError,
                OSError,
                RecursionError,
                ZoneInfoNotFoundError,
            ):
                pass
            # Raise outside the handler: even __context__ must not retain input errors.
            raise FactInterfaceError(failure)

        return checked

    return decorate


def _repr(self) -> str:
    return f"{type(self).__name__}()"


def _key_bytes(key: str) -> int:
    # N2: duplicate bounded_id's rejection locally, before encoding or set/hash use.
    if type(key) is not str or len(key) > MAX_KEY_CHARS:
        raise FactInterfaceError()
    return len(key.encode("utf-8"))


def _identity(identity: object) -> None:
    if (
        type(identity) is not tuple
        or len(identity) != 2
        or any(type(part) is not str or len(part) > 64 for part in identity)
        or identity not in (WHATSAPP_TEXT, DOCUMENT_TEXT)
    ):
        raise FactInterfaceError()


def _kind(kind: object) -> None:
    if type(kind) is not str or kind not in _KINDS:
        raise FactInterfaceError("unsupported_fact_selector")


def _local_text(parts: LocalParts) -> str:
    value = (
        f"{parts.year:0{parts.year_width}d}-{parts.month:02d}-{parts.day:02d}"
        f"T{parts.hour:02d}:{parts.minute:02d}"
    )
    return value + (f":{parts.second:02d}" if parts.precision == "second" else "")


def _utc_text(at: datetime) -> str:
    if type(at) is not datetime or at.utcoffset() is None or at.microsecond:
        raise FactInterfaceError()
    at = at.astimezone(UTC)
    if at.microsecond:
        raise FactInterfaceError()
    return (
        f"{at.year:04d}-{at.month:02d}-{at.day:02d}T{at.hour:02d}:{at.minute:02d}:{at.second:02d}Z"
    )


@cache
def _zone_keys() -> frozenset[str]:
    # ZoneInfo alone can accept incorrect case on a case-insensitive filesystem.
    # The installed IANA catalogue retains the actual spelling, including aliases.
    return frozenset(available_timezones())


def _canonical(kind: FactKind, value: str) -> str:
    _kind(kind)
    if type(value) is not str or not 0 < len(value) <= 512:
        raise FactInterfaceError("unsupported_fact_selector")
    value.encode("utf-8")
    if kind in ("local_candidate", "instant"):
        local = value[:-1] if kind == "instant" and value.endswith("Z") else value
        match = _LOCAL.fullmatch(local)
        if match is None or (kind == "instant" and (not value.endswith("Z") or len(match[1]) != 4)):
            raise FactInterfaceError("unsupported_fact_selector")
        parts = LocalParts(
            int(match[1]),
            len(match[1]),
            int(match[2]),
            int(match[3]),
            int(match[4]),
            int(match[5]),
            int(match[6] or "0"),
            "second" if match[6] is not None else "minute",
        )
        if kind == "instant":
            return _utc_text(parts.local_datetime().replace(tzinfo=UTC))
        return _local_text(parts)
    if kind == "source_offset":
        match = _OFFSET.fullmatch(value)
        if match is None or int(match[2]) > 23 or int(match[3]) > 59 or value == "-00:00":
            raise FactInterfaceError("unsupported_fact_selector")
        return value
    if len(value) > 128 or value != value.strip() or value not in _zone_keys():
        raise FactInterfaceError("unsupported_fact_selector")
    ZoneInfo(value)
    return value


@dataclass(frozen=True, slots=True, repr=False)
class FactValue:
    version: str
    kind: FactKind
    canonical: str

    @_safe()
    def __post_init__(self) -> None:
        if type(self) is not FactValue or type(self.version) is not str or self.version != VERSION:
            raise FactInterfaceError()
        if _canonical(self.kind, self.canonical) != self.canonical:
            raise FactInterfaceError()

    __repr__ = _repr


@dataclass(frozen=True, slots=True, repr=False)
class FactMessage:
    key: str
    at: datetime | None
    facts: TextSourceFacts

    @_safe()
    def __post_init__(self) -> None:
        if type(self) is not FactMessage or type(self.facts) is not TextSourceFacts:
            raise FactInterfaceError()
        _key_bytes(self.key)
        identity = WHATSAPP_TEXT if self.facts.locator.kind == "line_bytes" else DOCUMENT_TEXT
        check_text_transport(identity, self.facts, self.at)
        if self.at is not None:
            _utc_text(self.at)

    __repr__ = _repr


@dataclass(frozen=True, slots=True, repr=False)
class FactPredicate:
    version: str
    kind: FactKind
    value_bytes: int
    sha256: str

    @_safe("unsupported_fact_selector")
    def __post_init__(self) -> None:
        if (
            type(self) is not FactPredicate
            or type(self.version) is not str
            or self.version != VERSION
            or type(self.value_bytes) is not int
            or type(self.sha256) is not str
            or len(self.sha256) != 64
            or _DIGEST.fullmatch(self.sha256) is None
        ):
            raise FactInterfaceError("unsupported_fact_selector")
        _kind(self.kind)
        lengths = {
            "local_candidate": (14, 16, 17, 19),
            "instant": (20,),
            "source_offset": (6,),
            "source_timezone": range(1, 513),
        }
        if self.value_bytes not in lengths[self.kind]:
            raise FactInterfaceError("unsupported_fact_selector")

    __repr__ = _repr


@_safe()
def project_text_fact_values(
    facts: TextSourceFacts,
    *,
    at: datetime | None,
    source_identity: tuple[str, str],
) -> tuple[FactValue, ...]:
    _identity(source_identity)
    check_text_transport(source_identity, facts, at)
    values = []
    time = facts.time
    for value in sorted({_local_text(candidate) for candidate in time.local_candidates}):
        values.append(FactValue(VERSION, "local_candidate", value))
    if at is not None:
        values.append(FactValue(VERSION, "instant", _utc_text(at)))
    if time.source_offset_minutes is not None:
        offset = time.source_offset_minutes
        value = f"{'-' if offset < 0 else '+'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}"
        values.append(FactValue(VERSION, "source_offset", value))
    if time.source_timezone is not None:
        values.append(FactValue(VERSION, "source_timezone", time.source_timezone))
    if (
        len(values) > 5
        or sum(len(v.canonical.encode("utf-8")) for v in values) > MAX_PROJECTION_BYTES
    ):
        raise FactInterfaceError("fact_projection_limit")
    return tuple(values)


@_safe("unsupported_fact_selector")
def compile_text_fact_selector(*, kind: FactKind, value: str) -> FactPredicate:
    canonical = _canonical(kind, value).encode("utf-8")
    return FactPredicate(VERSION, kind, len(canonical), hashlib.sha256(canonical).hexdigest())


def _record_bytes(message: FactMessage) -> int:
    # N1: only this accounting view normalizes at. Source.content keeps ISO offsets.
    row = {
        "key": message.key,
        "at": None if message.at is None else _utc_text(message.at),
        "facts": message.facts.as_row(),
    }
    return len(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


@_safe()
def select_text_fact_message_keys(
    messages: Sequence[FactMessage],
    predicate: FactPredicate,
    *,
    source_identity: tuple[str, str],
) -> tuple[str, ...]:
    _identity(source_identity)
    if type(predicate) is not FactPredicate:
        raise FactInterfaceError("unsupported_fact_selector")
    predicate.__post_init__()
    if type(messages) not in (list, tuple):
        raise FactInterfaceError()
    if len(messages) > MAX_MESSAGES:
        raise FactInterfaceError("fact_projection_limit")
    seen = set()
    selected = []
    key_bytes = 0
    record_bytes = 2  # [] plus one comma between each pair of rows
    for index, message in enumerate(messages):
        if type(message) is not FactMessage:
            raise FactInterfaceError()
        message.__post_init__()
        key_bytes += _key_bytes(message.key)
        if key_bytes > MAX_KEY_BYTES:
            raise FactInterfaceError("fact_projection_limit")
        values = project_text_fact_values(
            message.facts,
            at=message.at,
            source_identity=source_identity,
        )
        record_bytes += _record_bytes(message) + (1 if index else 0)
        if record_bytes > MAX_RECORD_BYTES:
            raise FactInterfaceError("fact_projection_limit")
        if message.key in seen:
            raise FactInterfaceError()
        seen.add(message.key)
        for value in values:
            encoded = value.canonical.encode("utf-8")
            if (
                value.version == predicate.version
                and value.kind == predicate.kind
                and len(encoded) == predicate.value_bytes
                and hashlib.sha256(encoded).hexdigest() == predicate.sha256
            ):
                selected.append(message.key)
                break
    return tuple(selected)
