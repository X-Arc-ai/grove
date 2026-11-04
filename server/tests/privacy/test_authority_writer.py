"""Real inactive binder and local files with explicit synthetic resource owners.

The in-memory driver is an adapter control, not native Neo4j evidence. Legacy
domain application is an explicit test seam; register/inspect/finalize remain real.
"""

import copy
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from grove import clock
from grove.authority.publication import ObservedFile, PublicationRefused
from grove.authority.publisher import open_publication
from grove.writer import authority_forget, authority_outcomes
from grove.writer.authority_adapter import AuthorityOwners, CapturedJournal, InactiveAuthorityWriter
from grove.writer.authority_records import (
    InspectionCommand,
    check_binding,
    check_command,
)
from grove.writer.tx import WorldTx
from tests.privacy.test_authority_codec import canonical, hashed, uid
from tests.privacy.test_authority_legacy import WORLD, payload, row
from tests.privacy.test_authority_publication import (
    FileJournal,
    forget_proposal,
    install,
    proposal_for,
)
from tests.privacy.test_authority_publication import (
    fixed_publication_clock as fixed_publication_clock,
)
from tests.privacy.test_authority_publication_files import private_root


class Rows:
    def __init__(self, values=(), *, changed=False):
        self.values = list(values)
        self.changed = changed

    def __iter__(self):
        return iter(self.values)

    def single(self):
        assert len(self.values) <= 1
        return self.values[0] if self.values else None

    def consume(self):
        return SimpleNamespace(counters=SimpleNamespace(contains_updates=self.changed))


class Native:
    """A deterministic query responder for the actual WorldTx wrapper."""

    def __init__(self, driver):
        self.driver = driver

    def run(self, statement, parameters=None, **kwargs):
        p = {**(parameters or {}), **kwargs}
        db = self.driver
        db.calls.append(statement)
        if "lock_marker" in statement:
            return Rows([{"id": WORLD, "generation": db.data["generation"]}])
        if "read_generation = w.read_generation + 1" in statement:
            db.data["generation"] += 1
            return Rows([{"generation": db.data["generation"]}], changed=True)
        if "owner_user_id AS owner" in statement:
            return Rows(
                [
                    {
                        "owner": "owner-1",
                        "sequence": db.data["sequence"],
                        "generation": db.data["generation"],
                    }
                ]
            )
        if "count(n)" in statement:
            count = len(db.data["operations"]) if "AuthorityOperation" in statement else 0
            return Rows([{"count": count}])
        if "AuthorityProjection" in statement:
            if "SET n +=" in statement:
                db.data["projection"].update(p["props"])
                return Rows(changed=True)
            return Rows([{"n": copy.deepcopy(db.data["projection"])}])
        if "AuthorityOperation" in statement:
            if "CREATE" in statement:
                item = {**p["row"], "world_id": WORLD}
                assert item["id"] not in db.data["operations"]
                db.data["operations"][item["id"]] = item
                return Rows(changed=True)
            if "SET n +=" in statement:
                db.data["operations"][p["id"]].update(p["props"])
                return Rows(changed=True)
            return Rows(
                {"n": copy.deepcopy(item)}
                for item in db.data["operations"].values()
                if item["id"] == p.get("id") or item["idempotency_key"] == p.get("key")
            )
        raise AssertionError("unexpected synthetic database query")


class MemoryDriver:
    def __init__(self, fixture, paths):
        reduction = fixture.decode()
        ledger = reduction.ledger
        self.data = {
            "projection": {
                "world_id": WORLD,
                "owner_id": "owner-1",
                "installation_id": uid(1),
                "profile_code": "authority_state_v1",
                "profile_digest": fixture.header["schema_profile"]["digest"],
                "installation_epoch": reduction.installation_epoch,
                "authority_epoch": reduction.authority_epoch,
                "erasure_revision": reduction.erasure_revision,
                "sequence": len(fixture.events),
                "head_digest": hashed(fixture.events[-1]),
                "state_digest": reduction.state_digest,
                "ledger_format": ledger.format,
                "ledger_file_digest": ledger.file_digest,
                "ledger_rows_digest": ledger.rows_digest,
                "ledger_count": ledger.count,
                "ready": True,
                "fence": 0,
                "active_intent_id": None,
            },
            "operations": {},
            "generation": 11,
            "sequence": 7,
            "effects": 0,
        }
        self.paths, self.calls = paths, []
        self.callbacks = self.effect_callbacks = 0
        self.inside = False
        self.retry_effect = self.lose_effect_response = False

    @contextmanager
    def session(self, **kwargs):
        yield self

    def execute_write(self, callback):
        saved = copy.deepcopy(self.data)
        files = {key: path.read_bytes() for key, path in self.paths.items()}

        def attempt():
            self.callbacks += 1
            self.inside = True
            try:
                return callback(Native(self))
            finally:
                self.inside = False
                assert {key: path.read_bytes() for key, path in self.paths.items()} == files

        try:
            result = attempt()
            changed_effect = self.data["effects"] > saved["effects"]
            if changed_effect and self.retry_effect:
                self.retry_effect = False
                self.data = copy.deepcopy(saved)
                result = attempt()
        except Exception:
            self.data = saved
            raise
        if changed_effect and self.lose_effect_response:
            self.lose_effect_response = False
            raise OSError("synthetic response loss after committed callback")
        return result


class Resource:
    def __init__(self, harness, name):
        self.harness, self.name = harness, name
        self.resolved = []
        self.corrupt = None

    def resolve(self, requirements, bindings, state):
        assert bindings == self.harness.bindings and state.world_id == WORLD
        self.resolved.append(requirements)
        if requirements and self.corrupt == "drop":
            return requirements[:-1]
        if requirements and self.corrupt == "binding":
            return (replace(requirements[0], binding_digest="c" * 64), *requirements[1:])
        return requirements


class Installation(Resource):
    def validate_binding(self, bindings, world_id):
        assert bindings == self.harness.bindings and world_id == WORLD

    def validate_projection_in_tx(self, tx, bindings, projection):
        assert type(tx) is WorldTx and tx.world_id == WORLD
        assert projection["installation_id"] == bindings.installation_id


class Principal(Resource):
    current = True

    def validate_current(self, tx, state, proposal):
        self.harness.events.append("principal")
        if not self.current:
            raise PublicationRefused("binding", "outcome")
        assert state.owner_id == "owner-1"

    def approval_in_tx(self, tx, state, approval, *, historical):
        raise AssertionError("legacy effect seam must not claim native approval evidence")


class References(Resource):
    current = True

    def validate_in_tx(self, tx, state, prepare, phase):
        self.harness.events.append("references:" + phase)
        if not self.current:
            raise PublicationRefused("conflict", "outcome")
        assert prepare == self.harness.proposal.initial_event

    def validate_legacy_in_tx(self, tx, state, effect, inventory):
        if not self.current:
            raise PublicationRefused("conflict", "outcome")

    def validate_result_in_tx(self, tx, state, operation, projection):
        if not self.current:
            raise PublicationRefused("conflict", "outcome")


class LocalJournal(Resource):
    def observe(self, role):
        path = self.harness.paths[role]
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        assert before == after
        return raw, ObservedFile(
            role,
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            sha256(raw).hexdigest(),
        )

    def capture(self, bindings, world_id):
        assert bindings == self.harness.bindings and world_id == WORLD
        self.harness.events.append("capture")
        values = [self.observe(role) for role in ("manifest", "journal", "legacy")]
        return CapturedJournal(*(item[0] for item in values), tuple(item[1] for item in values))

    def confirm(self, capture, bindings, world_id):
        assert bindings == self.harness.bindings and world_id == WORLD
        assert (
            tuple(self.observe(role)[1] for role in ("manifest", "journal", "legacy"))
            == capture.files
        )


class Work:
    def __init__(self, resource):
        self.resource = resource

    def assert_current(self, bindings, world_id, operation_kind):
        assert self.resource.active and bindings == self.resource.harness.bindings
        assert world_id == WORLD and operation_kind == self.resource.harness.writer.operation_kind

    def drain(self):
        owner = self.resource
        owner.harness.events.append("drain")
        if owner.drain_hook is not None:
            owner.drain_hook()
        return owner.drain_result


class Workers(Resource):
    active = False
    drain_hook = None
    drain_result = None

    @contextmanager
    def operation(self, bindings, world_id, operation_kind):
        assert bindings == self.harness.bindings and world_id == WORLD
        assert operation_kind == self.harness.writer.operation_kind
        self.active = True
        self.harness.events.append("enter")
        try:
            yield Work(self)
        finally:
            self.harness.events.append("exit")
            self.active = False


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.root, self.bindings = private_root(tmp_path)
        self.before, self.after = payload([]), payload([row(1)])
        self.fixture = FileJournal(self.before)
        self.paths = install(self.root, self.fixture, self.before)
        self.db = MemoryDriver(self.fixture, self.paths)
        self.proposal = forget_proposal(self.fixture, self.before, self.after)
        self.events = []
        self.installation = Installation(self, "installation")
        self.principal = Principal(self, "principal")
        self.references = References(self, "references")
        self.journal = LocalJournal(self, "journal")
        self.workers = Workers(self, "workers")
        self.owners = AuthorityOwners(
            self.installation, self.principal, self.references, self.journal, self.workers
        )
        self.writer = self.new_writer()
        self.final_targets = []
        actual_finalize = authority_outcomes.finalize_projection_in_tx

        def prepare_legacy(tx, runtime, before, after, effect):
            owner = check_binding(runtime, tx)
            assert owner is self.writer
            assert (before, after) == (self.before, self.after)
            owner._validate_legacy_in_tx(tx, effect, {})
            self.events.append("preflight_legacy")
            return SimpleNamespace(effect=effect)

        def synthetic_effect(tx, command):
            owner = check_command(command, tx)
            assert owner is self.writer and self.db.inside
            self.db.effect_callbacks += 1
            self.events.append("effect")
            # These are actual durable local files, not the proposal's templates.
            assert self.paths["legacy"].read_bytes() == self.after
            assert canonical(self.fixture.events[-2]) in self.paths["journal"].read_bytes()
            assert canonical(self.fixture.events[-1]) not in self.paths["journal"].read_bytes()
            owner._validate_in_tx(tx, command.prepare, "apply")
            world = authority_outcomes._world(tx, owner)
            projection = authority_outcomes._projection(tx, owner)
            operation = authority_outcomes._operation(
                tx, owner, command.prepare.payload.intent.intent_id
            )
            if operation.get("outcome"):
                return authority_outcomes._fact(operation, projection)
            self.db.data["effects"] += 1
            self.db.data["sequence"] += 1
            return authority_outcomes._terminal(tx, owner, command, operation, projection, world)

        def finalize(tx, command):
            assert self.db.inside
            self.events.append("finalize")
            self.final_targets.append(command.target)
            return actual_finalize(tx, command)

        monkeypatch.setattr(authority_forget, "prepare_legacy_forget", prepare_legacy)
        monkeypatch.setattr(authority_outcomes, "apply_effects_in_tx", synthetic_effect)
        monkeypatch.setattr(authority_outcomes, "finalize_projection_in_tx", finalize)

    def new_writer(self, **changes):
        arguments = dict(
            owners=self.owners,
            world_id=WORLD,
            proposal=self.proposal,
            legacy_before=self.before,
            legacy_after=self.after,
        )
        arguments.update(changes)
        return InactiveAuthorityWriter(self.db, **arguments)

    def open(self):
        return open_publication(
            str(self.root),
            self.bindings,
            checks=self.writer,
            deadline=clock.monotonic() + 30,
        )

    def snapshot(self):
        return {key: path.read_bytes() for key, path in self.paths.items()}

    def pending(self, session):
        candidate = session.stage_legacy(WORLD, self.after)
        pending = session.prepare(WORLD, self.proposal)
        session.publish_legacy(pending, candidate)
        return pending


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_real_binder_preflight_precedes_stage_and_preserves_requirement_tuples(harness):
    h = harness
    before, data = h.snapshot(), copy.deepcopy(h.db.data)
    with h.open() as session:
        assert session.bind_writer(h.writer) is session
        assert h.snapshot() == before and h.db.data == data
        # Projection, capacity and legacy planning are separate read-only callbacks.
        assert "preflight_legacy" in h.events and h.db.callbacks == 3
        state = session.inspect(WORLD)
        observed = h.writer.check_current(state, h.proposal)
        assert observed.requirements == state.requirements
        assert observed.requirements
        for service in (h.installation, h.principal, h.references, h.journal, h.workers):
            assert service.resolved
            assert all(type(value) is tuple for value in service.resolved)
            for value in service.resolved:
                assert all(requirement in state.requirements for requirement in value)
        candidate = session.stage_legacy(WORLD, h.after)
        assert candidate is not None
        assert h.events.index("preflight_legacy") < len(h.events)
    assert h.events[-2:] == ["drain", "exit"]


@pytest.mark.parametrize("name", ["installation", "principal", "references", "journal", "workers"])
def test_missing_actual_owner_refuses_before_any_resource_mutation(harness, name):
    h = harness
    before = h.snapshot()
    arguments = {
        key: getattr(h.owners, key)
        for key in ("installation", "principal", "references", "journal", "workers")
    }
    assert h.new_writer() is not None
    arguments[name] = None
    with pytest.raises(PublicationRefused, match="adapter_missing"):
        AuthorityOwners(**arguments)
    assert h.snapshot() == before and not h.db.calls


def test_unsupported_effect_refuses_at_constructor_before_file_or_database_access(harness):
    h = harness
    before = h.snapshot()
    assert h.new_writer() is not None
    unsupported = proposal_for(copy.deepcopy(FileJournal(h.before)))
    with pytest.raises(PublicationRefused, match="adapter_missing"):
        h.new_writer(proposal=unsupported, legacy_before=None, legacy_after=None)
    assert h.snapshot() == before and not h.db.calls


@pytest.mark.parametrize("corrupt", ["drop", "binding"])
def test_owner_must_return_exact_required_tuple_before_stage(harness, corrupt):
    h = harness
    before = h.snapshot()
    h.installation.corrupt = corrupt
    with h.open() as session:
        with pytest.raises(PublicationRefused):
            session.bind_writer(h.writer)
        with pytest.raises(PublicationRefused):
            session.stage_legacy(WORLD, h.after)
    assert h.installation.resolved[0]
    assert h.snapshot() == before and not h.db.calls
    assert not list(h.root.rglob(".publication-*"))


def test_publication_uses_durable_prepare_and_legacy_and_exact_final_projection(harness):
    h = harness
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        assert h.db.data["effects"] == 0
        result = session.complete(pending, h.proposal.completion_template)
        assert result.code == "committed"
        assert result.requirements == result.state.requirements
        assert h.final_targets == [result.state.projection]
        assert h.writer._native.projection == result.state.projection
        assert h.db.data["projection"]["active_intent_id"] is None
        assert h.db.data["projection"]["fence"] == 1
        assert h.db.data["projection"]["ready"] == result.state.projection.ready
    assert h.db.data["effects"] == 1
    assert h.snapshot()["legacy"] == h.after
    assert h.events.index("effect") < h.events.index("finalize") < h.events.index("drain")


def test_copied_runtime_command_and_wrong_owner_do_not_transfer_authority(harness):
    h = harness
    with h.open() as session:
        session.bind_writer(h.writer)
        tx = WorldTx(Native(h.db), WORLD)
        runtime = h.writer._binding
        assert check_binding(runtime, tx) is h.writer
        for forged in (replace(runtime), replace(runtime, owner=copy.copy(h.writer))):
            with pytest.raises(PublicationRefused, match="binding"):
                check_binding(forged, tx)
        command = InspectionCommand(
            runtime,
            h.proposal.initial_event.payload.intent.intent_id,
            h.proposal.initial_event.payload.intent.intent_digest,
            h.writer._current.state.projection,
        )
        h.writer._issue(command)
        assert check_command(command, tx) is h.writer
        with pytest.raises(PublicationRefused, match="binding"):
            check_command(replace(command), tx)
        with pytest.raises(PublicationRefused, match="binding"):
            copy.copy(h.writer)._assert_live()
