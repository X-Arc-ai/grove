"""What the `brain` verbs read, for one pass of one world.

Every answer holds ids and codes as JSON, and the stored text (titles, summaries, the words of a
source) only in `data`: blocks rendered by `grove.prompts.render_blocks`, so an agent reads them
under the data rules and never as instructions (D14). Each read goes through `WorldQuery`, so it
sees only the pass's world.
"""

import json
from typing import Any

from grove.prompts import DataBlock, render_blocks
from grove.store.enums import ENUMS
from grove.store.query import WorldQuery
from grove.views.registry import read_registry
from grove.writer.provenance import LOCATOR_SCHEMES, connector_type_of

RECALL_LIMIT = 20
_SPEAKERS = frozenset(ENUMS["speaker_kind"])


def _world(driver: Any, world_id: str) -> tuple[WorldQuery, WorldQuery]:
    return WorldQuery(driver, world_id), WorldQuery.primitives(driver)


def status(driver: Any, world_id: str, record: dict[str, Any]) -> dict[str, Any]:
    query = WorldQuery(driver, world_id)
    items = query.read("MATCH (i:Item) RETURN count(i) AS n")[0]["n"]
    links = query.read(
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.active_key IS NOT NULL RETURN count(l) AS n"
    )[0]["n"]
    requests = {
        row["status"]: row["n"]
        for row in query.read("MATCH (r:Request) RETURN r.status AS status, count(r) AS n")
    }
    return {
        "world_id": world_id,
        "run_id": record["id"],
        "agent": record["agent"],
        "inputs": list(record.get("input_ids") or []),
        "items": items,
        "active_links": links,
        "requests": requests,
        "next": "Run brain pass to read this pass's input.",
    }


def _messages(content: object) -> list[dict[str, Any]]:
    if not isinstance(content, str):
        return []
    try:
        rows = json.loads(content)
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _speaker(connector_type: str, verbatim: object) -> str:
    """The speaker a block carries. A host's words are the person's own only when the host
    marked them verbatim; otherwise they are the host's account. ingest-b owns the speakers of
    connector sources (a chat's sender is not always the person)."""
    if connector_type == "person_in_views":
        return "person"
    if connector_type == "ai_host":
        return "person" if verbatim is True else "ai_host"
    return "third_party"


def _request_input(query: WorldQuery, request_id: str, n0: int) -> tuple[dict, list[DataBlock]]:
    row = query.read(
        "MATCH (r:Request {id: $id}) RETURN r.status AS status, r.source_id AS source, "
        "r.verbatim AS verbatim, r.said_at AS said_at",
        id=request_id,
    )[0]
    source = query.read(
        "MATCH (s:Source {id: $id}) RETURN s.connector_id AS connector, s.content AS content, "
        "s.received_at AS received",
        id=row["source"],
    )[0]
    connector = query.read("MATCH (c:Connector {id: $id}) RETURN c", id=source["connector"])
    connector_type = connector_type_of(connector[0]["c"]) if connector else "unknown"
    episode = query.read(
        "MATCH (e:Episode) WHERE $source IN e.source_ids RETURN e.id AS id ORDER BY e.id LIMIT 1",
        source=row["source"],
    )
    messages = _messages(source["content"])
    blocks: list[DataBlock] = []
    shown = []
    for message in messages:
        text = message.get("text")
        if not isinstance(text, str) or not text:
            continue
        speaker = _speaker(connector_type, row.get("verbatim"))
        blocks.append(DataBlock("source", speaker, None, row["source"], text))
        shown.append(
            {
                "block": n0 + len(blocks),
                "message_key": message.get("key"),
                "at": message.get("at"),
                "speaker_kind": speaker,
            }
        )
    content = source["content"] or ""
    entry = {
        "request_id": request_id,
        "status": row["status"],
        "source_id": row["source"],
        "episode_id": episode[0]["id"] if episode else None,
        "connector_type": connector_type,
        "locators": sorted(LOCATOR_SCHEMES.get(connector_type, ())),
        "source_characters": len(content),
        "received_at": source["received"].isoformat() if source.get("received") else None,
        "said_at": row["said_at"].isoformat() if row.get("said_at") else None,
        "messages": shown,
    }
    return entry, blocks


def pass_input(driver: Any, world_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """The input packet (3.5 rule 3): the pass's requests or job, and the batch form."""
    query = WorldQuery(driver, world_id)
    blocks: list[DataBlock] = []
    entries = []
    job = None
    if record["agent"] == "learner":
        for request_id in record.get("input_ids") or []:
            entry, more = _request_input(query, request_id, len(blocks))
            entries.append(entry)
            blocks.extend(more)
    else:
        job_id = (record.get("input_ids") or [None])[0]
        rows = query.read("MATCH (j:Job {id: $id}) RETURN j", id=job_id) if job_id else []
        if rows:
            job_row = rows[0]["j"]
            job = {
                "job_id": job_row["id"],
                "trigger": job_row.get("trigger"),
                "items": json.loads(job_row.get("input_references") or "[]"),
            }
    kinds, meanings = vocabulary(query)
    return {
        "world_id": world_id,
        "run_id": record["id"],
        "agent": record["agent"],
        "requests": entries,
        "job": job,
        "kinds": kinds,
        "meanings": meanings,
        "statement_classes": list(ENUMS["statement_class"]),
        "batch_form": batch_form(entries[0] if entries else None),
        "data": render_blocks(blocks),
    }


FIELD_GUIDE = {
    "create_item": "op_id, kind, title, became_true, evidence_refs, reason; optional temp_id "
    "(a new id that starts with the dollar sign, for later ops of the batch), summary, status "
    "(required for task, plan, problem and assumption), recheck_by (required with a live status)",
    "create_link": "op_id, from, meaning, to, became_true, evidence_refs, reason; from and to are "
    "item ids or temp ids of this batch; meaning is one of meanings, and its ends take the kinds "
    "it lists; recheck_by (an ISO time, such as a week ahead) when the meaning has "
    "needs_recheck_by",
    "update_item": "op_id, item, version, fields, evidence_refs, reason",
    "change_status": "op_id, item, status, evidence_refs, reason",
    "became_true": "unknown, or earliest, latest, precision (minute, hour, day, month, year) and "
    "words",
    "evidence_refs": "a list of ev_id (an evidence row of this batch) or evidence_id (a row that "
    "exists), each with polarity supports or contradicts",
}


def _ends(raw: object, kinds_by_id: dict[str, dict], by_class: dict[str, list[str]]) -> object:
    """A meaning's from or to side as kind names: "any", or the kinds it takes."""
    try:
        side = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        side = {}
    if not isinstance(side, dict) or side.get("any"):
        return "any"
    names = {kinds_by_id[k]["name"] for k in side.get("kinds") or [] if k in kinds_by_id}
    for lifecycle in side.get("classes") or []:
        names.update(by_class.get(lifecycle, []))
    return sorted(names)


def vocabulary(query: WorldQuery) -> tuple[list[dict], list[dict]]:
    """The world's own kinds (with their lifecycle class and status words) and meanings (with
    the kinds each end takes), so an agent picks a meaning that fits before the writer checks."""
    kinds = {
        row["id"]: row
        for row in query.read(
            "MATCH (k:Kind) WHERE k.status = 'canonical' RETURN k.id AS id, k.name AS name, "
            "k.lifecycle_class AS class, k.status_vocabulary AS vocabulary"
        )
    }
    by_class: dict[str, list[str]] = {}
    for row in kinds.values():
        by_class.setdefault(row["class"], []).append(row["name"])
    kind_list = []
    for row in sorted(kinds.values(), key=lambda r: r["name"]):
        try:
            words = [entry.get("status") for entry in json.loads(row["vocabulary"] or "[]")]
        except (ValueError, AttributeError):
            words = []
        kind_list.append({"name": row["name"], "class": row["class"], "statuses": words})
    meanings = [
        {
            "name": row["name"],
            "from": _ends(row["from_kinds"], kinds, by_class),
            "to": _ends(row["to_kinds"], kinds, by_class),
            # The writer refuses a link of a state meaning with no re-check date (missing_recheck).
            "needs_recheck_by": row["temporal_class"] == "state",
        }
        for row in query.read(
            "MATCH (m:Meaning) WHERE m.status = 'canonical' RETURN m.name AS name, "
            "m.from_kinds AS from_kinds, m.to_kinds AS to_kinds, "
            "m.temporal_class AS temporal_class ORDER BY m.name"
        )
    ]
    return kind_list, meanings


def batch_form(entry: dict[str, Any] | None) -> dict[str, Any]:
    """A filled example of one batch for the first request: code writes it, so it is no slot in
    an instruction file. The agent replaces the kinds, the titles, the meaning, the clocks and
    the reasons, and drops what the source does not state."""
    if entry is None:
        return {
            "note": "Cite an evidence row that already exists with evidence_id; read it with "
            "brain get.",
            "fields": FIELD_GUIDE,
        }
    message = entry["messages"][0] if entry["messages"] else {"message_key": None, "at": None}
    locator = "whole" if "whole" in entry["locators"] else (entry["locators"] or ["whole"])[0]
    support = [{"ev_id": "e1", "polarity": "supports"}]
    return {
        "request_id": entry["request_id"],
        "fields": FIELD_GUIDE,
        "batch": {
            "evidence": [
                {
                    "ev_id": "e1",
                    "episode_id": entry["episode_id"],
                    "source_id": entry["source_id"],
                    "locator": locator,
                    "message_key": message["message_key"] or entry["source_id"],
                    "source_time": message["at"] or entry["said_at"] or entry["received_at"],
                    "statement_class": "report_state",
                    "derivation": "observed",
                    "speaker_kind": message.get("speaker_kind", "person"),
                    "modality": "text",
                }
            ],
            "ops": [
                {
                    "op": "create_item",
                    "op_id": "o1",
                    "temp_id": "$i1",
                    "kind": "person",
                    "title": "The name as the source gives it",
                    "became_true": "unknown",
                    "evidence_refs": support,
                    "reason": "what in the source says it",
                },
                {
                    "op": "create_item",
                    "op_id": "o2",
                    "temp_id": "$i2",
                    "kind": "place",
                    "title": "A second thing the source names",
                    "became_true": "unknown",
                    "evidence_refs": support,
                    "reason": "what in the source says it",
                },
                {
                    "op": "create_link",
                    "op_id": "o3",
                    "from": "$i1",
                    "meaning": "related to",
                    "to": "$i2",
                    "became_true": "unknown",
                    "evidence_refs": support,
                    "reason": "what in the source relates the two",
                },
            ],
        },
    }


def recall(driver: Any, world_id: str, text: str, *, limit: int = RECALL_LIMIT) -> dict[str, Any]:
    """A lexical lookup over titles, summaries and search terms. No model call (D7). hosts-b
    replaces this with D7's recall; the answer keeps ids and codes outside `data`."""
    query, primitives = _world(driver, world_id)
    registry = read_registry(query, primitives)
    hits = query.search("grove_items_lexical", text, limit=limit, fuzzy=True)
    found, blocks = [], []
    for hit in hits:
        node = hit["node"]
        title = node.get("title") if isinstance(node.get("title"), str) else ""
        blocks.append(DataBlock("item", "automated", None, node["id"], title))
        found.append(
            {
                "block": len(blocks),
                "item_id": node["id"],
                "kind": registry.kind_name(node.get("kind")),
                "status": node.get("status"),
                "version": node.get("version"),
                "score": round(float(hit["score"]), 3),
            }
        )
    return {"query_characters": len(text), "matches": found, "data": render_blocks(blocks)}


def get_item(driver: Any, world_id: str, item_id: str) -> dict[str, Any] | None:
    query, primitives = _world(driver, world_id)
    rows = query.read("MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    if not rows:
        return None
    item = rows[0]["i"]
    registry = read_registry(query, primitives)
    blocks = [DataBlock("item", "automated", None, item["id"], item.get("title") or "")]
    if isinstance(item.get("summary"), str) and item["summary"]:
        blocks.append(DataBlock("item", "automated", None, item["id"], item["summary"]))
    links = []
    for row in query.read(
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE (a.id = $id OR b.id = $id) "
        "AND l.active_key IS NOT NULL "
        "RETURN l.id AS id, l.meaning AS meaning, a.id AS from_item, b.id AS to_item, "
        "l.version AS version",
        id=item_id,
    ):
        name, _ = registry.meaning(row["meaning"])
        links.append({**row, "meaning": name})
    evidence = query.read(
        "MATCH (e:Evidence {target_id: $id}) WHERE e.active = true "
        "RETURN e.id AS evidence_id, e.polarity AS polarity, e.source_type AS source_type, "
        "e.statement_class AS statement_class, e.source_id AS source_id ORDER BY e.id",
        id=item_id,
    )
    return {
        "item_id": item["id"],
        "kind": registry.kind_name(item.get("kind")),
        "status": item.get("status"),
        "version": item.get("version"),
        "end_class": item.get("end_class"),
        "links": links,
        "evidence": evidence,
        "data": render_blocks(blocks),
    }
