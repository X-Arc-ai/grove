"""Bounded local faults and actual owned process/thread controls; no power-loss claim."""

import copy
import multiprocessing
import os
import threading
import time
from dataclasses import replace

import pytest

from grove import clock
from grove.authority.lease import LeaseRefused, open_existing_root
from grove.authority.publication import PublicationRefused
from grove.authority.publication_files import PublicationFiles
from tests.privacy.test_authority_codec import canonical, effect, hashed, uid
from tests.privacy.test_authority_legacy import WORLD, payload
from tests.privacy.test_authority_publication import (
    FileJournal,
    SyntheticOwner,
    install,
    open_session,
    proposal_for,
    record_event,
    record_manifest,
    setup,
)
from tests.privacy.test_authority_publication import (
    fixed_publication_clock as fixed_publication_clock,
)


@pytest.mark.parametrize("step", [1, 2, 3, 4])
@pytest.mark.parametrize("after", [False, True])
def test_crash_boundary_preserves_refusal_and_recovers(tmp_path, monkeypatch, step, after):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    initial = {k: p.read_bytes() for k, p in paths.items()}
    original = PublicationFiles.replace
    calls = 0

    def fail(self, *args):
        nonlocal calls
        calls += 1
        if calls == step and not after:
            raise PublicationRefused("io", "stage")
        result = original(self, *args)
        if calls == step and after:
            raise PublicationRefused("recovery_required", "stage")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(PublicationFiles, "replace", fail)
        with open_session(root, bindings, owner) as session:
            with pytest.raises(PublicationRefused):
                pending = session.prepare(WORLD, proposal)
                session.complete(pending, proposal.completion_template)
    assert paths["legacy"].read_bytes() == initial["legacy"]
    if step == 1 and not after:
        assert {k: p.read_bytes() for k, p in paths.items()} == initial
        with open_session(root, bindings, owner) as session:
            pending = session.prepare(WORLD, proposal)
            result = session.complete(pending, proposal.completion_template)
    else:
        with open_session(root, bindings, owner) as session:
            result = session.recover(WORLD, proposal, proposal.completion_template)
    assert result.code == "committed"
    assert paths["journal"].read_bytes() == fixture.raw()
    assert paths["manifest"].read_bytes() == canonical(fixture.manifest())


def test_metadata_failure_leaves_completion_unavailable(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    owner.failed_metadata = True
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        with pytest.raises(PublicationRefused):
            session.complete(pending, proposal.completion_template)
    assert paths["journal"].read_bytes() == fixture.raw()
    assert b'"pending"' in paths["manifest"].read_bytes()
    owner.failed_metadata = False
    with open_session(root, bindings, owner) as session:
        assert session.recover(WORLD, proposal, proposal.completion_template).code == "committed"


def test_unknown_outcome_never_means_rollback(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        session.prepare(WORLD, proposal)
    before = paths["journal"].read_bytes()
    owner.outcome = "UNKNOWN"
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.recover(WORLD, proposal, proposal.completion_template)
    assert paths["journal"].read_bytes() == before
    assert b'"pending"' in paths["manifest"].read_bytes()


@pytest.mark.parametrize("tail", [b"", b"x", b"]}", b"null}"])
def test_partial_final_tail_never_yields_valid_prefix(tmp_path, tail):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    bad = paths["journal"].read_bytes()[:-5] + tail
    paths["journal"].write_bytes(bad)
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused):
            session.recover(WORLD, proposal, proposal.completion_template)
    assert paths["journal"].read_bytes() == bad


def test_cancelled_worker_retains_lease_until_terminal(tmp_path, monkeypatch):
    root, bindings, fixture, _, owner = setup(tmp_path)
    proposal = proposal_for(fixture)
    ready, resume, ended = threading.Event(), threading.Event(), threading.Event()
    holder, failures = [], []
    real_sync = os.fsync
    first = True

    def sync(fd):
        nonlocal first
        if first:
            first = False
            ready.set()
            assert resume.wait(3)
        return real_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)

    def worker():
        try:
            with open_session(root, bindings, owner) as session:
                holder.append(session)
                session.prepare(WORLD, proposal)
        except PublicationRefused as error:
            failures.append(error.code)
        finally:
            ended.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(3)
        holder[0].cancel()
        with pytest.raises(PublicationRefused, match="binding"):
            holder[0].close()
        contender = open_existing_root(str(root), bindings.root_identity)
        with pytest.raises(LeaseRefused, match="deadline"):
            contender.acquire_exclusive(clock.monotonic() + 0.04)
        assert not ended.is_set()
    finally:
        resume.set()
        thread.join(3)
    assert not thread.is_alive()
    assert failures == ["cancelled"]
    contender = open_existing_root(str(root), bindings.root_identity)
    contender.acquire_exclusive(clock.monotonic() + 1)
    contender.close()


def _process_holder(root, bindings, fixture, ready):
    owner = SyntheticOwner(bindings, fixture)
    with open_session(root, bindings, owner) as session:
        session.inspect(fixture.world)
        ready.set()
        time.sleep(5)


def test_actual_process_loss_releases_only_its_owned_lock(tmp_path):
    root, bindings, fixture, _, owner = setup(tmp_path)
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    child = ctx.Process(target=_process_holder, args=(root, bindings, fixture, ready))
    child.start()
    try:
        assert ready.wait(3)
        contender = open_existing_root(str(root), bindings.root_identity)
        with pytest.raises(LeaseRefused, match="deadline"):
            contender.acquire_exclusive(clock.monotonic() + 0.04)
        child.terminate()  # Only this exact owned child handle.
        child.join(3)
        assert not child.is_alive() and child.exitcode != 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)
    # Process exit establishes lock release only, never a database outcome.
    owner.outcome = "UNKNOWN"
    proposal = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.complete(pending, proposal.completion_template)


def test_session_thread_reentry_refuses(tmp_path):
    root, bindings, _, _, owner = setup(tmp_path)
    failures = []
    with open_session(root, bindings, owner) as session:

        def call():
            try:
                session.inspect(WORLD)
            except PublicationRefused as e:
                failures.append(e.code)

        child = threading.Thread(target=call)
        child.start()
        child.join(2)
        assert not child.is_alive() and failures == ["binding"]
        assert session.inspect(WORLD).sequence == 2


def test_fixed_seal_and_repeat_are_sticky(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    fixture.seal()
    event, manifest = record_event(fixture.events[-1]), record_manifest(fixture.manifest())
    with open_session(root, bindings, owner) as session:
        result = session.seal(WORLD, event, manifest)
        assert result.code == "sealed" and not result.state.projection.ready
    before = paths["journal"].read_bytes()
    with open_session(root, bindings, owner) as session:
        assert session.seal(WORLD, event, manifest).code == "sealed"
    assert paths["journal"].read_bytes() == before


def test_cancel_burns_approval_without_applying_expansion(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    original = proposal_for(fixture)
    with open_session(root, bindings, owner) as session:
        session.prepare(WORLD, original)
    # Rebuild the same pending intent, then its supported cancellation event.
    pending_fixture = copy.deepcopy(fixture)
    pending_fixture.events.pop()
    pending_fixture.state["applied"].pop()
    pending_fixture.state["burned_approvals"].pop()
    pending_fixture.pending = pending_fixture.events[-1]["payload"]["intent"]
    pending_fixture.commit(resolution="cancel_unapplied_expansion")
    owner.fixture = pending_fixture
    owner.outcome = "CANCELLED_EXACT"
    proposal = replace(
        original,
        completion_template=record_event(pending_fixture.events[-1]),
        completed_manifest=record_manifest(pending_fixture.manifest()),
    )
    with open_session(root, bindings, owner) as session:
        result = session.recover(WORLD, proposal, proposal.completion_template)
        assert result.code == "cancelled"
    assert len(pending_fixture.state["applied"]) == 1
    assert len(pending_fixture.state["burned_approvals"]) == 1
    assert paths["journal"].read_bytes() == pending_fixture.raw()


def test_closed_world_is_permanent(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = proposal_for(
        fixture, [effect("world_close", 90, closure_id=uid(123), owner_id="owner-1")], mode="close"
    )
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        assert session.complete(pending, proposal.completion_template).state.state == "closed"
    before = paths["journal"].read_bytes()
    with open_session(root, bindings, owner) as session:
        assert session.prepare(WORLD, proposal).code == "committed"
    fixture.seal()
    with open_session(root, bindings, owner) as session:
        with pytest.raises(PublicationRefused):
            session.seal(
                WORLD, record_event(fixture.events[-1]), record_manifest(fixture.manifest())
            )
    assert paths["journal"].read_bytes() == before


@pytest.mark.parametrize("after", [False, True])
def test_seal_recovery_after_manifest_failure(tmp_path, monkeypatch, after):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    fixture.seal()
    event, manifest = record_event(fixture.events[-1]), record_manifest(fixture.manifest())
    original = PublicationFiles.replace

    def fail(self, candidate, *args):
        if candidate.role == "manifest" and not after:
            raise PublicationRefused("io")
        result = original(self, candidate, *args)
        if candidate.role == "manifest" and after:
            raise PublicationRefused("recovery_required")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(PublicationFiles, "replace", fail)
        with open_session(root, bindings, owner) as session:
            with pytest.raises(PublicationRefused):
                session.seal(WORLD, event, manifest)
    with open_session(root, bindings, owner) as session:
        assert session.seal(WORLD, event, manifest).code == "sealed"
    assert paths["journal"].read_bytes() == fixture.raw()


def test_unknown_lifetime_exit_keeps_exclusive_lease(tmp_path):
    root, bindings, fixture, _, owner = setup(tmp_path)

    class Lifetime:
        def __enter__(self):
            owner.active = True
            return owner

        def __exit__(self, *args):
            if not self.terminal:
                raise RuntimeError("owned worker is not terminal")
            owner.active = False

    lifetime = Lifetime()
    lifetime.terminal = False
    owner.operation = lambda *a: lifetime
    session = open_session(root, bindings, owner)
    session.inspect(WORLD)
    try:
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.close()
        contender = open_existing_root(str(root), bindings.root_identity)
        with pytest.raises(LeaseRefused, match="deadline"):
            contender.acquire_exclusive(clock.monotonic() + 0.04)
    finally:
        lifetime.terminal = True
        session.close()
    contender = open_existing_root(str(root), bindings.root_identity)
    contender.acquire_exclusive(clock.monotonic() + 1)
    contender.close()


def test_restrictive_pending_seal_can_complete(tmp_path):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    fixture.prepare(
        [effect("world_close", 90, closure_id=uid(123), owner_id="owner-1")], mode="close"
    )
    initial = record_event(fixture.events[-1])
    fixture.seal()
    seal = record_event(fixture.events[-1])
    pending_manifest = record_manifest(fixture.manifest())
    fixture.commit()
    from grove.authority.publication import ProposedPublication

    proposal = ProposedPublication(
        initial,
        record_event(fixture.events[-1]),
        pending_manifest,
        record_manifest(fixture.manifest()),
        seal,
    )
    with open_session(root, bindings, owner) as session:
        token = session.prepare(WORLD, proposal)
        assert token.state.state == "sealed"
        result = session.complete(token, proposal.completion_template)
        assert result.state.state == "closed"
    assert paths["journal"].read_bytes() == fixture.raw()


def restore_proposal(fixture, scope):
    from grove.authority.publication import ProposedPublication
    from grove.authority.records import CREDENTIAL_KINDS
    from tests.privacy.test_authority_codec import PROFILE_RAW, H, controller

    common = {
        "operation_id": uid(92),
        "source_kind": "logical_snapshot",
        "artifact_digest": H,
        "inventory_digest": H,
        "d3_fingerprint": H,
        "old_epoch": 1,
        "new_epoch": 2,
        "scope": scope,
        "installation_before": 1,
        "installation_after": 2 if scope == "installation" else 1,
    }
    extra = {
        **common,
        "controller_receipt_id": "receipt-1",
        "live_authority_head": hashed(fixture.events[-1]),
        "ledger": fixture.state["ledger"],
    }
    fixture.prepare(
        [
            effect(
                "restore_begin",
                93,
                **common,
                profile=PROFILE_RAW,
                live_ledger=fixture.state["ledger"],
            ),
            effect("epoch_invalidate", 94, old_epoch=1, new_epoch=2, kinds=list(CREDENTIAL_KINDS)),
        ],
        auth=controller("restore", 92),
        mode="restore",
        kind="restore_epoch",
        extra=extra,
    )
    initial, pending = record_event(fixture.events[-1]), record_manifest(fixture.manifest())
    fixture.commit()
    if scope == "installation":
        original_manifest = fixture.manifest

        def restoring_manifest():
            value = original_manifest()
            value["state"] = "restoring"
            value["checksum"] = hashed({k: v for k, v in value.items() if k != "checksum"})
            return value

        fixture.manifest = restoring_manifest
    return ProposedPublication(
        initial, record_event(fixture.events[-1]), pending, record_manifest(fixture.manifest())
    )


@pytest.mark.parametrize("scope", ["world", "installation"])
def test_restore_epochs_remain_distinct_and_global_barrier_stays_closed(tmp_path, scope):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = restore_proposal(fixture, scope)
    with open_session(root, bindings, owner) as session:
        pending = session.prepare(WORLD, proposal)
        assert pending.state.state == "restoring"
        assert b'"restoring"' in paths["manifest"].read_bytes()
        result = session.complete(pending, proposal.completion_template)
    assert result.state.authority_epoch == 2
    assert result.state.installation_epoch == (2 if scope == "installation" else 1)
    if scope == "installation":
        assert result.state.state == "restoring" and not result.state.projection.ready


@pytest.mark.parametrize("step", [1, 2, 3, 4, 5])
def test_installation_barrier_crash_resumes_same_epoch(tmp_path, monkeypatch, step):
    root, bindings, fixture, paths, owner = setup(tmp_path)
    proposal = restore_proposal(fixture, "installation")
    original, calls = PublicationFiles.replace, 0

    def stop(self, *args):
        nonlocal calls
        calls += 1
        result = original(self, *args)
        if calls == step:
            raise PublicationRefused("recovery_required")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(PublicationFiles, "replace", stop)
        with open_session(root, bindings, owner) as session:
            with pytest.raises(PublicationRefused):
                pending = session.prepare(WORLD, proposal)
                session.complete(pending, proposal.completion_template)
    assert b'"state":"restoring"' in paths["manifest"].read_bytes()
    with open_session(root, bindings, owner) as session:
        result = session.recover(WORLD, proposal, proposal.completion_template)
    assert result.state.installation_epoch == 2
    assert result.state.state == "restoring"


def _publish_world(root, bindings, fixture, pipe):
    try:
        import json

        from grove.authority.codec import manifest_checksum

        owner = SyntheticOwner(bindings, fixture)
        proposal = proposal_for(fixture)
        with open_session(root, bindings, owner) as session:
            current = record_manifest(json.loads((root / "authority-manifest.json").read_bytes()))
            others = tuple(h for h in current.worlds if h.world_id != fixture.world)

            def merged(manifest):
                manifest = replace(
                    manifest,
                    worlds=tuple(sorted((*others, *manifest.worlds), key=lambda h: h.world_id)),
                )
                return replace(manifest, checksum=manifest_checksum(manifest))

            proposal = replace(
                proposal,
                pending_manifest=merged(proposal.pending_manifest),
                completed_manifest=merged(proposal.completed_manifest),
            )
            pending = session.prepare(fixture.world, proposal)
            session.complete(pending, proposal.completion_template)
        pipe.send("complete")
    except Exception as error:
        pipe.send(type(error).__name__ + ":" + str(error))
        raise
    finally:
        pipe.close()


def test_two_process_publishers_preserve_both_world_heads(tmp_path):
    import json

    root, bindings, first, paths, _ = setup(tmp_path)
    world = WORLD[:-1] + "2"
    envelope = json.loads(payload([]))
    envelope["world_id"] = world
    raw = canonical(envelope)
    second = FileJournal(raw, world)
    install(root, second, raw)
    manifest = first.manifest()
    manifest["worlds"].extend(second.manifest()["worlds"])
    manifest["checksum"] = hashed({k: v for k, v in manifest.items() if k != "checksum"})
    paths["manifest"].write_bytes(canonical(manifest))
    ctx = multiprocessing.get_context("fork")
    children, readers = [], []
    try:
        for fixture in (first, second):
            reader, writer = ctx.Pipe(duplex=False)
            child = ctx.Process(target=_publish_world, args=(root, bindings, fixture, writer))
            child.start()
            writer.close()
            children.append(child)
            readers.append(reader)
        for reader, child in zip(readers, children, strict=True):
            assert reader.poll(10)
            assert reader.recv() == "complete"
            child.join(3)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(3)
        for reader in readers:
            reader.close()
    final = json.loads(paths["manifest"].read_bytes())
    assert [head["sequence"] for head in final["worlds"]] == [4, 4]
    assert [head["world_id"] for head in final["worlds"]] == [WORLD, world]
