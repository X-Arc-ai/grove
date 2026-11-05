"""Worker Follow and freshness in a real browser, with local synthetic API responses only."""

import argparse
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ui_fidelity import FIXTURE, Handler, Preview

FEED = "/v1/views/workers"
RECORD = "/v1/views/workers/record"
NODES = [row["data"]["id"] for row in FIXTURE["elements"]["nodes"]]
FIRST, SECOND, THIRD, NEXT = NODES[20], NODES[40], NODES[60], NODES[80]
MISSING = "item-not-on-the-map"
RUN = {
    "outcome": "ok",
    "started_at": "2026-11-04T10:00:00Z",
    "finished_at": "2026-11-04T10:05:00Z",
}
FRESH = {
    "total_items": 200,
    "dated_items": 190,
    "window_days": 30,
    "fresh_items": 150,
    "newest_change_at": "2026-11-04T10:05:00Z",
    "oldest_change_at": "2026-09-01T10:00:00Z",
}
EMPTY = {
    "total_items": 0,
    "dated_items": 0,
    "window_days": 30,
    "fresh_items": 0,
    "newest_change_at": None,
    "oldest_change_at": None,
}
TOLERANCE = 16
CENTRE = """id => {
  const graph = GroveView.graph(), node = graph.graphData().nodes.find(row => row.id === id);
  const box = document.querySelector('#canvas canvas').getBoundingClientRect();
  const point = graph.graph2ScreenCoords(node.x, node.y);
  return Math.hypot(point.x - box.width / 2, point.y - box.height / 2);
}"""
CAMERA = "() => { const g = GroveView.graph(), c = g.centerAt(); return [c.x, c.y, g.zoom()]; }"
WATCH = """() => {
  const real = GroveView.followItems;
  window.__followCalls = [];
  GroveView.followItems = function (ids, label) {
    const shown = real.apply(this, arguments);
    window.__followCalls.push({ ids, label, shown });
    return shown;
  };
}"""


def feed(state="idle"):
    rows = (
        ("learner", "limited", "legacy_learner_only"),
        ("curator", "unavailable", "curator_authority_unavailable"),
        ("connector", "limited", "legacy_connector_only"),
    )
    return {
        "schema": "grove.worker-activity.v1",
        "observed_at": "2026-11-04T12:00:00Z",
        "observation_kind": "stored_run_records",
        "service_state": "unknown",
        "service_reason": "no_process_heartbeat",
        "recent_limit": 20,
        "recent_has_more": False,
        "recent_runs": [],
        "agents": [
            {
                "agent": agent,
                "policy": {"state": "allowed", "reason": "ordinary_world_policy"},
                "capability": {"state": level, "reason": reason},
                "activity": {
                    "state": state if agent == "learner" else "idle",
                    "running_records": int(agent == "learner" and state == "recorded_running"),
                    "stale_records": 0,
                },
            }
            for agent, level, reason in rows
        ],
    }


def record(ids, freshness=FRESH):
    trail = None
    if ids:
        trail = dict(RUN, changed_items=len(ids), item_ids=ids, has_more=False)
    return {
        "schema": "grove.worker-record.v1",
        "observed_at": "2026-11-04T12:00:00Z",
        "observation_kind": "stored_change_entries",
        "agents": [
            {"agent": "learner", "last_run": RUN, "trail": trail},
            {"agent": "curator", "last_run": RUN, "trail": None},
            {"agent": "connector", "last_run": None, "trail": None},
        ],
        "freshness": freshness,
    }


class FollowHandler(Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        self.server.reads[path] = self.server.reads.get(path, 0) + 1
        return super().do_GET()


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FollowHandler)
    server.reference = None
    Preview.reset(server)
    server.signed_in = True
    server.has_items = True
    server.reads = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Walk:
    def __init__(self, page, server, width, theme):
        self.page, self.server, self.width, self.theme = page, server, width, theme
        self.step = "load"

    def at(self, step):
        self.step = step
        print(f"  step: {step}", flush=True)

    def answer(self, activity, body, status=200):
        self.server.metadata = {
            FEED: {"body": activity},
            RECORD: {"body": body, "status": status},
        }

    def button(self, agent="learner"):
        return self.page.locator(f'[data-worker="{agent}"] .hood-live-worker-head button')

    def line(self, agent="learner"):
        return self.page.locator(f'[data-worker="{agent}"] .hood-live-record')

    def refresh(self):
        self.page.locator("[data-worker-refresh]").click()
        self.page.wait_for_function("!document.querySelector('.hood-live-message')")
        self.page.wait_for_timeout(150)

    def toggle(self):
        self.page.evaluate("document.getElementById('agents-toggle').click()")

    def open_panel(self):
        if not self.page.evaluate("document.getElementById('agents-toggle').checked"):
            self.toggle()
        self.page.locator("#grove-worker-panel").wait_for(state="visible")
        self.page.wait_for_timeout(200)

    def distance(self, item):
        return self.page.evaluate(CENTRE, item)

    def settled_on(self, item):
        self.page.wait_for_timeout(1050)
        seen = self.distance(item)
        assert seen <= TOLERANCE, f"{item} is {seen:.1f}px from the screen centre"

    def following(self):
        return self.page.evaluate("__hood.following()")

    def follow(self):
        from playwright.sync_api import expect

        self.button().click()
        expect(self.button()).to_have_text("Following")
        expect(self.button()).to_have_attribute("aria-pressed", "true")

    def ended(self):
        from playwright.sync_api import expect

        assert self.following() is None, "Following must end"
        expect(self.button()).to_have_text("Follow")
        expect(self.button()).to_have_attribute("aria-pressed", "false")
        expect(self.page.locator("[data-worker-overview]")).to_have_count(0)
        expect(self.page.locator("#selection-pill")).to_be_hidden()

    def free_point(self):
        return (self.width / 2, 170) if self.width < 700 else (300, 450)


def load(page, base, theme):
    page.goto(base + "/")
    page.wait_for_function(
        "window.GroveView && GroveView.graph() && GroveView.graph().graphData().nodes.length > 200",
    )
    page.wait_for_timeout(600)
    seen = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert seen == theme, f"The page must open in {theme}, not {seen}"
    page.evaluate(WATCH)


def follow_part(walk, shots):
    from playwright.sync_api import expect

    page = walk.page
    trail = [FIRST, SECOND, THIRD, MISSING]

    walk.at("a worker with a trail has an enabled Follow")
    walk.answer(feed(), record(trail))
    walk.open_panel()
    expect(walk.button()).to_be_enabled(timeout=4000)
    expect(walk.line()).to_contain_text("Last recorded change: 4 items, ")

    walk.at("Follow moves the real camera to the newest recorded item")
    start = walk.distance(FIRST)
    assert start > 40, f"The start must be away from the item, not {start:.1f}px"
    walk.follow()
    walk.settled_on(FIRST)
    calls = page.evaluate("__followCalls")
    assert len(calls) == 1, calls
    assert calls[0]["shown"] == 3, calls
    assert calls[0]["label"] == "Learner: 3 recorded items", calls
    assert walk.following() == "learner"
    expect(page.locator("#selection-pill")).to_contain_text("Learner: 3 recorded items")
    expect(page.locator("[data-worker-overview]")).to_have_text("Overview")
    if walk.width < 700:
        top = page.locator("#grove-worker-panel").bounding_box()["y"]
        assert top > 450, f"The phone panel must leave the centre clear, top {top}"
    page.screenshot(path=str(shots / f"follow-{walk.width}-{walk.theme}.png"))

    walk.at("Overview ends following")
    page.locator("[data-worker-overview]").click()
    walk.ended()
    page.wait_for_timeout(1000)
    assert walk.distance(FIRST) > TOLERANCE, "Overview must leave the item"

    walk.at("Escape ends following")
    walk.follow()
    page.keyboard.press("Escape")
    walk.ended()

    walk.at("Escape on a focused checkbox still returns to the overview")
    page.wait_for_timeout(1000)
    walk.follow()
    walk.settled_on(FIRST)
    page.locator("#agents-toggle").focus()
    page.keyboard.press("Escape")
    walk.ended()
    page.wait_for_timeout(1000)
    assert walk.distance(FIRST) > TOLERANCE, "Escape must return to the overview"

    walk.at("The toggle button ends following")
    walk.follow()
    walk.button().click()
    walk.ended()

    walk.at("A canvas press ends following without moving the camera")
    walk.follow()
    walk.settled_on(FIRST)
    before = page.evaluate(CAMERA)
    page.mouse.move(*walk.free_point())
    page.mouse.down()
    walk.ended()
    after = page.evaluate(CAMERA)
    page.mouse.up()
    assert max(abs(a - b) for a, b in zip(before, after)) < 1, (before, after)

    walk.at("A wheel gesture ends following")
    page.wait_for_timeout(700)
    # On a phone the press above can land on a node, and its panel covers the worker panel.
    page.evaluate("GroveView.close()")
    page.wait_for_timeout(300)
    walk.follow()
    page.mouse.move(*walk.free_point())
    page.mouse.wheel(0, 40)
    expect(walk.button()).to_have_text("Follow")
    walk.ended()

    walk.at("The workers toggle ends following")
    walk.follow()
    walk.toggle()
    page.wait_for_function("__hood.following() === null")
    walk.open_panel()

    walk.at("No run and no changes stay disabled with plain words")
    expect(walk.button("curator")).to_be_disabled()
    expect(walk.line("curator")).to_have_text(
        "Last run recorded no item changes. Nothing to follow."
    )
    expect(walk.button("connector")).to_be_disabled()
    expect(walk.line("connector")).to_have_text("No run recorded yet. Nothing to follow.")

    walk.at("With no trail the camera never moves after a click")
    walk.answer(feed(), record([]))
    walk.refresh()
    page.wait_for_timeout(900)
    before = page.evaluate(CAMERA)
    calls = len(page.evaluate("__followCalls"))
    for button in page.locator("[data-worker-follow]").all():
        button.dispatch_event("click")
    page.wait_for_timeout(900)
    after = page.evaluate(CAMERA)
    assert max(abs(a - b) for a, b in zip(before, after)) < 1, (before, after)
    assert len(page.evaluate("__followCalls")) == calls, "A disabled Follow must do nothing"
    assert walk.following() is None

    walk.at("A trail off the map is disabled and says so")
    walk.answer(feed(), record([MISSING]))
    walk.refresh()
    expect(walk.button()).to_be_disabled()
    expect(walk.line()).to_contain_text("Those items are not on your map.")

    walk.at("A record read failure keeps the status feed")
    walk.answer(feed(), {"error": {"code": "unavailable"}}, 503)
    walk.refresh()
    expect(page.locator("[data-worker]")).to_have_count(3)
    expect(page.locator(".hood-live-activity").first).to_have_text("No active run recorded")
    expect(walk.line()).to_have_text("Recorded work could not be read.")
    expect(walk.button()).to_be_disabled()

    walk.at("An active run re-reads, and the camera moves only on a new newest item")
    walk.answer(feed("recorded_running"), record(trail))
    walk.refresh()
    walk.follow()
    walk.settled_on(FIRST)
    reads = walk.server.reads.get(RECORD, 0)
    before = page.evaluate(CAMERA)
    calls = len(page.evaluate("__followCalls"))
    page.wait_for_timeout(5400)
    assert walk.server.reads.get(RECORD, 0) > reads, "An active run must re-read the record"
    after = page.evaluate(CAMERA)
    assert max(abs(a - b) for a, b in zip(before, after)) < 1, (before, after)
    assert len(page.evaluate("__followCalls")) == calls, "The same item must not move the camera"
    walk.answer(feed("recorded_running"), record([NEXT, FIRST, SECOND]))
    page.wait_for_function("count => __followCalls.length > count", arg=calls, timeout=7000)
    walk.settled_on(NEXT)
    assert walk.following() == "learner"

    walk.at("An idle worker has no timer")
    walk.answer(feed(), record(trail))
    page.locator("[data-worker-overview]").click()
    walk.refresh()
    walk.follow()
    reads = walk.server.reads.get(RECORD, 0)
    feeds = walk.server.reads.get(FEED, 0)
    page.wait_for_timeout(5600)
    assert walk.server.reads.get(RECORD, 0) == reads, "An idle worker must not re-read the record"
    assert walk.server.reads.get(FEED, 0) == feeds, "An idle worker must not re-read the feed"
    walk.button().click()
    walk.ended()


def freshness_part(walk):
    from playwright.sync_api import expect

    page = walk.page
    words = page.locator("[data-worker-freshness]")

    walk.at("The freshness bar and words equal the record")
    walk.answer(feed(), record([FIRST]))
    walk.open_panel()
    expect(words).to_be_visible(timeout=4000)
    walk.refresh()
    expect(words).to_contain_text("Grove freshness")
    expect(words).to_contain_text("150 of 200 items updated in the last 30 days.")
    expect(words).to_contain_text("Latest update ")
    expect(words).to_contain_text("10 items have no update time.")
    expect(words).to_contain_text("Older than 30 days")
    expect(words).not_to_contain_text("Refreshing soon")
    share = page.evaluate(
        "() => { const track = document.querySelector('[data-worker-freshness] .gp-freshness-track');"
        " return track.firstElementChild.getBoundingClientRect().width / track.getBoundingClientRect().width; }"
    )
    assert abs(share - 0.75) < 0.02, f"The bar must fill 75%, not {share:.3f}"
    summary = page.evaluate("GroveCare.summary().freshness")
    assert summary["percent"] == 75, summary
    assert summary["text"].startswith("150 of 200 items updated"), summary

    walk.at("No items shows the plain words and no bar")
    walk.answer(feed(), record([FIRST], EMPTY))
    walk.refresh()
    expect(words).to_contain_text("No items recorded yet, so freshness has no value.")
    expect(page.locator("[data-worker-freshness] .gp-freshness-track")).to_have_count(0)
    summary = page.evaluate("GroveCare.summary().freshness")
    assert summary["percent"] is None, summary

    walk.at("A failed read shows the failure words")
    walk.answer(feed(), {"error": {"code": "unavailable"}}, 503)
    walk.refresh()
    expect(words).to_contain_text("Freshness could not be read.")


def main(shots, only_freshness):
    from playwright.sync_api import sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    walk = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (1440, 390):
                for theme in ("light", "dark"):
                    print(f"{width}px {theme}", flush=True)
                    context = browser.new_context(
                        viewport={"width": width, "height": 900},
                        reduced_motion="no-preference",
                    )
                    context.add_init_script(
                        f"try{{localStorage.setItem('grove-minimal-codex-theme','{theme}')}}catch(_){{}}"
                    )
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on("pageerror", lambda error, seen=errors: seen.append(str(error)))
                    walk = Walk(page, server, width, theme)
                    walk.answer(feed(), record([FIRST, SECOND, THIRD, MISSING]))
                    load(page, base, theme)
                    if not only_freshness:
                        follow_part(walk, shots)
                    freshness_part(walk)
                    assert not errors, errors
                    print(f"PASS {width}px {theme}", flush=True)
                    context.close()
            if not only_freshness:
                print("reduced motion", flush=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
                )
                context.add_init_script(
                    "try{localStorage.setItem('grove-minimal-codex-theme','light')}catch(_){}"
                )
                page = context.new_page()
                walk = Walk(page, server, 1440, "light")
                walk.answer(feed(), record([FIRST, SECOND]))
                load(page, base, "light")
                walk.at("Reduced motion still ends on the newest recorded item")
                walk.open_panel()
                walk.follow()
                walk.settled_on(FIRST)
                print("PASS reduced motion", flush=True)
                context.close()
            browser.close()
    except AssertionError as error:
        where = f"{walk.width}px {walk.theme}: {walk.step}" if walk else "before the first page"
        print(f"FAIL {where}\n{error}", flush=True)
        sys.exit(1)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--only-freshness", action="store_true")
    args = parser.parse_args()
    main(args.shots, args.only_freshness)
