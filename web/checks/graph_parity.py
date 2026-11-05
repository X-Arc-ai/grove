"""Browser proof of the live map, with invented data on a local check server.

Records every canvas text call in a real page at every zoom, on a graph of about 400 nodes and one
of about 5,000, at 1440 and 390 px, light and dark. Only the owner's name may be written at rest.
Then it proves the +X count over a read and a reload, and its click.

Run from web/checks:
    uv run --no-project --python 3.12 --with playwright==1.58.0 python graph_parity.py \
        --shots <folder>
"""

import argparse
import json
import random
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import ui_fidelity

WEB = Path(__file__).resolve().parents[1]
READ = "2026-11-04T12:00:00Z"
OLD = "2026-10-10T09:00:00Z"
NEW = "2026-11-04T08:00:00Z"
ZOOMS = (0.05, 0.1, 0.2, 0.3, 0.39, 0.4, 0.41, 0.55, 0.6, 0.8, 1, 1.5, 2, 3, 4, 8)
RECORD_TEXT = """(() => {
    window.__texts = [];
    for (const name of ['fillText', 'strokeText']) {
        const original = CanvasRenderingContext2D.prototype[name];
        CanvasRenderingContext2D.prototype[name] = function (text, x, y) {
            const at = this.getTransform().transformPoint(new DOMPoint(x, y));
            window.__texts.push({ text: String(text), x: at.x, y: at.y });
            return original.apply(this, arguments);
        };
    }
})();"""


def brain(count, new_share=0.0):
    """An invented graph in the shape of a real brain: heavy hubs, leaves, links across groups."""
    rand = random.Random(count + 7)
    hubs = max(6, count // 130)
    nodes, edges = [], []

    def node(index, kind, label, recorded):
        nodes.append(
            {
                "data": {
                    "id": f"n{index:05d}",
                    "label": label,
                    "type": kind,
                    "summary": "",
                    "status": "active",
                    "file_path": "",
                    "valid_at": recorded,
                    "invalid_at": None,
                    "created_at": recorded,
                    "updated_at": recorded,
                    "status_since": None,
                    "verified_at": recorded,
                    "version": 1,
                    "properties": {},
                    "has_changes": False,
                }
            }
        )
        return f"n{index:05d}"

    def link(source, target, recorded):
        if source != target:
            edges.append(
                {
                    "data": {
                        "id": f"e{len(edges):06d}",
                        "source": source,
                        "target": target,
                        "label": "relates to",
                        "verb": "relates to",
                        "cardinality": "many",
                        "canonical": False,
                        "canonical_of": None,
                        "valid_at": "2020-01-01T00:00:00Z",
                        "invalid_at": None,
                        "created_at": recorded,
                        "note": "",
                        "source_field": "store",
                    }
                }
            )

    hub_ids = [
        node(h, "organisation" if h % 3 == 0 else "plan", f"Invented hub number {h}", OLD)
        for h in range(hubs)
    ]
    weights = [1 / (h + 1) ** 0.75 for h in range(hubs)]
    kinds = ["event"] * 33 + ["task"] * 19 + ["observation"] * 18 + ["decision"] * 13
    kinds += ["person"] * 7 + ["assumption"] * 6 + ["problem"] * 2 + ["place"] * 2
    members = {hub: [] for hub in hub_ids}
    for index in range(hubs, count):
        hub = rand.choices(hub_ids, weights)[0]
        recorded = NEW if rand.random() < new_share else OLD
        item = node(index, rand.choice(kinds), f"Invented item {index}", recorded)
        link(item, hub, recorded)
        members[hub].append(item)
    ids = [row["data"]["id"] for row in nodes]
    while len(edges) < int(count * 2.85):
        hub = rand.choices(hub_ids, weights)[0]
        if len(members[hub]) < 2:
            continue
        one = rand.choice(members[hub])
        roll = rand.random()
        other = (
            rand.choice(members[hub])
            if roll < 0.72
            else rand.choices(hub_ids, weights)[0]
            if roll < 0.88
            else rand.choice(ids)
        )
        link(one, other, OLD)
    return {"elements": {"nodes": nodes, "edges": edges}}


def use_graph(page, base, data):
    body = dict(
        elements=data["elements"],
        stats={},
        time={"min": None, "max": None},
        self_item_id=None,
        generated_at=READ,
    )
    page.request.post(
        base + "/__test/metadata",
        data=json.dumps(
            {
                "/v1/views/graph": {"body": body},
                "/v1/sources/pending-exports": {
                    "body": {
                        "account": "synthetic-review-account",
                        "revision": 1,
                        "pending_exports": {},
                    }
                },
            }
        ),
        headers={"content-type": "application/json"},
    )


def wait_for_map(page):
    # A poll from here: the page's content policy stops wait_for_function.
    for _ in range(120):
        if page.evaluate("!!window.GroveView && GroveView.nodes().length > 1"):
            return
        page.wait_for_timeout(250)
    raise AssertionError("the map did not draw")


def open_map(browser, base, data, width, theme):
    context = browser.new_context(
        viewport={"width": width, "height": 900 if width > 500 else 844}, color_scheme=theme
    )
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if urlparse(route.request.url).hostname == "127.0.0.1"
            else route.abort()
        ),
    )
    context.add_init_script(RECORD_TEXT)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.request.post(base + "/__test/account", data={"has_items": True})
    page.request.post(base + "/v1/auth/email/verify", data={"code": "246810"})
    use_graph(page, base, data)
    page.goto(base + "/")
    wait_for_map(page)
    page.wait_for_timeout(1800)
    if theme == "dark" and page.evaluate("document.documentElement.dataset.theme") != "dark":
        page.click("#theme-toggle")
        page.wait_for_timeout(400)
    return context, page, errors


def assert_no_text_in_nodes(page, label):
    """Each text the page writes on the canvas at each zoom: at rest only the owner name is left."""
    for zoom in ZOOMS:
        page.evaluate("z => { GroveView.graph().zoom(z); window.__texts = []; }", zoom)
        page.wait_for_timeout(180)
        words = {row["text"] for row in page.evaluate("window.__texts")}
        assert words == {"You"}, f"{label}: at zoom {zoom} the canvas wrote {sorted(words)[:6]}"
    print(f"  {label}: {len(ZOOMS)} zooms, the owner name is the only text", flush=True)


def main(shots):
    from playwright.sync_api import sync_playwright

    shots.mkdir(parents=True, exist_ok=True)
    server = ui_fidelity.Preview(0, None)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for count in (400, 5000):
                data = brain(count)
                for width in (1440, 390):
                    for theme in ("light", "dark"):
                        label = f"{count}-{width}-{theme}"
                        context, page, errors = open_map(browser, base, data, width, theme)
                        stats = page.locator("#stats").inner_text()
                        assert stats.startswith(f"{count} nodes"), (label, stats)
                        assert "graph check" not in stats, (label, stats)
                        page.screenshot(path=str(shots / f"map-{label}.png"))
                        if width == 1440 and theme == "light":
                            assert_no_text_in_nodes(page, label)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        assert not errors, (label, errors[:3])
                        print(f"PASS {label}: {stats}", flush=True)
                        context.close()

            # +X: on the first read, after a later read, after a reload, and its click.
            data = brain(5000, new_share=0.03)
            fresh = sum(row["data"]["created_at"] == NEW for row in data["elements"]["nodes"])
            links = sum(row["data"]["created_at"] == NEW for row in data["elements"]["edges"])
            for theme in ("light", "dark"):
                context, page, errors = open_map(browser, base, data, 1440, theme)
                want = f"(+{fresh})"
                assert want in page.locator("#stats").inner_text(), "the first read shows the count"
                assert f"(+{links})" in page.locator("#stats").inner_text()
                page.click("#refresh-data")
                page.wait_for_timeout(1800)
                assert want in page.locator("#stats").inner_text(), "a later read keeps the count"
                page.reload()
                wait_for_map(page)
                page.wait_for_timeout(1500)
                if (
                    theme == "dark"
                    and page.evaluate("document.documentElement.dataset.theme") != "dark"
                ):
                    page.click("#theme-toggle")
                    page.wait_for_timeout(300)
                assert want in page.locator("#stats").inner_text(), "a reload keeps the count"
                page.screenshot(path=str(shots / f"delta-{theme}-count.png"))
                before = page.evaluate("GroveView.graph().zoom()")
                page.locator('[data-kind="nodes"]').click()
                page.wait_for_timeout(1400)
                pill = page.locator("#selection-pill").inner_text()
                assert f"{fresh} new nodes since 2026-11-03" in pill, pill
                assert page.evaluate("GroveView.graph().zoom()") <= before * 1.2, (
                    "the map stays in view"
                )
                page.screenshot(path=str(shots / f"delta-{theme}-nodes.png"))
                page.keyboard.press("Escape")
                page.locator('[data-kind="edges"]').click()
                page.wait_for_timeout(1400)
                assert (
                    f"{links} new connections since 2026-11-03"
                    in page.locator("#selection-pill").inner_text()
                )
                page.screenshot(path=str(shots / f"delta-{theme}-edges.png"))
                assert not errors, errors[:3]
                print(f"PASS +X {theme}: {fresh} nodes, {links} connections", flush=True)
                context.close()
            browser.close()
    finally:
        server.shutdown()
    print("PASS graph parity", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, required=True)
    args = parser.parse_args()
    subprocess.run([sys.executable, str(WEB / "build.py")], check=True)
    main(args.shots)
