"""The view payloads: graph, node, schema, health and session, in the simulation's shapes.

as_of and known_as_of filter the current head of each row: known_as_of=T keeps rows recorded at or
before T; as_of=T keeps rows whose world interval holds T. The history reads that answer both
(grove.store.asof.world_as_of, the record axis's known_as_of) are wired to the slider by views-c.

Every node carries its standing now and its successors (D3: no read serves an overtaken claim
without its successor), from grove.store.current, the one liveness function.
"""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grove.store.current import Standing, standings_of
from grove.store.query import WorldQuery
from grove.views.registry import Registry, read_registry
from grove.views.session import ViewSession
from grove.views.times import instant, iso_z

STATUS_FIELD = "status"
SOURCE_FIELD = "store"
READY = "ready"
HEALTHY = "healthy"
SUMMARY_READY = "Your Grove is ready"
SUMMARY_EMPTY = "Your Grove is empty"

# The graph reads only what node_data, edge_data, the time filters and the standing use: never an
# item's body, search terms or property values. world_id rides along so the WorldLeak guard checks
# each row.
_INTERVAL_FIELDS = (
    "became_true_earliest",
    "became_true_latest",
    "stopped_being_true_earliest",
    "stopped_being_true_latest",
    "recorded",
)
_ITEM_FIELDS = (
    "id",
    "world_id",
    "title",
    "kind",
    "summary",
    "status",
    "status_since",
    "touched_at",
    "last_confirmed",
    "version",
    "properties",
    *_INTERVAL_FIELDS,
    # What grove.store.current.standing reads.
    "end_class",
    "status_role",
    "stopped_being_true_precision",
    "became_true_precision",
    "derived_source_type",
    "derivation",
)
_LINK_FIELDS = (
    "id",
    "world_id",
    "from_item",
    "to_item",
    "meaning",
    "active_key",
    *_INTERVAL_FIELDS,
)


def _projection(variable: str, fields: tuple[str, ...]) -> str:
    return ", ".join(f"{variable}.{name} AS {name}" for name in fields)


_ITEMS = "MATCH (i:Item) RETURN " + _projection("i", _ITEM_FIELDS)
_ITEM = "MATCH (i:Item {id: $id}) RETURN i"
_ITEM_ID = "MATCH (i:Item {id: $id}) RETURN i.id AS id"
_ANOTHER_ITEM = "MATCH (i:Item) WHERE i.id <> $own RETURN i.id AS id LIMIT 1"
_LINK_MATCH = "MATCH (a:Item)-[l:LINK]->(b:Item) "
_LINKS_RETURN = "RETURN " + _projection("l", _LINK_FIELDS)
# Without as_of only active links show, so the ended ones stay in the store.
_ACTIVE_LINKS = _LINK_MATCH + "WHERE l.active_key IS NOT NULL " + _LINKS_RETURN
_ALL_LINKS = _LINK_MATCH + _LINKS_RETURN
_POSITIONS = "MATCH (p:MapPosition) RETURN p.item_id AS item_id, p.x AS x, p.y AS y"
_POSITION = "MATCH (p:MapPosition {item_id: $id}) RETURN p.x AS x, p.y AS y"
_KIND_COUNTS = "MATCH (i:Item) RETURN i.kind AS kind, count(i) AS n"
_ACTIVE_LINK_COUNT = (
    "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.active_key IS NOT NULL RETURN count(l) AS n"
)


@dataclass(frozen=True)
class Window:
    """The two time filters of the graph and node views; None means no filter."""

    as_of: datetime | None = None
    known_as_of: datetime | None = None


def _stopped(row: dict) -> datetime | None:
    """The stopped bound shown as invalid_at: the latest bound, else the earliest."""
    return instant(row.get("stopped_being_true_latest")) or instant(
        row.get("stopped_being_true_earliest")
    )


def holds_at(row: dict, at: datetime) -> bool:
    """The world interval holds `at`: no start or a start at or before it, and no stop after."""
    start = instant(row.get("became_true_earliest"))
    if start is not None and start > at:
        return False
    stop = _stopped(row)
    return stop is None or stop > at


def known_at(row: dict, at: datetime) -> bool:
    recorded = instant(row.get("recorded"))
    return recorded is not None and recorded <= at


def in_window(row: dict, window: Window) -> bool:
    if window.known_as_of is not None and not known_at(row, window.known_as_of):
        return False
    return window.as_of is None or holds_at(row, window.as_of)


def link_in_window(link: dict, window: Window) -> bool:
    """An active link, or with as_of an ended link whose stopped bound says it held then."""
    if not in_window(link, window):
        return False
    if link.get("active_key") is not None:
        return True
    return window.as_of is not None and _stopped(link) is not None


def _valid_at(row: dict) -> str | None:
    for key in ("became_true_earliest", "became_true_latest", "recorded"):
        shown = iso_z(row.get(key))
        if shown is not None:
            return shown
    return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _version(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _properties(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def successors_data(found: Standing, registry: Registry) -> list[dict[str, str]]:
    """The successors on a standing, each by its item id, its link id and its meaning's name."""
    return [
        {
            "id": after.item_id,
            "link_id": after.link_id,
            "meaning": registry.meaning(after.meaning)[0],
        }
        for after in found.successors
    ]


def node_data(
    item: dict,
    registry: Registry,
    position: tuple[float, float] | None,
    self_item_id: str | None,
    found: Standing,
) -> dict[str, Any]:
    version = _version(item.get("version"))
    data: dict[str, Any] = {
        "id": item["id"],
        "label": _text(item.get("title")),
        "type": registry.kind_name(item.get("kind")),
        "summary": _text(item.get("summary")),
        "status": _text(item.get("status")),
        "file_path": "",
        "valid_at": _valid_at(item),
        "invalid_at": iso_z(_stopped(item)),
        "created_at": iso_z(item.get("recorded")),
        "updated_at": iso_z(item.get("touched_at")) or iso_z(item.get("recorded")),
        "status_since": iso_z(item.get("status_since")),
        "verified_at": iso_z(item.get("last_confirmed")),
        "version": version,
        "properties": _properties(item.get("properties")),
        "has_changes": version > 1,
        "standing": found.state,
        "successors": successors_data(found, registry),
    }
    if position is not None:
        data["x"], data["y"] = position
    if self_item_id is not None and item["id"] == self_item_id:
        data["self"] = True
    return data


def edge_data(link: dict, registry: Registry) -> dict[str, Any]:
    verb, cardinality = registry.meaning(link.get("meaning"))
    return {
        "id": link["id"],
        "source": link["from_item"],
        "target": link["to_item"],
        "label": verb,
        "verb": verb,
        "cardinality": cardinality,
        "canonical": False,
        "canonical_of": None,
        "valid_at": _valid_at(link),
        "invalid_at": iso_z(_stopped(link)),
        "note": "",
        "source_field": SOURCE_FIELD,
    }


def _positions(world: WorldQuery) -> dict[str, tuple[float, float]]:
    return {
        row["item_id"]: (float(row["x"]), float(row["y"]))
        for row in world.read(_POSITIONS)
        if isinstance(row["item_id"], str)
        and isinstance(row["x"], int | float)
        and isinstance(row["y"], int | float)
    }


def _time_range(nodes: list[dict], edges: list[dict]) -> dict[str, str | None]:
    starts = [d["valid_at"] for d in (*nodes, *edges) if d["valid_at"]]
    ends = starts + [d["invalid_at"] for d in (*nodes, *edges) if d["invalid_at"]]
    return {"min": min(starts)[:10] if starts else None, "max": max(ends)[:10] if ends else None}


def graph_payload(
    world: WorldQuery,
    primitives: WorldQuery,
    *,
    window: Window,
    self_item_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    registry = read_registry(world, primitives)
    positions = _positions(world)
    items = [
        item
        for item in sorted(world.read(_ITEMS), key=lambda item: item["id"])
        if in_window(item, window)
    ]
    found = standings_of(world, items, at=now)
    nodes = [
        node_data(item, registry, positions.get(item["id"]), self_item_id, found[item["id"]])
        for item in items
    ]
    shown = {node["id"] for node in nodes}
    links = world.read(_ACTIVE_LINKS if window.as_of is None else _ALL_LINKS)
    edges = [
        edge_data(link, registry)
        for link in sorted(links, key=lambda link: link["id"])
        if link.get("from_item") in shown
        and link.get("to_item") in shown
        and link_in_window(link, window)
    ]
    return {
        "elements": {
            "nodes": [{"data": node} for node in nodes],
            "edges": [{"data": edge} for edge in edges],
        },
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "by_type": dict(Counter(node["type"] for node in nodes)),
            "embedded": len(nodes),
            "edge_verbs": sorted({edge["verb"] for edge in edges}),
        },
        "time": _time_range(nodes, edges),
        "generated_at": iso_z(now),
        "as_of": iso_z(window.as_of),
        "known_as_of": iso_z(window.known_as_of),
        "self_item_id": self_item_id if self_item_id in shown else None,
    }


def node_payload(
    world: WorldQuery,
    primitives: WorldQuery,
    item_id: str,
    *,
    window: Window,
    self_item_id: str | None,
) -> dict[str, Any] | None:
    """None when the id is no item of this world, or no item in the window."""
    rows = world.read(_ITEM, id=item_id)
    if not rows or not in_window(rows[0]["i"], window):
        return None
    item = rows[0]["i"]
    registry = read_registry(world, primitives)
    placed = world.read(_POSITION, id=item_id)
    position = (float(placed[0]["x"]), float(placed[0]["y"])) if placed else None
    found = standings_of(world, [item])[item_id]
    node = node_data(item, registry, position, self_item_id, found)
    content = _text(item.get("summary")) or _text(item.get("body"))
    return {
        "node": node
        | {"title": node["label"], "content": content, "status_field_value": node["status"]},
        "required_props": [],
        "optional_props": registry.optional_props(item.get("kind")),
        "required_prop_names": [],
        "status_field": STATUS_FIELD,
    }


def schema_payload(world: WorldQuery, primitives: WorldQuery) -> dict[str, Any]:
    """Every kind by name, the primitive kinds first so a world kind of the same name wins."""
    registry = read_registry(world, primitives)
    kinds = sorted(registry.kinds.values(), key=lambda kind: (not kind.primitive, kind.name))
    return {
        kind.name: {
            "required": [],
            "optional": registry.optional_props(kind.id),
            "status_field": STATUS_FIELD,
            "status_vocab": list(kind.status_vocab),
        }
        for kind in kinds
    }


def health_payload(world: WorldQuery, primitives: WorldQuery, *, now: datetime) -> dict[str, Any]:
    registry = read_registry(world, primitives)
    by_type: Counter[str] = Counter()
    for row in world.read(_KIND_COUNTS):
        by_type[registry.kind_name(row["kind"])] += row["n"]
    nodes = sum(by_type.values())
    edges = world.read(_ACTIVE_LINK_COUNT)[0]["n"]
    return {
        "status": HEALTHY,
        "summary": SUMMARY_READY if nodes else SUMMARY_EMPTY,
        "generated_at": iso_z(now),
        "checks": {"graph": READY, "sources": READY},
        "nodes": nodes,
        "edges": edges,
        "by_type": dict(by_type),
        "overall": {"state": HEALTHY},
    }


def own_item(world: WorldQuery, self_item_id: str | None) -> str | None:
    """The person's own item id when it names an item of this world, else None."""
    if not self_item_id:
        return None
    return self_item_id if world.read(_ITEM_ID, id=self_item_id) else None


def session_payload(world: WorldQuery, session: ViewSession) -> dict[str, Any]:
    """has_items says the world holds more than the own item: the page opens the map, not the
    welcome, for a person who comes back on a browser that has no local state."""
    own = own_item(world, session.self_item_id)
    return {
        "signed_in": True,
        "self_item_id": own,
        "has_items": bool(world.read(_ANOTHER_ITEM, own=own or "")),
        "account": session.user_id,
        "method": session.method,
    }
