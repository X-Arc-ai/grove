"""Synthetic ordered-progress and outbox controls, without native concurrency claims."""

from copy import deepcopy
from dataclasses import replace

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.durable_types import ConsumerReceipt
from grove.writer import learner_progress as progress
from grove.writer import learner_requests as boundary
from tests.writer.test_learner_execution import AT, finish_blank, intake  # noqa: F401
from tests.writer.test_learner_execution import harness as harness


def test_gap_blocks_cursor_and_later_commit(harness):
    tx, binding = harness
    intake(tx, binding)
    row = next(iter(tx.tables["LearnerEpisodeProgress"].values()))
    tx.tables["LearnerEpisodeProgress"]["later"] = {
        **row,
        "id": "later",
        "position": 2,
        "predecessor_progress_id": row["id"],
        "required_work_id": "later-work",
    }
    with pytest.raises(ExtractionRefused, match="order_window_unavailable"):
        progress._ordered(tx, binding.source.id, binding.profile)


def test_empty_to_extracted_counts_once_and_never_downgrades(harness):
    tx, binding = harness
    result = finish_blank(tx, binding)
    work = tx.tables["LearnerWork"]["work-1"]
    row = next(iter(tx.tables["LearnerEpisodeProgress"].values()))
    completion = tx.tables["LearnerCompletion"][result.completion_id]
    stamp = boundary._stamp(tx, "counter-2", "run-1", "request-1")
    before = deepcopy(tx.tables)
    progress.complete_progress_in_tx(tx, stamp, work, completion)
    assert tx.tables == before
    row.pop("completion_id")
    row.pop("completed_generation")
    completion = {**completion, "id": "extracted-2", "outcome": "done_extracted"}
    progress.complete_progress_in_tx(tx, stamp, work, completion)
    connector = tx.tables["Connector"]["connector-1"]
    assert (connector["held_nothing_count"], connector["extracted_count"]) == (0, 1)
    row = next(iter(tx.tables["LearnerEpisodeProgress"].values()))
    row.pop("completion_id")
    row.pop("completed_generation")
    progress.complete_progress_in_tx(
        tx, stamp, work, {**completion, "id": "empty-3", "outcome": "done_empty"}
    )
    connector = tx.tables["Connector"]["connector-1"]
    assert (connector["held_nothing_count"], connector["extracted_count"]) == (0, 1)


def test_import_watermark_and_counts_are_unchanged(harness):
    tx, binding = harness
    before = deepcopy(tx.tables["Connector"]["connector-1"])
    finish_blank(tx, binding)
    after = tx.tables["Connector"]["connector-1"]
    assert (after["watermark"], after["read_count"]) == (before["watermark"], before["read_count"])


def test_unknown_source_time_remains_visible_pending_work(harness):
    tx, binding = harness
    intake(tx, binding)
    next(iter(tx.tables["LearnerEpisodeProgress"].values())).pop("source_start")
    report = progress.read_extraction_progress_in_tx(tx, "connector-1")
    assert (report["pending"], report["unknown_pending"], report["oldest_unprocessed_item"]) == (
        1,
        1,
        None,
    )
    tx.tables["LearnerEpisodeProgress"].clear()
    with pytest.raises(ExtractionRefused):
        progress.read_extraction_progress_in_tx(tx, "connector-1")


def test_handoff_redelivery_returns_same_consumer_receipt(harness, monkeypatch):
    tx, binding = harness
    result = finish_blank(tx, binding)
    work = tx.tables["LearnerWork"]["work-1"]
    completion = tx.tables["LearnerCompletion"][result.completion_id]
    stamp = boundary._stamp(tx, "handoff-test", "run-1", "request-1")
    ids = progress.enqueue_handoffs_in_tx(tx, stamp, work, completion, ["target-1"])
    before = deepcopy(tx.tables)
    assert progress.enqueue_handoffs_in_tx(tx, stamp, work, completion, ["target-1"]) == ids
    assert tx.tables == before
    monkeypatch.setattr(boundary, "_require_consumer_in_tx", lambda *args: None)
    row = tx.tables["LearnerHandoff"][ids[0]]
    receipt = ConsumerReceipt(ids[0], row["kind"], "consumer-transaction", ("target-1",))
    first = progress.acknowledge_handoff_in_tx(tx, None, ids[0], 1, receipt)
    before = deepcopy(tx.tables)
    assert progress.acknowledge_handoff_in_tx(tx, None, ids[0], 1, receipt) == first
    assert tx.tables == before
    with pytest.raises(ExtractionRefused):
        progress.acknowledge_handoff_in_tx(
            tx, None, ids[0], 1, replace(receipt, accepted_transaction_id="other")
        )


def test_curator_intent_cannot_complete_v1_or_truncate_locator(harness, monkeypatch):
    tx, binding = harness
    result = finish_blank(tx, binding)
    work = tx.tables["LearnerWork"]["work-1"]
    completion = tx.tables["LearnerCompletion"][result.completion_id]
    stamp = boundary._stamp(tx, "handoff-test", "run-1", "request-1")
    with pytest.raises(ExtractionRefused):
        progress.enqueue_handoffs_in_tx(
            tx, stamp, work, completion, [f"target-{i}" for i in range(257)]
        )
    ids = progress.enqueue_handoffs_in_tx(tx, stamp, work, completion, ["target-1"])
    row = tx.tables["LearnerHandoff"][ids[0]]
    row["kind"] = "curator_question_v1"
    monkeypatch.setattr(boundary, "_require_consumer_in_tx", lambda *args: None)
    with pytest.raises(ExtractionRefused):
        progress.acknowledge_handoff_in_tx(
            tx, None, ids[0], 1, ConsumerReceipt(ids[0], row["kind"], "receipt", ("target-1",))
        )


def test_outbox_values_cannot_exceed_the_work_budget(harness, monkeypatch):
    from grove.extraction import durable_codec as codec

    tx, binding = harness
    result = finish_blank(tx, binding)
    work = tx.tables["LearnerWork"][binding.work.id]
    completion = tx.tables["LearnerCompletion"][result.completion_id]
    monkeypatch.setattr(codec, "MAX_WORK_BYTES", 1)
    with pytest.raises(ExtractionRefused, match="work_bounds"):
        progress.enqueue_handoffs_in_tx(
            tx,
            boundary._stamp(tx, "outbox-budget", binding.run.id, binding.work.request_id),
            work,
            completion,
            ["target"],
        )
