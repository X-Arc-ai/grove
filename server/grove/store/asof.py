"""As-of reads on the world axis (D1, D3): an item's columns replayed from its change entries.

The create entry holds the row as it was written. Every later entry names one field and its new
value. For each field, the value with the latest world time at or before the asked time wins, ties
broken by sequence (commit order); an entry that went to history only counts like any other. A
field with no entry at or before the asked time keeps the value the create wrote.

world_as_of answers "what was true at T" with everything Grove knows now (D3, "Two time axes,
kept apart"). An item's content is its replay at T. Its ending is current knowledge: the head's
end class, read at T by its stop clock, decides it, never a replayed end class. The record axis
(what Grove believed at T) is grove.store.replay.known_as_of.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from grove.store.axes import AsOf, AsOfRow
from grove.store.current import (
    SUCCESSOR_MEANINGS,
    TERMINAL_ROLES,
    Standing,
    Successor,
    ids_of,
    link_standing,
    standings_of,
)
from grove.store.query import WorldQuery

_CLOCK_FIELDS = ("became_true", "stopped_being_true")
_BOUNDS = ("earliest", "latest", "precision")
# Entries that record an event on the row, not a column value (the markers of the entry
# vocabulary: reopened and merged_into on an item, restated on a link).
_NOT_COLUMNS = frozenset({"row", "reopened", "merged_into", "restated"})

# A refuted item or link was never true. A spent record ended on the record axis only (D3).
NEVER_TRUE = "refuted"
RECORD_AXIS_ONLY = "spent"
_STOP_COLUMNS = (
    "stopped_being_true_earliest",
    "stopped_being_true_latest",
    "stopped_being_true_precision",
    "stopped_being_true_words",
)
# The states of a row that did not stand at the time read (AsOf keeps it only when overtaken).
_DID_NOT_STAND = frozenset({"ended", "not_started"})

_HEAD_ITEMS = "MATCH (i:Item) RETURN i"
_HEAD_ITEMS_IN = "MATCH (i:Item) WHERE i.id IN $ids RETURN i"
_HEAD_LINKS = "MATCH (:Item)-[l:LINK]->(:Item) RETURN l"
_HEAD_LINKS_OF = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item IN $ids OR l.to_item IN $ids RETURN l"
)
_ITEM_ENTRIES = (
    "MATCH (e:ChangeEntry {entity_type: 'item'}) WHERE e.entity_id IN $ids "
    "RETURN e.entity_id AS entity_id, e.sequence AS sequence, e.field AS field, "
    "e.new_value_id AS value_id, e.world_time AS world_time ORDER BY e.sequence"
)
_LINK_END_ENTRIES = (
    "MATCH (e:ChangeEntry {entity_type: 'link', field: 'end_class'}) WHERE e.entity_id IN $ids "
    "RETURN e.entity_id AS entity_id, e.sequence AS sequence, e.field AS field, "
    "e.new_value_id AS value_id, e.world_time AS world_time ORDER BY e.sequence"
)
_VALUES = "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value"
# The evidence rows active now whose source spoke at or before the time read: what the claim
# rested on then, as far as Grove knows now.
_EVIDENCE_AT = (
    "MATCH (e:Evidence) WHERE e.target_id IN $ids AND e.polarity = 'supports' "
    "AND e.active = true AND e.source_time <= $at "
    "RETURN e.id AS id, e.target_id AS target_id, e.source_type AS source_type, "
    "e.derivation AS derivation, e.recorded AS recorded, e.speaker_kind AS speaker_kind, "
    "e.modality AS modality"
)


def _instant(value: datetime | str) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("an as-of time needs its offset")
    return at.astimezone(UTC)


def _decode(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


# The public names of the two helpers above, for the other history reads.
aware_instant = _instant
decode_value = _decode


def _put(row: dict[str, Any], field: str, value: Any) -> None:
    if field in _CLOCK_FIELDS:
        bounds = value if isinstance(value, dict) else {}
        if value == "unknown":
            bounds = {"precision": "unknown"}
        for bound in _BOUNDS:
            key = f"{field}_{bound}"
            if bounds.get(bound) is None:
                row.pop(key, None)
            else:
                row[key] = bounds[bound]
    elif value is None:
        row.pop(field, None)
    elif field == "properties":
        row[field] = json.dumps(value, sort_keys=True, ensure_ascii=False)
    else:
        row[field] = value


def item_as_of(query: WorldQuery, item_id: str, world_time: datetime | str) -> dict | None:
    """The item's columns as they stood at `world_time`, or None when it held nothing then.
    Times in the result are ISO text, as the history holds them."""
    entries = query.read(
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) "
        "RETURN e.sequence AS sequence, e.field AS field, e.new_value_id AS value_id, "
        "e.world_time AS world_time ORDER BY e.sequence",
        id=item_id,
    )
    value_ids = [entry["value_id"] for entry in entries if entry["value_id"]]
    values = {
        row["id"]: row["value"]
        for row in query.read(
            "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
            ids=value_ids,
        )
    }
    return replay(entries, values, world_time)


def replay(
    entries: list[dict[str, Any]], values: dict[str, str | None], world_time: datetime | str
) -> dict | None:
    """The replay itself, on entries (sequence, field, value_id, world_time) in sequence order
    and the stored text of their values. The writer calls it inside its own transaction."""
    at = _instant(world_time)
    created = next((entry for entry in entries if entry["field"] == "row"), None)
    if created is None:
        return None
    decoded = {value_id: _decode(text) for value_id, text in values.items()}
    stored = decoded.get(created["value_id"]) or {}
    row = dict(stored.get("row", {}))
    born = created["world_time"]
    born_key = (born, created["sequence"]) if born is not None else None

    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        when = entry["world_time"]
        if entry["field"] in _NOT_COLUMNS or when is None or when > at:
            continue
        held = latest.get(entry["field"])
        if held is None or (when, entry["sequence"]) > (held["world_time"], held["sequence"]):
            latest[entry["field"]] = entry
    if born is not None and born > at and not latest:
        return None
    for field, entry in latest.items():
        newer_create = born_key is not None and born <= at
        if newer_create and (entry["world_time"], entry["sequence"]) < born_key:
            continue
        _put(row, field, decoded.get(entry["value_id"]))
    return row


# The world axis -------------------------------------------------------------------------------


def _text_instants(row: Mapping[str, Any]) -> dict[str, Any]:
    """A head row with its instants as ISO text, the form the history holds and replay returns."""
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _point(value: datetime | str | None) -> datetime | None:
    return None if value is None else _instant(value)


def end_instant(head: Mapping[str, Any], end_world_time: datetime | str | None) -> datetime | None:
    """Where a row's ending sits on the world axis (S48), for items and links alike.

    The stop clock places the end: its earliest bound, else its latest (a `before` clock). With
    no bound (no stop clock, or an unknown one) the world time of the entry that set the end class
    places it. None when there is neither: Grove knows of no time the row was still true."""
    stop = _point(head.get("stopped_being_true_earliest")) or _point(
        head.get("stopped_being_true_latest")
    )
    return stop if stop is not None else _point(end_world_time)


def ending_holds(head: Mapping[str, Any], end_world_time: datetime | None, at: datetime) -> bool:
    """Whether the head's end class has ended the item by `at` on the world axis: the ending
    placed by `end_instant`, or at every time when it has no place."""
    stop = end_instant(head, end_world_time)
    return stop is None or stop <= at


def placed_link(
    link: Mapping[str, Any], end_world_time: datetime | str | None
) -> Mapping[str, Any]:
    """The link row that `link_standing` reads, on either axis (S48).

    A link that stopped being true in the world (an end class other than refuted, and a stop
    clock) has its ending placed as an item's is, by `end_instant`: a stop clock with no earliest
    bound (`before`, or `unknown`) still stood before that place. A refuted link never stood. A
    link ended with no stop clock (a merge moved its claim onto the survivor's copy) stands at no
    time on its own. Only the standing reads this row: a result keeps the link's own row."""
    if not _placeable(link):
        return link
    placed = end_instant(link, end_world_time)
    return link if placed is None else {**link, "stopped_being_true_earliest": placed}


def _placeable(link: Mapping[str, Any]) -> bool:
    """A link ending that `placed_link` places: ended in the world, with a stop clock that has no
    earliest bound (a link with one `link_standing` places itself)."""
    return (
        link.get("end_class") not in (None, NEVER_TRUE)
        and link.get("stopped_being_true_precision") is not None
        and link.get("stopped_being_true_earliest") is None
    )


def _set_end_times(
    entries: Iterable[Mapping[str, Any]], values: Mapping[str, str | None]
) -> dict[str, datetime | None]:
    """Per entity, the world time of the entry that last set a non-null end class (entries in
    sequence order; None when that entry has no world time)."""
    found: dict[str, datetime | None] = {}
    for entry in entries:
        if entry["field"] == "end_class" and _decode(values.get(entry["value_id"])) is not None:
            found[entry["entity_id"]] = _point(entry["world_time"])
    return found


def _ended_as_of(
    row: dict[str, Any], head: Mapping[str, Any], end_world_time: datetime | None, at: datetime
) -> dict[str, Any]:
    """The replayed row with its ending from current knowledge (D3, section 3 of the lane design).

    A replayed end class never decides: the head's does, by ending_holds. A spent end class is
    ignored on this axis. While no end class holds and the replayed status role is not terminal,
    a replayed stop clock is dropped: the writer's head never holds a stop clock in that state
    (apply._clear_stop), so a late terminal entry that went to history only ends the item only
    while its status stands."""
    row = dict(row)
    row.pop("end_class", None)
    end_class = head.get("end_class")
    if end_class and end_class != RECORD_AXIS_ONLY and ending_holds(head, end_world_time, at):
        row["end_class"] = end_class
        for column in _STOP_COLUMNS:
            value = head.get(column)
            if value is None:
                row.pop(column, None)
            else:
                row[column] = value.isoformat() if isinstance(value, datetime) else value
    elif row.get("status_role") not in TERMINAL_ROLES:
        for column in _STOP_COLUMNS:
            row.pop(column, None)
    return row


def _values(query: WorldQuery, value_ids: Iterable[str]) -> dict[str, str | None]:
    ids = sorted(set(value_ids))
    if not ids:
        return {}
    return {row["id"]: row["value"] for row in query.read(_VALUES, ids=ids)}


def _replayed(
    query: WorldQuery, item_ids: list[str], at: datetime
) -> tuple[dict[str, dict[str, Any]], dict[str, datetime | None]]:
    """Each item's replay at `at` (None ones left out), and the world time of the entry that last
    set an end class on it (None when no entry did, or when that entry has no world time)."""
    if not item_ids:
        return {}, {}
    entries = query.read(_ITEM_ENTRIES, ids=item_ids)
    values = _values(query, (entry["value_id"] for entry in entries if entry["value_id"]))
    by_item: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_item.setdefault(entry["entity_id"], []).append(entry)
    rows: dict[str, dict[str, Any]] = {}
    ended_at: dict[str, datetime | None] = {}
    for item_id, own in by_item.items():
        mine = {entry["value_id"]: values.get(entry["value_id"]) for entry in own}
        mine.pop(None, None)
        row = replay(own, mine, at)
        if row is None:
            continue
        rows[item_id] = row
        ended_at[item_id] = _set_end_times(own, mine).get(item_id)
    return rows, ended_at


def _link_end_times(
    query: WorldQuery, links: Iterable[Mapping[str, Any]]
) -> dict[str, datetime | None]:
    """`_set_end_times` for the links whose ending `placed_link` places."""
    ids = sorted(link["id"] for link in links if _placeable(link))
    if not ids:
        return {}
    entries = query.read(_LINK_END_ENTRIES, ids=ids)
    values = _values(query, (entry["value_id"] for entry in entries if entry["value_id"]))
    return _set_end_times(entries, values)


def _evidence_fields(query: WorldQuery, rows: dict[str, dict[str, Any]], at: datetime) -> set[str]:
    """Sets each row's derived claim fields from the supporting rows active now whose source time
    is at or before `at`; returns the ids with a planned origin then."""
    # Imported here: grove.writer imports this module (apply.replay), so a module-level import
    # would close a cycle when this module loads first.
    from grove.writer.source_types import claim_fields

    found = query.read(_EVIDENCE_AT, ids=list(rows), at=at)
    by_target: dict[str, list[dict[str, Any]]] = {}
    for evidence in found:
        by_target.setdefault(evidence["target_id"], []).append(evidence)
    for item_id, row in rows.items():
        for key, value in claim_fields(by_target.get(item_id, [])).items():
            if value is None:
                row.pop(key, None)
            else:
                row[key] = value
    return {evidence["target_id"] for evidence in found if evidence["source_type"] == "planned"}


def _stood(found: Standing) -> bool:
    return found.state not in _DID_NOT_STAND or bool(found.successors)


def world_as_of(
    query: WorldQuery, at: datetime | str, *, item_ids: Iterable[str] | None = None
) -> AsOf:
    """What was true at world time `at`, with everything Grove knows now (axis "world").

    Items: content from `replay` at `at`, the ending from current knowledge (`_ended_as_of`), a
    refuted item never (left out), and an item whose create entry's world time is after `at` left
    out. Derived claim fields and the planned origin come from the evidence rows active now whose
    source time is at or before `at`. Successors are the successor links that stand at `at`.
    Links: the head row, kept when `link_standing` at `at` is current, with the ending placed by
    `placed_link` (a refuted link never is).
    A row that did not stand at `at` is left out, except an item with successors then. Every
    instant in a result row is ISO text. With `item_ids`, only those items, and the links with an
    end among them."""
    at = _instant(at)
    if item_ids is None:
        heads = {row["i"]["id"]: row["i"] for row in query.read(_HEAD_ITEMS)}
        links = [row["l"] for row in query.read(_HEAD_LINKS)]
    else:
        ids = ids_of(item_ids)
        heads = {row["i"]["id"]: row["i"] for row in query.read(_HEAD_ITEMS_IN, ids=ids)}
        links = [row["l"] for row in query.read(_HEAD_LINKS_OF, ids=ids)] if ids else []

    replayed, ended_at = _replayed(query, list(heads), at)
    items = {
        item_id: _ended_as_of(row, heads[item_id], ended_at.get(item_id), at)
        for item_id, row in replayed.items()
        if heads[item_id].get("end_class") != NEVER_TRUE
    }

    standing_links: dict[str, AsOfRow] = {}
    successors: dict[str, list[Successor]] = {}
    end_times = _link_end_times(query, links)
    for link in sorted(links, key=lambda row: (str(row.get("recorded") or ""), row["id"])):
        row = _text_instants(link)
        found = link_standing(placed_link(row, end_times.get(link["id"])), at=at)
        if found.state != "current":
            continue
        standing_links[link["id"]] = AsOfRow(row, found)
        if link.get("meaning") in SUCCESSOR_MEANINGS and link.get("from_item") in items:
            successors.setdefault(link["from_item"], []).append(
                Successor(link["to_item"], link["id"], link["meaning"])
            )

    planned = _evidence_fields(query, items, at) if items else set()
    found_items = standings_of(
        query, items.values(), at=at, planned_origin=planned, successors=successors
    )
    kept = {
        item_id: AsOfRow(items[item_id], found)
        for item_id, found in found_items.items()
        if _stood(found)
    }
    return AsOf("world", at, items=kept, links=standing_links)
