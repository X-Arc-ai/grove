"""Fault controls for the real inactive binder and local publication owner.

These tests own all child handles and local files. The synthetic database does
not claim native isolation, power-loss durability, or production activation.
"""

import multiprocessing
import threading
from dataclasses import replace

import pytest

from grove import clock
from grove.authority.lease import LeaseRefused, open_existing_root
from grove.authority.publication import PublicationRefused
from grove.authority.publication_files import PublicationFiles
from grove.authority.publisher import PublicationSession
from grove.writer.authority_records import check_binding
from grove.writer.tx import WorldTx
from tests.privacy.test_authority_codec import uid
from tests.privacy.test_authority_legacy import WORLD
from tests.privacy.test_authority_writer import Native
from tests.privacy.test_authority_writer import (
    fixed_publication_clock as fixed_publication_clock,
)
from tests.privacy.test_authority_writer import (
    harness as harness,
)


def test_retried_effect_callback_never_publishes_a_file(harness, monkeypatch):
    h = harness
    h.db.retry_effect = True
    replacements = []
    original = PublicationFiles.replace

    def replace_file(self, *args):
        assert not h.db.inside
        replacements.append(args[0].role)
        return original(self, *args)

    monkeypatch.setattr(PublicationFiles, "replace", replace_file)
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        result = session.complete(pending, h.proposal.completion_template)
        assert result.code == "committed"
    assert h.db.effect_callbacks == 2
    assert h.db.data["effects"] == 1
    assert len(h.db.data["operations"]) == 1
    assert h.db.data["projection"]["fence"] == 1
    assert replacements.count("legacy") == 1
    assert replacements.count("journal") == 2


def test_response_loss_stays_unknown_even_if_effect_committed(harness):
    h = harness
    h.db.lose_effect_response = True
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        before = h.snapshot()
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.complete(pending, h.proposal.completion_template)
        assert h.snapshot() == before
        assert h.db.data["effects"] == 1
        assert next(iter(h.db.data["operations"].values()))["outcome"] == "COMMITTED_EXACT"
        assert not h.final_targets
        assert b'"pending"' in h.paths["manifest"].read_bytes()


@pytest.mark.parametrize("cut", ["effect_ack", "completion_ack"])
def test_fresh_recovery_resolves_exact_effect_and_completion_ack_loss(harness, monkeypatch, cut):
    h = harness
    original_replace = PublicationFiles.replace
    journals = 0

    def replace_then_lose_ack(self, candidate, *args):
        nonlocal journals
        result = original_replace(self, candidate, *args)
        if candidate.role == "journal":
            journals += 1
            if journals == 2:
                raise OSError("synthetic completion acknowledgement loss")
        return result

    if cut == "effect_ack":
        h.db.lose_effect_response = True
    else:
        monkeypatch.setattr(PublicationFiles, "replace", replace_then_lose_ack)
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        with pytest.raises(PublicationRefused):
            session.complete(pending, h.proposal.completion_template)
        assert h.db.data["effects"] == 1
        assert b'"pending"' in h.paths["manifest"].read_bytes()
    monkeypatch.setattr(PublicationFiles, "replace", original_replace)
    h.writer = h.new_writer(operation_kind="recovery")
    with h.open() as session:
        session.bind_writer(h.writer)
        result = session.recover(WORLD, h.proposal, h.proposal.completion_template)
        assert result.code == "committed"
        assert result.state.projection == h.writer._native.projection
    assert h.db.data["effects"] == 1
    assert h.db.data["projection"]["active_intent_id"] is None


@pytest.mark.parametrize("service", ["principal", "references"])
def test_changed_actual_principal_or_lineage_blocks_effect(harness, service):
    h = harness
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        before = h.snapshot()
        getattr(h, service).current = False
        with pytest.raises(PublicationRefused):
            session.complete(pending, h.proposal.completion_template)
        assert h.snapshot() == before
        assert h.db.data["effects"] == 0
        assert not h.db.data["operations"]
        assert not h.final_targets


def test_failed_preflight_cannot_stage_legacy_bytes(harness):
    h = harness
    h.principal.current = False
    before = h.snapshot()
    with h.open() as session:
        with pytest.raises(PublicationRefused):
            session.bind_writer(h.writer)
        with pytest.raises(PublicationRefused):
            session.stage_legacy(WORLD, h.after)
    assert h.snapshot() == before
    assert not h.db.calls
    assert not list(h.root.rglob(".publication-*"))


def test_wrong_session_operation_and_world_refuse_bound_owner(harness):
    h = harness
    with h.open() as session:
        session.bind_writer(h.writer)
        runtime = h.writer._binding
        # The same owner and runtime are accepted on their original session.
        session.assert_writer_owner(h.writer, WORLD, runtime.operation_id)
        other = PublicationSession(h.writer, h.bindings, clock.monotonic() + 30)
        with pytest.raises(PublicationRefused, match="binding"):
            other.assert_writer_owner(h.writer, WORLD, runtime.operation_id)
        with pytest.raises(PublicationRefused, match="binding"):
            session.assert_writer_owner(h.writer, WORLD, uid(9910))
        with pytest.raises(PublicationRefused, match="binding"):
            session.assert_writer_owner(h.writer, "other-world", runtime.operation_id)
        with pytest.raises(PublicationRefused, match="binding"):
            h.writer._lifetime.assert_current(h.bindings, WORLD, "recovery")
        with pytest.raises(PublicationRefused, match="binding"):
            check_binding(runtime, WorldTx(Native(h.db), "other-world"))


def test_bound_owner_refuses_a_different_thread(harness):
    h = harness
    results = []
    with h.open() as session:
        session.bind_writer(h.writer)
        before = h.snapshot()
        h.writer._assert_live()

        def foreign_thread():
            try:
                h.writer._assert_live()
            except PublicationRefused as error:
                results.append(error.code)
            else:
                results.append("incorrectly accepted")

        thread = threading.Thread(target=foreign_thread)
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
        assert results == ["binding"]
        assert h.snapshot() == before


def _foreign_process(writer, sender):
    try:
        writer._assert_live()
    except PublicationRefused as error:
        sender.send(error.code)
    else:
        sender.send("incorrectly accepted")
    finally:
        sender.close()


def test_bound_owner_refuses_inherited_process_without_releasing_parent_ex(harness):
    h = harness
    context = multiprocessing.get_context("fork")
    with h.open() as session:
        session.bind_writer(h.writer)
        before = h.snapshot()
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=_foreign_process, args=(h.writer, sender))
        child.start()
        sender.close()
        try:
            assert receiver.poll(3)
            assert receiver.recv() == "binding"
            child.join(3)
            assert not child.is_alive() and child.exitcode == 0
        finally:
            receiver.close()
            if child.is_alive():
                child.terminate()
                child.join(3)
        assert h.snapshot() == before
        assert session._lease._state == "held"
        h.writer._assert_live()


def test_owned_thread_drains_before_ex_release(harness):
    h = harness
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def owned_work():
        entered.set()
        assert release.wait(3)
        ended.set()

    thread = threading.Thread(target=owned_work)
    thread.start()
    assert entered.wait(3)
    session = h.open()
    try:
        session.bind_writer(h.writer)

        def drain_owned_work():
            assert session._lease._state == "held"
            contender = open_existing_root(str(h.root), h.bindings.root_identity)
            try:
                with pytest.raises(LeaseRefused, match="deadline"):
                    contender.acquire_exclusive(clock.monotonic() + 0.03)
            finally:
                contender.close()
            release.set()
            thread.join(3)
            assert not thread.is_alive() and ended.is_set()

        h.workers.drain_hook = drain_owned_work
        session.close()
        assert h.events[-2:] == ["drain", "exit"]
        contender = open_existing_root(str(h.root), h.bindings.root_identity)
        contender.acquire_exclusive(clock.monotonic() + 1)
        contender.close()
        with pytest.raises(PublicationRefused):
            h.writer._assert_live()
    finally:
        release.set()
        thread.join(3)
        h.workers.drain_hook = None
        session.close()


def test_failed_drain_retains_ex_until_actual_owner_drains(harness):
    h = harness
    session = h.open()
    session.bind_writer(h.writer)
    h.workers.drain_result = "still active"
    try:
        with pytest.raises(PublicationRefused, match="outcome_unknown"):
            session.close()
        assert session._lease._state == "held"
        assert not h.writer._lifetime.closed
        contender = open_existing_root(str(h.root), h.bindings.root_identity)
        try:
            with pytest.raises(LeaseRefused, match="deadline"):
                contender.acquire_exclusive(clock.monotonic() + 0.03)
        finally:
            contender.close()
    finally:
        h.workers.drain_result = None
        session.close()
    assert h.writer._lifetime.closed


def test_wrong_final_projection_never_replaces_pending_manifest(harness, monkeypatch):
    h = harness
    actual_finalize = h.writer.finalize_projection

    def wrong_target(pending, target):
        return actual_finalize(pending, replace(target, authority_epoch=target.authority_epoch + 1))

    monkeypatch.setattr(h.writer, "finalize_projection", wrong_target)
    with h.open() as session:
        session.bind_writer(h.writer)
        pending = h.pending(session)
        manifest_before = h.paths["manifest"].read_bytes()
        with pytest.raises(PublicationRefused, match="conflict"):
            session.complete(pending, h.proposal.completion_template)
        assert h.paths["manifest"].read_bytes() == manifest_before
        assert h.db.data["effects"] == 1
        assert not h.final_targets
        assert h.db.data["projection"]["active_intent_id"] == pending.intent_id
