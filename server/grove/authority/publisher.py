"""Inactive, explicitly owned publication for established authority origins.

No factory, database, account, environment fallback, or production registration.
Owner calls are synchronous lifetime boundaries: they must join any owned work
before return or exception. Cancellation cannot close this session from another
thread. Matching codec records remain untrusted throughout this protocol.
"""

import math
import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from functools import wraps
from uuid import uuid4

from grove import clock

from .codec import (
    DecoderBudget,
    complete_event_digest,
    decode_manifest,
    encode_event,
    encode_manifest,
    event_digest,
    journal_event,
    manifest_checksum,
    scan_journal,
)
from .lease import LeaseRefused, open_existing_root
from .legacy import decode_legacy, legacy_row, legacy_row_span
from .publication import (
    CheckObservation,
    LocalPublicationResult,
    OutcomeObservation,
    PendingPublication,
    ProposedPublication,
    PublicationBindings,
    PublicationRefused,
    RecoveryRequirements,
    UntrustedFileState,
    refuse,
)
from .publication_files import PublicationFiles
from .records import PROFILE, SCHEMA_NAMES, SCHEMAS, TYPES, AuthorityCodecRefused, check_scalar
from .scalars import ScalarRefused, validate_identifier
from .transitions import (
    assess_append,
    classify_effects,
    compare_projection,
    lookup_retry,
    reduce_journal,
)

_METHODS = (
    "operation",
    "check_current",
    "check_legacy_extension",
    "resolve_outcome",
    "finalize_projection",
    "check_recovery",
    "check_final",
)


def _cost(value, depth=0):
    """Conservative retained-record admission, before any copied record tree."""
    if depth > 14:
        refuse("capacity")
    if value is None or type(value) in (bool, int):
        return 64
    if type(value) is str:
        if len(value) > 1024:
            refuse("capacity")
        return 128 + len(value) * 4
    if type(value) is tuple:
        if len(value) > 262144:
            refuse("capacity")
        total = 128 + len(value) * 16
        for child in value:
            total += _cost(child, depth + 1)
            if total > 2097152:
                refuse("capacity")
        return total
    name = SCHEMA_NAMES.get(type(value))
    if name is not None:
        return 256 + sum(_cost(getattr(value, key), depth + 1) for key, _ in SCHEMAS[name])
    # VerificationRequirement contains only four bounded scalar fields.
    if type(value).__name__ == "VerificationRequirement":
        return 256 + sum(
            _cost(getattr(value, k), depth + 1)
            for k in ("code", "event_id", "intent_id", "binding_digest")
        )
    refuse("binding")


def _copy_record(value):
    """Detach public fields; no parser span or reservation crosses lifetimes."""
    if type(value) is tuple:
        return tuple(_copy_record(child) for child in value)
    name = SCHEMA_NAMES.get(type(value))
    if name is not None:
        return TYPES[name](**{key: _copy_record(getattr(value, key)) for key, _ in SCHEMAS[name]})
    return value


def _proposal_copy(proposal):
    if type(proposal) is not ProposedPublication:
        refuse("binding")
    values = (
        proposal.initial_event,
        proposal.completion_template,
        proposal.pending_manifest,
        proposal.completed_manifest,
        proposal.seal_event,
    )
    if sum(_cost(value) for value in values) > 1500000:
        refuse("capacity")
    return ProposedPublication(*(_copy_record(value) for value in values))


def _ledger(binding):
    return TYPES["Ledger"](
        format=binding.format,
        file_digest=binding.file_digest,
        rows_digest=binding.rows_digest,
        count=binding.count,
    )


def _completion_key(event):
    completion = replace(event.payload.completion, completed_at="2000-01-01T00:00:00.000000Z")
    return event_digest(replace(event, payload=replace(event.payload, completion=completion)))


def _boundary(stage):
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            self._assert_worker()
            if not self._guard.acquire(blocking=False):
                refuse("binding", stage)
            failure = None
            try:
                self._boundary_check()
                for value in args:
                    if type(value) is ProposedPublication:
                        self._transient += 2 * sum(
                            _cost(v)
                            for v in (
                                value.initial_event,
                                value.completion_template,
                                value.pending_manifest,
                                value.completed_manifest,
                                value.seal_event,
                            )
                        )
                    elif type(value) in SCHEMA_NAMES:
                        self._transient += 2 * _cost(value)
                if self._transient + self._retained > 1900000:
                    refuse("capacity", stage)
                return function(self, *args, **kwargs)
            except PublicationRefused as error:
                failure = error.code
            except (AuthorityCodecRefused, ScalarRefused, TypeError, AttributeError, ValueError):
                failure = "candidate"
            except FileNotFoundError:
                failure = "missing"
            except (OSError, LeaseRefused):
                failure = "io"
            except Exception:
                failure = "binding"
            finally:
                self._transient = 0
                self._guard.release()
            self._failed = True
            # Drop input-bearing exception chains before exposing a fixed code.
            args = kwargs = None
            raise PublicationRefused(failure, stage)

        return call

    return decorate


def open_publication(root, bindings, *, checks=None, deadline):
    """Open existing private resources; no missing-object repair is possible."""
    if checks is None or not all(callable(getattr(checks, method, None)) for method in _METHODS):
        refuse("adapter_missing", "open")
    if type(bindings) is not PublicationBindings:
        refuse("binding", "open")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        refuse("binding", "open")
    session = PublicationSession(checks, bindings, deadline)
    code = None
    try:
        session._lease = open_existing_root(root, bindings.root_identity)
        if session._lease.lock_identity != bindings.lock_identity:
            refuse("binding", "open")
        session._lease.acquire_exclusive(deadline)
        session._files = PublicationFiles(root, bindings, session._lease, session._boundary_check)
        return session
    except PublicationRefused as error:
        code = error.code
    except FileNotFoundError:
        code = "missing"
    except (OSError, LeaseRefused, ScalarRefused, ValueError):
        code = "io"
    try:
        session.close()
    except (PublicationRefused, OSError, LeaseRefused):
        pass
    root = checks = None
    raise PublicationRefused(code, "open")


class PublicationSession:
    def __init__(self, checks, bindings, deadline):
        self._checks, self.bindings, self._deadline = checks, bindings, deadline
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._guard, self._cancelled = threading.Lock(), threading.Event()
        self._lease = self._files = self._lifetime = self._context = None
        self._world = self._kind = self._staged_world = None
        self._closed = self._failed = self._closing = False
        self._session_id = str(uuid4())
        self._pending = self._legacy_candidate = None
        self._expected_files = None
        self._completion_key = self._completed_manifest = self._completion_candidate = None
        self._anticipated_head = None
        self._recovery_expected = None
        self._state = "new"
        self._external_bytes = self._retained = self._transient = 0

    def _assert_worker(self):
        if self._pid != os.getpid() or self._thread != threading.get_ident() or self._closed:
            refuse("binding")

    def _boundary_check(self):
        self._assert_worker()
        if not self._closing and (self._cancelled.is_set() or clock.monotonic() >= self._deadline):
            refuse("cancelled")
        if self._failed and not self._closing:
            refuse("recovery_required")
        if self._lifetime is not None:
            self._lifetime.assert_current(self.bindings, self._world, self._kind)

    def _activate(self, world_id, kind="publication"):
        validate_identifier(world_id)
        if self._staged_world is not None and self._staged_world != world_id:
            refuse("binding")
        if self._world is not None:
            if (world_id, kind) != (self._world, self._kind):
                refuse("binding")
            self._boundary_check()
            return
        context = self._checks.operation(self.bindings, world_id, kind)
        if not all(callable(getattr(context, n, None)) for n in ("__enter__", "__exit__")):
            refuse("adapter_missing")
        lifetime = context.__enter__()
        self._context, self._lifetime = context, lifetime
        self._world, self._kind = world_id, kind
        if not callable(getattr(lifetime, "assert_current", None)):
            refuse("adapter_missing")
        check_scalar("UUID", lifetime.lifetime_id)
        check_scalar("UUID", lifetime.operation_id)
        self._boundary_check()

    @contextmanager
    def _budget(self, extra=0):
        budget = DecoderBudget()
        retained = external = None
        try:
            retained = budget.reserve(
                "metadata", self._retained + max(extra, self._transient) + 131072
            )
            external = budget.reserve("input", self._external_bytes)
            yield budget
        finally:
            if retained is not None:
                retained.release()
            if external is not None:
                external.release()
            budget.close()

    def _manifest_binding(self, manifest):
        for field, expected in (
            ("installation_id", self.bindings.installation_id),
            ("activation_id", self.bindings.activation_id),
            ("genesis_digest", self.bindings.genesis_digest),
            ("inventory_digest", self.bindings.inventory_digest),
            ("schema_profile", self.bindings.profile),
        ):
            if getattr(manifest, field) != expected:
                refuse("binding")

    def _manifest_change(self, before, after, world_id, event):
        self._manifest_binding(after)
        if tuple(h for h in before.worlds if h.world_id != world_id) != tuple(
            h for h in after.worlds if h.world_id != world_id
        ) or len(before.worlds) != len(after.worlds):
            refuse("conflict")
        a = next((h for h in before.worlds if h.world_id == world_id), None)
        b = next((h for h in after.worlds if h.world_id == world_id), None)
        if (
            a is None
            or b is None
            or (a.owner_id, a.origin_event_id, a.origin_digest)
            != (b.owner_id, b.origin_event_id, b.origin_digest)
        ):
            refuse("binding")
        installation_restore = (
            event.kind == "restore_epoch" and event.payload.scope == "installation"
        )
        if installation_restore:
            if (
                after.state != "restoring"
                or after.installation_epoch != event.payload.installation_after
            ):
                refuse("binding")
            if before.installation_epoch not in (
                event.payload.installation_before,
                event.payload.installation_after,
            ):
                refuse("binding")
        elif (before.installation_epoch, before.state) != (after.installation_epoch, after.state):
            refuse("binding")

    def _state_from(self, manifest, journal, reduction, observations):
        last = journal_event(journal, journal.metrics.events - 1)
        projection = TYPES["ProjectionBinding"](
            installation_id=manifest.installation_id,
            installation_epoch=reduction.installation_epoch,
            authority_epoch=reduction.authority_epoch,
            sequence=journal.metrics.events,
            head_digest=complete_event_digest(last),
            schema_profile=PROFILE,
            state_digest=reduction.state_digest,
            ledger=_ledger(reduction.ledger),
            ready=reduction.release_state in ("ready", "no_host_policy"),
        )
        requirements = tuple(reduction.requirements)
        _cost(requirements)
        return UntrustedFileState(
            journal.header.world_id,
            journal.header.owner_id,
            reduction.installation_epoch,
            reduction.authority_epoch,
            reduction.erasure_revision,
            journal.metrics.events,
            projection.head_digest,
            reduction.state_digest,
            projection.ledger,
            tuple(observations),
            reduction.release_state,
            reduction.pending_id,
            requirements,
            projection,
        )

    @contextmanager
    def _load(
        self,
        world_id,
        *,
        manifest=None,
        journal=None,
        legacy=None,
        extra=0,
        allowed_ledger=None,
        barrier_for=None,
    ):
        with self._budget(extra) as budget, ExitStack() as stack:
            mraw, mo = stack.enter_context(
                self._files.read("manifest", world_id, budget, candidate=manifest)
            )
            m = decode_manifest(mraw, budget=budget)
            self._manifest_binding(m)
            jraw, jo = stack.enter_context(
                self._files.read("journal", world_id, budget, candidate=journal)
            )
            j = scan_journal(jraw, budget=budget)
            if j.header.world_id != world_id:
                refuse("binding")
            if (
                barrier_for is not None
                and j.metrics.events == barrier_for.payload.intent.base_sequence
            ):
                if (
                    barrier_for.kind != "restore_epoch"
                    or barrier_for.payload.scope != "installation"
                    or m.state != "restoring"
                    or m.installation_epoch != barrier_for.payload.installation_after
                ):
                    refuse("binding")
                m = replace(
                    _copy_record(m), installation_epoch=barrier_for.payload.installation_before
                )
                m = replace(m, checksum=manifest_checksum(m))
            r = reduce_journal(j, manifest=m, budget=budget)
            lraw, lo = stack.enter_context(
                self._files.read("legacy", world_id, budget, candidate=legacy)
            )
            ledger_view = decode_legacy(lraw, world_id=world_id, budget=budget)
            actual = _ledger(ledger_view.binding)
            if actual != r.ledger:
                permitted = allowed_ledger
                if permitted is None and r.pending_id:
                    intent = self._intent(j, r.pending_id)
                    effects = [e for e in intent.effects if e.kind == "forget_apply"]
                    if len(effects) == 1:
                        permitted = effects[0].after_ledger
                if permitted is None or actual != permitted:
                    refuse("conflict")
            state = self._state_from(m, j, r, (mo, jo, lo))
            yield budget, m, j, r, ledger_view, state

    def _intent(self, journal, intent_id):
        # Only a pending last prepare or its optional fixed seal is admissible.
        for index in range(journal.metrics.events - 1, max(-1, journal.metrics.events - 4), -1):
            event = journal_event(journal, index)
            if (
                event.kind in ("prepare", "restore_epoch", "world_birth", "adopt_world")
                and event.payload.intent.intent_id == intent_id
            ):
                return _copy_record(event.payload.intent)
        refuse("conflict")

    def _check_observation(self, value, state, intent_digest=None):
        self._boundary_check()
        if type(value) is not CheckObservation or (
            value.lifetime_id,
            value.operation_id,
            value.bindings,
            value.state,
            value.requirements,
            value.intent_digest,
        ) != (
            self._lifetime.lifetime_id,
            self._lifetime.operation_id,
            self.bindings,
            state,
            state.requirements,
            intent_digest,
        ):
            refuse("binding")

    def _check_outcome(self, value, pending, state, projection, outcomes):
        self._boundary_check()
        if type(value) is not OutcomeObservation:
            refuse("outcome_unknown", "outcome")
        if value.outcome == "UNKNOWN":
            refuse("outcome_unknown", "outcome")
        if value.outcome not in outcomes or (
            value.lifetime_id,
            value.operation_id,
            value.bindings,
            value.world_id,
            value.intent_id,
            value.intent_digest,
            value.owner_id,
            value.erasure_revision,
            value.state_digest,
            value.projection,
            value.ledger,
            value.requirements,
        ) != (
            self._lifetime.lifetime_id,
            self._lifetime.operation_id,
            self.bindings,
            state.world_id,
            pending.intent_id,
            pending.intent_digest,
            state.owner_id,
            state.erasure_revision,
            state.state_digest,
            projection,
            state.ledger,
            state.requirements,
        ):
            refuse("conflict", "outcome")

    def _journal_chunks(self, journal, additions, budget, count=None):
        yield b'{"events":['
        count = journal.metrics.events if count is None else count
        for index in range(count):
            if index:
                yield b","
            yield from encode_event(journal_event(journal, index), budget=budget)
        for event in additions:
            if count:
                yield b","
            yield from encode_event(event, budget=budget)
            count += 1
        h = journal.header
        # A fixed canonical wrapper over validated ASCII scalar fields, not a
        # second event encoder or a materialized event list.
        yield (
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

    def _stage_manifest(self, manifest, budget):
        return self._files._stage("manifest", encode_manifest(manifest, budget=budget))

    def _pending_token(self, state, intent, next_stage):
        self._retained = _cost(state.requirements) + 16384
        return PendingPublication(
            self._session_id,
            state.world_id,
            intent.intent_id,
            intent.intent_digest,
            self._lifetime.operation_id,
            state,
            next_stage,
        )

    def _owned_pending(self, pending):
        if (
            pending is not self._pending
            or pending.session_id != self._session_id
            or self._state not in ("pending", "legacy")
        ):
            refuse("binding")
        self._activate(pending.world_id, self._kind)
        if self._expected_files is not None:
            for observed in self._expected_files:
                self._files.confirm(observed, pending.world_id)

    @_boundary("inspect")
    def inspect(self, world_id):
        if self._state != "new":
            refuse("binding")
        self._activate(world_id)
        with self._load(world_id) as (_, _, _, _, _, state):
            self._check_observation(self._checks.check_current(state, None), state)
        del _
        self._state = "inspected"
        return state

    @_boundary("stage")
    def stage_legacy(self, world_id, raw):
        if self._state not in ("new", "inspected") or self._legacy_candidate is not None:
            refuse("binding")
        validate_identifier(world_id)
        if self._world is not None and self._world != world_id:
            refuse("binding")
        self._staged_world = world_id
        if type(raw) is not bytes or len(raw) > 16000000:
            refuse("capacity")
        with self._budget() as budget:
            decode_legacy(raw, world_id=world_id, budget=budget)
            candidate = self._files.stage_bytes("legacy", raw)
        self._external_bytes = len(raw) + 64
        self._legacy_candidate = candidate
        return candidate

    @_boundary("prepare")
    def prepare(self, world_id, proposal):
        return self._prepare(world_id, proposal)

    def _prepare(self, world_id, proposal, *, barrier=False):
        if (
            self._state not in ("new", "inspected", "recovery_inspected")
            or type(proposal) is not ProposedPublication
        ):
            refuse("binding")
        self._activate(world_id, self._kind or "publication")
        proposal = _proposal_copy(proposal)
        if proposal.initial_event.kind not in ("prepare", "restore_epoch"):
            refuse("candidate")
        middle = () if proposal.seal_event is None else (proposal.seal_event,)
        if middle and middle[0].kind != "seal":
            refuse("candidate")
        extra = sum(
            _cost(x)
            for x in (
                proposal.initial_event,
                proposal.completion_template,
                proposal.pending_manifest,
                proposal.completed_manifest,
            )
        )
        intent = proposal.initial_event.payload.intent
        with self._load(
            world_id, extra=extra, barrier_for=proposal.initial_event if barrier else None
        ) as (budget, manifest, journal, reduction, _, before):
            observed_check = (
                self._checks.check_recovery(before, proposal, proposal.completion_template)
                if barrier
                else self._checks.check_current(before, proposal)
            )
            self._check_observation(observed_check, before, intent.intent_digest)
            retry = lookup_retry(reduction, intent)
            if retry is not None:
                if retry.outcome == "pending":
                    refuse("pending")
                retry_token = self._pending_token(before, intent, "retry")
                observed = self._checks.resolve_outcome(retry_token)
                self._check_outcome(
                    observed,
                    retry_token,
                    before,
                    before.projection,
                    {"CANCELLED_EXACT"} if retry.outcome == "cancelled" else {"COMMITTED_EXACT"},
                )
                self._check_observation(self._checks.check_final(before, before.projection), before)
                self._files.sync_final(before.files, world_id)
                self._state = "complete"
                return LocalPublicationResult("final", before, before.requirements, retry.outcome)
            if before.state in ("closed", "sealed"):
                refuse("conflict")
            self._manifest_change(
                manifest, proposal.pending_manifest, world_id, proposal.initial_event
            )
            self._manifest_change(
                manifest, proposal.completed_manifest, world_id, proposal.initial_event
            )
            assess_append(
                reduction,
                (proposal.initial_event, *middle, proposal.completion_template),
                budget=budget,
            )
            barrier_manifest = None
            if (
                proposal.initial_event.kind == "restore_epoch"
                and proposal.initial_event.payload.scope == "installation"
                and not barrier
            ):
                barrier_record = replace(
                    _copy_record(manifest),
                    state="restoring",
                    installation_epoch=proposal.initial_event.payload.installation_after,
                )
                barrier_record = replace(barrier_record, checksum=manifest_checksum(barrier_record))
                barrier_manifest = self._stage_manifest(barrier_record, budget)
            pending_manifest = self._stage_manifest(proposal.pending_manifest, budget)
            completed_manifest = self._stage_manifest(proposal.completed_manifest, budget)
            pending_journal = self._files._stage(
                "journal", self._journal_chunks(journal, (proposal.initial_event, *middle), budget)
            )
            completed_journal = self._files._stage(
                "journal",
                self._journal_chunks(
                    journal, (proposal.initial_event, *middle, proposal.completion_template), budget
                ),
            )
            initial_key = _completion_key(proposal.completion_template)
        del budget, manifest, journal, reduction, _
        effects = [e for e in intent.effects if e.kind == "forget_apply"]
        if len(effects) > 1 or (bool(effects) != (self._legacy_candidate is not None)):
            refuse("candidate")
        with self._load(
            world_id, manifest=pending_manifest, journal=pending_journal, extra=extra
        ) as (_, _, _, _, _, pending_state):
            pass
        del _
        with self._load(
            world_id,
            manifest=completed_manifest,
            journal=completed_journal,
            legacy=self._legacy_candidate,
            extra=extra,
        ) as (_, _, _, _, _, completed_state):
            pass
        del _
        if effects:
            self._validate_extension(
                world_id, self._legacy_candidate, effects[0], pending_state, intent
            )
        # All complete candidates and the optional ledger are validated before
        # the first final-file mutation.
        for observed in before.files:
            self._files.confirm(observed, world_id)
        expected_manifest = before.files[0]
        if barrier_manifest is not None:
            expected_manifest = self._files.replace(barrier_manifest, expected_manifest, world_id)
        self._files.replace(pending_journal, before.files[1], world_id)
        self._files.replace(pending_manifest, expected_manifest, world_id)
        with self._load(world_id) as (_, _, _, _, _, installed):
            token = self._pending_token(installed, intent, "legacy" if effects else "outcome")
        del _
        self._pending, self._state = token, "pending"
        self._expected_files = installed.files
        self._completion_key = initial_key
        self._anticipated_head = complete_event_digest(proposal.completion_template)
        self._completed_manifest, self._completion_candidate = completed_manifest, completed_journal
        return token

    def _validate_extension(self, world_id, candidate, effect, state, intent):
        # Sequential decoder lifetimes retain only packed spans and bindings;
        # old and new complete ledger buffers never coexist.
        from array import array

        spans = array("I")
        with (
            self._budget() as budget,
            self._files.read("legacy", world_id, budget) as (raw, old_observation),
        ):
            old = decode_legacy(raw, world_id=world_id, budget=budget)
            if _ledger(old.binding) != effect.before_ledger:
                refuse("conflict", "legacy")
            reservation = budget.reserve("metadata", old.binding.count * 8 + 256)
            for index in range(old.binding.count):
                spans.extend(legacy_row_span(old, index))
            count = old.binding.count
        del old, raw, budget, reservation
        with (
            self._budget(len(spans) * 4 + 256) as budget,
            self._files.read("legacy", world_id, budget, candidate=candidate) as (raw, _),
        ):
            new = decode_legacy(raw, world_id=world_id, budget=budget)
            if _ledger(new.binding) != effect.after_ledger or new.binding.count != count + 1:
                refuse("conflict", "legacy")
            row = legacy_row(new, count)
            if (
                row.id,
                row.plan_version,
                row.row_digest,
                row.plan_digest,
                row.extension_digest,
            ) != (
                effect.row.id,
                effect.row.plan_version,
                effect.row.row_digest,
                effect.row.plan_digest,
                effect.row.extension_digest,
            ):
                refuse("conflict", "legacy")
            # Compare descriptor intervals in small bounded chunks, not hashes
            # of decoded/canonicalized old rows.
            self._files.confirm(old_observation, world_id)
            directory, name = self._files._location("legacy", world_id)
            fd, original = self._files._open(directory, name, "legacy")
            try:
                for index in range(count):
                    start, end = spans[index * 2 : index * 2 + 2]
                    ns, ne = legacy_row_span(new, index)
                    if end - start != ne - ns:
                        refuse("conflict", "legacy")
                    while start < end:
                        self._boundary_check()
                        length = min(65536, end - start)
                        chunk = os.pread(fd, length, start)
                        if len(chunk) != length or chunk != memoryview(raw)[ns : ns + length]:
                            refuse("conflict", "legacy")
                        start += length
                        ns += length
            finally:
                os.close(fd)
            self._files.confirm(old_observation, world_id)
            token = self._pending_token(state, intent, "legacy")
            self._check_observation(
                self._checks.check_legacy_extension(
                    token, effect.before_ledger, effect.after_ledger, row
                ),
                state,
                intent.intent_digest,
            )
        del new, row, raw, budget

    @_boundary("legacy")
    def publish_legacy(self, pending, owned_candidate):
        self._owned_pending(pending)
        if pending.next_stage != "legacy" or owned_candidate is not self._legacy_candidate:
            refuse("binding")
        with self._load(pending.world_id) as (_, _, journal, _, _, state):
            intent = self._intent(journal, pending.intent_id)
            effects = [e for e in intent.effects if e.kind == "forget_apply"]
            if len(effects) != 1:
                refuse("conflict")
            effect = effects[0]
        del journal, _
        self._validate_extension(pending.world_id, owned_candidate, effect, state, intent)
        for observed in state.files:
            self._files.confirm(observed, pending.world_id)
        self._files.replace(owned_candidate, state.files[2], pending.world_id)
        self._legacy_candidate = None
        self._state = "legacy"
        with self._load(pending.world_id) as (_, _, _, _, _, installed):
            self._expected_files = installed.files
            return installed

    def _finish(self, pending, event, *, recovery=False, already=False, manifest_candidate=None):
        world = pending.world_id
        expected = [self._files.observe(role, world) for role in ("manifest", "journal", "legacy")]
        if event.kind not in ("commit", "recover", "world_closed"):
            refuse("candidate")
        candidate_manifest = manifest_candidate or self._completed_manifest
        # The completion may only replace the anticipated fixed-width time.
        if not recovery and _completion_key(event) != self._completion_key:
            refuse("conflict")
        original_head = complete_event_digest(event)
        event = _copy_record(event)
        if not already:
            # Only the central trusted clock supplies a new completion instant.
            completed_at = clock.now(world).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            event = replace(
                event,
                payload=replace(
                    event.payload,
                    completion=replace(event.payload.completion, completed_at=completed_at),
                ),
            )
            event = replace(event, digest=event_digest(event))
            with (
                self._budget() as budget,
                self._files.read("manifest", world, budget, candidate=candidate_manifest) as (
                    raw,
                    _,
                ),
            ):
                manifest = decode_manifest(raw, budget=budget)
                head = next(h for h in manifest.worlds if h.world_id == world)
                if head.head_digest != (original_head if recovery else self._anticipated_head):
                    refuse("conflict")
                updated = replace(_copy_record(head), head_digest=complete_event_digest(event))
                manifest = replace(
                    _copy_record(manifest),
                    worlds=tuple(
                        updated if h.world_id == world else _copy_record(h) for h in manifest.worlds
                    ),
                )
                manifest = replace(manifest, checksum=manifest_checksum(manifest))
                revised_manifest = self._stage_manifest(manifest, budget)
            del raw, manifest, head, updated, budget, _
            self._files.discard(candidate_manifest)
            candidate_manifest = revised_manifest
            self._completed_manifest = revised_manifest
            with self._load(world) as (budget, _, journal, reduction, _, state):
                assess_append(reduction, (event,), budget=budget)
                if self._completion_candidate is not None:
                    self._files.discard(self._completion_candidate)
                    self._completion_candidate = None
                candidate_journal = self._files._stage(
                    "journal", self._journal_chunks(journal, (event,), budget)
                )
            del budget, journal, reduction, _
        else:
            candidate_journal = None
        with self._load(world, manifest=candidate_manifest, journal=candidate_journal) as (
            _,
            _,
            _,
            _,
            _,
            target,
        ):
            pass
        del _
        target_projection_before_metadata = replace(
            target.projection,
            sequence=pending.state.sequence,
            head_digest=pending.state.head_digest,
            ready=False,
        )
        for observation in expected:
            self._files.confirm(observation, world)
        outcome = self._checks.resolve_outcome(pending)
        cancellation = (
            event.kind == "recover" and event.payload.resolution == "cancel_unapplied_expansion"
        )
        self._check_outcome(
            outcome,
            pending,
            target,
            target_projection_before_metadata,
            {"CANCELLED_EXACT"} if cancellation else {"COMMITTED_EXACT"},
        )
        # A cancellation marker is a committed approval burn; a bare proof of
        # absence (NOT_APPLIED_PROVEN) is never enough for final publication.
        for observation in expected:
            self._files.confirm(observation, world)
        if candidate_journal is not None:
            expected[1] = self._files.replace(candidate_journal, expected[1], world)
        finalized = self._checks.finalize_projection(pending, target.projection)
        self._check_outcome(
            finalized,
            pending,
            target,
            target.projection,
            {"CANCELLED_EXACT"} if cancellation else {"COMMITTED_EXACT"},
        )
        for observation in expected:
            self._files.confirm(observation, world)
        self._files.replace(candidate_manifest, expected[0], world)
        with self._load(world) as (_, _, _, reduction, _, final):
            if final.projection.ready:
                compare_projection(reduction, finalized.projection)
            self._check_observation(self._checks.check_final(final, final.projection), final)
        del reduction, _
        self._files.sync_final(final.files, world)
        self._state = "complete"
        return LocalPublicationResult(
            "final", final, final.requirements, "cancelled" if cancellation else "committed"
        )

    @_boundary("completion")
    def complete(self, pending, completion_event):
        self._owned_pending(pending)
        if pending.next_stage == "legacy" and self._state != "legacy":
            refuse("pending")
        return self._finish(pending, completion_event)

    def _recovery_snapshot(self, world_id, operation, resolution_event):
        if type(operation) is not ProposedPublication or operation.initial_event.kind not in (
            "prepare",
            "restore_epoch",
        ):
            refuse("candidate")
        intent = operation.initial_event.payload.intent
        self._recovery_expected = tuple(
            self._files.observe(role, world_id) for role in ("manifest", "journal", "legacy")
        )
        base = intent.base_sequence
        pending_sequence = base + 1 + (operation.seal_event is not None)
        # Read the whole actual journal first. A corrupt tail never becomes an
        # accepted prefix and a temporary candidate is never selected as head.
        with (
            self._budget(
                sum(
                    _cost(x)
                    for x in (
                        operation.initial_event,
                        operation.pending_manifest,
                        operation.completed_manifest,
                    )
                )
            ) as budget,
            ExitStack() as stack,
        ):
            mraw, _ = stack.enter_context(self._files.read("manifest", world_id, budget))
            current = decode_manifest(mraw, budget=budget)
            self._manifest_binding(current)
            raw, _ = stack.enter_context(self._files.read("journal", world_id, budget))
            journal = scan_journal(raw, budget=budget)
            head = next((h for h in current.worlds if h.world_id == world_id), None)
            if (
                head is None
                or not base <= head.sequence <= journal.metrics.events
                or journal.metrics.events not in (pending_sequence, pending_sequence + 1)
            ):
                refuse("recovery_required")
            initial = journal_event(journal, base)
            if initial != operation.initial_event:
                refuse("conflict")
            if (
                operation.seal_event is not None
                and journal_event(journal, base + 1) != operation.seal_event
            ):
                refuse("conflict")
            completed = journal.metrics.events == pending_sequence + 1
            if completed and journal_event(journal, pending_sequence) != resolution_event:
                refuse("conflict")
            self._manifest_change(current, operation.pending_manifest, world_id, initial)
            self._manifest_change(current, operation.completed_manifest, world_id, initial)
            # The persisted manifest must describe an exact committed prefix.
            prefix = self._files._stage(
                "journal", self._journal_chunks(journal, (), budget, head.sequence)
            )
            selected = operation.completed_manifest if completed else operation.pending_manifest
            selected_candidate = self._stage_manifest(selected, budget)
            final_candidate = self._stage_manifest(operation.completed_manifest, budget)
        del budget, current, raw, mraw, journal, initial, head, _
        effects = [e for e in intent.effects if e.kind == "forget_apply"]
        if len(effects) > 1:
            refuse("conflict")
        allowed = effects[0].after_ledger if effects else None
        with self._load(
            world_id,
            journal=prefix,
            allowed_ledger=allowed,
            barrier_for=operation.initial_event
            if operation.initial_event.kind == "restore_epoch"
            and operation.initial_event.payload.scope == "installation"
            else None,
        ) as (_, _, _, _, _, _):
            pass
        del _
        self._files.discard(prefix)
        with self._load(world_id, manifest=selected_candidate, allowed_ledger=allowed) as (
            _,
            _,
            journal,
            reduction,
            ledger,
            state,
        ):
            retry = lookup_retry(reduction, intent)
            if retry is None or retry.outcome not in ("pending", "committed", "cancelled"):
                refuse("conflict")
            # When the new ledger is already durable, validate its exact row.
            if effects and _ledger(ledger.binding) == effects[0].after_ledger:
                row = legacy_row(ledger, ledger.binding.count - 1)
                if (
                    row.id,
                    row.row_digest,
                    row.plan_version,
                    row.plan_digest,
                    row.extension_digest,
                ) != (
                    effects[0].row.id,
                    effects[0].row.row_digest,
                    effects[0].row.plan_version,
                    effects[0].row.plan_digest,
                    effects[0].row.extension_digest,
                ):
                    refuse("conflict")
        del journal, reduction, ledger, _
        for observation in self._recovery_expected:
            self._files.confirm(observation, world_id)
        return intent, state, completed, selected_candidate, final_candidate

    @_boundary("recovery")
    def inspect_recovery(self, world_id, operation):
        if self._state != "new":
            refuse("binding")
        self._activate(world_id, "recovery")
        operation = _proposal_copy(operation)
        intent, state, completed, selected, final = self._recovery_snapshot(
            world_id, operation, operation.completion_template
        )
        self._check_observation(
            self._checks.check_recovery(state, operation, None), state, intent.intent_digest
        )
        self._files.discard(selected)
        self._files.discard(final)
        self._state = "recovery_inspected"
        resolutions = (
            ("confirm_committed",)
            if completed
            else (
                ("complete_restriction", "confirm_committed")
                if classify_effects(intent.effects) == "restrictive"
                else ("confirm_committed", "cancel_unapplied_expansion", "complete_reapproved")
            )
        )
        return RecoveryRequirements(
            "completion" if completed else "pending",
            intent.intent_id,
            resolutions,
            state.requirements,
            state,
        )

    @_boundary("recovery")
    def recover(self, world_id, operation, resolution_event):
        if self._state not in ("new", "recovery_inspected"):
            refuse("binding")
        self._activate(world_id, "recovery")
        operation = _proposal_copy(operation)
        if (
            operation.initial_event.kind == "restore_epoch"
            and operation.initial_event.payload.scope == "installation"
        ):
            with self._budget() as budget, ExitStack() as stack:
                raw, _ = stack.enter_context(self._files.read("manifest", world_id, budget))
                manifest = decode_manifest(raw, budget=budget)
                jraw, _ = stack.enter_context(self._files.read("journal", world_id, budget))
                journal = scan_journal(jraw, budget=budget)
                barrier_only = (
                    manifest.state == "restoring"
                    and manifest.installation_epoch
                    == operation.initial_event.payload.installation_after
                    and journal.metrics.events
                    == operation.initial_event.payload.intent.base_sequence
                )
            del raw, jraw, budget, manifest, journal, _
            if barrier_only:
                token = self._prepare(
                    world_id, replace(operation, completion_template=resolution_event), barrier=True
                )
                return self._finish(token, resolution_event, recovery=True)
        intent, state, completed, selected, final = self._recovery_snapshot(
            world_id, operation, resolution_event
        )
        recovery_check = self._checks.check_recovery(state, operation, resolution_event)
        self._check_observation(recovery_check, state, intent.intent_digest)
        for observation in self._recovery_expected:
            self._files.confirm(observation, world_id)
        token = self._pending_token(state, intent, "outcome")
        if (
            not completed
            and resolution_event.kind == "recover"
            and resolution_event.payload.resolution == "cancel_unapplied_expansion"
        ):
            self._check_outcome(
                recovery_check.prior_outcome, token, state, state.projection, {"NOT_APPLIED_PROVEN"}
            )
        if completed:
            # Metadata always names the original prepare, including retry after
            # a completion-journal replacement or a lost acknowledgment.
            with self._budget() as budget:
                pending_manifest = self._stage_manifest(operation.pending_manifest, budget)
            with (
                self._budget() as budget,
                self._files.read("journal", world_id, budget) as (raw, _),
            ):
                journal = scan_journal(raw, budget=budget)
                prefix = self._files._stage(
                    "journal",
                    self._journal_chunks(
                        journal,
                        (),
                        budget,
                        intent.base_sequence + 1 + (operation.seal_event is not None),
                    ),
                )
            del budget, journal, raw, _
            effects = [e for e in intent.effects if e.kind == "forget_apply"]
            with self._load(
                world_id,
                manifest=pending_manifest,
                journal=prefix,
                allowed_ledger=effects[0].after_ledger if effects else None,
            ) as (_, _, _, _, _, pending_state):
                token = self._pending_token(pending_state, intent, "outcome")
            del _
            self._files.discard(pending_manifest)
            self._files.discard(prefix)
        else:
            # Reconcile only the exact durable pending journal. Never restore a
            # previous journal or promote an orphan temporary file.
            self._files.replace(selected, self._recovery_expected[0], world_id)
            selected = None
        if selected is not None:
            self._files.discard(selected)
        self._pending, self._state = token, "pending"
        effects = [e for e in intent.effects if e.kind == "forget_apply"]
        if effects and not completed:
            actual = self._files.observe("legacy", world_id)
            if actual.digest != effects[0].after_ledger.file_digest:
                if self._legacy_candidate is None:
                    refuse("pending")
                self._validate_extension(
                    world_id, self._legacy_candidate, effects[0], token.state, intent
                )
                self._files.replace(self._legacy_candidate, actual, world_id)
                self._legacy_candidate = None
        self._completed_manifest = final
        return self._finish(token, resolution_event, recovery=True, already=completed)

    @_boundary("seal")
    def seal(self, world_id, seal_event, final_manifest):
        if self._state not in ("new", "inspected") or seal_event.kind != "seal":
            refuse("binding")
        self._activate(world_id)
        expected = [
            self._files.observe(role, world_id) for role in ("manifest", "journal", "legacy")
        ]
        seal_event, final_manifest = _copy_record(seal_event), _copy_record(final_manifest)
        with self._budget() as budget, ExitStack() as stack:
            mraw, _ = stack.enter_context(self._files.read("manifest", world_id, budget))
            manifest = decode_manifest(mraw, budget=budget)
            self._manifest_binding(manifest)
            jraw, _ = stack.enter_context(self._files.read("journal", world_id, budget))
            journal = scan_journal(jraw, budget=budget)
            head = next((h for h in manifest.worlds if h.world_id == world_id), None)
            if head is None:
                refuse("binding")
            behind = head.sequence != journal.metrics.events
            if behind:
                if (
                    head.sequence + 1 != journal.metrics.events
                    or journal_event(journal, journal.metrics.events - 1) != seal_event
                ):
                    refuse("recovery_required")
                self._manifest_change(manifest, final_manifest, world_id, seal_event)
                prefix = self._files._stage(
                    "journal", self._journal_chunks(journal, (), budget, head.sequence)
                )
                mc = self._stage_manifest(final_manifest, budget)
        del budget, manifest, journal, head, mraw, jraw, _
        if behind:
            with self._load(world_id, journal=prefix) as (budget, _, _, reduction, _, before):
                assess_append(reduction, (seal_event,), budget=budget)
                self._check_observation(
                    self._checks.check_current(before, seal_event), before, seal_event.digest
                )
            del budget, reduction, _
            self._files.discard(prefix)
            jc = None
        else:
            with self._load(world_id) as (budget, manifest, journal, reduction, _, before):
                self._check_observation(
                    self._checks.check_current(before, seal_event), before, seal_event.digest
                )
                if (
                    reduction.sealed
                    and journal_event(journal, journal.metrics.events - 1) == seal_event
                ):
                    self._check_observation(
                        self._checks.check_final(before, before.projection), before
                    )
                    self._files.sync_final(before.files, world_id)
                    self._state = "complete"
                    return LocalPublicationResult("final", before, before.requirements, "sealed")
                assess_append(reduction, (seal_event,), budget=budget)
                self._manifest_change(manifest, final_manifest, world_id, seal_event)
                mc = self._stage_manifest(final_manifest, budget)
                jc = self._files._stage(
                    "journal", self._journal_chunks(journal, (seal_event,), budget)
                )
            del budget, manifest, journal, reduction, _
        with self._load(world_id, manifest=mc, journal=jc) as (_, _, _, _, _, target):
            pass
        del _
        token = PendingPublication(
            self._session_id,
            world_id,
            seal_event.event_id,
            seal_event.digest,
            self._lifetime.operation_id,
            before,
            "metadata",
        )
        for observation in expected:
            self._files.confirm(observation, world_id)
        if jc is not None:
            expected[1] = self._files.replace(jc, expected[1], world_id)
        observed = self._checks.finalize_projection(token, target.projection)
        self._check_outcome(observed, token, target, target.projection, {"COMMITTED_EXACT"})
        for observation in expected:
            self._files.confirm(observation, world_id)
        self._files.replace(mc, expected[0], world_id)
        with self._load(world_id) as (_, _, _, _, _, final):
            self._check_observation(self._checks.check_final(final, final.projection), final)
        del _
        self._files.sync_final(final.files, world_id)
        self._state = "complete"
        return LocalPublicationResult("final", final, final.requirements, "sealed")

    def cancel(self):
        """Signal only. The owning worker must return and close its own session."""
        self._cancelled.set()

    def close(self):
        if self._closed:
            return
        self._assert_worker()
        if not self._guard.acquire(blocking=False):
            refuse("binding", "close")
        self._closing = True
        failure = False
        try:
            if self._files is not None:
                try:
                    self._files.close()
                except (OSError, PublicationRefused, LeaseRefused):
                    failure = True
            # The trusted lifetime owner joins all owned work before exit. EX
            # stays held throughout this call, even after caller cancellation.
            if self._context is not None:
                try:
                    self._context.__exit__(None, None, None)
                except Exception:
                    self._failed = True
                    refuse("outcome_unknown", "close")
                self._context = None
            if self._lease is not None:
                try:
                    self._lease.close()
                except (OSError, LeaseRefused):
                    failure = True
            self._closed = True
        finally:
            self._closing = False
            self._guard.release()
        if failure:
            refuse("recovery_required", "close")

    def __enter__(self):
        self._boundary_check()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except PublicationRefused:
            if exc_type is None:
                raise
