from datetime import UTC, datetime, timedelta

import pytest

from grove.reads.ranking import fuse, latest


def test_eligibility_precedes_fusion_ranks_and_any_page_limit():
    excluded = [f"other-{number}" for number in range(100)]
    assert fuse([excluded + ["eligible"], ["second"]], frozenset({"eligible", "second"})) == (
        "eligible",
        "second",
    )


def test_fusion_counts_each_retriever_once_and_boosts_exact_aliases():
    eligible = frozenset({"alias", "common", "single"})
    result = fuse(
        [
            ["single", "common", "common", "common"],
            ["common"],
        ],
        eligible,
        exact_aliases=frozenset({"alias", "hidden"}),
    )
    assert result == ("alias", "common", "single")
    assert fuse([["single", "common"]], eligible) == fuse(
        [["single", "single", "common"]],
        eligible,
    )


def test_past_value_retriever_can_have_lower_weight_without_changing_claims():
    assert fuse([["current"], ["past"]], frozenset({"current", "past"}), weights=[1, 0.5]) == (
        "current",
        "past",
    )


def test_latest_ignores_similarity_for_twenty_five_dated_events():
    base = datetime(2026, 10, 1, tzinfo=UTC)
    rows = {
        f"event-{number:02}": {
            "title": "release " * (25 - number),
            "became_true_earliest": base + timedelta(days=number),
            "recorded": base + timedelta(days=number),
        }
        for number in range(25)
    }
    assert latest(rows, frozenset(rows)) == tuple(reversed(tuple(rows)))


def test_latest_ties_use_record_time_then_id_and_unknowns_stay_last():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    rows = {
        "a": {"became_true_earliest": at, "recorded": at},
        "b": {"became_true_latest": at, "recorded": at},
        "newer-record": {"became_true_earliest": at, "recorded": at + timedelta(hours=1)},
        "unknown": {"became_true_mode": "unknown", "recorded": at + timedelta(days=30)},
    }
    assert latest(rows, frozenset(rows)) == ("newer-record", "a", "b", "unknown")


@pytest.mark.parametrize("weights", [[0], [True], [float("nan")], [float("inf")], [-1], [1, 2]])
def test_fusion_refuses_invalid_internal_weights(weights):
    with pytest.raises(ValueError):
        fuse([["item"]], frozenset({"item"}), weights=weights)
