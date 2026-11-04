"""Opt-in native and subprocess regressions, authored but NOT RUN by the source grant.

Only explicit future test authorization may set GROVE_LEARNER_NATIVE_TESTS=1.
This switch selects tests, never a production authority provider. All owner and
transport responses remain synthetic even when Neo4j or a child process is real.
"""

import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from grove import clock
from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.contracts import ExtractionRefused
from grove.extraction.durable_types import IntakeCommand, IntakeOrder, RunWitness
from grove.extraction.execution_types import Disposition, ExecutionEvent, Proposal
from grove.ids import new_id
from grove.store.connection import open_driver
from grove.writer import learner_execution as execution
from grove.writer import learner_requests as boundary
from grove.writer.testing import plant_node
from grove.writer.tx import WorldTx, WorldWriter
from grove.writer.worlds import create_world
from tests.writer import test_learner_execution as synthetic
from tests.writer.helpers import raw, world_dump

NATIVE = os.environ.get("GROVE_LEARNER_NATIVE_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not NATIVE, reason="NOT RUN: separate native/process-cut authorization required"
)
CUTS = (
    "after_claim",
    "after_reservation",
    "after_send",
    "after_result",
    "during_completion",
    "commit_reply_lost",
    "before_receipt",
    "consumer_before_ack",
    "old_callback",
    "revoke_before_release",
)


def _allow(tx, session, purpose, binding, run):
    """Explicit synthetic owner, used only by this opt-in test module."""
    return binding


def _seeded_binding(world):
    data = synthetic.document(" ")
    data["world_id"] = world
    data["schema_digest"] = codec.writer_schema_digest()
    data["run"]["deadline"] = int(synthetic.AT.timestamp()) + 600
    data["work"]["lease_deadline"] = int(synthetic.AT.timestamp()) + 540
    replacements = {
        data[section][field]: new_id(world)
        for section, field in (
            ("source", "id"),
            ("episode", "id"),
            ("producer", "id"),
            ("run", "id"),
            ("work", "id"),
            ("work", "request_id"),
            ("source", "screening_id"),
        )
    }

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rewrite(child) for child in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    return legacy.decode_binding(codec.canonical(rewrite(data)))


class NativeScenario:
    def __init__(self, driver, world, binding):
        self.driver, self.world, self.binding = driver, world, binding
        self.writer = WorldWriter(driver, world)

    def current(self):
        return self.writer.transaction(lambda tx: synthetic.current(tx, self.binding.work.id))

    def claim(self, event_id=None):
        head, run, _ = self.current()
        return self.writer.transaction(
            lambda tx: execution.claim_execution_in_tx(
                tx, None, head, run, event_id or new_id(self.world)
            )
        )

    def ready(self):
        b = self.binding
        self.claim()
        proposal = Proposal(
            legacy.binding_digest(b),
            (),
            (),
            (Disposition("msg1", "key:msg1", "ignored_blank", None, ()),),
            (),
        )
        payload = codec.decode_payload(
            codec.canonical(
                {
                    "schema": "grove.learner.payload.v1",
                    "writer_revision": b.source_revision,
                    "binding_digest": legacy.binding_digest(b),
                    "batch": {"ops": [], "evidence": []},
                    "operation_map": [],
                }
            )
        )
        head, run, _ = self.current()
        self.writer.transaction(
            lambda tx: execution.record_proposal_in_tx(
                tx, None, head, run, new_id(self.world), proposal, payload
            )
        )
        return proposal, payload

    def complete(self, proposal, payload, event_id=None):
        head, run, _ = self.current()
        return self.writer.transaction(
            lambda tx: execution.complete_execution_in_tx(
                tx, None, head, run, event_id or new_id(self.world), proposal, payload
            )
        )

    def generation(self):
        return raw(
            self.driver,
            "MATCH (w:World {id:$id}) RETURN w.read_generation AS generation",
            id=self.world,
        )[0]["generation"]


@pytest.fixture
def native_scenario(driver, make_world, monkeypatch):
    world = make_world()
    test_clock = clock.TestClock()
    test_clock.set(synthetic.AT)
    with clock.using(test_clock):
        create_world(driver, world, owner_user_id="synthetic-learner-owner")
        binding = _seeded_binding(world)
        memory = synthetic.MemoryTx(world)
        synthetic.seed(memory, binding)
        manifest = new_id(world)
        memory.tables["HistoryValue"][manifest] = {
            **memory.tables["HistoryValue"].pop("manifest-1"),
            "id": manifest,
        }
        for label, rows in memory.tables.items():
            for row in rows.values():
                plant_node(
                    driver, world, label, **{k: v for k, v in row.items() if k != "world_id"}
                )
        monkeypatch.setattr(boundary, "_require_authority_in_tx", _allow)
        run = RunWitness(binding.run.id, 7, 1, binding.run.pass_id, binding.run.input_ids)
        command = IntakeCommand(
            new_id(world),
            binding.work.id,
            binding.work.request_id,
            new_id(world),
            binding,
            run,
            IntakeOrder(binding.source.id, 0, None, None, manifest),
        )
        scenario = NativeScenario(driver, world, binding)
        scenario.writer.transaction(lambda tx: execution.enqueue_episode_in_tx(tx, None, command))
        yield scenario


def test_world_generation_advances_once_and_replay_is_a_noop(native_scenario):
    s = native_scenario
    proposal, payload = s.ready()
    before = s.generation()
    result = s.complete(proposal, payload)
    assert s.generation() == before + 1
    _, run, _ = s.current()
    dump = world_dump(s.driver, s.world)
    assert (
        s.writer.transaction(
            lambda tx: execution.read_completion_in_tx(
                tx, None, run, s.binding.work.id, result.completion_id
            )
        )
        == result
    )
    assert world_dump(s.driver, s.world) == dump


def test_exception_after_each_write_rolls_back_all_homes(native_scenario, monkeypatch):
    s = native_scenario
    proposal, payload = s.ready()
    head, run, _ = s.current()
    before = world_dump(s.driver, s.world)
    original = WorldTx.run
    # Count the actual final transaction without committing it.
    count = [0]

    def count_write(tx, query, **params):
        result = original(tx, query, **params)
        count[0] += int(query.startswith("CREATE") or " SET " in query)
        return result

    def probe(tx):
        execution.complete_execution_in_tx(tx, None, head, run, new_id(s.world), proposal, payload)
        raise RuntimeError("probe rollback")

    monkeypatch.setattr(WorldTx, "run", count_write)
    with pytest.raises(RuntimeError, match="probe rollback"):
        s.writer.transaction(probe)
    assert count[0] > 0
    for cut in range(1, count[0] + 1):
        seen = [0]

        def cut_write(tx, query, seen=seen, cut=cut, **params):
            result = original(tx, query, **params)
            seen[0] += int(query.startswith("CREATE") or " SET " in query)
            if seen[0] == cut:
                raise RuntimeError("native cut")
            return result

        monkeypatch.setattr(WorldTx, "run", cut_write)
        with pytest.raises(RuntimeError, match="native cut"):
            s.writer.transaction(
                lambda tx: execution.complete_execution_in_tx(
                    tx, None, head, run, new_id(s.world), proposal, payload
                )
            )
        assert world_dump(s.driver, s.world) == before


def test_two_claimants_have_one_current_epoch(native_scenario):
    s = native_scenario
    head, run, _ = s.current()
    start = Barrier(2)

    def claimant():
        test_clock = clock.TestClock()
        test_clock.set(synthetic.AT)
        with clock.using(test_clock):
            start.wait(timeout=5)
            try:
                s.writer.transaction(
                    lambda tx: execution.claim_execution_in_tx(tx, None, head, run, new_id(s.world))
                )
                return "accepted"
            except ExtractionRefused:
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claimant(), range(2)))
    assert sorted(results) == ["accepted", "refused"]
    assert s.current()[2].claim_epoch == 1


@pytest.mark.parametrize("field,value", [("version", 8), ("agent", "curator")])
def test_actual_run_version_and_owner_reject_old_witness(native_scenario, field, value):
    s = native_scenario
    head, run, _ = s.current()

    def mutate(tx):
        old = boundary._one(tx, "RunRecord", run.actual_run_id)
        boundary._put(tx, "RunRecord", {**old, field: value}, old)

    s.writer.transaction(mutate)
    before = world_dump(s.driver, s.world)
    with pytest.raises(ExtractionRefused):
        s.writer.transaction(
            lambda tx: execution.claim_execution_in_tx(tx, None, head, run, new_id(s.world))
        )
    assert world_dump(s.driver, s.world) == before


def _die(cut):
    # Self-signal only: this process is the exact child created by the test.
    print(json.dumps({"cut": cut, "pid": os.getpid()}), flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def _child(cut, world, encoded_binding):
    if not NATIVE or cut not in CUTS:
        return 2
    test_clock = clock.TestClock()
    test_clock.set(synthetic.AT)
    clock.install(test_clock)
    boundary._require_authority_in_tx = _allow
    binding = legacy.decode_binding(encoded_binding.encode())
    with open_driver() as driver:
        s = NativeScenario(driver, world, binding)
        if cut == "after_claim":
            s.claim()
            _die(cut)
        if cut in {"after_reservation", "after_send", "after_result"}:
            from grove.extraction.durable_types import (
                RecordedAttemptWitness,
                StoredCallRequest,
                StoredCallResult,
            )
            from tests.extraction.test_dispositions import invocation

            s.claim()
            head, run, state = s.current()
            call = invocation(binding, epoch=state.claim_epoch)
            packet = {
                "source_data": [codec.plain(m) for m in codec._source_batch(binding).messages]
            }
            normalized = codec.canonical(
                {
                    "binding_digest": legacy.binding_digest(binding),
                    "stage": "triage",
                    "payload": packet,
                }
            ).decode()
            call = replace(
                call,
                id=new_id(world),
                request=normalized,
                request_digest=legacy.text_digest(normalized),
            )
            request = StoredCallRequest(
                call.request,
                codec.canonical(
                    {"messages": [{"role": "user", "content": codec.canonical(packet).decode()}]}
                ).decode(),
            )
            s.writer.transaction(
                lambda tx: execution.record_invocation_in_tx(
                    tx, None, head, run, new_id(world), call, request
                )
            )
            if cut == "after_reservation":
                _die(cut)
            head, run, _ = s.current()
            s.writer.transaction(
                lambda tx: execution.record_invocation_in_tx(
                    tx, None, head, run, new_id(world), replace(call, state="in_flight"), request
                )
            )
            if cut == "after_send":
                _die(cut)
            # Synthetic provider bytes and synthetic metering owner, explicitly test-only.
            selected = codec.canonical(
                {
                    "schema": "grove.triage.v1",
                    "has_candidates": False,
                    "reviewed_locators": ["key:msg1"],
                    "candidates": [],
                    "redactions": [],
                }
            ).decode()
            normalized = codec.canonical(
                {"reviewed_locators": ["key:msg1"], "candidates": []}
            ).decode()
            result = StoredCallResult(
                codec.canonical(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": selected}],
                            }
                        ],
                    }
                ).decode(),
                selected,
                normalized,
                binding.source_revision,
            )
            digests = [
                legacy.data_digest("grove.learner." + name.replace("_", "-") + ".v1", text)
                for name, text in zip(
                    codec.DIGEST_NAMES,
                    (
                        request.provider_request_body,
                        result.provider_response_text,
                        result.selected_output_text,
                        request.normalized_request,
                        result.normalized_output,
                    ),
                    strict=True,
                )
            ]
            attempt = RecordedAttemptWitness(
                call.id,
                new_id(world),
                call.run_id,
                call.claim_epoch,
                call.stage,
                call.route,
                call.model,
                call.job,
                call.effort,
                "ok",
                *digests,
            )
            boundary._require_attempt_in_tx = lambda *args: "synthetic-owner-receipt"
            head, run, _ = s.current()
            s.writer.transaction(
                lambda tx: execution.record_result_in_tx(
                    tx, None, head, run, new_id(world), attempt, result
                )
            )
            _die(cut)
        proposal, payload = s.ready()
        if cut == "during_completion":
            from grove.writer import apply

            apply._before_commit = lambda: _die(cut)
        if cut == "revoke_before_release":

            def revoked(tx, session, purpose, value, run):
                if purpose == "release":
                    _die(cut)
                return value

            boundary._require_authority_in_tx = revoked
        result = s.complete(proposal, payload)
        if cut in {"commit_reply_lost", "before_receipt"}:
            _die(cut)
        if cut == "old_callback":
            # Capture the old completion owner identity before the child exits.
            assert result.work_id == binding.work.id
            _die(cut)
        if cut == "consumer_before_ack":
            from grove.writer.learner_progress import enqueue_handoffs_in_tx

            def consumer(tx):
                work = boundary._one(tx, "LearnerWork", binding.work.id)
                completion = boundary._one(tx, "LearnerCompletion", result.completion_id)
                stamp = boundary._stamp(tx, new_id(world), binding.run.id, binding.work.request_id)
                ids = enqueue_handoffs_in_tx(tx, stamp, work, completion, [binding.source.id])
                # A synthetic durable consumer receipt uses a test-only private value.
                plant = {
                    "id": new_id(world),
                    "version": 1,
                    "value": codec.canonical({"handoff_id": ids[0]}).decode(),
                }
                boundary._put(tx, "HistoryValue", plant)
                return ids

            s.writer.transaction(consumer)
            _die(cut)
    return 3


@pytest.mark.parametrize("cut", CUTS)
def test_process_cut_at_each_protocol_boundary(native_scenario, tmp_path, cut, monkeypatch):
    s = native_scenario
    log = tmp_path / f"{cut}.log"
    # The retained log is deliberately not removed, including on timeout/failure.
    with log.open("wb") as output:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                __name__,
                cut,
                s.world,
                legacy.encode_binding(s.binding).decode(),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "GROVE_LEARNER_NATIVE_TESTS": "1"},
        )
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            pytest.fail(f"owned child timed out; retained log: {log}")
    assert child.returncode == -signal.SIGKILL, log.read_text()
    reports = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
    assert reports[-1] == {"cut": cut, "pid": child.pid}
    head, run, state = s.current()
    if cut in {"commit_reply_lost", "before_receipt", "consumer_before_ack", "old_callback"}:
        assert state.status == "done_empty"
        before = world_dump(s.driver, s.world)
        s.writer.transaction(
            lambda tx: execution.read_completion_in_tx(
                tx, None, run, s.binding.work.id, state.completion_id
            )
        )
        if cut == "old_callback":
            event = ExecutionEvent(
                "decline",
                max(0, state.revision - 1),
                state.claim_epoch,
                int(synthetic.AT.timestamp()),
            )
            with pytest.raises(ExtractionRefused):
                s.writer.transaction(
                    lambda tx: execution.record_failure_in_tx(
                        tx,
                        None,
                        replace(head, execution_ordinal=head.execution_ordinal - 1),
                        run,
                        new_id(s.world),
                        event,
                    )
                )
        assert world_dump(s.driver, s.world) == before
        if cut == "consumer_before_ack":
            from grove.extraction.durable_types import ConsumerReceipt
            from grove.writer.learner_progress import acknowledge_handoff_in_tx

            handoffs = s.writer.transaction(
                lambda tx: boundary._rows(tx, "LearnerHandoff", work_id=s.binding.work.id)
            )
            stored = raw(
                s.driver, "MATCH (n:HistoryValue {world_id:$w}) RETURN n.value AS value", w=s.world
            )
            accepted = [
                json.loads(r["value"]) for r in stored if r["value"].startswith('{"handoff_id":')
            ]
            row = next(r for r in handoffs if r["id"] == accepted[0]["handoff_id"])
            receipt = ConsumerReceipt(
                row["id"], row["kind"], "synthetic-consumer-receipt", tuple(row["target_ids"])
            )
            monkeypatch.setattr(boundary, "_require_consumer_in_tx", lambda *args: None)
            first = s.writer.transaction(
                lambda tx: acknowledge_handoff_in_tx(tx, None, row["id"], row["version"], receipt)
            )
            before = world_dump(s.driver, s.world)
            assert (
                s.writer.transaction(
                    lambda tx: acknowledge_handoff_in_tx(
                        tx, None, row["id"], row["version"], receipt
                    )
                )
                == first
            )
            assert world_dump(s.driver, s.world) == before
    else:
        assert state.completion_id is None
        assert state.status not in {"done_empty", "done_extracted"}
        assert not raw(
            s.driver, "MATCH (n:LearnerCompletion {world_id:$w}) RETURN n.id AS id", w=s.world
        )
        if cut in {"after_reservation", "after_send", "after_result"}:
            assert state.transport_attempts == 1
            if cut == "after_result":
                assert state.invocations[-1].state == "validated"
                assert state.invocations[-1].model_call_id is not None
            else:
                assert all(call.model_call_id is None for call in state.invocations)


if __name__ == "__main__":
    raise SystemExit(_child(*sys.argv[1:]))


@pytest.mark.parametrize(
    "field,value", [("content", "[]"), ("speakers", "{}"), ("parser_version", "other")]
)
def test_same_version_source_bytes_or_speaker_change_refuses(native_scenario, field, value):
    s = native_scenario
    head, run, _ = s.current()

    def mutate(tx):
        row = boundary._one(tx, "Source", s.binding.source.id)
        boundary._put(tx, "Source", {**row, field: value}, row)

    s.writer.transaction(mutate)
    before = world_dump(s.driver, s.world)
    with pytest.raises(ExtractionRefused):
        s.writer.transaction(
            lambda tx: execution.claim_execution_in_tx(tx, None, head, run, new_id(s.world))
        )
    assert world_dump(s.driver, s.world) == before


def test_current_run_source_and_full_context_checked_inside_lock(native_scenario, monkeypatch):
    s = native_scenario
    head, run, _ = s.current()

    def changed(tx, session, purpose, value, witness):
        assert tx._locked
        return replace(value, context=replace(value.context, history_sequence=99))

    monkeypatch.setattr(boundary, "_require_authority_in_tx", changed)
    before = world_dump(s.driver, s.world)
    with pytest.raises(ExtractionRefused):
        s.writer.transaction(
            lambda tx: execution.claim_execution_in_tx(tx, None, head, run, new_id(s.world))
        )
    assert world_dump(s.driver, s.world) == before


def test_meter_generation_change_does_not_stale_semantic_binding(native_scenario, monkeypatch):
    s = native_scenario
    monkeypatch.setattr(
        boundary,
        "_require_authority_in_tx",
        lambda tx, se, p, b, r: replace(b, context=replace(b.context, read_generation=999)),
    )
    s.claim()
    assert s.current()[2].status == "running"


def test_only_one_repair_survives_concurrent_requeue_and_rebind(native_scenario):
    s = native_scenario
    s.ready()
    head, run, state = s.current()
    event = ExecutionEvent(
        "decline", state.revision, state.claim_epoch, int(synthetic.AT.timestamp())
    )
    s.writer.transaction(
        lambda tx: execution.record_failure_in_tx(tx, None, head, run, new_id(s.world), event)
    )
    head, run, state = s.current()
    event = ExecutionEvent(
        "repair", state.revision, state.claim_epoch, int(synthetic.AT.timestamp())
    )
    s.writer.transaction(
        lambda tx: execution.record_failure_in_tx(tx, None, head, run, new_id(s.world), event)
    )
    before = world_dump(s.driver, s.world)
    with pytest.raises(ExtractionRefused):
        s.writer.transaction(
            lambda tx: execution.record_failure_in_tx(tx, None, head, run, new_id(s.world), event)
        )
    assert world_dump(s.driver, s.world) == before
    assert s.current()[2].writer_repairs == 1


def test_recovery_checks_old_claimant_and_new_real_run_separately(native_scenario):
    s = native_scenario
    s.claim()
    head, run, state = s.current()
    replacement = new_id(s.world)

    def ended(tx):
        old = boundary._one(tx, "RunRecord", run.actual_run_id)
        boundary._put(
            tx, "RunRecord", {**old, "version": old["version"] + 1, "outcome": "killed"}, old
        )
        boundary._put(tx, "RunRecord", {**old, "id": replacement, "version": 1})

    s.writer.transaction(ended)
    event = ExecutionEvent(
        "recover", state.revision, state.claim_epoch, int(synthetic.AT.timestamp())
    )
    with pytest.raises(ExtractionRefused):
        s.writer.transaction(
            lambda tx: execution.record_failure_in_tx(
                tx,
                None,
                head,
                replace(run, actual_run_id=replacement, actual_row_version=1),
                new_id(s.world),
                event,
            )
        )
    s.writer.transaction(
        lambda tx: execution.record_failure_in_tx(
            tx,
            None,
            head,
            replace(run, actual_row_version=run.actual_row_version + 1),
            new_id(s.world),
            event,
        )
    )
    head, run, state = s.current()
    s.writer.transaction(
        lambda tx: execution.claim_execution_in_tx(
            tx,
            None,
            head,
            replace(
                run, actual_run_id=replacement, actual_row_version=1, pass_id="replacement-pass"
            ),
            new_id(s.world),
        )
    )
    assert s.current()[2].claimed_by == replacement


def test_complete_commits_graph_ledger_request_progress_and_outbox_once(
    driver, make_world, monkeypatch
):
    """Real ordinary writer and native transaction; supplied owner/model fixtures only."""
    from grove.extraction.contracts import RegistrySnapshot
    from tests.extraction import test_pipeline

    world = make_world()
    test_clock = clock.TestClock()
    test_clock.set(synthetic.AT)
    with clock.using(test_clock):
        create_world(driver, world, owner_user_id="synthetic-learner-owner")
        monkeypatch.setattr(synthetic, "WORLD", world)
        monkeypatch.setattr(boundary, "_require_authority_in_tx", _allow)
        original = test_pipeline.document

        def attach_document():
            doc = original()
            doc["operations"] = [
                {
                    "op": "attach_evidence",
                    "op_id": "o1",
                    "target_ref": "person-1",
                    "evidence_refs": [{"ev_id": "e1", "polarity": "supports"}],
                    "reason": "The source records this decision.",
                    "relation_to_standing": "complements",
                }
            ]
            return doc

        monkeypatch.setattr(test_pipeline, "document", attach_document)
        monkeypatch.setattr(
            test_pipeline,
            "registry",
            lambda: RegistrySnapshot(
                kinds={"person": ("active",)}, meanings=("related",), versions={"person-1": 1}
            ),
        )
        memory = synthetic.MemoryTx(world)
        binding, proposal, payload, trace = synthetic.ready_extracted(memory, monkeypatch)
        for label, rows in memory.tables.items():
            for row in rows.values():
                plant_node(
                    driver, world, label, **{k: v for k, v in row.items() if k != "world_id"}
                )
        [kind] = raw(
            driver, "MATCH (k:Kind {world_id:$w, name:'person'}) RETURN k.id AS id", w=world
        )
        plant_node(
            driver,
            world,
            "Item",
            id="person-1",
            kind=kind["id"],
            title="Cedar",
            version=1,
            recorded=synthetic.AT,
            touched_at=synthetic.AT,
        )
        writer = WorldWriter(driver, world)
        head, run, _ = writer.transaction(lambda tx: synthetic.current(tx, binding.work.id))
        result = writer.transaction(
            lambda tx: execution.complete_execution_in_tx(
                tx, None, head, run, new_id(world), proposal, payload, trace
            )
        )
        assert result.outcome == "done_extracted"
        assert len(raw(driver, "MATCH (n:Item {world_id:$w}) RETURN n.id AS id", w=world)) == 1
        assert (
            len(raw(driver, "MATCH (n:LearnerHandoff {world_id:$w}) RETURN n.id AS id", w=world))
            == 2
        )
        assert (
            len(raw(driver, "MATCH (n:LearnerCompletion {world_id:$w}) RETURN n.id AS id", w=world))
            == 1
        )
        _, run, _ = writer.transaction(lambda tx: synthetic.current(tx, binding.work.id))
        before = world_dump(driver, world)
        assert (
            writer.transaction(
                lambda tx: execution.read_completion_in_tx(
                    tx, None, run, binding.work.id, result.completion_id
                )
            )
            == result
        )
        assert world_dump(driver, world) == before
