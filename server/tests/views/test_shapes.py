"""The payloads match the HTTP contract of docs/views.md key for key and type for type."""

import re

from grove.store.current import STATES
from grove.store.primitives import MEANING_CORRECTED_BY
from grove.writer.testing import plant_node, set_self_item
from tests.views.conftest import T2, plant_keyed_link, plant_small_world

ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

GRAPH_KEYS = {
    "elements",
    "stats",
    "time",
    "generated_at",
    "as_of",
    "known_as_of",
    "self_item_id",
    "selection",
}
NODE_KEYS = {
    "id",
    "label",
    "type",
    "summary",
    "status",
    "file_path",
    "valid_at",
    "invalid_at",
    "created_at",
    "updated_at",
    "status_since",
    "verified_at",
    "version",
    "properties",
    "has_changes",
    "standing",
    "successors",
}
# The node detail only: the clocks in words (D4), docs/views.md "C".
CLOCK_KEYS = {"last confirmed", "re-check by", "became true", "recorded", "source type", "state"}
EDGE_KEYS = {
    "id",
    "source",
    "target",
    "label",
    "verb",
    "cardinality",
    "canonical",
    "canonical_of",
    "valid_at",
    "invalid_at",
    "created_at",
    "note",
    "source_field",
}
STATS_KEYS = {"total_nodes", "total_edges", "by_type", "embedded", "edge_verbs"}
DETAIL_KEYS = {"node", "required_props", "optional_props", "required_prop_names", "status_field"}
SUCCESSOR_KEYS = {"id", "link_id", "meaning"}
HEALTH_KEYS = {
    "status",
    "summary",
    "generated_at",
    "checks",
    "nodes",
    "edges",
    "by_type",
    "overall",
}


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nodes_by_id(payload):
    return {wrapper["data"]["id"]: wrapper["data"] for wrapper in payload["elements"]["nodes"]}


def edges_by_id(payload):
    return {wrapper["data"]["id"]: wrapper["data"] for wrapper in payload["elements"]["edges"]}


def assert_node_types(node):
    for key in ("id", "label", "type", "summary", "status", "file_path"):
        assert isinstance(node[key], str), key
    for key in ("valid_at", "invalid_at", "created_at", "updated_at", "status_since"):
        assert node[key] is None or ISO_Z.fullmatch(node[key]), key
    assert node["verified_at"] is None or ISO_Z.fullmatch(node["verified_at"])
    assert type(node["version"]) is int
    assert isinstance(node["properties"], dict)
    assert type(node["has_changes"]) is bool
    assert node["standing"] in STATES
    assert isinstance(node["successors"], list)
    for successor in node["successors"]:
        assert set(successor) == SUCCESSOR_KEYS
        assert all(isinstance(value, str) for value in successor.values())


def test_graph_keys_types_and_values(driver, world, client_for):
    small = plant_small_world(driver, world)
    response = client_for(world).get("/v1/views/graph")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()

    assert set(payload) == GRAPH_KEYS
    assert set(payload["elements"]) == {"nodes", "edges"}
    assert all(set(wrapper) == {"data"} for wrapper in payload["elements"]["nodes"])
    assert all(set(wrapper) == {"data"} for wrapper in payload["elements"]["edges"])
    nodes, edges = nodes_by_id(payload), edges_by_id(payload)
    for node in nodes.values():
        assert set(node) == NODE_KEYS
        assert_node_types(node)

    venue = nodes[small.venue["id"]]
    assert venue == {
        "id": small.venue["id"],
        "label": "Book the venue",
        "type": "task",
        "summary": "Find a hall for forty people",
        "status": "in progress",
        "file_path": "",
        "valid_at": iso(small.venue["became_true_earliest"]),
        "invalid_at": None,
        "created_at": iso(small.venue["recorded"]),
        "updated_at": iso(small.venue["touched_at"]),
        "status_since": iso(small.venue["status_since"]),
        "verified_at": iso(small.venue["last_confirmed"]),
        "version": 3,
        "properties": {"due": "2026-12-01", "budget": 400},
        "has_changes": True,
        "standing": "current",
        "successors": [],
    }
    catering = nodes[small.catering["id"]]
    # No world clock: valid_at falls back to recorded; no touched_at: updated_at is recorded.
    assert catering["valid_at"] == catering["created_at"] == iso(small.catering["recorded"])
    assert catering["updated_at"] == catering["created_at"]
    assert (catering["summary"], catering["status"]) == ("", "")
    assert (catering["version"], catering["has_changes"], catering["properties"]) == (1, False, {})

    for edge in edges.values():
        assert set(edge) == EDGE_KEYS
    assert edges[small.depends["id"]] == {
        "id": small.depends["id"],
        "source": small.catering["id"],
        "target": small.venue["id"],
        "label": "depends on",
        "verb": "depends on",
        "cardinality": "many",
        "canonical": False,
        "canonical_of": None,
        "valid_at": iso(small.depends["became_true_earliest"]),
        "invalid_at": None,
        "created_at": iso(small.depends["recorded"]),
        "note": "",
        "source_field": "store",
    }
    assert edges[small.assigned["id"]]["cardinality"] == "one_per_source"
    assert edges[small.assigned["id"]]["valid_at"] == iso(small.assigned["recorded"])

    assert set(payload["stats"]) == STATS_KEYS
    assert payload["stats"] == {
        "total_nodes": 3,
        "total_edges": 2,
        "by_type": {"task": 2, "person": 1},
        "embedded": 3,
        "edge_verbs": ["assigned to", "depends on"],
    }
    assert set(payload["time"]) == {"min", "max"}
    assert payload["time"] == {"min": "2026-09-01", "max": "2026-09-15"}
    assert ISO_Z.fullmatch(payload["generated_at"])
    assert (payload["as_of"], payload["known_as_of"], payload["self_item_id"]) == (None, None, None)


def test_an_empty_world_has_the_same_keys(world, client_for):
    payload = client_for(world).get("/v1/views/graph").json()
    assert set(payload) == GRAPH_KEYS
    assert payload["elements"] == {"nodes": [], "edges": []}
    assert payload["stats"] == {
        "total_nodes": 0,
        "total_edges": 0,
        "by_type": {},
        "embedded": 0,
        "edge_verbs": [],
    }
    assert payload["time"] == {"min": None, "max": None}


def test_node_detail_keys_and_values(driver, world, client_for):
    small = plant_small_world(driver, world)
    client = client_for(world)
    response = client.get(f"/v1/views/node/{small.venue['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert set(detail) == DETAIL_KEYS
    assert set(detail["node"]) == NODE_KEYS | {"title", "content", "status_field_value", "clocks"}
    assert_node_types(detail["node"])
    assert set(detail["node"]["clocks"]) == CLOCK_KEYS
    assert all(isinstance(line, str) for line in detail["node"]["clocks"].values())
    assert detail["node"]["title"] == detail["node"]["label"] == "Book the venue"
    assert detail["node"]["content"] == "Find a hall for forty people"
    assert detail["node"]["status_field_value"] == "in progress"
    assert detail["required_props"] == [] and detail["required_prop_names"] == []
    assert detail["optional_props"] == ["budget", "due"]
    assert detail["status_field"] == "status"

    # With no summary the content is the body; a person kind has no property names.
    catering = client.get(f"/v1/views/node/{small.catering['id']}").json()
    assert catering["node"]["content"] == "Three menus to compare"
    assert catering["node"]["status_field_value"] == ""
    assert client.get(f"/v1/views/node/{small.maya['id']}").json()["optional_props"] == []


def plant_correction(driver, small) -> tuple[dict, dict]:
    """A newer item and an active "corrected by" link from the venue to it, as the writer's
    correct operation (writer-b, Part A) writes one. Returns (successor, link)."""
    successor = plant_node(
        driver,
        small.world_id,
        "Item",
        kind=small.task["id"],
        title="Book the Okonkwo hall",
        recorded=T2,
        became_true_earliest=T2,
    )
    link = plant_keyed_link(
        driver,
        small.world_id,
        small.venue["id"],
        successor["id"],
        MEANING_CORRECTED_BY,
        recorded=T2,
        became_true_earliest=T2,
        became_true_latest=T2,
        became_true_precision="instant",
    )
    return successor, link


def test_successor_on_views_graph(driver, world, client_for):
    small = plant_small_world(driver, world)
    successor, link = plant_correction(driver, small)
    nodes = nodes_by_id(client_for(world).get("/v1/views/graph").json())

    venue = nodes[small.venue["id"]]
    assert venue["standing"] == "overtaken"
    assert venue["successors"] == [
        {"id": successor["id"], "link_id": link["id"], "meaning": "corrected by"}
    ]
    # The successor itself, and an item with no successor, carry none.
    for other in (successor, small.catering, small.maya):
        assert (nodes[other["id"]]["standing"], nodes[other["id"]]["successors"]) == (
            "current",
            [],
        )
    for node in nodes.values():
        assert set(node) == NODE_KEYS
        assert_node_types(node)


def test_successor_on_views_node(driver, world, client_for):
    small = plant_small_world(driver, world)
    successor, link = plant_correction(driver, small)
    client = client_for(world)

    venue = client.get(f"/v1/views/node/{small.venue['id']}").json()["node"]
    assert venue["standing"] == "overtaken"
    assert venue["successors"] == [
        {"id": successor["id"], "link_id": link["id"], "meaning": "corrected by"}
    ]
    assert_node_types(venue)
    catering = client.get(f"/v1/views/node/{small.catering['id']}").json()["node"]
    assert (catering["standing"], catering["successors"]) == ("current", [])


def test_an_unknown_node_is_a_typed_404(world, client_for):
    response = client_for(world).get("/v1/views/node/no-such-item")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "no such item",
            "details": {},
            "retryable": False,
        }
    }


def test_schema_keys_and_values(driver, world, client_for):
    plant_small_world(driver, world)
    schema = client_for(world).get("/v1/views/schema").json()
    for entry in schema.values():
        assert set(entry) == {"required", "optional", "status_field", "status_vocab"}
        assert entry["required"] == [] and entry["status_field"] == "status"
        assert all(isinstance(word, str) for word in entry["status_vocab"])
    assert schema["task"] == {
        "required": [],
        "optional": ["budget", "due"],
        "status_field": "status",
        "status_vocab": ["open", "in progress", "done"],
    }
    assert schema["person"]["optional"] == [] and schema["person"]["status_vocab"] == []


def test_health_keys_and_values(driver, make_world, client_for):
    empty, full = make_world(), make_world()
    plant_small_world(driver, full)

    payload = client_for(full).get("/v1/views/health").json()
    assert set(payload) == HEALTH_KEYS
    assert payload["status"] == "healthy"
    assert payload["summary"] == "Your Grove is ready"
    assert ISO_Z.fullmatch(payload["generated_at"])
    assert payload["checks"] == {"graph": "ready", "sources": "ready"}
    assert (payload["nodes"], payload["edges"]) == (3, 2)
    assert payload["by_type"] == {"task": 2, "person": 1}
    assert payload["overall"] == {"state": "healthy"}

    nothing = client_for(empty).get("/v1/views/health").json()
    assert set(nothing) == HEALTH_KEYS
    assert nothing["summary"] == "Your Grove is empty"
    assert (nothing["nodes"], nothing["edges"], nothing["by_type"]) == (0, 0, {})


def test_session_shape_and_the_own_item(driver, make_world, client_for):
    world, other = make_world(), make_world()
    small = plant_small_world(driver, world)
    client = client_for(world)
    session = client.get("/v1/views/session").json()
    assert session == {
        "signed_in": True,
        "self_item_id": None,
        "has_items": True,
        "account": session["account"],
        "method": None,
    }

    set_self_item(driver, world, small.maya["id"])
    assert client.get("/v1/views/session").json() == {
        "signed_in": True,
        "self_item_id": small.maya["id"],
        "has_items": True,
        "account": session["account"],
        "method": None,
    }
    graph = client.get("/v1/views/graph").json()
    assert graph["self_item_id"] == small.maya["id"]
    nodes = nodes_by_id(graph)
    assert nodes[small.maya["id"]]["self"] is True
    assert set(nodes[small.maya["id"]]) == NODE_KEYS | {"self"}
    assert all("self" not in node for key, node in nodes.items() if key != small.maya["id"])
    detail = client.get(f"/v1/views/node/{small.maya['id']}").json()
    assert detail["node"]["self"] is True

    # An id that is no item of this world is never the person's own item.
    stranger = plant_node(driver, other, "Item", title="Elsewhere")
    set_self_item(driver, world, stranger["id"])
    assert client.get("/v1/views/session").json()["self_item_id"] is None
    assert client.get("/v1/views/graph").json()["self_item_id"] is None
