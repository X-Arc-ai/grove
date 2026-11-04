"""Static demo routing does not require authentication or touch a store."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api.app import _add_web_pages
from grove.api.errors import install_error_handlers


def test_demo_is_a_separate_static_page_and_refuses_mutations(tmp_path):
    (tmp_path / "index.html").write_text("ordinary login")
    (tmp_path / "demo-onboarding.html").write_text("invented demo")
    app = FastAPI(redirect_slashes=False)
    install_error_handlers(app)
    _add_web_pages(app, str(tmp_path))
    client = TestClient(app)

    response = client.get("/demo/onboarding?provider=claude&start=1")
    assert response.status_code == 200
    assert response.text == "invented demo"
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert "connect-src 'none'" in response.headers["content-security-policy"]
    assert "form-action 'none'" in response.headers["content-security-policy"]
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/demo/onboarding").status_code == 405
    assert client.get("/demo-onboarding.html").status_code == 404
    assert client.get("/demo/onboarding/").status_code == 404
    for route in ("/login", "/signup", "/signin", "/logout"):
        assert client.get(route).text == "ordinary login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_demo_does_not_fall_back_to_the_live_app_when_artifact_is_absent(tmp_path):
    (tmp_path / "index.html").write_text("ordinary login")
    app = FastAPI()
    install_error_handlers(app)
    _add_web_pages(app, str(tmp_path))
    response = TestClient(app).get("/demo/onboarding")
    assert response.status_code == 404
    assert "ordinary login" not in response.text
