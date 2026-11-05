"""Real import progress in a browser, on the page source, with synthetic HTTP responses.

No build, database, provider, account, model, or public host. Pass --web for a source copy and
--shots for the evidence folder. Every node the page shows here comes from the graph route below.
"""
import argparse
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

BASE = "http://import-parity.test"
OWNER = "synthetic-guide-owner"
PAYLOAD = b'{"synthetic":"import progress regression"}'


def guide_asset(web):
    # Keep this response byte-identical to build.py's app-screens.js expression.
    screens = json.loads((web / "app-screens.json").read_text(encoding="utf-8"))
    return "/* Static illustrative screens from an earlier walkthrough. */\nwindow.GroveAppScreenCSS=" + json.dumps((web / "app-screens.css").read_text(encoding="utf-8")) + ";\nwindow.GroveAppScreens=" + json.dumps(screens) + ";\n"


def progress(provider, **changes):
    value = dict(contract="import_progress.v1", provider=provider, state="idle", sources_total=0,
                 requests_total=0, queued_count=0, processing_count=0, retry_wait_count=0,
                 extracted_count=0, held_nothing_count=0, held_count=0, failed_count=0,
                 graph=dict(items_created=0, items_changed=0, links_created=0, links_changed=0))
    value.update(changes)
    return value


def item(name):
    return dict(data=dict(id=name, type="person", label="Invented " + name, summary="Stored test response"))


def bind_source(context, web, calls, session):
    def reply(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def reminders():
        return dict(account=OWNER, revision=session.get("revision", 0), pending_exports=session.setdefault("pending", {}))

    def handle(route):
        request = route.request
        url = urlparse(request.url)
        if url.netloc != "import-parity.test":
            return route.abort()
        calls.append((request.method, url.path))
        if url.path == "/v1/views/session":
            return reply(route, dict(signed_in=True, account=OWNER, method="email", has_items=False))
        if url.path == "/v1/sources/import-progress":
            provider = parse_qs(url.query)["provider"][0]
            return reply(route, session["progress"].get(provider, progress(provider)))
        if url.path == "/v1/sources/pending-exports":
            return reply(route, reminders())
        if url.path.startswith("/v1/sources/pending-exports/"):
            provider = url.path.rsplit("/", 1)[1]
            session["revision"] = session.get("revision", 0) + 1
            session.setdefault("pending", {})[provider] = "00000000-0000-7000-8000-000000000001"
            return reply(route, reminders())
        if url.path == "/v1/sources/upload":
            provider = parse_qs(url.query)["provider"][0]
            session["uploads"].append((provider, request.post_data_buffer))
            if session.get("upload_status") == 503:
                return reply(route, dict(error=dict(message="Retry this synthetic export.")), 503)
            failed = session.get("failed_chats", 0)
            state = session.get("upload_state", "taken_in")
            kept = 2 if state == "taken_in" else 0
            result = dict(state=state, provider=provider, chats=1, chats_new=1 if kept else 0,
                          sources_added=1 if kept else 0, episodes_added=1 if kept else 0, messages_read=2,
                          messages_kept=kept, failed_chats=failed, screened={},
                          processing=dict(contract="import_progress.v1", provider=provider,
                                          requests_added=1 if kept else 0, held_sources=0,
                                          status_url="/v1/sources/import-progress?provider=" + provider))
            if not failed:
                session.setdefault("pending", {}).pop(provider, None)
                session["revision"] = session.get("revision", 0) + 1
                result["reminder"] = dict(status="updated", snapshot=reminders())
            if kept:
                session["progress"][provider] = progress(provider, state="queued", sources_total=2, requests_total=2, queued_count=2)
                if not any(row["provider"] == provider for row in session["connectors"]):
                    session["connectors"].append(dict(provider=provider, connector_id=provider, type="past_ai_conversations",
                                                      read_count=1, last_outcome="took_in"))
            return reply(route, result)
        if url.path == "/v1/views/graph":
            session["graph_reads"] = session.get("graph_reads", 0) + 1
            return reply(route, dict(elements=dict(nodes=session["nodes"], edges=[]), stats={}, time=dict(min=None, max=None), self_item_id=None))
        if url.path.startswith("/v1/views/node/"):
            node = next(row["data"] for row in session["nodes"] if row["data"]["id"] == url.path.rsplit("/", 1)[1])
            return reply(route, dict(node=dict(node, title=node["label"], content="Real detail of " + node["id"]), required_props=[], optional_props=[], required_prop_names=[], status_field="status"))
        if url.path == "/v1/views/schema":
            return reply(route, {})
        if url.path == "/v1/views/health":
            return reply(route, dict(nodes=0, edges=0, overall=dict(state="unmeasured")))
        if url.path == "/v1/views/positions":
            return reply(route, dict(placed=0, kept=0, unknown=0))
        if url.path == "/v1/sources/connectors":
            return reply(route, dict(connectors=session["connectors"]))
        if url.path == "/v1/connectors":
            return reply(route, dict(connections=[]))
        if url.path.startswith("/v1/"):
            return reply(route, dict(error=dict(code="unavailable")), 503)
        if url.path == "/app-screens.js":
            return route.fulfill(body=guide_asset(web), content_type="text/javascript")
        name = "index.html" if url.path in ("/", "/login") else url.path.lstrip("/")
        file = (web / name).resolve()
        if not file.is_relative_to(web) or not file.is_file() or "dist" in file.relative_to(web).parts:
            return reply(route, {}, 404)
        if file.name in ("fixture.js", "fixture.json", "demo.synthetic.json"):
            return reply(route, {}, 404)
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
    context.route("**/*", handle)


def tile_ring(tile):
    return tile.evaluate("el => el.style.getPropertyValue('--source-progress')")


def wait_ring(page, provider, value):
    page.wait_for_function(
        "([id, value]) => document.querySelector(`[data-source-choice=\"${id}\"]`).style.getPropertyValue('--source-progress') === value",
        arg=[provider, value], timeout=20000)


def exercise_provider(page, session, shots, provider, code, width):
    tile = page.locator(f'[data-source-choice="{provider}"]')
    page.locator(f'[data-guide="{provider}"]').click()
    expect(page.locator(".gp-app-frame iframe")).to_be_visible()
    expect(page.get_by_text("invented conversations")).to_have_count(0)
    page.locator('[data-action="export-requested"]').click()
    expect(page.get_by_text("will not be uploaded")).to_have_count(0)
    page.locator("[data-pick-export]").set_input_files(dict(name="synthetic.json", mimeType="application/json", buffer=PAYLOAD))

    # A refused upload: no check, and the file stays for the retry.
    session["upload_status"] = 503
    page.locator('[data-action="upload-export"]').click()
    expect(page.get_by_role("alert")).to_contain_text("Retry this synthetic export")
    expect(page.locator('[data-action="upload-export"]')).to_be_enabled()
    session["upload_status"] = 200

    # A partial upload: the guide stays, with its line and its file.
    session["failed_chats"] = 1
    page.locator('[data-action="upload-export"]').click()
    expect(page.locator(".gp-upload-result")).to_contain_text("Some chats failed: 1")
    expect(page.locator('[data-action="upload-export"]')).to_be_enabled()
    page.screenshot(path=str(shots / f"{width}-{provider}-partial-guide.png"), full_page=True)
    page.locator('[data-action="guide-back"]').first.click()
    expect(tile).to_contain_text("Partial import")
    expect(tile).to_have_class(re.compile("is-partial"))
    section = page.locator(f'[data-import-provider="{provider}"]')
    expect(section).to_contain_text("Some chats failed: 1")
    assert "rgb(22, 163, 74)" != tile.locator(".gp-source-check").evaluate("el => getComputedStyle(el).backgroundColor"), "A partial upload is no clean success"

    # The retry takes the kept file through the same real path.
    section.locator("[data-import-review]").click()
    session["failed_chats"] = 0
    page.locator('[data-action="upload-export"]').click()
    expect(page.get_by_role("heading", name="Start with your conversations")).to_be_visible()
    assert session["uploads"][-1] == (code, PAYLOAD)
    check = tile.locator(".gp-source-check")
    expect(check).to_have_css("opacity", "1")
    assert check.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(22, 163, 74)"
    expect(tile).to_contain_text("Added")
    expect(section).to_contain_text("Grove accepted 2 messages from 1 new chat. 1 part added to the queue.")
    assert "%" not in section.inner_text()
    assert page.evaluate("GroveImportGrowth.active()")

    # The check stays while the ring follows the server's counts.
    session["progress"][code] = progress(code, state="processing", sources_total=2, requests_total=2, extracted_count=1, processing_count=1)
    section.locator("[data-import-refresh]").click()
    wait_ring(page, provider, "0.5")
    expect(tile).to_contain_text("Added")
    expect(check).to_have_css("opacity", "1")
    expect(page.locator(f'[data-import-mark="{provider}"]')).to_have_attribute("aria-label", ("Claude" if provider == "claude" else "ChatGPT") + " import, 50 percent")
    expect(page.locator(".gp-complete-check")).to_have_count(1 if provider == "claude" else 0)

    # Real nodes arrive, a revealed node opens real detail, and the end is a real end.
    before = page.evaluate("GroveView.nodes().length")
    names = [f"{provider}-{index}" for index in range(3)]
    session["nodes"].extend(item(name) for name in names)
    page.wait_for_function("n => GroveView.nodes().length > n", arg=before, timeout=20000)
    shown = page.evaluate("GroveView.nodes().map(n => n.id)")
    revealed = next(name for name in names if name in shown)
    with page.expect_request(lambda request: urlparse(request.url).path == "/v1/views/node/" + revealed):
        page.evaluate("id => GroveView.openNode(id)", revealed)
    expect(page.locator("#detail-body")).to_contain_text("Invented " + revealed)
    page.evaluate("GroveView.close()")
    session["progress"][code] = progress(code, state="complete", sources_total=2, requests_total=2, extracted_count=2,
                                         graph=dict(items_created=3, items_changed=0, links_created=0, links_changed=0))
    section.locator("[data-import-refresh]").click()
    expect(page.locator(f'[data-import-mark="{provider}"] .gp-complete-check')).to_be_visible()
    wait_ring(page, provider, "1")
    page.wait_for_function("() => !GroveImportGrowth.active()", timeout=40000)
    assert page.evaluate("GroveView.nodes().length") == before + 3
    expect(tile).to_contain_text("Added")
    page.screenshot(path=str(shots / f"{width}-{provider}-complete.png"), full_page=True)


def exercise_repeat(page, session, shots, width):
    """A repeat upload adds no growth and no read of the graph beyond the normal one."""
    before = page.evaluate("GroveView.nodes().length")
    page.locator('[data-guide="claude"]').click()
    page.locator('[data-action="export-requested"]').click()
    page.locator("[data-pick-export]").set_input_files(dict(name="synthetic.json", mimeType="application/json", buffer=PAYLOAD))
    session["upload_state"] = "nothing_new"
    page.locator('[data-action="upload-export"]').click()
    section = page.locator('[data-import-provider="claude"]')
    expect(section).to_contain_text("No new messages")
    assert not page.evaluate("GroveImportGrowth.active()")
    assert page.evaluate("GroveView.nodes().length") == before
    session["upload_state"] = "taken_in"
    page.screenshot(path=str(shots / f"{width}-repeat.png"), full_page=True)


def exercise_settings(page, session, shots, width):
    """A failed or held end stays visible in the Sources row, and a reload keeps the graph whole."""
    session["progress"]["chatgpt"] = progress("chatgpt", state="partial", sources_total=3, requests_total=3, extracted_count=1, failed_count=1, held_count=1)
    page.evaluate("GroveSimulation.completeOnboarding()")
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.locator('[data-action="refresh-live-data"]').first.click()
    note = page.locator('.gp-source-row [data-import-note="chatgpt"]')
    expect(note).to_contain_text("Not all of this import finished: 1 part failed, 1 part held.")
    expect(page.locator('.gp-source-row [data-import-note="claude"]')).to_be_hidden()
    page.screenshot(path=str(shots / f"{width}-sources-row.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    count = page.evaluate("GroveView.nodes().length")
    page.reload()
    page.wait_for_function("n => GroveView.nodes().length === n", arg=count, timeout=15000)
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    expect(page.locator('.gp-source-row [data-import-note="chatgpt"]')).to_contain_text("1 part failed")


def run(web, shots):
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width, height, motion in ((1440, 1000, "no-preference"), (390, 844, "no-preference")):
            context = browser.new_context(viewport=dict(width=width, height=height), reduced_motion=motion)
            calls = []
            session = dict(progress={}, uploads=[], connectors=[], nodes=[])
            bind_source(context, web, calls, session)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            try:
                page.goto(BASE)
                page.locator('[data-action="start"]').click()
                expect(page.get_by_role("heading", name="Start with your conversations")).to_be_visible()
                for provider, code in (("chatgpt", "chatgpt"), ("claude", "claude_ai")):
                    exercise_provider(page, session, shots, provider, code, width)
                exercise_repeat(page, session, shots, width)
                exercise_settings(page, session, shots, width)
                assert not errors, errors
                assert not [call for call in calls if call[1].startswith("/demo") or call[1].endswith("demo.synthetic.json")]
            except BaseException:
                try:
                    page.screenshot(path=str(shots / f"failure-{width}.png"), full_page=True)
                except Exception:
                    pass
                raise
            finally:
                context.close()
            print(f"PASS {width}: refused, partial, retry, accepted check, real ring, paced real nodes, repeat upload, Sources row, reload", flush=True)

        # Reduced motion: the real nodes show as soon as the graph is read.
        context = browser.new_context(viewport=dict(width=1024, height=800), reduced_motion="reduce")
        calls = []
        session = dict(progress={"claude_ai": progress("claude_ai", state="processing", sources_total=2, requests_total=2, processing_count=2)},
                       uploads=[], connectors=[], nodes=[])
        bind_source(context, web, calls, session)
        page = context.new_page()
        page.goto(BASE)
        page.locator('[data-action="start"]').click()
        page.wait_for_function("() => GroveImportGrowth.active()", timeout=20000)
        session["nodes"].extend(item(f"quick-{index}") for index in range(30))
        page.wait_for_function("() => GroveView.nodes().length >= 30", timeout=20000)
        context.close()
        print("PASS reduced motion: a page load on moving work starts the pacer, and new nodes show at once", flush=True)
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--shots", type=Path, required=True)
    args = parser.parse_args()
    run(args.web.resolve(), args.shots)
