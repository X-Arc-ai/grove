"""Kinds and meanings show by name: the world's rows, the primitive rows and the starter set."""

import json
from uuid import uuid4

from grove.ids import new_id
from grove.store.primitives import KIND_BELIEF, MEANING_DESCRIBES
from grove.writer.testing import plant_node, seed_test_world
from tests.views.conftest import plant_keyed_link


def nodes_by_label(payload):
    return {w["data"]["label"]: w["data"] for w in payload["elements"]["nodes"]}


def test_world_and_primitive_kinds_and_meanings_resolve(driver, world, client_for):
    word = f"keepsake {uuid4().hex[:8]}"
    kind = plant_node(driver, world, "Kind", name="plan", status_vocabulary="[]")
    primitive_kind = plant_node(
        driver,
        world,
        "Kind",
        primitive=True,
        name=word,
        status_vocabulary=json.dumps([{"status": "kept", "role": "active"}]),
    )
    meaning = plant_node(driver, world, "Meaning", name="part of", cardinality="many")
    primitive_meaning = plant_node(
        driver, world, "Meaning", primitive=True, name=f"echoes {word}", cardinality="many"
    )
    trip = plant_node(driver, world, "Item", title="Lisbon trip", kind=kind["id"])
    charm = plant_node(driver, world, "Item", title="Brass charm", kind=primitive_kind["id"])
    stray = plant_node(driver, world, "Item", title="No kind row", kind=new_id())
    plant_keyed_link(driver, world, charm["id"], trip["id"], meaning["id"])
    plant_keyed_link(driver, world, trip["id"], charm["id"], primitive_meaning["id"])
    plant_keyed_link(driver, world, stray["id"], trip["id"], new_id())
    client = client_for(world)

    graph = client.get("/v1/views/graph").json()
    nodes = nodes_by_label(graph)
    assert nodes["Lisbon trip"]["type"] == "plan"
    assert nodes["Brass charm"]["type"] == word
    assert nodes["No kind row"]["type"] == "unknown"
    verbs = sorted(w["data"]["verb"] for w in graph["elements"]["edges"])
    assert verbs == sorted(["part of", f"echoes {word}", "unknown"])
    assert all(w["data"]["label"] == w["data"]["verb"] for w in graph["elements"]["edges"])
    assert graph["stats"]["by_type"] == {"plan": 1, word: 1, "unknown": 1}
    assert graph["stats"]["edge_verbs"] == sorted(verbs)
    assert client.get("/v1/views/health").json()["by_type"] == {"plan": 1, word: 1, "unknown": 1}

    schema = client.get("/v1/views/schema").json()
    assert schema["plan"]["status_vocab"] == []
    assert schema[word]["status_vocab"] == ["kept"]
    # Names are shown; registry ids never are.
    text = json.dumps(graph) + json.dumps(schema)
    for registry_id in (kind["id"], primitive_kind["id"], meaning["id"], primitive_meaning["id"]):
        assert registry_id not in text


def test_a_seeded_world_shows_the_starter_names(driver, world, client_for):
    seeded = seed_test_world(driver, world, owner_user_id=new_id())
    kinds, meanings = seeded["kinds"], seeded["meanings"]
    maya = plant_node(driver, world, "Item", title="Maya Chen", kind=kinds["person"])
    walk = plant_node(
        driver, world, "Item", title="Walk the dog", kind=kinds["task"], status="in progress"
    )
    belief = plant_node(driver, world, "Item", title="Maya likes mornings", kind=KIND_BELIEF)
    plant_keyed_link(driver, world, walk["id"], maya["id"], meanings["assigned to"])
    plant_keyed_link(driver, world, belief["id"], maya["id"], MEANING_DESCRIBES)
    client = client_for(world)

    graph = client.get("/v1/views/graph").json()
    nodes = nodes_by_label(graph)
    assert nodes["Maya Chen"]["type"] == "person"
    assert nodes["Walk the dog"]["type"] == "task"
    assert nodes["Maya likes mornings"]["type"] == "belief"
    edges = {w["data"]["verb"]: w["data"] for w in graph["elements"]["edges"]}
    assert set(edges) == {"assigned to", "describes"}
    assert edges["assigned to"]["cardinality"] == "one_per_source"
    assert edges["describes"]["cardinality"] == "many"

    schema = client.get("/v1/views/schema").json()
    starter = {
        "person",
        "organisation",
        "place",
        "plan",
        "task",
        "problem",
        "assumption",
        "event",
        "observation",
        "decision",
        "trip",
        "period",
    }
    assert starter | {"belief"} <= set(schema)
    assert schema["task"]["status_vocab"] == ["open", "in progress", "blocked", "done", "cancelled"]
    assert schema["organisation"]["status_vocab"] == ["active", "closed"]
    assert schema["belief"]["status_vocab"] == []
