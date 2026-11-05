"""GET /v1/sources/connectors: what each connector really holds in the graph.
Invented names only (R3)."""

import pytest

from grove.api import uploads
from grove.ids import new_id
from grove.store.query import WorldQuery
from grove.writer.accounts import CONNECTOR_TYPE
from grove.writer.sources import record_connector
from grove.writer.testing import plant_link, plant_node, seed_test_world
from tests.ingest.test_upload_policy import export_bytes

CONNECTORS_PATH = "/v1/sources/connectors"
JSON_TYPE = {"content-type": "application/json"}
TEXTS = ["The cedar planter sits on the north wall", "Order brackets on Friday"]
EMPTY = {"sources": 0, "nodes": 0, "edges": 0, "item_ids": []}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GROVE_ENV", "GROVE_RENAME_MAP", "GROVE_DENYLIST", uploads.MAX_BYTES_VAR):
        monkeypatch.delenv(name, raising=False)


def send(client, provider="chatgpt", texts=TEXTS):
    response = client.post(
        f"/v1/sources/upload?provider={provider}",
        content=export_bytes(provider, texts),
        headers=JSON_TYPE,
    )
    assert response.status_code == 200
    return response.json()


def rows_by_provider(client):
    response = client.get(CONNECTORS_PATH)
    assert response.status_code == 200
    return {row["provider"]: row for row in response.json()["connectors"]}


def source_of(driver, world, connector_id):
    rows = WorldQuery(driver, world).read(
        "MATCH (s:Source {connector_id: $id}) RETURN s.id AS id ORDER BY id", id=connector_id
    )
    return [row["id"] for row in rows]


def plant_evidence(driver, world, source_id, target_id, *, target_type="item", active=True):
    plant_node(
        driver,
        world,
        "Evidence",
        source_id=source_id,
        target_id=target_id,
        target_type=target_type,
        polarity="supports",
        active=active,
        message_key="k" + new_id(world),
        locator="whole",
    )


def plant_item(driver, world, **columns):
    return plant_node(driver, world, "Item", title="Synthetic item", **columns)["id"]


def test_contribution_counts_distinct_active_rows_of_each_connector(
    driver, seeded_world, client_for, test_clock
):
    client = client_for(seeded_world)
    first = send(client)
    second = send(client, "claude_ai", ["A note about the tide pool"])
    [one] = source_of(driver, seeded_world, first["connector_id"])
    [two] = source_of(driver, seeded_world, second["connector_id"])
    shared = plant_item(driver, seeded_world)
    only_one = plant_item(driver, seeded_world)
    ended_item = plant_item(driver, seeded_world, end_class="retired")
    weak = plant_item(driver, seeded_world)
    link = plant_link(driver, seeded_world, shared, only_one, active_key="live-key")["id"]
    ended_link = plant_link(driver, seeded_world, only_one, shared)["id"]
    # Two evidence rows on one item count once.
    plant_evidence(driver, seeded_world, one, shared)
    plant_evidence(driver, seeded_world, one, shared)
    plant_evidence(driver, seeded_world, one, only_one)
    plant_evidence(driver, seeded_world, one, ended_item)
    plant_evidence(driver, seeded_world, one, weak, active=False)
    plant_evidence(driver, seeded_world, one, link, target_type="link")
    plant_evidence(driver, seeded_world, one, link, target_type="link")
    plant_evidence(driver, seeded_world, one, ended_link, target_type="link")
    plant_evidence(driver, seeded_world, two, shared)
    rows = rows_by_provider(client)
    held = rows["chatgpt"]["contribution"]
    assert sorted(held.pop("item_ids")) == sorted([shared, only_one])
    assert held == {"sources": 1, "nodes": 2, "edges": 1}
    assert rows["claude_ai"]["contribution"] == {
        "sources": 1,
        "nodes": 1,
        "edges": 0,
        "item_ids": [shared],
    }


def test_a_connector_with_no_evidence_answers_zeros_and_stays_listed(
    driver, seeded_world, client_for, test_clock
):
    connector = record_connector(driver, seeded_world, CONNECTOR_TYPE)
    client = client_for(seeded_world)
    rows = client.get(CONNECTORS_PATH).json()["connectors"]
    [row] = [r for r in rows if r["connector_id"] == connector]
    assert row["provider"] is None and row["type"] == CONNECTOR_TYPE
    assert row["contribution"] == EMPTY
    for field in ("last_outcome", "read_count", "failed_count", "watermark"):
        assert field in row
    send(client)
    assert rows_by_provider(client)["chatgpt"]["contribution"] == {**EMPTY, "sources": 1}


def test_item_ids_stop_at_the_cap_and_the_count_does_not(
    driver, seeded_world, client_for, test_clock, monkeypatch
):
    monkeypatch.setattr(uploads, "MAX_CONTRIBUTION_ITEM_IDS", 3)
    client = client_for(seeded_world)
    taken = send(client)
    [source] = source_of(driver, seeded_world, taken["connector_id"])
    items = [plant_item(driver, seeded_world) for _ in range(5)]
    for item in reversed(items):
        plant_evidence(driver, seeded_world, source, item)
    held = rows_by_provider(client)["chatgpt"]["contribution"]
    assert held["nodes"] == 5
    assert held["item_ids"] == sorted(items)[:3]


def test_the_real_cap_is_500():
    assert uploads.MAX_CONTRIBUTION_ITEM_IDS == 500


def test_the_count_is_one_read_for_all_connectors(
    driver, seeded_world, client_for, test_clock, monkeypatch
):
    client = client_for(seeded_world)
    send(client)
    send(client, "claude_ai", ["A note about the tide pool"])
    calls = []
    original = WorldQuery.read

    def counted(self, cypher, **params):
        calls.append(cypher)
        return original(self, cypher, **params)

    monkeypatch.setattr(uploads.WorldQuery, "read", counted)
    assert len(client.get(CONNECTORS_PATH).json()["connectors"]) == 2
    assert len([cypher for cypher in calls if "Connector" in cypher]) == 1
    assert len([cypher for cypher in calls if "Evidence" in cypher]) == 1


def test_another_world_is_never_counted(driver, seeded_world, make_world, client_for, test_clock):
    other = make_world()
    seed_test_world(driver, other, owner_user_id=new_id())
    mine, theirs = client_for(seeded_world), client_for(other)
    taken = send(mine)
    send(theirs)
    [source] = source_of(driver, seeded_world, taken["connector_id"])
    item = plant_item(driver, seeded_world)
    plant_evidence(driver, seeded_world, source, item)
    assert rows_by_provider(mine)["chatgpt"]["contribution"]["item_ids"] == [item]
    assert rows_by_provider(theirs)["chatgpt"]["contribution"] == {**EMPTY, "sources": 1}
