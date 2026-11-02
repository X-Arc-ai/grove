"""POST /register (RFC 7591): public and confidential clients, the refusals, and the cap across the
server. Every client the API registers goes to hosts_plant.track_client for the purge."""

import json
from uuid import UUID, uuid4

import pytest

from grove.api.auth import CodeWindow
from grove.api.oauth import OAuthDeps
from grove.oauth.secrets import token_hash
from grove.oauth.store import find_client
from grove.store.query import PlatformQuery
from tests.hosts.conftest import CLIENT_NAME, LOOPBACK_REDIRECT
from tests.hosts.oauth_kit import browser, make_app, oauth_error

SUPPORTED = "grove.read grove.write grove.sensitive_write"


@pytest.fixture
def app(driver, migrated):
    return make_app(driver)


@pytest.fixture
def host(app):
    return browser(app)


def register(host, hosts_plant, body, **options):
    response = host.post("/register", json=body, **options)
    if response.status_code == 201:
        hosts_plant.track_client(response.json()["client_id"])
    return response


def test_a_public_client_gets_a_uuid7_and_no_secret(driver, host, hosts_plant):
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [LOOPBACK_REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        # The flow test's foreign scopes: ignored, never an error.
        "scope": "openid profile email offline_access",
    }
    response = register(host, hosts_plant, body)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    answer = response.json()
    assert UUID(answer["client_id"]).version == 7
    assert isinstance(answer["client_id_issued_at"], int)
    assert "client_secret" not in answer and "client_secret_expires_at" not in answer
    assert {
        key: answer[key] for key in answer if key not in ("client_id", "client_id_issued_at")
    } == {
        "client_name": CLIENT_NAME,
        "redirect_uris": [LOOPBACK_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SUPPORTED,
    }
    row = find_client(driver, answer["client_id"])
    assert row.registration_type == "dynamic" and row.host == CLIENT_NAME
    assert row.auth_method == "none" and row.secret_hash is None
    assert row.redirect_uris == (LOOPBACK_REDIRECT,)


def test_a_client_that_names_no_method_is_confidential_and_its_secret_is_kept_hashed(
    driver, host, hosts_plant
):
    response = register(host, hosts_plant, {"redirect_uris": ["https://app.example/cb"]})
    assert response.status_code == 201
    answer = response.json()
    assert answer["token_endpoint_auth_method"] == "client_secret_basic"
    secret = answer["client_secret"]
    assert secret.startswith("gvs_") and len(secret) == 47
    assert answer["client_secret_expires_at"] == 0
    assert "client_name" not in answer
    row = find_client(driver, answer["client_id"])
    assert row.secret_hash == token_hash(secret)
    stored = PlatformQuery(driver).read(
        "MATCH (c:OAuthClient {client_id: $id}) RETURN c {.*} AS row", id=answer["client_id"]
    )[0]["row"]
    assert secret not in json.dumps(stored, default=str)


def test_a_client_secret_post_client_is_registered_as_such(host, hosts_plant):
    body = {
        "redirect_uris": [LOOPBACK_REDIRECT],
        "token_endpoint_auth_method": "client_secret_post",
    }
    answer = register(host, hosts_plant, body).json()
    assert answer["token_endpoint_auth_method"] == "client_secret_post"
    assert answer["client_secret"].startswith("gvs_")


def test_the_name_is_cleaned_before_it_is_stored(driver, host, hosts_plant):
    body = {"redirect_uris": [LOOPBACK_REDIRECT], "client_name": "Lumen‮\x07 Desk"}
    answer = register(host, hosts_plant, body).json()
    assert answer["client_name"] == "Lumen Desk"
    assert find_client(driver, answer["client_id"]).host == "Lumen Desk"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({}, "invalid_redirect_uri"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://app.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["javascript:alert(1)"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://app.example/cb#x"]}, "invalid_redirect_uri"),
        ({"redirect_uris": [f"https://app.example/{n}" for n in range(11)]},
         "invalid_redirect_uri"),
        ({"redirect_uris": ["https://app.example/" + "x" * 2000]}, "invalid_redirect_uri"),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "token_endpoint_auth_method": "tls_client_auth"},
         "invalid_client_metadata"),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "grant_types": ["implicit"]},
         "invalid_client_metadata"),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "response_types": ["code", "token"]},
         "invalid_client_metadata"),
        ([LOOPBACK_REDIRECT], "invalid_client_metadata"),
    ],
)  # fmt: skip
def test_a_registration_that_breaks_a_rule_is_refused_and_stores_nothing(
    driver, host, hosts_plant, body, error
):
    name = f"Refused {uuid4()}"
    if isinstance(body, dict):
        body = {**body, "client_name": name}
    response = register(host, hosts_plant, body)
    assert response.status_code == 400
    assert oauth_error(response) == error
    assert response.headers["cache-control"] == "no-store"
    rows = PlatformQuery(driver).read("MATCH (c:OAuthClient {host: $name}) RETURN c", name=name)
    assert rows == []


@pytest.mark.parametrize(
    "send",
    [
        {"content": b"not json", "headers": {"content-type": "application/json"}},
        {"content": b'{"redirect_uris": NaN}', "headers": {"content-type": "application/json"}},
        {"data": {"redirect_uris": LOOPBACK_REDIRECT}},
        {
            "content": json.dumps(
                {"redirect_uris": [LOOPBACK_REDIRECT], "client_name": "x" * 17000}
            ).encode(),
            "headers": {"content-type": "application/json"},
        },
    ],
)
def test_a_body_that_is_not_a_json_object_of_at_most_16_kib_is_refused(host, send):
    response = host.post("/register", **send)
    assert response.status_code == 400
    assert oauth_error(response) == "invalid_client_metadata"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"redirect_uris": ["http://127.0.0.1:9/cb"], "client_name": "\\ud800"}',
        b'{"redirect_uris": ["http://127.0.0.1:9/cb"], "client_name": "\\udc00 Lumen"}',
        b'{"redirect_uris": ["http://127.0.0.1:9/cb"], "software_id": "\\ud800"}',
        b'{"redirect_uris": ["http://127.0.0.1:9/cb"], "software_version": 1e999}',
        '{"redirect_uris": ["http://127.0.0.1:9/cb"]}'.encode("utf-16"),
        b'\xef\xbb\xbf{"redirect_uris": ["http://127.0.0.1:9/cb"]}',
        b"[" * 8000 + b"]" * 8000,
    ],
    ids=[
        "lone surrogate in the name",
        "lone low surrogate in the name",
        "lone surrogate in another field",
        "a number that overflows",
        "utf-16",
        "a byte order mark",
        "nesting too deep",
    ],
)
def test_a_body_that_is_not_strict_json_is_refused_with_the_normal_refusal(host, raw):
    response = host.post("/register", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert oauth_error(response) == "invalid_client_metadata"


def test_the_cap_across_the_server_answers_429_and_a_refusal_spends_no_slot(
    driver, migrated, hosts_plant
):
    app = make_app(driver, oauth_deps=OAuthDeps(window=CodeWindow(limit=2, seconds=3600.0)))
    host = browser(app)
    good = {"redirect_uris": [LOOPBACK_REDIRECT], "token_endpoint_auth_method": "none"}
    assert register(host, hosts_plant, {"redirect_uris": []}).status_code == 400
    assert register(host, hosts_plant, good).status_code == 201
    assert register(host, hosts_plant, good).status_code == 201
    over = register(host, hosts_plant, good)
    assert over.status_code == 429
    assert oauth_error(over) == "temporarily_unavailable"
    assert int(over.headers["retry-after"]) >= 1
