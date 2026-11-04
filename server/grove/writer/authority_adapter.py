"""Explicitly constructed, inactive owner for the local authority publisher.

The five services are trusted actual owners, not caller-supplied permissions.
There are no default services, environment activation switches or registrations.
Tests provide synthetic services explicitly; that does not establish live consent.
"""

import os
import threading
from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import uuid4

from grove import clock
from grove.authority.codec import (
    DecoderBudget,
    complete_event_digest,
    decode_manifest,
    encode_event,
    journal_event,
    scan_journal,
)
from grove.authority.legacy import decode_legacy
from grove.authority.publication import (
    CheckObservation,
    ObservedFile,
    OutcomeObservation,
    PendingPublication,
    ProposedPublication,
    PublicationRefused,
    UntrustedFileState,
    _requirements,
    refuse,
)
from grove.authority.records import PROFILE, TYPES, check_scalar
from grove.authority.transitions import _pure_expansion, reduce_journal
from grove.writer import authority_outcomes as kernel
from grove.writer.authority_records import (
    ApplyCommand,
    AttemptCommand,
    BarrierCommand,
    CancelCommand,
    FinalizeCommand,
    InspectionCommand,
    RuntimeBinding,
    SealCommand,
    check_binding,
    codec_record,
    copy_record,
    projection_binding,
)
from grove.writer.tx import WorldTx, WorldWriter


@dataclass(frozen=True, slots=True)
class AuthorityOwners:
    """Required independent resource owners. No owner is optional."""

    installation: object
    principal: object
    references: object
    journal: object
    workers: object

    def __post_init__(self):
        required = {
            "installation": ("validate_binding", "validate_projection_in_tx", "resolve"),
            "principal": ("validate_current", "approval_in_tx", "resolve"),
            "references": (
                "validate_in_tx",
                "validate_legacy_in_tx",
                "validate_result_in_tx",
                "resolve",
            ),
            "journal": ("capture", "confirm", "resolve"),
            "workers": ("operation", "resolve"),
        }
        for name, methods in required.items():
            owner = getattr(self, name)
            if owner is None or not all(
                callable(getattr(owner, method, None)) for method in methods
            ):
                refuse("adapter_missing", "open")


@dataclass(frozen=True, slots=True, repr=False)
class CapturedJournal:
    """An actual journal owner's bounded capture; still untrusted until decoded."""

    manifest: bytes
    journal: bytes
    legacy: bytes
    files: tuple

    def __post_init__(self):
        for raw, bound in (
            (self.manifest, 1048576),
            (self.journal, 16000000),
            (self.legacy, 16000000),
        ):
            if type(raw) is not bytes or not 0 < len(raw) <= bound:
                refuse("capacity")
        if type(self.files) is not tuple or len(self.files) != 3:
            refuse("binding")
        for role, raw, observed in zip(
            ("manifest", "journal", "legacy"),
            (self.manifest, self.journal, self.legacy),
            self.files,
            strict=True,
        ):
            if (
                type(observed) is not ObservedFile
                or observed.role != role
                or observed.digest != sha256(raw).hexdigest()
            ):
                refuse("binding")


@dataclass(frozen=True, slots=True)
class _View:
    state: UntrustedFileState
    last: object
    original: object | None


def _append(journal, additions, budget):
    chunks = [b'{"events":[']
    size = len(chunks[0])
    for index in range(journal.metrics.events + len(additions)):
        event = (
            journal_event(journal, index)
            if index < journal.metrics.events
            else additions[index - journal.metrics.events]
        )
        if index:
            chunks.append(b",")
            size += 1
        for chunk in encode_event(event, budget=budget):
            size += len(chunk)
            if size > 15998000:
                refuse("capacity")
            chunks.append(chunk)
    h = journal.header
    chunks.append(
        (
            '],"format":"grove.authority-journal.v1","genesis_digest":"'
            + h.genesis_digest
            + '","installation_id":"'
            + h.installation_id
            + '","owner_id":"'
            + h.owner_id
            + '","schema_profile":{"code":"'
            + h.schema_profile.code
            + '","digest":"'
            + h.schema_profile.digest
            + '"},"world_id":"'
            + h.world_id
            + '"}'
        ).encode("ascii")
    )
    return b"".join(chunks)


class _Lifetime:
    def __init__(self, owner, context, world, kind):
        self.owner, self.context, self.world, self.kind = owner, context, world, kind
        self.lifetime_id, self.operation_id = str(uuid4()), str(uuid4())
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.closed = False
        self.worker = None

    def __enter__(self):
        self.worker = self.context.__enter__()
        if not all(
            callable(getattr(self.worker, name, None)) for name in ("assert_current", "drain")
        ):
            refuse("adapter_missing", "open")
        self.owner._binding = RuntimeBinding(
            self.owner,
            self.lifetime_id,
            self.operation_id,
            self.world,
            clock.now(self.world).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        return self

    def assert_current(self, bindings, world_id, operation_kind):
        if self.closed or (
            os.getpid(),
            threading.get_ident(),
            world_id,
            operation_kind,
            bindings,
        ) != (
            self.pid,
            self.thread,
            self.world,
            self.kind,
            self.owner.bindings,
        ):
            refuse("binding")
        self.worker.assert_current(bindings, world_id, operation_kind)

    def __exit__(self, exc_type, exc, tb):
        # A failed drain leaves the session and EX lease retained for its owner.
        if self.worker.drain() is not None:
            refuse("outcome_unknown", "close")
        self.context.__exit__(exc_type, exc, tb)
        self.closed = True
        self.owner._commands.clear()


class InactiveAuthorityWriter:
    """One operation and one concrete EX session, with no production factory.

    Pass the complete proposal and exact legacy before/after bytes up front.
    bind_writer performs read-only preflight before any stage_legacy call.
    Actual installation, principal, lineage, journal and worker owners remain required.
    """

    def __init__(
        self,
        driver,
        *,
        owners,
        world_id,
        proposal,
        legacy_before=None,
        legacy_after=None,
        operation_kind="publication",
    ):
        if type(owners) is not AuthorityOwners or operation_kind not in {"publication", "recovery"}:
            refuse("adapter_missing", "open")
        check_scalar("ID", world_id)
        self._driver, self.owners, self.world_id = driver, owners, world_id
        self.operation_kind = operation_kind
        self._proposal = self._admit(proposal)
        if self._proposal is None:
            refuse("adapter_missing", "open")
        event = (
            self._proposal.initial_event
            if type(self._proposal) is ProposedPublication
            else self._proposal
        )
        if event.world_id != world_id:
            refuse("binding", "open")
        self._legacy_before, self._legacy_after = legacy_before, legacy_after
        if event.kind == "prepare" and event.payload.intent.effects[0].kind == "forget_apply":
            for raw in (legacy_before, legacy_after):
                if type(raw) is not bytes or not 0 < len(raw) <= 16000000:
                    refuse("capacity", "open")
        elif legacy_before is not None or legacy_after is not None:
            refuse("binding", "open")
        self._session = self._session_id = self.bindings = self._binding = self._lifetime = None
        self._owner_id = self._capture = self._current = self._target = self._native = None
        self._legacy = self._prepare = self._completion = None
        self._commands = {}
        self._attempt_id, self._barrier_id = str(uuid4()), str(uuid4())
        self._barrier_command = self._recovery_proof = None
        self._resolution, self._renewed_approval = "apply", None
        self._issued_prepare = self._final_event = None
        self._prepared = False

    def _admit(self, proposal):
        if type(proposal) is TYPES["Event"]:
            event = codec_record(proposal, "Event")
            if event.kind != "seal":
                refuse("adapter_missing", "open")
            return event
        if type(proposal) is not ProposedPublication:
            refuse("adapter_missing", "open")
        p = ProposedPublication(
            codec_record(proposal.initial_event, "Event"),
            codec_record(proposal.completion_template, "Event"),
            codec_record(proposal.pending_manifest, "Manifest"),
            codec_record(proposal.completed_manifest, "Manifest"),
            codec_record(proposal.seal_event, "Event") if proposal.seal_event is not None else None,
        )
        if p.initial_event.kind != "prepare":
            refuse("adapter_missing", "open")
        intent = p.initial_event.payload.intent
        supported = (
            len(intent.effects) == 1
            and intent.effects[0].kind == "forget_apply"
            and intent.effects[0].row.plan_version in {"legacy", "v2"}
        )
        cancel = (
            self.operation_kind == "recovery"
            and p.completion_template.kind == "recover"
            and p.completion_template.payload.resolution == "cancel_unapplied_expansion"
            and _pure_expansion(intent.effects)
        )
        confirm = (
            self.operation_kind == "recovery"
            and p.completion_template.kind == "recover"
            and p.completion_template.payload.resolution == "confirm_committed"
        )
        if not (supported or cancel or confirm) or intent.authorization.kind != "owner_approval":
            refuse("adapter_missing", "open")
        return p

    def _bind_session(self, session, session_id):
        from grove.authority.publisher import PublicationSession

        if type(session) is not PublicationSession or self._session is not None:
            refuse("binding", "open")
        self._session, self._session_id, self.bindings = session, session_id, session.bindings
        self.owners.installation.validate_binding(self.bindings, self.world_id)

    def operation(self, bindings, world_id, operation_kind):
        if (
            self._session is None
            or self._lifetime is not None
            or (bindings, world_id, operation_kind)
            != (self.bindings, self.world_id, self.operation_kind)
        ):
            refuse("binding", "open")
        context = self.owners.workers.operation(bindings, world_id, operation_kind)
        if not all(callable(getattr(context, n, None)) for n in ("__enter__", "__exit__")):
            refuse("adapter_missing", "open")
        self._lifetime = _Lifetime(self, context, world_id, operation_kind)
        return self._lifetime

    def _assert_live(self):
        if self._binding is None:
            refuse("binding")
        self._session.assert_writer_owner(self, self.world_id, self._binding.operation_id)

    def _assert_binding(self, binding, tx):
        if binding is not self._binding or type(tx) is not WorldTx or tx.world_id != self.world_id:
            refuse("binding", "outcome")
        self._assert_live()
        tx.lock_world()

    def _assert_command(self, command):
        if self._commands.get(id(command)) is not command:
            refuse("binding", "outcome")

    def _issue(self, command):
        if len(self._commands) >= 64:
            refuse("capacity")
        self._commands[id(command)] = command
        return command

    def _run(self, function, command):
        self._assert_live()
        self._issue(command)
        return WorldWriter(self._driver, self.world_id).transaction(
            lambda tx: function(tx, command)
        )

    def _snapshot(self):
        self._assert_live()
        capture = self.owners.journal.capture(self.bindings, self.world_id)
        if type(capture) is not CapturedJournal:
            refuse("binding")
        self.owners.journal.confirm(capture, self.bindings, self.world_id)
        return capture

    def _confirm(self, capture):
        self._assert_live()
        self.owners.journal.confirm(capture, self.bindings, self.world_id)

    def _view(self, capture, *, manifest=None, additions=(), legacy=None):
        if type(capture) is not CapturedJournal or len(additions) > 3:
            refuse("binding")
        with closing(DecoderBudget()) as budget:
            actual_manifest = decode_manifest(capture.manifest, budget=budget)
            selected = actual_manifest if manifest is None else manifest
            if selected is not actual_manifest:
                codec_record(selected, "Manifest")
            for m in (actual_manifest, selected):
                if (
                    m.installation_id,
                    m.activation_id,
                    m.genesis_digest,
                    m.inventory_digest,
                    m.schema_profile,
                ) != (
                    self.bindings.installation_id,
                    self.bindings.activation_id,
                    self.bindings.genesis_digest,
                    self.bindings.inventory_digest,
                    PROFILE,
                ):
                    refuse("binding")
            if selected is not actual_manifest:
                if (
                    selected.installation_epoch != actual_manifest.installation_epoch
                    or selected.state != actual_manifest.state
                    or tuple(h for h in selected.worlds if h.world_id != self.world_id)
                    != tuple(h for h in actual_manifest.worlds if h.world_id != self.world_id)
                ):
                    refuse("conflict")
            journal = scan_journal(capture.journal, budget=budget)
            if (
                journal.header.world_id != self.world_id
                or journal.header.installation_id != self.bindings.installation_id
            ):
                refuse("binding")
            # A selected recovery manifest can be ahead of the actual manifest,
            # but the latter must still describe an exact committed prefix.
            head = next((h for h in actual_manifest.worlds if h.world_id == self.world_id), None)
            if (
                head is None
                or not 1 <= head.sequence <= journal.metrics.events
                or head.head_digest
                != complete_event_digest(journal_event(journal, head.sequence - 1))
            ):
                refuse("conflict")
            if additions:
                raw = _append(journal, additions, budget)
            else:
                raw = capture.journal
            selected = copy_record(selected)
        with closing(DecoderBudget()) as budget:
            # Selected records are detached before the first decoder expires.
            selected = codec_record(selected, "Manifest")
            journal = scan_journal(raw, budget=budget)
            reduced = reduce_journal(journal, manifest=selected, budget=budget)
            original = None
            intent_id = (
                self._proposal.initial_event.payload.intent.intent_id
                if type(self._proposal) is ProposedPublication
                else None
            )
            for index in range(journal.metrics.events):
                event = journal_event(journal, index)
                if event.kind == "prepare" and event.payload.intent.intent_id == intent_id:
                    if original is not None:
                        refuse("conflict")
                    original = copy_record(event)
            last = copy_record(journal_event(journal, journal.metrics.events - 1))
            ledger = copy_record(reduced.ledger)
            projection = TYPES["ProjectionBinding"](
                installation_id=self.bindings.installation_id,
                installation_epoch=reduced.installation_epoch,
                authority_epoch=reduced.authority_epoch,
                sequence=journal.metrics.events,
                head_digest=complete_event_digest(last),
                schema_profile=PROFILE,
                state_digest=reduced.state_digest,
                ledger=ledger,
                ready=reduced.release_state in {"ready", "no_host_policy"},
            )
            state = UntrustedFileState(
                self.world_id,
                journal.header.owner_id,
                reduced.installation_epoch,
                reduced.authority_epoch,
                reduced.erasure_revision,
                journal.metrics.events,
                projection.head_digest,
                reduced.state_digest,
                ledger,
                capture.files,
                reduced.release_state,
                reduced.pending_id,
                tuple(reduced.requirements),
                projection,
            )
        with closing(DecoderBudget()) as budget:
            found = decode_legacy(
                capture.legacy if legacy is None else legacy, world_id=self.world_id, budget=budget
            ).binding
            found = TYPES["Ledger"](
                format=found.format,
                file_digest=found.file_digest,
                rows_digest=found.rows_digest,
                count=found.count,
            )
            permitted = None
            if original is not None:
                effects = original.payload.intent.effects
                if len(effects) == 1 and effects[0].kind == "forget_apply" and state.pending_id:
                    permitted = effects[0].after_ledger
            if found != ledger and found != permitted:
                refuse("conflict")
        return _View(state, last, original)

    def _current_view(self, capture):
        """Select an exact recovery prefix without accepting an arbitrary tail."""
        with closing(DecoderBudget()) as budget:
            manifest = decode_manifest(capture.manifest, budget=budget)
            journal = scan_journal(capture.journal, budget=budget)
            head = next((h for h in manifest.worlds if h.world_id == self.world_id), None)
            if head is None:
                refuse("binding")
            count = journal.metrics.events
            last = copy_record(journal_event(journal, count - 1))
            caught_up = head.sequence == count
        if caught_up:
            return self._view(capture)
        if self.operation_kind != "recovery" or self._prepare is None:
            refuse("recovery_required")
        pending_count = self._prepare.sequence + int(self._proposal.seal_event is not None)
        if count == pending_count:
            selected = self._proposal.pending_manifest
            expected = self._proposal.seal_event or self._prepare
            if last != expected:
                refuse("conflict")
        elif count == pending_count + 1:
            if last.kind not in {"commit", "recover"}:
                refuse("conflict")
            normalized = replace(
                last,
                payload=replace(
                    last.payload,
                    completion=replace(
                        last.payload.completion,
                        completed_at=self._completion.payload.completion.completed_at,
                    ),
                ),
                digest=self._completion.digest,
            )
            if normalized != self._completion:
                refuse("conflict")
            selected = self._proposal.completed_manifest
            from grove.authority.codec import manifest_checksum

            selected = replace(
                selected,
                worlds=tuple(
                    replace(h, head_digest=complete_event_digest(last))
                    if h.world_id == self.world_id
                    else h
                    for h in selected.worlds
                ),
            )
            selected = replace(selected, checksum=manifest_checksum(selected))
        else:
            refuse("conflict")
        return self._view(capture, manifest=selected)

    def _resolve_requirements(self, view):
        requirements = view.state.requirements
        _requirements(requirements)
        if len(set(requirements)) != len(requirements):
            refuse("binding")
        mapping = {
            "trusted_genesis": "installation",
            "controller_receipt": "installation",
            "current_principal": "principal",
            "owner_approval": "principal",
            "renewed_approval": "principal",
            "source_origin": "references",
            "observed_lineage": "references",
            "effect_references": "references",
            "database_outcome": "references",
            "current_projection": "references",
            "ledger_content": "journal",
            "service_configuration": "workers",
        }
        if any(r.code not in mapping for r in requirements):
            refuse("adapter_missing")
        for name in ("installation", "principal", "references", "journal", "workers"):
            expected = tuple(r for r in requirements if mapping[r.code] == name)
            observed = getattr(self.owners, name).resolve(expected, self.bindings, view.state)
            if type(observed) is not tuple or observed != expected:
                refuse("binding")
        return requirements

    def _read_native(self, tx):
        check_binding(self._binding, tx)
        kernel._world(tx, self)
        row = kernel._projection(tx, self)
        return tuple(sorted(row.items()))

    def _preflight_bind(self):
        """Read-only admission before the bound session can stage any bytes."""
        capture = self._snapshot()
        if type(self._proposal) is ProposedPublication:
            self._prepare = self._proposal.initial_event
            self._completion = self._proposal.completion_template
            if self._completion.kind == "recover":
                self._resolution = self._completion.payload.resolution
                self._renewed_approval = self._completion.payload.renewed_approval
        view = self._current_view(capture)
        self._owner_id, self._capture, self._current = view.state.owner_id, capture, view
        self._resolve_requirements(view)
        self.owners.principal.validate_current(None, view.state, self._proposal)
        projection = dict(WorldWriter(self._driver, self.world_id).transaction(self._read_native))
        self._native = kernel._fact(None, projection)
        if self._prepare is None:
            if projection_binding(projection) != view.state.projection:
                refuse("conflict")
            self._confirm(capture)
            self._prepared = True
            return
        if view.original is not None and view.original != self._prepare:
            refuse("conflict")
        intent = self._prepare.payload.intent
        if view.original is None:
            if (
                self.operation_kind != "publication"
                or projection_binding(projection) != view.state.projection
            ):
                refuse("conflict")
            additions = (
                (self._prepare,)
                + ((self._proposal.seal_event,) if self._proposal.seal_event else ())
                + (self._completion,)
            )
            self._target = self._view(
                capture,
                manifest=self._proposal.completed_manifest,
                additions=additions,
                legacy=self._legacy_after,
            )

            def capacity(tx):
                check_binding(self._binding, tx)
                kernel._capacity(tx, operation=1, uses=1)

            WorldWriter(self._driver, self.world_id).transaction(capacity)
        else:
            if view.state.pending_id == intent.intent_id:
                self._target = self._view(
                    capture,
                    manifest=self._proposal.completed_manifest,
                    additions=(self._completion,),
                    legacy=self._legacy_after,
                )
            else:
                self._target = view
            self._native = self._inspect(view.state.projection)
            if view.state.pending_id is None and self._native.outcome not in {
                "COMMITTED_EXACT",
                "CANCELLED_EXACT",
            }:
                refuse("outcome_unknown")
        if (
            self._native.outcome not in {"COMMITTED_EXACT", "CANCELLED_EXACT"}
            and intent.effects[0].kind == "forget_apply"
        ):
            from grove.writer.authority_forget import prepare_legacy_forget

            self._legacy = WorldWriter(self._driver, self.world_id).transaction(
                lambda tx: prepare_legacy_forget(
                    tx, self._binding, self._legacy_before, self._legacy_after, intent.effects[0]
                )
            )
        self._confirm(capture)
        self._prepared = True

    def _check(self, state, digest=None, prior=None):
        self._assert_live()
        if (
            not self._prepared
            or state.world_id != self.world_id
            or state.owner_id != self._owner_id
        ):
            refuse("binding")
        _requirements(state.requirements)
        return CheckObservation(
            self._binding.lifetime_id,
            self._binding.operation_id,
            self.bindings,
            state,
            state.requirements,
            digest,
            prior,
        )

    def check_current(self, state, proposal):
        self._assert_live()
        if proposal is not None and proposal != self._proposal:
            refuse("binding")
        capture = self._snapshot()
        view = self._view(capture)
        if view.state != state:
            refuse("conflict")
        self._capture, self._current = capture, view
        self._resolve_requirements(view)
        self.owners.principal.validate_current(None, state, self._proposal)
        digest = (
            None
            if proposal is None
            else (
                self._prepare.payload.intent.intent_digest
                if self._prepare
                else self._proposal.digest
            )
        )
        return self._check(state, digest)

    def check_legacy_extension(self, pending, before, after, row):
        self._assert_live()
        if (
            self._legacy is None
            or pending.session_id != self._session_id
            or pending.operation_id != self._binding.operation_id
        ):
            refuse("binding")
        effect = self._legacy.effect
        if (before, after) != (effect.before_ledger, effect.after_ledger) or any(
            getattr(row, key) != getattr(effect.row, key)
            for key in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
        ):
            refuse("conflict")
        self._resolve_requirements(_View(pending.state, None, self._prepare))
        return self._check(pending.state, self._prepare.payload.intent.intent_digest)

    def _inspect(self, projection):
        intent = self._prepare.payload.intent
        return self._run(
            kernel.inspect_outcome_in_tx,
            InspectionCommand(self._binding, intent.intent_id, intent.intent_digest, projection),
        )

    def _observation(self, pending, native, state):
        return OutcomeObservation(
            self._binding.lifetime_id,
            self._binding.operation_id,
            self.bindings,
            self.world_id,
            pending.intent_id,
            pending.intent_digest,
            native.outcome,
            self._owner_id,
            native.erasure_revision,
            native.projection.state_digest,
            native.projection,
            native.projection.ledger,
            state.requirements,
        )

    def resolve_outcome(self, pending):
        self._assert_pending(pending)
        capture = self._snapshot()
        view = self._current_view(capture)
        if view.original != self._prepare:
            refuse("conflict")
        self._capture, self._current = capture, view
        self._resolve_requirements(view)
        if pending.next_stage == "retry":
            native = self._inspect(view.state.projection)
            self._confirm(capture)
            return self._observation(pending, native, view.state)
        if self._legacy is not None and capture.legacy != self._legacy_after:
            refuse("conflict", "outcome")
        try:
            native = self._inspect(view.state.projection)
            if native.outcome not in {"COMMITTED_EXACT", "CANCELLED_EXACT"}:
                if self._resolution == "cancel_unapplied_expansion":
                    if self._recovery_proof is None:
                        refuse("outcome_unknown", "outcome")
                    native = self._run(
                        kernel.cancel_intent_in_tx,
                        CancelCommand(
                            self._binding,
                            self._prepare,
                            self._completion,
                            pending,
                            self._recovery_proof,
                        ),
                    )
                elif self._resolution == "confirm_committed":
                    refuse("outcome_unknown", "outcome")
                else:
                    if self._issued_prepare is None:
                        self._issued_prepare = AttemptCommand(
                            self._binding,
                            self._prepare,
                            pending,
                            native.projection,
                            native.fence,
                            self._attempt_id,
                            self._binding.operation_id,
                        )
                    native = self._run(kernel.register_attempt_in_tx, self._issued_prepare)
                    if native.outcome not in {"COMMITTED_EXACT", "CANCELLED_EXACT"}:
                        native = self._run(
                            kernel.apply_effects_in_tx,
                            ApplyCommand(
                                self._binding,
                                self._prepare,
                                pending,
                                self._attempt_id,
                                native.fence,
                                self._legacy,
                            ),
                        )
            self._native = native
        except PublicationRefused:
            raise
        except Exception:
            # A transport failure is not rollback evidence. Recovery needs a new locked read.
            native = replace(self._native, outcome="UNKNOWN", proof=None)
        self._confirm(capture)
        return self._observation(pending, native, self._target.state)

    def finalize_projection(self, pending, exact_target_projection):
        self._assert_pending(pending, seal=self._prepare is None)
        capture = self._snapshot()
        # The completion journal is already durable; the manifest still names its prefix.
        selected = self._proposal.completed_manifest if self._prepare else None
        if selected is not None:
            # Completion timestamps come from the publisher. Reconstruct only the
            # selected head from the actual final event, never from the template hash.
            with closing(DecoderBudget()) as budget:
                journal = scan_journal(capture.journal, budget=budget)
                last = copy_record(journal_event(journal, journal.metrics.events - 1))
            from grove.authority.codec import manifest_checksum

            selected = replace(
                selected,
                worlds=tuple(
                    replace(h, head_digest=complete_event_digest(last))
                    if h.world_id == self.world_id
                    else h
                    for h in selected.worlds
                ),
            )
            selected = replace(selected, checksum=manifest_checksum(selected))
        else:
            # Seal metadata uses its actual current journal and caller target.
            selected = self._manifest_for_seal(capture, exact_target_projection)
        view = self._view(capture, manifest=selected)
        if view.state.projection != exact_target_projection:
            refuse("conflict", "metadata")
        self._capture, self._current, self._final_event = capture, view, view.last
        self._resolve_requirements(view)
        if self._prepare is None:
            command = SealCommand(
                self._binding,
                view.last,
                pending.state.projection,
                exact_target_projection,
                self._native.fence,
            )
            native = self._run(kernel.finalize_seal_in_tx, command)
        else:
            command = FinalizeCommand(
                self._binding, pending, view.last, exact_target_projection, self._native.fence
            )
            native = self._run(kernel.finalize_projection_in_tx, command)
        self._native = native
        self._confirm(capture)
        return self._observation(pending, native, view.state)

    def _manifest_for_seal(self, capture, target):
        from grove.authority.codec import manifest_checksum

        with closing(DecoderBudget()) as budget:
            m = copy_record(decode_manifest(capture.manifest, budget=budget))
        m = replace(
            m,
            worlds=tuple(
                replace(
                    h,
                    sequence=target.sequence,
                    head_digest=target.head_digest,
                    release_state="sealed",
                )
                if h.world_id == self.world_id
                else h
                for h in m.worlds
            ),
        )
        return replace(m, checksum=manifest_checksum(m))

    def check_recovery(self, state, operation, resolution_event):
        self._assert_live()
        if self.operation_kind != "recovery" or operation != self._proposal:
            refuse("binding")
        if resolution_event is not None and resolution_event != self._completion:
            refuse("binding")
        self._resolve_requirements(_View(state, None, self._prepare))
        self.owners.principal.validate_current(None, state, operation)
        prior = None
        if resolution_event is not None and self._resolution in {
            "cancel_unapplied_expansion",
            "complete_restriction",
        }:
            pending = PendingPublication(
                self._session_id,
                self.world_id,
                self._prepare.payload.intent.intent_id,
                self._prepare.payload.intent.intent_digest,
                self._binding.operation_id,
                state,
                "outcome",
            )
            native = self._inspect(state.projection)
            if not native.operation:
                # An existing, independently validated projection is required.
                # Register the exact already-published intent before fencing it.
                self._issued_prepare = AttemptCommand(
                    self._binding,
                    self._prepare,
                    pending,
                    native.projection,
                    native.fence,
                    self._attempt_id,
                    self._binding.operation_id,
                )
                native = self._run(kernel.register_attempt_in_tx, self._issued_prepare)
                self._issued_prepare = None
            row = dict(native.operation)
            if native.outcome not in {"COMMITTED_EXACT", "CANCELLED_EXACT"}:
                barrier_id = row["barrier_id"] if row["phase"] == "fenced" else self._barrier_id
                self._barrier_command = BarrierCommand(
                    self._binding,
                    self._prepare,
                    pending,
                    row["attempt_id"],
                    row["attempt_fence"],
                    barrier_id,
                )
                try:
                    self._run(kernel.fence_attempt_in_tx, self._barrier_command)
                except PublicationRefused:
                    raise
                except Exception:
                    # Lost acknowledgement remains unknown until this fresh read.
                    pass
                native = self._inspect(state.projection)
                if native.outcome != "NOT_APPLIED_PROVEN":
                    refuse("outcome_unknown", "recovery")
                self._recovery_proof = native.proof
                self._native = native
            prior = self._observation(pending, native, state)
        return self._check(state, self._prepare.payload.intent.intent_digest, prior)

    def check_final(self, state, exact_target_projection):
        capture = self._snapshot()
        view = self._view(capture)
        if view.state != state or state.projection != exact_target_projection:
            refuse("conflict", "final")
        self._capture, self._current = capture, view
        self._resolve_requirements(view)
        self.owners.principal.validate_current(None, state, self._proposal)
        projection = dict(WorldWriter(self._driver, self.world_id).transaction(self._read_native))
        if (
            projection_binding(projection) != exact_target_projection
            or projection["erasure_revision"] != state.erasure_revision
        ):
            refuse("conflict", "final")
        self._confirm(capture)
        return self._check(state)

    def _assert_pending(self, pending, seal=False):
        self._assert_live()
        if type(pending) is not PendingPublication or (
            pending.session_id,
            pending.world_id,
            pending.operation_id,
        ) != (self._session_id, self.world_id, self._binding.operation_id):
            refuse("binding")
        expected = (
            (self._proposal.event_id, self._proposal.digest)
            if seal
            else (
                self._prepare.payload.intent.intent_id,
                self._prepare.payload.intent.intent_digest,
            )
        )
        if (pending.intent_id, pending.intent_digest) != expected:
            refuse("binding")

    def _assert_original(self, prepare):
        if prepare != self._prepare or self._current.original != prepare:
            refuse("conflict", "outcome")

    def _validate_projection_in_tx(self, tx, row):
        self.owners.installation.validate_projection_in_tx(tx, self.bindings, row)

    def _validate_in_tx(self, tx, prepare, phase):
        self._assert_original(prepare)
        self.owners.principal.validate_current(tx, self._current.state, self._proposal)
        self.owners.references.validate_in_tx(tx, self._current.state, prepare, phase)

    def _validate_legacy_in_tx(self, tx, effect, inventory):
        self.owners.references.validate_legacy_in_tx(tx, self._current.state, effect, inventory)

    def _validate_result_in_tx(self, tx, row, projection):
        if (
            self._prepare is None
            or row["intent_digest"] != self._prepare.payload.intent.intent_digest
        ):
            refuse("conflict", "outcome")
        self.owners.principal.validate_current(tx, self._current.state, self._proposal)
        self.owners.references.validate_result_in_tx(tx, self._current.state, row, projection)

    def _approval_in_tx(self, tx, approval, *, historical):
        return self.owners.principal.approval_in_tx(
            tx, self._current.state, approval, historical=historical
        )

    def _terminal_target(self, prepare, *, cancelled):
        if prepare != self._prepare or cancelled != (
            self._resolution == "cancel_unapplied_expansion"
        ):
            refuse("conflict")
        return self._target.state.projection, self._target.state.erasure_revision

    def _assert_recovery_attempt(self, row, projection, command):
        proof = self._recovery_proof
        if (
            self.operation_kind != "recovery"
            or self._resolution != "complete_restriction"
            or proof is None
            or (
                row["phase"],
                row["barrier_id"],
                row["barrier_fence"],
                projection["fence"],
                command.expected_fence,
            )
            != (
                "fenced",
                proof.barrier_id,
                proof.barrier_fence,
                proof.barrier_fence,
                proof.barrier_fence,
            )
        ):
            refuse("conflict", "outcome")

    def _validate_finalization_in_tx(self, tx, command, row, projection):
        event = command.seal if type(command) is SealCommand else command.completion
        if event != self._final_event or command.target != self._current.state.projection:
            refuse("conflict", "metadata")
        self.owners.principal.validate_current(tx, self._current.state, self._proposal)
        self.owners.references.validate_result_in_tx(tx, self._current.state, row, projection)
