from dataclasses import replace

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.dispositions import check_invocation, plan_completion
from grove.extraction.execution_codec import binding_digest, plain
from grove.extraction.execution_types import (
    CandidateClass,
    ClassOutcome,
    Disposition,
    EvidenceRef,
    Invocation,
    OperationRef,
    Proposal,
    TriageTrace,
)
from tests.extraction.test_execution_codec import binding, digest, document, packed


def invocation(value, stage="triage", output=None, ordinal=1, epoch=1):
    step = next(s for s in value.model_steps if s.stage == stage)
    request = packed(
        {
            "binding_digest": binding_digest(value),
            "stage": stage,
            "payload": {"source_digest": value.source.content_digest},
        }
    )
    return Invocation(
        f"call-{ordinal}",
        binding_digest(value),
        stage,
        ordinal,
        digest(request),
        step.route,
        step.model,
        step.job,
        step.effort,
        "prepared" if output is None else "validated",
        None if output is None else f"model-{ordinal}",
        None if output is None else digest(packed(output)),
        request,
        None if output is None else packed(output),
        value.run.id,
        epoch,
    )


def proposal(value=None, *, classes=(), empty=True):
    value = value or binding()
    candidates = tuple(CandidateClass("key:msg1", cls) for cls in classes)
    triage_output = {"reviewed_locators": ["key:msg1"], "candidates": plain(candidates)}
    call = invocation(value, output=triage_output)
    trace = TriageTrace(call.id, call.request_digest, call.output_digest, ("key:msg1",), candidates)
    if empty:
        return Proposal(
            binding_digest(value),
            (),
            (),
            (Disposition("msg1", "key:msg1", "reviewed_empty", None, ()),),
            (call,),
        ), trace
    effects = tuple(
        OperationRef(f"op-{i}", "create_item", digest(f"op-{i}"), (f"ev-{i}",))
        for i, _ in enumerate(classes)
    )
    evidence = tuple(EvidenceRef(f"ev-{i}", "key:msg1", cls) for i, cls in enumerate(classes))
    outcomes = tuple(
        ClassOutcome(cls, cls, "extracted", (f"op-{i}",), (f"ev-{i}",), None)
        for i, cls in enumerate(classes)
    )
    dispositions = (Disposition("msg1", "key:msg1", "extracted", None, outcomes),)
    output = {
        "operations": plain(effects),
        "evidence": plain(evidence),
        "dispositions": plain(dispositions),
    }
    extract = invocation(value, "extraction", output, 2)
    return Proposal(binding_digest(value), effects, evidence, dispositions, (call, extract)), trace


def test_blank_messages_account_without_skipping_nonblank_work():
    from grove.extraction.execution_codec import decode_binding

    value = decode_binding(packed(document(" \n")))
    blank = Proposal(
        binding_digest(value),
        (),
        (),
        (Disposition("msg1", "key:msg1", "ignored_blank", None, ()),),
        (),
    )
    assert plan_completion(value, blank).blank_messages == 1
    value = binding()
    with pytest.raises(ExtractionRefused):
        plan_completion(value, replace(blank, binding_digest=binding_digest(value)))


def test_every_retained_key_has_one_disposition():
    value = binding()
    result, trace = proposal(value)
    for items in ((), result.dispositions * 2, (replace(result.dispositions[0], key="other"),)):
        with pytest.raises(ExtractionRefused):
            plan_completion(value, replace(result, dispositions=items), trace)


def test_every_candidate_class_has_a_final_outcome():
    value = binding()
    result, trace = proposal(value, classes=("decision", "report_state"), empty=False)
    assert plan_completion(value, result, trace).extracted_messages == 1
    dropped = replace(result.dispositions[0], classes=result.dispositions[0].classes[:1])
    with pytest.raises(ExtractionRefused, match="candidate_coverage"):
        plan_completion(value, replace(result, dispositions=(dropped,)), trace)


@pytest.mark.parametrize(
    "state",
    [
        "held_input",
        "held_fact",
        "redaction_required",
        "unresolved",
        "rejected",
        "revoked",
        "pending",
        "selected",
    ],
)
def test_held_redaction_unresolved_and_rejected_work_blocks_v1(state):
    value = binding()
    result, trace = proposal(value)
    with pytest.raises(ExtractionRefused):
        plan_completion(
            value,
            replace(result, dispositions=(replace(result.dispositions[0], state=state),)),
            trace,
        )


def test_empty_requires_complete_bound_triage():
    value = binding()
    result, trace = proposal(value)
    assert plan_completion(value, result, trace).outcome == "done_empty"
    for wrong in (
        None,
        replace(trace, reviewed_locators=()),
        replace(trace, output_digest=digest("another")),
        replace(trace, invocation_id="unrelated"),
    ):
        with pytest.raises(ExtractionRefused):
            plan_completion(value, result, wrong)


def test_call_identity_binds_request_output_and_stage():
    value = binding()
    result, _ = proposal(value)
    call = result.invocations[0]
    for wrong in (
        replace(call, request_digest=digest("wrong")),
        replace(call, output_digest=digest("wrong")),
        replace(call, stage="extraction"),
        replace(call, model_call_id=None),
        replace(call, binding_digest=digest("other")),
    ):
        with pytest.raises(ExtractionRefused):
            check_invocation(value, wrong)


def test_unknown_model_outcome_never_becomes_empty():
    value = binding()
    result, trace = proposal(value)
    unknown = replace(
        result.invocations[0],
        id="call-2",
        ordinal=2,
        state="unknown",
        model_call_id=None,
        output_digest=None,
        output=None,
    )
    with pytest.raises(ExtractionRefused):
        plan_completion(value, replace(result, invocations=(unknown,)), trace)
    with pytest.raises(ExtractionRefused):
        plan_completion(value, replace(result, invocations=(*result.invocations, unknown)), trace)


def test_class_adjudication_and_operation_mapping_cannot_be_fabricated():
    value = binding()
    result, trace = proposal(value, classes=("decision",), empty=False)
    outcome = replace(result.dispositions[0].classes[0], final_class="report_state")
    bad = replace(result, dispositions=(replace(result.dispositions[0], classes=(outcome,)),))
    with pytest.raises(ExtractionRefused):
        plan_completion(value, bad, trace)
    with pytest.raises(ExtractionRefused):
        plan_completion(
            value, replace(result, operations=(replace(result.operations[0], op="forget"),)), trace
        )


def test_complete_mixed_blank_and_nonblank_inventory():
    import json

    from grove.extraction.execution_codec import decode_binding

    data = document()
    rows = json.loads(data["source"]["content"])
    rows.append({**rows[0], "key": "blank-1", "text": " \n"})
    speakers = json.loads(data["source"]["speakers"])
    speakers["blank-1"] = {**speakers["msg1"], "locator": "key:blank-1"}
    data["source"].update(
        content=packed(rows),
        content_digest=digest(packed(rows)),
        speakers=packed(speakers),
        metadata_digest=digest(packed(speakers)),
    )
    data["inventory"].append(
        {
            **data["inventory"][0],
            "key": "blank-1",
            "locator": "key:blank-1",
            "text_digest": digest(" \n"),
        }
    )
    data["episode"]["last_key"] = "blank-1"
    value = decode_binding(packed(data))
    result, trace = proposal(value)
    result = replace(
        result,
        dispositions=(
            *result.dispositions,
            Disposition("blank-1", "key:blank-1", "ignored_blank", None, ()),
        ),
    )
    completion = plan_completion(value, result, trace)
    assert (completion.messages, completion.blank_messages, completion.empty_messages) == (2, 1, 1)
    with pytest.raises(ExtractionRefused):
        plan_completion(value, replace(result, dispositions=result.dispositions[1:]), trace)


def test_lost_later_extraction_cannot_reuse_old_result():
    value = binding()
    result, trace = proposal(value, classes=("decision",), empty=False)
    unknown = replace(
        result.invocations[1],
        id="call-3",
        ordinal=3,
        state="unknown",
        model_call_id=None,
        output=None,
        output_digest=None,
    )
    with pytest.raises(ExtractionRefused, match="extraction_output_identity"):
        plan_completion(value, replace(result, invocations=(*result.invocations, unknown)), trace)


@pytest.mark.parametrize(
    "output",
    [
        [],
        True,
        {"candidates": []},
        {"reviewed_locators": [], "candidates": []},
        {"reviewed_locators": ["key:msg1"], "candidates": [None]},
        {"reviewed_locators": ["key:msg1"], "candidates": [], "redactions": []},
    ],
)
def test_invalid_model_result_refuses_before_next_stage(output):
    with pytest.raises(ExtractionRefused):
        check_invocation(binding(), invocation(binding(), output=output))
