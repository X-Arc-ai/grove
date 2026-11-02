"""The transaction body of one batch: each operation in order, checked under the write lock.

An operation reads and locks the rows it touches, checks them, and only then writes. A refused
operation is recorded and the loop goes on, so one decline names every refused operation; an
operation that reads a temp id of a refused one is refused as depending on it. Any refusal raises
BatchDeclined at the end, which rolls the whole transaction back.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grove.ids import new_id
from grove.store.asof import replay
from grove.store.current import TERMINAL_ROLES, has_ended
from grove.store.primitives import KIND_BELIEF, PRIMITIVE_MEANING_IDS
from grove.writer.clocks import (
    Clock,
    clock_columns,
    clock_from_row,
    ends_before_start,
    parse_clock,
    parse_instant,
    sort_key,
)
from grove.writer.declines import write_override_row
from grove.writer.errors import BatchDeclined, Refusal, Refused
from grove.writer.history import Stamp, add_entry, add_text, compact, row_json
from grove.writer.neardup import MAX_CANDIDATES_SHOWN, threshold_for
from grove.writer.ops import is_temp, op_label, temp_refs
from grove.writer.provenance import OBSERVATION_COLUMNS, resolve_observation
from grove.writer.receipts import short_id
from grove.writer.registry import Registry
from grove.writer.resolver import title_scores
from grove.writer.source_types import claim_fields
from grove.writer.tx import WorldTx

# Fields whose head follows the latest world time (D1); the others always move the head.
CLAIM_FIELDS = frozenset({"title", "summary", "body", "properties", "became_true"})

_LOCK_ITEM = "MATCH (i:Item {id: $id}) SET i.touched_at = $at RETURN i"
_LOCK_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) SET l.touched_at = $at RETURN l"
_LOCK_EVIDENCE = "MATCH (e:Evidence {id: $id}) SET e.touched_at = $at RETURN e"
_READ_ITEM = "MATCH (i:Item {id: $id}) RETURN i"
_READ_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l"
_SET_ITEM = "MATCH (i:Item {id: $id}) SET i += $columns"
_SET_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) SET l += $columns"
_BUMP_ITEM = "MATCH (i:Item {id: $id}) SET i.version = coalesce(i.version, 0) + 1"
_BUMP_LINK = "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) SET l.version = coalesce(l.version, 0) + 1"
_ADD_ITEM = "CREATE (i:Item) SET i += $row"
_ADD_LINK = "MATCH (a:Item {id: $a}) MATCH (b:Item {id: $b}) CREATE (a)-[l:LINK]->(b) SET l += $row"
_ADD_EVIDENCE = "CREATE (e:Evidence) SET e += $row"
_ADD_PAIR = "CREATE (d:DistinctPair) SET d += $row"
_END_EVIDENCE = (
    "MATCH (e:Evidence {id: $id}) SET e.active = false, e.ended_at = $at, "
    "e.end_reason = $reason, e.version = coalesce(e.version, 0) + 1"
)
_SAME_EVIDENCE = (
    "MATCH (e:Evidence {message_key: $key, target_id: $target, polarity: $polarity}) RETURN e"
)
_SUPPORTING = "MATCH (e:Evidence {target_id: $id, polarity: 'supports', active: true}) RETURN e"


def _before_commit() -> None:
    """The last statement inside the transaction body, from apply_ops or, for apply_batch, from
    api.py after the request's `saved`: a test replaces it to inject a failure or a kill, with
    everything written and nothing committed."""


@dataclass
class _Plan:
    """One evidence row an operation will write once its target exists."""

    id: str
    polarity: str
    columns: dict[str, Any]
    # True once the row is known to exist already (in the store, or earlier in this operation):
    # `id` then names that row and nothing is written for the plan.
    known: bool = False

    @property
    def source_time(self) -> datetime:
        return self.columns["source_time"]


class _Run:
    def __init__(
        self,
        tx: WorldTx,
        stamp: Stamp,
        observations: dict[str, dict[str, Any]],
    ) -> None:
        self.tx = tx
        self.stamp = stamp
        self.registry = Registry.load(tx)
        self.observations = observations
        self.resolved: dict[str, dict[str, Any] | Refused] = {}
        self.temps: dict[str, tuple[str, str]] = {}
        self.declined_temps: set[str] = set()
        self.refused: dict[int, Refusal] = {}
        self.summaries: dict[int, str] = {}
        self.touched_items: dict[str, int] = {}
        self.done_items: dict[str, int] = {}
        self.created_evidence: list[dict[str, Any]] = []
        # Rows that existed already and that an operation of this batch cited again.
        self.cited_evidence: list[dict[str, Any]] = []
        self.recompute: dict[str, str] = {}

    @property
    def at(self) -> datetime:
        return self.stamp.at

    def new_id(self) -> str:
        return new_id(self.stamp.world_id)

    def text(self, value: str) -> str:
        return add_text(self.tx, self.stamp, value)

    def entry(self, **fields: Any) -> None:
        add_entry(self.tx, self.stamp, **fields)

    def refuse(self, index: int, op: Any, refused: Refused) -> None:
        if index in self.refused:
            return
        op_id, name = op_label(op)
        self.refused[index] = refused.refusal(op_id, name)
        temp = op.get("temp_id") if isinstance(op, dict) else None
        if is_temp(temp):
            self.declined_temps.add(temp)

    def observation(self, ev_id: str) -> dict[str, Any]:
        found = self.resolved.get(ev_id)
        if found is None:
            try:
                found = resolve_observation(self.tx, self.observations[ev_id])
            except Refused as refused:
                found = refused
            self.resolved[ev_id] = found
        if isinstance(found, Refused):
            raise found
        return found

    def ref(self, value: str, expect: str) -> str:
        """An id, or the id a temp id of this batch stands for."""
        if not is_temp(value):
            return value
        if value in self.declined_temps:
            raise _depends(value)
        if value not in self.temps:
            raise Refused(
                "invalid_arguments",
                "unknown_temp_id",
                f"temp id {value} is not declared by an earlier operation",
                {"temp_id": value},
            )
        entity, real = self.temps[value]
        if expect != "any" and entity != expect:
            raise Refused(
                "invalid_arguments",
                "temp_id_type",
                f"temp id {value} names a {entity}, not a {expect}",
                {"temp_id": value},
            )
        return real

    def label(self, value: str) -> str:
        return value if is_temp(value) else short_id(value)

    def touch(self, item_id: str, index: int) -> None:
        self.touched_items[item_id] = index


def _depends(temp: str) -> Refused:
    return Refused(
        "invalid_arguments",
        "depends_on_declined",
        f"{temp} names an operation this batch declined",
        {"temp_id": temp},
    )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _property_values(properties: dict[str, Any] | None) -> list[str] | None:
    """What the full-text indexes read from properties (S16)."""
    if not properties:
        return None
    values: list[str] = []
    for key in sorted(properties):
        value = properties[key]
        if value is None:
            continue
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(part) for part in value if part is not None)
        elif isinstance(value, dict):
            values.append(json.dumps(value, sort_keys=True, ensure_ascii=False))
        else:
            values.append(str(value))
    return values or None


def _instant_or_none(value: Any) -> datetime | None:
    return None if value is None else parse_instant(value)


def _one(run: _Run, cypher: str, entity: str, entity_id: str, *, lock: bool) -> dict[str, Any]:
    params = {"id": entity_id, "at": run.at} if lock else {"id": entity_id}
    rows = run.tx.run(cypher, **params)
    if not rows:
        raise Refused(
            "not_found", f"{entity}_not_found", f"no {entity} {entity_id} in this world", {}
        )
    return next(iter(rows[0].values()))


def _lock_item(run: _Run, item_id: str) -> dict[str, Any]:
    return _one(run, _LOCK_ITEM, "item", item_id, lock=True)


def _check_version(op: dict[str, Any], row: dict[str, Any]) -> None:
    if "version" in op and op["version"] != row.get("version"):
        raise Refused(
            "version_conflict",
            "version_conflict",
            "the row changed since the caller read it",
            {"expected": op["version"], "found": row.get("version")},
        )


def _not_a_record(kind: dict[str, Any], what: str) -> None:
    if kind.get("lifecycle_class") == "record":
        raise Refused(
            "record_immutable", "record_immutable", f"a record's claim never changes ({what})", {}
        )


# Evidence -----------------------------------------------------------------------------------


def _existing_evidence(run: _Run, evidence_id: str) -> dict[str, Any]:
    rows = run.tx.run("MATCH (e:Evidence {id: $id}) RETURN e", id=evidence_id)
    if not rows or not rows[0]["e"].get("active"):
        raise Refused(
            "missing_provenance",
            "evidence_not_found",
            f"no active evidence row {evidence_id} in this world",
            {"evidence_id": evidence_id},
        )
    row = rows[0]["e"]
    columns = {key: row.get(key) for key in OBSERVATION_COLUMNS}
    columns["premises"] = list(columns.get("premises") or [])
    return columns


def _plans(run: _Run, refs: list[dict[str, Any]]) -> list[_Plan]:
    plans = []
    for ref in refs:
        if "ev_id" in ref:
            columns = run.observation(ref["ev_id"])
        else:
            columns = _existing_evidence(run, ref["evidence_id"])
        plans.append(_Plan(run.new_id(), ref["polarity"], columns))
    return plans


def _world_time(plans: list[_Plan]) -> datetime | None:
    """The time an operation speaks for: the newest source time among its supporting rows."""
    supporting = [plan.source_time for plan in plans if plan.polarity == "supports"]
    times = supporting or [plan.source_time for plan in plans]
    return max(times) if times else None


def _first_supporting(plans: list[_Plan]) -> str | None:
    return next((plan.id for plan in plans if plan.polarity == "supports"), None)


def _settle(run: _Run, plans: list[_Plan], target_id: str) -> None:
    """Point each plan whose (message key, target, polarity) row exists already at that row, so
    an entry or a provenance that names the plan names a real row (D2's unique key). Runs before
    any entry is written; a settled plan is never settled again."""
    seen: dict[tuple[str, str], str] = {}
    for plan in plans:
        key = (plan.columns["message_key"], plan.polarity)
        if plan.known:
            seen.setdefault(key, plan.id)
            continue
        if key in seen:
            plan.id, plan.known = seen[key], True
            continue
        same = run.tx.run(_SAME_EVIDENCE, key=key[0], target=target_id, polarity=plan.polarity)
        if same:
            row = same[0]["e"]
            plan.id, plan.known = row["id"], True
            run.cited_evidence.append(row)
        seen[key] = plan.id


def _write_evidence(
    run: _Run, plans: list[_Plan], target_type: str, target_id: str, reason_id: str | None
) -> list[str]:
    """One row per plan; a plan whose row exists already names that row and writes nothing."""
    _settle(run, plans, target_id)
    written = []
    for plan in plans:
        if plan.known:
            continue
        row = compact(
            {
                "id": plan.id,
                **plan.columns,
                "target_type": target_type,
                "target_id": target_id,
                "polarity": plan.polarity,
                "active": True,
                "recorded": run.at,
                "request_id": run.stamp.request_id,
                "version": 1,
                "touched_at": run.at,
            }
        )
        run.tx.run(_ADD_EVIDENCE, row=row)
        run.entry(
            entity_type="evidence",
            entity_id=plan.id,
            field="row",
            new=row_json("evidence", row, run.stamp.world_id),
            world_time=plan.source_time,
            reason_id=reason_id,
            evidence_id=plan.id,
        )
        run.created_evidence.append(row)
        written.append(plan.id)
    if written:
        run.recompute[target_id] = "link" if target_type == "link" else "item"
    return written


# Shared item checks ---------------------------------------------------------------------------


def _declared_property(run: _Run, kind: dict[str, Any], name: str) -> dict[str, Any]:
    declared = run.registry.property_name(kind["id"], name)
    if declared is None:
        raise Refused(
            "invalid_arguments",
            "unknown_property",
            f"kind {kind['name']!r} declares no property {name!r}",
            {"property": name, "valid": run.registry.property_names_of(kind["id"])},
        )
    return declared


def _check_properties(run: _Run, kind: dict[str, Any], properties: dict[str, Any]) -> dict:
    """The properties under their declared names. A state property sits only on a record, and a
    record holds at most one (D1, D4)."""
    named: dict[str, Any] = {}
    state: list[str] = []
    for name, value in properties.items():
        declared = _declared_property(run, kind, name)
        named[declared["name"]] = value
        if declared.get("temporal_class") == "state":
            state.append(declared["name"])
    if state and kind.get("lifecycle_class") != "record":
        raise Refused(
            "state_property_on_item",
            "state_property_on_item",
            "a state property sits only on a record: write a link of a state meaning or a "
            "measured-figure record",
            {"properties": state},
        )
    if len(state) > 1:
        raise Refused(
            "state_property_on_item",
            "one_per_record",
            "a record holds one state property",
            {"properties": state},
        )
    return named


def _required_fields(kind: dict[str, Any]) -> list[str]:
    names = []
    for entry in json.loads(kind.get("required_fields") or "[]"):
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.append(entry["name"])
    return names


def _has_field(item: dict[str, Any], name: str) -> bool:
    if item.get(name) not in (None, "", []):
        return True
    properties = json.loads(item.get("properties") or "{}")
    return properties.get(name) not in (None, "", [])


def _state_property(run: _Run, item: dict[str, Any]) -> bool:
    for name in json.loads(item.get("properties") or "{}"):
        declared = run.registry.property_name(item["kind"], name)
        if declared is not None and declared.get("temporal_class") == "state":
            return True
    return False


def _claim_about_now(run: _Run, item: dict[str, Any], kind: dict[str, Any]) -> bool:
    """D4's field-based cases, read from declared fields only."""
    if item.get("end_class"):
        return False
    lifecycle = kind.get("lifecycle_class")
    if lifecycle == "work_with_state":
        role = item.get("status_role")
        return role is not None and role not in TERMINAL_ROLES
    if lifecycle == "bounded_period":
        begun = item.get("became_true_precision") not in (None, "unknown")
        return begun and item.get("stopped_being_true_precision") is None
    if lifecycle == "record":
        return _state_property(run, item)
    return False


def _ended(item: dict[str, Any]) -> bool:
    # Reads and guards share one liveness rule (spec line 2758), so the guard asks current().
    return has_ended(item)


def _head_world_time(run: _Run, item_id: str, field: str) -> datetime | None:
    rows = run.tx.run(
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) "
        "WHERE e.history_only = false AND e.field IN $fields "
        "RETURN e.field AS field, e.world_time AS world_time",
        id=item_id,
        fields=[field, "row"],
    )
    own = [row["world_time"] for row in rows if row["field"] == field and row["world_time"]]
    if own:
        return max(own)
    created = [row["world_time"] for row in rows if row["field"] == "row" and row["world_time"]]
    return max(created) if created else None


def _properties_as_of(run: _Run, item_id: str, at: datetime) -> dict[str, Any]:
    """The item's properties replayed to a world time, inside this transaction."""
    entries = run.tx.run(
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) "
        "WHERE e.field IN ['row', 'properties'] "
        "RETURN e.sequence AS sequence, e.field AS field, e.new_value_id AS value_id, "
        "e.world_time AS world_time ORDER BY e.sequence",
        id=item_id,
    )
    ids = [entry["value_id"] for entry in entries if entry["value_id"]]
    values = {
        row["id"]: row["value"]
        for row in run.tx.run(
            "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
            ids=ids,
        )
    }
    row = replay(entries, values, at) or {}
    return json.loads(row.get("properties") or "{}")


def _bump(run: _Run, cypher: str, entity_id: str) -> None:
    run.tx.run(cypher, id=entity_id)


def _words(run: _Run, prefix: str, clock: Clock | None) -> dict[str, Any]:
    words = clock.words if clock is not None else None
    return {f"{prefix}_words": run.text(words) if words else None}


# create_item ----------------------------------------------------------------------------------


def _near_duplicates(
    run: _Run, kind: dict[str, Any], title: str, distinct_from: list[dict[str, Any]]
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """The named pairs, and the candidates at or above the threshold that a pair overrides.

    The gate weighs only items with no end class, at the resolution level. The resolver's
    candidate band scores with the same helper, and keeps ended items."""
    rows = run.tx.run(
        "MATCH (i:Item {kind: $kind}) WHERE i.end_class IS NULL "
        "RETURN i.id AS id, i.title AS title",
        kind=kind["id"],
    )
    scores = title_scores(run.tx, title, {row["id"]: row["title"] for row in rows})
    threshold = threshold_for(kind["id"])
    candidates = sorted(
        ((score, item_id) for item_id, score in scores.items() if score >= threshold),
        reverse=True,
    )
    named: dict[str, str] = {}
    for entry in distinct_from:
        named[run.ref(entry["item"], "item")] = entry["reason"]
    missing = [(score, item_id) for score, item_id in candidates if item_id not in named]
    if missing:
        raise Refused(
            "near_duplicate",
            "near_duplicate",
            "an item of this kind has a title this close: update it, or send distinct_from",
            {
                "candidates": [
                    {"item_id": item_id, "score": round(score, 3)}
                    for score, item_id in missing[:MAX_CANDIDATES_SHOWN]
                ],
                "threshold": threshold,
                "how": "update the existing item, or send distinct_from with a reason",
            },
        )
    for item_id in named:
        _one(run, _READ_ITEM, "item", item_id, lock=False)
    overridden = [
        {"item_id": item_id, "score": round(score, 3)}
        for score, item_id in candidates
        if item_id in named
    ]
    return list(named.items()), overridden


def _record_pairs(
    run: _Run, item_id: str, pairs: list[tuple[str, str]], evidence_id: str | None
) -> None:
    for other, reason in pairs:
        first, second = sorted((item_id, other))
        known = run.tx.run(
            "MATCH (d:DistinctPair {item_a: $a, item_b: $b}) RETURN d.item_a AS a",
            a=first,
            b=second,
        )
        if known:
            continue
        run.tx.run(
            _ADD_PAIR,
            row=compact(
                {
                    "item_a": first,
                    "item_b": second,
                    "reason": run.text(reason),
                    "evidence_id": evidence_id,
                    "recorded_at": run.at,
                    "recorded_by": run.stamp.actor,
                }
            ),
        )


def _create_item(run: _Run, index: int, op: dict[str, Any]) -> None:
    registry = run.registry
    kind = registry.resolve_kind(op["kind"])
    run.summaries[index] = f"item {op.get('temp_id') or '(new)'} ({kind['name']})"
    became = parse_clock(op["became_true"])
    status = role = None
    if op.get("status") is not None:
        status, role = registry.resolve_status(kind, op["status"])
    elif kind.get("lifecycle_class") == "work_with_state":
        # Work with a state always has one: a task with no status would escape the claim-about-now
        # rule (D4) and the status rules (D1, D5). Other kinds keep status optional.
        raise Refused(
            "unknown_status",
            "status_required",
            f"an item of the work kind {kind['name']!r} carries a status",
            {"valid": [entry["status"] for entry in registry.vocabulary(kind)]},
        )
    properties = None
    if op.get("properties"):
        properties = _check_properties(run, kind, op["properties"])
    plans = _plans(run, op["evidence_refs"])
    world_time = _world_time(plans)
    provenance = _first_supporting(plans)
    values = {**op, "properties": _json(properties)}
    for name in _required_fields(kind):
        if not _has_field(values, name):
            raise Refused(
                "invalid_arguments",
                "required_field",
                f"kind {kind['name']!r} requires {name}",
                {"field": name},
            )
    pairs, overridden = _near_duplicates(run, kind, op["title"], op.get("distinct_from") or [])
    stopped = None
    if role in TERMINAL_ROLES:
        # A terminal status at create carries its end time: no later than the evidence.
        stopped = Clock("before", latest=world_time)
        if ends_before_start(became, stopped):
            raise Refused("end_before_start", "end_before_start", "it ended before it began", {})

    item_id = run.new_id()
    if op.get("temp_id"):
        run.temps[op["temp_id"]] = ("item", item_id)
    reason_id = run.text(op["reason"])
    row = compact(
        {
            "id": item_id,
            "kind": kind["id"],
            "title": op["title"],
            "summary": op.get("summary") or None,
            "body": op.get("body") or None,
            "properties": _json(properties),
            "property_values": _property_values(properties),
            "search_terms": op.get("search_terms") or None,
            "status": status,
            "status_role": role,
            "status_since": world_time if status else None,
            **clock_columns("became_true", became),
            **_words(run, "became_true", became),
            **(clock_columns("stopped_being_true", stopped) if stopped else {}),
            "recorded": run.at,
            "recheck_by": _instant_or_none(op.get("recheck_by")),
            "recheck_how": _json(op.get("recheck_how")),
            "provenance_evidence_id": provenance,
            "provenance_reason": reason_id,
            "version": 1,
            "touched_at": run.at,
        }
    )
    run.tx.run(_ADD_ITEM, row=row)
    run.entry(
        entity_type="item",
        entity_id=item_id,
        field="row",
        new=row_json("items", row, run.stamp.world_id),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=provenance,
    )
    _write_evidence(run, plans, "item", item_id, reason_id)
    _record_pairs(run, item_id, pairs, provenance)
    if overridden:
        write_override_row(run.tx, run.stamp.request_id, op["op_id"], overridden, run.at)
    run.touch(item_id, index)
    if role == "done":
        run.done_items[item_id] = index


# update_item ----------------------------------------------------------------------------------


def _update_item(run: _Run, index: int, op: dict[str, Any]) -> None:
    item_id = run.ref(op["item"], "item")
    run.summaries[index] = f"item {run.label(op['item'])}"
    item = _lock_item(run, item_id)
    kind = run.registry.kind(item["kind"])
    _check_version(op, item)
    fields = op["fields"]
    if set(fields) - {"search_terms"}:
        _not_a_record(kind, "only search terms may change")
    plans = _plans(run, op["evidence_refs"])
    _settle(run, plans, item_id)
    world_time = _world_time(plans)
    required = {"title", *_required_fields(kind)}

    changes: list[tuple[str, Any, Any, dict[str, Any]]] = []
    for name, value in fields.items():
        old: Any = item.get(name)
        if name == "properties":
            # Properties merge: a key sent as null is removed, the others are set. A change that
            # goes to history only merges onto the properties as they stood at its own world
            # time, so an as-of read there never shows a key that became true later (D1, D3).
            head = _head_world_time(run, item_id, name)
            if head is not None and world_time is not None and world_time < head:
                merged = _properties_as_of(run, item_id, world_time)
            else:
                merged = json.loads(item.get("properties") or "{}")
            old = json.loads(item.get("properties") or "{}") or None
            for key, part in value.items():
                name_of = _declared_property(run, kind, key)["name"]
                if part is None:
                    merged.pop(name_of, None)
                else:
                    merged[name_of] = part
            merged = _check_properties(run, kind, merged)
            new = merged or None
            columns = {"properties": _json(new), "property_values": _property_values(new)}
        elif name in ("title", "summary", "body"):
            new = value or None
            columns = {name: new}
        elif name == "search_terms":
            new = value or None
            columns = {name: new}
        elif name == "became_true":
            clock = parse_clock(value)
            previous = clock_from_row(item, "became_true")
            old = previous.to_json() if previous else None
            new = clock.to_json()
            if ends_before_start(clock, clock_from_row(item, "stopped_being_true")):
                raise Refused(
                    "end_before_start", "end_before_start", "it would end before it began", {}
                )
            columns = {**clock_columns("became_true", clock), **_words(run, "became_true", clock)}
        elif name == "recheck_by":
            new = _instant_or_none(value)
            columns = {name: new}
        else:  # recheck_how
            old = json.loads(item["recheck_how"]) if item.get("recheck_how") else None
            new = value
            columns = {name: _json(value)}
        if new == old:
            continue
        if new is None and name in required:
            raise Refused(
                "invalid_arguments", "required_field", f"{name} may not be blanked", {"field": name}
            )
        changes.append((name, old, new, columns))

    if changes:
        evidence_id = _first_supporting(plans)
        reason_id = run.text(op["reason"])
        moved = False
        for name, old, new, columns in changes:
            head = _head_world_time(run, item_id, name) if name in CLAIM_FIELDS else None
            history_only = head is not None and world_time is not None and world_time < head
            run.entry(
                entity_type="item",
                entity_id=item_id,
                field=name,
                old=None if history_only else old,
                new=new,
                world_time=world_time,
                reason_id=reason_id,
                evidence_id=evidence_id,
                history_only=history_only,
            )
            if not history_only:
                run.tx.run(_SET_ITEM, id=item_id, columns=columns)
                moved = True
        if moved:
            _bump(run, _BUMP_ITEM, item_id)
    else:
        reason_id = run.text(op["reason"])
    _write_evidence(run, plans, "item", item_id, reason_id)
    run.touch(item_id, index)


# change_status --------------------------------------------------------------------------------


def _set_status(
    run: _Run,
    item: dict[str, Any],
    status: str,
    role: str,
    since: datetime | None,
    *,
    history_only: bool,
    reason_id: str,
    evidence_id: str | None,
) -> dict[str, Any]:
    """Entries for the status and its role and time; the columns to set when it moves the head."""
    for field, old, new in (
        ("status", item.get("status"), status),
        ("status_role", item.get("status_role"), role),
        ("status_since", item.get("status_since"), since),
    ):
        run.entry(
            entity_type="item",
            entity_id=item["id"],
            field=field,
            old=None if history_only else old,
            new=new,
            world_time=since,
            reason_id=reason_id,
            evidence_id=evidence_id,
            history_only=history_only,
        )
    return {"status": status, "status_role": role, "status_since": since}


def _clear_stop(
    run: _Run,
    item: dict[str, Any],
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
) -> dict[str, Any]:
    previous = clock_from_row(item, "stopped_being_true")
    if previous is None:
        return {}
    run.entry(
        entity_type="item",
        entity_id=item["id"],
        field="stopped_being_true",
        old=previous.to_json(),
        new=None,
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    return {**clock_columns("stopped_being_true", None), "stopped_being_true_words": None}


def _set_stop(
    run: _Run,
    item: dict[str, Any],
    stop: Clock,
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
) -> dict[str, Any]:
    previous = clock_from_row(item, "stopped_being_true")
    run.entry(
        entity_type="item",
        entity_id=item["id"],
        field="stopped_being_true",
        old=previous.to_json() if previous else None,
        new=stop.to_json(),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    return {**clock_columns("stopped_being_true", stop), **_words(run, "stopped_being_true", stop)}


def _recheck(
    run: _Run,
    item: dict[str, Any],
    value: Any,
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
) -> dict[str, Any]:
    new = parse_instant(value)
    if new == item.get("recheck_by"):
        return {}
    run.entry(
        entity_type="item",
        entity_id=item["id"],
        field="recheck_by",
        old=item.get("recheck_by"),
        new=new,
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    return {"recheck_by": new}


def _change_status(run: _Run, index: int, op: dict[str, Any]) -> None:
    item_id = run.ref(op["item"], "item")
    run.summaries[index] = f"item {run.label(op['item'])}"
    item = _lock_item(run, item_id)
    kind = run.registry.kind(item["kind"])
    _check_version(op, item)
    _not_a_record(kind, "a record has no status")
    status, role = run.registry.resolve_status(kind, op["status"])
    plans = _plans(run, op["evidence_refs"])
    _settle(run, plans, item_id)
    world_time = _world_time(plans)
    stop = None
    if role in TERMINAL_ROLES:
        if op.get("stopped_being_true") is not None:
            stop = parse_clock(op["stopped_being_true"])
        else:
            stop = Clock("before", latest=world_time)
        if ends_before_start(clock_from_row(item, "became_true"), stop):
            raise Refused("end_before_start", "end_before_start", "it ended before it began", {})
    elif op.get("stopped_being_true") is not None:
        raise Refused(
            "invalid_arguments",
            "stopped_needs_terminal",
            "only a terminal status carries stopped_being_true",
            {"field": "stopped_being_true"},
        )
    since = item.get("status_since")
    history_only = since is not None and world_time is not None and world_time < since
    if history_only and op.get("stopped_being_true") is not None:
        # The as-of replay has no place for a late end time yet (an open point in the store map).
        # A value the writer cannot store is refused, never dropped behind a saved receipt (D2).
        raise Refused(
            "invalid_arguments",
            "stopped_on_history_only",
            "a change that goes to history only cannot store stopped_being_true: send it without",
            {"field": "stopped_being_true"},
        )
    reason_id = run.text(op["reason"])
    evidence_id = _first_supporting(plans)

    columns: dict[str, Any] = {}
    if history_only or status != item.get("status"):
        moved = _set_status(
            run,
            item,
            status,
            role,
            world_time,
            history_only=history_only,
            reason_id=reason_id,
            evidence_id=evidence_id,
        )
        # A done needs confirmation whether it moves the head or goes to history only: an as-of
        # read returns a history-only value as the truth at its time (D1, D5 rule 2).
        if role == "done":
            run.done_items[item_id] = index
        if not history_only:
            columns.update(moved)
            if stop is not None:
                columns.update(_set_stop(run, item, stop, world_time, reason_id, evidence_id))
            elif not item.get("end_class"):
                columns.update(_clear_stop(run, item, world_time, reason_id, evidence_id))
    if op.get("recheck_by") is not None:
        columns.update(_recheck(run, item, op["recheck_by"], world_time, reason_id, evidence_id))
    if columns:
        run.tx.run(_SET_ITEM, id=item_id, columns=columns)
        _bump(run, _BUMP_ITEM, item_id)
    _write_evidence(run, plans, "item", item_id, reason_id)
    run.touch(item_id, index)


# end_item and reopen_item ---------------------------------------------------------------------


def _end_entry(
    run: _Run,
    entity_type: str,
    entity_id: str,
    old: str | None,
    new: str | None,
    world_time: datetime | None,
    reason_id: str | None,
    evidence_id: str | None,
) -> None:
    run.entry(
        entity_type=entity_type,
        entity_id=entity_id,
        field="end_class",
        old=old,
        new=new,
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )


def _end_item(run: _Run, index: int, op: dict[str, Any]) -> None:
    item_id = run.ref(op["item"], "item")
    run.summaries[index] = f"item {run.label(op['item'])}"
    item = _lock_item(run, item_id)
    kind = run.registry.kind(item["kind"])
    _check_version(op, item)
    _not_a_record(kind, "a record is retired or refuted, never ended")
    if item.get("end_class"):
        raise Refused("invalid_arguments", "already_ended", "the item has already ended", {})
    stop = parse_clock(op["stopped_being_true"])
    if ends_before_start(clock_from_row(item, "became_true"), stop):
        raise Refused("end_before_start", "end_before_start", "it ended before it began", {})
    plans = _plans(run, op["evidence_refs"])
    _settle(run, plans, item_id)
    world_time = _world_time(plans)
    reason_id = run.text(op["reason"])
    evidence_id = _first_supporting(plans)
    _end_entry(run, "item", item_id, None, op["end_class"], world_time, reason_id, evidence_id)
    columns = {
        "end_class": op["end_class"],
        **_set_stop(run, item, stop, world_time, reason_id, evidence_id),
    }
    run.tx.run(_SET_ITEM, id=item_id, columns=columns)
    _bump(run, _BUMP_ITEM, item_id)
    _write_evidence(run, plans, "item", item_id, reason_id)
    run.touch(item_id, index)


def _reopen_item(run: _Run, index: int, op: dict[str, Any]) -> None:
    item_id = run.ref(op["item"], "item")
    run.summaries[index] = f"item {run.label(op['item'])}"
    item = _lock_item(run, item_id)
    kind = run.registry.kind(item["kind"])
    _check_version(op, item)
    _not_a_record(kind, "a record is not reopened")
    terminal = item.get("status_role") in TERMINAL_ROLES
    if not item.get("end_class") and not terminal:
        raise Refused("invalid_arguments", "not_ended", "only an ended item can be reopened", {})
    status = None
    if terminal:
        status = run.registry.reopened_status(kind)
        if status is None:
            raise Refused(
                "unknown_status",
                "no_live_status",
                f"kind {kind['name']!r} has no status to reopen to",
                {"valid": [entry["status"] for entry in run.registry.vocabulary(kind)]},
            )
    plans = _plans(run, op.get("evidence_refs") or [])
    _settle(run, plans, item_id)
    # The world time comes from the evidence, never from the moment of processing (D4). With no
    # evidence it is unknown: the entries carry none and the status since is left unset, so a
    # later status change from older evidence still moves the head (D1).
    world_time = _world_time(plans)
    reason_id = run.text(op["reason"])
    evidence_id = _first_supporting(plans)
    run.entry(
        entity_type="item",
        entity_id=item_id,
        field="reopened",
        new=True,
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    columns: dict[str, Any] = {}
    if item.get("end_class"):
        _end_entry(run, "item", item_id, item["end_class"], None, world_time, reason_id, None)
        columns["end_class"] = None
    columns.update(_clear_stop(run, item, world_time, reason_id, evidence_id))
    if status is not None:
        columns.update(
            _set_status(
                run,
                item,
                status[0],
                status[1],
                world_time,
                history_only=False,
                reason_id=reason_id,
                evidence_id=evidence_id,
            )
        )
    if op.get("recheck_by") is not None:
        columns.update(_recheck(run, item, op["recheck_by"], world_time, reason_id, evidence_id))
    run.tx.run(_SET_ITEM, id=item_id, columns=columns)
    _bump(run, _BUMP_ITEM, item_id)
    _write_evidence(run, plans, "item", item_id, reason_id)
    run.touch(item_id, index)


# create_link and end_link ---------------------------------------------------------------------


def _fits(declared: str | None, kind: dict[str, Any]) -> bool:
    ends = json.loads(declared or '{"any": true}')
    if ends.get("any"):
        return True
    return kind["id"] in (ends.get("kinds") or []) or kind.get("lifecycle_class") in (
        ends.get("classes") or []
    )


def _slot_priors(
    run: _Run, meaning: dict[str, Any], from_id: str, to_id: str
) -> list[dict[str, Any]]:
    cardinality = meaning.get("cardinality", "many")
    if cardinality not in ("one_per_source", "one_per_target"):
        return []
    meanings = run.registry.slot_meanings(meaning)
    if cardinality == "one_per_source":
        _lock_item(run, from_id)
        cypher = (
            "MATCH (a:Item {id: $id})-[l:LINK]->(:Item) "
            "WHERE l.meaning IN $meanings AND l.active_key IS NOT NULL RETURN l"
        )
        owner = from_id
    else:
        _lock_item(run, to_id)
        cypher = (
            "MATCH (:Item)-[l:LINK]->(b:Item {id: $id}) "
            "WHERE l.meaning IN $meanings AND l.active_key IS NOT NULL RETURN l"
        )
        owner = to_id
    return [row["l"] for row in run.tx.run(cypher, id=owner, meanings=meanings)]


def _end_prior(
    run: _Run,
    prior: dict[str, Any],
    stop: Clock,
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
) -> None:
    locked = _one(run, _LOCK_LINK, "link", prior["id"], lock=True)
    if not locked.get("active_key"):
        # A concurrent end_link ended it first: its end class and stop clock stand.
        return
    _end_entry(run, "link", prior["id"], None, "superseded", world_time, reason_id, evidence_id)
    run.entry(
        entity_type="link",
        entity_id=prior["id"],
        field="stopped_being_true",
        new=stop.to_json(),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    columns = {
        "active_key": None,
        "end_class": "superseded",
        **clock_columns("stopped_being_true", stop),
    }
    run.tx.run(_SET_LINK, id=prior["id"], columns=columns)
    _bump(run, _BUMP_LINK, prior["id"])


def _create_link(run: _Run, index: int, op: dict[str, Any]) -> None:
    registry = run.registry
    from_ref, to_ref = op["from"], op["to"]
    from_id, to_id = run.ref(from_ref, "item"), run.ref(to_ref, "item")
    meaning, reversed_ = registry.resolve_meaning(op["meaning"])
    if reversed_:
        from_ref, to_ref, from_id, to_id = to_ref, from_ref, to_id, from_id
    run.summaries[index] = f"link {run.label(from_ref)} -[{meaning['name']}]-> {run.label(to_ref)}"
    # Both ends under the write lock before any check, so a concurrent end_item waits for this
    # transaction (or this one reads the ended row).
    source = _lock_item(run, from_id)
    target = _lock_item(run, to_id)
    for declared, item, rule in (
        ("from_kinds", source, "from_kind"),
        ("to_kinds", target, "to_kind"),
    ):
        kind = registry.kind(item["kind"])
        if not _fits(meaning.get(declared), kind):
            raise Refused(
                "endpoint_kind_mismatch",
                rule,
                f"meaning {meaning['name']!r} does not take a {kind['name']} at this end",
                {"allowed": json.loads(meaning.get(declared) or "{}"), "kind": kind["name"]},
            )
    if meaning.get("carries_consequences"):
        for item in (source, target):
            if _ended(item):
                raise Refused(
                    "ended_target",
                    "ended_target",
                    "no new consequence link to an ended item; attach evidence instead",
                    {"item": item["id"]},
                )
    became = parse_clock(op["became_true"])
    if meaning.get("temporal_class") == "state" and op.get("recheck_by") is None:
        raise Refused(
            "missing_recheck",
            "state_meaning",
            f"a link of the state meaning {meaning['name']!r} is a claim about now",
            {"default_horizon_days": meaning.get("default_horizon")},
        )
    if op.get("original_phrase") is not None and not meaning.get("generic"):
        raise Refused(
            "invalid_arguments",
            "original_phrase",
            "only a link of the generic meaning keeps an original phrase",
            {"field": "original_phrase"},
        )
    plans = _plans(run, op["evidence_refs"])
    world_time = _world_time(plans)
    provenance = _first_supporting(plans)
    key = f"{run.stamp.world_id}|{from_id}|{meaning['id']}|{to_id}"

    existing = run.tx.run("MATCH (:Item)-[l:LINK {active_key: $key}]->(:Item) RETURN l", key=key)
    if existing:
        # Lock it, then read it again: a link ended by a concurrent end_link is no restatement.
        locked = _one(run, _LOCK_LINK, "link", existing[0]["l"]["id"], lock=True)
        if locked.get("active_key") != key:
            existing = []
    if existing:
        # A restatement: evidence on the link that holds, never a second link (D1).
        link_id = existing[0]["l"]["id"]
        if op.get("temp_id"):
            run.temps[op["temp_id"]] = ("link", link_id)
        _settle(run, plans, link_id)
        if all(plan.known for plan in plans):
            # Every row is in the store already: a reason text would be a value no entry names.
            return
        reason_id = run.text(op["reason"])
        written = _write_evidence(run, plans, "link", link_id, reason_id)
        if written:
            run.entry(
                entity_type="link",
                entity_id=link_id,
                field="restated",
                new=written,
                world_time=world_time,
                reason_id=reason_id,
                evidence_id=written[0],
            )
        return

    priors = _slot_priors(run, meaning, from_id, to_id)
    start = sort_key(became)
    later = [
        prior
        for prior in priors
        if start is not None
        and (prior_start := sort_key(clock_from_row(prior, "became_true"))) is not None
        and start < prior_start
    ]
    stored_stop = None
    if later:
        # Arrived late with an earlier start: stored already ended at the holder's start (D1).
        first = min(later, key=lambda prior: sort_key(clock_from_row(prior, "became_true")))
        stored_stop = clock_from_row(first, "became_true")
        priors = []

    link_id = run.new_id()
    if op.get("temp_id"):
        run.temps[op["temp_id"]] = ("link", link_id)
    reason_id = run.text(op["reason"])
    row = compact(
        {
            "id": link_id,
            "from_item": from_id,
            "meaning": meaning["id"],
            "to_item": to_id,
            **clock_columns("became_true", became),
            **_words(run, "became_true", became),
            "recorded": run.at,
            "recheck_by": _instant_or_none(op.get("recheck_by")),
            "recheck_how": _json(op.get("recheck_how")),
            "provenance_evidence_id": provenance,
            "provenance_reason": reason_id,
            "version": 1,
            "touched_at": run.at,
            "active_key": None if stored_stop else key,
            "end_class": "superseded" if stored_stop else None,
            **(clock_columns("stopped_being_true", stored_stop) if stored_stop else {}),
            "original_phrase": (
                run.text(op["original_phrase"]) if op.get("original_phrase") else None
            ),
        }
    )
    run.tx.run(_ADD_LINK, a=from_id, b=to_id, row=row)
    run.entry(
        entity_type="link",
        entity_id=link_id,
        field="row",
        new=row_json("links", row, run.stamp.world_id),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=provenance,
    )
    if priors:
        # The prior link ends at the new link's start, with the same bounds and precision.
        stop = Clock(became.precision, became.earliest, became.latest)
        for prior in priors:
            _end_prior(run, prior, stop, start or world_time, reason_id, provenance)
    _write_evidence(run, plans, "link", link_id, reason_id)


def _end_link(run: _Run, index: int, op: dict[str, Any]) -> None:
    link_id = run.ref(op["link"], "link")
    run.summaries[index] = f"link {run.label(op['link'])}"
    link = _one(run, _LOCK_LINK, "link", link_id, lock=True)
    if link.get("meaning") in PRIMITIVE_MEANING_IDS:
        # A successor or subject link moves only with its composite operation (D3, D9).
        raise Refused(
            "unknown_meaning",
            "primitive_meaning",
            "a primitive meaning is written only by the writer's composite operations",
            {"meaning": link["meaning"]},
        )
    if not link.get("active_key"):
        raise Refused("invalid_arguments", "already_ended", "the link has already ended", {})
    stop = parse_clock(op["stopped_being_true"])
    if ends_before_start(clock_from_row(link, "became_true"), stop):
        raise Refused("end_before_start", "end_before_start", "it ended before it began", {})
    plans = _plans(run, op.get("evidence_refs") or [])
    _settle(run, plans, link_id)
    world_time = _world_time(plans) or sort_key(stop) or run.at
    reason_id = run.text(op["reason"])
    evidence_id = _first_supporting(plans)
    _end_entry(run, "link", link_id, None, op["end_class"], world_time, reason_id, evidence_id)
    run.entry(
        entity_type="link",
        entity_id=link_id,
        field="stopped_being_true",
        new=stop.to_json(),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    columns = {
        "active_key": None,
        "end_class": op["end_class"],
        **clock_columns("stopped_being_true", stop),
        **_words(run, "stopped_being_true", stop),
    }
    run.tx.run(_SET_LINK, id=link_id, columns=columns)
    _bump(run, _BUMP_LINK, link_id)
    _write_evidence(run, plans, "link", link_id, reason_id)


# attach_evidence and end_evidence -------------------------------------------------------------


def _judgments(run: _Run, judgment_ids: list[str]) -> list[dict[str, Any]]:
    if not judgment_ids:
        return []
    rows = run.tx.run(
        "MATCH (j:Judgment) WHERE j.id IN $ids "
        "OPTIONAL MATCH (c:ModelCall {id: j.model_call_id}) "
        "RETURN j.id AS id, j.verdict AS verdict, c.model AS model, j.subject AS subject, "
        "j.role AS role, j.evidence_id AS evidence_id",
        ids=judgment_ids,
    )
    found = {row["id"]: row for row in rows}
    missing = [judgment_id for judgment_id in judgment_ids if judgment_id not in found]
    if missing:
        raise Refused(
            "unrecorded_verdict",
            "unrecorded_verdict",
            "a cited judgment is not a recorded judgment of this world",
            {"judgment_ids": missing},
        )
    return [found[judgment_id] for judgment_id in judgment_ids]


# The judge roles whose verdict can say a row contradicts a belief (B.1 judgments).
_CONTRADICTION_ROLES = frozenset({"first_judge", "second_judge", "contradicts_or_complements"})


def _belief_refs(run: _Run, belief_id: str) -> set[str]:
    """The S32 references a judgment of this belief may name as its subject: the belief's item id
    and its :Belief row id."""
    refs = {f"belief:{belief_id}", f"item:{belief_id}"}
    for row in run.tx.run("MATCH (b:Belief {item_id: $id}) RETURN b.id AS id", id=belief_id):
        refs.add(f"belief:{row['id']}")
    return refs


def _judged_this(
    run: _Run, judgments: list[dict[str, Any]], belief_id: str, evidence_id: str | None
) -> None:
    """D4: a verdict counts only when the judgment judged this belief, in a judge role, and, when
    it names an evidence row, the row this operation cites."""
    refs = _belief_refs(run, belief_id)
    for row in judgments:
        if row.get("subject") not in refs:
            raise Refused(
                "unrecorded_verdict",
                "judgment_subject",
                "a cited judgment did not judge this belief",
                {"judgment_id": row["id"], "subject": row.get("subject")},
            )
        if row.get("role") not in _CONTRADICTION_ROLES:
            raise Refused(
                "unrecorded_verdict",
                "judgment_role",
                "a cited judgment is not a judge's verdict on a contradiction",
                {"judgment_id": row["id"], "valid": sorted(_CONTRADICTION_ROLES)},
            )
        if row.get("evidence_id") is not None and row["evidence_id"] != evidence_id:
            raise Refused(
                "unrecorded_verdict",
                "judgment_evidence",
                "a cited judgment judged another evidence row",
                {"judgment_id": row["id"]},
            )


def _two_judges(judgments: list[dict[str, Any]]) -> None:
    """D9: a row that contradicts a belief needs two recorded judges, of two models, agreeing."""
    models = {row["model"] for row in judgments if row["model"]}
    if len(judgments) < 2 or len(models) < 2:
        raise Refused(
            "second_judge_required",
            "two_models",
            "a contradiction of a belief cites two recorded judgments from two models",
            {},
        )
    verdicts = {row["verdict"] for row in judgments}
    if len(verdicts) > 1:
        raise Refused(
            "judges_disagree",
            "judges_disagree",
            "the two judges disagree: open a contradiction instead",
            {"verdicts": sorted(v for v in verdicts if v)},
        )
    if verdicts != {"contradicts"}:
        raise Refused(
            "judge_not_supports",
            "judge_not_contradicts",
            "the judges do not say the row contradicts the belief",
            {"verdicts": sorted(v for v in verdicts if v)},
        )


def _attach_evidence(run: _Run, index: int, op: dict[str, Any]) -> None:
    target_id = run.ref(op["target"], "any")
    run.summaries[index] = f"evidence on {run.label(op['target'])}"
    items = run.tx.run(_READ_ITEM, id=target_id)
    if items:
        target_type = "belief" if items[0]["i"]["kind"] == KIND_BELIEF else "item"
    elif run.tx.run(_READ_LINK, id=target_id):
        target_type = "link"
    else:
        raise Refused("not_found", "target_not_found", f"no item or link {target_id}", {})
    run.summaries[index] = f"evidence on {target_type} {run.label(op['target'])}"
    judgments = _judgments(run, op.get("judgment_ids") or [])
    ref = {key: op[key] for key in ("ev_id", "evidence_id", "polarity") if key in op}
    plans = _plans(run, [ref])
    if target_type == "belief" and op["polarity"] == "contradicts":
        _judged_this(run, judgments, target_id, op.get("evidence_id"))
        _two_judges(judgments)
    _settle(run, plans, target_id)
    if all(plan.known for plan in plans):
        # The row is in the store already: a reason text would be a value no entry names.
        return
    reason_id = run.text(op["reason"]) if op.get("reason") else None
    _write_evidence(run, plans, target_type, target_id, reason_id)


def _end_evidence(run: _Run, index: int, op: dict[str, Any]) -> None:
    evidence_id = op["evidence"]
    run.summaries[index] = f"evidence {run.label(evidence_id)}"
    row = _one(run, _LOCK_EVIDENCE, "evidence", evidence_id, lock=True)
    if not row.get("active"):
        raise Refused("invalid_arguments", "already_ended", "the evidence row has ended", {})
    reason_id = run.text(op["reason"]) if op.get("reason") else None
    run.tx.run(_END_EVIDENCE, id=evidence_id, at=run.at, reason=op["end_reason"])
    run.entry(
        entity_type="evidence",
        entity_id=evidence_id,
        field="end_reason",
        new=op["end_reason"],
        world_time=run.at,
        reason_id=reason_id,
        evidence_id=evidence_id,
    )
    run.recompute[row["target_id"]] = "link" if row.get("target_type") == "link" else "item"


_HANDLERS = {
    "create_item": _create_item,
    "update_item": _update_item,
    "change_status": _change_status,
    "end_item": _end_item,
    "reopen_item": _reopen_item,
    "create_link": _create_link,
    "end_link": _end_link,
    "attach_evidence": _attach_evidence,
    "end_evidence": _end_evidence,
}


# The end of the batch -------------------------------------------------------------------------


def _check_item(run: _Run, item_id: str) -> None:
    item = _one(run, _READ_ITEM, "item", item_id, lock=False)
    kind = run.registry.kind(item["kind"])
    if _claim_about_now(run, item, kind) and item.get("recheck_by") is None:
        raise Refused(
            "missing_recheck",
            "claim_about_now",
            "a claim about now carries a re-check by",
            {"item": item_id},
        )
    for name in _required_fields(kind):
        if not _has_field(item, name):
            raise Refused(
                "invalid_arguments",
                "required_field",
                f"kind {kind['name']!r} requires {name}",
                {"field": name},
            )
    for meaning_id in json.loads(kind.get("required_links") or "[]"):
        rows = run.tx.run(
            "MATCH (i:Item {id: $id})-[l:LINK]->(:Item) "
            "WHERE l.meaning = $meaning AND l.active_key IS NOT NULL RETURN count(l) AS n",
            id=item_id,
            meaning=meaning_id,
        )
        if not rows or rows[0]["n"] == 0:
            raise Refused(
                "invalid_arguments",
                "required_link",
                f"kind {kind['name']!r} requires a link of meaning {meaning_id}",
                {"meaning": meaning_id},
            )


def _rolled_up_from_parts(run: _Run, item_id: str, premises: list[str]) -> bool:
    """R3: a rolled-up row confirms a parent only when the parent has parts (through an active link
    of a meaning that rolls up completion), every part is at a terminal role, and the premises are
    active confirming rows on those parts, one at least on every part that reached done."""
    meanings = [
        meaning_id
        for meaning_id, meaning in run.registry.meanings.items()
        if meaning.get("rolls_up_completion")
    ]
    if not meanings or not premises:
        return False
    parts = {
        row["id"]: row["role"]
        for row in run.tx.run(
            "MATCH (p:Item)-[l:LINK]->(:Item {id: $id}) "
            "WHERE l.meaning IN $meanings AND l.active_key IS NOT NULL "
            "RETURN p.id AS id, p.status_role AS role",
            id=item_id,
            meanings=meanings,
        )
    }
    if not parts or any(role not in TERMINAL_ROLES for role in parts.values()):
        return False
    rows = run.tx.run(
        "MATCH (e:Evidence) WHERE e.id IN $ids RETURN e.id AS id, e.target_id AS target, "
        "e.active AS active, e.polarity AS polarity, e.source_type AS source_type",
        ids=premises,
    )
    if len(rows) != len(set(premises)):
        return False
    for row in rows:
        if not (
            row["active"]
            and row["polarity"] == "supports"
            and row["source_type"] == "confirmed"
            and row["target"] in parts
        ):
            return False
    covered = {row["target"] for row in rows}
    return all(part in covered for part, role in parts.items() if role == "done")


def _confirmed_in_batch(run: _Run, item_id: str) -> bool:
    """D5 rule 2: an observed confirming row, or the parts' confirming rows rolled up. An inferred
    row is never read as confirmed, even when every premise is. A row counts when this batch wrote
    it, or when an operation of this batch cited it again."""
    for row in [*run.created_evidence, *run.cited_evidence]:
        if not (
            row["target_id"] == item_id
            and row["polarity"] == "supports"
            and row.get("active", True)
            and row["source_type"] == "confirmed"
        ):
            continue
        if row.get("derivation") == "observed":
            return True
        if row.get("derivation") == "rolled_up" and _rolled_up_from_parts(
            run, item_id, list(row.get("premises") or [])
        ):
            return True
    return False


def _recompute(run: _Run, target_id: str, target_type: str) -> None:
    rows = [row["e"] for row in run.tx.run(_SUPPORTING, id=target_id)]
    fields = claim_fields(rows)
    if target_type == "link":
        fields = {key: fields[key] for key in ("derived_source_type", "derivation")}
        run.tx.run(_SET_LINK, id=target_id, columns=fields)
    else:
        run.tx.run(_SET_ITEM, id=target_id, columns=fields)


def _refuse_dependents(run: _Run, ops: list[Any]) -> None:
    """An operation that read a temp id of an operation refused at the end of the batch is refused
    as depending on it, and so on down the chain, as the loop does for a refusal in a handler."""
    changed = True
    while changed:
        changed = False
        for index, op in enumerate(ops):
            if index in run.refused or not isinstance(op, dict):
                continue
            declined = next((temp for temp in temp_refs(op) if temp in run.declined_temps), None)
            if declined is not None:
                run.refuse(index, op, _depends(declined))
                changed = True


def _finish(run: _Run, ops: list[Any]) -> None:
    for item_id, index in run.touched_items.items():
        if index in run.refused:
            continue
        try:
            _check_item(run, item_id)
        except Refused as refused:
            run.refuse(index, ops[index], refused)
    for item_id, index in run.done_items.items():
        if index not in run.refused and not _confirmed_in_batch(run, item_id):
            run.refuse(
                index,
                ops[index],
                Refused(
                    "terminal_needs_confirmation",
                    "terminal_needs_confirmation",
                    "a terminal done needs confirming evidence in this batch",
                    {"alternative": "reported done"},
                ),
            )
    _refuse_dependents(run, ops)
    if not run.refused:
        for target_id, target_type in run.recompute.items():
            _recompute(run, target_id, target_type)


def apply_ops(
    tx: WorldTx,
    *,
    stamp: Stamp,
    ops: list[Any],
    observations: dict[str, dict[str, Any]],
    pre_refused: dict[int, Refusal],
    before_commit: bool = True,
) -> None:
    """The whole batch inside one write transaction. Raises BatchDeclined on any refusal.

    A caller that owns its transaction (sign-in's own item) gets the `_before_commit` seam last.
    apply_batch passes before_commit=False and calls the seam itself, after the request's
    `saved`, so a kill there leaves both written and nothing committed."""
    run = _Run(tx, stamp, observations)
    for index, op in enumerate(ops):
        if index in pre_refused:
            run.refused[index] = pre_refused[index]
            if is_temp(op.get("temp_id") if isinstance(op, dict) else None):
                run.declined_temps.add(op["temp_id"])
            continue
        try:
            for temp in temp_refs(op):
                if temp in run.declined_temps:
                    raise _depends(temp)
            _HANDLERS[op["op"]](run, index, op)
        except Refused as refused:
            run.refuse(index, op, refused)
    _finish(run, ops)
    if run.refused:
        raise BatchDeclined(
            [run.refused[index] for index in sorted(run.refused)],
            batch_size=len(ops),
            summaries={
                op_label(ops[index])[0] or "": text for index, text in run.summaries.items()
            },
            request_id=stamp.request_id,
        )
    if before_commit:
        _before_commit()
