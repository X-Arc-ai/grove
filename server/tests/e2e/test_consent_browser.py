"""The consent page in a real browser: from a host's /authorize link, through sign-in, to Approve.

Runs only when asked (`-m browser`), never in CI, with the command test_views_browser.py names:

    cd server && GROVE_E2E_SHOTS=<folder> uv run --with playwright==1.58.0 \
        python -m pytest -m browser tests/e2e/test_consent_browser.py -q -p no:cacheprovider

It builds web/dist with web/build.py and serves the real app (the built pages, sign-in, the OAuth
routes) on a free local port. A host registers over HTTP, as Muse Code does. A small local HTTP
server stands for the host's loopback callback. A capture mailer keeps each sign-in code, and the
test types it into the page, so no mail leaves. The address is invented (example.test); the
account, its world and the client are purged when the module ends.

Screenshots of the consent page, light and dark, go to GROVE_E2E_SHOTS, else to a pytest temporary
folder. No token, code or cookie is printed.
"""

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from grove.api.app import create_app
from grove.api.config import WEB_DIST_ENV, ApiSettings
from grove.writer.oauth import delete_unused_client
from grove.writer.testing import purge_test_account, purge_test_world
from tests.auth.conftest import Accounts, CaptureMailer
from tests.hosts import flow

try:
    from playwright import sync_api
except ImportError:
    sync_api = None  # a default run deselects every test here, so it never needs Playwright

pytestmark = pytest.mark.browser

RIGHT_COMMAND = "uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e"

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
SHOTS_ENV = "GROVE_E2E_SHOTS"
HOST = flow.HOST
DESKTOP = {"width": 1440, "height": 900}
WAIT_MS = 30_000
CLIENT_NAME = "Lumen Desk"
STATE = "st-4417"
SCOPE = "openid grove.read grove.write"
CONSENT_HEADING = "Connect an app to your Grove"
READ_WORDS = "Read what Grove knows: recall, what changed, what it breaks, questions"
WRITE_WORDS = "Add to Grove: remember, answer questions, correct"
SENSITIVE_WORDS = "Forget things in Grove"
# The email sign-in's own item has the neutral title.
NEUTRAL_TITLE = "You"
LIGHT_BACKGROUND = "rgb(247, 246, 241)"
DARK_BACKGROUND = "rgb(10, 10, 11)"
BACKGROUND_JS = "() => getComputedStyle(document.body).backgroundColor"


class Callback:
    """The host's loopback redirect: it keeps each query it gets and answers a small page."""

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []
        self._server = ThreadingHTTPServer((HOST, 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def uri(self) -> str:
        return f"http://{HOST}:{self._server.server_address[1]}/callback"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=15)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path == "/callback":
                    callback.queries.append(dict(parse_qsl(parts.query)))
                raw = b"<!doctype html><title>Lumen Desk</title><p>Connected.</p>"
                self.send_response(200 if parts.path == "/callback" else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """A request line holds a code: it goes to no log."""

        return Handler


@dataclass
class Live:
    url: str
    mailer: CaptureMailer = field(repr=False)
    accounts: Accounts = field(repr=False)
    callback: Callback = field(repr=False)
    shots: Path
    clients: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def context(self, browser, **options):
        return browser.new_context(viewport=DESKTOP, base_url=self.url, **options)

    def open(self, context):
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        return page

    def shot(self, page, name: str, settle_ms: int = 400) -> None:
        page.wait_for_timeout(settle_ms)
        page.screenshot(path=str(self.shots / f"e2e-{name}.png"), full_page=True)


@pytest.fixture(scope="session", autouse=True)
def playwright_is_here():
    """Runs before any other fixture of a selected test, so the store is not touched either."""
    if sync_api is None:
        pytest.fail(
            f"the browser tests selected with no Playwright installed. Run: {RIGHT_COMMAND}",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        opened = playwright.chromium.launch()
        yield opened
        opened.close()


@pytest.fixture(scope="module")
def built_pages() -> Path:
    subprocess.run(
        [sys.executable, str(WEB / "build.py")], cwd=REPO, check=True, capture_output=True
    )
    folder = WEB / "dist"
    for name in ("home.html", "index.html", "privacy.html", "consent.html"):
        assert (folder / name).is_file(), name
    return folder


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    folder = Path(os.environ.get(SHOTS_ENV) or tmp_path_factory.mktemp("shots"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(scope="module")
def callback():
    host = Callback()
    host.start()
    yield host
    host.stop()


@pytest.fixture(scope="module")
def live(driver, migrated, built_pages, shots, callback):
    """The real app on a free local port, in a thread. GROVE_WEB_DIST stays set while it runs,
    because /authorize reads the consent page at each request."""
    accounts = Accounts()
    mailer = CaptureMailer()
    port = flow.free_port()
    url = f"http://{HOST}:{port}"
    made = Live(url=url, mailer=mailer, accounts=accounts, callback=callback, shots=shots)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(WEB_DIST_ENV, str(built_pages))
            settings = ApiSettings(public_base_url=url)
            app = create_app(
                settings, driver=driver, mailer=mailer, world_id_for=accounts.world_id_for
            )
            with flow.serving(app, port):
                yield made
    finally:
        for world_id in accounts.worlds:
            purge_test_world(driver, world_id)
        for hashed in accounts.hashes:
            purge_test_account(driver, hashed)
        for client_id in made.clients:
            delete_unused_client(driver, client_id=client_id)


@pytest.fixture(autouse=True)
def no_page_errors(live):
    """Every test here ends with no script error on any page it opened."""
    yield
    seen, live.errors[:] = list(live.errors), []
    assert seen == []


# The page -------------------------------------------------------------------------------------


def _path_of(page) -> str:
    parts = urlsplit(page.url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _email_sign_in(live: Live, page, address: str) -> None:
    page.get_by_role("button", name="Continue with Google").wait_for(timeout=WAIT_MS)
    page.fill("#gp-email", address)
    page.get_by_role("button", name="Continue with email").click()
    page.locator("#gp-code").wait_for(timeout=WAIT_MS)
    page.fill("#gp-code", live.mailer.last_code(address))
    page.locator("[data-code-form] button").click()


def _register(live: Live) -> str:
    asked = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [live.callback.uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
    }
    reply = flow.call("POST", f"{live.url}/register", asked)
    assert reply.status == 201, reply.status
    client_id = reply.json()["client_id"]
    live.clients.append(client_id)
    assert flow.is_uuid7(client_id)
    return client_id


# The case -------------------------------------------------------------------------------------


def test_a_host_link_signs_in_shows_the_consent_and_approves(browser, live):
    client_id = _register(live)
    pkce = flow.new_pkce()
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": live.callback.uri,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": STATE,
        "scope": SCOPE,
        "resource": f"{live.url}/mcp",
    }
    authorize = f"/authorize?{urlencode(query)}"
    address = live.accounts.address()
    context = live.context(browser, color_scheme="light")
    try:
        page = live.open(context)

        # No session: /authorize sends the person to the sign-in card, with the way back.
        page.goto(authorize)
        page.get_by_role("button", name="Continue with Google").wait_for(timeout=WAIT_MS)
        signin = urlsplit(page.url)
        assert signin.path == "/signin"
        assert dict(parse_qsl(signin.query)) == {"next": authorize}

        # An email-code sign-in comes back to the consent page, signed in.
        _email_sign_in(live, page, address)
        page.get_by_role("heading", name=CONSENT_HEADING).wait_for(timeout=WAIT_MS)
        assert _path_of(page) == authorize

        card = page.locator(".consent-card")
        text = card.inner_text()
        assert f"An app that says it is {CLIENT_NAME}" in " ".join(text.split())
        assert page.locator(".consent-who q").inner_text() == CLIENT_NAME
        scopes = page.locator(".consent-scopes li").all_inner_texts()
        assert scopes == [READ_WORDS, WRITE_WORDS]
        assert SENSITIVE_WORDS not in text
        assert f"Signed in as {NEUTRAL_TITLE}" in text
        assert "Grove sends you back to this computer" in text
        assert page.title() == "Connect an app: Grove"
        assert page.get_by_role("button", name="Deny").is_visible()
        assert page.get_by_role("button", name="Approve").is_visible()

        assert page.evaluate(BACKGROUND_JS) == LIGHT_BACKGROUND
        live.shot(page, "consent-light")
        page.emulate_media(color_scheme="dark")
        assert page.evaluate(BACKGROUND_JS) == DARK_BACKGROUND
        live.shot(page, "consent-dark")
        page.emulate_media(color_scheme="light")

        # Approve: the browser lands on the host's callback with the code, the state and iss.
        page.get_by_role("button", name="Approve").click()
        page.wait_for_url(f"{live.callback.uri}?**", timeout=WAIT_MS)
        back = dict(parse_qsl(urlsplit(page.url).query))
        assert set(back) == {"code", "state", "iss"}
        assert back["code"][:4] == "gvc_"
        assert back["state"] == STATE
        assert back["iss"] == live.url
        # Compared to a bool first, so a failure prints no code.
        reached = live.callback.queries == [back]
        assert reached, "the callback did not get the one redirect the page showed"
    finally:
        context.close()

    # The host exchanges the code with its verifier: the code is real.
    form = {
        "grant_type": "authorization_code",
        "code": back["code"],
        "redirect_uri": live.callback.uri,
        "client_id": client_id,
        "code_verifier": pkce.verifier,
    }
    tokens = flow.call("POST", f"{live.url}/token", form, form=True)
    assert tokens.status == 200, tokens.status
    assert tokens.json()["token_type"] == "Bearer"
    assert tokens.json()["scope"] == "grove.read grove.write"
