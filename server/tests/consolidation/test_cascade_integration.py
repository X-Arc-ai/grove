"""D12 uses the accepted planner, stores decisions, and rejects omitted work."""

import json
from types import SimpleNamespace

import pytest

from grove.consolidation import packet, schema
from grove.ids import new_id
from grove.runner.brain import pass_input
from grove.runner.passes import end_pass, run_record
from grove.store.query import WorldQuery
from grove.writer.consolidation import review_cascade
from grove.writer.errors import Refused
from grove.writer.testing import advance_version, plant_link, plant_node
from tests.cascade.test_decisions import proposal
from tests.consolidation.test_connector import bearer, form, grant, item
from tests.writer.helpers import make_source


def graph(driver, world, frozen, *, carries=False):
    root = item(driver, world, kind="synthetic")
    child = item(driver, world, kind="synthetic")
    meaning = plant_node(
        driver,
        world,
        "Meaning",
        name="requires a reliable basis",
        carries_consequences=carries,
        downstream_end="from",
        decomposition=False,
        status="canonical",
    )
    link = plant_link(
        driver,
        world,
        child["id"],
        root["id"],
        meaning=meaning["id"],
        active_key=new_id(world),
        version=1,
    )
    value = plant_node(driver, world, "HistoryValue", value=json.dumps("The basis changed."))
    plant_node(
        driver,
        world,
        "ChangeEntry",
        entity_type="item",
        entity_id=root["id"],
        field="title",
        new_value_id=value["id"],
        sequence=100001,
        recorded_at=frozen.now(),
        world_time=frozen.now(),
        history_only=False,
        change_set_id="synthetic-cause",
        actor="agent:learner",
    )
    source = make_source(driver, world, "web_pages", ["The dependency still holds."])
    return root, child, link, source


def review(row, source, **fields):
    return {
        "op": "review_cascade",
        "op_id": "review",
        "trigger": row["trigger"],
        "target": row["target"],
        "cause_change_id": row["cause_change_id"],
        "review_token": row["review_token"],
        "disposition": "still_holding",
        "checked_source_ids": [source["source_id"]],
        "reason": "The checked source still supports this dependency.",
        "gap_reason": "The dependency had no recorded consequence check.",
        **fields,
    }


def submit(rig, live, doc):
    return rig[0].post("/v1/internal/brain/propose", json={"batch": doc}, headers=bearer(live))


@pytest.mark.parametrize("carries", [False, True])
def test_safety_net_requires_review_and_retains_gap_without_changing_claim(
    driver, seeded, frozen, rig, carries
):
    root, child, link, source = graph(driver, seeded, frozen, carries=carries)
    live = grant(driver, seeded, rig)
    body = pass_input(driver, seeded, run_record(driver, seeded, live.run_id))
    [row] = body["cascade_reviews"]
    assert row["target"] == child["id"]
    assert row["path"] == [link["id"]]
    assert row["flag_gap"] is (not carries)
    assert row["missing_mark"] is carries
    doc = form(driver, seeded, live)
    assert submit(rig, live, doc).status_code == 422
    doc["cascade_reviews"] = [review(row, source)]
    response = submit(rig, live, doc)
    assert response.status_code == 200, response.text
    query = WorldQuery(driver, seeded)
    history = query.read(
        "MATCH (c:ChangeEntry {field: 'cascade_review'}) "
        "MATCH (v:HistoryValue {id: c.new_value_id}) RETURN v.value AS value"
    )
    [stored] = [json.loads(entry["value"]) for entry in history]
    assert stored["gap_reason_id"]
    assert stored["disposition"] == "still_holding"
    assert (
        query.read("MATCH (i:Item {id: $id}) RETURN i.title AS title", id=child["id"])[0]["title"]
        == child["title"]
    )
    marks = query.read("MATCH (m:Impact) RETURN m.affected_item AS target")
    assert marks == ([{"target": child["id"]}] if carries else [])
    assert packet.work(driver, seeded)["cascade_reviews"] == []
    assert end_pass(driver, seeded, live.run_id, outcome="ok")["outcome"] == "ok"


def test_missing_gap_reason_rolls_back_the_disposition(driver, seeded, frozen, rig):
    _, _, _, source = graph(driver, seeded, frozen)
    live = grant(driver, seeded, rig)
    doc = form(driver, seeded, live)
    row = packet.work(driver, seeded)["cascade_reviews"][0]
    operation = review(row, source)
    operation.pop("gap_reason")
    doc["cascade_reviews"] = [operation]
    response = submit(rig, live, doc)
    assert response.status_code == 422, response.text
    assert response.json()["refusals"][0]["rule"] == "cascade_gap_reason"
    assert (
        WorldQuery(driver, seeded).read("MATCH (c:ChangeEntry {field: 'cascade_review'}) RETURN c")
        == []
    )
    assert end_pass(driver, seeded, live.run_id, outcome="ok")["outcome"] == "failed"


@pytest.mark.parametrize("decision", ["accept_proposal", "reject_proposal", "question_proposal"])
def test_connector_handles_stored_proposals_through_the_real_api(
    driver, seeded, frozen, rig, decision
):
    target = item(driver, seeded, kind="synthetic")
    proposed = proposal(driver, seeded, target["id"], action="review")
    source = make_source(driver, seeded, "web_pages", ["Two outcomes remain possible."])
    live = grant(driver, seeded, rig)
    body = pass_input(driver, seeded, run_record(driver, seeded, live.run_id))
    assert body["cascade_proposals"][0]["proposal_id"] == proposed
    doc = form(driver, seeded, live)
    operation = {
        "op": decision,
        "op_id": "decide",
        "proposal": proposed,
        "reason": "The retained source supports this decision.",
    }
    if decision == "question_proposal":
        operation.update(
            question="Which dependency should remain?",
            options=["Retain the dependency.", "Remove the dependency."],
            checked_source_ids=[source["source_id"]],
        )
    doc["operations"] = [operation]
    response = submit(rig, live, doc)
    assert response.status_code == 200, response.text
    held = WorldQuery(driver, seeded).read(
        "MATCH (p:CascadeProposal {id: $id}) RETURN p", id=proposed
    )[0]["p"]
    assert (
        held["state"]
        == {
            "accept_proposal": "accepted",
            "reject_proposal": "rejected",
            "question_proposal": "open",
        }[decision]
    )
    if decision == "question_proposal":
        assert json.loads(held["detail"])["question_id"]
    assert packet.work(driver, seeded)["cascade_proposals"] == []
    assert end_pass(driver, seeded, live.run_id, outcome="ok")["outcome"] == "ok"


def test_dependency_added_after_translation_refuses_atomic_commit(
    driver, seeded, frozen, rig, monkeypatch
):
    root, _, link, source = graph(driver, seeded, frozen)
    live = grant(driver, seeded, rig)
    doc = form(driver, seeded, live)
    doc["cascade_reviews"] = [review(packet.work(driver, seeded)["cascade_reviews"][0], source)]
    original = schema.translate

    def racing(*args):
        translated = original(*args)
        child = item(driver, seeded, kind="synthetic")
        plant_link(
            driver,
            seeded,
            child["id"],
            root["id"],
            meaning=link["meaning"],
            active_key=new_id(seeded),
        )
        return translated

    monkeypatch.setattr(schema, "translate", racing)
    response = submit(rig, live, doc)
    assert response.status_code == 422, response.text
    assert response.json()["refusals"][0]["rule"] == "consolidation_stale"
    assert (
        WorldQuery(driver, seeded).read("MATCH (c:ChangeEntry {field: 'cascade_review'}) RETURN c")
        == []
    )


def test_safety_net_cannot_cite_another_world_source(driver, seeded, frozen, rig, make_world):
    graph(driver, seeded, frozen)
    from tests.runner.helpers import seeded_world

    other = seeded_world(driver, make_world())
    source = make_source(driver, other, "web_pages", ["Unrelated private source."])
    live = grant(driver, seeded, rig)
    doc = form(driver, seeded, live)
    doc["cascade_reviews"] = [review(packet.work(driver, seeded)["cascade_reviews"][0], source)]
    response = submit(rig, live, doc)
    assert response.status_code == 422, response.text
    assert response.json()["refusals"][0]["rule"] == "cascade_review_sources"


@pytest.mark.parametrize("changed", ["item", "link"])
def test_dependency_version_change_after_translation_refuses_commit(
    driver, seeded, frozen, rig, monkeypatch, changed
):
    _, child, link, source = graph(driver, seeded, frozen)
    live = grant(driver, seeded, rig)
    doc = form(driver, seeded, live)
    doc["cascade_reviews"] = [review(packet.work(driver, seeded)["cascade_reviews"][0], source)]
    original = schema.translate

    def racing(*args):
        translated = original(*args)
        advance_version(driver, seeded, changed, child["id"] if changed == "item" else link["id"])
        return translated

    monkeypatch.setattr(schema, "translate", racing)
    response = submit(rig, live, doc)
    assert response.status_code == 422, response.text
    assert response.json()["refusals"][0]["rule"] == "consolidation_stale"
    assert (
        WorldQuery(driver, seeded).read("MATCH (c:ChangeEntry {field: 'cascade_review'}) RETURN c")
        == []
    )


def test_revised_disposition_refuses_a_noop_operation():
    row = {"trigger": "root", "target": "child", "cause_change_id": 1, "review_token": "held"}
    operation = {"op": "update_item", "op_id": "noop", "item": "child"}
    run = SimpleNamespace(
        tx=SimpleNamespace(run=lambda *args, **kwargs: [{"id": "source"}]),
        cache={"cascade_review_work": {("root", "child", 1): row}, "done_ops": {"noop": operation}},
        cascade_entries=[],
    )
    with pytest.raises(Refused, match="committed revision"):
        review_cascade(
            run,
            0,
            review(row, {"source_id": "source"}, disposition="revised", operation_ids=["noop"]),
        )


@pytest.mark.parametrize("changed", ["item", "link"])
def test_dependency_change_after_packet_before_submit_refuses_commit(
    driver, seeded, frozen, rig, changed
):
    _, child, link, source = graph(driver, seeded, frozen)
    live = grant(driver, seeded, rig)
    doc = form(driver, seeded, live)
    doc["cascade_reviews"] = [review(packet.work(driver, seeded)["cascade_reviews"][0], source)]
    advance_version(driver, seeded, changed, child["id"] if changed == "item" else link["id"])
    response = submit(rig, live, doc)
    assert response.status_code == 422, response.text
    assert response.json()["refusals"][0]["rule"] == "consolidation_stale"
    assert (
        WorldQuery(driver, seeded).read("MATCH (c:ChangeEntry {field: 'cascade_review'}) RETURN c")
        == []
    )
