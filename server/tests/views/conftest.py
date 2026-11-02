"""A signed-in test client per world, and a small synthetic world with invented names.

Every row is planted through grove.writer.testing, so this folder holds no write Cypher.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.views.session import SESSION_COOKIE
from grove.writer.testing import plant_link, plant_node, plant_session

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

TASK_VOCABULARY = [
    {"status": "open", "role": "open"},
    {"status": "in progress", "role": "active"},
    {"status": "done", "role": "done"},
]


@pytest.fixture
def app(driver):
    return create_app(driver=driver)


@pytest.fixture
def client_for(driver, app):
    """client_for(world, **plant_session options) -> a TestClient that carries the cookie."""

    def make(world_id: str, **options) -> TestClient:
        token = plant_session(driver, world_id, **options)
        return TestClient(app, raise_server_exceptions=False, cookies={SESSION_COOKIE: token})

    return make


@pytest.fixture
def anonymous(app):
    return TestClient(app, raise_server_exceptions=False)


@dataclass
class SmallWorld:
    """Two kinds, two meanings, three items and two links of one world."""

    world_id: str
    task: dict
    person: dict
    depends_on: dict
    assigned_to: dict
    venue: dict
    catering: dict
    maya: dict
    depends: dict
    assigned: dict


def plant_small_world(driver, world_id: str) -> SmallWorld:
    task = plant_node(
        driver, world_id, "Kind", name="task", status_vocabulary=json.dumps(TASK_VOCABULARY)
    )
    person = plant_node(driver, world_id, "Kind", name="person", status_vocabulary="[]")
    depends_on = plant_node(driver, world_id, "Meaning", name="depends on", cardinality="many")
    assigned_to = plant_node(
        driver, world_id, "Meaning", name="assigned to", cardinality="one_per_source"
    )
    plant_node(driver, world_id, "PropertyName", kind=task["id"], name="due")
    plant_node(driver, world_id, "PropertyName", kind=task["id"], name="budget")
    venue = plant_node(
        driver,
        world_id,
        "Item",
        kind=task["id"],
        title="Book the venue",
        summary="Find a hall for forty people",
        status="in progress",
        status_since=T1,
        recorded=T0,
        became_true_earliest=T0,
        touched_at=T1,
        last_confirmed=T1,
        version=3,
        properties=json.dumps({"due": "2026-12-01", "budget": 400}),
    )
    catering = plant_node(
        driver,
        world_id,
        "Item",
        kind=task["id"],
        title="Order the catering",
        body="Three menus to compare",
        recorded=T1,
    )
    maya = plant_node(driver, world_id, "Item", kind=person["id"], title="Maya Chen", recorded=T0)
    depends = plant_keyed_link(
        driver,
        world_id,
        catering["id"],
        venue["id"],
        depends_on["id"],
        recorded=T1,
        became_true_earliest=T1,
    )
    assigned = plant_keyed_link(
        driver, world_id, venue["id"], maya["id"], assigned_to["id"], recorded=T0
    )
    return SmallWorld(
        world_id, task, person, depends_on, assigned_to, venue, catering, maya, depends, assigned
    )


def plant_keyed_link(driver, world_id, from_id, to_id, meaning_id, **props) -> dict:
    """An active link: active_key is world|from|meaning|to while the link is active (S7)."""
    key = "|".join((world_id, from_id, meaning_id, to_id))
    return plant_link(driver, world_id, from_id, to_id, meaning=meaning_id, active_key=key, **props)
