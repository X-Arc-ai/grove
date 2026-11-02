"""Sign-in in a real browser, against the real app and the real store.

Runs only when asked (`-m browser`), never in CI, with the command test_views_browser.py names.

A capture mailer keeps each code, and the test types it into the page. A small local HTTP server
plays Google: an account page with a Continue and a Refuse button, and a token endpoint that checks
the PKCE verifier and returns an unsigned id token. The app reaches it through a synthetic client
file and its real HTTP client, so no real secret is read and no mail leaves. Addresses are invented
(example.test), and every account and world is purged when the module ends.

Screenshots go to GROVE_E2E_SHOTS, else to a pytest temporary folder.
"""

import base64
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
import uvicorn

from grove import clock
from grove.api.app import WEB_DIST_ENV, create_app
from grove.api.config import ApiSettings
from grove.api.google import SCOPE
from grove.views.session import SESSION_COOKIE
from grove.writer import demo
from grove.writer.testing import purge_test_account, purge_test_world
from tests.auth.conftest import (
    Accounts,
    CaptureMailer,
    id_token,
    items_of,
    sessions_of,
    user_of,
    world_counts,
    worlds_of,
)

try:
    from playwright import sync_api
except ImportError:
    sync_api = None  # a default run deselects every test here, so it never needs Playwright

pytestmark = pytest.mark.browser

RIGHT_COMMAND = "uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e"

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
SHOTS_ENV = "GROVE_E2E_SHOTS"
HOST = "127.0.0.1"
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
WAIT_MS = 30_000
POLL_SECONDS = 0.25
POLLS = 120
SELF_VIEW_ID = "grove-self"
CALLBACK_PATH = "/v1/auth/google/callback"
CLIENT_ID = "client-id.example.test"
CLIENT_SECRET = "client-secret-for-tests"
GOOGLE_NAME = "Maya Chen"
NEUTRAL_TITLE = "You"
WRONG_CODE_TEXT = "That code doesn’t match. Check it and try again."
GOOGLE_ERROR_TEXT = "Google sign-in did not finish. Try again."
TAGLINE = "Give your AI a world to understand"

NODE_COUNT_JS = """(count) => !!window.GroveView && window.GroveView.nodes().length === count"""
NODE_IDS_JS = """() => window.GroveView.nodes().map((n) => n.id)"""
SESSION_STATUS_JS = """async () => {
  const response = await fetch('/v1/views/session', {credentials: 'same-origin'});
  return {status: response.status, body: await response.json()};
}"""
# What a person typed or chose on this browser, kept as the page keeps it.
LEAVE_LOCAL_STATE_JS = """() => {
  const sim = window.GroveSimulation;
  sim.saveGrowth({focus: 'the harbour walk', reading: '', avoid: '', confirm: true});
  return Promise.resolve(sim.addFile('harbour notes.md', 'the print proofs', 'note'))
    .then(() => true, () => true);
}"""


class FakeGoogle:
    """Google's two endpoints on a local port. `address` is who signs in at the next Continue."""

    def __init__(self) -> None:
        self.address: str | None = None
        self.name: str | None = GOOGLE_NAME
        self.refuse_token = False
        self.auth_queries: list[dict[str, str]] = []
        self.token_forms: list[dict[str, str]] = []
        self._codes: dict[str, dict[str, str]] = {}
        self._server = ThreadingHTTPServer((HOST, 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://{HOST}:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=15)

    def _page(self, query: dict[str, str]) -> str:
        code = uuid4().hex
        self._codes[code] = query
        target = html.escape(query["redirect_uri"], quote=True)
        state = html.escape(query["state"], quote=True)

        def form(label: str, name: str, value: str) -> str:
            return (
                f'<form action="{target}" method="get">'
                f'<input type="hidden" name="{name}" value="{value}">'
                f'<input type="hidden" name="state" value="{state}">'
                f"<button>{label}</button></form>"
            )

        return (
            "<!doctype html><title>Fake Google</title><h1>Fake Google</h1>"
            + form("Continue", "code", code)
            + form("Refuse", "error", "access_denied")
        )

    def _token_answer(self, form: dict[str, str]) -> tuple[int, dict]:
        """The real endpoint's checks that the app depends on: the client and PKCE."""
        self.token_forms.append(form)
        asked = self._codes.pop(form.get("code", ""), None)
        if self.refuse_token or asked is None:
            return 400, {"error": "invalid_grant"}
        digest = hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
            or form.get("grant_type") != "authorization_code"
            or form.get("redirect_uri") != asked["redirect_uri"]
            or challenge != asked["code_challenge"]
        ):
            return 400, {"error": "invalid_grant"}
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "exp": clock.now().timestamp() + 3600,
            "nonce": asked["nonce"],
            "sub": "1234567890",
            "email": self.address,
            "email_verified": True,
        }
        if self.name is not None:
            claims["name"] = self.name
        return 200, {"id_token": id_token(claims)}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, status: int, content_type: str, body: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path != "/auth":
                    self._answer(404, "text/plain", "not found")
                    return
                query = {key: value[0] for key, value in parse_qs(parts.query).items()}
                fake.auth_queries.append(query)
                self._answer(200, "text/html; charset=utf-8", fake._page(query))

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                form = {key: value[0] for key, value in parse_qs(raw).items()}
                status, body = fake._token_answer(form)
                self._answer(status, "application/json", json.dumps(body))

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """A request line holds a code: it goes to no log."""

        return Handler


@dataclass
class Live:
    """One running app and what the test needs to reach behind it."""

    url: str
    mailer: CaptureMailer
    google: FakeGoogle
    accounts: Accounts
    shots: Path
    errors: list[str] = field(default_factory=list)

    def context(self, browser, viewport=DESKTOP):
        return browser.new_context(viewport=viewport, base_url=self.url)

    def open(self, context):
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        return page

    def shot(self, page, name: str, settle_ms: int = 400) -> None:
        page.wait_for_timeout(settle_ms)
        page.screenshot(path=str(self.shots / f"e2e-{name}.png"))


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
    for name in ("home.html", "index.html", "privacy.html"):
        assert (folder / name).is_file(), name
    return folder


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    folder = Path(os.environ.get(SHOTS_ENV) or tmp_path_factory.mktemp("shots"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(scope="module")
def accounts(driver, migrated):
    made = Accounts()
    yield made
    for world_id in made.worlds:
        purge_test_world(driver, world_id)
    for hashed in made.hashes:
        purge_test_account(driver, hashed)


@pytest.fixture(scope="module")
def google():
    fake = FakeGoogle()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def client_file(tmp_path_factory, google) -> Path:
    """A synthetic client file, in the shape of Google's: no real secret is read here."""
    path = tmp_path_factory.mktemp("google") / "google-oauth.json"
    web = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": f"{google.origin}/auth",
        "token_uri": f"{google.origin}/token",
    }
    path.write_text(json.dumps({"web": web}), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def live(driver, built_pages, shots, accounts, google, client_file):
    """The real app on a free local port, in a thread; stopped when the module ends."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]
    url = f"http://{HOST}:{port}"
    mailer = CaptureMailer()
    settings = ApiSettings(public_base_url=url, google_oauth_file=str(client_file))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(WEB_DIST_ENV, str(built_pages))
        app = create_app(settings, driver=driver, mailer=mailer, world_id_for=accounts.world_id_for)
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="grove-signin-e2e", daemon=True)
    thread.start()
    for _ in range(POLLS):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("the app stopped while it started")
        time.sleep(POLL_SECONDS)
    else:
        raise RuntimeError("the app did not start")
    yield Live(url=url, mailer=mailer, google=google, accounts=accounts, shots=shots)
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture(autouse=True)
def no_page_errors(live):
    """Every test here ends with no script error on any page it opened."""
    yield
    seen, live.errors[:] = list(live.errors), []
    assert seen == []


# The page -------------------------------------------------------------------------------------


def _wait_for_card(page) -> None:
    page.get_by_role("button", name="Continue with Google").wait_for(timeout=WAIT_MS)


def _send_code(page, address: str) -> None:
    _wait_for_card(page)
    page.fill("#gp-email", address)
    page.get_by_role("button", name="Continue with email").click()
    page.locator("#gp-code").wait_for(timeout=WAIT_MS)


def _type_code(page, code: str) -> None:
    page.fill("#gp-code", code)
    page.locator("[data-code-form] button").click()


def _email_sign_in(live: Live, page, address: str) -> None:
    """Sign in on the card that shows now; the code comes from the capture mailer."""
    _send_code(page, address)
    _type_code(page, live.mailer.last_code(address))
    page.locator(".gp-auth-form").wait_for(state="detached", timeout=WAIT_MS)


def _wait_for_welcome(page) -> None:
    page.get_by_role("button", name="Get Started").wait_for(timeout=WAIT_MS)


def _finish_onboarding(page) -> None:
    """From the welcome, through the steps and past the tour, to the map's own controls."""
    page.get_by_role("button", name="Get Started").click()
    for _ in range(4):
        page.locator(".gp-next").click()
    page.locator("[data-tour-skip]").click(timeout=WAIT_MS)
    page.locator("#profile-button").wait_for(state="visible", timeout=WAIT_MS)


def _open_account(page) -> None:
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="account"]').click()


def _log_out(page) -> None:
    _open_account(page)
    page.locator('[data-action="logout"]').click()
    _wait_for_card(page)


def _wait_for_nodes(page, ids: set[str]) -> None:
    page.wait_for_function(NODE_COUNT_JS, arg=len(ids), timeout=WAIT_MS)
    assert set(page.evaluate(NODE_IDS_JS)) == ids


def _session(page) -> dict:
    return page.evaluate(SESSION_STATUS_JS)


def _session_cookie(context) -> str | None:
    found = [c["value"] for c in context.cookies() if c["name"] == SESSION_COOKIE]
    return found[0] if found else None


def _stored(page) -> dict:
    source = (WEB / "simulation.js").read_text(encoding="utf-8")
    key = re.search(r"var STORAGE_KEY = '([^']+)'", source)
    assert key, "simulation.js no longer names its storage key"
    return page.evaluate(f"() => JSON.parse(localStorage.getItem({json.dumps(key.group(1))}))")


def _path_of(page) -> str:
    parts = urlsplit(page.url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


# The store ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """One address's rows: exactly one user and one world, or the helper fails."""

    user_id: str
    world_id: str
    self_item_id: str


def _held(driver, address: str) -> Held:
    user = user_of(driver, address)
    assert user is not None, "no user for the address"
    worlds = worlds_of(driver, user["id"])
    assert len(worlds) == 1, f"{len(worlds)} worlds for one address"
    assert worlds[0]["self_item_id"], "the world names no own item"
    return Held(user["id"], worlds[0]["id"], worlds[0]["self_item_id"])


def _own_item(driver, held: Held) -> dict:
    """The world's one item: it must be the own item, made once."""
    items = items_of(driver, held.world_id)
    assert [item["id"] for item in items] == [held.self_item_id]
    return items[0]


# The cases ------------------------------------------------------------------------------------


def test_the_homepage_leads_to_the_card(browser, live):
    context = live.context(browser)
    try:
        page = live.open(context)
        answer = page.goto("/")
        assert answer.status == 200
        assert answer.headers["vary"] == "Cookie"
        page.get_by_text(TAGLINE).first.wait_for(timeout=WAIT_MS)
        assert page.locator('a[href="/privacy"]').first.is_visible()
        assert page.locator("#grove-product").count() == 0, "the homepage holds the views"
        assert page.get_by_text("contact@x-arc.ai").first.is_visible()
        live.shot(page, "home")

        page.get_by_role("link", name="Sign in").first.click()
        _wait_for_card(page)
        assert _path_of(page) == "/signin"
        assert page.get_by_role("button", name="Continue with Apple").is_visible()
        assert page.get_by_role("link", name="Privacy").is_visible()
        live.shot(page, "card")
    finally:
        context.close()


def test_an_email_code_signs_in_logs_out_and_resumes(browser, live, driver):
    address = live.accounts.address()
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        assert user_of(driver, address) is None

        # The first sign-in: the welcome, with the own item already in the store.
        _send_code(page, address)
        live.shot(page, "code-step")
        assert address in page.locator(".gp-auth-form").inner_text()
        _type_code(page, live.mailer.last_code(address))
        _wait_for_welcome(page)
        assert _path_of(page) == "/", "the address stays on /signin after a sign-in"
        live.shot(page, "welcome")
        held = _held(driver, address)
        own = _own_item(driver, held)
        assert own["title"] == NEUTRAL_TITLE
        session = _session(page)
        assert session["status"] == 200
        assert session["body"] == {
            "signed_in": True,
            "self_item_id": held.self_item_id,
            "has_items": False,
            "account": held.user_id,
            "method": "email",
        }

        # The map draws the own item, once.
        _finish_onboarding(page)
        _wait_for_nodes(page, {SELF_VIEW_ID})
        live.shot(page, "map-own-item", 1500)
        counts = world_counts(driver, held.world_id)
        first_cookie = _session_cookie(context)
        assert first_cookie

        # Logout shows the card and leaves every row of the world as it was.
        _log_out(page)
        live.shot(page, "logged-out")
        assert _session_cookie(context) is None
        assert _session(page)["status"] == 401
        assert world_counts(driver, held.world_id) == counts
        after = _own_item(driver, held)
        assert (after["id"], after["version"]) == (own["id"], own["version"])
        assert _held(driver, address) == held
        assert all(row["revoked_at"] for row in sessions_of(driver, held.user_id))

        # With no cookie, / is the homepage again.
        page.reload()
        page.get_by_text(TAGLINE).first.wait_for(timeout=WAIT_MS)
        assert page.locator("#grove-product").count() == 0

        # The old cookie is dead, and a second sign-in resumes the same world and own item.
        page.goto("/signin")
        _email_sign_in(live, page, address)
        _wait_for_nodes(page, {SELF_VIEW_ID})
        assert page.get_by_role("button", name="Get Started").count() == 0
        assert _session_cookie(context) not in (None, first_cookie)
        assert _held(driver, address) == held
        again = _own_item(driver, held)
        assert (again["id"], again["version"]) == (own["id"], own["version"])
        assert world_counts(driver, held.world_id) == counts
        assert len(sessions_of(driver, held.user_id)) == 2
        live.shot(page, "resumed", 1500)
    finally:
        context.close()


def test_a_fresh_browser_with_an_extra_item_skips_the_welcome(browser, live, driver):
    address = live.accounts.address()
    first = live.context(browser)
    try:
        page = live.open(first)
        page.goto("/signin")
        _email_sign_in(live, page, address)
        _wait_for_welcome(page)
    finally:
        first.close()
    held = _held(driver, address)
    added = demo.add_items(driver, held.world_id, 1)

    # A new browser holds no local state: the world itself says this person has been here.
    fresh = live.context(browser)
    try:
        page = live.open(fresh)
        page.goto("/signin")
        _email_sign_in(live, page, address)
        _wait_for_nodes(page, {SELF_VIEW_ID, added[0]})
        assert page.get_by_role("button", name="Get Started").count() == 0
        assert page.locator(".gp-welcome-art").count() == 0
        stored = _stored(page)
        assert stored["onboardingComplete"] is True
        assert stored["account"] == held.user_id
        assert page.locator("[data-tour-skip]").count() == 0, "a tour over a returning person's map"
        assert _session(page)["body"]["has_items"] is True
        assert _held(driver, address) == held
        items = {item["id"] for item in items_of(driver, held.world_id)}
        assert items == {held.self_item_id, added[0]}
        live.shot(page, "fresh-browser-map", 1500)
    finally:
        fresh.close()


def _google_sign_in(live: Live, page, address: str) -> dict[str, str]:
    """Through the card and the fake's Continue. Returns what the app asked the fake for."""
    live.google.address = address
    page.get_by_role("button", name="Continue with Google").click()
    page.get_by_role("heading", name="Fake Google").wait_for(timeout=WAIT_MS)
    asked = live.google.auth_queries[-1]
    page.get_by_role("button", name="Continue").click()
    return asked


def test_google_signs_in_and_reaches_one_world_for_the_address(browser, live, driver):
    address = live.accounts.address()

    # Google first: the own item takes the name of the id token.
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _wait_for_card(page)
        asked = _google_sign_in(live, page, address)
        _wait_for_welcome(page)
        assert _path_of(page) == "/"
        assert asked["scope"] == SCOPE == "openid email profile"
        assert asked["redirect_uri"] == f"{live.url}{CALLBACK_PATH}"
        assert asked["code_challenge_method"] == "S256" and asked["code_challenge"]
        assert asked["state"] and asked["nonce"]
        assert "access_type" not in asked
        assert "grove_google" not in {cookie["name"] for cookie in context.cookies()}
        held = _held(driver, address)
        assert _own_item(driver, held)["title"] == GOOGLE_NAME
        session = _session(page)
        assert session["body"]["method"] == "google"
        assert session["body"]["account"] == held.user_id
        assert _stored(page)["auth"]["provider"] == "google"
        live.shot(page, "google-welcome")
    finally:
        context.close()

    # The email code on another browser: one person, one world, one own item.
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _email_sign_in(live, page, address)
        _wait_for_welcome(page)
        assert _held(driver, address) == held
        assert _own_item(driver, held)["title"] == GOOGLE_NAME
        assert _session(page)["body"]["method"] == "email"
    finally:
        context.close()

    # And Google again.
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _wait_for_card(page)
        _google_sign_in(live, page, address)
        _wait_for_welcome(page)
        assert _held(driver, address) == held
        _own_item(driver, held)
    finally:
        context.close()


def test_apple_says_not_available_yet(browser, live):
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _wait_for_card(page)
        page.locator('[data-signin="apple"]').click()
        page.locator('[data-signin="apple"][aria-disabled="true"]').wait_for(timeout=WAIT_MS)
        assert page.locator('[data-signin="apple"]').inner_text().strip() == "Not available yet"
        assert _path_of(page) == "/signin"
        assert _session_cookie(context) is None
        live.shot(page, "apple")
    finally:
        context.close()


def test_a_wrong_code_shows_the_error_and_the_right_one_still_works(browser, live, driver):
    address = live.accounts.address()
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _send_code(page, address)
        right = live.mailer.last_code(address)
        _type_code(page, "000000" if right != "000000" else "111111")
        alert = page.locator('.gp-auth-form [role="alert"]')
        alert.wait_for(timeout=WAIT_MS)
        assert alert.inner_text() == WRONG_CODE_TEXT
        assert _session_cookie(context) is None
        assert user_of(driver, address) is None, "a wrong code made an account"
        live.shot(page, "wrong-code")

        _type_code(page, right)
        _wait_for_welcome(page)
        assert _session_cookie(context)
    finally:
        context.close()


@pytest.mark.parametrize(
    ("label", "button", "refuse_token"),
    [("refused", "Refuse", False), ("token-error", "Continue", True)],
)
def test_a_google_failure_shows_its_error_on_the_card(
    browser, live, driver, label, button, refuse_token
):
    address = live.accounts.address()
    live.google.address = address
    live.google.refuse_token = refuse_token
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _wait_for_card(page)
        page.get_by_role("button", name="Continue with Google").click()
        page.get_by_role("heading", name="Fake Google").wait_for(timeout=WAIT_MS)
        page.get_by_role("button", name=button).click()
        alert = page.locator('.gp-auth-form [role="alert"]')
        alert.wait_for(timeout=WAIT_MS)
        assert alert.inner_text() == GOOGLE_ERROR_TEXT
        assert _path_of(page) == "/signin", "the error stays in the address"
        assert _session_cookie(context) is None
        assert "grove_google" not in {cookie["name"] for cookie in context.cookies()}
        assert _session(page)["status"] == 401
        assert user_of(driver, address) is None
        live.shot(page, f"google-{label}")
    finally:
        live.google.refuse_token = False
        context.close()


def test_a_google_callback_with_a_bad_state_shows_the_error_and_no_session(browser, live):
    context = live.context(browser)
    try:
        page = live.open(context)
        query = urlencode({"code": "a-code", "state": "not-the-state"})
        page.goto(f"{CALLBACK_PATH}?{query}")
        alert = page.locator('.gp-auth-form [role="alert"]')
        alert.wait_for(timeout=WAIT_MS)
        assert alert.inner_text() == GOOGLE_ERROR_TEXT
        assert _path_of(page) == "/signin"
        assert _session_cookie(context) is None
    finally:
        context.close()


def test_the_privacy_page_opens_from_the_card(browser, live):
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _wait_for_card(page)
        page.get_by_role("link", name="Privacy").click()
        page.get_by_role("heading", name="What Grove asks Google for, and why").wait_for(
            timeout=WAIT_MS
        )
        assert _path_of(page) == "/privacy"
        text = page.locator("body").inner_text()
        for word in ("openid", "email", "profile", "calendar.readonly", "contact@x-arc.ai"):
            assert word in text, word
        live.shot(page, "privacy")
    finally:
        context.close()


def test_a_second_person_on_one_browser_sees_nothing_of_the_first(browser, live, driver):
    first, second = live.accounts.address(), live.accounts.address()
    context = live.context(browser)
    try:
        page = live.open(context)
        page.goto("/signin")
        _email_sign_in(live, page, first)
        _finish_onboarding(page)

        # What the first person leaves here: a name, some guidance and a file.
        _open_account(page)
        page.locator('[data-action="edit-name"]').click()
        page.fill("#gp-name", GOOGLE_NAME)
        page.locator("[data-name-form] button[type=submit]").click()
        page.get_by_text(GOOGLE_NAME).first.wait_for(timeout=WAIT_MS)
        page.evaluate(LEAVE_LOCAL_STATE_JS)
        left = _stored(page)
        assert left["name"] == GOOGLE_NAME and left["growth"]["focus"] and left["files"]
        assert left["onboardingComplete"] is True
        page.locator('[data-action="logout"]').click()
        _wait_for_card(page)
        assert page.input_value("#gp-email") == "", "the first person's address stays on the card"

        # The second person starts from nothing, in the same browser.
        _email_sign_in(live, page, second)
        _wait_for_welcome(page)
        stored = _stored(page)
        assert stored["account"] == _held(driver, second).user_id
        assert stored["name"] == ""
        assert stored["growth"]["focus"] == ""
        assert stored["files"] == {}
        assert stored["onboardingComplete"] is False
        assert stored["auth"]["email"] == second
        raw = json.dumps(stored)
        for trace in (first, GOOGLE_NAME, "harbour"):
            assert trace not in raw, f"local storage keeps {trace}"
        live.shot(page, "second-person-welcome")

        _finish_onboarding(page)
        _wait_for_nodes(page, {SELF_VIEW_ID})
        _open_account(page)
        text = page.locator("#grove-product").inner_text()
        assert GOOGLE_NAME not in text and first not in text
        assert second in text
        live.shot(page, "second-person-account")

        # Two people, two worlds.
        assert _held(driver, first).world_id != _held(driver, second).world_id
    finally:
        context.close()


def test_the_card_and_the_public_pages_fit_a_phone(browser, live):
    context = live.context(browser, viewport=PHONE)
    try:
        page = live.open(context)
        for path, name in (("/", "home-390"), ("/privacy", "privacy-390")):
            page.goto(path)
            page.wait_for_load_state("networkidle")
            wide = page.evaluate("() => document.documentElement.scrollWidth")
            assert wide <= PHONE["width"], f"{path} scrolls sideways: {wide} px"
            live.shot(page, name)

        page.goto("/signin")
        _wait_for_card(page)
        box = page.locator(".gp-auth").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], box
        live.shot(page, "card-390")
    finally:
        context.close()
