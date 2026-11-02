"""A session of world A never sees, finds or places anything of world B."""

from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node, set_self_item
from tests.views.conftest import plant_keyed_link, plant_small_world


def node_ids(payload):
    return {wrapper["data"]["id"] for wrapper in payload["elements"]["nodes"]}


def edge_ids(payload):
    return {wrapper["data"]["id"] for wrapper in payload["elements"]["edges"]}


def position_count(driver, world_id):
    return WorldQuery(driver, world_id).read("MATCH (p:MapPosition) RETURN count(p) AS n")[0]["n"]


def test_a_session_of_a_sees_only_a(driver, make_world, client_for):
    a, b = make_world(), make_world()
    small_a, small_b = plant_small_world(driver, a), plant_small_world(driver, b)
    # A link of world B between B's items, and a link that crosses from A's item to B's item.
    crossing = plant_link(
        driver,
        a,
        small_a.venue["id"],
        small_b.venue["id"],
        to_world=b,
        meaning=small_a.depends_on["id"],
        active_key="crossing",
    )
    client_a, client_b = client_for(a), client_for(b)

    graph_a = client_a.get("/v1/views/graph").json()
    ids_a = {small_a.venue["id"], small_a.catering["id"], small_a.maya["id"]}
    assert node_ids(graph_a) == ids_a
    assert edge_ids(graph_a) == {small_a.depends["id"], small_a.assigned["id"]}
    assert crossing["id"] not in edge_ids(graph_a)
    assert graph_a["stats"]["total_nodes"] == 3

    graph_b = client_b.get("/v1/views/graph").json()
    assert node_ids(graph_b).isdisjoint(ids_a)
    assert edge_ids(graph_b) == {small_b.depends["id"], small_b.assigned["id"]}

    health_a = client_a.get("/v1/views/health").json()
    assert (health_a["nodes"], health_a["edges"]) == (3, 2)


def test_a_node_of_b_is_404_for_a(driver, make_world, client_for):
    a, b = make_world(), make_world()
    plant_small_world(driver, a)
    small_b = plant_small_world(driver, b)
    response = client_for(a).get(f"/v1/views/node/{small_b.venue['id']}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert client_for(b).get(f"/v1/views/node/{small_b.venue['id']}").status_code == 200


def test_positions_for_b_ids_are_unknown_and_write_nothing(driver, make_world, client_for):
    a, b = make_world(), make_world()
    small_a, small_b = plant_small_world(driver, a), plant_small_world(driver, b)
    client_a = client_for(a)
    body = {
        "positions": [
            {"id": small_b.venue["id"], "x": 1.0, "y": 2.0},
            {"id": small_b.maya["id"], "x": 3.0, "y": 4.0},
            {"id": small_a.venue["id"], "x": 5.0, "y": 6.0},
        ]
    }
    response = client_a.post("/v1/views/positions", json=body)
    assert response.status_code == 200
    assert response.json() == {"placed": 1, "kept": 0, "unknown": 2}
    assert position_count(driver, b) == 0
    assert position_count(driver, a) == 1

    # B's own map is untouched: its items carry no x and y.
    graph_b = client_for(b).get("/v1/views/graph").json()
    assert all("x" not in w["data"] and "y" not in w["data"] for w in graph_b["elements"]["nodes"])


def test_the_kinds_of_b_do_not_name_the_items_of_a(driver, make_world, client_for):
    """A's item that points at B's kind id shows no name from B's registry."""
    a, b = make_world(), make_world()
    small_b = plant_small_world(driver, b)
    plant_node(driver, a, "Item", title="Borrowed kind", kind=small_b.task["id"])
    graph_a = client_for(a).get("/v1/views/graph").json()
    assert [w["data"]["type"] for w in graph_a["elements"]["nodes"]] == ["unknown"]
    assert "task" not in client_for(a).get("/v1/views/schema").json()


def test_an_own_item_of_b_is_never_marked_for_a(driver, make_world, client_for):
    a, b = make_world(), make_world()
    plant_small_world(driver, a)
    small_b = plant_small_world(driver, b)
    client_a = client_for(a)
    set_self_item(driver, a, small_b.maya["id"])
    graph_a = client_a.get("/v1/views/graph").json()
    assert graph_a["self_item_id"] is None
    assert all("self" not in w["data"] for w in graph_a["elements"]["nodes"])
    assert client_a.get("/v1/views/session").json()["self_item_id"] is None


def test_a_link_of_b_between_a_ids_is_not_shown(driver, make_world, client_for):
    """Links are read through world A's scope, so B's rows never join A's graph."""
    a, b = make_world(), make_world()
    small_a = plant_small_world(driver, a)
    small_b = plant_small_world(driver, b)
    extra = plant_keyed_link(
        driver, b, small_b.catering["id"], small_b.maya["id"], small_b.depends_on["id"]
    )
    graph_a = client_for(a).get("/v1/views/graph").json()
    assert extra["id"] not in edge_ids(graph_a)
    assert edge_ids(graph_a) == {small_a.depends["id"], small_a.assigned["id"]}
