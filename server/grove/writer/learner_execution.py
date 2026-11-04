"""Inactive durable Learner transaction protocol.

All functions operate inside one existing WorldTx. The missing owner boundary
refuses unconditionally; tests substitute that boundary explicitly. No callback,
provider, route, scheduler, or runtime constructor is installed here.
"""

from dataclasses import replace

from grove import clock
from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.dispositions import plan_completion
from grove.extraction.durable_types import (
    CompletionRef,
    ExecutionRef,
    ExpectedHead,
    IntakeCommand,
    StoredCallRequest,
    StoredCallResult,
    StoredInvocationRef,
)
from grove.extraction.execution_state import advance, plan_replay
from grove.extraction.execution_types import ExecutionEvent, ExecutionState, WorkRef
from grove.ids import new_id
from grove.writer import learner_requests as boundary
from grove.writer.api import execute_prepared_in_tx, prepare_batch
from grove.writer.caller import Caller
from grove.writer.learner_progress import (
    check_predecessor_in_tx,
    complete_progress_in_tx,
    enqueue_handoffs_in_tx,
    register_progress_in_tx,
)
from grove.writer.learner_values import (
    collect_values_in_tx,
    read_private_value_in_tx,
    write_private_value_in_tx,
)


def _ref(work, request, row):
    return ExecutionRef(
        work["id"],
        row["id"],
        row["ordinal"],
        request["version"],
        work["version"],
        row["claim_epoch"],
    )


def _head(work, request, row):
    return ExpectedHead(
        work["id"],
        work["version"],
        row["id"],
        row["ordinal"],
        request["id"],
        request["version"],
        row["claim_epoch"],
    )


def _completion(row):
    return CompletionRef(
        row["id"],
        row["work_id"],
        row["request_id"],
        row["execution_id"],
        row["commit_id"],
        row["change_set_id"],
        row["outcome"],
    )


def _work_roots(tx, work_id):
    roots = {}
    mapping = {
        "LearnerWork": {"initial_input_value_id": "binding"},
        "LearnerExecution": {
            "binding_value_id": "binding",
            "event_value_id": "event",
            "state_value_id": "state",
            "proposal_value_id": "proposal",
            "batch_value_id": "batch",
            "triage_value_id": "triage",
        },
        "LearnerCall": {
            "request_value_id": "call_request",
            "result_value_id": "call_result",
            "binding_value_id": "call_binding",
        },
        "LearnerDisposition": {"detail_value_id": "disposition"},
        "LearnerCompletion": {"detail_value_id": "completion"},
        "LearnerHandoff": {"payload_value_id": "handoff"},
    }
    for label, columns in mapping.items():
        rows = boundary._rows(
            tx, label, **({"id": work_id} if label == "LearnerWork" else {"work_id": work_id})
        )
        if label == "LearnerExecution" and len(rows) > 64:
            codec.refuse("work_bounds")
        for row in rows:
            for column, kind in columns.items():
                if row.get(column):
                    roots[row[column]] = kind
    return roots


def _reserve_values(tx, work_id, pending):
    existing = collect_values_in_tx(tx, _work_roots(tx, work_id))
    total = sum(len(raw) for raw in existing.values())
    for kind, raw in pending:
        codec.load_json(raw, codec.LIMITS[kind])
        total += len(raw)
    if total > codec.MAX_WORK_BYTES:
        codec.refuse("work_bounds")


def enqueue_episode_in_tx(tx, session, command):
    if type(command) is not IntakeCommand:
        codec.refuse("intake_command")
    tx.lock_world()
    binding = command.binding
    current = boundary._require_authority_in_tx(
        tx, session, "enqueue", binding, command.actual_run_witness
    )
    from grove.extraction.execution_profile import check_dependencies

    check_dependencies(binding, current)
    boundary._check_source_in_tx(tx, current)
    run = boundary.check_run_witness_in_tx(tx, command.actual_run_witness, binding)
    if (
        run.id != binding.run.id
        or command.actual_run_witness.protocol_run_version != binding.run.version
    ):
        codec.refuse("run_witness")
    for value in (
        command.event_id,
        command.new_work_id,
        command.new_request_id,
        command.new_execution_id,
    ):
        codec._id(value)
    if (
        (command.new_work_id, command.new_request_id) != (binding.work.id, binding.work.request_id)
        or binding.work.version != 1
        or binding.work.request_version != 1
        or binding.work.claimed_by is not None
        or any(
            (
                binding.work.transport_attempts,
                binding.work.regrounds,
                binding.work.writer_repairs,
                binding.work.claim_epoch,
            )
        )
    ):
        codec.refuse("intake_identity")
    old = boundary._one(tx, "LearnerWork", command.new_work_id, optional=True)
    if old is not None:
        initial = boundary._one(tx, "LearnerExecution", command.new_execution_id)
        expected = {
            "kind": "initial",
            "binding_value_id": old["initial_input_value_id"],
            "order": codec.plain(command.order),
        }
        if (
            initial["event_id"] != command.event_id
            or old["request_id"] != command.new_request_id
            or read_private_value_in_tx(tx, old["initial_input_value_id"], "binding")
            != legacy.encode_binding(binding)
            or codec.load_json(read_private_value_in_tx(tx, initial["event_value_id"], "event"))
            != expected
        ):
            codec.refuse("intake_identity")
        return WorkRef(
            **{
                **codec.plain(binding.work),
                "version": old["version"],
                "request_version": boundary._one(tx, "Request", old["request_id"])["version"],
            }
        )
    if boundary._one(tx, "Request", command.new_request_id, optional=True) is not None:
        codec.refuse("protected_request")
    predecessors = boundary._rows(
        tx, "LearnerWork", episode_id=binding.episode.id, profile=binding.profile
    )
    if any(
        r["state"] not in {"done_empty", "done_extracted", "blocked", "failed", "revoked"}
        for r in predecessors
    ):
        codec.refuse("intake_identity")
    predecessor = max(predecessors, key=lambda r: r["generation"]) if predecessors else None
    generation = 1 if predecessor is None else codec._int(predecessor["generation"] + 1, 1)
    state = ExecutionState(
        binding, request_version=1, work_version=1, run_version=binding.run.version
    )
    stamp = boundary._stamp(tx, command.event_id, run.id, command.new_request_id)
    binding_raw = legacy.encode_binding(binding)
    value_id = write_private_value_in_tx(tx, stamp, "binding", binding_raw)
    event_raw = codec.canonical(
        {"kind": "initial", "binding_value_id": value_id, "order": codec.plain(command.order)}
    )
    event_id = write_private_value_in_tx(tx, stamp, "event", event_raw)
    state_id = write_private_value_in_tx(
        tx, stamp, "state", codec.encode_stored_state(state, command.actual_run_witness, value_id)
    )
    work = boundary._newrow(
        tx,
        id=command.new_work_id,
        episode_id=binding.episode.id,
        source_id=binding.source.id,
        connector_id=binding.producer.id,
        generation=generation,
        profile=binding.profile,
        request_id=command.new_request_id,
        initial_input_value_id=value_id,
        latest_execution_id=command.new_execution_id,
        state="pending",
        created_at=stamp.at,
        supersedes_work_id=None if predecessor is None else predecessor["id"],
    )
    request = boundary._newrow(
        tx,
        id=command.new_request_id,
        protocol=boundary.PROTOCOL,
        learner_work_id=command.new_work_id,
        caller="learner",
        source_id=binding.source.id,
        content_key=binding.work.content_key,
        status="queued",
        attempts=0,
        repairs=0,
        run_id=run.id,
        created_at=stamp.at,
    )
    execution = boundary._newrow(
        tx,
        id=command.new_execution_id,
        work_id=work["id"],
        request_id=request["id"],
        run_id=run.id,
        ordinal=0,
        binding_revision=1,
        claim_epoch=0,
        event_id=command.event_id,
        event_kind="initial",
        state="pending",
        binding_value_id=value_id,
        event_value_id=event_id,
        state_value_id=state_id,
        dependency_ids=sorted({r.id for r in binding.context.dependencies}),
        commit_id=stamp.commit_id,
        change_set_id=stamp.change_set_id,
        at=stamp.at,
    )
    boundary._put(tx, "LearnerWork", work)
    boundary._put(tx, "Request", request)
    boundary._put(tx, "LearnerExecution", execution)
    register_progress_in_tx(tx, stamp, work, command.order)
    return binding.work


def _journal(
    tx,
    work,
    request,
    prior,
    state,
    run,
    event_id,
    event,
    *,
    payload=None,
    requests=None,
    results=None,
    proofs=None,
    execution_id=None,
):
    if state.revision >= 64:
        codec.refuse("work_bounds")
    stamp = boundary._stamp(tx, event_id, run.actual_run_id, request["id"])
    execution_id = execution_id or new_id(tx.world_id)
    retained = collect_values_in_tx(tx, _work_roots(tx, work["id"]))
    prospective = sum(len(raw) for raw in retained.values())

    def reserve(kind, raw):
        nonlocal prospective
        codec.load_json(raw, codec.LIMITS[kind])
        prospective += len(raw)
        if prospective > codec.MAX_WORK_BYTES:
            codec.refuse("work_bounds")
        return write_private_value_in_tx(tx, stamp, kind, raw)

    binding_id = prior["binding_value_id"]
    if read_private_value_in_tx(tx, binding_id, "binding") != legacy.encode_binding(state.binding):
        binding_id = reserve("binding", legacy.encode_binding(state.binding))
    call_refs = {}
    for call in (*state.prior_invocations, *state.invocations):
        old = boundary._one(tx, "LearnerCall", call.id, optional=True)
        req = (requests or {}).get(call.id)
        result = (results or {}).get(call.id)
        if old is None:
            if req is None or call.request != req.normalized_request:
                codec.refuse("payload_missing")
            request_value_id = reserve("call_request", codec.canonical(codec.plain(req)))
        else:
            request_value_id = old["request_value_id"]
            if (
                codec.load_json(read_private_value_in_tx(tx, request_value_id, "call_request"))[
                    "normalized_request"
                ]
                != call.request
            ):
                codec.refuse("call_evidence_missing")
        result_value_id = old.get("result_value_id") if old else None
        if result is not None:
            if result_value_id is not None:
                codec.refuse("call_evidence_missing")
            result_value_id = reserve("call_result", codec.canonical(codec.plain(result)))
        proof = (proofs or {}).get(call.id)
        prior_binding = (
            codec.load_json(read_private_value_in_tx(tx, old["binding_value_id"], "call_binding"))
            if old
            else {}
        )
        raw_binding = codec.encode_call_binding(
            call,
            digests=proof[0] if proof else prior_binding.get("digests"),
            owner_receipt_id=proof[1] if proof else prior_binding.get("owner_receipt_id"),
        )
        binding_value_id = (
            old["binding_value_id"]
            if old
            and raw_binding == read_private_value_in_tx(tx, old["binding_value_id"], "call_binding")
            else reserve("call_binding", raw_binding)
        )
        call_refs[call.id] = StoredInvocationRef(
            call.id, request_value_id, result_value_id, binding_value_id
        )
        if old is not None and old["binding_value_id"] == binding_value_id:
            continue
        row = boundary._newrow(
            tx,
            id=call.id,
            work_id=work["id"],
            prepared_execution_id=execution_id if old is None else old["prepared_execution_id"],
            run_id=call.run_id,
            ordinal=call.ordinal,
            claim_epoch=call.claim_epoch,
            binding_revision=prior["binding_revision"] + int(event.kind == "rebind"),
            stage=call.stage,
            route=call.route,
            model=call.model,
            job=call.job,
            effort=call.effort,
            state=call.state,
            request_value_id=request_value_id,
            result_value_id=result_value_id,
            binding_value_id=binding_value_id,
            model_call_id=call.model_call_id,
            latest_execution_id=execution_id,
            prepared_at=stamp.at if old is None else old["prepared_at"],
            sent_at=stamp.at if call.state == "in_flight" else old.get("sent_at") if old else None,
            settled_at=stamp.at
            if call.state in {"validated", "unknown", "failed", "cancelled"}
            else None,
            validation_code="ok"
            if call.state == "validated"
            else call.state
            if call.state in {"unknown", "cancelled"}
            else "invalid"
            if call.state == "failed"
            else None,
            version=1 if old is None else codec._int(old["version"] + 1, 1),
        )
        boundary._put(tx, "LearnerCall", row, old)
    inv_refs = tuple(call_refs[c.id] for c in state.invocations)
    old_refs = tuple(call_refs[c.id] for c in state.prior_invocations)
    proposal_id = (
        None
        if state.proposal is None
        else reserve("proposal", codec.encode_stored_proposal(state.proposal, binding_id, inv_refs))
    )
    triage_id = (
        None
        if state.triage is None
        else reserve("triage", codec.canonical(codec.plain(state.triage)))
    )
    current_id = (
        None if event.current is None else reserve("binding", legacy.encode_binding(event.current))
    )
    event_proposal_id = proposal_id
    if event.proposal is not None and event.proposal != state.proposal:
        codec.refuse("payload_mismatch")
    event_raw = codec.encode_stored_event(
        event,
        invocation_ref=None if event.invocation is None else call_refs[event.invocation.id],
        proposal_value_id=event_proposal_id if event.proposal else None,
        triage_value_id=triage_id if event.triage else None,
        current_value_id=current_id,
    )
    event_value_id = reserve("event", event_raw)
    witness = replace(run, protocol_run_version=state.run_version)
    state_id = reserve(
        "state",
        codec.encode_stored_state(
            state,
            witness,
            binding_id,
            invocation_refs=inv_refs,
            prior_invocation_refs=old_refs,
            proposal_value_id=proposal_id,
            triage_value_id=triage_id,
        ),
    )
    batch_id = (
        reserve("batch", codec.payload_bytes(payload))
        if payload
        else prior.get("batch_value_id")
        if state.proposal
        else None
    )
    row = boundary._newrow(
        tx,
        id=execution_id,
        work_id=work["id"],
        request_id=request["id"],
        run_id=run.actual_run_id,
        ordinal=state.revision,
        binding_revision=prior["binding_revision"] + int(event.kind == "rebind"),
        claim_epoch=state.claim_epoch,
        predecessor_id=prior["id"],
        event_id=event_id,
        event_kind=event.kind,
        state=state.status,
        binding_value_id=binding_id,
        event_value_id=event_value_id,
        state_value_id=state_id,
        proposal_value_id=proposal_id,
        batch_value_id=batch_id,
        triage_value_id=triage_id,
        dependency_ids=sorted({r.id for r in state.binding.context.dependencies}),
        commit_id=stamp.commit_id,
        change_set_id=stamp.change_set_id,
        at=stamp.at,
    )
    boundary._put(tx, "LearnerExecution", row)
    work = boundary._put(
        tx,
        "LearnerWork",
        {
            **work,
            "latest_execution_id": row["id"],
            "state": state.status,
            "version": state.work_version,
            "retry_at": boundary._instant(state.retry_at),
            "completion_id": state.completion_id,
            "touched_at": stamp.at,
        },
        work,
    )
    request = boundary._put(
        tx,
        "Request",
        {
            **request,
            "status": state.request_status,
            "version": state.request_version,
            "run_id": run.actual_run_id,
            "claimed_by": state.claimed_by,
            "repairs": state.writer_repairs,
            "attempts": request["attempts"] + int(event.kind == "claim"),
            "retry_at": boundary._instant(state.retry_at),
            "lease_until": boundary._instant(state.binding.work.lease_deadline),
            "last_error_code": state.reason,
            "learner_completion_id": state.completion_id,
            "touched_at": stamp.at,
            **(
                {
                    "commit_id": stamp.commit_id,
                    "change_set_id": stamp.change_set_id,
                    "committed_at": stamp.at,
                }
                if state.completion_id
                else {}
            ),
        },
        request,
    )
    return work, request, row


def _event(state, current, kind, **kwargs):
    return ExecutionEvent(
        kind=kind,
        expected_revision=state.revision,
        claim_epoch=state.claim_epoch,
        now=boundary._epoch(clock.now(current.world_id)),
        current=current,
        **kwargs,
    )


def _try_replay(tx, session, head, run, event_id, kind, event_fields=None, payload=None):
    tx.lock_world()
    rows = boundary._rows(tx, "LearnerExecution", work_id=head.work_id, event_id=event_id)
    if not rows:
        return None
    if len(rows) != 1:
        codec.refuse("snapshot_inconsistent")
    row = rows[0]
    values = collect_values_in_tx(
        tx, {row["event_value_id"]: "event", row["state_value_id"]: "state"}
    )
    event = codec.rehydrate_event(values[row["event_value_id"]], values)
    if (
        event.kind != kind
        or event.expected_revision != head.execution_ordinal
        or event.claim_epoch != head.claim_epoch
        or row.get("predecessor_id") != head.execution_id
        or event.current.work.version != head.work_version
        or event.current.work.request_version != head.request_version
    ):
        codec.refuse("event_identity")
    if any(getattr(event, key) != value for key, value in (event_fields or {}).items()):
        codec.refuse("event_identity")
    if payload is not None and read_private_value_in_tx(
        tx, row["batch_value_id"], "batch"
    ) != codec.payload_bytes(payload):
        codec.refuse("event_identity")
    work, request, latest, state, stored_run, _ = boundary._load_work(tx, head.work_id)
    if state.completion is not None or row["claim_epoch"] != state.claim_epoch:
        codec.refuse("stale_claim")
    current_run = replace(run, protocol_run_version=state.run_version)
    if current_run != stored_run:
        codec.refuse("stale_claim")
    boundary.check_current_claim_in_tx(
        tx, session, _head(work, request, latest), current_run, purpose="event_replay"
    )
    old_state = codec.rehydrate_state(values[row["state_value_id"]], values)
    return ExecutionRef(
        head.work_id,
        row["id"],
        row["ordinal"],
        old_state.request_version,
        old_state.work_version,
        row["claim_epoch"],
    )


def _transition(
    tx,
    session,
    head,
    run,
    event_id,
    kind,
    *,
    event_fields=None,
    payload=None,
    requests=None,
    results=None,
    proofs=None,
):
    codec._id(event_id)
    replay = _try_replay(tx, session, head, run, event_id, kind, event_fields, payload)
    if replay is not None:
        return replay
    ended = kind == "recover"
    work, request, prior, state, current, _ = boundary.check_current_claim_in_tx(
        tx, session, head, run, purpose=kind, ended=ended
    )
    if boundary._rows(tx, "LearnerExecution", work_id=work["id"], event_id=event_id):
        codec.refuse("stale_claim")
    event = _event(state, current, kind, **(event_fields or {}))
    next_state = advance(state, event)
    if next_state is state:
        return _ref(work, request, prior)
    work, request, row = _journal(
        tx,
        work,
        request,
        prior,
        next_state,
        run,
        event_id,
        event,
        payload=payload,
        requests=requests,
        results=results,
        proofs=proofs,
    )
    return _ref(work, request, row)


def claim_execution_in_tx(tx, session, head, run, event_id):
    return _transition(tx, session, head, run, event_id, "claim")


def record_invocation_in_tx(tx, session, head, run, event_id, call, request):
    tx.lock_world()
    if type(request) is not StoredCallRequest:
        codec.refuse("payload_missing")
    if call.state == "prepared":
        old = boundary._one(tx, "LearnerCall", call.id, optional=True)
        if old is not None and codec.load_json(
            read_private_value_in_tx(tx, old["request_value_id"], "call_request")
        ) != codec.plain(request):
            codec.refuse("event_identity")
        return _transition(
            tx,
            session,
            head,
            run,
            event_id,
            "prepare",
            event_fields={"invocation": call},
            requests={call.id: request},
        )
    if call.state == "in_flight":
        old = boundary._one(tx, "LearnerCall", call.id)
        if codec.load_json(
            read_private_value_in_tx(tx, old["request_value_id"], "call_request")
        ) != codec.plain(request):
            codec.refuse("payload_mismatch")
        stored = codec._invocations(
            [
                {
                    "call_id": call.id,
                    "request_value_id": old["request_value_id"],
                    "result_value_id": old.get("result_value_id"),
                    "binding_value_id": old["binding_value_id"],
                }
            ],
            collect_values_in_tx(
                tx,
                {
                    old["request_value_id"]: "call_request",
                    old["binding_value_id"]: "call_binding",
                    **(
                        {old["result_value_id"]: "call_result"}
                        if old.get("result_value_id")
                        else {}
                    ),
                },
            ),
        )[0]
        if (
            replace(stored, state="in_flight", output=None, output_digest=None, model_call_id=None)
            != call
        ):
            codec.refuse("event_identity")
        return _transition(tx, session, head, run, event_id, "send")
    codec.refuse("invocation_state")


def record_result_in_tx(tx, session, head, run, event_id, attempt, result):
    tx.lock_world()
    previous = boundary._rows(tx, "LearnerExecution", work_id=head.work_id, event_id=event_id)
    if previous:
        values = collect_values_in_tx(tx, {previous[0]["event_value_id"]: "event"})
        event = codec.rehydrate_event(values[previous[0]["event_value_id"]], values)
        if event.kind != "result" or event.invocation.id != attempt.call_id:
            codec.refuse("event_identity")
        call_row = boundary._one(tx, "LearnerCall", attempt.call_id)
        if codec.load_json(
            read_private_value_in_tx(tx, call_row["result_value_id"], "call_result")
        ) != codec.plain(result):
            codec.refuse("event_identity")
        request = StoredCallRequest(
            **codec.load_json(
                read_private_value_in_tx(tx, call_row["request_value_id"], "call_request")
            )
        )
        binding = legacy.decode_binding(
            read_private_value_in_tx(tx, previous[0]["binding_value_id"], "binding")
        )
        codec.check_transport_binding(binding, event.invocation, request, result, attempt)
        boundary._require_attempt_in_tx(tx, session, attempt, request, result)
        return _try_replay(
            tx, session, head, run, event_id, "result", {"invocation": event.invocation}
        )
    _, _, _, state, _, _ = boundary.check_current_claim_in_tx(
        tx, session, head, run, purpose="result"
    )
    call = next((c for c in state.invocations if c.id == attempt.call_id), None)
    if call is None or state.active_invocation != call.id:
        codec.refuse("call_evidence_missing")
    row = boundary._one(tx, "LearnerCall", call.id)
    request = StoredCallRequest(
        **codec.load_json(read_private_value_in_tx(tx, row["request_value_id"], "call_request"))
    )
    updated = replace(
        call,
        state="validated",
        model_call_id=attempt.model_call_id,
        output=result.normalized_output,
        output_digest=legacy.text_digest(result.normalized_output),
    )
    digests = codec.check_transport_binding(state.binding, updated, request, result, attempt)
    receipt = boundary._require_attempt_in_tx(tx, session, attempt, request, result)
    codec._id(receipt)
    return _transition(
        tx,
        session,
        head,
        run,
        event_id,
        "result",
        event_fields={"invocation": updated},
        results={call.id: result},
        proofs={call.id: (digests, receipt)},
    )


def record_failure_in_tx(tx, session, head, run, event_id, event):
    if type(event) is not ExecutionEvent or event.kind not in {
        "provider_failed",
        "unknown",
        "decline",
        "repair",
        "stale",
        "rebind",
        "recover",
        "revoke",
        "block",
        "budget",
    }:
        codec.refuse("invalid_transition")
    reasons = {
        None,
        "unsupported",
        "writer_declined",
        "near_duplicate",
        "dependencies_changed",
        "budget_wait",
        "privacy_revoked",
        "invalid",
        "unknown",
        "cancelled",
        "payload_missing",
        "payload_mismatch",
        "call_evidence_missing",
        "protected_request",
        "stale_claim",
        "order_window_unavailable",
        "lineage_unavailable",
        "snapshot_inconsistent",
        "erased_dependency",
    }
    if event.reason not in reasons:
        codec.refuse("reason_code")
    # Callers may name a transition but may not supply a replacement current snapshot.
    if (
        event.current is not None
        or event.invocation is not None
        or event.proposal is not None
        or event.triage is not None
        or event.completion_id is not None
        or event.expected_revision != head.execution_ordinal
        or event.claim_epoch != head.claim_epoch
    ):
        codec.refuse("stale_claim")
    return _transition(
        tx,
        session,
        head,
        run,
        event_id,
        event.kind,
        event_fields={"reason": event.reason, "retry_at": event.retry_at},
    )


def record_proposal_in_tx(tx, session, head, run, event_id, proposal, payload, triage=None):
    replay = _try_replay(
        tx,
        session,
        head,
        run,
        event_id,
        "propose",
        {"proposal": proposal, "triage": triage},
        payload,
    )
    if replay is not None:
        return replay
    _, _, _, state, _, _ = boundary.check_current_claim_in_tx(
        tx, session, head, run, purpose="propose"
    )
    codec.check_descriptor_payloads(state.binding, proposal, payload)
    plan_completion(state.binding, proposal, triage)
    return _transition(
        tx,
        session,
        head,
        run,
        event_id,
        "propose",
        event_fields={"proposal": proposal, "triage": triage},
        payload=payload,
    )


def _recheck_calls(tx, session, state, payload):
    for call in state.invocations:
        if call.state != "validated":
            continue
        row = boundary._one(tx, "LearnerCall", call.id)
        request = StoredCallRequest(
            **codec.load_json(read_private_value_in_tx(tx, row["request_value_id"], "call_request"))
        )
        result = StoredCallResult(
            **codec.load_json(read_private_value_in_tx(tx, row["result_value_id"], "call_result"))
        )
        stored = codec.load_json(
            read_private_value_in_tx(tx, row["binding_value_id"], "call_binding")
        )
        from grove.extraction.durable_types import RecordedAttemptWitness

        digests = stored["digests"]
        if type(digests) is not dict or not stored["owner_receipt_id"]:
            codec.refuse("call_evidence_missing")
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
            *(digests[name] for name in codec.DIGEST_NAMES),
        )
        codec.check_transport_binding(
            state.binding, call, request, result, attempt, payload=payload
        )
        if (
            boundary._require_attempt_in_tx(tx, session, attempt, request, result)
            != stored["owner_receipt_id"]
        ):
            codec.refuse("call_evidence_missing")


def _effect_ledger(tx, stamp, batch):
    """Read actual committed-in-this-transaction identities, never proposed counts."""
    entries = boundary._rows(tx, "ChangeEntry", commit_id=stamp.commit_id)
    if any(e["change_set_id"] != stamp.change_set_id for e in entries):
        codec.refuse("payload_mismatch")
    entries.sort(key=lambda e: e["sequence"])
    creates = {
        kind: [
            e
            for e in entries
            if e["entity_type"] == kind
            and e["field"] in ({"row"} if kind == "item" else {"row", "restated"})
        ]
        for kind in ("item", "link")
    }
    observations = {e["ev_id"]: e for e in batch["evidence"]}
    temporary, effects, consumed = {}, [], set()
    from grove.writer.ops import cited_ev_ids

    for op in batch["ops"]:
        kind = "link" if op["op"] in {"create_link", "end_link"} else op.get("target_type", "item")
        if op["op"] in {"create_item", "create_link"}:
            matches = []
            if kind == "link":
                from grove.writer.registry import Registry

                meaning, reversed_ = Registry.load(tx).resolve_meaning(op["meaning"])
                start, end = (temporary.get(op[key], op[key]) for key in ("from", "to"))
                if reversed_:
                    start, end = end, start
                matches = boundary._rows(
                    tx, "Link", from_item=start, meaning=meaning["id"], to_item=end
                )
            selected = next(
                (
                    e
                    for e in creates[kind]
                    if e["sequence"] not in consumed
                    and (kind != "link" or e["entity_id"] in {row["id"] for row in matches})
                ),
                None,
            )
            if selected is None:
                if op["op"] != "create_link":
                    codec.refuse("payload_mismatch")
                if len(matches) != 1:
                    codec.refuse("payload_mismatch")
                target = matches[0]["id"]
            else:
                consumed.add(selected["sequence"])
                target = selected["entity_id"]
            if op.get("temp_id"):
                temporary[op["temp_id"]] = target
        else:
            target = op.get("item", op.get("link", op.get("target", op.get("claim"))))
            target = temporary.get(target, target)
        if op["op"] in {"attach_evidence", "refute", "supersede"}:
            kind = "item" if boundary._one(tx, "Item", target, optional=True) else "link"
        # The ordinary writer resolves kind names and applies later operations.
        # Read the actual identity; never compare a final row to a pre-update payload.
        boundary._one(tx, "Link" if kind == "link" else "Item", target)
        actual_evidence = []
        for ev_id in cited_ev_ids(op):
            source = observations[ev_id]
            polarity = (
                op["polarity"]
                if op["op"] == "attach_evidence"
                else next(
                    ref["polarity"] for ref in op["evidence_refs"] if ref.get("ev_id") == ev_id
                )
            )
            matches = [
                e
                for e in boundary._rows(tx, "Evidence", target_id=target)
                if e.get("source_id") == source["source_id"]
                and e.get("episode_id") == source["episode_id"]
                and e.get("message_key") == source["message_key"]
                and e.get("locator") == source["locator"]
                and e.get("statement_class") == source["statement_class"]
                and e.get("speaker_kind") == source["speaker_kind"]
                and e.get("polarity") == polarity
                and all(
                    e.get(key) == source.get(key)
                    for key in ("speaker_item", "original_author", "modality", "derivation")
                )
                and boundary._same_instant(e.get("source_time"), source["source_time"])
            ]
            if not matches:
                codec.refuse("payload_mismatch")
            actual_evidence.extend(e["id"] for e in matches)
        effects.append(
            {
                "op_id": op["op_id"],
                "target_type": kind,
                "target_id": target,
                "evidence_ids": sorted(set(actual_evidence)),
                "entry_sequences": [
                    e["sequence"]
                    for e in entries
                    if e["entity_id"] == target or e["entity_id"] in actual_evidence
                ],
            }
        )
    created = len(
        {e["entity_id"] for e in entries if e["entity_type"] == "item" and e["field"] == "row"}
    )
    return effects, created


def complete_execution_in_tx(tx, session, head, run, event_id, proposal, payload, triage=None):
    codec._id(event_id)
    tx.lock_world()
    prior_events = boundary._rows(tx, "LearnerExecution", work_id=head.work_id, event_id=event_id)
    if prior_events:
        work, request, latest, state, _, values = boundary._load_work(tx, head.work_id)
        row = prior_events[0]
        if len(prior_events) != 1 or row["id"] != latest["id"] or state.completion_id is None:
            codec.refuse("event_identity")
        event = codec.rehydrate_event(values[row["event_value_id"]], values)
        if (
            event.kind != "commit"
            or row.get("predecessor_id") != head.execution_id
            or (
                event.expected_revision,
                event.claim_epoch,
                event.current.work.version,
                event.current.work.request_version,
                request["id"],
            )
            != (
                head.execution_ordinal,
                head.claim_epoch,
                head.work_version,
                head.request_version,
                head.request_id,
            )
            or proposal != state.proposal
            or triage != state.triage
            or read_private_value_in_tx(tx, row["batch_value_id"], "batch")
            != codec.payload_bytes(payload)
        ):
            codec.refuse("event_identity")
        return read_completion_in_tx(
            tx,
            session,
            replace(run, protocol_run_version=state.run_version),
            head.work_id,
            state.completion_id,
        )
    work, request, prior, state, current, _ = boundary.check_current_claim_in_tx(
        tx, session, head, run, purpose="complete"
    )
    if state.completion_id:
        return read_completion_in_tx(tx, session, run, work["id"], state.completion_id)
    if state.status != "ready" or proposal != state.proposal or triage != state.triage:
        codec.refuse("proposal_not_ready")
    batch = codec.check_descriptor_payloads(state.binding, proposal, payload)
    if read_private_value_in_tx(tx, prior["batch_value_id"], "batch") != codec.payload_bytes(
        payload
    ):
        codec.refuse("payload_mismatch")
    plan = plan_completion(state.binding, proposal, triage)
    _recheck_calls(tx, session, state, payload)
    check_predecessor_in_tx(tx, work)
    completion_id, execution_id = new_id(tx.world_id), new_id(tx.world_id)
    stamp = boundary._stamp(tx, event_id, run.actual_run_id, request["id"])
    effects, created = [], 0
    if batch["ops"]:
        caller = Caller("update", ("grove.propose",), "learner")
        execute_prepared_in_tx(
            tx, stamp, prepare_batch(caller, batch, request_id=request["id"]), request
        )
        effects, created = _effect_ledger(tx, stamp, batch)
    elif batch["evidence"] or plan.outcome != "done_empty":
        codec.refuse("payload_mismatch")
    # The empty branch has the same current-source, owner, ledger and order checks.
    checked = boundary._require_authority_in_tx(tx, session, "commit", state.binding, run)
    from grove.extraction.execution_profile import check_dependencies

    check_dependencies(state.binding, checked)
    boundary._check_source_in_tx(tx, checked)
    boundary.check_run_witness_in_tx(tx, run, state.binding)
    event = _event(state, current, "commit", triage=triage, completion_id=completion_id)
    final = advance(state, event)
    dispositions = []
    detail_values = []
    for ordinal, (message, disposition) in enumerate(
        zip(state.binding.inventory, proposal.dispositions, strict=True)
    ):
        raw = codec.canonical(
            {"message": codec.plain(message), "disposition": codec.plain(disposition)},
            codec.LIMITS["disposition"],
        )
        detail_values.append(("disposition", raw))
        dispositions.append((ordinal, message, disposition, raw))
    detail = codec.canonical(
        {
            "plan": codec.plain(plan),
            "effect_ledger": effects,
            "request_version": final.request_version,
            "work_version": final.work_version,
            "run_version": final.run_version,
        },
        codec.LIMITS["completion"],
    )
    _reserve_values(tx, work["id"], detail_values + [("completion", detail)])
    disposition_ids = []
    for ordinal, message, disposition, raw in dispositions:
        value_id = write_private_value_in_tx(tx, stamp, "disposition", raw)
        row = boundary._newrow(
            tx,
            work_id=work["id"],
            execution_id=execution_id,
            source_id=state.binding.source.id,
            episode_id=state.binding.episode.id,
            binding_revision=prior["binding_revision"],
            source_version=state.binding.source.version,
            episode_version=state.binding.episode.version,
            message_ordinal=ordinal,
            locator=message.locator,
            state=disposition.state,
            detail_value_id=value_id,
            operation_ids=sorted({key for c in disposition.classes for key in c.operation_ids}),
            evidence_ids=sorted({key for c in disposition.classes for key in c.evidence_ids}),
            at=stamp.at,
        )
        boundary._put(tx, "LearnerDisposition", row)
        disposition_ids.append(row["id"])
    detail_id = write_private_value_in_tx(tx, stamp, "completion", detail)
    completion = boundary._newrow(
        tx,
        id=completion_id,
        work_id=work["id"],
        request_id=request["id"],
        execution_id=execution_id,
        run_id=run.actual_run_id,
        binding_revision=prior["binding_revision"],
        claim_epoch=state.claim_epoch,
        commit_id=stamp.commit_id,
        change_set_id=stamp.change_set_id,
        outcome=plan.outcome,
        detail_value_id=detail_id,
        disposition_ids=disposition_ids,
        operation_count=len(proposal.operations),
        message_count=plan.messages,
        blank_count=plan.blank_messages,
        empty_count=plan.empty_messages,
        extracted_count=plan.extracted_messages,
        created_item_count=created,
        completed_at=stamp.at,
    )
    boundary._put(tx, "LearnerCompletion", completion)
    work, request, _ = _journal(
        tx,
        work,
        request,
        prior,
        final,
        run,
        event_id,
        event,
        payload=payload,
        execution_id=execution_id,
    )
    complete_progress_in_tx(tx, stamp, work, completion)
    enqueue_handoffs_in_tx(tx, stamp, work, completion, [e["target_id"] for e in effects])
    boundary._require_authority_in_tx(tx, session, "release", checked, run)
    from grove.writer import apply as writer_apply

    writer_apply._before_commit()
    return _completion(completion)


def read_completion_in_tx(tx, session, run, work_id, completion_id):
    tx.lock_world()
    work, request, row, state, _, _ = boundary._load_work(tx, work_id)
    current = boundary._require_authority_in_tx(tx, session, "replay", state.binding, run)
    actual = boundary.check_run_witness_in_tx(tx, run, state.binding)
    boundary._check_source_in_tx(tx, current)
    from grove.extraction.execution_profile import check_dependencies

    check_dependencies(state.binding, current, replay=True)
    if run.actual_run_id != state.claimed_by:
        # A new recovery claim must come from the real owner. Stored booleans cannot establish it.
        boundary._require_authority_in_tx(tx, session, "recovery_release", current, run)
    else:
        current = replace(
            current,
            work=replace(
                current.work,
                version=state.work_version,
                request_version=state.request_version,
                claim_epoch=state.claim_epoch,
                claimed_by=state.claimed_by,
            ),
        )
    current = replace(current, run=actual)
    plan_replay(state, current, boundary._epoch(clock.now(tx.world_id)), completion_id)
    completion = boundary._one(tx, "LearnerCompletion", completion_id)
    if (
        completion["work_id"] != work_id
        or completion["request_id"] != request["id"]
        or completion["execution_id"] != row["id"]
        or request.get("learner_completion_id") != completion_id
    ):
        codec.refuse("snapshot_inconsistent")
    _recheck_calls(
        tx,
        session,
        state,
        codec.decode_payload(read_private_value_in_tx(tx, row["batch_value_id"], "batch")),
    )
    boundary._require_authority_in_tx(tx, session, "release", current, run)
    return _completion(completion)
