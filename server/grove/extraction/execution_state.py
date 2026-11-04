"""Pure transition plans. Callers cannot execute them through this component."""

from dataclasses import replace

from .dispositions import check_invocation, plan_completion
from .execution_codec import (
    MAX_COUNTER,
    data_digest,
    load_json,
    plain,
    proposal_digest,
    refuse,
    validate_record,
)
from .execution_profile import check_dependencies, check_legacy_profile
from .execution_types import CounterDelta

_TERMINAL = {"done_empty", "done_extracted", "blocked", "failed", "revoked"}
_EVENTS = {
    "claim",
    "prepare",
    "send",
    "result",
    "provider_failed",
    "unknown",
    "propose",
    "decline",
    "repair",
    "stale",
    "rebind",
    "recover",
    "revoke",
    "block",
    "budget",
    "commit",
}


def _next(state, **changes):
    for value in (state.revision, state.request_version, state.work_version, state.run_version):
        if value >= MAX_COUNTER:
            refuse("version_overflow")
    return replace(
        state,
        revision=state.revision + 1,
        predecessor=data_digest("grove.learner.state.v1", plain(state)),
        request_version=state.request_version + 1,
        work_version=state.work_version + 1,
        run_version=state.run_version + 1,
        **changes,
    )


def _live(state, current, now, *, claim=False, replay=False, semantic=True):
    if current is None:
        refuse("current_snapshot_required")
    if semantic:
        check_dependencies(state.binding, current, replay=replay)
    else:
        check_legacy_profile(current)
        if (
            current.world_id != state.binding.world_id
            or current.work.id != state.binding.work.id
            or current.work.request_id != state.binding.work.request_id
        ):
            refuse("work_identity_changed")
    if (
        current.run.outcome != "running"
        or now >= current.run.deadline
        or now >= current.work.lease_deadline
        or current.run.token_cap == 0
        or current.run.deadline > state.binding.run.deadline
        or current.run.token_cap > state.binding.run.token_cap
    ):
        refuse("run_not_live")
    if (
        current.work.version != state.work_version
        or current.work.request_version != state.request_version
        or current.work.claim_epoch != state.claim_epoch
        or current.run.version != state.run_version
    ):
        refuse("protocol_version_changed")
    if not claim and (
        state.claimed_by is None
        or current.run.id != state.claimed_by
        or current.work.claimed_by != state.claimed_by
        or current.run.pass_id != state.active_pass_id
        or current.run.input_ids != state.active_input_ids
    ):
        refuse("claim_not_current")


def plan_retry(attempts, now, deadline):
    if any(type(v) is not int or not 0 <= v <= MAX_COUNTER for v in (attempts, now, deadline)):
        refuse("retry_integer")
    if not 1 <= attempts < 5:
        return None
    due = now + (30, 60, 120, 240)[attempts - 1]
    return due if due < deadline and due <= MAX_COUNTER else None


def _update_call(state, call):
    return tuple(call if old.id == call.id else old for old in state.invocations)


def advance(state, event):
    result = _advance(state, event)
    return replace(result, last_event_at=event.now) if result is not state else state


def _advance(state, event):
    validate_record(state)
    validate_record(event)
    check_legacy_profile(state.binding)
    if event.kind not in _EVENTS:
        refuse("unknown_event")
    if event.expected_revision != state.revision or event.claim_epoch != state.claim_epoch:
        refuse("stale_event")
    if event.now < state.last_event_at:
        refuse("operational_time_reversed")
    if (
        state.transport_attempts > 5
        or state.regrounds > 1
        or state.writer_repairs > 1
        or state.transport_attempts < state.binding.work.transport_attempts
        or state.writer_repairs < state.binding.work.writer_repairs
        or state.regrounds < state.binding.work.regrounds
    ):
        refuse("protocol_bounds")
    # A completed effect is immutable, including after a late failure or revocation.
    if state.completion is not None or state.status in _TERMINAL:
        refuse("terminal_state")
    kind = event.kind
    if kind == "revoke":
        return _next(state, status="revoked", request_status="failed", reason="privacy_revoked")
    if kind == "recover":
        if event.current is None:
            refuse("recovery_snapshot_required")
        check_dependencies(state.binding, event.current)
        if event.current.run.outcome == "running" and event.now < event.current.work.lease_deadline:
            refuse("worker_cleanup_required")
        if (
            event.current.work.version != state.work_version
            or event.current.work.request_version != state.request_version
            or event.current.work.claim_epoch != state.claim_epoch
            or state.claim_epoch >= MAX_COUNTER
        ):
            refuse("recovery_version")
        calls = tuple(
            replace(c, state="unknown") if c.state in {"prepared", "in_flight"} else c
            for c in state.invocations
        )
        return _next(
            state,
            status="pending",
            request_status="queued",
            invocations=calls,
            active_invocation=None,
            claimed_by=None,
            claim_epoch=state.claim_epoch + 1,
            reason="recovered_unknown" if state.active_invocation else "recovered",
        )
    if kind == "rebind":
        if not state.rebind_required or state.status != "pending" or event.current is None:
            refuse("rebind_not_pending")
        current = event.current
        _live(state, current, event.now, claim=True, semantic=False)
        check_legacy_profile(current)
        if (
            current.world_id != state.binding.world_id
            or current.principal != state.binding.principal
            or current.work.id != state.binding.work.id
            or current.work.request_id != state.binding.work.request_id
            or current.source.id != state.binding.source.id
        ):
            refuse("rebind_identity")
        return _next(
            state,
            binding=current,
            rebind_required=0,
            proposal=None,
            prior_invocations=state.prior_invocations + state.invocations,
            invocations=(),
            active_invocation=None,
            triage=None,
        )
    if kind == "stale":
        if event.current is None or state.status not in {"running", "ready", "repair_pending"}:
            refuse("stale_not_running")
        _live(state, event.current, event.now, semantic=False)
        # Do not admit the changed snapshot here. A separate rebind validates it.
        if state.regrounds >= 1:
            return _next(
                state,
                status="blocked",
                request_status="failed",
                proposal=None,
                reason="reground_exhausted",
            )
        if state.active_invocation is not None:
            refuse("worker_cleanup_required")
        return _next(
            state,
            status="pending",
            request_status="queued",
            proposal=None,
            regrounds=state.regrounds + 1,
            rebind_required=1,
            claimed_by=None,
            reason="dependencies_changed",
            triage=None,
        )
    _live(state, event.current, event.now, claim=kind == "claim")
    if kind == "claim":
        if state.status not in {"pending", "retry_wait"} or state.rebind_required:
            refuse("claim_not_pending")
        if state.retry_at is not None and event.now < state.retry_at:
            refuse("retry_not_due")
        if state.claim_epoch >= MAX_COUNTER:
            refuse("claim_overflow")
        return _next(
            state,
            status="running",
            request_status="applying",
            claim_epoch=state.claim_epoch + 1,
            claimed_by=event.current.run.id,
            active_pass_id=event.current.run.pass_id,
            active_input_ids=event.current.run.input_ids,
            retry_at=None,
            reason=None,
        )
    if kind == "block":
        if state.active_invocation is not None:
            refuse("worker_cleanup_required")
        return _next(
            state, status="blocked", request_status="failed", reason=event.reason or "unsupported"
        )
    if kind == "budget":
        if state.active_invocation is not None:
            refuse("worker_cleanup_required")
        if event.retry_at is None or not event.now < event.retry_at < event.current.run.deadline:
            refuse("budget_retry_time")
        return _next(
            state,
            status="retry_wait",
            request_status="failed",
            retry_at=event.retry_at,
            reason="budget_wait",
        )
    if kind == "prepare":
        if (
            state.status != "running"
            or state.active_invocation is not None
            or event.invocation is None
        ):
            refuse("prepare_not_running")
        call = event.invocation
        check_invocation(state.binding, call)
        previous = state.prior_invocations + state.invocations
        if (
            call.state != "prepared"
            or call.ordinal != state.transport_attempts + 1
            or call.run_id != state.claimed_by
            or call.claim_epoch != state.claim_epoch
            or any(c.id == call.id for c in previous)
            or state.transport_attempts >= 5
        ):
            refuse("invocation_sequence")
        limit = next(s.max_calls for s in state.binding.model_steps if s.stage == call.stage)
        if sum(c.stage == call.stage for c in previous) >= limit:
            refuse("stage_retry_exhausted")
        good = [c for c in state.invocations if c.state == "validated"]
        triages = [c for c in good if c.stage == "triage"]
        if call.stage == "triage" and any(c.stage != "triage" for c in state.invocations):
            refuse("stage_order")
        if call.stage == "extraction" and not any(
            load_json(c.output).get("candidates") for c in triages
        ):
            refuse("triage_before_extraction")
        if call.stage == "extraction" and any(
            c.stage == "extraction" and c.ordinal > state.repair_after for c in good
        ):
            refuse("extraction_retry_not_reserved")
        if call.stage == "adjudication" and not any(c.stage == "extraction" for c in good):
            refuse("extraction_before_adjudication")
        return _next(
            state,
            invocations=state.invocations + (call,),
            active_invocation=call.id,
            transport_attempts=state.transport_attempts + 1,
        )
    if kind in {"send", "result", "provider_failed", "unknown"}:
        if state.status != "running" or state.active_invocation is None:
            refuse("no_active_invocation")
        old = next(c for c in state.invocations if c.id == state.active_invocation)
        if kind == "send":
            if old.state != "prepared":
                refuse("already_sent")
            return _next(state, invocations=_update_call(state, replace(old, state="in_flight")))
        if old.state != "in_flight":
            refuse("invocation_not_sent")
        if kind == "result":
            call = event.invocation
            if call is None:
                refuse("invocation_result")
            check_invocation(state.binding, call)
            if (
                call.state != "validated"
                or replace(
                    call, state=old.state, model_call_id=None, output_digest=None, output=None
                )
                != old
            ):
                refuse("invocation_result_identity")
            if any(
                c.model_call_id == call.model_call_id for c in state.invocations if c.id != call.id
            ):
                refuse("duplicate_model_call")
            return _next(state, invocations=_update_call(state, call), active_invocation=None)
        due = plan_retry(state.transport_attempts, event.now, event.current.run.deadline)
        call = replace(old, state="unknown" if kind == "unknown" else "failed")
        return _next(
            state,
            invocations=_update_call(state, call),
            active_invocation=None,
            status="retry_wait" if due is not None else "failed",
            request_status="failed",
            retry_at=due,
            reason=call.state,
        )
    if kind == "repair":
        if state.status != "repair_pending" or state.writer_repairs:
            refuse("repair_exhausted")
        return _next(
            state,
            writer_repairs=1,
            repair_after=state.transport_attempts,
            status="running",
            request_status="applying",
            proposal=None,
        )
    if kind == "propose":
        if (
            state.status not in {"running", "repair_pending"}
            or event.proposal is None
            or state.active_invocation
        ):
            refuse("proposal_not_ready")
        if proposal_digest(event.proposal) in state.declined_digests:
            return state  # Replaying a decline spends no call, repair, version, or counter.
        if state.status == "repair_pending":
            refuse("repair_reservation_required")
        if event.proposal.invocations != state.invocations:
            refuse("proposal_invocation_trace")
        plan_completion(state.binding, event.proposal, event.triage)
        return _next(state, proposal=event.proposal, status="ready", triage=event.triage)
    if kind == "decline":
        if state.status != "ready" or state.proposal is None:
            refuse("decline_not_ready")
        key = proposal_digest(state.proposal)
        return _next(
            state,
            declined_digests=state.declined_digests + (key,),
            status="failed" if state.writer_repairs else "repair_pending",
            request_status="failed" if state.writer_repairs else "declined",
            reason=event.reason or "writer_declined",
        )
    if kind == "commit":
        if state.status != "ready" or state.proposal is None or event.completion_id is None:
            refuse("commit_not_ready")
        if event.triage != state.triage:
            refuse("completion_triage_changed")
        completion = plan_completion(state.binding, state.proposal, state.triage)
        return _next(
            state,
            completion=completion,
            completion_id=event.completion_id,
            status=completion.outcome,
            request_status="saved",
        )
    refuse("invalid_transition")


def plan_replay(state, current, now, completion_id):
    validate_record(state)
    if state.completion is None or completion_id != state.completion_id:
        refuse("completion_not_found")
    if (
        state.proposal is None
        or plan_completion(state.binding, state.proposal, state.triage) != state.completion
    ):
        refuse("completion_identity")
    check_dependencies(state.binding, current, replay=True)
    if (
        type(now) is not int
        or not state.last_event_at <= now <= MAX_COUNTER
        or current.run.outcome != "running"
        or now >= current.run.deadline
        or now >= current.work.lease_deadline
        or current.work.claimed_by != current.run.id
    ):
        refuse("replay_not_live")
    if current.run.id == state.claimed_by:
        if (
            current.work.claim_epoch != state.claim_epoch
            or current.work.request_version != state.request_version
            or current.work.version != state.work_version
            or current.run.version != state.run_version
            or current.run.pass_id != state.active_pass_id
            or current.run.input_ids != state.active_input_ids
        ):
            refuse("replay_claim")
    elif current.work.claim_epoch <= state.claim_epoch:
        refuse("recovery_claim_required")
    # This is a pure replay plan, never a release of a receipt to a principal.
    return state.completion


def plan_counter_delta(
    previous, completion, completion_id, completed_ids, ordered_statuses, cursor
):
    validate_record(completion)
    if previous not in {None, "done_empty", "done_extracted"}:
        refuse("counter_previous")
    if (
        type(completed_ids) is not tuple
        or type(ordered_statuses) is not tuple
        or len(ordered_statuses) > 256
        or len(completed_ids) > 256
        or type(cursor) is not int
        or not 0 <= cursor <= len(ordered_statuses)
    ):
        refuse("counter_bounds")
    if completion_id in completed_ids:
        return CounterDelta(0, 0, cursor)
    if completion.outcome not in {"done_empty", "done_extracted"}:
        refuse("counter_completion")
    empty = int(previous is None and completion.outcome == "done_empty")
    extracted = int(previous != "done_extracted" and completion.outcome == "done_extracted")
    if previous == "done_empty" and completion.outcome == "done_extracted":
        empty = -1
    while cursor < len(ordered_statuses) and ordered_statuses[cursor] in {
        "done_empty",
        "done_extracted",
    }:
        cursor += 1
    return CounterDelta(empty, extracted, cursor)
