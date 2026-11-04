"""Synthetic source tests only: no database, provider, native lock or process kill.

The in-memory transaction adapter models atomic rollback and a durable snapshot
loaded by a new adapter. This is not evidence of Neo4j durability or concurrency.
Run this file with --noconftest to exclude the repository's live-store fixtures.
"""

import copy
import json
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from grove.cleanup.legwork import LegworkAttemptV1, NoSourceDeferralV1
from grove.cleanup.question_lifecycle import TargetV1, canonical_json

from grove import clock
from grove.cleanup.access import CleanupRefused
from grove.cleanup.runtime import require_runtime
from grove.store import lane, replay
from grove.store.primitives import KIND_BELIEF
from grove.store.scope import scope_cypher
from grove.writer import apply, cleanup, verify
from grove.writer.caller import Caller
from grove.writer.errors import BatchDeclined, Refused
from grove.writer.history import Stamp, add_entry, encode, row_json
from grove.writer.ops import shape_refusals

WORLD = "t_deferral_11111111-1111-4111-8111-111111111111"
AT = datetime(2026, 11, 4, 12, tzinfo=UTC)
CALLER = Caller("cleanup", {"grove.write"}, "synthetic-curator")


class SyntheticInMemoryTransactionAdapter:
    """Only the exact query shapes exercised here; unknown queries fail loudly."""

    def __init__(self, state):
        self.state = state
        self.world_id = WORLD
        self.locked = False

    def lock_world(self):
        self.locked = True

    def next_sequence(self):
        assert self.locked
        self.state["sequence"] += 1
        return self.state["sequence"]

    def rows(self, label):
        return self.state["tables"].setdefault(label, [])

    def run(self, query, **p):
        assert self.locked
        # Validate against the real scoper and the integrated schema catalogue.
        scope_cypher(query, "write")
        self.state["queries"].append(query)
        if self.state.get("fail_on") and self.state["fail_on"] in query:
            raise RuntimeError("synthetic transaction fault")
        create = re.fullmatch(r"CREATE \((\w+):(\w+)\) SET \1 \+= \$(row|entry)", query)
        if create:
            self.rows(create[2]).append({**copy.deepcopy(p[create[3]]), "world_id": WORLD})
            return []
        if query == "MATCH (r:CuratorRunOrder) RETURN coalesce(max(r.ordinal), 0) AS ordinal":
            return [
                {"ordinal": max((r["ordinal"] for r in self.rows("CuratorRunOrder")), default=0)}
            ]
        if "ORDER BY r.ordinal DESC LIMIT 3" in query:
            return [
                {"run_id": r["run_id"]}
                for r in sorted(self.rows("CuratorRunOrder"), key=lambda r: -r["ordinal"])[:3]
            ]
        if query.startswith("MATCH (v:Verification") and "(h:HistoryValue)" in query:
            result = []
            values = {r["id"]: r["value"] for r in self.rows("HistoryValue")}
            for v in self.rows("Verification"):
                for e in self.rows("ChangeEntry"):
                    if e["field"] != "verified" or e["commit_id"] != v["commit_id"]:
                        continue
                    value = values.get(e["new_value_id"])
                    if "value" in p:
                        if (
                            v["id"] == p["id"]
                            and e["entity_id"] == v["claim_id"]
                            and value == p["value"]
                        ):
                            result.append({"sequence": e["sequence"]})
                    elif (
                        v["claim_id"] == p["id"]
                        and e["entity_id"] == p["id"]
                        and e["entity_type"] == p["kind"]
                    ):
                        result.append(
                            {"v": copy.deepcopy(v), "sequence": e["sequence"], "value": value}
                        )
            return sorted(result, key=lambda r: r["sequence"])
        if query.startswith("MATCH (e:CuratorEscalation"):
            return [
                {"e": copy.deepcopy(r)}
                for r in self.rows("CuratorEscalation")
                if (r["claim_type"], r["claim_id"], r["streak_start"])
                == (p["kind"], p["id"], p["start"])
            ]
        if query == lane._ITEMS:
            return [
                copy.deepcopy(r)
                for r in self.rows("Item")
                if not r.get("end_class") and r["kind"] != p["belief"]
            ]
        if query == lane._LINKS:
            return [
                copy.deepcopy(r)
                for r in self.rows("LINK")
                if r.get("active_key") and not r.get("end_class")
            ]
        if query == lane._NEWEST_EVIDENCE:
            return [
                {
                    "id": claim,
                    "newest": max(
                        r["recorded"]
                        for r in self.rows("Evidence")
                        if r.get("active") and r["target_id"] == claim
                    ),
                }
                for claim in p["ids"]
                if any(r.get("active") and r["target_id"] == claim for r in self.rows("Evidence"))
            ]
        if query == lane._CONFIRMED_AT:
            return [
                {"id": r["id"], "at": r["at"]}
                for r in self.rows("Verification")
                if r["id"] in p["ids"]
            ]
        if query == "MATCH (h:HistoryValue {id: $id}) RETURN h.value AS value":
            return [{"value": r["value"]} for r in self.rows("HistoryValue") if r["id"] == p["id"]]
        if "RETURN w.self_item_id AS own" in query:
            return [{"own": None}]
        if query.startswith("MATCH (a:Alias)"):
            return []
        if query.startswith("MATCH (c:Capability") or "RETURN DISTINCT e.sender AS sender" in query:
            return []
        for label, alias in (("Kind", "k"), ("Meaning", "m"), ("PropertyName", "p")):
            if query == f"MATCH ({alias}:{label}) RETURN {alias}":
                return [{alias: copy.deepcopy(r)} for r in self.rows(label)]
        label = "LINK" if "[l:LINK" in query else None
        alias = "l" if label else None
        match = re.match(r"MATCH \((\w+):(\w+)(?: \{(id|run_id|attempt_id): \$id\})?\)", query)
        if match:
            alias, label, key = match.groups()
        else:
            key = "id"
        if label:
            rows = [r for r in self.rows(label) if r.get(key or "id") == p.get("id")]
            if "SET" in query:
                for r in rows:
                    if "+= $row" in query:
                        for key, value in p["row"].items():
                            if value is None:
                                r.pop(key, None)
                            else:
                                r[key] = copy.deepcopy(value)
                    if "+= $columns" in query:
                        r.update(copy.deepcopy(p["columns"]))
                    if "touched_at = $at" in query:
                        r["touched_at"] = p["at"]
                    if "version = coalesce" in query:
                        r["version"] = r.get("version", 0) + 1
                    if "commit_sequence = $sequence" in query:
                        r["commit_sequence"] = p["sequence"]
            if f"RETURN {alias}" in query:
                return [{alias: copy.deepcopy(r)} for r in rows]
            if "SET" in query:
                return []
        raise AssertionError(f"unimplemented synthetic query: {query}")

    def unscoped(self):
        # The only unscoped read reached by these leaf fixtures is the self item.
        return SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(data=lambda: [{"own": None}]))


class SyntheticInMemoryStore:
    def __init__(self, snapshot=None):
        self.state = (
            copy.deepcopy(snapshot) if snapshot else {"sequence": 0, "tables": {}, "queries": []}
        )
        self.lose_reply = False

    def transaction(self, work):
        draft = copy.deepcopy(self.state)
        tx = SyntheticInMemoryTransactionAdapter(draft)
        tx.lock_world()
        result = work(tx)
        self.state = draft
        if self.lose_reply:
            self.lose_reply = False
            raise RuntimeError("synthetic lost reply after commit")
        return result

    def rows(self, label):
        return self.state["tables"].setdefault(label, [])

    def add(self, label, **row):
        self.rows(label).append({"world_id": WORLD, **row})


class SyntheticAccessLease:
    def __init__(self):
        self.revoked = False
        self.depth = 0
        self.calls = []

    @contextmanager
    def hold(self, world_id, run_id, action, references):
        assert world_id == WORLD
        if self.revoked:
            raise CleanupRefused("synthetic_revoked")
        self.calls.append((run_id, action, references))
        self.depth += 1
        try:
            yield
            if self.revoked:
                raise CleanupRefused("synthetic_revoked")
        finally:
            self.depth -= 1


@pytest.fixture
def env(monkeypatch):
    store, access, timer = SyntheticInMemoryStore(), SyntheticAccessLease(), clock.TestClock()
    timer.set(AT, WORLD)
    monkeypatch.setattr(verify, "WorldWriter", lambda driver, world: driver)
    monkeypatch.setattr(cleanup, "designation_in_tx", lambda tx: {})
    store.add("Kind", id="work-kind", lifecycle_class="work_with_state")
    store.add("Meaning", id="state-meaning", temporal_class="state")
    store.add("HistoryValue", id="reason", value="Synthetic checked source inventory", version=1)
    store.add(
        "Item",
        id="claim",
        kind="work-kind",
        status="open",
        status_role="open",
        version=1,
        touched_at=AT,
        recorded=AT - timedelta(days=30),
        became_true_precision="unknown",
        recheck_by=AT - timedelta(days=1),
        recheck_how='"check_sources"',
    )
    with clock.using(timer):
        yield SimpleNamespace(store=store, access=access, timer=timer)


def command_for(env, number=1, *, result="no_eligible_source", kind="item"):
    claim = env.store.rows("Item" if kind == "item" else "LINK")[0]
    target = TargetV1(kind, claim["id"], claim["version"])
    run_id = f"run-{number}"
    if not any(r["id"] == run_id for r in env.store.rows("RunRecord")):
        env.store.add(
            "RunRecord", id=run_id, agent="curator", outcome="running", started=clock.now(WORLD)
        )
    attempt = LegworkAttemptV1(
        f"legwork-{number}",
        "work",
        run_id,
        target,
        result,
        ("synthetic-scope",),
        1,
        "synthetic-authority",
        "reason",
    )
    env.store.add(
        "CuratorLegwork",
        id=attempt.attempt_id,
        work_id="work",
        run_id=run_id,
        target=canonical_json(target),
        result=result,
        attempt=canonical_json(attempt),
        reason="reason",
        recorded_at=clock.now(WORLD),
        version=1,
    )
    return NoSourceDeferralV1(
        f"attempt-{number}",
        "work",
        run_id,
        target,
        (attempt.attempt_id,),
        "reason",
        clock.now(WORLD) + timedelta(days=1),
        "check_sources",
    )


def record(env, command):
    return verify.record_no_source_deferral(env.store, WORLD, CALLER, command, access=env.access)


def seed_verification(env, identity, disposition="deferred", run_id=None):
    def work(tx):
        stamp = Stamp(
            WORLD, AT, CALLER.actor, f"commit-{identity}", f"change-{identity}", run_id=run_id
        )
        if run_id:
            verify.accepted_curator_run(tx, run_id, AT, stamp=stamp)
        tx.run(
            "CREATE (v:Verification) SET v += $row",
            row={
                "id": identity,
                "claim_id": "claim",
                "run_id": run_id,
                "disposition": disposition,
                "commit_id": stamp.commit_id,
                "at": AT,
            },
        )
        add_entry(
            tx,
            stamp,
            entity_type="item",
            entity_id="claim",
            field="verified",
            new=identity,
            world_time=None,
        )

    env.store.transaction(work)


def test_no_source_preserves_truth_and_creates_no_evidence(env):
    command = command_for(env)
    before = copy.deepcopy(env.store.rows("Item")[0])
    verification_id = record(env, command)
    after = env.store.rows("Item")[0]
    allowed = {"recheck_by", "recheck_how", "version", "touched_at"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert after["version"] == 2 and after["recheck_by"] == command.next_check
    (row,) = env.store.rows("Verification")
    assert row["id"] == verification_id and row["attempt_kind"] == "no_source"
    assert row["agree"] == row["disagree"] == 0 and row["band"] == "zero_signal"
    assert row["evidence_pieces"] == "[]" and "ratio" not in row
    assert "second_judge_judgment_ids" not in row
    assert not any(env.store.rows(label) for label in ("Source", "Evidence", "Judgment"))
    assert all("world_time" not in e for e in env.store.rows("ChangeEntry"))
    assert row["commit_sequence"] == max(e["sequence"] for e in env.store.rows("ChangeEntry"))
    assert env.access.depth == 0 and len(env.access.calls) == 2


def test_synthetic_lost_reply_restart_and_replay(env):
    command = command_for(env)
    env.store.lose_reply = True
    with pytest.raises(RuntimeError, match="lost reply"):
        record(env, command)
    env.store = SyntheticInMemoryStore(env.store.state)
    before = copy.deepcopy(env.store.state["tables"])
    assert record(env, command) == env.store.rows("Verification")[0]["id"]
    assert env.store.state["tables"] == before
    env.access.revoked = True
    with pytest.raises(CleanupRefused, match="synthetic_revoked"):
        record(env, command)
    assert env.store.state["tables"] == before


def test_reused_attempt_with_changed_command_refuses(env):
    command = command_for(env)
    record(env, command)
    before = copy.deepcopy(env.store.state)
    with pytest.raises(CleanupRefused, match="idempotency_conflict"):
        record(env, replace(command, next_check=command.next_check + timedelta(days=1)))
    assert env.store.state == before


@pytest.mark.parametrize(
    "fault", ["CREATE (e:ChangeEntry)", "CREATE (w:QuestionWork)", "CREATE (e:CuratorEscalation)"]
)
def test_synthetic_transaction_rollback_at_each_stage(env, fault):
    seed_verification(env, "old-1")
    seed_verification(env, "old-2")
    command = command_for(env)
    env.store.state["fail_on"] = fault
    before = copy.deepcopy(env.store.state)
    with pytest.raises(RuntimeError, match="synthetic transaction fault"):
        record(env, command)
    assert env.store.state == before
    env.store.state.pop("fail_on")
    record(env, command)
    assert env.store.rows("CuratorRunOrder")[0]["ordinal"] == 1
    assert len(env.store.rows("Verification")) == 3


@pytest.mark.parametrize("result", ["permission_required", "unavailable", "incomplete"])
def test_incomplete_legwork_cannot_prove_source_absence(env, result):
    command = command_for(env, result=result)
    before = copy.deepcopy(env.store.state)
    with pytest.raises(CleanupRefused, match="legwork_incomplete"):
        record(env, command)
    assert env.store.state == before


@pytest.mark.parametrize(
    "change,error",
    [
        ({"version": 2}, "target_stale"),
        ({"end_class": "retired"}, "target_ended"),
        ({"kind": KIND_BELIEF}, "belief_retest"),
        ({"recheck_by": AT + timedelta(days=5)}, "not_due"),
    ],
)
def test_invalid_targets_refuse_before_effects(env, change, error):
    command = command_for(env)
    env.store.rows("Item")[0].update(change)
    before = copy.deepcopy(env.store.state)
    with pytest.raises((CleanupRefused, Refused), match=error):
        record(env, command)
    assert env.store.state == before


def test_unknown_target_and_invalid_run_refuse(env):
    command = command_for(env)
    env.store.rows("RunRecord")[0]["outcome"] = "cancelled"
    with pytest.raises(CleanupRefused, match="run_invalid"):
        record(env, command)
    env.store.rows("RunRecord")[0]["outcome"] = "running"
    env.store.rows("Item").clear()
    with pytest.raises(Refused, match="no item or link"):
        record(env, command)
    assert not env.store.rows("Verification")


def test_missing_authority_and_empty_ordinary_verify_remain_refused(env):
    command = command_for(env)
    with pytest.raises(CleanupRefused, match="authority_unavailable"):
        verify.record_no_source_deferral(env.store, WORLD, CALLER, command)
    with pytest.raises(CleanupRefused, match="authority_unavailable"):
        require_runtime()
    faults, indexed = shape_refusals(
        {
            "ops": [
                {
                    "op": "verify",
                    "op_id": "empty",
                    "claim": "claim",
                    "pieces": [],
                    "disposition": "deferred",
                    "reason": "no source",
                    "recheck_by": command.next_check.isoformat(),
                }
            ],
            "evidence": [],
        }
    )
    assert faults or indexed


@pytest.mark.parametrize(
    "person_only,destination,prerequisite",
    [
        (True, "question", "bound_question_plan"),
        (False, "retirement_proposal", "retirement_proposal_owner"),
    ],
)
def test_mixed_legacy_third_deferral_persists_one_pending_destination(
    env, person_only, destination, prerequisite
):
    seed_verification(env, "legacy-1")
    seed_verification(env, "legacy-2")
    # Reverse snapshot storage order: the ChangeEntry sequence is authoritative.
    env.store.rows("Verification").reverse()
    command = command_for(env, result="person_only" if person_only else "no_eligible_source")
    record(env, command)
    (escalation,) = env.store.rows("CuratorEscalation")
    assert (escalation["destination"], escalation["state"], escalation["prerequisite"]) == (
        destination,
        "pending",
        prerequisite,
    )
    assert escalation["streak_start"] == "legacy-1" and "destination_id" not in escalation
    assert not env.store.rows("Question") and not env.store.rows("Proposal")
    env.store = SyntheticInMemoryStore(env.store.state)
    record(env, command)
    env.timer.advance(world_id=WORLD, days=2)
    next_command = command_for(env, 2)
    record(env, next_command)
    assert env.store.rows("CuratorEscalation") == [escalation]
    assert (
        len(env.store.transaction(lambda tx: verify.verification_streak(tx, "item", "claim"))) == 4
    )


def test_streak_resets_only_on_real_resolution_and_deduplicates_markers(env):
    seed_verification(env, "defer")
    seed_verification(env, "confirm", "confirmed")
    seed_verification(env, "new-defer")
    marker = copy.deepcopy(env.store.rows("ChangeEntry")[-1])
    marker["sequence"] += 1
    env.store.rows("ChangeEntry").append(marker)
    streak = env.store.transaction(lambda tx: verify.verification_streak(tx, "item", "claim"))
    assert [r["id"] for r in streak] == ["new-defer"]


def test_recent_resolution_refuses_scored_before_any_write_and_records_open_finding(env):
    seed_verification(env, "resolution", "confirmed", "resolved-run")
    command = command_for(env)
    claim_before = copy.deepcopy(env.store.rows("Item")[0])
    before = copy.deepcopy(env.store.state)

    def scored(tx):
        run = apply._Run(
            tx, Stamp(WORLD, AT, CALLER.actor, "scored", "scored", run_id="run-1"), {}, "cleanup"
        )
        verify.verify_op(run, 0, {"claim": "claim", "disposition": "confirmed"})

    with pytest.raises(Refused, match="oscillation"):
        env.store.transaction(scored)
    assert env.store.state == before
    record(env, command)
    assert env.store.rows("Item")[0] == claim_before
    assert env.store.rows("QuestionWork")[0]["state"] == "pending"
    (finding,) = [
        json.loads(e["snapshot"])["finding"]
        for e in env.store.rows("QuestionEvent")
        if e["event"] == "oscillation"
    ]
    assert finding == {
        "resolution_id": "resolution",
        "resolution_run": "resolved-run",
        "flag_attempt": command.attempt_id,
        "flag_run": "run-1",
        "target": command.target.to_json(),
    }
    assert env.store.rows("Verification")[-1]["disposition"] == "deferred"


def test_accepted_run_window_uses_distinct_committed_order(env):
    seed_verification(env, "resolution", "confirmed", "resolved-run")
    for run_id in ("next-1", "next-1", "next-2"):
        env.store.transaction(lambda tx, run_id=run_id: verify.accepted_curator_run(tx, run_id, AT))
    assert env.store.transaction(lambda tx: verify._recent_resolution(tx, "item", "claim", "new"))
    env.store.transaction(lambda tx: verify.accepted_curator_run(tx, "next-3", AT))
    assert (
        env.store.transaction(lambda tx: verify._recent_resolution(tx, "item", "claim", "new"))
        is None
    )
    assert [r["ordinal"] for r in env.store.rows("CuratorRunOrder")] == [1, 2, 3, 4]


def test_link_deferral_and_snapshot_event_chain(env):
    env.store.add(
        "LINK",
        id="link-claim",
        meaning="state-meaning",
        active_key="active",
        version=1,
        recorded=AT,
        recheck_by=AT - timedelta(days=1),
        touched_at=AT,
    )
    command = command_for(env, kind="link")
    items = copy.deepcopy(env.store.rows("Item"))
    record(env, command)
    assert env.store.rows("Item") == items
    assert env.store.rows("LINK")[0]["version"] == 2
    reconstructed = {}
    for event in sorted(env.store.rows("QuestionEvent"), key=lambda e: e["sequence"]):
        payload = json.loads(event["snapshot"])
        key = (payload["label"], payload["after"]["id"])
        assert reconstructed.get(key) == payload["before"]
        reconstructed[key] = payload["after"]
    for (label, identity), row in reconstructed.items():
        assert row == json.loads(
            encode(next(r for r in env.store.rows(label) if r["id"] == identity))
        )


def test_next_check_is_explicit_future_time(env):
    command = command_for(env)
    with pytest.raises(CleanupRefused, match="recheck_not_future"):
        record(env, replace(command, next_check=AT))
    assert not env.store.rows("Verification")


def test_real_d3_replay_preserves_operational_json_and_truth_fields(env):
    kind = env.store.rows("Kind")[0]
    kind.update(name="Work", status_vocabulary=json.dumps([{"status": "open", "role": "open"}]))
    env.store.rows("Item")[0]["title"] = "Synthetic claim"

    def seed_history(tx):
        stamp = Stamp(WORLD, AT, "synthetic", "create", "create")
        for entity, table, row in (
            ("registry_row", "kinds", kind),
            ("item", "items", env.store.rows("Item")[0]),
        ):
            add_entry(
                tx,
                stamp,
                entity_type=entity,
                entity_id=row["id"],
                field="row",
                new=row_json(table, row, WORLD),
                world_time=None,
            )

    env.store.transaction(seed_history)
    command = replace(command_for(env), recheck_how="ask_the_person")
    record(env, command)
    projection = replay.rebuild(
        env.store.rows("ChangeEntry"), {v["id"]: v["value"] for v in env.store.rows("HistoryValue")}
    )
    rebuilt = projection.rows["items"]["claim"]
    actual = env.store.rows("Item")[0]
    for field in (
        "recheck_by",
        "recheck_how",
        "version",
        "recorded",
        "status",
        "last_confirmed",
        "confirmed_against",
        "became_true_precision",
    ):
        assert rebuilt.get(field) == actual.get(field), field


def test_scored_legacy_run_needs_no_run_record_and_persists_order(env, monkeypatch):
    # Only the already-validated piece acquisition is synthetic. The scored
    # arithmetic, capability resolver, band/refusal logic and writes are real.
    piece = verify.Piece(
        0,
        {
            "source_id": "source",
            "source_time": AT,
            "source_type": "reported",
            "derivation": "observed",
            "locator": "whole",
        },
        "web_pages",
        {},
        AT,
        None,
        {"id": "synthetic-judgment", "verdict": "not_about_claim"},
        None,
    )
    monkeypatch.setattr(verify, "_pieces", lambda *a, **kw: [piece])
    seed_verification(env, "legacy-1")
    seed_verification(env, "legacy-2")

    def scored(tx):
        stamp = Stamp(WORLD, AT, CALLER.actor, "scored", "scored", run_id="legacy-run")
        run = apply._Run(tx, stamp, {}, "cleanup")
        verify.verify_op(
            run,
            0,
            {
                "claim": "claim",
                "disposition": "deferred",
                "reason": "Synthetic scored deferral",
                "recheck_by": (AT + timedelta(days=1)).isoformat(),
            },
        )

    env.store.transaction(scored)
    row = env.store.rows("Verification")[-1]
    assert row["attempt_kind"] == "scored" and row["run_ordinal"] == 1
    assert row["commit_sequence"] > 0 and row["change_set_id"] == "scored"
    assert not env.store.rows("RunRecord")
    (escalation,) = env.store.rows("CuratorEscalation")
    assert escalation["state"] == "pending" and escalation["destination"] == "retirement_proposal"


def test_scored_confirmation_still_refuses_unconfirmable_piece(env, monkeypatch):
    piece = verify.Piece(
        0,
        {
            "source_id": "source",
            "source_time": AT,
            "source_type": "reported",
            "derivation": "observed",
            "locator": "whole",
        },
        "web_pages",
        {},
        AT,
        None,
        {"id": "synthetic-judgment", "verdict": "supports"},
        None,
    )
    monkeypatch.setattr(verify, "_pieces", lambda *a, **kw: [piece])

    def scored(tx):
        run = apply._Run(
            tx, Stamp(WORLD, AT, CALLER.actor, "scored", "scored", run_id="legacy"), {}, "cleanup"
        )
        verify.verify_op(run, 0, {"claim": "claim", "disposition": "confirmed"})

    before = copy.deepcopy(env.store.state)
    with pytest.raises(Refused, match="source type confirmed"):
        env.store.transaction(scored)
    assert env.store.state == before


@pytest.mark.parametrize(
    "field,value",
    [("work_id", "other"), ("result", "person_only"), ("target", "{}"), ("reason", "missing")],
)
def test_legwork_storage_binding_cannot_be_relabelled(env, field, value):
    command = command_for(env)
    env.store.rows("CuratorLegwork")[0][field] = value
    before = copy.deepcopy(env.store.state)
    with pytest.raises(CleanupRefused, match="binding_invalid"):
        record(env, command)
    assert env.store.state == before


def test_current_access_is_checked_inside_transaction_before_effects(env, monkeypatch):
    command = command_for(env)
    original = verify._checked_legwork

    def revoke(tx, command):
        found = original(tx, command)
        env.access.revoked = True
        return found

    monkeypatch.setattr(verify, "_checked_legwork", revoke)
    before = copy.deepcopy(env.store.state)
    with pytest.raises(CleanupRefused, match="synthetic_revoked"):
        record(env, command)
    assert env.store.state == before


def test_shared_seam_preflight_refuses_before_a_preceding_retirement(env):
    seed_verification(env, "resolution", "confirmed", "prior")
    calls = []

    def work(tx):
        stamp = Stamp(WORLD, AT, CALLER.actor, "new", "new", run_id="new", caller_kind="cleanup")
        verify.guard_scored_resolutions(
            tx,
            stamp,
            [
                {"op": "end_item", "op_id": "end", "item": "claim"},
                {"op": "verify", "op_id": "verify", "claim": "claim", "disposition": "retired"},
            ],
        )
        calls.append("graph mutation")

    with pytest.raises(BatchDeclined) as caught:
        env.store.transaction(work)
    assert not calls
    assert caught.value.refusals[0].rule == "curator_oscillation"


def test_cancelled_run_keeps_accepted_effect_and_replay_identity(env):
    command = command_for(env)
    identity = record(env, command)
    env.store.rows("RunRecord")[0]["outcome"] = "cancelled"
    assert record(env, command) == identity
    assert env.store.rows("CuratorRunOrder")[0]["ordinal"] == 1
