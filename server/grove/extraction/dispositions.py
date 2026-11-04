"""Complete message/class accounting for the inactive legacy profile."""

from grove.store.enums import ENUMS

from .execution_codec import (
    binding_digest,
    canonical,
    data_digest,
    decode_proposal,
    load_json,
    plain,
    proposal_digest,
    refuse,
    text_digest,
    validate_record,
)
from .execution_profile import check_inventory, check_legacy_profile
from .execution_types import CompletionPlan


def check_invocation(binding, call):
    validate_record(call)
    if call.binding_digest != binding_digest(binding) or not 1 <= call.ordinal <= 5:
        refuse("invocation_binding")
    if not 1 <= len(call.run_id) <= 80 or not call.run_id.isascii() or call.claim_epoch < 1:
        refuse("invocation_claim")
    steps = {step.stage: step for step in binding.model_steps}
    step = steps.get(call.stage)
    if step is None or (call.route, call.model, call.job, call.effort) != (
        step.route,
        step.model,
        step.job,
        step.effort,
    ):
        refuse("invocation_model")
    request = load_json(call.request, step.request_limit)
    if (
        type(request) is not dict
        or set(request) != {"binding_digest", "stage", "payload"}
        or request["binding_digest"] != call.binding_digest
        or request["stage"] != call.stage
        or text_digest(call.request) != call.request_digest
    ):
        refuse("invocation_request")
    if call.state not in {"prepared", "in_flight", "validated", "failed", "unknown", "cancelled"}:
        refuse("invocation_state")
    if call.state == "validated":
        if call.model_call_id is None or call.output is None or call.output_digest is None:
            refuse("invocation_result")
        output = load_json(call.output, step.output_limit)
        _check_output(binding, call.stage, output)
        if text_digest(call.output) != call.output_digest:
            refuse("invocation_output")
    elif (
        call.output is not None or call.output_digest is not None or call.model_call_id is not None
    ):
        refuse("unvalidated_output")


def _check_output(binding, stage, output):
    if type(output) is not dict:
        refuse("model_output_shape")
    selected = tuple(ref.locator for ref in binding.inventory if ref.key in binding.selected_keys)
    if stage == "triage":
        if (
            set(output) != {"reviewed_locators", "candidates"}
            or type(output["reviewed_locators"]) is not list
            or tuple(output["reviewed_locators"]) != selected
            or type(output["candidates"]) is not list
        ):
            refuse("complete_triage_required")
        seen = set()
        for candidate in output["candidates"]:
            if (
                type(candidate) is not dict
                or set(candidate) != {"locator", "statement_class"}
                or type(candidate["locator"]) is not str
                or type(candidate["statement_class"]) is not str
                or candidate["locator"] not in selected
                or candidate["statement_class"] not in ENUMS["statement_class"]
            ):
                refuse("candidate_identity")
            pair = candidate["locator"], candidate["statement_class"]
            if pair in seen:
                refuse("duplicate_candidate")
            seen.add(pair)
    elif stage == "extraction":
        if set(output) != {"operations", "evidence", "dispositions"}:
            refuse("extraction_output_shape")
        decode_proposal(
            canonical({**output, "binding_digest": binding_digest(binding), "invocations": []})
        )
    else:
        if set(output) != {"classifications"} or type(output["classifications"]) is not list:
            refuse("adjudication_output_shape")
        seen = set()
        for item in output["classifications"]:
            if (
                type(item) is not dict
                or set(item) != {"locator", "from", "to"}
                or any(type(v) is not str for v in item.values())
                or item["locator"] not in selected
                or item["from"] not in ENUMS["statement_class"]
                or item["to"] not in ENUMS["statement_class"]
            ):
                refuse("adjudication_output_shape")
            pair = item["locator"], item["from"]
            if pair in seen:
                refuse("duplicate_adjudication")
            seen.add(pair)


def _calls(binding, calls):
    if type(calls) is not tuple or len(calls) > 5:
        refuse("invocation_count")
    identities, models, ordinals = set(), set(), set()
    for call in calls:
        check_invocation(binding, call)
        if call.id in identities or call.ordinal in ordinals:
            refuse("duplicate_invocation")
        identities.add(call.id)
        ordinals.add(call.ordinal)
        if call.model_call_id is not None:
            if call.model_call_id in models:
                refuse("duplicate_model_call")
            models.add(call.model_call_id)
    return {c.id: c for c in calls}


def account_messages(binding, dispositions, triage, invocations):
    rows = check_inventory(binding)
    if type(dispositions) is not tuple or len(dispositions) != len(rows):
        refuse("complete_dispositions_required")
    for item in dispositions:
        validate_record(item)
    if tuple(d.key for d in dispositions) != tuple(r.key for r in binding.inventory):
        refuse("disposition_keys")
    selected = tuple(r.locator for r in binding.inventory if r.key in binding.selected_keys)
    calls = _calls(binding, invocations)
    if selected:
        if triage is None:
            refuse("complete_triage_required")
        validate_record(triage)
        call = calls.get(triage.invocation_id)
        if (
            call is None
            or call.stage != "triage"
            or call.state != "validated"
            or call.request_digest != triage.request_digest
            or call.output_digest != triage.output_digest
            or triage.reviewed_locators != selected
        ):
            refuse("complete_bound_triage_required")
        if any(c.stage == "triage" and c.ordinal > call.ordinal for c in calls.values()):
            refuse("latest_triage_required")
        expected = {
            "reviewed_locators": list(triage.reviewed_locators),
            "candidates": plain(triage.candidates),
        }
        if load_json(call.output) != expected:
            refuse("triage_output_identity")
    elif triage is not None or invocations:
        refuse("blank_inventory_needs_no_call")
    for row, ref, disposition in zip(rows, binding.inventory, dispositions, strict=True):
        if disposition.locator != ref.locator:
            refuse("disposition_locator")
        if not row["text"].strip():
            if disposition.state != "ignored_blank" or disposition.classes or disposition.reason:
                refuse("blank_disposition")
        elif disposition.state not in {"reviewed_empty", "extracted"} or disposition.reason:
            refuse("incomplete_disposition")
    return calls


def account_candidate_classes(binding, proposal, triage, calls):
    candidates = () if triage is None else triage.candidates
    if len(candidates) > 1024:
        refuse("candidate_bounds")
    selected = {ref.locator for ref in binding.inventory if ref.key in binding.selected_keys}
    expected = {(c.locator, c.statement_class) for c in candidates}
    if len(expected) != len(candidates) or any(
        c.locator not in selected or c.statement_class not in ENUMS["statement_class"]
        for c in candidates
    ):
        refuse("candidate_identity")
    ops = {op.id: op for op in proposal.operations}
    evidence = {ev.id: ev for ev in proposal.evidence}
    if len(ops) != len(proposal.operations) or len(evidence) != len(proposal.evidence):
        refuse("duplicate_effect")
    if any(
        op.op not in binding.writer_ops
        or not op.evidence_ids
        or len(set(op.evidence_ids)) != len(op.evidence_ids)
        or any(ev not in evidence for ev in op.evidence_ids)
        for op in ops.values()
    ):
        refuse("writer_operation")
    covered, used_ops, used_evidence = set(), set(), set()
    speakers = {r.locator: r.speaker_kind for r in binding.inventory}
    for disposition in proposal.dispositions:
        if (disposition.state == "extracted") != bool(disposition.classes):
            refuse("class_disposition")
        for outcome in disposition.classes:
            pair = disposition.locator, outcome.statement_class
            if pair in covered or pair not in expected:
                refuse("candidate_coverage")
            covered.add(pair)
            if (
                outcome.outcome != "extracted"
                or outcome.final_class not in ENUMS["statement_class"]
                or not outcome.operation_ids
                or not outcome.evidence_ids
                or len(set(outcome.operation_ids)) != len(outcome.operation_ids)
                or len(set(outcome.evidence_ids)) != len(outcome.evidence_ids)
            ):
                refuse("incomplete_class")
            if outcome.final_class == "decision" and speakers[disposition.locator] in {
                "ai_host",
                "automated",
            }:
                refuse("decider_not_speaker")
            if outcome.final_class != outcome.statement_class:
                call = calls.get(outcome.adjudication_id)
                if call is None or call.stage != "adjudication" or call.state != "validated":
                    refuse("class_adjudication_required")
                result = load_json(call.output)
                decision = {
                    "locator": disposition.locator,
                    "from": outcome.statement_class,
                    "to": outcome.final_class,
                }
                if (
                    type(result) is not dict
                    or set(result) != {"classifications"}
                    or decision not in result["classifications"]
                ):
                    refuse("class_adjudication_identity")
            for ev_id in outcome.evidence_ids:
                ev = evidence.get(ev_id)
                if ev is None or (ev.locator, ev.statement_class) != (
                    disposition.locator,
                    outcome.final_class,
                ):
                    refuse("class_evidence")
            for op_id in outcome.operation_ids:
                op = ops.get(op_id)
                if op is None or not set(outcome.evidence_ids).intersection(op.evidence_ids):
                    refuse("class_operation")
            if any(
                not any(ev_id in ops[op_id].evidence_ids for op_id in outcome.operation_ids)
                for ev_id in outcome.evidence_ids
            ):
                refuse("unapplied_class_evidence")
            used_ops.update(outcome.operation_ids)
            used_evidence.update(outcome.evidence_ids)
    if covered != expected or used_ops != set(ops) or used_evidence != set(evidence):
        refuse("candidate_coverage")


def plan_completion(binding, proposal, triage=None):
    check_legacy_profile(binding)
    proposal_digest(proposal)
    if proposal.binding_digest != binding_digest(binding):
        refuse("proposal_binding")
    calls = account_messages(binding, proposal.dispositions, triage, proposal.invocations)
    account_candidate_classes(binding, proposal, triage, calls)
    if proposal.operations:
        expected = {
            key: plain(getattr(proposal, key)) for key in ("operations", "evidence", "dispositions")
        }
        extraction = [c for c in calls.values() if c.stage == "extraction"]
        latest = max(extraction, key=lambda c: c.ordinal) if extraction else None
        if latest is None or latest.state != "validated" or load_json(latest.output) != expected:
            refuse("extraction_output_identity")
    elif any(c.stage != "triage" for c in calls.values()):
        refuse("empty_after_extraction")
    blank = sum(d.state == "ignored_blank" for d in proposal.dispositions)
    empty = sum(d.state == "reviewed_empty" for d in proposal.dispositions)
    extracted = sum(d.state == "extracted" for d in proposal.dispositions)
    return CompletionPlan(
        binding_digest(binding),
        proposal_digest(proposal),
        data_digest("grove.learner.dispositions.v1", plain(proposal.dispositions)),
        "done_extracted" if extracted else "done_empty",
        len(proposal.dispositions),
        blank,
        empty,
        extracted,
        tuple(op.id for op in proposal.operations),
    )
