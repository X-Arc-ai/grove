import subprocess
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import grove.writer.testing as testing
from grove import clock
from grove.store.connection import DATABASE
from grove.writer.testing import plant_link, plant_node, purge_test_world
from tests import conftest


def count(driver, cypher, **params):
    with driver.session(database=DATABASE) as session:
        return session.run(cypher, **params).single()["c"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "real-world",
        "t_short",
        "t_foundation_nothex",
        f"t_UPPER_{uuid4()}",
        f"x_foundation_{uuid4()}",
        f"t_foundation_{uuid4()}\n",
        f"t_foundation_{uuid4()}' OR '1",
    ],
)
def test_all_three_helpers_refuse_a_world_that_is_not_a_test_world(bad):
    with pytest.raises(ValueError):
        plant_node(object(), bad, "Item")
    with pytest.raises(ValueError):
        plant_link(object(), bad, "a", "b")
    with pytest.raises(ValueError):
        purge_test_world(object(), bad)


def test_plant_link_refuses_a_bad_to_world(driver, world):
    with pytest.raises(ValueError):
        plant_link(driver, world, "a", "b", to_world="real-world")


@pytest.mark.parametrize(
    "label", ["User", "Migration", "Nope", "Item) DETACH DELETE (n", "Item:User"]
)
def test_plant_node_takes_catalogue_world_labels_only(driver, world, label):
    with pytest.raises(ValueError):
        plant_node(driver, world, label)


def test_plant_node_gives_an_id_and_keeps_a_given_one(driver, world):
    assert plant_node(driver, world, "Item")["id"]
    assert plant_node(driver, world, "Request", id="chosen")["id"] == "chosen"


def test_plant_link_repeats_the_endpoints_and_stamps_the_world(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    b = plant_node(driver, world, "Item")["id"]
    row = plant_link(driver, world, a, b, meaning="m1")
    assert (row["from_item"], row["to_item"], row["world_id"]) == (a, b, world)
    assert row["meaning"] == "m1"


def test_plant_node_and_link_take_their_ids_from_the_worlds_own_time(driver, world):
    at = datetime(2030, 1, 2, tzinfo=UTC)
    per_world = clock.TestClock()
    per_world.set(at, world)  # no store-wide default: a call with no world id raises
    with clock.using(per_world):
        a = plant_node(driver, world, "Item")["id"]
        b = plant_node(driver, world, "Item")["id"]
        primitive = plant_node(driver, world, "Meaning", primitive=True, name=f"p {world}")["id"]
        link = plant_link(driver, world, a, b)["id"]
    for planted in (a, b, primitive, link):
        assert UUID(planted).int >> 80 == int(at.timestamp() * 1000), planted


def test_plant_link_refuses_a_missing_endpoint(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    with pytest.raises(ValueError):
        plant_link(driver, world, a, "absent")


def test_purge_removes_every_row_in_batches_and_leaves_the_other_world(
    driver, make_world, monkeypatch
):
    monkeypatch.setattr(testing, "_PURGE_BATCH", 2)
    doomed, kept = make_world(), make_world()
    items = [plant_node(driver, doomed, "Item")["id"] for _ in range(5)]
    plant_link(driver, doomed, items[0], items[1])
    plant_node(driver, doomed, "World", next_sequence=0)
    plant_node(driver, doomed, "Meaning", primitive=True, name=f"p {doomed}")
    survivor = plant_node(driver, kept, "Item")["id"]

    purge_test_world(driver, doomed)

    assert count(driver, "MATCH (n {world_id: $w}) RETURN count(n) AS c", w=doomed) == 0
    assert count(driver, "MATCH (n {planted_for: $w}) RETURN count(n) AS c", w=doomed) == 0
    assert count(driver, "MATCH (n:World {id: $w}) RETURN count(n) AS c", w=doomed) == 0
    assert count(driver, "MATCH (n:Item {id: $i}) RETURN count(n) AS c", i=survivor) == 1


def test_make_world_gives_a_name_the_helpers_accept(world):
    assert testing._TEST_WORLD.fullmatch(world)


def test_slice_id_from_the_env(monkeypatch):
    monkeypatch.setenv("GROVE_SLICE_ID", "ci")
    assert conftest.slice_id() == "ci"


@pytest.mark.parametrize("bad", ["my_lane", "Upper", "a b", "lane/x", "lane\n"])
def test_slice_id_from_the_env_refuses_a_name_purge_would_refuse(monkeypatch, bad):
    monkeypatch.setenv("GROVE_SLICE_ID", bad)
    with pytest.raises(ValueError, match="GROVE_SLICE_ID"):
        conftest.slice_id()


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("feat/foundation-20261101", "foundation"),
        ("feat/views-a-20261102", "views-a"),
        ("main", "local"),
        ("HEAD", "local"),
    ],
)
def test_slice_id_from_the_branch(monkeypatch, branch, expected):
    monkeypatch.delenv("GROVE_SLICE_ID", raising=False)
    done = subprocess.CompletedProcess([], 0, stdout=f"{branch}\n", stderr="")
    monkeypatch.setattr(conftest.subprocess, "run", lambda *args, **kwargs: done)
    assert conftest.slice_id() == expected
