"""The person's connections in the views: GET /v1/views/connections and the revoke of one host,
for the person's own world only. A revoked grant's access token no longer passes."""

import pytest

from grove import clock
from grove.ids import new_id
from grove.oauth.secrets import token_hash
from grove.oauth.store import find_access_token
from tests.hosts.conftest import CLIENT_NAME, SECOND_TITLE
from tests.hosts.oauth_kit import (
    MCP_URL,
    browser,
    built_consent_page,
    exchange,
    make_app,
    new_code,
    new_pkce,
)

CONNECTIONS = "/v1/views/connections"


@pytest.fixture
def app(driver, migrated, tmp_path, monkeypatch):
    built_consent_page(tmp_path / "dist", monkeypatch)
    return make_app(driver)


@pytest.fixture
def ines(hosts_plant):
    return hosts_plant.person(seeded=False)


@pytest.fixture
def web(app, ines):
    return browser(app, ines)


def connect(app, web, client_id: str) -> dict:
    """Approve the host in the person's browser and exchange the code: the host's token pair."""
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    response = exchange(browser(app), client_id, code, pkce.verifier)
    assert response.status_code == 200, response.text
    return response.json()


def revoke(web, grant_id: str):
    return web.post(f"{CONNECTIONS}/{grant_id}/revoke", json={})


def test_the_connections_list_the_approved_hosts_newest_first(app, web, hosts_plant):
    assert web.get(CONNECTIONS).json() == {"mcp_url": MCP_URL, "connections": []}
    first = hosts_plant.client()
    second = hosts_plant.client(host="Harbor Notes")
    connect(app, web, first)
    connect(app, web, second)
    response = web.get(CONNECTIONS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["mcp_url"] == MCP_URL
    assert [(c["client_id"], c["host"]) for c in body["connections"]] == [
        (second, "Harbor Notes"),
        (first, CLIENT_NAME),
    ]
    entry = body["connections"][1]
    assert set(entry) == {
        "grant_id",
        "client_id",
        "host",
        "registration_type",
        "scopes",
        "granted_at",
    }
    assert entry["registration_type"] == "dynamic"
    assert entry["scopes"] == ["grove.read", "grove.write"]
    assert entry["granted_at"].endswith("Z")


def test_revoking_a_connection_ends_its_tokens_and_drops_it_from_the_list(
    app, web, hosts_plant, driver
):
    client_id = hosts_plant.client()
    pair = connect(app, web, client_id)
    assert find_access_token(driver, token_hash(pair["access_token"]), clock.now()) is not None
    (connection,) = web.get(CONNECTIONS).json()["connections"]
    response = revoke(web, connection["grant_id"])
    assert response.status_code == 200 and response.json() == {"revoked": True}
    assert find_access_token(driver, token_hash(pair["access_token"]), clock.now()) is None
    assert web.get(CONNECTIONS).json()["connections"] == []
    host = browser(app)
    form = {"grant_type": "refresh_token", "refresh_token": pair["refresh_token"]}
    refused = host.post("/token", data={**form, "client_id": client_id})
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"
    # Approving again gives the host a new, live grant.
    again = connect(app, web, client_id)
    assert find_access_token(driver, token_hash(again["access_token"]), clock.now()) is not None


def test_another_worlds_connection_is_neither_listed_nor_revoked(
    app, web, hosts_plant, plant_person, driver
):
    tomas = plant_person(SECOND_TITLE, seeded=False)
    tomas_web = browser(app, tomas)
    client_id = hosts_plant.client()
    pair = connect(app, tomas_web, client_id)
    (theirs,) = tomas_web.get(CONNECTIONS).json()["connections"]
    assert web.get(CONNECTIONS).json()["connections"] == []
    for grant_id in (theirs["grant_id"], new_id()):
        response = revoke(web, grant_id)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert find_access_token(driver, token_hash(pair["access_token"]), clock.now()) is not None


def test_the_connections_need_a_session_and_the_revoke_a_json_body(app, web, hosts_plant):
    signed_out = browser(app)
    assert signed_out.get(CONNECTIONS).status_code == 401
    assert signed_out.get(CONNECTIONS).json()["error"]["code"] == "unauthenticated"
    assert revoke(signed_out, new_id()).status_code == 401
    connect(app, web, hosts_plant.client())
    (connection,) = web.get(CONNECTIONS).json()["connections"]
    as_form = web.post(f"{CONNECTIONS}/{connection['grant_id']}/revoke", data={"a": "b"})
    assert as_form.status_code == 400
    assert as_form.json()["error"]["code"] == "invalid_arguments"
    assert len(web.get(CONNECTIONS).json()["connections"]) == 1
