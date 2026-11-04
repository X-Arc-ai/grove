"""Strict synthetic facts; these tests do not activate a provider or write a source."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from grove.ingest.connector import CalendarEvent, ParsedMessage
from grove.source_facts import (
    DOCUMENT_TEXT,
    WHATSAPP_TEXT,
    LocalParts,
    SourceLocator,
    SourceTimeFacts,
    TextSourceFacts,
    check_text_transport,
)


def local(**changes):
    return LocalParts(
        **dict(
            dict(
                year=2026,
                year_width=4,
                month=4,
                day=13,
                hour=10,
                minute=20,
                second=0,
                precision="minute",
            ),
            **changes,
        )
    )


def facts(time=None, locator=None):
    return TextSourceFacts(
        "a" * 64,
        locator or SourceLocator("bytes", byte_start=0, byte_end=12),
        time or SourceTimeFacts("absent"),
    )


def test_facts_are_immutable_and_json_round_trip_preserves_exact_fields():
    value = facts(SourceTimeFacts("local", ("dmy",), (local(),)))
    assert TextSourceFacts.from_json(value.canonical_json()) == value
    assert value.encoded_size == len(value.canonical_json().encode()) <= 2048
    with pytest.raises(FrozenInstanceError):
        value.artifact_sha256 = "b" * 64
    with pytest.raises(FrozenInstanceError):
        value.time.local_candidates[0].year = 2027
    row = value.as_row()
    row["time"]["local_candidates"][0]["year"] = 2027
    assert value.time.local_candidates[0].year == 2026


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "a" * 100_000, None, True])
def test_artifact_digest_is_exact_and_bounded(digest):
    with pytest.raises(ValueError):
        replace(facts(), artifact_sha256=digest)


@pytest.mark.parametrize(
    "changes",
    [
        {"year": True},
        {"year_width": True},
        {"year_width": 3},
        {"year": 0},
        {"year": 100, "year_width": 2},
        {"month": 13},
        {"month": "4"},
        {"day": 31},
        {"hour": 24},
        {"minute": 60},
        {"second": 60},
        {"second": 1},
        {"precision": "millisecond"},
        {"year": 2025, "month": 2, "day": 29},
    ],
)
def test_local_components_refuse_wrong_types_ranges_and_precision(changes):
    with pytest.raises(ValueError):
        local(**changes)


def test_two_digit_year_retains_its_width_without_a_century_pivot():
    value = SourceTimeFacts("ambiguous", ("dmy",), (local(year=0, year_width=2),), 0)
    assert value.local_candidates[0].year == 0
    assert value.local_candidates[0].local_datetime() is None
    value.check_at(None)
    with pytest.raises(ValueError):
        value.check_at(datetime(2000, 4, 13, 10, 20, tzinfo=UTC))


@pytest.mark.parametrize(
    "changes",
    [
        {"date_order_candidates": ["dmy"]},
        {"date_order_candidates": ("dmy", "dmy")},
        {"date_order_candidates": ("unknown",)},
        {"date_order_candidates": ()},
        {"local_candidates": [local()]},
        {"local_candidates": (local(), local())},
        {"local_candidates": (local(), local(day=14), local(day=15))},
        {"local_candidates": ({"year": 2026},)},
        {"local_candidates": ()},
        {"source_offset_minutes": True},
        {"source_offset_minutes": 1440},
        {"source_offset_minutes": -1440},
        {"source_timezone": "../private"},
        {"source_timezone": "x" * 129},
        {"source_timezone": True},
    ],
)
def test_time_candidates_are_exact_bounded_and_unique(changes):
    with pytest.raises(ValueError):
        replace(SourceTimeFacts("local", ("dmy",), (local(),)), **changes)


def test_ambiguous_order_cannot_supply_an_instant_even_with_an_offset():
    value = SourceTimeFacts(
        "ambiguous", ("dmy", "mdy"), (local(month=4, day=3), local(month=3, day=4)), 0
    )
    value.check_at(None)
    with pytest.raises(ValueError):
        replace(value, status="instant")


@pytest.mark.parametrize(
    "changes",
    [
        {"local_candidates": (local(),)},
        {"date_order_candidates": ("ymd",)},
        {"source_offset_minutes": 0},
        {"source_timezone": "UTC"},
    ],
)
def test_absent_time_cannot_hide_components(changes):
    with pytest.raises(ValueError):
        replace(SourceTimeFacts("absent"), **changes)


def test_instant_requires_exact_source_components_and_aware_time():
    value = facts(SourceTimeFacts("instant", ("dmy",), (local(),), 120))
    at = datetime(2026, 4, 13, 8, 20, tzinfo=UTC)
    value.check_at(at)
    value.check_at(at.astimezone(timezone(timedelta(hours=2))))
    for wrong in (
        None,
        at.replace(tzinfo=None),
        at + timedelta(seconds=1),
        at.replace(microsecond=1),
    ):
        with pytest.raises(ValueError):
            value.check_at(wrong)
    with pytest.raises(ValueError):
        SourceTimeFacts("instant", ("dmy",), (local(),))
    with pytest.raises(ValueError):
        SourceTimeFacts("local", ("dmy",), (local(),), 0)


def test_timezone_requires_unambiguous_resolution_and_consistent_offset():
    known = SourceTimeFacts("instant", ("ymd",), (local(),), source_timezone="Europe/Paris")
    known.check_at(datetime(2026, 4, 13, 8, 20, tzinfo=UTC))
    for candidate in (
        local(month=11, day=1, hour=1, minute=30),
        local(month=3, day=8, hour=2, minute=30),
    ):
        unresolved = SourceTimeFacts(
            "local", ("ymd",), (candidate,), source_timezone="America/New_York"
        )
        unresolved.check_at(None)
        with pytest.raises(ValueError):
            replace(unresolved, status="instant")
    with pytest.raises(ValueError):
        replace(known, source_offset_minutes=0)


def test_source_offset_resolves_repeated_hour_and_retained_fold_compares_by_instant():
    candidate = local(month=11, day=1, hour=1, minute=30)
    value = SourceTimeFacts("instant", ("ymd",), (candidate,), -240, "America/New_York")
    retained = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    value.check_at(retained)
    with pytest.raises(ValueError):
        value.check_at(retained.replace(fold=1))


def test_canonical_fact_size_is_enforced_without_truncation(monkeypatch):
    from grove import source_facts

    value = facts()
    monkeypatch.setattr(source_facts, "MAX_FACT_BYTES", value.encoded_size - 1)
    with pytest.raises(ValueError, match="encoded byte limit"):
        facts()


@pytest.mark.parametrize(
    ("candidate", "offset"),
    [
        (local(year=1, month=1, day=1, hour=0, minute=0), 1),
        (local(year=9999, month=12, day=31, hour=23, minute=59), -1),
    ],
)
def test_utc_range_overflow_refuses_before_retention(candidate, offset):
    with pytest.raises(ValueError, match="UTC range"):
        SourceTimeFacts("instant", ("ymd",), (candidate,), offset)


@pytest.mark.parametrize(
    "changes",
    [
        {"byte_start": True},
        {"byte_start": -1},
        {"byte_end": 0},
        {"byte_end": 1 << 100},
        {"byte_start": 12},
        {"line_start": 1},
        {"page": 1},
        {"kind": "raw"},
    ],
)
def test_locator_variants_and_ranges_are_strict(changes):
    with pytest.raises(ValueError):
        replace(SourceLocator("bytes", byte_start=0, byte_end=12), **changes)


def test_parser_checks_actual_byte_line_and_page_extents():
    locator = SourceLocator("line_bytes", 2, 3, 4, 12)
    locator.check_extent(byte_count=12, line_count=3)
    with pytest.raises(ValueError):
        locator.check_extent(byte_count=11, line_count=3)
    with pytest.raises(ValueError):
        locator.check_extent(byte_count=12, line_count=2)
    page = SourceLocator("page", page=2)
    page.check_extent(page_count=2)
    with pytest.raises(ValueError):
        page.check_extent(page_count=1)
    with pytest.raises(ValueError):
        SourceLocator("line_bytes", 3, 2, 0, 12)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(extra="not permitted"),
        lambda r: r["locator"].update(byte_start=True),
        lambda r: r["time"].update(source_offset_minutes=False),
        lambda r: r["time"].update(extra=None),
        lambda r: r.update(contract="other"),
        lambda r: r.pop("identity_basis"),
    ],
)
def test_json_decoder_refuses_extra_missing_and_wrong_type_fields(mutate):
    row = facts().as_row()
    mutate(row)
    with pytest.raises(ValueError):
        TextSourceFacts.from_json(json.dumps(row))


def test_json_decoder_refuses_duplicates_and_limits_bytes_before_parse(monkeypatch):
    encoded = facts().canonical_json()
    with pytest.raises(ValueError):
        TextSourceFacts.from_json(
            encoded.replace('"status":"absent"', '"status":"absent","status":"absent"')
        )
    calls = []
    monkeypatch.setattr(json, "loads", lambda *a, **k: calls.append(1))
    for oversized in (b" " * 2049, "é" * 1100, " " * 100_000):
        with pytest.raises(ValueError):
            TextSourceFacts.from_json(oversized)
    assert calls == []


def test_typed_objects_refuse_dicts_and_subclasses():
    class DerivedFacts(TextSourceFacts):
        pass

    for value in (facts().as_row(), DerivedFacts("a" * 64, facts().locator, facts().time)):
        with pytest.raises(ValueError):
            ParsedMessage("docabc", "A note.", None, None, source_facts=value)
    with pytest.raises(ValueError):
        TextSourceFacts("a" * 64, facts().locator, {"status": "absent"})


def test_null_speaker_requires_typed_facts_and_calendar_stays_separate():
    with pytest.raises(ValueError):
        ParsedMessage("abc", "A note.", None, None)
    typed = ParsedMessage("docabc", "A note.", None, None, source_facts=facts())
    assert typed.speaker_kind is None
    calendar = CalendarEvent(None, "UTC", "cancelled")
    legacy = ParsedMessage("gcalabc", "Cancelled.", None, "third_party", calendar_event=calendar)
    assert legacy.calendar_event is calendar and legacy.source_facts is None
    with pytest.raises(ValueError, match="cannot be mixed"):
        replace(typed, calendar_event=calendar)
    with pytest.raises(ValueError):
        replace(typed, modality="speech")


def test_internal_tuple_whitelist_is_exact_and_matches_locator_variant():
    check_text_transport(DOCUMENT_TEXT, facts(), None)
    for identity in (
        None,
        list(DOCUMENT_TEXT),
        ("document", "document.v1"),
        ("google_calendar", "google_calendar.v1"),
        ("unknown", "document.text.v1"),
    ):
        with pytest.raises(ValueError):
            check_text_transport(identity, facts(), None)
    with pytest.raises(ValueError):
        check_text_transport(WHATSAPP_TEXT, facts(), None)


def test_material_office_facts_roundtrip_and_legacy_bytes_unchanged():
    import json
    from dataclasses import asdict

    from grove.source_facts import SourceLocator, SourceTimeFacts, TextSourceFacts

    for locator in (
        SourceLocator("block", block=1),
        SourceLocator("slide_block", block=2, slide=1),
    ):
        facts = TextSourceFacts("a" * 64, locator, SourceTimeFacts("absent"))
        assert TextSourceFacts.from_json(facts.canonical_json()) == facts
    facts = TextSourceFacts(
        "b" * 64, SourceLocator("bytes", byte_start=0, byte_end=10), SourceTimeFacts("absent")
    )
    row = asdict(facts)
    del row["locator"]["block"]
    del row["locator"]["slide"]
    assert facts.canonical_json() == json.dumps(
        row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_material_locator_nonobject_is_a_controlled_refusal():
    import json

    import pytest

    from grove.source_facts import SourceLocator, SourceTimeFacts, TextSourceFacts

    row = TextSourceFacts(
        "a" * 64, SourceLocator("block", block=1), SourceTimeFacts("absent")
    ).as_row()
    row["locator"] = []
    with pytest.raises(ValueError):
        TextSourceFacts.from_json(json.dumps(row))
