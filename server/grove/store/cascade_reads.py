"""D6 read adapters. The planner has no store or clock access."""

import json
from datetime import datetime
from typing import Any

from grove.store.current import read_rows
from grove.store.primitives import PRIMITIVE_MEANINGS
from grove.writer.registry import Registry


def snapshot(source: Any, trigger_ids: list[str], *, now: datetime, registry=None) -> dict:
    """Load the bounded consequence neighbourhood through the endpoint indexes.

    The last frontier's incident links are needed to decide whether a direct dependent still
    has another live upstream. They never extend the planner's three-hop reach.
    """
    registry = registry or Registry.load(source)
    meanings = {**registry.meanings, **PRIMITIVE_MEANINGS}
    consequence = [key for key, row in meanings.items() if row.get("carries_consequences")]
    reached = set(trigger_ids)
    frontier = set(trigger_ids)
    links: dict[str, dict] = {}
    for _ in range(4):
        if not frontier or not consequence:
            break
        rows = read_rows(
            source,
            "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.meaning IN $meanings "
            "AND (l.from_item IN $ids OR l.to_item IN $ids) "
            "AND l.active_key IS NOT NULL RETURN l ORDER BY l.id",
            meanings=consequence,
            ids=sorted(frontier),
        )
        following = set()
        for found in rows:
            link = found["l"]
            links[link["id"]] = link
            meaning = meanings[link["meaning"]]
            downstream = "from_item" if meaning.get("downstream_end") == "from" else "to_item"
            upstream = "to_item" if downstream == "from_item" else "from_item"
            if link[upstream] in frontier:
                following.add(link[downstream])
        frontier = following - reached
        reached.update(following)
    # Retained R1 history supplies reopen proposals; it does not restore links by itself.
    ended = []
    for found in read_rows(
        source,
        "MATCH (e:ChangeEntry {field: 'cascade_end'}) "
        "MATCH (v:HistoryValue {id: e.new_value_id}) RETURN e.entity_id AS id, v.value AS value",
    ):
        detail = json.loads(found["value"])
        if detail["trigger_id"] in trigger_ids:
            ended.append({"link_id": found["id"], "trigger_id": detail["trigger_id"]})
    missing = [row["link_id"] for row in ended if row["link_id"] not in links]
    if missing:
        for found in read_rows(
            source, "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.id IN $ids RETURN l", ids=missing
        ):
            links[found["l"]["id"]] = found["l"]
    ids = reached | {link[key] for link in links.values() for key in ("from_item", "to_item")}
    items = {
        found["i"]["id"]: found["i"]
        for found in read_rows(
            source, "MATCH (i:Item) WHERE i.id IN $ids RETURN i", ids=sorted(ids)
        )
    }
    latest, prior = {}, {}
    rows = read_rows(
        source,
        "MATCH (e:ChangeEntry {entity_type: 'item'}) WHERE e.entity_id IN $ids "
        "AND e.history_only = false RETURN e ORDER BY e.sequence",
        ids=sorted(ids),
    )
    values = {}
    value_ids = [
        r["e"].get("new_value_id")
        for r in rows
        if r["e"]["field"] in ("row", "status", "status_role")
    ]
    if value_ids:
        values = {
            r["id"]: json.loads(r["value"])
            for r in read_rows(
                source,
                "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
                ids=value_ids,
            )
        }
    status = {}
    for found in rows:
        entry = found["e"]
        item_id, field = entry["entity_id"], entry["field"]
        at = entry.get("world_time")
        if at is not None and field in (
            "row",
            "title",
            "body",
            "summary",
            "properties",
            "status",
            "end_class",
        ):
            latest[item_id] = max(latest.get(item_id, at), at)
        if field == "row":
            created = (values.get(entry.get("new_value_id")) or {}).get("row", {})
            status[item_id] = created.get("status")
            if (
                created.get("status_role") in ("open", "active", "reported_done")
                and status[item_id]
            ):
                prior[item_id] = {"status": status[item_id], "role": created["status_role"]}
        elif field == "status":
            status[item_id] = values.get(entry.get("new_value_id"))
        elif field == "status_role":
            role = values.get(entry.get("new_value_id"))
            if role in ("open", "active", "reported_done") and status.get(item_id):
                prior[item_id] = {"status": status[item_id], "role": role}
    return {
        "items": items,
        "links": list(links.values()),
        "meanings": meanings,
        "kinds": registry.kinds,
        "now": now,
        "latest_change": latest,
        "prior_status": prior,
        "r1_endings": ended,
    }


def what_it_breaks(source: Any, item_id: str, *, hypothetical: dict | None = None, now=None):
    """Return stored consequences, or the same pure plan the writer applies. Never writes."""
    from grove.cascade import plan

    if hypothetical is not None:
        if now is None:
            raise ValueError("simulation requires the caller's world clock")
        at = now
        held = snapshot(source, [item_id], now=at)
        if item_id not in held["items"]:
            raise ValueError("no item in this world")
        change = {**hypothetical, "item_id": item_id}
        return plan(held, [change])
    rows = read_rows(
        source,
        "MATCH (a:Item)-[l:LINK]->(t:Item {id: $id}) MATCH (m:Impact {link_id: l.id}) RETURN m, l",
        id=item_id,
    )
    marks = [r["m"] for r in rows if r["m"]["state"] in ("open", "proposed", "applied")]
    proposals = []
    for found in read_rows(source, "MATCH (p:CascadeProposal {state: 'open'}) RETURN p"):
        row = found["p"]
        if json.loads(row.get("detail") or "{}").get("trigger_id") == item_id:
            proposals.append(row)
    return {"marks": marks, "proposals": proposals}
