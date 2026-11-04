"""Pure storage and wire controls. No test here establishes provider authority."""

import json
from dataclasses import replace

import pytest

from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.contracts import ExtractionRefused
from grove.extraction.durable_types import (
    RecordedAttemptWitness,
    RunWitness,
    StoredCallRequest,
    StoredCallResult,
)
from grove.extraction.execution_types import EvidenceRef, ExecutionState, OperationRef, Proposal
from tests.extraction.test_dispositions import invocation
from tests.writer.test_learner_execution import make_binding


def payload_fixture():
    binding = make_binding("I chose Cedar.")
    observation = {
        "ev_id": "ev-1",
        "source_id": binding.source.id,
        "episode_id": binding.episode.id,
        "message_key": "msg1",
        "locator": "key:msg1",
        "source_time": binding.inventory[0].source_time,
        "speaker_kind": "person",
        "speaker_item": "person-1",
        "modality": "text",
        "derivation": "observed",
        "statement_class": "decision",
    }
    op = {
        "op": "create_item",
        "op_id": "op-1",
        "kind": "task",
        "title": "Cedar",
        "became_true": {"precision": "unknown"},
        "evidence_refs": [{"ev_id": "ev-1", "polarity": "supports"}],
        "reason": "Source decision",
    }
    raw = {
        "schema": "grove.learner.payload.v1",
        "writer_revision": binding.source_revision,
        "binding_digest": legacy.binding_digest(binding),
        "batch": {"ops": [op], "evidence": [observation]},
        "operation_map": [{"descriptor_id": "op-1", "operation_index": 0}],
    }
    digest = legacy.data_digest(
        "grove.learner.operation.v1", {"operation": op, "evidence": [observation]}
    )
    proposal = Proposal(
        legacy.binding_digest(binding),
        (OperationRef("op-1", "create_item", digest, ("ev-1",)),),
        (EvidenceRef("ev-1", "key:msg1", "decision"),),
        (),
        (),
    )
    return binding, proposal, raw


def test_descriptor_requires_complete_payload():
    binding, proposal, raw = payload_fixture()
    assert (
        codec.check_descriptor_payloads(
            binding, proposal, codec.decode_payload(codec.canonical(raw))
        )
        == raw["batch"]
    )
    del raw["batch"]["ops"][0]["title"]
    with pytest.raises(ExtractionRefused):
        codec.decode_payload(codec.canonical(raw))


@pytest.mark.parametrize("change", ["title", "evidence", "extra", "order", "descriptor"])
def test_payload_map_is_bijective_and_binds_observations(change):
    binding, proposal, raw = payload_fixture()
    if change == "title":
        raw["batch"]["ops"][0]["title"] = "Lumen"
    elif change == "evidence":
        raw["batch"]["evidence"][0]["speaker_item"] = "other"
    elif change == "extra":
        raw["batch"]["evidence"].append({**raw["batch"]["evidence"][0], "ev_id": "unused"})
    elif change == "order":
        raw["operation_map"][0]["operation_index"] = 1
    else:
        raw["operation_map"][0]["descriptor_id"] = "foreign"
    with pytest.raises(ExtractionRefused):
        codec.check_descriptor_payloads(
            binding, proposal, codec.decode_payload(codec.canonical(raw))
        )


def test_split_attach_operations_keep_distinct_final_ids():
    binding, _, raw = payload_fixture()
    ev = raw["batch"]["evidence"][0]
    raw["batch"]["evidence"] = [ev, {**ev, "ev_id": "ev-2"}]
    raw["batch"]["ops"] = [
        {
            "op": "attach_evidence",
            "op_id": f"attach:{i}",
            "target": "target-1",
            "polarity": "supports",
            "ev_id": f"ev-{i}",
        }
        for i in (1, 2)
    ]
    raw["operation_map"] = [
        {"descriptor_id": f"attach:{i}", "operation_index": i - 1} for i in (1, 2)
    ]
    result = codec.decode_payload(codec.canonical(raw))
    assert tuple(r.descriptor_id for r in result.operation_map) == ("attach:1", "attach:2")
    raw["operation_map"][1]["descriptor_id"] = "attach:1"
    with pytest.raises(ExtractionRefused):
        codec.decode_payload(codec.canonical(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "[" * 17 + "0" + "]" * 17,
        "[" + ",".join("0" for _ in range(2049)) + "]",
        '"' + "x" * 1048576 + '"',
        "[" + ",".join("[" + ",".join("0" for _ in range(1024)) + "]" for _ in range(17)) + "]",
    ],
)
def test_preflight_rejects_deep_wide_and_oversized_json_before_allocation(raw, monkeypatch):
    monkeypatch.setattr(json, "loads", lambda *a, **k: pytest.fail("allocated before preflight"))
    with pytest.raises(ExtractionRefused):
        codec.load_json(raw)


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":2}', '{"x":1.1}', '{"x":NaN}', '{"x":"\\ud800"}', b"\xff"]
)
def test_private_json_rejects_ambiguous_values(raw):
    with pytest.raises(ExtractionRefused):
        codec.load_json(raw)


def test_storage_wrappers_rehydrate_exact_pr41_bytes():
    binding = make_binding()
    state = ExecutionState(binding)
    witness = RunWitness(binding.run.id, 7, 1, binding.run.pass_id, binding.run.input_ids)
    raw = codec.encode_stored_state(state, witness, "binding-1")
    values = {"binding-1": legacy.encode_binding(binding)}
    assert codec.rehydrate_state(raw, values) == state
    assert codec.canonical(codec.plain(codec.rehydrate_state(raw, values))) == codec.canonical(
        codec.plain(state)
    )
    with pytest.raises(ExtractionRefused):
        codec.rehydrate_state(raw, {})


def test_run_witness_separates_actual_and_protocol_versions():
    binding = make_binding()
    witness = RunWitness(binding.run.id, 7, 1, binding.run.pass_id, binding.run.input_ids)
    codec.check_run_witness(witness, binding.run)
    with pytest.raises(ExtractionRefused):
        codec.check_run_witness(replace(witness, protocol_run_version=7), binding.run)
    with pytest.raises(ExtractionRefused):
        codec.decode_run_witness(replace(witness, actual_row_version=True))


def triage_wire():
    binding = make_binding("I chose Cedar.")
    raw = {
        "schema": "grove.triage.v1",
        "has_candidates": False,
        "reviewed_locators": ["key:msg1"],
        "candidates": [],
        "redactions": [],
    }
    output = {"reviewed_locators": ["key:msg1"], "candidates": []}
    call = invocation(binding, output=output)
    packet = {"source_data": [codec.plain(m) for m in codec._source_batch(binding).messages]}
    normalized = codec.canonical(
        {"binding_digest": legacy.binding_digest(binding), "stage": "triage", "payload": packet}
    ).decode()
    call = replace(call, request=normalized, request_digest=legacy.text_digest(normalized))
    request = StoredCallRequest(
        call.request,
        codec.canonical(
            {"messages": [{"role": "user", "content": codec.canonical(packet).decode()}]}
        ).decode(),
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
        call.output,
        binding.source_revision,
    )
    texts = (
        request.provider_request_body,
        result.provider_response_text,
        result.selected_output_text,
        request.normalized_request,
        result.normalized_output,
    )
    digests = [
        legacy.data_digest("grove.learner." + name.replace("_", "-") + ".v1", text)
        for name, text in zip(codec.DIGEST_NAMES, texts, strict=True)
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
    return binding, call, request, result, attempt


def test_wire_and_normalized_results_require_exact_translation():
    args = triage_wire()
    assert len(codec.check_transport_binding(*args)) == 5
    binding, call, request, result, attempt = args
    with pytest.raises(ExtractionRefused):
        codec.check_transport_binding(
            binding, call, request, replace(result, selected_output_text="{}"), attempt
        )
    with pytest.raises(ExtractionRefused):
        codec.check_transport_binding(
            binding, call, request, result, replace(attempt, model_call_id="foreign")
        )


def test_redaction_or_unsupported_branch_cannot_disappear_in_normalization():
    binding, call, request, result, attempt = triage_wire()
    raw = codec.load_json(result.selected_output_text)
    raw["redactions"] = [{"locator": "key:msg1", "start": 0, "end": 1, "category": "private"}]
    selected = codec.canonical(raw).decode()
    result = replace(
        result,
        selected_output_text=selected,
        provider_response_text=codec.canonical(
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": selected}]}
                ],
            }
        ).decode(),
    )
    with pytest.raises(ExtractionRefused):
        codec.check_transport_binding(binding, call, request, result, attempt)


@pytest.mark.parametrize(
    "wire",
    [
        {"status": "completed", "output": None},
        {"choices": [None]},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "{}"},
                        {"type": "refusal", "refusal": "no"},
                    ],
                }
            ],
        },
    ],
)
def test_malformed_or_partially_refused_wire_never_selects_success(wire):
    with pytest.raises(ExtractionRefused):
        codec._selected_output(codec.canonical(wire))


def test_provider_request_cannot_substitute_source_bytes():
    binding, call, request, result, attempt = triage_wire()
    packet = codec.load_json(call.request)
    packet["payload"]["source_data"][0]["text"] = "A different decision."
    normalized = codec.canonical(packet).decode()
    changed = replace(call, request=normalized, request_digest=legacy.text_digest(normalized))
    request = StoredCallRequest(
        normalized,
        codec.canonical(
            {"messages": [{"role": "user", "content": codec.canonical(packet["payload"]).decode()}]}
        ).decode(),
    )
    with pytest.raises(ExtractionRefused, match="source_binding"):
        codec.check_transport_binding(binding, changed, request, result, attempt)
