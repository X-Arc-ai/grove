"""Inactive writer controls with a synthetic transactional in-memory store.

These tests exercise real lifecycle code and retained-message checks. They do
not establish native database atomicity, process recovery, or real authority.
"""

import copy
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from grove.cleanup.access import CleanupRefused
from grove.cleanup.legwork import LegworkAttemptV1
from grove.cleanup.question_intents import CuratorQuestionIntentV1, IntentPartV1
from grove.cleanup.question_lifecycle import (
    AnswerCommandV1,
    PlanV1,
    QuestionBindingV1,
    SourceBindingV1,
    TargetV1,
)
from grove.screening import Screener, ScreeningConfig
from grove.store.replay import QUESTION_SNAPSHOT_LABELS, ReplayRefused, validate_question_snapshots
from grove.writer import question_lifecycle as life
from grove.writer import requests
from grove.writer.caller import Caller
from grove.writer.errors import BatchDeclined, Refused

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
WORLD = "t_cleanup_00000000-0000-4000-8000-000000000001"
PERSON = Caller("person", {"grove.write"}, "owner")
CURATOR = Caller("cleanup", {"grove.propose"}, "curator")
SCREEN = Screener(ScreeningConfig())


class SyntheticStore:
    def __init__(self):
        self.tables = {}
        self.sequence = 0
        self.generation = 0
        self.lock = threading.RLock()
        self.fail_label = None

    def add(self, label, row):
        row = {"world_id": WORLD, **copy.deepcopy(row)}
        self.tables.setdefault(label, {})[row["id"]] = row


class SyntheticTx:
    world_id = WORLD

    def __init__(self, store):
        self.store = store

    def lock_world(self):
        pass

    def next_sequence(self):
        self.store.sequence += 1
        return self.store.sequence

    def run(self, query, **params):
        from grove.store.scope import scope_cypher

        scope_cypher(query, "write")
        match = re.search(r"[(:\[]([a-z]+):([A-Za-z]+)", query)
        if match is None:
            raise AssertionError(query)
        alias, label = match.groups()
        label = "Link" if label == "LINK" else label
        table = self.store.tables.setdefault(label, {})
        if query.startswith("CREATE"):
            if label == self.store.fail_label:
                raise RuntimeError("synthetic commit-stage failure")
            self.store.add(label, {k: v for k, v in params["row"].items() if v is not None})
            return []
        if "SET" in query:
            row = table[params["id"]]
            for key, value in params["row"].items():
                if value is None:
                    row.pop(key, None)
                else:
                    row[key] = copy.deepcopy(value)
            return []
        conditions = re.findall(r"(\w+): \$(\w+)", query)
        found = [
            row
            for row in table.values()
            if row.get("world_id") == WORLD
            and all(row.get(key) == params[param] for key, param in conditions)
        ]
        return [{alias: copy.deepcopy(row)} for row in found[:1001]]


class SyntheticWriter:
    def __init__(self, driver, world_id):
        assert world_id == WORLD
        self.driver = driver

    def transaction(self, callback):
        with self.driver.lock:
            before = copy.deepcopy((self.driver.tables, self.driver.sequence))
            try:
                result = callback(SyntheticTx(self.driver))
            except BaseException:
                self.driver.tables, self.driver.sequence = before
                raise
            if (self.driver.tables, self.driver.sequence) != before:
                self.driver.generation += 1
            return result


class SyntheticAuthority:
    def __init__(self):
        self.revoked = False
        self.refuse_action = None

    @contextmanager
    def hold(self, world_id, run_id, action, references):
        assert world_id == WORLD
        if self.revoked or action == self.refuse_action:
            raise CleanupRefused("synthetic_authority_refused")
        yield


@pytest.fixture
def memory(monkeypatch):
    store, access = SyntheticStore(), SyntheticAuthority()
    monkeypatch.setattr(life, "WorldWriter", SyntheticWriter)
    monkeypatch.setattr(life, "_ordinary", lambda tx: None)
    monkeypatch.setattr(life.clock, "now", lambda _: NOW)
    store.add(
        "Item",
        {"id": "claim", "version": 1, "status": "active", "recorded": NOW, "last_confirmed": NOW},
    )
    store.add("Link", {"id": "link", "version": 1, "active_key": "link"})
    store.add(
        "HistoryValue",
        {"id": "reason", "value": "The inventory contains no eligible source.", "version": 1},
    )
    store.add("Connector", {"id": "connector", "type": "person_in_views", "version": 1})
    store.add(
        "Source",
        {
            "id": "source",
            "version": 1,
            "connector_id": "connector",
            "forget_state": "kept",
            "received_at": NOW,
            "content": json.dumps(
                [{"key": "m1", "text": "Check the sources.", "at": NOW.isoformat()}]
            ),
        },
    )
    store.add(
        "Episode",
        {
            "id": "episode",
            "version": 1,
            "connector_id": "connector",
            "source_ids": ["source"],
            "first_message_key": "m1",
            "last_message_key": "m1",
        },
    )
    store.add("ScreeningReceipt", {"id": "screened", "source_id": "source"})
    return store, access


def source():
    return SourceBindingV1("source", 1, "episode", 1, "m1", "whole", NOW)


def make_question(
    memory,
    *,
    topic="topic",
    qid="question",
    parts=("part",),
    target=None,
    plans=None,
    identity=False,
    week=None,
):
    store, access = memory
    target = target or TargetV1("item", "claim", 1)
    attempt = LegworkAttemptV1(
        "legwork",
        "part",
        "run",
        target,
        "no_eligible_source",
        ("inventory",),
        1,
        "authority",
        "reason",
    )
    life.record_legwork(store, WORLD, CURATOR, attempt, access=access)
    plans = plans or (PlanV1("return", "return_to_sources", parts, (target,)),)
    binding = QuestionBindingV1(qid, 1, topic, (target,), plans, ("legwork",))
    result = life.create_or_join_question(
        store,
        WORLD,
        "run",
        CURATOR,
        binding,
        ask="Which source should confirm this?",
        kind="decision",
        reason="No source settles the question.",
        screener=SCREEN,
        identity=identity,
        backfill_week=week,
        access=access,
    )
    return binding, result


def command(binding, *, request="answer", plan="return", variant="return_to_sources"):
    return AnswerCommandV1(
        request, binding.question_id, binding.revision, plan, variant, source(), "owner"
    )


def submit(memory, cmd):
    store, access = memory
    return life.submit_answer(
        store, WORLD, "run", PERSON, cmd, answer_text="Check the sources.", access=access
    )


def apply(memory, cmd):
    store, access = memory
    return life.apply_answer(store, WORLD, "run", PERSON, cmd, access=access)


def snapshots(store):
    return {
        table: list(store.tables.get(label, {}).values())
        for label, table in QUESTION_SNAPSHOT_LABELS.items()
    }


def test_return_to_sources_applies_transfer_only_and_replay_is_read_only(memory):
    store, _ = memory
    binding, _ = make_question(memory)
    original_claim = copy.deepcopy(store.tables["Item"])
    cmd = command(binding)
    assert submit(memory, cmd) == "accepted"
    generation = store.generation
    result = apply(memory, cmd)
    assert store.generation == generation + 1
    assert store.tables["Question"]["question"]["state"] == "answered"
    assert store.tables["QuestionWork"]["part"]["state"] == "pending"
    assert (
        json.loads(store.tables["QuestionWork"]["part"]["target"]) == binding.targets[0].to_json()
    )
    assert result.transferred_ids == ("part",) and not result.resolved_ids and not result.effects
    assert "part" in result.pending_ids
    assert store.tables["Item"] == original_claim
    assert not store.tables.get("Evidence") and not store.tables.get("Verification")
    generation = store.generation
    assert apply(memory, cmd) == result
    assert store.generation == generation
    assert len(store.tables["QuestionApplication"]) == 1
    validate_question_snapshots(snapshots(store))


def test_answer_and_close_share_commit_and_lost_reply_reconstructs(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    before = copy.deepcopy((store.tables, store.sequence, store.generation))
    store.fail_label = "QuestionApplication"
    with pytest.raises(RuntimeError, match="commit-stage"):
        apply(memory, cmd)
    assert (store.tables, store.sequence, store.generation) == before
    store.fail_label = None
    result = apply(memory, cmd)  # discard the first transport result in the scenario
    assert life.reconstruct_application(store, WORLD, "run", PERSON, cmd, access=access) == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_version", 2),
        ("episode_version", 2),
        ("message_key", "unrelated"),
        ("locator", "key:other"),
    ],
)
def test_answer_binds_source_message_and_version(memory, field, value):
    binding, _ = make_question(memory)
    cmd = replace(command(binding), source=replace(source(), **{field: value}))
    before = copy.deepcopy(memory[0].tables)
    with pytest.raises((CleanupRefused, Refused)):
        submit(memory, cmd)
    assert memory[0].tables == before


def test_changed_text_or_request_key_refuses(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    with pytest.raises(CleanupRefused, match="text_changed"):
        life.submit_answer(
            store, WORLD, "run", PERSON, cmd, answer_text="A different answer.", access=access
        )
    submit(memory, cmd)
    with pytest.raises(CleanupRefused, match="idempotency_conflict"):
        submit(memory, replace(cmd, plan_id="other"))


def test_stale_target_refuses_before_work_changes(memory):
    store, _ = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    store.tables["Item"]["claim"]["version"] = 2
    with pytest.raises(CleanupRefused, match="target_version"):
        apply(memory, cmd)
    assert store.tables["Question"]["question"]["state"] == "open"


def test_replay_checks_current_authority_but_not_old_target_version(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    result = apply(memory, cmd)
    store.tables["Item"]["claim"]["version"] += 1
    assert apply(memory, cmd) == result
    access.revoked = True
    with pytest.raises(CleanupRefused, match="authority"):
        apply(memory, cmd)


def test_cancel_and_answer_have_one_winner(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    assert life.cancel_answer(store, WORLD, "run", PERSON, cmd, access=access) == "cancelled"
    with pytest.raises(CleanupRefused, match="not_applicable"):
        apply(memory, cmd)
    assert store.tables["Question"]["question"]["state"] == "open"
    second = replace(cmd, request_id="second")
    submit(memory, second)
    result = apply(memory, second)
    assert life.cancel_answer(store, WORLD, "run", PERSON, second, access=access) == result


def test_partial_answer_preserves_open_parts(memory):
    target = TargetV1("item", "claim", 1)
    plans = (
        PlanV1("first", "return_to_sources", ("a",), (target,)),
        PlanV1("second", "return_to_sources", ("b",), (target,)),
    )
    binding, _ = make_question(memory, plans=plans)
    cmd = command(binding, plan="first")
    submit(memory, cmd)
    result = apply(memory, cmd)
    assert set(result.pending_ids) == {"a", "b"}
    assert memory[0].tables["Question"]["question"]["state"] == "open"
    second = command(binding, request="second", plan="second")
    submit(memory, second)
    apply(memory, second)
    assert memory[0].tables["Question"]["question"]["state"] == "answered"


def test_coalesce_preserves_all_obligations_and_invalidates_old_binding(memory):
    store, _ = memory
    binding, _ = make_question(memory)
    generation = store.generation
    make_question(memory)
    assert store.generation == generation
    _, result = make_question(memory, qid="other", parts=("part", "new"))
    assert result["question_id"] == "question" and set(result["work_ids"]) == {"part", "new"}
    assert len(store.tables["Question"]) == 1
    with pytest.raises(CleanupRefused, match="binding_version"):
        submit(memory, command(binding))


def test_identity_question_cap_is_durable_and_retains_excess_work(memory):
    for index in range(3):
        _, result = make_question(
            memory,
            topic=f"topic{index}",
            qid=f"q{index}",
            parts=(f"part{index}",),
            identity=True,
            week=NOW.date(),
        )
        assert result["state"] == "open"
    _, result = make_question(
        memory, topic="fourth", qid="fourth", parts=("fourth",), identity=True, week=NOW.date()
    )
    assert result["state"] == "cap_reached" and result["question_id"] is None
    assert memory[0].tables["QuestionWork"]["fourth"]["state"] == "waiting_person"


def test_withdrawal_has_complete_work_disposition_and_reason(memory):
    store, access = memory
    make_question(memory, parts=("a", "b"))
    with pytest.raises(CleanupRefused, match="coverage"):
        life.withdraw_question(
            store,
            WORLD,
            "run",
            PERSON,
            "question",
            1,
            reason="No longer needed.",
            outcomes={"a": "cancel"},
            screener=SCREEN,
            access=access,
        )
    life.withdraw_question(
        store,
        WORLD,
        "run",
        PERSON,
        "question",
        1,
        reason="No longer needed.",
        outcomes={"a": "cancel", "b": "return_to_sources"},
        screener=SCREEN,
        access=access,
    )
    assert store.tables["QuestionWork"]["a"]["state"] == "cancelled"
    assert store.tables["QuestionWork"]["b"]["state"] == "pending"
    assert store.tables["Question"]["question"]["state"] == "withdrawn"
    validate_question_snapshots(snapshots(store))


def test_question_restore_refuses_mixed_commit_boundary(memory):
    store, _ = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    apply(memory, cmd)
    rows = snapshots(store)
    validate_question_snapshots(rows)
    rows = copy.deepcopy(rows)
    rows["question_work"][0]["state"] = "resolved"
    with pytest.raises(ReplayRefused, match="snapshot_mismatch"):
        validate_question_snapshots(rows)


def test_no_default_authority_and_host_paraphrase_refuse(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    with pytest.raises(CleanupRefused, match="authority_unavailable"):
        life.submit_answer(store, WORLD, "run", PERSON, cmd, answer_text="Check the sources.")
    store.tables["Connector"]["connector"]["type"] = "ai_host"
    with pytest.raises(CleanupRefused, match="not_verbatim"):
        life.submit_answer(
            store,
            WORLD,
            "run",
            Caller("ai_host", {"grove.write"}, "owner"),
            replace(cmd, host_message_id="host1"),
            answer_text="Check the sources.",
            access=access,
        )


@pytest.mark.parametrize("state", ["queued", "saved", "declined", "failed"])
def test_generic_replay_cannot_complete_question_protocol(state):
    row = {"id": "request", "protocol": life.PROTOCOL, "status": state, "fingerprint": "digest"}
    with pytest.raises(BatchDeclined) as caught:
        requests.replay_rule(row, "digest")
    assert caught.value.refusals[0].rule == "protected_request"


def test_legacy_touched_receipt_never_closes_question():
    from grove.writer.cleanup import close_question

    with pytest.raises(CleanupRefused, match="binding_required"):
        close_question(None, WORLD, "run", PERSON, "question", answer_request_id="saved")


def test_d10_intent_preserves_members_and_stale_source_rolls_back(memory):
    store, access = memory
    part = IntentPartV1(
        "disposition", "obligation", source(), "operation", "reason", 1, "unresolved"
    )
    intent = CuratorQuestionIntentV1(
        "intent", WORLD, "run", "request", "execution", 1, "topic", (part,), (), ()
    )
    assert life.consume_question_intent(store, WORLD, CURATOR, intent, access=access) == (
        ("disposition", "obligation"),
    )
    row = store.tables["QuestionWork"]["obligation"]
    assert row["state"] == "pending" and row["payload_value_id"] == "reason"
    assert not store.tables.get("Question") and not store.tables.get("QuestionApplication")
    store.tables["Source"]["source"]["version"] = 2
    with pytest.raises(CleanupRefused, match="source_version"):
        life.consume_question_intent(store, WORLD, CURATOR, intent, access=access)


def _synthetic_graph_effect(tx, stamp, prepared, request):
    """A controlled graph executor, not a substitute for D4 integration evidence."""
    batch = json.loads(prepared.document)
    for op in batch["ops"]:
        row_id = op["claim"]
        label = "Item" if row_id in tx.store.tables["Item"] else "Link"
        tx.store.tables[label][row_id]["version"] += 1
        tx.store.add(
            "ChangeEntry",
            {
                "id": "change",
                "entity_type": label.lower(),
                "entity_id": row_id,
                "commit_id": stamp.commit_id,
                "history_only": False,
            },
        )


def test_answer_link_uses_link_version_without_endpoint_touch(memory, monkeypatch):
    store, _ = memory
    target = TargetV1("link", "link", 1)
    plans = (PlanV1("confirm", "confirm", ("part",), (target,)),)
    binding, _ = make_question(memory, target=target, plans=plans)
    cmd = command(binding, plan="confirm", variant="choose_option")
    submit(memory, cmd)
    monkeypatch.setattr(life, "execute_prepared_in_tx", _synthetic_graph_effect)
    result = apply(memory, cmd)
    assert result.resolved_ids == ("part",)
    assert result.effects == (("link", "link", 1, 2, "answer_0"),)
    assert store.tables["Item"]["claim"]["version"] == 1
    assert store.tables["Link"]["link"]["version"] == 2


def test_unrelated_touched_effect_refuses_and_rolls_back(memory, monkeypatch):
    store, _ = memory
    target = TargetV1("link", "link", 1)
    binding, _ = make_question(
        memory, target=target, plans=(PlanV1("confirm", "confirm", ("part",), (target,)),)
    )
    cmd = command(binding, plan="confirm", variant="choose_option")
    submit(memory, cmd)

    def unrelated(tx, stamp, prepared, request):
        tx.store.tables["Item"]["claim"]["version"] += 1
        tx.store.add(
            "ChangeEntry",
            {
                "id": "wrong",
                "entity_type": "item",
                "entity_id": "claim",
                "commit_id": stamp.commit_id,
            },
        )

    monkeypatch.setattr(life, "execute_prepared_in_tx", unrelated)
    before = copy.deepcopy(store.tables)
    with pytest.raises(CleanupRefused, match="exact_effect_missing"):
        apply(memory, cmd)
    assert store.tables == before


def test_failure_after_graph_effect_rolls_back_graph_and_question(memory, monkeypatch):
    store, _ = memory
    target = TargetV1("item", "claim", 1)
    binding, _ = make_question(memory, plans=(PlanV1("confirm", "confirm", ("part",), (target,)),))
    cmd = command(binding, plan="confirm", variant="choose_option")
    submit(memory, cmd)
    monkeypatch.setattr(life, "execute_prepared_in_tx", _synthetic_graph_effect)
    store.fail_label = "QuestionApplication"
    before = copy.deepcopy(store.tables)
    with pytest.raises(RuntimeError):
        apply(memory, cmd)
    assert store.tables == before


def test_shared_executor_preserves_shape_and_two_source_checks(monkeypatch):
    from grove.writer import api
    from grove.writer.history import Stamp

    calls = []
    batch = {
        "ops": [
            {
                "op": "verify",
                "op_id": "v",
                "claim": "claim",
                "pieces": [{"ev_id": "answer", "answer": "supports"}],
                "disposition": "confirmed",
                "reason": "A reason.",
            }
        ],
        "evidence": [life._observation(source())],
    }
    prepared = api.prepare_batch(PERSON, batch, request_id="req")
    batch["ops"].clear()
    monkeypatch.setattr(api, "check_batch_sources", lambda *args: calls.append("source"))
    monkeypatch.setattr(
        api.apply_module, "apply_ops", lambda *args, **kw: calls.append(("ops", len(kw["ops"])))
    )
    tx = SyntheticTx(SyntheticStore())
    api.execute_prepared_in_tx(
        tx, Stamp(WORLD, NOW, "owner", "commit", "change", caller_kind="person"), prepared
    )
    assert calls == ["source", ("ops", 1), "source"]
    with pytest.raises(BatchDeclined):
        api.prepare_batch(PERSON, {"ops": []})


def test_generic_apply_and_finish_refuse_before_saved_shortcuts(memory, monkeypatch):
    from grove.writer import api

    row = {"id": "protected", "protocol": life.PROTOCOL, "status": "saved", "fingerprint": "x"}
    monkeypatch.setattr(api, "read_row", lambda *args: row)
    with pytest.raises(BatchDeclined):
        api.apply_batch(None, WORLD, PERSON, {"ops": []}, request_id="protected")
    monkeypatch.setattr(requests, "read_row", lambda *args: row)
    with pytest.raises(BatchDeclined):
        requests.apply_request(None, WORLD, PERSON, "protected", {"ops": []})

    class ReadTx:
        def run(self, *args, **kwargs):
            return [{"r": row}]

    monkeypatch.setattr(requests, "_worked", lambda driver, world, request, work: work(ReadTx()))
    with pytest.raises(BatchDeclined):
        requests.finish_request(None, WORLD, CURATOR, "protected", worker="worker")


def test_final_release_refusal_preserves_committed_effect(memory):
    store, access = memory
    binding, _ = make_question(memory)
    cmd = command(binding)
    submit(memory, cmd)
    access.refuse_action = "release_answer"
    with pytest.raises(CleanupRefused, match="authority"):
        apply(memory, cmd)
    assert store.tables["Request"]["answer"]["answer_state"] == "applied"
    access.refuse_action = None
    result = apply(memory, cmd)
    assert result.request_id == "answer" and len(store.tables["QuestionApplication"]) == 1


def test_d10_replay_rejects_changed_execution_binding(memory):
    store, access = memory
    part = IntentPartV1(
        "disposition", "obligation", source(), "operation", "reason", 1, "unresolved"
    )
    intent = CuratorQuestionIntentV1(
        "intent", WORLD, "run", "request", "execution", 1, "topic", (part,), (), ()
    )
    life.consume_question_intent(store, WORLD, CURATOR, intent, access=access)
    with pytest.raises(CleanupRefused, match="replay_conflict"):
        life.consume_question_intent(
            store, WORLD, CURATOR, replace(intent, execution_version=2), access=access
        )


def test_identity_backfill_week_retains_eleventh_obligation(memory, monkeypatch):
    from datetime import timedelta

    store, _ = memory
    for index in range(11):
        now = NOW + timedelta(days=index // 3)
        monkeypatch.setattr(life.clock, "now", lambda _, now=now: now)
        _, result = make_question(
            memory,
            topic=f"topic{index}",
            qid=f"q{index}",
            parts=(f"work{index}",),
            identity=True,
            week=NOW.date(),
        )
        assert result["state"] == ("open" if index < 10 else "cap_reached")
    assert len(store.tables["Question"]) == 10
    assert store.tables["QuestionWork"]["work10"]["state"] == "waiting_person"


def test_concurrent_topic_creation_has_one_question_and_all_parts(memory):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(make_question, memory, qid=f"q{index}", parts=(f"p{index}",))
            for index in range(2)
        ]
        results = [future.result()[1] for future in futures]
    assert results[0]["question_id"] == results[1]["question_id"]
    assert set(memory[0].tables["QuestionWork"]) == {"p0", "p1"}


def test_ordered_verification_restore_requires_exact_marker():
    rows = {
        "verifications": [
            {
                "id": "verification",
                "claim_id": "claim",
                "commit_id": "commit",
                "commit_sequence": 3,
                "attempt_kind": "no_source",
                "evidence_pieces": "[]",
                "agree": 0,
                "disagree": 0,
                "band": "zero_signal",
                "disposition": "deferred",
            }
        ],
        "history_values": [{"id": "value", "value": '"verification"'}],
        "change_entries": [
            {
                "sequence": 3,
                "field": "verified",
                "entity_id": "claim",
                "commit_id": "commit",
                "new_value_id": "value",
            }
        ],
    }
    validate_question_snapshots(rows)
    rows["verifications"][0]["commit_sequence"] = 4
    with pytest.raises(ReplayRefused, match="verification_order"):
        validate_question_snapshots(rows)


def test_shared_executor_runs_resolution_guard_before_graph_operations(monkeypatch):
    from grove.writer import api, verify
    from grove.writer.history import Stamp

    calls = []
    batch = {
        "ops": [
            {
                "op": "verify",
                "op_id": "v",
                "claim": "claim",
                "pieces": [{"ev_id": "answer", "answer": "supports"}],
                "disposition": "confirmed",
                "reason": "A reason.",
            }
        ],
        "evidence": [life._observation(source())],
    }
    prepared = api.prepare_batch(CURATOR, batch, request_id="req")
    monkeypatch.setattr(api, "check_batch_sources", lambda *args: calls.append("source"))

    def refuse(*args):
        calls.append("guard")
        raise CleanupRefused("synthetic_oscillation")

    monkeypatch.setattr(verify, "guard_scored_resolutions", refuse)
    monkeypatch.setattr(api.apply_module, "apply_ops", lambda *args, **kw: calls.append("ops"))
    with pytest.raises(CleanupRefused, match="oscillation"):
        api.execute_prepared_in_tx(
            SyntheticTx(SyntheticStore()),
            Stamp(WORLD, NOW, "curator", "commit", "change", run_id="run", caller_kind="cleanup"),
            prepared,
        )
    assert calls == ["source", "guard"]
