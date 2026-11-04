"""D4's time-as-data tests for the words a model returns: resolved in code, never by a model.

Written from the spec's "How to test it" for D4 (time as data) and its "Where the values come
from": a relative expression is resolved against the message's own time and the person's timezone
at that moment, a UTC time is never read as local, a month keeps its bounds and its precision, and
the same words give the same clock on every run. Every expected instant is computed by hand.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from grove import clock
from grove.clock import TestClock
from grove.timewords import TimeWordsError, horizon, resolve

DUBAI = "Asia/Dubai"  # UTC+04:00 all year, no daylight saving.
PLUS_4 = timezone(timedelta(hours=4))


def test_tomorrow_at_9_sent_at_01_30_in_dubai_is_9_on_the_next_dubai_day():
    # 01:30 on 10 July in Dubai is 21:30 on 9 July in UTC: the two calendars name different days.
    sent = datetime(2026, 7, 10, 1, 30, tzinfo=PLUS_4)
    assert sent.astimezone(UTC) == datetime(2026, 7, 9, 21, 30, tzinfo=UTC)

    found = resolve("tomorrow at 9", anchor=sent, timezone=DUBAI)

    # By hand: the next Dubai day is 11 July; 09:00 there is 05:00 UTC.
    expected = datetime(2026, 7, 11, 9, 0, tzinfo=PLUS_4)
    assert found.earliest == found.latest == expected
    assert found.earliest.astimezone(PLUS_4).date().isoformat() == "2026-07-11"
    assert found.precision == "minute"
    assert found.words == "tomorrow at 9"
    # Read against UTC (still 9 July), "tomorrow" is 10 July: later the same Dubai day.
    utc_reading = datetime(2026, 7, 10, 9, 0, tzinfo=PLUS_4)
    assert found.earliest != utc_reading
    assert found.earliest - utc_reading == timedelta(days=1)


def test_in_july_is_stored_with_julys_bounds_in_the_persons_timezone():
    sent = datetime(2026, 5, 3, 12, 0, tzinfo=PLUS_4)

    found = resolve("in July", anchor=sent, timezone=DUBAI)

    # By hand: 1 July 00:00 to 31 July 23:59:59.999999 in Dubai.
    assert found.precision == "month"
    assert found.earliest == datetime(2026, 7, 1, 0, 0, tzinfo=PLUS_4)
    assert found.latest == datetime(2026, 8, 1, 0, 0, tzinfo=PLUS_4) - timedelta(microseconds=1)
    assert found.earliest.astimezone(UTC) == datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
    assert found.words == "in July"
    # The clock carries its words and its precision to the writer.
    assert found.to_json() == {
        "precision": "month",
        "earliest": "2026-06-30T20:00:00+00:00",
        "latest": "2026-07-31T19:59:59.999999+00:00",
        "words": "in July",
    }


def test_in_july_after_july_has_ended_is_next_years_july():
    sent = datetime(2026, 8, 2, 12, 0, tzinfo=PLUS_4)
    found = resolve("in July", anchor=sent, timezone=DUBAI)
    assert found.earliest == datetime(2027, 7, 1, 0, 0, tzinfo=PLUS_4)


@pytest.mark.parametrize(
    "words", ["today at 14:00 UTC", "today at 14:00 utc", "2026-07-10T14:00:00Z"]
)
def test_a_release_time_written_in_utc_is_never_read_as_local(words):
    sent = datetime(2026, 7, 10, 10, 0, tzinfo=PLUS_4)  # 06:00 UTC, the same date in both.

    found = resolve(words, anchor=sent, timezone=DUBAI)

    assert found.earliest == found.latest == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    # The local reading would be 14:00 in Dubai, four hours earlier.
    assert found.earliest != datetime(2026, 7, 10, 14, 0, tzinfo=PLUS_4)


def test_today_at_14_00_without_utc_is_local():
    sent = datetime(2026, 7, 10, 10, 0, tzinfo=PLUS_4)
    found = resolve("today at 14:00", anchor=sent, timezone=DUBAI)
    assert found.earliest == datetime(2026, 7, 10, 14, 0, tzinfo=PLUS_4)


def test_rel_in_two_days_is_two_days_after_the_message():
    sent = datetime(2026, 7, 10, 1, 30, tzinfo=PLUS_4)

    found = resolve("rel:in two days", anchor=sent, timezone=DUBAI)

    assert found.earliest == found.latest == sent + timedelta(days=2)
    assert found.precision == "day"
    assert found.words == "rel:in two days"
    assert horizon("rel:in two days", anchor=sent, timezone=DUBAI) == sent + timedelta(days=2)


@pytest.mark.parametrize("words", ["2026-07-12T09:00:00", "2026-07-12 09:00"])
def test_an_iso_time_with_no_offset_is_refused(words):
    sent = datetime(2026, 7, 10, 10, 0, tzinfo=PLUS_4)
    with pytest.raises(TimeWordsError):
        resolve(words, anchor=sent, timezone=DUBAI)


@pytest.mark.parametrize(
    "words", ["sometime soonish", "when the moon is full", "the week after Rowan's party", ""]
)
def test_words_the_grammar_does_not_know_raise(words):
    sent = datetime(2026, 7, 10, 10, 0, tzinfo=PLUS_4)
    with pytest.raises(TimeWordsError):
        resolve(words, anchor=sent, timezone=DUBAI)


def test_an_anchor_with_no_offset_is_refused():
    with pytest.raises(TimeWordsError):
        resolve("tomorrow at 9", anchor=datetime(2026, 7, 10, 1, 30), timezone=DUBAI)


@pytest.mark.parametrize(
    "words", ["tomorrow at 9", "in July", "rel:in two days", "next friday at 9pm", "12 july"]
)
def test_the_same_words_give_the_same_clock_on_every_run(words):
    # The words resolve against the anchor only: whatever the clock provider says, the clock is
    # the same.
    sent = datetime(2026, 7, 10, 1, 30, tzinfo=PLUS_4)
    first = resolve(words, anchor=sent, timezone=DUBAI)
    results = []
    for provider_now in (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2030, 12, 31, 23, 59, tzinfo=UTC),
    ):
        test_clock = TestClock()
        test_clock.set(provider_now)
        with clock.using(test_clock):
            results.append(resolve(words, anchor=sent, timezone=DUBAI))
    assert results == [first, first]
