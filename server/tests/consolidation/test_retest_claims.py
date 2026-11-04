"""One authorized source read checks the belief and a nearby ordinary claim."""

import json
from datetime import timedelta

import httpx
import pytest

from grove.runner.passes import end_pass, start_pass
from grove.screening import Screener, ScreeningConfig
from grove.store.primitives import MEANING_DESCRIBES
from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node
from tests.belief_tools.test_tools import layer, verdict
from tests.consolidation.test_connector import bearer, belief, form, grant
from tests.models.test_judge import Provider, glimmer, spark, verdicts
from tests.runner.helpers import queue_store
from tests.writer.helpers import make_source


def scenario(driver, world, frozen, rig, count=3):
    url = "https://status.example.test/queues"
    connector = plant_node(driver, world, "Connector", type="web_pages", scope=json.dumps([url]))
    target, _, _ = belief(
        driver,
        world,
        frozen,
        due=True,
        retest_how=json.dumps({"connector": connector["id"], "locator": url}),
    )
    kind = plant_node(
        driver,
        world,
        "Kind",
        name="service capacity",
        lifecycle_class="lasting_thing",
        status="canonical",
        status_vocabulary="[]",
        required_fields="[]",
        required_links="[]",
    )
    claim = plant_node(
        driver,
        world,
        "Item",
        kind=kind["id"],
        title="The service has 3 queues",
        version=1,
        recorded=frozen.now() - timedelta(days=10),
        last_confirmed=frozen.now() - timedelta(days=9),
        recheck_by=frozen.now() - timedelta(days=1),
    )
    plant_link(
        driver,
        world,
        target["id"],
        claim["id"],
        meaning=MEANING_DESCRIBES,
        active_key=f"capacity:{claim['id']}",
        version=1,
    )
    source_text = f"The service answers promptly. The service has {count} queues. <source-note>"
    reads = []

    def read(request):
        reads.append(request)
        return httpx.Response(200, text=source_text)

    rig[0].app.state.belief_read_dependencies = {
        "screener": Screener(ScreeningConfig()),
        "resolver": lambda *_: ["93.184.215.14"],
        "transport": httpx.MockTransport(read),
    }
    return target, claim, source_text, reads


@pytest.mark.parametrize("count", [3, 5])
def test_retest_refreshes_a_stale_count_one_link_away(driver, seeded, frozen, rig, count):
    client = rig[0]
    target, claim, source_text, reads = scenario(driver, seeded, frozen, rig, count)
    ordinary_verdict = "supports" if count == 3 else "contradicts"
    provider = Provider(
        spark(verdict("supports")),
        glimmer(verdict("supports")),
        spark(verdicts((2, ordinary_verdict, 0.9))),
        glimmer(verdicts((2, ordinary_verdict, 0.9))),
        spark(verdict("supports")),
        glimmer(verdict("supports")),
    )
    client.app.state.belief_model_factory = lambda store: layer(store, provider)
    live = grant(driver, seeded, rig)
    response = client.post(
        "/v1/internal/brain/retest", json={"belief_id": target["id"]}, headers=bearer(live)
    )
    assert response.status_code == 200, response.text
    retest = response.json()
    assert "The service has" in retest["read"]["data"]
    assert "&lt;source-note&gt;" in retest["read"]["data"]
    assert source_text not in json.dumps({k: v for k, v in retest["read"].items() if k != "data"})
    response = client.post(
        "/v1/internal/brain/judge",
        json={
            "mode": "claim",
            "claim_id": claim["id"],
            "source_id": retest["read"]["source_id"],
            "locator": "whole",
        },
        headers=bearer(live),
    )
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["verdicts"] == [ordinary_verdict, ordinary_verdict]
    assert len(reads) == 1
    observation = retest["read"]["observation"]
    horizon = frozen.now() + timedelta(days=7)
    doc = form(driver, seeded, live)
    doc["evidence"] = [observation]
    if count != 3:
        doc["operations"] = [
            {
                "op": "update_item",
                "op_id": "refresh-count",
                "item": claim["id"],
                "version": 1,
                "fields": {"title": f"The service has {count} queues"},
                "evidence_refs": [{"ev_id": observation["ev_id"], "polarity": "supports"}],
                "reason": "The fetched service page states the revised queue count.",
            }
        ]
    doc["dispositions"] = [
        {
            "op": "retest_belief",
            "op_id": "retest-service",
            "target": target["id"],
            "pieces": [{"ev_id": observation["ev_id"], "judgment_id": retest["judgment_ids"][0]}],
            "second_judge": [retest["judgment_ids"][1]],
            "recheck_by": horizon.isoformat(),
            "reason": "The service page supports the recurring service claim.",
        },
        {
            "op": "verify",
            "op_id": "check-count",
            "claim": claim["id"],
            "disposition": "confirmed" if count == 3 else "moved",
            "pieces": [{"ev_id": observation["ev_id"], "judgment_id": checked["judgment_ids"][0]}],
            "second_judge": [checked["judgment_ids"][1]],
            "recheck_by": horizon.isoformat(),
            "reason": "The same live page checks the nearby queue count.",
        },
    ]
    response = client.post("/v1/internal/brain/propose", json={"batch": doc}, headers=bearer(live))
    assert response.status_code == 200, response.text
    query = WorldQuery(driver, seeded)
    stored = query.read("MATCH (i:Item {id: $id}) RETURN i", id=claim["id"])[0]["i"]
    assert stored["title"] == f"The service has {count} queues"
    assert stored["last_confirmed"] == (frozen.now() if count == 3 else claim["last_confirmed"])
    checks = query.read("MATCH (v:Verification {claim_id: $id}) RETURN v", id=claim["id"])
    assert len(checks) == 1
    assert checks[0]["v"]["disposition"] == ("confirmed" if count == 3 else "moved")
    # Checking or changing a subject queues its belief for a fresh support judgment.
    # Complete that new queue with the same source, without another network read.
    frozen.advance(seconds=1)
    packet = client.get("/v1/internal/brain/pass", headers=bearer(live)).json()
    assert packet["support_changed"]
    response = client.post(
        "/v1/internal/brain/judge",
        headers=bearer(live),
        json={
            "mode": "evidence",
            "belief_id": target["id"],
            "source_id": retest["read"]["source_id"],
            "locator": "whole",
        },
    )
    assert response.status_code == 200, response.text
    followup = packet["batch_form"]["batch"]
    followup["support_changes"] = [
        {
            "op": "resolve_support_change",
            "op_id": "settle-count-support",
            **row,
            "disposition": "still_holding",
            "judgment_ids": response.json()["judgment_ids"],
            "reason": "The new count check leaves the recurring service claim intact.",
        }
        for row in packet["support_changed"]
    ]
    response = client.post(
        "/v1/internal/brain/propose",
        json={"batch": followup},
        headers=bearer(live),
    )
    assert response.status_code == 200, response.text
    assert len(reads) == 1
    assert end_pass(driver, seeded, live.run_id, outcome="ok")["outcome"] == "ok"


def test_ordinary_claim_judge_is_available_without_a_second_fetch(driver, seeded, frozen, rig):
    client = rig[0]
    target, claim, _, reads = scenario(driver, seeded, frozen, rig)
    provider = Provider(
        spark(verdict("supports")),
        glimmer(verdict("supports")),
        spark(verdicts((2, "supports", 0.9))),
        glimmer(verdicts((2, "supports", 0.9))),
    )
    client.app.state.belief_model_factory = lambda store: layer(store, provider)
    live = grant(driver, seeded, rig)
    retest = client.post(
        "/v1/internal/brain/retest", json={"belief_id": target["id"]}, headers=bearer(live)
    ).json()
    response = client.post(
        "/v1/internal/brain/judge",
        headers=bearer(live),
        json={
            "mode": "claim",
            "claim_id": claim["id"],
            "source_id": retest["read"]["source_id"],
            "locator": "whole",
        },
    )
    assert response.status_code == 200, response.text
    assert len(reads) == 1


def test_claim_judge_rejects_forged_and_other_world_references_before_model_io(
    driver,
    seeded,
    frozen,
    rig,
    make_world,
):
    client = rig[0]
    target, claim, _, _ = scenario(driver, seeded, frozen, rig)
    provider = Provider(spark(verdict("supports")), glimmer(verdict("supports")))
    client.app.state.belief_model_factory = lambda store: layer(store, provider)
    live = grant(driver, seeded, rig)
    retest = client.post(
        "/v1/internal/brain/retest", json={"belief_id": target["id"]}, headers=bearer(live)
    ).json()
    body = {
        "mode": "claim",
        "claim_id": claim["id"],
        "source_id": retest["read"]["source_id"],
        "locator": "whole",
    }
    foreign = plant_node(driver, make_world(), "Item", title="A foreign count", version=1)
    old = make_source(driver, seeded, "web_pages", ["An older page with the same count."])
    untouched = Provider()
    client.app.state.belief_model_factory = lambda store: layer(store, untouched)
    for changes in (
        {"claim_id": foreign["id"]},
        {"source_id": old["source_id"]},
        {"claim_id": target["id"]},
        {"claim_id": "x" * 1025},
        {"source_text": "A fabricated count"},
        {"verdict": "supports"},
        {"load_bearing": True},
        {"world_id": foreign["world_id"]},
        {"locator": "key:ret"},
    ):
        response = client.post(
            "/v1/internal/brain/judge", json=body | changes, headers=bearer(live)
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_arguments"
    assert untouched.requests == []

    # A source read by an earlier pass does not grant authority to a later pass.
    assert end_pass(driver, seeded, live.run_id, outcome="failed")["outcome"] == "failed"
    frozen.advance(seconds=61)
    later = grant(driver, seeded, rig)
    response = client.post("/v1/internal/brain/judge", json=body, headers=bearer(later))
    assert response.status_code == 400, response.text
    assert untouched.requests == []


@pytest.mark.parametrize("claim_type", ["item", "consequence_link", "ordinary_link"])
def test_claim_judge_uses_stored_load_bearing_fields(driver, seeded, frozen, rig, claim_type):
    client = rig[0]
    target, claim, _, _ = scenario(driver, seeded, frozen, rig)
    meaning = plant_node(
        driver,
        seeded,
        "Meaning",
        name="requires",
        status="canonical",
        temporal_class="state",
        carries_consequences=True,
    )
    link = plant_link(
        driver,
        seeded,
        target["id"],
        claim["id"],
        meaning=meaning["id"],
        active_key=f"requires:{claim['id']}",
    )
    query = WorldQuery(driver, seeded)
    ordinary = query.read(
        "MATCH (a:Item {id: $a})-[l:LINK]->(b:Item {id: $b}) WHERE l.meaning = $meaning "
        "RETURN l.id AS id",
        a=target["id"],
        b=claim["id"],
        meaning=MEANING_DESCRIBES,
    )[0]["id"]
    claim_id = (
        claim["id"]
        if claim_type == "item"
        else (link["id"] if claim_type == "consequence_link" else ordinary)
    )
    provider = Provider(
        spark(verdict("supports")),
        glimmer(verdict("supports")),
        spark(verdicts((2, "supports", 0.9))),
        glimmer(verdicts((2, "supports", 0.9))),
    )
    client.app.state.belief_model_factory = lambda store: layer(store, provider)
    live = grant(driver, seeded, rig)
    response = client.post(
        "/v1/internal/brain/retest", json={"belief_id": target["id"]}, headers=bearer(live)
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/v1/internal/brain/judge",
        headers=bearer(live),
        json={
            "mode": "claim",
            "claim_id": claim_id,
            "source_id": response.json()["read"]["source_id"],
            "locator": "whole",
        },
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    calls = query.read(
        "MATCH (c:ModelCall {id: $id}) RETURN c.job AS job", id=answer["model_call_ids"][0]
    )
    assert calls[0]["job"] == (
        "verification" if claim_type == "ordinary_link" else "verification_load_bearing"
    )
    judgments = query.read(
        "MATCH (j:Judgment) WHERE j.id IN $ids RETURN j.subject AS subject",
        ids=answer["judgment_ids"],
    )
    prefix = "item" if claim_type == "item" else "link"
    assert {row["subject"] for row in judgments} == {f"{prefix}:{claim_id}"}


def test_learner_cannot_request_retest_claim_judgments(driver, seeded, rig):
    client, auth, _, settings = rig
    queue_store(driver, seeded)
    live = start_pass(driver, auth, seeded, settings=settings)
    assert live.agent == "learner"

    def forbidden(*_):
        pytest.fail("an unauthorized claim judgment constructed a model layer")

    client.app.state.belief_model_factory = forbidden
    response = client.post(
        "/v1/internal/brain/judge",
        headers=bearer(live),
        json={"mode": "claim", "claim_id": "claim", "source_id": "source", "locator": "whole"},
    )
    assert response.status_code == 403, response.text
