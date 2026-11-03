"""GET /authorize and POST /authorize/decision: every refusal is a page on Grove, the checks in the
design's order, the sign-in round trip, the consent page, and the decision with its csrf value. The
only redirects to a client are Approve and Deny."""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest

from grove import clock
from grove.api import oauth as oauth_routes
from grove.api.auth import CodeWindow
from grove.api.config import WEB_DIST_ENV
from grove.api.oauth import OAuthDeps
from grove.ids import new_id
from grove.oauth import MAX_GRANTS_PER_WORLD, metadata_document
from grove.oauth.store import find_client, list_grants, world_grants
from grove.writer.oauth import grant_consent, register_client, revoke_grant
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
    exchange,
    hidden_fields,
    location,
    make_app,
    new_pkce,
    oauth_error,
)

PUBLIC_ADDRESS = "93.184.215.14"
FORM_TYPE = "application/x-www-form-urlencoded"
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


@dataclass
class DocumentHost:
    """The resolver and the server of one document URL, which record every call."""

    url: str
    body: bytes
    asked: list[str] = field(default_factory=list)
    fetched: list[httpx.Request] = field(default_factory=list)

    def resolve(self, host: str, port: int) -> list[str]:
        self.asked.append(host)
        return [PUBLIC_ADDRESS]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetched.append(request)
        return httpx.Response(200, content=self.body)

    def deps(self, **options) -> OAuthDeps:
        return OAuthDeps(
            resolver=self.resolve, transport=httpx.MockTransport(self.handle), **options
        )


def _document_host(hosts_plant, netloc: str = "lumen.example", **fields) -> DocumentHost:
    url = hosts_plant.track_client(f"https://{netloc}/clients/{uuid4()}.json")
    return DocumentHost(url, document(url, **fields))


@pytest.fixture
def consent_page(tmp_path, monkeypatch):
    built_consent_page(tmp_path / "dist", monkeypatch)


def test_a_document_client_with_an_https_redirect_is_shown_by_its_documents_host(
    driver, migrated, ines, hosts_plant, consent_page
):
    redirect = "https://lumen.example/cb"
    served = _document_host(hosts_plant, redirect_uris=[redirect])
    web = browser(make_app(driver, oauth_deps=served.deps()), ines)
    text = consent(web, authorize_query(served.url, new_pkce(), redirect_uri=redirect)).text
    assert "<strong>lumen.example</strong> asks to use your Grove." in text
    assert f"It calls itself <q>{CLIENT_NAME}</q>." in text
    assert "says it is" not in text
    assert "Any program on this computer" not in text


def test_a_document_client_with_a_loopback_redirect_is_hedged_and_warned_about(
    driver, migrated, ines, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    web = browser(make_app(driver, oauth_deps=served.deps()), ines)
    text = consent(web, authorize_query(served.url, new_pkce())).text
    assert f"An app that says it is <strong><q>{CLIENT_NAME}</q></strong>" in text
    assert "<strong>lumen.example</strong> asks to use your Grove." not in text
    assert "It names a description on <strong>lumen.example</strong>." in text
    assert "Any program on this computer can claim this name." in text


def test_the_page_keeps_a_port_that_is_not_the_default(
    driver, migrated, ines, hosts_plant, consent_page
):
    redirect = "https://app.example:8443/cb"
    served = _document_host(hosts_plant, netloc="lumen.example:8443", redirect_uris=[redirect])
    web = browser(make_app(driver, oauth_deps=served.deps()), ines)
    text = consent(web, authorize_query(served.url, new_pkce(), redirect_uri=redirect)).text
    assert "<strong>lumen.example:8443</strong> asks to use your Grove." in text
    assert "Grove sends you back to app.example:8443" in text


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
    driver, migrated, ines, consent_page, hosts_plant
):
    url = hosts_plant.track_client(f"https://lumen.example/clients/{uuid4()}.json")
    deps = OAuthDeps(resolver=lambda host, port: ["10.0.0.7"])
    web = browser(make_app(driver, oauth_deps=deps), ines)
    assert_page(web.get("/authorize", params=authorize_query(url, new_pkce())), 400)


@pytest.mark.parametrize(
    "client",
    ["https://a..b/x", "https://.a/x", f"https://{'a' * 64}.example/x"],
    ids=["empty label", "empty first label", "label over 63 characters"],
)
def test_a_host_that_getaddrinfo_cannot_encode_is_an_unknown_client(web, client):
    # The real resolver: getaddrinfo raises UnicodeError for these before it asks any server.
    assert_page(web.get("/authorize", params={"client_id": client}), 400)


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


# A refusal never goes to the client: registration is open, so a registered URI proves nothing -----

PHISH = "https://phish.example/login"
REFUSALS = [
    pytest.param({"response_type": "token"}, [], id="response_type is not code"),
    pytest.param({"response_type": None}, [], id="no response_type"),
    pytest.param({"code_challenge_method": "plain"}, [], id="method plain"),
    pytest.param({"code_challenge_method": None}, [], id="no method"),
    pytest.param({"code_challenge": None}, [], id="no challenge"),
    pytest.param({"code_challenge": "a" * 42}, [], id="short challenge"),
    pytest.param({"code_challenge": "a" * 42 + "!"}, [], id="bad character in the challenge"),
    pytest.param({"resource": "https://other.example/mcp"}, [], id="another resource"),
    pytest.param({"resource": BASE}, [], id="the base as the resource"),
    pytest.param({}, [("resource", "https://other.example/mcp")], id="a second resource"),
    pytest.param({"state": "a\nb"}, [], id="state with a control character"),
    pytest.param({"state": "s" * 1025}, [], id="state too long"),
    pytest.param({}, [("state", "st-other")], id="state twice"),
    pytest.param({}, [("response_type", "code")], id="response_type twice"),
    pytest.param({}, [("code_challenge", "b" * 43)], id="challenge twice"),
    pytest.param({}, [("code_challenge_method", "S256")], id="method twice"),
    pytest.param({}, [("scope", "grove.read")], id="scope twice"),
]


@pytest.fixture
def phishing_client(hosts_plant):
    """A stranger's client, with an https redirect that nothing vouches for."""
    return hosts_plant.client(redirect_uris=[PHISH])


def _refused_pairs(client_id: str, change: dict, more: list) -> list[tuple[str, str]]:
    query = {**authorize_query(client_id, new_pkce(), redirect_uri=PHISH), **change}
    return [(key, value) for key, value in query.items() if value is not None] + more


def assert_stays_on_grove(response: httpx.Response) -> None:
    assert_page(response, 400)
    assert "phish.example" not in response.text
    assert "<a " not in response.text
    assert STATE not in response.text


@pytest.mark.parametrize(("change", "more"), REFUSALS)
@pytest.mark.parametrize("signed_in", [False, True], ids=["no session", "signed in"])
def test_a_refusal_at_authorize_is_a_page_and_never_a_redirect(
    app, ines, phishing_client, change, more, signed_in
):
    visitor = browser(app, ines if signed_in else None)
    pairs = _refused_pairs(phishing_client, change, more)
    assert_stays_on_grove(visitor.get("/authorize", params=pairs))


@pytest.mark.parametrize(("change", "more"), REFUSALS)
def test_a_refusal_at_the_decision_is_a_page_and_never_a_redirect(
    web, phishing_client, change, more
):
    pairs = _refused_pairs(phishing_client, change, more)
    form = urlencode([*pairs, ("decision", "approve"), ("csrf", "0" * 64)])
    answer = web.post("/authorize/decision", content=form, headers={"content-type": FORM_TYPE})
    assert_stays_on_grove(answer)


def test_a_stranger_who_registers_a_page_cannot_make_grove_redirect_to_it(host, hosts_plant):
    registered = host.post("/register", json={"redirect_uris": [PHISH]})
    assert registered.status_code == 201
    client_id = hosts_plant.track_client(registered.json()["client_id"])
    assert_stays_on_grove(host.get("/authorize", params={"client_id": client_id}))


def test_the_page_says_what_was_wrong_in_words_of_the_rule(host, client_id):
    query = authorize_query(client_id, new_pkce(), resource="https://other<b>.example/mcp")
    text = host.get("/authorize", params=query).text
    assert "This request is not valid" in text
    assert f"The resource must be {MCP_URL}." in text
    assert "other" not in text


# A stranger reaches no fetch and no row ----------------------------------------------------------


@pytest.mark.parametrize("cookie", [None, "not-a-session"])
def test_a_document_client_id_with_no_session_goes_to_sign_in_with_no_fetch_and_no_row(
    driver, migrated, ines, hosts_plant, consent_page, cookie
):
    served = _document_host(hosts_plant)
    app = make_app(driver, oauth_deps=served.deps())
    stranger = browser(app)
    if cookie is not None:
        stranger.cookies.set("grove_session", cookie, domain="grove.example", path="/")
    query = authorize_query(served.url, new_pkce())
    answer = stranger.get("/authorize", params=query)
    assert answer.status_code == 303
    assert answer.headers["location"].startswith("/signin?next=%2Fauthorize%3F")
    assert served.asked == [] and served.fetched == []
    assert find_client(driver, served.url) is None

    consent(browser(app, ines), query)
    assert served.asked == ["lumen.example"] and len(served.fetched) == 1


def test_a_document_client_id_twice_is_a_page_with_no_fetch(
    driver, migrated, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    stranger = browser(make_app(driver, oauth_deps=served.deps()))
    pairs = [("client_id", served.url), ("client_id", served.url)]
    assert_page(stranger.get("/authorize", params=pairs), 400)
    assert served.asked == []


def test_a_decision_with_no_session_reaches_no_fetch_and_no_row(
    driver, migrated, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    stranger = browser(make_app(driver, oauth_deps=served.deps()))
    form = authorize_query(served.url, new_pkce())
    assert_page(stranger.post("/authorize/decision", data=form), 401)
    assert served.asked == [] and served.fetched == []
    assert find_client(driver, served.url) is None


@pytest.mark.parametrize("change", [{"client_id": new_id()}, {"response_type": "token"}])
def test_a_decision_with_no_session_is_401_before_any_other_check(host, client_id, change):
    form = {**authorize_query(client_id, new_pkce()), **change}
    response = host.post("/authorize/decision", data=form)
    assert_page(response, 401)
    assert "You are signed out" in response.text


# No document row before Approve, and no slot of the registration window ------------------------


REGISTRATION = {"redirect_uris": [LOOPBACK_REDIRECT], "token_endpoint_auth_method": "none"}
FORGED_CSRF = "0" * 64
# How long a held fetch waits, and the limit of a fetch in the tests that hold one: a test that ends
# early releases it, and a slow store must not end the first fetch while the test still looks at it.
HELD_SECONDS = 10.0
WAIT_SECONDS = 5.0


@dataclass
class AnyDocumentHost:
    """One resolver and one server for every URL: each document names the URL it was asked for.
    With held, each request waits for `release`."""

    held: bool = False
    fetched: list[str] = field(default_factory=list)
    arrived: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    release: threading.Event = field(default_factory=threading.Event)

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_ADDRESS]

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = f"https://{request.headers['host']}{request.url.raw_path.decode()}"
        self.fetched.append(url)
        self.arrived.release()
        if self.held:
            self.release.wait(HELD_SECONDS)
        return httpx.Response(200, content=document(url))

    def deps(self, **options) -> OAuthDeps:
        return OAuthDeps(
            resolver=self.resolve, transport=httpx.MockTransport(self.handle), **options
        )


def _hostile_url(hosts_plant, run: object, number: int = 0) -> str:
    return hosts_plant.track_client(f"https://{run}.evil.example/doc/{number}.json")


def _forged_decision(url: str, decision: str = "approve") -> dict[str, str]:
    return {**authorize_query(url, new_pkce()), "csrf": FORGED_CSRF, "decision": decision}


def _register(app) -> httpx.Response:
    return browser(app).post("/register", json=REGISTRATION)


def test_many_decisions_with_new_documents_and_a_wrong_csrf_leave_no_row_and_spend_no_slot(
    driver, migrated, ines, hosts_plant, consent_page
):
    """The measured attack: anyone can sign in, and the csrf is keyed by the caller's own cookie, so
    it is no bar to them. Each decision still reaches the fetch."""
    hostile = AnyDocumentHost()
    app = make_app(driver, oauth_deps=hostile.deps(window=CodeWindow(limit=1, seconds=3600.0)))
    web = browser(app, ines)
    run = uuid4()
    urls = [_hostile_url(hosts_plant, run, number) for number in range(6)]
    for url in urls:
        answer = web.post("/authorize/decision", data=_forged_decision(url))
        assert_page(answer, 400)
        assert "This page has expired" in answer.text
    assert hostile.fetched == urls, "each decision reached the fetch"
    assert [find_client(driver, url) for url in urls] == [None] * len(urls)

    # The stranger's registration has the whole window, and the window still counts.
    stranger = _register(app)
    assert stranger.status_code == 201, stranger.text
    hosts_plant.track_client(stranger.json()["client_id"])
    full = _register(app)
    assert full.status_code == 429 and oauth_error(full) == "temporarily_unavailable"


def test_the_consent_page_of_a_new_document_writes_no_row_and_spends_no_slot(
    driver, migrated, ines, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    window = CodeWindow(limit=1, seconds=3600.0)
    web = browser(make_app(driver, oauth_deps=served.deps(window=window)), ines)
    page = consent(web, authorize_query(served.url, new_pkce()))
    assert f"<q>{CLIENT_NAME}</q>" in page.text
    assert "It names a description on <strong>lumen.example</strong>." in page.text
    assert find_client(driver, served.url) is None
    assert window.take() is None, "the slot is still free"


@pytest.mark.parametrize(
    ("decision", "status"), [("deny", 302), ("maybe", 400)], ids=["deny", "neither"]
)
def test_a_deny_and_a_refused_decision_write_no_row_and_spend_no_slot(
    driver, migrated, ines, hosts_plant, consent_page, decision, status
):
    served = _document_host(hosts_plant)
    window = CodeWindow(limit=1, seconds=3600.0)
    web = browser(make_app(driver, oauth_deps=served.deps(window=window)), ines)
    answer = decide(web, authorize_query(served.url, new_pkce()), decision)
    assert answer.status_code == status, answer.text
    assert find_client(driver, served.url) is None
    assert list_grants(driver, ines.world_id) == []
    assert window.take() is None, "the slot is still free"


def test_approve_writes_the_row_and_the_grant_and_the_token_endpoint_knows_the_client(
    driver, migrated, ines, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    window = CodeWindow(limit=1, seconds=3600.0)
    app = make_app(driver, oauth_deps=served.deps(window=window))
    pkce = new_pkce()
    start = clock.now()
    answer = decide(browser(app, ines), authorize_query(served.url, pkce))
    assert answer.status_code == 302, answer.text
    assert len(served.fetched) == 2, "the consent page and the decision each fetch a new document"

    row = find_client(driver, served.url)
    assert row.stored is True and row.registration_type == "metadata_document"
    assert row.host == CLIENT_NAME and row.redirect_uris == (LOOPBACK_REDIRECT,)
    assert row.auth_method == "none" and row.secret_hash is None
    assert row.registered_at >= start and row.fetched_at == row.registered_at
    (grant,) = list_grants(driver, ines.world_id)
    assert grant.client_id == served.url and grant.host == CLIENT_NAME
    assert grant.registration_type == "metadata_document"
    assert window.take() is None, "the slot is still free"

    tokens = exchange(browser(app), served.url, location(answer)[1]["code"], pkce.verifier)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["access_token"].startswith("gva_")


def test_a_second_person_who_approves_uses_the_row_and_writes_no_new_one(
    driver, migrated, ines, plant_person, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    app = make_app(driver, oauth_deps=served.deps())
    assert decide(browser(app, ines), authorize_query(served.url, new_pkce())).status_code == 302
    row = find_client(driver, served.url)
    fetched = len(served.fetched)
    tomas = plant_person(SECOND_TITLE, seeded=False)
    assert decide(browser(app, tomas), authorize_query(served.url, new_pkce())).status_code == 302
    assert len(served.fetched) == fetched, "a fresh row is not fetched again"
    assert find_client(driver, served.url) == row
    assert list_grants(driver, tomas.world_id)[0].client_id == served.url


def _stale_copy(driver, hosts_plant) -> tuple[str, datetime]:
    """A document's row, last fetched two hours ago: past the cache, inside the stale limit."""
    url = _hostile_url(hosts_plant, uuid4())
    fetched_at = clock.now() - timedelta(hours=2)
    register_client(
        driver,
        client_id=url,
        host=CLIENT_NAME,
        redirect_uris=[LOOPBACK_REDIRECT],
        registration_type="metadata_document",
        auth_method="none",
        secret_hash=None,
        at=fetched_at,
    )
    return url, fetched_at


def test_approve_on_a_stale_copy_keeps_the_time_of_its_last_fetch(
    driver, migrated, ines, hosts_plant, consent_page
):
    url, fetched_at = _stale_copy(driver, hosts_plant)
    deps = OAuthDeps(resolver=lambda host, port: ["10.0.0.7"])
    web = browser(make_app(driver, oauth_deps=deps), ines)
    assert decide(web, authorize_query(url, new_pkce())).status_code == 302
    assert find_client(driver, url).fetched_at == fetched_at
    assert list_grants(driver, ines.world_id)[0].client_id == url


def test_a_full_window_stops_a_registration_and_no_document(
    driver, migrated, ines, hosts_plant, consent_page
):
    served = _document_host(hosts_plant)
    app = make_app(driver, oauth_deps=served.deps(window=CodeWindow(limit=1, seconds=3600.0)))
    first = _register(app)
    assert first.status_code == 201, first.text
    hosts_plant.track_client(first.json()["client_id"])
    assert _register(app).status_code == 429
    assert decide(browser(app, ines), authorize_query(served.url, new_pkce())).status_code == 302
    assert find_client(driver, served.url) is not None


# A busy fetch is not an unknown app ------------------------------------------------------------


ROUTES = {
    "authorize": lambda web, url: web.get("/authorize", params=authorize_query(url, new_pkce())),
    "decision": lambda web, url: web.post("/authorize/decision", data=_forged_decision(url)),
}


def assert_busy_page(answer: httpx.Response) -> None:
    assert_page(answer, 503)
    assert answer.headers["retry-after"] == "5"
    assert "Grove is busy" in answer.text and "Try again in a moment" in answer.text
    assert "does not know" not in answer.text


@pytest.fixture
def long_limit(monkeypatch):
    """A held fetch outlasts the test's own steps, also on a slow store."""
    monkeypatch.setattr(metadata_document, "FETCH_TIMEOUT_SECONDS", HELD_SECONDS * 2)


@pytest.fixture
def own_slots(monkeypatch):
    """Slots of this test: a worker of another test takes none of them."""
    slots = threading.BoundedSemaphore(metadata_document.MAX_CONCURRENT_DOCUMENT_FETCHES)
    monkeypatch.setattr(metadata_document, "_fetch_slots", slots)
    return slots


@pytest.mark.parametrize("route", ROUTES)
def test_a_second_fetch_by_one_person_gets_the_busy_page_and_another_person_does_not(
    driver, migrated, ines, plant_person, hosts_plant, consent_page, long_limit, own_slots, route
):
    host = AnyDocumentHost(held=True)
    app = make_app(driver, oauth_deps=host.deps())
    run = uuid4()
    first_url, second_url, other_url = (_hostile_url(hosts_plant, run, n) for n in range(3))
    tomas = plant_person(SECOND_TITLE, seeded=False)
    answers: dict[str, httpx.Response] = {}

    def ask(name: str, person, url: str) -> threading.Thread:
        def one() -> None:
            answers[name] = ROUTES["authorize"](browser(app, person), url)

        caller = threading.Thread(target=one)
        caller.start()
        assert host.arrived.acquire(timeout=WAIT_SECONDS), f"{name} did not reach the host"
        return caller

    callers = [ask("first", ines, first_url)]
    try:
        assert_busy_page(ROUTES[route](browser(app, ines), second_url))
        assert host.fetched == [first_url], "a busy refusal reaches no host"
        callers.append(ask("other", tomas, other_url))
    finally:
        host.release.set()
        for caller in callers:
            caller.join()
    assert answers["first"].status_code == 200, answers["first"].text
    assert answers["other"].status_code == 200, answers["other"].text

    # The worker has ended, so the person's turn is back.
    assert consent(browser(app, ines), authorize_query(second_url, new_pkce()))
    assert [find_client(driver, url) for url in (first_url, second_url, other_url)] == [None] * 3


@pytest.mark.parametrize("route", ROUTES)
def test_every_slot_in_use_gives_the_busy_page(
    driver, migrated, ines, hosts_plant, consent_page, monkeypatch, route
):
    taken = threading.BoundedSemaphore(1)
    assert taken.acquire(blocking=False)
    monkeypatch.setattr(metadata_document, "_fetch_slots", taken)
    host = AnyDocumentHost()
    web = browser(make_app(driver, oauth_deps=host.deps()), ines)
    assert_busy_page(ROUTES[route](web, _hostile_url(hosts_plant, uuid4())))
    assert host.fetched == []


def test_with_a_copy_to_serve_a_busy_fetch_shows_no_busy_page(
    driver, migrated, ines, hosts_plant, consent_page, monkeypatch
):
    url, fetched_at = _stale_copy(driver, hosts_plant)
    taken = threading.BoundedSemaphore(1)
    assert taken.acquire(blocking=False)
    monkeypatch.setattr(metadata_document, "_fetch_slots", taken)
    host = AnyDocumentHost()
    web = browser(make_app(driver, oauth_deps=host.deps()), ines)
    assert ROUTES["authorize"](web, url).status_code == 200
    assert host.fetched == [] and find_client(driver, url).fetched_at == fetched_at


@pytest.mark.parametrize("route", ROUTES)
def test_a_refused_document_still_gets_the_400_page(
    driver, migrated, ines, hosts_plant, consent_page, route
):
    deps = OAuthDeps(resolver=lambda host, port: ["10.0.0.7"])
    web = browser(make_app(driver, oauth_deps=deps), ines)
    answer = ROUTES[route](web, _hostile_url(hosts_plant, uuid4()))
    assert_page(answer, 400)
    assert "Grove does not know this app" in answer.text
    assert "retry-after" not in answer.headers


@pytest.mark.parametrize("route", ROUTES)
def test_a_fetch_that_runs_out_of_time_still_gets_the_400_page(
    driver, migrated, ines, hosts_plant, consent_page, monkeypatch, own_slots, route
):
    monkeypatch.setattr(metadata_document, "FETCH_TIMEOUT_SECONDS", 0.2)
    host = AnyDocumentHost(held=True)
    web = browser(make_app(driver, oauth_deps=host.deps()), ines)
    try:
        answer = ROUTES[route](web, _hostile_url(hosts_plant, uuid4()))
    finally:
        host.release.set()
    assert_page(answer, 400)
    assert "Grove does not know this app" in answer.text


# The cap on the grants of a world --------------------------------------------------------------

CAP = 2


@pytest.fixture
def small_cap(monkeypatch):
    monkeypatch.setattr(oauth_routes, "MAX_GRANTS_PER_WORLD", CAP)


def _granted(driver, person, client_id: str) -> str:
    return grant_consent(
        driver,
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        scopes=["grove.read"],
        at=clock.now(),
    )


def _fill(driver, hosts_plant, person, count: int = CAP) -> list[str]:
    clients = [hosts_plant.client() for _ in range(count)]
    for client_id in clients:
        _granted(driver, person, client_id)
    return clients


def assert_too_many(answer: httpx.Response) -> None:
    assert_page(answer, 400)
    assert "You have too many connected apps" in answer.text
    assert "Disconnect" in answer.text


def test_the_cap_is_100_grants_a_world():
    assert MAX_GRANTS_PER_WORLD == 100


def test_a_new_dynamic_client_at_the_cap_gets_the_page_and_no_grant(
    driver, web, ines, hosts_plant, small_cap
):
    _fill(driver, hosts_plant, ines)
    client_id = hosts_plant.client()
    assert_too_many(decide(web, authorize_query(client_id, new_pkce())))
    assert len(list_grants(driver, ines.world_id)) == CAP
    assert world_grants(driver, ines.world_id, client_id).has_client is False


def test_a_new_document_at_the_cap_gets_the_page_and_no_row_and_no_grant(
    driver, migrated, ines, hosts_plant, consent_page, small_cap
):
    _fill(driver, hosts_plant, ines)
    served = _document_host(hosts_plant)
    web = browser(make_app(driver, oauth_deps=served.deps()), ines)
    assert_too_many(decide(web, authorize_query(served.url, new_pkce())))
    assert find_client(driver, served.url) is None
    assert len(list_grants(driver, ines.world_id)) == CAP


def test_one_under_the_cap_a_new_client_passes(driver, web, ines, hosts_plant, small_cap):
    _fill(driver, hosts_plant, ines, CAP - 1)
    client_id = hosts_plant.client()
    assert decide(web, authorize_query(client_id, new_pkce())).status_code == 302
    assert world_grants(driver, ines.world_id, client_id).count == CAP


def test_a_revoked_grant_still_counts_and_its_client_still_passes(
    driver, web, ines, hosts_plant, small_cap
):
    clients = _fill(driver, hosts_plant, ines)
    grant_id = list_grants(driver, ines.world_id)[0].grant_id
    assert revoke_grant(driver, grant_id=grant_id, world_id=ines.world_id, at=clock.now())
    revoked_client = next(
        client_id
        for client_id in clients
        if client_id not in {grant.client_id for grant in list_grants(driver, ines.world_id)}
    )
    grants = world_grants(driver, ines.world_id, revoked_client)
    assert grants.count == CAP and grants.has_client is True

    assert_too_many(decide(web, authorize_query(hosts_plant.client(), new_pkce())))
    assert decide(web, authorize_query(revoked_client, new_pkce())).status_code == 302
    assert world_grants(driver, ines.world_id, revoked_client).count == CAP


def test_a_client_with_a_live_grant_still_passes_at_the_cap(
    driver, web, ines, hosts_plant, small_cap
):
    clients = _fill(driver, hosts_plant, ines)
    assert decide(web, authorize_query(clients[0], new_pkce())).status_code == 302
    assert world_grants(driver, ines.world_id, clients[0]).count == CAP


def test_a_deny_passes_at_the_cap(driver, web, ines, hosts_plant, small_cap):
    _fill(driver, hosts_plant, ines)
    answer = decide(web, authorize_query(hosts_plant.client(), new_pkce()), "deny")
    assert answer.status_code == 302
    assert location(answer)[1]["error"] == "access_denied"


def test_the_cap_counts_one_world(driver, web, ines, plant_person, hosts_plant, small_cap):
    _fill(driver, hosts_plant, plant_person(SECOND_TITLE, seeded=False))
    assert decide(web, authorize_query(hosts_plant.client(), new_pkce())).status_code == 302


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


def test_a_valid_decision_with_no_session_gets_401(app, web, client_id):
    form = _form(web, client_id)
    response = browser(app).post("/authorize/decision", data=form)
    assert_page(response, 401)
    assert "You are signed out" in response.text


def test_a_decision_that_is_not_a_form_is_refused_with_a_page(web, client_id):
    response = web.post("/authorize/decision", json=_form(web, client_id))
    assert_page(response, 400)
