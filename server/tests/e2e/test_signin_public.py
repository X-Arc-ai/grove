"""Sign-in against the public host: read only, except one mail to the provider's test address.

Runs only when GROVE_E2E_PUBLIC_URL names the host, after a deploy, and never in CI:

    cd server && GROVE_E2E_PUBLIC_URL=https://<host> GROVE_E2E_SHOTS=<folder> \
        uv run --with playwright==1.58.0 python -m pytest -m browser \
        tests/e2e/test_signin_public.py -q -p no:cacheprovider

With no URL, a run of tests/e2e skips this file, and a run that names it fails and says what is
missing, so a run meant for the public host can never look green and test nothing.

It stops at Google's page: it never types a credential. It never signs in, so it makes no account.
Its one send goes to delivered@resend.dev, and the code stays with the provider.
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

try:
    from playwright import sync_api
except ImportError:
    sync_api = None  # a default run deselects every test here, so it never needs Playwright

pytestmark = pytest.mark.browser

URL_ENV = "GROVE_E2E_PUBLIC_URL"
SHOTS_ENV = "GROVE_E2E_SHOTS"
RIGHT_COMMAND = "uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e"
THIS_FILE = "test_signin_public"
NO_URL = f"{URL_ENV} is not set: it must name the public host for these tests"

GOOGLE_HOST = "accounts.google.com"
CALLBACK_PATH = "/v1/auth/google/callback"
TEST_ADDRESS = "delivered@resend.dev"
# A send's real code is six random digits, so this one is wrong in all but one run of a million.
WRONG_CODE = "000000"
WRONG_CODE_TEXT = "That code doesn’t match. Check it and try again."
GOOGLE_ERROR_TEXT = "Google sign-in did not finish. Try again."
PRIVACY_HEADING = "What Grove asks Google for, and why"
WAIT_MS = 30_000


def _names_this_file(config: pytest.Config) -> bool:
    return any(THIS_FILE in str(arg) for arg in config.args) or "public" in (
        config.getoption("keyword") or ""
    )


@pytest.fixture(scope="module")
def public_url(request) -> str:
    """Before anything else: a run with no host needs neither Playwright nor a browser."""
    url = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if url:
        return url
    if _names_this_file(request.config):
        pytest.fail(NO_URL, pytrace=False)
    pytest.skip(NO_URL)


@pytest.fixture(scope="module")
def browser(public_url):
    if sync_api is None:
        pytest.fail(
            f"the browser tests selected with no Playwright installed. Run: {RIGHT_COMMAND}",
            pytrace=False,
        )
    with sync_api.sync_playwright() as playwright:
        opened = playwright.chromium.launch()
        yield opened
        opened.close()


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    folder = Path(os.environ.get(SHOTS_ENV) or tmp_path_factory.mktemp("shots"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def page(browser, public_url):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, base_url=public_url)
    yield context.new_page()
    context.close()


def _shot(page, shots: Path, name: str) -> None:
    page.wait_for_timeout(400)
    page.screenshot(path=str(shots / f"public-{name}.png"))


def _wait_for_card(page) -> None:
    page.get_by_role("button", name="Continue with Google").wait_for(timeout=WAIT_MS)


def test_root_enters_login_with_signup_and_privacy_links(page, shots):
    redirect = page.request.get("/", max_redirects=0)
    assert redirect.status == 303
    assert redirect.headers["location"] == "/login"
    answer = page.goto("/")
    assert answer.status == 200
    assert urlsplit(page.url).path == "/login"
    _wait_for_card(page)
    assert page.locator('a[href="/privacy"]').first.is_visible()
    assert page.locator('a[href="/signup"]').first.is_visible()
    _shot(page, shots, "root-login")


def test_the_privacy_page(page, shots):
    answer = page.goto("/privacy")
    assert answer.status == 200
    page.get_by_role("heading", name=PRIVACY_HEADING).wait_for(timeout=WAIT_MS)
    text = page.locator("body").inner_text()
    for word in ("openid", "profile", "calendar.readonly", "contact@x-arc.ai"):
        assert word in text, word
    _shot(page, shots, "privacy")


def test_the_card_shows_on_signin(page, shots):
    page.goto("/signin")
    _wait_for_card(page)
    assert page.locator("#gp-email").is_visible()
    _shot(page, shots, "card")


def test_google_takes_the_request_and_the_test_stops_at_its_page(page, shots, public_url):
    """The one question only Google can answer: is this callback registered for this client?"""
    asked: list[str] = []
    page.on(
        "request",
        lambda request: (
            asked.append(request.url) if urlsplit(request.url).hostname == GOOGLE_HOST else None
        ),
    )
    page.goto("/signin")
    _wait_for_card(page)
    page.get_by_role("button", name="Continue with Google").click()
    page.wait_for_url(lambda url: urlsplit(url).hostname == GOOGLE_HOST, timeout=WAIT_MS)
    page.wait_for_load_state("domcontentloaded")
    _shot(page, shots, "google")

    assert asked, "no request reached Google"
    query = {key: value[0] for key, value in parse_qs(urlsplit(asked[0]).query).items()}
    assert query["scope"] == "openid email profile"
    assert query["redirect_uri"] == f"{public_url}{CALLBACK_PATH}"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    assert query["state"] and query["nonce"]
    assert "access_type" not in query
    assert "redirect_uri_mismatch" not in page.content(), "Google refused the callback"


def test_the_one_send_reaches_the_code_step_and_a_wrong_code_is_refused(page, shots):
    """One send only: this is the one test here that writes, and the code row is all it writes."""
    page.goto("/signin")
    _wait_for_card(page)
    page.fill("#gp-email", TEST_ADDRESS)
    page.get_by_role("button", name="Continue with email").click()
    page.get_by_role("heading", name="Check your inbox").wait_for(timeout=WAIT_MS)
    _shot(page, shots, "code-step")

    page.fill("#gp-code", WRONG_CODE)
    page.locator("[data-code-form] button").click()
    alert = page.locator('.gp-auth-form [role="alert"]')
    alert.wait_for(timeout=WAIT_MS)
    assert alert.inner_text() == WRONG_CODE_TEXT
    _shot(page, shots, "wrong-code")


def test_a_bad_state_at_the_callback_goes_to_the_card_with_the_error(page, shots):
    page.goto(f"{CALLBACK_PATH}?code=a-code&state=not-the-state")
    alert = page.locator('.gp-auth-form [role="alert"]')
    alert.wait_for(timeout=WAIT_MS)
    assert alert.inner_text() == GOOGLE_ERROR_TEXT
    assert urlsplit(page.url).path == "/signin"
    assert urlsplit(page.url).query == "", "the error stays in the address"
    _shot(page, shots, "google-error")


def test_the_session_view_with_no_cookie_is_401(page):
    assert page.request.get("/v1/views/session").status == 401
