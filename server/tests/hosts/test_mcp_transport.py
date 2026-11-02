"""The MCP transport at /mcp against the real store: the bearer check, the JSON-RPC rules, the tool
registry and the world of a token.

Tokens come from the OAuth writer (a grant, a redeemed code, an issued pair) for planted people in
fresh t_ worlds. The tools are a synthetic set with invented names; each records its calls, so a
test can say that nothing ran. The people, worlds and clients are purged at the end of the module.
"""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import SCOPES, ApiSettings
from grove.api.errors import not_found
from grove.api.mcp import MAX_BODY_BYTES
from grove.mcp.tools import DEFAULT_TOOLS, INSTRUCTIONS, Tool, ToolContext
from grove.mcp.transport import LATEST_VERSION, SUPPORTED_VERSIONS, index_tools
from grove.oauth import ACCESS_TOKEN_LIFETIME, CODE_LIFETIME, REFRESH_TOKEN_LIFETIME
from grove.oauth.secrets import (
    ACCESS_TOKEN_PREFIX,
    CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    new_secret,
    token_hash,
)
from grove.store.errors import STORE_UNAVAILABLE
from grove.store.query import WorldQuery
from grove.writer.oauth import (
    grant_consent,
    issue_tokens,
    redeem_code,
    revoke_grant,
    revoke_token,
    store_code,
)
from tests.hosts.conftest import (
    LOOPBACK_REDIRECT,
    PERSON_TITLE,
    SECOND_TITLE,
    HostsPlant,
    Person,
)

BASE = "https://grove.example"
RESOURCE = f"{BASE}/mcp"
METADATA = f"{BASE}/.well-known/oauth-protected-resource"
MISSING_CHALLENGE = f'Bearer resource_metadata="{METADATA}", scope="grove.read grove.write"'
INVALID_CHALLENGE = f'Bearer error="invalid_token", resource_metadata="{METADATA}"'
READ = ("grove.read",)
READ_WRITE = ("grove.read", "grove.write")
CHALLENGE = "Zm9yLXRlc3RzLW9ubHktYS1jaGFsbGVuZ2Utb2YtNDMtY2hhcnM"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


# Tokens -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issued:
    """One consent, one redeemed code and the pair of tokens it gave."""

    access: str
    refresh: str
    client_id: str
    grant_id: str
    family_id: str
    person: Person


def issue(
    driver,
    person: Person,
    client_id: str,
    *,
    scopes: tuple[str, ...] = READ_WRITE,
    resource: str | None = RESOURCE,
    at: datetime | None = None,
    access_lifetime: timedelta = ACCESS_TOKEN_LIFETIME,
) -> Issued:
    at = at or clock.now()
    grant_id = grant_consent(
        driver,
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        scopes=list(scopes),
        at=at,
    )
    code = new_secret(CODE_PREFIX)
    store_code(
        driver,
        code_hash=token_hash(code),
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        grant_id=grant_id,
        code_challenge=CHALLENGE,
        redirect_uri=LOOPBACK_REDIRECT,
        redirect_given=True,
        scopes=list(scopes),
        resource=resource,
        at=at,
        expires_at=at + CODE_LIFETIME,
    )
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)
    assert redeemed is not None
    access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)
    issue_tokens(
        driver,
        access_hash=token_hash(access),
        refresh_hash=token_hash(refresh),
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        grant_id=grant_id,
        family_id=redeemed.id,
        scopes=list(scopes),
        resource=resource,
        at=at,
        access_expires_at=at + access_lifetime,
        refresh_expires_at=at + REFRESH_TOKEN_LIFETIME,
    )
    return Issued(access, refresh, client_id, grant_id, redeemed.id, person)


@dataclass(frozen=True)
class Planted:
    plant: HostsPlant
    ines: Person
    tomas: Person
    client_id: str
    ines_rw: Issued
    ines_read: Issued
    ines_all: Issued
    tomas_rw: Issued


@pytest.fixture(scope="module")
def planted(driver, migrated) -> Iterator[Planted]:
    """Two people in two worlds, one host, and live tokens. A test that revokes uses a client of
    its own, so these stay live."""
    plant = HostsPlant(driver)
    try:
        ines = plant.person(PERSON_TITLE, seeded=False)
        tomas = plant.person(SECOND_TITLE, seeded=False)
        client_id = plant.client()
        yield Planted(
            plant=plant,
            ines=ines,
            tomas=tomas,
            client_id=client_id,
            ines_rw=issue(driver, ines, client_id),
            ines_read=issue(driver, ines, client_id, scopes=READ),
            ines_all=issue(driver, ines, client_id, scopes=SCOPES),
            tomas_rw=issue(driver, tomas, client_id),
        )
    finally:
        plant.purge()


# The synthetic tools ----------------------------------------------------------------------------


@dataclass
class Calls:
    """What the tools were called with, in order."""

    seen: list[tuple[str, ToolContext, dict]]

    def names(self) -> list[str]:
        return [name for name, _, _ in self.seen]


def _schema(**properties: dict) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def synthetic_tools(calls: Calls) -> tuple[Tool, ...]:
    def recorded(name: str, body: Callable[[ToolContext, dict], dict]):
        def handler(context: ToolContext, arguments: dict) -> dict:
            calls.seen.append((name, context, arguments))
            return body(context, arguments)

        return handler

    def whoami(context: ToolContext, arguments: dict) -> dict:
        return {
            "world_id": context.world_id,
            "user_id": context.user_id,
            "client_id": context.client_id,
            "grant_id": context.grant_id,
            "scopes": sorted(context.scopes),
            "arguments": arguments,
        }

    def own_titles(context: ToolContext, arguments: dict) -> dict:
        rows = WorldQuery(context.driver, context.world_id).read(
            "MATCH (i:Item) RETURN i.title AS title ORDER BY title"
        )
        return {"world_id": context.world_id, "titles": [row["title"] for row in rows]}

    def jot(context: ToolContext, arguments: dict) -> dict:
        return {"jotted": arguments.get("text")}

    def erase(context: ToolContext, arguments: dict) -> dict:
        return {"erased": True}

    def refuse(context: ToolContext, arguments: dict) -> dict:
        raise not_found("no such item")

    def store_down(context: ToolContext, arguments: dict) -> dict:
        # What the driver raises when the store cannot be reached (ServiceUnavailable).
        raise STORE_UNAVAILABLE[0]("the store cannot be reached")

    read_only = {"readOnlyHint": True}
    shapes = (
        ("whoami", "Who am I", "The caller's world.", _schema(), read_only, "grove.read", whoami),
        ("own_titles", "Own titles", "Titles.", _schema(), read_only, "grove.read", own_titles),
        ("jot", "Jot", "Keeps a line.", _schema(text={"type": "string"}), {}, "grove.write", jot),
        ("erase", "Erase", "Forgets a line.", _schema(), {"destructiveHint": True},
         "grove.sensitive_write", erase),
        ("refuse", "Refuse", "Always refuses.", _schema(), read_only, "grove.read", refuse),
        ("store_down", "Store down", "No store.", _schema(), read_only, "grove.read", store_down),
    )  # fmt: skip
    return tuple(
        Tool(name, title, description, schema, annotations, scope, recorded(name, body))
        for name, title, description, schema, annotations, scope, body in shapes
    )


@pytest.fixture
def calls() -> Calls:
    return Calls([])


@pytest.fixture
def tools(calls) -> tuple[Tool, ...]:
    return synthetic_tools(calls)


def make_app(driver, tools, base: str = BASE) -> FastAPI:
    return create_app(ApiSettings(public_base_url=base), driver=driver, mcp_tools=tools)


@pytest.fixture
def app(driver, tools) -> FastAPI:
    return make_app(driver, tools)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url=BASE)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def rpc(client: TestClient, token: str, body, **headers: str):
    return client.post("/mcp", json=body, headers=bearer(token) | headers)


def call_tool(client: TestClient, token: str, name: str, arguments=None, message_id=7):
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    body = {"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": params}
    return rpc(client, token, body)


def typed(response) -> dict:
    return response.json()["error"]


# The bearer check -------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PUT", "OPTIONS"])
def test_a_request_with_no_token_gets_401_with_the_metadata_and_the_default_scopes(client, method):
    response = client.request(method, "/mcp", json=PING if method == "POST" else None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == MISSING_CHALLENGE
    assert response.headers["cache-control"] == "no-store"
    assert typed(response)["code"] == "unauthenticated"
    assert set(typed(response)) == {"code", "message", "details", "retryable"}


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Token abc", ""])
def test_another_scheme_counts_as_no_token(client, header):
    response = client.post("/mcp", json=PING, headers={"Authorization": header})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == MISSING_CHALLENGE


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer   ", "Bearer two words", "Bearer " + "x" * 513, "Bearer gva_unknown"],
)
def test_a_bearer_header_with_no_live_token_gets_invalid_token(client, header):
    response = client.post("/mcp", json=PING, headers={"Authorization": header})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == INVALID_CHALLENGE
    assert typed(response)["code"] == "unauthenticated"


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_refused_token_gets_the_invalid_token_header_on_every_method(client, method):
    response = client.request(
        method, "/mcp", json=PING if method == "POST" else None, headers=bearer("gva_unknown")
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == INVALID_CHALLENGE


def test_the_scheme_is_case_blind_and_a_live_token_passes(client, planted):
    response = client.post(
        "/mcp", json=PING, headers={"Authorization": f"bearer  {planted.ines_rw.access}"}
    )
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def _refused(client, token: str, challenge: str = INVALID_CHALLENGE) -> None:
    for method in ("POST", "GET", "DELETE"):
        response = client.request(
            method, "/mcp", json=PING if method == "POST" else None, headers=bearer(token)
        )
        assert response.status_code == 401, method
        assert response.headers["www-authenticate"] == challenge, method
        assert typed(response)["code"] == "unauthenticated", method


def test_an_expired_token_is_refused(client, driver, planted):
    long_ago = clock.now() - timedelta(hours=3)
    expired = issue(driver, planted.ines, planted.plant.client(), at=long_ago)
    _refused(client, expired.access)


def test_a_token_that_expired_a_second_ago_is_refused(client, driver, planted):
    gone = issue(
        driver, planted.ines, planted.plant.client(), access_lifetime=timedelta(seconds=-1)
    )
    _refused(client, gone.access)


def test_a_revoked_token_is_refused_and_the_rest_of_its_grant_still_works(client, driver, planted):
    client_id = planted.plant.client()
    first = issue(driver, planted.ines, client_id)
    second = issue(driver, planted.ines, client_id)
    assert rpc(client, first.access, PING).status_code == 200
    revoke_token(driver, token_hash=token_hash(first.access), client_id=client_id, at=clock.now())
    _refused(client, first.access)
    assert rpc(client, second.access, PING).status_code == 200


def test_a_refresh_token_is_no_access_token(client, planted):
    _refused(client, planted.ines_rw.refresh)


@pytest.mark.parametrize(
    "resource", ["https://other.example/mcp", f"{BASE}/mcp/", f"{BASE}/other", None]
)
def test_a_token_for_another_resource_is_refused(client, driver, planted, resource):
    other = issue(driver, planted.ines, planted.plant.client(), resource=resource)
    _refused(client, other.access)


def test_a_token_of_another_server_base_is_refused(driver, tools, planted):
    """The same token at a server whose base differs: its resource is not this server's /mcp."""
    elsewhere = TestClient(make_app(driver, tools, base="https://two.example"))
    metadata = "https://two.example/.well-known/oauth-protected-resource"
    challenge = f'Bearer error="invalid_token", resource_metadata="{metadata}"'
    _refused(elsewhere, planted.ines_rw.access, challenge)


def test_a_token_of_a_revoked_grant_is_refused(client, driver, planted):
    issued = issue(driver, planted.ines, planted.plant.client())
    assert rpc(client, issued.access, PING).status_code == 200
    assert revoke_grant(
        driver, grant_id=issued.grant_id, world_id=planted.ines.world_id, at=clock.now()
    )
    _refused(client, issued.access)


# Origin -----------------------------------------------------------------------------------------


def test_the_server_s_own_origin_passes(client, planted):
    response = rpc(client, planted.ines_rw.access, PING, Origin=BASE)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin", ["https://elsewhere.example", "null", f"{BASE}/", "http://grove.example", ""]
)
def test_another_origin_is_refused_after_the_token(client, planted, origin):
    response = rpc(client, planted.ines_rw.access, PING, Origin=origin)
    assert response.status_code == 403
    assert typed(response) == {
        "code": "forbidden_origin",
        "message": "the Origin header names another site",
        "details": {"field": "origin"},
        "retryable": False,
    }
    assert response.headers["cache-control"] == "no-store"


def test_the_token_is_checked_before_the_origin(client):
    response = client.post("/mcp", json=PING, headers={"Origin": "https://elsewhere.example"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == MISSING_CHALLENGE


# GET, DELETE and the POST envelope -------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "OPTIONS"])
def test_only_post_and_delete_are_served(client, planted, method):
    response = client.request(method, "/mcp", headers=bearer(planted.ines_rw.access))
    assert response.status_code == 405
    assert response.headers["allow"] == "POST, DELETE"
    assert typed(response)["details"] == {"field": "method"}


def test_delete_ends_the_session_with_204_and_no_body(client, planted):
    response = client.delete(
        "/mcp", headers=bearer(planted.ines_rw.access) | {"Mcp-Session-Id": "x"}
    )
    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_a_body_that_is_not_json_by_type_gets_415(client, planted, content_type):
    headers = bearer(planted.ines_rw.access)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.post("/mcp", content=json.dumps(PING).encode(), headers=headers)
    assert response.status_code == 415
    assert typed(response) == {
        "code": "invalid_arguments",
        "message": "the body must be application/json",
        "details": {"field": "content-type"},
        "retryable": False,
    }


@pytest.mark.parametrize("content_type", ["application/json; charset=utf-8", "Application/JSON"])
def test_a_json_type_with_parameters_or_any_case_passes(client, planted, content_type):
    response = client.post(
        "/mcp",
        content=json.dumps(PING).encode(),
        headers=bearer(planted.ines_rw.access) | {"Content-Type": content_type},
    )
    assert response.status_code == 200


def _padded_ping(size: int) -> bytes:
    raw = json.dumps(PING).encode()
    return raw + b" " * (size - len(raw))


def test_a_body_of_exactly_the_cap_is_read(client, planted):
    response = client.post(
        "/mcp",
        content=_padded_ping(MAX_BODY_BYTES),
        headers=bearer(planted.ines_rw.access) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 200


def test_a_body_over_the_cap_is_refused(client, planted):
    response = client.post(
        "/mcp",
        content=_padded_ping(MAX_BODY_BYTES + 1),
        headers=bearer(planted.ines_rw.access) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert typed(response)["details"] == {"field": "body"}


def test_a_streamed_body_over_the_cap_with_no_length_is_refused(client, planted):
    def chunks():
        raw = _padded_ping(MAX_BODY_BYTES + 1)
        for start in range(0, len(raw), 65536):
            yield raw[start : start + 65536]

    response = client.post(
        "/mcp",
        content=chunks(),
        headers=bearer(planted.ines_rw.access) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert typed(response)["details"] == {"field": "body"}


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"", b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":NaN}}', b"\xff"],
)
def test_a_body_that_does_not_parse_gets_the_json_rpc_parse_error(client, planted, raw):
    response = client.post(
        "/mcp",
        content=raw,
        headers=bearer(planted.ines_rw.access) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == -32700


# Versions and initialize ------------------------------------------------------------------------


def _initialize(client, token: str, version, **headers):
    params = {"capabilities": {}, "clientInfo": {"name": "lumen-desk", "version": "1"}}
    if version is not None:
        params["protocolVersion"] = version
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    return rpc(client, token, body, **headers)


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_initialize_echoes_a_supported_version(client, planted, version):
    response = _initialize(client, planted.ines_rw.access, version)
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "grove", "version": "0.1.0"},
            "instructions": INSTRUCTIONS,
        },
    }


@pytest.mark.parametrize("version", ["1999-01-01", "2026-01-01", "", 20250618, None])
def test_initialize_answers_the_newest_version_for_any_other(client, planted, version):
    response = _initialize(client, planted.ines_rw.access, version)
    assert response.json()["result"]["protocolVersion"] == LATEST_VERSION == "2025-11-25"


def test_initialize_gives_a_new_session_id_that_no_later_request_needs(client, planted):
    token = planted.ines_rw.access
    first = _initialize(client, token, "2025-06-18").headers["mcp-session-id"]
    second = _initialize(client, token, "2025-06-18").headers["mcp-session-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second
    later = rpc(client, token, PING)
    assert later.status_code == 200
    assert "mcp-session-id" not in later.headers
    unknown = rpc(client, token, PING, **{"Mcp-Session-Id": "never-issued"})
    assert unknown.status_code == 200


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_a_supported_protocol_version_header_passes(client, planted, version):
    response = rpc(client, planted.ines_rw.access, PING, **{"MCP-Protocol-Version": version})
    assert response.status_code == 200


@pytest.mark.parametrize("version", ["2099-01-01", "latest", ""])
def test_an_unsupported_protocol_version_header_gets_400(client, planted, version, calls):
    body = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "whoami"}}
    response = rpc(client, planted.ines_rw.access, body, **{"MCP-Protocol-Version": version})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert response.json()["id"] is None
    assert calls.seen == []


# Methods ----------------------------------------------------------------------------------------


def test_ping_answers_an_empty_result(client, planted):
    response = rpc(
        client, planted.ines_rw.access, {"jsonrpc": "2.0", "id": "p-1", "method": "ping"}
    )
    assert response.json() == {"jsonrpc": "2.0", "id": "p-1", "result": {}}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"


def test_tools_list_names_every_registered_tool_with_no_cursor(client, planted, tools):
    response = rpc(
        client, planted.ines_read.access, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {
                    "name": tool.name,
                    "title": tool.title,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": tool.annotations,
                }
                for tool in tools
            ]
        },
    }
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert names == ["whoami", "own_titles", "jot", "erase", "refuse", "store_down"]


def test_the_registry_is_read_at_request_time(client, app, planted, tools):
    app.state.mcp_tools = tools[:1]
    response = rpc(
        client, planted.ines_rw.access, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["whoami"]
    gone = call_tool(client, planted.ines_rw.access, "jot", {"text": "a line"})
    assert gone.json()["error"]["code"] == -32602


@pytest.mark.parametrize("method", ["resources/list", "prompts/list"])
def test_resources_and_prompts_are_empty_lists(client, planted, method):
    response = rpc(client, planted.ines_rw.access, {"jsonrpc": "2.0", "id": 3, "method": method})
    assert response.json() == {"jsonrpc": "2.0", "id": 3, "result": {method.split("/")[0]: []}}


@pytest.mark.parametrize("method", ["sampling/createMessage", "resources/read", "tools/run", ""])
def test_another_method_gets_method_not_found(client, planted, method):
    response = rpc(client, planted.ines_rw.access, {"jsonrpc": "2.0", "id": 5, "method": method})
    assert response.status_code == 200
    assert response.json()["id"] == 5
    assert response.json()["error"]["code"] == -32601


@pytest.mark.parametrize("params", [[], "text", 3])
def test_params_that_are_no_object_get_invalid_params(client, planted, params):
    body = {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": params}
    response = rpc(client, planted.ines_rw.access, body)
    assert response.json()["error"]["code"] == -32602


# tools/call -------------------------------------------------------------------------------------


def test_a_tool_runs_with_the_token_s_world_person_host_and_scopes(client, planted, calls):
    issued = planted.ines_rw
    response = call_tool(client, issued.access, "whoami", {"say": "hello"})
    assert response.status_code == 200
    result = response.json()["result"]
    expected = {
        "world_id": planted.ines.world_id,
        "user_id": planted.ines.user_id,
        "client_id": planted.client_id,
        "grant_id": issued.grant_id,
        "scopes": ["grove.read", "grove.write"],
        "arguments": {"say": "hello"},
    }
    assert result == {
        "content": [{"type": "text", "text": json.dumps(expected, ensure_ascii=False)}],
        "structuredContent": expected,
        "isError": False,
    }
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    [(name, context, arguments)] = calls.seen
    assert (name, arguments) == ("whoami", {"say": "hello"})
    assert context.world_id == planted.ines.world_id
    assert context.scopes == frozenset(READ_WRITE)


def test_a_tool_of_world_a_never_sees_world_b(client, planted, calls):
    ines = call_tool(client, planted.ines_rw.access, "own_titles").json()["result"]
    tomas = call_tool(client, planted.tomas_rw.access, "own_titles").json()["result"]
    assert ines["structuredContent"] == {
        "world_id": planted.ines.world_id,
        "titles": [PERSON_TITLE],
    }
    assert tomas["structuredContent"] == {
        "world_id": planted.tomas.world_id,
        "titles": [SECOND_TITLE],
    }
    assert SECOND_TITLE not in ines["content"][0]["text"]
    assert PERSON_TITLE not in tomas["content"][0]["text"]
    assert [context.world_id for _, context, _ in calls.seen] == [
        planted.ines.world_id,
        planted.tomas.world_id,
    ]


def test_arguments_may_be_absent(client, planted, calls):
    response = call_tool(client, planted.ines_rw.access, "whoami")
    assert response.json()["result"]["structuredContent"]["arguments"] == {}
    assert calls.names() == ["whoami"]


@pytest.mark.parametrize("arguments", [[], "text", 5, None, True])
def test_arguments_that_are_no_object_get_invalid_params(client, planted, calls, arguments):
    body = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "whoami", "arguments": arguments},
    }
    response = rpc(client, planted.ines_rw.access, body)
    assert response.status_code == 200
    assert response.json()["id"] == 8
    assert response.json()["error"]["code"] == -32602
    assert calls.seen == []


@pytest.mark.parametrize("name", ["nothing_here", "", 5, None])
def test_an_unknown_tool_gets_invalid_params(client, planted, calls, name):
    response = call_tool(client, planted.ines_rw.access, name, {})
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602
    assert calls.seen == []


def test_a_typed_refusal_of_a_tool_is_a_tool_error(client, planted):
    result = call_tool(client, planted.ines_rw.access, "refuse").json()["result"]
    refusal = not_found("no such item").body()
    assert result == {
        "content": [{"type": "text", "text": json.dumps(refusal)}],
        "structuredContent": refusal,
        "isError": True,
    }


def test_a_store_that_is_down_during_a_tool_is_a_retryable_tool_error(client, planted):
    result = call_tool(client, planted.ines_rw.access, "store_down").json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "unavailable"
    assert result["structuredContent"]["error"]["retryable"] is True


@pytest.mark.parametrize(
    ("issued", "tool", "scope"),
    [("ines_read", "jot", "grove.write"), ("ines_rw", "erase", "grove.sensitive_write")],
)
def test_a_tool_outside_the_token_s_scopes_gets_403_insufficient_scope(
    client, planted, calls, issued, tool, scope
):
    response = call_tool(client, getattr(planted, issued).access, tool, {})
    assert response.status_code == 403
    assert response.headers["www-authenticate"] == (
        f'Bearer error="insufficient_scope", scope="{scope}", resource_metadata="{METADATA}"'
    )
    assert response.json() == {
        "error": {
            "code": "scope_denied",
            "message": "the access token does not hold the scope this tool needs",
            "details": {"tool": tool, "scope": scope},
            "retryable": False,
        }
    }
    assert calls.seen == []


def test_a_token_with_the_scope_runs_the_tool(client, planted, calls):
    assert call_tool(client, planted.ines_read.access, "whoami").status_code == 200
    jotted = call_tool(client, planted.ines_rw.access, "jot", {"text": "a line"})
    assert jotted.json()["result"]["structuredContent"] == {"jotted": "a line"}
    erased = call_tool(client, planted.ines_all.access, "erase", {})
    assert erased.json()["result"]["structuredContent"] == {"erased": True}
    assert calls.names() == ["whoami", "jot", "erase"]


def test_a_batch_with_one_denied_call_runs_nothing(client, planted, calls):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "jot"}},
    ]
    response = rpc(client, planted.ines_read.access, batch)
    assert response.status_code == 403
    assert typed(response)["code"] == "scope_denied"
    assert calls.seen == []


# Notifications, responses and batches -----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}},
        {"jsonrpc": "2.0", "id": 9, "result": {}},
        {"jsonrpc": "2.0", "id": "s-1", "error": {"code": -1, "message": "declined"}},
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 9, "result": {}},
        ],
    ],
)
def test_notifications_and_client_responses_get_202_with_no_body(client, planted, body):
    response = rpc(client, planted.ines_rw.access, body)
    assert response.status_code == 202
    assert response.content == b""


def test_a_tool_call_sent_as_a_notification_runs_nothing(client, planted, calls):
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "jot", "arguments": {}}}
    response = rpc(client, planted.ines_read.access, body)
    assert response.status_code == 202
    assert calls.seen == []


def test_a_batch_answers_each_request_in_order_and_skips_the_rest(client, planted):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "two", "method": "tools/call", "params": {"name": "whoami"}},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "no/such"},
    ]
    response = rpc(client, planted.ines_rw.access, batch)
    assert response.status_code == 200
    body = response.json()
    assert [message["id"] for message in body] == [1, "two", 4]
    assert body[0]["result"] == {}
    assert body[1]["result"]["structuredContent"]["world_id"] == planted.ines.world_id
    assert body[2]["error"]["code"] == -32601
    assert "mcp-session-id" not in response.headers


def test_a_batch_with_initialize_carries_a_session_id(client, planted):
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    response = rpc(client, planted.ines_rw.access, batch)
    assert response.json()[0]["result"]["protocolVersion"] == "2025-03-26"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["mcp-session-id"])


def test_an_empty_batch_is_an_invalid_request(client, planted):
    response = rpc(client, planted.ines_rw.access, [])
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "a batch must not be empty"},
    }


def test_invalid_members_of_a_batch_get_their_own_errors(client, planted):
    batch = [
        1,
        {"jsonrpc": "1.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    response = rpc(client, planted.ines_rw.access, batch)
    assert response.status_code == 200
    body = response.json()
    assert [(message["id"], message.get("error", {}).get("code")) for message in body] == [
        (None, -32600),
        (2, -32600),
        (3, None),
    ]


@pytest.mark.parametrize(
    "body",
    [
        42,
        "ping",
        None,
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": {"n": 1}, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "x"}},
        {"jsonrpc": "2.0", "result": {}},
    ],
)
def test_one_invalid_message_gets_400_invalid_request(client, planted, calls, body):
    # json=None would send no body at all, so the JSON text goes as content.
    response = client.post(
        "/mcp",
        content=json.dumps(body).encode(),
        headers=bearer(planted.ines_rw.access) | {"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["jsonrpc"] == "2.0"
    assert response.json()["error"]["code"] == -32600
    assert calls.seen == []


def test_an_invalid_message_echoes_a_valid_id(client, planted):
    response = rpc(
        client, planted.ines_rw.access, {"jsonrpc": "1.0", "id": "m-1", "method": "ping"}
    )
    assert response.json()["id"] == "m-1"


# The registry -----------------------------------------------------------------------------------


def test_two_tools_with_one_name_are_refused(tools):
    with pytest.raises(ValueError, match="whoami"):
        index_tools((*tools, tools[0]))


def test_the_default_registry_is_empty_until_the_tools_lane(driver, planted):
    assert DEFAULT_TOOLS == ()
    default = TestClient(
        create_app(ApiSettings(public_base_url=BASE), driver=driver), base_url=BASE
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = rpc(default, planted.ines_rw.access, body)
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    refused = call_tool(default, planted.ines_rw.access, "whoami")
    assert refused.json()["error"]["code"] == -32602
