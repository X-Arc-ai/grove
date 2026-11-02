"""The graph read takes only the fields its payload uses, and without as_of only active links."""

import json

import pytest

from grove.store.query import WorldQuery
from grove.views.graph import Window, graph_payload
from grove.writer.testing import plant_link, plant_node
from tests.views.conftest import T0, T1, T2, plant_keyed_link

HIDDEN = {"body", "search_terms", "property_values"}
MARK = "PLANTED-ONLY-IN-HIDDEN-FIELDS"


class RecordingQuery(WorldQuery):
    """A WorldQuery that keeps every row its reads return."""

    def __init__(self, driver, world_id: str) -> None:
        super().__init__(driver, world_id)
        self.rows: list[dict] = []

    def read(self, cypher: str, /, **params) -> list[dict]:
        rows = super().read(cypher, **params)
        self.rows.extend(rows)
        return rows


def keys_in(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_in(item)


@pytest.fixture
def planted(driver, world):
    kind = plant_node(driver, world, "Kind", name="task", status_vocabulary="[]")
    meaning = plant_node(driver, world, "Meaning", name="depends on", cardinality="many")
    lanterns = plant_node(
        driver,
        world,
        "Item",
        kind=kind["id"],
        title="Pack the lanterns",
        summary="For the fair",
        body=f"body {MARK}",
        search_terms=[f"term {MARK}"],
        property_values=[f"value {MARK}"],
        properties=json.dumps({"due": "2026-12-01"}),
        recorded=T0,
    )
    hooks = plant_node(driver, world, "Item", kind=kind["id"], title="Buy hooks", recorded=T0)
    active = plant_keyed_link(
        driver, world, hooks["id"], lanterns["id"], meaning["id"], recorded=T0
    )
    ended = plant_link(
        driver,
        world,
        lanterns["id"],
        hooks["id"],
        meaning=meaning["id"],
        recorded=T0,
        stopped_being_true_earliest=T1,
    )
    return lanterns, active, ended


def test_the_payload_never_holds_body_search_terms_or_property_values(world, client_for, planted):
    response = client_for(world).get("/v1/views/graph")
    assert response.status_code == 200
    payload = response.json()
    assert not set(keys_in(payload)) & HIDDEN
    assert MARK not in response.text
    nodes = {w["data"]["label"]: w["data"] for w in payload["elements"]["nodes"]}
    assert nodes["Pack the lanterns"]["properties"] == {"due": "2026-12-01"}


def test_the_read_takes_no_hidden_field_and_no_ended_link(driver, world, planted):
    lanterns, active, ended = planted
    reads = RecordingQuery(driver, world)
    payload = graph_payload(
        reads, WorldQuery.primitives(driver), window=Window(), self_item_id=None, now=T2
    )
    assert not set(keys_in(reads.rows)) & HIDDEN
    taken = json.dumps(reads.rows, default=str)
    assert MARK not in taken
    assert active["id"] in taken and ended["id"] not in taken
    assert [w["data"]["id"] for w in payload["elements"]["edges"]] == [active["id"]]
    shown = next(
        w["data"] for w in payload["elements"]["nodes"] if w["data"]["id"] == lanterns["id"]
    )
    assert (shown["label"], shown["summary"]) == ("Pack the lanterns", "For the fair")


def test_with_as_of_the_read_still_finds_a_link_that_held_then(driver, world, planted):
    _, active, ended = planted
    reads = RecordingQuery(driver, world)
    payload = graph_payload(
        reads,
        WorldQuery.primitives(driver),
        window=Window(as_of=T0),
        self_item_id=None,
        now=T2,
    )
    assert not set(keys_in(reads.rows)) & HIDDEN
    edges = sorted(w["data"]["id"] for w in payload["elements"]["edges"])
    assert edges == sorted([active["id"], ended["id"]])
