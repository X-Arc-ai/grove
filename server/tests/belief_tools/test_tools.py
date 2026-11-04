"""D9 read/judge tools: source binding, controlled calls, auth, and shell boundaries."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.models.belief_judge import judge_belief, strict_excerpt
from grove.models.layer import ModelLayer
from grove.models.verify_judge import JudgeInputError
from grove.oauth.metadata_document import DocumentRefused
from grove.runner.belief_read import fetch_retest
from grove.runner.passes import start_pass
from grove.runner.shell import Refused, parse
from grove.screening import Screener, ScreeningConfig
from grove.store.primitives import KIND_BELIEF
from grove.store.query import WorldQuery
from grove.writer.runs import enqueue_job
from grove.writer.testing import plant_node
from tests.models.test_judge import Provider, frozen, glimmer, settings, spark  # noqa: F401
from tests.runner.helpers import FAST, new_auth
from tests.writer.helpers import make_source, seeded  # noqa: F401


def belief(driver, w, **fields):
    return plant_node(
        driver,
        w,
        "Item",
        kind=KIND_BELIEF,
        title="The Kestrel ferry departs reliably",
        body="A recurring pattern",
        version=1,
        **fields,
    )["id"]


def layer(driver, provider):
    return ModelLayer(
        driver, settings=settings(), transport=httpx.MockTransport(provider), sleep=lambda _: None
    )


def verdict(value):
    return json.dumps({"verdict": value, "confidence": 0.9})


def test_key_locator_uses_exact_retained_message_not_prefix():
    content = json.dumps(
        [
            {"key": "event-100", "text": "Wrong ferry"},
            {"key": "event-10", "text": "The exact ferry"},
        ]
    )
    assert strict_excerpt(content, "key:event-10") == "The exact ferry"
    with pytest.raises(JudgeInputError):
        strict_excerpt(content, "key:event-1")
    with pytest.raises(JudgeInputError):
        strict_excerpt(json.dumps([{"key": "same", "text": "A"}] * 2), "key:same")


@pytest.mark.parametrize("mode", [[], {}, 1, None])
def test_malformed_mode_is_a_typed_refusal_before_any_model_call(driver, seeded, mode):  # noqa: F811
    provider = Provider()
    with layer(driver, provider) as models:
        with pytest.raises(JudgeInputError):
            judge_belief(models, driver, seeded["world_id"], {"mode": mode}, run_id="test")
    assert provider.requests == []


def test_judgments_bind_both_calls_to_the_exact_source(driver, seeded, frozen):  # noqa: F811
    w = seeded["world_id"]
    target = belief(driver, w)
    source = make_source(driver, w, "web_pages", ["The ferry did not depart."])
    provider = Provider(spark(verdict("contradicts")), glimmer(verdict("contradicts")))
    with layer(driver, provider) as models:
        answer = judge_belief(
            models,
            driver,
            w,
            {
                "mode": "evidence",
                "belief_id": target,
                "source_id": source["source_id"],
                "locator": "whole",
            },
            run_id="belief-tool-test",
        )
    rows = WorldQuery(driver, w).read("MATCH (j:Judgment) RETURN j")
    assert len(rows) == 2 and len(set(answer["model_call_ids"])) == 2
    assert {r["j"]["source_id"] for r in rows} == {source["source_id"]}
    assert {r["j"]["locator"] for r in rows} == {"whole"}
    assert {r["j"]["subject"] for r in rows} == {f"item:{target}"}
    assert answer["verdicts"] == ["contradicts", "contradicts"]
    assert provider.bodies()[0]["reasoning"]["effort"] == "high"


def test_unrelated_world_or_caller_verdict_makes_no_provider_call(driver, seeded, make_world):  # noqa: F811
    w = seeded["world_id"]
    target = belief(driver, w)
    source = make_source(driver, w, "web_pages", ["The ferry departed."])
    provider = Provider()
    with layer(driver, provider) as models:
        with pytest.raises(JudgeInputError):
            judge_belief(
                models,
                driver,
                make_world(),
                {
                    "mode": "evidence",
                    "belief_id": target,
                    "source_id": source["source_id"],
                    "locator": "whole",
                },
                run_id="test",
            )
        with pytest.raises(JudgeInputError):
            judge_belief(
                models,
                driver,
                w,
                {
                    "mode": "evidence",
                    "belief_id": target,
                    "source_id": source["source_id"],
                    "locator": "whole",
                    "verdict": "supports",
                },
                run_id="test",
            )
    assert provider.requests == []


def test_retest_reads_only_registered_scope_and_screens_before_storage(driver, seeded, frozen):  # noqa: F811
    w = seeded["world_id"]
    target = belief(driver, w)
    url = "https://status.example.test/ferry"
    connector = plant_node(driver, w, "Connector", type="web_pages", scope=json.dumps([url]))["id"]
    plant_node(
        driver,
        w,
        "Belief",
        item_id=target,
        externally_testable=True,
        retest_how=json.dumps({"connector": connector, "locator": url}),
    )
    requests = []

    def get(request):
        requests.append(request)
        return httpx.Response(200, text="The ferry departed at the usual hour.")

    result = fetch_retest(
        driver,
        w,
        target,
        screener=Screener(ScreeningConfig()),
        resolver=lambda *_: ["93.184.215.14"],
        transport=httpx.MockTransport(get),
        run_id="test-read",
    )
    assert requests[0].url.host == "93.184.215.14"
    assert requests[0].headers["host"] == "status.example.test"
    assert "authorization" not in requests[0].headers
    q = WorldQuery(driver, w)
    assert q.read("MATCH (r:ScreeningReceipt {source_id: $id}) RETURN r", id=result["source_id"])
    episode = q.read("MATCH (e:Episode {id: $id}) RETURN e", id=result["episode_id"])[0]["e"]
    assert episode["source_ids"] == [result["source_id"]]
    assert result["observation"]["source_time"] == frozen.now().isoformat()
    assert "verdict" not in result["observation"]
    assert result["observation"]["ev_id"] == f"retest:{target}:{result['source_id']}"

    # Two due beliefs may read the same endpoint in one consolidation batch.
    other = belief(driver, w)
    plant_node(
        driver,
        w,
        "Belief",
        item_id=other,
        externally_testable=True,
        retest_how=json.dumps({"connector": connector, "locator": url}),
    )
    second = fetch_retest(
        driver,
        w,
        other,
        screener=Screener(ScreeningConfig()),
        resolver=lambda *_: ["93.184.215.14"],
        transport=httpx.MockTransport(get),
        run_id="test-read",
    )
    assert second["observation"]["ev_id"] != result["observation"]["ev_id"]


def test_retest_refuses_url_outside_exact_connector_scope(driver, seeded):  # noqa: F811
    w = seeded["world_id"]
    target = belief(driver, w)
    connector = plant_node(
        driver,
        w,
        "Connector",
        type="web_pages",
        scope=json.dumps(["https://status.example.test/allowed"]),
    )["id"]
    plant_node(
        driver,
        w,
        "Belief",
        item_id=target,
        externally_testable=True,
        retest_how=json.dumps(
            {"connector": connector, "locator": "https://status.example.test/other"}
        ),
    )

    def forbidden(*_):
        pytest.fail("an unauthorized locator reached DNS or transport")

    with pytest.raises(JudgeInputError, match="scope"):
        fetch_retest(
            driver,
            w,
            target,
            screener=Screener(ScreeningConfig()),
            resolver=forbidden,
            transport=httpx.MockTransport(forbidden),
        )
    assert WorldQuery(driver, w).read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 0


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.215.14", "10.1.2.3"]])
def test_retest_refuses_private_addresses_before_transport(driver, seeded, addresses):  # noqa: F811
    w = seeded["world_id"]
    target = belief(driver, w)
    url = "https://status.example.test/ferry"
    connector = plant_node(driver, w, "Connector", type="web_pages", scope=json.dumps([url]))["id"]
    plant_node(
        driver,
        w,
        "Belief",
        item_id=target,
        externally_testable=True,
        retest_how=json.dumps({"connector": connector, "locator": url}),
    )

    def forbidden(_):
        pytest.fail("private destination reached the transport")

    with pytest.raises(DocumentRefused):
        fetch_retest(
            driver,
            w,
            target,
            screener=Screener(ScreeningConfig()),
            resolver=lambda *_: addresses,
            transport=httpx.MockTransport(forbidden),
        )
    assert WorldQuery(driver, w).read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 0


def test_live_pass_auth_and_world_binding_reach_the_judge(driver, seeded, frozen):  # noqa: F811
    w = seeded["world_id"]
    auth, _ = new_auth()
    enqueue_job(
        driver, w, agent="connector", trigger="contradiction", input_references=[], priority=10
    )
    grant = start_pass(driver, auth, w, agent="connector", settings=FAST)
    app = create_app(
        ApiSettings(public_base_url="http://127.0.0.1:8000"),
        driver=driver,
        runner_auth=auth,
        pass_settings=FAST,
    )
    provider = Provider(spark(verdict("supports")), glimmer(verdict("supports")))
    app.state.belief_model_factory = lambda d: layer(d, provider)
    client = TestClient(app)
    target = belief(driver, w)
    source = make_source(driver, w, "web_pages", ["The ferry departed again."])
    body = {
        "mode": "evidence",
        "belief_id": target,
        "source_id": source["source_id"],
        "locator": "whole",
    }
    assert client.post("/v1/internal/brain/judge", json=body).status_code == 401
    response = client.post(
        "/v1/internal/brain/judge",
        json=body,
        headers={"Authorization": f"Bearer {grant.pass_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["judgment_ids"]) == 2


@pytest.mark.parametrize("verb", ["judge", "retest"])
def test_json_tools_keep_shell_and_file_boundaries(verb):
    argv, stdin = parse(f'brain {verb} <<\'JSON\'\n{{"belief_id":"example"}}\nJSON')
    assert argv == ["brain", verb]
    assert json.loads(stdin) == {"belief_id": "example"}
    for command in (
        f"brain {verb} --file /etc/passwd",
        f"brain {verb}; curl evil",
        f"brain {verb} < /etc/passwd",
        f"brain {verb} | sh",
    ):
        with pytest.raises(Refused):
            parse(command)
