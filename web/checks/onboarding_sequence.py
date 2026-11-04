"""Full onboarding regression with local synthetic API responses only."""
import argparse
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ui_fidelity import Handler, Preview


class OnboardingHandler(Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/sources/pending-exports":
            return self.send({"account": self.server.account, "revision": 0, "pending_exports": {}})
        if path == "/v1/views/pipedream/apps":
            return self.send({"account": self.server.account, "configured": True,
                "apps": [{"provider": "gmail", "name": "Gmail", "auth_type": "oauth", "connectable": True}],
                "total": 1, "next_cursor": None})
        if path == "/v1/views/pipedream/accounts":
            # The catalog works, but the status service fails before the first consent.
            return self.send({"error": {"message": "Synthetic status failure"}}, 503)
        if path == "/v1/materials":
            return self.send({"account": self.server.account, "generation": 0,
                "materials": [], "next_cursor": None})
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/views/pipedream/gmail/connect":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.connections += 1
            return self.send({"account": self.server.account, "provider": "gmail",
                "connect_url": "https://pipedream.com/_static/connect.html?token=synthetic-test-only"})
        if path == "/v1/sources/pending-exports/claude_ai":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return self.send({"account": self.server.account, "revision": 1,
                "pending_exports": {"claude_ai": "019f0000-0000-7000-8000-000000000001"}})
        if path == "/v1/views/positions":
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.server.positions.extend(payload["positions"])
            return self.send({"placed": 0, "kept": len(payload["positions"]), "unknown": 0})
        return super().do_POST()


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), OnboardingHandler)
    server.reference = None
    Preview.reset(server)
    server.signed_in = True
    server.connections = 0
    server.positions = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(shots):
    from playwright.sync_api import sync_playwright, expect

    shots.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (1440, 390):
                Preview.reset(server)
                server.signed_in = True
                server.connections = 0
                server.positions = []
                context = browser.new_context(viewport={"width": width, "height": 900})
                # No provider request may leave this controlled browser context.
                context.route("https://pipedream.com/**", lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<h1>Synthetic provider authorization</h1>"))
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                page.get_by_role("button", name="Get Started", exact=True).click()
                expect(page.get_by_role("heading", name="Start with your conversations")).to_be_visible()
                page.locator('[data-guide="claude"]').click()
                page.get_by_role("button", name="I’ve requested it", exact=True).click()
                page.locator('[data-pick-export="claude"]').set_input_files({
                    "name": "synthetic-claude.json", "mimeType": "application/json", "buffer": b"[]"})
                positions_before_preview = len(server.positions)
                page.get_by_role("button", name="Upload export", exact=True).click()
                expect(page.get_by_role("heading", name="Start with your conversations")).to_be_visible()
                assert page.url == base + "/"
                assert not page.evaluate("GroveSimulation.getState().onboardingComplete")
                assert not page.evaluate("GroveSimulation.getState().tourComplete")
                assert server.uploads == []
                page.screenshot(path=str(shots / f"{width}-01-conversations.png"))
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(page.get_by_role("heading", name="Bring your world closer")).to_be_visible()
                page.get_by_role("button", name="Gmail connection", exact=True).click()
                expect(page.get_by_role("button", name="Connect Gmail", exact=True)).to_be_enabled()
                with context.expect_page() as popup:
                    page.get_by_role("button", name="Connect Gmail", exact=True).click()
                expect(popup.value.get_by_role("heading", name="Synthetic provider authorization")).to_be_visible()
                assert server.connections == 1
                popup.value.close()
                page.screenshot(path=str(shots / f"{width}-02-apps.png"))
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(page.get_by_role("heading", name="Add what matters")).to_be_visible()
                expect(page.locator('[data-materials="onboarding"]')).to_be_visible()
                page.screenshot(path=str(shots / f"{width}-03-materials.png"))
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(page.get_by_role("heading", name="Take Grove with you")).to_be_visible()
                expect(page.get_by_role("button", name="Set up Grove in Muse", exact=True)).to_be_visible()
                page.screenshot(path=str(shots / f"{width}-04-ai.png"))
                # The original previous/next controls retain all four steps.
                page.get_by_role("button", name="Previous step", exact=True).click()
                expect(page.get_by_role("heading", name="Add what matters")).to_be_visible()
                page.get_by_role("button", name="Next step", exact=True).click()
                page.get_by_role("button", name="Enter your Grove", exact=True).click()
                expect(page.get_by_role("dialog", name="Grove tour", exact=True)).to_be_visible(timeout=15000)
                for index, title in enumerate(("Your world, connected", "Follow the connections", "Account & settings",
                        "Your Grove workers", "Worker status", "Connect your AI"), 1):
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                    page.screenshot(path=str(shots / f"{width}-tour-{index}.png"))
                    page.locator('[data-tour-next]').click()
                assert page.evaluate("GroveSimulation.getState().tourComplete")
                assert not errors, errors
                # The example cannot send synthetic node coordinates to the real service.
                assert len(server.positions) == positions_before_preview, "Preview must not write graph coordinates"
                assert server.uploads == []
                print(f"PASS {width}px: four steps, Gmail consent control, live material and AI controls, six tour steps, no Claude upload", flush=True)
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    main(parser.parse_args().shots)
