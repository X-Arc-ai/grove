"""Synthetic Request boundary controls; no native database evidence."""

from datetime import UTC, datetime

import pytest

from grove.writer import requests
from grove.writer.caller import Caller
from grove.writer.errors import BatchDeclined, Refusal

AT = datetime(2026, 11, 4, tzinfo=UTC)
WORLD = "t_ingest-b_00000000-0000-4000-8000-000000000001"


class RequestTx:
    def __init__(self, row):
        self.row = row
        self.writes = []

    def run(self, query, **params):
        if query in (requests._READ, requests._LOCK_IF_DUE):
            return [{"r": self.row}]
        self.writes.append((query, params))
        return []

    def lock_world(self):
        pass


@pytest.fixture
def protected(monkeypatch):
    row = {
        "id": "request-1",
        "protocol": "learner_execution_v1",
        "status": "applying",
        "source_id": "source-1",
        "version": 2,
        "claimed_by": "run-1",
    }
    tx = RequestTx(row)
    monkeypatch.setattr(requests.clock, "now", lambda _: AT)
    monkeypatch.setattr(requests, "read_row", lambda *_: row)
    monkeypatch.setattr(requests.WorldWriter, "transaction", lambda _, work: work(tx))
    return row, tx


@pytest.mark.parametrize("shortcut", ["conflict_decline", "state_read"])
def test_conflict_decline_and_state_read_refuse_protected_request(protected, shortcut):
    _, tx = protected
    with pytest.raises(BatchDeclined, match="protected_request"):
        if shortcut == "state_read":
            requests.request_state(None, WORLD, "request-1")
        else:
            declined = BatchDeclined(
                [Refusal("op-1", "update_item", "version_conflict", "version", "stale", {})],
                batch_size=1,
                request_id="request-1",
            )
            assert declined.retryable
            requests.log_decline(
                None,
                WORLD,
                Caller("update", frozenset({"grove.propose"}), "worker"),
                "request-1",
                declined,
                fingerprint="digest",
                run_id="run-1",
            )
    assert tx.writes == []


@pytest.mark.parametrize(
    "shortcut",
    ["replay", "merge", "mark_saved", "receipt", "unavailable", "claim", "finish", "fail", "apply"],
)
def test_all_generic_shortcuts_refuse_protected_request(protected, shortcut):
    row, tx = protected
    caller = Caller("update", frozenset({"grove.propose"}), "worker")
    from datetime import timedelta

    with pytest.raises(BatchDeclined, match="protected_request"):
        if shortcut == "replay":
            requests.replay_rule(row, "digest")
        elif shortcut == "merge":
            requests.merge_row(tx, caller, "request-1", AT)
        elif shortcut == "mark_saved":
            requests.mark_saved(tx, row, None, "digest", "run-1")
        elif shortcut == "receipt":
            requests.receipt_of(None, WORLD, row)
        elif shortcut == "unavailable":
            requests.note_unavailable(None, WORLD, "request-1", "run-1")
        elif shortcut == "claim":
            requests._claim_one(tx, "request-1", "run-1", AT, timedelta(seconds=5))
        elif shortcut == "finish":
            requests.finish_request(None, WORLD, caller, "request-1", worker="run-1")
        elif shortcut == "fail":
            requests.fail_request(None, WORLD, caller, "request-1", "failed", worker="run-1")
        else:
            requests.apply_request(None, WORLD, caller, "request-1", {})
    assert tx.writes == []


def test_ordinary_and_question_requests_keep_existing_behavior():
    assert requests.replay_rule({"id": "ordinary", "status": "queued"}, "digest") == requests.APPLY
    with pytest.raises(BatchDeclined, match="protected_request"):
        requests.replay_rule({"id": "question", "protocol": "question_answer_v1"}, "digest")


def test_existing_request_cannot_change_protocol_or_work_identity(protected):
    _, tx = protected
    with pytest.raises(BatchDeclined, match="protected_request"):
        requests.merge_row(tx, None, "request-1", AT, protocol=None, learner_work_id="other")
    assert tx.writes == []
