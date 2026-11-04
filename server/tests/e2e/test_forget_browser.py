"""Real browser regression of D14 review/confirm and privacy configuration rendering.

The app serves built pages. HTTP fixtures isolate the UI from the cascade and use only
synthetic data. Select with -m browser; these tests do not claim physical deletion proof.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.privacy import flow_statement
from tests.hosts import flow

try:
    from playwright import sync_api
except ImportError:
    sync_api = None

pytestmark = pytest.mark.browser
REPO = Path(__file__).resolve().parents[3]
ITEM = "00000000-0000-7000-8000-000000000001"
PHRASE = "Synthetic Juniper phrase"
MUTATIONS = re.compile(r"/v1/forget(?:/approve)?$")


@pytest.fixture(scope="module")
def browser():
    if sync_api is None:
        pytest.fail("The selected browser tests require the existing Playwright environment")
    with sync_api.sync_playwright() as playwright:
        opened = playwright.chromium.launch()
        yield opened
        opened.close()


@pytest.fixture(scope="module")
def live():
    subprocess.run(
        [sys.executable, str(REPO / "web" / "build.py")], check=True, capture_output=True
    )
    port = flow.free_port()
    url = f"http://{flow.HOST}:{port}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GROVE_WEB_DIST", str(REPO / "web" / "dist"))
        app = create_app(ApiSettings(public_base_url=url), driver=object())
        with flow.serving(app, port):
            yield url


@pytest.fixture
def screen(browser, live):
    context = browser.new_context(base_url=live, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert errors == []


def session(page):
    page.route("**/v1/views/session", lambda route: route.fulfill(json={"signed_in": True}))
    page.route(
        "**/v1/views/graph",
        lambda route: route.fulfill(
            json={"elements": {"nodes": [{"data": {"id": ITEM, "label": "Juniper plan"}}]}}
        ),
    )


def shot(page, tmp_path, name):
    folder = Path(os.environ.get("GROVE_E2E_SHOTS", str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def test_review_and_cancel_make_no_write_then_confirmation_clears_content(screen, tmp_path):
    page = screen
    session(page)
    calls = []

    def mutation(route):
        calls.append((route.request.url.rsplit("/", 1)[-1], route.request.post_data_json))
        if route.request.url.endswith("/approve"):
            route.fulfill(json={"approval_token": "synthetic-one-use-approval"})
        else:
            route.fulfill(
                json={
                    "counts": {"items": 1, "history_values": 2},
                    "backups_clear_after": "2026-12-04",
                }
            )

    page.route(MUTATIONS, mutation)
    page.goto("/forget")
    page.get_by_label("Word or phrase", exact=True).fill(PHRASE)
    shot(page, tmp_path, "forget-select-light")
    page.get_by_role("button", name="Review removal").click()
    assert page.locator("#choice").inner_text() == "Word or phrase: " + PHRASE
    assert calls == []
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert calls == []
    page.get_by_role("button", name="Review removal").click()
    page.emulate_media(color_scheme="dark")
    shot(page, tmp_path, "forget-confirm-dark")
    page.get_by_role("button", name="Confirm and forget").click()
    page.get_by_role("heading", name="Removal recorded").wait_for()
    assert [name for name, _ in calls] == ["approve", "forget"]
    assert calls[0][1] == {"target": {"type": "named_value", "value": PHRASE}, "confirm": True}
    assert calls[1][1] == {
        "target": {"type": "named_value", "value": PHRASE},
        "approval_token": "synthetic-one-use-approval",
    }
    assert PHRASE not in page.locator("body").inner_text()
    assert page.locator("#named-value").input_value() == ""
    assert "3 stored entries changed" in page.locator("#receipt").inner_text()
    assert "2026-12-04" in page.locator("#receipt").inner_text()
    assert page.evaluate("[localStorage.length, sessionStorage.length]") == [0, 0]
    shot(page, tmp_path, "forget-receipt-dark")


def test_item_field_keeps_opaque_id_out_of_display_and_sends_exact_target(screen):
    page = screen
    session(page)
    calls = []

    def refuse(route):
        calls.append(route.request.post_data_json)
        route.fulfill(status=503, json={"error": {"code": "unavailable"}})

    page.route(MUTATIONS, refuse)
    page.goto("/forget")
    page.get_by_label("What must Grove forget?").select_option("field_value")
    page.get_by_label("Item", exact=True).select_option(ITEM)
    page.get_by_label("Field", exact=True).select_option("summary")
    page.get_by_role("button", name="Review removal").click()
    assert ITEM not in page.locator("body").inner_text()
    assert "Juniper plan" in page.locator("#choice").inner_text()
    page.get_by_role("button", name="Confirm and forget").click()
    page.get_by_text("Forget is not available on this server.", exact=False).wait_for()
    assert calls == [
        {"target": {"type": "field_value", "item_id": ITEM, "field": "summary"}, "confirm": True}
    ]
    assert page.locator("#receipt").is_hidden()
    assert page.locator("#forget-form").is_visible()


def test_signed_out_user_gets_return_link_without_write_controls(screen):
    page = screen
    page.route(
        "**/v1/views/session",
        lambda route: route.fulfill(status=401, json={"error": {"code": "unauthenticated"}}),
    )
    page.goto("/forget")
    link = page.get_by_role("link", name="Sign in to continue")
    link.wait_for()
    assert link.get_attribute("href") == "/signin?next=%2Fforget"
    assert page.locator("#forget-form").is_hidden()


def test_network_failure_never_retries_or_claims_success(screen):
    page = screen
    session(page)
    calls = []

    def unknown(route):
        calls.append(route.request.url)
        if route.request.url.endswith("/approve"):
            route.fulfill(json={"approval_token": "synthetic-one-use-approval"})
        else:
            route.abort("failed")

    page.route(MUTATIONS, unknown)
    page.goto("/forget")
    page.get_by_label("Word or phrase", exact=True).fill(PHRASE)
    page.get_by_role("button", name="Review removal").click()
    page.get_by_role("button", name="Confirm and forget").click()
    page.get_by_text("The request result is unknown.", exact=False).wait_for()
    page.wait_for_timeout(300)
    assert len(calls) == 2
    assert page.locator("#receipt").is_hidden()


def test_privacy_runtime_status_and_mobile_layout_keep_limits_visible(screen, tmp_path):
    page = screen
    page.route(
        "**/v1/privacy",
        lambda route: route.fulfill(
            json=flow_statement({"MODEL_API_KEY": "synthetic"}, runtime=True)
        ),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/privacy")
    page.get_by_text(
        "The status column uses this server's current configuration.", exact=False
    ).wait_for()
    assert "Settings present" in page.locator(".flow-table").inner_text()
    assert "Implementation pending" in page.locator(".flow-table").inner_text()
    assert "does not prove a successful connection" in page.locator("#flow-state").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, tmp_path, "privacy-mobile-light")
    page.emulate_media(color_scheme="dark")
    shot(page, tmp_path, "privacy-mobile-dark")


def test_static_privacy_fallback_never_claims_deployment_when_request_fails(screen):
    screen.route("**/v1/privacy", lambda route: route.abort("failed"))
    screen.goto("/privacy")
    assert "Deployment is not checked" in screen.locator("#flow-state").inner_text()
    assert "Deployment not checked" in screen.locator(".flow-table").inner_text()
