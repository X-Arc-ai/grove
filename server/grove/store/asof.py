"""As-of reads on the world axis (D1, D3): an item's columns replayed from its change entries.

The create entry holds the row as it was written. Every later entry names one field and its new
value. For each field, the value with the latest world time at or before the asked time wins, ties
broken by sequence (commit order); an entry that went to history only counts like any other. A
field with no entry at or before the asked time keeps the value the create wrote.
"""

import json
from datetime import UTC, datetime
from typing import Any

from grove.store.query import WorldQuery

_CLOCK_FIELDS = ("became_true", "stopped_being_true")
_BOUNDS = ("earliest", "latest", "precision")
# Entries that record an event on the row, not a column value.
_NOT_COLUMNS = frozenset({"row", "reopened", "restated"})


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
