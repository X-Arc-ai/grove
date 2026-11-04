"""D6 explanation reads and post-commit work. Reads never call a model or enqueue work."""

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from grove.models.explain_break import ExplanationInputError, explain, template
from grove.models.layer import ModelLayer
from grove.screening import Screener
from grove.store.asof import decode_value
from grove.store.connection import Driver
from grove.store.current import read_rows
from grove.store.d3_fingerprint import read_once
from grove.store.primitives import MEANING_AFFECTED_BY
from grove.store.query import WorldQuery

JOB = "explain_break"
_GENERIC = "This item is affected by a change to another item."
_STATE = frozenset(
    {
        "title",
        "summary",
        "body",
        "properties",
        "status",
        "status_role",
        "status_since",
        "end_class",
        "became_true",
        "stopped_being_true",
        "recorded",
        "last_confirmed",
        "recheck_by",
        "became_true_earliest",
        "became_true_latest",
        "became_true_precision",
        "stopped_being_true_earliest",
        "stopped_being_true_latest",
        "stopped_being_true_precision",
    }
)


@dataclass(frozen=True)
class ExplanationRead:
    mark_id: str
    text: str = field(repr=False)
    by: str
    needs_explanation: bool


@dataclass(frozen=True)
class LoadedExplanation:
    mark: dict[str, Any]
    packet: dict[str, Any] | None = field(repr=False)
    cached: str | None = field(repr=False)


@dataclass(frozen=True)
class ExplanationReport:
    selected: int
    saved: int
    failed: int
    stale: int
    requested_job_id: str | None = None


def _json_time(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("explanation data is not JSON")


def packet_fingerprint(packet: dict[str, Any]) -> str:
    text = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _values(source: Any, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {}
    return {
        row["v"]["id"]: row["v"].get("value")
        for row in read_rows(source, "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v", ids=ids)
        if not row["v"].get("forgotten_at")
    }


def _states(source: Any, trigger: str, change_set: str) -> tuple[dict, dict]:
    bound = read_rows(
        source,
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id, "
        "change_set_id: $change_set}) RETURN max(e.sequence) AS last",
        id=trigger,
        change_set=change_set,
    )
    if not bound or bound[0]["last"] is None:
        return {}, {}
    entries = [
        row["e"]
        for row in read_rows(
            source,
            "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) "
            "WHERE e.sequence <= $last RETURN e ORDER BY e.sequence",
            id=trigger,
            last=bound[0]["last"],
        )
    ]
    values = _values(
        source,
        list(
            {
                row[key]
                for row in entries
                for key in ("old_value_id", "new_value_id")
                if row.get(key)
            }
        ),
    )
    state, old, changed = {}, None, {}
    for row in entries:
        field_name = row["field"]
        selected = row["change_set_id"] == change_set
        if selected and old is None:
            old = copy.deepcopy(state)
        value = decode_value(values.get(row.get("new_value_id")))
        if field_name == "row" and isinstance(value, dict):
            state = {key: val for key, val in value.get("row", {}).items() if key in _STATE}
            continue
        if field_name not in _STATE:
            continue
        if selected:
            changed[field_name] = value
            if field_name not in old:
                old[field_name] = decode_value(values.get(row.get("old_value_id")))
        if not row.get("history_only"):
            state[field_name] = value
    return old or {}, {**state, **changed}


def load_explanation(source: Any, mark_id: str) -> LoadedExplanation | None:
    """Scoped packet assembly for OneRead or a WorldTx held by the cache writer."""
    rows = read_rows(
        source,
        "MATCH (m:Impact {link_id: $id}) "
        "MATCH (a:Item)-[l:LINK {id: $id}]->(t:Item) "
        "WHERE l.meaning = $meaning RETURN m, l, a, t",
        id=mark_id,
        meaning=MEANING_AFFECTED_BY,
    )
    if not rows:
        return None
    row = rows[0]
    mark, link, affected, trigger = (row[key] for key in ("m", "l", "a", "t"))
    cached = (
        _values(source, [mark["explanation"]]).get(mark["explanation"])
        if mark.get("explanation")
        else None
    )
    path = mark.get("path") or []
    if not 1 <= len(path) <= 3 or mark.get("affected_item") != affected["id"]:
        return LoadedExplanation(mark, None, cached)
    links = {
        entry["l"]["id"]: entry["l"]
        for entry in read_rows(
            source,
            "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.id IN $ids RETURN l",
            ids=path,
        )
    }
    meanings = {
        entry["m"]["id"]: entry["m"]
        for entry in read_rows(
            source,
            "MATCH (m:Meaning) WHERE m.id IN $ids RETURN m",
            ids=list({entry["meaning"] for entry in links.values()}),
        )
    }
    path_ids, cursor = [trigger["id"]], trigger["id"]
    for link_id in path:
        edge = links.get(link_id)
        meaning = meanings.get(edge["meaning"]) if edge else None
        if not meaning or not meaning.get("carries_consequences"):
            return LoadedExplanation(mark, None, cached)
        downstream = meaning.get("downstream_end")
        upstream_key, downstream_key = (
            ("to_item", "from_item") if downstream == "from" else ("from_item", "to_item")
        )
        if downstream not in ("from", "to") or edge[upstream_key] != cursor:
            return LoadedExplanation(mark, None, cached)
        cursor = edge[downstream_key]
        if cursor in path_ids:
            return LoadedExplanation(mark, None, cached)
        path_ids.append(cursor)
    if cursor != affected["id"]:
        return LoadedExplanation(mark, None, cached)
    items = {
        entry["i"]["id"]: entry["i"]
        for entry in read_rows(
            source,
            "MATCH (i:Item) WHERE i.id IN $ids RETURN i",
            ids=path_ids,
        )
    }
    if len(items) != len(path_ids):
        return LoadedExplanation(mark, None, cached)
    old, new = _states(source, trigger["id"], mark["trigger_change_set"])
    trigger_title = new.get("title") or trigger.get("title")
    titles = {item_id: items[item_id].get("title") for item_id in path_ids}
    titles[trigger["id"]] = trigger_title
    descriptions = _values(
        source, [m["description"] for m in meanings.values() if m.get("description")]
    )
    packet = {
        "mark_id": mark_id,
        "trigger": {
            "id": trigger["id"],
            "title": trigger_title,
            "old": old,
            "new": new,
            "source_type": mark["trigger_source_type"],
            "clocks": {
                key: value
                for key, value in new.items()
                if key.startswith(("became_true", "stopped_being_true"))
                or key in ("recorded", "last_confirmed", "recheck_by")
            },
            "world_time": link.get("became_true_earliest") or link.get("became_true_latest"),
        },
        "affected": {"id": affected["id"], "title": titles[affected["id"]]},
        "path_items": [{"id": item_id, "title": titles[item_id]} for item_id in path_ids],
        "meanings": [
            {
                "id": links[link_id]["meaning"],
                "name": meanings[links[link_id]["meaning"]].get("name"),
                "description": descriptions.get(
                    meanings[links[link_id]["meaning"]].get("description")
                ),
            }
            for link_id in path
        ],
    }
    packet = json.loads(json.dumps(packet, default=_json_time, ensure_ascii=False))
    try:
        template(packet)
    except ExplanationInputError:
        packet = None
    return LoadedExplanation(mark, packet, cached)


def read_explanation(
    driver: Driver, world_id: str, mark_id: str, *, screener: Screener | None = None
) -> ExplanationRead | None:
    """Read cached text or a template. This function performs no writes or network calls."""
    loaded = read_once(driver, world_id, lambda source: load_explanation(source, mark_id))
    if loaded is None:
        return None
    if loaded.cached is not None:
        return ExplanationRead(
            mark_id, loaded.cached, loaded.mark.get("explained_by", "spark"), False
        )
    text = template(loaded.packet, screener=screener) if loaded.packet else _GENERIC
    return ExplanationRead(
        mark_id, text, "template", loaded.packet is not None and not loaded.mark.get("explanation")
    )


def explain_pending(
    layer: ModelLayer,
    driver: Driver,
    world_id: str,
    *,
    limit: int = 20,
    worker: str = "cascade-explanations",
    screener: Screener | None = None,
) -> ExplanationReport:
    """Run after commit: requested jobs first, then proposal marks; no model call holds a lock."""
    from grove.writer.cascade_explanations import cache_explanation
    from grove.writer.runs import claim_job, end_job

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("explanation limit must be from 1 to 100")
    job = claim_job(driver, world_id, agent=JOB, worker=worker)
    requested = json.loads(job.get("input_references") or "[]") if job else []
    if requested:
        pending = {
            row["id"]
            for row in WorldQuery(driver, world_id).read(
                "MATCH (m:Impact) WHERE m.link_id IN $ids AND m.explanation IS NULL "
                "RETURN m.link_id AS id",
                ids=requested,
            )
        }
        requested = [mark_id for mark_id in requested if mark_id in pending]
    proposals = WorldQuery(driver, world_id).read(
        "MATCH (m:Impact) WHERE m.proposal_id IS NOT NULL AND m.explanation IS NULL "
        "AND m.state <> 'undone' RETURN m.link_id AS id ORDER BY m.touched_at, m.link_id "
        "LIMIT $limit",
        limit=limit,
    )
    selected = list(dict.fromkeys([*requested, *(row["id"] for row in proposals)]))[:limit]
    saved = failed = stale = 0
    failed_requested = False
    for mark_id in selected:
        loaded = read_once(
            driver, world_id, lambda source, mark_id=mark_id: load_explanation(source, mark_id)
        )
        if loaded is None or loaded.mark.get("explanation") is not None or loaded.packet is None:
            continue
        result = explain(layer, world_id, loaded.packet, screener=screener)
        if result.by != "spark":
            failed += 1
            failed_requested |= mark_id in requested
            continue
        stored = cache_explanation(
            driver,
            world_id,
            mark_id,
            result,
            expected_version=loaded.mark.get("version", 0),
            expected_packet=packet_fingerprint(loaded.packet),
            screener=screener,
        )
        saved += stored == "saved"
        failed += stored == "refused"
        stale += stored == "stale"
        failed_requested |= stored in ("stale", "refused") and mark_id in requested
    if job:
        remaining = any(mark_id not in selected for mark_id in requested)
        end_job(
            driver,
            world_id,
            job["id"],
            worker=worker,
            done=not (remaining or failed_requested),
            requeue=remaining and not failed_requested,
        )
    return ExplanationReport(len(selected), saved, failed, stale, job["id"] if job else None)
