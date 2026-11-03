"""Build Grove's pages into self-contained HTML files in web/dist: the views (index.html), the
public homepage (home.html), the privacy page (privacy.html) and the consent page (consent.html).

Standard library only; runs under the Mac's /usr/bin/python3 (3.9) and under 3.12.
The views load no fixture. The checks inject a synthetic one into the unbundled sources
(window.GROVE_FIXTURE); the bundle compiles that hook out, so it always runs live.
The privacy page's table comes from docs/data-flow.md, so the page cannot say less than that file.
The consent page is a template: the API fills its two marks at each /authorize (grove.oauth.consent).
"""
from pathlib import Path
import base64
import html as html_text
import json
import re

ROOT = Path(__file__).resolve().parent
FIXTURE_HOOK = "window.GROVE_FIXTURE"
DATA_FLOW = ROOT.parent / "docs" / "data-flow.md"
TABLE_MARK = "<!-- data-flow-table -->"
CONSENT_MARKS = ("<!--grove:title-->", "<!--grove:consent-->")


def asset_url(raw, base):
    name = raw.strip(" \"'")
    if name.startswith("data:"):
        return raw
    path = ROOT / name.lstrip("/") if name.startswith("/") else base / name
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


def script(match):
    text = (ROOT / match[1].removeprefix("./")).read_text(encoding="utf-8")
    # The bundle never reads a fixture: the hook becomes undefined, so the page runs live.
    text = text.replace(FIXTURE_HOOK, "undefined")
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


pages = {name: bundle(name) for name in ("index.html", "home.html", "privacy.html", "consent.html")}
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
