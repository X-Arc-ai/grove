"""The app shell: the local web page, typed errors for unknown routes, and the factory."""

from fastapi.testclient import TestClient

from grove.api.app import WEB_DIST_ENV, create_app


def test_get_root_serves_the_built_page_when_the_folder_is_set(driver, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><title>Grove</title>", encoding="utf-8")
    monkeypatch.setenv(WEB_DIST_ENV, str(tmp_path))
    response = TestClient(create_app(driver=driver)).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == "<!doctype html><title>Grove</title>"


def test_get_root_is_a_typed_404_without_the_folder(driver, monkeypatch):
    monkeypatch.delenv(WEB_DIST_ENV, raising=False)
    response = TestClient(create_app(driver=driver)).get("/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_folder_with_no_page_is_a_typed_404(driver, tmp_path, monkeypatch):
    monkeypatch.setenv(WEB_DIST_ENV, str(tmp_path))
    response = TestClient(create_app(driver=driver)).get("/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_unknown_route_and_a_wrong_method_are_typed(driver):
    client = TestClient(create_app(driver=driver))
    missing = client.get("/v1/views/nothing-here")
    assert missing.status_code == 404
    assert set(missing.json()["error"]) == {"code", "message", "details", "retryable"}
    wrong = client.delete("/v1/views/graph")
    assert wrong.status_code == 405
    assert set(wrong.json()["error"]) == {"code", "message", "details", "retryable"}


def test_no_docs_pages_are_served(driver):
    client = TestClient(create_app(driver=driver))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_factory_opens_and_closes_its_own_driver(monkeypatch):
    """With no driver handed in, startup opens one from the environment and shutdown closes it."""
    import grove.api.app as app_module

    opened = []

    class Recorded:
        closed = False

        def close(self):
            self.closed = True

    def fake_open():
        opened.append(Recorded())
        return opened[-1]

    monkeypatch.setattr(app_module, "open_driver", fake_open)
    app = app_module.create_app()
    with TestClient(app):
        assert app.state.driver is opened[0]
    assert opened[0].closed is True
