"""Invented, pure fact fixtures. No persistence, private inputs or model calls."""

import ast
import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove import source_fact_values as values
from grove.source_fact_values import (
    VERSION,
    FactInterfaceError,
    FactMessage,
    FactPredicate,
    FactValue,
    compile_text_fact_selector,
    project_text_fact_values,
    select_text_fact_message_keys,
)
from grove.source_facts import (
    DOCUMENT_TEXT,
    WHATSAPP_TEXT,
    LocalParts,
    SourceLocator,
    SourceTimeFacts,
    TextSourceFacts,
)


def local(**changes):
    fields = dict(
        year=2026, year_width=4, month=3, day=5, hour=0, minute=5, second=0, precision="minute"
    )
    return LocalParts(**(fields | changes))


def facts(time=None, *, document=False):
    locator = (
        SourceLocator("bytes", byte_start=0, byte_end=12)
        if document
        else SourceLocator("line_bytes", 1, 1, 0, 12)
    )
    return TextSourceFacts("a" * 64, locator, time or SourceTimeFacts("absent"))


def projected(fact, at=None):
    return [
        (v.kind, v.canonical)
        for v in project_text_fact_values(fact, at=at, source_identity=WHATSAPP_TEXT)
    ]


def select(messages, kind, value, identity=WHATSAPP_TEXT):
    return select_text_fact_message_keys(
        messages,
        compile_text_fact_selector(kind=kind, value=value),
        source_identity=identity,
    )


def test_instant_keeps_local_candidate_across_utc_date_boundary():
    fact = facts(SourceTimeFacts("instant", ("ymd",), (local(),), 60))
    at = datetime(2026, 3, 4, 23, 5, tzinfo=UTC)
    assert projected(fact, at) == [
        ("local_candidate", "2026-03-05T00:05"),
        ("instant", "2026-03-04T23:05:00Z"),
        ("source_offset", "+01:00"),
    ]
    messages = [FactMessage("original-opaque-key", at, fact)]
    assert select(messages, "local_candidate", "2026-03-05T00:05") == ("original-opaque-key",)
    assert select(messages, "instant", "2026-03-04T23:05Z") == ("original-opaque-key",)


def test_all_ambiguous_candidates_are_sorted_and_select_the_whole_message():
    fact = facts(
        SourceTimeFacts("ambiguous", ("dmy", "mdy"), (local(month=5, day=3), local()), -60, "UTC")
    )
    assert projected(fact) == [
        ("local_candidate", "2026-03-05T00:05"),
        ("local_candidate", "2026-05-03T00:05"),
        ("source_offset", "-01:00"),
        ("source_timezone", "UTC"),
    ]
    for kind, value in projected(fact):
        assert select([FactMessage("complete-message", None, fact)], kind, value) == (
            "complete-message",
        )


@pytest.mark.parametrize(
    ("year", "width", "precision", "second", "expected"),
    [
        (1, 4, "minute", 0, "0001-03-05T00:05"),
        (1, 2, "minute", 0, "01-03-05T00:05"),
        (0, 2, "second", 0, "00-03-05T00:05:00"),
        (2026, 4, "second", 7, "2026-03-05T00:05:07"),
    ],
)
def test_year_width_and_precision_are_exact(year, width, precision, second, expected):
    time = SourceTimeFacts(
        "ambiguous" if width == 2 else "local",
        ("ymd",),
        (local(year=year, year_width=width, precision=precision, second=second),),
    )
    assert projected(facts(time)) == [("local_candidate", expected)]
    predicate = compile_text_fact_selector(kind="local_candidate", value=expected)
    assert predicate.value_bytes == len(expected)
    assert predicate.sha256 == hashlib.sha256(expected.encode()).hexdigest()


def test_precision_and_century_are_not_equivalent():
    message = FactMessage(
        "short",
        None,
        facts(SourceTimeFacts("ambiguous", ("ymd",), (local(year=26, year_width=2),))),
    )
    assert select([message], "local_candidate", "2026-03-05T00:05") == ()
    assert select([message], "local_candidate", "26-03-05T00:05:00") == ()
    assert select([message], "local_candidate", "26-03-05T00:05") == ("short",)


def test_historical_timezone_preserves_nonzero_utc_seconds():
    time = SourceTimeFacts(
        "instant", ("ymd",), (local(year=1900, month=1, day=1),), source_timezone="Europe/Paris"
    )
    at = datetime(1899, 12, 31, 23, 55, 39, tzinfo=UTC)
    assert projected(facts(time), at) == [
        ("local_candidate", "1900-01-01T00:05"),
        ("instant", "1899-12-31T23:55:39Z"),
        ("source_timezone", "Europe/Paris"),
    ]
    assert select(
        [FactMessage("historical", at, facts(time))], "instant", "1899-12-31T23:55:39Z"
    ) == ("historical",)
    with pytest.raises(FactInterfaceError):
        project_text_fact_values(
            facts(time), at=at.replace(microsecond=1), source_identity=WHATSAPP_TEXT
        )


def test_timezone_catalogue_case_is_checked_before_a_permissive_zone_loader(monkeypatch):
    loaded = []
    monkeypatch.setattr(values, "ZoneInfo", lambda name: loaded.append(name))
    for name in ("europe/paris", "EUROPE/PARIS"):
        with pytest.raises(FactInterfaceError, match="^unsupported_fact_selector$"):
            compile_text_fact_selector(kind="source_timezone", value=name)
    assert loaded == []
    predicate = compile_text_fact_selector(kind="source_timezone", value="Europe/Paris")
    assert predicate.sha256 == hashlib.sha256(b"Europe/Paris").hexdigest()
    assert loaded == ["Europe/Paris"]


def test_timezone_spelling_is_exact_without_alias_equivalence():
    time = SourceTimeFacts(
        "ambiguous", ("ymd",), (local(year=26, year_width=2),), source_timezone="US/Eastern"
    )
    message = FactMessage("zone", None, facts(time))
    assert select([message], "source_timezone", "US/Eastern") == ("zone",)
    assert select([message], "source_timezone", "America/New_York") == ()


@pytest.mark.parametrize("offset", [-1439, -60, 0, 60, 1439])
def test_explicit_offset_signs_and_zero(offset):
    time = SourceTimeFacts("ambiguous", ("ymd",), (local(year=26, year_width=2),), offset)
    canonical = f"{'-' if offset < 0 else '+'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}"
    assert projected(facts(time))[-1] == ("source_offset", canonical)
    assert select([FactMessage("offset", None, facts(time))], "source_offset", canonical) == (
        "offset",
    )
    assert (
        compile_text_fact_selector(kind="source_offset", value="+01:00").sha256
        != compile_text_fact_selector(kind="source_offset", value="-01:00").sha256
    )


def test_absent_projection_is_empty_and_excludes_structural_facts():
    assert projected(facts()) == []
    assert (
        project_text_fact_values(facts(document=True), at=None, source_identity=DOCUMENT_TEXT) == ()
    )


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("date", "2026-03-05"),
        ("local_candidate", "2026-03-05"),
        ("local_candidate", "2026-03-05T00:05Z"),
        ("local_candidate", " 2026-03-05T00:05"),
        ("local_candidate", "2026-03-05T00:05 "),
        ("local_candidate", "２０２６-03-05T00:05"),
        ("local_candidate", "2026-02-30T00:05"),
        ("local_candidate", "2026-03-05T24:05"),
        ("local_candidate", "2026-03-05T00:05:60"),
        ("local_candidate", "2026-03-05T00:05:00.1"),
        ("local_candidate", "2026-03-05T00:05/*"),
        ("local_candidate", "yesterday"),
        ("instant", "26-03-05T00:05Z"),
        ("instant", "0000-03-05T00:05Z"),
        ("instant", "2026-03-05T00:05+00:00"),
        ("instant", "2026-03-05T00:05z"),
        ("instant", "2026-03-05T00:05:00.0Z"),
        ("instant", "2026-03-05T00:05:60Z"),
        ("source_offset", "-00:00"),
        ("source_offset", "+24:00"),
        ("source_offset", "+01:60"),
        ("source_offset", "01:00"),
        ("source_offset", "+0100"),
        ("source_offset", " +01:00"),
        ("source_timezone", "europe/paris"),
        ("source_timezone", "UTC "),
        ("source_timezone", "../UTC"),
        ("source_timezone", "x" * 129),
        ("source_timezone", "\ud800"),
        ("source_timezone", "x" * 513),
        ("source_offset", b"+00:00"),
        ("source_offset", {"value": "+00:00"}),
        (True, "+00:00"),
    ],
)
def test_unsupported_selector_is_a_safe_refusal_not_a_zero(kind, value):
    with pytest.raises(FactInterfaceError) as error:
        compile_text_fact_selector(kind=kind, value=value)
    assert str(error.value) == "unsupported_fact_selector"
    assert error.value.__context__ is None
    assert repr(error.value) == "FactInterfaceError('unsupported_fact_selector')"


def test_raw_selector_bound_precedes_encoding_or_hashing(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("hash called before selector validation")

    monkeypatch.setattr(values.hashlib, "sha256", forbidden)
    with pytest.raises(FactInterfaceError):
        compile_text_fact_selector(kind="source_timezone", value="\ud800" * 513)


@pytest.mark.parametrize(
    "change",
    [
        {"version": "other"},
        {"kind": "date"},
        {"value_bytes": True},
        {"value_bytes": 0},
        {"value_bytes": 19},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"sha256": 1},
    ],
)
def test_predicate_constructor_refuses_invalid_fields(change):
    good = compile_text_fact_selector(kind="instant", value="2026-03-05T00:05Z")
    with pytest.raises(FactInterfaceError):
        replace(good, **change)


def test_exact_types_immutability_and_safe_reprs():
    message = FactMessage("invented-source-key", None, facts())
    atom = FactValue(VERSION, "source_timezone", "Europe/Paris")
    predicate = compile_text_fact_selector(kind="source_timezone", value="Europe/Paris")
    for obj in (message, atom, predicate):
        assert repr(obj) == type(obj).__name__ + "()"
        assert not hasattr(obj, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(obj, "key" if type(obj) is FactMessage else "version", "no")

    class DerivedMessage(FactMessage):
        pass

    class DerivedValue(FactValue):
        pass

    class DerivedPredicate(FactPredicate):
        pass

    class DerivedStr(str):
        pass

    for build in (
        lambda: DerivedMessage("key", None, facts()),
        lambda: DerivedValue(VERSION, "source_timezone", "UTC"),
        lambda: DerivedPredicate(VERSION, "source_offset", 6, "a" * 64),
        lambda: FactMessage(DerivedStr("key"), None, facts()),
        lambda: FactValue(VERSION, "instant", "2026-03-05T00:05Z"),
        lambda: compile_text_fact_selector(kind=DerivedStr("source_offset"), value="+00:00"),
    ):
        with pytest.raises(FactInterfaceError):
            build()


@pytest.mark.parametrize(
    "identity",
    [
        None,
        list(WHATSAPP_TEXT),
        ("whatsapp", "wrong"),
        DOCUMENT_TEXT,
        ("google_calendar", "google_calendar.v1"),
    ],
)
def test_wrong_tuple_and_mixed_locator_refuse(identity):
    with pytest.raises(FactInterfaceError):
        project_text_fact_values(facts(), at=None, source_identity=identity)


def test_fact_message_constructor_rejects_unsupported_page_locator():
    page = replace(facts(), locator=SourceLocator("page", page=1))
    with pytest.raises(FactInterfaceError):
        FactMessage("page", None, page)


def test_full_batch_validation_rejects_later_nonmatch_and_duplicate():
    good = FactMessage("first", None, facts(SourceTimeFacts("local", ("ymd",), (local(),))))
    wrong_locator = FactMessage("later", None, facts(document=True))
    for rows in ([good, wrong_locator], [good, good], [good, {}]):
        with pytest.raises(FactInterfaceError):
            select(rows, "local_candidate", "2026-03-05T00:05")
    damaged = FactMessage("later", None, facts())
    object.__setattr__(damaged, "at", datetime(2026, 3, 5, tzinfo=UTC))
    with pytest.raises(FactInterfaceError):
        select([good, damaged], "local_candidate", "2026-03-05T00:05")


def test_selection_returns_original_keys_once_in_input_order():
    fact = facts(SourceTimeFacts("local", ("ymd",), (local(),)))
    rows = [FactMessage(key, None, fact) for key in ("z-last-lexically", "a-first-lexically")]
    assert select(rows, "local_candidate", "2026-03-05T00:05") == tuple(row.key for row in rows)


def test_containers_and_row_count_refuse_before_traversal():
    class Unbounded:
        def __iter__(self):
            pytest.fail("custom iterable traversed")

    for rows in (Unbounded(), iter(()), [None] * 1001):
        with pytest.raises(FactInterfaceError):
            select(rows, "source_offset", "+00:00")


def test_exact_1000_fact_messages_remain_within_the_batch_limit():
    rows = [FactMessage(str(index), None, facts()) for index in range(1000)]
    assert select(rows, "source_offset", "+00:00") == ()


def test_key_limits_and_unicode_precede_retention():
    assert FactMessage("k" * 1024, None, facts()).key == "k" * 1024
    for key in ("k" * 1025, "\ud800", 1):
        with pytest.raises(FactInterfaceError) as error:
            FactMessage(key, None, facts())
        assert error.value.__context__ is None
    rows = [FactMessage(f"{i:03d}" + "😀" * 1021, None, facts()) for i in range(251)]
    with pytest.raises(FactInterfaceError, match="^fact_projection_limit$"):
        select(rows, "source_offset", "+00:00")


def test_n1_equivalent_instants_have_exact_equal_canonical_accounting(monkeypatch):
    fact = facts(SourceTimeFacts("instant", ("ymd",), (local(),), 60))
    at = datetime(2026, 3, 4, 23, 5, tzinfo=UTC)
    rows = [
        FactMessage('line\n"é', time, fact)
        for time in (at, at.astimezone(timezone(timedelta(hours=1))))
    ]
    expected = len(
        json.dumps(
            [{"key": rows[0].key, "at": "2026-03-04T23:05:00Z", "facts": fact.as_row()}],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    assert values._record_bytes(rows[0]) + 2 == expected
    assert values._record_bytes(rows[1]) + 2 == expected
    monkeypatch.setattr(values, "MAX_RECORD_BYTES", expected)
    for row in rows:
        assert select([row], "instant", "2026-03-04T23:05Z") == (row.key,)
    monkeypatch.setattr(values, "MAX_RECORD_BYTES", expected - 1)
    for row in rows:
        with pytest.raises(FactInterfaceError, match="^fact_projection_limit$"):
            select([row], "instant", "2026-03-04T23:05Z")


def test_array_accounting_includes_null_escapes_brackets_and_commas(monkeypatch):
    rows = [FactMessage('"\n' + str(i), None, facts()) for i in range(2)]
    expected = len(
        json.dumps(
            [{"key": r.key, "at": None, "facts": r.facts.as_row()} for r in rows],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    monkeypatch.setattr(values, "MAX_RECORD_BYTES", expected)
    assert select(rows, "source_offset", "+00:00") == ()
    monkeypatch.setattr(values, "MAX_RECORD_BYTES", expected - 1)
    with pytest.raises(FactInterfaceError, match="^fact_projection_limit$"):
        select(rows, "source_offset", "+00:00")


def test_real_complete_record_limit_can_refuse_within_key_byte_budget():
    rows = [FactMessage("\x01" * 1021 + f"{i:03d}", None, facts()) for i in range(700)]
    assert sum(len(row.key.encode()) for row in rows) <= values.MAX_KEY_BYTES
    with pytest.raises(FactInterfaceError, match="^fact_projection_limit$"):
        select(rows, "source_offset", "+00:00")


def test_projection_and_fact_limits_remain_separate(monkeypatch):
    fact = facts(SourceTimeFacts("local", ("ymd",), (local(),)))
    monkeypatch.setattr(values, "MAX_PROJECTION_BYTES", 15)
    with pytest.raises(FactInterfaceError, match="^fact_projection_limit$"):
        projected(fact)
    from grove import source_facts

    monkeypatch.setattr(values, "MAX_PROJECTION_BYTES", 2048)
    monkeypatch.setattr(source_facts, "MAX_FACT_BYTES", fact.encoded_size - 1)
    with pytest.raises(FactInterfaceError, match="^invalid_text_facts$"):
        projected(fact)


def test_n2_fresh_ordinary_import_has_no_ingest_or_screening_dependency():
    code = """
import sys
import grove.source_fact_values
assert 'grove.ingest' not in sys.modules
assert 'grove.screening' not in sys.modules
assert not any(n.startswith(('grove.writer', 'grove.api', 'grove.runner')) for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    source = Path(values.__file__).read_text()
    imports = [
        node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
    ]
    assert [name for name in imports if name.startswith("grove")] == ["grove.source_facts"]
