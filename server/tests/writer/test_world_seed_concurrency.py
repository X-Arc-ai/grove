"""Seed decisions must use the state read after the mandatory World lock."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from grove.writer import worlds
from grove.writer.testing import plant_node
from grove.writer.tx import WorldTx
from tests.writer.helpers import world_dump
from tests.writer.test_read_generation import world_properties

OWNER = "u_t_original_owner"


@pytest.mark.parametrize("initial_state", ["unseeded", "upgrade"])
def test_waiting_creator_uses_the_committed_seed_state(driver, world, monkeypatch, initial_state):
    if initial_state == "unseeded":
        plant_node(driver, world, "World", owner_user_id=OWNER, next_sequence=0)
    else:
        # Produce the supported old starter set, before property names were introduced.
        starter = worlds.load_starter_set()
        legacy = {**starter, "seed_version": "1", "property_names": []}
        with monkeypatch.context() as setup:
            setup.setattr(worlds, "load_starter_set", lambda: legacy)
            worlds.create_world(driver, world, owner_user_id=OWNER)
    before = world_properties(driver, world)
    paused, release = threading.Event(), threading.Event()
    original_lock = WorldTx.lock_world
    first_thread = None

    def hold_before_lock(tx):
        if threading.get_ident() == first_thread:
            paused.set()
            assert release.wait(10)
        original_lock(tx)

    monkeypatch.setattr(WorldTx, "lock_world", hold_before_lock)

    def delayed_creator():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return worlds.create_world(driver, world, owner_user_id="u_t_delayed_caller")

    with ThreadPoolExecutor(max_workers=2) as pool:
        delayed = pool.submit(delayed_creator)
        assert paused.wait(10)
        try:
            winner = pool.submit(
                worlds.create_world, driver, world, owner_user_id="u_t_winning_caller"
            ).result(timeout=10)
            committed = world_dump(driver, world)
        finally:
            release.set()
        resumed = delayed.result(timeout=10)

    assert resumed == winner
    assert resumed["owner_user_id"] == OWNER
    assert resumed["seed_version"] == worlds.load_starter_set()["seed_version"]
    assert resumed["property_names"]
    # A completed seed or upgrade makes the delayed caller a complete-state no-op.
    assert world_dump(driver, world) == committed
    assert world_properties(driver, world)["read_generation"] == before["read_generation"] + 1
