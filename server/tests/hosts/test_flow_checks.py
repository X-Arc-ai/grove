"""The synthetic connector's 14 checks, green against the real server: one test per check.

One module-scoped run (tests/hosts/flow.py) serves create_app(...) under uvicorn on a free
loopback port, with ApiSettings naming that origin, a consent template that holds the two marks in
a temporary GROVE_WEB_DIST, and the synthetic tool set (tests/hosts/synthetic_tools.py) over a
belief list per world. Two signed-in people, each in a world of their own, come from HostsPlant;
the run's worlds, grants, codes, tokens and its registered client are purged when the module ends.

Each test asserts the original's condition for its check, then what this server pins beyond it.
The last test runs the flow for the second person and shows that their token reaches their own
world's beliefs only.
"""

import re
from dataclasses import dataclass, field

import pytest

from grove.api.app import create_app
from grove.api.config import SCOPES, WEB_DIST_ENV, ApiSettings
from grove.mcp import tools as registry
from grove.oauth.store import find_client
from grove.writer.oauth import DYNAMIC
from tests.hosts import flow
from tests.hosts.conftest import PERSON_TITLE, SECOND_TITLE, HostsPlant
from tests.hosts.synthetic_tools import (
    KESTREL_BELIEFS,
    KESTREL_PREFIX,
    MARLOW_BELIEFS,
    MARLOW_PREFIX,
    TOOL_NAMES,
    BeliefBook,
    synthetic_tools,
)

TEMPLATE = (
    "<!doctype html><html><head><title><!--grove:title--></title></head>"
    "<body><main><!--grove:consent--></main></body></html>"
)
DEFAULT_SCOPE = "grove.read grove.write"
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Live:
    """The run. No field that holds a token prints, so a failure report shows none."""

    base: str
    first_world: str
    second_world: str
    second_token: str = field(repr=False)
    book: BeliefBook = field(repr=False)
    result: flow.FlowResult = field(repr=False)

    def check(self, name: str) -> flow.Check:
        found = self.result.check(name)
        assert found.passed, f"{name}: {found.detail}"
        return found


@pytest.fixture(scope="module")
def live(driver, migrated, tmp_path_factory):
    plant = HostsPlant(driver)
    book = BeliefBook()
    dist = tmp_path_factory.mktemp("flow-dist")
    (dist / "consent.html").write_text(TEMPLATE, encoding="utf-8")
    try:
        first = plant.person(PERSON_TITLE)
        second = plant.person(SECOND_TITLE)
        book.seed(first.world_id, KESTREL_PREFIX, KESTREL_BELIEFS)
        book.seed(second.world_id, MARLOW_PREFIX, MARLOW_BELIEFS)
        port = flow.free_port()
        base = f"http://{flow.HOST}:{port}"
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(WEB_DIST_ENV, str(dist))
            app = create_app(
                ApiSettings(public_base_url=base), driver=driver, mcp_tools=synthetic_tools(book)
            )
            with flow.serving(app, port) as origin:
                assert origin == base
                result = flow.run_checks(base, first.token, track_client=plant.track_client)
                yield Live(base, first.world_id, second.world_id, second.token, book, result)
    finally:
        plant.purge()


def _result_of(reply: flow.Reply) -> dict:
    answer = reply.json()
    assert answer["jsonrpc"] == "2.0"
    return answer["result"]


def test_unauthenticated_mcp_returns_401_with_resource_metadata(live):
    reply = live.check(flow.UNAUTHENTICATED).reply()
    assert reply.header("WWW-Authenticate") == (
        f'Bearer resource_metadata="{live.base}/.well-known/oauth-protected-resource", '
        f'scope="{DEFAULT_SCOPE}"'
    )
    assert reply.json()["error"]["code"] == "unauthenticated"
    assert reply.header("Cache-Control") == "no-store"


def test_protected_resource_metadata(live):
    reply = live.check(flow.RESOURCE_METADATA).reply()
    assert reply.path == "/.well-known/oauth-protected-resource"
    assert reply.json() == {
        "resource": f"{live.base}/mcp",
        "authorization_servers": [live.base],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Grove",
    }
    assert reply.header("Access-Control-Allow-Origin") == "*"


def test_authorization_server_metadata_s256_registration(live):
    asm = live.check(flow.SERVER_METADATA).reply().json()
    assert asm["issuer"] == live.base
    assert asm["code_challenge_methods_supported"] == ["S256"]
    for name, path in (
        ("authorization_endpoint", "/authorize"),
        ("token_endpoint", "/token"),
        ("registration_endpoint", "/register"),
        ("revocation_endpoint", "/revoke"),
    ):
        assert asm[name] == live.base + path, name
    assert asm["client_id_metadata_document_supported"] is True
    assert asm["authorization_response_iss_parameter_supported"] is True


def test_dynamic_client_registration_public_client_foreign_scopes(live, driver):
    reply = live.check(flow.REGISTRATION).reply()
    client = reply.json()
    assert flow.is_uuid7(client["client_id"])
    assert not client["client_id"].startswith("grove-")
    assert client["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in client
    assert client["redirect_uris"] == [flow.REDIRECT_URI]
    assert client["client_name"] == flow.CLIENT_NAME
    # The foreign scopes are not an error: the client may ask for every scope Grove has.
    assert client["scope"] == " ".join(SCOPES)
    stored = find_client(driver, client["client_id"])
    assert stored is not None and stored.registration_type == DYNAMIC
    assert stored.secret_hash is None


def test_consent_page_renders(live):
    reply = live.check(flow.CONSENT).reply()
    page = reply.body
    assert reply.header("Content-Type").startswith("text/html")
    assert reply.header("X-Frame-Options") == "DENY"
    assert reply.header("Content-Security-Policy") == "frame-ancestors 'none'"
    assert f"<q>{flow.CLIENT_NAME}</q>" in page
    assert f"Signed in as <strong>{PERSON_TITLE}</strong>" in page
    assert "Read what Grove knows" in page and "Add to Grove" in page
    assert "Forget things in Grove" not in page, "sensitive_write was granted with no ask"
    fields = flow.form_fields(page)
    # The foreign scopes are dropped and the default is asked in their place.
    assert fields["scope"] == DEFAULT_SCOPE
    assert fields["resource"] == f"{live.base}/mcp"
    assert fields["redirect_uri"] == flow.REDIRECT_URI
    assert _HEX64.fullmatch(fields["csrf"])


def test_approve_redirects_with_code_state_iss(live):
    reply = live.check(flow.APPROVE).reply()
    location = reply.header("Location")
    assert location.startswith(flow.REDIRECT_URI + "?")
    back = flow.query_of(location)
    assert back["code"][:4] == "gvc_"
    assert set(back) == {"code", "state", "iss"}


def test_wrong_pkce_verifier_is_rejected(live):
    reply = live.check(flow.WRONG_VERIFIER).reply()
    body = reply.json()
    assert body["error"] == "invalid_grant"
    assert set(body) == {"error", "error_description"}


def test_token_exchange_with_pkce(live):
    reply = live.check(flow.EXCHANGE).reply()
    tokens = reply.json()
    assert tokens["access_token"][:4] == "gva_"
    assert tokens["refresh_token"][:4] == "gvr_"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == DEFAULT_SCOPE
    assert reply.header("Cache-Control") == "no-store"
    assert reply.header("Pragma") == "no-cache"


def test_mcp_initialize(live):
    reply = live.check(flow.INITIALIZE).reply()
    result = _result_of(reply)
    assert result["serverInfo"] == {"name": "grove", "version": "0.1.0"}
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["instructions"] == registry.INSTRUCTIONS
    assert _HEX32.fullmatch(reply.header("Mcp-Session-Id"))


def test_initialized_notification_202(live):
    reply = live.check(flow.INITIALIZED).reply()
    assert reply.body == ""


def test_tools_list(live):
    tools = {
        tool["name"]: tool for tool in _result_of(live.check(flow.TOOLS_LIST).reply())["tools"]
    }
    assert tuple(tools) == TOOL_NAMES
    assert tools["grove_recall"]["annotations"]["readOnlyHint"] is True
    assert tools["grove_as_of"]["annotations"]["readOnlyHint"] is True
    assert tools["grove_retain"]["annotations"]["readOnlyHint"] is False
    for tool in tools.values():
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}
        assert tool["inputSchema"]["type"] == "object"


def test_grove_as_of_2026_10_13_true_55_usd_grove_did_not_know_yet(live):
    result = _result_of(live.check(flow.AS_OF).reply())
    assert result["isError"] is False
    found = result["structuredContent"]
    assert [belief["statement"] for belief in found["true_on_date"]] == [
        "Kestrel Studio costs 55 USD per month."
    ]
    assert found["true_on_date"][0]["recorded_at"] == "2026-10-15"


def test_grove_retain_closes_the_old_launch_belief(live):
    found = _result_of(live.check(flow.RETAIN).reply())["structuredContent"]
    assert found["stored"]["id"] == "b5"
    assert found["stored"]["valid_from"] == "2026-10-26"
    held = {belief["id"]: belief for belief in live.book.beliefs(live.first_world)}
    assert held["b3"]["valid_to"] == "2026-10-26", "the old belief is closed, not deleted"
    assert held["b5"]["valid_to"] is None


def test_refresh_token_grant(live):
    refreshed, ping = live.check(flow.REFRESH).replies
    tokens = refreshed.json()
    first = live.result.values["tokens"]
    # Compared to a bool first, so a failure prints no token.
    new_access = tokens["access_token"] != first["access_token"]
    new_refresh = tokens["refresh_token"] != first["refresh_token"]
    assert new_access and new_refresh, "the refresh gave back the old tokens"
    assert tokens["scope"] == DEFAULT_SCOPE
    assert ping.status == 200 and _result_of(ping) == {}


def test_the_flow_tools_never_see_another_world(live):
    """The same client, a second person: their token reaches the second world's beliefs only."""
    client_id = live.result.values["client_id"]
    first_token = live.result.values["refreshed"]["access_token"]
    first_before = live.book.beliefs(live.first_world)
    calls_before = len(live.book.calls)

    second = flow.connect(live.base, live.second_token, client_id)
    token = second["access_token"]
    differs = token != first_token
    assert differs, "the second person got the first person's token"

    recalled = flow.tool_call(live.base, token, 1, "grove_recall", {"query": "pricing"})
    beliefs = _result_of(recalled)["structuredContent"]["beliefs"]
    assert [belief["id"] for belief in beliefs] == ["m1"]

    dated = flow.tool_call(
        live.base, token, 2, "grove_as_of", {"topic": "pricing", "date": "2026-10-13"}
    )
    found = _result_of(dated)["structuredContent"]
    assert [belief["id"] for belief in found["true_on_date"]] == ["m1"]
    assert [belief["id"] for belief in found["known_by_grove_on_date"]] == ["m1"]

    retained = flow.tool_call(
        live.base,
        token,
        3,
        "grove_retain",
        {
            "topic": "launch",
            "statement": "The pear cider waits for spring.",
            "valid_from": "2026-11-01",
        },
    )
    assert _result_of(retained)["structuredContent"]["closed"] == ["m2"]

    # The first world is as it was, and its own token still sees its own launch only.
    assert live.book.beliefs(live.first_world) == first_before
    theirs = flow.tool_call(live.base, first_token, 4, "grove_recall", {"query": "launch"})
    statements = [
        belief["statement"] for belief in _result_of(theirs)["structuredContent"]["beliefs"]
    ]
    assert statements == ["Kestrel Teams launch moved to 2026-12-01."]

    worlds = [call.world_id for call in live.book.calls[calls_before:]]
    assert worlds == [live.second_world] * 3 + [live.first_world]
