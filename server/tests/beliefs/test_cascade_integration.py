"""Actual D9 transitions through the published D6 adapter and D3 undo."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from grove import clock
from grove.writer import BatchDeclined, beliefs
from grove.writer.cascade import typed_changes
from tests.beliefs.test_event_provenance import event_row, event_run
from tests.beliefs.test_state_machine import CONNECTOR, attach, data, establish, judges, op, state
from tests.cascade.test_writer import impacts, links
from tests.writer.helpers import T0, apply, create_item, create_link, created_id, read, supports
from tests.writer.test_contract_d3 import undo


def linked(driver, world):
    belief, subject, sources, rows = establish(driver, world)
    child = created_id(
        apply(driver, world, [create_item("organisation", "Garden team", "e1")], [rows[0]])
    )
    receipt = apply(
        driver,
        world,
        [
            create_link(
                child,
                "depends on",
                belief,
                "e1",
                recheck_by=(clock.now(world) + timedelta(days=2)).isoformat(),
            )
        ],
        [rows[0]],
    )
    link = next(line.entity_id for line in receipt.lines if line.symbol == ">")
    return belief, child, link, sources, rows


def trigger(driver, world, change_set):
    return [
        json.loads(row["value"])
        for row in read(
            driver,
            world,
            "MATCH (c:ChangeEntry {change_set_id: $id, field: 'cascade_trigger'}) "
            "MATCH (v:HistoryValue {id: c.new_value_id}) RETURN v.value AS value",
            id=change_set,
        )
    ]


def test_inferred_planned_trigger_is_not_upgraded_by_the_adapter():
    run = event_run()
    beliefs._stage_change(
        run,
        "belief",
        "established",
        "superseded",
        [event_row("evidence", "episode", T0, source_type="planned", derivation="inferred")],
        None,
        cause="belief_end",
    )
    from grove.store.primitives import KIND_BELIEF

    run.tx = SimpleNamespace(run=lambda *args, **kwargs: [{"kind": KIND_BELIEF}])
    assert typed_changes(run)[0]["source_type"] == "planned"


def test_observed_rank_then_latest_time_selects_the_trigger_without_borrowing_inference():
    run = event_run()
    rows = [
        event_row("inferred", "one", T0 + timedelta(days=9), derivation="inferred"),
        event_row("earlier", "two", T0, source_type="said"),
        event_row("latest", "three", T0 + timedelta(days=1), source_type="said"),
    ]
    beliefs._stage_change(
        run, "belief", "tentative", "established", rows, None, cause="evidence_transition"
    )
    [event] = beliefs.cascade_changes(run)
    assert event["world_time"] == T0 + timedelta(days=1)
    assert event["original_source_type"] == "said"
    assert event["original_derivation"] == "observed"
    assert event["source_type"] == "said"


def test_weakening_marks_dependents_with_its_own_rank_time_and_provenance(driver, seeded):
    world = seeded["world_id"]
    belief, child, link, _, _ = linked(driver, world)
    _, contrary = data(driver, world, 4)
    receipt = attach(
        driver,
        world,
        belief,
        contrary,
        "contradicts",
        judgment_ids=judges(driver, world, belief, contrary),
    )
    assert state(driver, world, belief)["state"] == "weakening"
    [event] = trigger(driver, world, receipt.change_set_id)
    assert event["source_type"] == "said"
    assert event["world_time"] == (T0 + timedelta(hours=4)).isoformat()
    assert event["reason_id"] is not None
    assert event["evidence_ids"]
    assert links(driver, world)[link]["active_key"]
    marks = impacts(driver, world)
    assert {mark["affected_item"] for mark in marks} == {child}
    assert {mark["trigger_source_type"] for mark in marks} == {"said"}
    assert all(mark["state"] == "open" for mark in marks)


def test_historical_person_correction_ends_link_and_one_undo_restores_both(driver, seeded):
    world = seeded["world_id"]
    belief, child, link, _, _ = linked(driver, world)
    _, personal = data(driver, world, 6, connector="person_in_views")
    end = {
        "precision": "day",
        "earliest": (T0 + timedelta(hours=3)).isoformat(),
        "latest": (T0 + timedelta(days=1)).isoformat(),
    }
    receipt = apply(
        driver,
        world,
        [
            op(
                "correct_belief",
                target=belief,
                correction="not_any_more",
                stopped_being_true=end,
                evidence_refs=supports("e6"),
            )
        ],
        [personal],
    )
    assert state(driver, world, belief)["state"] == "superseded"
    held = links(driver, world)[link]
    assert not held.get("active_key")
    assert held["stopped_being_true_earliest"] == T0 + timedelta(hours=3)
    assert held["stopped_being_true_latest"] == T0 + timedelta(days=1)
    assert any(mark["affected_item"] == child for mark in impacts(driver, world))
    apply(driver, world, [undo(receipt.change_set_id)], [])
    assert state(driver, world, belief)["state"] == "established"
    assert links(driver, world)[link]["active_key"]
    assert {mark["state"] for mark in impacts(driver, world)} == {"undone"}


def test_refused_batch_rolls_back_belief_and_all_cascade_outputs(driver, seeded):
    world = seeded["world_id"]
    belief, _, link, _, _ = linked(driver, world)
    before_state, before_links = state(driver, world, belief), links(driver, world)
    before = read(driver, world, "MATCH (c:ChangeEntry) RETURN count(c) AS n")
    _, personal = data(driver, world, 6, connector="person_in_views")
    with pytest.raises(BatchDeclined):
        apply(
            driver,
            world,
            [
                op(
                    "correct_belief",
                    target=belief,
                    correction="wrong",
                    evidence_refs=supports("e6"),
                ),
                {
                    "op": "reopen_item",
                    "op_id": "bad",
                    "item": "missing",
                    "reason": "Invalid target",
                },
            ],
            [personal],
        )
    assert state(driver, world, belief) == before_state
    assert links(driver, world) == before_links
    assert read(driver, world, "MATCH (c:ChangeEntry) RETURN count(c) AS n") == before
    assert impacts(driver, world) == []


def test_belief_trigger_cannot_cross_worlds(driver, seeded, make_world):
    world = seeded["world_id"]
    belief, _, _, _, _ = linked(driver, world)
    other = make_world()
    from grove.store.query import WorldQuery

    run = event_run()
    run.cache["belief_cascade_changes"] = [{"item_id": belief, "change_set_id": "change"}]
    run.tx = SimpleNamespace(run=WorldQuery(driver, other).read)
    with pytest.raises(ValueError, match="this world"):
        typed_changes(run)


def test_inferred_supersession_proposes_link_ending_and_undo_restores_claim(driver, seeded):
    world = seeded["world_id"]
    belief, child, link, _, rows = linked(driver, world)
    successor = created_id(
        apply(
            driver,
            world,
            [
                op(
                    "mint_belief",
                    title="The team has a quiet room",
                    subject_refs=[child],
                    evidence_refs=supports("e1", "e2"),
                )
            ],
            rows,
            caller=CONNECTOR,
        ),
        entity_type="belief",
    )
    _, fresh = data(driver, world, 7)
    receipt = apply(
        driver,
        world,
        [
            op(
                "supersede_belief",
                target=belief,
                successor=successor,
                end_class="replaced",
                evidence_refs=supports("e7"),
            )
        ],
        [fresh],
        caller=CONNECTOR,
    )
    assert state(driver, world, belief)["successor"] == successor
    assert links(driver, world)[link]["active_key"]
    [proposal] = [
        row["p"]
        for row in read(driver, world, "MATCH (p:CascadeProposal {rule: 'R1'}) RETURN p")
        if json.loads(row["p"]["detail"])["trigger_change_set"] == receipt.change_set_id
    ]
    assert proposal["target_item"] == belief
    assert json.loads(proposal["detail"])["link_ids"] == [link]
    apply(driver, world, [undo(receipt.change_set_id)], [])
    assert state(driver, world, belief)["state"] == "established"
    assert not state(driver, world, belief).get("successor")
    assert links(driver, world)[link]["active_key"]
    from tests.belief_history.test_operations import check_restore

    check_restore(driver, world)
