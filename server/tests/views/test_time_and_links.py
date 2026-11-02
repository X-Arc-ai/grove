"""Active links only; as_of and known_as_of filter the current head; a bad time is a typed 400."""

from datetime import timedelta, timezone

import pytest

from grove.writer.testing import plant_link, plant_node
from tests.views.conftest import T0, T1, T2, T3, plant_keyed_link

MEANING = {"name": "depends on", "cardinality": "many"}


def labels(payload):
    return sorted(w["data"]["label"] for w in payload["elements"]["nodes"])


def edge_ids(payload):
    return {w["data"]["id"] for w in payload["elements"]["edges"]}


def z(moment):
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def timeline(driver, world):
    """Items and links whose clocks sit around T0 < T1 < T2 < T3."""
    meaning = plant_node(driver, world, "Meaning", **MEANING)["id"]
    old = plant_node(driver, world, "Item", title="Old", recorded=T0, became_true_earliest=T0)
    late = plant_node(driver, world, "Item", title="Late", recorded=T2, became_true_earliest=T2)
    plant_node(
        driver,
        world,
        "Item",
        title="Ended",
        recorded=T0,
        became_true_earliest=T0,
        stopped_being_true_earliest=T1,
        stopped_being_true_latest=T1,
    )
    told_late = plant_node(
        driver, world, "Item", title="Told late", recorded=T3, became_true_earliest=T0
    )
    active = plant_keyed_link(
        driver, world, late["id"], old["id"], meaning, recorded=T2, became_true_earliest=T2
    )
    # An ended link: no active_key; its stopped bound says it held from T0 until T2.
    closed = plant_link(
        driver,
        world,
        told_late["id"],
        old["id"],
        meaning=meaning,
        recorded=T0,
        became_true_earliest=T0,
        stopped_being_true_latest=T2,
    )
    # An ended link with no stopped bound (corrected away): never shown.
    corrected = plant_link(driver, world, old["id"], told_late["id"], meaning=meaning, recorded=T0)
    return {"active": active["id"], "closed": closed["id"], "corrected": corrected["id"]}


def test_without_filters_all_items_and_only_active_links(world, client_for, timeline):
    graph = client_for(world).get("/v1/views/graph").json()
    assert labels(graph) == ["Ended", "Late", "Old", "Told late"]
    assert edge_ids(graph) == {timeline["active"]}
    nodes = {w["data"]["label"]: w["data"] for w in graph["elements"]["nodes"]}
    assert nodes["Ended"]["invalid_at"] == "2026-09-15T09:00:00.000Z"
    assert graph["time"] == {"min": "2026-09-01", "max": "2026-10-01"}


def test_as_of_equal_to_the_stop_bound_leaves_the_row_out(world, client_for, timeline):
    """The interval holds T while the stop is later than T: the stop itself is outside."""
    client = client_for(world)
    just_before = client.get("/v1/views/graph", params={"as_of": z(T1 - timedelta(seconds=1))})
    assert "Ended" in labels(just_before.json())
    at_the_stop = client.get("/v1/views/graph", params={"as_of": z(T1)}).json()
    assert "Ended" not in labels(at_the_stop)

    link_before = client.get("/v1/views/graph", params={"as_of": z(T2 - timedelta(seconds=1))})
    assert timeline["closed"] in edge_ids(link_before.json())
    link_at_stop = client.get("/v1/views/graph", params={"as_of": z(T2)}).json()
    assert timeline["closed"] not in edge_ids(link_at_stop)


def test_a_row_with_two_stop_bounds_uses_the_latest(driver, world, client_for):
    """The stop lies between two bounds: invalid_at and as_of both read the latest one."""
    meaning = plant_node(driver, world, "Meaning", **MEANING)["id"]
    bounds = {"stopped_being_true_earliest": T1, "stopped_being_true_latest": T2}
    unsure = plant_node(
        driver, world, "Item", title="Unsure", recorded=T0, became_true_earliest=T0, **bounds
    )["id"]
    other = plant_node(driver, world, "Item", title="Other", recorded=T0)["id"]
    link = plant_link(
        driver,
        world,
        unsure,
        other,
        meaning=meaning,
        recorded=T0,
        became_true_earliest=T0,
        **bounds,
    )["id"]
    client = client_for(world)

    plain = client.get("/v1/views/graph").json()
    item = next(w["data"] for w in plain["elements"]["nodes"] if w["data"]["id"] == unsure)
    assert item["invalid_at"] == "2026-10-01T09:00:00.000Z"
    assert client.get(f"/v1/views/node/{unsure}").json()["node"]["invalid_at"] == item["invalid_at"]

    between = client.get("/v1/views/graph", params={"as_of": z(T1 + timedelta(days=1))}).json()
    assert "Unsure" in labels(between)
    assert edge_ids(between) == {link}
    shown = between["elements"]["edges"][0]["data"]
    assert shown["invalid_at"] == "2026-10-01T09:00:00.000Z"

    at_latest = client.get("/v1/views/graph", params={"as_of": z(T2)}).json()
    assert "Unsure" not in labels(at_latest)
    assert edge_ids(at_latest) == set()


def test_time_max_comes_from_a_stop_later_than_every_start(driver, world, client_for):
    plant_node(driver, world, "Item", title="Short", recorded=T1, became_true_earliest=T1)
    plant_node(
        driver,
        world,
        "Item",
        title="Long",
        recorded=T0,
        became_true_earliest=T0,
        stopped_being_true_earliest=T3,
        stopped_being_true_latest=T3,
    )
    graph = client_for(world).get("/v1/views/graph").json()
    assert graph["time"] == {"min": "2026-09-01", "max": "2026-10-15"}


def test_health_counts_only_the_active_links(driver, world, client_for):
    meaning = plant_node(driver, world, "Meaning", **MEANING)["id"]
    first = plant_node(driver, world, "Item", title="First", recorded=T0)["id"]
    second = plant_node(driver, world, "Item", title="Second", recorded=T0)["id"]
    active = plant_keyed_link(driver, world, first, second, meaning, recorded=T0)
    # An ended link: no active_key, and it stays in the store.
    plant_link(driver, world, second, first, meaning=meaning, recorded=T0)

    client = client_for(world)
    assert client.get("/v1/views/health").json()["edges"] == 1
    assert edge_ids(client.get("/v1/views/graph").json()) == {active["id"]}


def test_known_as_of_keeps_rows_recorded_by_then(world, client_for, timeline):
    client = client_for(world)
    graph = client.get("/v1/views/graph", params={"known_as_of": z(T1)}).json()
    assert labels(graph) == ["Ended", "Old"]
    assert edge_ids(graph) == set()
    assert graph["known_as_of"] == "2026-09-15T09:00:00.000Z" and graph["as_of"] is None

    later = client.get("/v1/views/graph", params={"known_as_of": z(T2)}).json()
    assert labels(later) == ["Ended", "Late", "Old"]
    assert edge_ids(later) == {timeline["active"]}


def test_as_of_keeps_rows_whose_interval_holds_then(world, client_for, timeline):
    client = client_for(world)
    at_start = client.get("/v1/views/graph", params={"as_of": z(T0)}).json()
    assert labels(at_start) == ["Ended", "Old", "Told late"]
    # The ended link held at T0: as_of shows it although it is no longer active.
    assert edge_ids(at_start) == {timeline["closed"]}

    middle = client.get("/v1/views/graph", params={"as_of": z(T1 + timedelta(days=1))}).json()
    assert labels(middle) == ["Old", "Told late"]
    assert edge_ids(middle) == {timeline["closed"]}

    at_end = client.get("/v1/views/graph", params={"as_of": z(T3)}).json()
    assert labels(at_end) == ["Late", "Old", "Told late"]
    assert edge_ids(at_end) == {timeline["active"]}
    assert at_end["as_of"] == "2026-10-15T09:00:00.000Z"


def test_both_filters_together(world, client_for, timeline):
    graph = (
        client_for(world)
        .get("/v1/views/graph", params={"as_of": z(T0), "known_as_of": z(T2)})
        .json()
    )
    assert labels(graph) == ["Ended", "Old"]
    assert edge_ids(graph) == set()


def test_an_offset_is_read_as_the_same_instant(world, client_for, timeline):
    offset = T1.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    assert offset == "2026-09-15T14:30:00+05:30"
    graph = client_for(world).get("/v1/views/graph", params={"known_as_of": offset}).json()
    assert graph["known_as_of"] == "2026-09-15T09:00:00.000Z"
    assert labels(graph) == ["Ended", "Old"]


def test_the_node_view_takes_the_same_filters(driver, world, client_for, timeline):
    client = client_for(world)
    graph = client.get("/v1/views/graph").json()
    late = next(w["data"]["id"] for w in graph["elements"]["nodes"] if w["data"]["label"] == "Late")
    assert client.get(f"/v1/views/node/{late}").status_code == 200
    assert client.get(f"/v1/views/node/{late}", params={"as_of": z(T3)}).status_code == 200
    hidden = client.get(f"/v1/views/node/{late}", params={"as_of": z(T1)})
    assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "not_found"
    unknown_then = client.get(f"/v1/views/node/{late}", params={"known_as_of": z(T1)})
    assert unknown_then.status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        "yesterday",
        "2026-11-02",
        "2026-11-02T10:00:00",
        "",
        "2026-13-01T00:00:00Z",
        "2026-11-02T10:00:00Z" + "0" * 60,
    ],
)
@pytest.mark.parametrize("field", ["as_of", "known_as_of"])
def test_a_bad_time_is_a_typed_400(world, client_for, value, field):
    client = client_for(world)
    for path in ("/v1/views/graph", "/v1/views/node/some-id"):
        response = client.get(path, params={field: value})
        assert response.status_code == 400, (path, value)
        error = response.json()["error"]
        assert error["code"] == "invalid_arguments"
        assert error["details"] == {"field": field}
        assert error["retryable"] is False
        assert value not in error["message"] or not value


def test_a_link_to_a_hidden_item_is_hidden(driver, world, client_for):
    meaning = plant_node(driver, world, "Meaning", **MEANING)["id"]
    seen = plant_node(driver, world, "Item", title="Seen", recorded=T0)
    later = plant_node(driver, world, "Item", title="Later", recorded=T2)
    link = plant_keyed_link(driver, world, seen["id"], later["id"], meaning, recorded=T0)
    client = client_for(world)
    assert edge_ids(client.get("/v1/views/graph").json()) == {link["id"]}
    filtered = client.get("/v1/views/graph", params={"known_as_of": z(T1)}).json()
    assert labels(filtered) == ["Seen"] and edge_ids(filtered) == set()
