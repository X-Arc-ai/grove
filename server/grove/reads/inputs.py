"""Private, complete source inputs collected inside the existing D7 capture bracket.

This is not a public packet or a disclosure policy. The owner filter must cover every retained
row and retrieval input before eligibility, statistics, rank, counts, or presentation. Temporal
assembly still uses the existing D3 helpers in that same transaction; this module replays nothing.
"""

from dataclasses import asdict
from typing import Any

from grove.reads.capture import PacketSource
from grove.reads.runtime import ReadRuntime, deadline, snapshot_busy
from grove.store.capabilities import DEFAULTS
from grove.store.catalogue import table
from grove.store.primitives import (
    KIND_BELIEF,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANINGS,
)

# A deliberate inventory, not every world table. Operational/auth rows and optional diagnostic
# views need their own declared consumer. AreaRevision remains outside the accepted schema.
NODE_TABLES = (
    "items",
    "kinds",
    "meanings",
    "property_names",
    "aliases",
    "capabilities",
    "evidence",
    "beliefs",
    "change_entries",
    "history_values",
    "connectors",
    "sources",
    "episodes",
    "areas",
    "impacts",
    "cascade_proposals",
    "contradictions",
    "verifications",
)
_TABLES = {table(name).label: table(name) for name in NODE_TABLES}
_NODES = f"MATCH (n:{'|'.join(_TABLES)}) RETURN labels(n) AS labels, n AS row"
# Do not scope the endpoints away. Return the actual endpoints so PacketSource's native guard
# refuses a corrupt cross-world relationship instead of silently omitting it from the inventory.
_LINKS = (
    "MATCH (a)-[l:LINK]->(b) RETURN l AS row, a AS from_node, labels(a) AS from_labels, "
    "b AS to_node, labels(b) AS to_labels"
)
LEXICAL_INDEXES = (
    "grove_items_lexical",
    "grove_aliases_lexical",
    "grove_past_values",
)
_SEARCH_LABEL = dict(zip(LEXICAL_INDEXES, ("Item", "Alias", "HistoryValue"), strict=True))


def _identity(label: str, row: dict) -> tuple:
    definition = table("links") if label == "LINK" else _TABLES[label]
    values = tuple(row.get(prop) for prop in definition.uniques[0].props)
    for prop, value in zip(definition.uniques[0].props, values, strict=True):
        if prop == "sequence":
            if type(value) is not int or value < 1:
                raise snapshot_busy()
        elif not isinstance(value, str) or not value:
            raise snapshot_busy()
    return values


def _record(record: Any, world_id: str) -> tuple[str, tuple, dict]:
    if not isinstance(record, dict) or set(record) != {"labels", "row"}:
        raise snapshot_busy()
    labels, row = record["labels"], record["row"]
    if (
        not isinstance(labels, list | tuple)
        or not labels
        or any(not isinstance(label, str) for label in labels)
        or len(set(labels)) != len(labels)
        or not isinstance(row, dict)
        or row.get("world_id") != world_id
    ):
        raise snapshot_busy()
    primary = set(labels) & _TABLES.keys()
    if len(primary) != 1:
        raise snapshot_busy()
    label = next(iter(primary))
    if set(labels) - {label} - ({"PastValue"} if label == "HistoryValue" else set()):
        raise snapshot_busy()
    return label, _identity(label, row), {"labels": sorted(labels), "row": row}


def _assemble_inputs(
    world_id: str, node_records: list, link_records: list, native_membership: dict | None
) -> dict:
    deadline()
    tables: dict[str, list] = {name: [] for name in NODE_TABLES}
    seen: dict[str, set[tuple]] = {label: set() for label in _TABLES}
    records = []
    for record in node_records:
        deadline()
        label, key, normalized = _record(record, world_id)
        if key in seen[label]:
            raise snapshot_busy()
        seen[label].add(key)
        records.append((label, key, normalized))
    for label, _, record in sorted(records, key=lambda value: (value[0], value[1])):
        tables[_TABLES[label].name].append(record)

    items = {record["row"]["id"] for record in tables["items"]}
    links, link_keys = [], set()
    for record in link_records:
        deadline()
        if not isinstance(record, dict):
            raise snapshot_busy()
        row = record.get("row")
        if not isinstance(row, dict) or row.get("world_id") != world_id:
            raise snapshot_busy()
        key = _identity("LINK", row)
        if key in link_keys:
            raise snapshot_busy()
        for side, prop in (("from", "from_item"), ("to", "to_item")):
            endpoint = record.get(f"{side}_node")
            if (
                not isinstance(endpoint, dict)
                or record.get(f"{side}_labels") != ["Item"]
                or endpoint.get("world_id") != world_id
                or endpoint.get("id") != row.get(prop)
                or row.get(prop) not in items
            ):
                raise snapshot_busy()
        link_keys.add(key)
        links.append(row)
    links.sort(key=lambda row: _identity("LINK", row))

    membership: dict[str, list] | None = None
    if native_membership is not None:
        membership = {}
        for index in LEXICAL_INDEXES:
            matches, keys = [], set()
            for match in native_membership[index]:
                deadline()
                if not isinstance(match, dict) or set(match) != {"node", "labels"}:
                    raise snapshot_busy()
                label, key, normalized = _record(
                    {"row": match["node"], "labels": match["labels"]}, world_id
                )
                if (
                    label != _SEARCH_LABEL[index]
                    or key not in seen[label]
                    or key in keys
                    or (index == "grove_past_values" and "PastValue" not in normalized["labels"])
                ):
                    raise snapshot_busy()
                keys.add(key)
                matches.append(normalized)
            membership[index] = sorted(matches, key=lambda match: match["row"]["id"])
    deadline()
    return {
        "world_id": world_id,
        "tables": tables,
        "links": links,
        "lexical_membership": membership,
        # Fresh containers isolate fixed developer configuration from consumer mutation.
        "fixed": {
            "belief_kind": {"id": KIND_BELIEF, **PRIMITIVE_KIND_BELIEF},
            "meanings": [{"id": key, **value} for key, value in PRIMITIVE_MEANINGS.items()],
            "capabilities": [asdict(value) for value in DEFAULTS],
        },
    }


async def collect_inputs(
    source: PacketSource, runtime: ReadRuntime, *, search_text: str | None = None
) -> dict:
    """Read the current source inventory and complete native lexical memberships.

    Invoke only from an application's capture materializer, before its final marker. There is no
    item/page limit here. Raw Source text and Connector settings stay private to the pipeline.
    All HistoryValues are retained, including reasons and non-replay text references.
    Validation and sorting use the bounded worker instead of the event loop.
    """
    deadline()
    if search_text is not None and not isinstance(search_text, str):
        raise ValueError("search text must be a string")
    nodes = await source.read(_NODES)
    links = await source.read(_LINKS)
    membership = None
    if search_text is not None:
        membership = {}
        for index in LEXICAL_INDEXES:
            membership[index] = await source.search(index, search_text)
    return await runtime.worker(_assemble_inputs, source.world_id, nodes, links, membership)
