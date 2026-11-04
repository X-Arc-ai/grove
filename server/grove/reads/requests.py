"""Pure D7 request validation. Registry names are resolved within the captured world later.

No client field selects a world, a clock, a source classification, or an access scope.
The REST and MCP adapters share these parsers. Accepted fields must be consumed by the read
engine; unknown fields are errors, never silently ignored.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from grove.api.errors import ApiError, invalid_arguments
from grove.store.enums import ENUMS
from grove.store.normalise import reads_blank
from grove.strict_json import loads_strict

MAX_BODY_BYTES = 1_048_576
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class TimeFilter:
    mode: str = "current"
    axis: str | None = None
    at: date | datetime | None = None
    start: date | None = None
    end: date | None = None
    n: int | None = None


@dataclass(frozen=True)
class Hops:
    meanings: tuple[str, ...] = ()
    direction: str = "both"
    depth: int = 1


@dataclass(frozen=True)
class RecallQuery:
    about: str | None
    kinds: tuple[str, ...]
    areas: tuple[str, ...]
    source_types: tuple[str, ...]
    hops: Hops | None
    time: TimeFilter
    include: tuple[str, ...]
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class ChangedQuery:
    start: date
    end: date
    kinds: tuple[str, ...]
    areas: tuple[str, ...]
    include_breaks: bool


def _object(value: object, allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise invalid_arguments("an object is required", field)
    if set(value) - allowed:
        # Do not echo unknown keys: a malformed key can itself contain private text.
        raise invalid_arguments("the object contains an unknown field", field)
    return value


def _text(value: object, field: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or reads_blank(value):
        raise invalid_arguments("a nonblank bounded string is required", field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise invalid_arguments("the string is not valid Unicode", field) from error
    if any(ord(char) < 32 for char in value):
        raise invalid_arguments("control characters are not allowed", field)
    return value


def _integer(value: object, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise invalid_arguments("the integer is outside the allowed range", field)
    return value


def _strings(value: object, field: str, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise invalid_arguments("a nonempty list of at most 100 strings is required", field)
    result = tuple(_text(part, field) for part in value)
    if len(set(result)) != len(result):
        raise invalid_arguments("duplicate entries are not allowed", field)
    if allowed is not None and any(part not in allowed for part in result):
        raise invalid_arguments("an entry is outside the allowed values", field)
    return result


def _day(value: object, field: str) -> date:
    if not isinstance(value, str) or _DAY.fullmatch(value) is None:
        raise invalid_arguments("a date in YYYY-MM-DD form is required", field)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise invalid_arguments("the calendar date is invalid", field) from error


def _instant_or_day(value: object) -> datetime | date:
    text = _text(value, "time.date", maximum=64)
    if _DAY.fullmatch(text):
        return _day(text, "time.date")
    try:
        if "T" not in text:
            raise ValueError
        moment = datetime.fromisoformat(text)
        if moment.utcoffset() is None:
            raise ValueError
        return moment.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise invalid_arguments(
            "an ISO instant with an offset or a date is required", "time.date"
        ) from error


def _range(data: dict, prefix: str = "") -> tuple[date, date]:
    start = _day(data.get("start"), prefix + "start")
    end = _day(data.get("end"), prefix + "end")
    if start > end or end == date.max:
        raise invalid_arguments("the inclusive date range is invalid", prefix + "end")
    return start, end


def parse_time(value: object) -> TimeFilter:
    data = _object(value, {"mode", "axis", "date", "start", "end", "n"}, "time")
    mode = data.get("mode")
    fields = {
        "current": {"mode"},
        "as_of": {"mode", "date", "axis"},
        "between": {"mode", "start", "end"},
        "latest": {"mode", "n"},
    }
    if not isinstance(mode, str) or mode not in fields:
        raise invalid_arguments("the time mode is invalid", "time.mode")
    _object(data, fields[mode], "time")
    if mode == "as_of":
        axis = data.get("axis")
        if axis not in ("world", "record"):
            raise invalid_arguments("an axis of world or record is required", "time.axis")
        return TimeFilter(mode, axis=axis, at=_instant_or_day(data.get("date")))
    if mode == "between":
        start, end = _range(data, "time.")
        return TimeFilter(mode, start=start, end=end)
    if mode == "latest":
        return TimeFilter(mode, n=_integer(data.get("n", 10), "time.n", 1, 50))
    return TimeFilter()


def parse_recall(value: object) -> RecallQuery:
    data = _object(
        value,
        {
            "about",
            "kinds",
            "areas",
            "source_types",
            "hops",
            "time",
            "include",
            "limit",
            "cursor",
        },
        "request",
    )
    hops = None
    if "hops" in data:
        hop = _object(data["hops"], {"meanings", "direction", "depth"}, "hops")
        direction = hop.get("direction", "both")
        if direction not in ("out", "in", "both"):
            raise invalid_arguments("the hop direction is invalid", "hops.direction")
        hops = Hops(
            _strings(hop["meanings"], "hops.meanings") if "meanings" in hop else (),
            direction,
            _integer(hop.get("depth", 1), "hops.depth", 1, 3),
        )
    return RecallQuery(
        about=_text(data["about"], "about") if "about" in data else None,
        kinds=_strings(data["kinds"], "kinds") if "kinds" in data else (),
        areas=_strings(data["areas"], "areas") if "areas" in data else (),
        source_types=_strings(data["source_types"], "source_types", ENUMS["source_type"])
        if "source_types" in data
        else (),
        hops=hops,
        time=parse_time(data["time"]) if "time" in data else TimeFilter(),
        include=_strings(data["include"], "include", ("history", "evidence", "ended"))
        if "include" in data
        else (),
        limit=_integer(data.get("limit", 50), "limit", 1, 50),
        cursor=_text(data["cursor"], "cursor", maximum=2048) if "cursor" in data else None,
    )


def parse_changed(value: object) -> ChangedQuery:
    data = _object(value, {"start", "end", "kinds", "areas", "include_breaks"}, "request")
    start, end = _range(data)
    breaks = data.get("include_breaks", False)
    if type(breaks) is not bool:
        raise invalid_arguments("a boolean is required", "include_breaks")
    return ChangedQuery(
        start,
        end,
        _strings(data["kinds"], "kinds") if "kinds" in data else (),
        _strings(data["areas"], "areas") if "areas" in data else (),
        breaks,
    )


def whole_days(start: date, end: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Inclusive local dates become an exclusive UTC interval; DST days need not be 24 hours."""
    if start > end or end == date.max:
        raise invalid_arguments("the inclusive date range is invalid", "end")
    lower = datetime.combine(start, time.min, zone)
    upper = datetime.combine(end + timedelta(days=1), time.min, zone)
    try:
        return lower.astimezone(UTC), upper.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise invalid_arguments(
            "the date range is outside supported UTC instants", "time"
        ) from error


def decode_body(raw: bytes) -> object:
    """The raw REST body gate. Duplicate keys are refused at every object depth."""
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "invalid_arguments", "the request body is too large")

    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        # Keep the shared UTF-8, finite-number, depth, and surrogate contract. The second
        # bounded parse adds duplicate-key detection without weakening that shared parser.
        loads_strict(raw)
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, RecursionError) as error:
        raise invalid_arguments("the request body is not strict JSON", "request") from error
