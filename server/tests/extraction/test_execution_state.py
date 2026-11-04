from dataclasses import replace

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.execution_codec import plain
from grove.extraction.execution_profile import check_dependencies
from grove.extraction.execution_state import advance, plan_counter_delta, plan_replay, plan_retry
from grove.extraction.execution_types import ExecutionEvent, ExecutionState
from tests.extraction.test_dispositions import invocation, proposal
from tests.extraction.test_execution_codec import binding, digest


def current(state, **changes):
    value = replace(
        state.binding,
        run=replace(state.binding.run, version=state.run_version),
        work=replace(
            state.binding.work,
            version=state.work_version,
            request_version=state.request_version,
            claim_epoch=state.claim_epoch,
            claimed_by=state.claimed_by,
            transport_attempts=state.transport_attempts,
            regrounds=state.regrounds,
            writer_repairs=state.writer_repairs,
        ),
    )
    return replace(value, **changes)


def event(state, kind, **changes):
    args = dict(
        kind=kind,
        expected_revision=state.revision,
        claim_epoch=state.claim_epoch,
        now=max(10, state.last_event_at),
        current=current(state),
    )
    args.update(changes)
    return ExecutionEvent(**args)


def perform_call(state, call):
    intent = replace(
        call,
        state="prepared",
        model_call_id=None,
        output_digest=None,
        output=None,
        claim_epoch=state.claim_epoch,
    )
    state = advance(state, event(state, "prepare", invocation=intent))
    state = advance(state, event(state, "send"))
    return advance(
        state, event(state, "result", invocation=replace(call, claim_epoch=state.claim_epoch))
    )


def ready(*, empty=True):
    value = binding()
    result, trace = proposal(value, classes=() if empty else ("decision",), empty=empty)
    state = advance(ExecutionState(value), event(ExecutionState(value), "claim"))
    for call in result.invocations:
        state = perform_call(state, call)
    state = advance(state, event(state, "propose", proposal=result, triage=trace))
    return state, trace


def test_protocol_versions_advance_without_refreshing_source_dependencies():
    state = ExecutionState(binding())
    claimed = advance(state, event(state, "claim"))
    assert claimed.work_version == state.work_version + 1
    assert claimed.binding == state.binding
    assert claimed.predecessor is not None
    check_dependencies(claimed.binding, current(claimed))
    wrong = current(claimed, source=replace(claimed.binding.source, version=2))
    with pytest.raises(ExtractionRefused):
        advance(
            claimed,
            event(claimed, "prepare", current=wrong, invocation=invocation(claimed.binding)),
        )


def test_ended_run_or_old_claim_epoch_has_no_commit_action():
    state, trace = ready()
    for changes in (
        {"claim_epoch": state.claim_epoch - 1},
        {"current": current(state, run=replace(current(state).run, outcome="ended"))},
        {"current": current(state, work=replace(current(state).work, claim_epoch=9))},
        {"current": current(state, run=replace(current(state).run, pass_id="foreign-pass"))},
        {
            "current": current(
                state, run=replace(current(state).run, input_ids=("request-1", "foreign-request"))
            )
        },
    ):
        with pytest.raises(ExtractionRefused):
            advance(
                state, event(state, "commit", triage=trace, completion_id="completion-1", **changes)
            )
    assert state.completion is None


def test_stale_source_invalidates_plan_and_limits_reground():
    state, _ = ready()
    changed = current(state, source=replace(state.binding.source, version=2))
    pending = advance(state, event(state, "stale", current=changed))
    assert pending.proposal is None and pending.regrounds == 1 and pending.rebind_required == 1
    with pytest.raises(ExtractionRefused):
        advance(pending, event(pending, "claim"))
    rebound = advance(
        pending, event(pending, "rebind", current=current(pending, source=changed.source))
    )
    running = advance(rebound, event(rebound, "claim"))
    blocked = advance(running, event(running, "stale", current=current(running)))
    assert blocked.status == "blocked" and blocked.reason == "reground_exhausted"


def test_repair_slot_is_spent_once_and_survives_requeue():
    state, trace = ready(empty=False)
    declined = advance(state, event(state, "decline", reason="near_duplicate"))
    repair = advance(declined, event(declined, "repair"))
    assert repair.writer_repairs == 1
    dead = current(repair, run=replace(current(repair).run, outcome="killed"))
    pending = advance(repair, event(repair, "recover", current=dead))
    claimed = advance(pending, event(pending, "claim"))
    assert claimed.writer_repairs == 1
    with pytest.raises(ExtractionRefused):
        advance(claimed, event(claimed, "repair"))
    assert trace is not None


def test_identical_decline_replays_without_spend():
    state, trace = ready(empty=False)
    declined = advance(state, event(state, "decline"))
    repeated = advance(declined, event(declined, "propose", proposal=state.proposal, triage=trace))
    assert repeated is declined
    assert repeated.writer_repairs == 0


def completed():
    state, trace = ready()
    return advance(state, event(state, "commit", triage=trace, completion_id="completion-1"))


def test_saved_replay_uses_completion_not_old_target_versions():
    from grove.extraction.execution_types import Membership, VersionRef

    state = completed()
    ctx = state.binding.context
    groups = list(ctx.memberships)
    groups[1] = Membership("target", ("target-1",), digest('["target-1"]'))
    now = current(
        state,
        context=replace(
            ctx,
            dependencies=(VersionRef("target", "target-1", 10),),
            memberships=tuple(groups),
            read_generation=99,
        ),
    )
    assert plan_replay(state, now, 20, "completion-1") is state.completion


def test_saved_replay_still_requires_current_source_and_privacy_checks():
    state = completed()
    for now in (
        current(state, source=replace(state.binding.source, version=2)),
        current(state, principal=replace(state.binding.principal, privacy_revision=2)),
        current(state, producer=replace(state.binding.producer, provider="whatsapp")),
        current(state, run=replace(current(state).run, outcome="ended")),
    ):
        with pytest.raises(ExtractionRefused):
            plan_replay(state, now, 20, "completion-1")
    with pytest.raises(ExtractionRefused):
        advance(state, event(state, "provider_failed"))


def test_counter_delta_is_zero_for_duplicate_completion():
    state = completed()
    result = plan_counter_delta(
        None, state.completion, "completion-1", ("completion-1",), ("done_empty",), 0
    )
    assert (result.held_nothing, result.extracted, result.next_cursor) == (0, 0, 0)
    result = plan_counter_delta(None, state.completion, "completion-1", (), ("done_empty",), 0)
    assert (result.held_nothing, result.extracted, result.next_cursor) == (1, 0, 1)


def test_failed_gap_blocks_source_cursor():
    state = completed()
    result = plan_counter_delta(
        None, state.completion, "completion-1", (), ("done_empty", "failed", "done_empty"), 0
    )
    assert result.next_cursor == 1


def test_transport_attempts_unknown_and_stage_budget_survive_recovery():
    state = ExecutionState(binding())
    state = advance(state, event(state, "claim"))
    for ordinal in (1, 2):
        state = advance(
            state,
            event(
                state,
                "prepare",
                invocation=invocation(state.binding, ordinal=ordinal, epoch=state.claim_epoch),
            ),
        )
        state = advance(state, event(state, "send"))
        state = advance(state, event(state, "unknown"))
        assert state.invocations[-1].state == "unknown"
        state = advance(state, event(state, "claim", now=state.retry_at))
    assert state.transport_attempts == 2
    with pytest.raises(ExtractionRefused, match="stage_retry_exhausted"):
        advance(
            state,
            event(
                state,
                "prepare",
                invocation=invocation(state.binding, ordinal=3, epoch=state.claim_epoch),
            ),
        )
    assert [plan_retry(i, 1, 1000) for i in range(1, 6)] == [31, 61, 121, 241, None]


def test_simulated_crash_preserves_unknown_intent_and_exact_completion():
    state = ExecutionState(binding())
    state = advance(state, event(state, "claim"))
    state = advance(state, event(state, "prepare", invocation=invocation(state.binding)))
    dead = current(state, run=replace(current(state).run, outcome="killed"))
    restored = advance(state, event(state, "recover", current=dead))
    assert restored.invocations[0].state == "unknown"
    assert restored.transport_attempts == 1 and restored.completion is None
    saved = completed()
    assert plan_replay(saved, current(saved), 20, saved.completion_id) == saved.completion


def test_result_cannot_replace_prepared_request_or_model_call():
    state = ExecutionState(binding())
    state = advance(state, event(state, "claim"))
    result, _ = proposal(state.binding)
    state = advance(state, event(state, "prepare", invocation=invocation(state.binding)))
    state = advance(state, event(state, "send"))
    for wrong in (
        replace(result.invocations[0], id="foreign-call"),
        replace(result.invocations[0], claim_epoch=0),
    ):
        with pytest.raises(ExtractionRefused):
            advance(state, event(state, "result", invocation=wrong))


def test_second_semantic_decline_is_terminal_after_one_real_repair():

    state, trace = ready(empty=False)
    original = state.proposal
    state = advance(state, event(state, "decline"))
    state = advance(state, event(state, "repair"))
    operations = (replace(original.operations[0], payload_digest=digest("repaired-operation")),)
    output = {
        "operations": plain(operations),
        "evidence": plain(original.evidence),
        "dispositions": plain(original.dispositions),
    }
    call = invocation(state.binding, "extraction", output, 3, state.claim_epoch)
    state = perform_call(state, call)
    repaired = replace(original, operations=operations, invocations=state.invocations)
    state = advance(state, event(state, "propose", proposal=repaired, triage=trace))
    state = advance(state, event(state, "decline"))
    assert state.status == "failed" and state.writer_repairs == 1
    assert len(state.declined_digests) == 2
    assert state.completion is None


def test_five_total_transport_attempts_bound_all_stages():
    result, _ = proposal(classes=("decision",), empty=False)
    state = ExecutionState(binding())
    state = advance(state, event(state, "claim"))
    state = perform_call(state, result.invocations[0])
    for ordinal in range(2, 6):
        call = invocation(state.binding, "extraction", ordinal=ordinal, epoch=state.claim_epoch)
        state = advance(state, event(state, "prepare", invocation=call))
        state = advance(state, event(state, "send"))
        state = advance(state, event(state, "provider_failed"))
        if ordinal < 5:
            state = advance(state, event(state, "claim", now=state.retry_at))
    assert state.status == "failed" and state.transport_attempts == 5
    with pytest.raises(ExtractionRefused):
        advance(state, event(state, "claim"))


def test_stale_failure_cannot_reverse_a_newer_claim_or_extend_deadline():
    state, _ = ready()
    with pytest.raises(ExtractionRefused, match="protocol_version_changed"):
        advance(
            state,
            event(
                state,
                "stale",
                current=current(state, work=replace(current(state).work, version=99)),
            ),
        )
    with pytest.raises(ExtractionRefused, match="run_not_live"):
        advance(
            state,
            event(
                state,
                "block",
                current=current(state, run=replace(current(state).run, deadline=2000)),
            ),
        )
    with pytest.raises(ExtractionRefused):
        advance(state, event(state, "block", now=state.last_event_at - 1))


def test_saved_completion_rejects_tampering_and_allows_new_recovery_claim():
    state = completed()
    corrupt = replace(state, completion=replace(state.completion, empty_messages=99))
    with pytest.raises(ExtractionRefused, match="completion_identity"):
        plan_replay(corrupt, current(corrupt), 20, corrupt.completion_id)
    fresh = current(
        state,
        run=replace(current(state).run, id="replacement-run"),
        work=replace(
            current(state).work, claimed_by="replacement-run", claim_epoch=state.claim_epoch + 1
        ),
    )
    assert plan_replay(state, fresh, 20, state.completion_id) == state.completion


def test_counter_promotion_never_erases_historical_extraction():
    state, trace = ready(empty=False)
    state = advance(state, event(state, "commit", triage=trace, completion_id="extracted-1"))
    delta = plan_counter_delta("done_empty", state.completion, "extracted-1", (), (), 0)
    assert (delta.held_nothing, delta.extracted) == (-1, 1)
    empty = completed()
    delta = plan_counter_delta("done_extracted", empty.completion, "empty-later", (), (), 0)
    assert (delta.held_nothing, delta.extracted) == (0, 0)
