"""Cedar capacity, mixed-topic truncation, and unchanged provenance refusal controls."""

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from grove.api.errors import ApiError
from grove.demo import corpus
from grove.demo_policy import DEMO_TEXT, DEMO_VERSION
from grove.reads import demo_current as recall
from tests.demo.test_bounds import source_bundle

AT = datetime.fromisoformat(corpus.SOURCE_TIME)


def snapshot(cedar_count, rich_items):
    """Complete saved provenance, including two facts from the same Cedar source."""
    _, source_rows, indexed = source_bundle()
    bundle = {key: list(rows.values()) for key, rows in indexed.items()}
    bundle.update(source_rows)
    bundle.update(
        items=[], evidence=[], entries=[], values=[], kinds=[], properties=[], successors=[]
    )
    bundle["sources"].append(
        {
            "id": "cedar-source",
            "demo_version": DEMO_VERSION,
            "version": 1,
            "forget_state": "kept",
            "parser_version": "mcp-store-1",
            "external_locator": "",
            "connector_id": "cedar-connector",
            "demo_request_id": "cedar-request",
            "received_at": AT,
            "content": json.dumps(
                [{"key": "cedar-message", "text": DEMO_TEXT, "at": AT.isoformat(), "sender": None}]
            ),
        }
    )
    bundle["connectors"].append(
        {"id": "cedar-connector", "type": "ai_host", "provider": "synthetic-control"}
    )
    bundle["requests"].append(
        {
            "id": "cedar-request",
            "source_id": "cedar-source",
            "status": "saved",
            "caller": "synthetic-control",
            "receipt": json.dumps(
                {
                    "state": "saved",
                    "request_id": "cedar-request",
                    "commit_id": "commit",
                    "change_set_id": "changes",
                    "lines": [],
                }
            ),
        }
    )
    bundle["episodes"].append(
        {"id": "cedar-episode", "source_ids": ["cedar-source"], "connector_id": "cedar-connector"}
    )
    bundle["screenings"].append(
        {"source_id": "cedar-source", "screened_where": "server", "screened_at": AT}
    )
    records = [(None, index) for index in range(cedar_count)] + [
        (item, index + cedar_count) for index, item in enumerate(rich_items)
    ]
    for fact, index in records:
        item_id, evidence_id = f"item-{index}", f"evidence-{index}"
        request = bundle["requests"][0 if fact else 1]
        item = {
            "id": item_id,
            "world_id": "world",
            "version": 1,
            "title": fact.title if fact else f"Cedar demo fact {index}",
            "summary": fact.summary if fact else DEMO_TEXT,
            "kind": fact.kind if fact else "cedar-kind",
            "status": fact.status if fact else None,
            "recorded": AT,
            "touched_at": AT,
            "provenance_evidence_id": evidence_id,
            "derived_source_type": "said",
            "derivation": "observed",
            "modality": "text",
        }
        evidence = {
            "id": evidence_id,
            "world_id": "world",
            "version": 1,
            "target_type": "item",
            "target_id": item_id,
            "source_id": request["source_id"],
            "request_id": request["id"],
            "episode_id": "e" if fact else "cedar-episode",
            "message_key": fact.key if fact else "cedar-message",
            "locator": "whole",
            "source_time": AT,
            "source_type": "said",
            "derivation": "observed",
            "modality": "text",
            "active": True,
            "polarity": "supports",
        }
        bundle["items"].append(item)
        bundle["evidence"].append(evidence)
        if not any(kind["id"] == item["kind"] for kind in bundle["kinds"]):
            bundle["kinds"].append({"id": item["kind"], "name": item["kind"]})
        for entity_type, row, table in [
            ("item", item, "items"),
            ("evidence", evidence, "evidence"),
        ]:
            value_id = f"value-{row['id']}"
            bundle["entries"].append(
                {
                    "id": f"entry-{row['id']}",
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "field": "row",
                    "new_value_id": value_id,
                    "actor": corpus.ACTOR,
                    "request_id": request["id"],
                    "commit_id": "commit",
                    "change_set_id": "changes",
                }
            )
            bundle["values"].append(
                {"id": value_id, "value": json.dumps({"table": table, "row": recall._plain(row)})}
            )
        receipt = json.loads(request["receipt"])
        receipt["lines"].append({"symbol": "+", "entity_type": "item", "entity_id": item_id})
        request["receipt"] = json.dumps(receipt)
    return bundle


def handler_snapshot(monkeypatch, bundle, selected):
    calls = []
    monkeypatch.setattr("grove.demo_policy.require_designation", lambda *_: {"self_item_id": "own"})
    monkeypatch.setattr(corpus, "topic_items", lambda *_: tuple(item.title for item in selected))
    monkeypatch.setattr(recall.clock, "now", lambda *_: AT)

    class Query:
        def __init__(self, *args, **kwargs):
            assert kwargs == {"timeout": 5}

        def read(self, query, **params):
            calls.append((query, params))
            return [deepcopy(bundle)]

    monkeypatch.setattr(recall, "WorldQuery", Query)
    context = SimpleNamespace(
        driver=object(), world_id="world", user_id="user", scopes={"grove.read"}
    )
    return context, calls


@pytest.mark.parametrize("mixed", [False, True])
def test_two_cedar_facts_and_31_rich_candidates_report_capacity(monkeypatch, mixed):
    selected = corpus.stories()[0].items[:31]
    bundle = snapshot(2, selected[:30])
    context, calls = handler_snapshot(monkeypatch, bundle, selected)
    about = "Lantern Amber Quay" + (" Cedar room" if mixed else "")
    result = recall.DemoRecallHandler()(context, {"about": about})
    assert len(calls) == 1
    assert len(bundle["items"]) == recall.MAX_ITEMS == 32
    assert len(result["items"]) == (32 if mixed else 30)
    assert result["answer"] == "truncated"
    assert result["coverage"]["truncated"] is True
    assert all(item["provenance"] for item in result["items"])
    assert sum(item["title"].startswith("Cedar") for item in result["items"]) == (2 if mixed else 0)
    assert calls[0][1]["item_cap"] == 33
    assert calls[0][1]["row_cap"] == 257


@pytest.mark.parametrize("cedar_count", [0, 1, 4, 32])
def test_capacity_uses_actual_cedar_count_and_preserves_rank(monkeypatch, cedar_count):
    selected = tuple(reversed(corpus.stories()[0].items[:32]))
    bundle = snapshot(cedar_count, selected[: 32 - cedar_count])
    context, calls = handler_snapshot(monkeypatch, bundle, selected)
    result = recall.DemoRecallHandler()(context, {"about": "Lantern Amber Quay"})
    assert calls[0][1]["message_keys"] == [item.key for item in selected]
    assert len(result["items"]) == 32 - cedar_count
    assert result["coverage"]["truncated"] is (cedar_count > 0)
    assert result["answer"] == ("truncated" if cedar_count else "found")


@pytest.mark.parametrize("fault", ["cedar_overflow", "row_overflow", "text", "history", "receipt"])
def test_capacity_does_not_weaken_refusal(monkeypatch, fault):
    selected = corpus.stories()[0].items[:31]
    bundle = snapshot(33 if fault == "cedar_overflow" else 2, [])
    if fault == "row_overflow":
        bundle["properties"] = [{}] * (recall.MAX_ROWS + 1)
    elif fault == "text":
        bundle["items"][0] = {"id": bundle["items"][0]["id"], "overflow": True}
    elif fault == "history":
        bundle["entries"].append(deepcopy(bundle["entries"][0]))
    elif fault == "receipt":
        bundle["requests"][1]["receipt"] = "{}"
    context, _ = handler_snapshot(monkeypatch, bundle, selected)
    with pytest.raises(ApiError, match="provenance"):
        recall.DemoRecallHandler()(context, {"about": "Cedar Lantern Amber Quay"})


def test_native_two_cedar_facts_leave_room_for_rich_recall(request, monkeypatch):
    """Execute the actual Cypher and writer when the focused native test is authorized."""
    from grove.demo_policy import configured_email_hash
    from grove.writer.demo_seed import seed_story
    from tests.hosts.test_demo_recall import queue
    from tests.hosts.test_store_component import complete

    context, _ = request.getfixturevalue("demo_context")
    for _ in range(2):
        complete(context, queue(context))
    seed_story(context.driver, configured_email_hash(), corpus.stories()[0].key)
    selected = corpus.topic_items("Lantern Amber Quay", 31)
    monkeypatch.setattr(corpus, "topic_items", lambda *_: selected)
    captured = []
    read = recall.WorldQuery.read

    def capture(query, statement, **params):
        rows = read(query, statement, **params)
        if statement == recall._TOPIC_CAPTURE:
            captured.append(rows[0])
        return rows

    monkeypatch.setattr(recall.WorldQuery, "read", capture)
    rich = recall.DemoRecallHandler()(context, {"about": "Lantern Amber Quay"})
    mixed = recall.DemoRecallHandler()(context, {"about": "Cedar Lantern Amber Quay"})
    assert [len(bundle["items"]) for bundle in captured] == [32, 32]
    assert 0 < len(rich["items"]) <= 30
    assert all(item["title"] != "Cedar demo" for item in rich["items"])
    assert len(mixed["items"]) == len(rich["items"]) + 2
    assert sum(item["title"] == "Cedar demo" for item in mixed["items"]) == 2
    assert all(result["coverage"]["truncated"] for result in (rich, mixed))


# Register the disposable-world fixture without requesting it in the pure controls.
from tests.hosts.test_demo_recall import demo_context as demo_context  # noqa: E402
