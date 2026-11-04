"""Historical demo links must retain the live onboarding and authentication flow."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api.app import _add_web_pages
from grove.api.errors import install_error_handlers


def test_old_demo_link_returns_to_complete_onboarding_and_refuses_mutations(tmp_path):
    (tmp_path / "index.html").write_text("ordinary login")
    (tmp_path / "demo-onboarding.html").write_text("invented demo")
    app = FastAPI(redirect_slashes=False)
    install_error_handlers(app)
    _add_web_pages(app, str(tmp_path))
    client = TestClient(app)

    response = client.get("/demo/onboarding?provider=claude&start=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/demo/onboarding").status_code == 405
    assert client.get("/demo-onboarding.html").status_code == 404
    assert client.get("/demo/onboarding/").status_code == 404
    for route in ("/login", "/signup", "/signin", "/logout"):
        assert client.get(route).text == "ordinary login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_old_demo_link_never_needs_a_separate_simulation_artifact(tmp_path):
    (tmp_path / "index.html").write_text("ordinary login")
    app = FastAPI()
    install_error_handlers(app)
    _add_web_pages(app, str(tmp_path))
    response = TestClient(app).get("/demo/onboarding")
    assert response.status_code == 200
    assert response.text == "ordinary login"


def test_claude_preview_assets_fit_and_pass_the_real_public_asset_route():
    """A successful static build must not ship a preview that the API returns as 404."""
    import re
    import subprocess
    import sys
    from pathlib import Path

    from grove.api.assets import add_public_assets

    web = Path(__file__).resolve().parents[3] / "web"
    subprocess.run([sys.executable, str(web / "build.py")], check=True, capture_output=True)
    page = (web / "dist/index.html").read_text()
    urls = re.findall(r"GroveDeferred\.register\('claude-preview(?:-edges)?',\"([^\"]+)\"\)", page)
    assert urls, "The authenticated onboarding must register its Claude preview assets"
    app = FastAPI()
    add_public_assets(app, web / "dist")
    client = TestClient(app)
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, "The Claude preview must pass the real asset size limit"
        assert response.content == (web / "dist/assets" / url.rsplit("/", 1)[1]).read_bytes()
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
