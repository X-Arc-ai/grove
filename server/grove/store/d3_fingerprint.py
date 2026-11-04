"""The D3 fingerprint of one world: what a restore must give back, with the text left out.

It covers every projection row's id with its clocks (became true, stopped being true, recorded,
last confirmed), its horizon (re-check by), status, end class and version, and on evidence the
active flag; the change history's last sequence and its count of entries; and the row count and
newest id of each snapshot table. Instants are UTC ISO text. A forget changes only text, so it
leaves the fingerprint as it was, and the restore drill checks forgotten values on their own.

The newest id of a table is its greatest id in text order (a UUIDv7 sorts by time). A table with
no id column takes the greatest value of its first unique key, the key's columns joined by `|`.
The change entries are the history part, so they are not a snapshot table here.

It is not the dump fingerprint of grove.store.dump_fingerprint, which covers every byte.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work

from grove.store.catalogue import TABLES, Table
from grove.store.connection import DATABASE
from grove.store.current import read_rows
from grove.store.query import CYPHER_PREFIX, check_timeout, collect
from grove.store.scope import WORLD_PARAM, check_world_id, refuse_world_param, scope_cypher

VERSION = "grove.d3-fingerprint.v1"

PROJECTION_TABLES: tuple[Table, ...] = tuple(
    t for t in TABLES if t.group == "projection" and t.scope == "world"
)
# The change entries are counted in the history part.
SNAPSHOT_TABLES: tuple[Table, ...] = tuple(
    t
    for t in TABLES
    if t.group == "snapshot"
    and t.scope == "world"
    and t.element == "node"
    and t.name != "change_entries"
)

_CLOCK_INSTANTS = (
    "became_true_earliest",
    "became_true_latest",
    "stopped_being_true_earliest",
    "stopped_being_true_latest",
    "recorded",
    "last_confirmed",
    "recheck_by",
)
_CODES = ("became_true_precision", "stopped_being_true_precision", "status", "end_class")
_EVIDENCE_FLAG = "active"
_EVIDENCE = "evidence"
_KEY_SEPARATOR = "|"


def key_column(table: Table) -> str:
    """The column that names a row: `id`, or the first column of the first unique key."""
    if any("id" in column.props for column in table.columns):
        return "id"
    return table.uniques[0].props[0]


def _props(table: Table) -> frozenset[str]:
    return frozenset(prop for column in table.columns for prop in column.props)


def _utc_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    elif not isinstance(value, datetime) and hasattr(value, "to_native"):
        value = value.to_native()
    if not isinstance(value, datetime):
        raise TypeError(f"not an instant: {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an instant in a fingerprint needs its offset")
    return value.astimezone(UTC).isoformat()


def _columns(table: Table) -> tuple[str, ...]:
    """The columns of one table that the fingerprint reads, in a fixed order."""
    held = _props(table)
    wanted = [*_CLOCK_INSTANTS, *_CODES, "version"]
    if table.name == _EVIDENCE:
        wanted.append(_EVIDENCE_FLAG)
    return tuple(name for name in wanted if name in held)


def row_part(table_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The no-text part of one projection row. Applied to its own output it gives the same part,
    so a snapshot can keep parts and a restore can fingerprint them again."""
    table = _TABLE[table_name]
    key = key_column(table)
    part: dict[str, Any] = {"id": row.get("id") if "id" in row else row.get(key)}
    for name in _columns(table):
        value = row.get(name)
        if value is None:
            continue
        if name in _CLOCK_INSTANTS:
            part[name] = _utc_text(value)
        elif name == "version":
            part[name] = int(value)
        elif name == _EVIDENCE_FLAG:
            part[name] = bool(value)
        else:
            part[name] = str(value)
    return part


_TABLE: dict[str, Table] = {t.name: t for t in PROJECTION_TABLES}


def _rows_of(rows: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> Iterable:
    if isinstance(rows, Mapping):
        return rows.values()
    return rows


def projection_parts(
    projection_rows: Mapping[str, Any], *, forgotten: Iterable[str] = ()
) -> dict[str, dict[str, dict[str, Any]]]:
    """{table: {id: part}} for every projection table. `projection_rows` maps a table name to
    {id: row} or to a list of rows; a table it leaves out has no rows. Forgotten ids are left
    out of every table."""
    gone = frozenset(forgotten)
    parts: dict[str, dict[str, dict[str, Any]]] = {}
    for table in PROJECTION_TABLES:
        held: dict[str, dict[str, Any]] = {}
        for row in _rows_of(projection_rows.get(table.name) or {}):
            part = row_part(table.name, row)
            if part["id"] not in gone:
                held[part["id"]] = part
        parts[table.name] = held
    return parts


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(
    projection_rows: Mapping[str, Any],
    history: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]],
    *,
    forgotten: Iterable[str] = (),
) -> dict[str, Any]:
    """The D3 fingerprint of rows held in memory. `history` holds last_sequence and entries;
    `tables` maps a snapshot table name to {count, newest}."""
    parts = projection_parts(projection_rows, forgotten=forgotten)
    last = int(history.get("last_sequence") or 0)
    entries = int(history.get("entries") or 0)
    sizes = {
        name: {"count": int(tables[name].get("count") or 0), "newest": tables[name].get("newest")}
        for name in sorted(tables)
    }
    payload = [
        VERSION,
        [[name, [parts[name][key] for key in sorted(parts[name])]] for name in sorted(parts)],
        {"last_sequence": last, "entries": entries},
        sizes,
    ]
    return {
        "version": VERSION,
        "fingerprint": _digest(payload),
        "rows": {name: len(parts[name]) for name in sorted(parts)},
        "last_sequence": last,
        "entries": entries,
        "tables": sizes,
    }


# Reads of the live store ---------------------------------------------------------------------


class OneRead:
    """Scoped reads inside one read transaction, so every part of a fingerprint sees one store.
    It reads the way WorldQuery.read does: through the scope parser and the world guard."""

    def __init__(self, tx: ManagedTransaction, world_id: str) -> None:
        self._tx = tx
        self._world_id = world_id

    @property
    def world_id(self) -> str:
        return self._world_id

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        statement = CYPHER_PREFIX + scope_cypher(cypher, "read")
        result = self._tx.run(statement, {**params, WORLD_PARAM: self._world_id})
        return collect(result, self._world_id)


def read_once[T](
    driver: Driver, world_id: str, work: Callable[[OneRead], T], *, timeout: float = 600.0
) -> T:
    """`work` runs in one read transaction of the world. The driver may run it twice."""
    check_world_id(world_id)

    @unit_of_work(timeout=check_timeout(timeout))
    def unit(tx: ManagedTransaction) -> T:
        return work(OneRead(tx, world_id))

    with driver.session(database=DATABASE) as session:
        return session.execute_read(unit)


def _pattern(table: Table) -> str:
    if table.element == "relationship":
        return f"(:Item)-[n:{table.label}]->(:Item)"
    return f"(n:{table.label})"


def projection_rows(source: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Every projection row of the world, whole, as {table: {id: row}}. `source` is a WorldQuery,
    a WorldTx or a OneRead. The primitive rows of world '' are not world rows, so they are not
    here."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for table in PROJECTION_TABLES:
        key = key_column(table)
        rows = read_rows(source, f"MATCH {_pattern(table)} RETURN n")
        found[table.name] = {row["n"][key]: row["n"] for row in rows}
    return found


def _part_rows(source: Any) -> dict[str, list[dict[str, Any]]]:
    """Only the columns the fingerprint reads, so no text leaves the store."""
    found: dict[str, list[dict[str, Any]]] = {}
    for table in PROJECTION_TABLES:
        names = (key_column(table), *_columns(table))
        shown = ", ".join(f"n.{name} AS {name}" for name in names)
        found[table.name] = read_rows(source, f"MATCH {_pattern(table)} RETURN {shown}")
    return found


def history_part(source: Any) -> dict[str, int]:
    rows = read_rows(
        source, "MATCH (e:ChangeEntry) RETURN max(e.sequence) AS last, count(e) AS entries"
    )
    return {"last_sequence": rows[0]["last"] or 0, "entries": rows[0]["entries"]}


def _newest(table: Table) -> str:
    if key_column(table) == "id":
        return "n.id"
    columns = table.uniques[0].props
    return f" + '{_KEY_SEPARATOR}' + ".join(f"coalesce(toString(n.{name}), '')" for name in columns)


def table_parts(source: Any) -> dict[str, dict[str, Any]]:
    """{table: {count, newest}} for every snapshot table of the world."""
    sizes: dict[str, dict[str, Any]] = {}
    for table in SNAPSHOT_TABLES:
        rows = read_rows(
            source,
            f"MATCH (n:{table.label}) RETURN count(n) AS count, max({_newest(table)}) AS newest",
        )
        sizes[table.name] = {"count": rows[0]["count"], "newest": rows[0]["newest"]}
    return sizes


def live_parts(source: Any) -> dict[str, Any]:
    """What a fingerprint of the live world is made of: the parts of every projection row, the
    history part and the snapshot tables' sizes."""
    return {
        "projection": projection_parts(_part_rows(source)),
        "history": history_part(source),
        "tables": table_parts(source),
    }


def live_fingerprint(
    driver: Driver, world_id: str, *, forgotten: Iterable[str] = ()
) -> dict[str, Any]:
    """The D3 fingerprint of the world as the store holds it now, in one read transaction."""
    parts = read_once(driver, world_id, live_parts)
    return fingerprint(parts["projection"], parts["history"], parts["tables"], forgotten=forgotten)
