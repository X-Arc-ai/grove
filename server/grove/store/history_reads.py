"""History reads (D3): one item's story, what changed in a span of record time, and what Grove
stopped believing.

Every value comes from the history values by id, decoded: a column value is JSON, and a reason is
its own text. A forgotten value reads as the forget marker, so these reads never hold a value the
forget removed (D14). The spans are on the record axis: an entry counts by its `recorded_at`.
"""

from datetime import datetime
from typing import Any

from grove.store.asof import NEVER_TRUE, aware_instant, decode_value
from grove.store.current import standings
from grove.store.query import WorldQuery

KINDS: tuple[str, ...] = ("created", "changed", "ended", "reopened", "undone")
# Endings of these entities can say Grove stopped believing a claim; an evidence row ends with a
# reason, not an end class.
_CLAIMS = frozenset({"item", "link"})

_ENTRY = (
    "e.sequence AS sequence, e.entity_type AS entity_type, e.entity_id AS entity_id, "
    "e.field AS field, e.old_value_id AS old_value_id, e.new_value_id AS new_value_id, "
    "e.reason AS reason_id, e.actor AS actor, e.recorded_at AS recorded_at, "
    "e.world_time AS world_time, e.evidence_id AS evidence_id, "
    "e.change_set_id AS change_set_id, e.inverse_of AS inverse_of, "
    "e.history_only AS history_only"
)
_LINKS_OF = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item = $id OR l.to_item = $id RETURN l.id AS id"
)
_EVIDENCE_OF = "MATCH (e:Evidence) WHERE e.target_id IN $ids RETURN e.id AS id"
_BELIEFS_OF = "MATCH (b:Belief {item_id: $id}) RETURN b.item_id AS id"
_ENTRIES_OF = (
    "MATCH (e:ChangeEntry) WHERE (e.entity_type = 'item' AND e.entity_id = $id) "
    "OR (e.entity_type = 'link' AND e.entity_id IN $links) "
    "OR (e.entity_type = 'evidence' AND e.entity_id IN $evidence) "
    "OR (e.entity_type = 'belief' AND e.entity_id IN $beliefs) "
    f"RETURN {_ENTRY} ORDER BY e.sequence"
)
_ENTRIES_BETWEEN = (
    "MATCH (e:ChangeEntry) WHERE e.recorded_at >= $start AND e.recorded_at < $end "
    f"RETURN {_ENTRY} ORDER BY e.sequence"
)
_VALUES = "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value"
# The fields whose new value changes_between reads.
_DECIDING = ("end_class", "end_reason", "stopped_being_true")


class ItemHistory(list[dict[str, Any]]):
    """item_history's answer: the entries, in sequence, as a list of dicts, and `successors`, the
    item's successors now ([{"id", "link_id", "meaning"}], empty when none)."""

    def __init__(self, entries: list[dict[str, Any]], successors: list[dict[str, str]]) -> None:
        super().__init__(entries)
        self.successors = successors


def _values(query: WorldQuery, value_ids: set[str | None]) -> dict[str, str | None]:
    ids = sorted(value_id for value_id in value_ids if value_id)
    if not ids:
        return {}
    return {row["id"]: row["value"] for row in query.read(_VALUES, ids=ids)}


def _value(value_id: str | None, values: dict[str, str | None]) -> Any:
    return None if value_id is None else decode_value(values.get(value_id))


def _reason(reason: str | None, values: dict[str, str | None]) -> str | None:
    """A reason is a value id whose value is its text. A seeded entry holds its reason as plain
    text (grove.writer.worlds), so a reason with no value row is read as it stands."""
    if reason is None:
        return None
    return values.get(reason, reason)


def _entry(row: dict[str, Any], values: dict[str, str | None]) -> dict[str, Any]:
    return {
        "sequence": row["sequence"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "field": row["field"],
        "old": _value(row["old_value_id"], values),
        "new": _value(row["new_value_id"], values),
        "reason": _reason(row["reason_id"], values),
        "actor": row["actor"],
        "recorded_at": row["recorded_at"],
        "world_time": row["world_time"],
        "evidence_id": row["evidence_id"],
        "change_set_id": row["change_set_id"],
        "inverse_of": row["inverse_of"],
        "history_only": bool(row["history_only"]),
    }


def successor_rows(query: WorldQuery, item_id: str) -> list[dict[str, str]]:
    """The item's successors now, by grove.store.current: [{"id", "link_id", "meaning"}]."""
    found = standings(query, [item_id]).get(item_id)
    if found is None:
        return []
    return [
        {"id": after.item_id, "link_id": after.link_id, "meaning": after.meaning}
        for after in found.successors
    ]


def item_history(query: WorldQuery, item_id: str) -> ItemHistory:
    """Every entry of the item, of its links (either end), of the evidence rows on the item and
    on those links, and of its belief row, in sequence, with old and new values decoded and the
    reason as text. `successors` on the answer are the item's successors now."""
    links = [row["id"] for row in query.read(_LINKS_OF, id=item_id)]
    evidence = [row["id"] for row in query.read(_EVIDENCE_OF, ids=[item_id, *links])]
    beliefs = [row["id"] for row in query.read(_BELIEFS_OF, id=item_id)]
    rows = query.read(_ENTRIES_OF, id=item_id, links=links, evidence=evidence, beliefs=beliefs)
    values = _values(
        query,
        {row[key] for row in rows for key in ("old_value_id", "new_value_id", "reason_id")},
    )
    return ItemHistory([_entry(row, values) for row in rows], successor_rows(query, item_id))


def _kind(by_field: dict[str, dict[str, Any]], inverse_of: str | None, ended: Any) -> str:
    if inverse_of is not None:
        return "undone"
    if "row" in by_field:
        return "created"
    if ended is not None:
        return "ended"
    if "reopened" in by_field or "end_class" in by_field or "end_reason" in by_field:
        # An end lifted: reopen_item writes its marker and a null end class.
        return "reopened"
    return "changed"


def _change(entries: list[dict[str, Any]], values: dict[str, str | None]) -> dict[str, Any]:
    first = entries[0]
    by_field: dict[str, dict[str, Any]] = {}
    for entry in entries:
        by_field[entry["field"]] = entry
    decided = {
        field: _value(by_field[field]["new_value_id"], values)
        for field in _DECIDING
        if field in by_field
    }
    end_class = decided.get("end_class")
    end_reason = decided.get("end_reason")
    inverse_of = next((entry["inverse_of"] for entry in entries if entry["inverse_of"]), None)
    kind = _kind(by_field, inverse_of, end_class if end_class is not None else end_reason)
    deciding = next(
        (
            by_field[field]
            for field in ("end_class", "end_reason", "row", "reopened")
            if field in by_field
        ),
        first,
    )
    reason = next((entry["reason_id"] for entry in entries if entry["reason_id"]), None)
    return {
        "entity_type": first["entity_type"],
        "entity_id": first["entity_id"],
        "change_set_id": first["change_set_id"],
        "kind": kind,
        "end_class": end_class,
        "end_reason": end_reason,
        "stopped_being_true": decided.get("stopped_being_true"),
        "fields": sorted(by_field),
        "sequence": first["sequence"],
        "recorded_at": first["recorded_at"],
        "world_time": deciding["world_time"],
        "actor": first["actor"],
        "reason": _reason(reason, values),
        "inverse_of": inverse_of,
    }


def changes_between(
    query: WorldQuery, start: datetime | str, end: datetime | str
) -> list[dict[str, Any]]:
    """The entries recorded in [start, end), one dict per (entity, change set), in sequence of
    their first entry. `kind` is created, changed, ended, reopened or undone; an ending carries
    its `end_class` (an item or a link: so "ended_in_the_world" and "refuted" are told apart) or
    its `end_reason` (an evidence row), and `stopped_being_true` when the change set set one."""
    start, end = aware_instant(start), aware_instant(end)
    if end < start:
        raise ValueError("the span ends before it starts")
    rows = query.read(_ENTRIES_BETWEEN, start=start, end=end)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["entity_type"], row["entity_id"], row["change_set_id"])
        groups.setdefault(key, []).append(row)
    wanted = {row["reason_id"] for row in rows} | {
        row["new_value_id"] for row in rows if row["field"] in _DECIDING
    }
    values = _values(query, wanted)
    return [_change(entries, values) for entries in groups.values()]


def stopped_believing(
    query: WorldQuery, start: datetime | str, end: datetime | str
) -> list[dict[str, Any]]:
    """The endings recorded in [start, end) whose end class is refuted, of items and links, in
    the shape of changes_between. An ending in the world, a retirement, a supersession or a spent
    record is no belief Grove gave up, so none is here."""
    return [
        change
        for change in changes_between(query, start, end)
        if change["entity_type"] in _CLAIMS and change["end_class"] == NEVER_TRUE
    ]
