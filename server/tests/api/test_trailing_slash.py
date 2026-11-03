"""No path redirects to its other spelling. Starlette would build that redirect from the request,
and uvicorn runs without proxy headers, so it would name plain http: a client that follows it sends
its JSON-RPC body in clear, and Go's client sends the bearer token too. So /mcp/ is served by the
handler of /mcp, and every other trailing slash is the typed 404."""

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from tests.hosts.conftest import HostsPlant
from tests.hosts.oauth_kit import (
    browser,
    built_consent_page,
    exchange,
    new_code,
    new_pkce,
)

BASE = "https://grove.example"
CHALLENGE = (
    f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource", '
    'scope="grove.read grove.write"'
)
INVALID_CHALLENGE = (
    f'Bearer error="invalid_token", resource_metadata="{BASE}/.well-known/oauth-protected-resource"'
)
JSON_TYPE = {"content-type": "application/json"}
PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'


@pytest.fixture
def app(driver, migrated):
    return create_app(ApiSettings(public_base_url=BASE), driver=driver)


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE, follow_redirects=False, raise_server_exceptions=False)


@pytest.fixture
def live_token(driver, make_world, app, tmp_path, monkeypatch):
    """An access token from the real flow."""
    built_consent_page(tmp_path / "dist", monkeypatch)
    plant = HostsPlant(driver, world_id_for=make_world)
    try:
        person = plant.person(seeded=False)
        client_id = plant.client()
        pkce = new_pkce()
        web = browser(app, person)
        code = new_code(web, client_id, pkce)
        tokens = exchange(browser(app), client_id, code, pkce.verifier)
        assert tokens.status_code == 200, tokens.text
        yield tokens.json()["access_token"]
    finally:
        plant.purge()


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
def test_mcp_with_a_slash_and_no_token_is_the_401_with_the_challenge(client, method):
    response = client.request(method, "/mcp/", content=PING, headers=JSON_TYPE)
    assert response.status_code == 401
    assert "location" not in response.headers
    assert response.headers["www-authenticate"] == CHALLENGE
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_mcp_with_a_slash_and_a_dead_token_is_the_401_of_a_dead_token(client, method):
    headers = {**JSON_TYPE, "authorization": "Bearer gva_nothing"}
    response = client.request(method, "/mcp/", content=PING, headers=headers)
    assert response.status_code == 401
    assert "location" not in response.headers
    assert response.headers["www-authenticate"] == INVALID_CHALLENGE


def test_mcp_with_a_slash_and_a_live_token_answers_as_mcp_does(client, live_token):
    headers = {**JSON_TYPE, "authorization": f"Bearer {live_token}"}
    with_slash = client.post("/mcp/", content=PING, headers=headers)
    assert with_slash.status_code == 200
    assert with_slash.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert with_slash.json() == client.post("/mcp", content=PING, headers=headers).json()
    no_stream = client.get("/mcp/", headers=headers)
    assert no_stream.status_code == 405 and "location" not in no_stream.headers
    assert no_stream.headers["allow"] == "POST, DELETE"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("method", ["HEAD", "TRACE"])
def test_a_method_that_is_not_routed_is_405_before_the_bearer_check(client, method, path):
    """docs/hosts.md: HEAD and TRACE never reach the route, so they get no challenge."""
    response = client.request(method, path)
    assert response.status_code == 405
    assert "www-authenticate" not in response.headers
    assert "location" not in response.headers
    if method != "HEAD":
        assert response.json()["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/token/"),
        ("GET", "/token/"),
        ("POST", "/register/"),
        ("POST", "/revoke/"),
        ("GET", "/authorize/"),
        ("POST", "/authorize/decision/"),
        ("GET", "/.well-known/oauth-protected-resource/"),
        ("GET", "/.well-known/oauth-protected-resource/mcp/"),
        ("GET", "/.well-known/oauth-authorization-server/"),
        ("GET", "/.well-known/openid-configuration/"),
        ("GET", "/healthz/"),
        ("GET", "/v1/views/connections/"),
        ("GET", "/v1/views/session/"),
    ],
)
def test_every_other_trailing_slash_is_the_typed_404_and_never_a_redirect(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 404
    assert "location" not in response.headers
    assert response.json()["error"]["code"] == "not_found"


def test_the_app_keeps_redirect_slashes_off(app):
    assert app.router.redirect_slashes is False
