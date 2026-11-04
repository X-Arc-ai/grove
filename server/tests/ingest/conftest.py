"""Fixtures of the ingest tests: uploads, screeners, a signed-in client and store counts.

Every row is planted through grove.writer.testing or written by the code under test, so this
folder holds no write Cypher (the writer lint). Data is synthetic, with invented names only (R3).
"""

import io
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.clock import TestClock
from grove.ids import new_id
from grove.ingest import Upload
from grove.screening import Denylist, RenameMap, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE
from grove.writer.testing import plant_session, seed_test_world

STORE_TIME = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def upload_of(data: bytes) -> Upload:
    return Upload(io.BytesIO(data), len(data))


def make_screener(
    *, rename: dict[str, str] | None = None, denylist: list[str] | None = None
) -> Screener:
    return Screener(
        ScreeningConfig(
            rename=RenameMap.from_mapping(rename or {}), denylist=Denylist(denylist or [])
        )
    )


def counts(driver, world_id: str) -> dict[str, int]:
    """How many rows of each ingest table the world holds."""
    query = WorldQuery(driver, world_id)
    out = {}
    for name, label in (
        ("connectors", "Connector"),
        ("sources", "Source"),
        ("episodes", "Episode"),
        ("receipts", "ScreeningReceipt"),
    ):
        out[name] = query.read(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
    return out


@pytest.fixture
def test_clock():
    installed = TestClock()
    installed.set(STORE_TIME)
    with clock.using(installed):
        yield installed


@pytest.fixture
def seeded_world(driver, make_world):
    """A world with its :World row and starter set, as a first sign-in leaves it."""
    world_id = make_world()
    seed_test_world(driver, world_id, owner_user_id=new_id())
    return world_id


@pytest.fixture
def app(driver):
    return create_app(driver=driver)


@pytest.fixture
def client_for(driver, app):
    """client_for(world) -> a TestClient that carries a session cookie of that world's owner."""

    def make(world_id: str, **options) -> TestClient:
        token = plant_session(driver, world_id, **options)
        return TestClient(app, raise_server_exceptions=False, cookies={SESSION_COOKIE: token})

    return make


@pytest.fixture
def anonymous(app):
    return TestClient(app, raise_server_exceptions=False)
