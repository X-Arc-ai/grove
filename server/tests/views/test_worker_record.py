"""Synthetic controls of the worker record's row checks; these do not claim native Cypher."""

import json
import re
from datetime import timedelta

import pytest

from grove.api.errors import ApiError
from grove.store.probe import DriverError, Neo4jError
from grove.views import workers
from grove.views.session import ViewSession
from grove.views.workers import (
    AGENTS,
    FRESH_DAYS,
    MAX_BYTES,
    MAX_COUNT,
    MAX_ID,
    READ_TIMEOUT,
    TRAIL_ITEMS,
    TRAIL_RUNS,
    worker_record_payload,
)
from tests.views.worker_record_driver import (
    NO_ITEMS,
    NOW,
    WORLD_A,
    RecordDriver,
    freshness_row,
    read_name,
    run_row,
)

SESSION = ViewSession("user-a", WORLD_A, None)
PATTERN = re.compile(r"\((?:\w+)?:(?:Item|ChangeEntry|RunRecord)[^)]*\)|\[(?:\w+)?:LINK[^\]]*\]")


def world(runs=None, items=None, freshness=None):
    return {WORLD_A: {"runs": runs or {}, "items": items or {}, "freshness": freshness or NO_ITEMS}}


def filled():
    return RecordDriver(
        world(
            runs={"learner": [run_row("run-1")], "connector": [run_row("run-2", "failed")]},
            items={"run-1": ["item-1", "item-2"], "run-2": ["item-3"]},
            freshness=freshness_row(),
        )
    )


def payload(driver=None):
    return worker_record_payload(driver or RecordDriver(), SESSION, now=NOW)


def refused(driver):
    with pytest.raises(ApiError, match="worker activity is unavailable") as caught:
        payload(driver)
    assert caught.value.status == 503 and caught.value.code == "unavailable"


def test_an_empty_world_says_nothing_was_recorded_in_the_fixed_order():
    result = payload()
    assert result == {
        "schema": "grove.worker-record.v1",
        "observed_at": "2026-11-04T18:00:00.123456Z",
        "observation_kind": "stored_change_entries",
        "agents": [{"agent": agent, "last_run": None, "trail": None} for agent in AGENTS],
        "freshness": {
            "total_items": 0,
            "dated_items": 0,
            "window_days": 30,
            "fresh_items": 0,
            "newest_change_at": None,
            "oldest_change_at": None,
        },
    }
    assert AGENTS == ("learner", "curator", "connector")


def test_last_run_is_the_newest_run_and_trail_is_the_newest_run_with_changes():
    newest = run_row("run-new", "running", started=NOW - timedelta(minutes=1))
    changed = run_row("run-old", "ok")
    result = payload(
        RecordDriver(
            world(runs={"curator": [newest, changed]}, items={"run-old": ["item-1", "item-2"]})
        )
    )
    curator = result["agents"][1]
    assert curator["last_run"] == {
        "outcome": "running",
        "started_at": "2026-11-04T17:59:00.123456Z",
        "finished_at": None,
    }
    assert curator["trail"] == {
        "outcome": "ok",
        "started_at": "2026-11-04T17:00:00.123456Z",
        "finished_at": "2026-11-04T17:30:00.123456Z",
        "changed_items": 2,
        "item_ids": ["item-1", "item-2"],
        "has_more": False,
    }


def test_a_run_with_no_changes_leaves_last_run_and_no_trail():
    result = payload(RecordDriver(world(runs={"learner": [run_row("run-1")]})))
    assert result["agents"][0]["last_run"]["outcome"] == "ok"
    assert result["agents"][0]["trail"] is None


def test_has_more_compares_the_total_with_the_page():
    driver = RecordDriver(
        world(
            runs={"learner": [run_row("run-1")]},
            items={"run-1": [f"item-{n}" for n in range(TRAIL_ITEMS + 2)]},
        )
    )
    trail = payload(driver)["agents"][0]["trail"]
    assert trail["changed_items"] == TRAIL_ITEMS + 2
    assert len(trail["item_ids"]) == TRAIL_ITEMS == 50
    assert trail["has_more"] is True


def test_a_page_of_exactly_the_limit_has_no_more():
    driver = RecordDriver(
        world(
            runs={"learner": [run_row("run-1")]},
            items={"run-1": [f"item-{n}" for n in range(TRAIL_ITEMS)]},
        )
    )
    assert payload(driver)["agents"][0]["trail"]["has_more"] is False


def test_the_page_is_read_before_the_total_so_a_writing_run_cannot_shrink_it():
    driver = filled()
    payload(driver)
    names = [read_name(query) for query, _ in driver.calls]
    assert names.index("ids") < names.index("count")


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "unknown"},
        {"outcome": None},
        {"started": None},
        {"started": "private-time"},
        {"started": NOW.replace(tzinfo=None)},
        {"finished": "private-time"},
        {"finished": None},
        {"finished": NOW - timedelta(days=2)},
    ],
)
def test_a_malformed_run_reads_as_unknown_not_as_success(change):
    row = run_row("run-1", **change)
    result = payload(RecordDriver(world(runs={"learner": [row]}, items={"run-1": ["item-1"]})))
    assert result["agents"][0]["last_run"]["outcome"] == "unknown"
    assert result["agents"][0]["trail"]["outcome"] == "unknown"
    assert "private" not in json.dumps(result)


def test_a_running_run_with_a_finish_time_is_unknown():
    row = run_row("run-1", "running", finished=NOW)
    result = payload(RecordDriver(world(runs={"learner": [row]})))
    assert result["agents"][0]["last_run"]["outcome"] == "unknown"


@pytest.mark.parametrize("key", ["id", "outcome", "started", "finished"])
def test_a_run_row_with_a_missing_column_is_refused(key):
    row = run_row("run-1")
    del row[key]
    refused(RecordDriver(world(runs={"learner": [row]})))


@pytest.mark.parametrize("bad", [None, "", 7, True, ["run-1"], "x" * (MAX_ID + 1)])
def test_a_run_row_with_a_bad_id_is_refused(bad):
    refused(RecordDriver(world(runs={"learner": [run_row(bad)]})))


def test_a_run_row_with_an_extra_column_is_refused():
    refused(RecordDriver(world(runs={"learner": [run_row("run-1", summary="private")]})))


def test_more_runs_than_the_bound_are_refused_not_cut():
    driver = RecordDriver()
    driver.replies["runs"] = [run_row(f"run-{n}") for n in range(TRAIL_RUNS + 1)]
    refused(driver)


@pytest.mark.parametrize(
    "rows",
    [
        [{"run_id": "run-unknown"}],
        [{"run_id": 7}],
        [{"run_id": "run-1", "extra": 1}],
        [{}],
        [{"run_id": "run-1"}] * 3,
    ],
)
def test_a_changed_run_read_that_names_anything_else_is_refused(rows):
    driver = filled()
    driver.replies["changed"] = rows
    refused(driver)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{}],
        [{"item_id": None}],
        [{"item_id": ""}],
        [{"item_id": 7}],
        [{"item_id": True}],
        [{"item_id": "x" * (MAX_ID + 1)}],
        [{"item_id": "item-1", "title": "private"}],
        [{"item_id": "item-1"}, {"item_id": "item-1"}],
        [{"item_id": f"item-{n}"} for n in range(TRAIL_ITEMS + 1)],
    ],
)
def test_a_bad_trail_page_is_refused(rows):
    driver = filled()
    driver.replies["ids"] = rows
    refused(driver)


def test_a_trail_page_that_repeats_an_id_is_refused_when_the_total_fits():
    driver = filled()
    driver.replies["ids"] = [{"item_id": "item-1"}, {"item_id": "item-1"}]
    driver.replies["count"] = [{"changed_items": 2}]
    refused(driver)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{}],
        [{"changed_items": 2}, {"changed_items": 2}],
        [{"changed_items": True}],
        [{"changed_items": 2.0}],
        [{"changed_items": None}],
        [{"changed_items": -1}],
        [{"changed_items": 1}],
        [{"changed_items": 0}],
        [{"changed_items": MAX_COUNT + 1}],
    ],
)
def test_a_bad_trail_total_is_refused(rows):
    driver = filled()
    driver.replies["count"] = rows
    refused(driver)


@pytest.mark.parametrize(
    "change",
    [
        {"total_items": True},
        {"total_items": 3.0},
        {"total_items": None},
        {"total_items": MAX_COUNT + 1, "dated_items": MAX_COUNT + 1},
        {"dated_items": 4},
        {"dated_items": "2"},
        {"fresh_items": 3},
        {"fresh_items": -1},
        {"newest_change_at": None},
        {"oldest_change_at": None},
        {"newest_change_at": "private-time"},
        {"oldest_change_at": NOW.replace(tzinfo=None)},
        {"newest_change_at": NOW - timedelta(days=50)},
    ],
)
def test_a_bad_freshness_row_is_refused(change):
    refused(RecordDriver(world(freshness=freshness_row(**change))))


@pytest.mark.parametrize("key", ["newest_change_at", "oldest_change_at"])
def test_a_time_with_no_dated_item_is_refused(key):
    row = {**NO_ITEMS, key: NOW}
    refused(RecordDriver(world(freshness=row)))


@pytest.mark.parametrize(
    "rows", [[], [freshness_row(), freshness_row()], [{"total_items": 3}], [{}]]
)
def test_a_freshness_read_of_the_wrong_shape_is_refused(rows):
    driver = RecordDriver()
    driver.replies["freshness"] = rows
    refused(driver)


def test_freshness_gives_the_store_counts_and_the_time_window():
    driver = RecordDriver(world(freshness=freshness_row()))
    assert payload(driver)["freshness"] == {
        "total_items": 3,
        "dated_items": 2,
        "window_days": 30,
        "fresh_items": 1,
        "newest_change_at": "2026-11-03T18:00:00.123456Z",
        "oldest_change_at": "2026-09-25T18:00:00.123456Z",
    }
    params = next(p for query, p in driver.calls if read_name(query) == "freshness")
    assert params["now"] == NOW
    assert params["cutoff"] == NOW - timedelta(days=FRESH_DAYS)
    assert FRESH_DAYS == 30


@pytest.mark.parametrize("failure", [Neo4jError("private-detail"), DriverError("private-detail")])
@pytest.mark.parametrize("read", ["runs", "changed", "ids", "count", "freshness"])
def test_a_failed_read_is_unavailable_and_never_an_empty_record(failure, read):
    driver = filled()
    driver.failure, driver.fail_on = failure, read
    refused(driver)


def test_the_cap_holds_fifty_ids_of_the_longest_form_for_all_three_agents():
    runs = {agent: [run_row(f"run-{agent}")] for agent in AGENTS}
    items = {
        f"run-{agent}": [f"{agent[0]}{n:03}".ljust(MAX_ID, "x") for n in range(TRAIL_ITEMS)]
        for agent in AGENTS
    }
    result = payload(RecordDriver(world(runs=runs, items=items, freshness=freshness_row())))
    assert [len(agent["trail"]["item_ids"]) for agent in result["agents"]] == [TRAIL_ITEMS] * 3
    size = len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    assert size < MAX_BYTES == 32 * 1024


def test_a_record_above_the_cap_is_refused(monkeypatch):
    result = payload(filled())
    size = len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
    monkeypatch.setattr(workers, "MAX_BYTES", size)
    assert payload(filled()) == result
    monkeypatch.setattr(workers, "MAX_BYTES", size - 1)
    refused(filled())


def test_a_malformed_clock_is_refused_before_any_read():
    driver = filled()
    with pytest.raises(ApiError):
        worker_record_payload(driver, SESSION, now=NOW.replace(tzinfo=None))
    assert not driver.calls


def test_every_read_is_bounded_in_time_and_scoped_in_every_pattern():
    driver = filled()
    payload(driver)
    assert driver.timeouts == [READ_TIMEOUT] * len(driver.calls)
    assert {read_name(query) for query, _ in driver.calls} == {
        "runs",
        "changed",
        "ids",
        "count",
        "freshness",
    }
    for query, params in driver.calls:
        assert params["world_id"] == WORLD_A
        patterns = PATTERN.findall(query)
        assert patterns
        assert all("world_id: $world_id" in pattern for pattern in patterns)


def test_no_read_collects_change_rows_or_names_a_text_field():
    driver = filled()
    payload(driver)
    for query, _ in driver.calls:
        assert "collect(" not in query.lower()
        for forbidden in (
            "title",
            "summary",
            "body",
            "input_ids",
            "proposed_operations",
            "receipts",
            "new_value_id",
            "reason",
            "owner_user_id",
        ):
            assert forbidden not in query
    ids = next(query for query, _ in driver.calls if read_name(query) == "ids")
    assert ids.endswith("ORDER BY newest DESC, item_id LIMIT $limit")
