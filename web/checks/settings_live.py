"""Settings, Health, refresh and the three onboarding steps, in a real browser.

Local synthetic API responses only. Each check runs on its own page, at 1440 and 390 px, in light
and dark, so a red names the exact comment it belongs to. Every world here holds the connector row
of an ordinary world: type person_in_views, provider null. That row crashed the page.
"""

import argparse
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from onboarding_sequence import OnboardingHandler
from ui_fidelity import Preview

SELF_ROW = {
    "connector_id": "01a1a8f5-4aeb-7737-ae71-5307c712a99c",
    "type": "person_in_views",
    "provider": None,
    "last_attempted_read": None,
    "last_successful_read": None,
    "last_outcome": None,
    "read_count": 0,
    "failed_count": 0,
    "oldest_unprocessed_item": None,
    "watermark": None,
    "contribution": {"sources": 0, "nodes": 0, "edges": 0, "item_ids": []},
}
CHATGPT_ID = "01a1a8f7-4c34-77da-97e1-67909d733822"


def chatgpt_row():
    return {
        "connector_id": CHATGPT_ID,
        "type": "past_ai_conversations",
        "provider": "chatgpt",
        "last_attempted_read": "2026-11-04T23:49:01+00:00",
        "last_successful_read": "2026-11-04T23:49:01+00:00",
        "last_outcome": "took_in",
        "read_count": 3,
        "failed_count": 0,
        "oldest_unprocessed_item": None,
        "watermark": None,
        "contribution": {
            "sources": 3,
            "nodes": 12,
            "edges": 30,
            "item_ids": ["p002", "p003"],
        },
    }


def workers():
    agents = [
        {
            "agent": name,
            "policy": {"state": "allowed", "reason": "ordinary_world_policy"},
            "capability": {"state": state, "reason": reason},
            "activity": {"state": "idle", "running_records": 0, "stale_records": 0},
        }
        for name, state, reason in (
            ("learner", "limited", "legacy_learner_only"),
            ("curator", "unavailable", "curator_authority_unavailable"),
            ("connector", "limited", "legacy_connector_only"),
        )
    ]
    return {
        "schema": "grove.worker-activity.v1",
        "observed_at": "2026-11-04T23:47:47.660137Z",
        "observation_kind": "stored_run_records",
        "service_state": "unknown",
        "service_reason": "no_process_heartbeat",
        "agents": agents,
        "recent_runs": [],
        "recent_limit": 20,
        "recent_has_more": False,
    }


class SettingsHandler(OnboardingHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        self.server.gets.append(path)
        if path == "/v1/views/graph":
            time.sleep(self.server.graph_delay)
        if path == "/v1/sources/connectors":
            return self.send({"connectors": self.server.rows})
        if path == "/v1/views/health":
            return self.send(self.server.health)
        if path == "/v1/views/workers":
            return self.send(workers())
        if path == "/v1/materials":
            return self.send(
                {
                    "account": self.server.account,
                    "generation": 1,
                    "materials": [],
                    "next_cursor": None,
                }
            )
        if path == "/v1/views/pipedream/accounts":
            return self.send(
                {
                    "account": self.server.account,
                    "configured": True,
                    "providers": [
                        {
                            "provider": "gmail",
                            "name": "Gmail",
                            "status": self.server.gmail,
                        },
                        {
                            "provider": "notion",
                            "name": "Notion",
                            "status": "not_connected",
                        },
                    ],
                }
            )
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/v1/forget/approve", "/v1/forget") or path.endswith(
            ("/gmail/disconnect", "/gmail/connect")
        ):
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}")
            self.server.posts.append((path, body))
            if path == "/v1/forget/approve":
                return self.send({"approval_token": "gfa_synthetic", "one_use": True})
            if path == "/v1/forget":
                self.server.rows = [SELF_ROW]
                return self.send({"receipt_id": "r1", "counts": {"sources": 3}})
            account = self.server.account
            if path.endswith("/gmail/disconnect"):
                self.server.gmail = "not_connected"
                return self.send(
                    {"account": account, "provider": "gmail", "status": "not_connected"}
                )
            return self.send(
                {
                    "account": account,
                    "provider": "gmail",
                    "connect_url": "https://pipedream.com/_static/connect.html?token=synthetic",
                }
            )
        return super().do_POST()


def reset(server, has_items=True):
    Preview.reset(server)
    server.signed_in = True
    server.has_items = has_items
    server.connections = 0
    server.positions = []
    server.agent_files = [{"name": "plan.pdf", "kind": "file", "bytes": 6}]
    server.rows = [chatgpt_row(), SELF_ROW]
    server.health = {
        "status": "healthy",
        "nodes": 222,
        "edges": 413,
        "by_type": {"person": 1},
        "overall": {"state": "healthy"},
        "growth": [
            {"at": "2026-11-02", "nodes": 100, "edges": 200},
            {"at": "2026-11-03", "nodes": 180, "edges": 350},
            {"at": "2026-11-04", "nodes": 222, "edges": 413},
        ],
        "additions": {"nodes": 7, "edges": 9, "since": "2026-11-03T23:00:00Z"},
    }
    server.gmail = "connected"
    server.gets = []
    server.posts = []
    server.graph_delay = 0


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), SettingsHandler)
    server.reference = None
    reset(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def opened(page, base, theme):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/")
    page.wait_for_function(
        "GroveSimulation.getState().auth&&GroveSimulation.getState().onboardingComplete"
    )
    page.evaluate("GroveSimulation.completeTour&&GroveSimulation.completeTour()")
    page.locator("#stats").wait_for()
    page.wait_for_timeout(500)
    if theme == "dark":
        page.locator("#theme-toggle").click()
    return errors


def check_account_icon(page, base, server, shots, label, expect, theme):
    errors = opened(page, base, theme)
    button = page.locator("#profile-button")
    assert button.locator("svg circle").count() == 1, (
        "The account control shows a user icon"
    )
    assert button.inner_text().strip() == "", "The account control holds no letter"
    button.click()
    expect(page.locator(".gp-settings")).to_be_visible()
    assert not errors, errors
    page.screenshot(path=str(shots / f"{label}-account.png"))
    page.keyboard.press("Escape")
    expect(page.locator(".gp-settings")).to_have_count(0)
    button.click()
    expect(page.locator(".gp-settings")).to_be_visible()
    assert not errors, errors


def check_health(page, base, server, shots, label, expect, theme):
    errors = opened(page, base, theme)
    page.locator("#health-link").click()
    dialog = page.locator(".gp-health-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator('[data-live-count="nodes"]')).to_have_text("222")
    expect(page.locator('[data-live-count="edges"]')).to_have_text("413")
    expect(page.locator('[data-growth-chart="nodes"] polyline')).to_have_count(1)
    expect(page.locator('[data-growth-chart="edges"] polyline')).to_have_count(1)
    expect(page.locator('[data-live-added="nodes"]')).to_have_text("+7")
    expect(dialog).not_to_contain_text("Changes since previous graph check")
    groups = page.locator(".gp-health-source-label")
    expect(groups.nth(0)).to_contain_text("1")
    expect(groups.nth(1)).to_contain_text("1")
    expect(groups.nth(2)).to_contain_text("1")
    expect(dialog).to_contain_text("Last checked")
    expect(page.locator(".gp-health-freshness strong")).not_to_have_text("—")
    assert not errors, errors
    page.screenshot(path=str(shots / f"{label}-health.png"))
    server.health = dict(server.health, nodes=230)
    page.get_by_role("button", name="Check health", exact=True).click()
    expect(page.locator('[data-live-count="nodes"]')).to_have_text("230")
    assert not errors, errors


def check_sources(page, base, server, shots, label, expect, theme):
    context = page.context
    context.route(
        "https://pipedream.com/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<h1>Synthetic</h1>"
        ),
    )
    errors = opened(page, base, theme)
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    chat = page.locator('[data-live-row="chatgpt"]')
    expect(chat).to_contain_text("12")
    expect(chat).to_contain_text("nodes")
    expect(chat).to_contain_text("30")
    expect(chat).to_contain_text("connections")
    expect(page.locator(".gp-settings")).to_contain_text("Gmail")
    expect(page.locator('[data-agent-files="file"]')).to_contain_text("plan.pdf")
    assert not errors, errors
    page.screenshot(path=str(shots / f"{label}-sources.png"))

    # Disconnect, then reconnect, a synced app.
    page.get_by_role("button", name="Disconnect Gmail", exact=True).click()
    page.locator('.gp-confirm-actions [data-action="confirm-delete"]').click()
    reconnect = page.get_by_role("button", name="Reconnect Gmail", exact=True)
    expect(reconnect).to_be_visible()
    assert (
        "/v1/views/pipedream/gmail/disconnect",
        {"confirm": True},
    ) in server.posts, server.posts
    with context.expect_page() as popup:
        reconnect.click()
    popup.value.wait_for_url("https://pipedream.com/**")
    assert any(path.endswith("/gmail/connect") for path, _ in server.posts), (
        server.posts
    )
    popup.value.close()

    # Remove an uploaded source through the two D14 steps.
    page.get_by_role("button", name="Remove ChatGPT", exact=True).click()
    page.screenshot(path=str(shots / f"{label}-sources-remove.png"))
    page.locator('.gp-confirm-actions [data-action="confirm-delete"]').click()
    expect(page.locator(".gp-settings")).to_contain_text(
        "ChatGPT was removed from your Grove"
    )
    target = {"type": "connector", "connector_id": CHATGPT_ID}
    assert (
        "/v1/forget/approve",
        {"target": target, "confirm": True},
    ) in server.posts, server.posts
    assert (
        "/v1/forget",
        {"target": target, "approval_token": "gfa_synthetic"},
    ) in server.posts, server.posts
    expect(page.locator('[data-live-row="chatgpt"]')).to_have_count(0)

    # A file added in setup can be removed here too.
    page.get_by_role("button", name="Remove plan.pdf", exact=True).click()
    expect(page.locator('[data-agent-files="file"]')).not_to_contain_text("plan.pdf")
    assert not errors, errors


def check_refresh(page, base, server, shots, label, expect, theme):
    errors = opened(page, base, theme)
    button = page.locator("#refresh-data")
    assert button.get_attribute("aria-label") == "Refresh your Grove"
    server.graph_delay = 0.4
    server.health = dict(server.health, nodes=240)
    server.gets = []
    button.click()
    turning = page.evaluate(
        "getComputedStyle(document.querySelector('#refresh-data svg')).animationName"
    )
    assert turning != "none", "The icon must move while the refresh runs"
    note = page.locator("#gp-refresh-note")
    expect(note).to_contain_text("up to date", timeout=4000)
    for path in (
        "/v1/views/graph",
        "/v1/views/health",
        "/v1/sources/connectors",
        "/v1/views/workers",
    ):
        assert path in server.gets, (path, server.gets)
    expect(button).not_to_have_class("is-refreshing")
    page.screenshot(path=str(shots / f"{label}-refresh.png"))
    server.graph_delay = 0
    assert not errors, errors


def check_three_steps(page, base, server, shots, label, expect, theme):
    server.has_items = False
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base + "/")
    page.get_by_role("button", name="Get Started", exact=True).click()
    number = page.locator(".gp-step-number")
    expect(number).to_have_text("01 / 03")
    assert page.locator(".gp-step-track i").count() == 3
    expect(page.locator(".gp-stepper")).to_have_attribute("aria-label", "Step 1 of 3")
    page.get_by_role("button", name="Next step", exact=True).click()
    expect(number).to_have_text("02 / 03")
    page.get_by_role("button", name="Next step", exact=True).click()
    expect(number).to_have_text("03 / 03")
    expect(page.get_by_role("heading", name="Add what matters")).to_be_visible()
    page.screenshot(path=str(shots / f"{label}-step-3.png"))
    assert page.get_by_role("button", name="Skip for now", exact=True).count() == 0
    # A browser that stopped on the old fourth card comes back to the last step.
    assert page.evaluate("GroveSimulation.setStep(4).onboardingStep") == 3
    page.get_by_role("button", name="Enter your Grove", exact=True).click()
    page.wait_for_function("GroveSimulation.getState().onboardingComplete")
    expect(page.get_by_role("heading", name="Take Grove with you")).to_have_count(0)
    page.evaluate("GroveSimulation.completeTour&&GroveSimulation.completeTour()")
    page.keyboard.press("Escape")
    page.locator(".gp-accounts-trigger").click()
    for name in ("Muse", "ChatGPT", "Claude", "Grok"):
        expect(
            page.get_by_role("button", name=f"Set up Grove in {name}", exact=True)
        ).to_be_visible()
    expect(page.locator(".gp-settings")).to_contain_text("Custom MCP")
    page.screenshot(path=str(shots / f"{label}-connections.png"))
    assert not errors, errors


CHECKS = (
    ("account icon and click", check_account_icon),
    ("health", check_health),
    ("sources", check_sources),
    ("refresh", check_refresh),
    ("three onboarding steps", check_three_steps),
)


def main(shots, only):
    from playwright.sync_api import expect, sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    expect.set_options(timeout=5000)
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    failed = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, height in ((1440, 900), (390, 844)):
                for theme in ("light", "dark"):
                    label = f"{width}-{theme}"
                    for name, check in CHECKS:
                        if only and only not in name:
                            continue
                        reset(server)
                        context = browser.new_context(
                            viewport={"width": width, "height": height}
                        )
                        context.route(
                            "**/*",
                            lambda route: (
                                route.continue_()
                                if urlparse(route.request.url).hostname == "127.0.0.1"
                                else route.fallback()
                            ),
                        )
                        page = context.new_page()
                        try:
                            check(page, base, server, shots, label, expect, theme)
                            print(f"PASS {label} {name}", flush=True)
                        except Exception as error:  # noqa: BLE001 a red names its check, then the run goes on
                            failed += 1
                            first = (
                                " ".join(str(error).split())[:400]
                                or type(error).__name__
                            )
                            print(f"FAIL {label} {name}: {first}", flush=True)
                        context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f"{failed} failed", flush=True)
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    sys.exit(1 if main(args.shots, args.only) else 0)
