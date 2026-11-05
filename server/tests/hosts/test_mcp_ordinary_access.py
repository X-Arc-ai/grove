"""Ordinary accounts over /mcp: their own world only, and the prepared account stays restricted.

Everything runs through the real route, the real OAuth store and real worlds. Every name is
invented.
"""

import ast
import inspect
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.errors import ApiError
from grove.demo_guard import configuration
from grove.mcp.tools import DEFAULT_TOOLS, ToolContext, store_tool
from grove.store.query import WorldQuery
from grove.store.replay import FORGOTTEN_MARKER
from grove.writer.accounts import email_hash, ensure_demo_account
from grove.writer.testing import plant_node, purge_test_account
from tests.hosts.conftest import SECOND_TITLE, Person
from tests.hosts.test_mcp_transport import BASE, READ, READ_WRITE, RESOURCE, bearer, issue

OWN = "Quillon harbour lease"
OTHER = "Vesperine orchard contract"


def call(name, arguments, message_id=1):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def listing():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def plant_item(driver, person: Person, title: str, **values) -> dict:
    kind = WorldQuery(driver, person.world_id).read(
        "MATCH (i:Item {id: $id}) RETURN i.kind AS kind", id=person.self_item_id
    )[0]["kind"]
    fields = {"properties": "{}", "recorded": clock.now(), **values}
    return plant_node(driver, person.world_id, "Item", kind=kind, title=title, **fields)


def recall(web, token, arguments):
    response = web.post("/mcp", json=call("recall", arguments), headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()["result"]


def titles(result) -> list[str]:
    assert not result.get("isError"), result
    return [item["title"] for item in result["structuredContent"]["items"]]


def tool_names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(tool["name"] for tool in response.json()["result"]["tools"])


@pytest.fixture
def web(driver):
    with TestClient(create_app(ApiSettings(BASE), driver=driver), base_url=BASE) as client:
        yield client


@pytest.fixture
def pair(driver, hosts_plant):
    """Two ordinary people in two worlds, each with one item and a live read and write token."""
    first, second = hosts_plant.person(), hosts_plant.person(SECOND_TITLE)
    plant_item(driver, first, OWN, summary="The lease runs from the north quay.")
    plant_item(driver, second, OTHER, summary="The contract names the south gate.")
    return (
        first,
        second,
        issue(driver, first, hosts_plant.client()),
        issue(driver, second, hosts_plant.client()),
    )


@pytest.fixture
def demo(driver, make_world, monkeypatch, tmp_path):
    """The prepared account: configuration set, and its durable designation."""
    address = f"demo-{uuid4().hex}@example.invalid"
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", email_hash(address))
    path = tmp_path / "demo-release.lock"
    path.touch(mode=0o600)
    monkeypatch.setenv("GROVE_DEMO_GUARD_FILE", str(path))
    world = make_world()
    account = ensure_demo_account(driver, email_hash(address), world_id_for=lambda _: world)
    assert configuration() is not None
    try:
        yield Person(account.user_id, account.world_id, "unused", account.self_item_id, "You")
    finally:
        purge_test_account(driver, email_hash(address))


# Two worlds ---------------------------------------------------------------------------------


def test_two_ordinary_worlds_cannot_read_each_other_over_mcp(pair, web):
    _, _, first_token, second_token = pair

    assert titles(recall(web, first_token.access, {"about": "Quillon harbour"})) == [OWN]
    assert titles(recall(web, second_token.access, {"about": "Vesperine orchard"})) == [OTHER]

    for token, foreign_words, foreign_text in (
        (first_token, "Vesperine orchard", OTHER),
        (second_token, "Quillon harbour", OWN),
    ):
        answer = recall(web, token.access, {"about": foreign_words})
        assert titles(answer) == []
        assert answer["structuredContent"]["answer"] == "no_record"
        assert foreign_text not in str(answer)


def test_a_foreign_item_id_and_a_world_argument_reach_nothing(driver, pair, web):
    first, second, first_token, _ = pair
    foreign = WorldQuery(driver, second.world_id).read(
        "MATCH (i:Item {title: $title}) RETURN i.id AS id", title=OTHER
    )[0]["id"]
    answer = recall(web, first_token.access, {"about": foreign})
    assert OTHER not in str(answer)

    refused = recall(
        web, first_token.access, {"about": "Vesperine orchard", "world_id": second.world_id}
    )
    assert refused["isError"] is True
    assert OTHER not in str(refused)
    assert refused["structuredContent"]["error"]["code"] == "invalid_arguments"


# The route by account -----------------------------------------------------------------------


def test_an_ordinary_token_gets_both_tools_on_an_app_with_demo_configuration(
    driver, demo, hosts_plant, web
):
    """The Muse 403 of 2026-11-04: consent worked, and every /mcp call then refused."""
    ordinary = hosts_plant.person()
    plant_item(driver, ordinary, OWN)
    token = issue(driver, ordinary, hosts_plant.client())

    response = web.post("/mcp", json=listing(), headers=bearer(token.access))

    assert tool_names(response) == ["recall", "store"]
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert "Cedar" not in tools["store"]["title"] and "Cedar" not in tools["recall"]["title"]
    assert titles(recall(web, token.access, {"about": "Quillon harbour"})) == [OWN]


def test_the_designated_account_keeps_the_restricted_pair_and_its_guard(
    driver, demo, hosts_plant, web
):
    token = issue(driver, demo, hosts_plant.client())

    response = web.post("/mcp", json=listing(), headers=bearer(token.access))

    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert tools["store"]["title"] == "Store the Cedar demo fact"
    assert tools["recall"]["title"] == "Recall the Cedar demo"
    assert "const" in tools["store"]["inputSchema"]["properties"]["text"]
    free_text = call("store", {"text": "Any words.", "verbatim": False, "idempotency_key": "k"})
    refused = web.post("/mcp", json=free_text, headers=bearer(token.access))
    assert refused.json()["result"]["isError"] is True
    assert WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r.id AS id") == []


def test_a_demo_world_gets_nothing_general_once_its_configuration_is_removed(
    driver, demo, hosts_plant, monkeypatch
):
    token = issue(driver, demo, hosts_plant.client())
    monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH")
    monkeypatch.delenv("GROVE_DEMO_GUARD_FILE")
    assert configuration() is None

    with TestClient(create_app(ApiSettings(BASE), driver=driver), base_url=BASE) as client:
        for body in (listing(), call("recall", {"about": "Cedar"})):
            refused = client.post("/mcp", json=body, headers=bearer(token.access))
            assert refused.status_code == 403
            assert refused.json()["error"]["code"] == "demo_access_refused"


def test_the_general_store_handler_refuses_a_demo_world_by_its_real_cause(
    driver, demo, hosts_plant
):
    token = issue(driver, demo, hosts_plant.client())
    context = ToolContext(
        driver, demo.world_id, demo.user_id, token.client_id, frozenset(READ_WRITE), token.grant_id
    )
    arguments = {"text": "Any words.", "verbatim": False, "idempotency_key": "k"}

    with pytest.raises(ApiError) as refused:
        store_tool().handler(context, arguments)

    assert refused.value.code == "demo_access_refused"
    assert WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r.id AS id") == []


def test_two_authorization_headers_never_open_the_general_tools_for_a_demo_world(
    driver, demo, hosts_plant, web
):
    ordinary = hosts_plant.person()
    designated = issue(driver, demo, hosts_plant.client())
    other = issue(driver, ordinary, hosts_plant.client())

    for first, last in ((designated, other), (other, designated)):
        response = web.post(
            "/mcp",
            json=listing(),
            headers=[
                ("Authorization", f"Bearer {first.access}"),
                ("Authorization", f"Bearer {last.access}"),
            ],
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "demo_access_refused"


def test_a_token_whose_account_does_not_own_the_world_is_refused(driver, hosts_plant, web):
    owner = hosts_plant.person()
    plant_item(driver, owner, OWN)
    intruder = Person(str(uuid4()), owner.world_id, "unused", owner.self_item_id, "Nobody")
    token = issue(driver, intruder, hosts_plant.client())

    refused = web.post(
        "/mcp", json=call("recall", {"about": "Quillon harbour"}), headers=bearer(token.access)
    )

    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "unauthenticated"
    assert "invalid_token" in refused.headers["www-authenticate"]
    assert OWN not in refused.text


# Scopes and refusals ------------------------------------------------------------------------


def test_scope_denied_means_a_missing_scope_and_a_read_only_grant_cannot_store(
    driver, hosts_plant, web
):
    person = hosts_plant.person()
    plant_item(driver, person, OWN)
    token = issue(driver, person, hosts_plant.client(), scopes=READ)

    assert titles(recall(web, token.access, {"about": "Quillon harbour"})) == [OWN]
    refused = web.post(
        "/mcp",
        json=call("store", {"text": "A note.", "verbatim": False, "idempotency_key": "k"}),
        headers=bearer(token.access),
    )

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "scope_denied"
    assert 'error="insufficient_scope"' in refused.headers["www-authenticate"]
    assert 'scope="grove.write"' in refused.headers["www-authenticate"]


def test_a_write_only_grant_cannot_recall(driver, hosts_plant, web):
    person = hosts_plant.person()
    plant_item(driver, person, OWN)
    token = issue(driver, person, hosts_plant.client(), scopes=("grove.write",))

    refused = web.post(
        "/mcp", json=call("recall", {"about": "Quillon harbour"}), headers=bearer(token.access)
    )

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "scope_denied"
    assert OWN not in refused.text


def test_a_batch_of_recall_and_store_refuses_before_any_tool_runs(driver, pair, web):
    first, _, token, _ = pair
    arguments = {"text": "A note.", "verbatim": False, "idempotency_key": "k"}

    for batch in (
        [call("recall", {"about": "Quillon"}, 1), call("store", arguments, 2)],
        [call("store", arguments, 1), call("recall", {"about": "Quillon"}, 2)],
    ):
        refused = web.post("/mcp", json=batch, headers=bearer(token.access))
        assert refused.status_code == 400
        assert refused.json()["error"]["details"]["rule"] == "mixed_read_write_batch"
    assert WorldQuery(driver, first.world_id).read("MATCH (r:Request) RETURN r.id AS id") == []


def test_a_revoked_or_expired_token_reads_nothing(driver, hosts_plant, web):
    from grove.writer.oauth import revoke_grant

    person = hosts_plant.person()
    plant_item(driver, person, OWN)
    live = issue(driver, person, hosts_plant.client())
    assert titles(recall(web, live.access, {"about": "Quillon harbour"})) == [OWN]
    revoke_grant(driver, grant_id=live.grant_id, world_id=person.world_id, at=clock.now())
    expired = issue(
        driver,
        person,
        hosts_plant.client(),
        at=clock.now() - timedelta(hours=2),
        access_lifetime=timedelta(hours=1),
    )
    foreign_resource = issue(
        driver, person, hosts_plant.client(), resource=RESOURCE.replace("grove", "other")
    )

    for token in (live, expired, foreign_resource):
        refused = web.post(
            "/mcp", json=call("recall", {"about": "Quillon"}), headers=bearer(token.access)
        )
        assert refused.status_code == 401
        assert OWN not in refused.text


# What recall returns ------------------------------------------------------------------------


def test_recall_leaves_out_forgotten_rows(driver, hosts_plant, web):
    person = hosts_plant.person()
    plant_item(driver, person, FORGOTTEN_MARKER, summary="Quillon harbour keeps this line.")
    plant_item(driver, person, OWN, summary=FORGOTTEN_MARKER, body="Quillon body words.")
    token = issue(driver, person, hosts_plant.client())

    answer = recall(web, token.access, {"about": "Quillon"})

    assert titles(answer) == [OWN]
    assert FORGOTTEN_MARKER not in str(answer["structuredContent"])
    assert "keeps this line" not in str(answer)
    assert answer["structuredContent"]["items"][0]["summary"] is None


def test_recall_leaves_out_an_ended_item_and_keeps_kinds_and_source_types_as_filters(
    driver, hosts_plant, web
):
    person = hosts_plant.person()
    plant_item(driver, person, "Quillon old berth", end_class="ended")
    plant_item(driver, person, OWN, derived_source_type="said")
    token = issue(driver, person, hosts_plant.client())

    assert titles(recall(web, token.access, {"about": "Quillon"})) == [OWN]
    filtered = {"about": "Quillon", "source_types": ["confirmed"]}
    assert titles(recall(web, token.access, filtered)) == []
    filtered = {"about": "Quillon", "kinds": ["no-such-kind"]}
    assert titles(recall(web, token.access, filtered)) == []


def test_recall_shows_the_cited_message_only_and_skips_a_forgotten_source(driver, hosts_plant, web):
    person = hosts_plant.person()
    item = plant_item(driver, person, OWN)
    kept = plant_node(
        driver,
        person.world_id,
        "Source",
        content='[{"key":"m-1","text":"The quay is north."},{"key":"m-2","text":"Private words."}]',
        forget_state="kept",
    )
    gone = plant_node(driver, person.world_id, "Source", content="[]", forget_state="forgotten")
    for source, key in ((kept, "m-1"), (gone, "m-9")):
        plant_node(
            driver,
            person.world_id,
            "Evidence",
            target_type="item",
            target_id=item["id"],
            source_id=source["id"],
            locator=f"key:{key}",
            message_key=key,
            active=True,
            source_type="said",
            source_time=clock.now(),
        )
    token = issue(driver, person, hosts_plant.client())

    answer = recall(web, token.access, {"about": "Quillon", "include": ["evidence"]})

    card = answer["structuredContent"]["items"][0]
    assert [proof["excerpt"] for proof in card["evidence"]] == ["The quay is north."]
    assert "Private words." not in str(answer)


def test_the_own_item_and_a_pointer_source_do_not_break_an_answer(driver, hosts_plant, web):
    person = hosts_plant.person(OWN)
    pointer = plant_node(
        driver, person.world_id, "Source", content_pointer="pointer", forget_state="kept"
    )
    plant_node(
        driver,
        person.world_id,
        "Evidence",
        target_type="item",
        target_id=person.self_item_id,
        source_id=pointer["id"],
        locator="whole",
        active=True,
        source_type="said",
        source_time=clock.now(),
    )
    token = issue(driver, person, hosts_plant.client())

    answer = recall(web, token.access, {"about": "Quillon", "include": ["evidence"]})

    assert titles(answer) == [OWN]
    assert answer["structuredContent"]["items"][0]["evidence"][0]["excerpt"] is None


@pytest.mark.parametrize(
    "arguments,field",
    [
        ({"about": "Quillon", "hops": {"depth": 2}}, "hops"),
        ({"about": "Quillon", "cursor": "x" * 43}, "cursor"),
        ({"about": "a " * 400}, "about"),
    ],
)
def test_a_mode_this_release_lacks_is_a_typed_refusal(driver, hosts_plant, web, arguments, field):
    person = hosts_plant.person()
    token = issue(driver, person, hosts_plant.client())

    refused = recall(web, token.access, arguments)

    assert refused["isError"] is True
    assert refused["structuredContent"]["error"]["details"]["field"] == field


def test_no_match_says_no_record_and_never_claims_absence_in_the_world(driver, pair, web):
    _, _, token, _ = pair

    answer = recall(web, token.access, {"about": "Zephyrine"})["structuredContent"]

    assert answer["answer"] == "no_record"
    assert answer["items"] == []
    assert answer["coverage"]["live_state_checked"] is False
    assert "does not mean" in answer["note"]


def test_recall_is_a_direct_read_with_no_model_call():
    from grove.reads import host_recall

    tree = ast.parse(Path(inspect.getfile(host_recall)).read_text())
    imported = [
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [a.name for a in node.names]
        )
    ]
    assert not [name for name in imported if name.startswith(("grove.models", "grove.runner"))]


def test_the_default_set_is_both_tools_with_the_declared_scopes():
    assert {tool.name: tool.scope for tool in DEFAULT_TOOLS} == {
        "store": "grove.write",
        "recall": "grove.read",
    }
