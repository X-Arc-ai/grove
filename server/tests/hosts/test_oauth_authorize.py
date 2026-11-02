"""GET /authorize and POST /authorize/decision: the page before the redirect, the checks in the
design's order, the sign-in round trip, the consent page, and the decision with its csrf value."""

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from grove.api.config import WEB_DIST_ENV
from grove.api.oauth import OAuthDeps
from grove.ids import new_id
from grove.oauth.store import list_grants
from tests.hosts.conftest import CLIENT_NAME, LOOPBACK_REDIRECT, SECOND_TITLE
from tests.hosts.oauth_kit import (
    BASE,
    MCP_URL,
    STATE,
    authorize_query,
    browser,
    built_consent_page,
    consent,
    decide,
    document,
    hidden_fields,
    location,
    make_app,
    new_pkce,
)

PAGE_HEADERS = {
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
}


@pytest.fixture
def app(driver, migrated, tmp_path, monkeypatch):
    built_consent_page(tmp_path / "dist", monkeypatch)
    return make_app(driver)


@pytest.fixture
def ines(hosts_plant):
    return hosts_plant.person(seeded=False)


@pytest.fixture
def web(app, ines):
    """Ines's browser, signed in."""
    return browser(app, ines)


@pytest.fixture
def host(app):
    """A browser with no session."""
    return browser(app)


@pytest.fixture
def client_id(hosts_plant):
    return hosts_plant.client()


def assert_page(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    for name, value in PAGE_HEADERS.items():
        assert response.headers[name] == value, name


# The consent page ------------------------------------------------------------------------------


def test_the_consent_page_says_who_asks_what_for_and_who_is_signed_in(web, client_id):
    pkce = new_pkce()
    page = consent(web, authorize_query(client_id, pkce))
    assert_page(page, 200)
    text = page.text
    assert "<title>Connect an app: Grove</title>" in text
    assert f"An app that says it is <strong><q>{CLIENT_NAME}</q></strong>" in text
    assert "Signed in as <strong>Ines Duarte</strong>" in text
    assert "Grove sends you back to this computer" in text
    assert "Read what Grove knows" in text and "Add to Grove" in text
    assert "Forget things" not in text
    assert 'value="approve">Approve</button>' in text and 'value="deny">Deny</button>' in text
    fields = hidden_fields(text)
    csrf = fields.pop("csrf")
    assert len(csrf) == 64 and int(csrf, 16) >= 0
    assert fields == {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": LOOPBACK_REDIRECT,
        "scope": "grove.read grove.write",
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": STATE,
        "resource": MCP_URL,
    }


def test_foreign_scopes_drop_to_the_default_and_sensitive_write_needs_its_name(web, client_id):
    foreign = authorize_query(client_id, new_pkce(), scope="openid profile email offline_access")
    assert hidden_fields(consent(web, foreign).text)["scope"] == "grove.read grove.write"
    named = authorize_query(client_id, new_pkce(), scope="grove.sensitive_write openid")
    page = consent(web, named)
    assert hidden_fields(page.text)["scope"] == "grove.sensitive_write"
    assert "Forget things in Grove (each forget still asks you)" in page.text
    assert "Add to Grove" not in page.text


def test_the_page_escapes_the_name_a_client_gave_itself(app, web, hosts_plant):
    hostile = hosts_plant.client(host='<img src=x onerror="alert(1)">')
    text = consent(web, authorize_query(hostile, new_pkce())).text
    assert "<img" not in text
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in text


def test_a_trailing_slash_on_the_resource_is_tolerated(web, client_id):
    query = authorize_query(client_id, new_pkce(), resource=MCP_URL + "/")
    assert hidden_fields(consent(web, query).text)["resource"] == MCP_URL
    assert "resource" not in hidden_fields(
        consent(web, authorize_query(client_id, new_pkce(), resource=None)).text
    )


def test_a_document_client_is_shown_by_its_documents_host(
    driver, migrated, ines, hosts_plant, tmp_path, monkeypatch
):
    built_consent_page(tmp_path / "dist", monkeypatch)
    url = hosts_plant.track_client(f"https://lumen.example/clients/{uuid4()}.json")
    deps = OAuthDeps(
        resolver=lambda host, port: ["93.184.215.14"],
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=document(url))),
    )
    web = browser(make_app(driver, oauth_deps=deps), ines)
    text = consent(web, authorize_query(url, new_pkce())).text
    assert "<strong>lumen.example</strong> asks to use your Grove." in text
    assert f"It calls itself <q>{CLIENT_NAME}</q>." in text
    assert "says it is" not in text


def test_with_no_built_page_a_signed_in_request_gets_a_typed_503(web, client_id, monkeypatch):
    monkeypatch.delenv(WEB_DIST_ENV)
    response = web.get("/authorize", params=authorize_query(client_id, new_pkce()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert response.json()["error"]["message"] == "the consent page is not built"


# Before the client and the redirect are known: a page, never a redirect ------------------------


@pytest.mark.parametrize("client", [None, "unknown", "twice"])
def test_no_client_an_unknown_client_or_two_of_them_get_a_page(host, client_id, client):
    query = authorize_query(client_id, new_pkce())
    if client is None:
        del query["client_id"]
    elif client == "unknown":
        query["client_id"] = new_id()
    params = list(query.items()) + ([("client_id", client_id)] if client == "twice" else [])
    assert_page(host.get("/authorize", params=params), 400)


def test_a_document_that_cannot_be_fetched_is_an_unknown_client(
    driver, migrated, tmp_path, monkeypatch, hosts_plant
):
    built_consent_page(tmp_path / "dist", monkeypatch)
    url = hosts_plant.track_client(f"https://lumen.example/clients/{uuid4()}.json")
    deps = OAuthDeps(resolver=lambda host, port: ["10.0.0.7"])
    host = browser(make_app(driver, oauth_deps=deps))
    assert_page(host.get("/authorize", params=authorize_query(url, new_pkce())), 400)


@pytest.mark.parametrize(
    "redirect",
    [
        "http://127.0.0.1:43117/other",
        "http://127.0.0.1:50999/callback/",
        "http://localhost:43117/callback",
        "https://127.0.0.1:43117/callback",
        "https://attacker.example/callback",
        "http://127.0.0.1:43117/callback?x=1",
    ],
)
def test_a_redirect_that_matches_no_registered_uri_gets_a_page(host, client_id, redirect):
    response = host.get(
        "/authorize", params=authorize_query(client_id, new_pkce(), redirect_uri=redirect)
    )
    assert_page(response, 400)


def test_an_https_redirect_must_match_exactly(web, hosts_plant):
    client = hosts_plant.client(redirect_uris=["https://app.example/cb"])
    assert consent(web, authorize_query(client, new_pkce(), redirect_uri="https://app.example/cb"))
    for other in (
        "https://app.example/cb/",
        "https://app.example:8443/cb",
        "https://app.example/cb?x=1",
    ):
        response = web.get(
            "/authorize", params=authorize_query(client, new_pkce(), redirect_uri=other)
        )
        assert_page(response, 400)


def test_a_loopback_redirect_on_another_port_passes_and_is_where_the_code_goes(web, client_id):
    other_port = "http://127.0.0.1:50999/callback"
    answer = decide(web, authorize_query(client_id, new_pkce(), redirect_uri=other_port))
    assert answer.status_code == 302
    target, query = location(answer)
    assert target == other_port and query["code"].startswith("gvc_")


def test_with_no_redirect_uri_the_only_registered_one_is_used(web, client_id, hosts_plant):
    query = authorize_query(client_id, new_pkce(), redirect_uri=None)
    assert "redirect_uri" not in hidden_fields(consent(web, query).text)
    target, answer = location(decide(web, query))
    assert target == LOOPBACK_REDIRECT and "code" in answer
    two = hosts_plant.client(redirect_uris=[LOOPBACK_REDIRECT, "https://app.example/cb"])
    assert_page(
        web.get("/authorize", params=authorize_query(two, new_pkce(), redirect_uri=None)), 400
    )


# After: errors go to the redirect, with state and iss ------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"response_type": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": "a" * 42}, "invalid_request"),
        ({"code_challenge": "a" * 42 + "!"}, "invalid_request"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"resource": BASE}, "invalid_target"),
    ],
)
def test_a_request_with_a_bad_parameter_returns_its_error_to_the_redirect(
    host, client_id, change, error
):
    query = {**authorize_query(client_id, new_pkce()), **change}
    response = host.get("/authorize", params={k: v for k, v in query.items() if v is not None})
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    target, answer = location(response)
    assert target == LOOPBACK_REDIRECT
    assert answer["error"] == error and answer["state"] == STATE and answer["iss"] == BASE
    assert answer["error_description"]


def test_a_scope_given_twice_is_refused_at_the_redirect(host, client_id):
    params = list(authorize_query(client_id, new_pkce()).items()) + [("scope", "grove.read")]
    _, answer = location(host.get("/authorize", params=params))
    assert answer["error"] == "invalid_request"


def test_a_state_that_is_not_printable_ascii_is_not_sent_back(host, client_id):
    response = host.get("/authorize", params=authorize_query(client_id, new_pkce(), state="a\nb"))
    _, answer = location(response)
    assert answer["error"] == "invalid_request" and "state" not in answer


# The sign-in round trip ------------------------------------------------------------------------


@pytest.mark.parametrize("cookie", [None, "not-a-session"])
def test_with_no_valid_session_the_person_goes_to_sign_in_and_back(app, client_id, cookie):
    host = browser(app)
    if cookie is not None:
        host.cookies.set("grove_session", cookie, domain="grove.example", path="/")
    query = authorize_query(client_id, new_pkce())
    response = host.get("/authorize", params=query)
    assert response.status_code == 303
    target = response.headers["location"]
    assert target.startswith("/signin?next=%2Fauthorize%3F")
    back = parse_qs(urlsplit(target).query)["next"][0]
    assert {key: values[0] for key, values in parse_qs(urlsplit(back).query).items()} == query
    assert len(back) <= 2048


# The decision ----------------------------------------------------------------------------------


def test_approve_redirects_with_the_code_the_state_and_the_issuer(web, ines, client_id, driver):
    answer = decide(web, authorize_query(client_id, new_pkce()))
    assert answer.status_code == 302
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["referrer-policy"] == "no-referrer"
    target, query = location(answer)
    assert target == LOOPBACK_REDIRECT
    assert list(query) == ["code", "state", "iss"]
    assert query["code"].startswith("gvc_") and query["state"] == STATE and query["iss"] == BASE
    (grant,) = list_grants(driver, ines.world_id)
    assert grant.client_id == client_id and grant.user_id == ines.user_id
    assert grant.scopes == ("grove.read", "grove.write")


def test_deny_redirects_with_access_denied_and_grants_nothing(web, ines, client_id, driver):
    answer = decide(web, authorize_query(client_id, new_pkce()), decision="deny")
    assert answer.status_code == 302
    target, query = location(answer)
    assert target == LOOPBACK_REDIRECT
    assert query["error"] == "access_denied" and query["state"] == STATE and query["iss"] == BASE
    assert "code" not in query
    assert list_grants(driver, ines.world_id) == []


def test_a_redirect_with_a_query_gets_the_code_after_an_ampersand(web, hosts_plant):
    with_query = "http://127.0.0.1:43117/callback?tab=2"
    client = hosts_plant.client(redirect_uris=[with_query])
    answer = decide(web, authorize_query(client, new_pkce(), redirect_uri=with_query, state=None))
    target = answer.headers["location"]
    assert target.startswith(with_query + "&code=gvc_")
    assert "state=" not in target and target.endswith("&iss=https%3A%2F%2Fgrove.example")


def _form(web, client_id):
    fields = hidden_fields(consent(web, authorize_query(client_id, new_pkce())).text)
    return {**fields, "decision": "approve"}


@pytest.mark.parametrize(
    "change",
    [
        {"csrf": "0" * 64},
        {"csrf": None},
        {"scope": "grove.read grove.write grove.sensitive_write"},
        {"state": "st-other"},
        {"code_challenge": "b" * 43},
        {"redirect_uri": "http://127.0.0.1:50999/callback"},
        {"decision": "maybe"},
        {"decision": None},
    ],
)
def test_a_changed_or_forged_decision_is_refused_with_a_page(web, ines, client_id, driver, change):
    form = {**_form(web, client_id), **change}
    response = web.post(
        "/authorize/decision", data={k: v for k, v in form.items() if v is not None}
    )
    assert_page(response, 400)
    assert list_grants(driver, ines.world_id) == []


def test_another_persons_session_cannot_use_the_form(app, web, client_id, plant_person, driver):
    tomas = plant_person(SECOND_TITLE, seeded=False)
    form = _form(web, client_id)
    response = browser(app, tomas).post("/authorize/decision", data=form)
    assert_page(response, 400)
    assert list_grants(driver, tomas.world_id) == []


def test_a_decision_with_no_session_gets_401(app, web, client_id):
    form = _form(web, client_id)
    response = browser(app).post("/authorize/decision", data=form)
    assert_page(response, 401)
    assert "You are signed out" in response.text


def test_a_decision_that_is_not_a_form_is_refused_with_a_page(web, client_id):
    response = web.post("/authorize/decision", json=_form(web, client_id))
    assert_page(response, 400)
