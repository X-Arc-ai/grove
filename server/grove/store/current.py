"""current(): the one function in code that says what is current (D3, spec line 832).

Every read and every guard asks this module, so no reader filters on its own (spec line 2758).
`standing` is pure: it takes an item's row and the time to read it at. Its rules, in order:

1. ended: an end class of any kind, a terminal status role, or a stop clock. `spent` counts too:
   it ends a record on the record axis only (D3), so an as-of read on the record axis decides
   which row it passes in.
2. overtaken: an active "corrected by" or "superseded by" link to a successor.
3. the scheduled class reads its evidence first, and no date moves it (D4; D5 rule 3, spec 1075,
   1187). The class is a bounded period, or a record that a plan scheduled: one with a planned
   origin (an active supporting row of source type planned, which stays when a report arrives)
   or a scheduled start. Confirmed and not inferred goes on to rule 4, and a period needs a known
   start for that, because a period gets its start only when a source confirms it took place.
   Reported done and not inferred reads reported. Anything else reads planned.
4. not_started: a known start wholly after the time read.
5. current. Outside the scheduled class an unknown start counts as started.

The successors ride on every state, so no read serves an overtaken claim without them. A plan is
work with a state: it became true when it was made, so it reads current once made (D4).
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from grove import clock
from grove.store.primitives import (
    KIND_BELIEF,
    MEANING_CORRECTED_BY,
    MEANING_SUPERSEDED_BY,
    PRIMITIVE_KIND_BELIEF,
)
from grove.store.query import WorldQuery

if TYPE_CHECKING:
    from grove.writer.tx import WorldTx

TERMINAL_ROLES = frozenset({"done", "cancelled", "ended"})
SUCCESSOR_MEANINGS: tuple[str, ...] = (MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY)
STATES: tuple[str, ...] = ("current", "planned", "reported", "not_started", "ended", "overtaken")

_UNKNOWN = "unknown"
# A refuted link was never true, so a stop clock after the time read cannot make it stand then.
_NEVER_TRUE = "refuted"

_ITEMS = "MATCH (i:Item) WHERE i.id IN $ids RETURN i"
_KINDS = "MATCH (k:Kind) WHERE k.id IN $ids RETURN k.id AS id, k.lifecycle_class AS lifecycle"
_SCHEDULED_NAMES = (
    "MATCH (p:PropertyName) WHERE p.kind IN $kinds AND p.scheduled_role IS NOT NULL "
    "RETURN p.kind AS kind, p.name AS name, p.scheduled_role AS role ORDER BY p.name"
)
_PLANNED_ORIGIN = (
    "MATCH (e:Evidence) WHERE e.target_type = 'item' AND e.target_id IN $ids "
    "AND e.polarity = 'supports' AND e.active = true AND e.source_type = 'planned' "
    "RETURN DISTINCT e.target_id AS id"
)
_SUCCESSORS = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item IN $ids AND l.meaning IN $meanings "
    "AND l.active_key IS NOT NULL "
    "RETURN l.from_item AS item_id, l.to_item AS successor_id, l.id AS link_id, "
    "l.meaning AS meaning ORDER BY l.recorded, l.id"
)


@dataclass(frozen=True)
class Successor:
    """The item that carries the newer truth, and the link that points at it."""

    item_id: str
    link_id: str
    meaning: str


@dataclass(frozen=True)
class Standing:
    """`reason` is a code: end_class, terminal_status or stop_clock (ended; inactive for a link
    with no active key); successor (overtaken); inferred, start_unknown or not_confirmed
    (planned); reported_done (reported); future_start (not_started); started (current)."""

    state: str
    end_class: str | None
    status_role: str | None
    scheduled: dict[str, Any] = field(hash=False)
    successors: tuple[Successor, ...]
    reason: str

    @property
    def is_current(self) -> bool:
        return self.state == "current"


def read_rows(source: "WorldQuery | WorldTx", cypher: str, /, **params: Any) -> list[dict]:
    """A WorldQuery reads in its own transaction; a WorldTx reads in the caller's, and so sees
    the writes that transaction has made and not yet committed."""
    return (getattr(source, "read", None) or source.run)(cypher, **params)


def ids_of(values: Iterable[str]) -> list[str]:
    """The ids once each, in order. A bare string is refused: it would read as its letters."""
    if isinstance(values, str):
        raise TypeError("pass a list of ids, not one id")
    return list(dict.fromkeys(values))


def _instant(value: Any) -> datetime | None:
    # A head row holds datetimes; an as-of row holds the history's ISO text.
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a standing is read at an aware time")
    return at


def _ended_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("end_class"):
        return "end_class"
    if row.get("status_role") in TERMINAL_ROLES:
        return "terminal_status"
    if row.get("stopped_being_true_precision"):
        return "stop_clock"
    return None


def has_ended(row: Mapping[str, Any]) -> bool:
    """Rule 1 alone. The writer's guards call it, so a read and a guard never disagree."""
    return _ended_reason(row) is not None


def _known_start(row: Mapping[str, Any]) -> bool:
    return row.get("became_true_precision") not in (None, _UNKNOWN)


def in_scheduled_class(
    lifecycle_class: str | None, *, planned_origin: bool, scheduled: Mapping[str, Any]
) -> bool:
    if lifecycle_class == "bounded_period":
        return True
    return lifecycle_class == "record" and (planned_origin or "start" in scheduled)


def _by_evidence(row: Mapping[str, Any], lifecycle_class: str | None) -> tuple[str, str] | None:
    """Rule 3: the state the evidence gives, or None when the evidence lets rule 4 decide."""
    if row.get("derivation") == "inferred":
        return "planned", "inferred"
    source_type = row.get("derived_source_type")
    if source_type == "confirmed":
        if lifecycle_class == "bounded_period" and not _known_start(row):
            return "planned", "start_unknown"
        return None
    if source_type == "reported_done":
        return "reported", "reported_done"
    return "planned", "not_confirmed"


def standing(
    row: Mapping[str, Any],
    *,
    at: datetime,
    lifecycle_class: str | None,
    planned_origin: bool = False,
    scheduled: Mapping[str, Any] | None = None,
    successors: Iterable[Successor] = (),
) -> Standing:
    """The item's state at `at`. `row` is a head row, or a row from
    grove.store.asof.item_as_of; `scheduled` is {role: value} of its scheduled properties, which
    rides on the result and never decides it."""
    at = _aware(at)
    held = dict(scheduled or {})
    after = tuple(successors)

    def made(state: str, reason: str) -> Standing:
        return Standing(state, row.get("end_class"), row.get("status_role"), held, after, reason)

    ended = _ended_reason(row)
    if ended is not None:
        return made("ended", ended)
    if after:
        return made("overtaken", "successor")
    if in_scheduled_class(lifecycle_class, planned_origin=planned_origin, scheduled=held):
        decided = _by_evidence(row, lifecycle_class)
        if decided is not None:
            return made(*decided)
    start = _instant(row.get("became_true_earliest"))
    if start is not None and start > at:
        return made("not_started", "future_start")
    return made("current", "started")


def link_standing(row: Mapping[str, Any], *, at: datetime) -> Standing:
    """A link's state at `at`. An ended link whose stop clock lies wholly after `at` still stood
    then: a slot link ended at a new link's future start is one."""
    at = _aware(at)

    def made(state: str, reason: str) -> Standing:
        return Standing(state, row.get("end_class"), None, {}, (), reason)

    if not row.get("active_key") or row.get("end_class"):
        stop = _instant(row.get("stopped_being_true_earliest"))
        stood = stop is not None and stop > at and row.get("end_class") != _NEVER_TRUE
        if not stood:
            return made("ended", "end_class" if row.get("end_class") else "inactive")
    start = _instant(row.get("became_true_earliest"))
    if start is not None and start > at:
        return made("not_started", "future_start")
    return made("current", "started")


def _scheduled(item: Mapping[str, Any], names: list[dict[str, Any]]) -> dict[str, Any]:
    properties = json.loads(item.get("properties") or "{}")
    return {
        name["role"]: properties[name["name"]]
        for name in names
        if name["kind"] == item.get("kind") and properties.get(name["name"]) not in (None, "")
    }


def standings(source: "WorldQuery | WorldTx", item_ids: Iterable[str]) -> dict[str, Standing]:
    """The standing of each item's head row at the world's clock.now. An id with no item is left
    out. Inside a write transaction it reads that transaction's own writes."""
    ids = ids_of(item_ids)
    if not ids:
        return {}
    items = {row["i"]["id"]: row["i"] for row in read_rows(source, _ITEMS, ids=ids)}
    if not items:
        return {}
    found = list(items)
    kinds = sorted({row["kind"] for row in items.values()})
    lifecycle = {row["id"]: row["lifecycle"] for row in read_rows(source, _KINDS, ids=kinds)}
    # The belief kind is a primitive row, which a world read cannot see.
    lifecycle[KIND_BELIEF] = PRIMITIVE_KIND_BELIEF["lifecycle_class"]
    names = read_rows(source, _SCHEDULED_NAMES, kinds=kinds)
    planned = {row["id"] for row in read_rows(source, _PLANNED_ORIGIN, ids=found)}
    after: dict[str, list[Successor]] = {}
    for row in read_rows(source, _SUCCESSORS, ids=found, meanings=list(SUCCESSOR_MEANINGS)):
        after.setdefault(row["item_id"], []).append(
            Successor(row["successor_id"], row["link_id"], row["meaning"])
        )
    at = clock.now(next(iter(items.values()))["world_id"])
    return {
        item_id: standing(
            items[item_id],
            at=at,
            lifecycle_class=lifecycle.get(items[item_id]["kind"]),
            planned_origin=item_id in planned,
            scheduled=_scheduled(items[item_id], names),
            successors=after.get(item_id, ()),
        )
        for item_id in ids
        if item_id in items
    }


def current_items(source: "WorldQuery | WorldTx", item_ids: Iterable[str]) -> list[str]:
    """The ids, in order, whose standing is current."""
    ids = ids_of(item_ids)
    found = standings(source, ids)
    return [item_id for item_id in ids if item_id in found and found[item_id].is_current]
