"""Every view endpoint answers a typed 401 unless the cookie names a live session."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.store.errors import STORE_UNAVAILABLE
from grove.store.query import PlatformQuery
from grove.views.session import SESSION_COOKIE, token_hash
from grove.writer.testing import plant_node, session_token_hash

VIEWS_DOC = Path(__file__).resolve().parents[3] / "docs" / "views.md"
UNAUTHENTICATED = {
    "error": {
        "code": "unauthenticated",
        "message": "sign in to see your Grove",
        "details": {},
        "retryable": False,
    }
}

CALLS = [
    ("GET", "/v1/views/session", None),
    ("GET", "/v1/views/graph", None),
    ("GET", "/v1/views/graph?as_of=not-a-time", None),
    ("GET", "/v1/views/node/some-id", None),
    ("GET", "/v1/views/schema", None),
    ("GET", "/v1/views/health", None),
    ("POST", "/v1/views/positions", {"positions": [{"id": "some-id", "x": 1.0, "y": 2.0}]}),
]


def call(client, method, path, body):
    if method == "POST":
        return client.post(path, json=body)
    return client.get(path)


def assert_unauthenticated(response):
    assert response.status_code == 401
    assert response.json() == UNAUTHENTICATED
    assert response.headers["cache-control"] == "no-store"


def test_the_hash_is_the_lowercase_sha256_hex_of_the_cookie():
    assert token_hash("abc") == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert session_token_hash("abc") == token_hash("abc")


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_no_cookie_is_401(anonymous, method, path, body):
    assert_unauthenticated(call(anonymous, method, path, body))


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_an_unknown_token_is_401(app, world, client_for, method, path, body):
    client_for(world)  # a live session exists in the store, under another token
    stranger = TestClient(
        app, raise_server_exceptions=False, cookies={SESSION_COOKIE: "made-up-token"}
    )
    assert_unauthenticated(call(stranger, method, path, body))


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_an_expired_session_is_401(world, client_for, method, path, body):
    client = client_for(world, expires_at=clock.now() - timedelta(seconds=1))
    assert_unauthenticated(call(client, method, path, body))


def test_a_session_that_expires_at_this_moment_is_401(world, client_for):
    """A session is valid while expires_at is later than now: equal to now is over."""
    frozen = clock.TestClock()
    frozen.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with clock.using(frozen):
        at = frozen.now()
        assert_unauthenticated(client_for(world, expires_at=at).get("/v1/views/session"))
        later = client_for(world, expires_at=at + timedelta(milliseconds=1))
        assert later.get("/v1/views/session").status_code == 200
        frozen.advance(milliseconds=1)
        assert_unauthenticated(later.get("/v1/views/session"))


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_a_revoked_session_is_401(world, client_for, method, path, body):
    client = client_for(world, revoked_at=clock.now() - timedelta(minutes=5))
    assert_unauthenticated(call(client, method, path, body))


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_a_session_whose_user_owns_no_world_is_401(world, client_for, method, path, body):
    """A live, unrevoked session whose user owns no :World reads as signed out."""
    orphan = client_for(world)
    owner = client_for(world)  # a second user takes the world over: the first owns none now
    assert owner.get("/v1/views/session").status_code == 200
    assert_unauthenticated(call(orphan, method, path, body))


def test_the_contract_says_signin_makes_the_world_before_the_cookie():
    contract = VIEWS_DOC.read_text(encoding="utf-8")
    assert "Signin must create the user's `:World` row before it sets the cookie." in contract
    assert "A valid session whose user owns no `:World` reads as signed out" in contract
    assert "or a session whose user owns no `:World`. |" in contract


def test_the_raw_token_is_not_accepted_as_its_own_hash(app, driver, world, client_for):
    """A cookie that holds the stored hash is a different token, so it fails."""
    live = client_for(world)
    stored_hash = token_hash(live.cookies[SESSION_COOKIE])
    replay = TestClient(app, raise_server_exceptions=False, cookies={SESSION_COOKIE: stored_hash})
    assert_unauthenticated(replay.get("/v1/views/graph"))
    assert live.get("/v1/views/graph").status_code == 200


def test_an_oversized_cookie_is_401(app):
    client = TestClient(app, raise_server_exceptions=False, cookies={SESSION_COOKIE: "x" * 600})
    assert_unauthenticated(client.get("/v1/views/graph"))


def test_a_live_session_passes_and_a_revoked_twin_does_not(driver, world, client_for):
    plant_node(driver, world, "Item", title="Spare key")
    live = client_for(world)
    user_id = first_user(driver, live.cookies[SESSION_COOKIE])
    revoked = client_for(world, user_id=user_id, revoked_at=clock.now())
    assert live.get("/v1/views/graph").json()["stats"]["total_nodes"] == 1
    assert_unauthenticated(revoked.get("/v1/views/graph"))


def test_a_user_with_two_worlds_reads_the_oldest(driver, make_world, client_for):
    older, newer = make_world(), make_world()
    plant_node(driver, older, "Item", title="In the older world")
    plant_node(driver, newer, "Item", title="In the newer world")
    first = client_for(older)
    user_id = first_user(driver, first.cookies[SESSION_COOKIE])
    second = client_for(newer, user_id=user_id)
    for client in (first, second):
        labels = [
            n["data"]["label"] for n in client.get("/v1/views/graph").json()["elements"]["nodes"]
        ]
        assert labels == ["In the older world"]


def first_user(driver, token):
    rows = PlatformQuery(driver).read(
        "MATCH (s:Session {token_hash: $h}) RETURN s.user_id AS user_id", h=token_hash(token)
    )
    return rows[0]["user_id"]


class UnreachableDriver:
    """A driver whose store is down: every session fails as the real driver does."""

    def session(self, **options):
        raise STORE_UNAVAILABLE[0]("the store is down")


def test_an_unreachable_store_is_a_typed_503():
    client = TestClient(
        create_app(driver=UnreachableDriver()),
        raise_server_exceptions=False,
        cookies={SESSION_COOKIE: "any-token"},
    )
    response = client.get("/v1/views/graph")
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "unavailable",
            "message": "the store cannot be reached",
            "details": {},
            "retryable": True,
        }
    }
