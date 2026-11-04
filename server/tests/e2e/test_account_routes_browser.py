"""Account UI against a local, synthetic HTTP contract; no provider or database acceptance."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from grove.api.app import _add_web_pages

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

pytestmark = pytest.mark.browser
REPO = Path(__file__).resolve().parents[3]
COOKIE = "grove_session"
STORAGE = "grove-minimal-codex-v3"


@pytest.fixture(scope="module")
def controlled():
    subprocess.run([sys.executable, "web/build.py"], cwd=REPO, check=True, capture_output=True)
    app = FastAPI()
    _add_web_pages(app, str(REPO / "web/dist"))
    state = {"accounts": {}, "sessions": {}, "sequence": 0, "logout_posts": 0, "fail_logout": False}

    def account(request):
        key = state["sessions"].get(request.cookies.get(COOKIE))
        return state["accounts"].get(key)

    def payload(row):
        return {
            "signed_in": True,
            "account": row["id"],
            "method": "email",
            "has_items": row["items"],
            "self_item_id": "self-" + row["id"],
        }

    def absent():
        return JSONResponse({"error": {"code": "unauthenticated"}}, status_code=401)

    @app.post("/v1/auth/email/start")
    async def start(request: Request):
        await request.json()
        return {"accepted": True}

    @app.post("/v1/auth/email/verify")
    async def verify(request: Request):
        body = await request.json()
        if body.get("code") != "135790":
            return absent()
        key = body["email"].strip().lower()
        row = state["accounts"].setdefault(
            key, {"id": "account-" + str(len(state["accounts"])), "items": False}
        )
        state["sessions"].pop(request.cookies.get(COOKIE), None)
        state["sequence"] += 1
        token = "synthetic-session-" + str(state["sequence"])
        state["sessions"][token] = key
        response = JSONResponse(payload(row))
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax")
        return response

    @app.post("/v1/auth/logout")
    async def logout(request: Request):
        state["logout_posts"] += 1
        if state["fail_logout"]:
            return JSONResponse({"error": {"code": "unavailable"}}, status_code=503)
        state["sessions"].pop(request.cookies.get(COOKIE), None)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/v1/views/session")
    def session(request: Request):
        row = account(request)
        return payload(row) if row else absent()

    @app.get("/v1/views/graph")
    def graph(request: Request):
        row = account(request)
        if not row:
            return absent()
        own = "self-" + row["id"]
        nodes = [
            {
                "data": {
                    "id": own,
                    "label": "Synthetic owner",
                    "type": "person",
                    "self": True,
                    "x": 0,
                    "y": 0,
                }
            }
        ]
        if row["items"]:
            nodes.append(
                {
                    "data": {
                        "id": "task-" + row["id"],
                        "label": "Private synthetic task " + row["id"],
                        "type": "task",
                        "x": 100,
                        "y": 100,
                    }
                }
            )
        return {
            "elements": {"nodes": nodes, "edges": []},
            "stats": {"total_nodes": len(nodes)},
            "self_item_id": own,
            "time": {"min": None, "max": None},
        }

    @app.get("/v1/views/schema")
    def schema(request: Request):
        return {} if account(request) else absent()

    @app.get("/v1/views/health")
    def health(request: Request):
        return {"nodes": 1, "edges": 0, "by_type": {}} if account(request) else absent()

    @app.get("/v1/sources/pending-exports")
    def reminders(request: Request):
        row = account(request)
        return {"account": row["id"], "pending_exports": {}, "revision": 0} if row else absent()

    @app.get("/v1/sources/connectors")
    def sources():
        return {"connectors": []}

    @app.get("/v1/connectors")
    def connections():
        return {"connections": []}

    @app.post("/v1/views/positions")
    def positions(request: Request):
        return {"placed": 0, "kept": 1, "unknown": 0} if account(request) else absent()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="module")
def chromium():
    assert sync_playwright, "Use uv run --with playwright==1.58.0 python -m pytest -m browser"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def context(chromium, controlled):
    context = chromium.new_context(base_url=controlled[0], viewport={"width": 1440, "height": 1000})
    context.set_default_timeout(8000)
    errors = []
    context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
    yield context
    context.close()
    assert errors == []


def login(page, address="maya@example.test"):
    page.locator("#gp-email").fill(address)
    page.get_by_role("button", name="Continue with email").click()
    page.locator("#gp-code").fill("135790")
    page.locator("[data-code-form] button").click()
    page.locator(".gp-auth-form").wait_for(state="detached")


def finish(page):
    page.evaluate(
        """() => { const s=GroveSimulation; s.completeOnboarding(); s.completeTour(); }"""
    )
    page.locator("#profile-button").wait_for(state="visible")


def shot(page, name):
    page.wait_for_function("""() => Array.from(document.querySelectorAll('[role=dialog]')).every(
      dialog => Number(getComputedStyle(dialog).opacity) >= 0.99 &&
      dialog.getAnimations({subtree:true}).every(animation =>
        !Number.isFinite(animation.effect.getComputedTiming().endTime) ||
        animation.playState === 'finished'))""")
    folder = os.environ.get("GROVE_E2E_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / (name + ".png")), full_page=True)


@pytest.mark.parametrize(
    "route,title", [("/login", "Log in"), ("/signup", "Create account"), ("/signin", "Log in")]
)
def test_direct_account_route_survives_reload(context, route, title):
    page = context.new_page()
    assert page.goto(route).status == 200
    page.get_by_role("heading", name=title, exact=True).wait_for()
    page.reload()
    page.get_by_role("heading", name=title, exact=True).wait_for()
    assert page.get_by_role("button", name="Apple · Not available yet").is_disabled()
    shot(page, title.lower().replace(" ", "-") + "-desktop")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, title.lower().replace(" ", "-") + "-mobile")


def test_root_enters_login_and_signup_without_an_intermediate_page(context):
    page = context.new_page()
    response = page.goto("/")
    assert response.status == 200
    assert page.url.endswith("/login")
    page.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert not page.locator(".gp-welcome").count()
    shot(page, "root-login-desktop")
    page.get_by_role("link", name="New to Grove? Create account", exact=True).click()
    page.get_by_role("heading", name="Create account", exact=True).wait_for()
    assert page.url.endswith("/signup")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "root-signup-mobile")


def test_authenticated_root_preserves_onboarding_and_graph(context, controlled):
    page = context.new_page()
    page.goto("/")
    login(page, "root-owner@example.test")
    page.goto("/")
    page.get_by_role("button", name="Get Started").wait_for()
    assert page.url.endswith("/")
    controlled[1]["accounts"]["root-owner@example.test"]["items"] = True
    finish(page)
    page.goto("/")
    page.locator("#profile-button").wait_for(state="visible")
    assert page.url.endswith("/")
    assert not page.locator(".gp-auth-form").count()


def test_signup_verifies_then_enters_onboarding_and_preserves_account(context, controlled):
    page = context.new_page()
    page.goto("/signup")
    page.locator("#gp-email").fill("new-owner@example.test")
    page.get_by_role("button", name="Continue with email").click()
    assert "If a code arrives" in page.locator(".gp-auth-intro").inner_text()
    page.locator("#gp-code").fill("000000")
    page.locator("[data-code-form] button").click()
    page.get_by_role("alert").filter(has_text="invalid or expired").wait_for()
    assert "new-owner@example.test" not in controlled[1]["accounts"]
    page.locator("#gp-code").fill("135790")
    page.locator("[data-code-form] button").click()
    page.get_by_role("button", name="Get Started").wait_for()
    row = controlled[1]["accounts"]["new-owner@example.test"]
    page.goto("/signup")
    page.get_by_role("button", name="Get Started").wait_for()
    assert controlled[1]["accounts"]["new-owner@example.test"] is row


def test_logout_get_is_confirmation_post_clears_graph_and_back_reload(context, controlled):
    page = context.new_page()
    page.goto("/login")
    login(page, "logout-owner@example.test")
    finish(page)
    page.evaluate(
        "GroveSimulation.saveGrowth({focus:'Private synthetic draft',"
        "reading:'',avoid:'',confirm:true})"
    )
    posts = controlled[1]["logout_posts"]
    page.goto("/logout")
    page.get_by_role("heading", name="Log out?", exact=True).wait_for()
    assert controlled[1]["logout_posts"] == posts
    assert page.request.get("/v1/views/session").status == 200
    shot(page, "logout-confirmation-desktop")
    assert page.locator("#canvas").evaluate("el => getComputedStyle(el).visibility") == "hidden"
    assert page.locator('[data-action="logout"]').evaluate("""el => {
      const box=el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
    }""")
    page.set_viewport_size({"width": 390, "height": 844})
    shot(page, "logout-confirmation-mobile")
    page.get_by_role("button", name="Log out", exact=True).click()
    page.wait_for_url("**/login")
    page.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert page.request.get("/v1/views/session").status == 401
    assert page.evaluate("GroveView.nodes().length") == 0
    assert "Private synthetic draft" not in page.evaluate("JSON.stringify(localStorage)")
    page.go_back()
    page.reload()
    assert page.evaluate("window.GroveView ? GroveView.nodes().length : 0") == 0
    assert "Private synthetic draft" not in page.locator("body").inner_text()
    page.goto("/logout")
    page.wait_for_url("**/login")
    page.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert "logout-owner@example.test" in controlled[1]["accounts"]


def test_second_tab_logout_clears_first_tab_without_a_graph_poll(context):
    first = context.new_page()
    first.goto("/login")
    login(first, "two-tabs@example.test")
    finish(first)
    second = context.new_page()
    second.goto("/logout")
    second.get_by_role("button", name="Log out", exact=True).click()
    first.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert first.evaluate("GroveSimulation.getState().auth") is None
    assert first.evaluate("GroveView.nodes().length") == 0


def test_expired_session_in_one_tab_clears_the_other(context, controlled):
    first = context.new_page()
    first.goto("/login")
    login(first, "expired-tabs@example.test")
    finish(first)
    second = context.new_page()
    second.goto("/")
    second.locator("#profile-button").wait_for(state="visible")
    controlled[1]["sessions"].clear()
    first.evaluate("GroveLive.getJSON(GroveLive.ENDPOINTS.graph)")
    second.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert second.evaluate("GroveView.nodes().length") == 0


def test_next_navigation_keeps_only_safe_consent_destination(context):
    from urllib.parse import quote

    page = context.new_page()
    next_path = "/authorize?client_id=synthetic&scope=grove.read"
    page.goto("/login?next=" + quote(next_path, safe=""))
    page.get_by_role("link", name="New to Grove? Create account").click()
    assert "next=" in page.url
    assert page.locator(".gp-auth-navigation a").first.get_attribute(
        "href"
    ) == "/login?next=" + quote(next_path, safe="")
    page.goto("/signup?next=" + quote("//outside.example", safe=""))
    assert page.locator(".gp-auth-navigation a").first.get_attribute("href") == "/login"


def test_account_switch_in_another_tab_clears_old_account_and_drafts(context, controlled):
    first = context.new_page()
    first.goto("/login")
    login(first, "old-owner@example.test")
    finish(first)
    old_id = first.evaluate("GroveSimulation.getState().account")
    controlled[1]["accounts"]["old-owner@example.test"]["items"] = True
    first.evaluate("window.reloadGrove()")
    first.wait_for_function(
        "id => GroveView.nodes().some(node => node.id === id)", arg="task-" + old_id
    )
    first.evaluate("GroveSimulation.setName('Private old owner')")
    second = context.new_page()
    second.goto("/logout")
    second.get_by_role("button", name="Log out", exact=True).click()
    second.get_by_role("heading", name="Log in", exact=True).wait_for()
    login(second, "other-owner@example.test")
    new_id = controlled[1]["accounts"]["other-owner@example.test"]["id"]
    assert new_id != old_id
    first.wait_for_function(
        "id => GroveSimulation.getState().account === id && !!GroveSimulation.getState().auth",
        arg=new_id,
    )
    assert first.evaluate("GroveSimulation.getState().name") == ""
    assert first.evaluate("GroveSimulation.getState().onboardingComplete") is False
    assert "Private old owner" not in first.evaluate("JSON.stringify(localStorage)")
    assert first.evaluate("id => GroveView.nodes().every(node => node.id !== id)", "task-" + old_id)


def test_failed_logout_does_not_claim_a_revoked_session(context, controlled):
    page = context.new_page()
    page.goto("/login")
    login(page, "failure-owner@example.test")
    page.goto("/logout")
    controlled[1]["fail_logout"] = True
    try:
        page.get_by_role("button", name="Log out", exact=True).click()
        page.get_by_role("alert").filter(has_text="could not sign you out").wait_for()
        assert page.request.get("/v1/views/session").status == 200
    finally:
        controlled[1]["fail_logout"] = False


@pytest.mark.parametrize("complete", [False, True])
def test_same_verified_account_restores_only_bounded_onboarding(context, complete):
    page = context.new_page()
    address = "progress-" + str(complete).lower() + "@example.test"
    page.goto("/signup")
    login(page, address)
    page.evaluate("GroveSimulation.setStep(2)")
    if complete:
        finish(page)
    page.evaluate("GroveSimulation.setName('Private progress owner')")
    account = page.evaluate("GroveSimulation.getState().account")
    page.goto("/logout")
    page.get_by_role("button", name="Log out", exact=True).click()
    page.get_by_role("heading", name="Log in", exact=True).wait_for()
    state = page.evaluate("GroveSimulation.getState()")
    assert state["account"] is None
    assert state["auth"] is None
    assert state["name"] == ""
    stored = page.evaluate("JSON.parse(localStorage.getItem('grove-onboarding-v1'))")
    assert stored == [{"account": account, "step": 2, "complete": complete, "tour": complete}]
    assert "Private progress owner" not in page.evaluate("JSON.stringify(localStorage)")
    page.reload()
    login(page, address)
    resumed = page.evaluate("GroveSimulation.getState()")
    assert resumed["account"] == account
    assert resumed["onboardingStep"] == 2
    assert resumed["onboardingComplete"] is complete
    assert resumed["name"] == ""
