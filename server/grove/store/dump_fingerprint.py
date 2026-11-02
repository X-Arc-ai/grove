"""The dump fingerprint: one digest of every node, relationship and schema row of the store.

The daily backup records it before the dump, and the restore check compares it with the store
that the dump loads into. Any changed byte of a property, label, type, endpoint or schema row
changes it, because a dump and its load must hold the same store.

It is not D3's fingerprint, which `writer-b` builds: that one covers the projections' ids and
clocks and leaves text out, so that a forget keeps it. This one covers every byte.

It is the one read that crosses worlds. It returns only digests and counts per label and type:
no row, no id and no text leaves this module. It runs from operator commands on the box and from
tests, never from an API route.

    python -m grove.store.dump_fingerprint [--world <world id>] [--expect <fingerprint>]
"""

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work
from neo4j.spatial import Point
from neo4j.time import Date, DateTime, Duration, Time

from grove.store.connection import DATABASE, open_driver
from grove.store.scope import check_world_id

VERSION = "grove.dump-fingerprint.v1"
MISMATCH = 3

_NODES = (
    "CYPHER 25 MATCH (n) WHERE $world_id IS NULL OR n.world_id = $world_id "
    "RETURN elementId(n) AS element, labels(n) AS labels, properties(n) AS props"
)
_LINKS = (
    "CYPHER 25 MATCH (a)-[r]->(b) "
    "WHERE $world_id IS NULL OR (a.world_id = $world_id AND b.world_id = $world_id) "
    "RETURN elementId(a) AS start, elementId(b) AS end, type(r) AS type, "
    "properties(r) AS props"
)
_CONSTRAINTS = (
    "CYPHER 25 SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties, "
    "ownedIndex RETURN name, type, entityType, labelsOrTypes, properties, ownedIndex"
)
# State and population percentage are left out: an index of a store loaded a moment ago may
# still be populating, and it holds the same definition.
_INDEXES = (
    "CYPHER 25 SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, "
    "indexProvider, options, owningConstraint RETURN name, type, entityType, labelsOrTypes, "
    "properties, indexProvider, options, owningConstraint"
)


class StoreMoved(RuntimeError):
    """A write landed between the reads. Stop every writer, then read again."""


class UnknownValue(TypeError):
    """A property value of a type this module cannot encode. The message names only the type."""


def _float(value: float) -> str:
    # hex is exact and keeps -0.0 apart from 0.0; nan has one spelling.
    return "nan" if math.isnan(value) else value.hex()


def encode(value: Any) -> Any:
    """A JSON form of a stored value that keeps every type apart: 1, 1.0, '1' and True differ."""
    # bool before int (bool is an int); Duration and Point before list (both are tuples).
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", _float(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes | bytearray):
        return ["bytes", bytes(value).hex()]
    if isinstance(value, Duration):
        return ["duration", value.months, value.days, value.seconds, value.nanoseconds]
    if isinstance(value, Point):
        return ["point", value.srid, [_float(float(axis)) for axis in value]]
    if isinstance(value, DateTime | Time):
        zone = getattr(value.tzinfo, "zone", None) or getattr(value.tzinfo, "key", None)
        return [type(value).__name__.lower(), value.iso_format(), zone]
    if isinstance(value, Date):
        return ["date", value.iso_format()]
    if isinstance(value, dict):
        return ["map", [[key, encode(value[key])] for key in sorted(value)]]
    if isinstance(value, list | tuple):
        return ["list", [encode(item) for item in value]]
    raise UnknownValue(f"no encoding for a value of type {type(value).__name__}")


def _digest(parts: Any) -> str:
    text = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def node_digest(labels: Iterable[str], props: dict[str, Any]) -> str:
    return _digest(["node", sorted(labels), encode(props)])


def link_digest(kind: str, props: dict[str, Any], start: str, end: str) -> str:
    """`start` and `end` are the digests of the end nodes, so a link moved to another node
    changes its own digest."""
    return _digest(["link", kind, encode(props), start, end])


def schema_digest(rows: Iterable[dict[str, Any]], kind: str) -> str:
    return _digest([kind, sorted(_digest(encode(row)) for row in rows)])


def combine(
    nodes: list[str], links: list[str], schema: list[str] | None, counts: dict[str, Any]
) -> str:
    # A multiset: two equal rows count twice, and the order rows arrive in does not matter.
    return _digest([VERSION, sorted(nodes), sorted(links), schema, counts])


def _read(tx: ManagedTransaction, world_id: str | None, with_schema: bool) -> dict[str, Any]:
    by_element: dict[str, str] = {}
    labels: Counter[str] = Counter()
    for row in tx.run(_NODES, world_id=world_id):
        by_element[row["element"]] = node_digest(row["labels"], row["props"])
        labels.update(row["labels"])

    links: list[str] = []
    types: Counter[str] = Counter()
    for row in tx.run(_LINKS, world_id=world_id):
        start, end = by_element.get(row["start"]), by_element.get(row["end"])
        if start is None or end is None:
            raise StoreMoved("a link reached a node the first read did not see")
        links.append(link_digest(row["type"], row["props"], start, end))
        types[row["type"]] += 1

    schema = None
    constraints = indexes = 0
    if with_schema:
        constraint_rows = [dict(row) for row in tx.run(_CONSTRAINTS)]
        index_rows = [dict(row) for row in tx.run(_INDEXES)]
        constraints, indexes = len(constraint_rows), len(index_rows)
        schema = [
            schema_digest(constraint_rows, "constraints"),
            schema_digest(index_rows, "indexes"),
        ]

    counts = {
        "nodes": len(by_element),
        "relationships": len(links),
        "labels": dict(sorted(labels.items())),
        "types": dict(sorted(types.items())),
        "constraints": constraints,
        "indexes": indexes,
    }
    return {
        "version": VERSION,
        "scope": "store" if world_id is None else "world",
        "fingerprint": combine(list(by_element.values()), links, schema, counts),
        **counts,
    }


def dump_fingerprint(
    driver: Driver, *, world_id: str | None = None, timeout: float = 600.0
) -> dict[str, Any]:
    """The whole store, or one world's nodes and the links between them.

    A world's fingerprint leaves the schema out: the schema is shared by every world. Every read
    runs in one read transaction. Neo4j reads committed rows, so the caller stops every writer
    first when two fingerprints must agree.
    """
    if world_id is not None:
        check_world_id(world_id)

    @unit_of_work(timeout=timeout)
    def work(tx: ManagedTransaction) -> dict[str, Any]:
        return _read(tx, world_id, with_schema=world_id is None)

    with driver.session(database=DATABASE) as session:
        return session.execute_read(work)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m grove.store.dump_fingerprint")
    parser.add_argument("--world", help="one world's rows instead of the whole store")
    parser.add_argument("--expect", help="exit 3 when the fingerprint is another one")
    args = parser.parse_args(argv)

    with open_driver() as driver:
        result = dump_fingerprint(driver, world_id=args.world)
    if args.expect is not None:
        result["expected"] = args.expect
        result["match"] = result["fingerprint"] == args.expect
    print(json.dumps(result, sort_keys=True))
    return MISMATCH if result.get("match") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
