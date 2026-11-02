"""Build the local Grove simulation into one offline HTML file."""
from pathlib import Path
import base64
import json
import re

ROOT = Path(__file__).resolve().parent


def asset_url(raw, base):
    name = raw.strip(" \"'")
    if name.startswith("data:"):
        return raw
    path = ROOT / name.lstrip("/") if name.startswith("/") else base / name
    mime = {".woff2": "font/woff2", ".woff": "font/woff", ".svg": "image/svg+xml"}.get(path.suffix, "application/octet-stream")
    return "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode()


fixture = json.loads((ROOT / "fixture.json").read_text())
(ROOT / "fixture.js").write_text("window.GROVE_FIXTURE=" + json.dumps(fixture, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";\n")
screens = json.loads((ROOT / "app-screens.json").read_text())
(ROOT / "app-screens.js").write_text("/* Static illustrative screens from an earlier walkthrough. */\nwindow.GroveAppScreenCSS=" + json.dumps((ROOT / "app-screens.css").read_text()) + ";\nwindow.GroveAppScreens=" + json.dumps(screens) + ";\n")
html = (ROOT / "index.html").read_text()


def css(match):
    path = ROOT / match[1].removeprefix("./")
    text = path.read_text()
    text = re.sub(r"url\(([^)]+)\)", lambda m: "url(" + asset_url(m[1], path.parent) + ")", text)
    return "<style>\n" + text + "\n</style>"


html = re.sub(r'<link (?:href="([^"]+)" rel="stylesheet"|rel="stylesheet" href="([^"]+)")\s*/?>',
              lambda m: css([None, m[1] or m[2]]), html)
html = re.sub(r'<script src="([^"]+)"></script>',
              lambda m: "<script>\n" + (ROOT / m[1].removeprefix("./")).read_text().replace("</script", "<\\/script") + "\n</script>", html)
html = html.replace('href="./favicon.svg"', 'href="' + asset_url("favicon.svg", ROOT) + '"')
dist = ROOT / "dist"
dist.mkdir(exist_ok=True)
(dist / "index.html").write_text(html)
print(f"Built {dist / 'index.html'} ({len(html.encode()):,} bytes)")
