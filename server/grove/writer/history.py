"""The change history (D3): append-only entries whose values live in history values by id.

An entry's old and new values are JSON in a :HistoryValue. A value that holds an old value of an
item's content column also gets the label :PastValue and the item's id (S18). Free text that is
not a column value (a reason, the original words of a clock) is stored as its own text.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grove.ids import new_id
from grove.writer.tx import WorldTx

# Item columns whose old values are past values (S18, line 1387).
CONTENT_FIELDS = frozenset({"title", "summary", "body", "properties", "search_terms"})
_ABSENT = object()

_ADD_VALUE = "CREATE (v:HistoryValue) SET v += $row"
_ADD_PAST_VALUE = "CREATE (v:HistoryValue:PastValue) SET v += $row"
_ADD_ENTRY = "CREATE (e:ChangeEntry) SET e += $entry"


@dataclass(frozen=True)
class Stamp:
    """What every entry of one batch shares: one commit and one change set (S10)."""

    world_id: str
    at: datetime
    actor: str
    commit_id: str
    change_set_id: str
    request_id: str | None = None
    run_id: str | None = None
    # The caller's kind (B.2's letters), for an operation whose rule differs by caller (O9).
    caller_kind: str | None = None


def _iso(value: Any) -> Any:
    """Times (datetime, date, and the driver's own time types) as ISO text."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"no JSON form for {type(value).__name__}")


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_iso)


def compact(row: dict[str, Any]) -> dict[str, Any]:
    """A row as it is stored: no null, and no empty list (S16)."""
    return {key: value for key, value in row.items() if value is not None and value != []}


def add_text(tx: WorldTx, stamp: Stamp, text: str) -> str:
    value_id = new_id(stamp.world_id)
    tx.run(_ADD_VALUE, row={"id": value_id, "value": text, "version": 1, "touched_at": stamp.at})
    return value_id


def add_value(tx: WorldTx, stamp: Stamp, value: Any, *, past_of_item: str | None = None) -> str:
    value_id = new_id(stamp.world_id)
    row = {"id": value_id, "value": encode(value), "version": 1, "touched_at": stamp.at}
    if past_of_item is None:
        tx.run(_ADD_VALUE, row=row)
    else:
        tx.run(_ADD_PAST_VALUE, row={**row, "item_id": past_of_item})
    return value_id


def row_json(table: str, row: dict[str, Any], world_id: str) -> dict[str, Any]:
    """The value of a create entry, the form the seed uses: what a rebuild reads the row from."""
    return {"table": table, "row": {**compact(row), "world_id": world_id}}


def add_entry(
    tx: WorldTx,
    stamp: Stamp,
    *,
    entity_type: str,
    entity_id: str,
    field: str,
    new: Any = _ABSENT,
    old: Any = _ABSENT,
    world_time: datetime | None,
    reason_id: str | None = None,
    evidence_id: str | None = None,
    history_only: bool = False,
    inverse_of: str | None = None,
) -> None:
    past_of = entity_id if entity_type == "item" and field in CONTENT_FIELDS else None
    old_id = None
    if old is not _ABSENT and old is not None:
        old_id = add_value(tx, stamp, old, past_of_item=past_of)
    new_id_ = None if new is _ABSENT else add_value(tx, stamp, new)
    entry = {
        "sequence": tx.next_sequence(),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field": field,
        "old_value_id": old_id,
        "new_value_id": new_id_,
        "recorded_at": stamp.at,
        "world_time": world_time,
        "reason": reason_id,
        "evidence_id": evidence_id,
        "actor": stamp.actor,
        "request_id": stamp.request_id,
        "run_id": stamp.run_id,
        "commit_id": stamp.commit_id,
        "change_set_id": stamp.change_set_id,
        "inverse_of": inverse_of,
        "history_only": history_only,
    }
    tx.run(_ADD_ENTRY, entry=compact(entry))
