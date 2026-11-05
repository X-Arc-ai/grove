"""Focused UI regressions with controlled local HTTP responses, never a live account.

Run with the existing Playwright environment. --serve exposes the repaired live-mode
renderer and, if supplied, an in-memory synthetic reference renderer for visual review.
The reference's fixture and built output are never read or copied.
"""
import argparse
import hashlib
import gzip
import json
import mimetypes
import re
import subprocess
import sys
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WEB = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((WEB / "checks/fixture.synthetic.json").read_text())
RESULT = dict(state="taken_in", provider="chatgpt", connector_id="test-connector",
              chats=1, chats_new=1, sources_added=1, episodes_added=1,
              messages_read=2, messages_kept=2, failed_chats=0, screened={})
REFERENCE_FILES = {
    "index.html", "favicon.svg", "simulation.js", "native-app.js", "style.css",
    "product.js", "product.css", "journey.css", "refinements.css", "system.css",
    "agents.js", "agents.css", "app-screens.js", "provider-icons.js",
    "entry-visual.js", "motion.js", "integrations.js", "whatsapp-guide.js",
    "vendor/fonts.css", "vendor/d3.min.js", "vendor/force-graph.min.js", "vendor/marked.min.js",
}


class Preview(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, reference):
        super().__init__(("127.0.0.1", port), Handler)
        self.reference = reference
        self.reset()

    def reset(self):
        self.signed_in = False
        self.has_items = False
        self.account = "synthetic-review-account"
        self.metadata = {}
        self.connectors = []
        self.uploads = []
        self.upload_status = 200
        self.upload_result = dict(RESULT)
        self.upload_delay = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, status=200, kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = urlparse(self.path).path
        if path == "/__test/reset":
            self.server.reset()
            return self.send({})
        if path == "/__test/upload":
            values = json.loads(data)
            self.server.upload_status = values.get("status", 200)
            self.server.upload_delay = values.get("delay", 0)
            self.server.upload_result = dict(RESULT, **values.get("result", {}))
            return self.send({})
        if path == "/__test/metadata":
            self.server.metadata = json.loads(data)
            return self.send({})
        if path == "/__test/account":
            values = json.loads(data)
            self.server.account = values.get("account", self.server.account)
            self.server.has_items = values.get("has_items", self.server.has_items)
            return self.send({})
        if path == "/v1/auth/email/start":
            return self.send({})
        if path == "/v1/auth/email/verify":
            self.server.signed_in = True
            return self.session()
        if path == "/v1/auth/logout":
            self.server.signed_in = False
            return self.send({})
        if path == "/v1/views/positions":
            return self.send(dict(placed=0, kept=len(json.loads(data)["positions"]), unknown=0))
        if path == "/v1/sources/upload":
            provider = parse_qs(urlparse(self.path).query)["provider"][0]
            self.server.uploads.append(dict(provider=provider, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
            time.sleep(self.server.upload_delay)
            if self.server.upload_status != 200:
                return self.send({"error": {"message": "Try this export again."}}, self.server.upload_status)
            if self.server.upload_result["state"] == "taken_in" and not self.server.upload_result["failed_chats"]:
                if not any(row["provider"] == provider for row in self.server.connectors):
                    self.server.connectors.append(dict(provider=provider, connector_id=provider,
                        type="past_ai_conversations", read_count=1, last_outcome="took_in"))
            return self.send(dict(self.server.upload_result, provider=provider))
        return self.send({"error": {"message": "Not available yet"}}, 503)

    def session(self):
        if not self.server.signed_in:
            return self.send({"error": {"code": "unauthenticated"}}, 401)
        return self.send(dict(signed_in=True, self_item_id="p001", account=self.server.account,
                              has_items=self.server.has_items, method="email"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in self.server.metadata:
            reply = dict(self.server.metadata[path])
            time.sleep(reply.get("delay", 0))
            return self.send(reply.get("body", {}), reply.get("status", 200))
        if path == "/__test/uploads":
            return self.send(self.server.uploads)
        if path == "/v1/views/session":
            return self.session()
        if path == "/v1/views/graph":
            return self.send(dict(elements=FIXTURE["elements"], stats={}, time={"min": None, "max": None},
                                  self_item_id="p001", generated_at="2026-11-04T12:00:00Z"))
        if path.startswith("/v1/views/node/"):
            node = next((n["data"] for n in FIXTURE["elements"]["nodes"] if n["data"]["id"] == path.rsplit("/", 1)[-1]), None)
            return self.send(dict(node=node, required_props=[], optional_props=[], required_prop_names=[], status_field="status"))
        if path == "/v1/views/schema":
            return self.send(FIXTURE.get("schema", {}))
        if path == "/v1/views/health":
            return self.send(dict(nodes=len(FIXTURE["elements"]["nodes"]), edges=len(FIXTURE["elements"]["edges"]), overall={"state": "unmeasured"}))
        if path == "/v1/views/pipedream/accounts":
            return self.send(dict(account=self.server.account, configured=True, providers=[
                dict(provider=p, status="not_connected") for p in ("gmail", "google_calendar", "notion")]))
        if path == "/v1/sources/connectors":
            return self.send({"connectors": self.server.connectors})
        if path == "/v1/connectors":
            return self.send({"connections": []})
        if path == "/built":
            return self.send((WEB / "dist/index.html").read_bytes(), kind="text/html")
        if path.startswith("/assets/grove/"):
            name = path.removeprefix("/assets/grove/")
            manifest = json.loads((WEB / "dist/assets/manifest.json").read_text())
            if not re.fullmatch(r"[a-f0-9]{64}\.(js|woff2|woff|css)", name) or name not in manifest:
                return self.send({}, 404)
            return self.send((WEB / "dist/assets" / name).read_bytes(), kind=manifest[name])
        if path.startswith("/v1/"):
            return self.send({"error": {"message": "Not available yet"}}, 503)
        reference = path.startswith("/reference/")
        base = self.server.reference if reference else WEB
        if not base:
            return self.send({}, 404)
        name = path.removeprefix("/reference/") if reference else path.lstrip("/")
        if not name or name in ("signin", "live"):
            name = "index.html"
        if not reference and name == "index.html":
            return self.send((WEB / "dist/index.html").read_bytes(), kind="text/html")
        if name in ("fixture.js", "fixture.json"):
            if not reference:
                return self.send({}, 404)
            if name.endswith(".json"):
                return self.send(FIXTURE)
            return self.send("window.GROVE_FIXTURE=" + json.dumps(FIXTURE) + ";", kind="text/javascript")
        if reference and name not in REFERENCE_FILES and not (name.startswith("vendor/fonts/") and name.endswith((".woff", ".woff2"))):
            return self.send({}, 404)
        target = (base / name).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file() or "dist" in target.relative_to(base).parts:
            return self.send({}, 404)
        body = target.read_bytes()
        return self.send(body, kind=mimetypes.guess_type(name)[0] or "application/octet-stream")


def run_checks(base, shots):
    from playwright.sync_api import sync_playwright, expect
    shots.mkdir(parents=True, exist_ok=True)

    def measure(page, responses, label):
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(800)
        rows = []
        for response in responses:
            if "/__test/" in response.url:
                continue
            body = response.body()
            kind = response.headers.get("content-type", "")
            size = len(body) if kind.startswith("font/") else len(gzip.compress(body))
            rows.append(dict(path=urlparse(response.url).path, bytes=size, type=kind))
        total = sum(row["bytes"] for row in rows)
        (shots / (label + "-bytes.json")).write_text(json.dumps(dict(total=total, responses=rows), indent=2))
        assert total < 500000, (label, total)
        assert not any(row["type"].startswith("text/javascript") for row in rows), "Deferred scripts must not load for initial paint"
        print(f"PASS {label}: full uncached initial UI load {total} bytes", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width in (1440, 768, 375):
            context = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="no-preference")
            page = context.new_page()
            responses = []
            page.on("response", responses.append)
            page.set_default_timeout(10000)
            page.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
            page.request.post(base + "/__test/reset")
            page.goto(base + "/signin")
            expect(page.locator("#gp-email")).to_be_visible()
            measure(page, responses, f"signin-{width}")
            page.screenshot(path=str(shots / f"live-{width}-signin.png"))
            page.locator("#gp-email").fill("mira@example.com")
            page.locator("[data-email-form] button").click()
            page.locator("#gp-code").fill("246810")
            page.locator("[data-code-form] button").click()
            expect(page.locator("[data-action=start]")).to_be_visible()
            page.evaluate("GroveSimulation.setName('Mira')")
            page.screenshot(path=str(shots / f"live-{width}-welcome.png"))
            page.locator("[data-action=start]").click()
            expect(page.locator(".gp-source-choice")).to_have_count(5)
            assert page.locator(".gp-source-choice").evaluate_all("els=>els.map(e=>e.dataset.guide)") == ["muse", "chats", "chatgpt", "claude", "grok"]
            expect(page.get_by_text("Give Grove the context behind how you think")).to_be_visible()
            page.screenshot(path=str(shots / f"live-{width}-conversations.png"))
            for provider in ("chatgpt", "claude"):
                guide_url = base + "/assets/grove/" + hashlib.sha256((WEB / "app-screens.js").read_bytes()).hexdigest() + ".js"
                fail_asset = width == 1440 and provider == "chatgpt"
                if fail_asset:
                    page.route(guide_url, lambda route: route.fulfill(status=503, body=""))
                page.locator(f"[data-guide={provider}]").click()
                if fail_asset:
                    expect(page.locator("[data-retry-asset=guides]")).to_be_visible()
                    assert not page.evaluate("GroveSimulation.getState().pendingExports.chatgpt")
                    page.unroute(guide_url)
                    page.locator("[data-retry-asset=guides]").click()
                expect(page.locator("[data-action=film-toggle]")).to_have_attribute("aria-label", "Pause guide")
                assert page.evaluate("GroveDeferred.load('guides')===GroveDeferred.load('guides')"), "Concurrent loads share one promise"
                frame = page.locator(".gp-app-frame iframe")
                assert frame.get_attribute("sandbox") == ""
                assert frame.get_attribute("inert") is not None
                assert "<script" not in frame.get_attribute("srcdoc").lower()
                page.wait_for_timeout(800)
                first = frame.screenshot()
                page.wait_for_timeout(2300)
                assert first != frame.screenshot(), "The export film must advance"
                page.locator("[data-action=film-toggle]").click()
                page.wait_for_timeout(400)
                frozen = frame.screenshot()
                page.wait_for_timeout(2300)
                assert frozen == frame.screenshot(), "Pause must stop the export film"
                page.screenshot(path=str(shots / f"live-{width}-{provider}-export.png"))
                page.locator("[data-action=export-requested]").click()
                expect(page.locator("[data-action=film-toggle]")).to_have_count(0)
                expect(page.locator("[data-action=upload-export]")).to_be_disabled()
                frame = page.locator(".gp-app-frame iframe")
                page.wait_for_timeout(800)
                still = frame.screenshot()
                page.wait_for_timeout(800)
                assert still == frame.screenshot(), "Transfer arrows must remain static"
                payload = b'{"synthetic":"Lumen review bytes"}'
                page.locator("[data-pick-export]").set_input_files(dict(name="lumen.json", mimeType="application/json", buffer=payload))
                page.request.post(base + "/__test/upload", data={"status": 503, "delay": 0.3})
                page.locator("[data-action=upload-export]").click()
                expect(page.locator("[data-action=upload-export]")).to_be_disabled()
                expect(page.get_by_role("alert")).to_contain_text("Try this export again")
                assert page.evaluate("p=>!!GroveSimulation.getState().pendingExports[p]", provider)
                expect(page.locator("[data-action=upload-export]")).to_be_enabled()
                page.request.post(base + "/__test/upload", data={"result": {"failed_chats": 1}})
                page.locator("[data-action=upload-export]").click()
                expect(page.locator(".gp-upload-result")).to_contain_text("Some chats failed")
                assert page.evaluate("p=>!!GroveSimulation.getState().pendingExports[p]", provider)
                page.request.post(base + "/__test/upload", data={})
                page.locator("[data-action=upload-export]").click()
                # A clean answer closes the guide; its counts stay below the tiles.
                expect(page.locator(f"[data-import-provider={provider}] .gp-upload-result")).to_contain_text("Sources added: 1")
                expect(page.locator("[data-action=upload-export]")).to_have_count(0)
                assert not page.evaluate("p=>!!GroveSimulation.getState().pendingExports[p]", provider)
                assert not page.evaluate("p=>!!GroveSimulation.getState().accountImports[p]", provider), "No invented import contribution"
                calls = page.request.get(base + "/__test/uploads").json()
                assert calls[-1] == dict(provider="claude_ai" if provider == "claude" else provider, size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
                page.screenshot(path=str(shots / f"live-{width}-{provider}-upload.png"))
            for provider in ("chats", "grok"):
                page.locator(f"[data-guide={provider}]").click()
                page.locator("[data-action=export-requested]").click()
                expect(page.locator("[data-action=pick-export]")).to_be_disabled()
                expect(page.locator("[data-action=upload-export]")).to_be_disabled()
                page.locator("[data-action=guide-back]").first.click()
            page.locator("[data-action=next]").click()
            expect(page.locator("[data-pipedream-search]")).to_be_visible()
            expect(page.locator("[data-pipedream-google]")).to_be_visible()
            page.screenshot(path=str(shots / f"live-{width}-apps.png"))
            page.locator("[data-pipedream-google]").click()
            expect(page.locator("[data-pipedream-connect=gmail]")).to_be_enabled()
            expect(page.locator("[data-pipedream-connect=google_drive]")).to_be_disabled()
            page.locator("[data-pipedream-back]").click()
            page.locator("[data-pipedream-search]").fill("Slack")
            expect(page.locator("[data-pipedream-connect=slack]")).to_be_disabled()
            page.locator("[data-action=next]").click()
            expect(page.get_by_role("heading", name="Your files")).to_be_visible()
            expect(page.get_by_role("heading", name="Your notes")).to_be_visible()
            expect(page.locator("[data-action=choose-files]")).to_be_disabled()
            expect(page.locator("[data-action=write-note]")).to_be_disabled()
            page.screenshot(path=str(shots / f"live-{width}-files.png"))
            page.locator("[data-action=next]").click()
            page.evaluate("GroveSimulation.completeTour()")
            page.keyboard.press("Escape")
            page.wait_for_timeout(2500)
            page.keyboard.press("Escape")
            page.screenshot(path=str(shots / f"live-{width}-graph.png"))
            # The AI hosts left setup. Settings > Connections holds them.
            page.locator(".gp-accounts-trigger").click()
            page.screenshot(path=str(shots / f"live-{width}-hosts.png"))
            page.locator("[data-host-inline=muse]").click()
            expect(page.locator("#gp-mcp-url")).to_have_value(base + "/mcp")
            page.screenshot(path=str(shots / f"live-{width}-muse.png"))
            page.locator("[data-action=host-back]").click()
            page.keyboard.press("Escape")
            page.locator("#profile-button").click()
            expect(page.locator(".gp-source-pending")).to_have_count(2)
            page.screenshot(path=str(shots / f"live-{width}-sources.png"))
            page.locator("[data-add-group=chats]").click()
            page.locator("[data-guide=chatgpt]").click()
            expect(page.locator("[aria-label='Settings sections']")).to_be_visible()
            page.locator("[data-action=export-requested]").click()
            expect(page.locator("[aria-label='Settings sections']")).to_be_visible()
            page.screenshot(path=str(shots / f"live-{width}-settings-upload.png"))
            page.locator("[data-action=export-stage]").click()
            expect(page.locator("[data-action=film-toggle]")).to_be_visible()
            page.locator("[data-action=sources-back]").click()
            expect(page.locator(".gp-source-pending")).to_have_count(3)
            expect(page.locator(".gp-source-value").first).to_contain_text("1 episodes read")
            expect(page.locator("button.gp-source-value")).to_have_count(0)
            page.keyboard.press("Escape")
            page.locator("#health-link").click()
            expect(page.locator('[data-live-count="nodes"]')).to_have_text(str(len(FIXTURE["elements"]["nodes"])))
            expect(page.get_by_text("Growth history unavailable")).to_have_count(2)
            expect(page.locator("[data-care]")).to_have_count(0)
            page.screenshot(path=str(shots / f"live-{width}-health.png"))
            page.locator("[data-action=watch-care]").click()
            expect(page.locator(".hood-score")).to_be_visible()
            expect(page.locator(".hood-score-row:disabled")).to_have_count(3)
            assert page.evaluate("__hood.turns().length") == 0
            assert page.evaluate("GroveCare.summary().fresh") is None
            assert page.evaluate("GroveCare.summary().running") is False
            page.screenshot(path=str(shots / f"live-{width}-workers.png"))
            expect(page.locator("#health-state .healthy")).to_have_count(0)
            page.locator("#requests-button").click()
            expect(page.get_by_text("Questions from your Grove appear here.")).to_be_visible()
            page.keyboard.press("Escape")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), "No horizontal page overflow"
            context.close()
            graph_context = browser.new_context(viewport={"width": width, "height": 900})
            graph = graph_context.new_page()
            graph.request.post(base + "/__test/account", data={"has_items": True})
            graph_responses = []
            graph.on("response", graph_responses.append)
            graph.goto(base + "/")
            expect(graph.locator("#profile-button")).to_be_visible()
            measure(graph, graph_responses, f"graph-{width}")
            graph_context.close()
            print(f"PASS {width}: five choices, films, pause, static transfer, byte upload, failure/retry/counts, catalog, files, Muse, Settings", flush=True)
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--port", type=int, default=18761)
    parser.add_argument("--shots", type=Path)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(WEB / "build.py")], check=True)
    server = Preview(args.port, args.reference)
    if args.serve:
        print(f"Synthetic live renderer: http://127.0.0.1:{args.port}/; reference: /reference/", flush=True)
        server.serve_forever()
    else:
        if not args.shots:
            parser.error("--shots is required for evidence")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            run_checks(f"http://127.0.0.1:{args.port}", args.shots)
        finally:
            server.shutdown()
