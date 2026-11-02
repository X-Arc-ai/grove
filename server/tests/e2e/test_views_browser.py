"""The views in a real browser: the sign-in card, the welcome, and a map that keeps every place.

Runs only when asked (`-m browser`) and only where Playwright is installed, never in CI:

    cd server && GROVE_E2E_SHOTS=<folder> uv run --with playwright==1.58.0 \
        pytest -m browser tests/e2e -q -p no:cacheprovider

It builds web/dist, seeds a t_ world from web/checks/fixture.synthetic.json with the demo seeder,
serves the real app (the views API and the built page) on a free local port, and drives Chromium
at 1440x900. Screenshots go to GROVE_E2E_SHOTS, else to a pytest temporary folder. The session
token goes into the browser's cookie jar only; it is never printed.
"""

import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from grove.api.app import WEB_DIST_ENV, create_app  # noqa: E402
from grove.views.session import SESSION_COOKIE  # noqa: E402
from grove.writer import demo  # noqa: E402

pytestmark = pytest.mark.browser

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
FIXTURE_PATH = WEB / "checks" / "fixture.synthetic.json"
SHOTS_ENV = "GROVE_E2E_SHOTS"
HOST = "127.0.0.1"
VIEWPORT = {"width": 1440, "height": 900}
SELF_VIEW_ID = "grove-self"
# A pinned node (a hub, the person's own node) sits on its home. A leaf's hold force clamps it to
# 5 px each way around its home, x and y apart (a square, as in the sim), and one velocity step
# past it: so each axis stays within 6 px, while the corner of the square is 7.4 px away.
SETTLED_PX = 6.0
# No new item may sit on top of another node, and each sits by the item it links to.
APART_PX = 4.0
NEAR_PX = 250.0
# A node fades and grows in over its first second on the map.
FADE_IN_MS = 1500
NEW_ITEMS = 12
WAIT_MS = 30_000
POLL_SECONDS = 0.25
POLLS = 120

# Each node's home: fx/fy for a pinned node, else the place the hold force keeps it near.
HOMES_JS = """() => {
  const out = {};
  for (const n of window.GroveView.nodes()) {
    const pinned = n.fx != null && n.fy != null;
    const home = pinned ? [n.fx, n.fy] : n.__home;
    out[n.id] = home ? [home[0], home[1]] : null;
  }
  return out;
}"""
SETTLED_JS = """(limit) => window.GroveView.nodes().every((n) => {
  const home = n.fx != null && n.fy != null ? [n.fx, n.fy] : n.__home;
  return !!home && Math.abs(n.x - home[0]) <= limit && Math.abs(n.y - home[1]) <= limit;
})"""
WORST_JS = """() => window.GroveView.nodes().map((n) => {
  const home = n.fx != null && n.fy != null ? [n.fx, n.fy] : n.__home;
  return [n.id, home ? Math.max(Math.abs(n.x - home[0]), Math.abs(n.y - home[1])) : null];
}).sort((a, b) => (b[1] == null ? Infinity : b[1]) - (a[1] == null ? Infinity : a[1]))
  .slice(0, 5)"""
NODE_COUNT_JS = """(count) => !!window.GroveView && window.GroveView.nodes().length === count"""
NODE_IDS_JS = """() => window.GroveView.nodes().map((n) => n.id)"""
# The view ids each of `ids` links to, from the links on the map.
LINKED_JS = """(ids) => {
  const end = (v) => (typeof v === 'object' ? v.id : v);
  const links = window.GroveView.graph().graphData().links;
  return Object.fromEntries(ids.map((id) => [id, links
    .filter((l) => end(l.source) === id || end(l.target) === id)
    .map((l) => (end(l.source) === id ? end(l.target) : end(l.source)))]));
}"""
# What the server holds, read from the page with the page's own cookie.
SERVER_PLACED_JS = """async () => {
  const response = await fetch('/v1/views/graph', {credentials: 'same-origin'});
  const body = await response.json();
  const placed = {};
  for (const w of (body.elements || {}).nodes || []) {
    const d = w.data;
    if (typeof d.x === 'number' && typeof d.y === 'number') placed[d.id] = [d.x, d.y];
  }
  return {status: response.status, placed: placed};
}"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _local_state() -> tuple[str, dict]:
    """The page's local view state with onboarding and the tour done, keyed as simulation.js."""
    source = (WEB / "simulation.js").read_text(encoding="utf-8")
    key = re.search(r"var STORAGE_KEY = '([^']+)'", source)
    version = re.search(r"var VERSION = (\d+);", source)
    schema = re.search(r"schema: (\d+),", source)
    assert key and version and schema, "simulation.js no longer names its storage key"
    return key.group(1), {
        "version": int(version.group(1)),
        "schema": int(schema.group(1)),
        "auth": {"provider": "google", "email": "maya.chen@example.com"},
        "name": "Maya Chen",
        "onboardingComplete": True,
        "onboardingStep": 4,
        "tourComplete": True,
    }


@pytest.fixture(scope="module")
def shots(tmp_path_factory) -> Path:
    folder = Path(os.environ.get(SHOTS_ENV) or tmp_path_factory.mktemp("shots"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(scope="module")
def built_page() -> Path:
    subprocess.run(
        [sys.executable, str(WEB / "build.py")], cwd=REPO, check=True, capture_output=True
    )
    page = WEB / "dist" / "index.html"
    assert page.is_file()
    return page.parent


@pytest.fixture(scope="module")
def base_url(driver, built_page):
    """The real app on a free local port, in a thread; stopped when the module ends."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(WEB_DIST_ENV, str(built_page))
        app = create_app(driver=driver)
    port = _free_port()
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="grove-e2e-server", daemon=True)
    thread.start()
    for _ in range(POLLS):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("the app stopped while it started")
        time.sleep(POLL_SECONDS)
    else:
        raise RuntimeError("the app did not start")
    yield f"http://{HOST}:{port}"
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        opened = playwright.chromium.launch()
        yield opened
        opened.close()


@pytest.fixture
def seeded(driver, make_world):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return demo.seed_world(driver, make_world(), fixture)


def _context(browser, base_url, token=None, state=None):
    context = browser.new_context(viewport=VIEWPORT, base_url=base_url)
    if token is not None:
        context.add_cookies([{"name": SESSION_COOKIE, "value": token, "url": base_url}])
    if state is not None:
        key, value = json.dumps(state[0]), json.dumps(json.dumps(state[1]))
        # Only when the key is absent, so a reload keeps what the page itself saved.
        context.add_init_script(
            f"(() => {{ try {{ if (!localStorage.getItem({key})) "
            f"localStorage.setItem({key}, {value}); }} catch (_) {{}} }})();"
        )
    return context


def _open(context, errors):
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    return page


def _shot(page, shots, name, settle_ms=0):
    if settle_ms:
        page.wait_for_timeout(settle_ms)
    page.screenshot(path=str(shots / f"{name}.png"))


def _view_ids(seeded) -> set[str]:
    return {
        SELF_VIEW_ID if item_id == seeded.self_item_id else item_id
        for item_id in seeded.items.values()
    }


def _real_id(view_id, seeded):
    return seeded.self_item_id if view_id == SELF_VIEW_ID else view_id


def _wait_for_nodes(page, ids):
    page.wait_for_function(NODE_COUNT_JS, arg=len(ids), timeout=WAIT_MS)
    assert set(page.evaluate(NODE_IDS_JS)) == ids


def _wait_settled(page):
    try:
        page.wait_for_function(SETTLED_JS, arg=SETTLED_PX, timeout=WAIT_MS)
    except sync_api.TimeoutError:
        pytest.fail(f"nodes drew away from their homes: {page.evaluate(WORST_JS)}")


def _server_placed(page, real_ids):
    """Poll the graph view until the server holds x and y for every one of real_ids."""
    held = {}
    for _ in range(POLLS):
        answer = page.evaluate(SERVER_PLACED_JS)
        assert answer["status"] == 200
        held = answer["placed"]
        if set(real_ids) <= set(held):
            return held
        time.sleep(POLL_SECONDS)
    pytest.fail(f"the server has no position for {len(set(real_ids) - set(held))} items")


def _homes(page):
    homes = page.evaluate(HOMES_JS)
    missing = [view_id for view_id, home in homes.items() if home is None]
    assert not missing, f"nodes with no home: {missing[:5]}"
    return {view_id: (home[0], home[1]) for view_id, home in homes.items()}


def _assert_homes_are_stored(homes, held, seeded):
    """A home is the stored position itself, to the last bit."""
    differ = [v for v, home in homes.items() if tuple(held[_real_id(v, seeded)]) != home]
    assert not differ, f"homes that are not the stored position: {differ[:5]}"


def _assert_same_homes(before, after, ids):
    moved = [(i, before[i], after.get(i)) for i in sorted(ids) if after.get(i) != before[i]]
    assert not moved, f"{len(moved)} placed items moved, for example {moved[:3]}"


def test_the_sign_in_card_offers_google_and_no_code_hint(browser, base_url, shots):
    errors: list[str] = []
    context = _context(browser, base_url)
    try:
        page = _open(context, errors)
        page.goto("/")
        google = page.get_by_role("button", name="Continue with Google")
        google.wait_for(timeout=WAIT_MS)
        apple = page.locator('[data-signin="apple"]')
        assert apple.inner_text().strip() == "Continue with Apple"
        assert page.locator('[data-action="use-code"], .gp-email-preview').count() == 0
        page.wait_for_timeout(600)
        _shot(page, shots, "signin")

        apple.click()
        page.locator('[data-signin="apple"][aria-disabled="true"]').wait_for(timeout=WAIT_MS)
        assert page.locator('[data-signin="apple"]').inner_text().strip() == "Not available yet"
        assert page.get_by_role("button", name="Continue with Google").is_visible()
        _shot(page, shots, "signin-apple")

        # The code step, as signin will answer it: no code is shown and none is offered.
        page.route(
            "**/v1/auth/email/start",
            lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
        )
        page.fill("#gp-email", "maya.chen@example.com")
        page.get_by_role("button", name="Continue with email").click()
        page.locator("#gp-code").wait_for(timeout=WAIT_MS)
        assert page.locator('[data-action="use-code"], .gp-email-preview').count() == 0
        text = page.locator("#grove-product").inner_text()
        assert "482" not in text and "Use code" not in text
        _shot(page, shots, "signin-code")
        assert errors == []
    finally:
        context.close()


def test_the_welcome_and_a_map_that_keeps_every_place(browser, base_url, shots, driver, seeded):
    errors: list[str] = []
    ids = _view_ids(seeded)
    real_ids = set(seeded.items.values())

    # b) Signed in, with clean local storage: the welcome.
    context = _context(browser, base_url, token=seeded.token)
    try:
        page = _open(context, errors)
        page.goto("/")
        page.get_by_role("button", name="Get Started").wait_for(timeout=WAIT_MS)
        page.wait_for_timeout(800)
        _shot(page, shots, "welcome")
    finally:
        context.close()

    # c) Signed in, onboarding and the tour done: the graph of every seeded item.
    context = _context(browser, base_url, token=seeded.token, state=_local_state())
    try:
        page = _open(context, errors)
        page.goto("/")
        _wait_for_nodes(page, ids)
        held = _server_placed(page, real_ids)
        homes = _homes(page)
        _assert_homes_are_stored(homes, held, seeded)
        _wait_settled(page)
        _shot(page, shots, "graph", FADE_IN_MS)

        page.reload()
        _wait_for_nodes(page, ids)
        _assert_same_homes(homes, _homes(page), ids)
        _wait_settled(page)

        # New items arrive: every placed item stays, each new one gets a free spot.
        added = demo.add_items(driver, seeded.world_id, NEW_ITEMS)
        page.evaluate("() => window.reloadGrove()")
        grown = ids | set(added)
        _wait_for_nodes(page, grown)
        after = _homes(page)
        _assert_same_homes(homes, after, ids)
        for new_id in added:
            nx, ny = after[new_id]
            nearest = min(
                math.hypot(nx - x, ny - y) for other, (x, y) in after.items() if other != new_id
            )
            assert nearest > APART_PX, f"a new item sits on another node ({nearest:.2f} px)"
        linked = page.evaluate(LINKED_JS, added)
        for new_id in added:
            nx, ny = after[new_id]
            assert linked[new_id], "a new item has no link on the map"
            near = min(math.hypot(nx - after[o][0], ny - after[o][1]) for o in linked[new_id])
            assert near <= NEAR_PX, f"a new item sits {near:.0f} px from the item it links to"
        held = _server_placed(page, real_ids | set(added))
        _assert_homes_are_stored(after, held, seeded)
        _wait_settled(page)
        _shot(page, shots, "graph-new", FADE_IN_MS)

        page.reload()
        _wait_for_nodes(page, grown)
        _assert_same_homes(after, _homes(page), grown)
        _wait_settled(page)

        # The workers toggle on: the workers panel shows, and no playback bar is built.
        page.locator("label.toggle").click()
        assert page.locator("#agents-toggle").is_checked()
        page.locator(".hood-score").wait_for(state="visible", timeout=WAIT_MS)
        assert page.locator(".hood-bar").count() == 0
        assert errors == []
    finally:
        context.close()
