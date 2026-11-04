"""Host arguments fail before a store read; dates retain the person's whole local day."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from grove.api.errors import ApiError
from grove.reads.requests import decode_body, parse_changed, parse_recall, whole_days


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        True,
        {"world_id": "other"},
        {"scope": "grove.write"},
        {"about": None},
        {"about": ""},
        {"about": "\u200b"},
        {"about": "\ud800"},
        {"about": "x" * 1025},
        {"about": "x\n"},
        {"limit": True},
        {"limit": 0},
        {"limit": 51},
        {"limit": 1.0},
        {"kinds": "event"},
        {"areas": []},
        {"kinds": ["event", "event"]},
        {"source_types": ["calendar"]},
        {"include": ["body"]},
        {"hops": {"depth": 4}},
        {"hops": {"depth": False}},
        {"hops": {"direction": "upstream"}},
        {"hops": {"meanings": []}},
        {"time": {}},
        {"time": {"mode": []}},
        {"time": {"mode": "current", "date": "2026-01-01"}},
        {"time": {"mode": "latest", "n": True}},
        {"time": {"mode": "latest", "n": 51}},
        {"time": {"mode": "as_of", "date": "2026-01-01"}},
        {"time": {"mode": "as_of", "axis": "record", "date": "2026-01-01T12:00:00"}},
        {"time": {"mode": "as_of", "axis": "world", "date": "2026-02-30"}},
        {"time": {"mode": "as_of", "axis": "world", "date": "0001-01-01T00:00:00+01:00"}},
        {"time": {"mode": "between", "start": "2026-02-02", "end": "2026-02-01"}},
        {"cursor": ""},
        {"cursor": "x" * 2049},
    ],
)
def test_invalid_recall_is_typed_and_does_not_echo_input(payload):
    with pytest.raises(ApiError) as caught:
        parse_recall(payload)
    assert caught.value.status == 400
    assert caught.value.code == "invalid_arguments"
    assert not caught.value.retryable
    assert "field" in caught.value.details


def test_recall_preserves_every_filter_and_detaches_mutable_inputs():
    request = {
        "about": "Nacre lease",
        "kinds": ["plan"],
        "areas": ["area-1"],
        "source_types": ["said"],
        "include": ["history", "evidence", "ended"],
        "hops": {"meanings": ["depends"], "direction": "in", "depth": 3},
        "time": {"mode": "as_of", "axis": "world", "date": "2026-10-02T09:00:00+02:00"},
        "limit": 50,
        "cursor": "opaque",
    }
    parsed = parse_recall(request)
    request["kinds"].append("person")
    request["hops"]["meanings"].clear()
    assert parsed.about == "Nacre lease"
    assert parsed.kinds == ("plan",)
    assert parsed.areas == ("area-1",)
    assert parsed.source_types == ("said",)
    assert parsed.include == ("history", "evidence", "ended")
    assert parsed.hops.meanings == ("depends",)
    assert parsed.hops.direction == "in" and parsed.hops.depth == 3
    assert parsed.time.axis == "world"
    assert parsed.time.at == datetime(2026, 10, 2, 7, tzinfo=UTC)
    assert parsed.cursor == "opaque" and parsed.limit == 50


def test_default_recall_is_the_area_index():
    parsed = parse_recall({})
    assert parsed.about is None and parsed.hops is None
    assert parsed.time.mode == "current"
    assert parsed.kinds == parsed.areas == parsed.include == parsed.source_types == ()


def test_date_only_as_of_stays_a_local_calendar_date():
    parsed = parse_recall({"time": {"mode": "as_of", "axis": "record", "date": "2026-10-02"}})
    assert type(parsed.time.at) is date
    assert parsed.time.at == date(2026, 10, 2)


@pytest.mark.parametrize(
    "patch",
    [
        {"end": "2026-02-29"},
        {"end": "2026-10-01T12:00:00Z"},
        {"end": "9999-12-31"},
        {"start": "2026-10-03"},
        {"include_breaks": 1},
        {"world_id": "other"},
    ],
)
def test_changed_refuses_invalid_or_unhonoured_arguments(patch):
    with pytest.raises(ApiError):
        parse_changed({"start": "2026-10-01", "end": "2026-10-02", **patch})


def test_changed_keeps_filters_and_breaks():
    parsed = parse_changed(
        {
            "start": "2026-10-01",
            "end": "2026-10-02",
            "kinds": ["task"],
            "areas": ["area-1"],
            "include_breaks": True,
        }
    )
    assert parsed.start == date(2026, 10, 1) and parsed.end == date(2026, 10, 2)
    assert parsed.kinds == ("task",) and parsed.areas == ("area-1",)
    assert parsed.include_breaks is True


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_between_includes_the_whole_end_day_across_dst(day, hours):
    lower, upper = whole_days(day, day, ZoneInfo("America/New_York"))
    assert upper - lower == timedelta(hours=hours)
    assert lower.astimezone(ZoneInfo("America/New_York")).date() == day
    assert (upper - timedelta(microseconds=1)).astimezone(
        ZoneInfo("America/New_York")
    ).date() == day


def test_between_includes_end_day_in_a_non_hour_offset():
    lower, upper = whole_days(date(2026, 10, 1), date(2026, 10, 2), ZoneInfo("Asia/Kathmandu"))
    assert lower == datetime(2026, 9, 30, 18, 15, tzinfo=UTC)
    assert upper == datetime(2026, 10, 2, 18, 15, tzinfo=UTC)


def test_unrepresentable_utc_boundary_is_a_typed_error():
    with pytest.raises(ApiError):
        whole_days(date.min, date.min, ZoneInfo("Asia/Kathmandu"))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"about":"one","about":"two"}',
        b'{"hops":{"depth":1,"depth":2}}',
        b'{"time":{"mode":"current","m\\u006fde":"latest"}}',
        b'{"n":NaN}',
        b'{"n":1e999}',
        b'{"about":"\\ud800"}',
        b"\xef\xbb\xbf{}",
        b"\xff",
        '{"about":"test"}'.encode("utf-16"),
    ],
)
def test_wire_json_refuses_duplicates_and_non_json_values(raw):
    with pytest.raises(ApiError) as caught:
        decode_body(raw)
    assert caught.value.code == "invalid_arguments" and caught.value.status == 400


def test_wire_json_retains_unicode_and_refuses_oversize():
    assert decode_body('{"about":"café بحر"}'.encode()) == {"about": "café بحر"}
    with pytest.raises(ApiError) as caught:
        decode_body(b" " * 1_048_577)
    assert caught.value.status == 413
