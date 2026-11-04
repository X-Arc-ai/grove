"""Exercise the live-token read/judge/envelope path with only external IO scripted."""

import json
from datetime import timedelta

import httpx
import pytest

from grove.runner.passes import end_pass
from grove.screening import Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer.testing import plant_node
from tests.belief_tools.test_tools import layer, verdict
from tests.consolidation.test_connector import bearer, belief, form, grant, question
from tests.models.test_judge import Provider, glimmer, spark


def test_retest_api_rejects_cross_world_and_forged_fields_before_io(
    driver, seeded, frozen, rig, make_world
):
    client = rig[0]
    claim, _, _ = belief(driver, seeded, frozen, due=True)
    other, _, _ = belief(driver, make_world(), frozen, due=True)
    live = grant(driver, seeded, rig)

    def forbidden(*_):
        pytest.fail("an unauthorized retest reached a source read or model call")

    client.app.state.belief_model_factory = forbidden
    client.app.state.belief_read_dependencies = {
        "resolver": forbidden,
        "transport": httpx.MockTransport(forbidden),
    }
    assert (
        client.post("/v1/internal/brain/retest", json={"belief_id": claim["id"]}).status_code == 401
    )
    for body in (
        {"belief_id": other["id"]},
        {"belief_id": claim["id"], "world_id": other["world_id"]},
        {"belief_id": claim["id"], "verdict": "supports"},
        {"belief_id": claim["id"], "source_text": "Invented source text"},
        {"belief_id": claim["id"], "locator": "https://outside.example.test"},
    ):
        response = client.post("/v1/internal/brain/retest", json=body, headers=bearer(live))
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_arguments"
    assert WorldQuery(driver, seeded).read("MATCH (s:Source) RETURN s") == []


@pytest.mark.parametrize(
    "first,second,expected_state",
    [
        ("supports", "supports", "established"),
        ("contradicts", "contradicts", "weakening"),
        ("complements", "complements", "established"),
        ("not_about_claim", "not_about_claim", "established"),
        ("supports", "contradicts", "established"),
    ],
)
def test_retest_read_two_judges_and_disposition_commit_through_real_api(
    driver, seeded, frozen, rig, first, second, expected_state
):
    client = rig[0]
    url = "https://status.example.test/service"
    previous_confirmation = frozen.now() - timedelta(days=2)
    connector = plant_node(driver, seeded, "Connector", type="web_pages", scope=json.dumps([url]))
    claim, _, _ = belief(
        driver,
        seeded,
        frozen,
        due=True,
        confirmed=previous_confirmation,
        retest_how=json.dumps({"connector": connector["id"], "locator": url}),
    )
    provider = Provider(spark(verdict(first)), glimmer(verdict(second)))
    client.app.state.belief_model_factory = lambda store: layer(store, provider)
    reads = []

    def read(request):
        reads.append(request)
        text = {
            "supports": "The service answers promptly after each request.",
            "contradicts": "The service no longer answers requests promptly.",
            "complements": "The service also accepts requests by post.",
            "not_about_claim": "The harbour has three piers.",
        }[first]
        return httpx.Response(200, text=text)

    client.app.state.belief_read_dependencies = {
        "screener": Screener(ScreeningConfig()),
        "resolver": lambda *_: ["93.184.215.14"],
        "transport": httpx.MockTransport(read),
    }
    live = grant(driver, seeded, rig)
    response = client.get("/v1/internal/brain/pass", headers=bearer(live))
    assert response.status_code == 200, response.text
    packet = response.json()
    assert url in packet["data"]
    assert url not in json.dumps({key: value for key, value in packet.items() if key != "data"})
    catalogue = packet["operation_catalogue"]
    assert "field_subject" in catalogue["mint_belief"]["optional"]
    assert "judgment_ids" in catalogue["resolve_support_change"]["required"]
    assert "pieces" in catalogue["retest_belief"]["required"]
    assert "second_judge" in catalogue["retest_belief"]["optional"]
    assert "correct_belief" not in catalogue
    response = client.post(
        "/v1/internal/brain/retest", json={"belief_id": claim["id"]}, headers=bearer(live)
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    observation = answer["read"]["observation"]
    assert observation["ev_id"] == f"retest:{claim['id']}:{answer['read']['source_id']}"
    assert len(reads) == 1 and reads[0].headers["host"] == "status.example.test"
    assert answer["verdicts"] == [first, second]
    assert len(set(answer["model_call_ids"])) == 2

    horizon = frozen.now() + timedelta(days=7)
    doc = form(driver, seeded, live)
    doc["evidence"] = [observation]
    doc["dispositions"] = [
        {
            "op": "retest_belief",
            "op_id": "retest-service",
            "target": claim["id"],
            "pieces": [{"ev_id": observation["ev_id"], "judgment_id": answer["judgment_ids"][0]}],
            "second_judge": [answer["judgment_ids"][1]],
            "recheck_by": horizon.isoformat(),
            "reason": "The authorized live source and both recorded judgments complete the check.",
        }
    ]
    response = client.post("/v1/internal/brain/propose", json={"batch": doc}, headers=bearer(live))
    assert response.status_code == 200, response.text
    query = WorldQuery(driver, seeded)
    stored = query.read("MATCH (i:Item {id: $id}) RETURN i", id=claim["id"])[0]["i"]
    assert stored["last_confirmed"] == (
        frozen.now() if first == second == "supports" else previous_confirmation
    )
    assert stored["recheck_by"] == horizon
    state = query.read("MATCH (b:Belief {item_id: $id}) RETURN b", id=claim["id"])[0]["b"]
    assert state["state"] == expected_state
    assert state["last_retested_at"] == frozen.now()
    assert state["last_retest_reason"]
    if first == second:
        assert state["last_retest_outcome"] == first
    evidence = query.read(
        "MATCH (e:Evidence {target_id: $id, active: true}) RETURN e", id=claim["id"]
    )
    if first == second and first in {"supports", "contradicts"}:
        assert len(evidence) == 1
        assert evidence[0]["e"]["source_id"] == answer["read"]["source_id"]
        assert evidence[0]["e"]["episode_id"] == answer["read"]["episode_id"]
        assert evidence[0]["e"]["source_type"] == "confirmed"
        assert evidence[0]["e"]["polarity"] == first
    else:
        assert evidence == []
    judgments = query.read("MATCH (j:Judgment) RETURN j")
    assert {row["j"]["id"] for row in judgments} == set(answer["judgment_ids"])
    assert {row["j"]["subject"] for row in judgments} == {f"item:{claim['id']}"}
    assert {row["j"]["source_id"] for row in judgments} == {answer["read"]["source_id"]}
    ended = end_pass(driver, seeded, live.run_id, outcome="ok")
    if first == second:
        assert ended["outcome"] == "ok"
    else:
        # A disagreeing pair must not silently consume the new contradiction.
        assert ended["outcome"] == "failed"
        queued = query.read("MATCH (c:Contradiction {status: 'open'}) RETURN c")
        assert len(queued) == 1
        assert queued[0]["c"]["fresh_source_id"] == answer["read"]["source_id"]
        frozen.advance(seconds=61)
        retry = grant(driver, seeded, rig)
        retry_doc = form(driver, seeded, retry)
        resolution = question(queued[0]["c"])
        resolution["checked_source_ids"] = [answer["read"]["source_id"]]
        retry_doc["resolutions"] = [resolution]
        response = client.post(
            "/v1/internal/brain/propose", json={"batch": retry_doc}, headers=bearer(retry)
        )
        assert response.status_code == 200, response.text
        assert end_pass(driver, seeded, retry.run_id, outcome="ok")["outcome"] == "ok"
