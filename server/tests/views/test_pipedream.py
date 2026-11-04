"""Controlled Pipedream transport; no real account, credential or provider call."""

import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api import pipedream
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.session import current_session
from grove.views.session import SESSION_COOKIE, ViewSession, token_hash
from grove.writer.accounts import create_session, email_hash, ensure_demo_account
from grove.writer.testing import purge_test_account, purge_test_world
from tests.auth.conftest import Accounts

TEST_APPS = {
    **pipedream.PROVIDERS,
    "slack": "Slack",
    "google_drive": "Google Drive",
    "microsoft_outlook": "Microsoft Outlook",
    "linear": "Linear",
    "dropbox": "Dropbox",
}


def page(rows, *, total=None, cursor=None):
    return {
        "data": rows,
        "page_info": {
            "count": len(rows),
            "total_count": len(rows) if total is None else total,
            "end_cursor": cursor,
        },
    }


def app_row(slug):
    return {"name_slug": slug, "name": TEST_APPS[slug], "auth_type": "oauth"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(pipedream, "is_demo_world", lambda driver, world_id: False)
    for key, value in {
        "PROJECT_ID": "proj_test",
        "CLIENT_ID": "test-client",
        "CLIENT_SECRET": "test-secret",
        "ENVIRONMENT": "production",
    }.items():
        monkeypatch.setenv("PIPEDREAM_" + key, value)
    session = ViewSession("user-one", "t_views-b2_one", None)
    calls = []
    responses = {key: [] for key in TEST_APPS}

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
        if request.url.path == "/v1/connect/apps":
            query = request.url.params.get("q", "").lower()
            return httpx.Response(
                200,
                json=page([app_row(key) for key in TEST_APPS if query in TEST_APPS[key].lower()]),
            )
        if request.url.path.startswith("/v1/connect/apps/"):
            key = request.url.path.rsplit("/", 1)[1]
            return (
                httpx.Response(200, json={"data": app_row(key)})
                if key in TEST_APPS
                else httpx.Response(404)
            )
        if request.method == "DELETE":
            account_id = request.url.path.rsplit("/", 1)[1]
            for key, items in responses.items():
                if isinstance(items, list):
                    responses[key] = [row for row in items if row.get("id") != account_id]
            return httpx.Response(204)
        assert request.url.path == "/v1/connect/proj_test/accounts"
        assert request.url.params["external_user_id"] == pipedream.external_user_id(session)
        assert request.url.params["include_credentials"] == "false"
        rows = responses[request.url.params["app"]]
        return httpx.Response(200, json=page(rows) if isinstance(rows, list) else rows)

    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as client:
            yield client

    app = create_app(ApiSettings("https://grove.example"), driver=object())
    app.dependency_overrides[current_session] = lambda: session
    app.dependency_overrides[pipedream.http_client] = http
    client = TestClient(app, headers={"Origin": "https://grove.example"})
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


def test_documented_account_list_contract(setup):
    """The real list route is project-scoped; the owner is a required query filter."""
    client, app, session, _, _ = setup
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path != "/v1/connect/proj_test/accounts":
            return httpx.Response(404, json={"error": "Not Found"})
        assert request.url.params["external_user_id"] == pipedream.external_user_id(session)
        assert request.url.params["include_credentials"] == "false"
        return httpx.Response(
            200,
            json={"data": [], "page_info": {"count": 0, "total_count": 0, "end_cursor": None}},
        )

    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as upstream:
            yield upstream

    app.dependency_overrides[pipedream.http_client] = http
    response = client.get(pipedream.PREFIX + "/accounts")
    assert response.status_code == 200
    assert {row["status"] for row in response.json()["providers"]} == {"not_connected"}
    assert len(calls) == 4


def test_nonproduction_is_unavailable(setup, monkeypatch):
    client, _, _, calls, _ = setup
    monkeypatch.setenv("PIPEDREAM_ENVIRONMENT", "development")
    assert client.get(pipedream.PREFIX + "/accounts").json()["configured"] is False
    assert calls == []


@pytest.mark.parametrize("provider", TEST_APPS)
def test_connect_binds_owner_environment_app_and_ttl(setup, provider):
    client, _, session, calls, _ = setup
    response = client.post(pipedream.PREFIX + f"/{provider}/connect", json={})
    assert response.status_code == 200
    assert response.json()["account"] == session.user_id
    assert response.json()["connect_url"].endswith("app=" + provider)
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 3
    assert calls[1].url.path == "/v1/connect/apps/" + provider
    sent = json.loads(calls[2].content)
    assert sent["external_user_id"] == pipedream.external_user_id(session)
    assert session.user_id not in sent["external_user_id"]
    assert sent["app_id"] == provider
    assert sent["expires_in"] == 600
    assert sent["allowed_origins"] == ["https://grove.example"]
    assert calls[2].headers["x-pd-environment"] == "production"
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
    response = client.get(pipedream.PREFIX + "/accounts")
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
    # A different authenticated user derives a different filter, independent of World selection.
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
    assert client.post(pipedream.PREFIX + "/gmail/connect?user=other", json={}).status_code == 400
    assert calls == []
    assert client.post(pipedream.PREFIX + "/unknown_app/connect", json={}).status_code == 400
    assert all(not request.url.path.endswith("/tokens") for request in calls)


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


def test_persisted_designation_refuses_status_and_connect_without_runtime_config(
    driver, migrated, monkeypatch
):
    made = Accounts()
    hashed = email_hash(made.address())
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
    calls = []

    def transport(request):
        calls.append(request)
        raise AssertionError("A designated world must not call Pipedream")

    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as upstream:
            yield upstream

    try:
        account = ensure_demo_account(driver, hashed, world_id_for=made.world_id_for)
        token = "pipedream-test-" + uuid4().hex
        create_session(
            driver,
            account.user_id,
            token_hash(token),
            method="email",
            expires_at=clock.now() + timedelta(minutes=5),
        )
        # Persisted designation must still refuse after runtime demo settings are removed.
        monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH")
        monkeypatch.delenv("GROVE_DEMO_GUARD_FILE", raising=False)
        for key, value in {
            "PROJECT_ID": "proj_test",
            "CLIENT_ID": "test-client",
            "CLIENT_SECRET": "test-secret",
            "ENVIRONMENT": "production",
        }.items():
            monkeypatch.setenv("PIPEDREAM_" + key, value)
        app = create_app(ApiSettings("https://grove.example"), driver=driver)
        app.dependency_overrides[pipedream.http_client] = http
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/v1/views/session").json()["account"] == account.user_id
        response = client.get(pipedream.PREFIX + "/accounts")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "scope_denied"
        for provider in pipedream.PROVIDERS:
            response = client.post(pipedream.PREFIX + f"/{provider}/connect", json={})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "scope_denied"
        assert calls == []
    finally:
        for world in made.worlds:
            purge_test_world(driver, world)
        for value in made.hashes:
            purge_test_account(driver, value)


def override_transport(app, transport):
    async def http():
        async with httpx.AsyncClient(
            base_url=pipedream.API_BASE, transport=httpx.MockTransport(transport)
        ) as upstream:
            yield upstream

    app.dependency_overrides[pipedream.http_client] = http


def test_catalog_search_is_bounded_and_returns_only_public_fields(setup):
    client, _, session, calls, _ = setup
    response = client.get(pipedream.PREFIX + "/apps?q=slack")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "account": session.user_id,
        "configured": True,
        "apps": [{"provider": "slack", "name": "Slack", "auth_type": "oauth", "connectable": True}],
        "next_cursor": None,
        "total": 1,
    }
    assert calls[-1].url.path == "/v1/connect/apps"
    assert dict(calls[-1].url.params) == {
        "q": "slack",
        "limit": "30",
        "sort_key": "name",
        "sort_direction": "asc",
    }


@pytest.mark.parametrize(
    "query", ["external_user_id=foreign", "q=x&q=y", "limit=100000", "q=" + "x" * 129]
)
def test_catalog_rejects_identity_and_unbounded_inputs_before_upstream(setup, query):
    client, _, _, calls, _ = setup
    assert client.get(pipedream.PREFIX + "/apps?" + query).status_code == 400
    assert calls == []


def test_account_status_rejects_browser_owner_selection(setup):
    client, _, _, calls, _ = setup
    assert client.get(pipedream.PREFIX + "/accounts?external_user_id=foreign").status_code == 400
    assert calls == []


def test_selected_catalog_provider_status_uses_metadata_and_owner_filter(setup):
    client, _, session, calls, responses = setup
    responses["slack"] = [{"app": {"name_slug": "slack"}, "healthy": True, "dead": False}]
    response = client.get(pipedream.PREFIX + "/accounts?provider=slack")
    assert response.status_code == 200
    assert response.json()["providers"] == [
        {"provider": "slack", "name": "Slack", "status": "connected"}
    ]
    assert calls[1].url.path == "/v1/connect/apps/slack"
    assert calls[2].url.params["external_user_id"] == pipedream.external_user_id(session)
    assert calls[2].url.params["app"] == "slack"


@pytest.mark.parametrize(
    "bad", [[], {"data": []}, page([], total=True), page([], total=-1), page([{}], total=0)]
)
def test_account_response_shape_is_fail_closed(setup, bad):
    client, _, _, _, responses = setup
    responses["gmail"] = bad if isinstance(bad, dict) else {"not_data": bad}
    assert client.get(pipedream.PREFIX + "/accounts").status_code == 503


def test_real_account_pagination_keeps_owner_and_accepts_final_null_cursor(setup):
    client, app, session, _, _ = setup
    account_calls = []

    def transport(request):
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path.startswith("/v1/connect/apps/"):
            return httpx.Response(200, json={"data": app_row("slack")})
        account_calls.append(request)
        assert request.url.params["external_user_id"] == pipedream.external_user_id(session)
        assert request.url.params["app"] == "slack"
        row = {"app": {"name_slug": "slack"}, "healthy": False, "dead": False}
        if request.url.params.get("after") is None:
            return httpx.Response(200, json=page([row], total=2, cursor="next-page"))
        assert request.url.params["after"] == "next-page"
        return httpx.Response(200, json=page([{**row, "healthy": True}], total=2))

    override_transport(app, transport)
    response = client.get(pipedream.PREFIX + "/accounts?provider=slack")
    assert response.status_code == 200
    assert response.json()["providers"][0]["status"] == "connected"
    assert len(account_calls) == 2


def test_incomplete_account_page_never_reports_not_connected(setup):
    client, _, _, _, responses = setup
    responses["gmail"] = page([], total=1)
    assert client.get(pipedream.PREFIX + "/accounts").status_code == 503


def test_repeated_account_cursor_is_bounded(setup):
    client, _, _, calls, responses = setup
    responses["gmail"] = page([{"app": {"name_slug": "gmail"}}], total=10, cursor="repeat")
    assert client.get(pipedream.PREFIX + "/accounts").status_code == 503
    assert len(calls) == 3


def test_catalog_cursor_and_final_page(setup):
    client, app, _, _, _ = setup
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        assert request.url.params["after"] == "opaque-page"
        return httpx.Response(200, json=page([app_row("slack")], total=100, cursor=None))

    override_transport(app, transport)
    response = client.get(pipedream.PREFIX + "/apps?cursor=opaque-page")
    assert response.status_code == 200
    assert response.json()["next_cursor"] is None
    assert len(calls) == 2


def test_unconnectable_app_never_mints_token(setup):
    client, app, _, _, _ = setup
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        return httpx.Response(200, json={"data": {**app_row("slack"), "auth_type": "none"}})

    override_transport(app, transport)
    assert client.post(pipedream.PREFIX + "/slack/connect", json={}).status_code == 400
    assert len(calls) == 2


def test_metadata_slug_mismatch_never_mints_token(setup):
    client, app, _, _, _ = setup
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        return httpx.Response(200, json={"data": app_row("notion")})

    override_transport(app, transport)
    assert client.post(pipedream.PREFIX + "/slack/connect", json={}).status_code == 400
    assert len(calls) == 2


def test_confirmed_disconnect_only_uses_owned_provider_accounts(setup):
    client, _, session, calls, responses = setup
    responses["slack"] = [{"id": "apn_owned", "app": {"name_slug": "slack"}, "healthy": True}]
    responses["gmail"] = [{"id": "apn_otherapp", "app": {"name_slug": "gmail"}, "healthy": True}]
    response = client.post(pipedream.PREFIX + "/slack/disconnect", json={"confirm": True})
    assert response.status_code == 200
    assert response.json() == {
        "account": session.user_id,
        "provider": "slack",
        "status": "not_connected",
        "disconnected": 1,
    }
    deletes = [request for request in calls if request.method == "DELETE"]
    assert [request.url.path for request in deletes] == ["/v1/connect/proj_test/accounts/apn_owned"]
    reads = [request for request in calls if request.url.path.endswith("/accounts")]
    assert len(reads) == 2
    assert all(
        request.url.params["external_user_id"] == pipedream.external_user_id(session)
        for request in reads
    )
    assert all(request.url.params["include_credentials"] == "false" for request in reads)
    assert responses["gmail"][0]["id"] == "apn_otherapp"
    assert "apn_" not in response.text
    again = client.post(pipedream.PREFIX + "/slack/disconnect", json={"confirm": True})
    assert again.status_code == 200 and again.json()["disconnected"] == 0


@pytest.mark.parametrize(
    "body", [{}, {"confirm": False}, {"confirm": 1}, {"confirm": True, "account_id": "apn_foreign"}]
)
def test_disconnect_requires_exact_confirmation_without_account_selection(setup, body):
    client, _, _, calls, _ = setup
    assert client.post(pipedream.PREFIX + "/slack/disconnect", json=body).status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    "path,method",
    [
        ("/apps", "get"),
        ("/accounts", "get"),
        ("/slack/connect", "post"),
        ("/slack/disconnect", "post"),
    ],
)
def test_all_routes_refuse_demo_and_bearer_before_upstream(setup, monkeypatch, path, method):
    client, _, _, calls, _ = setup
    request = getattr(client, method)
    kwargs = {"json": {"confirm": True}} if method == "post" else {}
    assert (
        request(
            pipedream.PREFIX + path, headers={"Authorization": "Bearer synthetic"}, **kwargs
        ).status_code
        == 401
    )
    monkeypatch.setattr(pipedream, "is_demo_world", lambda driver, world_id: True)
    assert request(pipedream.PREFIX + path, **kwargs).status_code == 403
    assert calls == []


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_mutations_require_same_origin_and_refuse_cross_site_metadata(setup, action):
    client, _, _, calls, _ = setup
    body = {"confirm": True} if action == "disconnect" else {}
    client.headers.pop("origin")
    assert client.post(pipedream.PREFIX + f"/slack/{action}", json=body).status_code == 403
    assert (
        client.post(
            pipedream.PREFIX + f"/slack/{action}",
            json=body,
            headers={"Origin": "https://grove.example", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    assert calls == []


def test_disconnect_failure_never_claims_success(setup):
    client, app, _, _, _ = setup
    deleted = []

    def transport(request):
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path.startswith("/v1/connect/apps/"):
            return httpx.Response(200, json={"data": app_row("slack")})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return (
                httpx.Response(204)
                if len(deleted) == 1
                else httpx.Response(500, text="private-provider-detail")
            )
        return httpx.Response(
            200,
            json=page(
                [
                    {"id": "apn_first", "app": {"name_slug": "slack"}},
                    {"id": "apn_second", "app": {"name_slug": "slack"}},
                ]
            ),
        )

    override_transport(app, transport)
    response = client.post(pipedream.PREFIX + "/slack/disconnect", json={"confirm": True})
    assert response.status_code == 503
    assert len(deleted) == 2
    assert "not_connected" not in response.text and "private-provider-detail" not in response.text


@pytest.mark.parametrize(
    "path,method,body,expected_scope",
    [
        ("/accounts", "get", None, {"connect:accounts:read"}),
        ("/apps", "get", None, {"connect:apps:*"}),
        (
            "/accounts?provider=slack",
            "get",
            None,
            {"connect:accounts:read", "connect:apps:*"},
        ),
        (
            "/slack/connect",
            "post",
            {},
            {"connect:tokens:create", "connect:apps:*"},
        ),
        (
            "/slack/disconnect",
            "post",
            {"confirm": True},
            {"connect:accounts:read", "connect:accounts:write", "connect:apps:*"},
        ),
    ],
)
def test_operations_request_only_the_documented_scopes(setup, path, method, body, expected_scope):
    client, _, _, calls, _ = setup
    response = getattr(client, method)(
        pipedream.PREFIX + path, **({"json": body} if body is not None else {})
    )
    assert response.status_code == 200
    scopes = json.loads(calls[0].content)["scope"].split()
    assert set(scopes) == expected_scope
    if path.endswith("/connect"):
        assert set(json.loads(calls[-1].content)["scope"].split()) == {
            "connect:accounts:read",
            "connect:accounts:write",
            "connect:apps:*",
        }
