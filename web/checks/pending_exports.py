"""Focused real-browser reminder regression with controlled HTTP responses only.

No database, account, provider or public-host action. Run after web/build.py with
the existing Playwright environment and --shots pointing to the lane evidence.
"""

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

WEB = Path(__file__).resolve().parents[1]
BASE = "http://grove.test"
PENDING = "/v1/sources/pending-exports"
PAYLOAD = b'{"synthetic":"reminder regression"}'


class Server:
    def __init__(self):
        self.states = {
            account: {"account": account, "revision": 0, "pending_exports": {}}
            for account in ("owner-a", "owner-b")
        }
        self.calls = []
        self.hold_next = None
        self.held = []
        self.upload_mode = "partial"
        self.uploads = []
        self.fail_get = False

    def reply(self, route, body, status=200):
        route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)
        )

    def deliver(self, route, body):
        key = (route.request.method, urlparse(route.request.url).path)
        if self.hold_next == key:
            self.hold_next = None
            self.held.append((route, json.loads(json.dumps(body))))
        else:
            self.reply(route, body)

    def release(self, *, fail=False):
        route, body = self.held.pop(0)
        self.reply(
            route,
            {"error": {"code": "unavailable"}} if fail else body,
            503 if fail else 200,
        )

    def bind(self, context, owner):
        identity = {"account": owner}

        def handler(route):
            request = route.request
            url = urlparse(request.url)
            if url.netloc != "grove.test":
                return route.abort()
            path = url.path
            account = identity["account"]
            state = self.states[account]
            self.calls.append((account, request.method, path))
            if path == "/v1/views/session" or path == "/v1/auth/email/verify":
                return self.reply(
                    route,
                    {
                        "signed_in": True,
                        "account": account,
                        "method": "email",
                        "has_items": False,
                        "self_item_id": None,
                    },
                )
            if path in ("/v1/auth/email/start", "/v1/auth/logout"):
                return self.reply(route, {})
            if path == PENDING:
                if self.fail_get:
                    return self.reply(route, {"error": {"code": "unavailable"}}, 503)
                return self.deliver(route, state)
            if path.startswith(PENDING + "/"):
                assert request.method == "POST" and request.post_data_json == {}
                provider = path.rsplit("/", 1)[1]
                assert provider in ("chatgpt", "claude_ai", "whatsapp", "muse", "grok")
                state["revision"] += 1
                state["pending_exports"][provider] = (
                    f"00000000-0000-7000-8000-{state['revision']:012x}"
                )
                return self.deliver(route, state)
            if path == "/v1/sources/upload":
                provider = parse_qs(url.query)["provider"][0]
                self.uploads.append((provider, request.post_data_buffer))
                result = {
                    "state": "taken_in",
                    "failed_chats": 1 if self.upload_mode == "partial" else 0,
                    "chats": 1,
                    "chats_new": 1,
                    "sources_added": 1,
                    "episodes_added": 1,
                    "messages_read": 2,
                    "messages_kept": 2,
                    "screened": {},
                }
                if self.upload_mode == "unavailable":
                    result["reminder"] = {"status": "unavailable"}
                elif self.upload_mode == "success":
                    state["pending_exports"].pop(provider, None)
                    state["revision"] += 1
                    result["reminder"] = {"status": "updated", "snapshot": state}
                return self.deliver(route, result)
            if path == "/v1/views/graph":
                return self.reply(
                    route,
                    {
                        "elements": {"nodes": [], "edges": []},
                        "stats": {},
                        "time": {"min": None, "max": None},
                        "self_item_id": None,
                    },
                )
            if path == "/v1/views/schema":
                return self.reply(route, {})
            if path == "/v1/views/health":
                return self.reply(
                    route, {"nodes": 0, "edges": 0, "overall": {"state": "unmeasured"}}
                )
            if path == "/v1/sources/connectors":
                return self.reply(route, {"connectors": []})
            if path == "/v1/connectors":
                return self.reply(route, {"connections": []})
            if path == "/v1/views/pipedream/accounts":
                return self.reply(
                    route, {"account": account, "configured": False, "providers": []}
                )
            if path.startswith("/v1/"):
                return self.reply(route, {"error": {"code": "unavailable"}}, 503)
            if path.startswith("/assets/grove/"):
                name = path.rsplit("/", 1)[1]
                manifest = json.loads((WEB / "dist/assets/manifest.json").read_text())
                if name in manifest:
                    return route.fulfill(
                        content_type=manifest[name],
                        body=(WEB / "dist/assets" / name).read_bytes(),
                    )
                return route.abort()
            if path in ("/", "/signin"):
                return route.fulfill(
                    content_type="text/html",
                    body=(WEB / "dist/index.html").read_bytes(),
                )
            return route.fulfill(status=404, body="")

        context.route("**/*", handler)
        return identity


def pending(page):
    return page.evaluate("GroveSimulation.getState().pendingExports")


def wait_pending(page, keys):
    page.wait_for_function(
        "keys => JSON.stringify(Object.keys(GroveSimulation.getState().pendingExports).sort()) === JSON.stringify(keys.sort())",
        arg=keys,
    )


def sign_in_after_switch(page, identity, account):
    identity["account"] = account
    page.evaluate("window.dispatchEvent(new CustomEvent('grove:unauthenticated'))")
    page.locator("#gp-email").fill("mira@example.test")
    page.locator("[data-email-form] button").click()
    page.locator("#gp-code").fill("246810")
    page.locator("[data-code-form] button").click()
    page.wait_for_function(
        "account => GroveSimulation.getState().account === account && !!GroveSimulation.getState().auth",
        arg=account,
    )


def run(shots):
    server = Server()
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        identity = server.bind(context, "owner-a")
        context.add_init_script(
            "localStorage.setItem('grove-minimal-codex-v3', JSON.stringify({version:3, account:'owner-a', pendingExports:{grok:{requestedAt:'old-local'}}}));"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(7000)
        page.goto(BASE + "/signin")
        page.locator("[data-action=start]").click()
        wait_pending(page, [])
        assert not any(
            method == "POST" and path.startswith(PENDING)
            for _, method, path in server.calls
        )
        expect(page.locator(".gp-source-choice")).to_have_count(5)
        page.locator("[data-guide=chatgpt]").click()
        expect(page.locator("[data-action=film-toggle]")).to_be_visible()

        server.hold_next = ("POST", PENDING + "/chatgpt")
        page.locator("[data-action=export-requested]").click()
        expect(page.locator("[data-action=export-requested]")).to_be_disabled()
        assert pending(page) == {}
        page.wait_for_timeout(50)
        server.release(fail=True)
        expect(
            page.locator("[role=alert]").filter(has_text="could not save")
        ).to_be_visible()
        assert pending(page) == {}
        page.screenshot(path=str(shots / "request-failed.png"))
        page.locator("[data-action=export-requested]").click()
        expect(page.locator("[data-action=upload-export]")).to_be_disabled()
        wait_pending(page, ["chatgpt"])
        assert (
            page.evaluate(
                "JSON.parse(localStorage.getItem('grove-minimal-codex-v3')).pendingExports"
            )
            == {}
        )
        page.locator("[data-action=guide-back]").click()
        page.locator("[data-action=skip]").click()
        page.reload()
        wait_pending(page, ["chatgpt"])

        second = browser.new_context(viewport={"width": 375, "height": 850})
        server.bind(second, "owner-a")
        other = second.new_page()
        other.goto(BASE + "/signin")
        other.locator("[data-action=start]").click()
        wait_pending(other, ["chatgpt"])
        other.screenshot(path=str(shots / "second-browser-pending.png"))
        other.locator("[data-guide=chatgpt]").click()
        expect(other.locator("[data-action=upload-export]")).to_be_disabled()
        other.locator("[data-pick-export]").set_input_files(
            {"name": "export.json", "mimeType": "application/json", "buffer": PAYLOAD}
        )
        other.locator("[data-action=upload-export]").click()
        expect(other.locator(".gp-upload-result")).to_contain_text("Some chats failed")
        wait_pending(other, ["chatgpt"])
        server.upload_mode = "unavailable"
        other.locator("[data-action=upload-export]").click()
        expect(
            other.locator("[role=alert]").filter(has_text="import succeeded")
        ).to_be_visible()
        wait_pending(other, ["chatgpt"])
        other.screenshot(path=str(shots / "completion-unavailable.png"))
        server.upload_mode = "success"
        other.locator("[data-pick-export]").set_input_files(
            {"name": "export.json", "mimeType": "application/json", "buffer": PAYLOAD}
        )
        other.locator("[data-action=upload-export]").click()
        wait_pending(other, [])
        expect(other.locator(".gp-upload-result")).to_contain_text("Sources added: 1")
        assert server.uploads == [("chatgpt", PAYLOAD)] * 3

        # An older read cannot remove the result of a newer provider request.
        page.evaluate("GroveSimulation.refreshPendingExports()")
        page.evaluate("GroveSimulation.requestExport('chatgpt')")
        server.hold_next = ("GET", PENDING)
        page.evaluate("void (window.oldRead = GroveSimulation.refreshPendingExports())")
        page.wait_for_timeout(50)
        page.evaluate("GroveSimulation.requestExport('claude')")
        server.release()
        page.evaluate("window.oldRead")
        wait_pending(page, ["chatgpt", "claude"])

        # The stale response belongs to A even if another account has the same auth method.
        server.hold_next = ("POST", PENDING + "/grok")
        page.evaluate("void (window.oldWrite = GroveSimulation.requestExport('grok'))")
        page.wait_for_timeout(50)
        sign_in_after_switch(page, identity, "owner-b")
        server.release()
        page.evaluate("window.oldWrite")
        wait_pending(page, [])
        assert page.evaluate("GroveSimulation.getState().account") == "owner-b"
        server.fail_get = True
        page.reload()
        expect(page.locator("#gp-session-note")).to_contain_text(
            "could not load your export reminders"
        )
        assert pending(page) == {}
        assert errors == [], errors
        second.close()
        context.close()
        browser.close()
    print(
        "PASS: no local reminder migration; awaited request/refusal; skip/reload and separate-browser persistence; account/epoch isolation; revision order; partial/metadata failure; exact upload bytes; successful completion; truthful unavailable state",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    run(parser.parse_args().shots)
