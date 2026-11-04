"""Bounded Calendar facts supplied by connector code, never extracted from event text."""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from grove.screening import ScreenedMessage
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import InvalidRecord

MAX_CALENDAR_EVENTS = 1000
MAX_CALENDAR_FACT_BYTES = 512_000
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_FIELDS = frozenset({"scheduled_at", "timezone", "status", "updated_at"})
_REQUIRED = _FIELDS - {"updated_at"}
_STATUSES = frozenset({"confirmed", "tentative", "cancelled"})


def _bad() -> InvalidRecord:
    return InvalidRecord("invalid_arguments", "calendar_facts", "invalid Calendar message facts")


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise _bad()
    try:
        return parse_instant(value).isoformat()
    except (ClockError, ValueError, TypeError):
        raise _bad() from None


def calendar_facts_json(facts: Any, messages: Sequence[ScreenedMessage]) -> str | None:
    """Validate facts against retained message keys and scheduled instants before storage.

    Each fact binds exactly `key:<message key>`. The timezone is the calendar's trusted log-day
    timezone. `updated_at` records a revision time and never establishes an occurrence time.
    """
    if facts is None:
        return None
    if not isinstance(facts, Mapping) or len(facts) > MAX_CALENDAR_EVENTS:
        raise _bad()
    retained = {}
    for message in messages:
        if message.key in facts:
            if message.key in retained:
                raise _bad()
            retained[message.key] = message.at
    checked = {}
    for key, entry in facts.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None or key not in retained:
            raise _bad()
        if not isinstance(entry, Mapping) or set(entry) - _FIELDS or not _REQUIRED <= set(entry):
            raise _bad()
        status, zone = entry["status"], entry["timezone"]
        if not isinstance(status, str) or status not in _STATUSES:
            raise _bad()
        if not isinstance(zone, str) or not 1 <= len(zone) <= 128:
            raise _bad()
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise _bad() from None
        scheduled, updated = _time(entry["scheduled_at"]), _time(entry.get("updated_at"))
        if scheduled is None and status != "cancelled":
            raise _bad()
        message_at = retained[key]
        if (scheduled is None) != (message_at is None):
            raise _bad()
        if scheduled is not None and parse_instant(scheduled) != message_at:
            raise _bad()
        checked[key] = {
            "scheduled_at": scheduled,
            "timezone": zone,
            "status": status,
            "updated_at": updated,
        }
    encoded = json.dumps(checked, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_CALENDAR_FACT_BYTES:
        raise _bad()
    return encoded if checked else None
