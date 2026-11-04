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
