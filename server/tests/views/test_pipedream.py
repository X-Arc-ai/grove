"""Controlled Pipedream transport; no real account, credential or provider call."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from grove.api import pipedream
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.session import current_session
from grove.views.session import ViewSession


@pytest.fixture
def setup(monkeypatch):
    for key, value in {
        "PROJECT_ID": "proj_test",
        "CLIENT_ID": "test-client",
        "CLIENT_SECRET": "test-secret",
        "ENVIRONMENT": "production",
    }.items():
        monkeypatch.setenv("PIPEDREAM_" + key, value)
    session = ViewSession("user-one", "t_views-b2_one", None)
    calls = []
    responses = {key: [] for key in pipedream.PROVIDERS}

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path.endswith("/tokens"):
            return httpx.Response(
                200,
                json={
                    "connect_link_url": "https://pipedream.com/_static/connect.html?token=test-connect",
                    "expires_at": "2026-11-04T16:00:00Z",
                    "token": "test-connect",
                },
            )
        assert request.url.path == (
            "/v1/connect/proj_test/users/" + pipedream.external_user_id(session) + "/accounts"
        )
        assert request.url.params["include_credentials"] == "false"
        return httpx.Response(200, json=responses[request.url.params["app"]])

    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as client:
            yield client

    app = create_app(ApiSettings("https://grove.example"), driver=object())
    app.include_router(pipedream.router)
    app.dependency_overrides[current_session] = lambda: session
    app.dependency_overrides[pipedream.http_client] = http
    client = TestClient(app)
    return client, app, session, calls, responses


def test_unconfigured_never_calls_provider(setup, monkeypatch):
    client, _, _, calls, _ = setup
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET")
    response = client.get(pipedream.PREFIX + "/accounts")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["configured"] is False
    assert {row["status"] for row in response.json()["providers"]} == {"unavailable"}
    assert client.post(pipedream.PREFIX + "/gmail/connect", json={}).status_code == 503
    assert calls == []


def test_nonproduction_is_unavailable(setup, monkeypatch):
    client, _, _, calls, _ = setup
    monkeypatch.setenv("PIPEDREAM_ENVIRONMENT", "development")
    assert client.get(pipedream.PREFIX + "/accounts").json()["configured"] is False
    assert calls == []


@pytest.mark.parametrize("provider", pipedream.PROVIDERS)
def test_connect_binds_owner_environment_app_and_ttl(setup, provider):
    client, _, session, calls, _ = setup
    response = client.post(pipedream.PREFIX + f"/{provider}/connect", json={})
    assert response.status_code == 200
    assert response.json()["account"] == session.user_id
    assert response.json()["connect_url"].endswith("app=" + provider)
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 2
    sent = json.loads(calls[1].content)
    assert sent["external_user_id"] == pipedream.external_user_id(session)
    assert session.user_id not in sent["external_user_id"]
    assert sent["app_id"] == provider
    assert sent["expires_in"] == 600
    assert sent["allowed_origins"] == ["https://grove.example"]
    assert calls[1].headers["x-pd-environment"] == "production"
    assert "proxy" not in sent["scope"] and "actions" not in sent["scope"]
    assert "test-secret" not in response.text and "test-access" not in response.text


def test_status_is_owner_scoped_and_never_exposes_account_data(setup):
    client, app, _, calls, responses = setup
    responses["gmail"] = [
        {
            "app": {"name_slug": "gmail"},
            "healthy": True,
            "dead": False,
            "credentials": {"key": "private-test-value"},
            "name": "private-name",
        }
    ]
    responses["notion"] = [{"app": {"name_slug": "notion"}, "healthy": True, "dead": True}]
    response = client.get(pipedream.PREFIX + "/accounts?external_user_id=someone-else&success=true")
    assert response.status_code == 200
    assert [row["status"] for row in response.json()["providers"]] == [
        "connected",
        "not_connected",
        "attention",
    ]
    assert "private" not in response.text
    assert len(calls) == 4
    assert all(request.headers["x-pd-environment"] == "production" for request in calls[1:])
    assert all("someone-else" not in str(request.url) for request in calls)
    # A different authenticated user derives a different path, independent of world selection.
    first = pipedream.external_user_id(ViewSession("user-one", "t_a", None))
    assert first == pipedream.external_user_id(ViewSession("user-one", "t_b", None))
    assert first != pipedream.external_user_id(ViewSession("user-two", "t_a", None))


@pytest.mark.parametrize(
    "body", [{"user_id": "other"}, {"world_id": "other"}, {"account_id": "other"}, [], None]
)
def test_browser_identity_is_refused(setup, body):
    client, _, _, calls, _ = setup
    response = client.post(
        pipedream.PREFIX + "/gmail/connect",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert calls == []


def test_cross_origin_and_unknown_app_are_refused(setup):
    client, _, _, calls, _ = setup
    assert (
        client.post(
            pipedream.PREFIX + "/gmail/connect",
            json={},
            headers={"Origin": "https://other.example"},
        ).status_code
        == 403
    )
    assert client.post(pipedream.PREFIX + "/slack/connect", json={}).status_code == 400
    assert client.post(pipedream.PREFIX + "/gmail/connect?user=other", json={}).status_code == 400
    assert calls == []


def test_signed_out_is_refused_before_provider(setup):
    client, app, _, calls, _ = setup
    from grove.api.errors import unauthenticated

    def signed_out():
        raise unauthenticated()

    app.dependency_overrides[current_session] = signed_out
    assert client.get(pipedream.PREFIX + "/accounts").status_code == 401
    assert client.post(pipedream.PREFIX + "/gmail/connect", json={}).status_code == 401
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "credentials", "shape"])
def test_upstream_failure_never_looks_connected(setup, failure):
    client, app, _, _, _ = setup

    def transport(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-provider-detail")
        if failure == "credentials":
            return httpx.Response(401, text="private-provider-detail")
        return httpx.Response(200, json=[])

    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as upstream:
            yield upstream

    app.dependency_overrides[pipedream.http_client] = http
    response = client.get(pipedream.PREFIX + "/accounts")
    assert response.status_code == 503
    assert "private-provider-detail" not in response.text
    assert "connected" not in response.text


def test_foreign_app_response_is_not_accepted(setup):
    client, _, _, _, responses = setup
    responses["gmail"] = [{"app": {"name_slug": "notion"}, "healthy": True}]
    assert client.get(pipedream.PREFIX + "/accounts").status_code == 503
