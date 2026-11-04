"""The HTTP identity boundary; the actual approval storage is tested separately."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grove.api import forget as api
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.errors import unauthenticated
from grove.views.session import ViewSession
from grove.writer.errors import InvalidRecord
from grove.writer.forget_approvals import MintedApproval

BASE = "https://grove.example"
TARGET = {"type": "named_value", "value": "Juniper phrase"}


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    state = SimpleNamespace(mints=[], writes=[], session=True, token=None)
    monkeypatch.setenv(api.LEDGER_ENV, str(tmp_path))
    settings = ApiSettings(public_base_url=BASE)

    def session(request):
        if not state.session:
            raise unauthenticated()
        return ViewSession("person", "world", None)

    def mint(driver, world, owner, target):
        state.mints.append((world, owner, target))
        return MintedApproval("id", "gfa_test-token", datetime(2026, 11, 4, tzinfo=UTC))

    def execute(driver, world, target, **options):
        state.writes.append((world, target, options))
        if options["approval_token"] != "gfa_test-token":
            raise InvalidRecord("approval_required", "forget_approval", "private failure detail")
        return {
            "receipt_id": "receipt",
            "counts": {"items": 1},
            "backups_clear_after": "2026-12-04",
        }

    monkeypatch.setattr(api, "current_session", session)
    monkeypatch.setattr(api, "mint_approval", mint)
    monkeypatch.setattr(api, "find_access_token", lambda *args: state.token)
    monkeypatch.setattr(api, "_execute", execute)
    state.client = TestClient(create_app(settings, driver=object()), base_url=BASE)
    return state


def test_person_must_confirm_the_exact_request(boundary):
    client = boundary.client
    for body in (
        {"target": TARGET},
        {"target": TARGET, "confirm": False},
        {"target": TARGET, "confirm": 1},
    ):
        assert client.post("/v1/forget/approve", json=body).status_code == 400
    assert boundary.mints == []
    response = client.post("/v1/forget/approve", json={"target": TARGET, "confirm": True})
    assert response.status_code == 200
    assert response.json()["one_use"] is True
    assert TARGET["value"] not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert boundary.mints == [("world", "person", TARGET)]


def test_host_cannot_mint_person_approval_even_with_session_cookie(boundary):
    response = boundary.client.post(
        "/v1/forget/approve",
        json={"target": TARGET, "confirm": True},
        headers={"Authorization": "Bearer host"},
    )
    assert response.status_code == 403
    assert boundary.mints == []


def test_read_scoped_host_cannot_forget_despite_approval_or_cookie(boundary):
    boundary.token = SimpleNamespace(
        world_id="world", user_id="person", resource=BASE + "/mcp", scopes=("grove.read",)
    )
    response = boundary.client.post(
        "/v1/forget",
        json={"target": TARGET, "approval_token": "gfa_test-token"},
        headers={"Authorization": "Bearer host"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert boundary.writes == []


def test_sensitive_host_still_needs_a_valid_person_approval(boundary):
    boundary.token = SimpleNamespace(
        world_id="host-world",
        user_id="owner",
        resource=BASE + "/mcp",
        scopes=("grove.sensitive_write",),
    )
    response = boundary.client.post(
        "/v1/forget",
        json={"target": TARGET, "approval_token": "invalid"},
        headers={"Authorization": "Bearer host"},
    )
    assert response.status_code == 403
    assert "private failure detail" not in response.text
    response = boundary.client.post(
        "/v1/forget",
        json={"target": TARGET, "approval_token": "gfa_test-token"},
        headers={"Authorization": "Bearer host"},
    )
    assert response.status_code == 200
    assert boundary.writes[-1][0] == "host-world"
    assert boundary.writes[-1][2]["actor"] == "owner"


@pytest.mark.parametrize("route", ["/v1/forget", "/v1/forget/approve"])
def test_cross_site_request_is_refused_before_mutation(boundary, route):
    response = boundary.client.post(
        route,
        json={"target": TARGET, "confirm": True},
        headers={"Origin": "https://elsewhere.example"},
    )
    assert response.status_code == 403
    assert boundary.writes == boundary.mints == []


@pytest.mark.parametrize("route", ["/v1/forget", "/v1/forget/approve"])
def test_no_ledger_configuration_means_no_write(boundary, monkeypatch, route):
    monkeypatch.delenv(api.LEDGER_ENV)
    response = boundary.client.post(route, json={"target": TARGET, "confirm": True})
    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is False
    assert boundary.writes == boundary.mints == []


def test_present_bad_authorization_never_falls_back_to_session(boundary):
    response = boundary.client.post(
        "/v1/forget",
        json={"target": TARGET, "approval_token": "gfa_test-token"},
        headers={"Authorization": "Basic bad"},
    )
    assert response.status_code == 401
    assert boundary.writes == []


def test_oversized_body_is_refused_without_reflecting_it(boundary):
    response = boundary.client.post(
        "/v1/forget/approve",
        content=b"x" * (api.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert len(response.content) < 500
    assert boundary.mints == []


@pytest.mark.parametrize("route", ["/v1/forget", "/v1/forget/approve"])
def test_no_session_means_no_mutation(boundary, route):
    boundary.session = False
    response = boundary.client.post(route, json={"target": TARGET, "confirm": True})
    assert response.status_code == 401
    assert boundary.mints == boundary.writes == []


def test_invalid_unicode_target_returns_safe_validation_error(boundary):
    response = boundary.client.post(
        "/v1/forget/approve",
        content=b'{"target":{"type":"named_value","value":"\\ud800"},"confirm":true}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert boundary.mints == []


def test_forget_page_is_served_without_cache(monkeypatch, tmp_path):
    (tmp_path / "forget.html").write_text("<!doctype html><title>Forget</title>")
    monkeypatch.setenv("GROVE_WEB_DIST", str(tmp_path))
    client = TestClient(create_app(ApiSettings(public_base_url=BASE), driver=object()))
    response = client.get("/forget")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<title>Forget</title>" in response.text
