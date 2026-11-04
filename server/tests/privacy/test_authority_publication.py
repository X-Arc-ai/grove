"""An explicit synthetic owner exercises local publication; no native database."""

import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.authority.codec import DecoderBudget, decode_event, decode_manifest, manifest_checksum
from grove.authority.legacy import decode_legacy, legacy_row
from grove.authority.legacy_reader import ledger_name
from grove.authority.publication import (
    CheckObservation,
    InitializationRequirements,
    OutcomeObservation,
    ProposedPublication,
    PublicationRefused,
    inspect_initialization,
)
from grove.authority.publisher import open_publication
from grove.authority.records import TYPES, AuthorityCodecRefused
from tests.privacy.test_authority_codec import (
    PROFILE_RAW,
    H,
    Journal,
    canonical,
    controller,
    effect,
    hashed,
    resign,
    uid,
)
from tests.privacy.test_authority_legacy import WORLD, payload, row
from tests.privacy.test_authority_publication_files import private_root


@pytest.fixture(autouse=True)
def fixed_publication_clock(monkeypatch):
    # Keep monotonic time real for bounded thread/process exclusion tests.
    monkeypatch.setattr(clock, "now", lambda world=None: datetime(2026, 11, 4, 12, 4, tzinfo=UTC))


def binding(raw, world=WORLD):
    ledger = decode_legacy(raw, world_id=world, budget=DecoderBudget()).binding
    return {
        name: getattr(ledger, name) for name in ("format", "count", "file_digest", "rows_digest")
    }


class FileJournal(Journal):
    def __init__(self, raw, world=WORLD):
        self.world = world
        self.events, self.pending, self.sealed = [], None, False
        self.header = {
            "format": "grove.authority-journal.v1",
            "world_id": world,
            "owner_id": "owner-1",
            "installation_id": uid(1),
            "genesis_digest": H,
            "schema_profile": PROFILE_RAW,
        }
        self.state = {
            "format": "grove.authority-state.v1",
            "installation_id": uid(1),
            "world_id": world,
            "owner_id": "owner-1",
            "profile": PROFILE_RAW,
            "installation_epoch": 1,
            "authority_epoch": 0,
            "erasure_revision": 0,
            "ledger": None,
            "applied": [],
            "cancelled": [],
            "burned_approvals": [],
            "closed": False,
        }
        ledger = binding(raw, world)
        self.prepare(
            [
                effect(
                    "origin",
                    mode="adopt",
                    owner_id="owner-1",
                    ledger=ledger,
                    inventory_digest=H,
                    host_policy="none",
                )
            ],
            auth=controller("adopt"),
            mode="origin",
            kind="adopt_world",
            extra={
                "owner_id": "owner-1",
                "installation_id": uid(1),
                "activation_id": uid(2),
                "genesis_digest": H,
                "ledger": ledger,
                "inventory_digest": H,
                "live_store_digest": H,
                "prior_authority_digest": None,
            },
        )
        self.commit()

    def event(self, *args, **kwargs):
        event = super().event(*args, **kwargs)
        event["world_id"] = self.world
        resign(event)
        return event

    def prepare(self, *args, **kwargs):
        intent = super().prepare(*args, **kwargs)
        authorization = intent["authorization"]
        if authorization["kind"] == "owner_approval":
            authorization["payload_digest"] = hashed(
                [
                    "grove.authority.choice.v1",
                    self.world,
                    intent["base_sequence"],
                    intent["base_head_digest"],
                    intent["expected_installation_epoch"],
                    intent["expected_authority_epoch"],
                    intent["expected_erasure_revision"],
                    intent["effects"],
                ]
            )
        without = {k: v for k, v in intent.items() if k != "intent_digest"}
        intent["intent_digest"] = hashed(["grove.authority.intent.v1", self.world, without])
        resign(self.events[-1])
        return intent

    def manifest(self):
        result = super().manifest()
        result["worlds"][0]["world_id"] = self.world
        result["checksum"] = hashed({k: v for k, v in result.items() if k != "checksum"})
        return result


def record_event(raw):
    return decode_event(canonical(raw), budget=DecoderBudget())


def record_manifest(raw):
    return decode_manifest(canonical(raw), budget=DecoderBudget())


def proposal_for(fixture, effects=None, *, mode="ordinary", resolution=None):
    fixture.prepare(
        effects or [effect("class_create", 77, class_id="class-1", label_value_id="value-1")],
        mode=mode,
    )
    initial = record_event(fixture.events[-1])
    pending = record_manifest(fixture.manifest())
    fixture.commit(resolution=resolution)
    return ProposedPublication(
        initial, record_event(fixture.events[-1]), pending, record_manifest(fixture.manifest())
    )


def install(root, fixture, raw):
    paths = {
        "manifest": root / "authority-manifest.json",
        "journal": root / "authority" / ledger_name(fixture.world),
        "legacy": root / ledger_name(fixture.world),
    }
    for role, data in (
        ("manifest", canonical(fixture.manifest())),
        ("journal", fixture.raw()),
        ("legacy", raw),
    ):
        paths[role].write_bytes(data)
        paths[role].chmod(0o600)
    return paths


class SyntheticOwner:
    """Test-only deterministic resource owner, not a production authorization."""

    def __init__(self, bindings, fixture):
        self.bindings, self.fixture = bindings, fixture
        self.lifetime_id, self.operation_id = uid(9000), uid(9001)
        self.active = False
        self.calls = []
        self.outcome = "COMMITTED_EXACT"
        self.corrupt = None
        self.failed_metadata = False
        self.effects = 0
        self.projection = None

    @contextmanager
    def operation(self, bindings, world_id, operation_kind):
        assert bindings == self.bindings and world_id == self.fixture.world
        self.active = True
        self.calls.append("enter")
        try:
            yield self
        finally:
            self.calls.append("exit")
            self.active = False

    def assert_current(self, bindings, world_id, operation_kind):
        if not self.active or bindings != self.bindings or world_id != self.fixture.world:
            raise PublicationRefused("binding")

    def check(self, state, intent=None):
        self.assert_current(self.bindings, state.world_id, "publication")
        return CheckObservation(
            self.lifetime_id, self.operation_id, self.bindings, state, state.requirements, intent
        )

    def check_current(self, state, proposal):
        self.calls.append("current")
        intent = (
            None
            if proposal is None
            else (
                proposal.initial_event.payload.intent.intent_digest
                if type(proposal) is ProposedPublication
                else proposal.digest
            )
        )
        value = self.check(state, intent)
        if self.corrupt == "check":
            return replace(value, requirements=())
        return value

    def check_legacy_extension(self, pending, before, after, row):
        self.calls.append("ledger")
        return self.check(pending.state, pending.intent_digest)

    def observation(self, pending, projection=None):
        reduction = self.fixture.decode()
        target = TYPES["ProjectionBinding"](
            installation_id=uid(1),
            installation_epoch=reduction.installation_epoch,
            authority_epoch=reduction.authority_epoch,
            sequence=len(self.fixture.events),
            head_digest=hashed(self.fixture.events[-1]),
            schema_profile=pending.state.projection.schema_profile,
            state_digest=reduction.state_digest,
            ledger=reduction.ledger,
            ready=reduction.release_state in ("ready", "no_host_policy"),
        )
        value = OutcomeObservation(
            lifetime_id=self.lifetime_id,
            operation_id=self.operation_id,
            bindings=self.bindings,
            world_id=self.fixture.world,
            intent_id=pending.intent_id,
            intent_digest=pending.intent_digest,
            outcome=self.outcome,
            owner_id="owner-1",
            erasure_revision=reduction.erasure_revision,
            state_digest=reduction.state_digest,
            projection=projection
            or (
                target
                if pending.next_stage == "retry"
                else replace(
                    target,
                    sequence=pending.state.sequence,
                    head_digest=pending.state.head_digest,
                    ready=False,
                )
            ),
            ledger=reduction.ledger,
            requirements=reduction.requirements,
        )
        if self.corrupt and self.corrupt != "check":
            changes = {
                "intent_digest": "b" * 64,
                "state_digest": "c" * 64,
                "operation_id": uid(9002),
                "lifetime_id": uid(9003),
                "world_id": "other",
                "owner_id": "other",
                "erasure_revision": 99,
                "requirements": (),
                "projection": replace(value.projection, authority_epoch=99),
                "ledger": replace(value.ledger, count=99),
            }
            value = replace(value, **{self.corrupt: changes[self.corrupt]})
        return value

    def resolve_outcome(self, pending):
        self.calls.append("outcome")
        return self.observation(pending)

    def finalize_projection(self, pending, exact_target_projection):
        self.calls.append("metadata")
        if self.failed_metadata:
            raise OSError("synthetic metadata failure")
        self.projection = exact_target_projection
        return self.observation(pending, exact_target_projection)

    def check_recovery(self, state, operation, resolution_event):
        self.calls.append("recovery")
        checked = self.check(state, operation.initial_event.payload.intent.intent_digest)
        if (
            resolution_event is not None
            and resolution_event.kind == "recover"
            and resolution_event.payload.resolution == "cancel_unapplied_expansion"
        ):
            prior = OutcomeObservation(
                lifetime_id=self.lifetime_id,
                operation_id=self.operation_id,
                bindings=self.bindings,
                world_id=state.world_id,
                intent_id=operation.initial_event.payload.intent.intent_id,
                intent_digest=operation.initial_event.payload.intent.intent_digest,
                outcome="NOT_APPLIED_PROVEN",
                owner_id=state.owner_id,
                erasure_revision=state.erasure_revision,
                state_digest=state.state_digest,
                projection=state.projection,
                ledger=state.ledger,
                requirements=state.requirements,
            )
            checked = replace(checked, prior_outcome=prior)
        return checked

    def check_final(self, state, exact_target_projection):
        self.calls.append("final")
        return self.check(state)


def setup(tmp_path, *, rows=()):
    root, bindings = private_root(tmp_path)
    raw = payload(list(rows))
    fixture = FileJournal(raw)
    paths = install(root, fixture, raw)
    owner = SyntheticOwner(bindings, fixture)
    return root, bindings, fixture, paths, owner


def open_session(root, bindings, owner):
    return open_publication(str(root), bindings, checks=owner, deadline=clock.monotonic() + 30)


def test_missing_owner_adapter_never_writes(tmp_path):
    root, bindings, _, paths, _ = setup(tmp_path)
    before = {p: p.read_bytes() for p in paths.values()}
    with pytest.raises(PublicationRefused, match="adapter_missing"):
        open_publication(str(root), bindings, deadline=clock.monotonic() + 5)
    assert {p: p.read_bytes() for p in paths.values()} == before
    assert not tuple(root.glob(".publication-*"))


def test_prepare_complete_and_exact_retry(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    before = paths["journal"].read_bytes()
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        assert pending.next_stage == "outcome"
        assert paths["journal"].read_bytes() != before
        assert (
            paths["manifest"].read_bytes() == canonical(fixture.manifest())
            or b'"pending"' in paths["manifest"].read_bytes()
        )
        assert "outcome" not in owner.calls
        result = session.complete(pending, proposal.completion_template)
        assert result.code == "committed" and result.remaining
        assert result.state.sequence == 4
        assert result.requirements
    final = {k: p.read_bytes() for k, p in paths.items()}
    with open_session(root, bindings, owner) as session:
        retry = session.prepare(WORLD, proposal)
        assert retry.code == "committed"
    assert {k: p.read_bytes() for k, p in paths.items()} == final
    assert owner.calls.count("metadata") == 1
    assert owner.calls.index("outcome") < owner.calls.index("metadata") < owner.calls.index("final")


@pytest.mark.parametrize(
    "field",
    [
        "intent_digest",
        "state_digest",
        "operation_id",
        "lifetime_id",
        "world_id",
        "owner_id",
        "erasure_revision",
        "projection",
        "ledger",
        "requirements",
    ],
)
def test_completion_requires_exact_owner_outcome(tmp_path, field):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        before = paths["journal"].read_bytes()
        owner.corrupt = field
        with pytest.raises(PublicationRefused):
            session.complete(pending, proposal.completion_template)
        assert paths["journal"].read_bytes() == before
        assert "metadata" not in owner.calls


@pytest.mark.parametrize("outcome", ["UNKNOWN", "NOT_APPLIED_PROVEN", "CANCELLED_EXACT"])
def test_noncommit_never_completes_ordinary_intent(tmp_path, outcome):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        owner.outcome = outcome
        with pytest.raises(PublicationRefused):
            session.complete(pending, proposal.completion_template)
        assert b'"pending"' in paths["manifest"].read_bytes()


def test_candidate_arithmetic_does_not_replace_semantic_replay(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    before = {k: p.read_bytes() for k, p in paths.items()}
    changed = replace(proposal.completed_manifest.worlds[0], erasure_revision=7)
    manifest = replace(proposal.completed_manifest, worlds=(changed,))
    manifest = replace(manifest, checksum=manifest_checksum(manifest))
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused):
            session.prepare(WORLD, replace(proposal, completed_manifest=manifest))
    assert {k: p.read_bytes() for k, p in paths.items()} == before


def test_manifest_cannot_drop_other_world(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    raw = fixture.manifest()
    other = copy.deepcopy(raw["worlds"][0])
    other["world_id"] = "z-other"
    raw["worlds"].append(other)
    raw["checksum"] = hashed({k: v for k, v in raw.items() if k != "checksum"})
    paths["manifest"].write_bytes(canonical(raw))
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused, match="conflict"):
            session.prepare(WORLD, proposal)
    assert paths["manifest"].read_bytes() == canonical(raw)


def test_legacy_extension_preserves_all_prior_row_bytes(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path, rows=[row(1)])
    before = paths["legacy"].read_bytes()
    after = payload([row(1), row(2)])
    parsed = decode_legacy(after, world_id=WORLD, budget=DecoderBudget())
    new = legacy_row(parsed, 1)
    effects = [
        effect(
            "forget_apply",
            90,
            row={
                k: getattr(new, k)
                for k in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
            },
            before_ledger=binding(before),
            after_ledger=binding(after),
            expected_revision=0,
            revision=1,
        )
    ]
    proposal = proposal_for(fixture, effects, mode="restrictive")
    with open_session(root, bindings, owner) as session:
        session.stage_legacy(WORLD, after)
        pending = session.prepare(WORLD, proposal)
        assert paths["legacy"].read_bytes() == before
        with pytest.raises(PublicationRefused, match="pending"):
            session.complete(pending, proposal.completion_template)
    # A refused session is burned. A fresh recovery is required after prepare.
    # Complete the independent positive path in a second private fixture below.


def test_legacy_positive_protocol(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    before, after = paths["legacy"].read_bytes(), payload([row(1)])
    new = legacy_row(decode_legacy(after, world_id=WORLD, budget=DecoderBudget()), 0)
    proposal = proposal_for(
        fixture,
        [
            effect(
                "forget_apply",
                90,
                row={
                    k: getattr(new, k)
                    for k in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
                },
                before_ledger=binding(before),
                after_ledger=binding(after),
                expected_revision=0,
                revision=1,
            )
        ],
        mode="restrictive",
    )
    with open_session(root, bindings, owner) as session:
        candidate = session.stage_legacy(WORLD, after)
        pending = session.prepare(WORLD, proposal)
        session.publish_legacy(pending, candidate)
        assert paths["legacy"].read_bytes() == after
        result = session.complete(pending, proposal.completion_template)
        assert result.state.erasure_revision == 1
    assert owner.calls.index("ledger") < owner.calls.index("outcome")


def test_initialization_never_grants_bootstrap(tmp_path):
    root, bindings, _, _, _ = setup(tmp_path)
    files = set(root.iterdir())
    result = inspect_initialization(InitializationRequirements("new_empty", bindings, ()))
    assert "bootstrap_owner_not_implemented" in result.missing
    assert set(root.iterdir()) == files


def test_read_path_does_not_create_or_repair(tmp_path, monkeypatch):
    root, bindings, _, _, owner = setup(tmp_path)

    def forbidden(*a, **k):
        raise AssertionError("inspection mutated a file")

    with open_session(root, bindings, owner) as session:
        import os

        for name in ("write", "replace", "unlink", "mkdir", "chmod", "fsync"):
            monkeypatch.setattr(os, name, forbidden)
        assert session.inspect(WORLD).sequence == 2


@pytest.mark.parametrize("stage", ["between_methods", "outcome", "metadata"])
def test_changed_base_cannot_be_accepted_as_new_expected_file(tmp_path, stage):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)

    def replace_same_bytes():
        alternate = paths["manifest"].with_name("substitute")
        alternate.write_bytes(paths["manifest"].read_bytes())
        alternate.chmod(0o600)
        alternate.replace(paths["manifest"])

    original_outcome, original_metadata = owner.resolve_outcome, owner.finalize_projection
    if stage == "outcome":

        def outcome(pending):
            replace_same_bytes()
            return original_outcome(pending)

        owner.resolve_outcome = outcome
    elif stage == "metadata":

        def metadata(pending, target):
            replace_same_bytes()
            return original_metadata(pending, target)

        owner.finalize_projection = metadata
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        if stage == "between_methods":
            replace_same_bytes()
        with pytest.raises(PublicationRefused, match="changed"):
            session.complete(pending, proposal.completion_template)
    assert b'"pending"' in paths["manifest"].read_bytes()


def test_exact_retry_still_requires_actual_terminal_outcome(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        session.complete(pending, proposal.completion_template)
    before = paths["journal"].read_bytes()
    owner.outcome = "UNKNOWN"
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.prepare(WORLD, proposal)
    assert paths["journal"].read_bytes() == before


def forget_proposal(fixture, before, after):
    parsed = decode_legacy(after, world_id=WORLD, budget=DecoderBudget())
    new = legacy_row(parsed, parsed.binding.count - 1)
    return proposal_for(
        fixture,
        [
            effect(
                "forget_apply",
                90,
                row={
                    k: getattr(new, k)
                    for k in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
                },
                before_ledger=binding(before),
                after_ledger=binding(after),
                expected_revision=0,
                revision=1,
            )
        ],
        mode="restrictive",
    )


@pytest.mark.parametrize("rewrite", ["whitespace", "reorder", "replace"])
def test_legacy_prior_row_rewrite_refuses_even_with_exact_candidate_digest(tmp_path, rewrite):
    root, bindings, fixture, paths, owner = setup(tmp_path, rows=[row(1), row(2)])
    before = paths["legacy"].read_bytes()
    rows = [row(1), row(2), row(3)]
    if rewrite == "reorder":
        rows[:2] = reversed(rows[:2])
    elif rewrite == "replace":
        rows[0]["requested_by"] = "another-owner"
    after = payload(rows)
    if rewrite == "whitespace":
        after = after.replace(b'{"approved_at":', b'{ "approved_at":', 1)
    proposal = forget_proposal(fixture, before, after)
    original_journal = paths["journal"].read_bytes()
    with open_session(root, bindings, owner) as session:
        session.stage_legacy(WORLD, after)
        with pytest.raises(PublicationRefused, match="conflict"):
            session.prepare(WORLD, proposal)
    assert paths["journal"].read_bytes() == original_journal
    assert paths["legacy"].read_bytes() == before


@pytest.mark.parametrize("ledger_installed", [False, True])
def test_legacy_pending_recovery_preserves_exact_extension(tmp_path, ledger_installed):
    root, bindings, fixture, paths, owner = setup(tmp_path, rows=[row(1)])
    before, after = paths["legacy"].read_bytes(), payload([row(1), row(2)])
    proposal = forget_proposal(fixture, before, after)
    with open_session(root, bindings, owner) as session:
        candidate = session.stage_legacy(WORLD, after)
        pending = session.prepare(WORLD, proposal)
        if ledger_installed:
            session.publish_legacy(pending, candidate)
    with open_session(root, bindings, owner) as session:
        if not ledger_installed:
            session.stage_legacy(WORLD, after)
        result = session.recover(WORLD, proposal, proposal.completion_template)
        assert result.state.erasure_revision == 1
    assert paths["legacy"].read_bytes() == after


def test_retained_budget_reservations_survive_the_context(tmp_path):
    root, bindings, _, _, owner = setup(tmp_path)
    with open_session(root, bindings, owner) as session:
        session._retained = 2000
        session._external_bytes = 3000
        with session._budget() as budget:
            assert dict(budget.usage)["metadata"] >= 133072
            assert dict(budget.usage)["input"] == 3000
        with pytest.raises(AuthorityCodecRefused, match="cancelled"):
            budget.tick()


def test_completion_time_can_change_without_changing_the_choice(tmp_path, monkeypatch):
    from grove.authority.codec import event_digest

    root, bindings, fixture, _, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    event = proposal.completion_template
    monkeypatch.setattr(
        clock, "now", lambda world=None: datetime(2026, 11, 4, 12, 4, 1, tzinfo=UTC)
    )
    completion = replace(event.payload.completion, completed_at="2026-11-04T12:04:01.000000Z")
    event = replace(event, payload=replace(event.payload, completion=completion))
    event = replace(event, digest=event_digest(event))
    fixture.events[-1]["payload"]["completion"]["completed_at"] = completion.completed_at
    resign(fixture.events[-1])
    # A changed time changes the full event head and therefore the final manifest.
    # The publisher must regenerate that manifest from the validated final head.
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        assert session.complete(pending, event).code == "committed"
