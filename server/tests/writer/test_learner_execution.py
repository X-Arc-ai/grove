"""Synthetic transaction controls. Native tests below require a separate explicit run."""

import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.contracts import ExtractionRefused
from grove.extraction.durable_types import IntakeCommand, IntakeOrder, RunWitness
from grove.extraction.execution_types import Disposition, ExecutionEvent, Proposal
from grove.store.catalogue import TABLES
from grove.writer import learner_execution as execution
from grove.writer import learner_requests as boundary
from tests.extraction.test_execution_codec import document, packed

AT = datetime(2026, 11, 4, 14, tzinfo=UTC)
WORLD = "t_ingest-b_00000000-0000-4000-8000-000000000001"


class MemoryTx:
    """Statement-level test double; this does not prove native rollback or locking."""

    def __init__(self, world_id=WORLD):
        self.world_id = world_id
        self.tables = {}
        self.writes = 0
        self.locked = False

    def lock_world(self):
        self.locked = True

    def run(self, query, **params):
        label = "Link" if ":LINK " in query else re.search(r"\([a-z]+:([A-Za-z]+)", query).group(1)
        rows = self.tables.setdefault(label, {})
        if query.startswith("CREATE"):
            row = deepcopy(params["row"])
            row["world_id"] = self.world_id
            assert row["id"] not in rows
            rows[row["id"]] = row
            self.writes += 1
            return []
        if " SET " in query:
            row = rows[params["id"]]
            row.update(deepcopy(params["row"]))
            for key in list(row):
                if row[key] is None:
                    del row[key]
            self.writes += 1
            return []
        if "WHERE $source_id IN n.source_ids" in query:
            found = [r for r in rows.values() if params["source_id"] in r.get("source_ids", [])]
        else:
            found = [r for r in rows.values() if all(r.get(k) == v for k, v in params.items())]
        alias = re.search(r" RETURN ([a-z]+)", query).group(1)
        return [{alias: deepcopy(row)} for row in found]

    def atomic(self, work):
        before, count = deepcopy(self.tables), self.writes
        try:
            return work(self)
        except BaseException:
            self.tables, self.writes = before, count
            raise

    def snapshot(self):
        labels = {t.label: t.name for t in TABLES}
        return {
            labels[k]: list(deepcopy(v).values()) for k, v in self.tables.items() if k in labels
        }


def make_binding(text=" "):
    data = document(text)
    data["world_id"] = WORLD
    data["schema_digest"] = codec.writer_schema_digest()
    data["run"]["deadline"] = int((AT + timedelta(minutes=10)).timestamp())
    data["work"]["lease_deadline"] = int((AT + timedelta(minutes=9)).timestamp())
    return legacy.decode_binding(packed(data))


def seed(tx, binding):
    b = binding
    values = {
        "Source": [
            {
                "id": b.source.id,
                "version": 1,
                "connector_id": b.producer.id,
                "content": b.source.content,
                "speakers": b.source.speakers,
                "parser_version": b.producer.parser,
                "forget_state": "retained",
                "received_at": datetime.fromisoformat(b.source.received_at),
                "external_locator": "synthetic:session",
            }
        ],
        "Connector": [
            {
                "id": b.producer.id,
                "version": 1,
                "type": b.producer.connector_type,
                "provider": b.producer.provider,
                "batch_rule": b.producer.batch_rule,
                "watermark": "import-9",
                "read_count": 19,
                "extracted_count": 0,
                "held_nothing_count": 0,
            }
        ],
        "Episode": [
            {
                "id": b.episode.id,
                "version": 1,
                "connector_id": b.producer.id,
                "source_ids": [b.source.id],
                "first_message_key": b.episode.first_key,
                "last_message_key": b.episode.last_key,
                "cut_rule": b.episode.cut_rule,
                "start_time": datetime.fromisoformat(b.episode.start),
                "end_time": datetime.fromisoformat(b.episode.end),
            }
        ],
        "RunRecord": [
            {
                "id": b.run.id,
                "version": 7,
                "agent": "learner",
                "input_ids": [b.work.request_id],
                "outcome": "running",
                "token_cap": b.run.token_cap,
                "time_cap": 600,
                "started": AT,
            }
        ],
        "ScreeningReceipt": [{"id": b.source.screening_id, "version": 1}],
        "HistoryValue": [
            {
                "id": "manifest-1",
                "version": 1,
                "value": codec.canonical(
                    {
                        "schema": "grove.learner.order.v1",
                        "stream_anchor_source_id": b.source.id,
                        "episode_ids": [b.episode.id],
                    }
                ).decode(),
            }
        ],
    }
    for label, rows in values.items():
        tx.tables[label] = {r["id"]: {**r, "world_id": tx.world_id} for r in rows}


@pytest.fixture
def harness(monkeypatch):
    test_clock = clock.TestClock()
    test_clock.set(AT)
    with clock.using(test_clock):
        tx, binding = MemoryTx(), make_binding()
        seed(tx, binding)
        # This explicit test replacement is not a production provider.
        monkeypatch.setattr(
            boundary, "_require_authority_in_tx", lambda tx, session, purpose, binding, run: binding
        )
        yield tx, binding


def intake(tx, binding):
    witness = RunWitness(binding.run.id, 7, 1, binding.run.pass_id, binding.run.input_ids)
    command = IntakeCommand(
        "intake-1",
        binding.work.id,
        binding.work.request_id,
        "execution-initial",
        binding,
        witness,
        IntakeOrder(binding.source.id, 0, None, None, "manifest-1"),
    )
    execution.enqueue_episode_in_tx(tx, None, command)
    return command


def current(tx, work_id="work-1"):
    work, request, row, state, witness, _ = boundary._load_work(tx, work_id)
    return execution._head(work, request, row), witness, state


def ready_blank(tx, binding):
    intake(tx, binding)
    head, witness, _ = current(tx)
    execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    head, witness, state = current(tx)
    proposal = Proposal(
        legacy.binding_digest(binding),
        (),
        (),
        (Disposition("msg1", "key:msg1", "ignored_blank", None, ()),),
        (),
    )
    payload = codec.decode_payload(
        codec.canonical(
            {
                "schema": "grove.learner.payload.v1",
                "writer_revision": binding.source_revision,
                "binding_digest": legacy.binding_digest(binding),
                "batch": {"ops": [], "evidence": []},
                "operation_map": [],
            }
        )
    )
    execution.record_proposal_in_tx(tx, None, head, witness, "proposal-1", proposal, payload)
    return proposal, payload


def finish_blank(tx, binding):
    proposal, payload = ready_blank(tx, binding)
    head, witness, _ = current(tx)
    result = execution.complete_execution_in_tx(
        tx, None, head, witness, "complete-1", proposal, payload
    )
    return result


def test_valid_empty_completion_uses_no_ordinary_batch_executor(harness, monkeypatch):
    tx, binding = harness
    monkeypatch.setattr(
        execution, "execute_prepared_in_tx", lambda *_: pytest.fail("ordinary empty executor")
    )
    result = finish_blank(tx, binding)
    assert result.outcome == "done_empty"
    assert tx.tables["Request"]["request-1"]["status"] == "saved"
    assert len(tx.tables["LearnerDisposition"]) == 1
    assert tx.tables["Connector"]["connector-1"]["held_nothing_count"] == 1


def test_synthetic_completion_replay_is_a_noop(harness):
    tx, binding = harness
    result = finish_blank(tx, binding)
    _, witness, _ = current(tx)
    before = deepcopy(tx.tables)
    assert (
        execution.read_completion_in_tx(tx, None, witness, "work-1", result.completion_id) == result
    )
    assert tx.tables == before


def test_actual_run_version_mismatch_rejects_old_witness(harness):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    tx.tables["RunRecord"]["run-1"]["version"] += 1
    with pytest.raises(ExtractionRefused, match="stale_claim"):
        execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    assert tx.tables["Request"]["request-1"]["status"] == "queued"


@pytest.mark.parametrize(
    "field,value", [("content", "[]"), ("speakers", "{}"), ("parser_version", "whatsapp.v1")]
)
def test_same_version_source_bytes_or_speaker_change_refuses(harness, field, value):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    tx.tables["Source"]["source-1"][field] = value
    with pytest.raises(ExtractionRefused):
        execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")


def test_two_claimants_have_one_current_epoch(harness):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    with pytest.raises(ExtractionRefused, match="stale_claim"):
        execution.claim_execution_in_tx(tx, None, head, witness, "claim-2")
    assert current(tx)[2].claim_epoch == 1


def test_stale_failure_cannot_reverse_new_claim_or_saved_work(harness):
    tx, binding = harness
    proposal, payload = ready_blank(tx, binding)
    head, witness, state = current(tx)
    execution.complete_execution_in_tx(tx, None, head, witness, "complete-1", proposal, payload)
    before = deepcopy(tx.tables)
    with pytest.raises(ExtractionRefused):
        execution.record_failure_in_tx(
            tx,
            None,
            head,
            witness,
            "late-failure",
            ExecutionEvent("decline", state.revision, state.claim_epoch, int(AT.timestamp())),
        )
    assert tx.tables == before


def test_current_run_source_and_full_context_checked_inside_lock(harness, monkeypatch):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)

    def changed(tx, session, purpose, value, run):
        assert tx.locked
        return replace(value, context=replace(value.context, history_sequence=99))

    monkeypatch.setattr(boundary, "_require_authority_in_tx", changed)
    with pytest.raises(ExtractionRefused, match="dependencies_changed"):
        execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")


def test_meter_generation_change_does_not_stale_semantic_binding(harness, monkeypatch):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    monkeypatch.setattr(
        boundary,
        "_require_authority_in_tx",
        lambda tx, s, p, b, r: replace(b, context=replace(b.context, read_generation=999)),
    )
    execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    assert current(tx)[2].status == "running"


def test_synthetic_exception_after_each_write_rolls_back_all_homes(harness, monkeypatch):
    tx, binding = harness
    proposal, payload = ready_blank(tx, binding)
    head, witness, _ = current(tx)
    before = deepcopy(tx.tables)
    original = tx.run
    for cut in range(1, 14):
        remaining = [cut]

        def failure(query, remaining=remaining, **params):
            result = original(query, **params)
            if query.startswith("CREATE") or " SET " in query:
                remaining[0] -= 1
                if remaining[0] == 0:
                    raise RuntimeError("synthetic cut")
            return result

        monkeypatch.setattr(tx, "run", failure)
        with pytest.raises(RuntimeError, match="synthetic cut"):
            tx.atomic(
                lambda t: execution.complete_execution_in_tx(
                    t, None, head, witness, "complete-1", proposal, payload
                )
            )
        assert tx.tables == before
    monkeypatch.setattr(tx, "run", original)


def test_identical_claim_event_replays_without_mutation(harness):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    first = execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    before = deepcopy(tx.tables)
    assert execution.claim_execution_in_tx(tx, None, head, witness, "claim-1") == first
    assert tx.tables == before
    with pytest.raises(ExtractionRefused):
        execution.claim_execution_in_tx(
            tx, None, replace(head, request_version=9), witness, "claim-1"
        )


def test_every_retained_message_and_candidate_class_is_required():
    from grove.extraction.dispositions import plan_completion
    from tests.extraction.test_dispositions import proposal

    binding = make_binding("I chose Cedar.")
    value, trace = proposal(binding, classes=("decision", "report_state"), empty=False)
    with pytest.raises(ExtractionRefused):
        plan_completion(binding, replace(value, dispositions=()), trace)
    disposition = replace(value.dispositions[0], classes=value.dispositions[0].classes[:1])
    with pytest.raises(ExtractionRefused):
        plan_completion(binding, replace(value, dispositions=(disposition,)), trace)


@pytest.mark.parametrize(
    "state", ["held_fact", "unresolved", "rejected", "unknown", "redaction_required"]
)
def test_held_unresolved_rejected_and_unknown_never_complete(state):
    from grove.extraction.dispositions import plan_completion
    from tests.extraction.test_dispositions import proposal

    binding = make_binding("I chose Cedar.")
    value, trace = proposal(binding)
    value = replace(value, dispositions=(replace(value.dispositions[0], state=state),))
    with pytest.raises(ExtractionRefused):
        plan_completion(binding, value, trace)


def test_empty_completion_requires_blank_or_latest_complete_triage():
    from grove.extraction.dispositions import plan_completion
    from tests.extraction.test_dispositions import proposal

    binding = make_binding("I chose Cedar.")
    value, trace = proposal(binding)
    with pytest.raises(ExtractionRefused):
        plan_completion(binding, value, None)
    later = replace(
        value.invocations[0],
        id="unknown-later",
        ordinal=2,
        state="unknown",
        output=None,
        output_digest=None,
        model_call_id=None,
    )
    with pytest.raises(ExtractionRefused):
        plan_completion(binding, replace(value, invocations=value.invocations + (later,)), trace)


def test_model_id_without_owner_association_refuses():
    with pytest.raises(ExtractionRefused, match="learner_authority_unavailable"):
        boundary._require_attempt_in_tx(None, object(), "model-1", None, None)


def test_exact_times_speakers_and_typed_source_guard_survive_storage(harness):
    tx, binding = harness
    intake(tx, binding)
    assert current(tx)[2].binding.inventory == binding.inventory
    tx.tables["Source"]["source-1"]["content"] = '[{"source_facts":{}}]'
    head, witness, _ = current(tx)
    with pytest.raises(ExtractionRefused):
        execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")


def test_recovery_checks_old_claimant_and_new_real_run_separately(harness):
    tx, binding = harness
    intake(tx, binding)
    head, witness, _ = current(tx)
    execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    head, witness, state = current(tx)
    tx.tables["RunRecord"]["unrelated"] = {
        **tx.tables["RunRecord"]["run-1"],
        "id": "unrelated",
        "outcome": "killed",
    }
    event = ExecutionEvent("recover", state.revision, state.claim_epoch, int(AT.timestamp()))
    with pytest.raises(ExtractionRefused):
        execution.record_failure_in_tx(
            tx, None, head, replace(witness, actual_run_id="unrelated"), "recover-1", event
        )
    tx.tables["RunRecord"]["run-1"].update(outcome="killed", version=8)
    execution.record_failure_in_tx(
        tx, None, head, replace(witness, actual_row_version=8), "recover-1", event
    )
    head, witness, state = current(tx)
    tx.tables["RunRecord"]["replacement"] = {
        **tx.tables["RunRecord"]["run-1"],
        "id": "replacement",
        "outcome": "running",
        "version": 1,
    }
    execution.claim_execution_in_tx(
        tx,
        None,
        head,
        replace(
            witness, actual_run_id="replacement", actual_row_version=1, pass_id="replacement-pass"
        ),
        "claim-2",
    )
    assert current(tx)[2].claimed_by == "replacement"


def test_recovery_does_not_renew_deadline_or_budget(harness):
    tx, binding = harness
    intake(tx, binding)
    tx.tables["RunRecord"]["run-1"]["time_cap"] = 99999
    run = RunWitness("run-1", 7, 1, "pass-1", ("request-1",))
    checked = boundary.check_run_witness_in_tx(tx, run, binding)
    assert checked.deadline == binding.run.deadline
    assert checked.token_cap == binding.run.token_cap


def test_only_one_repair_survives_concurrent_requeue_and_rebind(harness):
    tx, binding = harness
    ready_blank(tx, binding)
    head, witness, state = current(tx)
    execution.record_failure_in_tx(
        tx,
        None,
        head,
        witness,
        "decline-1",
        ExecutionEvent("decline", state.revision, state.claim_epoch, int(AT.timestamp())),
    )
    head, witness, state = current(tx)
    execution.record_failure_in_tx(
        tx,
        None,
        head,
        witness,
        "repair-1",
        ExecutionEvent("repair", state.revision, state.claim_epoch, int(AT.timestamp())),
    )
    assert current(tx)[2].writer_repairs == 1
    assert tx.tables["Request"]["request-1"]["repairs"] == 1
    with pytest.raises(ExtractionRefused):
        execution.record_failure_in_tx(
            tx,
            None,
            head,
            witness,
            "repair-2",
            ExecutionEvent("repair", state.revision, state.claim_epoch, int(AT.timestamp())),
        )


def settle(tx, binding, stage, raw, normalized, ordinal, packet, monkeypatch):
    from grove.extraction.durable_types import (
        RecordedAttemptWitness,
        StoredCallRequest,
        StoredCallResult,
    )
    from tests.extraction.test_dispositions import invocation

    head, witness, state = current(tx)
    normalized_request = codec.canonical(
        {"binding_digest": legacy.binding_digest(binding), "stage": stage, "payload": packet}
    ).decode()
    output = codec.canonical(normalized).decode()
    call = invocation(binding, stage, normalized, ordinal, state.claim_epoch)
    call = replace(
        call,
        request=normalized_request,
        request_digest=legacy.text_digest(normalized_request),
        output=output,
        output_digest=legacy.text_digest(output),
    )
    prepared = replace(call, state="prepared", output=None, output_digest=None, model_call_id=None)
    body = (
        {"messages": [{"role": "user", "content": codec.canonical(packet).decode()}]}
        if stage == "triage"
        else {"input": codec.canonical(packet).decode()}
    )
    request = StoredCallRequest(normalized_request, codec.canonical(body).decode())
    execution.record_invocation_in_tx(
        tx, None, head, witness, f"prepare-{ordinal}", prepared, request
    )
    head, witness, _ = current(tx)
    execution.record_invocation_in_tx(
        tx, None, head, witness, f"send-{ordinal}", replace(prepared, state="in_flight"), request
    )
    selected = codec.canonical(raw).decode()
    result = StoredCallResult(
        codec.canonical(
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": selected}]}
                ],
            }
        ).decode(),
        selected,
        output,
        binding.source_revision,
    )
    digests = [
        legacy.data_digest("grove.learner." + name.replace("_", "-") + ".v1", text)
        for name, text in zip(
            codec.DIGEST_NAMES,
            (
                request.provider_request_body,
                result.provider_response_text,
                result.selected_output_text,
                request.normalized_request,
                result.normalized_output,
            ),
            strict=True,
        )
    ]
    attempt = RecordedAttemptWitness(
        call.id,
        call.model_call_id,
        call.run_id,
        call.claim_epoch,
        call.stage,
        call.route,
        call.model,
        call.job,
        call.effort,
        "ok",
        *digests,
    )
    monkeypatch.setattr(boundary, "_require_attempt_in_tx", lambda *args: "owner-receipt")
    head, witness, _ = current(tx)
    execution.record_result_in_tx(tx, None, head, witness, f"result-{ordinal}", attempt, result)
    return call


def ready_extracted(tx, monkeypatch):
    from dataclasses import asdict

    from grove.extraction import pipeline
    from grove.extraction.execution_types import (
        CandidateClass,
        ClassOutcome,
        EvidenceRef,
        OperationRef,
        TriageTrace,
    )
    from tests.extraction.test_pipeline import document as wire_document
    from tests.extraction.test_pipeline import registry
    from tests.extraction.test_pipeline import triage as wire_triage

    binding = make_binding("I chose Cedar.")
    seed(tx, binding)
    intake(tx, binding)
    head, witness, _ = current(tx)
    execution.claim_execution_in_tx(tx, None, head, witness, "claim-1")
    batch = codec._source_batch(binding)
    raw = wire_triage(batch)
    normalized = {
        "reviewed_locators": ["key:msg1"],
        "candidates": [{"locator": "key:msg1", "statement_class": "decision"}],
    }
    triage_call = settle(
        tx,
        binding,
        "triage",
        raw,
        normalized,
        1,
        {"source_data": [asdict(m) for m in batch.messages]},
        monkeypatch,
    )
    raw = wire_document()
    raw["episode"] = {"episode_id": binding.episode.id, "source_id": binding.source.id}
    raw["evidence"][0]["locator"] = "key:msg1"
    reg = registry()
    observations, facts = pipeline._observations(raw, batch, {"key:msg1"})
    operations = pipeline._operations(raw, facts, reg, "model-2")
    ops = tuple(
        OperationRef(
            op["op_id"],
            op["op"],
            legacy.data_digest(
                "grove.learner.operation.v1", {"operation": op, "evidence": observations}
            ),
            tuple(pipeline.cited_ev_ids(op)),
        )
        for op in operations
    )
    evs = (EvidenceRef("e1", "key:msg1", "decision"),)
    disposition = Disposition(
        "msg1",
        "key:msg1",
        "extracted",
        None,
        (ClassOutcome("decision", "decision", "extracted", ("o1",), ("e1",), None),),
    )
    normalized = {
        "operations": codec.plain(ops),
        "evidence": codec.plain(evs),
        "dispositions": codec.plain((disposition,)),
    }
    packet = {
        "registry_data": asdict(reg),
        "source_data": [asdict(m) for m in batch.messages],
        "triage_classes": [["key:msg1", "decision"]],
    }
    settle(tx, binding, "extraction", raw, normalized, 2, packet, monkeypatch)
    head, witness, state = current(tx)
    proposal = Proposal(legacy.binding_digest(binding), ops, evs, (disposition,), state.invocations)
    trace = TriageTrace(
        triage_call.id,
        triage_call.request_digest,
        triage_call.output_digest,
        ("key:msg1",),
        (CandidateClass("key:msg1", "decision"),),
    )
    payload = codec.decode_payload(
        codec.canonical(
            {
                "schema": "grove.learner.payload.v1",
                "writer_revision": binding.source_revision,
                "binding_digest": legacy.binding_digest(binding),
                "batch": {"ops": operations, "evidence": observations},
                "operation_map": [{"descriptor_id": "o1", "operation_index": 0}],
            }
        )
    )
    execution.record_proposal_in_tx(tx, None, head, witness, "proposal-1", proposal, payload, trace)
    return binding, proposal, payload, trace


def test_complete_commits_graph_ledger_request_progress_and_outbox_once(harness, monkeypatch):
    """Synthetic adapter integration; native semantics need separate authorization."""
    tx, _ = harness
    finish_extracted(tx, monkeypatch)


def finish_extracted(tx, monkeypatch):
    binding, proposal, payload, trace = ready_extracted(tx, monkeypatch)

    def ordinary(tx, stamp, prepared, request):
        assert prepared.request_id == request["id"]
        assert prepared.caller.kind == "update"
        batch = codec.load_json(prepared.document)
        op = batch["ops"][0]
        boundary._put(
            tx,
            "Item",
            {"id": "actual-item", "title": op["title"], "kind": "resolved-kind-id", "version": 1},
        )
        ev = {
            **batch["evidence"][0],
            "id": "actual-evidence",
            "target_id": "actual-item",
            "polarity": "supports",
            "version": 1,
        }
        boundary._put(tx, "Evidence", ev)
        for i, kind in enumerate(("item", "evidence"), 1):
            boundary._put(
                tx,
                "ChangeEntry",
                {
                    "id": f"entry-{i}",
                    "entity_id": f"actual-{kind}",
                    "entity_type": kind,
                    "field": "row",
                    "sequence": i,
                    "commit_id": stamp.commit_id,
                    "change_set_id": stamp.change_set_id,
                },
            )

    monkeypatch.setattr(execution, "execute_prepared_in_tx", ordinary)
    head, witness, _ = current(tx)
    result = execution.complete_execution_in_tx(
        tx, None, head, witness, "complete-1", proposal, payload, trace
    )
    assert result.outcome == "done_extracted"
    assert len(tx.tables["LearnerHandoff"]) == 2
    assert tx.tables["Connector"]["connector-1"]["extracted_count"] == 1
    _, witness, _ = current(tx)
    before = deepcopy(tx.tables)
    assert (
        execution.read_completion_in_tx(tx, None, witness, "work-1", result.completion_id) == result
    )
    assert tx.tables == before
    from grove.store.learner_snapshot import validate_learner_snapshot

    validate_learner_snapshot(tx.snapshot())
    return result


def test_proposal_and_lost_commit_reply_replay_exact_event(harness):
    tx, binding = harness
    proposal, payload = ready_blank(tx, binding)
    rows = sorted(tx.tables["LearnerExecution"].values(), key=lambda r: r["ordinal"])
    old = rows[-2]
    old_state = codec.rehydrate_state(
        tx.tables["HistoryValue"][old["state_value_id"]]["value"],
        {k: v["value"] for k, v in tx.tables["HistoryValue"].items()},
    )
    head = execution.ExpectedHead(
        "work-1",
        old_state.work_version,
        old["id"],
        old["ordinal"],
        "request-1",
        old_state.request_version,
        old_state.claim_epoch,
    )
    witness = codec.decode_run_witness(
        codec.load_json(tx.tables["HistoryValue"][old["state_value_id"]]["value"])["run_witness"]
    )
    before = deepcopy(tx.tables)
    assert (
        execution.record_proposal_in_tx(
            tx, None, head, witness, "proposal-1", proposal, payload
        ).execution_id
        == rows[-1]["id"]
    )
    assert tx.tables == before
    head, witness, _ = current(tx)
    result = execution.complete_execution_in_tx(
        tx, None, head, witness, "complete-1", proposal, payload
    )
    before = deepcopy(tx.tables)
    assert (
        execution.complete_execution_in_tx(tx, None, head, witness, "complete-1", proposal, payload)
        == result
    )
    assert tx.tables == before
    with pytest.raises(ExtractionRefused):
        execution.complete_execution_in_tx(
            tx, None, replace(head, request_id="foreign"), witness, "complete-1", proposal, payload
        )


def test_result_retry_compares_wire_bytes_and_current_owner(harness, monkeypatch):
    from grove.extraction.durable_types import RecordedAttemptWitness, StoredCallResult

    tx, _ = harness
    ready_extracted(tx, monkeypatch)
    rows = sorted(tx.tables["LearnerExecution"].values(), key=lambda r: r["ordinal"])
    result_row = next(r for r in rows if r["event_id"] == "result-2")
    prior = next(r for r in rows if r["id"] == result_row["predecessor_id"])
    values = {k: v["value"] for k, v in tx.tables["HistoryValue"].items()}
    state = codec.rehydrate_state(values[prior["state_value_id"]], values)
    run = codec.decode_run_witness(codec.load_json(values[prior["state_value_id"]])["run_witness"])
    head = execution.ExpectedHead(
        "work-1",
        state.work_version,
        prior["id"],
        prior["ordinal"],
        "request-1",
        state.request_version,
        state.claim_epoch,
    )
    row = tx.tables["LearnerCall"]["call-2"]
    result = StoredCallResult(**codec.load_json(values[row["result_value_id"]]))
    stored = codec.load_json(values[row["binding_value_id"]])
    call = codec.rehydrate_event(values[result_row["event_value_id"]], values).invocation
    attempt = RecordedAttemptWitness(
        call.id,
        call.model_call_id,
        call.run_id,
        call.claim_epoch,
        call.stage,
        call.route,
        call.model,
        call.job,
        call.effort,
        "ok",
        *(stored["digests"][n] for n in codec.DIGEST_NAMES),
    )
    before = deepcopy(tx.tables)
    assert (
        execution.record_result_in_tx(tx, None, head, run, "result-2", attempt, result).execution_id
        == result_row["id"]
    )
    assert tx.tables == before
    with pytest.raises(ExtractionRefused):
        execution.record_result_in_tx(
            tx, None, head, run, "result-2", attempt, replace(result, provider_response_text="{}")
        )


def test_protected_request_source_identity_and_erased_private_value_refuse(harness):
    tx, binding = harness
    intake(tx, binding)
    tx.tables["Request"]["request-1"]["source_id"] = "foreign"
    with pytest.raises(ExtractionRefused, match="protected_request"):
        current(tx)
    tx.tables["Request"]["request-1"]["source_id"] = "source-1"
    key = tx.tables["LearnerExecution"]["execution-initial"]["state_value_id"]
    tx.tables["HistoryValue"][key]["forgotten_at"] = AT
    with pytest.raises(ExtractionRefused, match="erased_dependency"):
        current(tx)


def test_commit_rechecks_changed_owner_context_and_rolls_back(harness, monkeypatch):
    tx, binding = harness
    proposal, payload = ready_blank(tx, binding)
    head, witness, _ = current(tx)
    before = deepcopy(tx.tables)

    def owner(tx, session, purpose, value, run):
        return (
            replace(value, context=replace(value.context, history_sequence=99))
            if purpose == "commit"
            else value
        )

    monkeypatch.setattr(boundary, "_require_authority_in_tx", owner)
    with pytest.raises(ExtractionRefused):
        tx.atomic(
            lambda tx: execution.complete_execution_in_tx(
                tx, None, head, witness, "complete-1", proposal, payload
            )
        )
    assert tx.tables == before


def test_effect_ledger_preserves_link_targets_and_noop_restatements(harness, monkeypatch):
    from types import SimpleNamespace

    from grove.writer.registry import Registry
    from tests.extraction.test_durable_codec import payload_fixture

    tx, binding = harness
    _, _, raw = payload_fixture()
    ev = raw["batch"]["evidence"][0]
    tx.tables["Link"] = {
        "link-1": {"id": "link-1", "from_item": "a", "to_item": "b", "meaning": "meaning-1"}
    }
    tx.tables["Evidence"] = {
        "ev-actual": {**ev, "id": "ev-actual", "target_id": "link-1", "polarity": "supports"}
    }
    monkeypatch.setattr(
        Registry,
        "load",
        lambda tx: SimpleNamespace(resolve_meaning=lambda _: ({"id": "meaning-1"}, False)),
    )
    stamp = boundary._stamp(tx, "event-ledger", binding.run.id, binding.work.request_id)
    for operation in (
        {
            "op": "attach_evidence",
            "op_id": "attach-1",
            "target": "link-1",
            "polarity": "supports",
            "ev_id": "ev-1",
        },
        {
            "op": "create_link",
            "op_id": "restated-1",
            "from": "a",
            "to": "b",
            "meaning": "related",
            "evidence_refs": [{"ev_id": "ev-1", "polarity": "supports"}],
        },
    ):
        effects, created = execution._effect_ledger(
            tx, stamp, {"ops": [operation], "evidence": [ev]}
        )
        assert created == 0
        assert effects[0]["target_type"] == "link"
        assert effects[0]["target_id"] == "link-1"
        assert effects[0]["evidence_ids"] == ["ev-actual"]
        tx.tables["Evidence"]["ev-actual"]["polarity"] = "contradicts"
        with pytest.raises(ExtractionRefused):
            execution._effect_ledger(tx, stamp, {"ops": [operation], "evidence": [ev]})
        tx.tables["Evidence"]["ev-actual"]["polarity"] = "supports"
