"""Real scored writer/history/replay with explicit synthetic in-memory storage.

Resolved observations stand in for source admission. No database, model, account,
native transaction, concurrency, or runtime acceptance is exercised here.
Use --noconftest to exclude the repository's live-store fixtures.
"""

import copy
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from grove.store import replay
from grove.store.scope import scope_cypher
from grove.writer import apply, verify
from grove.writer.errors import Refused
from grove.writer.history import Stamp, add_entry, row_json
from tests.cleanup import test_no_source_deferral as no_source

AT, WORLD, CALLER = no_source.AT, no_source.WORLD, no_source.CALLER
base_env = no_source.env
MISSING = object()
NEXT = AT + timedelta(days=1)
HOW = {"connector": "web_pages", "query": "Synthetic service status"}


class SyntheticScoredTransaction(no_source.SyntheticInMemoryTransactionAdapter):
    def run(self, query, **params):
        if query == verify._JUDGMENTS:
            assert self.locked
            scope_cypher(query, "write")
            self.state["queries"].append(query)
            calls = {row["id"]: row for row in self.rows("ModelCall")}
            return [
                {
                    **copy.deepcopy(row),
                    "model": calls[row["model_call_id"]]["model"],
                    "run_id": calls[row["model_call_id"]]["run_id"],
                }
                for row in self.rows("Judgment")
                if row["id"] in params["ids"]
            ]
        if query == apply._SAME_EVIDENCE:
            assert self.locked
            scope_cypher(query, "write")
            self.state["queries"].append(query)
            return [
                {"e": copy.deepcopy(row)}
                for row in self.rows("Evidence")
                if (row["message_key"], row["target_id"], row["polarity"])
                == (params["key"], params["target"], params["polarity"])
            ]
        return super().run(query, **params)


class SyntheticScoredStore(no_source.SyntheticInMemoryStore):
    def transaction(self, work):
        draft = copy.deepcopy(self.state)
        tx = SyntheticScoredTransaction(draft)
        tx.lock_world()
        result = work(tx)
        self.state = draft
        return result


@pytest.fixture
def env(base_env):
    base_env.store = SyntheticScoredStore(base_env.store.state)
    store = base_env.store
    store.rows("Kind")[0].update(
        name="Work", status_vocabulary=json.dumps([{"status": "open", "role": "open"}])
    )
    store.rows("Meaning")[0].update(
        name="Synthetic state", from_kinds='{"any": true}', to_kinds='{"any": true}'
    )
    store.rows("Item")[0].update(title="Synthetic claim", recheck_by=NEXT)
    store.add("Connector", id="connector", type="web_pages")
    store.add("Source", id="source", connector_id="connector", received_at=AT)
    for identity in ("first", "second"):
        store.add("ModelCall", id=identity, model=f"synthetic-{identity}", run_id="judge-run")
        store.add(
            "Judgment",
            id=identity,
            model_call_id=identity,
            subject="item:claim",
            source_id="source",
            locator="whole",
            role=f"{identity}_judge",
            verdict="not_about_claim" if identity == "first" else "supports",
        )
    return base_env


def seed(env, *, kind="item", old=MISSING):
    store = env.store
    if kind == "link":
        store.add("Item", id="other", kind="work-kind", title="Synthetic peer", version=1)
        store.add(
            "LINK",
            id="link-claim",
            from_item="claim",
            to_item="other",
            meaning="state-meaning",
            active_key=f"{WORLD}|claim|state-meaning|other",
            recorded=AT - timedelta(days=30),
            touched_at=AT,
            version=1,
            recheck_by=NEXT,
        )
    claim = store.rows("Item" if kind == "item" else "LINK")[0]
    if old is MISSING:
        claim.pop("recheck_how", None)
    else:
        claim["recheck_how"] = old
    for row in store.rows("Judgment"):
        row["subject"] = f"{kind}:{claim['id']}"

    def work(tx):
        stamp = Stamp(WORLD, AT - timedelta(days=2), "synthetic", "create", "create")
        for label, entity, table in (
            ("Kind", "registry_row", "kinds"),
            ("Meaning", "registry_row", "meanings"),
            ("Item", "item", "items"),
            ("LINK", "link", "links"),
        ):
            for row in store.rows(label):
                add_entry(
                    tx,
                    stamp,
                    entity_type=entity,
                    entity_id=row["id"],
                    field="row",
                    new=row_json(table, row, WORLD),
                    world_time=None,
                )

    store.transaction(work)
    return claim["id"]


def scored(env, claim="claim", *, how=HOW, disposition="deferred", number=1, recheck=NEXT):
    def work(tx):
        stamp = Stamp(
            WORLD,
            AT,
            CALLER.actor,
            f"commit-{number}",
            f"change-{number}",
            request_id=f"request-{number}",
            run_id=f"run-{number}",
            caller_kind="cleanup",
        )
        run = apply._Run(tx, stamp, {}, "cleanup")
        # The fixture supplies only the resolved observation. The actual _pieces
        # path still reads Source, Connector, Judgment and ModelCall rows.
        run.resolved["observed"] = {
            "source_id": "source",
            "episode_id": "episode",
            "message_key": f"message-{number}",
            "locator": "whole",
            "source_time": AT - timedelta(hours=1),
            "source_type": "confirmed",
            "derivation": "observed",
            "speaker_kind": "person",
            "modality": "text",
        }
        op = {
            "claim": claim,
            "pieces": [{"ev_id": "observed", "judgment_id": "first"}],
            "disposition": disposition,
            "reason": "Synthetic scored verification",
            "recheck_by": recheck.isoformat(),
            "second_judge": ["second"],
        }
        if how is not MISSING:
            op["recheck_how"] = how
        verify.verify_op(run, 0, op)

    env.store.transaction(work)


def history(env, claim="claim"):
    return [
        row
        for row in env.store.rows("ChangeEntry")
        if row["entity_id"] == claim and row["field"] == "recheck_how"
    ]


def decoded(env, entry, side):
    values = {row["id"]: row["value"] for row in env.store.rows("HistoryValue")}
    identity = entry.get(f"{side}_value_id")
    return None if identity is None else json.loads(values[identity])


def rebuilt(env, claim="claim", kind="item", *, known_at=None):
    projection = replay.rebuild(
        env.store.rows("ChangeEntry"),
        {row["id"]: row["value"] for row in env.store.rows("HistoryValue")},
        known_at=known_at,
    )
    return projection.rows["items" if kind == "item" else "links"][claim]


@pytest.mark.parametrize("kind", ["item", "link"])
@pytest.mark.parametrize("old", [MISSING, '"check_sources"', '{"query":"Old synthetic query"}'])
@pytest.mark.parametrize("how", [HOW, "ask_the_person"])
def test_scored_change_records_semantic_old_new_history(env, kind, old, how):
    claim = seed(env, kind=kind, old=old)
    scored(env, claim, how=how)
    (event,) = history(env, claim)
    assert event["entity_type"] == kind
    assert decoded(env, event, "old") == (None if old is MISSING else json.loads(old))
    assert decoded(env, event, "new") == how
    assert event["change_set_id"] == "change-1" and event["request_id"] == "request-1"
    assert event["world_time"] == AT - timedelta(hours=1)
    (verification,) = env.store.rows("Verification")
    assert event["reason"] == verification["reason"]
    assert verification["attempt_kind"] == "scored" and verification["band"] == "zero_signal"
    assert verification["disposition"] == "deferred" and verification["run_ordinal"] == 1
    assert event["sequence"] < verification["commit_sequence"]
    actual = env.store.rows("Item" if kind == "item" else "LINK")[0]
    restored = rebuilt(env, claim, kind)
    assert json.loads(restored["recheck_how"]) == json.loads(actual["recheck_how"]) == how
    assert restored["version"] == actual["version"] == 2


@pytest.mark.parametrize("kind", ["item", "link"])
@pytest.mark.parametrize("disposition", ["deferred", "confirmed"])
def test_scored_rebuild_preserves_exact_fields_and_record_axis(env, kind, disposition):
    claim = seed(env, kind=kind, old='"check_sources"')
    if disposition == "confirmed":
        env.store.rows("Judgment")[0]["verdict"] = "supports"
    scored(env, claim, disposition=disposition, recheck=NEXT + timedelta(days=1))
    actual = env.store.rows("Item" if kind == "item" else "LINK")[0]
    restored = rebuilt(env, claim, kind)
    for field in (
        "recheck_how",
        "recheck_by",
        "version",
        "recorded",
        "status",
        "last_confirmed",
        "confirmed_against",
        "became_true_precision",
        "touched_at",
    ):
        assert restored.get(field) == actual.get(field), field
    assert actual["version"] == 2
    prior = rebuilt(env, claim, kind, known_at=AT - timedelta(seconds=1))
    assert json.loads(prior["recheck_how"]) == "check_sources" and prior["version"] == 1
    assert len(history(env, claim)) == 1


@pytest.mark.parametrize("kind", ["item", "link"])
@pytest.mark.parametrize(
    "old,how",
    [
        ('"check_sources"', "check_sources"),
        (' { "query" : "Synthetic service status", "connector" : "web_pages" } ', HOW),
    ],
)
def test_semantically_unchanged_recheck_has_no_field_event_or_version_bump(env, kind, old, how):
    claim = seed(env, kind=kind, old=old)
    scored(env, claim, how=how)
    actual = env.store.rows("Item" if kind == "item" else "LINK")[0]
    assert not history(env, claim)
    assert actual["recheck_how"] == old
    assert actual["version"] == rebuilt(env, claim, kind)["version"] == 1
    assert len(env.store.rows("Verification")) == 1


def test_absent_recheck_argument_preserves_existing_history_and_value(env):
    seed(env, old='"check_sources"')
    scored(env, how=MISSING)
    assert not history(env)
    assert rebuilt(env)["recheck_how"] == env.store.rows("Item")[0]["recheck_how"]


def test_scored_then_no_source_history_survives_synthetic_restart_and_replay(env):
    seed(env, old='"check_sources"')
    scored(env)
    env.store = SyntheticScoredStore(env.store.state)
    env.timer.advance(world_id=WORLD, days=2)
    command = replace(no_source.command_for(env, 2), recheck_how="ask_the_person")
    identity = no_source.record(env, command)
    snapshot = copy.deepcopy(env.store.state["tables"])
    assert no_source.record(env, command) == identity
    assert env.store.state["tables"] == snapshot
    events = history(env)
    assert [decoded(env, event, "new") for event in events] == [HOW, "ask_the_person"]
    assert decoded(env, events[1], "old") == HOW
    actual = env.store.rows("Item")[0]
    restored = rebuilt(env)
    assert restored["recheck_how"] == actual["recheck_how"] == '"ask_the_person"'
    assert restored["version"] == actual["version"] == 3
    assert not env.store.rows("Evidence")


def test_scored_history_failure_rolls_back_synthetic_transaction(env):
    seed(env, old='"check_sources"')
    env.store.state["fail_on"] = "ChangeEntry"
    before = copy.deepcopy(env.store.state)
    with pytest.raises(RuntimeError, match="synthetic transaction fault"):
        scored(env)
    assert env.store.state == before


def test_wrong_judgment_still_refuses_before_history(env):
    seed(env, old='"check_sources"')
    env.store.rows("Judgment")[0]["subject"] = "item:other"
    before = copy.deepcopy(env.store.state)
    with pytest.raises(Refused, match="did not judge this claim"):
        scored(env)
    assert env.store.state == before


def test_loadbearing_disagreement_still_refuses_before_history(env):
    env.store.rows("Meaning")[0]["carries_consequences"] = True
    claim = seed(env, kind="link", old='"check_sources"')
    env.store.rows("Judgment")[0]["verdict"] = "supports"
    env.store.rows("Judgment")[1]["verdict"] = "contradicts"
    before = copy.deepcopy(env.store.state)
    with pytest.raises(Refused, match="the two judges disagree"):
        scored(env, claim, disposition="confirmed")
    assert env.store.state == before
