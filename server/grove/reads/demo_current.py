"""Restricted Cedar recall from the actual saved graph, never from a canned answer.

This adapter deliberately accepts only unchanged, newly created items. It is not the general
D7 reader. One bounded read statement captures the facts and every provenance row needed to
check them. The HTTP adapter owns the cross-process credential guard through body release.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from grove import clock
from grove.api.errors import ApiError, invalid_arguments, unavailable
from grove.mcp.tools import ToolContext
from grove.store.current import SUCCESSOR_MEANINGS, Successor, standing
from grove.store.errors import Neo4jError
from grove.store.query import WorldQuery
from grove.strict_json import loads_strict
from grove.writer.source_types import claim_fields

MAX_ITEMS = 32
MAX_ROWS = 256
MAX_TEXT = 32_768
MAX_VALUE = 131_072
DEMO_RECALL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "about": {"type": "string", "minLength": 1, "maxLength": 256},
        "time": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {"mode": {"const": "current"}},
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS},
    },
}

# Each LIMIT applies before collect, so overflow is detected without materializing a whole
# world. Text is bounded in the statement before rows cross the database connection.
_CAPTURE = """
CALL () {
  MATCH (i:Item) WHERE i.id <> $own
  WITH i LIMIT $item_cap
  RETURN collect(CASE WHEN size(coalesce(i.title, '')) <= $text_cap
    AND size(coalesce(i.summary, '')) <= $text_cap
    AND size(coalesce(i.body, '')) <= $text_cap
    AND size(coalesce(i.properties, '')) <= $text_cap
    AND size(coalesce(i.search_terms, '')) <= $text_cap
    THEN i ELSE {id: i.id, overflow: true} END) AS items
}
CALL (items) {
  MATCH (e:Evidence) WHERE e.target_type = 'item' AND e.target_id IN [i IN items | i.id]
  WITH e LIMIT $row_cap
  RETURN collect(e) AS evidence
}
CALL (items, evidence) {
  MATCH (c:ChangeEntry)
  WHERE (c.entity_type = 'item' AND c.entity_id IN [i IN items | i.id])
     OR (c.entity_type = 'evidence' AND c.entity_id IN [e IN evidence | e.id])
  WITH c LIMIT $row_cap
  RETURN collect(c) AS entries
}
CALL (entries) {
  MATCH (v:HistoryValue) WHERE v.id IN [c IN entries | c.new_value_id]
  WITH v LIMIT $row_cap
  RETURN collect({id: v.id, value: CASE WHEN size(v.value) <= $value_cap
                    THEN v.value ELSE null END}) AS values
}
CALL (evidence) {
  MATCH (s:Source) WHERE s.id IN [e IN evidence | e.source_id]
  WITH s LIMIT $row_cap
  RETURN collect(s {.*, content: CASE WHEN size(s.content) <= $text_cap
                    THEN s.content ELSE null END}) AS sources
}
CALL (sources) {
  MATCH (r:Request) WHERE r.id IN [s IN sources | s.demo_request_id]
  WITH r LIMIT $row_cap
  RETURN collect(r {.*, receipt: CASE WHEN size(r.receipt) <= $value_cap
                    THEN r.receipt ELSE null END}) AS requests
}
CALL (sources) {
  MATCH (c:Connector) WHERE c.id IN [s IN sources | s.connector_id]
  WITH c LIMIT $row_cap
  RETURN collect(c) AS connectors
}
CALL (sources) {
  MATCH (s:ScreeningReceipt) WHERE s.source_id IN [x IN sources | x.id]
  WITH s LIMIT $row_cap
  RETURN collect(s) AS screenings
}
CALL (evidence) {
  MATCH (e:Episode) WHERE e.id IN [x IN evidence | x.episode_id]
  WITH e LIMIT $row_cap
  RETURN collect(e) AS episodes
}
CALL (items) {
  MATCH (k:Kind) WHERE k.id IN [i IN items | i.kind]
  WITH k LIMIT $row_cap
  RETURN collect(k) AS kinds
}
CALL (items) {
  MATCH (p:PropertyName) WHERE p.kind IN [i IN items | i.kind] AND p.scheduled_role IS NOT NULL
  WITH p LIMIT $row_cap
  RETURN collect(p) AS properties
}
CALL (items) {
  MATCH (:Item)-[l:LINK]->(:Item)
  WHERE l.from_item IN [i IN items | i.id] AND l.meaning IN $successors
    AND l.active_key IS NOT NULL
  WITH l LIMIT $row_cap
  RETURN collect(l) AS successors
}
RETURN items, evidence, entries, values, sources, requests, connectors, screenings,
       episodes, kinds, properties, successors
"""


def _refused() -> ApiError:
    return ApiError(403, "demo_provenance_refused", "the saved demo provenance is not complete")


def _json(value: object, maximum: int = MAX_VALUE) -> Any:
    if not isinstance(value, str) or len(value) > maximum:
        raise _refused()
    try:
        return loads_strict(value.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        raise _refused() from None


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _index(rows: list[dict], key: str = "id") -> dict[str, dict]:
    indexed = {row.get(key): row for row in rows}
    if None in indexed or len(indexed) != len(rows):
        raise _refused()
    return indexed


def _creation(row: dict, entity_type: str, entries: list[dict], values: dict) -> tuple[dict, dict]:
    history = [
        e
        for e in entries
        if e.get("entity_type") == entity_type and e.get("entity_id") == row["id"]
    ]
    # Even a change followed by a restoration is outside the unchanged-fact contract.
    if len(history) != 1:
        raise _refused()
    entry = history[0]
    if (
        entry.get("field") != "row"
        or entry.get("old_value_id") is not None
        or entry.get("inverse_of") is not None
        or entry.get("history_only")
    ):
        raise _refused()
    saved = _json(values.get(entry.get("new_value_id"), {}).get("value"))
    table = "items" if entity_type == "item" else "evidence"
    if (
        not isinstance(saved, dict)
        or saved.get("table") != table
        or not isinstance(saved.get("row"), dict)
    ):
        raise _refused()
    return entry, saved["row"]


def _source(evidence: dict, bundle: dict, indexed: dict) -> tuple[dict, dict, dict]:
    from grove.demo_policy import DEMO_TEXT, DEMO_VERSION

    source = indexed["sources"].get(evidence.get("source_id"), {})
    request = indexed["requests"].get(source.get("demo_request_id"), {})
    connector = indexed["connectors"].get(source.get("connector_id"), {})
    episode = indexed["episodes"].get(evidence.get("episode_id"), {})
    if (
        source.get("demo_version") != DEMO_VERSION
        or source.get("version") != 1
        or source.get("forget_state") != "kept"
        or source.get("parser_version") != "mcp-store-1"
        or source.get("external_locator") != ""
        or source.get("content_pointer")
        or source.get("speakers")
        or source.get("document")
        or source.get("calendar_events")
        or connector.get("type") != "ai_host"
        or request.get("status") != "saved"
        or request.get("source_id") != source.get("id")
        or request.get("relay")
        or request.get("about")
        or evidence.get("request_id") != request.get("id")
        or request.get("caller") != connector.get("provider")
        or source.get("id") not in episode.get("source_ids", [])
        or episode.get("connector_id") != connector.get("id")
    ):
        raise _refused()
    messages = _json(source.get("content"), MAX_TEXT)
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], dict)
        or messages[0].get("text") != DEMO_TEXT
        or messages[0].get("sender") is not None
    ):
        raise _refused()
    message = messages[0]
    if (
        evidence.get("message_key") not in (message.get("key"), source.get("id"))
        or _plain(evidence.get("source_time")) != message.get("at")
        or evidence.get("locator") != "whole"
    ):
        # The runner's published template falls back to source_id for a long message key.
        # This is unambiguous only because this source has exactly one approved message.
        # The restricted adapter accepts whole-message provenance only.
        raise _refused()
    screened = [r for r in bundle["screenings"] if r.get("source_id") == source["id"]]
    if (
        len(screened) != 1
        or screened[0].get("screened_where") != "server"
        or screened[0].get("screened_at") != source.get("received_at")
    ):
        raise _refused()
    receipt = _json(request.get("receipt"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("state") != "saved"
        or receipt.get("request_id") != request["id"]
        or not receipt.get("commit_id")
        or not receipt.get("change_set_id")
        or not isinstance(receipt.get("lines"), list)
    ):
        raise _refused()
    return source, request, receipt


def _validate(bundle: dict, world_id: str, now: datetime) -> list[dict]:
    for key, rows in bundle.items():
        cap = MAX_ITEMS if key == "items" else MAX_ROWS
        if not isinstance(rows, list) or len(rows) > cap:
            raise _refused()
    indexed = {
        key: _index(bundle[key])
        for key in ("values", "sources", "requests", "connectors", "episodes", "kinds")
    }
    output = []
    for item in bundle["items"]:
        if item.get("world_id") != world_id or item.get("version") != 1:
            raise _refused()
        evidence = [e for e in bundle["evidence"] if e.get("target_id") == item.get("id")]
        if not evidence or item.get("provenance_evidence_id") not in {e["id"] for e in evidence}:
            raise _refused()
        item_entry, initial = _creation(item, "item", bundle["entries"], indexed["values"])
        provenance = []
        for row in evidence:
            if (
                row.get("world_id") != world_id
                or row.get("target_type") != "item"
                or row.get("active") is not True
                or row.get("polarity") != "supports"
                or row.get("derivation") != "observed"
                or row.get("premises")
                or row.get("modality") != "text"
                or row.get("version") != 1
            ):
                raise _refused()
            entry, original = _creation(row, "evidence", bundle["entries"], indexed["values"])
            if original != _plain(row):
                raise _refused()
            source, request, receipt = _source(row, bundle, indexed)
            if any(
                e.get("request_id") != request["id"]
                or e.get("commit_id") != receipt["commit_id"]
                or e.get("change_set_id") != receipt["change_set_id"]
                for e in (item_entry, entry)
            ):
                raise _refused()
            if not any(
                line.get("symbol") == "+"
                and line.get("entity_type") == "item"
                and line.get("entity_id") == item["id"]
                for line in receipt["lines"]
            ):
                raise _refused()
            provenance.append(
                {
                    "source_id": source["id"],
                    "request_id": request["id"],
                    "message_key": row["message_key"],
                    "locator": row["locator"],
                    "source_time": _plain(row["source_time"]),
                    "source_type": row["source_type"],
                    "derivation": "observed",
                    "modality": "text",
                }
            )
        derived = {k: v for k, v in claim_fields(evidence).items() if v is not None}
        if {**initial, **derived} != _plain(item):
            raise _refused()
        if any(isinstance(v, str) and len(v) > MAX_TEXT for v in item.values()):
            raise _refused()
        kind = indexed["kinds"].get(item.get("kind"))
        if kind is None:
            raise _refused()
        properties = _json(item["properties"]) if item.get("properties") else {}
        scheduled = {
            p["scheduled_role"]: properties[p["name"]]
            for p in bundle["properties"]
            if p.get("kind") == item.get("kind") and p.get("name") in properties
        }
        successors = [
            Successor(link["to_item"], link["id"], link["meaning"])
            for link in bundle["successors"]
            if link.get("from_item") == item["id"]
        ]
        state = standing(
            item,
            at=now,
            lifecycle_class=kind.get("lifecycle_class"),
            planned_origin=any(e.get("source_type") == "planned" for e in evidence),
            scheduled=scheduled,
            successors=successors,
        )
        if not state.is_current:
            continue
        shown = {
            key: _plain(item[key])
            for key in ("id", "title", "summary", "body", "status", "recorded")
            if key in item
        }
        shown.update(
            {
                "standing": "current",
                "source_type": item["derived_source_type"],
                "provenance": provenance,
            }
        )
        if properties:
            shown["properties"] = properties
        output.append(shown)
    return output


def _arguments(arguments: dict) -> tuple[str | None, int]:
    if not isinstance(arguments, dict) or set(arguments) - {"about", "time", "limit"}:
        raise invalid_arguments(
            "this demo supports about, current time and limit only", "arguments"
        )
    about = arguments.get("about")
    if "about" in arguments and (
        not isinstance(about, str) or not about.strip() or len(about) > 256
    ):
        raise invalid_arguments("about must be bounded non-empty text", "about")
    if "time" in arguments and arguments["time"] != {"mode": "current"}:
        raise invalid_arguments("this demo supports current time only", "time")
    limit = arguments.get("limit", MAX_ITEMS)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise invalid_arguments("limit is outside the demo range", "limit")
    return about, limit


class DemoRecallHandler:
    def __call__(self, context: ToolContext, arguments: dict) -> dict:
        from grove.demo_policy import DemoDenied, require_designation

        if "grove.read" not in context.scopes:
            raise ApiError(403, "scope_denied", "the credential lacks the read scope")
        about, limit = _arguments(arguments)
        try:
            designation = require_designation(context.driver, context.world_id, context.user_id)
        except DemoDenied:
            raise ApiError(
                403, "demo_access_refused", "the restricted demo is not available"
            ) from None
        own = designation.get("self_item_id")
        if not own:
            raise _refused()
        now = clock.now(context.world_id)
        try:
            captured = WorldQuery(context.driver, context.world_id, timeout=5).read(
                _CAPTURE,
                own=own,
                item_cap=MAX_ITEMS + 1,
                row_cap=MAX_ROWS + 1,
                text_cap=MAX_TEXT,
                value_cap=MAX_VALUE,
                successors=list(SUCCESSOR_MEANINGS),
            )
            if len(captured) != 1:
                raise _refused()
            items = _validate(captured[0], context.world_id, now)
        except Neo4jError as error:
            if error.is_retryable():
                raise unavailable("the demo evidence could not be read") from None
            raise ApiError(500, "internal", "the demo evidence could not be read") from None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise _refused() from None
        # The complete restricted dataset is tiny. A loose topic match may return its facts,
        # while a miss only names the approved topic and does not assert a real-world absence.
        terms = set(re.findall(r"[a-z0-9]+", (about or "cedar").casefold()))
        matching = not about or bool(terms & {"cedar", "demo", "room", "presentation", "14", "utc"})
        total = len(items) if matching else 0
        shown = items[:limit] if matching else []
        return {
            "mode": "restricted_demo",
            "as_of": now.isoformat(),
            "answer": "truncated" if total > limit else "found" if shown else "no_record",
            "items": shown,
            "coverage": {
                "scope": "approved saved Cedar requests only",
                "returned": len(shown),
                "total": total,
                "truncated": total > limit,
                "live_state_checked": False,
            },
            "available_topics": ["Cedar demo"] if items else [],
            "note": "Grove returns saved evidence only. No record does not mean it did not happen.",
        }
