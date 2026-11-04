"""Account UI against a local, synthetic HTTP contract; no provider or database acceptance."""

import copy
import json
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


def worker_snapshot():
    """Closed, synthetic read-only contract; it does not attest to a live process."""
    rows = []
    for name, policy, capability, activity in [
        (
            "learner",
            ("limited", "demo_learner_only"),
            ("limited", "legacy_learner_only"),
            "recorded_running",
        ),
        (
            "curator",
            ("disabled", "demo_autonomous_disabled"),
            ("unavailable", "curator_authority_unavailable"),
            "idle",
        ),
        (
            "connector",
            ("disabled", "demo_autonomous_disabled"),
            ("limited", "legacy_connector_only"),
            "stale",
        ),
    ]:
        rows.append(
            {
                "agent": name,
                "policy": {"state": policy[0], "reason": policy[1]},
                "capability": {"state": capability[0], "reason": capability[1]},
                "activity": {
                    "state": activity,
                    "running_records": int(activity == "recorded_running"),
                    "stale_records": int(activity == "stale"),
                },
            }
        )
    return {
        "schema": "grove.worker-activity.v1",
        "observed_at": "2026-11-04T12:00:00Z",
        "observation_kind": "stored_run_records",
        "service_state": "unknown",
        "service_reason": "no_process_heartbeat",
        "agents": rows,
        "recent_runs": [
            {
                "agent": "learner",
                "outcome": "ok",
                "started_at": "2026-11-04T11:00:00Z",
                "finished_at": "2026-11-04T11:02:00Z",
            }
        ],
        "recent_limit": 20,
        "recent_has_more": False,
    }


@pytest.fixture(scope="module")
def controlled():
    subprocess.run([sys.executable, "web/build.py"], cwd=REPO, check=True, capture_output=True)
    app = FastAPI()
    _add_web_pages(app, str(REPO / "web/dist"))
    state = {
        "accounts": {},
        "sessions": {},
        "sequence": 0,
        "logout_posts": 0,
        "fail_logout": False,
        "worker_calls": [],
        "worker_status": 200,
        "worker_body": None,
    }

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

    @app.get("/v1/views/workers")
    def workers(request: Request):
        row = account(request)
        if not row:
            return absent()
        state["worker_calls"].append(row["id"])
        body = copy.deepcopy(state["worker_body"] or worker_snapshot())
        return JSONResponse(body, status_code=state["worker_status"])

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
    skip = page.get_by_role("button", name="Skip tour", exact=True)
    if skip.count() and skip.is_visible():
        skip.click()


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


@pytest.mark.parametrize("width,height", [(1440, 900), (768, 1024), (375, 667)])
def test_workers_toggle_reveals_panel_during_first_tour_step(context, width, height):
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/login")
    login(page, f"workers-tour-{width}@example.test")
    page.evaluate("GroveSimulation.completeOnboarding()")
    page.get_by_role("heading", name="Your world, connected", exact=True).wait_for()
    page.locator("#agents-toggle").check()
    page.locator(".hood-score").wait_for(state="visible")
    assert page.locator("#agents-toggle").is_checked()
    assert page.locator(".hood-score").evaluate("""el => {
      const b=el.getBoundingClientRect();
      return b.left >= 0 && b.right <= innerWidth && b.top >= 0 && b.bottom <= innerHeight;
    }""")
    shot(page, f"workers-tour-{width}")


def open_workers(page, address):
    page.goto("/login")
    login(page, address)
    finish(page)
    page.locator("#agents-toggle").check()
    page.locator(".hood-live-observed").wait_for()


def hold_worker_reads(page):
    page.evaluate("""() => {
      const prior=window.fetch;
      window.workerReads=[];
      window.fetch=function(url, options) {
        if (url==='/v1/views/workers') return new Promise(resolve => workerReads.push(resolve));
        return prior.apply(this, arguments);
      };
    }""")


def resolve_worker(page, index, body, status=200):
    page.evaluate(
        """({index, body, status}) => {
      workerReads[index](new Response(JSON.stringify(body),
        {status, headers:{'Content-Type':'application/json'}}));
    }""",
        {"index": index, "body": body, "status": status},
    )


def test_workers_show_separate_policy_capability_and_stored_activity(context, controlled):
    page = context.new_page()
    open_workers(page, "worker-states@example.test")
    assert (
        page.locator('[data-worker="learner"] .hood-live-activity').inner_text()
        == "Run recorded as active"
    )
    assert (
        page.locator('[data-worker="curator"] .hood-live-policy').inner_text() == "Off in this demo"
    )
    assert (
        page.locator('[data-worker="curator"] .hood-live-capability').inner_text()
        == "Not available yet"
    )
    assert (
        page.locator('[data-worker="connector"] .hood-live-activity').inner_text()
        == "Last run needs a status update"
    )
    assert page.locator(".hood-live-worker button:disabled").count() == 3
    page.get_by_text("Recent activity", exact=True).click()
    assert "Learner · Completed" in page.locator(".hood-live-recent").inner_text()
    assert "Process status is unavailable" in page.locator(".hood-live-note").inner_text()
    count = len(controlled[1]["worker_calls"])
    page.locator("[data-worker-refresh]").click()
    page.locator(".hood-live-observed").wait_for()
    assert len(controlled[1]["worker_calls"]) == count + 1
    page.locator("#health-link").click()
    page.locator(".gp-health-workers").get_by_text("Run recorded as active", exact=False).wait_for()
    assert "Off in this demo" in page.locator(".gp-health-workers").inner_text()
    shot(page, "workers-health-stored-status")


def test_workers_error_retry_and_malformed_body_do_not_echo_untrusted_text(context, controlled):
    page = context.new_page()
    open_workers(page, "worker-error@example.test")
    try:
        controlled[1]["worker_status"] = 503
        controlled[1]["worker_body"] = {"error": "PRIVATE-UNTRUSTED-WORKER-TEXT"}
        page.locator("[data-worker-refresh]").click()
        page.get_by_role("button", name="Retry", exact=True).wait_for()
        assert "PRIVATE-UNTRUSTED" not in page.locator(".hood-live").inner_text()
        controlled[1]["worker_status"] = 200
        body = worker_snapshot()
        body["agents"][0]["activity"]["state"] = "PRIVATE-UNTRUSTED-WORKER-TEXT"
        controlled[1]["worker_body"] = body
        page.get_by_role("button", name="Retry", exact=True).click()
        page.get_by_role("button", name="Retry", exact=True).wait_for()
        assert not page.locator(".hood-live-worker").count()
        assert "PRIVATE-UNTRUSTED" not in page.locator(".hood-live").inner_text()
        controlled[1]["worker_body"] = None
        page.get_by_role("button", name="Retry", exact=True).click()
        page.locator(".hood-live-observed").wait_for()
    finally:
        controlled[1]["worker_status"] = 200
        controlled[1]["worker_body"] = None


def test_workers_new_refresh_wins_and_hidden_panel_discards_inflight_response(context):
    page = context.new_page()
    open_workers(page, "worker-race@example.test")
    hold_worker_reads(page)
    page.locator("[data-worker-refresh]").click()
    page.get_by_text("Checking worker status…", exact=True).wait_for()
    page.locator("[data-worker-refresh]").click()
    page.wait_for_function("workerReads.length === 2")
    newest = worker_snapshot()
    newest["agents"][0]["activity"] = {"state": "idle", "running_records": 0, "stale_records": 0}
    resolve_worker(page, 1, newest)
    page.locator('[data-worker="learner"]').get_by_text(
        "No active run recorded", exact=True
    ).wait_for()
    resolve_worker(page, 0, worker_snapshot())
    assert (
        page.locator('[data-worker="learner"] .hood-live-activity').inner_text()
        == "No active run recorded"
    )
    page.locator("[data-worker-refresh]").click()
    page.wait_for_function("workerReads.length === 3")
    page.locator("#agents-toggle").uncheck()
    resolve_worker(page, 2, worker_snapshot())
    assert not page.locator(".hood-live-worker").count()
    assert not page.locator(".hood-live").is_visible()
    page.locator("#agents-toggle").check()
    page.wait_for_function("workerReads.length === 4")
    resolve_worker(page, 3, newest)
    page.locator('[data-worker="learner"]').get_by_text(
        "No active run recorded", exact=True
    ).wait_for()


def test_workers_tab_hide_discards_inflight_response(context):
    page = context.new_page()
    open_workers(page, "worker-hidden@example.test")
    hold_worker_reads(page)
    page.locator("[data-worker-refresh]").click()
    page.wait_for_function("workerReads.length === 1")
    page.evaluate("""() => {
      Object.defineProperty(document,'hidden',{configurable:true,value:true});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    resolve_worker(page, 0, worker_snapshot())
    assert not page.locator(".hood-live-worker").count()
    page.evaluate("""() => {
      Object.defineProperty(document,'hidden',{configurable:true,value:false});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.locator("[data-worker-refresh]").click()
    page.wait_for_function("workerReads.length === 2")
    resolve_worker(page, 1, worker_snapshot())
    page.locator(".hood-live-observed").wait_for()


def test_workers_logout_and_second_account_refuse_old_response(context):
    page = context.new_page()
    open_workers(page, "worker-old@example.test")
    hold_worker_reads(page)
    page.locator("[data-worker-refresh]").click()
    page.wait_for_function("workerReads.length === 1")
    other = context.new_page()
    other.goto("/logout")
    other.get_by_role("button", name="Log out", exact=True).click()
    page.get_by_role("heading", name="Log in", exact=True).wait_for()
    assert not page.locator(".hood-live-worker").count()
    assert not page.locator("#agents-toggle").is_checked()
    login(other, "worker-new@example.test")
    page.wait_for_function("GroveSimulation.getState().account !== null")
    finish(page)
    resolve_worker(page, 0, worker_snapshot())
    assert not page.locator(".hood-live-worker").count()
    assert not page.locator("#agents-toggle").is_checked()
    page.locator("#agents-toggle").check()
    page.wait_for_function("workerReads.length === 2")
    next_body = worker_snapshot()
    next_body["agents"][0]["activity"] = {"state": "idle", "running_records": 0, "stale_records": 0}
    next_body["recent_runs"] = []
    resolve_worker(page, 1, next_body)
    page.locator('[data-worker="learner"]').get_by_text(
        "No active run recorded", exact=True
    ).wait_for()
    assert "Completed" not in page.locator(".hood-live").inner_text()


def test_fidelity_account_uses_user_icon_and_opens_during_tour(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-account@example.test")
    page.evaluate("GroveSimulation.completeOnboarding()")
    page.get_by_role("heading", name="Your world, connected", exact=True).wait_for()
    assert page.locator("#profile-button svg").count() == 1
    assert page.locator("#profile-button").inner_text() == ""
    page.locator("#profile-button").click()
    page.get_by_role("heading", name="Settings", exact=True).wait_for()
    assert page.locator(".gp-settings-content").is_visible()


def test_fidelity_refresh_remains_accessible_on_phone(context):
    page = context.new_page()
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto("/login")
    login(page, "fidelity-refresh@example.test")
    finish(page)
    assert page.locator("#refresh-data").is_visible()
    page.locator("#refresh-data").click()
    page.locator("#graph-status").get_by_text("Graph updated", exact=False).wait_for()


def test_fidelity_delete_brain_has_truthful_confirmation(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-delete@example.test")
    finish(page)
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="account"]').click()
    page.get_by_role("button", name="Delete brain", exact=True).click()
    page.get_by_role("heading", name="Delete your brain?", exact=True).wait_for()
    assert page.get_by_role("button", name="Delete brain", exact=True).is_disabled()
    assert "not available" in page.locator(".gp-confirm-dialog").inner_text()
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert page.locator(".gp-settings-content").is_visible()
    assert page.evaluate("GroveView.nodes().length") > 0


@pytest.mark.parametrize("provider", ["muse", "chatgpt", "claude"])
def test_fidelity_host_guides_have_visual_frames_and_pause(context, provider):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-guide-" + provider + "@example.test")
    finish(page)
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="connections"]').click()
    page.locator('[data-host-inline="' + provider + '"]').click()
    frame = page.locator(".gp-app-frame iframe")
    frame.wait_for(state="visible")
    assert "sandbox" in frame.evaluate("el => el.outerHTML")
    source = frame.get_attribute("srcdoc")
    assert source.count('class="film-frame"') >= 3
    page.get_by_role("button", name="Pause guide", exact=True).click()
    assert "animation-play-state:paused" in frame.get_attribute("srcdoc")
    page.get_by_role("button", name="Play guide", exact=True).click()
    assert "animation-play-state:paused" not in frame.get_attribute("srcdoc")
    assert not page.locator('[data-action="host-connect"]').count()


def install_graph_snapshot(page, nodes, edges):
    page.evaluate(
        """({nodes,edges}) => {
      window.testGraph={elements:{nodes:nodes.map(data=>({data})),
        edges:edges.map(data=>({data}))},self_item_id:'self-test'};
      if (!window.originalGraphGet) {
        window.originalGraphGet=GroveLive.getJSON;
        GroveLive.getJSON=function(url){
          if(url==='/v1/views/graph')
            return Promise.resolve({ok:true,status:200,body:window.testGraph});
          return originalGraphGet.apply(this,arguments);
        };
      }
    }""",
        {"nodes": nodes, "edges": edges},
    )
    page.locator("#refresh-data").click()
    page.locator("#graph-status").get_by_text("Graph updated", exact=False).wait_for()


def test_fidelity_session_deltas_show_current_subgraph_and_private_removal_summary(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-deltas@example.test")
    finish(page)
    base = [{"id": "self-test", "label": "Owner", "type": "person"}]
    install_graph_snapshot(page, base, [])
    install_graph_snapshot(page, base, [])
    assert page.evaluate("GroveSimulation.getState().nodeDelta") == 0
    added = base + [
        {"id": "n1", "label": "Private old title", "type": "task"},
        {"id": "n2", "label": "Project", "type": "plan"},
    ]
    edges = [
        {"id": "e1", "source": "self-test", "target": "n1", "verb": "HAS"},
        {"id": "e2", "source": "n1", "target": "n2", "verb": "IN"},
    ]
    install_graph_snapshot(page, added, edges)
    page.locator('[data-kind="nodes"]').get_by_text("(+2)", exact=True).wait_for()
    page.locator('[data-kind="edges"]').get_by_text("(+2)", exact=True).wait_for()
    page.locator('[data-kind="nodes"]').click()
    assert "2 new nodes" in page.locator("#selection-pill").inner_text()
    assert "previous graph check" in page.locator("#stats").inner_text()
    install_graph_snapshot(page, [base[0], added[2]], [])
    page.locator('[data-kind="removed-nodes"]').click()
    assert "1 node removed" in page.locator("#selection-pill").inner_text()
    assert "Private old title" not in page.locator("#selection-pill").inner_text()
    assert page.evaluate("GroveSimulation.getState().removedEdges") == 2
    assert page.evaluate("GroveSimulation.getChanges().new_nodes.length") == 0
    assert "Private old title" not in page.evaluate("JSON.stringify(GroveSimulation.getChanges())")
    page.evaluate("GroveSimulation.signOut()")
    assert page.evaluate("GroveSimulation.getState().removedNodes") == 0


def test_fidelity_refresh_error_and_late_response_keep_current_graph(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-refresh-race@example.test")
    finish(page)
    page.evaluate("""() => {
      window.heldGraphs=[];const get=GroveLive.getJSON;
      GroveLive.getJSON=function(url){
        if(url==='/v1/views/graph')return new Promise(resolve=>heldGraphs.push(resolve));
        return get.apply(this,arguments);};
      window.reloadGrove();window.reloadGrove();
    }""")
    assert "Refresh in progress" in page.locator("#graph-status").inner_text()
    page.evaluate(
        """heldGraphs[1]({ok:true,status:200,body:{elements:{nodes:[{data:{id:'current',label:'Current',type:'task'}}],edges:[]}}})"""
    )
    page.locator("#graph-status").get_by_text("Graph updated", exact=False).wait_for()
    page.evaluate(
        """heldGraphs[0]({ok:true,status:200,body:{elements:{
            nodes:[{data:{id:'old',label:'Old private',type:'task'}}],edges:[]}}})"""
    )
    assert "old" not in page.evaluate("GroveView.nodes().map(n=>n.id)")
    page.evaluate("() => { window.reloadGrove(); }")
    page.evaluate("""heldGraphs[2]({ok:false,status:503,body:{error:{code:'unavailable'}}})""")
    page.locator("#graph-status").get_by_text("Refresh failed", exact=False).wait_for()
    assert "current" in page.evaluate("GroveView.nodes().map(n=>n.id)")


def test_fidelity_dark_guide_and_health_sources(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-dark@example.test")
    finish(page)
    if page.locator("html").get_attribute("data-theme") != "dark":
        page.locator("#theme-toggle").click()
    page.locator("#health-link").click()
    page.locator('[data-live-count="nodes"]').get_by_text("1", exact=True).wait_for()
    page.get_by_role("button", name="Your sources", exact=False).click()
    assert page.locator(".gp-settings-content").is_visible()
    page.locator('[data-settings-tab="connections"]').click()
    page.locator('[data-host-inline="muse"]').click()
    assert "background:#111113" in page.locator(".gp-app-frame iframe").get_attribute("srcdoc")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    shot(page, "fidelity-dark-guide")


@pytest.mark.parametrize("width,height", [(1440, 900), (768, 1024), (375, 667)])
def test_fidelity_organic_graph_and_controls_at_each_viewport(context, width, height):
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": height})
    page.goto("/login")
    login(page, "fidelity-viewport-" + str(width) + "@example.test")
    finish(page)
    nodes = [{"id": "self-test", "label": "Owner", "type": "person"}]
    edges = []
    for group in range(16):
        hub = "project-" + str(group)
        nodes.append({"id": hub, "label": "Project " + str(group), "type": "plan"})
        if group:
            edges.append(
                {
                    "id": "bridge-" + hub,
                    "source": "project-" + str(group - 1),
                    "target": hub,
                    "verb": "SUPPORTS",
                }
            )
        for index in range(50):
            key = hub + "-" + str(index)
            nodes.append(
                {
                    "id": key,
                    "label": "Task " + str(index),
                    "type": ["task", "observation", "person", "decision"][index % 4],
                }
            )
            edges.append({"id": "edge-" + key, "source": hub, "target": key, "verb": "INCLUDES"})
            if index:
                edges.append(
                    {
                        "id": "sequence-" + key,
                        "source": hub + "-" + str(index - 1),
                        "target": key,
                        "verb": "FOLLOWS",
                    }
                )
    # This is an already-placed graph. The display projection must not overwrite
    # any server coordinate. The insert-only first-visit path has its own check.
    for index, node in enumerate(nodes):
        node.update(x=1000 + index * 7, y=-500 + index * 3)
    writes = []
    page.on(
        "request",
        lambda request: (
            writes.append(request.url)
            if request.method == "POST" and "/positions" in request.url
            else None
        ),
    )
    install_graph_snapshot(page, nodes, edges)
    page.evaluate("GroveView.fit(0)")
    assert page.evaluate("GroveView.nodes().length") == len(nodes)
    assert page.evaluate("GroveView.nodes().every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y))")
    assert writes == []
    assert page.locator("#profile-button").is_visible()
    assert page.locator("#health-link").is_visible()
    assert page.locator("#refresh-data").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shot(page, "fidelity-graph-" + str(width))
    page.locator("#theme-toggle").click()
    shot(page, "fidelity-graph-theme-" + str(width))
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="connections"]').click()
    page.locator('[data-host-inline="claude"]').click()
    assert page.locator(".gp-settings-layout").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shot(page, "fidelity-settings-guide-" + str(width))


def test_fidelity_active_filter_preserves_real_graph_and_refresh(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-active@example.test")
    finish(page)
    nodes = [
        {"id": "self-test", "label": "Owner", "type": "person"},
        {"id": "plan", "label": "Plan", "type": "plan"},
        {"id": "done", "label": "Ended", "type": "plan", "status": "archived"},
        {"id": "task", "label": "Task", "type": "task"},
    ]
    install_graph_snapshot(page, nodes, [])
    page.locator("#active-only").check()
    assert page.evaluate("GroveView.graph().graphData().nodes.map(n=>n.id).sort()") == [
        "grove-self",
        "plan",
    ]
    page.locator("#refresh-data").click()
    page.locator("#graph-status").get_by_text("Graph updated", exact=False).wait_for()
    assert page.evaluate("GroveView.graph().graphData().nodes.length") == 2
    assert page.evaluate("GroveSimulation.getState().currentNodeCount") == 4
    page.locator("#active-only").uncheck()
    assert page.evaluate("GroveView.graph().graphData().nodes.length") == 4


@pytest.mark.parametrize("device", ["iphone", "android"])
def test_fidelity_whatsapp_device_films_pause_and_keep_settings(context, device):
    page = context.new_page()
    page.goto("/login")
    login(page, "fidelity-whatsapp-" + device + "@example.test")
    finish(page)
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.locator('[data-add-group="chats"]').click()
    page.locator('[data-guide="chats"]').click()
    frame = page.locator(".gp-app-frame iframe")
    frame.wait_for(state="visible")
    page.locator('[data-guide-device="' + device + '"]').click()
    assert (
        page.locator('[data-guide-device="' + device + '"]').get_attribute("aria-pressed") == "true"
    )
    assert frame.get_attribute("srcdoc").count('class="film-frame"') == 6
    assert page.locator(".gp-settings-layout").is_visible()
    assert page.get_by_role("button", name="Desktop", exact=True).count() == 0
    page.get_by_role("button", name="Pause guide", exact=True).click()
    assert "animation-play-state:paused" in frame.get_attribute("srcdoc")
    shot(page, "fidelity-whatsapp-" + device)
    page.get_by_role("button", name="Play guide", exact=True).click()
    assert "animation-play-state:paused" not in frame.get_attribute("srcdoc")


def test_fidelity_mobile_worker_header_actions_remain_reachable(context):
    page = context.new_page()
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto("/login")
    login(page, "worker-header@example.test")
    finish(page)
    page.locator("#agents-toggle").check()
    page.locator(".hood-live-recent summary").click()
    for identity in [
        "agents-toggle",
        "active-only",
        "health-link",
        "profile-button",
        "refresh-data",
        "theme-toggle",
    ]:
        assert page.locator("#" + identity).evaluate("""el => {
          const r=el.getBoundingClientRect();
          const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
          return el===hit || el.contains(hit);
        }"""), identity
    page.locator("#agents-toggle").uncheck(timeout=1500)
    page.locator("#agents-toggle").check()
    page.locator("#theme-toggle").click()
    page.locator("#refresh-data").click()
    page.locator("#profile-button").click()
    page.locator(".gp-settings-layout").wait_for(state="visible")


@pytest.mark.parametrize("provider", ["iphone", "android", "muse", "chatgpt", "claude"])
def test_fidelity_pause_has_one_complete_frame(context, provider):
    page = context.new_page()
    page.goto("/login")
    login(page, "pause-" + provider + "@example.test")
    finish(page)
    page.locator("#profile-button").click()
    if provider in ("iphone", "android"):
        page.locator('[data-settings-tab="sources"]').click()
        page.locator('[data-add-group="chats"]').click()
        page.locator('[data-guide="chats"]').click()
        page.locator('[data-guide-device="' + provider + '"]').click()
    else:
        page.locator('[data-settings-tab="connections"]').click()
        page.locator('[data-host-inline="' + provider + '"]').click()
    frame = page.locator(".gp-app-frame iframe")
    frame.wait_for(state="visible")
    # Exercise the wrap boundary and an interior point without a long real-time wait.
    for elapsed in [0.01, 3.8]:
        frame.evaluate(
            """(el, seconds) => {
              el.dataset.started=String(performance.now()-seconds*1000);
              el.dataset.elapsed='0';
            }""",
            elapsed,
        )
        page.get_by_role("button", name="Pause guide", exact=True).click()
        page.wait_for_timeout(150)
        inner = frame.element_handle().content_frame()
        opacity = inner.locator(".film-frame").evaluate_all(
            "els => els.map(el => Number(getComputedStyle(el).opacity))"
        )
        assert sum(value > 0.01 for value in opacity) == 1, opacity
        assert max(opacity) == 1
        first = opacity[:]
        page.wait_for_timeout(200)
        assert (
            inner.locator(".film-frame").evaluate_all(
                "els => els.map(el => Number(getComputedStyle(el).opacity))"
            )
            == first
        )
        page.get_by_role("button", name="Play guide", exact=True).click()
    shot(page, "repaired-guide-" + provider)


def test_fidelity_dark_owner_canvas_label_uses_readable_ink(context):
    page = context.new_page()
    page.add_init_script("""(() => {
      const original=CanvasRenderingContext2D.prototype.fillText;
      window.ownerInk=[];
      CanvasRenderingContext2D.prototype.fillText=function(text,...rest){
        if(text==='Synthetic owner') window.ownerInk.push(this.fillStyle);
        return original.call(this,text,...rest);
      };
    })();""")
    page.goto("/login")
    login(page, "owner-ink@example.test")
    finish(page)
    for theme in ("light", "dark", "light"):
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#theme-toggle").click()
        page.evaluate("ownerInk=[]")
        page.locator("#refresh-data").click()
        page.wait_for_function("ownerInk.length > 0")
        expected = page.evaluate("""() => {
          const context=document.createElement('canvas').getContext('2d');
          context.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--text');
          return context.fillStyle;
        }""")
        assert page.evaluate("ownerInk[ownerInk.length-1]") == expected


def test_fidelity_pipedream_catalog_connect_and_confirmed_disconnect(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "catalog@example.test")
    finish(page)
    owner = page.evaluate("GroveSimulation.getState().account")
    actions = []
    state = {"connected": False}

    def route(request):
        path = request.request.url.split("/v1/views/pipedream/", 1)[1]
        actions.append((request.request.method, path, request.request.post_data))
        if path.startswith("apps"):
            body = {
                "account": owner,
                "configured": True,
                "apps": [
                    {
                        "provider": "slack",
                        "name": "Slack",
                        "auth_type": "oauth",
                        "connectable": True,
                    },
                    {
                        "provider": "public_weather",
                        "name": "Public Weather",
                        "auth_type": "none",
                        "connectable": False,
                    },
                ],
                "next_cursor": None,
                "total": 2,
            }
        elif path.startswith("accounts"):
            body = {
                "account": owner,
                "configured": True,
                "providers": [
                    {
                        "provider": "slack",
                        "name": "Slack",
                        "status": "connected" if state["connected"] else "not_connected",
                    }
                ],
            }
        elif path == "slack/connect":
            body = {
                "account": owner,
                "provider": "slack",
                "connect_url": "https://pipedream.com/connect/test",
                "expires_at": "2026-11-04T20:00:00Z",
            }
        elif path == "slack/disconnect":
            assert json.loads(request.request.post_data) == {"confirm": True}
            state["connected"] = False
            body = {
                "account": owner,
                "provider": "slack",
                "status": "not_connected",
                "disconnected": 1,
            }
        else:
            raise AssertionError(path)
        request.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/v1/views/pipedream/**", route)
    page.evaluate(
        "window.open=()=>({opener:null,location:{href:''},closed:false,close(){this.closed=true}})"
    )
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.locator('[data-add-group="apps"]').click()
    page.locator('[data-pipedream-select="slack"]').click()
    page.locator('[data-pipedream-connect="slack"]').click()
    page.get_by_text("Complete authorization in the new tab", exact=False).wait_for()
    assert not page.locator("[data-pipedream-disconnect]").count()
    state["connected"] = True
    page.locator("[data-pipedream-refresh]").click()
    page.locator('[data-pipedream-disconnect="slack"]').click()
    assert not any(path == "slack/disconnect" for _, path, _ in actions)
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert not any(path == "slack/disconnect" for _, path, _ in actions)
    page.locator('[data-pipedream-disconnect="slack"]').click()
    page.get_by_role("button", name="Disconnect Slack", exact=True).click()
    page.locator('[data-pipedream-connect="slack"]').wait_for()
    assert sum(path == "slack/disconnect" for _, path, _ in actions) == 1
    assert any(path.startswith("apps") for _, path, _ in actions)
    assert any(path == "accounts?provider=slack" for _, path, _ in actions)


def test_fidelity_pipedream_demo_refusal_is_not_a_connection_error(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "catalog-demo@example.test")
    finish(page)
    page.route(
        "**/v1/views/pipedream/**",
        lambda route: route.fulfill(
            status=403,
            content_type="application/json",
            body=json.dumps(
                {
                    "error": {
                        "code": "scope_denied",
                        "message": "This account cannot connect external apps.",
                    }
                }
            ),
        ),
    )
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.locator('[data-add-group="apps"]').click()
    page.get_by_text(
        "Connections are disabled for this prepared demo account.", exact=False
    ).first.wait_for()
    assert not page.locator("[data-pipedream-connect]").count()


def test_fidelity_pipedream_search_and_session_discard_old_results(context):
    page = context.new_page()
    page.goto("/login")
    login(page, "catalog-race@example.test")
    finish(page)
    page.evaluate("""() => {
      window.catalogReads=[];const old=GroveLive.getJSON;
      GroveLive.getJSON=function(url){
        if(url.startsWith('/v1/views/pipedream/apps'))
          return new Promise(resolve=>catalogReads.push({url,resolve}));
        if(url.startsWith('/v1/views/pipedream/accounts'))return Promise.resolve({ok:true,
          body:{account:GroveSimulation.getState().account,configured:true,providers:[]}});
        return old.apply(this,arguments);
      };
      window.catalogAnswer=function(index,id){catalogReads[index].resolve({ok:true,body:{account:GroveSimulation.getState().account,configured:true,apps:[{provider:id,name:id,auth_type:'oauth',connectable:true}],next_cursor:null,total:1}});};
    }""")
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="sources"]').click()
    page.locator('[data-add-group="apps"]').click()
    page.wait_for_function("catalogReads.length===1")
    page.locator("[data-pipedream-search]").fill("notion")
    page.wait_for_function("catalogReads.length===2")
    page.evaluate("catalogAnswer(1,'notion')")
    page.locator('[data-pipedream-select="notion"]').wait_for()
    page.evaluate("catalogAnswer(0,'slack')")
    page.wait_for_timeout(50)
    assert not page.locator('[data-pipedream-select="slack"]').count()
    page.locator("[data-pipedream-search]").fill("linear")
    page.wait_for_function("catalogReads.length===3")
    page.evaluate(
        "window.dispatchEvent(new Event('grove:session-changed'));catalogAnswer(2,'linear')"
    )
    page.wait_for_timeout(50)
    assert not page.locator('[data-pipedream-select="linear"]').count()
