"""The pure rebuild of a world's projections from its change history (D3), and the record axis.

`rebuild` replays the change entries in sequence. History-only entries never move a row. Each
entry is checked with the writer's own rules, against the registry as the replay has rebuilt it
up to that entry; a failed check raises ReplayRefused, which names the entry's sequence and the
rule. There is no restore mode and no bypass. Nothing here writes: grove.writer.restore writes
what this module returns.

How an entry moves a row is the entry vocabulary of the writer's README. Derived columns are
never entries: `version` is 1 for the row and 1 more for each further operation that moved the
row (one change set and one reason, with an entry that is not history only and not a marker
that leaves the row as it was: `restated` or `no_head_update`);
`touched_at` is the recording time of the last entry on the row; a link's `active_key` follows
its end class; `property_values` follows `properties`; and the claim fields of items and links
follow their active supporting evidence rows, as the writer computes them.

A value that a forget replaced reads FORGOTTEN_MARKER. A row whose own value reads it is not
rebuilt, and neither is any later entry on that row.

`known_as_of` is the record axis: the head as Grove held it at a record time, read on the world
axis at that same time.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, NoReturn

from grove.store.asof import end_instant, placed_link
from grove.store.axes import AsOf, AsOfRow
from grove.store.catalogue import Table
from grove.store.current import (
    SUCCESSOR_MEANINGS,
    TERMINAL_ROLES,
    Successor,
    _scheduled,
    link_standing,
    read_rows,
    standing,
)
from grove.store.d3_fingerprint import PROJECTION_TABLES, key_column
from grove.store.enums import ENUMS
from grove.store.normalise import reads_blank
from grove.store.primitives import KIND_BELIEF, PRIMITIVE_KIND_BELIEF, PRIMITIVE_MEANINGS
from grove.writer.apply import CLAIM_FIELDS, _fits, _property_values
from grove.writer.clocks import ClockError, clock_from_row, ends_before_start, parse_clock
from grove.writer.registry import Registry
from grove.writer.source_types import RANK, claim_fields

# What a forget leaves in a history value (D3, D14). It is not JSON, so no value decodes to it.
FORGOTTEN_MARKER = "[forgotten]"

ENTITY_TABLES: dict[str, tuple[str, ...]] = {
    "item": ("items",),
    "link": ("links",),
    "evidence": ("evidence",),
    "belief": ("beliefs",),
    "registry_row": ("kinds", "meanings", "property_names", "aliases", "capabilities"),
}
TABLES: dict[str, Table] = {t.name: t for t in PROJECTION_TABLES}

_CLOCKS = ("became_true", "stopped_being_true")
_BOUNDS = ("earliest", "latest")
_UNKNOWN = "unknown"
_MARKERS: dict[str, frozenset[str]] = {
    "item": frozenset({"reopened", "merged_into"}),
    "link": frozenset({"restated", "no_head_update"}),
}
# Markers that never move their row, so they add no version: a known link restated, and the
# reason a correction gives for no head update, which can land on a link that a correction of an
# earlier batch made (the writer leaves that link's version as it was). The undo's conflict
# check leaves out the same set (grove.writer.composite.STILL_MARKERS).
_NO_VERSION = frozenset({"restated", "no_head_update"})
# Item fields of the entry vocabulary that a later migration may add to the catalogue.
_ITEM_FIELDS = frozenset({"lesson_in"})
_FIXED = frozenset({"id", "world_id", "version", "touched_at"})
# Item and link columns that the replay derives and no entry sets.
_DERIVED = frozenset(
    {"active_key", "property_values", "derived_source_type", "derivation", "speaker", "modality"}
)
_JSON_TEXT = frozenset({"properties", "recheck_how"})
_CLAIM_ITEM = ("derived_source_type", "derivation", "speaker", "modality")
_CLAIM_LINK = ("derived_source_type", "derivation")
_TEXT = "text_home"
_NEVER_STOOD = frozenset({"refuted", "spent"})
_EARLIEST = datetime.min.replace(tzinfo=UTC)


class ReplayRefused(ValueError):
    """An entry failed a check of the replay: `sequence` names the entry, `rule` the check."""

    def __init__(self, sequence: int | None, rule: str, message: str = "") -> None:
        super().__init__(f"entry {sequence} refused ({rule})" + (f": {message}" if message else ""))
        self.sequence = sequence
        self.rule = rule


class _Skip(Exception):
    """An entry the replay leaves out when it does not validate."""


class _Forgotten:
    def __repr__(self) -> str:
        return "FORGOTTEN"


_FORGOTTEN = _Forgotten()


@dataclass
class Projection:
    """The rebuilt rows, {table: {id: row}}, for every projection table, with the derived columns
    of the entry vocabulary. Instants are aware datetimes in UTC. A belief row's id is its item
    id. `forgotten` holds the ids whose row a forget replaced. `ended_at` holds, per item or link
    with an end class, the world time of the entry that set it (None when it had none), which
    places the ending on the record axis (S48)."""

    world_id: str
    rows: dict[str, dict[str, dict[str, Any]]]
    last_sequence: int
    entries: int
    forgotten: frozenset[str] = field(default_factory=frozenset)
    ended_at: dict[str, datetime | None] = field(default_factory=dict)

    @property
    def history(self) -> dict[str, int]:
        return {"last_sequence": self.last_sequence, "entries": self.entries}

    @property
    def items(self) -> dict[str, dict[str, Any]]:
        return self.rows["items"]

    @property
    def links(self) -> dict[str, dict[str, Any]]:
        return self.rows["links"]

    @property
    def evidence(self) -> dict[str, dict[str, Any]]:
        return self.rows["evidence"]


def _props(table: Table, type_: str | None = None) -> frozenset[str]:
    return frozenset(
        prop
        for column in table.columns
        if type_ is None or column.type == type_
        for prop in column.props
    )


_COLUMNS = {name: _props(table) for name, table in TABLES.items()}
_INSTANTS = {name: _props(table, "instant") for name, table in TABLES.items()}
_DATES = {name: _props(table, "date") for name, table in TABLES.items()}
_TEXTS = {name: _props(table, _TEXT) for name, table in TABLES.items()}
_KEYS = {name: key_column(table) for name, table in TABLES.items()}


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    elif not isinstance(value, datetime) and hasattr(value, "to_native"):
        value = value.to_native()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an instant needs its offset")
    return value.astimezone(UTC)


def _clock_key(value: Any) -> tuple | None:
    """A clock as (precision, earliest, latest): the form two clocks compare in."""
    if value is None:
        return None
    if value == _UNKNOWN:
        return (_UNKNOWN, None, None)
    if not isinstance(value, Mapping):
        return ("?", value, None)
    return (
        value.get("precision"),
        _instant(value.get("earliest")),
        _instant(value.get("latest")),
    )


def _row_clock(row: Mapping[str, Any], name: str) -> tuple | None:
    precision = row.get(f"{name}_precision")
    if precision is None:
        return None
    return (precision, _instant(row.get(f"{name}_earliest")), _instant(row.get(f"{name}_latest")))


class _Replay:
    def __init__(self, values: Mapping[str, str | None], validate: bool) -> None:
        self.values = values
        self.validate = validate
        self.world_id: str | None = None
        self.rows: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in TABLES}
        self.where: dict[str, str] = {}
        self.groups: dict[str, set[tuple[Any, Any]]] = {}
        self.touched: dict[str, datetime | None] = {}
        self.heads: dict[tuple[str, str], datetime] = {}
        self.born: dict[str, datetime | None] = {}
        self.keys: dict[str, str] = {}
        self.active: dict[str, str] = {}
        self.forgotten: set[str] = set()
        self.ended_at: dict[str, datetime | None] = {}
        # (entity id, field) whose head value a forget replaced: no old value can match it.
        self.blanked: set[tuple[str, str]] = set()
        self.sequence: int | None = None
        self.last: int | None = None

    # Checks ------------------------------------------------------------------------------------

    def fail(self, rule: str, message: str = "") -> NoReturn:
        if self.validate:
            raise ReplayRefused(self.sequence, rule, message)
        raise _Skip

    def value(self, entry: Mapping[str, Any], name: str) -> Any:
        value_id = entry.get(name)
        if value_id is None:
            return None
        if value_id not in self.values:
            self.fail("missing_value", f"no history value {value_id}")
        text = self.values[value_id]
        if text == FORGOTTEN_MARKER:
            return _FORGOTTEN
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            self.fail("value_not_json", f"history value {value_id} is not JSON")

    def kind(self, kind_id: Any) -> dict[str, Any] | None:
        if kind_id == KIND_BELIEF:
            return {"id": KIND_BELIEF, **PRIMITIVE_KIND_BELIEF}
        return self.rows["kinds"].get(kind_id)

    def meaning(self, meaning_id: Any) -> dict[str, Any] | None:
        if meaning_id in PRIMITIVE_MEANINGS:
            return {"id": meaning_id, **PRIMITIVE_MEANINGS[meaning_id]}
        return self.rows["meanings"].get(meaning_id)

    def vocabulary(self, kind: dict[str, Any]) -> list[str]:
        registry = Registry(self.rows["kinds"], self.rows["meanings"], [], [])
        return [entry["status"] for entry in registry.vocabulary(kind)]

    def world(self, world_id: Any) -> None:
        if self.world_id is None and isinstance(world_id, str):
            self.world_id = world_id
        elif world_id != self.world_id:
            self.fail("world_id", f"a row of world {world_id!r} in world {self.world_id!r}")

    # The loop ----------------------------------------------------------------------------------

    def apply(self, entry: Mapping[str, Any]) -> None:
        sequence = entry.get("sequence")
        self.sequence = sequence if isinstance(sequence, int) else None
        if self.sequence is None or (self.last is not None and self.sequence <= self.last):
            self.fail("sequence", "sequences rise one entry at a time")
        self.last = self.sequence
        if entry.get("world_id") is not None:
            self.world(entry["world_id"])
        entity_type, entity_id = entry.get("entity_type"), entry.get("entity_id")
        if entity_type not in ENTITY_TABLES or not isinstance(entity_id, str):
            self.fail("entity_type", f"no entity type {entity_type!r}")
        if entity_id in self.forgotten:
            return
        name = entry.get("field")
        if name == "row":
            if not self.create(entry, entity_type, entity_id):
                return
        else:
            self.change(entry, entity_type, entity_id, name)
        self.touched[entity_id] = _instant(entry.get("recorded_at"))
        if not entry.get("history_only") and name not in _NO_VERSION:
            group = (entry.get("change_set_id"), entry.get("reason"))
            self.groups.setdefault(entity_id, set()).add(group)

    # Row entries -------------------------------------------------------------------------------

    def typed(self, table: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value == []:
                continue
            try:
                if key in _INSTANTS[table] and isinstance(value, str):
                    value = _instant(value)
                elif key in _DATES[table] and isinstance(value, str):
                    value = date.fromisoformat(value)
            except ValueError:
                self.fail("bad_instant", f"{key} is not an ISO time")
            row[key] = value
        return row

    def create(self, entry: Mapping[str, Any], entity_type: str, entity_id: str) -> bool:
        value = self.value(entry, "new_value_id")
        if value is _FORGOTTEN:
            self.forgotten.add(entity_id)
            return False
        if not (
            isinstance(value, dict)
            and isinstance(value.get("table"), str)
            and isinstance(value.get("row"), dict)
        ):
            self.fail("row_value", "a row entry holds {table, row}")
        name = value["table"]
        if name not in TABLES:
            self.fail("unknown_table", f"{name!r} is not a projection table")
        if name not in ENTITY_TABLES[entity_type]:
            self.fail("table_entity", f"a {entity_type} row is not a row of {name}")
        row = self.typed(name, value["row"])
        if row.get(_KEYS[name]) != entity_id:
            self.fail("row_id", "the row's id is not the entry's entity id")
        if entity_id in self.where:
            self.fail("id_not_new", f"{entity_id} exists already")
        self.world(row.get("world_id"))
        if name == "items":
            self.check_item(row)
        elif name == "links":
            self.check_link(row)
        elif name == "evidence":
            self.check_evidence(row)
        if name in ("items", "links"):
            self.check_order(row)
        if name == "links":
            key = row.get("active_key") or self.standard_key(row)
            if row.get("active_key") and key in self.active:
                self.fail("active_key_taken", "another active link holds this key")
            self.keys[entity_id] = key
            if row.get("active_key"):
                self.active[key] = entity_id
        self.rows[name][entity_id] = row
        self.where[entity_id] = name
        self.born[entity_id] = _instant(entry.get("world_time"))
        return True

    def standard_key(self, row: Mapping[str, Any]) -> str:
        return f"{self.world_id}|{row.get('from_item')}|{row.get('meaning')}|{row.get('to_item')}"

    def check_item(self, row: dict[str, Any]) -> None:
        kind = self.kind(row.get("kind"))
        if kind is None:
            self.fail("unknown_kind", f"no kind {row.get('kind')!r} in the registry so far")
        title = row.get("title")
        if not isinstance(title, str) or reads_blank(title):
            self.fail("blank_title", "an item's title reads non-blank")
        status = row.get("status")
        if status is not None and status not in self.vocabulary(kind):
            self.fail("unknown_status", f"{status!r} is not a status of the kind")
        self.check_end_class(row.get("end_class"))

    def check_link(self, row: dict[str, Any]) -> None:
        meaning = self.meaning(row.get("meaning"))
        if meaning is None:
            self.fail("unknown_meaning", f"no meaning {row.get('meaning')!r} so far")
        for declared, end in (("from_kinds", "from_item"), ("to_kinds", "to_item")):
            item = self.rows["items"].get(row.get(end))
            if item is None:
                self.fail("missing_end", f"no item {row.get(end)!r} at the link's {end}")
            kind = self.kind(item.get("kind"))
            if kind is None or not _fits(meaning.get(declared), kind):
                self.fail("endpoint_kind", f"the meaning does not take this kind at {end}")
        self.check_end_class(row.get("end_class"))

    def check_evidence(self, row: dict[str, Any]) -> None:
        target_type, target_id = row.get("target_type"), row.get("target_id")
        table = "links" if target_type == "link" else "items"
        if target_type not in ENUMS["evidence_target_type"] or target_id not in self.rows[table]:
            self.fail("missing_target", f"no {target_type} {target_id!r} for the evidence row")

    def check_end_class(self, value: Any) -> None:
        if value is not None and value not in ENUMS["end_class"]:
            self.fail("unknown_end_class", f"{value!r} is not an end class")

    def check_order(self, row: dict[str, Any]) -> None:
        if self.validate and ends_before_start(
            clock_from_row(row, "became_true"), clock_from_row(row, "stopped_being_true")
        ):
            self.fail("end_before_start", "the clock ends before it starts")

    # Field entries -----------------------------------------------------------------------------

    def settable(self, entity_type: str, table: str, name: Any) -> bool:
        if not isinstance(name, str) or name in _FIXED or name == _KEYS[table]:
            return False
        if entity_type in ("item", "link"):
            if name in _CLOCKS:
                return True
            if name in _DERIVED or any(name.startswith(f"{clock}_") for clock in _CLOCKS):
                return False
            if entity_type == "item" and name in _ITEM_FIELDS:
                return True
        return name in _COLUMNS[table]

    def same(self, table: str, row: Mapping[str, Any], name: str, old: Any) -> bool:
        if name in _CLOCKS:
            return _clock_key(old) == _row_clock(row, name)
        current = row.get(name)
        if name in _JSON_TEXT and isinstance(current, str):
            current = json.loads(current)
        if name in _INSTANTS[table]:
            return _instant(old) == current
        if name in _DATES[table] and isinstance(old, str):
            return date.fromisoformat(old) == current
        return old == current

    def check_value(self, entity_type: str, row: Mapping[str, Any], name: str, new: Any) -> None:
        if new is None or new is _FORGOTTEN:
            return
        if entity_type == "item" and name == "status":
            if new not in self.vocabulary(self.kind(row.get("kind")) or {}):
                self.fail("unknown_status", f"{new!r} is not a status of the kind")
        elif entity_type in ("item", "link") and name == "end_class":
            self.check_end_class(new)
        elif entity_type == "evidence" and name == "end_reason":
            if new not in ENUMS["evidence_end_reason"]:
                self.fail("unknown_end_reason", f"{new!r} is not an evidence end reason")
        elif name in _CLOCKS and new != _UNKNOWN:
            shown = new
            if isinstance(new, dict):
                shown = {key: part for key, part in new.items() if key != "words_id"}
            try:
                parse_clock(shown)
            except ClockError as error:
                self.fail("clock", str(error))
        elif name == "properties" and not isinstance(new, dict):
            self.fail("bad_value", "properties are an object")

    def change(self, entry: Mapping[str, Any], entity_type: str, entity_id: str, name: Any) -> None:
        table = self.where.get(entity_id)
        if table is None:
            self.fail("missing_entity", f"no {entity_type} {entity_id} so far")
        if table not in ENTITY_TABLES[entity_type]:
            self.fail("table_entity", f"{entity_id} is a row of {table}")
        row = self.rows[table][entity_id]
        if name in _MARKERS.get(entity_type, ()):
            return
        if not self.settable(entity_type, table, name):
            self.fail("unknown_field", f"{name!r} is no field of {table}")
        history_only = bool(entry.get("history_only"))
        new = self.value(entry, "new_value_id")
        self.check_value(entity_type, row, name, new)
        if entry.get("old_value_id") is not None and not history_only:
            old = self.value(entry, "old_value_id")
            known = old is not _FORGOTTEN and (entity_id, name) not in self.blanked
            if known and not self.same(table, row, name, old):
                self.fail("old_value", f"the old {name} is not the rebuilt {name}")
        at = _instant(entry.get("world_time"))
        if entity_type == "item" and name in CLAIM_FIELDS and not history_only:
            head = self.heads.get((entity_id, name)) or self.born.get(entity_id)
            # An undo sets an older value back on purpose (D9).
            older = head is not None and at is not None and at < head
            if older and entry.get("inverse_of") is None:
                self.fail("older_than_head", f"{name} is older than its head's world time")
        if history_only:
            return
        if name == "end_class" and entity_type == "link" and new is None:
            key = self.keys[entity_id]
            if self.validate and self.active.get(key, entity_id) != entity_id:
                self.fail("active_key_taken", "another active link holds this key")
        self.put(entity_type, table, row, name, new, entry)
        if name == "end_class":
            if new is None:
                self.ended_at.pop(entity_id, None)
            else:
                self.ended_at[entity_id] = at
        if new is _FORGOTTEN:
            self.blanked.add((entity_id, name))
        else:
            self.blanked.discard((entity_id, name))
        if name in _CLOCKS:
            self.check_order(row)
        if at is not None:
            held = self.heads.get((entity_id, name))
            self.heads[(entity_id, name)] = at if held is None else max(held, at)

    def put(
        self,
        entity_type: str,
        table: str,
        row: dict[str, Any],
        name: str,
        new: Any,
        entry: Mapping[str, Any],
    ) -> None:
        if new is _FORGOTTEN:
            # The value is gone: a text column shows the marker, any other column is left empty.
            if name in _TEXTS[table] and isinstance(row.get(name), str):
                row[name] = FORGOTTEN_MARKER
            else:
                self.put(entity_type, table, row, name, None, entry)
            return
        if name in _CLOCKS:
            self.put_clock(row, name, new)
        elif entity_type == "link" and name == "end_class":
            if new is None:
                row.pop("end_class", None)
                key = self.keys[row["id"]]
                row["active_key"] = key
                self.active[key] = row["id"]
            else:
                row["end_class"] = new
                key = row.pop("active_key", None)
                if key is not None:
                    self.active.pop(key, None)
        elif entity_type == "evidence" and name == "end_reason":
            if new is None:
                row["active"] = True
                row.pop("ended_at", None)
                row.pop("end_reason", None)
            else:
                row["active"] = False
                row["ended_at"] = _instant(entry.get("recorded_at"))
                row["end_reason"] = new
        elif entity_type in ("item", "link") and name == "properties":
            if not new:
                row.pop("properties", None)
                row.pop("property_values", None)
            else:
                row["properties"] = json.dumps(new, sort_keys=True, ensure_ascii=False)
                values = _property_values(new)
                if values:
                    row["property_values"] = values
                else:
                    row.pop("property_values", None)
        elif entity_type in ("item", "link") and name == "recheck_how":
            if new is None:
                row.pop(name, None)
            else:
                row[name] = json.dumps(new, sort_keys=True, ensure_ascii=False)
        elif new is None or new == []:
            row.pop(name, None)
        elif name in _INSTANTS[table] and isinstance(new, str):
            row[name] = _instant(new)
        elif name in _DATES[table] and isinstance(new, str):
            row[name] = date.fromisoformat(new)
        else:
            row[name] = new

    def put_clock(self, row: dict[str, Any], name: str, new: Any) -> None:
        for bound in (*_BOUNDS, "precision", "words"):
            row.pop(f"{name}_{bound}", None)
        if new is None:
            return
        if new == _UNKNOWN:
            row[f"{name}_precision"] = _UNKNOWN
            return
        if not isinstance(new, dict):
            return
        row[f"{name}_precision"] = new.get("precision")
        for bound in _BOUNDS:
            if new.get(bound) is not None:
                row[f"{name}_{bound}"] = _instant(new[bound])
        if new.get("words_id"):
            row[f"{name}_words"] = new["words_id"]

    # Derived columns ---------------------------------------------------------------------------

    def finish(self) -> dict[str, dict[str, dict[str, Any]]]:
        for rows in self.rows.values():
            for entity_id, row in rows.items():
                row["version"] = len(self.groups.get(entity_id, ())) or 1
                touched = self.touched.get(entity_id)
                if touched is not None:
                    row["touched_at"] = touched
                else:
                    row.pop("touched_at", None)
        supporting: dict[str, list[dict[str, Any]]] = {}
        for row in self.rows["evidence"].values():
            if (
                row.get("polarity") == "supports"
                and row.get("active")
                and row.get("source_type") in RANK
            ):
                supporting.setdefault(row.get("target_id"), []).append(row)
        for table, names in (("items", _CLAIM_ITEM), ("links", _CLAIM_LINK)):
            for entity_id, row in self.rows[table].items():
                fields = claim_fields(supporting.get(entity_id, []))
                for name in names:
                    if fields[name] is None:
                        row.pop(name, None)
                    else:
                        row[name] = fields[name]
        return self.rows


def _by_sequence(entry: Mapping[str, Any]) -> tuple[int, int]:
    sequence = entry.get("sequence")
    return (0, sequence) if isinstance(sequence, int) else (1, 0)


def rebuild(
    entries: Iterable[Mapping[str, Any]],
    values: Mapping[str, str | None],
    *,
    known_at: datetime | str | None = None,
    validate: bool = True,
) -> Projection:
    """The projections the entries give, replayed in sequence. `values` maps a history value id
    to its stored text. With `known_at`, only the entries recorded at or before it count (the
    record axis). With validate False an entry that fails a check is left out instead."""
    cutoff = _instant(known_at)
    chosen = [
        entry
        for entry in entries
        if cutoff is None
        or (entry.get("recorded_at") is not None and _instant(entry["recorded_at"]) <= cutoff)
    ]
    chosen.sort(key=_by_sequence)
    replay = _Replay(values, validate)
    for entry in chosen:
        try:
            replay.apply(entry)
        except _Skip:
            continue
    rows = replay.finish()
    sequences = [entry["sequence"] for entry in chosen if isinstance(entry.get("sequence"), int)]
    return Projection(
        world_id=replay.world_id or "",
        rows=rows,
        last_sequence=max(sequences, default=0),
        entries=len(chosen),
        forgotten=frozenset(replay.forgotten),
        ended_at={
            entity_id: at
            for entity_id, at in replay.ended_at.items()
            if entity_id not in replay.forgotten
        },
    )


def read_history(
    source: Any, *, known_at: datetime | None = None
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """The world's change entries in sequence, and the text of every value they name. `source`
    is a WorldQuery, a WorldTx (inside its transaction) or a OneRead."""
    if known_at is None:
        rows = read_rows(source, "MATCH (e:ChangeEntry) RETURN e ORDER BY e.sequence")
    else:
        rows = read_rows(
            source,
            "MATCH (e:ChangeEntry) WHERE e.recorded_at <= $at RETURN e ORDER BY e.sequence",
            at=_instant(known_at),
        )
    entries = [row["e"] for row in rows]
    ids = sorted(
        {
            entry[name]
            for entry in entries
            for name in ("new_value_id", "old_value_id")
            if entry.get(name)
        }
    )
    values = {
        row["id"]: row["value"]
        for row in read_rows(
            source,
            "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
            ids=ids,
        )
    }
    return entries, values


# The record axis -------------------------------------------------------------------------------


def _stood(row: dict[str, Any], at: datetime, end_world_time: datetime | None) -> dict[str, Any]:
    """The row to read a standing from: an ending placed after `at` (by `end_instant`, as on the
    world axis) had not happened yet at `at`. A refuted or spent row, or an ending with no place,
    stays ended."""
    if row.get("end_class") in _NEVER_STOOD:
        return row
    stop = end_instant(row, end_world_time if row.get("end_class") else None)
    if stop is None or stop <= at:
        return row
    shown = {
        key: value
        for key, value in row.items()
        if key != "end_class" and not key.startswith("stopped_being_true_")
    }
    if shown.get("status_role") in TERMINAL_ROLES:
        shown.pop("status_role")
    return shown


def _recorded_order(row: Mapping[str, Any]) -> tuple:
    return (row.get("recorded") or _EARLIEST, row["id"])


def record_axis(head: Projection, at: datetime) -> AsOf:
    """What Grove believed at record time `at`: `head` is the projection rebuilt to `at`, and each
    row's standing is read on the world axis at `at`, with each ending placed as `world_as_of`
    places it. A link is kept when it stood at `at`; an item when it stood, or when a successor
    stood over it."""
    at = _instant(at)
    if at is None:
        raise ValueError("an as-of time is needed")
    links: dict[str, AsOfRow] = {}
    successors: dict[str, list[Successor]] = {}
    for link in sorted(head.links.values(), key=_recorded_order):
        held = link_standing(placed_link(link, head.ended_at.get(link["id"])), at=at)
        if held.state != "current":
            continue
        links[link["id"]] = AsOfRow(link, held)
        if link.get("meaning") in SUCCESSOR_MEANINGS:
            successors.setdefault(link["from_item"], []).append(
                Successor(link["to_item"], link["id"], link["meaning"])
            )
    lifecycle = {kind_id: row.get("lifecycle_class") for kind_id, row in head.rows["kinds"].items()}
    lifecycle[KIND_BELIEF] = PRIMITIVE_KIND_BELIEF["lifecycle_class"]
    names = sorted(
        (
            {"kind": row.get("kind"), "name": row.get("name"), "role": row["scheduled_role"]}
            for row in head.rows["property_names"].values()
            if row.get("scheduled_role") is not None
        ),
        key=lambda name: str(name["name"]),
    )
    planned = {
        row.get("target_id")
        for row in head.evidence.values()
        if row.get("target_type") == "item"
        and row.get("polarity") == "supports"
        and row.get("active")
        and row.get("source_type") == "planned"
    }
    items: dict[str, AsOfRow] = {}
    for item_id, item in head.items.items():
        held = standing(
            _stood(item, at, head.ended_at.get(item_id)),
            at=at,
            lifecycle_class=lifecycle.get(item.get("kind")),
            planned_origin=item_id in planned,
            scheduled=_scheduled(item, names),
            successors=successors.get(item_id, ()),
        )
        if held.state in ("ended", "not_started") and not held.successors:
            continue
        items[item_id] = AsOfRow(item, held)
    return AsOf("record", at, items, links)


def known_as_of(query: Any, at: datetime) -> AsOf:
    """The record axis (D3): the head as it stood at record time `at` (the rebuild of the entries
    recorded at or before `at`, not validated), read at world time `at`. `query` is a WorldQuery."""
    cutoff = _instant(at)
    if cutoff is None:
        raise ValueError("an as-of time is needed")
    entries, values = read_history(query, known_at=cutoff)
    return record_axis(rebuild(entries, values, known_at=cutoff, validate=False), cutoff)
