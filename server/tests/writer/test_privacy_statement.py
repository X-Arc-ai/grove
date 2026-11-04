"""D14's public configuration projection contains status codes, never configuration values."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.privacy import (
    ABSENT,
    CODE_POLICY,
    PRESENT,
    STATIC,
    flow_statement,
    markdown_statement,
)


def statuses(statement):
    return {row["id"]: row["status"] for row in statement["rows"]}


def test_static_statement_does_not_claim_a_deployment():
    found = statuses(flow_statement({}, runtime=False))
    assert set(found.values()) == {STATIC}
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
    assert found["uploads"] == CODE_POLICY
    assert found["backups"] == STATIC
    assert found["calendar"] == ABSENT
    assert "does not prove" in statement["notice"]


def test_missing_settings_are_not_reported_as_present():
    found = statuses(flow_statement({"GROVE_ENV": "production"}, runtime=True))
    for key in (
        "email",
        "google",
        "store",
        "spark",
        "voice",
        "glimmer",
        "forget",
        "calendar",
        "git",
        "pipedream",
    ):
        assert found[key] == ABSENT


def test_upload_status_describes_code_without_global_film_config_or_import_claim():
    statement = flow_statement({"GROVE_ENV": "production"}, runtime=True)
    rows = {row["id"]: row for row in statement["rows"]}
    assert rows["uploads"]["status"] == CODE_POLICY
    assert rows["calendar"]["status"] == ABSENT
    assert rows["git"]["status"] == ABSENT
    assert "code-supplied account policy" in rows["uploads"]["terms"]
    assert "operator and connector policies are separate" in rows["uploads"]["terms"]
    assert "not that an import completed" in statement["notice"]
    assert "owner consent" in statement["notice"]


def test_published_connector_paths_are_implemented_without_claiming_deployment():
    statement = flow_statement({}, runtime=False)
    rows = {row["id"]: row for row in statement["rows"]}
    for key in ("calendar", "git", "pipedream"):
        assert rows[key]["status"] == STATIC
    assert "`openid`" in rows["calendar"]["terms"]
    assert "encrypted" in rows["calendar"]["terms"]
    assert "without fetching" in rows["git"]["terms"]
    assert "not proof of deployment" in rows["git"]["terms"]
    assert "opaque owner identifier" in rows["pipedream"]["data"]
    assert "imports no source content" in rows["pipedream"]["terms"]
    assert "executes no provider actions" in rows["pipedream"]["terms"]


def test_connector_settings_report_presence_without_values_or_owner_state():
    value = "synthetic-private-configuration-marker"
    env = {
        "GROVE_ENV": "production",
        "GROVE_GOOGLE_OAUTH_FILE": value,
        "GROVE_CONNECTOR_KEY_FILE": value,
        "GROVE_GIT_REPOSITORIES_FILE": value,
        "GROVE_RENAME_MAP": value,
        "GROVE_DENYLIST": value,
        "PIPEDREAM_PROJECT_ID": value,
        "PIPEDREAM_CLIENT_ID": value,
        "PIPEDREAM_CLIENT_SECRET": value,
        "PIPEDREAM_ENVIRONMENT": "production",
    }
    statement = flow_statement(env, runtime=True)
    found = statuses(statement)
    assert found["calendar"] == found["git"] == found["pipedream"] == PRESENT
    assert value not in json.dumps(statement)
    assert "consent" in statement["notice"]
    assert "not checked" in statement["notice"]
    assert all(
        set(row) == {"id", "data", "destination", "terms", "status"} for row in statement["rows"]
    )


@pytest.mark.parametrize(
    ("setting", "affected"),
    [
        ("GROVE_GOOGLE_OAUTH_FILE", "calendar"),
        ("GROVE_CONNECTOR_KEY_FILE", "calendar"),
        ("GROVE_RENAME_MAP", "calendar"),
        ("GROVE_DENYLIST", "git"),
        ("GROVE_GIT_REPOSITORIES_FILE", "git"),
        ("PIPEDREAM_CLIENT_SECRET", "pipedream"),
        ("PIPEDREAM_ENVIRONMENT", "pipedream"),
    ],
)
def test_missing_connector_setting_never_claims_configuration(setting, affected):
    env = {
        "GROVE_ENV": "production",
        "GROVE_GOOGLE_OAUTH_FILE": "synthetic",
        "GROVE_CONNECTOR_KEY_FILE": "synthetic",
        "GROVE_GIT_REPOSITORIES_FILE": "synthetic",
        "GROVE_RENAME_MAP": "synthetic",
        "GROVE_DENYLIST": "synthetic",
        "PIPEDREAM_PROJECT_ID": "synthetic",
        "PIPEDREAM_CLIENT_ID": "synthetic",
        "PIPEDREAM_CLIENT_SECRET": "synthetic",
        "PIPEDREAM_ENVIRONMENT": "production",
    }
    env.pop(setting)
    assert statuses(flow_statement(env, runtime=True))[affected] == ABSENT


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


def test_material_flow_distinguishes_retention_and_graph_admission():
    row = next(r for r in flow_statement({}, runtime=True)["rows"] if r["id"] == "files")
    assert row["status"] == CODE_POLICY
    assert "Original binaries are not retained" in row["terms"]
    assert "pending typed graph admission" in row["terms"]
    assert "ordinary Learner queue" in row["terms"]
