"""Pure parts of the writer, with no store: clocks, trigrams, locators, shapes, ids."""

from datetime import UTC, datetime

import pytest

from grove.writer import HTTP_STATUS, BatchDeclined, Caller, Refusal, short_id
from grove.writer.clocks import (
    UNKNOWN,
    Clock,
    ClockError,
    ends_before_start,
    parse_clock,
    parse_instant,
    sort_key,
)
from grove.writer.errors import Refused
from grove.writer.neardup import similarity, trigrams
from grove.writer.ops import check_shape
from grove.writer.provenance import check_locator
from grove.writer.source_types import claim_fields, derive_row_source_type, weakest

NOON = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


# Clocks ---------------------------------------------------------------------------------------


def test_unknown_is_a_clock_with_no_bounds():
    assert parse_clock("unknown") is UNKNOWN
    assert parse_clock({"precision": "unknown"}).unknown
    assert sort_key(UNKNOWN) is None


def test_a_clock_is_stored_in_utc():
    clock = parse_clock(
        {
            "earliest": "2026-10-28T16:00:00+04:00",
            "latest": "2026-10-28T16:00:00+04:00",
            "precision": "minute",
            "words": "at four",
        }
    )
    assert clock.earliest == NOON and clock.latest == NOON
    assert clock.earliest.utcoffset().total_seconds() == 0
    assert clock.words == "at four"


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("now", "clock_shape"),
        ({"precision": "minute", "earliest": "2026-10-28T12:00:00+00:00"}, "clock_bounds"),
        ({"precision": "before", "earliest": "2026-10-28T12:00:00+00:00"}, "clock_bounds"),
        ({"precision": "after", "latest": "2026-10-28T12:00:00+00:00"}, "clock_bounds"),
        ({"precision": "unknown", "latest": "2026-10-28T12:00:00+00:00"}, "clock_bounds"),
        ({"precision": "fortnight"}, "clock_precision"),
        ({"precision": "unknown", "when": "soon"}, "clock_field"),
        (
            {
                "precision": "day",
                "earliest": "2026-10-29T00:00:00+00:00",
                "latest": "2026-10-28T00:00:00+00:00",
            },
            "clock_order",
        ),
        (
            {
                "precision": "minute",
                "earliest": "2026-10-28T12:00:00",
                "latest": "2026-10-28T12:00:00",
            },
            "instant_offset",
        ),
    ],
)
def test_a_malformed_clock_is_refused(value, rule):
    with pytest.raises(ClockError) as refused:
        parse_clock(value)
    assert refused.value.rule == rule


def test_before_and_after_take_one_bound():
    before = parse_clock({"precision": "before", "latest": "2026-10-28T12:00:00Z"})
    after = parse_clock({"precision": "after", "earliest": "2026-10-28T12:00:00Z"})
    assert (before.earliest, before.latest) == (None, NOON)
    assert (after.earliest, after.latest) == (NOON, None)
    assert sort_key(before) == NOON and sort_key(after) == NOON


def test_an_end_before_a_start_needs_every_stop_before_every_start():
    start = Clock("day", NOON, NOON.replace(hour=23))
    assert ends_before_start(start, Clock("minute", NOON.replace(hour=9), NOON.replace(hour=9)))
    assert not ends_before_start(start, Clock("day", NOON.replace(hour=0), NOON.replace(hour=13)))
    assert not ends_before_start(start, Clock("after", NOON.replace(hour=1), None))
    assert not ends_before_start(Clock("before", None, NOON), Clock("minute", NOON, NOON))
    assert not ends_before_start(UNKNOWN, Clock("minute", NOON, NOON))
    assert not ends_before_start(start, UNKNOWN)


def test_an_instant_needs_its_offset():
    assert parse_instant("2026-10-28T12:00:00Z") == NOON
    with pytest.raises(ClockError):
        parse_instant("2026-10-28T12:00:00")
    with pytest.raises(ClockError):
        parse_instant(datetime(2026, 10, 28, 12, 0))


# Trigrams -------------------------------------------------------------------------------------


def test_trigrams_follow_the_padded_word_form():
    assert trigrams("Cat") == frozenset({"  c", " ca", "cat", "at "})
    assert trigrams("") == frozenset()
    assert trigrams("Café, CAFE") == trigrams("cafe")


def test_similarity_is_shared_over_union():
    assert similarity("Book the venue", "book the venue") == 1.0
    assert similarity("", "") == 0.0
    reworded = similarity(
        "Book the venue for the Okonkwo offsite", "Book a venue for the Okonkwo offsite"
    )
    assert reworded >= 0.8
    assert similarity("Book the venue", "Print the invitations") < 0.3


# Locators -------------------------------------------------------------------------------------

LINES = "first line\nsecond line\nthird line with key 4f9c2ab1"


@pytest.mark.parametrize(
    ("connector", "locator"),
    [
        ("whatsapp", "line:1"),
        ("whatsapp", "line:2-3"),
        ("web_pages", "char:0-5"),
        ("web_pages", "whole"),
        ("git", "key:4f9c2ab1"),
        ("notes_files_releases", f"char:0-{len(LINES)}"),
    ],
)
def test_a_locator_that_resolves_passes(connector, locator):
    check_locator(locator, content=LINES, connector_type=connector)


@pytest.mark.parametrize(
    ("connector", "locator", "rule"),
    [
        ("whatsapp", "line:4", "line_out_of_range"),
        ("whatsapp", "line:0", "line_out_of_range"),
        ("whatsapp", "line:3-2", "line_out_of_range"),
        ("whatsapp", "line:x", "bad_locator"),
        ("whatsapp", "char:0-5", "scheme_not_allowed"),
        ("whatsapp", "whole", "scheme_not_allowed"),
        ("web_pages", f"char:0-{len(LINES) + 1}", "char_out_of_range"),
        ("web_pages", "char:5-5", "char_out_of_range"),
        ("git", "key:abc", "key_not_found"),
        ("git", "key:deadbeef", "key_not_found"),
        ("screenshots", "whole:1", "bad_locator"),
        ("calendar", "msg:88213", "scheme_not_allowed"),
    ],
)
def test_a_locator_that_does_not_resolve_is_refused(connector, locator, rule):
    with pytest.raises(Refused) as refused:
        check_locator(locator, content=LINES, connector_type=connector)
    assert (refused.value.code, refused.value.rule) == ("unresolvable_locator", rule)


def test_a_source_held_as_a_pointer_resolves_only_whole():
    check_locator("whole", content=None, connector_type="screenshots")
    with pytest.raises(Refused) as refused:
        check_locator("char:0-2", content=None, connector_type="web_pages")
    assert refused.value.rule == "pointer_only"


# Source types ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "statement", "speaker", "expected"),
    [
        ("whatsapp", "decision", "person", "said"),
        ("whatsapp", "report_happened", "person", "reported_done"),
        ("whatsapp", "report_state", "third_party", "reported_done"),
        # A caller's speaker kind never raises the type: an automated chat line is a chat line.
        ("whatsapp", "promise", "automated", "said"),
        ("whatsapp", "report_happened", "automated", "reported_done"),
        ("voice_notes", "report_state", "automated", "reported_done"),
        ("git", "report_happened", "automated", "confirmed"),
        ("git", "none", "person", "confirmed"),
        ("web_pages", "report_state", "person", "confirmed"),
        ("calendar", "intention", "automated", "planned"),
        ("voice_notes", "preference_or_opinion", "person", "said"),
    ],
)
def test_the_row_source_type_comes_from_connector_statement_and_speaker(
    connector, statement, speaker, expected
):
    assert speaker in ("person", "third_party", "automated")
    assert derive_row_source_type(connector, statement) == expected


def test_the_claim_takes_the_strongest_observed_row():
    rows = [
        {"id": "b", "derivation": "observed", "source_type": "said", "recorded": "1"},
        {
            "id": "a",
            "derivation": "observed",
            "source_type": "confirmed",
            "speaker_kind": "automated",
            "modality": "text",
            "recorded": "2",
        },
        {"id": "c", "derivation": "inferred", "source_type": "confirmed", "recorded": "0"},
    ]
    assert claim_fields(rows) == {
        "derived_source_type": "confirmed",
        "derivation": "observed",
        "speaker": "automated",
        "modality": "text",
    }
    assert claim_fields(rows[2:])["derivation"] == "inferred"
    assert set(claim_fields([]).values()) == {None}
    assert weakest(["confirmed", "said", "reported_done"]) == "said"


# Shapes ---------------------------------------------------------------------------------------

OBSERVATION = {
    "ev_id": "e1",
    "episode_id": "ep",
    "source_id": "src",
    "locator": "line:1",
    "message_key": "mk-1",
    "source_time": "2026-10-28T12:00:00Z",
    "statement_class": "decision",
    "derivation": "observed",
    "speaker_kind": "person",
    "modality": "text",
}


def item_op(**fields) -> dict:
    return {
        "op": "create_item",
        "op_id": "o1",
        "kind": "task",
        "title": "Sand the boat",
        "became_true": "unknown",
        "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
        "reason": "Rowan asked for it",
        **fields,
    }


def shape(*ops, evidence=None) -> list[tuple[str | None, str, str]]:
    batch = {"ops": list(ops), "evidence": [OBSERVATION] if evidence is None else evidence}
    return [(r.op_id, r.code, r.rule) for r in check_shape(batch)]


def test_a_well_formed_batch_has_no_shape_refusal():
    assert shape(item_op()) == []


@pytest.mark.parametrize(
    ("change", "code", "rule"),
    [
        ({"source_type": "confirmed"}, "caller_source_type", "caller_source_type"),
        ({"derivation": "observed"}, "caller_source_type", "caller_source_type"),
        ({"weight": 2}, "caller_score", "caller_score"),
        ({"verdict": "supports"}, "unrecorded_verdict", "caller_verdict"),
        ({"last_confirmed": "2026-10-28T12:00:00Z"}, "invalid_arguments", "verify_only"),
        ({"recorded": "2026-10-28T12:00:00Z"}, "invalid_arguments", "writer_field"),
        ({"version": 1}, "invalid_arguments", "writer_field"),
        ({"colour": "blue"}, "invalid_arguments", "unknown_field"),
        ({"became_true": None}, "missing_became_true", "became_true_required"),
        ({"became_true": "now"}, "missing_became_true", "clock_shape"),
        ({"evidence_refs": []}, "missing_provenance", "evidence_refs_required"),
        (
            {"evidence_refs": [{"ev_id": "e1", "polarity": "contradicts"}]},
            "missing_provenance",
            "no_supporting_evidence",
        ),
        (
            {"evidence_refs": [{"ev_id": "e9", "polarity": "supports"}]},
            "missing_provenance",
            "unknown_ev_id",
        ),
        ({"title": ""}, "invalid_arguments", "required_field"),
        ({"recheck_by": "next week"}, "invalid_arguments", "instant_format"),
        ({"temp_id": "i1"}, "invalid_arguments", "temp_id"),
    ],
)
def test_an_op_shape_fault_is_refused_before_the_store(change, code, rule):
    assert shape(item_op(**change)) == [("o1", code, rule)]


@pytest.mark.parametrize(
    ("change", "code", "rule"),
    [
        ({"locator": None}, "missing_provenance", "missing_locator"),
        ({"source_type": "confirmed"}, "caller_source_type", "caller_source_type"),
        ({"score": 0.9}, "caller_score", "caller_score"),
        ({"derivation": "inferred"}, "inferred_evidence", "premises_required"),
        ({"modality": "smell"}, "invalid_arguments", "enum"),
        ({"source_time": "yesterday"}, "invalid_arguments", "instant_format"),
        ({"person_timezone": "Mars/Olympus"}, "invalid_arguments", "timezone"),
    ],
)
def test_an_observation_fault_refuses_the_op_that_cites_it(change, code, rule):
    observation = {
        key: value for key, value in {**OBSERVATION, **change}.items() if value is not None
    }
    assert shape(item_op(), evidence=[observation]) == [("o1", code, rule)]


def test_an_uncited_observation_is_not_checked():
    status = {
        "op": "end_evidence",
        "op_id": "o1",
        "evidence": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "end_reason": "correction",
    }
    assert shape(status, evidence=[{**OBSERVATION, "score": 1}]) == []


def test_temp_ids_are_declared_before_use_and_once():
    link = {
        "op": "create_link",
        "op_id": "o2",
        "from": "$a",
        "meaning": "part of",
        "to": "$b",
        "became_true": "unknown",
        "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
        "reason": "it is part of it",
    }
    first, second = item_op(temp_id="$a"), item_op(op_id="o3", temp_id="$a")
    assert shape(first, link) == [("o2", "invalid_arguments", "unknown_temp_id")]
    assert shape(first, second) == [("o3", "invalid_arguments", "duplicate_temp_id")]


@pytest.mark.parametrize(
    ("batch", "rule"),
    [
        ([], "type"),
        ({"ops": []}, "ops_count"),
        ({"ops": [item_op()] * 501}, "ops_count"),
        ({"ops": [item_op()], "schema": "x"}, "unknown_field"),
        ({"ops": [item_op(), item_op()], "evidence": [OBSERVATION]}, "duplicate_op_id"),
        ({"ops": [item_op()], "evidence": [OBSERVATION, OBSERVATION]}, "duplicate_ev_id"),
    ],
)
def test_a_batch_fault_names_no_op(batch, rule):
    refusals = check_shape(batch)
    assert [(r.op_id, r.code, r.rule) for r in refusals][:1] == [(None, "invalid_arguments", rule)]


def test_update_item_fields_are_limited_and_checked():
    update = {
        "op": "update_item",
        "op_id": "o1",
        "item": "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        "version": 1,
        "fields": {"kind": "event"},
        "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
        "reason": "renamed",
    }
    assert shape(update) == [("o1", "invalid_arguments", "unknown_field")]
    update["fields"] = {"confirmed_against": "x"}
    assert shape(update) == [("o1", "invalid_arguments", "verify_only")]
    update["fields"] = {"title": "  "}
    assert shape(update) == [("o1", "invalid_arguments", "required_field")]


# Ids, callers, errors -------------------------------------------------------------------------


def test_a_short_id_is_the_last_eight_hex_digits():
    assert short_id("0192f3a4-5b6c-7d8e-9f01-23456789abcd") == "6789abcd"


def test_a_caller_checks_its_kind_and_knows_its_write_scope():
    assert Caller("person", {"grove.write"}, "person:t").can_write
    assert Caller("update", frozenset({"grove.propose"}), "agent:t").can_write
    assert not Caller("person", ["grove.read"], "person:t").can_write
    with pytest.raises(ValueError):
        Caller("admin", {"grove.write"}, "x")
    with pytest.raises(ValueError):
        Caller("person", "grove.write", "x")


def test_every_code_has_its_http_status():
    assert HTTP_STATUS["invalid_arguments"] == 400
    assert HTTP_STATUS["scope_denied"] == 403
    assert HTTP_STATUS["not_found"] == 404
    assert HTTP_STATUS["version_conflict"] == 409
    assert HTTP_STATUS["near_duplicate"] == 422
    assert HTTP_STATUS["state_property_on_item"] == 422
    assert HTTP_STATUS["unavailable"] == 503


def test_a_declined_receipt_renders_each_refusal():
    refusals = [
        Refusal("o1", "create_link", "endpoint_kind_mismatch", "from_kind", "no", {}),
        Refusal("o3", "create_item", "unknown_kind", "unknown_kind", "no", {}),
    ]
    declined = BatchDeclined(
        refusals, batch_size=3, summaries={"o1": "link 9a1e0c33 -[lives in]-> 31cdee01"}
    )
    assert declined.receipt.render().splitlines() == [
        "declined (batch of 3, nothing written)",
        "  ! link 9a1e0c33 -[lives in]-> 31cdee01: endpoint_kind_mismatch (from_kind)",
        "  ! create_item o3: unknown_kind (unknown_kind)",
    ]
