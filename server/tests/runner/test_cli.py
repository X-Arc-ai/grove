"""The brain CLI against a served API: each verb, the batch from stdin only, and two worlds that
cannot see each other through it."""

import json

import pytest
from click.testing import CliRunner

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.cli.main import brain
from tests.runner.helpers import FAST, Served, new_auth, queue_store, seeded_world


@pytest.fixture
def served(driver, make_world):
    auth, token = new_auth()
    app = create_app(
        ApiSettings(public_base_url="http://127.0.0.1:8000"),
        driver=driver,
        runner_auth=auth,
        pass_settings=FAST,
    )
    with Served(app) as server:
        import httpx

        def grant(world_id: str) -> dict:
            answer = httpx.post(
                f"{server.url}/v1/internal/runner/passes",
                json={"world_id": world_id},
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
            )
            assert answer.status_code == 201, answer.text
            return answer.json()["grant"]

        yield server, grant, lambda: seeded_world(driver, make_world())


def cli(server, grant, *args, stdin=None):
    env = {"GROVE_API_URL": server.url, "GROVE_PASS_TOKEN": grant["pass_token"]}
    result = CliRunner().invoke(brain, list(args), input=stdin, env=env)
    try:
        doc = json.loads(result.output)
    except ValueError:
        doc = None
    return result.exit_code, doc, result.output


def test_status_pass_propose_get_and_recall(driver, served):
    server, grant, world = served
    world_id = world()
    request_id = queue_store(driver, world_id)
    live = grant(world_id)
    code, status, _ = cli(server, live, "status")
    assert code == 0 and status["world_id"] == world_id and status["inputs"] == [request_id]
    code, packet, _ = cli(server, live, "pass")
    assert code == 0 and packet["requests"][0]["request_id"] == request_id
    form = packet["batch_form"]
    form["batch"]["ops"] = [form["batch"]["ops"][0] | {"title": "Lina Okafor"}]
    code, saved, _ = cli(server, live, "propose", stdin=json.dumps(form))
    assert code == 0 and saved["state"] == "saved"
    item_id = saved["receipt"]["lines"][0]["entity_id"]
    code, item, _ = cli(server, live, "get", item_id)
    assert code == 0 and item["item_id"] == item_id and item["kind"] == "person"
    assert "Lina Okafor" in item["data"]
    code, found, _ = cli(server, live, "recall", "Lina", "Okafor")
    assert code == 0 and [m["item_id"] for m in found["matches"]] == [item_id]


def test_a_refusal_is_json_with_exit_status_1(driver, served):
    server, grant, world = served
    world_id = world()
    queue_store(driver, world_id)
    live = grant(world_id)
    code, doc, _ = cli(server, live, "get", "01a19fe3-44f1-74af-8efc-e3029c5a17b6")
    assert code == 1 and doc["error"]["code"] == "not_found"
    code, doc, _ = cli(server, live, "propose", stdin="not json")
    assert code == 1 and doc["error"]["code"] == "invalid_arguments"


def test_propose_takes_no_batch_from_its_arguments(driver, served):
    server, grant, world = served
    world_id = world()
    queue_store(driver, world_id)
    live = grant(world_id)
    code, _, output = cli(server, live, "propose", '{"ops": []}')
    assert code == 2 and "unexpected extra argument" in output.lower()


def test_a_wrong_or_missing_token_is_refused(driver, served):
    server, grant, world = served
    env = {"GROVE_API_URL": server.url, "GROVE_PASS_TOKEN": "p1.bad.token"}
    result = CliRunner().invoke(brain, ["status"], env=env)
    assert result.exit_code == 1 and json.loads(result.output)["error"]["code"] == "unauthenticated"
    result = CliRunner().invoke(
        brain, ["status"], env={"GROVE_API_URL": server.url, "GROVE_PASS_TOKEN": ""}
    )
    assert result.exit_code == 1 and "not set" in result.output


def test_two_worlds_cannot_see_each_other_through_the_cli(driver, served):
    server, grant, world = served
    world_a, world_b = world(), world()
    queue_store(driver, world_a, "Remember: Ines Varga runs the Birch Lane book swap.")
    request_b = queue_store(driver, world_b, "Remember: Lina Okafor runs the Elm Street fair.")
    pass_a, pass_b = grant(world_a), grant(world_b)
    _, packet_b, _ = cli(server, pass_b, "pass")
    form_b = packet_b["batch_form"]
    form_b["batch"]["ops"] = [form_b["batch"]["ops"][0] | {"title": "Lina Okafor"}]
    _, saved_b, _ = cli(server, pass_b, "propose", stdin=json.dumps(form_b))
    item_b = saved_b["receipt"]["lines"][0]["entity_id"]
    code, doc, _ = cli(server, pass_a, "get", item_b)
    assert code == 1 and doc["error"]["code"] == "not_found"
    code, found, _ = cli(server, pass_a, "recall", "Lina")
    assert code == 0 and found["matches"] == []
    code, doc, _ = cli(
        server, pass_a, "propose", "--request", request_b, stdin=json.dumps(form_b["batch"])
    )
    assert code == 1 and doc["error"]["code"] == "not_found"
    code, doc, _ = cli(server, pass_a, "finish", request_b)
    assert code == 1 and doc["error"]["code"] == "not_found"
    _, status_a, _ = cli(server, pass_a, "status")
    assert status_a["world_id"] == world_a


def test_brain_get_refuses_a_path_and_recall_sends_no_text_in_a_url(driver, make_world):
    """Regression (R4 security review): no stored words in a URL, and no path through get."""
    seen: list[str] = []
    auth, token = new_auth()
    app = create_app(
        ApiSettings(public_base_url="http://127.0.0.1:8000"),
        driver=driver,
        runner_auth=auth,
        pass_settings=FAST,
    )

    async def recording(scope, receive, send):
        if scope["type"] == "http":
            seen.append(scope["path"] + "?" + scope["query_string"].decode())
        await app(scope, receive, send)

    world_id = seeded_world(driver, make_world())
    queue_store(driver, world_id)
    with Served(recording) as server:
        import httpx

        grant = httpx.post(
            f"{server.url}/v1/internal/runner/passes",
            json={"world_id": world_id},
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
        ).json()["grant"]
        code, found, _ = cli(server, grant, "recall", "Lina", "Okafor")
        assert code == 0 and found["matches"] == []
        code, doc, _ = cli(server, grant, "get", "../../runner/due")
        assert code == 1 and doc["error"]["code"] == "invalid_arguments"
    assert not any("Lina" in url or "Okafor" in url for url in seen), seen
    assert not any("runner/due" in url for url in seen if "brain" in url), seen
