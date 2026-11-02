"""Instants in and out of the views: ISO 8601 with a timezone in, UTC with a Z out."""

from datetime import UTC, datetime

MAX_INSTANT_TEXT = 64


class InvalidInstant(ValueError):
    """A time parameter is not an ISO 8601 instant with a timezone. The message names the field."""

    def __init__(self, field: str) -> None:
        super().__init__(f"{field} must be an ISO 8601 date and time with a timezone")
        self.field = field


def parse_instant(text: str | None, field: str) -> datetime | None:
    """None when the parameter is absent; an aware UTC datetime; InvalidInstant otherwise.

    A date alone or a time with no offset names no instant, so it is refused, as is empty text. So
    is a time whose offset takes it past the first or the last year a datetime can hold in UTC.
    """
    if text is None:
        return None
    if not text or len(text) > MAX_INSTANT_TEXT:
        raise InvalidInstant(field)
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidInstant(field) from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInstant(field)
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise InvalidInstant(field) from None


def instant(value: object) -> datetime | None:
    """A stored instant as an aware datetime; anything else (absent, malformed) as None."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return None


def iso_z(value: object) -> str | None:
    """`2026-11-02T09:30:00.000Z`, the form of JavaScript's toISOString; None when absent."""
    moment = instant(value)
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
