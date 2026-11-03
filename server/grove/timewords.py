"""Time words resolved in code (D4, "Where the values come from").

A model returns the words a message used ("tomorrow at 9", "in July", "rel:in two days") and never
a date. This module turns those words into a clock, against the message's own time and the
person's timezone at that moment, and keeps the words. It reads no clock: the anchor is the
evidence's own time, so the same words give the same clock on every run.

Grammar (case does not matter):

- `rel:<words>` is the extraction schema's form of a relative horizon; the words follow below.
- `in <n> <unit>`, `<n> <unit> from now`, `after <n> <unit>`: the anchor plus the span, where
  `<n>` is a digit string, `a`, `an` or a number word up to twelve, and the unit is minutes,
  hours, days, weeks or months (singular or plural).
- `today`, `tomorrow`, `yesterday`, `tonight`, each with an optional time (`at 9`, `at 9:30`,
  `at 9pm`, `at noon`, `at midnight`, `at 21:00 UTC`).
- a weekday: `friday`, `on friday`, `this friday`, `next friday` (the first one after the
  anchor's local day), `last friday` (the last one before it), with an optional time.
- a month: `july`, `in july`, `in july 2027`: the bounds of that month, in the person's
  timezone; with no year, the first such month that has not ended at the anchor.
- a day: `12 july`, `july 12`, `12 july 2027`, `2026-07-12`, with an optional time.
- an ISO 8601 instant with an offset (`2026-07-12T09:00:00+04:00`): that instant.

A time with `UTC` (or `GMT`, `Z`) is read in UTC, never in the person's timezone. Any other text
raises TimeWordsError, and the caller keeps the words with the clock "unknown".
"""

import re
import zoneinfo
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from grove.writer.clocks import Clock


class TimeWordsError(ValueError):
    """The words are not in the grammar. The message names the rule, never the words."""


_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_UNITS = {
    "minute": ("minutes", "minute"),
    "hour": ("hours", "hour"),
    "day": ("days", "day"),
    "week": ("weeks", "week"),
    "month": ("months", "month"),
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_ABBREVIATIONS = {name[:3]: index for index, name in enumerate(_MONTHS, start=1)}
_UTC_WORDS = frozenset({"utc", "gmt", "z"})
_SPACES = re.compile(r"\s+")

_NUMBER = r"(?P<n>\d{1,4}|" + "|".join(_NUMBERS) + r")"
_UNIT = r"(?P<unit>minutes?|hours?|days?|weeks?|months?)"
_SPAN_FORMS = (
    re.compile(rf"in {_NUMBER} {_UNIT}"),
    re.compile(rf"{_NUMBER} {_UNIT} from now"),
    re.compile(rf"after {_NUMBER} {_UNIT}"),
)
_TIME = (
    r"(?: at (?P<clock>noon|midnight|\d{1,2}(?::\d{2})?(?: ?(?:am|pm))?)"
    r"(?: (?P<zone>utc|gmt|z))?)?"
)
_DAY_WORDS = re.compile(rf"(?P<day>today|tomorrow|yesterday|tonight){_TIME}")
_WEEKDAY = re.compile(
    rf"(?:(?P<which>on|this|next|last) )?(?P<weekday>{'|'.join(_WEEKDAYS)}){_TIME}"
)
_MONTH_NAME = r"(?P<month>" + "|".join(_MONTHS) + "|" + "|".join(_MONTH_ABBREVIATIONS) + r")"
_MONTH = re.compile(rf"(?:in )?{_MONTH_NAME}(?: (?P<year>\d{{4}}))?")
_DAY_MONTH = re.compile(rf"(?:on )?(?P<dom>\d{{1,2}}) {_MONTH_NAME}(?: (?P<year>\d{{4}}))?{_TIME}")
_MONTH_DAY = re.compile(rf"(?:on )?{_MONTH_NAME} (?P<dom>\d{{1,2}})(?: (?P<year>\d{{4}}))?{_TIME}")
_ISO_DAY = re.compile(rf"(?:on )?(?P<iso>\d{{4}}-\d{{2}}-\d{{2}}){_TIME}")


@dataclass(frozen=True)
class _Local:
    zone: tzinfo
    anchor: datetime

    @property
    def day(self) -> date:
        return self.anchor.astimezone(self.zone).date()


def _zone(name: str) -> tzinfo:
    if not isinstance(name, str) or not name:
        raise TimeWordsError("timezone is an IANA name")
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise TimeWordsError("timezone is not a known IANA name") from error


def _words_of(text: str) -> str:
    if not isinstance(text, str):
        raise TimeWordsError("time words are text")
    folded = _SPACES.sub(" ", text.strip().lower()).rstrip(".")
    if folded.startswith("rel:"):
        folded = folded[4:].strip()
    if not folded:
        raise TimeWordsError("time words are empty")
    return folded


def _count(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBERS[value]


def _add_months(at: datetime, months: int) -> datetime:
    month_index = at.month - 1 + months
    year, month = at.year + month_index // 12, month_index % 12 + 1
    return at.replace(year=year, month=month, day=min(at.day, monthrange(year, month)[1]))


def _span(words: str, local: _Local) -> Clock | None:
    for form in _SPAN_FORMS:
        found = form.fullmatch(words)
        if found is None:
            continue
        n = _count(found["n"])
        unit = found["unit"].rstrip("s")
        if unit == "month":
            point = _add_months(local.anchor.astimezone(local.zone), n).astimezone(UTC)
        else:
            point = local.anchor + timedelta(**{_UNITS[unit][0]: n})
        return Clock(_UNITS[unit][1], point, point)
    return None


def _clock_time(text: str) -> time:
    if text == "noon":
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    found = re.fullmatch(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?: ?(?P<half>am|pm))?", text)
    if found is None:
        raise TimeWordsError("a time is H, H:MM, Ham or Hpm")
    hour, minute = int(found["h"]), int(found["m"] or 0)
    if found["half"]:
        if not 1 <= hour <= 12:
            raise TimeWordsError("an am or pm hour runs from 1 to 12")
        hour = hour % 12 + (12 if found["half"] == "pm" else 0)
    if hour > 23 or minute > 59:
        raise TimeWordsError("a time runs from 00:00 to 23:59")
    return time(hour, minute)


def _on_day(day: date, found: re.Match, local: _Local, words: str) -> Clock:
    """The bounds of a local day, or one instant on it when the words give a time."""
    clock_text = found["clock"]
    if clock_text is None:
        start = datetime.combine(day, time(0, 0), local.zone)
        end = datetime.combine(day + timedelta(days=1), time(0, 0), local.zone)
        return Clock(
            "day", start.astimezone(UTC), (end - timedelta(microseconds=1)).astimezone(UTC), words
        )
    zone = UTC if found["zone"] in _UTC_WORDS else local.zone
    at = datetime.combine(day, _clock_time(clock_text), zone).astimezone(UTC)
    return Clock("minute", at, at, words)


def _day_words(words: str, local: _Local, original: str) -> Clock | None:
    found = _DAY_WORDS.fullmatch(words)
    if found is None:
        return None
    offset = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}[found["day"]]
    if found["day"] == "tonight" and found["clock"] is None:
        day = local.day
        start = datetime.combine(day, time(18, 0), local.zone)
        end = datetime.combine(day + timedelta(days=1), time(0, 0), local.zone)
        return Clock(
            "hour",
            start.astimezone(UTC),
            (end - timedelta(microseconds=1)).astimezone(UTC),
            original,
        )
    # A time with UTC names a UTC date too: "tomorrow at 9 UTC" is the next UTC day.
    base = local.anchor.astimezone(UTC).date() if found["zone"] in _UTC_WORDS else local.day
    return _on_day(base + timedelta(days=offset), found, local, original)


def _weekday(words: str, local: _Local, original: str) -> Clock | None:
    found = _WEEKDAY.fullmatch(words)
    if found is None:
        return None
    target = _WEEKDAYS.index(found["weekday"])
    today = local.day
    if found["which"] == "last":
        back = (today.weekday() - target) % 7 or 7
        day = today - timedelta(days=back)
    else:
        ahead = (target - today.weekday()) % 7 or 7
        day = today + timedelta(days=ahead)
    return _on_day(day, found, local, original)


def _month_number(name: str) -> int:
    if name in _MONTHS:
        return _MONTHS.index(name) + 1
    return _MONTH_ABBREVIATIONS[name]


def _month(words: str, local: _Local, original: str) -> Clock | None:
    found = _MONTH.fullmatch(words)
    if found is None:
        return None
    month = _month_number(found["month"])
    today = local.day
    if found["year"]:
        year = int(found["year"])
    else:
        year = today.year if month >= today.month else today.year + 1
    start = datetime(year, month, 1, tzinfo=local.zone)
    end = _add_months(start, 1)
    return Clock(
        "month",
        start.astimezone(UTC),
        (end - timedelta(microseconds=1)).astimezone(UTC),
        original,
    )


def _calendar_day(words: str, local: _Local, original: str) -> Clock | None:
    for form in (_DAY_MONTH, _MONTH_DAY):
        found = form.fullmatch(words)
        if found is None:
            continue
        month, dom = _month_number(found["month"]), int(found["dom"])
        today = local.day
        if found["year"]:
            year = int(found["year"])
        else:
            year = today.year
            if (month, dom) < (today.month, today.day):
                year += 1
        try:
            day = date(year, month, dom)
        except ValueError as error:
            raise TimeWordsError("the day does not exist in that month") from error
        return _on_day(day, found, local, original)
    found = _ISO_DAY.fullmatch(words)
    if found is not None:
        try:
            day = date.fromisoformat(found["iso"])
        except ValueError as error:
            raise TimeWordsError("the date does not exist") from error
        return _on_day(day, found, local, original)
    return None


def _instant(text: str) -> Clock | None:
    candidate = text.strip()
    if "t" not in candidate.lower() or not re.match(r"\d{4}-\d{2}-\d{2}[tT ]", candidate):
        return None
    try:
        at = datetime.fromisoformat(candidate.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None or at.utcoffset() is None:
        # A time with no offset is local time with no zone named: never guessed.
        raise TimeWordsError("an ISO time needs its offset")
    point = at.astimezone(UTC)
    return Clock("minute", point, point, text.strip())


def resolve(text: str, *, anchor: datetime, timezone: str) -> Clock:
    """The clock the words name, against the evidence's own time and the person's timezone then.

    `anchor` is an aware time (the message's time); `timezone` the person's IANA zone at that
    moment. The result keeps the original words."""
    if not isinstance(anchor, datetime) or anchor.tzinfo is None or anchor.utcoffset() is None:
        raise TimeWordsError("the anchor is an aware time")
    local = _Local(_zone(timezone), anchor.astimezone(UTC))
    instant = _instant(text) if isinstance(text, str) else None
    if instant is not None:
        return instant
    words = _words_of(text)
    original = text.strip()
    spanned = _span(words, local)
    if spanned is not None:
        return Clock(spanned.precision, spanned.earliest, spanned.latest, original)
    for rule in (_day_words, _weekday, _month, _calendar_day):
        found = rule(words, local, original)
        if found is not None:
            return found
    raise TimeWordsError("the words are not in the time grammar")


def horizon(text: str, *, anchor: datetime, timezone: str) -> datetime:
    """A re-check by from words: the latest instant the words allow, so a check never comes
    before the time they name has passed (`rel:in two days` is two days after the message)."""
    clock = resolve(text, anchor=anchor, timezone=timezone)
    if clock.latest is None:
        raise TimeWordsError("the words name no latest time")
    return clock.latest
