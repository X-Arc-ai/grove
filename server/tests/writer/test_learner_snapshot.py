"""Pure transformed-snapshot controls; no database load or physical erasure."""

from copy import deepcopy

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.store.learner_snapshot import validate_learner_snapshot, validate_learner_value_closure
from tests.writer.test_learner_execution import finish_blank, intake  # noqa: F401
from tests.writer.test_learner_execution import harness as harness


def test_restore_rejects_inconsistent_journal_and_completion(harness):
    tx, binding = harness
    finish_blank(tx, binding)
    snapshot = tx.snapshot()
    validate_learner_snapshot(snapshot)
    for table, key, value in [
        ("learner_work", "state", "failed"),
        ("learner_completions", "message_count", 2),
        ("learner_executions", "ordinal", 99),
    ]:
        wrong = deepcopy(snapshot)
        wrong[table][0][key] = value
        with pytest.raises(ExtractionRefused):
            validate_learner_snapshot(wrong)


def test_restore_after_erasure_cannot_resurrect_values_or_work(harness):
    tx, binding = harness
    finish_blank(tx, binding)
    snapshot = tx.snapshot()
    for table in ("sources", "learner_completions", "learner_dispositions", "history_values"):
        wrong = deepcopy(snapshot)
        wrong[table] = []
        with pytest.raises(ExtractionRefused):
            validate_learner_snapshot(wrong)


def test_nested_private_value_closure_covers_all_seven_families(harness, monkeypatch):
    tx, binding = harness
    from tests.writer.test_learner_execution import finish_extracted

    finish_extracted(tx, monkeypatch)
    snapshot = tx.snapshot()
    validate_learner_value_closure(snapshot)
    # Every retained derived value must be reachable, and removal must be detected.
    for row in snapshot["history_values"]:
        wrong = deepcopy(snapshot)
        wrong["history_values"] = [r for r in wrong["history_values"] if r["id"] != row["id"]]
        with pytest.raises(ExtractionRefused):
            validate_learner_value_closure(wrong)


def test_restore_never_runs_outbox_or_changes_unknown_call_to_success(harness):
    from dataclasses import replace

    from grove.extraction.durable_types import StoredCallRequest
    from grove.extraction.execution_types import ExecutionEvent
    from grove.writer import learner_execution as execution
    from tests.extraction.test_dispositions import invocation
    from tests.writer.test_learner_execution import AT, current

    tx, binding = harness
    intake(tx, binding)
    head, run, _ = current(tx)
    execution.claim_execution_in_tx(tx, None, head, run, "claim-1")
    head, run, state = current(tx)
    call = invocation(binding, epoch=state.claim_epoch)
    request = StoredCallRequest(call.request, "{}")
    execution.record_invocation_in_tx(tx, None, head, run, "prepare-1", call, request)
    head, run, _ = current(tx)
    execution.record_invocation_in_tx(
        tx, None, head, run, "send-1", replace(call, state="in_flight"), request
    )
    head, run, state = current(tx)
    execution.record_failure_in_tx(
        tx,
        None,
        head,
        run,
        "unknown-1",
        ExecutionEvent("unknown", state.revision, state.claim_epoch, int(AT.timestamp())),
    )
    snapshot = tx.snapshot()
    before = deepcopy(snapshot)
    validate_learner_snapshot(snapshot)
    assert snapshot == before
    assert snapshot["learner_calls"][0]["state"] == "unknown"
    assert not snapshot.get("learner_completions")


def test_value_budget_is_per_work_not_per_snapshot(harness, monkeypatch):
    from grove.extraction import durable_codec as codec

    tx, binding = harness
    finish_blank(tx, binding)
    first = tx.snapshot()
    second = deepcopy(first)
    keys = {r["id"]: r["id"] + "-copy" for r in second["history_values"]}
    for rows in second.values():
        for row in rows:
            for field, value in row.items():
                if isinstance(value, str):
                    row[field] = keys.get(value, value)
            if row.get("work_id") == "work-1":
                row["work_id"] = "work-copy"
    second["learner_work"][0]["id"] = "work-copy"
    for row in second["history_values"]:
        for old, new in keys.items():
            row["value"] = row["value"].replace('"' + old + '"', '"' + new + '"')
    budget = max(
        sum(len(r["value"].encode()) for r in snap["history_values"]) for snap in (first, second)
    )
    monkeypatch.setattr(codec, "MAX_WORK_BYTES", budget)
    combined = {name: rows + second.get(name, []) for name, rows in first.items()}
    validate_learner_value_closure(combined)
    monkeypatch.setattr(codec, "MAX_WORK_BYTES", budget // 2)
    with pytest.raises(ExtractionRefused, match="work_bounds"):
        validate_learner_value_closure(combined)


@pytest.mark.parametrize("change", ["source", "screening", "request", "orphan_completion"])
def test_restore_rejects_lost_dependencies_and_orphan_success(harness, change):
    tx, binding = harness
    finish_blank(tx, binding)
    snapshot = tx.snapshot()
    if change == "source":
        snapshot["sources"][0]["content"] = "[]"
    elif change == "screening":
        snapshot["screening_receipts"] = []
    elif change == "request":
        snapshot["requests"][0]["source_id"] = "foreign"
    else:
        snapshot["learner_completions"].append(
            {**snapshot["learner_completions"][0], "id": "orphan"}
        )
    with pytest.raises(ExtractionRefused):
        validate_learner_snapshot(snapshot)
