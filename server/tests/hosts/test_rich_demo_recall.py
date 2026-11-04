"""Native integration: saved synthetic evidence, Cedar and actual graph payloads."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from grove import clock
from grove.api.errors import ApiError
from grove.demo.corpus import VERSION, stories
from grove.demo_policy import DEMO_VERSION, configured_email_hash
from grove.reads.demo_current import (
    _TOPIC_CAPTURE,
    MAX_ITEMS,
    MAX_ROWS,
    MAX_TEXT,
    MAX_VALUE,
    DemoRecallHandler,
    _validate,
)
from grove.store.current import SUCCESSOR_MEANINGS
from grove.store.query import WorldQuery
from grove.views.graph import Window, graph_payload
from grove.writer import Caller, apply_batch
from grove.writer.demo_seed import seed_story
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

    # Even a matching fabricated creation snapshot cannot add unapproved content
    # to a valid fixed source. This is a pure tampered-copy control, no store write.
    captured = query.read(
        _TOPIC_CAPTURE,
        own=account.self_item_id,
        cedar_version=DEMO_VERSION,
        rich_version=VERSION,
        message_keys=[stories()[0].items[0].key],
        item_cap=MAX_ITEMS + 1,
        row_cap=MAX_ROWS + 1,
        text_cap=MAX_TEXT,
        value_cap=MAX_VALUE,
        successors=list(SUCCESSOR_MEANINGS),
    )[0]
    tampered = deepcopy(captured)
    victim = next(
        item for item in tampered["items"] if item["title"] == stories()[0].items[0].title
    )
    victim["body"] = "Unapproved extra material"
    entry = next(e for e in tampered["entries"] if e["entity_id"] == victim["id"])
    value = next(v for v in tampered["values"] if v["id"] == entry["new_value_id"])
    original = json.loads(value["value"])
    original["row"]["body"] = victim["body"]
    value["value"] = json.dumps(original)
    with pytest.raises(ApiError, match="provenance"):
        _validate(tampered, context.world_id, clock.now(context.world_id))

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


def test_later_checked_bridge_keeps_prior_story_recallable(demo_context):
    context, _ = demo_context
    first, second = stories()[:2]
    seed_story(context.driver, configured_email_hash(), first.key)
    later = clock.TestClock()
    later.set(clock.now(context.world_id) + timedelta(minutes=1))
    with clock.using(later):
        seed_story(context.driver, configured_email_hash(), second.key)
        result = DemoRecallHandler()(context, {"about": "Lantern Amber Quay access plan"})
    assert first.items[6].title in {item["title"] for item in result["items"]}
