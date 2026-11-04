"""D7 coverage controls over synthetic safe metadata, without a store or system clock."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from grove.reads.coverage import (
    ConnectorState,
    CoverageError,
    decide_answer,
    evaluate_coverage,
    item_coverage_gap,
)

WORLD = "t_hosts-b1_11111111-1111-4111-8111-111111111111"
OTHER = "t_hosts-b1_22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def connector(**kwargs):
    defaults = dict(
        world_id=WORLD,
        connector_id="calendar-one",
        source_kind="calendar",
        last_outcome="took_in",
        expected_read_interval=3600,
        last_successful_read=NOW,
        last_attempted_read=NOW,
    )
    return ConnectorState(**(defaults | kwargs))


def coverage(rows=None, **kwargs):
    defaults = dict(now=NOW, inventory_complete=True, disclosure_complete=True)
    return evaluate_coverage(WORLD, [connector()] if rows is None else rows, **(defaults | kwargs))


def answer(statement, **kwargs):
    defaults = dict(total=0, returned=0, eligibility_complete=True)
    return decide_answer(statement, **(defaults | kwargs))


@pytest.mark.parametrize("outcome", ["took_in", "nothing_new"])
def test_successful_current_coverage_can_establish_no_record(outcome):
    statement = coverage([connector(last_outcome=outcome)], not_connected=["email"])
    assert statement.sources[0].last_outcome == outcome
    assert statement.not_connected == ("email",)
    assert statement.fresh
    assert answer(statement).answer == "no_record"


@pytest.mark.parametrize("outcome", ["denied", "failed", "kept_out"])
def test_non_success_outcomes_cannot_be_hidden_by_a_success_stamp(outcome):
    statement = coverage([connector(last_outcome=outcome)])
    assert statement.sources[0].last_outcome == outcome
    assert statement.sources[0].reasons == (outcome,)
    assert statement.since == NOW
    assert answer(statement).answer == "not_seen_since"


def test_every_connector_is_returned_without_a_page_limit():
    rows = [connector(connector_id=f"source-{i:03}") for i in reversed(range(125))]
    statement = coverage(rows)
    assert len(statement.sources) == 125
    assert [row.connector_id for row in statement.sources] == sorted(r.connector_id for r in rows)


def test_read_interval_is_seconds_and_inclusive_at_boundary():
    statement = coverage([connector(last_successful_read=NOW - HOUR)])
    assert statement.fresh
    overdue = coverage([connector(last_successful_read=NOW - HOUR - timedelta(microseconds=1))])
    assert not overdue.fresh
    assert overdue.sources[0].behind_seconds == pytest.approx(0.000001)
    assert answer(overdue).answer == "not_seen_since"


def test_upload_has_no_interval_and_reports_elapsed_time():
    statement = coverage(
        [
            connector(
                source_kind="past_ai_conversations",
                expected_read_interval=None,
                last_successful_read=NOW - HOUR,
            )
        ]
    )
    assert statement.sources[0].expected_read_interval is None
    assert statement.sources[0].behind_seconds == 3600
    assert statement.sources[0].reasons == ("behind",)
    assert statement.since == NOW - HOUR
    assert answer(statement).answer == "not_seen_since"


def test_upload_exactly_at_reference_is_fresh():
    assert coverage([connector(expected_read_interval=None)]).fresh


def test_historical_reference_is_distinct_from_current_polling_interval():
    row = connector(last_successful_read=NOW - 2 * HOUR)
    assert not coverage([row]).fresh
    historical = coverage([row], question_at=NOW - 3 * HOUR)
    assert historical.fresh
    assert answer(historical).answer == "no_record"
    # A current interval must not excuse a historical gap after the last successful read.
    row = connector(last_successful_read=NOW - timedelta(minutes=30))
    assert coverage([row]).fresh
    assert not coverage([row], question_at=NOW - timedelta(minutes=10)).fresh


def test_historical_denial_stays_explicit_even_if_old_coverage_was_successful():
    result = coverage([connector(last_outcome="denied")], question_at=NOW - HOUR)
    assert answer(result).answer == "not_seen_since"


def test_unknown_last_success_never_invents_a_gap_date():
    statement = coverage([connector(last_successful_read=None)])
    assert statement.sources[0].behind_seconds is None
    assert statement.sources[0].reasons == ("unknown_success",)
    assert statement.since is None
    assert answer(statement).answer == "not_seen_since"


def test_unknown_outcome_is_not_treated_as_nothing_new():
    statement = coverage([connector(last_outcome=None)])
    assert statement.sources[0].reasons == ("unknown_outcome",)
    assert not statement.fresh


def test_oldest_unprocessed_item_blocks_absence_and_supplies_its_date():
    statement = coverage([connector(oldest_unprocessed_item=NOW - 4 * HOUR)])
    assert statement.sources[0].reasons == ("unprocessed",)
    assert statement.sources[0].oldest_unprocessed_item == NOW - 4 * HOUR
    assert statement.since == NOW - 4 * HOUR
    assert answer(statement).answer == "not_seen_since"


def test_earliest_gap_date_includes_backlog_and_failed_read():
    statement = coverage(
        [
            connector(
                connector_id="one", last_outcome="failed", last_successful_read=NOW - 2 * HOUR
            ),
            connector(connector_id="two", oldest_unprocessed_item=NOW - 5 * HOUR),
        ]
    )
    assert statement.since == NOW - 5 * HOUR


def test_one_unknown_gap_keeps_the_aggregate_date_unknown():
    statement = coverage(
        [
            connector(connector_id="one", last_outcome="denied", last_successful_read=None),
            connector(connector_id="two", last_successful_read=NOW - 2 * HOUR),
        ]
    )
    assert statement.since is None
    assert answer(statement).answer == "not_seen_since"


@pytest.mark.parametrize("flag", ["inventory_complete", "disclosure_complete"])
def test_hidden_or_unevaluated_sources_yield_generic_incomplete_coverage(flag):
    statement = coverage(**{flag: False})
    assert statement.incomplete
    assert statement.since is None
    assert answer(statement).answer == "not_seen_since"
    assert "hidden" not in repr(asdict(statement))


def test_empty_complete_inventory_names_missing_kinds_without_inventing_connections():
    statement = coverage([], not_connected=["email", "whatsapp"])
    assert statement.sources == ()
    assert answer(statement).answer == "no_record"


def test_empty_incomplete_inventory_never_asserts_absence():
    assert answer(coverage([], inventory_complete=False)).answer == "not_seen_since"


def test_found_items_keep_coverage_gaps():
    statement = coverage([connector(last_outcome="denied")])
    assert answer(statement, total=1, returned=1).answer == "found"
    assert not statement.fresh


def test_truncation_uses_total_matches_and_never_absence():
    result = answer(coverage(), total=125, returned=50)
    assert asdict(result) == dict(answer="truncated", total=125, returned=50, truncated=True)
    assert answer(coverage(), total=1, returned=0).answer == "truncated"


def test_ambiguous_is_never_guessed_even_when_the_page_is_partial():
    result = answer(coverage(), total=125, returned=50, ambiguous=True)
    assert result.answer == "ambiguous"
    assert result.truncated
    assert result.total == 125


def test_incomplete_index_evaluation_is_an_error_not_absence():
    with pytest.raises(CoverageError, match="^incomplete_eligibility$"):
        answer(coverage(), eligibility_complete=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(total=-1),
        dict(returned=1),
        dict(total=True),
        dict(total=1.0),
        dict(returned=False),
        dict(ambiguous=1),
        dict(ambiguous=True),
    ],
)
def test_invalid_counts_and_ambiguity_fail_safely(kwargs):
    with pytest.raises(CoverageError):
        answer(coverage(), **kwargs)


def test_item_gap_uses_explicit_evidence_and_recheck_ids_only():
    statement = coverage(
        [
            connector(connector_id="evidence", last_successful_read=NOW - 2 * HOUR),
            connector(
                connector_id="recheck", last_outcome="denied", last_successful_read=NOW - 4 * HOUR
            ),
            connector(connector_id="unrelated", last_successful_read=NOW - 8 * HOUR),
        ]
    )
    gap = item_coverage_gap(
        statement,
        contributed_connector_ids=["evidence"],
        recheck_connector_ids=["recheck"],
        references_complete=True,
    )
    assert gap.connector_ids == ("evidence", "recheck")
    assert gap.since == NOW - 4 * HOUR
    assert not gap.incomplete


def test_an_unrelated_gap_does_not_attach_to_an_item():
    statement = coverage([connector(last_outcome="failed")])
    assert item_coverage_gap(statement, references_complete=True) is None


def test_recheck_of_not_connected_kind_has_unknown_date():
    statement = coverage(not_connected=["email"])
    gap = item_coverage_gap(statement, recheck_missing_kinds=["email"], references_complete=True)
    assert gap.not_connected == ("email",)
    assert gap.since is None


def test_missing_or_excluded_reference_is_generic_and_does_not_expose_id():
    gap = item_coverage_gap(
        coverage(), contributed_connector_ids=["private-connector"], references_complete=True
    )
    assert gap.incomplete
    assert gap.since is None
    assert gap.connector_ids == ()
    assert "private-connector" not in repr(asdict(gap))


def test_unknown_item_gap_date_dominates_known_relevant_dates():
    statement = coverage(
        [
            connector(connector_id="one", last_successful_read=None),
            connector(connector_id="two", last_successful_read=NOW - 4 * HOUR),
        ]
    )
    gap = item_coverage_gap(
        statement, contributed_connector_ids=["one", "two"], references_complete=True
    )
    assert gap.since is None


def test_incomplete_references_or_disclosure_produce_generic_item_gap():
    assert item_coverage_gap(coverage(), references_complete=False).incomplete
    assert item_coverage_gap(
        coverage(disclosure_complete=False), references_complete=True
    ).incomplete


def test_unknown_recheck_kind_is_a_refusal_not_a_free_text_guess():
    with pytest.raises(CoverageError, match="^unknown_coverage_reference$"):
        item_coverage_gap(
            coverage(), recheck_missing_kinds=["private-text"], references_complete=True
        )


def test_safe_metadata_has_no_auth_settings_watermark_or_arbitrary_message():
    keys = asdict(coverage()).get("sources")[0].keys()
    assert set(keys) == {
        "connector_id",
        "source_kind",
        "last_outcome",
        "expected_read_interval",
        "last_successful_read",
        "last_attempted_read",
        "oldest_unprocessed_item",
        "behind_seconds",
        "reasons",
        "since",
    }
    with pytest.raises(CoverageError, match="^invalid_coverage_outcome$"):
        connector(last_outcome="private failure detail")


def test_frozen_outputs_and_inputs_have_no_mutation():
    rows = [connector()]
    missing = ["email"]
    before = asdict(rows[0])
    result = coverage(rows, not_connected=missing)
    missing.clear()
    assert result.not_connected == ("email",)
    assert asdict(rows[0]) == before
    with pytest.raises(FrozenInstanceError):
        result.sources[0].last_outcome = "failed"
    with pytest.raises(FrozenInstanceError):
        result.incomplete = True
    with pytest.raises(FrozenInstanceError):
        answer(result).answer = "found"


@pytest.mark.parametrize(
    "field", ["last_successful_read", "last_attempted_read", "oldest_unprocessed_item"]
)
@pytest.mark.parametrize(
    "bad",
    [
        "private-date",
        datetime(2026, 11, 1),
        42,
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_bad_source_dates_are_typed_safe_refusals(field, bad):
    with pytest.raises(CoverageError, match="^invalid_coverage_time$"):
        connector(**{field: bad})


@pytest.mark.parametrize("field", ["now", "question_at"])
def test_bad_reference_dates_are_typed_safe_refusals(field):
    with pytest.raises(CoverageError, match="^invalid_coverage_time$"):
        coverage(**{field: "private-date"})


@pytest.mark.parametrize("field", ["last_successful_read", "last_attempted_read"])
def test_future_success_or_attempt_cannot_establish_coverage(field):
    with pytest.raises(CoverageError, match="^invalid_coverage_time$"):
        coverage([connector(**{field: NOW + HOUR})])


def test_future_question_is_a_typed_refusal():
    with pytest.raises(CoverageError, match="^invalid_coverage_time$"):
        coverage(question_at=NOW + HOUR)


@pytest.mark.parametrize("interval", [0, -1, True, 1.5, "3600"])
def test_interval_is_a_positive_integer_number_of_seconds(interval):
    with pytest.raises(CoverageError, match="^invalid_coverage_interval$"):
        connector(expected_read_interval=interval)


def test_wrong_world_duplicate_rows_and_inconsistent_inventory_fail_safely():
    with pytest.raises(CoverageError, match="^invalid_coverage_world$"):
        coverage([replace(connector(), world_id=OTHER)])
    with pytest.raises(CoverageError, match="^duplicate_coverage_source$"):
        coverage([connector(), connector()])
    with pytest.raises(CoverageError, match="^inconsistent_coverage_inventory$"):
        coverage(not_connected=["calendar"])
    with pytest.raises(CoverageError, match="^invalid_coverage_world$"):
        evaluate_coverage(
            "private-world", [], now=NOW, inventory_complete=True, disclosure_complete=True
        )
