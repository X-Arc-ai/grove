"""D2 declines: every decline is logged by its operation, in ids and codes (S41).

Spec, D2 "Validation at write": "every decline is logged for the clean-up agent"; the declines
table holds "request id, operation id (its values stay in the run record, D14), the rule that
declined it, near-duplicate candidate ids with their scores, whether an override was used". D2
"How to test it": "a document's revision N after revision N-1 passes with an override and appears
in the decline log without one."
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from neo4j.exceptions import ServiceUnavailable

from grove import clock
from grove.clock import TestClock
from grove.store.query import WorldQuery
from grove.writer import (
    BatchDeclined,
    Refusal,
    apply_batch,
    apply_request,
    claim_next_request,
    decline_log,
    enqueue_request,
    fail_request,
    finish_request,
    request_state,
)
from grove.writer import api as api_module
from grove.writer import requests as requests_module
from grove.writer.declines import candidates_by_op, declines_from, stored_op_id
from tests.writer.helpers import (
    HOST,
    LEARNER,
    PERSON,
    READ_ONLY,
    batch,
    count_rows,
    create_item,
    observation,
    read,
    request_key,
    request_row,
    stale_update,
    unknown_kind,
    world_dump,
)

key = request_key
row_of = request_row

RECHECK = "2027-06-01T00:00:00+00:00"
FIRST = "Release notes revision 7"
SECOND = "Release notes revision 8"
SECRET = "Okonkwo keeps the salary figure in a drawer"


def rows_of(driver, world_id: str, request_id: str) -> dict[str, dict[str, Any]]:
    rows = read(driver, world_id, "MATCH (d:Decline {request_id: $id}) RETURN d", id=request_id)
    return {row["d"]["op_id"]: row["d"] for row in rows}


def note(chat: dict[str, str], title: str, line: int, **fields: Any) -> dict:
    ev = f"e{line}"
    return batch([create_item("observation", title, ev, **fields)], [observation(chat, ev, line)])


def first_revision(driver, world_id: str, chat: dict[str, str]) -> str:
    receipt = apply_batch(driver, world_id, PERSON, note(chat, FIRST, 1))
    return receipt.lines[0].entity_id


# Rows ------------------------------------------------------------------------------------------


def test_each_refused_operation_gets_one_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    ops = [
        create_item("spaceship", "The silver hull", "e1"),
        create_item("person", "Rowan", "e1"),
        create_item("airship", "The grey hull", "e1"),
    ]
    before = count_rows(driver, world_id)

    with pytest.raises(BatchDeclined):
        apply_batch(
            driver,
            world_id,
            PERSON,
            batch(ops, [observation(chat, "e1", 1)]),
            request_id=request_id,
            run_id="run-1",
        )

    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {ops[0]["op_id"], ops[2]["op_id"]}
    for row in rows.values():
        assert row["rule"] == "unknown_kind"
        assert row["code"] == "unknown_kind"
        assert row["override_used"] is False
        assert row["recorded_at"] is not None
        assert row["world_id"] == world_id
        assert "nearduplicate_candidate_ids" not in row
    assert count_rows(driver, world_id)["Decline"] == before.get("Decline", 0) + 2
    request = row_of(driver, world_id, request_id)
    assert request["status"] == "declined"
    assert request["last_error_code"] == "unknown_kind"
    assert request["attempts"] == 1
    assert request["run_id"] == "run-1"
    assert request["declined_fingerprint"]
    stored = json.loads(request["receipt"])
    assert stored["state"] == "declined"
    assert stored["batch_size"] == 3
    assert stored["request_id"] == request_id
    assert stored["refusals"] == [
        {
            "op_id": ops[0]["op_id"],
            "op": "create_item",
            "code": "unknown_kind",
            "rule": "unknown_kind",
        },
        {
            "op_id": ops[2]["op_id"],
            "op": "create_item",
            "code": "unknown_kind",
            "rule": "unknown_kind",
        },
    ]


def test_a_direct_decline_with_no_key_is_logged_under_the_minted_id(driver, seeded, chat):
    world_id = seeded["world_id"]

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, unknown_kind(chat))

    minted = declined.value.receipt.request_id
    assert minted
    assert set(rows_of(driver, world_id, minted))
    assert row_of(driver, world_id, minted)["status"] == "declined"


def test_a_fault_of_the_whole_batch_has_the_sentinel(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    twice = create_item("person", "Rowan", "e1", op_id="o-twice")
    sent = batch([twice, dict(twice)], [observation(chat, "e1", 1)])

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert declined.value.refusals[0].op_id is None
    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {"batch"}
    assert rows["batch"]["rule"] == "duplicate_op_id"
    assert rows["batch"]["code"] == "invalid_arguments"
    stored = json.loads(row_of(driver, world_id, request_id)["receipt"])
    assert stored["refusals"][0]["op_id"] == "batch"
    assert stored["refusals"][0]["op"] == "unknown"


@pytest.mark.parametrize(
    "batch_",
    [
        pytest.param("not an object", id="not_an_object"),
        pytest.param({"ops": [], "evidence": []}, id="no_ops"),
        pytest.param({1: "odd key", "ops": []}, id="keys_of_two_types"),
    ],
)
def test_a_batch_no_fingerprint_can_read_is_declined_as_a_fault(driver, seeded, batch_):
    world_id = seeded["world_id"]
    request_id = key()

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, batch_, request_id=request_id)

    assert declined.value.refusals[0].code == "invalid_arguments"
    assert set(rows_of(driver, world_id, request_id)) == {"batch"}


def test_a_replay_of_a_fault_of_the_whole_batch_names_no_operation(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    twice = create_item("person", "Rowan", "e1", op_id="o-twice")
    sent = batch([twice, dict(twice)], [observation(chat, "e1", 1)])
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    with pytest.raises(BatchDeclined) as replayed:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert replayed.value.refusals[0].op_id is None
    assert replayed.value.refusals[0].op is None
    assert replayed.value.refusals[0].rule == "duplicate_op_id"


@pytest.mark.parametrize(
    "op_id",
    [
        pytest.param(SECRET, id="words"),
        pytest.param("x" * 81, id="too_long"),
        pytest.param("batch", id="the_sentinel"),
    ],
)
def test_an_op_id_that_could_hold_text_is_stored_by_its_hash(driver, seeded, chat, op_id):
    world_id = seeded["world_id"]
    request_id = key()
    sent = batch(
        [create_item("spaceship", "The silver hull", "e1", op_id=op_id)],
        [observation(chat, "e1", 1)],
    )

    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    expected = "opaque:" + hashlib.sha256(op_id.encode()).hexdigest()[:16]
    assert set(rows_of(driver, world_id, request_id)) == {expected}
    stored = json.loads(row_of(driver, world_id, request_id)["receipt"])
    assert stored["refusals"][0]["op_id"] == expected
    assert stored["refusals"][0]["op_id"] != op_id


@pytest.mark.parametrize("op_id", ["o1", "op:7", "$temp-op_1.2", "x" * 80])
def test_a_plain_op_id_is_stored_as_it_came(op_id):
    assert stored_op_id(op_id) == op_id


def test_an_unknown_operation_name_is_stored_as_unknown(driver, seeded):
    world_id = seeded["world_id"]
    request_id = key()
    sent = batch([{"op": SECRET, "op_id": "o1"}])

    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    stored = json.loads(row_of(driver, world_id, request_id)["receipt"])
    assert stored["refusals"][0]["op"] == "unknown"
    assert SECRET not in world_dump(driver, world_id)


def test_no_message_or_details_are_stored(driver, seeded, chat):
    # The refusal repeats the caller's phrase, and a forget could not reach a copy in the log.
    world_id = seeded["world_id"]
    request_id = key()
    sent = batch(
        [create_item("person", "Rowan", "e1", temp_id="$r"), create_item(SECRET, "Hull", "e1")],
        [observation(chat, "e1", 1)],
    )

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert SECRET in declined.value.refusals[0].message, "the caller still reads the message"
    dump = world_dump(driver, world_id)
    assert SECRET not in dump
    assert '"message"' not in dump and '"details"' not in dump
    for row in rows_of(driver, world_id, request_id).values():
        assert "message" not in row and "details" not in row


def test_a_caller_with_no_write_scope_leaves_no_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as denied:
        apply_batch(driver, world_id, READ_ONLY, unknown_kind(chat), request_id=request_id)

    assert denied.value.refusals[0].rule == "write_scope"
    assert world_dump(driver, world_id) == before


def test_a_caller_kind_refusal_is_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    sent = note(chat, FIRST, 1)

    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, HOST, sent, request_id=request_id)

    rows = rows_of(driver, world_id, request_id)
    assert rows[sent["ops"][0]["op_id"]]["rule"] == "caller_kind"
    assert rows[sent["ops"][0]["op_id"]]["code"] == "scope_denied"


# Near-duplicates -------------------------------------------------------------------------------


def test_revision_n_without_an_override_is_refused_and_in_the_log(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = first_revision(driver, world_id, chat)
    request_id = key()
    sent = note(chat, SECOND, 2)

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert declined.value.refusals[0].code == "near_duplicate"
    row = rows_of(driver, world_id, request_id)[sent["ops"][0]["op_id"]]
    assert row["rule"] == "near_duplicate"
    assert row["override_used"] is False
    assert json.loads(row["nearduplicate_candidate_ids"]) == [{"item_id": first, "score": 0.84}]
    found = decline_log(driver, world_id, rule="near_duplicate")
    assert [(entry["request_id"], entry["override_used"]) for entry in found] == [
        (request_id, False)
    ]
    assert found[0]["candidates"] == [{"item_id": first, "score": 0.84}]


def test_revision_n_with_an_override_passes_and_is_in_the_log(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = first_revision(driver, world_id, chat)
    request_id = key()
    sent = note(
        chat, SECOND, 2, distinct_from=[{"item": first, "reason": "the next revision of it"}]
    )

    receipt = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert receipt.state == "saved"
    row = rows_of(driver, world_id, request_id)[sent["ops"][0]["op_id"]]
    assert row["rule"] == "near_duplicate"
    assert row["code"] == "near_duplicate"
    assert row["override_used"] is True
    assert json.loads(row["nearduplicate_candidate_ids"]) == [{"item_id": first, "score": 0.84}]
    state = request_state(driver, world_id, request_id)
    assert state.api_state == "saved"
    assert state.declines == [], "an override row is no refusal of this request"
    assert candidates_by_op(WorldQuery(driver, world_id), request_id) == {}


def test_a_distinct_from_that_names_no_candidate_writes_no_override_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    apply_batch(driver, world_id, PERSON, note(chat, "Invoice 12 from Okonkwo", 1))
    other = apply_batch(driver, world_id, PERSON, note(chat, "A boathouse plan", 2))
    far = other.lines[0].entity_id
    request_id = key()
    sent = note(
        chat, "Tamsin travel note", 3, distinct_from=[{"item": far, "reason": "not the same"}]
    )

    apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert rows_of(driver, world_id, request_id) == {}


def test_an_override_on_a_batch_that_declines_rolls_back(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = first_revision(driver, world_id, chat)
    request_id = key()
    passes = create_item(
        "observation", SECOND, "e2", distinct_from=[{"item": first, "reason": "the next revision"}]
    )
    fails = create_item("spaceship", "The silver hull", "e2")
    sent = batch([passes, fails], [observation(chat, "e2", 2)])

    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {fails["op_id"]}
    assert rows[fails["op_id"]]["override_used"] is False


def test_a_candidate_keeps_only_its_id_and_score():
    # Details are free to hold text, and a stored or answered candidate never does.
    candidates = [
        {"item_id": "a", "score": 0.9, "title": SECRET},
        {"title": SECRET},
        SECRET,
    ]
    refusal = Refusal(
        "o1", "create_item", "near_duplicate", "near_duplicate", "m", {"candidates": candidates}
    )

    declines = declines_from(BatchDeclined([refusal], batch_size=1))

    assert declines[0]["candidates"] == [{"item_id": "a", "score": 0.9}]


def test_a_replayed_decline_carries_its_candidates(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = first_revision(driver, world_id, chat)
    request_id = key()
    sent = note(chat, SECOND, 2)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = world_dump(driver, world_id)

    with pytest.raises(BatchDeclined) as replayed:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    refusal = replayed.value.refusals[0]
    assert refusal.op_id == sent["ops"][0]["op_id"]
    assert refusal.op == "create_item"
    assert refusal.code == "near_duplicate"
    assert refusal.details == {"candidates": [{"item_id": first, "score": 0.84}]}
    assert world_dump(driver, world_id) == before
    state = request_state(driver, world_id, request_id)
    assert state.declines == [
        {
            "op_id": sent["ops"][0]["op_id"],
            "op": "create_item",
            "code": "near_duplicate",
            "rule": "near_duplicate",
            "candidates": [{"item_id": first, "score": 0.84}],
        }
    ]


def test_a_repair_with_the_override_clears_the_refusal(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = first_revision(driver, world_id, chat)
    request_id = key()
    sent = note(chat, SECOND, 2)
    op_id = sent["ops"][0]["op_id"]
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    repair = note(
        chat,
        SECOND,
        2,
        op_id=op_id,
        distinct_from=[{"item": first, "reason": "the next revision of it"}],
    )

    apply_batch(driver, world_id, PERSON, repair, request_id=request_id)

    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {op_id}
    assert rows[op_id]["override_used"] is True
    assert count_rows(driver, world_id)["Decline"] == 1


def test_a_failed_repair_of_the_same_operation_overwrites_its_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    first_revision(driver, world_id, chat)
    request_id = key()
    sent = note(chat, SECOND, 2)
    op_id = sent["ops"][0]["op_id"]
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    assert rows_of(driver, world_id, request_id)[op_id]["nearduplicate_candidate_ids"]
    repair = batch(
        [create_item("spaceship", SECOND, "e2", op_id=op_id)], [observation(chat, "e2", 2)]
    )

    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, repair, request_id=request_id)

    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {op_id}
    assert rows[op_id]["rule"] == "unknown_kind"
    assert rows[op_id]["code"] == "unknown_kind"
    assert "nearduplicate_candidate_ids" not in rows[op_id]
    assert row_of(driver, world_id, request_id)["repairs"] == 1


# What a log may not do -------------------------------------------------------------------------


def test_a_failed_log_never_masks_the_decline_and_a_replay_logs_it(
    driver, seeded, chat, monkeypatch
):
    world_id = seeded["world_id"]
    request_id = key()
    sent = unknown_kind(chat)
    before = world_dump(driver, world_id)

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the log failed")

    monkeypatch.setattr(api_module, "log_decline", fail)

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert declined.value.refusals[0].code == "unknown_kind"
    assert world_dump(driver, world_id) == before

    monkeypatch.undo()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert set(rows_of(driver, world_id, request_id)) == {sent["ops"][0]["op_id"]}
    assert row_of(driver, world_id, request_id)["status"] == "declined"


def test_a_failed_log_never_masks_a_shape_fault(driver, seeded, chat, monkeypatch):
    world_id = seeded["world_id"]

    def fail(*args, **kwargs):
        raise ServiceUnavailable("injected: the log failed")

    monkeypatch.setattr(api_module, "log_decline", fail)

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, batch([{"op": "create_item", "op_id": "o1"}]))

    assert declined.value.refusals[0].rule == "missing_field"


def test_a_late_log_changes_nothing_on_a_saved_row(driver, seeded, chat):
    # A decline that lost a race: the row saved between the rollback and this log transaction.
    world_id = seeded["world_id"]
    request_id = key()
    apply_batch(driver, world_id, PERSON, note(chat, FIRST, 1), request_id=request_id)
    before = world_dump(driver, world_id)
    late = BatchDeclined(
        [_refusal("o-late", "unknown_kind", "unknown_kind")], batch_size=1, request_id=request_id
    )

    requests_module.log_decline(
        driver, world_id, PERSON, request_id, late, fingerprint="f-late", run_id="run-late"
    )

    assert world_dump(driver, world_id) == before


def test_a_late_log_of_a_conflict_changes_nothing_on_a_saved_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])
    apply_request(driver, world_id, LEARNER, request_id, note(chat, FIRST, 1))
    before = world_dump(driver, world_id)
    late = BatchDeclined(
        [_refusal("o-late", "version_conflict", "version_conflict")],
        batch_size=1,
        request_id=request_id,
    )

    requests_module.log_decline(
        driver, world_id, PERSON, request_id, late, fingerprint="f-late", run_id="run-late"
    )

    assert world_dump(driver, world_id) == before


def test_a_late_log_of_the_same_batch_spends_no_repair(driver, seeded, chat):
    # Two callers sent one batch, and both were declined: the second log finds the first.
    world_id = seeded["world_id"]
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    row = row_of(driver, world_id, request_id)
    before = world_dump(driver, world_id)
    late = BatchDeclined(
        [_refusal("o-late", "unknown_kind", "unknown_kind")], batch_size=1, request_id=request_id
    )

    requests_module.log_decline(
        driver,
        world_id,
        PERSON,
        request_id,
        late,
        fingerprint=row["declined_fingerprint"],
        run_id=None,
    )

    assert world_dump(driver, world_id) == before


def test_a_late_log_changes_nothing_when_the_repair_is_spent(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat, 2), request_id=request_id)
    before = world_dump(driver, world_id)
    late = BatchDeclined(
        [_refusal("o-late", "unknown_kind", "unknown_kind")], batch_size=1, request_id=request_id
    )

    requests_module.log_decline(
        driver, world_id, PERSON, request_id, late, fingerprint="f-late", run_id=None
    )

    assert world_dump(driver, world_id) == before


def _refusal(op_id: str, code: str, rule: str) -> Refusal:
    return Refusal(op_id, "create_item", code, rule, "words", {})


def _no_decline_rows(driver, world_id: str) -> None:
    assert count_rows(driver, world_id).get("Decline", 0) == 0


def test_a_refusal_of_the_key_is_never_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    saved = key()
    apply_batch(driver, world_id, PERSON, note(chat, FIRST, 1), request_id=saved)
    with pytest.raises(BatchDeclined) as reused:
        apply_batch(driver, world_id, PERSON, note(chat, "Tamsin note", 2), request_id=saved)
    assert reused.value.refusals[0].rule == "request_key_reused"
    _no_decline_rows(driver, world_id)


def test_a_spent_repair_is_never_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    first = unknown_kind(chat)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, first, request_id=request_id)
    second = unknown_kind(chat, 2)
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, second, request_id=request_id)
    logged = rows_of(driver, world_id, request_id)
    third = unknown_kind(chat, 3)

    with pytest.raises(BatchDeclined) as spent:
        apply_batch(driver, world_id, PERSON, third, request_id=request_id)

    assert spent.value.refusals[0].rule == "repair_spent"
    assert rows_of(driver, world_id, request_id) == logged


def test_the_queue_refusals_are_never_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    for call in (
        lambda: finish_request(driver, world_id, HOST, key(), worker="pass-1"),
        lambda: fail_request(driver, world_id, HOST, key(), "boom", worker="pass-1"),
        lambda: finish_request(driver, world_id, LEARNER, key(), worker="pass-1"),
        lambda: apply_request(driver, world_id, LEARNER, key(), unknown_kind(chat)),
        lambda: enqueue_request(driver, world_id, READ_ONLY, key(), source_id=chat["source_id"]),
        lambda: enqueue_request(driver, world_id, HOST, key(), source_id=str(uuid4())),
    ):
        with pytest.raises(BatchDeclined):
            call()
    _no_decline_rows(driver, world_id)
    assert count_rows(driver, world_id).get("Request", 0) == 0


def test_a_lost_lease_is_never_logged(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = key()
    enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])
    claim_next_request(driver, world_id, LEARNER, worker="pass-1")

    with pytest.raises(BatchDeclined):
        finish_request(driver, world_id, LEARNER, request_id, worker="pass-2")

    _no_decline_rows(driver, world_id)


# The form of a version conflict ----------------------------------------------------------------


def one_person(chat: dict[str, str], title: str = "Tamsin") -> dict:
    return batch([create_item("person", title, "e1")], [observation(chat, "e1", 1)])


def version_two(driver, world_id: str, chat: dict[str, str]) -> str:
    receipt = apply_batch(driver, world_id, PERSON, one_person(chat))
    item_id = receipt.lines[0].entity_id
    apply_batch(driver, world_id, PERSON, stale_update(chat, item_id, 2))
    return item_id


def test_a_direct_version_conflict_logs_rows_and_writes_no_request(driver, seeded, chat):
    world_id = seeded["world_id"]
    item_id = version_two(driver, world_id, chat)
    request_id = key()
    sent = stale_update(chat, item_id)

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert declined.value.retryable is True
    assert row_of(driver, world_id, request_id) is None
    rows = rows_of(driver, world_id, request_id)
    assert set(rows) == {sent["ops"][0]["op_id"]}
    assert rows[sent["ops"][0]["op_id"]]["rule"] == "version_conflict"


def test_a_version_conflict_leaves_a_row_no_enqueue_wrote(driver, seeded, chat):
    # Such a row must never reach the queue: nothing could claim it, and it would read "saving".
    world_id = seeded["world_id"]
    item_id = version_two(driver, world_id, chat)
    request_id = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=request_id)
    before = row_of(driver, world_id, request_id)
    sent = stale_update(chat, item_id)

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert declined.value.retryable is True
    assert row_of(driver, world_id, request_id) == before
    assert sent["ops"][0]["op_id"] in rows_of(driver, world_id, request_id)
    receipt = apply_batch(driver, world_id, PERSON, one_person(chat, "Ilse"), request_id=request_id)
    assert receipt.state == "saved", "the conflict did not spend the repair"


@pytest.mark.parametrize(
    ("refusals", "retryable"),
    [
        pytest.param([("version_conflict", "version_conflict")], True, id="conflict"),
        pytest.param(
            [
                ("version_conflict", "version_conflict"),
                ("invalid_arguments", "depends_on_declined"),
            ],
            True,
            id="conflict_and_its_dependent",
        ),
        pytest.param(
            [("version_conflict", "version_conflict"), ("unknown_kind", "unknown_kind")],
            False,
            id="conflict_and_another_refusal",
        ),
        pytest.param([("invalid_arguments", "depends_on_declined")], False, id="dependent_alone"),
        pytest.param([("unknown_kind", "unknown_kind")], False, id="another_refusal"),
    ],
)
def test_only_a_stale_version_makes_a_decline_retryable(refusals, retryable):
    built = [_refusal(f"o{n}", code, rule) for n, (code, rule) in enumerate(refusals)]
    assert BatchDeclined(built, batch_size=len(built)).retryable is retryable


def test_only_a_version_conflict_is_retryable(driver, seeded, chat):
    world_id = seeded["world_id"]
    with pytest.raises(BatchDeclined) as declined:
        apply_batch(driver, world_id, PERSON, unknown_kind(chat))
    assert declined.value.retryable is False


def test_a_stale_version_on_an_enqueued_row_goes_back_to_the_queue(driver, seeded, chat):
    world_id = seeded["world_id"]
    item_id = version_two(driver, world_id, chat)
    request_id = key()
    enqueue_request(driver, world_id, HOST, request_id, source_id=chat["source_id"])
    sent = stale_update(chat, item_id)

    state = apply_request(driver, world_id, LEARNER, request_id, sent)

    assert state.status == "queued"
    assert state.declines == []
    assert state.last_error_code == "version_conflict"
    row = row_of(driver, world_id, request_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["retry_at"] == row["touched_at"] + timedelta(seconds=30)
    assert rows_of(driver, world_id, request_id)[sent["ops"][0]["op_id"]]["code"] == (
        "version_conflict"
    )


# The log as a read -----------------------------------------------------------------------------


def test_decline_log_filters_and_limits(driver, seeded, chat):
    world_id = seeded["world_id"]
    first_revision(driver, world_id, chat)
    near = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, note(chat, SECOND, 2), request_id=near)
    unknown = key()
    with pytest.raises(BatchDeclined):
        apply_batch(driver, world_id, PERSON, unknown_kind(chat, 3), request_id=unknown)

    assert {entry["request_id"] for entry in decline_log(driver, world_id)} == {near, unknown}
    assert [e["request_id"] for e in decline_log(driver, world_id, rule="unknown_kind")] == [
        unknown
    ]
    assert [e["rule"] for e in decline_log(driver, world_id, request_id=near)] == ["near_duplicate"]
    assert decline_log(driver, world_id, rule="unknown_kind", request_id=near) == []
    assert len(decline_log(driver, world_id, limit=1)) == 1
    with pytest.raises(ValueError):
        decline_log(driver, world_id, limit=0)


def test_decline_log_is_newest_first_and_uses_the_clock(driver, seeded, chat):
    world_id = seeded["world_id"]
    test_clock = TestClock()
    at = datetime(2031, 1, 1, tzinfo=UTC)
    test_clock.set(at)
    with clock.using(test_clock):
        older = key()
        with pytest.raises(BatchDeclined):
            apply_batch(driver, world_id, PERSON, unknown_kind(chat), request_id=older)
        test_clock.advance(minutes=1)
        newer = key()
        with pytest.raises(BatchDeclined):
            apply_batch(driver, world_id, PERSON, unknown_kind(chat, 2), request_id=newer)

    found = decline_log(driver, world_id)

    assert [entry["request_id"] for entry in found] == [newer, older]
    assert found[1]["recorded_at"] == at
