"""Google sign-in: the redirect, the state cookie, the callback, and each way the callback fails."""

import base64
import hashlib

import httpx
import pytest

from grove.api import auth as auth_module
from grove.api.google import HeldState, pack_state, unpack_state
from grove.store.errors import STORE_UNAVAILABLE
from tests.auth.conftest import (
    AUTH_URI,
    BASE,
    CLIENT_ID,
    CLIENT_SECRET,
    GOOGLE_CALLBACK,
    GOOGLE_START,
    LOOPBACK,
    SIGNIN_ERROR,
    TOKEN_URI,
    browser,
    cookie_flags,
    email_sign_in,
    google_sign_in,
    google_start,
    items_of,
    sessions_of,
    user_of,
    worlds_of,
)

STATE_COOKIE = "__Host-grove_google"


def test_the_start_redirect_has_exactly_the_scope_pkce_state_nonce_and_callback(client):
    response = client.get(GOOGLE_START)
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"].startswith(AUTH_URI + "?")
    query = google_start(client)
    assert query["scope"] == "openid email profile"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{BASE}/v1/auth/google/callback"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["prompt"] == "select_account"
    assert len(query["state"]) >= 32 and len(query["nonce"]) >= 32
    assert len(query["code_challenge"]) == 43
    assert "access_type" not in query
    assert "calendar" not in response.headers["location"]
    assert CLIENT_SECRET not in response.headers["location"]


def test_the_challenge_is_the_hash_of_the_verifier_sent_at_the_exchange(client, accounts, google):
    query = google_start(client)
    google.claims = google.good_claims(accounts.address(), query["nonce"])
    client.get(GOOGLE_CALLBACK, params={"code": "a-code", "state": query["state"]})
    form = google.requests[0]["form"]
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    assert 43 <= len(form["code_verifier"]) <= 128
    assert google.requests[0]["url"] == TOKEN_URI
    assert form["client_secret"] == CLIENT_SECRET
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "a-code"
    assert form["redirect_uri"] == f"{BASE}/v1/auth/google/callback"


def test_the_state_cookie_on_https(client):
    response = client.get(GOOGLE_START)
    _, flags = cookie_flags(response, STATE_COOKIE)
    assert {"httponly", "samesite=lax", "max-age=600", "path=/", "secure"} <= flags
    assert not any(flag.startswith("domain") for flag in flags)


def test_the_state_cookie_on_a_loopback_http_base(make_app):
    client = browser(make_app(LOOPBACK), LOOPBACK)
    response = client.get(GOOGLE_START)
    _, flags = cookie_flags(response, "grove_google")
    assert {"httponly", "samesite=lax", "max-age=600", "path=/"} <= flags
    assert "secure" not in flags
    assert not any(
        header.startswith(STATE_COOKIE) for header in response.headers.get_list("set-cookie")
    )
    query = dict(
        part.split("=", 1) for part in response.headers["location"].split("?", 1)[1].split("&")
    )
    assert query["redirect_uri"] == "http%3A%2F%2F127.0.0.1%3A8000%2Fv1%2Fauth%2Fgoogle%2Fcallback"


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"web": {"client_id": "only"}}'])
def test_no_client_file_or_a_bad_one_goes_to_the_card(content, make_app, tmp_path):
    path = tmp_path / "client.json"
    if content is not None:
        path.write_text(content)
    client = browser(make_app(oauth_file=str(path)))
    response = client.get(GOOGLE_START)
    assert response.status_code == 303
    assert response.headers["location"] == SIGNIN_ERROR
    assert "set-cookie" not in response.headers


def test_no_configured_path_goes_to_the_card(make_app):
    client = browser(make_app(oauth_file=None))
    assert client.get(GOOGLE_START).headers["location"] == SIGNIN_ERROR


def test_the_callback_signs_in(client, driver, accounts, google):
    address = accounts.address()
    response = google_sign_in(client, google, address)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    token, flags = cookie_flags(response, "grove_session")
    assert token
    assert {"httponly", "samesite=lax", "path=/", "secure"} <= flags
    _, cleared = cookie_flags(response, STATE_COOKIE)
    assert "max-age=0" in cleared

    session = client.get("/v1/views/session").json()
    assert session["method"] == "google"
    user = user_of(driver, address)
    assert session["account"] == user["id"]
    world = worlds_of(driver, user["id"])[0]
    assert items_of(driver, world["id"])[0]["title"] == "Maya Chen"


def test_the_callback_goes_to_the_next_path_it_was_given(client, accounts, google):
    address = accounts.address()
    query = google_start(client, "/some/page?tab=2")
    google.claims = google.good_claims(address, query["nonce"])
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    assert response.headers["location"] == "/some/page?tab=2"


@pytest.mark.parametrize(
    "bad_next",
    [
        "//evil.example",
        "/\\evil.example",
        "https://evil.example",
        "evil",
        "/a\\b",
        "/line\nbreak",
        "/tab\t",
        "/" + "x" * 512,
        "",
    ],
)
def test_an_open_redirect_next_is_refused(bad_next, client, accounts, google):
    address = accounts.address()
    query = google_start(client, bad_next)
    google.claims = google.good_claims(address, query["nonce"])
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_next_changed_in_the_cookie_is_refused_too(client, accounts, google):
    address = accounts.address()
    query = google_start(client)
    held = unpack_state(client.cookies.get(STATE_COOKIE))
    forged = HeldState(held.state, held.nonce, held.verifier, "//evil.example")
    client.cookies.set(STATE_COOKIE, pack_state(forged), domain="grove.example", path="/")
    google.claims = google.good_claims(address, query["nonce"])
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("field", ["state", "nonce", "verifier"])
def test_a_non_ascii_value_in_the_state_cookie_fails_neutrally(
    field, client, driver, accounts, google
):
    address = accounts.address()
    query = google_start(client)
    held = unpack_state(client.cookies.get(STATE_COOKIE))
    values = {"state": held.state, "nonce": held.nonce, "verifier": held.verifier}
    values[field] = "\ud800" if field == "verifier" else values[field] + "\u00e9"
    forged = HeldState(values["state"], values["nonce"], values["verifier"], "/")
    client.cookies.set(STATE_COOKIE, pack_state(forged), domain="grove.example", path="/")
    google.claims = google.good_claims(address, query["nonce"])
    state = values["state"] if field == "state" else query["state"]
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": state})
    _assert_failed(response, driver, address)
    assert google.requests == []


def _assert_failed(response, driver, address):
    assert response.status_code == 303
    assert response.headers["location"] == SIGNIN_ERROR
    assert response.headers["referrer-policy"] == "no-referrer"
    _, cleared = cookie_flags(response, STATE_COOKIE)
    assert "max-age=0" in cleared
    assert not any(
        header.startswith("grove_session=") for header in response.headers.get_list("set-cookie")
    )
    assert user_of(driver, address) is None


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "another-nonce"},
        {"aud": "another-client"},
        {"azp": "another-client"},
        {"iss": "https://evil.example"},
        {"exp": 1},
        {"exp": "soon"},
        {"exp": True},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": "not-an-address"},
        {"email": None},
        {"sub": ""},
        {"sub": None},
        {"nonce": None},
    ],
    ids=lambda claims: "-".join(f"{key}={value}" for key, value in claims.items()),
)
def test_a_bad_claim_fails_with_no_session(claims, client, driver, accounts, google):
    address = accounts.address()
    _assert_failed(google_sign_in(client, google, address, **claims), driver, address)


def test_the_second_issuer_form_and_a_token_with_no_azp_pass(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    claims = google.good_claims(address, query["nonce"])
    claims["iss"] = "accounts.google.com"
    del claims["azp"]
    google.claims = claims
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    assert response.headers["location"] == "/"


def test_a_state_mismatch_fails_with_no_exchange(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    google.claims = google.good_claims(address, query["nonce"])
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": "another-state"})
    _assert_failed(response, driver, address)
    assert google.requests == []


def test_no_state_param_fails(client, driver, accounts, google):
    address = accounts.address()
    google_start(client)
    response = client.get(GOOGLE_CALLBACK, params={"code": "c"})
    _assert_failed(response, driver, address)
    assert google.requests == []


def test_no_state_cookie_fails(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    client.cookies.clear()
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    assert response.headers["location"] == SIGNIN_ERROR
    assert google.requests == []
    assert user_of(driver, address) is None


def test_an_error_param_fails_with_no_exchange(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    response = client.get(
        GOOGLE_CALLBACK, params={"error": "access_denied", "code": "c", "state": query["state"]}
    )
    _assert_failed(response, driver, address)
    assert google.requests == []


def test_no_code_fails_with_no_exchange(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    response = client.get(GOOGLE_CALLBACK, params={"state": query["state"]})
    _assert_failed(response, driver, address)
    assert google.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(302, headers={"location": "https://evil.example/token"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"access_token": "no id token"}),
        httpx.Response(200, json={"id_token": "not.a.token"}),
        httpx.Response(200, json={"id_token": "two.parts"}),
        httpx.Response(200, json={"id_token": 5}),
    ],
    ids=["400", "redirect", "text", "list", "no-token", "bad-payload", "two-parts", "number"],
)
def test_a_bad_token_answer_fails_with_no_session(answer, client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)
    google.answer = answer
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    _assert_failed(response, driver, address)


def test_a_network_failure_at_the_exchange_fails_with_no_session(client, driver, accounts, google):
    address = accounts.address()
    query = google_start(client)

    def broken(request):
        raise httpx.ConnectTimeout("no answer")

    client.app.state.auth.google_http = httpx.Client(transport=httpx.MockTransport(broken))
    response = client.get(GOOGLE_CALLBACK, params={"code": "c", "state": query["state"]})
    _assert_failed(response, driver, address)


def test_a_store_failure_at_the_callback_fails_with_no_session(
    client, driver, accounts, google, monkeypatch
):
    address = accounts.address()

    def down(*args, **kwargs):
        raise STORE_UNAVAILABLE[0]("the store went away")

    monkeypatch.setattr(auth_module, "ensure_account", down)
    response = google_sign_in(client, google, address)
    assert response.headers["location"] == SIGNIN_ERROR
    assert not any(
        header.startswith("grove_session=") for header in response.headers.get_list("set-cookie")
    )


def test_google_and_email_with_one_address_reach_one_world(app, driver, accounts, google, mailer):
    address = accounts.address()
    first = browser(app)
    assert google_sign_in(first, google, address).status_code == 303
    by_google = first.get("/v1/views/session").json()

    second = browser(app)
    by_email = email_sign_in(second, mailer, address.upper()).json()
    assert by_email["account"] == by_google["account"]
    assert by_email["self_item_id"] == by_google["self_item_id"]
    assert by_email["method"] == "email"
    assert len(worlds_of(driver, by_google["account"])) == 1
    world = worlds_of(driver, by_google["account"])[0]["id"]
    assert len(items_of(driver, world)) == 1
    assert {row["method"] for row in sessions_of(driver, by_google["account"])} == {
        "google",
        "email",
    }


def test_a_new_sign_in_revokes_the_session_the_incoming_cookie_names(
    client, driver, accounts, google, mailer
):
    address = accounts.address()
    first = email_sign_in(client, mailer, address)
    first_token, _ = cookie_flags(first, "grove_session")
    assert google_sign_in(client, google, address).status_code == 303
    second_token = client.cookies.get("grove_session")
    assert second_token != first_token
    rows = sessions_of(driver, first.json()["account"])
    assert len(rows) == 2
    assert sum(1 for row in rows if row["revoked_at"] is None) == 1
    assert client.get("/v1/views/session").json()["method"] == "google"


@pytest.mark.parametrize("name", ["  ", "\x00\x01", 7, None])
def test_a_name_with_nothing_left_makes_the_neutral_title(name, client, driver, accounts, google):
    address = accounts.address()
    assert google_sign_in(client, google, address, name=name).headers["location"] == "/"
    world = worlds_of(driver, user_of(driver, address)["id"])[0]
    assert items_of(driver, world["id"])[0]["title"] == "You"


def test_a_long_name_is_cut_and_control_characters_go(client, driver, accounts, google):
    address = accounts.address()
    name = "Maya\x07 Chen\n" + "x" * 300
    assert google_sign_in(client, google, address, name=name).headers["location"] == "/"
    world = worlds_of(driver, user_of(driver, address)["id"])[0]
    title = items_of(driver, world["id"])[0]["title"]
    assert title.startswith("Maya Chen")
    assert len(title) == 120
    assert "\x07" not in title and "\n" not in title
