"""Value-free import progress projected from retained legacy rows.

No write, model call, percentage, or host-product inference occurs here.
The provider scan is bounded; an incomplete read is unavailable, never zero.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime

from grove import clock
from grove.ingest.import_parts import IMPORT_PROVIDERS, MAX_PART_BYTES, input_status
from grove.learner_input import select_input
from grove.oauth.store import list_grants
from grove.store.query import PlatformQuery, WorldQuery

MAX_PROGRESS_SOURCES = 4096
READ_PAGE = 128
GRAPH_FIELDS = ("items_created", "items_changed", "links_created", "links_changed")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class ProgressUnavailable(RuntimeError):
    """The complete bounded status cannot be read safely."""


class ProgressSelectionDenied(PermissionError):
    """No current owner grant authorizes this exact connection selection."""


def _selected_host(driver, world_id, user_id, client_id):
    if client_id is None:
        raise ProgressUnavailable("muse_selection_required")
    try:
        bounded = isinstance(client_id, str) and bool(client_id) and len(client_id.encode("utf-8")) <= 2048
    except UnicodeError:
        bounded = False
    if not bounded:
        raise ProgressSelectionDenied("connection_selection_denied")
    if not any(
        grant.client_id == client_id and grant.user_id == user_id and "grove.read" in grant.scopes
        for grant in list_grants(driver, world_id)
    ):
        raise ProgressSelectionDenied("connection_selection_denied")
    # Exactly mcp.store._digest, including the JSON string framing.
    encoded = json.dumps(client_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return "host:" + digest


def _generation(driver, world_id):
    rows = PlatformQuery(driver).read(
        "MATCH (w:World {id: $id}) RETURN w.read_generation AS generation", id=world_id,
    )
    if len(rows) != 1 or type(rows[0]["generation"]) is not int or rows[0]["generation"] < 0:
        raise ProgressUnavailable("progress_unavailable")
    return rows[0]["generation"]


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else "request_failed"


def _empty_graph():
    return dict.fromkeys(GRAPH_FIELDS, 0)


def graph_contribution(query, request: dict) -> tuple[dict, int]:
    """Count committed ChangeEntry identities belonging to this exact Request."""
    if not request.get("commit_id") or not request.get("change_set_id"):
        return _empty_graph(), 0
    return _contributions(query, [request]).get(request["id"], (_empty_graph(), 0))


def _contributions(query, requests):
    saved = {r["id"]: r for r in requests if r.get("status") == "saved" and r.get("commit_id")}
    if not saved:
        return {}
    rows = query.read(
        "MATCH (e:ChangeEntry) WHERE e.request_id IN $ids "
        "RETURN e.request_id AS request_id, e.commit_id AS commit_id, e.change_set_id AS change_set_id, "
        "count(e) AS effects, "
        "count(DISTINCT CASE WHEN e.entity_type = 'item' AND e.field = 'row' THEN e.entity_id END) AS items_created, "
        "count(DISTINCT CASE WHEN e.entity_type = 'item' AND e.field <> 'row' THEN e.entity_id END) AS items_changed, "
        "count(DISTINCT CASE WHEN e.entity_type = 'link' AND e.field = 'row' THEN e.entity_id END) AS links_created, "
        "count(DISTINCT CASE WHEN e.entity_type = 'link' AND e.field <> 'row' THEN e.entity_id END) AS links_changed",
        ids=list(saved),
    )
    return {
        row["request_id"]: ({key: row[key] for key in GRAPH_FIELDS}, row["effects"])
        for row in rows
        if row["commit_id"] == saved[row["request_id"]].get("commit_id")
        and row["change_set_id"] == saved[row["request_id"]].get("change_set_id")
    }


def source_state(source: dict, connector: dict, request: dict | None, now, *, effects=0, import_profile=True):
    """Return (state, safe reason, retry time); saved alone is never completion proof."""
    if import_profile:
        reason = input_status(source, connector, request)
    else:
        selected = select_input(source, connector, request or {})
        if source.get("forget_state") in {"forgotten", "span_forgotten"}:
            reason = "source_unavailable"
        elif not selected.complete:
            reason = selected.reasons[0] if selected.reasons else "source_unavailable"
        else:
            reason = None if selected.messages else "kept_out"
    if reason == "source_unavailable":
        return "unavailable", reason, None
    if request is None:
        if reason == "kept_out":
            return "kept_out", None, None
        return "held", reason or "not_scheduled", None
    error = request.get("last_error_code") or ""
    if error.startswith("import_hold:"):
        return "held", _code(error.removeprefix("import_hold:")), None
    if reason not in (None, "kept_out"):
        return "held", reason, None
    status = request.get("status")
    if status == "saved":
        if effects:
            return "extracted", None, None
        try:
            receipt = json.loads(request.get("receipt") or "null")
        except (ValueError, TypeError):
            receipt = None
        if (
            isinstance(receipt, dict) and receipt.get("state") == "saved"
            and {"commit_id", "change_set_id"} <= receipt.keys()
            and receipt.get("request_id") == request["id"] and receipt.get("lines") == []
            and receipt.get("commit_id") is None and request.get("commit_id") is None
            and receipt.get("change_set_id") is None and request.get("change_set_id") is None
        ):
            return "held_nothing", None, None
        return "unavailable", "completion_unavailable", None
    if status == "declined":
        return "failed", _code(error), None
    retry = request.get("retry_at")
    if status == "failed" and retry is None:
        return "failed", _code(error), None
    if retry is not None and retry > now:
        return "retry_wait", _code(error) if error else None, _iso(retry)
    if status == "applying" and request.get("lease_until") is not None and request["lease_until"] > now:
        return "processing", None, None
    if status in {"queued", "applying", "failed"}:
        return "queued", _code(error) if error else None, _iso(retry)
    return "unavailable", "request_state_unknown", None


def _aggregate(states: Counter) -> str:
    blocked = sum(states[key] for key in ("held", "failed", "unavailable"))
    moving = sum(states[key] for key in ("queued", "processing", "retry_wait"))
    complete = states["extracted"] + states["held_nothing"]
    if blocked and (moving or complete):
        return "partial"
    for state in ("processing", "queued", "retry_wait"):
        if states[state]:
            return state
    if states["unavailable"]:
        return "unavailable"
    if states["failed"]:
        return "partial" if states["held"] else "failed"
    if states["held"]:
        return "held"
    return "complete" if complete else "idle"


def read_import_progress(driver, world_id: str, provider: str, *, after=None, limit=50, client_id=None, user_id=None) -> dict:
    if provider not in IMPORT_PROVIDERS | {"muse"} or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid import progress selection")
    muse = provider == "muse"
    if not muse and client_id is not None:
        raise ValueError("connection selection is only valid for Muse")
    selected_provider = _selected_host(driver, world_id, user_id, client_id) if muse else provider
    connector_type = "ai_host" if muse else "past_ai_conversations"
    caller = selected_provider if muse else "import:" + provider
    query = WorldQuery(driver, world_id)
    generation = _generation(driver, world_id)
    # A vanished source or connector must not turn queued work into an idle zero.
    missing = query.read(
        "MATCH (r:Request {caller: $caller}) WHERE r.protocol IS NULL "
        "AND r.source_id IS NOT NULL AND NOT EXISTS { "
        "MATCH (s:Source {id: r.source_id}) "
        "MATCH (c:Connector {id: s.connector_id, type: $type, provider: $provider}) } "
        "RETURN r.id AS id LIMIT 1", caller=caller, provider=selected_provider, type=connector_type,
    )
    if missing:
        raise ProgressUnavailable("source_unavailable")
    connectors = query.read(
        "MATCH (c:Connector {type: $type, provider: $provider}) "
        "RETURN c.id AS id, c.type AS type, c.provider AS provider", provider=selected_provider, type=connector_type,
    )
    by_connector = {row["id"]: row for row in connectors}
    cursor = ""
    now = clock.now(world_id)
    states = Counter()
    graph = _empty_graph()
    selected = []
    source_total = request_total = 0
    oldest = None
    while by_connector:
        rows = query.read(
            "MATCH (s:Source) WHERE s.connector_id IN $connectors AND s.id > $cursor "
            "RETURN s.id AS id, s.connector_id AS connector_id, "
            "substring(s.content, 0, $cap) AS content, s.received_at AS received_at, "
            "substring(s.speakers, 0, $cap) AS speakers, s.parser_version AS parser_version, "
            "s.forget_state AS forget_state ORDER BY s.id LIMIT $limit",
            connectors=list(by_connector), cursor=cursor,
            cap=4 * MAX_PART_BYTES + 4096 if muse else MAX_PART_BYTES + 1, limit=READ_PAGE,
        )
        if not rows:
            break
        requests = query.read(
            "MATCH (r:Request {caller: $caller}) WHERE r.source_id IN $ids AND r.protocol IS NULL RETURN r",
            caller=caller, ids=[source["id"] for source in rows],
        )
        by_request = {row["r"]["source_id"]: row["r"] for row in requests}
        if len(by_request) != len(requests):
            raise ProgressUnavailable("request_source_ambiguous")
        contributions = _contributions(query, list(by_request.values()))
        for source in rows:
            source_total += 1
            if source_total > MAX_PROGRESS_SOURCES:
                raise ProgressUnavailable("progress_limit")
            cursor = source["id"]
            request = by_request.get(source["id"])
            request_id = request["id"] if request else None
            if request is not None and (
                request.get("source_id") != source["id"]
                or request.get("caller") != caller
                or (not muse and request_id != "import:" + source["id"])
            ):
                raise ProgressUnavailable("request_source_mismatch")
            request_total += int(request is not None)
            contribution, effects = contributions.get(request_id, (_empty_graph(), 0))
            state, reason, retry = source_state(
                source, by_connector[source["connector_id"]], request, now, effects=effects, import_profile=not muse,
            )
            states[state] += 1
            if state in {"unavailable", "held"}:
                contribution = _empty_graph()
            for key in graph:
                graph[key] += contribution[key]
            if state not in {"extracted", "held_nothing", "kept_out"}:
                at = source.get("received_at")
                if at is not None and (oldest is None or at < oldest):
                    oldest = at
            if (after is None or source["id"] > after) and len(selected) <= limit:
                selected.append({
                    "source_id": source["id"], "connector_id": source["connector_id"],
                    "request_id": request_id if request else None, "state": state,
                    "reason_code": reason, "retry_at": retry, "graph": contribution,
                })
    if _generation(driver, world_id) != generation:
        raise ProgressUnavailable("progress_changed")
    if muse:
        # Revocation during the paged read invalidates the entire answer.
        _selected_host(driver, world_id, user_id, client_id)
    return {
        "contract": "import_progress.v1", "provider": provider, "state": _aggregate(states),
        "sources_total": source_total, "requests_total": request_total,
        "queued_count": states["queued"], "processing_count": states["processing"],
        "retry_wait_count": states["retry_wait"], "extracted_count": states["extracted"],
        "held_nothing_count": states["held_nothing"], "held_count": states["held"],
        "failed_count": states["failed"], "oldest_unprocessed_item": _iso(oldest),
        "graph": graph, "sources": selected[:limit],
        "next": selected[limit - 1]["source_id"] if len(selected) > limit else None,
    }
