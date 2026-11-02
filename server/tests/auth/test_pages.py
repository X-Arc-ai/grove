"""The page routes and the session answer. The pages here are small files of this test's own."""

import pytest

from tests.auth.conftest import SESSION_VIEW, browser, email_sign_in

PAGES = {
    "home.html": "<!doctype html><title>home page</title>",
    "index.html": "<!doctype html><title>app page</title>",
    "privacy.html": "<!doctype html><title>privacy page</title>",
}


@pytest.fixture
def dist(tmp_path, monkeypatch):
    folder = tmp_path / "dist"
    folder.mkdir()
    for name, text in PAGES.items():
        (folder / name).write_text(text)
    monkeypatch.setenv("GROVE_WEB_DIST", str(folder))
    return folder


@pytest.fixture
def pages(dist, make_app):
    return browser(make_app())


def test_root_with_no_cookie_is_the_home_page(pages):
    response = pages.get("/")
    assert response.status_code == 200
    assert response.text == PAGES["home.html"]
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Cookie"


def test_root_with_a_cookie_is_the_app_page_and_reads_no_store(dist, make_app):
    class NoStore:
        def session(self, **options):
            raise AssertionError("the page route read the store")

    app = make_app()
    app.state.driver = NoStore()
    client = browser(app)
    client.cookies.set("grove_session", "any-value", domain="grove.example", path="/")
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == PAGES["index.html"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Cookie"


def test_signin_is_the_app_page(pages):
    response = pages.get("/signin")
    assert response.status_code == 200
    assert response.text == PAGES["index.html"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/html")


def test_signin_keeps_its_query_for_the_page(pages):
    assert pages.get("/signin?signin_error=google").text == PAGES["index.html"]


def test_privacy_is_the_privacy_page(pages):
    response = pages.get("/privacy")
    assert response.status_code == 200
    assert response.text == PAGES["privacy.html"]
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    ("path", "name"),
    [("/", "home.html"), ("/signin", "index.html"), ("/privacy", "privacy.html")],
)
def test_a_missing_file_is_the_typed_404(path, name, dist, pages):
    (dist / name).unlink()
    response = pages.get(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_no_web_dist_means_no_page_routes(make_app, monkeypatch):
    monkeypatch.delenv("GROVE_WEB_DIST", raising=False)
    client = browser(make_app())
    for path in ("/", "/signin", "/privacy"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_the_session_answer_has_its_keys(client, accounts, mailer):
    verified = email_sign_in(client, mailer, accounts.address())
    asked = client.get(SESSION_VIEW)
    assert asked.status_code == 200
    assert asked.json() == verified.json()
    assert set(asked.json()) == {"signed_in", "self_item_id", "has_items", "account", "method"}
    assert asked.json()["signed_in"] is True
    assert asked.headers["cache-control"] == "no-store"
