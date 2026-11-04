"""Material controls against the actual local API and isolated synthetic Worlds."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn

from grove.api.app import WEB_DIST_ENV, create_app
from grove.api.config import ApiSettings
from grove.views.session import SESSION_COOKIE
from tests.ingest.test_documents import pdf_bytes
from tests.ingest.test_material_files import office, template_presentation
from tests.writer.test_materials import material_world  # noqa: F401

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

pytestmark = pytest.mark.browser
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def material_pages():
    root = Path(os.environ.get("GROVE_MATERIALS_WEB_ROOT", REPO))
    subprocess.run([sys.executable, str(root / "web/build.py")], check=True, capture_output=True)
    return root / "web/dist"


@pytest.fixture(scope="module")
def material_browser():
    assert sync_playwright, "Run with the existing Playwright environment; do not install browsers"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def material_app(driver, material_world, material_pages, monkeypatch):  # noqa: F811
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(WEB_DIST_ENV, str(material_pages))
    app = create_app(ApiSettings(origin), driver=driver)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        yield origin, material_world
    finally:
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()


@pytest.fixture
def material_page(material_browser, material_app):
    origin, (_, cookie, _) = material_app
    context = material_browser.new_context(
        base_url=origin, viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
    )
    context.add_cookies(
        [{"name": SESSION_COOKIE, "value": cookie, "url": origin, "httpOnly": True}]
    )
    context.set_default_timeout(8000)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("/")
        page.get_by_role("button", name="Get Started").click()
        page.get_by_role("button", name="Skip for now", exact=True).click()
        page.get_by_role("button", name="Skip for now", exact=True).click()
        page.get_by_role("heading", name="Add what matters").wait_for()
        yield page
    finally:
        context.close()
        assert errors == []


def action(page, name):
    return page.locator(f'[data-material-action="{name}"]')


def note(page, title="Cedar launch", body="Mira plans the Cedar launch."):
    page.get_by_role("button", name="Add note +", exact=True).click()
    page.get_by_label("Title", exact=True).fill(title)
    page.get_by_label("Note", exact=True).fill(body)
    page.get_by_role("button", name="Save note", exact=True).click()
    page.locator("[data-material-status]").wait_for()
    assert "Request status: queued" in page.locator("[data-material-status]").inner_text()


def materials(page):
    response = page.context.request.get("/v1/materials")
    assert response.status == 200
    return response.json()["materials"]


def shot(page, name):
    folder = os.environ.get("GROVE_E2E_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / (name + ".png")), full_page=True)


def test_live_material_controls_are_enabled(material_page):
    page = material_page
    expect(page.get_by_role("button", name="Add files +", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Add note +", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Add note +", exact=True).click()
    assert page.get_by_label("Title", exact=True).is_visible()


def test_create_reload_edit_conflict_and_confirmed_all_revision_delete(material_page, material_app):
    page = material_page
    note(page)
    saved = materials(page)[0]
    path = "/v1/materials/" + saved["id"]
    page.reload()
    page.get_by_role("button", name="Cedar launch", exact=True).click()
    assert "Mira plans the Cedar launch." in page.locator(".gp-material-parts").inner_text()
    action(page, "edit").click()
    page.get_by_label("Note", exact=True).fill("Mira moved the Cedar launch.")
    assert "Earlier statements remain" in page.locator(".gp-materials").inner_text()
    page.get_by_role("button", name="Save revision", exact=True).click()
    page.get_by_text("Revision 2", exact=False).wait_for()
    action(page, "edit").click()
    page.get_by_label("Note", exact=True).fill("Unsent local draft")
    response = page.context.request.put(
        "/v1/materials/notes/" + saved["id"],
        data={"title": "Cedar launch", "body": "Another tab revision", "expected_revision": 2},
        headers={
            "Origin": material_app[0],
            "X-Grove-Action": "materials",
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert response.status == 201
    page.get_by_role("button", name="Save revision", exact=True).click()
    page.get_by_role("button", name="Discard draft and reload latest").wait_for()
    assert page.get_by_label("Note", exact=True).input_value() == "Unsent local draft"
    page.get_by_role("button", name="Discard draft and reload latest").click()
    page.get_by_text("Revision 3", exact=False).wait_for()
    assert "Another tab revision" in page.locator(".gp-material-parts").inner_text()
    calls = []
    page.on("request", lambda request: calls.append((request.method, request.url)))
    action(page, "preview").click()
    page.get_by_text("Delete all 3 revisions", exact=False).wait_for()
    assert not any("delete-confirmations" in url for _, url in calls)
    assert "Backups" in page.locator(".gp-material-delete").inner_text()
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.locator("[data-material-status]").wait_for()
    assert page.context.request.get(path).status == 200
    action(page, "preview").click()
    page.get_by_role("button", name="Delete all revisions", exact=True).click()
    page.get_by_text("Material deleted.", exact=False).wait_for()
    assert page.context.request.get(path).status == 404
    assert materials(page) == []
    assert sum("delete-confirmations" in url for _, url in calls) == 1
    assert sum(method == "POST" and url.endswith("/delete") for method, url in calls) == 1
    assert not page.evaluate(
        "JSON.stringify(localStorage).includes('Unsent local draft') || "
        "JSON.stringify(localStorage).includes('approval_token')"
    )


@pytest.mark.parametrize(
    "viewport", [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}]
)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_setup_and_settings_share_materials_with_readable_layout(material_page, viewport, theme):
    page = material_page
    page.set_viewport_size(viewport)
    page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
    note(page)
    shot(page, f"material-detail-{viewport['width']}-{theme}")
    page.get_by_role("button", name="Next step", exact=True).click()
    page.get_by_role("button", name="Enter your Grove", exact=True).click()
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.get_by_role("button", name="Cedar launch", exact=True).wait_for()
    page.locator(".gp-materials").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".gp-materials").evaluate("el => el.scrollWidth <= el.clientWidth")
    shot(page, f"material-settings-{viewport['width']}-{theme}")
    page.get_by_role("button", name="Cedar launch", exact=True).click()
    page.locator("[data-material-status]").wait_for()
    action(page, "edit").click()
    shot(page, f"material-editor-{viewport['width']}-{theme}")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.locator("[data-material-status]").wait_for()
    action(page, "preview").click()
    page.get_by_text("Delete all 1 revisions", exact=False).wait_for()
    shot(page, f"material-delete-{viewport['width']}-{theme}")


@pytest.mark.parametrize("extension", ["txt", "md", "pdf", "docx", "pptx"])
def test_file_bytes_formats_and_encoded_filename(material_page, extension):
    page = material_page
    data = b"A cedar file plan."
    if extension == "pdf":
        data = pdf_bytes("A cedar file plan.")
    elif extension == "docx":
        data = office(
            [("word/document.xml", "<document><p><t>A cedar file plan.</t></p></document>")]
        )
    elif extension == "pptx":
        data = template_presentation()
    name = "Café %20." + extension
    before = page.context.request.get("/v1/views/graph").json()
    add_files = page.get_by_role("button", name="Add files +", exact=True)
    expect(add_files).to_be_enabled()
    add_files.click()
    page.locator("[data-material-file]").set_input_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": data}
    )
    with page.expect_response(
        lambda response: (
            response.url.endswith("/v1/materials/files") and response.request.method == "POST"
        )
    ) as upload:
        page.get_by_role("button", name="Upload file", exact=True).click()
    assert upload.value.status == 201
    assert upload.value.request.header_value("content-length") == str(len(data))
    page.get_by_text("File saved. Brain extraction needs source details.", exact=True).wait_for()
    expect(page.get_by_role("heading", name="Add what matters", exact=True)).to_be_visible()
    shot(page, "onboarding-file-saved-" + extension)
    saved = materials(page)[0]
    assert saved["title"] == name
    assert saved["request"] is None and saved["graph_state"] == "held_provenance"
    assert page.context.request.get("/v1/views/graph").json()["elements"] == before["elements"]
    page.reload()
    page.get_by_role("button", name=name, exact=True).click()
    page.locator(".gp-material-parts").wait_for()
    expected = "Quarterly plan" if extension == "pptx" else "cedar"
    assert expected in page.locator(".gp-material-parts").inner_text()


def test_file_errors_leave_retry_controls_available(material_page):
    page = material_page
    page.get_by_role("button", name="Add files +", exact=True).click()
    for name, content, expected in [
        ("bad.zip", b"zip", "Choose a nonempty"),
        ("bad.pdf", b"not a PDF", "unsupported, unreadable"),
    ]:
        page.locator("[data-material-file]").set_input_files(
            {"name": name, "mimeType": "application/octet-stream", "buffer": content}
        )
        page.get_by_role("button", name="Upload file", exact=True).click()
        page.get_by_role("alert").filter(has_text=expected).wait_for()
        assert page.get_by_role("button", name="Upload file", exact=True).is_enabled()
    assert materials(page) == []


def test_route_close_and_logout_discard_late_responses_and_drafts(material_page):
    page = material_page
    page.evaluate("""() => {
      const original = window.fetch;
      window.materialOriginalFetch = original;
      window.fetch = (url, init) => String(url).includes('/v1/materials') && init.method === 'POST'
        ? new Promise(resolve => window.resolveMaterial = resolve) : original(url, init);
    }""")
    page.get_by_role("button", name="Add note +", exact=True).click()
    page.get_by_label("Title", exact=True).fill("Private late title")
    page.get_by_label("Note", exact=True).fill("Private late body")
    page.get_by_role("button", name="Save note", exact=True).click()
    page.get_by_role("button", name="Next step", exact=True).click()
    page.evaluate("""() => resolveMaterial(new Response(JSON.stringify({
      account:GroveSimulation.getState().account,generation:0,material:{
        id:'11111111-1111-4111-8111-111111111111',title:'Private late title',revision:1}
    }), {status:201}))""")
    page.get_by_role("button", name="Previous step", exact=True).click()
    page.get_by_role("button", name="Add note +", exact=True).click()
    assert page.get_by_label("Title", exact=True).input_value() == ""
    page.get_by_label("Title", exact=True).fill("Private unsent title")
    page.get_by_label("Note", exact=True).fill("Private unsent body")
    page.evaluate("window.dispatchEvent(new Event('grove:session-changed'))")
    page.get_by_role("button", name="Add note +", exact=True).wait_for()
    assert "Private" not in page.locator(".gp-materials").inner_text()
    assert not page.evaluate("JSON.stringify(localStorage).includes('Private')")


def test_missing_ledger_displays_service_error_without_success(material_page, monkeypatch):
    page = material_page
    monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR")
    page.get_by_role("button", name="Add note +", exact=True).click()
    page.get_by_label("Title", exact=True).fill("Service refusal")
    page.get_by_label("Note", exact=True).fill("A retained local draft")
    page.get_by_role("button", name="Save note", exact=True).click()
    page.get_by_role("alert").filter(has_text="temporarily unavailable").wait_for()
    assert page.get_by_label("Note", exact=True).input_value() == "A retained local draft"
    assert page.get_by_role("button", name="Save note", exact=True).is_enabled()
    assert page.locator("[data-material-status]").count() == 0
