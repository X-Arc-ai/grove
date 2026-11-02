"""Build Grove's views into one offline HTML file: web/dist/index.html.

Standard library only; runs under the Mac's /usr/bin/python3 (3.9) and under 3.12.
The page loads no fixture. The checks inject a synthetic one into the unbundled sources
(window.GROVE_FIXTURE); the bundle compiles that hook out, so it always runs live.
"""
from pathlib import Path
import base64
import json
import re

ROOT = Path(__file__).resolve().parent
FIXTURE_HOOK = "window.GROVE_FIXTURE"


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
html = (ROOT / "index.html").read_text(encoding="utf-8")


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


html = re.sub(r'<link (?:href="([^"]+)" rel="stylesheet"|rel="stylesheet" href="([^"]+)")\s*/?>',
              lambda m: css([None, m[1] or m[2]]), html)
html = re.sub(r'<script src="([^"]+)"></script>', script, html)
html = html.replace('href="./favicon.svg"', 'href="' + asset_url("favicon.svg", ROOT) + '"')
if "GROVE_FIXTURE" in html or "fixture.js" in html or "fixture.json" in html:
    raise SystemExit("build.py: the bundle must not carry or reference a fixture")
dist = ROOT / "dist"
dist.mkdir(exist_ok=True)
(dist / "index.html").write_text(html, encoding="utf-8")
print(f"Built {(dist / 'index.html').relative_to(ROOT.parent)} ({len(html.encode()):,} bytes)")
