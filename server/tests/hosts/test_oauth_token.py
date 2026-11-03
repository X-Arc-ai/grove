"""POST /token and POST /revoke: both grants, PKCE, a code spent once, the redirect and resource
checks, client authentication for the three methods, rotation and reuse of refresh tokens, and
RFC 7009 revocation."""

import base64
from urllib.parse import quote

import pytest

from grove import clock
from grove.clock import TestClock
from grove.oauth.secrets import token_hash
from grove.oauth.store import find_access_token
from tests.hosts.conftest import LOOPBACK_REDIRECT
from tests.hosts.oauth_kit import (
    MCP_URL,
    authorize_query,
    browser,
    built_consent_page,
    decide,
    exchange,
    location,
    make_app,
    new_code,
    new_pkce,
    oauth_error,
    refresh,
)

SECRET = "gvs_" + "k" * 43


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


@pytest.fixture
def host(app):
    return browser(app)


@pytest.fixture
def client_id(hosts_plant):
    return hosts_plant.client()


def live(driver, access_token: str):
    return find_access_token(driver, token_hash(access_token), clock.now())


def tokens(web, host, client_id, **query):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce, **query)
    response = exchange(host, client_id, code, pkce.verifier)
    assert response.status_code == 200, response.text
    return response.json()


def basic(client_id: str, secret: str) -> dict[str, str]:
    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


# The authorization code ------------------------------------------------------------------------


def test_a_code_with_its_verifier_gives_a_bearer_pair_bound_to_the_world(
    web, host, client_id, ines, driver
):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    response = exchange(host, client_id, code, pkce.verifier, resource=MCP_URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token", "scope"}
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    assert body["access_token"].startswith("gva_") and body["refresh_token"].startswith("gvr_")
    assert body["scope"] == "grove.read grove.write"
    row = live(driver, body["access_token"])
    assert row.world_id == ines.world_id and row.user_id == ines.user_id
    assert row.client_id == client_id and row.resource == MCP_URL
    assert row.scopes == ("grove.read", "grove.write")
    assert live(driver, body["refresh_token"]) is None, "a refresh token is no access token"


def test_a_wrong_verifier_fails_and_spends_the_code(web, host, client_id):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    wrong = exchange(host, client_id, code, "wrong" + pkce.verifier)
    assert wrong.status_code == 400 and oauth_error(wrong) == "invalid_grant"
    again = exchange(host, client_id, code, pkce.verifier)
    assert again.status_code == 400 and oauth_error(again) == "invalid_grant"


def test_a_code_used_twice_revokes_what_the_first_use_gave(web, host, client_id, driver):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    first = exchange(host, client_id, code, pkce.verifier).json()
    assert live(driver, first["access_token"]) is not None
    second = exchange(host, client_id, code, pkce.verifier)
    assert oauth_error(second) == "invalid_grant"
    assert live(driver, first["access_token"]) is None
    assert oauth_error(refresh(host, client_id, first["refresh_token"])) == "invalid_grant"


@pytest.mark.parametrize(
    ("redirect", "at_token"),
    [
        (LOOPBACK_REDIRECT, None),
        (LOOPBACK_REDIRECT, "http://127.0.0.1:50999/callback"),
        (None, "http://127.0.0.1:50999/callback"),
    ],
)
def test_the_redirect_uri_at_the_token_must_be_the_one_of_the_request(
    web, host, client_id, redirect, at_token
):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce, redirect_uri=redirect)
    response = exchange(host, client_id, code, pkce.verifier, redirect_uri=at_token)
    assert response.status_code == 400 and oauth_error(response) == "invalid_grant"


def test_a_request_that_named_no_redirect_needs_none_at_the_token(web, host, client_id):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce, redirect_uri=None)
    assert exchange(host, client_id, code, pkce.verifier, redirect_uri=None).status_code == 200


def test_a_wrong_resource_is_invalid_target_and_leaves_the_code_unspent(web, host, client_id):
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    wrong = exchange(host, client_id, code, pkce.verifier, resource="https://other.example/mcp")
    assert wrong.status_code == 400 and oauth_error(wrong) == "invalid_target"
    assert exchange(host, client_id, code, pkce.verifier).status_code == 200


def test_a_code_issued_to_another_client_is_refused(web, host, client_id, hosts_plant):
    other = hosts_plant.client()
    pkce = new_pkce()
    code = new_code(web, client_id, pkce)
    response = exchange(host, other, code, pkce.verifier)
    assert response.status_code == 400 and oauth_error(response) == "invalid_grant"


def test_a_code_past_its_five_minutes_is_refused(web, host, client_id):
    pkce = new_pkce()
    test_clock = TestClock()
    test_clock.set(clock.now())
    with clock.using(test_clock):
        code = new_code(web, client_id, pkce)
        test_clock.advance(minutes=5, seconds=1)
        response = exchange(host, client_id, code, pkce.verifier)
    assert response.status_code == 400 and oauth_error(response) == "invalid_grant"


@pytest.mark.parametrize(
    ("form", "error"),
    [
        ({"grant_type": "authorization_code", "code_verifier": "v" * 43}, "invalid_request"),
        ({"grant_type": "authorization_code", "code": "gvc_x"}, "invalid_request"),
        ({"grant_type": "authorization_code", "code": "gvc_x", "code_verifier": "short"},
         "invalid_request"),
        ({"grant_type": "authorization_code", "code": "gvc_x", "code_verifier": "v" * 43},
         "invalid_grant"),
        ({"grant_type": "password"}, "unsupported_grant_type"),
        ({"grant_type": "client_credentials"}, "unsupported_grant_type"),
        ({}, "invalid_request"),
        ({"grant_type": "refresh_token"}, "invalid_request"),
    ],
)  # fmt: skip
def test_a_token_request_that_breaks_a_rule_gets_its_error(host, client_id, form, error):
    response = host.post("/token", data={"client_id": client_id, **form})
    assert response.status_code == 400
    assert oauth_error(response) == error
    assert response.headers["cache-control"] == "no-store"


def test_a_parameter_given_twice_or_a_body_that_is_no_form_is_invalid_request(host, client_id):
    twice = (
        "grant_type=authorization_code&code=a&code=b"
        f"&code_verifier={'v' * 43}&client_id={client_id}"
    )
    response = host.post(
        "/token", content=twice, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert oauth_error(response) == "invalid_request"
    as_json = host.post("/token", json={"grant_type": "refresh_token", "client_id": client_id})
    assert as_json.status_code == 400 and oauth_error(as_json) == "invalid_request"


# Client authentication -------------------------------------------------------------------------


@pytest.mark.parametrize("client", [None, "nobody"])
def test_an_unknown_client_or_none_at_all_is_invalid_client(host, client):
    form = {"grant_type": "refresh_token", "refresh_token": "gvr_x", "client_id": client}
    response = host.post("/token", data={k: v for k, v in form.items() if v is not None})
    assert response.status_code == 401 and oauth_error(response) == "invalid_client"
    assert "www-authenticate" not in response.headers


def test_a_public_client_that_sends_a_secret_is_refused(host, client_id):
    form = {"grant_type": "refresh_token", "refresh_token": "gvr_x", "client_id": client_id}
    response = host.post("/token", data={**form, "client_secret": SECRET})
    assert response.status_code == 401 and oauth_error(response) == "invalid_client"


def test_a_basic_client_needs_its_secret_and_gets_a_basic_challenge(web, host, hosts_plant):
    client = hosts_plant.client(auth_method="client_secret_basic", secret_hash=token_hash(SECRET))
    pkce = new_pkce()
    code = new_code(web, client, pkce)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": pkce.verifier,
        "redirect_uri": LOOPBACK_REDIRECT,
    }
    alone = host.post("/token", data={**form, "client_id": client})
    assert alone.status_code == 401 and oauth_error(alone) == "invalid_client"
    wrong = host.post("/token", data=form, headers=basic(client, SECRET + "x"))
    assert wrong.status_code == 401 and oauth_error(wrong) == "invalid_client"
    assert wrong.headers["www-authenticate"].startswith("Basic")
    broken = host.post("/token", data=form, headers={"Authorization": "Basic %%%"})
    assert broken.status_code == 401 and broken.headers["www-authenticate"].startswith("Basic")
    both = host.post(
        "/token", data={**form, "client_secret": SECRET}, headers=basic(client, SECRET)
    )
    assert both.status_code == 400 and oauth_error(both) == "invalid_request"
    # The code is still unspent: every refusal above came before the code was read.
    right = host.post("/token", data=form, headers=basic(client, SECRET))
    assert right.status_code == 200, right.text


def test_basic_credentials_are_form_url_decoded(web, host, hosts_plant):
    client = hosts_plant.client(auth_method="client_secret_basic", secret_hash=token_hash(SECRET))
    pkce = new_pkce()
    code = new_code(web, client, pkce)
    every_char = "".join(f"%{byte:02X}" for byte in SECRET.encode())
    raw = f"{client}:{every_char}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": pkce.verifier,
        "redirect_uri": LOOPBACK_REDIRECT,
    }
    assert host.post("/token", data=form, headers=headers).status_code == 200


def test_a_post_client_sends_its_secret_in_the_form(web, host, hosts_plant):
    client = hosts_plant.client(auth_method="client_secret_post", secret_hash=token_hash(SECRET))
    pkce = new_pkce()
    code = new_code(web, client, pkce)
    wrong = exchange(host, client, code, pkce.verifier, client_secret="gvs_wrong")
    assert wrong.status_code == 401 and oauth_error(wrong) == "invalid_client"
    assert "www-authenticate" not in wrong.headers
    assert exchange(host, client, code, pkce.verifier, client_secret=SECRET).status_code == 200


# Refresh ---------------------------------------------------------------------------------------


def test_a_refresh_gives_a_new_pair_and_spends_the_old_refresh_token(web, host, client_id, driver):
    first = tokens(web, host, client_id)
    second = refresh(host, client_id, first["refresh_token"])
    assert second.status_code == 200 and second.headers["pragma"] == "no-cache"
    pair = second.json()
    assert pair["refresh_token"] != first["refresh_token"]
    assert pair["scope"] == "grove.read grove.write"
    assert live(driver, pair["access_token"]) is not None
    assert live(driver, first["access_token"]) is not None, "rotation spends the refresh token only"
    # A refresh token used twice ends the whole family.
    reused = refresh(host, client_id, first["refresh_token"])
    assert reused.status_code == 400 and oauth_error(reused) == "invalid_grant"
    assert live(driver, pair["access_token"]) is None
    assert live(driver, first["access_token"]) is None
    assert oauth_error(refresh(host, client_id, pair["refresh_token"])) == "invalid_grant"


def test_a_refresh_may_narrow_the_scopes_and_never_widen_them(web, host, client_id):
    first = tokens(web, host, client_id)
    wider = refresh(
        host, client_id, first["refresh_token"], scope="grove.read grove.sensitive_write"
    )
    assert wider.status_code == 400 and oauth_error(wider) == "invalid_scope"
    foreign = refresh(host, client_id, first["refresh_token"], scope="offline_access")
    assert foreign.status_code == 200 and foreign.json()["scope"] == "grove.read grove.write"
    narrow = refresh(host, client_id, foreign.json()["refresh_token"], scope="grove.read openid")
    assert narrow.status_code == 200 and narrow.json()["scope"] == "grove.read"


def test_a_refresh_token_of_another_client_or_resource_is_refused(
    web, host, client_id, hosts_plant
):
    first = tokens(web, host, client_id)
    other = hosts_plant.client()
    stolen = refresh(host, other, first["refresh_token"])
    assert stolen.status_code == 400 and oauth_error(stolen) == "invalid_grant"
    target = refresh(host, client_id, first["refresh_token"], resource="https://other.example/mcp")
    assert target.status_code == 400 and oauth_error(target) == "invalid_target"
    assert refresh(host, client_id, first["refresh_token"], resource=MCP_URL).status_code == 200


# /revoke ---------------------------------------------------------------------------------------


def test_revoke_needs_an_authenticated_client_and_a_token(host, client_id):
    assert oauth_error(host.post("/revoke", data={"token": "gva_x"})) == "invalid_client"
    missing = host.post("/revoke", data={"client_id": client_id})
    assert missing.status_code == 400 and oauth_error(missing) == "invalid_request"


def test_revoking_an_access_token_ends_it_alone(web, host, client_id, driver):
    pair = tokens(web, host, client_id)
    response = host.post(
        "/revoke",
        data={
            "token": pair["access_token"],
            "token_type_hint": "access_token",
            "client_id": client_id,
        },
    )
    assert response.status_code == 200 and response.json() == {}
    assert response.headers["cache-control"] == "no-store"
    assert live(driver, pair["access_token"]) is None
    assert refresh(host, client_id, pair["refresh_token"]).status_code == 200


def test_revoking_a_refresh_token_ends_its_family(web, host, client_id, driver):
    pair = tokens(web, host, client_id)
    response = host.post("/revoke", data={"token": pair["refresh_token"], "client_id": client_id})
    assert response.status_code == 200
    assert live(driver, pair["access_token"]) is None
    assert oauth_error(refresh(host, client_id, pair["refresh_token"])) == "invalid_grant"


def test_an_unknown_token_or_another_clients_token_is_200_and_changes_nothing(
    web, host, client_id, hosts_plant, driver
):
    pair = tokens(web, host, client_id)
    other = hosts_plant.client()
    for value in ("gva_unknown", pair["access_token"]):
        response = host.post("/revoke", data={"token": value, "client_id": other})
        assert response.status_code == 200 and response.json() == {}
    assert live(driver, pair["access_token"]) is not None


def test_a_full_round_with_the_flow_tests_steps(web, host, client_id):
    """Ported from the synthetic server's flow test: deny, approve, a wrong verifier, a new code,
    the exchange, a refresh."""
    pkce = new_pkce()
    query = authorize_query(client_id, pkce, scope="openid profile email offline_access")
    _, denied = location(decide(web, query, decision="deny"))
    assert denied["error"] == "access_denied"
    _, approved = location(decide(web, query))
    assert oauth_error(exchange(host, client_id, approved["code"], "w" + pkce.verifier)) == (
        "invalid_grant"
    )
    pair = exchange(host, client_id, new_code(web, client_id, pkce), pkce.verifier).json()
    assert pair["scope"] == "grove.read grove.write"
    assert refresh(host, client_id, pair["refresh_token"]).json()["access_token"].startswith("gva_")
