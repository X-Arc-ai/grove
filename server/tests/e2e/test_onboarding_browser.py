"""Truthful setup on the real local app, with synthetic accounts and captured email codes."""

import pytest

# Pytest resolves these imported fixture names against the test parameters.
# ruff: noqa: F811
# Reuse the established local app and browser fixtures; no external credentials or mail.
from tests.e2e.test_signin_browser import (  # noqa: F401
    PHONE,
    _email_sign_in,
    _session,
    accounts,
    browser,
    built_pages,
    client_file,
    google,
    live,
    no_page_errors,
    playwright_is_here,
    shots,
)

pytestmark = pytest.mark.browser

STATE_JS = """() => {
  const s = window.GroveSimulation.getState();
  return Object.fromEntries(['sources', 'accountImports', 'pendingExports',
    'connectedHosts', 'mcpClients', 'files'].map(k => [k, s[k]]));
}"""


def _start(page, live):
    page.goto("/signin")
    _email_sign_in(live, page, live.accounts.address())
    page.get_by_role("button", name="Get Started").click()


def _skip_sources(page):
    for step in range(1, 4):
        assert page.locator(".gp-stepper").get_attribute("aria-label") == f"Step {step} of 4"
        page.get_by_role("button", name="Skip for now", exact=True).click()


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, PHONE])
def test_skip_preserves_pending_exports_without_source_or_grant(browser, live, viewport):
    context = live.context(browser, viewport=viewport)
    try:
        page = live.open(context)
        _start(page, live)
        page.evaluate("() => window.GroveSimulation.requestExport('chatgpt')")
        before = page.evaluate(STATE_JS)
        session = _session(page)
        writes = []
        page.on(
            "request",
            lambda request: writes.append(request.url) if request.method != "GET" else None,
        )
        for step in range(1, 4):
            skip = page.get_by_role("button", name="Skip for now", exact=True)
            assert skip.is_visible()
            assert (
                page.locator(
                    "[data-import], [data-integration], [data-files-form], [data-note-form]"
                ).count()
                == 0
            )
            live.shot(page, f"setup-skip-{viewport['width']}-{step}")
            skip.focus()
            page.keyboard.press("Enter")
            assert page.evaluate(STATE_JS) == before
            assert not page.evaluate("() => window.GroveSimulation.getState().onboardingComplete")
        assert writes == []
        assert _session(page) == session
        page.reload()
        page.get_by_role("heading", name="Take Grove with you").wait_for()
        assert page.evaluate(STATE_JS) == before
        assert page.locator('[data-action="skip"]').count() == 0
    finally:
        context.close()


def test_muse_directions_and_copy_never_create_a_connection(browser, live):
    context = live.context(browser)
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    try:
        page = live.open(context)
        _start(page, live)
        _skip_sources(page)
        before = page.evaluate(STATE_JS)
        writes = []
        page.on(
            "request",
            lambda request: writes.append(request.url) if request.method != "GET" else None,
        )
        muse = page.locator('[data-host-inline="muse"]')
        muse.focus()
        page.keyboard.press("Enter")
        page.get_by_role("heading", name="Use Grove in Muse Desktop").wait_for()
        assert "consent" in page.locator(".gp-connect-dialog").inner_text()
        assert page.get_by_label("Grove MCP server URL").input_value() == live.url + "/mcp"
        assert page.locator('[data-action="host-connect"]').count() == 0
        copy = page.get_by_role("button", name="Copy", exact=True)
        copy.focus()
        page.keyboard.press("Enter")
        page.get_by_role("button", name="Copied", exact=True).wait_for()
        assert page.evaluate("() => navigator.clipboard.readText()") == live.url + "/mcp"
        assert page.evaluate(STATE_JS) == before
        assert writes == []
        live.shot(page, "setup-muse-directions")
        page.keyboard.press("Escape")
        page.get_by_role("heading", name="Take Grove with you").wait_for()
        assert page.locator(".gp-host-choice.is-connected").count() == 0
    finally:
        context.close()


def test_cached_host_and_custom_client_never_show_live_success(browser, live):
    context = live.context(browser)
    try:
        page = live.open(context)
        _start(page, live)
        _skip_sources(page)
        # A prior simulation version can leave these flags in this account's browser storage.
        page.evaluate("""() => {
          window.GroveSimulation.connectHost('muse');
          window.GroveSimulation.addMcpClient('Cached custom app');
        }""")
        page.reload()
        page.get_by_role("heading", name="Take Grove with you").wait_for()
        assert page.locator(".gp-host-choice.is-connected").count() == 0
        assert "Grove is connected" not in page.locator("#grove-product").inner_text()
        summary = page.locator("[data-mcp-details] summary")
        summary.focus()
        page.keyboard.press("Enter")
        url = page.get_by_label("Grove MCP server URL")
        assert url.input_value() == live.url + "/mcp"
        assert page.locator("[data-mcp-form], [data-mcp-remove]").count() == 0
        assert "Cached custom app" not in page.locator("#grove-product").inner_text()
        live.shot(page, "setup-custom-directions")
        # After setup the shared host controls also ignore browser-local connection flags.
        page.get_by_role("button", name="Enter your Grove").click()
        page.locator('[data-action="connections"]').click()
        assert page.locator(".gp-host-choice.is-connected").count() == 0
        assert page.locator("[data-mcp-form], [data-mcp-remove]").count() == 0
    finally:
        context.close()
