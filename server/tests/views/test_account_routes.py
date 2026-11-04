"""Account entry pages never authenticate, revoke, or read the store."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api.app import _add_web_pages


@pytest.mark.parametrize("path", ["/login", "/signup", "/signin", "/logout"])
@pytest.mark.parametrize("cookie", [None, "synthetic-session"])
def test_account_entry_pages_are_public_no_store_gets(tmp_path, path, cookie):
    (tmp_path / "index.html").write_text("<title>synthetic account page</title>")
    app = FastAPI()

    class NoStore:
        def __getattr__(self, name):
            raise AssertionError("An account page GET must not access the store")

    app.state.driver = NoStore()
    _add_web_pages(app, str(tmp_path))
    client = TestClient(app)
    if cookie:
        client.cookies.set("grove_session", cookie)
    response = client.get(path + "?next=%2Fauthorize%3Fclient_id%3Dsynthetic")
    assert response.status_code == 200
    assert response.text == "<title>synthetic account page</title>"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert response.url.path == path
    assert client.cookies.get("grove_session") == cookie


@pytest.mark.parametrize("path", ["/login", "/signup", "/logout"])
def test_missing_account_bundle_never_returns_success(tmp_path, path):
    from grove.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    _add_web_pages(app, str(tmp_path))
    response = TestClient(app).get(path)
    assert response.status_code == 404
