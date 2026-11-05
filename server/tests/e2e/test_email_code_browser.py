"""The email code in a real browser: the card waits while a person goes to read the inbox.

Runs only when asked (`-m browser`), never in CI. `python -m pytest` matters, as in
test_views_browser.py:

    cd server && GROVE_E2E_SHOTS=<folder> uv run --with playwright==1.58.0 \
        python -m pytest -m browser tests/e2e/test_email_code_browser.py -q -p no:cacheprovider

The real app, the real store and the built page, on a free local port. The one fake is the mail
sink: it keeps each code in place of sending it, and a test reads the code from there. Addresses
are invented (example.test). Every account and world a test makes is purged at its end.

A headless page does not get a window focus event of its own, so the second-tab test fires the
event by hand. The listener, the session request and the server's 401 are all real, and the test
fails if that 401 does not come.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from grove.api.app import WEB_DIST_ENV, create_app
from grove.api.config import ApiSettings
from grove.writer.testing import purge_test_account, purge_test_world
from tests.auth.conftest import Accounts, CaptureMailer

try:
    from playwright import sync_api
except ImportError:
    sync_api = None  # a default run deselects every test here, so it never needs Playwright

pytestmark = pytest.mark.browser

RIGHT_COMMAND = "uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e"

REPO = Path(__file__).resolve().parents[3]
SHOTS_ENV = "GROVE_E2E_SHOTS"
HOST = "127.0.0.1"
VIEWPORT = {"width": 1440, "height": 900}
SESSION_PATH = "/v1/views/session"
WAIT_MS = 30_000
POLL_SECONDS = 0.25
POLLS = 120
PARTIAL_CODE = "12"
SESSION_JS = """async () => {
  const response = await fetch('/v1/views/session', {credentials: 'same-origin'});
  return {status: response.status, body: await response.json()};
}"""


@pytest.fixture(scope="session", autouse=True)
def playwright_is_here():
    """Runs before any other fixture of a selected test, so the store is not touched either."""
    if sync_api is None:
        pytest.fail(
            f"the browser tests selected with no Playwright installed. Run: {RIGHT_COMMAND}",
            pytrace=False,
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    folder = Path(os.environ.get(SHOTS_ENV) or tmp_path_factory.mktemp("shots"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(scope="module")
def built_page() -> Path:
    web = REPO / "web"
    subprocess.run(
        [sys.executable, str(web / "build.py")], cwd=REPO, check=True, capture_output=True
    )
    assert (web / "dist" / "index.html").is_file()
    return web / "dist"


@pytest.fixture(scope="module")
def accounts(driver, migrated):
    made = Accounts()
    yield made
    for world_id in made.worlds:
        purge_test_world(driver, world_id)
    for hashed in made.hashes:
        purge_test_account(driver, hashed)


@pytest.fixture(scope="module")
def mail_sink() -> CaptureMailer:
    return CaptureMailer()


@pytest.fixture(scope="module")
def base_url(driver, built_page, accounts, mail_sink):
    """The real app on a free local port, in a thread; stopped when the module ends."""
    port = _free_port()
    url = f"http://{HOST}:{port}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(WEB_DIST_ENV, str(built_page))
        app = create_app(
            ApiSettings(public_base_url=url),
            driver=driver,
            mailer=mail_sink,
            world_id_for=accounts.world_id_for,
        )
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="grove-email-e2e", daemon=True)
    thread.start()
    for _ in range(POLLS):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("the app stopped while it started")
        time.sleep(POLL_SECONDS)
    else:
        raise RuntimeError("the app did not start")
    yield url
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        opened = playwright.chromium.launch()
        yield opened
        opened.close()


def _ask_for_a_code(page, address: str) -> None:
    page.goto("/login")
    page.fill("#gp-email", address)
    page.get_by_role("button", name="Continue with email").click()
    page.locator("#gp-code").wait_for(timeout=WAIT_MS)


def _expect_session_401(page):
    return page.expect_response(
        lambda r: r.url.endswith(SESSION_PATH) and r.status == 401, timeout=WAIT_MS
    )


def _leave_for_a_second_tab(page, context, base_url) -> None:
    inbox = context.new_page()
    inbox.goto(f"{base_url}/privacy")
    inbox.close()
    page.bring_to_front()
    with _expect_session_401(page):
        page.evaluate("window.dispatchEvent(new Event('focus'))")


def _leave_in_the_same_tab(page, context, base_url) -> None:
    page.get_by_role("link", name="Privacy").click()
    page.wait_for_url("**/privacy", timeout=WAIT_MS)
    with _expect_session_401(page):
        page.go_back()


def _reload(page, context, base_url) -> None:
    with _expect_session_401(page):
        page.reload()


LEAVES = {
    "second-tab": _leave_for_a_second_tab,
    "same-tab": _leave_in_the_same_tab,
    "reload": _reload,
}


@pytest.mark.parametrize("leave", LEAVES)
def test_the_code_card_stays_and_the_code_signs_in(
    browser, base_url, accounts, mail_sink, shots, leave
):
    errors: list[str] = []
    address = accounts.address()
    context = browser.new_context(viewport=VIEWPORT, base_url=base_url)
    try:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        _ask_for_a_code(page, address)
        page.screenshot(path=str(shots / f"{leave}-1-card.png"))
        page.fill("#gp-code", PARTIAL_CODE)
        page.evaluate("window.codeField = document.querySelector('#gp-code')")

        LEAVES[leave](page, context, base_url)
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots / f"{leave}-2-returned.png"))

        assert page.locator("#gp-code").is_visible(), "the code card is gone after the return"
        assert address in page.locator(".gp-auth-intro").inner_text()
        assert page.locator("[data-email-form]").count() == 0
        if leave == "second-tab":
            assert page.input_value("#gp-code") == PARTIAL_CODE, "the typed digits are gone"
            assert page.evaluate("window.codeField.isConnected"), "the card was drawn again"
        assert len([to for to, _ in mail_sink.sent if to == address]) == 1

        page.fill("#gp-code", mail_sink.last_code(address))
        page.locator("[data-code-form] button").click()
        page.locator("#gp-code").wait_for(state="detached", timeout=WAIT_MS)
        session = page.evaluate(SESSION_JS)
        page.screenshot(path=str(shots / f"{leave}-3-signed-in.png"))

        assert session["status"] == 200
        assert session["body"]["signed_in"] is True
        assert page.evaluate("Object.keys(sessionStorage)") == []
        assert errors == []
    finally:
        context.close()
