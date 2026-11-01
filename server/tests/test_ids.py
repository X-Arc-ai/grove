from datetime import UTC, datetime, timedelta
from uuid import UUID

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id

T0 = datetime(2026, 3, 4, 5, 6, 7, 123000, tzinfo=UTC)


def _millis(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def test_layout_is_uuid_version_7_with_the_rfc_variant():
    value = UUID(new_id())
    assert value.version == 7
    assert (value.int >> 76) & 0xF == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_text_form_is_the_canonical_36_characters():
    text = new_id()
    assert len(text) == 36
    assert text == text.lower()
    assert str(UUID(text)) == text


def test_time_bits_come_from_the_provider():
    test = TestClock()
    test.set(T0)
    with clock.using(test):
        value = UUID(new_id())
    assert value.int >> 80 == _millis(T0)


def test_a_world_clock_sets_the_time_bits_of_that_worlds_ids():
    later = T0 + timedelta(days=400)
    test = TestClock()
    test.set(T0)
    test.set(later, world_id="w1")
    with clock.using(test):
        assert UUID(new_id("w1")).int >> 80 == _millis(later)
        assert UUID(new_id("w2")).int >> 80 == _millis(T0)


def test_ids_under_a_frozen_clock_still_differ():
    test = TestClock()
    test.set(T0)
    with clock.using(test):
        assert len({new_id() for _ in range(1000)}) == 1000


def test_ids_sort_by_time():
    test = TestClock()
    test.set(T0)
    with clock.using(test):
        first = new_id()
        test.advance(milliseconds=1)
        second = new_id()
    assert first < second
