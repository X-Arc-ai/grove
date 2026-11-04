"""D14's public configuration projection contains status codes, never configuration values."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.privacy import ABSENT, PENDING, PRESENT, STATIC, flow_statement, markdown_statement


def statuses(statement):
    return {row["id"]: row["status"] for row in statement["rows"]}


def test_static_statement_does_not_claim_a_deployment():
    found = statuses(flow_statement({}, runtime=False))
    assert set(found.values()) == {STATIC, PENDING}
    assert "Runs" not in markdown_statement()


def test_runtime_status_follows_configuration_without_returning_values():
    value = "synthetic-value-that-must-not-be-public"
    env = {
        "MODEL_API_KEY": value,
        "RESEND_API_KEY": value,
        "MAIL_FROM": value,
        "NEO4J_AUTH": value,
        "GROVE_GOOGLE_OAUTH_FILE": value,
        "GROVE_GLIMMER_BASE_URL": value,
        "GROVE_GLIMMER_KEY": value,
        "GROVE_FORGET_LEDGER_DIR": value,
        "GROVE_ENV": "production",
    }
    statement = flow_statement(env, runtime=True)
    assert value not in json.dumps(statement)
    found = statuses(statement)
    assert found["spark"] == found["email"] == found["forget"] == PRESENT
    assert found["uploads"] == ABSENT
    assert found["backups"] == STATIC
    assert found["calendar"] == PENDING
    assert "does not prove" in statement["notice"]


def test_missing_settings_are_not_reported_as_present():
    found = statuses(flow_statement({"GROVE_ENV": "production"}, runtime=True))
    for key in ("email", "google", "store", "uploads", "spark", "voice", "glimmer", "forget"):
        assert found[key] == ABSENT


def test_google_uses_effective_app_settings_and_public_response_is_not_cached(monkeypatch):
    monkeypatch.delenv("GROVE_GOOGLE_OAUTH_FILE", raising=False)
    app = create_app(
        ApiSettings(public_base_url="https://grove.example", google_oauth_file="private/path"),
        driver=object(),
    )
    response = TestClient(app).get("/v1/privacy")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert statuses(response.json())["google"] == PRESENT
    assert "private/path" not in response.text


def test_checked_in_statement_matches_generator():
    path = Path(__file__).resolve().parents[3] / "docs/data-flow.md"
    assert path.read_text() == markdown_statement()
