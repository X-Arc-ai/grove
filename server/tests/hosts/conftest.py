"""The hosts tests against the real store: signed-in people in fresh t_ worlds, and OAuth clients.

Every name and title is invented. HostsPlant remembers what it made, and its purge removes the
worlds first (with their users, sessions, grants, codes and tokens) and then the clients, which no
grant names after that. A test that registers a client through the API hands its id to
track_client.

A module-scoped run builds its own HostsPlant(driver) and calls purge() at its end; the fixtures
below are per test.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from grove import clock
from grove.ids import new_id
from grove.views.session import SESSION_COOKIE
from grove.writer.oauth import DYNAMIC, delete_unused_client, register_client
from grove.writer.testing import plant_node, plant_session, purge_test_world, seed_test_world
from tests.conftest import slice_id

PERSON_TITLE = "Ines Duarte"
SECOND_TITLE = "Tomas Okafor"
LOOPBACK_REDIRECT = "http://127.0.0.1:43117/callback"
CLIENT_NAME = "Lumen Desk"
_OWN_KIND = "person"


def new_test_world_id() -> str:
    """A t_<slice>_<uuid4> id, as make_world gives, for a run outside one test's scope."""
    return f"t_{slice_id()}_{uuid4()}"


@dataclass(frozen=True)
class Person:
    """A user that owns one world, with a live session and an own item that has `title`."""

    user_id: str
    world_id: str
    token: str
    self_item_id: str
    title: str

    @property
    def cookies(self) -> dict[str, str]:
        """The cookie the views and /authorize read: grove_session holds the session token."""
        return {SESSION_COOKIE: self.token}


@dataclass
class HostsPlant:
    driver: object
    world_id_for: Callable[[], str] = new_test_world_id
    worlds: list[str] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)

    def world(self) -> str:
        """A fresh t_ world id that the purge removes. No :World row exists for it yet."""
        world_id = self.world_id_for()
        if world_id not in self.worlds:
            self.worlds.append(world_id)
        return world_id

    def person(self, title: str = PERSON_TITLE, *, seeded: bool = True) -> Person:
        """A person in a world of their own. seeded gives the world its starter set, as a real
        first sign-in does (about a second); unseeded is the :World row alone."""
        world_id = self.world()
        user_id = new_id()
        item: dict = {"title": title}
        if seeded:
            ids = seed_test_world(self.driver, world_id, owner_user_id=user_id)
            item["kind"] = ids["kinds"][_OWN_KIND]
        self_item_id = plant_node(self.driver, world_id, "Item", **item)["id"]
        token = plant_session(self.driver, world_id, user_id=user_id, self_item_id=self_item_id)
        return Person(user_id, world_id, token, self_item_id, title)

    def track_client(self, client_id: str) -> str:
        """A client the purge deletes once its worlds are gone."""
        if client_id not in self.clients:
            self.clients.append(client_id)
        return client_id

    def client(
        self,
        *,
        host: str | None = CLIENT_NAME,
        redirect_uris: list[str] | None = None,
        auth_method: str = "none",
        secret_hash: str | None = None,
    ) -> str:
        """A dynamic client registered now through the writer; returns its UUIDv7 client id."""
        client_id = self.track_client(new_id())
        register_client(
            self.driver,
            client_id=client_id,
            host=host,
            redirect_uris=redirect_uris or [LOOPBACK_REDIRECT],
            registration_type=DYNAMIC,
            auth_method=auth_method,
            secret_hash=secret_hash,
            at=clock.now(),
        )
        return client_id

    def purge(self) -> None:
        """The clients go even when a world's purge fails, so that a store error under load leaves
        no client row behind. The failure still reaches the test run."""
        try:
            for world_id in self.worlds:
                purge_test_world(self.driver, world_id)
        finally:
            for client_id in self.clients:
                delete_unused_client(self.driver, client_id=client_id)


@pytest.fixture
def hosts_plant(driver, make_world):
    """Per test. The worlds come from make_world, whose own purge after this one finds nothing."""
    plant = HostsPlant(driver, world_id_for=make_world)
    yield plant
    plant.purge()


@pytest.fixture
def person(hosts_plant) -> Person:
    """A signed-in person in a fresh, seeded t_ world."""
    return hosts_plant.person()


@pytest.fixture
def plant_person(hosts_plant) -> Callable[..., Person]:
    """Plants another person in another world: plant_person(SECOND_TITLE)."""
    return hosts_plant.person
