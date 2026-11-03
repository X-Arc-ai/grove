"""The API side of a pass: grants, one pass per world, the kill and replay rule, and the cycle
order (the Curator after a Learner pass that changed items, the Connector on Appendix C)."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from grove.models.config import load_catalog
from grove.models.gateway_auth import verify as verify_gateway
from grove.runner.passes import (
    CONNECTOR_CHANGED_ITEMS,
    PassBusy,
    caller_for,
    connector_due,
    due_work,
    end_pass,
    run_record,
    start_pass,
)
from grove.runner.tokens import verify_pass_token
from grove.store.query import WorldQuery
from grove.writer import apply_request, request_state
from grove.writer.testing import plant_node
from tests.runner.helpers import FAST, new_auth, queue_store


@pytest.fixture
def auth():
    return new_auth()[0]


def learner_batch(driver, world, grant, title="Lina Okafor", n=0):
    from grove.runner.brain import pass_input

    form = pass_input(driver, world, run_record(driver, world, grant.run_id))["batch_form"]
    batch = form["batch"]
    batch["ops"] = [batch["ops"][0] | {"title": title, "op_id": f"o{n}"}]
    return form["request_id"], batch


def items(driver, world, title):
    return WorldQuery(driver, world).read("MATCH (i:Item {title: $t}) RETURN i.id AS id", t=title)


def test_a_queued_store_is_granted_to_a_learner_pass(driver, seeded, auth, frozen):
    request_id = queue_store(driver, seeded)
    assert {"world_id": seeded, "agent": "learner"} in due_work(driver, settings=FAST)
    grant = start_pass(driver, auth, seeded, settings=FAST)
    assert grant.agent == "learner" and grant.input_ids == [request_id]
    row = WorldQuery(driver, seeded).read(
        "MATCH (r:Request {id: $id}) RETURN r.status AS s, r.claimed_by AS c, r.lease_until AS l",
        id=request_id,
    )[0]
    assert row["s"] == "applying" and row["c"] == grant.run_id
    assert row["l"] == frozen.now() + timedelta(seconds=FAST.lease_seconds)
    # D10: the episode is written before any model call.
    source = request_state(driver, seeded, request_id).source_id
    assert WorldQuery(driver, seeded).read(
        "MATCH (e:Episode) WHERE $s IN e.source_ids RETURN e.id AS id", s=source
    )
    claims = verify_pass_token(auth.gateway_secret, grant.pass_token)
    assert (claims.world_id, claims.run_id, claims.agent) == (seeded, grant.run_id, "learner")
    gateway = verify_gateway(auth.gateway_secret, load_catalog(), grant.gateway_token)
    assert (gateway.world_id, gateway.run_id, gateway.job) == (seeded, grant.run_id, "update_pass")
    assert gateway.token_cap == FAST.token_cap
    record = run_record(driver, seeded, grant.run_id)
    assert record["outcome"] == "running" and record["input_ids"] == [request_id]
    assert record["time_cap"] == FAST.time_cap_seconds and record["token_cap"] == FAST.token_cap


def test_one_pass_at_a_time_per_world(driver, seeded, auth, frozen):
    one = replace(FAST, learner_requests=1)
    queue_store(driver, seeded)
    second = queue_store(driver, seeded, "Remember: Tomas Reyes joins the Elm Street fair.")
    start_pass(driver, auth, seeded, settings=one)
    assert request_state(driver, seeded, second).status == "queued"
    with pytest.raises(PassBusy):
        start_pass(driver, auth, seeded, settings=one)
    assert all(row["world_id"] != seeded for row in due_work(driver, settings=one))


def test_nothing_due_gives_no_grant(driver, seeded, auth, frozen):
    assert start_pass(driver, auth, seeded, settings=FAST) is None
    assert start_pass(driver, auth, seeded, agent="learner", settings=FAST) is None


def test_a_killed_pass_leaves_its_request_queued_and_the_next_pass_applies_it_once(
    driver, seeded, auth, frozen
):
    request_id = queue_store(driver, seeded)
    first = start_pass(driver, auth, seeded, settings=FAST)
    end_pass(driver, seeded, first.run_id, outcome="killed")
    state = request_state(driver, seeded, request_id)
    assert state.status == "failed" and state.retry_at is not None
    assert state.api_state == "saving" and state.last_error_code == "pass_killed"
    assert run_record(driver, seeded, first.run_id)["outcome"] == "killed"
    frozen.advance(seconds=31)
    second = start_pass(driver, auth, seeded, settings=FAST)
    assert second.input_ids == [request_id] and second.run_id != first.run_id
    key, batch = learner_batch(driver, seeded, second)
    saved = apply_request(driver, seeded, caller_for("learner"), key, batch, run_id=second.run_id)
    assert saved.api_state == "saved"
    end_pass(driver, seeded, second.run_id, outcome="ok")
    # A third delivery of the same batch changes nothing.
    again = apply_request(driver, seeded, caller_for("learner"), key, batch, run_id="late")
    assert again.api_state == "saved"
    assert len(items(driver, seeded, "Lina Okafor")) == 1
    # Nothing is left for a Learner (the Curator's job on the new items is the next pass).
    assert start_pass(driver, auth, seeded, agent="learner", settings=FAST) is None


def test_a_pass_that_dies_with_no_end_comes_back_through_its_lease(driver, seeded, auth, frozen):
    request_id = queue_store(driver, seeded)
    first = start_pass(driver, auth, seeded, settings=FAST)
    frozen.advance(seconds=FAST.lease_seconds - 1)
    assert all(row["world_id"] != seeded for row in due_work(driver, settings=FAST))
    frozen.advance(seconds=2)
    assert {"world_id": seeded, "agent": "learner"} in due_work(driver, settings=FAST)
    second = start_pass(driver, auth, seeded, settings=FAST)
    assert second.input_ids == [request_id]
    assert run_record(driver, seeded, first.run_id)["outcome"] == "abandoned"
    key, batch = learner_batch(driver, seeded, second)
    apply_request(driver, seeded, caller_for("learner"), key, batch, run_id=second.run_id)
    end_pass(driver, seeded, second.run_id, outcome="ok")
    assert request_state(driver, seeded, request_id).api_state == "saved"
    assert len(items(driver, seeded, "Lina Okafor")) == 1


def test_a_pass_that_committed_before_it_died_is_not_applied_again(driver, seeded, auth, frozen):
    request_id = queue_store(driver, seeded)
    first = start_pass(driver, auth, seeded, settings=FAST)
    key, batch = learner_batch(driver, seeded, first)
    apply_request(driver, seeded, caller_for("learner"), key, batch, run_id=first.run_id)
    frozen.advance(seconds=FAST.lease_seconds + 1)
    assert start_pass(driver, auth, seeded, agent="learner", settings=FAST) is None
    assert request_state(driver, seeded, request_id).api_state == "saved"
    assert len(items(driver, seeded, "Lina Okafor")) == 1


def test_an_ok_pass_that_settled_nothing_hands_its_request_back(driver, seeded, auth, frozen):
    request_id = queue_store(driver, seeded)
    grant = start_pass(driver, auth, seeded, settings=FAST)
    end_pass(driver, seeded, grant.run_id, outcome="ok")
    state = request_state(driver, seeded, request_id)
    assert state.status == "failed" and state.last_error_code == "not_proposed"
    assert state.retry_at is not None


def test_ending_a_pass_twice_changes_nothing(driver, seeded, auth, frozen):
    request_id = queue_store(driver, seeded)
    grant = start_pass(driver, auth, seeded, settings=FAST)
    end_pass(driver, seeded, grant.run_id, outcome="killed")
    attempts = request_state(driver, seeded, request_id).attempts
    assert end_pass(driver, seeded, grant.run_id, outcome="ok")["outcome"] == "killed"
    assert request_state(driver, seeded, request_id).attempts == attempts


def test_a_learner_pass_that_changed_items_queues_the_curator_on_them(driver, seeded, auth, frozen):
    queue_store(driver, seeded)
    grant = start_pass(driver, auth, seeded, settings=FAST)
    key, batch = learner_batch(driver, seeded, grant)
    apply_request(driver, seeded, caller_for("learner"), key, batch, run_id=grant.run_id)
    end_pass(driver, seeded, grant.run_id, outcome="ok")
    item_id = items(driver, seeded, "Lina Okafor")[0]["id"]
    assert {"world_id": seeded, "agent": "curator"} in due_work(driver, settings=FAST)
    curator = start_pass(driver, auth, seeded, settings=FAST)
    assert curator.agent == "curator" and curator.input_ids == [item_id]
    end_pass(driver, seeded, curator.run_id, outcome="ok")
    jobs = WorldQuery(driver, seeded).read(
        "MATCH (j:Job) RETURN j.state AS s, j.agent_or_job_type AS a"
    )
    assert {"s": "done", "a": "curator"} in jobs


def test_a_learner_pass_that_changed_nothing_queues_no_curator(driver, seeded, auth, frozen):
    queue_store(driver, seeded)
    grant = start_pass(driver, auth, seeded, settings=FAST)
    end_pass(driver, seeded, grant.run_id, outcome="killed")
    assert not WorldQuery(driver, seeded).read("MATCH (j:Job) RETURN j.id AS id")


def test_ten_changed_items_queue_the_connector_and_one_does_not(driver, seeded, auth, frozen):
    for n in range(CONNECTOR_CHANGED_ITEMS):
        queue_store(driver, seeded, f"Remember: invented person number {n} joins the fair.")
    # Ten invented names far apart, so no title meets the near-duplicate gate.
    names = [
        "Lina Okafor",
        "Tomas Reyes",
        "Ines Varga",
        "Kofi Mensah",
        "Aiko Tanaka",
        "Ravi Patel",
        "Marta Sousa",
        "Olu Adeyemi",
        "Sven Lund",
        "Noor Haddad",
    ]
    grant = start_pass(driver, auth, seeded, settings=FAST)
    key, batch = learner_batch(driver, seeded, grant, title=names[0])
    apply_request(driver, seeded, caller_for("learner"), key, batch, run_id=grant.run_id)
    assert not connector_due(driver, seeded)
    end_pass(driver, seeded, grant.run_id, outcome="ok")
    assert not WorldQuery(driver, seeded).read(
        "MATCH (j:Job {agent_or_job_type: 'connector'}) RETURN j.id AS id"
    )
    done = 1
    while done < CONNECTOR_CHANGED_ITEMS:
        frozen.advance(seconds=31)
        grant = start_pass(driver, auth, seeded, agent="learner", settings=FAST)
        from grove.runner.brain import pass_input

        packet = pass_input(driver, seeded, run_record(driver, seeded, grant.run_id))
        for entry in packet["requests"]:
            form = pass_input(driver, seeded, run_record(driver, seeded, grant.run_id))
            batch = json.loads(json.dumps(form["batch_form"]["batch"]))
            batch["evidence"][0] |= {
                "source_id": entry["source_id"],
                "episode_id": entry["episode_id"],
            }
            batch["ops"] = [batch["ops"][0] | {"title": names[done]}]
            saved = apply_request(
                driver,
                seeded,
                caller_for("learner"),
                entry["request_id"],
                batch,
                run_id=grant.run_id,
            )
            assert saved.api_state == "saved", saved.to_api()
            done += 1
            if done == CONNECTOR_CHANGED_ITEMS:
                break
        end_pass(driver, seeded, grant.run_id, outcome="ok")
    jobs = WorldQuery(driver, seeded).read(
        "MATCH (j:Job {agent_or_job_type: 'connector'}) RETURN j.state AS s"
    )
    assert jobs == [{"s": "queued"}]


def test_a_new_contradiction_queues_the_connector(driver, seeded, auth, frozen):
    assert not connector_due(driver, seeded)
    plant_node(
        driver,
        seeded,
        "Contradiction",
        id="01a19fe6-3010-7fa3-998c-fa62730b1bf9",
        detected_at=frozen.now(),
        status="open",
    )
    assert connector_due(driver, seeded)


def test_the_learner_comes_before_a_due_job(driver, seeded, auth, frozen):
    from grove.writer import runs

    runs.enqueue_job(
        driver, seeded, agent="curator", trigger="after_update", input_references=["x"], priority=20
    )
    queue_store(driver, seeded)
    rows = [row for row in due_work(driver, settings=FAST) if row["world_id"] == seeded]
    assert rows == [{"world_id": seeded, "agent": "learner"}]
    assert start_pass(driver, auth, seeded, settings=FAST).agent == "learner"


def test_a_world_at_its_budget_ceiling_starts_no_pass_and_its_request_stays_queued(
    driver, seeded, auth, frozen
):
    request_id = queue_store(driver, seeded)
    day = frozen.now().date()
    plant_node(driver, seeded, "Budget", day=day, ceiling=1.0, spent=1.0, state="reached")
    assert all(row["world_id"] != seeded for row in due_work(driver, settings=FAST))
    state = request_state(driver, seeded, request_id)
    assert state.status == "queued" and state.attempts == 0
    frozen.advance(days=1)
    assert {"world_id": seeded, "agent": "learner"} in due_work(driver, settings=FAST)
