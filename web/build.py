"""Build Grove's HTML and content-hashed public assets in web/dist: the views (index.html), the
public homepage (home.html), privacy, consent and forget pages.

Standard library only; runs under the Mac's /usr/bin/python3 (3.9) and under 3.12.
The ordinary views load no fixture. Their bundle compiles the fixture hook out.
Only demo-onboarding.html carries the tracked invented fixture and page-memory state.
The privacy generator writes docs/data-flow.md; the page includes every generated table row.
The consent page is a template: the API fills its two marks at each /authorize (grove.oauth.consent).
"""
import base64
import html as html_text
import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE_HOOK = "window.GROVE_FIXTURE"
DATA_FLOW = ROOT.parent / "docs" / "data-flow.md"
TABLE_MARK = "<!-- data-flow-table -->"
CONSENT_MARKS = ("<!--grove:title-->", "<!--grove:consent-->")
FLOW_SOURCE = ROOT.parent / "server" / "grove" / "privacy.py"


def generate_data_flow():
    """The same fixed vocabulary serves the build and the runtime configuration endpoint."""
    if not FLOW_SOURCE.is_file():
        raise SystemExit("build.py: the data-flow generator is missing")
    spec = importlib.util.spec_from_file_location("grove_build_privacy", FLOW_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    DATA_FLOW.parent.mkdir(parents=True, exist_ok=True)
    DATA_FLOW.write_text(module.markdown_statement(), encoding="utf-8")


ASSET_MANIFEST = {}
DEFERRED = {"app-screens.js": "guides", "integrations.js": "catalog"}


def emit_asset(data, suffix, content_type):
    name = hashlib.sha256(data).hexdigest() + suffix
    folder = ROOT / "dist" / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    ASSET_MANIFEST[name] = content_type
    return "/assets/grove/" + name


def asset_url(raw, base):
    name = raw.strip(" \"'")
    if name.startswith("data:"):
        return raw
    path = ROOT / name.lstrip("/") if name.startswith("/") else base / name
    if path.suffix in (".woff2", ".woff"):
        return emit_asset(path.read_bytes(), path.suffix, "font/" + path.suffix[1:])
    mime = {".woff2": "font/woff2", ".woff": "font/woff", ".svg": "image/svg+xml"}.get(path.suffix, "application/octet-stream")
    # A data: URL is percent-decoded before its base64 is read, so %41 and %61 give the browser
    # the same bytes. Encoding every A and a keeps any word that holds an a from appearing in the
    # page text by chance inside encoded data (the R1 no-names words all hold one).
    data = base64.b64encode(path.read_bytes()).decode().replace("A", "%41").replace("a", "%61")
    return "data:" + mime + ";base64," + data


screens = json.loads((ROOT / "app-screens.json").read_text(encoding="utf-8"))
(ROOT / "app-screens.js").write_text("/* Static illustrative screens from an earlier walkthrough. */\nwindow.GroveAppScreenCSS=" + json.dumps((ROOT / "app-screens.css").read_text(encoding="utf-8")) + ";\nwindow.GroveAppScreens=" + json.dumps(screens) + ";\n", encoding="utf-8")


def css(match):
    path = ROOT / match[1].removeprefix("./")
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"url\(([^)]+)\)", lambda m: "url(" + asset_url(m[1], path.parent) + ")", text)
    return "<style>\n" + text + "\n</style>"


def script(match, demo=False):
    name = Path(match[1]).name
    if demo and name in {"live.js", "pipedream.js", "conversation-preview.js"}:
        return ""
    text = (ROOT / match[1].removeprefix("./")).read_text(encoding="utf-8")
    if name == "conversation-preview.js":
        data = json.loads((ROOT / "demo.synthetic.json").read_text(encoding="utf-8"))
        payload = "window.GroveClaudePreviewData=" + json.dumps(data).replace("<", "\\u003c") + ";"
        url = emit_asset(payload.encode("utf-8"), ".js", "text/javascript")
        text = "GroveDeferred.register('claude-preview'," + json.dumps(url) + ");\n" + text
    if name in DEFERRED:
        url = emit_asset(text.encode("utf-8"), ".js", "text/javascript")
        return "<script>GroveDeferred.register(" + json.dumps(DEFERRED[name]) + "," + json.dumps(url) + ");</script>"
    # The ordinary bundle never reads a fixture; the explicit demo is separate.
    if not demo:
        text = text.replace(FIXTURE_HOOK, "undefined")
    elif name in {"simulation.js", "native-app.js", "agents.js"}:
        # These existing modules keep preferences. The demo gives them page memory,
        # without changing the browser's storage or the ordinary app's modules.
        text = "(function(localStorage){\n" + text + "\n})(window.GroveDemo.storage);"
    return "<script>\n" + text.replace("</script", "<\\/script") + "\n</script>"


def table_rows(markdown):
    """The rows of the first table under the first heading, as lists of cell text."""
    lines = markdown.splitlines()
    heading = next((i for i, line in enumerate(lines) if line.startswith("#")), None)
    if heading is None:
        raise SystemExit("build.py: docs/data-flow.md has no heading")
    rows = []
    for line in lines[heading + 1:]:
        if line.lstrip().startswith("#"):
            break
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif rows:
            break
    if len(rows) < 3 or not all(set(cell) <= set(":-") for cell in rows[1]):
        raise SystemExit("build.py: docs/data-flow.md has no table under its first heading")
    if any(len(row) != len(rows[0]) for row in rows):
        raise SystemExit("build.py: a row of the table in docs/data-flow.md has another column count")
    return [rows[0]] + rows[2:]


def inline(cell):
    """Escaped cell text; a pair of backticks becomes a code element."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html_text.escape(cell, quote=False))


def data_flow_table():
    if not DATA_FLOW.is_file():
        raise SystemExit("build.py: docs/data-flow.md is missing")
    header, *rows = table_rows(DATA_FLOW.read_text(encoding="utf-8"))
    out = ['<table class="flow-table">', "<thead><tr>"]
    out += ['<th scope="col">' + inline(name) + "</th>" for name in header]
    out.append("</tr></thead><tbody>")
    for row in rows:
        cells = []
        for name, cell in zip(header, row):
            label = html_text.escape(name.replace("`", ""), quote=True)
            # A status cell carries its word as an attribute too: the stylesheet marks it by that.
            status = ' data-status="' + html_text.escape(cell.lower(), quote=True) + '"' if name == "Status" else ""
            cells.append('<td data-label="' + label + '"' + status + ">" + inline(cell) + "</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def bundle(name):
    html = (ROOT / name).read_text(encoding="utf-8")
    html = re.sub(r'<link (?:href="([^"]+)" rel="stylesheet"|rel="stylesheet" href="([^"]+)")\s*/?>',
                  lambda m: css([None, m[1] or m[2]]), html)
    html = re.sub(r'<script src="([^"]+)"></script>', script, html)
    html = html.replace('href="./favicon.svg"', 'href="' + asset_url("favicon.svg", ROOT) + '"')
    if "GROVE_FIXTURE" in html or "fixture.js" in html or "fixture.json" in html:
        raise SystemExit("build.py: the bundle must not carry or reference a fixture")
    return html


def demo_bundle():
    """The explicit demo is the only artifact that carries invented graph data."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    fixture = json.loads((ROOT / "demo.synthetic.json").read_text(encoding="utf-8"))
    bootstrap = "<script>window.GROVE_FIXTURE=" + json.dumps(fixture).replace("<", "\\u003c") + ";</script>"
    html = html.replace('<script src="./simulation.js"></script>',
                        bootstrap + '<script src="./demo-onboarding.js"></script><script src="./simulation.js"></script>')
    html = re.sub(r'<link (?:href="([^"]+)" rel="stylesheet"|rel="stylesheet" href="([^"]+)")\s*/?>',
                  lambda m: css([None, m[1] or m[2]]), html)
    html = re.sub(r'<script src="([^"]+)"></script>', lambda m: script(m, demo=True), html)
    return html.replace('href="./favicon.svg"', 'href="' + asset_url("favicon.svg", ROOT) + '"')


generate_data_flow()
pages = {name: bundle(name) for name in ("index.html", "home.html", "privacy.html", "consent.html", "forget.html")}
pages["demo-onboarding.html"] = demo_bundle()
for mark in CONSENT_MARKS:
    if pages["consent.html"].count(mark) != 1:
        raise SystemExit("build.py: consent.html must hold the mark " + mark + " exactly once")
if pages["privacy.html"].count(TABLE_MARK) != 1:
    raise SystemExit("build.py: privacy.html must hold the data-flow mark exactly once")
pages["privacy.html"] = pages["privacy.html"].replace(TABLE_MARK, data_flow_table())
dist = ROOT / "dist"
dist.mkdir(exist_ok=True)
for name, html in pages.items():
    (dist / name).write_text(html, encoding="utf-8")
    print(f"Built {(dist / name).relative_to(ROOT.parent)} ({len(html.encode()):,} bytes)")

(dist / "assets" / "manifest.json").write_text(json.dumps(ASSET_MANIFEST, sort_keys=True), encoding="utf-8")
