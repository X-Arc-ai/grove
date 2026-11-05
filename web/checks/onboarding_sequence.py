"""Full onboarding regression with local synthetic API responses only."""

import argparse
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ui_fidelity import FIXTURE, Handler, Preview


def progress_body(provider, **changes):
    """One provider's import_progress.v1, as the server answers it."""
    body = dict(contract="import_progress.v1", provider=provider, state="idle", sources_total=0,
                requests_total=0, queued_count=0, processing_count=0, retry_wait_count=0,
                extracted_count=0, held_nothing_count=0, held_count=0, failed_count=0,
                graph=dict(items_created=0, items_changed=0, links_created=0, links_changed=0))
    body.update(changes)
    return body


def graph_body(server):
    """The self item first, then as many stored items as the import has written so far."""
    nodes = sorted(FIXTURE["elements"]["nodes"], key=lambda row: row["data"]["id"] != "p001")[: server.graph_limit]
    ids = {row["data"]["id"] for row in nodes}
    edges = [row for row in FIXTURE["elements"]["edges"] if row["data"]["source"] in ids and row["data"]["target"] in ids]
    return dict(elements=dict(nodes=nodes, edges=edges), stats={}, time={"min": None, "max": None},
                self_item_id="p001", generated_at="2026-11-04T12:00:00Z")


def reset_import(server):
    server.graph_limit = 1
    server.progress = {}
    server.import_run = None
    server.auto_import = True


class OnboardingHandler(Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/views/graph":
            return self.send(graph_body(self.server))
        if path == "/v1/sources/import-progress":
            provider = parse_qs(urlparse(self.path).query)["provider"][0]
            run = self.server.import_run
            if run and run["provider"] == provider:
                # Each read finds one more part finished, and the graph that part wrote.
                if run["done"] < run["total"]:
                    run["done"] += 1
                    self.server.graph_limit += run["items"]
                left = run["total"] - run["done"]
                return self.send(progress_body(provider, state="processing" if left else "complete", sources_total=run["total"],
                                               requests_total=run["total"], extracted_count=run["done"], processing_count=left))
            return self.send(self.server.progress.get(provider, progress_body(provider)))
        if path == "/v1/sources/pending-exports":
            return self.send(
                {"account": self.server.account, "revision": 0, "pending_exports": {}}
            )
        if path == "/v1/views/pipedream/apps":
            return self.send(
                {
                    "account": self.server.account,
                    "configured": True,
                    "apps": [
                        {
                            "provider": "gmail",
                            "name": "Gmail",
                            "auth_type": "oauth",
                            "connectable": True,
                        }
                    ],
                    "total": 1,
                    "next_cursor": None,
                }
            )
        if path == "/v1/views/pipedream/accounts":
            # The catalog works, but the status service fails before the first consent.
            return self.send({"error": {"message": "Synthetic status failure"}}, 503)
        if path == "/v1/agent-files":
            return self.send(
                {"account": self.server.account, "files": self.server.agent_files}
            )
        return super().do_GET()

    def do_DELETE(self):
        name = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        self.server.agent_files = [
            row for row in self.server.agent_files if row["name"] != name
        ]
        return self.send({"account": self.server.account, "removed": name})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/sources/upload" and self.server.auto_import:
            provider = parse_qs(urlparse(self.path).query)["provider"][0]
            self.server.import_run = dict(provider=provider, total=2, done=0, items=20)
        if path == "/v1/agent-files":
            size = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(size)
            name = parse_qs(urlparse(self.path).query)["name"][0]
            assert self.headers["Content-Type"] == "application/octet-stream"
            row = {"name": name, "kind": "file", "bytes": len(data)}
            self.server.agent_files.append(row)
            return self.send({"account": self.server.account, "file": row})
        if path == "/v1/agent-files/notes":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            row = {"name": body["title"] + ".md", "kind": "note", "bytes": len(body["body"])}
            self.server.agent_files.append(row)
            return self.send({"account": self.server.account, "file": row})
        if path == "/v1/views/pipedream/gmail/connect":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.connections += 1
            return self.send(
                {
                    "account": self.server.account,
                    "provider": "gmail",
                    "connect_url": "https://pipedream.com/_static/connect.html?token=synthetic-test-only",
                }
            )
        if path == "/v1/sources/pending-exports/claude_ai":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return self.send(
                {
                    "account": self.server.account,
                    "revision": 1,
                    "pending_exports": {
                        "claude_ai": "019f0000-0000-7000-8000-000000000001"
                    },
                }
            )
        if path == "/v1/views/positions":
            payload = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            self.server.positions.extend(payload["positions"])
            return self.send(
                {"placed": 0, "kept": len(payload["positions"]), "unknown": 0}
            )
        return super().do_POST()


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), OnboardingHandler)
    server.reference = None
    Preview.reset(server)
    server.signed_in = True
    server.connections = 0
    server.positions = []
    server.agent_files = []
    reset_import(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(shots):
    from playwright.sync_api import expect, sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, height in ((1440, 900), (390, 900), (375, 667)):
                Preview.reset(server)
                server.signed_in = True
                server.connections = 0
                server.positions = []
                server.agent_files = []
                reset_import(server)
                context = browser.new_context(
                    viewport={"width": width, "height": height}
                )
                # No provider request may leave this controlled browser context.
                context.route(
                    "https://pipedream.com/**",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<h1>Synthetic provider authorization</h1>",
                    ),
                )
                page = context.new_page()
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                page.goto(base)
                page.get_by_role("button", name="Get Started", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Start with your conversations")
                ).to_be_visible()
                page.locator('[data-guide="claude"]').click()
                page.get_by_role("button", name="I’ve requested it", exact=True).click()
                page.locator('[data-pick-export="claude"]').set_input_files(
                    {
                        "name": "synthetic-claude.json",
                        "mimeType": "application/json",
                        "buffer": b"[]",
                    }
                )
                page.get_by_role("button", name="Upload export", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Start with your conversations")
                ).to_be_visible()
                assert page.url == base + "/"
                assert not page.evaluate(
                    "GroveSimulation.getState().onboardingComplete"
                )
                assert not page.evaluate("GroveSimulation.getState().tourComplete")
                assert [row["provider"] for row in server.uploads] == ["claude_ai"], server.uploads
                page.screenshot(
                    animations="disabled",
                    path=str(shots / f"{width}-01-conversations.png"),
                )
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Bring your world closer")
                ).to_be_visible()
                with context.expect_page() as popup:
                    page.locator('[data-pipedream-select="gmail"]').click()
                expect(
                    popup.value.get_by_role(
                        "heading", name="Synthetic provider authorization"
                    )
                ).to_be_visible()
                assert server.connections == 1
                popup.value.close()
                page.screenshot(
                    animations="disabled", path=str(shots / f"{width}-02-apps.png")
                )
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Add what matters")
                ).to_be_visible()
                # The original defect: both buttons were disabled in live mode.
                with page.expect_file_chooser() as chooser:
                    page.get_by_role("button", name="Add files +", exact=True).click()
                chooser.value.set_files(
                    [
                        {"name": "plan.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1"},
                        {"name": "ideas.txt", "mimeType": "text/plain", "buffer": b"Ideas"},
                    ]
                )
                expect(page.locator('[data-agent-files="file"]')).to_contain_text("plan.pdf")
                expect(page.locator('[data-agent-files="file"]')).to_contain_text("ideas.txt")
                expect(page.locator('[data-agent-files="file"]')).not_to_contain_text("Adding")
                assert sorted(row["name"] for row in server.agent_files) == ["ideas.txt", "plan.pdf"], server.agent_files
                page.get_by_role("button", name="Remove ideas.txt", exact=True).click()
                expect(page.locator('[data-agent-files="file"]')).not_to_contain_text("ideas.txt")
                page.get_by_role("button", name="Add note +", exact=True).click()
                page.get_by_label("Title", exact=True).fill("Synthetic note")
                body = page.get_by_label("Note", exact=True)
                body.fill("Keep this draft and its focus during graph growth.")
                # New real items reach the graph while the person types.
                before_tick = page.evaluate("GroveSimulation.getState().currentNodeCount")
                server.import_run["total"] += 2
                page.wait_for_function(
                    "before => GroveSimulation.getState().currentNodeCount > before",
                    arg=before_tick,
                    timeout=45000,
                )
                expect(body).to_be_focused()
                expect(body).to_have_value("Keep this draft and its focus during graph growth.")
                page.get_by_role("button", name="Add note", exact=True).click()
                expect(page.locator('[data-agent-files="note"]')).to_contain_text("Synthetic note.md")
                page.screenshot(
                    animations="disabled", path=str(shots / f"{width}-03-materials.png")
                )
                page.get_by_role("button", name="Next step", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Take Grove with you")
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Set up Grove in Muse", exact=True)
                ).to_be_visible()
                page.screenshot(
                    animations="disabled", path=str(shots / f"{width}-04-ai.png")
                )
                # The original previous/next controls retain all four steps.
                page.get_by_role("button", name="Previous step", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Add what matters")
                ).to_be_visible()
                page.get_by_role("button", name="Next step", exact=True).click()
                page.get_by_role("button", name="Enter your Grove", exact=True).click()
                expect(
                    page.get_by_role("dialog", name="Grove tour", exact=True)
                ).to_be_visible(timeout=15000)
                for index, title in enumerate(
                    (
                        "Your world, connected",
                        "Follow the connections",
                        "Account & settings",
                        "Your Grove workers",
                        "Worker status",
                        "Connect your AI",
                    ),
                    1,
                ):
                    expect(
                        page.get_by_role("heading", name=title, exact=True)
                    ).to_be_visible()
                    page.screenshot(
                        animations="disabled",
                        path=str(shots / f"{width}-tour-{index}.png"),
                    )
                    if index == 5:
                        card = page.get_by_role(
                            "dialog", name="Grove tour", exact=True
                        ).bounding_box()
                        panel = page.locator(".hood-score").bounding_box()
                        assert card and panel
                        assert (
                            card["x"] + card["width"] <= panel["x"]
                            or card["y"] + card["height"] <= panel["y"]
                            or panel["x"] + panel["width"] <= card["x"]
                            or panel["y"] + panel["height"] <= card["y"]
                        ), "Tour must not cover the worker panel"
                    page.locator("[data-tour-next]").click()
                assert page.evaluate("GroveSimulation.getState().tourComplete")
                assert not errors, errors
                # Saved coordinates belong to items the server holds, and to no invented node.
                stored = {row["data"]["id"] for row in FIXTURE["elements"]["nodes"]}
                assert {row["id"] for row in server.positions} <= stored, "Only real items get coordinates"
                assert len(server.uploads) == 1
                print(
                    f"PASS {width}px: four steps, real Claude upload, paced real graph, one-click Gmail consent, working file and note buttons, AI controls, six tour steps",
                    flush=True,
                )
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    args = parser.parse_args()
    main(args.shots)
