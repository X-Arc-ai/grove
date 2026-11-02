"""World clocks (D4, S3): became true and stopped being true, parsed before the store is touched.

A clock is "unknown" or {earliest, latest, precision, words}. `before` has only latest, `after`
only earliest, `unknown` neither, and every other precision both, with earliest not after latest.
Times are ISO 8601 with an offset; the writer keeps UTC.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from grove.store.enums import ENUMS

UNKNOWN_WORD = "unknown"
_KEYS = frozenset({"earliest", "latest", "precision", "words"})
_BOUNDS = ("earliest", "latest")


class ClockError(ValueError):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class Clock:
    precision: str
    earliest: datetime | None = None
    latest: datetime | None = None
    words: str | None = None

    @property
    def unknown(self) -> bool:
        return self.precision == UNKNOWN_WORD

    def to_json(self) -> dict[str, Any] | str:
        if self.unknown and self.words is None:
            return UNKNOWN_WORD
        shown: dict[str, Any] = {"precision": self.precision}
        if self.earliest is not None:
            shown["earliest"] = self.earliest.isoformat()
        if self.latest is not None:
            shown["latest"] = self.latest.isoformat()
        if self.words is not None:
            shown["words"] = self.words
        return shown


UNKNOWN = Clock(UNKNOWN_WORD)


def parse_instant(value: Any) -> datetime:
    """An aware datetime or an ISO 8601 string with an offset, as UTC."""
    if isinstance(value, datetime):
        at = value
    elif isinstance(value, str) and value.strip():
        try:
            at = datetime.fromisoformat(value.strip())
        except ValueError as error:
            raise ClockError("instant_format", f"not an ISO 8601 time: {value!r}") from error
    else:
        raise ClockError("instant_format", "a time is an ISO 8601 string with an offset")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ClockError("instant_offset", "a time needs its offset; the writer never guesses one")
    return at.astimezone(UTC)


def parse_clock(value: Any) -> Clock:
    if value == UNKNOWN_WORD:
        return UNKNOWN
    if not isinstance(value, dict):
        raise ClockError("clock_shape", 'a clock is "unknown" or an object with a precision')
    extra = set(value) - _KEYS
    if extra:
        raise ClockError("clock_field", f"a clock has no field {sorted(extra)[0]!r}")
    precision = value.get("precision")
    if precision not in ENUMS["precision"]:
        raise ClockError("clock_precision", f"precision must be one of {list(ENUMS['precision'])}")
    words = value.get("words")
    if words is not None and (not isinstance(words, str) or not words.strip()):
        raise ClockError("clock_words", "words, when sent, are the original non-empty text")
    bounds = {key: parse_instant(value[key]) for key in _BOUNDS if value.get(key) is not None}
    wanted = {"unknown": set(), "before": {"latest"}, "after": {"earliest"}}.get(
        precision, set(_BOUNDS)
    )
    if set(bounds) != wanted:
        need = " and ".join(sorted(wanted)) or "no bounds"
        raise ClockError("clock_bounds", f"precision {precision} takes {need}")
    earliest, latest = bounds.get("earliest"), bounds.get("latest")
    if earliest is not None and latest is not None and earliest > latest:
        raise ClockError("clock_order", "earliest is after latest")
    return Clock(precision, earliest, latest, words)


def sort_key(clock: Clock | None) -> datetime | None:
    """Where a clock sits on the world axis: its earliest bound, else its latest, else None."""
    if clock is None or clock.unknown:
        return None
    return clock.earliest if clock.earliest is not None else clock.latest


def ends_before_start(start: Clock | None, stop: Clock | None) -> bool:
    """True only when every instant the stop allows is before every instant the start allows."""
    if start is None or stop is None:
        return False
    return stop.latest is not None and start.earliest is not None and stop.latest < start.earliest


def clock_columns(prefix: str, clock: Clock | None) -> dict[str, Any]:
    """The three columns of a clock; None marks a column the row must not hold."""
    if clock is None:
        return {f"{prefix}_earliest": None, f"{prefix}_latest": None, f"{prefix}_precision": None}
    return {
        f"{prefix}_earliest": clock.earliest,
        f"{prefix}_latest": clock.latest,
        f"{prefix}_precision": clock.precision,
    }


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_instant(value)
    return value.astimezone(UTC)


def clock_from_row(row: dict[str, Any], prefix: str) -> Clock | None:
    precision = row.get(f"{prefix}_precision")
    if precision is None:
        return None
    return Clock(
        precision, _as_utc(row.get(f"{prefix}_earliest")), _as_utc(row.get(f"{prefix}_latest"))
    )
