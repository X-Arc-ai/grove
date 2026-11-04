"""Inactive outcome kernel controls with explicit, non-production test owners.

The synthetic tests exercise decisions only. The opt-in native tests exercise
WorldWriter locking, commit/rollback, and mutation accounting on the exact local
development target. Both classes replace the external owners and the erasure
adapter. Neither class proves real erasure closure, production ownership, file
durability, or power-loss recovery.
"""

import copy
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from grove import clock
from grove.authority.publication import (
    ObservedFile,
    PendingPublication,
    PublicationRefused,
    UntrustedFileState,
)
from grove.authority.records import PROFILE, TYPES
from grove.store.query import CYPHER_PREFIX
from grove.writer import authority_forget
from grove.writer import authority_outcomes as kernel
from grove.writer.authority_records import (
    ApplyCommand,
    ApprovalAccess,
    AttemptCommand,
    BarrierCommand,
    CancelCommand,
    FinalizeCommand,
    InspectionCommand,
    RuntimeBinding,
    SealCommand,
    validate_row,
)
from grove.writer.tx import WorldWriter
from grove.writer.worlds import create_world
from tests.privacy.test_authority_codec import H, canonical, effect, hashed, ledger, uid
from tests.privacy.test_authority_publication import FileJournal, record_event

STAMP = "2026-11-04T12:02:00.000000Z"
NOW = datetime(2026, 11, 4, 12, 2, tzinfo=UTC)
TOKEN = "gfa_" + "a" * 43  # Deliberately synthetic; no issued credential.
TARGET_DIGEST = "b" * 64
SENTINEL_ID = uid(8800)
TABLES = (
    "AuthorityProjection",
    "AuthorityOperation",
    "AuthorityApprovalUse",
    "ForgetApproval",
    "HistoryValue",
)


def _empty_ledger(world):
    return canonical(
        {
            "format": "grove.forget-ledger.v1",
            "world_id": world,
            "count": 0,
            "rows": [],
            "sha256": sha256(b"[]").hexdigest(),
        }
    )


def _projection(journal):
    reduced = journal.decode()
    return TYPES["ProjectionBinding"](
        installation_id=uid(1),
        installation_epoch=reduced.installation_epoch,
        authority_epoch=reduced.authority_epoch,
        sequence=len(journal.events),
        head_digest=hashed(journal.events[-1]),
        schema_profile=PROFILE,
        state_digest=reduced.state_digest,
        ledger=reduced.ledger,
        ready=reduced.release_state in ("ready", "no_host_policy"),
    )


def _projection_row(world, projection):
    return validate_row(
        "projection",
        {
            "world_id": world,
            "owner_id": "owner-1",
            "installation_id": uid(1),
            "profile_code": "authority_state_v1",
            "profile_digest": PROFILE.digest,
            "installation_epoch": projection.installation_epoch,
            "authority_epoch": projection.authority_epoch,
            "erasure_revision": 0,
            "sequence": projection.sequence,
            "head_digest": projection.head_digest,
            "state_digest": projection.state_digest,
            "ready": projection.ready,
            "ledger_format": projection.ledger.format,
            "ledger_count": projection.ledger.count,
            "ledger_file_digest": projection.ledger.file_digest,
            "ledger_rows_digest": projection.ledger.rows_digest,
            "fence": 0,
            "active_intent_id": None,
        },
    )


@dataclass(frozen=True)
class _TestLegacyEvidence:
    effect: object


class _KernelOwner:
    """An explicit test substitute, never an InactiveAuthorityWriter."""

    def __init__(self, world, *, expansion=False):
        self.world = world
        self._owner_id = "owner-1"
        self.bindings = SimpleNamespace(installation_id=uid(1))
        self._barrier_command = None
        self._resolution = "apply"
        self._renewed_approval = None
        self._issued = set()
        self.effects = 0
        self.fail_effect = False
        self.fail_result = False
        self.journal = FileJournal(_empty_ledger(world), world)
        self.base = _projection(self.journal)
        if expansion:
            effects = [effect("class_create", 77, class_id="class-1", label_value_id="value-1")]
        else:
            effects = [
                effect(
                    "forget_apply",
                    77,
                    row={
                        "id": uid(7800),
                        "plan_version": "v2",
                        "row_digest": H,
                        "plan_digest": H,
                        "extension_digest": None,
                    },
                    before_ledger={
                        name: getattr(self.base.ledger, name)
                        for name in ("format", "count", "file_digest", "rows_digest")
                    },
                    after_ledger=ledger(1),
                    expected_revision=0,
                    revision=1,
                )
            ]
        self.journal.prepare(effects, mode="ordinary" if expansion else "restrictive")
        self.prepare = record_event(self.journal.events[-1])
        prepared = _projection(self.journal)
        intent = self.prepare.payload.intent
        self.binding = RuntimeBinding(self, uid(9000), uid(9001), world, STAMP)
        files = tuple(
            ObservedFile(role, 1, i, 0o600, 1, 1, 1, 1, 1, H)
            for i, role in enumerate(("manifest", "journal", "legacy"), 1)
        )
        state = UntrustedFileState(
            world,
            self._owner_id,
            prepared.installation_epoch,
            prepared.authority_epoch,
            0,
            prepared.sequence,
            prepared.head_digest,
            prepared.state_digest,
            prepared.ledger,
            files,
            "pending",
            intent.intent_id,
            (),
            prepared,
        )
        self.pending = PendingPublication(
            uid(9002),
            world,
            intent.intent_id,
            intent.intent_digest,
            self.binding.operation_id,
            state,
            "outcome",
        )
        self.journal.commit(resolution="cancel_unapplied_expansion" if expansion else None)
        self.completion = record_event(self.journal.events[-1])
        self.target = _projection(self.journal)
        self.revision = 0 if expansion else 1
        self.expansion = expansion
        self.attempt = self.command(
            AttemptCommand,
            prepare=self.prepare,
            pending=self.pending,
            expected_projection=self.base,
            expected_fence=0,
            attempt_id=uid(9003),
            operation_id=self.binding.operation_id,
        )
        self.apply = self.command(
            ApplyCommand,
            prepare=self.prepare,
            pending=self.pending,
            attempt_id=self.attempt.attempt_id,
            fence=1,
            legacy=_TestLegacyEvidence(intent.effects[0]),
        )

    def command(self, kind, **values):
        command = kind(binding=self.binding, **values)
        self._issued.add(id(command))
        return command

    def inspect(self, current=None):
        intent = self.prepare.payload.intent
        return self.command(
            InspectionCommand,
            intent_id=intent.intent_id,
            intent_digest=intent.intent_digest,
            current_projection=current or self.base,
        )

    def barrier(self):
        command = self.command(
            BarrierCommand,
            prepare=self.prepare,
            pending=self.pending,
            old_attempt_id=self.attempt.attempt_id,
            old_fence=1,
            barrier_id=uid(9004),
        )
        self._barrier_command = command
        return command

    def cancel(self, proof):
        return self.command(
            CancelCommand,
            prepare=self.prepare,
            resolution=self.completion,
            pending=self.pending,
            proof=proof,
        )

    def finalize(self, fence=1):
        return self.command(
            FinalizeCommand,
            pending=self.pending,
            completion=self.completion,
            target=self.target,
            expected_fence=fence,
        )

    def _assert_original(self, prepare):
        assert prepare == self.prepare

    def _validate_projection_in_tx(self, tx, row):
        assert tx.world_id == self.world == row["world_id"]
        assert row["installation_id"] == self.bindings.installation_id

    def _validate_in_tx(self, tx, prepare, stage):
        assert tx.world_id == self.world and prepare == self.prepare
        assert stage in {"register", "barrier", "apply", "cancel"}

    def _assert_recovery_attempt(self, old, projection, command):
        # These controls never issue a recovery registration. The production
        # owner's separate recovery authorization cannot be inferred here.
        raise PublicationRefused("conflict", "outcome")

    def _validate_result_in_tx(self, tx, row, projection):
        if self.fail_result:
            raise PublicationRefused("conflict", "outcome")
        assert tx.world_id == self.world
        if row.get("outcome"):
            assert row["result_state_digest"] == self.target.state_digest
            assert projection["state_digest"] == self.target.state_digest

    def _approval_in_tx(self, tx, approval, historical=False):
        assert approval == self.prepare.payload.intent.authorization
        return ApprovalAccess(approval, TARGET_DIGEST, None if historical else TOKEN)

    def _terminal_target(self, prepare, cancelled=False):
        assert prepare == self.prepare and cancelled == self.expansion
        return self.target, self.revision

    def _validate_finalization_in_tx(self, tx, command, row, projection):
        assert tx.world_id == self.world
        assert command.target == self.target
        if row is not None:
            assert command.completion == self.completion


@pytest.fixture
def kernel_owners(monkeypatch):
    """Replace only the external authority and effect adapter, explicitly."""
    owners = []

    def check_binding(binding, tx):
        if (
            binding.owner not in owners
            or binding is not binding.owner.binding
            or binding.world_id != tx.world_id
        ):
            raise PublicationRefused("binding", "outcome")
        return binding.owner

    def check_command(command, tx):
        owner = check_binding(command.binding, tx)
        if id(command) not in owner._issued:
            raise PublicationRefused("binding", "outcome")
        return owner

    def test_effect(tx, binding, evidence):
        owner = check_binding(binding, tx)
        assert type(evidence) is _TestLegacyEvidence
        owner.effects += 1
        tx.run(
            "MATCH (n:HistoryValue {id: $id}) SET n.value = $value", id=SENTINEL_ID, value="after"
        )
        if owner.fail_effect:
            raise RuntimeError("synthetic effect fault")

    monkeypatch.setattr(kernel, "check_binding", check_binding)
    monkeypatch.setattr(kernel, "check_command", check_command)
    monkeypatch.setattr(authority_forget, "LegacyPlanEvidence", _TestLegacyEvidence)
    monkeypatch.setattr(authority_forget, "apply_legacy_forget_in_tx", test_effect)
    monkeypatch.setattr(clock, "now", lambda world=None: NOW)

    def make(world="world-1", *, expansion=False):
        owner = _KernelOwner(world, expansion=expansion)
        owners.append(owner)
        return owner

    return make


def _seed_rows(owner):
    approval = owner.prepare.payload.intent.authorization
    return {
        "AuthorityProjection": [_projection_row(owner.world, owner.base)],
        "AuthorityOperation": [],
        "AuthorityApprovalUse": [],
        "ForgetApproval": [
            {
                "world_id": owner.world,
                "id": approval.approval_id,
                "user_id": owner._owner_id,
                "target_digest": TARGET_DIGEST,
                "token_hash": sha256(TOKEN.encode("ascii")).hexdigest(),
                "created_at": NOW,
                "expires_at": NOW + timedelta(minutes=5),
            }
        ],
        "HistoryValue": [{"world_id": owner.world, "id": SENTINEL_ID, "value": "before"}],
    }


class _MemoryStore:
    """A decision-test double. Its transaction behavior is not native evidence."""

    def __init__(self, owner):
        self.world_id = owner.world
        self.tables = _seed_rows(owner)
        self.world = {"owner": owner._owner_id, "sequence": 17, "generation": 4}

    def transaction(self, work):
        before = self.snapshot()
        try:
            value = work(self)
        except BaseException:
            self.tables, self.world = before["tables"], before["world"]
            raise
        if self.tables != before["tables"]:
            self.world["generation"] += 1
        return value

    def snapshot(self):
        return copy.deepcopy({"tables": self.tables, "world": self.world})

    def lock_world(self):
        pass

    def unscoped(self):
        def run(query, **params):
            assert "MATCH (w:World" in query and params["world_id"] == self.world_id
            return SimpleNamespace(single=lambda: copy.deepcopy(self.world))

        return SimpleNamespace(run=run)

    def run(self, query, **params):
        label = next((name for name in TABLES if ":" + name in query), None)
        assert label is not None, query
        rows = self.tables[label]
        if "count(n)" in query:
            return [{"count": len(rows)}]
        if query.startswith("CREATE"):
            rows.append({"world_id": self.world_id, **copy.deepcopy(params["row"])})
            return []
        if "token_hash: $token_hash" in query:
            matching = [
                r
                for r in rows
                if r["token_hash"] == params["token_hash"]
                and r["user_id"] == params["actor"]
                and r["target_digest"] == params["digest"]
                and r.get("used_at") is None
                and r["expires_at"] > params["now"]
            ]
            for row in matching:
                row["used_at"] = params["now"]
            return [{"id": row["id"]} for row in matching]
        matching = rows
        if "installation" in params:
            matching = [r for r in matching if r["installation_id"] == params["installation"]]
        if "idempotency_key = $key" in query:
            matching = [
                r
                for r in matching
                if r["id"] == params["id"] or r["idempotency_key"] == params["key"]
            ]
        elif "approval_id: $id" in query:
            matching = [r for r in matching if r["approval_id"] == params["id"]]
        elif "id" in params and params["id"] is not None:
            matching = [r for r in matching if r["id"] == params["id"]]
        if "SET n += $props" in query:
            for row in matching:
                row.update(copy.deepcopy(params["props"]))
            return []
        if "SET n.used_at = $at" in query:
            for row in matching:
                row["used_at"] = params["at"]
            return []
        if "SET n.value = $value" in query:
            for row in matching:
                row["value"] = params["value"]
            return []
        assert "RETURN n" in query, query
        return [{"n": copy.deepcopy(row)} for row in matching]


class _Harness:
    def __init__(self, owner, store):
        self.owner, self.store = owner, store

    def call(self, function, command):
        return self.store.transaction(lambda tx: function(tx, command))

    def register(self):
        return self.call(kernel.register_attempt_in_tx, self.owner.attempt)

    def fence(self):
        return self.call(kernel.fence_attempt_in_tx, self.owner.barrier())

    def inspect(self):
        return self.call(kernel.inspect_outcome_in_tx, self.owner.inspect())

    def apply(self):
        return self.call(kernel.apply_effects_in_tx, self.owner.apply)

    def snapshot(self):
        return self.store.snapshot()


@pytest.fixture
def synthetic(kernel_owners):
    def make(*, expansion=False):
        owner = kernel_owners(expansion=expansion)
        return _Harness(owner, _MemoryStore(owner))

    return make


def test_synthetic_unknown_is_not_absence_and_barrier_requires_fresh_inspection(synthetic):
    h = synthetic()
    assert h.inspect().outcome == "UNKNOWN"
    registered = h.register()
    assert registered.outcome == "UNKNOWN" and registered.fence == 1
    assert h.inspect().outcome == "UNKNOWN"
    fenced = h.fence()
    assert fenced.outcome == "UNKNOWN" and fenced.proof is None and fenced.fence == 2
    proof = h.inspect()
    assert proof.outcome == "NOT_APPLIED_PROVEN"
    assert proof.proof.old_fence == 1 and proof.proof.barrier_fence == 2
    assert proof.proof.read_generation == h.snapshot()["world"]["generation"]


def test_synthetic_duplicate_register_apply_and_finalize_are_exact_noops(synthetic):
    h = synthetic()
    h.register()
    before = h.snapshot()
    h.register()
    assert h.snapshot() == before
    applied = h.apply()
    assert applied.outcome == "COMMITTED_EXACT"
    assert applied.projection.state_digest == h.owner.target.state_digest
    before = h.snapshot()
    assert h.apply() == applied
    assert h.snapshot() == before and h.owner.effects == 1
    final = h.owner.finalize()
    result = h.call(kernel.finalize_projection_in_tx, final)
    assert result.projection == h.owner.target
    before = h.snapshot()
    assert h.call(kernel.finalize_projection_in_tx, final) == result
    assert h.snapshot() == before
    assert len(before["tables"]["AuthorityApprovalUse"]) == 1
    assert before["world"]["sequence"] == 17


def test_synthetic_fence_refuses_delayed_registration_and_apply(synthetic):
    h = synthetic()
    h.register()
    h.fence()
    before = h.snapshot()
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.register()
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.apply()
    assert h.snapshot() == before and h.owner.effects == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_generation", 999),
        ("d3_sequence", 999),
        ("old_fence", 9),
        ("barrier_fence", 9),
        ("barrier_id", uid(999)),
        ("old_attempt_id", uid(999)),
        ("lifetime_id", uid(999)),
        ("operation_id", uid(999)),
        ("base_state_digest", "c" * 64),
        ("authority_epoch", 9),
    ],
)
def test_synthetic_cancellation_requires_every_exact_proof_field(synthetic, field, value):
    h = synthetic(expansion=True)
    h.register()
    h.fence()
    proof = replace(h.inspect().proof, **{field: value})
    before = h.snapshot()
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.call(kernel.cancel_intent_in_tx, h.owner.cancel(proof))
    assert h.snapshot() == before


def test_synthetic_cancel_burns_original_approval_and_cannot_revive(synthetic):
    h = synthetic(expansion=True)
    h.register()
    h.fence()
    command = h.owner.cancel(h.inspect().proof)
    result = h.call(kernel.cancel_intent_in_tx, command)
    assert result.outcome == "CANCELLED_EXACT"
    before = h.snapshot()
    assert h.call(kernel.cancel_intent_in_tx, command) == result
    assert h.register().outcome == h.apply().outcome == "CANCELLED_EXACT"
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.store.transaction(
            lambda tx: kernel.validate_approval_in_tx(
                tx,
                h.owner.binding,
                h.owner.prepare.payload.intent.authorization,
            )
        )
    assert h.snapshot() == before and h.owner.effects == 0
    assert before["tables"]["AuthorityApprovalUse"][0]["use_kind"] == "cancelled"


@pytest.mark.parametrize("fault", ["effect", "after_approval", "after_terminal"])
def test_synthetic_fault_rolls_back_all_effects(synthetic, monkeypatch, fault):
    h = synthetic()
    h.register()
    before = h.snapshot()
    if fault == "effect":
        h.owner.fail_effect = True
    else:
        original = kernel._use if fault == "after_approval" else kernel._terminal

        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic transaction fault")

        monkeypatch.setattr(
            kernel, "_use" if fault == "after_approval" else "_terminal", fail_after
        )
    with pytest.raises(RuntimeError, match="synthetic"):
        h.apply()
    assert h.snapshot() == before
    assert h.inspect().outcome == "UNKNOWN"


def test_synthetic_terminal_result_requires_current_owner_validation(synthetic):
    h = synthetic()
    h.register()
    h.apply()
    h.owner.fail_result = True
    before = h.snapshot()
    for operation in (h.inspect, h.register, h.apply):
        with pytest.raises(PublicationRefused, match="outcome:conflict"):
            operation()
    assert h.snapshot() == before


def test_synthetic_capacity_refuses_without_partial_registration(synthetic, monkeypatch):
    h = synthetic()
    before = h.snapshot()
    monkeypatch.setattr(kernel, "MAX_ROWS", 0)
    with pytest.raises(PublicationRefused, match="outcome:capacity"):
        h.register()
    assert h.snapshot() == before


def test_synthetic_seal_is_metadata_only_and_duplicate_is_noop(synthetic):
    h = synthetic()
    h.register()
    h.apply()
    h.call(kernel.finalize_projection_in_tx, h.owner.finalize())
    before = h.owner.target
    h.owner.journal.seal()
    h.owner.target = _projection(h.owner.journal)
    command = h.owner.command(
        SealCommand,
        seal=record_event(h.owner.journal.events[-1]),
        before=before,
        target=h.owner.target,
        expected_fence=1,
    )
    prior = h.snapshot()
    result = h.call(kernel.finalize_seal_in_tx, command)
    after = h.snapshot()
    assert result.projection == h.owner.target and not result.projection.ready
    assert after["world"]["generation"] == prior["world"]["generation"] + 1
    assert after["world"]["sequence"] == prior["world"]["sequence"]
    assert after["tables"]["AuthorityOperation"] == prior["tables"]["AuthorityOperation"]
    h.call(kernel.finalize_seal_in_tx, command)
    assert h.snapshot() == after


class _NativeStore:
    def __init__(self, driver, owner):
        self.writer = WorldWriter(driver, owner.world)
        self.world_id = owner.world

        def seed(tx):
            for label, rows in _seed_rows(owner).items():
                for row in rows:
                    tx.run(
                        f"CREATE (n:{label}) SET n += $row",
                        row={k: v for k, v in row.items() if k != "world_id"},
                    )

        self.transaction(seed)

    def transaction(self, work):
        return self.writer.transaction(work)

    def snapshot(self):
        def read(tx):
            world = (
                tx.unscoped()
                .run(
                    CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) RETURN "
                    "w.owner_user_id AS owner, w.next_sequence AS sequence, "
                    "w.read_generation AS generation",
                    world_id=tx.world_id,
                )
                .single()
            )
            tables = {}
            for label in TABLES:
                rows = tx.run(f"MATCH (n:{label}) RETURN n")
                tables[label] = sorted(
                    (row["n"] for row in rows), key=lambda row: str(row.get("id", ""))
                )
            return {"world": dict(world), "tables": tables}

        return self.transaction(read)


def _require_native_target():
    # Execute this before requesting driver/make_world. The default fixture can
    # load credentials and run migrations, so a decorator-only skip is inadequate.
    if os.environ.get("GROVE_AUTHORITY_NATIVE_TESTS") != "1":
        pytest.skip("native outcome controls require explicit local opt-in")
    if (
        os.environ.get("GROVE_NEO4J_URI", "bolt://127.0.0.1:57687") != "bolt://127.0.0.1:57687"
        or os.environ.get("GROVE_ENV", "development") != "development"
    ):
        pytest.fail("native outcome controls require the exact loopback development target")


@pytest.fixture
def native(request, monkeypatch, kernel_owners):
    _require_native_target()
    monkeypatch.setenv("GROVE_SLICE_ID", "forget")
    driver = request.getfixturevalue("driver")
    make_world = request.getfixturevalue("make_world")

    def make(*, expansion=False):
        world = make_world()
        create_world(driver, world, owner_user_id="owner-1")
        owner = kernel_owners(world, expansion=expansion)
        return _Harness(owner, _NativeStore(driver, owner))

    return make


def _race_locked(h, first, second, *, rollback=False):
    """B is submitted while A holds the real WorldWriter lock."""
    entered_a, attempted_b, entered_b, release_a = (threading.Event() for _ in range(4))

    def a(tx):
        value = first(tx)
        entered_a.set()
        if not release_a.wait(10):
            raise AssertionError("test failed to release transaction A")
        if rollback:
            raise RuntimeError("intentional native rollback")
        return value

    def b():
        attempted_b.set()

        def work(tx):
            entered_b.set()
            return second(tx)

        return h.store.transaction(work)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(h.store.transaction, a)
        future_b = None
        try:
            assert entered_a.wait(10), "transaction A did not enter"
            future_b = pool.submit(b)
            assert attempted_b.wait(10), "transaction B was not submitted"
            assert not entered_b.wait(0.2), "transaction B bypassed the World lock"
        finally:
            release_a.set()
        if rollback:
            with pytest.raises(RuntimeError, match="intentional native rollback"):
                future_a.result(timeout=20)
            result_a = None
        else:
            result_a = future_a.result(timeout=20)
        assert future_b is not None
        result_b = future_b.result(timeout=20)
        assert entered_b.is_set()
        return result_a, result_b


def test_native_commit_then_waiting_inspection_observes_committed_exact(native):
    h = native()
    h.register()
    command = h.owner.inspect()
    before = h.snapshot()
    a, b = _race_locked(
        h,
        lambda tx: kernel.apply_effects_in_tx(tx, h.owner.apply),
        lambda tx: kernel.inspect_outcome_in_tx(tx, command),
    )
    assert a.outcome == b.outcome == "COMMITTED_EXACT"
    assert b.projection.state_digest == h.owner.target.state_digest
    after = h.snapshot()
    assert after["world"]["generation"] == before["world"]["generation"] + 1
    assert after["world"]["sequence"] == before["world"]["sequence"]
    assert len(after["tables"]["AuthorityApprovalUse"]) == 1


def test_native_rollback_then_waiting_barrier_refuses_stale_callback(native):
    h = native()
    h.register()
    barrier = h.owner.barrier()
    before = h.snapshot()
    _, fenced = _race_locked(
        h,
        lambda tx: kernel.apply_effects_in_tx(tx, h.owner.apply),
        lambda tx: kernel.fence_attempt_in_tx(tx, barrier),
        rollback=True,
    )
    assert fenced.outcome == "UNKNOWN" and fenced.proof is None
    after = h.snapshot()
    assert after["world"]["generation"] == before["world"]["generation"] + 1
    assert after["tables"]["HistoryValue"] == before["tables"]["HistoryValue"]
    assert after["tables"]["ForgetApproval"] == before["tables"]["ForgetApproval"]
    assert after["tables"]["AuthorityApprovalUse"] == []
    assert h.inspect().outcome == "NOT_APPLIED_PROVEN"
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.apply()
    assert h.snapshot() == after


@pytest.mark.parametrize("delayed", ["register", "apply"])
def test_native_fence_first_blocks_delayed_registration_or_apply(native, delayed):
    h = native()
    h.register()
    barrier = h.owner.barrier()
    operation, command = (
        (kernel.register_attempt_in_tx, h.owner.attempt)
        if delayed == "register"
        else (
            kernel.apply_effects_in_tx,
            h.owner.apply,
        )
    )

    def stale(tx):
        with pytest.raises(PublicationRefused, match="outcome:conflict"):
            operation(tx, command)
        return "refused"

    _, result = _race_locked(h, lambda tx: kernel.fence_attempt_in_tx(tx, barrier), stale)
    assert result == "refused" and h.owner.effects == 0
    assert h.inspect().outcome == "NOT_APPLIED_PROVEN"
    assert h.snapshot()["tables"]["AuthorityApprovalUse"] == []


def test_native_lost_barrier_ack_requires_fresh_exact_observation(native):
    h = native()
    h.register()
    barrier = h.owner.barrier()

    def lose_ack():
        h.call(kernel.fence_attempt_in_tx, barrier)
        raise ConnectionError("synthetic lost acknowledgement after native commit")

    with pytest.raises(ConnectionError, match="lost acknowledgement"):
        lose_ack()
    before = h.snapshot()
    assert h.call(kernel.fence_attempt_in_tx, barrier).proof is None
    observed = h.inspect()
    assert observed.outcome == "NOT_APPLIED_PROVEN"
    assert observed.proof.barrier_id == barrier.barrier_id
    assert observed.proof.read_generation == before["world"]["generation"]
    assert observed.proof.d3_sequence == before["world"]["sequence"]
    assert h.snapshot() == before


def test_native_cancel_prevents_old_approval_and_revival(native):
    h = native(expansion=True)
    h.register()
    h.fence()
    command = h.owner.cancel(h.inspect().proof)
    before = h.snapshot()
    result, stale = _race_locked(
        h,
        lambda tx: kernel.cancel_intent_in_tx(tx, command),
        lambda tx: kernel.apply_effects_in_tx(tx, h.owner.apply),
    )
    assert result.outcome == stale.outcome == "CANCELLED_EXACT"
    after = h.snapshot()
    assert after["world"]["generation"] == before["world"]["generation"] + 1
    assert after["world"]["sequence"] == before["world"]["sequence"]
    assert after["tables"]["AuthorityApprovalUse"][0]["use_kind"] == "cancelled"
    assert h.register().outcome == h.apply().outcome == "CANCELLED_EXACT"
    assert h.call(kernel.cancel_intent_in_tx, command).outcome == "CANCELLED_EXACT"
    with pytest.raises(PublicationRefused, match="outcome:conflict"):
        h.store.transaction(
            lambda tx: kernel.validate_approval_in_tx(
                tx,
                h.owner.binding,
                h.owner.prepare.payload.intent.authorization,
            )
        )
    assert h.owner.effects == 0 and h.snapshot() == after


def test_native_competing_finalizers_mutate_metadata_and_generation_once(native):
    h = native()
    h.register()
    h.apply()
    command = h.owner.finalize()
    before = h.snapshot()
    first, second = _race_locked(
        h,
        lambda tx: kernel.finalize_projection_in_tx(tx, command),
        lambda tx: kernel.finalize_projection_in_tx(tx, command),
    )
    assert first == second and first.projection == h.owner.target
    after = h.snapshot()
    assert after["world"]["generation"] == before["world"]["generation"] + 1
    assert after["world"]["sequence"] == before["world"]["sequence"]
    for label in TABLES:
        if label != "AuthorityProjection":
            assert after["tables"][label] == before["tables"][label]
    assert after["tables"]["AuthorityProjection"][0].get("active_intent_id") is None
    h.call(kernel.finalize_projection_in_tx, command)
    assert h.snapshot() == after


@pytest.mark.parametrize("fault", ["effect", "after_approval", "after_terminal"])
def test_native_atomicity_fault_restores_effect_approval_outcome_and_generation(
    native, monkeypatch, fault
):
    h = native()
    h.register()
    before = h.snapshot()
    if fault == "effect":
        h.owner.fail_effect = True
    else:
        name = "_use" if fault == "after_approval" else "_terminal"
        original = getattr(kernel, name)

        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic fault inside native transaction")

        monkeypatch.setattr(kernel, name, fail_after)
    with pytest.raises(RuntimeError, match="synthetic"):
        h.apply()
    assert h.snapshot() == before
    assert h.inspect().outcome == "UNKNOWN"


def test_native_duplicate_attempt_and_apply_are_noops(native):
    h = native()
    h.register()
    before = h.snapshot()
    h.register()
    assert h.snapshot() == before
    first = h.apply()
    after = h.snapshot()
    assert h.apply() == first
    assert h.snapshot() == after and h.owner.effects == 1


@pytest.mark.parametrize(
    "uri,environment",
    [
        ("bolt://localhost:57687", "development"),
        ("bolt://127.0.0.1:7687", "development"),
        ("bolt://127.0.0.1:57687", "production"),
    ],
)
def test_native_target_guard_refuses_before_database_fixture_request(monkeypatch, uri, environment):
    monkeypatch.setenv("GROVE_AUTHORITY_NATIVE_TESTS", "1")
    monkeypatch.setenv("GROVE_NEO4J_URI", uri)
    monkeypatch.setenv("GROVE_ENV", environment)
    with pytest.raises(pytest.fail.Exception, match="exact loopback development target"):
        _require_native_target()
