"""Unit controls for bounded graph data and exact synthetic provenance."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from grove.api.errors import ApiError
from grove.demo.corpus import (
    ACTOR,
    PARSER,
    SOURCE_TIME,
    VERSION,
    content_identity,
    request_identity,
    stories,
)
from grove.demo_policy import RICH_CORPUS_SHA256
from grove.reads.demo_current import _TOPIC_CAPTURE, _rich_source
from grove.store.current import Standing
from grove.store.scope import scope_cypher
from grove.views import graph
from grove.views.registry import Kind, Meaning, Registry


def test_topic_capture_is_scoped_before_bounded_materialization():
    scoped = scope_cypher(_TOPIC_CAPTURE, "read")
    assert "e.message_key IN $message_keys" in scoped
    assert scoped.index("e.message_key IN $message_keys") < scoped.index("LIMIT $item_cap")
    assert "WITH DISTINCT i" in scoped
    assert scoped.count("world_id") >= 10


def test_graph_payload_bounds_and_endpoints(monkeypatch):
    monkeypatch.setattr(graph, "MAX_GRAPH_ITEMS", 4)
    monkeypatch.setattr(graph, "MAX_GRAPH_LINKS", 5)
    registry = Registry(
        kinds={"k": Kind("k", "task", (), False)},
        meanings={"m": Meaning("m", "related to", "many")},
    )
    monkeypatch.setattr(graph, "read_registry", lambda *_: registry)
    monkeypatch.setattr(
        graph,
        "standings_of",
        lambda world, items, **kwargs: {
            item["id"]: Standing("current", None, None, {}, (), "started") for item in items
        },
    )
    calls = []

    class Query:
        def read(self, query, **params):
            calls.append((query, params))
            if query == graph._ITEMS:
                return [{"id": str(i), "kind": "k", "title": "Invented task"} for i in range(5)]
            if query == graph._POSITIONS:
                return [
                    {"item_id": "0", "x": float("nan"), "y": 1},
                    {"item_id": "1", "x": 2, "y": 3},
                ]
            assert query == graph._ACTIVE_LINKS
            return [
                {
                    "id": str(i),
                    "from_item": str(i % 4),
                    "to_item": str((i + 1) % 4),
                    "meaning": "m",
                    "active_key": str(i),
                }
                for i in range(6)
            ]

    result = graph.graph_payload(
        Query(),
        Query(),
        window=graph.Window(),
        self_item_id=None,
        now=datetime(2026, 11, 4, tzinfo=UTC),
    )
    nodes = [row["data"] for row in result["elements"]["nodes"]]
    edges = [row["data"] for row in result["elements"]["edges"]]
    ids = {node["id"] for node in nodes}
    assert len(nodes) == result["stats"]["total_nodes"] == 4
    assert len(edges) == result["stats"]["total_edges"] == 5
    assert all(edge["source"] in ids and edge["target"] in ids for edge in edges)
    assert result["selection"]["truncated"] is True
    assert "x" not in nodes[0]
    assert (nodes[1]["x"], nodes[1]["y"]) == (2, 3)
    json.dumps(result, allow_nan=False)
    assert calls[0][1] == {"item_cap": 5}
    assert calls[-1][1] == {"item_ids": sorted(ids), "link_cap": 6}


@pytest.mark.parametrize("value", ['{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'])
def test_nonfinite_properties_are_not_rendered(value):
    assert graph._properties(value) == {}


def source_bundle():
    story = stories()[0]
    rid = request_identity("world", story.key)
    at = datetime.fromisoformat(SOURCE_TIME)
    source = {
        "id": "s",
        "demo_version": VERSION,
        "demo_request_id": rid,
        "world_id": "world",
        "connector_id": "c",
        "external_locator": "synthetic:" + story.key,
        "version": 1,
        "forget_state": "kept",
        "parser_version": PARSER,
        "untrusted": True,
        "content": story.content(),
        "received_at": at,
    }
    connector = {
        "id": "c",
        "type": "ai_host",
        "provider": ACTOR,
        "auth_reference": "synthetic",
        "batch_rule": "fixed_corpus_v1",
        "settings": json.dumps({"version": VERSION, "sha256": RICH_CORPUS_SHA256}),
    }
    receipt = {
        "state": "saved",
        "request_id": rid,
        "commit_id": "commit",
        "change_set_id": "changes",
        "lines": [],
    }
    request = {
        "id": rid,
        "content_key": content_identity(story.key),
        "source_id": "s",
        "caller": ACTOR,
        "verbatim": False,
        "status": "saved",
        "commit_id": "commit",
        "change_set_id": "changes",
        "receipt": json.dumps(receipt),
    }
    episode = {
        "id": "e",
        "connector_id": "c",
        "source_ids": ["s"],
        "first_message_key": story.items[0].key,
        "last_message_key": story.items[-1].key,
        "cut_rule": "fixed_corpus_v1",
        "start_time": at,
        "end_time": at,
    }
    evidence = {
        "source_id": "s",
        "episode_id": "e",
        "request_id": rid,
        "message_key": story.items[0].key,
        "locator": "whole",
        "source_time": at,
        "source_type": "said",
    }
    indexed = {
        "sources": {"s": source},
        "connectors": {"c": connector},
        "requests": {rid: request},
        "episodes": {"e": episode},
    }
    bundle = {
        "screenings": [
            {
                "source_id": "s",
                "screened_where": "server",
                "screened_at": at,
                "counts_per_class": "{}",
            }
        ]
    }
    return evidence, bundle, indexed


def test_exact_source_provenance_is_accepted():
    evidence, bundle, indexed = source_bundle()
    assert _rich_source(evidence, bundle, indexed)[0]["id"] == "s"


@pytest.mark.parametrize(
    "fault",
    [
        "content",
        "parser",
        "connector",
        "request",
        "receipt",
        "episode",
        "message",
        "time",
        "screening",
    ],
)
def test_modified_rich_provenance_refuses(fault):
    evidence, bundle, indexed = deepcopy(source_bundle())
    if fault == "content":
        indexed["sources"]["s"]["content"] += " "
    elif fault == "parser":
        indexed["sources"]["s"]["parser_version"] = "private"
    elif fault == "connector":
        indexed["connectors"]["c"]["settings"] = "{}"
    elif fault == "request":
        indexed["requests"][evidence["request_id"]]["verbatim"] = True
    elif fault == "receipt":
        indexed["requests"][evidence["request_id"]]["receipt"] = "{}"
    elif fault == "episode":
        indexed["episodes"]["e"]["source_ids"].append("private")
    elif fault == "message":
        evidence["message_key"] = "private"
    elif fault == "time":
        evidence["source_time"] = datetime(2026, 11, 3, tzinfo=UTC)
    else:
        bundle["screenings"] = []
    with pytest.raises(ApiError, match="provenance"):
        _rich_source(evidence, bundle, indexed)
