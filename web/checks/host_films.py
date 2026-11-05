"""Connection films in Settings, in a real browser, with synthetic API responses only.

Run from web/checks:
uv run --no-project --python 3.12 --with playwright==1.58.0 python host_films.py --shots <folder>

--keep-going records every failed section and continues, so one run on an older
tree shows each defect. Without it the first failed section stops the run.

The picture is the film as a person sees it. A sample is a 76 by 48 grid of luminance
values, read from a screenshot, so the slow pulse on a highlighted control does not
count as a new frame. No film is driven from inside its sandboxed iframe.
"""

import argparse
import base64
import hashlib
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from ui_fidelity import WEB, Handler, Preview

FILM = ".gp-app-frame"
FRAME = ".gp-app-frame iframe"
TOGGLE = "[data-action=film-toggle]"
FRAME_SECONDS = 3
GRID = (76, 48)
# Measured at 1440 px: the highlight pulse moves 25 cells, the smallest frame change 151.
SAME_PICTURE_CELLS = 60
# The reader clock starts when the iframe is placed, before its first paint.
READER_LEAD_SECONDS = 0.4
# Rescaling a reference screen to the film width moves some cells on text edges.
REFERENCE_CELLS = 60
STEPS = {
    "muse": [
        "Connect Muse to Grove through a remote MCP connector.",
        "Ask Muse to add Grove as a remote MCP connector.",
        "Use the server URL below. Muse opens the Grove authorization page.",
        "Sign in to Grove, review the requested access, then return to Muse.",
    ],
    "chatgpt": [
        "Open ChatGPT and click your profile.",
        "Open Settings, then the custom connector settings.",
        "Add a remote MCP connector with the server URL below.",
        "Sign in to Grove and review access. Return to ChatGPT after approval.",
    ],
    "claude": [
        "Open Claude and click your profile.",
        "Open Settings, then the custom connector settings.",
        "Add a remote MCP connector with the server URL below.",
        "Sign in to Grove and review access. Return to Claude after approval.",
    ],
}
SCREENS = {
    "chatgpt": ("chatgpt-home", "chatgpt-menu"),
    "claude": ("claude-home", "claude-menu"),
}
EXPORT_FRAMES = {"chatgpt": 4, "chats": 6}
GRID_SCRIPT = """async ([data, width, height]) => {
  const image = new Image();
  image.src = 'data:image/png;base64,' + data;
  await image.decode();
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d');
  context.imageSmoothingQuality = 'high';
  context.drawImage(image, 0, 0, width, height);
  const pixels = context.getImageData(0, 0, width, height).data, cells = [];
  for (let i = 0; i < pixels.length; i += 4) {
    cells.push(Math.round(pixels[i] * .299 + pixels[i + 1] * .587 + pixels[i + 2] * .114));
  }
  return cells;
}"""
READER_SCRIPT = """() => [...document.querySelectorAll('[data-guide-reader] [data-guide-step]')]
  .findIndex(section => !section.hidden)"""
COLOUR_SCRIPT = """() => {
  const parse = value => {
    const parts = value.match(/[\\d.]+/g).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const light = colour => {
    const part = value => {
      value /= 255;
      return value <= .03928 ? value / 12.92 : Math.pow((value + .055) / 1.055, 2.4);
    };
    return .2126 * part(colour.r) + .7152 * part(colour.g) + .0722 * part(colour.b);
  };
  const backing = element => {
    for (; element; element = element.parentElement) {
      const colour = parse(getComputedStyle(element).backgroundColor);
      if (colour.a >= .99) return colour;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const contrast = (a, b) => {
    const high = Math.max(light(a), light(b)), low = Math.min(light(a), light(b));
    return (high + .05) / (low + .05);
  };
  return [...document.querySelectorAll('.film-frame')].map(frame => {
    const walker = document.createTreeWalker(frame, NodeFilter.SHOW_TEXT);
    let node, worst = 99, word = '';
    while ((node = walker.nextNode())) {
      if (!node.textContent.trim()) continue;
      const element = node.parentElement, style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const value = contrast(parse(style.color), backing(element));
      if (value < worst) { worst = value; word = node.textContent.trim().slice(0, 40); }
    }
    const root = frame.firstElementChild;
    return {
      card: getComputedStyle(root).backgroundColor,
      text: getComputedStyle(root).color,
      backing: light(backing(root)),
      contrast: Number(worst.toFixed(2)),
      word,
    };
  });
}"""


class FilmHandler(Handler):
    def do_GET(self):
        if urlparse(self.path).path == "/v1/sources/pending-exports":
            return self.send(
                {"account": self.server.account, "revision": 0, "pending_exports": {}}
            )
        return super().do_GET()


class Ledger:
    def __init__(self, keep_going):
        self.keep_going = keep_going
        self.passed = 0
        self.failed = []

    def check(self, label, action, *values):
        try:
            action(*values)
        except (AssertionError, PlaywrightTimeout) as error:
            words = str(error).strip().splitlines() or ["no message"]
            print(f"FAIL {label}: {words[0]}", flush=True)
            self.failed.append(label)
            if not self.keep_going:
                raise
        else:
            self.passed += 1
            print(f"PASS {label}", flush=True)


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FilmHandler)
    server.reference = None
    Preview.reset(server)
    server.signed_in = True
    server.has_items = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def grid(helper, shot):
    data = base64.b64encode(shot).decode()
    return helper.evaluate(GRID_SCRIPT, [data, GRID[0], GRID[1]])


def moved(first, second):
    return sum(abs(a - b) > 16 for a, b in zip(first, second))


def same(first, second):
    return moved(first, second) <= SAME_PICTURE_CELLS


def reader_index(page):
    return page.evaluate(READER_SCRIPT)


def picture(page, helper):
    return grid(helper, page.locator(FILM).screenshot())


def watch(page, helper, seconds):
    """Sample the picture and the reader together. Both reads bracket the shot."""
    rows = []
    stop = time.monotonic() + seconds
    while time.monotonic() < stop:
        before = reader_index(page)
        shot = page.locator(FILM).screenshot()
        rows.append(
            {
                "at": time.monotonic(),
                "before": before,
                "after": reader_index(page),
                "picture": grid(helper, shot),
            }
        )
        page.wait_for_timeout(100)
    return rows


def group(rows):
    """Give each sample the number of its picture, in order of first appearance."""
    pictures = []
    for row in rows:
        for index, known in enumerate(pictures):
            if same(row["picture"], known):
                row["group"] = index
                break
        else:
            pictures.append(row["picture"])
            row["group"] = len(pictures) - 1
    return pictures


def order_of(rows):
    order = []
    for row in rows:
        if not order or order[-1] != row["group"]:
            order.append(row["group"])
    return order


def assert_every_frame(rows, pictures, count):
    order = order_of(rows)
    assert len(pictures) == count, f"expected {count} pictures, saw {len(pictures)}"
    assert len(order) >= 2 * count, f"the film did not loop twice: {order}"
    assert order[:count] == list(range(count)), f"first loop was {order[:count]}"
    assert all(order[i] == order[i + count] for i in range(len(order) - count)), (
        f"the order did not repeat: {order}"
    )


def assert_reader_in_step(rows, count):
    start = rows[0]["before"]
    assert start >= 0, "the film has no reader step"
    changed = rows[0]["at"]
    worst = 0
    for previous, row in pairwise(rows):
        if row["before"] != previous["after"] or row["before"] != row["after"]:
            changed = row["at"]
        if row["before"] != row["after"]:
            continue
        if (row["group"] + start) % count != row["after"]:
            worst = max(worst, row["at"] - changed)
    assert worst <= READER_LEAD_SECONDS, (
        f"reader and picture disagreed for {worst:.2f} s"
    )
    assert {row["after"] for row in rows} == set(range(count)), (
        "the reader did not show every step"
    )


def assert_film_is_inert(page):
    frame = page.locator(FRAME)
    source = frame.get_attribute("srcdoc")
    assert frame.get_attribute("sandbox") == "", "the iframe must stay sandboxed"
    assert frame.get_attribute("inert") is not None, "the iframe must stay inert"
    assert frame.get_attribute("tabindex") == "-1"
    assert frame.get_attribute("aria-hidden") == "true"
    assert "<script" not in source.lower(), "the film must hold no script"
    content = frame.element_handle().content_frame()
    assert content.evaluate("document.scripts.length") == 0


def frame_sources(page):
    return (
        page.locator(FRAME)
        .get_attribute("srcdoc")
        .split('<section class="film-frame"')[1:]
    )


def assert_screen_sources(page, host):
    page.evaluate("GroveDeferred.load('guides')")
    parts = frame_sources(page)
    for index, key in enumerate(SCREENS[host]):
        screen = page.evaluate("key => GroveAppScreens[key]", key)
        assert screen, f"the repo has no screen {key}"
        assert index < len(parts) and screen in parts[index], (
            f"frame {index + 1} does not hold {key}"
        )


def reference(reference_page, helper, page, key):
    """Draw one app screen alone, with the film's own theme, away from the film."""
    style, screen = page.evaluate(
        """key => {
          const source = document.querySelector('.gp-app-frame iframe').getAttribute('srcdoc');
          const doc = new DOMParser().parseFromString(source, 'text/html');
          return [GroveAppScreenCSS + doc.querySelector('style').textContent, GroveAppScreens[key]];
        }""",
        key,
    )
    reference_page.set_content(
        f'<!doctype html><html><head><meta charset="utf-8"><style>{style}</style></head>'
        f'<body><div class="film-stage"><section class="film-frame">{screen}</section></div></body></html>'
    )
    return grid(helper, reference_page.locator(".film-stage").screenshot())


def assert_screen_pictures(reference_page, helper, page, host, pictures, start):
    page.evaluate("GroveDeferred.load('guides')")
    for index, key in enumerate(SCREENS[host]):
        wanted = reference(reference_page, helper, page, key)
        spot = (index - start) % max(len(pictures), 1)
        cells = moved(pictures[spot], wanted)
        assert cells <= REFERENCE_CELLS, (
            f"frame {index + 1} is not the {key} screen: {cells} cells differ"
        )


def assert_colours(page, theme):
    content = page.locator(FRAME).element_handle().content_frame()
    rows = content.evaluate(COLOUR_SCRIPT)
    assert rows, "the film has no frames"
    for index, row in enumerate(rows, 1):
        if theme == "dark":
            assert row["backing"] < 0.03, f"frame {index} is not dark: {row}"
            assert row["contrast"] >= 4.5, f"frame {index} text is not readable: {row}"
        else:
            assert row["backing"] > 0.8, f"frame {index} is not light: {row}"


def assert_pause(page, helper, pictures, count):
    toggle = page.locator(TOGGLE)
    assert toggle.get_attribute("aria-label") == "Pause guide"
    toggle.click()
    assert toggle.get_attribute("aria-label") == "Play guide"
    page.wait_for_timeout(400)
    assert_film_is_inert(page)
    held = page.locator(FILM).screenshot()
    step = reader_index(page)
    page.wait_for_timeout(int((FRAME_SECONDS + 1) * 1000))
    assert held == page.locator(FILM).screenshot(), "Pause must hold the picture"
    assert reader_index(page) == step, "Pause must hold the reader"
    start = grid(helper, held)
    toggle.click()
    assert toggle.get_attribute("aria-label") == "Pause guide"
    stop = time.monotonic() + FRAME_SECONDS + 1
    changed = None
    while time.monotonic() < stop and changed is None:
        page.wait_for_timeout(100)
        current = picture(page, helper)
        if not same(start, current):
            changed = current
    assert changed is not None, "Play must advance the picture again"
    known = [index for index, known in enumerate(pictures) if same(known, start)]
    following = pictures[(known[0] + 1) % count] if known else None
    assert following is not None and same(following, changed), (
        "Play must show the next frame"
    )
    assert reader_index(page) == (step + 1) % count, "Play must advance the reader"


def assert_steps_listed(page, host, rows_count):
    listed = page.locator(".gp-host-guide ol li").all_text_contents()
    assert listed == STEPS[host], f"the listed steps are {listed}"
    reader = page.locator("[data-guide-reader] [data-guide-step] p").all_text_contents()
    assert reader == STEPS[host], f"the reader steps are {reader}"
    assert rows_count == len(STEPS[host])


def open_page(browser, base, width, theme, motion):
    context = browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion=motion
    )
    page = context.new_page()
    errors = []
    outside = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def only_local(route):
        if urlparse(route.request.url).hostname == "127.0.0.1":
            route.continue_()
        else:
            outside.append(route.request.url)
            route.abort()

    page.route("**/*", only_local)
    page.set_default_timeout(10000)
    page.goto(base + "/")
    page.locator("#profile-button").wait_for(state="visible")
    if theme == "dark":
        page.locator("#theme-toggle").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    helper = context.new_page()
    helper.goto("about:blank")
    return context, page, helper, errors, outside


def open_connections(page):
    page.locator("#profile-button").click()
    page.locator('[data-settings-tab="connections"]').click()
    page.locator('[data-host-inline="muse"]').wait_for(state="visible")


def open_host(page, host):
    page.locator(f'[data-host-inline="{host}"]').click()
    page.locator(FRAME).wait_for(state="attached")
    # The opening reveal fades the box. Every sample starts after it.
    page.wait_for_timeout(800)


def close_host(page):
    page.locator("[data-action=host-back]").first.click()
    page.locator('[data-host-inline="muse"]').wait_for(state="visible")


def assert_clean(page, errors, outside):
    assert not errors, errors
    assert not outside, outside
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), (
        "the page must not scroll sideways"
    )


def motion_run(ledger, browser, base, width, theme, shots):
    context, page, helper, errors, outside = open_page(
        browser, base, width, theme, "no-preference"
    )
    reference_context = browser.new_context(viewport={"width": 760, "height": 475})
    reference_page = reference_context.new_page()
    open_connections(page)
    for host, steps in STEPS.items():
        label = f"{width} {theme} {host}"
        count = len(steps)
        open_host(page, host)
        rows = watch(page, helper, count * FRAME_SECONDS * 2 + 1)
        pictures = group(rows)
        ledger.check(f"{label} iframe inert", lambda: assert_film_is_inert(page))
        ledger.check(f"{label} every frame", assert_every_frame, rows, pictures, count)
        ledger.check(f"{label} reader", assert_reader_in_step, rows, count)
        ledger.check(
            f"{label} listed steps",
            assert_steps_listed,
            page,
            host,
            len(frame_sources(page)),
        )
        if host in SCREENS:
            start = max(rows[0]["before"], 0)
            ledger.check(
                f"{label} app screen source", assert_screen_sources, page, host
            )
            ledger.check(
                f"{label} app screen picture",
                assert_screen_pictures,
                reference_page,
                helper,
                page,
                host,
                pictures,
                start,
            )
        ledger.check(f"{label} colours", lambda: assert_colours(page, theme))
        ledger.check(
            f"{label} pause and play",
            assert_pause,
            page,
            helper,
            pictures,
            len(pictures),
        )
        page.screenshot(path=str(shots / f"film-{host}-{width}-{theme}.png"))
        close_host(page)
    ledger.check(f"{width} {theme} page", lambda: assert_clean(page, errors, outside))
    reference_context.close()
    context.close()


def deferred_run(ledger, browser, base, width, theme, shots):
    guide = hashlib.sha256((WEB / "app-screens.js").read_bytes()).hexdigest()
    url = f"{base}/assets/grove/{guide}.js"

    def hold_then_frames():
        context, page, helper, errors, outside = open_page(
            browser, base, width, theme, "no-preference"
        )
        held = []
        page.route(url, lambda route: held.append(route))
        open_connections(page)
        page.locator('[data-host-inline="chatgpt"]').click()
        notice = page.locator(".gp-host-guide [role=status]")
        notice.wait_for(state="visible", timeout=3000)
        assert notice.text_content() == "Loading your guide…", notice.text_content()
        assert page.locator(FRAME).count() == 0, "no film before the asset arrives"
        page.screenshot(path=str(shots / f"film-notice-{width}-{theme}.png"))
        assert len(held) == 1, f"the asset was asked {len(held)} times"
        held[0].continue_()
        page.locator(FRAME).wait_for(state="attached")
        assert page.locator(".gp-host-guide [role=status]").count() == 0
        page.wait_for_timeout(800)
        rows = watch(page, helper, 4 * FRAME_SECONDS + 1)
        pictures = group(rows)
        assert len(pictures) == 4, f"saw {len(pictures)} pictures after the notice"
        assert_clean(page, errors, outside)
        context.close()

    def failure_then_retry():
        context, page, _, errors, outside = open_page(
            browser, base, width, theme, "no-preference"
        )
        page.route(url, lambda route: route.fulfill(status=503, body=""))
        open_connections(page)
        page.locator('[data-host-inline="chatgpt"]').click()
        retry = page.locator("[data-retry-asset=guides]")
        retry.wait_for(state="visible", timeout=3000)
        page.unroute(url)
        retry.click()
        page.locator(FRAME).wait_for(state="attached")
        assert len(frame_sources(page)) == 4
        assert_clean(page, errors, outside)
        context.close()

    ledger.check(f"{width} {theme} notice then frames", hold_then_frames)
    ledger.check(f"{width} {theme} failed asset retry", failure_then_retry)


def assert_still(page, seconds):
    held = page.locator(FILM).screenshot()
    step = reader_index(page)
    page.wait_for_timeout(int(seconds * 1000))
    assert held == page.locator(FILM).screenshot(), "the picture moved with no press"
    assert reader_index(page) == step, "the reader moved with no press"
    return step


def assert_control(page):
    toggle = page.locator(TOGGLE)
    assert toggle.text_content() == "Next step", toggle.text_content()
    assert toggle.get_attribute("aria-label") == "Next guide step", (
        toggle.get_attribute("aria-label")
    )


def visible_step_text(page, width):
    section = page.locator("[data-guide-reader] section:not([hidden])")
    if section.count() != 1:
        return ""
    if width <= 600:
        assert section.is_visible(), "the reader step must be visible on a phone"
    return section.text_content()


def assert_stepping(page, helper, count, width):
    toggle = page.locator(TOGGLE)
    pictures = [picture(page, helper)]
    words = [visible_step_text(page, width)]
    assert reader_index(page) == 0, "the film must start on its first step"
    assert words[0], "the first step has no reader text"
    for press in range(1, count + 1):
        toggle.click()
        expected = press % count
        page.wait_for_timeout(450)
        assert reader_index(page) == expected, (
            f"press {press} shows reader step {reader_index(page)}"
        )
        now = picture(page, helper)
        text = visible_step_text(page, width)
        if press == count:
            assert same(now, pictures[0]), (
                "the last press must return to the first frame"
            )
            assert text == words[0], "the last press must return to the first step"
        else:
            assert not any(same(now, known) for known in pictures), (
                f"press {press} did not show a new frame"
            )
            assert text and text not in words, f"press {press} kept the step text"
            pictures.append(now)
            words.append(text)
    toggle.click()
    page.wait_for_timeout(450)
    assert same(picture(page, helper), pictures[1]), "the film must keep stepping"
    assert_still(page, FRAME_SECONDS + 0.6)


def reduced_film(ledger, page, helper, label, count, width):
    ledger.check(
        f"{label} reduced still", lambda: assert_still(page, FRAME_SECONDS + 0.6)
    )
    ledger.check(f"{label} reduced control", lambda: assert_control(page))
    ledger.check(
        f"{label} reduced steps",
        lambda: assert_stepping(page, helper, count, width),
    )


def reduced_run(ledger, browser, base, width, theme, shots):
    context, page, helper, errors, outside = open_page(
        browser, base, width, theme, "reduce"
    )
    open_connections(page)
    for host, steps in STEPS.items():
        open_host(page, host)
        reduced_film(ledger, page, helper, f"{width} {theme} {host}", len(steps), width)
        page.screenshot(path=str(shots / f"film-reduced-{host}-{width}-{theme}.png"))
        close_host(page)
    page.locator('[data-settings-tab="sources"]').click()
    page.locator("[data-add-group=chats]").click()
    for provider, count in EXPORT_FRAMES.items():
        page.locator(f"[data-guide={provider}]").click()
        page.locator(FRAME).wait_for(state="attached")
        page.wait_for_timeout(800)
        reduced_film(
            ledger, page, helper, f"{width} {theme} export {provider}", count, width
        )
        page.screenshot(
            path=str(shots / f"film-reduced-export-{provider}-{width}-{theme}.png")
        )
        page.locator("[data-action=guide-back]").first.click()
        page.locator(f"[data-guide={provider}]").wait_for(state="visible")
    ledger.check(
        f"{width} {theme} reduced page", lambda: assert_clean(page, errors, outside)
    )
    context.close()


def health_run(ledger, browser, base, width, theme, shots):
    context, page, _, errors, outside = open_page(
        browser, base, width, theme, "no-preference"
    )
    # This wraps GroveCare only to set the value that web/agents.js supplies.
    page.evaluate(
        """() => {
          const summary = GroveCare.summary, refresh = GroveCare.refresh;
          window.filmRefreshes = 0;
          GroveCare.summary = () => {
            const value = summary();
            if (window.filmFreshness === undefined) delete value.freshness;
            else value.freshness = window.filmFreshness;
            return value;
          };
          GroveCare.refresh = () => { window.filmRefreshes += 1; return refresh(); };
        }"""
    )
    line = page.locator(".gp-health-freshness")
    value = page.locator("[data-live-freshness]")

    def fallback():
        page.locator("#health-link").click()
        value.wait_for(state="visible", timeout=3000)
        assert value.text_content() == "Not recorded", value.text_content()
        assert line.locator(".gp-freshness-track").count() == 0
        assert line.locator("small").count() == 0
        assert page.evaluate("filmRefreshes") == 1, "Health must read the record once"

    def recorded():
        text = "5 of 6 items updated in the last 30 days. Latest update Nov 4, 2026."
        page.evaluate(
            """text => {
              window.filmFreshness = { percent: 83, text };
              dispatchEvent(new CustomEvent('grove:worker-status'));
            }""",
            text,
        )
        page.wait_for_function(
            "document.querySelector('[data-live-freshness]')?.textContent === '83%'",
            timeout=3000,
        )
        track = line.locator(".gp-freshness-track").bounding_box()
        fill = line.locator(".gp-freshness-track i").bounding_box()
        assert track and fill, "the known value has a bar"
        assert abs(fill["width"] / track["width"] - 0.83) < 0.02, (fill, track)
        assert line.locator("small").text_content() == text
        page.screenshot(path=str(shots / f"film-health-{width}-{theme}.png"))

    def empty():
        text = "No items recorded yet, so freshness has no value."
        page.evaluate(
            """text => {
              window.filmFreshness = { percent: null, text };
              dispatchEvent(new CustomEvent('grove:worker-status'));
            }""",
            text,
        )
        page.wait_for_function(
            "document.querySelector('[data-live-freshness]')?.textContent === 'Not recorded'",
            timeout=3000,
        )
        assert line.locator(".gp-freshness-track").count() == 0
        assert line.locator("small").text_content() == text

    ledger.check(f"{width} {theme} health fallback", fallback)
    ledger.check(f"{width} {theme} health recorded", recorded)
    ledger.check(f"{width} {theme} health empty", empty)
    ledger.check(
        f"{width} {theme} health page", lambda: assert_clean(page, errors, outside)
    )
    context.close()


def main(shots, keep_going):
    shots.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(WEB / "build.py")],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    ledger = Ledger(keep_going)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (1440, 390):
                for theme in ("light", "dark"):
                    health_run(ledger, browser, base, width, theme, shots)
                    reduced_run(ledger, browser, base, width, theme, shots)
                    deferred_run(ledger, browser, base, width, theme, shots)
                    motion_run(ledger, browser, base, width, theme, shots)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f"RESULT {ledger.passed} passed, {len(ledger.failed)} failed", flush=True)
    return 1 if ledger.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.shots, args.keep_going))
