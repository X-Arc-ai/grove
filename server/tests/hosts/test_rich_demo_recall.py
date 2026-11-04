"""Native integration: saved synthetic evidence, Cedar and actual graph payloads."""

import json

import pytest
from grove.writer.demo_seed import seed_story

from grove import clock
from grove.api.errors import ApiError
from grove.demo.corpus import stories
from grove.demo_policy import configured_email_hash
from grove.reads.demo_current import MAX_ITEMS, DemoRecallHandler
from grove.store.query import WorldQuery
from grove.views.graph import Window, graph_payload
from grove.writer import Caller, apply_batch
from tests.hosts.test_demo_recall import demo_context as demo_context
from tests.hosts.test_demo_recall import queue
from tests.hosts.test_store_component import complete
from tests.writer.helpers import world_dump


def test_rich_saved_recall_preserves_cedar_and_has_real_graph(demo_context):
    context, account = demo_context
    cedar_request = queue(context)
    complete(context, cedar_request)
    seed_story(context.driver, configured_email_hash(), stories()[0].key)
    before = world_dump(context.driver, context.world_id)
    handler = DemoRecallHandler()
    cedar = handler(context, {"about": "Which room is assigned to Cedar?"})
    assert len(cedar["items"]) == 1
    assert cedar["items"][0]["provenance"][0]["request_id"] == cedar_request
    rich = handler(context, {"about": "Lantern Amber Quay book lockers"})
    assert 0 < len(rich["items"]) <= MAX_ITEMS
    assert all("Lantern Amber Quay" in item["title"] for item in rich["items"])
    assert all(item["provenance"][0]["synthetic"] for item in rich["items"])
    assert all(item["source_type"] == "said" for item in rich["items"])
    assert handler(context, {"about": "unrelated-private-identity"})["items"] == []
    assert world_dump(context.driver, context.world_id) == before

    query = WorldQuery(context.driver, context.world_id)
    payload = graph_payload(
        query,
        WorldQuery.primitives(context.driver),
        window=Window(),
        self_item_id=account.self_item_id,
        now=clock.now(context.world_id),
    )
    nodes = payload["elements"]["nodes"]
    edges = payload["elements"]["edges"]
    assert payload["stats"]["total_nodes"] == len(nodes) == 54
    assert payload["stats"]["total_edges"] == len(edges) == 156
    ids = {node["data"]["id"] for node in nodes}
    assert all(edge["data"]["source"] in ids and edge["data"]["target"] in ids for edge in edges)
    assert payload["selection"]["truncated"] is False
    json.dumps(payload, allow_nan=False)

    # The candidate selection uses the bound source key, not a mutable title.
    chosen = rich["items"][0]
    stored = query.read("MATCH (i:Item {id: $id}) RETURN i", id=chosen["id"])[0]["i"]
    apply_batch(
        context.driver,
        context.world_id,
        Caller("person", context.scopes, "person:synthetic-control"),
        {
            "ops": [
                {
                    "op": "update_item",
                    "op_id": "change-title",
                    "item": stored["id"],
                    "version": stored["version"],
                    "fields": {"title": "Changed synthetic fact"},
                    "evidence_refs": [
                        {"evidence_id": stored["provenance_evidence_id"], "polarity": "supports"}
                    ],
                    "reason": "Exercise immutable synthetic provenance",
                }
            ]
        },
    )
    with pytest.raises(ApiError, match="provenance"):
        handler(context, {"about": "Lantern Amber Quay book lockers"})
