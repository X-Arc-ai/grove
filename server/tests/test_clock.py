import os
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from grove import clock
from grove.clock import ClockNotSet, SystemClock, TestClock

T0 = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_system_clock_agrees_with_the_filesystem_clock(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    written = os.stat(marker).st_mtime

    now = SystemClock().now()

    assert now.utcoffset() == timedelta(0)
    assert abs(now.timestamp() - written) < 5


def test_system_clock_monotonic_does_not_go_back():
    system = SystemClock()
    first = system.monotonic()
    assert system.monotonic() >= first


def test_default_provider_is_the_system_clock():
    with clock.using(TestClock()):
        pass
    assert clock.now().tzinfo is not None
    assert isinstance(clock.monotonic(), float)


def test_test_clock_never_falls_back_to_the_system_clock():
    test = TestClock()
    with pytest.raises(ClockNotSet):
        test.now()
    with pytest.raises(ClockNotSet):
        test.now("w1")


def test_a_world_with_no_time_uses_the_default_and_its_own_time_wins():
    test = TestClock()
    test.set(T0)
    assert test.now("w1") == T0

    later = T0 + timedelta(days=1)
    test.set(later, world_id="w1")
    assert test.now("w1") == later
    assert test.now("w2") == T0
    assert test.now() == T0


def test_a_naive_datetime_is_refused():
    test = TestClock()
    with pytest.raises(ValueError):
        test.set(datetime(2026, 1, 1))  # noqa: DTZ001


def test_a_time_in_another_zone_reads_back_in_utc():
    test = TestClock()
    test.set(datetime(2026, 1, 1, 4, 0, tzinfo=timezone(timedelta(hours=4))))
    assert test.now() == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert test.now().utcoffset() == timedelta(0)


def test_advance_moves_one_world_and_the_monotonic_reading():
    test = TestClock()
    test.set(T0)
    test.set(T0, world_id="w1")
    before = test.monotonic()

    assert test.advance(world_id="w1", hours=2) == T0 + timedelta(hours=2)

    assert test.now("w1") == T0 + timedelta(hours=2)
    assert test.now() == T0
    assert test.monotonic() == before + 7200


def test_advance_of_a_world_with_only_the_default_gives_it_its_own_time():
    test = TestClock()
    test.set(T0)
    test.advance(world_id="w1", minutes=1)
    assert test.now("w1") == T0 + timedelta(minutes=1)
    assert test.now("w2") == T0


def test_advance_with_no_time_set_raises():
    with pytest.raises(ClockNotSet):
        TestClock().advance(seconds=1)


def test_advance_refuses_negative_time():
    test = TestClock()
    test.set(T0)
    with pytest.raises(ValueError):
        test.advance(seconds=-1)
    assert test.now() == T0


def test_module_functions_read_the_installed_clock():
    test = TestClock()
    test.set(T0)
    test.set(T0 + timedelta(days=9), world_id="w1")
    with clock.using(test):
        assert clock.now() == T0
        assert clock.today() == date(2026, 3, 4)
        assert clock.today("w1") == date(2026, 3, 13)
        clock_before = clock.monotonic()
        test.advance(seconds=3)
        assert clock.monotonic() == clock_before + 3


def test_using_restores_the_previous_clock_after_an_error():
    outer, inner = TestClock(), TestClock()
    outer.set(T0)
    with clock.using(outer):
        with pytest.raises(RuntimeError, match="boom"):
            with clock.using(inner):
                raise RuntimeError("boom")
        assert clock.now() == T0


def test_install_of_a_test_clock_is_refused_in_production(monkeypatch):
    monkeypatch.setenv("GROVE_ENV", "production")
    with pytest.raises(RuntimeError, match="production"):
        clock.install(TestClock())
    with pytest.raises(RuntimeError, match="production"):
        with clock.using(TestClock()):
            pass
    assert isinstance(clock.now(), datetime)


def test_install_of_the_system_clock_is_allowed_in_production(monkeypatch):
    monkeypatch.setenv("GROVE_ENV", "production")
    clock.install(SystemClock())
