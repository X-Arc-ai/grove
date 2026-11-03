"""D3's composite operations: correct, refute, supersede, merge, undo and retire_record.

Each runs inside apply_ops, in the batch's one transaction, so lineage, the head and the links move
together or not at all (D3, B.2). Nothing is deleted: an ending is an end class and an entry, and an
undo appends the inverse of a change set as a new change set.

Successor links are :LINK rows of the primitive meanings "corrected by" and "superseded by". Only
this module writes them (create_link and end_link refuse a primitive meaning). A successor link
takes effect at the operation's world time, never later than now (S46), so no successor overtakes
its item before the newer truth existed.

Every entry one operation writes shares the operation's reason value id, and each row it changes
gets one version bump: a rebuild from the history counts versions that way (S47).
"""

import json
from datetime import datetime
from typing import Any

from grove.store.asof import replay
from grove.store.normalise import normalise_phrase
from grove.store.primitives import (
    KIND_BELIEF,
    MEANING_CORRECTED_BY,
    MEANING_SUPERSEDED_BY,
    PRIMITIVE_MEANING_IDS,
)
from grove.writer import apply as ap
from grove.writer import ontology
from grove.writer.clocks import (
    UNKNOWN,
    Clock,
    clock_columns,
    clock_from_row,
    ends_before_start,
    parse_clock,
    parse_instant,
)
from grove.writer.errors import Refused
from grove.writer.history import compact, row_json
from grove.writer.provenance import OBSERVATION_COLUMNS
from grove.writer.receipts import short_id
from grove.writer.source_types import ORDER, RANK

SUCCESSOR_MEANINGS = (MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY)
# Entry fields that mark an event and hold no column (D3, S47).
MARKERS = frozenset({"reopened", "merged_into", "restated", "no_head_update"})
_CLOCK_FIELDS = ("became_true", "stopped_being_true")
_INSTANT_FIELDS = frozenset({"status_since", "recheck_by", "last_confirmed"})
_JSON_FIELDS = frozenset({"recheck_how"})
_FORGOTTEN_REASON = "a value this change set holds was forgotten"

# A lock that changes nothing on the row: a correction leaves the record byte-identical, and the
# lock still makes a concurrent end of the same row wait for this transaction.
_MARK_ITEM = "MATCH (i:Item {id: $id}) SET i.lock_marker = true REMOVE i.lock_marker RETURN i"
_SUCCESSORS_OUT = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item = $id AND l.meaning IN $meanings "
    "AND l.active_key IS NOT NULL RETURN l.id AS id"
)
_ACTIVE_LINKS_OF = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE (l.from_item = $id OR l.to_item = $id) "
    "AND l.active_key IS NOT NULL RETURN l ORDER BY l.id"
)
_LINK_BY_KEY = "MATCH (:Item)-[l:LINK {active_key: $key}]->(:Item) RETURN l.id AS id"
_ACTIVE_EVIDENCE_ON = "MATCH (e:Evidence {target_id: $id, active: true}) RETURN e ORDER BY e.id"
_ADD_ALIAS = "CREATE (a:Alias) SET a += $row"
_ITEM_ALIASES = (
    "MATCH (a:Alias {target_type: 'item', target_id: $id}) "
    "WHERE coalesce(a.status, '') <> 'retired' "
    "RETURN a ORDER BY a.id"
)
_ALIAS_ON = (
    "MATCH (a:Alias {normalised_phrase: $phrase, target_type: 'item', target_id: $target}) "
    "RETURN a.id AS id"
)
_SET_EVIDENCE = "MATCH (e:Evidence {id: $id}) SET e += $columns"
_BUMP_EVIDENCE = "MATCH (e:Evidence {id: $id}) SET e.version = coalesce(e.version, 0) + 1"
_ENTRIES_OF_SET = "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN e ORDER BY e.sequence"
_UNDONE = "MATCH (e:ChangeEntry {inverse_of: $id}) RETURN e.sequence AS sequence LIMIT 1"
# The markers that leave their row as it was (S47): no version moves with them, so no undo
# conflicts on them. grove.store.replay._NO_VERSION is the same set (a test holds the two equal).
STILL_MARKERS = frozenset({"restated", "no_head_update"})
_LATER_MOVES = (
    "MATCH (e:ChangeEntry) WHERE e.sequence > $after AND e.entity_id IN $ids "
    "AND e.history_only = false AND NOT e.field IN $still "
    "RETURN e.entity_id AS id, e.sequence AS sequence ORDER BY e.sequence LIMIT 1"
)
_VALUES = (
    "MATCH (v:HistoryValue) WHERE v.id IN $ids "
    "RETURN v.id AS id, v.value AS value, v.forgotten_at AS forgotten_at"
)
_ITEM_ENTRIES_BEFORE = (
    "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) WHERE e.sequence < $before "
    "RETURN e.sequence AS sequence, e.field AS field, e.new_value_id AS value_id, "
    "e.world_time AS world_time ORDER BY e.sequence"
)


# Shared checks -------------------------------------------------------------------------------


def _not_belief(item: dict[str, Any]) -> None:
    if item.get("kind") == KIND_BELIEF:
        raise Refused(
            "invalid_arguments",
            "belief_transition",
            "a belief moves by the beliefs operations of its state machine (D9)",
            {"item": item["id"]},
        )


def _overtaken(run: ap._Run, item_id: str) -> bool:
    return bool(run.tx.run(_SUCCESSORS_OUT, id=item_id, meanings=list(SUCCESSOR_MEANINGS)))


def _mark_item(run: ap._Run, item_id: str) -> dict[str, Any]:
    return ap._one(run, _MARK_ITEM, "item", item_id, lock=False)


def _distinct(first: str, second: str, rule: str, message: str) -> None:
    if first == second:
        raise Refused("invalid_arguments", rule, message, {})


def _evidence_id(plans: list[ap._Plan]) -> str | None:
    """The row an entry cites: the first supporting row, else the first row the operation cites."""
    return ap._first_supporting(plans) or (plans[0].id if plans else None)


def _successor_clock(run: ap._Run, world_time: datetime | None) -> Clock:
    """S46: a successor link takes effect at the newest time its evidence speaks for."""
    if world_time is None:
        return UNKNOWN
    if world_time > run.at:
        raise Refused(
            "invalid_arguments",
            "successor_in_future",
            "a successor link takes effect when its evidence was written, never after now",
            {"world_time": world_time.isoformat()},
        )
    return Clock("instant", world_time, world_time)


def _successor_link(
    run: ap._Run,
    from_id: str,
    to_id: str,
    meaning_id: str,
    became: Clock,
    plans: list[ap._Plan],
    reason_id: str,
    world_time: datetime | None,
) -> str:
    """The successor link from -> to, or the one that holds already (evidence added, D1)."""
    key = f"{run.stamp.world_id}|{from_id}|{meaning_id}|{to_id}"
    found = run.tx.run(_LINK_BY_KEY, key=key)
    if found:
        link_id = found[0]["id"]
        locked = ap._one(run, ap._LOCK_LINK, "link", link_id, lock=True)
        if locked.get("active_key") == key:
            written = ap._write_evidence(run, plans, "link", link_id, reason_id)
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
            return link_id
    link_id = run.new_id()
    ap._settle(run, plans, link_id)
    provenance = ap._first_supporting(plans)
    row = compact(
        {
            "id": link_id,
            "from_item": from_id,
            "meaning": meaning_id,
            "to_item": to_id,
            **clock_columns("became_true", became),
            "recorded": run.at,
            "provenance_evidence_id": provenance,
            "provenance_reason": reason_id,
            "version": 1,
            "touched_at": run.at,
            "active_key": key,
        }
    )
    run.tx.run(ap._ADD_LINK, a=from_id, b=to_id, row=row)
    run.entry(
        entity_type="link",
        entity_id=link_id,
        field="row",
        new=row_json("links", row, run.stamp.world_id),
        world_time=world_time,
        reason_id=reason_id,
        evidence_id=provenance,
    )
    ap._write_evidence(run, plans, "link", link_id, reason_id)
    return link_id


def _end_item_row(
    run: ap._Run,
    item_id: str,
    end_class: str,
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    ap._end_entry(run, "item", item_id, None, end_class, world_time, reason_id, evidence_id)
    run.tx.run(ap._SET_ITEM, id=item_id, columns={"end_class": end_class, **(extra or {})})
    ap._bump(run, ap._BUMP_ITEM, item_id)


# correct --------------------------------------------------------------------------------------


def _correct(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    target_id = run.ref(op["target"], "item")
    successor_id = run.ref(op["successor"], "item")
    run.summaries[index] = (
        f"item {run.label(op['target'])} corrected by {run.label(op['successor'])}"
    )
    _distinct(target_id, successor_id, "successor_is_target", "an item does not correct itself")
    target = _mark_item(run, target_id)
    successor = _mark_item(run, successor_id)
    _not_belief(target)
    if ap._ended(target):
        raise Refused(
            "ended_target",
            "ended_target",
            "an ended item is not corrected: reopen it, or correct its successor",
            {"item": target_id},
        )
    if ap._ended(successor):
        raise Refused(
            "ended_target",
            "ended_successor",
            "the newer truth cannot sit on an ended item",
            {"item": successor_id},
        )
    plans = ap._plans(run, op["evidence_refs"])
    world_time = ap._world_time(plans)
    became = _successor_clock(run, world_time)
    reason_id = run.text(op["reason"])
    link_id = _successor_link(
        run, target_id, successor_id, MEANING_CORRECTED_BY, became, plans, reason_id, world_time
    )
    if op.get("no_head_update_reason"):
        # On the link, in the link's own group: the record's row and version stay as they were.
        run.entry(
            entity_type="link",
            entity_id=link_id,
            field="no_head_update",
            new=op["no_head_update_reason"],
            world_time=world_time,
            reason_id=reason_id,
        )
    run.corrections.append((index, successor_id, bool(op.get("no_head_update_reason"))))


# refute ---------------------------------------------------------------------------------------


def _rank(source_type: str | None, derivation: str | None) -> int:
    """D9's ranks among sources: an inferred row or claim, or one with no source, ranks lowest."""
    if derivation == "inferred" or source_type not in RANK:
        return -1
    return RANK[source_type]


def _check_rank(run: ap._Run, claim: dict[str, Any], plans: list[ap._Plan]) -> None:
    """B.2: evidence at or above the claim's rank. The person's own word outranks every source
    (D9 rule 1), so a person caller passes."""
    if run.caller_kind == "person":
        return
    claim_rank = _rank(claim.get("derived_source_type"), claim.get("derivation"))
    best = max(
        (_rank(plan.columns.get("source_type"), plan.columns.get("derivation")) for plan in plans),
        default=-1,
    )
    if best < claim_rank:
        raise Refused(
            "invalid_arguments",
            "refute_rank",
            "a refutation needs evidence at or above the claim's rank",
            {
                "claim": ORDER[claim_rank] if claim_rank >= 0 else "inferred",
                "evidence": ORDER[best] if best >= 0 else "inferred",
            },
        )


def _refute(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    target_id = run.ref(op["target"], "any")
    run.summaries[index] = f"refute {run.label(op['target'])}"
    if run.tx.run(ap._READ_ITEM, id=target_id):
        _refute_item(run, index, op, target_id)
    elif run.tx.run(ap._READ_LINK, id=target_id):
        _refute_link(run, op, target_id)
    else:
        raise Refused("not_found", "target_not_found", f"no item or link {target_id}", {})


def _refute_item(run: ap._Run, index: int, op: dict[str, Any], item_id: str) -> None:
    item = ap._lock_item(run, item_id)
    _not_belief(item)
    ap._check_version(op, item)
    if item.get("end_class"):
        raise Refused("invalid_arguments", "already_ended", "the item has already ended", {})
    plans = ap._plans(run, op["evidence_refs"])
    ap._settle(run, plans, item_id)
    _check_rank(run, item, plans)
    world_time = ap._world_time(plans)
    reason_id = run.text(op["reason"])
    # Refuted: never true, so no world end time is stored (D3, "end classes").
    _end_item_row(run, item_id, "refuted", world_time, reason_id, _evidence_id(plans))
    ap._write_evidence(run, plans, "item", item_id, reason_id)
    run.touch(item_id, index)


def _refute_link(run: ap._Run, op: dict[str, Any], link_id: str) -> None:
    link = ap._one(run, ap._LOCK_LINK, "link", link_id, lock=True)
    if link.get("meaning") in PRIMITIVE_MEANING_IDS:
        raise Refused(
            "unknown_meaning",
            "primitive_meaning",
            "a primitive link moves only with its composite operation or an undo",
            {"meaning": link["meaning"]},
        )
    ap._check_version(op, link)
    if not link.get("active_key"):
        raise Refused("invalid_arguments", "already_ended", "the link has already ended", {})
    plans = ap._plans(run, op["evidence_refs"])
    ap._settle(run, plans, link_id)
    _check_rank(run, link, plans)
    world_time = ap._world_time(plans)
    reason_id = run.text(op["reason"])
    ap._end_entry(run, "link", link_id, None, "refuted", world_time, reason_id, _evidence_id(plans))
    run.tx.run(ap._SET_LINK, id=link_id, columns={"active_key": None, "end_class": "refuted"})
    ap._bump(run, ap._BUMP_LINK, link_id)
    ap._write_evidence(run, plans, "link", link_id, reason_id)
    run.head_changes.update((link["from_item"], link["to_item"]))


# supersede ------------------------------------------------------------------------------------


def _supersede(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    target_id = run.ref(op["target"], "item")
    successor_id = run.ref(op["successor"], "item")
    run.summaries[index] = (
        f"item {run.label(op['target'])} superseded by {run.label(op['successor'])}"
    )
    _distinct(target_id, successor_id, "successor_is_target", "an item does not supersede itself")
    target = ap._lock_item(run, target_id)
    successor = _mark_item(run, successor_id)
    _not_belief(target)
    # A record-class item is taken: a decision is one, and a replaced decision is superseded (spec
    # lines 796, 822). An overtaken observation or event is corrected instead (S46).
    ap._check_version(op, target)
    if ap._ended(target):
        raise Refused("invalid_arguments", "already_ended", "the item has already ended", {})
    if _overtaken(run, target_id):
        raise Refused(
            "invalid_arguments",
            "already_overtaken",
            "the item has a successor already: supersede its successor",
            {"item": target_id},
        )
    if ap._ended(successor):
        raise Refused(
            "ended_target",
            "ended_successor",
            "the successor cannot be an ended item",
            {"item": successor_id},
        )
    plans = ap._plans(run, op["evidence_refs"])
    world_time = ap._world_time(plans)
    became = _successor_clock(run, world_time)
    if op.get("stopped_being_true") is not None:
        stop = parse_clock(op["stopped_being_true"])
    elif world_time is not None:
        stop = Clock("before", latest=world_time)
    else:
        stop = UNKNOWN
    if ends_before_start(clock_from_row(target, "became_true"), stop):
        raise Refused("end_before_start", "end_before_start", "it ended before it began", {})
    reason_id = run.text(op["reason"])
    _successor_link(
        run, target_id, successor_id, MEANING_SUPERSEDED_BY, became, plans, reason_id, world_time
    )
    evidence_id = _evidence_id(plans)
    stop_columns = ap._set_stop(run, target, stop, world_time, reason_id, evidence_id)
    _end_item_row(run, target_id, "superseded", world_time, reason_id, evidence_id, stop_columns)
    run.touch(target_id, index)


# retire_record --------------------------------------------------------------------------------


def _retire_record(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    record_id = run.ref(op["record"], "item")
    lesson_id = run.ref(op["lesson"], "item")
    run.summaries[index] = f"record {run.label(op['record'])}"
    _distinct(record_id, lesson_id, "lesson_is_record", "a record's lesson lives elsewhere")
    record = ap._lock_item(run, record_id)
    if run.registry.kind(record["kind"]).get("lifecycle_class") != "record":
        raise Refused(
            "invalid_arguments",
            "not_a_record",
            "only a record is retired as spent; end or supersede an item of another kind",
            {"item": record_id},
        )
    ap._check_version(op, record)
    if ap._ended(record):
        raise Refused("invalid_arguments", "already_ended", "the record has already ended", {})
    lesson = _mark_item(run, lesson_id)
    if ap._ended(lesson):
        raise Refused(
            "ended_target",
            "ended_lesson",
            "the lesson must live in an item that holds",
            {"item": lesson_id},
        )
    reason_id = run.text(op["reason"])
    # The record axis only: no world time, no stop clock. The claim stays true; Grove stops
    # serving it as current, and its evidence rows stay active (D3).
    ap._end_entry(run, "item", record_id, None, "spent", None, reason_id, None)
    run.entry(
        entity_type="item",
        entity_id=record_id,
        field="lesson_in",
        new=lesson_id,
        world_time=None,
        reason_id=reason_id,
    )
    run.tx.run(ap._SET_ITEM, id=record_id, columns={"end_class": "spent", "lesson_in": lesson_id})
    ap._bump(run, ap._BUMP_ITEM, record_id)
    run.touch(record_id, index)


# merge ----------------------------------------------------------------------------------------


def _repointed(link: dict[str, Any], absorbed_id: str, survivor_id: str) -> tuple[str, str]:
    from_id = survivor_id if link["from_item"] == absorbed_id else link["from_item"]
    to_id = survivor_id if link["to_item"] == absorbed_id else link["to_item"]
    return from_id, to_id


def _check_fit(run: ap._Run, link: dict[str, Any], absorbed_id: str, kind: dict[str, Any]) -> None:
    meaning = run.registry.meaning(link["meaning"])
    for end, declared in (("from_item", "from_kinds"), ("to_item", "to_kinds")):
        if link[end] == absorbed_id and not ap._fits(meaning.get(declared), kind):
            raise Refused(
                "endpoint_kind_mismatch",
                "merge_link_fit",
                f"link {short_id(link['id'])} does not fit the survivor's kind {kind['name']!r}",
                {"link": link["id"], "meaning": meaning["name"]},
            )


def _copy_evidence(run: ap._Run, old_link: str, new_link: str, reason_id: str) -> list[str]:
    """The active rows of the re-pointed link, copied onto its new link (same observation)."""
    written = []
    for row in (found["e"] for found in run.tx.run(_ACTIVE_EVIDENCE_ON, id=old_link)):
        if run.tx.run(
            ap._SAME_EVIDENCE, key=row["message_key"], target=new_link, polarity=row["polarity"]
        ):
            continue
        evidence_id = run.new_id()
        copy = compact(
            {
                "id": evidence_id,
                **{key: row.get(key) for key in OBSERVATION_COLUMNS},
                "target_type": "link",
                "target_id": new_link,
                "polarity": row["polarity"],
                "active": True,
                "recorded": run.at,
                "request_id": run.stamp.request_id,
                "version": 1,
                "touched_at": run.at,
            }
        )
        run.tx.run(ap._ADD_EVIDENCE, row=copy)
        run.entry(
            entity_type="evidence",
            entity_id=evidence_id,
            field="row",
            new=row_json("evidence", copy, run.stamp.world_id),
            world_time=copy.get("source_time"),
            reason_id=reason_id,
            evidence_id=evidence_id,
        )
        written.append(evidence_id)
    if written:
        run.recompute[new_link] = "link"
    return written


_LINK_COPIED = (
    "became_true_earliest",
    "became_true_latest",
    "became_true_precision",
    "became_true_words",
    "recorded",
    "last_confirmed",
    "confirmed_against",
    "recheck_by",
    "recheck_how",
    "provenance_evidence_id",
    "provenance_reason",
    "original_phrase",
)


def _repoint_link(
    run: ap._Run,
    link: dict[str, Any],
    absorbed_id: str,
    survivor_id: str,
    world_time: datetime | None,
    reason_id: str,
    evidence_id: str | None,
) -> None:
    locked = ap._one(run, ap._LOCK_LINK, "link", link["id"], lock=True)
    if locked.get("active_key") != link["active_key"]:
        return  # a concurrent end came first; its ending stands
    # The old link ends as superseded by its new link, with no world end time: the claim goes on
    # holding, on the survivor (D3 merge).
    ap._end_entry(run, "link", link["id"], None, "superseded", world_time, reason_id, evidence_id)
    run.tx.run(ap._SET_LINK, id=link["id"], columns={"active_key": None, "end_class": "superseded"})
    ap._bump(run, ap._BUMP_LINK, link["id"])
    from_id, to_id = _repointed(link, absorbed_id, survivor_id)
    if from_id == to_id:
        return  # a link between the two items has nothing left to join
    key = f"{run.stamp.world_id}|{from_id}|{link['meaning']}|{to_id}"
    found = run.tx.run(_LINK_BY_KEY, key=key)
    if found:
        target = ap._one(run, ap._LOCK_LINK, "link", found[0]["id"], lock=True)["id"]
        written = _copy_evidence(run, link["id"], target, reason_id)
        if written:
            run.entry(
                entity_type="link",
                entity_id=target,
                field="restated",
                new=written,
                world_time=world_time,
                reason_id=reason_id,
                evidence_id=written[0],
            )
    else:
        target = run.new_id()
        row = compact(
            {
                "id": target,
                "from_item": from_id,
                "meaning": link["meaning"],
                "to_item": to_id,
                **{name: locked.get(name) for name in _LINK_COPIED},
                "version": 1,
                "touched_at": run.at,
                "active_key": key,
            }
        )
        run.tx.run(ap._ADD_LINK, a=from_id, b=to_id, row=row)
        run.entry(
            entity_type="link",
            entity_id=target,
            field="row",
            new=row_json("links", row, run.stamp.world_id),
            world_time=world_time,
            reason_id=reason_id,
            evidence_id=row.get("provenance_evidence_id"),
        )
        _copy_evidence(run, link["id"], target, reason_id)
    run.head_changes.update((from_id, to_id))


def _alias_names(
    run: ap._Run, absorbed: dict[str, Any], survivor: dict[str, Any], reason_id: str
) -> None:
    """The absorbed item's names become item aliases of the survivor (D3 merge)."""
    survivor_id = survivor["id"]
    phrase = normalise_phrase(absorbed.get("title") or "")
    if phrase and phrase != normalise_phrase(survivor.get("title") or ""):
        if not run.tx.run(_ALIAS_ON, phrase=phrase, target=survivor_id):
            row = {
                "id": run.new_id(),
                "phrase": absorbed["title"],
                "normalised_phrase": phrase,
                "target_type": "item",
                "target_id": survivor_id,
                "reversed": False,
                "added_at": run.at,
                "reason": reason_id,
                "status": "canonical",
                "origin": "merged_from",
                "minted_at": run.at,
                "minted_by": run.stamp.actor,
                "version": 1,
                "touched_at": run.at,
            }
            run.tx.run(_ADD_ALIAS, row=row)
            run.entry(
                entity_type="registry_row",
                entity_id=row["id"],
                field="row",
                new=row_json("aliases", row, run.stamp.world_id),
                world_time=None,
                reason_id=reason_id,
            )
    for alias in (found["a"] for found in run.tx.run(_ITEM_ALIASES, id=absorbed["id"])):
        ontology.lock_registry(run, "Alias", alias["id"])
        if run.tx.run(_ALIAS_ON, phrase=alias["normalised_phrase"], target=survivor_id):
            field, old, new = "status", alias.get("status"), "retired"
        else:
            field, old, new = "target_id", alias["target_id"], survivor_id
        run.entry(
            entity_type="registry_row",
            entity_id=alias["id"],
            field=field,
            old=old,
            new=new,
            world_time=None,
            reason_id=reason_id,
        )
        ontology.set_registry(run, "Alias", alias["id"], {field: new})
        ontology.bump_registry(run, "Alias", alias["id"])


def _merge(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    survivor_id = run.ref(op["survivor"], "item")
    absorbed_id = run.ref(op["absorbed"], "item")
    run.summaries[index] = (
        f"item {run.label(op['absorbed'])} merged into {run.label(op['survivor'])}"
    )
    _distinct(survivor_id, absorbed_id, "merge_same_item", "an item does not merge into itself")
    survivor = _mark_item(run, survivor_id)
    absorbed = ap._lock_item(run, absorbed_id)
    for item in (survivor, absorbed):
        _not_belief(item)
        if ap._ended(item):
            raise Refused(
                "ended_target", "ended_target", "an ended item is not merged", {"item": item["id"]}
            )
        if _overtaken(run, item["id"]):
            raise Refused(
                "invalid_arguments",
                "already_overtaken",
                "an overtaken item is not merged: merge its successor",
                {"item": item["id"]},
            )
    survivor_kind = run.registry.kind(survivor["kind"])
    absorbed_kind = run.registry.kind(absorbed["kind"])
    if survivor_kind.get("lifecycle_class") != absorbed_kind.get("lifecycle_class"):
        raise Refused(
            "invalid_arguments",
            "merge_lifecycle",
            "two items merge only within one lifecycle class",
            {
                "survivor": survivor_kind.get("lifecycle_class"),
                "absorbed": absorbed_kind.get("lifecycle_class"),
            },
        )
    links = [found["l"] for found in run.tx.run(_ACTIVE_LINKS_OF, id=absorbed_id)]
    for link in links:
        _check_fit(run, link, absorbed_id, survivor_kind)
    plans = ap._plans(run, op.get("evidence_refs") or [])
    world_time = ap._world_time(plans)
    reason_id = run.text(op["reason"])

    run.entry(
        entity_type="item",
        entity_id=absorbed_id,
        field="merged_into",
        new=survivor_id,
        world_time=world_time,
        reason_id=reason_id,
    )
    # The absorbed item is overtaken from its own start: the two were always one.
    became = clock_from_row(absorbed, "became_true") or UNKNOWN
    _successor_link(
        run, absorbed_id, survivor_id, MEANING_SUPERSEDED_BY, became, plans, reason_id, world_time
    )
    evidence_id = _evidence_id(plans)
    _end_item_row(run, absorbed_id, "superseded", world_time, reason_id, evidence_id)
    for link in links:
        _repoint_link(run, link, absorbed_id, survivor_id, world_time, reason_id, evidence_id)
    _alias_names(run, absorbed, survivor, reason_id)
    run.touch(absorbed_id, index)


# undo -----------------------------------------------------------------------------------------


def _decode(text: Any) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _conflict(message: str, **details: Any) -> Refused:
    return Refused("version_conflict", "undo_conflict", message, details)


def _clock_json(value: Any) -> Clock | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = {key: part for key, part in value.items() if key != "words_id"}
    return parse_clock(value)


def _clock_bounds(clock: Clock | None) -> tuple[Any, ...] | None:
    if clock is None:
        return None
    return (clock.precision, clock.earliest, clock.latest)


def _same(field: str, current: dict[str, Any], expected: Any) -> bool:
    """The row's field holds the value an entry set (types as the history holds them)."""
    if field in _CLOCK_FIELDS:
        return _clock_bounds(clock_from_row(current, field)) == _clock_bounds(_clock_json(expected))
    held = current.get(field)
    if field == "properties":
        return (json.loads(held) if held else None) == (expected or None)
    if field in _JSON_FIELDS:
        return (json.loads(held) if held else None) == expected
    if field in _INSTANT_FIELDS:
        return (parse_instant(held) if held else None) == (
            parse_instant(expected) if expected is not None else None
        )
    return held == expected


def _columns(field: str, value: Any) -> dict[str, Any]:
    """The columns that hold `value` for `field`, as the writer stores them (S47)."""
    if field in _CLOCK_FIELDS:
        words = value.get("words_id") if isinstance(value, dict) else None
        return {**clock_columns(field, _clock_json(value)), f"{field}_words": words}
    if field == "properties":
        return {
            "properties": ap._json(value or None),
            "property_values": ap._property_values(value or None),
        }
    if field in _JSON_FIELDS:
        return {field: ap._json(value)}
    if field in _INSTANT_FIELDS:
        return {field: parse_instant(value) if value is not None else None}
    return {field: value}


class _Undo:
    """The inverse of one change set, written as a new change set of this batch (D3, B.2)."""

    def __init__(self, run: ap._Run, index: int, change_set: str, reason_id: str) -> None:
        self.run = run
        self.index = index
        self.change_set = change_set
        self.reason_id = reason_id
        self.bumped: dict[tuple[str, str], str] = {}

    def entry(self, entry: dict[str, Any], **fields: Any) -> None:
        self.run.entry(
            entity_type=entry["entity_type"],
            entity_id=entry["entity_id"],
            reason_id=self.reason_id,
            inverse_of=self.change_set,
            **fields,
        )

    def bump(self, entity_type: str, entity_id: str, label: str = "") -> None:
        self.bumped.setdefault((entity_type, entity_id), label)

    def finish(self) -> None:
        for (entity_type, entity_id), label in self.bumped.items():
            if entity_type == "item":
                ap._bump(self.run, ap._BUMP_ITEM, entity_id)
            elif entity_type == "link":
                ap._bump(self.run, ap._BUMP_LINK, entity_id)
            elif entity_type == "evidence":
                ap._bump(self.run, _BUMP_EVIDENCE, entity_id)
            else:
                ontology.bump_registry(self.run, label, entity_id)

    # one entry ---------------------------------------------------------------------------------

    def invert(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        field = entry["field"]
        if field in MARKERS:
            return
        if entry["entity_type"] == "belief":
            raise Refused(
                "invalid_arguments",
                "not_undoable",
                "a belief change is undone by the beliefs operations (D9)",
                {},
            )
        if entry.get("history_only"):
            self.history_only(entry, values)
        elif field == "row":
            self.creation(entry)
        else:
            self.field(entry, values)

    def history_only(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """A late value: its inverse is a late value at the same world time that holds what the
        replay found just before it, so an as-of read there sees the value again."""
        run = self.run
        rows = run.tx.run(_ITEM_ENTRIES_BEFORE, id=entry["entity_id"], before=entry["sequence"])
        texts = {
            row["id"]: row["value"]
            for row in run.tx.run(_VALUES, ids=[row["value_id"] for row in rows if row["value_id"]])
        }
        before = replay(rows, texts, entry.get("world_time")) or {}
        field = entry["field"]
        if field in _CLOCK_FIELDS:
            clock = clock_from_row(before, field)
            prior: Any = clock.to_json() if clock is not None else None
        elif field == "properties":
            prior = json.loads(before["properties"]) if before.get("properties") else None
        else:
            prior = before.get(field)
        self.entry(
            entry,
            field=field,
            new=prior,
            world_time=entry.get("world_time"),
            history_only=True,
        )
        # A history-only inverse moves no row, so it bumps nothing (S47).

    def creation(self, entry: dict[str, Any]) -> None:
        run = self.run
        entity_type, entity_id = entry["entity_type"], entry["entity_id"]
        if entity_type == "item":
            item = ap._lock_item(run, entity_id)
            if item.get("end_class"):
                raise _conflict("the item has ended since", item=entity_id)
            self.entry(
                entry,
                field="end_class",
                old=None,
                new="refuted",
                world_time=entry.get("world_time"),
            )
            run.tx.run(ap._SET_ITEM, id=entity_id, columns={"end_class": "refuted"})
            self.bump("item", entity_id)
            run.touch(entity_id, self.index)
        elif entity_type == "link":
            link = ap._one(run, ap._LOCK_LINK, "link", entity_id, lock=True)
            if link.get("end_class") == "refuted":
                return
            self.entry(
                entry,
                field="end_class",
                old=link.get("end_class"),
                new="refuted",
                world_time=entry.get("world_time"),
            )
            run.tx.run(
                ap._SET_LINK, id=entity_id, columns={"active_key": None, "end_class": "refuted"}
            )
            self.bump("link", entity_id)
            run.head_changes.update((link["from_item"], link["to_item"]))
        elif entity_type == "evidence":
            row = ap._one(run, ap._LOCK_EVIDENCE, "evidence", entity_id, lock=True)
            if not row.get("active"):
                return
            self.entry(entry, field="end_reason", new="correction", world_time=run.at)
            run.tx.run(
                _SET_EVIDENCE,
                id=entity_id,
                columns={"active": False, "ended_at": run.at, "end_reason": "correction"},
            )
            self.bump("evidence", entity_id)
            run.recompute[row["target_id"]] = "link" if row["target_type"] == "link" else "item"
        else:
            label = ontology.registry_label(run, entity_id)
            row = ontology.lock_registry(run, label, entity_id)
            if row.get("status") == "retired":
                return
            self.entry(entry, field="status", old=row.get("status"), new="retired", world_time=None)
            ontology.set_registry(run, label, entity_id, {"status": "retired"})
            self.bump(entity_type, entity_id, label)

    def field(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        run = self.run
        entity_type, entity_id, field = entry["entity_type"], entry["entity_id"], entry["field"]
        expected = values.get(entry.get("new_value_id"))
        old = values.get(entry.get("old_value_id"))
        label = ""
        if entity_type == "item":
            row = ap._lock_item(run, entity_id)
        elif entity_type == "link":
            row = ap._one(run, ap._LOCK_LINK, "link", entity_id, lock=True)
        elif entity_type == "evidence":
            row = ap._one(run, ap._LOCK_EVIDENCE, "evidence", entity_id, lock=True)
        else:
            label = ontology.registry_label(run, entity_id)
            row = ontology.lock_registry(run, label, entity_id)
        if not _same(field, row, expected):
            raise _conflict(
                "the row no longer holds the value this change set wrote",
                entity=entity_id,
                field=field,
            )
        if entity_type == "evidence" and field == "end_reason":
            columns = (
                {"active": True, "ended_at": None, "end_reason": None}
                if old is None
                else {"active": False, "ended_at": run.at, "end_reason": old}
            )
            run.recompute[row["target_id"]] = "link" if row["target_type"] == "link" else "item"
        elif entity_type == "link" and field == "end_class":
            columns = {"end_class": old, "active_key": None}
            if old is None:
                key = f"{run.stamp.world_id}|{row['from_item']}|{row['meaning']}|{row['to_item']}"
                if run.tx.run(_LINK_BY_KEY, key=key):
                    raise _conflict("another active link holds this link's key now", link=entity_id)
                columns["active_key"] = key
        elif entity_type == "registry_row" and field == "target_id":
            if run.tx.run(_ALIAS_ON, phrase=row.get("normalised_phrase"), target=old):
                raise _conflict("the alias's old target holds this phrase now", alias=entity_id)
            columns = {field: old}
        else:
            columns = _columns(field, old)
        self.entry(entry, field=field, old=expected, new=old, world_time=entry.get("world_time"))
        if entity_type == "item":
            run.tx.run(ap._SET_ITEM, id=entity_id, columns=columns)
            run.touch(entity_id, self.index)
        elif entity_type == "link":
            run.tx.run(ap._SET_LINK, id=entity_id, columns=columns)
            run.head_changes.update((row["from_item"], row["to_item"]))
        elif entity_type == "evidence":
            run.tx.run(_SET_EVIDENCE, id=entity_id, columns=columns)
        else:
            ontology.set_registry(run, label, entity_id, columns)
        self.bump(entity_type, entity_id, label)


def _undo(run: ap._Run, index: int, op: dict[str, Any]) -> None:
    change_set = op["change_set"]
    run.summaries[index] = f"undo change set {short_id(change_set)}"
    entries = [found["e"] for found in run.tx.run(_ENTRIES_OF_SET, id=change_set)]
    if not entries:
        raise Refused(
            "not_found", "change_set_not_found", f"no change set {change_set} in this world", {}
        )
    if any(entry.get("actor") == "seed" for entry in entries):
        raise Refused("invalid_arguments", "not_undoable", "the starter set is not undone", {})
    if run.tx.run(_UNDONE, id=change_set):
        raise Refused(
            "invalid_arguments", "already_undone", "this change set was undone already", {}
        )
    moved = sorted(
        {
            entry["entity_id"]
            for entry in entries
            if not entry.get("history_only") and entry["field"] not in STILL_MARKERS
        }
    )
    later = run.tx.run(
        _LATER_MOVES,
        after=max(entry["sequence"] for entry in entries),
        ids=moved,
        still=sorted(STILL_MARKERS),
    )
    if later:
        raise _conflict(
            "a row this change set moved has changed since: undo the later change first",
            entity=later[0]["id"],
        )
    value_ids = sorted(
        {
            value_id
            for entry in entries
            for value_id in (entry.get("old_value_id"), entry.get("new_value_id"))
            if value_id
        }
    )
    stored = run.tx.run(_VALUES, ids=value_ids)
    if any(row["forgotten_at"] is not None for row in stored):
        raise Refused("invalid_arguments", "value_forgotten", _FORGOTTEN_REASON, {})
    values = {row["id"]: _decode(row["value"]) for row in stored}
    reason_id = run.text(op["reason"])
    undo = _Undo(run, index, change_set, reason_id)
    for entry in reversed(entries):
        undo.invert(entry, values)
    undo.finish()
    ontology.mark_undone(run, entries, change_set)


HANDLERS = {
    "correct": _correct,
    "refute": _refute,
    "supersede": _supersede,
    "merge": _merge,
    "undo": _undo,
    "retire_record": _retire_record,
}
