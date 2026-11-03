"""D3's one restore procedure: load the snapshot, rebuild the projections, replay the forget
ledger, and compare the D3 fingerprint. It lives in the writer folder because it writes.

The order is the spec's: (1) the snapshot tables go into a world that holds no world row; (2)
the projections are rebuilt from the change entries in sequence, each checked as the writer
checks it, with nothing appended; (3) the forget ledger replaces every value it names with the
marker; (4) the fingerprint of the restored world must equal the one recorded with the snapshot,
recomputed without the ids the ledger forgot since. A mismatch raises RestoreMismatch, and the
store must not go live.

In production the snapshot is the whole-store dump (S21). `take_snapshot` is the scratch form a
test uses: every row of the world's snapshot tables with its labels, values tagged so instants
and dates survive JSON, the live fingerprint, and the no-text parts of every projection row.

    python -m grove.writer.restore fingerprint --world <world id>
    python -m grove.writer.restore rebuild --world <world id> [--expect <fingerprint>]
"""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from grove import clock
from grove.store.catalogue import TABLES, WORLD_LABELS, Table, table
from grove.store.connection import Driver, open_driver
from grove.store.d3_fingerprint import (
    PROJECTION_TABLES,
    OneRead,
    fingerprint,
    live_parts,
    read_once,
)
from grove.store.query import CYPHER_PREFIX
from grove.store.replay import (
    FORGOTTEN_MARKER,
    Projection,
    ReplayRefused,
    read_history,
    rebuild,
)
from grove.writer.tx import UnknownWorld, WorldTx, WorldWriter

__all__ = [
    "FORGOTTEN_MARKER",
    "SNAPSHOT_FORMAT",
    "RestoreMismatch",
    "WorldNotEmpty",
    "load_snapshot",
    "main",
    "rebuild_in_place",
    "replay_ledger",
    "restore",
    "take_snapshot",
]

SNAPSHOT_FORMAT = "grove.d3-snapshot.v1"
MISMATCH = 3
REFUSED = 4
_TIMEOUT = 600.0
_CHUNK = 500
_SHOWN = 5

# Every snapshot table of a world, the change entries included.
_EXPORTED: dict[str, Table] = {
    t.name: t
    for t in TABLES
    if t.group == "snapshot" and t.scope == "world" and t.element == "node"
}
_TABLE_LABELS = frozenset(t.label for t in TABLES if t.scope == "world" and t.element == "node")
_LEDGER_TYPES = {
    prop: column.type for column in table("forget_ledger").columns for prop in column.props
}
_FORGET_VALUES = frozenset({"field_value", "named_value"})
_FORGET_ITEM = "item"

_SET_NEXT_SEQUENCE = (
    CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) "
    "SET w.next_sequence = $sequence RETURN w.id AS id"
)
_MARK_VALUES = (
    "MATCH (v:HistoryValue) WHERE v.id IN $ids AND v.value <> $marker "
    "SET v.value = $marker, v.forgotten_at = $at"
)


class RestoreMismatch(RuntimeError):
    """The restored world does not match its snapshot. `problems` says what differs, `expected`
    and `found` are the two fingerprints. The store holds what the restore loaded and rebuilt,
    for a person to read; it must not go live."""

    def __init__(
        self, problems: list[str], *, expected: dict[str, Any], found: dict[str, Any]
    ) -> None:
        super().__init__("the restore does not match its snapshot: " + "; ".join(problems))
        self.problems = problems
        self.expected = expected
        self.found = found


class WorldNotEmpty(ValueError):
    """A snapshot loads only into a world that holds no world row."""


# Values in JSON --------------------------------------------------------------------------------


def _tag(value: Any) -> Any:
    """A stored value in a JSON form that keeps instants and dates apart from text."""
    if isinstance(value, datetime):
        return {"$t": "instant", "v": value.isoformat()}
    if isinstance(value, date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, list | tuple):
        return [_tag(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"no JSON form for a stored {type(value).__name__}")


def _untag(value: Any) -> Any:
    if isinstance(value, dict):
        kind, text = value.get("$t"), value.get("v")
        if kind == "instant":
            return datetime.fromisoformat(text)
        if kind == "date":
            return date.fromisoformat(text)
        raise ValueError("a stored value is never a map")
    if isinstance(value, list):
        return [_untag(item) for item in value]
    return value


def _stored(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as a write sets it: the scope adds world_id, and null and [] are no value (S16)."""
    return {
        key: value
        for key, value in row.items()
        if key != "world_id" and value is not None and value != []
    }


def _chunks(rows: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK):
        yield rows[start : start + _CHUNK]


# The snapshot ----------------------------------------------------------------------------------


def take_snapshot(driver: Driver, world_id: str) -> dict[str, Any]:
    """Every row of the world's snapshot tables, and the D3 fingerprint of the same moment, read
    in one transaction. The result is plain JSON."""

    def work(reader: OneRead) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        rows: dict[str, list[dict[str, Any]]] = {}
        for name, held in _EXPORTED.items():
            found = reader.read(
                f"MATCH (n:{held.label}) RETURN labels(n) AS labels, properties(n) AS p"
            )
            exported = [
                {
                    "labels": sorted(row["labels"]),
                    "properties": {key: _tag(value) for key, value in sorted(row["p"].items())},
                }
                for row in found
            ]
            rows[name] = sorted(exported, key=lambda row: json.dumps(row, sort_keys=True))
        return rows, live_parts(reader)

    rows, parts = read_once(driver, world_id, work, timeout=_TIMEOUT)
    return {
        "format": SNAPSHOT_FORMAT,
        "world_id": world_id,
        "rows": rows,
        "fingerprint": fingerprint(parts["projection"], parts["history"], parts["tables"]),
        "projection": parts["projection"],
    }


def _snapshot_rows(world_id: str, snapshot: Mapping[str, Any]) -> dict[tuple[str, ...], list]:
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"not a snapshot of the form {SNAPSHOT_FORMAT}")
    if snapshot.get("world_id") != world_id:
        raise ValueError("the snapshot is of another world")
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for name, rows in snapshot["rows"].items():
        held = _EXPORTED.get(name)
        if held is None:
            raise ValueError(f"{name!r} is not a snapshot table")
        for row in rows:
            labels = tuple(sorted(row["labels"]))
            others = _TABLE_LABELS & set(labels) - {held.label}
            if held.label not in labels or others or not set(labels) <= WORLD_LABELS:
                raise ValueError(f"a row of {name} with the labels {list(labels)}")
            properties = {key: _untag(value) for key, value in row["properties"].items()}
            if properties.get("world_id", world_id) != world_id:
                raise ValueError(f"a row of {name} is of another world")
            groups.setdefault(labels, []).append(_stored(properties))
    return groups


def _last_sequence(snapshot: Mapping[str, Any]) -> int:
    sequences = [
        row["properties"].get("sequence") for row in snapshot["rows"].get("change_entries", [])
    ]
    return max((sequence for sequence in sequences if isinstance(sequence, int)), default=0)


def _refuse_world_rows(tx: WorldTx) -> None:
    for label in sorted(WORLD_LABELS):
        if tx.run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]:
            raise WorldNotEmpty(f"the world still holds {label} rows")


def load_snapshot(driver: Driver, world_id: str, snapshot: Mapping[str, Any]) -> dict[str, int]:
    """Writes the snapshot's rows into a world that holds no world row, in one transaction, and
    sets :World.next_sequence to the snapshot's last sequence. Returns the count per table. The
    :World row must exist; a world with any world row is refused (WorldNotEmpty)."""
    groups = _snapshot_rows(world_id, snapshot)
    last = _last_sequence(snapshot)

    def work(tx: WorldTx) -> dict[str, int]:
        tx.lock_world()
        _refuse_world_rows(tx)
        for labels, rows in sorted(groups.items()):
            statement = f"UNWIND $rows AS row CREATE (n:{':'.join(labels)}) SET n += row"
            for chunk in _chunks(rows):
                tx.run(statement, rows=chunk)
        # :World is a platform row: the scoped transaction refuses its label by design.
        done = tx.unscoped().run(_SET_NEXT_SEQUENCE, world_id=world_id, sequence=last).single()
        if done is None:
            raise UnknownWorld(world_id)
        return {name: len(rows) for name, rows in snapshot["rows"].items()}

    return WorldWriter(driver, world_id, timeout=_TIMEOUT).transaction(work)


# The rebuild -----------------------------------------------------------------------------------


def _write_projection(tx: WorldTx, projection: Projection) -> None:
    nodes = [t for t in PROJECTION_TABLES if t.element == "node"]
    for held in nodes:
        # An item takes its links with it.
        tx.run(f"MATCH (n:{held.label}) DETACH DELETE n")
    for held in sorted(nodes, key=lambda t: t.name != "items"):
        statement = f"UNWIND $rows AS row CREATE (n:{held.label}) SET n += row"
        rows = [_stored(row) for row in projection.rows[held.name].values()]
        for chunk in _chunks(rows):
            tx.run(statement, rows=chunk)
    links = [_stored(row) for row in projection.links.values()]
    for chunk in _chunks(links):
        made = tx.run(
            "UNWIND $rows AS row MATCH (a:Item {id: row.from_item}) "
            "MATCH (b:Item {id: row.to_item}) CREATE (a)-[n:LINK]->(b) SET n += row "
            "RETURN count(n) AS made",
            rows=chunk,
        )
        if made[0]["made"] != len(chunk):
            raise RuntimeError("a rebuilt link lost an end item")


def rebuild_in_place(driver: Driver, world_id: str) -> Projection:
    """Replaces the world's projection rows with the rebuild of its change entries, in one
    transaction under the world's lock. Nothing is appended to the history. A refused entry
    raises ReplayRefused and nothing is written. The primitive rows of world '' are never
    touched."""

    def work(tx: WorldTx) -> Projection:
        tx.lock_world()
        entries, values = read_history(tx)
        projection = rebuild(entries, values)
        _write_projection(tx, projection)
        return projection

    return WorldWriter(driver, world_id, timeout=_TIMEOUT).transaction(work)


# The forget ledger -----------------------------------------------------------------------------


def _ledger_value(name: str, value: Any) -> Any:
    value = _untag(value)
    if isinstance(value, str) and _LEDGER_TYPES.get(name) == "instant":
        return datetime.fromisoformat(value)
    if isinstance(value, str) and _LEDGER_TYPES.get(name) == "date":
        return date.fromisoformat(value)
    return value


def _ledger_row(world_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    typed = {key: _ledger_value(key, value) for key, value in row.items()}
    if typed.get("world_id", world_id) != world_id:
        raise ValueError("a forget ledger row of another world")
    target = typed.get("target_type")
    if target not in (*_FORGET_VALUES, _FORGET_ITEM):
        # A source span is the forget lane's: its text lives in sources, not in the history.
        raise ValueError(f"a forget of target type {target!r} is not replayed here")
    return typed


def _value_columns(name: str) -> tuple[str, ...]:
    held = table(name)
    return tuple(
        prop for column in held.columns if column.type == "value_id" for prop in column.props
    )


def _forget_items(tx: WorldTx, item_ids: list[str]) -> tuple[set[str], set[str]]:
    """The items, their links and the evidence rows on both: their ids, and the value ids their
    rows and entries hold."""
    items = [row["i"] for row in tx.run("MATCH (i:Item) WHERE i.id IN $ids RETURN i", ids=item_ids)]
    links = [
        row["l"]
        for row in tx.run(
            "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE a.id IN $ids OR b.id IN $ids RETURN l",
            ids=item_ids,
        )
    ]
    targets = [*item_ids, *(link["id"] for link in links)]
    evidence = [
        row["e"]
        for row in tx.run("MATCH (e:Evidence) WHERE e.target_id IN $ids RETURN e", ids=targets)
    ]
    gone = {*item_ids, *targets, *(row["id"] for row in evidence)}
    values: set[str] = set()
    for name, rows in (("items", items), ("links", links), ("evidence", evidence)):
        for row in rows:
            values.update(row[prop] for prop in _value_columns(name) if row.get(prop))
    for row in tx.run(
        "MATCH (e:ChangeEntry) WHERE e.entity_id IN $ids "
        "RETURN e.new_value_id AS new, e.old_value_id AS old, e.reason AS reason",
        ids=sorted(gone),
    ):
        values.update(value for value in row.values() if value)
    return gone, values


def replay_ledger(
    driver: Driver, world_id: str, ledger_rows: Iterable[Mapping[str, Any]]
) -> set[str]:
    """Replays forget ledger rows, in one transaction. `field_value` and `named_value`: each
    history value named in `internal_ids` reads FORGOTTEN_MARKER, with `forgotten_at` (the
    ledger row's `approved_at`). `item`: every value that the item, its links and the evidence
    rows on them hold, in their rows and in their entries, reads the marker. No change entry is
    touched.

    Then the projections are rebuilt from the history as the ledger left it, in the same
    transaction: that is the redaction (D3 step 3). A row whose own value reads the marker is
    not rebuilt, so a forgotten item, its links and their evidence rows leave the projection,
    and a column whose value reads the marker reads it too, or goes. Returns the projection
    ids the ledger forgot."""
    rows = [_ledger_row(world_id, row) for row in ledger_rows]
    if not rows:
        return set()

    def work(tx: WorldTx) -> set[str]:
        tx.lock_world()
        forgotten: set[str] = set()
        for row in rows:
            named = list(row.get("internal_ids") or [])
            if row["target_type"] == _FORGET_ITEM:
                gone, values = _forget_items(tx, named)
                forgotten |= gone
            else:
                values = set(named)
            at = row.get("approved_at") or clock.now(world_id)
            tx.run(_MARK_VALUES, ids=sorted(values), marker=FORGOTTEN_MARKER, at=at)
        entries, values_now = read_history(tx)
        projection = rebuild(entries, values_now)
        kept = forgotten & {key for held in projection.rows.values() for key in held}
        if kept:
            raise RuntimeError(f"a forgotten row came back in the rebuild: {sorted(kept)[0]}")
        _write_projection(tx, projection)
        return forgotten | set(projection.forgotten)

    return WorldWriter(driver, world_id, timeout=_TIMEOUT).transaction(work)


def _keep_ledger(driver: Driver, world_id: str, ledger_rows: list[Mapping[str, Any]]) -> None:
    """Puts back each ledger row the restored world lacks: the ledger is kept apart from the
    snapshot, so the restore is what brings it home."""
    rows = [_stored(_ledger_row(world_id, row)) for row in ledger_rows]
    if not rows:
        return

    def work(tx: WorldTx) -> None:
        held = {
            row["id"]
            for row in tx.run(
                "MATCH (f:ForgetLedger) WHERE f.id IN $ids RETURN f.id AS id",
                ids=[row.get("id") for row in rows],
            )
        }
        missing = [row for row in rows if row.get("id") not in held]
        if missing:
            tx.run("UNWIND $rows AS row CREATE (f:ForgetLedger) SET f += row", rows=missing)

    WorldWriter(driver, world_id, timeout=_TIMEOUT).transaction(work)


# The restore -----------------------------------------------------------------------------------


def _differences(
    expected: Mapping[str, dict[str, dict[str, Any]]], found: Mapping[str, dict[str, dict]]
) -> list[str]:
    problems = []
    for name in sorted(expected):
        want, got = expected[name], found.get(name, {})
        differ = sorted(key for key in want.keys() | got.keys() if want.get(key) != got.get(key))
        for key in differ[:_SHOWN]:
            if key not in got:
                problems.append(f"{name} {key}: the snapshot has it, the restore does not")
            elif key not in want:
                problems.append(f"{name} {key}: the restore has it, the snapshot does not")
            else:
                columns = sorted(
                    column
                    for column in want[key].keys() | got[key].keys()
                    if want[key].get(column) != got[key].get(column)
                )
                problems.append(f"{name} {key}: {', '.join(columns)} differ")
        if len(differ) > _SHOWN:
            problems.append(f"{name}: {len(differ) - _SHOWN} more rows differ")
    return problems


def _compare(
    expected: dict[str, Any],
    found: dict[str, Any],
    expected_parts: Mapping[str, Any],
    found_parts: Mapping[str, Any],
) -> list[str]:
    problems = []
    for name in ("last_sequence", "entries"):
        if found[name] != expected[name]:
            problems.append(f"{name}: {found[name]}, the snapshot holds {expected[name]}")
    for name, size in expected["tables"].items():
        got = found["tables"].get(name, {})
        if got.get("count") != size["count"]:
            problems.append(f"{name}: {got.get('count')} rows, the snapshot holds {size['count']}")
        elif got.get("newest") != size["newest"]:
            problems.append(f"{name}: the newest id differs")
    problems += _differences(expected_parts, found_parts)
    if found["fingerprint"] != expected["fingerprint"] and not problems:
        problems.append("the fingerprint differs")
    return problems


def restore(
    driver: Driver,
    world_id: str,
    snapshot: Mapping[str, Any],
    ledger_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """D3's full restore into a world that holds only its :World row. Returns the restored
    world's fingerprint without the forgotten ids, with `forgotten` (the ids, sorted). Raises
    RestoreMismatch before any write when the snapshot does not give its own recorded
    fingerprint, and after the restore when the world does not match it; ReplayRefused when an
    entry of the snapshot fails a check (the rebuild then writes nothing)."""
    ledger = list(ledger_rows)
    recorded = snapshot["fingerprint"]
    history = {"last_sequence": recorded["last_sequence"], "entries": recorded["entries"]}
    whole = fingerprint(snapshot["projection"], history, recorded["tables"])
    if whole["fingerprint"] != recorded["fingerprint"]:
        raise RestoreMismatch(
            ["the snapshot does not give the fingerprint recorded with it; nothing was loaded"],
            expected=recorded,
            found=whole,
        )
    load_snapshot(driver, world_id, snapshot)
    rebuild_in_place(driver, world_id)
    forgotten = replay_ledger(driver, world_id, ledger)
    _keep_ledger(driver, world_id, ledger)

    expected = fingerprint(snapshot["projection"], history, recorded["tables"], forgotten=forgotten)
    parts = read_once(driver, world_id, live_parts, timeout=_TIMEOUT)
    found = fingerprint(parts["projection"], parts["history"], parts["tables"], forgotten=forgotten)
    gone = frozenset(forgotten)
    expected_parts = {
        name: {key: part for key, part in rows.items() if key not in gone}
        for name, rows in snapshot["projection"].items()
    }
    found_parts = {
        name: {key: part for key, part in rows.items() if key not in gone}
        for name, rows in parts["projection"].items()
    }
    problems = _compare(expected, found, expected_parts, found_parts)
    if problems:
        raise RestoreMismatch(problems, expected=expected, found=found)
    return {**found, "forgotten": sorted(forgotten)}


# The command line ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`fingerprint` prints the world's D3 fingerprint. `rebuild` rebuilds the world's projections
    in place, prints the fingerprint after, and exits 3 when it is not `--expect`, 4 when an
    entry is refused (nothing written)."""
    parser = argparse.ArgumentParser(prog="python -m grove.writer.restore")
    commands = parser.add_subparsers(dest="command", required=True)
    shown = commands.add_parser("fingerprint", help="print the D3 fingerprint of a world")
    shown.add_argument("--world", required=True)
    again = commands.add_parser("rebuild", help="rebuild a world's projections in place")
    again.add_argument("--world", required=True)
    again.add_argument("--expect", help="exit 3 when the fingerprint after is another one")
    args = parser.parse_args(argv)

    with open_driver() as driver:
        if args.command == "rebuild":
            try:
                rebuild_in_place(driver, args.world)
            except ReplayRefused as refused:
                problem = {"refused": {"sequence": refused.sequence, "rule": refused.rule}}
                print(json.dumps(problem, sort_keys=True), file=sys.stderr)
                return REFUSED
        parts = read_once(driver, args.world, live_parts, timeout=_TIMEOUT)
    result = fingerprint(parts["projection"], parts["history"], parts["tables"])
    if args.command == "rebuild" and args.expect is not None:
        result["expected"] = args.expect
        result["match"] = result["fingerprint"] == args.expect
    print(json.dumps(result, sort_keys=True))
    return MISMATCH if result.get("match") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
