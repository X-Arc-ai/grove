"""Fixed authority transactions under WorldWriter's existing World lock.

No file access, callbacks selected by callers, bootstrap, or runtime registration.
Only immutable issued commands can reach these functions. Metadata is not consent.
"""

from dataclasses import replace

from grove.authority.codec import complete_event_digest
from grove.authority.publication import refuse
from grove.authority.records import PROFILE, check_scalar
from grove.authority.scalars import MAX_COUNT
from grove.authority.transitions import _pure_expansion, classify_effects
from grove.store.query import CYPHER_PREFIX
from grove.writer.authority_records import (
    MAX_ROWS,
    ApplyCommand,
    ApprovalAccess,
    AttemptCommand,
    BarrierCommand,
    BarrierProof,
    CancelCommand,
    FinalizeCommand,
    InspectionCommand,
    NativeOutcome,
    SealCommand,
    check_binding,
    check_command,
    instant,
    projection_binding,
    validate_row,
)


def _world(tx, owner):
    found = (
        tx.unscoped()
        .run(
            CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) RETURN w.owner_user_id AS owner, "
            "w.next_sequence AS sequence, w.read_generation AS generation",
            world_id=tx.world_id,
        )
        .single()
    )
    if found is None or found["owner"] != owner._owner_id:
        refuse("binding", "outcome")
    for name in ("sequence", "generation"):
        check_scalar("U", found[name])
    return dict(found)


def _projection(tx, owner):
    rows = tx.run(
        "MATCH (n:AuthorityProjection {installation_id: $installation}) RETURN n",
        installation=owner.bindings.installation_id,
    )
    if len(rows) != 1:
        refuse("adapter_missing", "outcome")
    row = validate_row("projection", rows[0]["n"])
    if row["world_id"] != tx.world_id or row["owner_id"] != owner._owner_id:
        refuse("binding", "outcome")
    owner._validate_projection_in_tx(tx, row)
    return row


def _operation(tx, owner, intent_id, key=None):
    rows = tx.run(
        "MATCH (n:AuthorityOperation {installation_id: $installation}) "
        "WHERE n.id = $id OR n.idempotency_key = $key RETURN n",
        installation=owner.bindings.installation_id,
        id=intent_id,
        key=key,
    )
    if len(rows) > 1:
        refuse("conflict", "outcome")
    return validate_row("operation", rows[0]["n"]) if rows else None


def _count(tx, label):
    # The label comes exclusively from these two literals, never a command.
    if label not in {"AuthorityOperation", "AuthorityApprovalUse"}:
        refuse("binding")
    count = tx.run(f"MATCH (n:{label}) RETURN count(n) AS count")[0]["count"]
    check_scalar("U", count)
    if count > MAX_ROWS:
        refuse("capacity", "outcome")
    return count


def _capacity(tx, *, operation=0, uses=0):
    if _count(tx, "AuthorityOperation") + operation > MAX_ROWS:
        refuse("capacity", "outcome")
    if _count(tx, "AuthorityApprovalUse") + uses > MAX_ROWS:
        refuse("capacity", "outcome")


def _patch(tx, label, old, new):
    if label not in {"AuthorityProjection", "AuthorityOperation"}:
        refuse("binding")
    props = {k: v for k, v in new.items() if k != "world_id" and old.get(k) != v}
    if not props:
        return
    tx.run(
        f"MATCH (n:{label} {{installation_id: $installation}}) "
        + ("WHERE n.id = $id " if label == "AuthorityOperation" else "")
        + "SET n += $props",
        installation=old["installation_id"],
        id=old.get("id"),
        props=props,
    )


def _fact(row, projection, outcome=None, proof=None):
    return NativeOutcome(
        outcome or (row.get("outcome") if row else None) or "UNKNOWN",
        projection_binding(projection),
        projection["erasure_revision"],
        projection["fence"],
        tuple(sorted(row.items())) if row else (),
        proof,
    )


def _registration(command, owner):
    event, intent = command.prepare, command.prepare.payload.intent
    approval = intent.authorization
    return {
        "id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "world_id": command.binding.world_id,
        "owner_id": owner._owner_id,
        "installation_id": owner.bindings.installation_id,
        "profile_digest": PROFILE.digest,
        "intent_digest": intent.intent_digest,
        "prepare_event_id": event.event_id,
        "prepare_sequence": event.sequence,
        "prepare_head_digest": complete_event_digest(event),
        "base_sequence": intent.base_sequence,
        "base_head_digest": intent.base_head_digest,
        "base_state_digest": intent.base_state_digest,
        "expected_result_digest": intent.result_state_digest,
        "installation_epoch": intent.expected_installation_epoch,
        "authority_epoch": intent.expected_authority_epoch,
        "erasure_revision": intent.expected_erasure_revision,
        "mode": intent.mode,
        "original_approval_id": approval.approval_id if approval.kind == "owner_approval" else None,
        "choice_digest": approval.payload_digest if approval.kind == "owner_approval" else None,
        "registered_at": instant(command.binding.at),
    }


def _same_intent(row, command, owner):
    expected = _registration(command, owner)
    if any(row.get(k) != v for k, v in expected.items() if k != "registered_at"):
        refuse("conflict", "outcome")
    owner._assert_original(command.prepare)


def _base(projection, intent):
    if (
        projection["installation_epoch"],
        projection["authority_epoch"],
        projection["erasure_revision"],
        projection["state_digest"],
    ) != (
        intent.expected_installation_epoch,
        intent.expected_authority_epoch,
        intent.expected_erasure_revision,
        intent.base_state_digest,
    ):
        refuse("conflict", "outcome")


def _increment(value):
    check_scalar("U", value)
    if value == MAX_COUNT:
        refuse("capacity", "outcome")
    return value + 1


def register_attempt_in_tx(tx, command):
    if type(command) is not AttemptCommand:
        refuse("binding")
    owner = check_command(command, tx)
    _world(tx, owner)
    projection = _projection(tx, owner)
    intent = command.prepare.payload.intent
    owner._validate_in_tx(tx, command.prepare, "register")
    old = _operation(tx, owner, intent.intent_id, intent.idempotency_key)
    if old:
        _same_intent(old, command, owner)
        if old.get("outcome"):
            owner._validate_result_in_tx(tx, old, projection)
            return _fact(old, projection)
        if (
            old["phase"] == "active"
            and old["attempt_id"] == command.attempt_id
            and old["operation_id"] == command.operation_id
            and old["attempt_fence"] == command.expected_fence + 1 == projection["fence"]
        ):
            return _fact(old, projection)
        owner._assert_recovery_attempt(old, projection, command)
    elif projection["active_intent_id"] is not None:
        refuse("pending", "outcome")
    if command.operation_id != command.binding.operation_id:
        refuse("binding")
    if (
        projection["fence"] != command.expected_fence
        or projection_binding(projection) != command.expected_projection
    ):
        refuse("conflict", "outcome")
    _base(projection, intent)
    if not old and (projection["sequence"], projection["head_digest"]) != (
        intent.base_sequence,
        intent.base_head_digest,
    ):
        refuse("conflict", "outcome")
    _capacity(tx, operation=int(old is None), uses=1)
    fence = _increment(projection["fence"])
    row = {
        **(old or _registration(command, owner)),
        "phase": "active",
        "attempt_id": command.attempt_id,
        "attempt_fence": fence,
        "operation_id": command.operation_id,
        "revoked_through": projection["fence"],
        "barrier_id": None,
        "barrier_fence": None,
    }
    validate_row("operation", row)
    if old is None:
        tx.run(
            "CREATE (n:AuthorityOperation) SET n += $row",
            row={k: v for k, v in row.items() if k != "world_id"},
        )
    else:
        _patch(tx, "AuthorityOperation", old, row)
    updated = {
        **projection,
        "fence": fence,
        "ready": False,
        "active_intent_id": intent.intent_id,
        "sequence": command.pending.state.sequence,
        "head_digest": command.pending.state.head_digest,
    }
    _patch(tx, "AuthorityProjection", projection, updated)
    return _fact(row, updated)


def _proof(binding, row, projection, world):
    return BarrierProof(
        binding.lifetime_id,
        binding.operation_id,
        projection["installation_id"],
        binding.world_id,
        row["id"],
        row["intent_digest"],
        row["attempt_id"],
        row["attempt_fence"],
        row["barrier_id"],
        row["barrier_fence"],
        projection["installation_epoch"],
        projection["authority_epoch"],
        projection["erasure_revision"],
        projection["state_digest"],
        projection_binding(projection).ledger,
        world["sequence"],
        world["generation"],
    )


def inspect_outcome_in_tx(tx, command):
    if type(command) is not InspectionCommand:
        refuse("binding")
    owner = check_command(command, tx)
    world = _world(tx, owner)
    projection = _projection(tx, owner)
    row = _operation(tx, owner, command.intent_id)
    if row is None:
        return _fact(None, projection)
    if row["intent_digest"] != command.intent_digest:
        refuse("conflict", "outcome")
    owner._validate_result_in_tx(tx, row, projection)
    if row.get("outcome"):
        return _fact(row, projection)
    barrier = owner._barrier_command
    if (
        row["phase"] == "fenced"
        and barrier is not None
        and row["barrier_id"] == barrier.barrier_id
        and row["attempt_id"] == barrier.old_attempt_id
        and row["attempt_fence"] == barrier.old_fence
        and row["barrier_fence"] == projection["fence"]
        and projection["active_intent_id"] == command.intent_id
    ):
        _base(projection, barrier.prepare.payload.intent)
        owner._validate_in_tx(tx, barrier.prepare, "barrier")
        return _fact(
            row, projection, "NOT_APPLIED_PROVEN", _proof(command.binding, row, projection, world)
        )
    return _fact(row, projection)


def fence_attempt_in_tx(tx, command):
    if type(command) is not BarrierCommand:
        refuse("binding")
    owner = check_command(command, tx)
    _world(tx, owner)
    projection = _projection(tx, owner)
    row = _operation(tx, owner, command.prepare.payload.intent.intent_id)
    if row is None:
        return _fact(None, projection)
    _same_intent(row, command, owner)
    if row.get("outcome"):
        owner._validate_result_in_tx(tx, row, projection)
        return _fact(row, projection)
    if (
        row["phase"] == "fenced"
        and row["barrier_id"] == command.barrier_id
        and row["barrier_fence"] == projection["fence"]
    ):
        return _fact(row, projection)
    if (
        row["phase"],
        row["attempt_id"],
        row["attempt_fence"],
        projection["fence"],
        projection["active_intent_id"],
    ) != ("active", command.old_attempt_id, command.old_fence, command.old_fence, row["id"]):
        refuse("conflict", "outcome")
    _base(projection, command.prepare.payload.intent)
    owner._validate_in_tx(tx, command.prepare, "barrier")
    fence = _increment(projection["fence"])
    updated = {
        **row,
        "phase": "fenced",
        "barrier_id": command.barrier_id,
        "barrier_fence": fence,
        "revoked_through": command.old_fence,
    }
    _patch(tx, "AuthorityOperation", row, updated)
    p = {**projection, "fence": fence, "ready": False}
    _patch(tx, "AuthorityProjection", projection, p)
    # A callback return is not a confirmed commit. Only a fresh inspection can prove absence.
    return _fact(updated, p)


def validate_approval_in_tx(tx, binding, approval, *, historical=False):
    owner = check_binding(binding, tx)
    access = owner._approval_in_tx(tx, approval, historical=historical)
    if type(access) is not ApprovalAccess or access.approval != approval:
        refuse("binding", "outcome")
    rows = tx.run("MATCH (n:ForgetApproval {id: $id}) RETURN n", id=approval.approval_id)
    if len(rows) != 1:
        refuse("binding", "outcome")
    row = rows[0]["n"]
    if (row.get("user_id"), row.get("target_digest"), row.get("used_at")) != (
        approval.user_id,
        access.target_digest,
        None,
    ):
        refuse("conflict", "outcome")
    if not historical and (
        access.token is None
        or row.get("expires_at") is None
        or row["expires_at"] <= instant(binding.at)
    ):
        refuse("binding", "outcome")
    uses = tx.run(
        "MATCH (n:AuthorityApprovalUse {approval_id: $id}) RETURN n", id=approval.approval_id
    )
    if uses:
        refuse("conflict", "outcome")
    return access


def _use(tx, binding, intent, approval, access, *, role, kind):
    owner = check_binding(binding, tx)
    expected = intent.authorization if role == "original" else owner._renewed_approval
    if role not in {"original", "renewed"} or approval != expected:
        refuse("binding", "outcome")
    _capacity(tx, uses=1)
    row = {
        "id": approval.approval_id,
        "approval_id": approval.approval_id,
        "world_id": binding.world_id,
        "user_id": approval.user_id,
        "installation_id": owner.bindings.installation_id,
        "intent_id": intent.intent_id,
        "choice_digest": approval.payload_digest,
        "display_digest": approval.display_digest,
        "installation_epoch": intent.expected_installation_epoch,
        "authority_epoch": intent.expected_authority_epoch,
        "use_kind": kind,
        "role": role,
        "used_at": instant(binding.at),
    }
    validate_row("use", row)
    tx.run(
        "MATCH (n:ForgetApproval {id: $id}) SET n.used_at = $at",
        id=approval.approval_id,
        at=instant(binding.at),
    )
    tx.run(
        "CREATE (n:AuthorityApprovalUse) SET n += $row",
        row={k: v for k, v in row.items() if k != "world_id"},
    )


def consume_authority_approval_in_tx(
    tx, binding, intent, approval, *, role="original", original=None
):
    from grove.writer.forget_approvals import consume_approval

    if original is not None:
        if (
            role != "renewed"
            or original != intent.authorization
            or approval.approval_id == original.approval_id
            or (approval.user_id, approval.payload_digest, approval.expected_revision)
            != (original.user_id, original.payload_digest, original.expected_revision)
            or instant(approval.approved_at) <= instant(original.approved_at)
        ):
            refuse("binding", "outcome")
        _capacity(tx, uses=2)
        validate_approval_in_tx(tx, binding, original, historical=True)
    access = validate_approval_in_tx(tx, binding, approval)
    consumed = consume_approval(tx, access.token, access.target_digest, approval.user_id)
    if consumed != approval.approval_id:
        refuse("conflict", "outcome")
    # Preserve the existing helper's current expiry check. Persist the fixed
    # operation stamp so managed retries cannot choose another effect time.
    _use(tx, binding, intent, approval, access, role=role, kind="applied")
    if original is not None:
        burn_authority_approval_in_tx(tx, binding, intent, original, kind="applied")


def burn_authority_approval_in_tx(
    tx, binding, intent, approval, *, role="original", kind="cancelled"
):
    access = validate_approval_in_tx(tx, binding, approval, historical=True)
    _use(tx, binding, intent, approval, access, role=role, kind=kind)


def _terminal(tx, owner, command, row, projection, world_before, *, cancelled=False):
    target, revision = owner._terminal_target(command.prepare, cancelled=cancelled)
    intent = command.prepare.payload.intent
    resolution = "cancel_unapplied_expansion" if cancelled else owner._resolution
    world_after = _world(tx, owner)
    terminal = {
        **row,
        "phase": "cancelled" if cancelled else "applied",
        "outcome": "CANCELLED_EXACT" if cancelled else "COMMITTED_EXACT",
        "result_state_digest": target.state_digest,
        "result_installation_epoch": target.installation_epoch,
        "result_authority_epoch": target.authority_epoch,
        "result_erasure_revision": revision,
        "result_ledger_format": target.ledger.format,
        "result_ledger_file_digest": target.ledger.file_digest,
        "result_ledger_rows_digest": target.ledger.rows_digest,
        "result_ledger_count": target.ledger.count,
        "d3_sequence_before": world_before["sequence"],
        "d3_sequence_after": world_after["sequence"],
        "terminal_at": instant(command.binding.at),
        "resolution": resolution,
        "renewed_approval_id": owner._renewed_approval.approval_id
        if owner._renewed_approval
        else None,
    }
    validate_row("operation", terminal)
    updated = {
        **projection,
        "state_digest": target.state_digest,
        "installation_epoch": target.installation_epoch,
        "authority_epoch": target.authority_epoch,
        "erasure_revision": revision,
        "ledger_format": target.ledger.format,
        "ledger_file_digest": target.ledger.file_digest,
        "ledger_rows_digest": target.ledger.rows_digest,
        "ledger_count": target.ledger.count,
        "ready": False,
        "active_intent_id": intent.intent_id,
    }
    _patch(tx, "AuthorityOperation", row, terminal)
    _patch(tx, "AuthorityProjection", projection, updated)
    return _fact(terminal, updated)


def apply_effects_in_tx(tx, command):
    if type(command) is not ApplyCommand:
        refuse("binding")
    owner = check_command(command, tx)
    world = _world(tx, owner)
    projection = _projection(tx, owner)
    intent = command.prepare.payload.intent
    row = _operation(tx, owner, intent.intent_id)
    if row is None:
        refuse("conflict", "outcome")
    _same_intent(row, command, owner)
    if row.get("outcome"):
        owner._validate_result_in_tx(tx, row, projection)
        return _fact(row, projection)
    if (
        row["phase"],
        row["attempt_id"],
        row["attempt_fence"],
        row["operation_id"],
        projection["fence"],
        projection["active_intent_id"],
    ) != (
        "active",
        command.attempt_id,
        command.fence,
        command.binding.operation_id,
        command.fence,
        intent.intent_id,
    ) or row["revoked_through"] >= command.fence:
        refuse("conflict", "outcome")
    _base(projection, intent)
    if (
        len(intent.effects) != 1
        or intent.effects[0].kind != "forget_apply"
        or intent.effects[0].row.plan_version not in {"legacy", "v2"}
    ):
        refuse("adapter_missing", "outcome")
    if (
        intent.authorization.kind != "owner_approval"
        or classify_effects(intent.effects) != "restrictive"
    ):
        refuse("binding", "outcome")
    owner._validate_in_tx(tx, command.prepare, "apply")
    historical = owner._resolution == "complete_restriction"
    validate_approval_in_tx(tx, command.binding, intent.authorization, historical=historical)
    from grove.writer.authority_forget import LegacyPlanEvidence, apply_legacy_forget_in_tx

    if type(command.legacy) is not LegacyPlanEvidence or command.legacy.effect != intent.effects[0]:
        refuse("binding", "outcome")
    apply_legacy_forget_in_tx(tx, command.binding, command.legacy)
    if historical:
        burn_authority_approval_in_tx(
            tx, command.binding, intent, intent.authorization, kind="applied"
        )
    else:
        consume_authority_approval_in_tx(tx, command.binding, intent, intent.authorization)
    return _terminal(tx, owner, command, row, projection, world)


def cancel_intent_in_tx(tx, command):
    if type(command) is not CancelCommand or type(command.proof) is not BarrierProof:
        refuse("binding")
    owner = check_command(command, tx)
    world = _world(tx, owner)
    projection = _projection(tx, owner)
    intent, proof = command.prepare.payload.intent, command.proof
    row = _operation(tx, owner, intent.intent_id)
    if row is None:
        refuse("conflict", "outcome")
    _same_intent(row, command, owner)
    if row.get("outcome"):
        if row["outcome"] != "CANCELLED_EXACT":
            refuse("conflict", "outcome")
        owner._validate_result_in_tx(tx, row, projection)
        return _fact(row, projection)
    if (
        row["phase"] != "fenced"
        or projection["fence"] != proof.barrier_fence
        or proof != _proof(command.binding, row, projection, world)
    ):
        refuse("conflict", "outcome")
    _base(projection, intent)
    event = command.resolution
    if (
        intent.authorization.kind != "owner_approval"
        or not _pure_expansion(intent.effects)
        or event.kind != "recover"
        or event.payload.resolution != "cancel_unapplied_expansion"
        or event.payload.observed_db_digest != intent.base_state_digest
    ):
        refuse("adapter_missing", "outcome")
    owner._validate_in_tx(tx, command.prepare, "cancel")
    burn_authority_approval_in_tx(tx, command.binding, intent, intent.authorization)
    return _terminal(tx, owner, command, row, projection, world, cancelled=True)


def _terminal_agrees(row, target, revision):
    if (
        row["result_state_digest"],
        row["result_installation_epoch"],
        row["result_authority_epoch"],
        row["result_erasure_revision"],
        row["result_ledger_format"],
        row["result_ledger_file_digest"],
        row["result_ledger_rows_digest"],
        row["result_ledger_count"],
    ) != (
        target.state_digest,
        target.installation_epoch,
        target.authority_epoch,
        revision,
        target.ledger.format,
        target.ledger.file_digest,
        target.ledger.rows_digest,
        target.ledger.count,
    ):
        refuse("conflict", "metadata")


def finalize_projection_in_tx(tx, command):
    if type(command) is not FinalizeCommand:
        refuse("binding")
    owner = check_command(command, tx)
    _world(tx, owner)
    projection = _projection(tx, owner)
    row = _operation(tx, owner, command.pending.intent_id)
    if (
        row is None
        or row.get("outcome") not in {"COMMITTED_EXACT", "CANCELLED_EXACT"}
        or row["intent_digest"] != command.pending.intent_digest
    ):
        refuse("outcome_unknown", "metadata")
    if projection["fence"] != command.expected_fence:
        refuse("conflict", "metadata")
    owner._validate_finalization_in_tx(tx, command, row, projection)
    target = command.target
    _terminal_agrees(row, target, projection["erasure_revision"])
    completion = command.completion
    if completion.kind not in {"commit", "recover"} or (
        completion.payload.completion.intent_id,
        completion.payload.completion.intent_digest,
        completion.sequence,
        complete_event_digest(completion),
    ) != (row["id"], row["intent_digest"], target.sequence, target.head_digest):
        refuse("conflict", "metadata")
    if projection_binding(projection) == target and projection["active_intent_id"] is None:
        return _fact(row, projection)
    if projection["active_intent_id"] != row["id"] or projection["sequence"] >= target.sequence:
        refuse("conflict", "metadata")
    updated = {
        **projection,
        "sequence": target.sequence,
        "head_digest": target.head_digest,
        "ready": target.ready,
        "active_intent_id": None,
    }
    _patch(tx, "AuthorityProjection", projection, updated)
    return _fact(row, updated)


def finalize_seal_in_tx(tx, command):
    if type(command) is not SealCommand or command.seal.kind != "seal":
        refuse("binding")
    owner = check_command(command, tx)
    _world(tx, owner)
    projection = _projection(tx, owner)
    owner._validate_finalization_in_tx(tx, command, None, projection)
    target, before = command.target, command.before
    if (
        command.expected_fence != projection["fence"]
        or target.ready
        or target.sequence != command.seal.sequence
        or target.head_digest != complete_event_digest(command.seal)
        or replace(
            target, sequence=before.sequence, head_digest=before.head_digest, ready=before.ready
        )
        != before
    ):
        refuse("conflict", "metadata")
    if projection_binding(projection) == target:
        return _fact(None, projection, "COMMITTED_EXACT")
    if projection_binding(projection) != before or target.sequence != before.sequence + 1:
        refuse("conflict", "metadata")
    updated = {
        **projection,
        "sequence": target.sequence,
        "head_digest": target.head_digest,
        "ready": False,
    }
    _patch(tx, "AuthorityProjection", projection, updated)
    return _fact(None, updated, "COMMITTED_EXACT")
