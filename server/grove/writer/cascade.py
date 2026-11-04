"""The D6 transaction adapter: plan in code, validate, then write consequences.

Impact and proposal metadata remain snapshot rows. Their history markers follow D4's
verification pattern. The affected-by link and all R1 endings are ordinary D3 history.
"""

import json

from grove.store.cascade_reads import snapshot
from grove.store.current import TERMINAL_ROLES
from grove.store.primitives import KIND_BELIEF, MEANING_AFFECTED_BY
from grove.writer.clocks import Clock, clock_columns, parse_clock, sort_key
from grove.writer.errors import Refused
from grove.writer.history import compact, encode, row_json
from grove.writer.message_binding import check_reused_evidence
from grove.writer.registry import Registry
from grove.writer.source_types import RANK

MARKERS = frozenset({"cascade_mark", "cascade_proposal", "cascade_end", "cascade_disposition"})


def _decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def _changes(run) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for entry in run.cascade_entries:
        if entry.get("inverse_of") or entry.get("history_only"):
            continue
        if entry["field"] not in ("status_role", "end_class", "properties", "reopened", "state"):
            continue
        if entry.get("new") == entry.get("old"):
            continue
        key = (entry["entity_id"], entry.get("reason_id"))
        groups.setdefault(key, []).append(entry)
    result = []
    for (item_id, reason_id), entries in groups.items():
        rows = run.tx.run("MATCH (i:Item {id: $id}) RETURN i", id=item_id)
        if not rows:
            continue
        item = rows[0]["i"]
        if item["kind"] == KIND_BELIEF:
            # D9 supplies a checked, operation-specific trigger; its item evidence may be older.
            continue
        kind = run.registry.kind(item["kind"])
        propagating = _decoded(kind.get("propagating_fields") or "[]")
        names = {p if isinstance(p, str) else p.get("name") for p in propagating}
        # D4's code-held schedule declarations also declare D6 date propagation. Existing
        # worlds need no migration and neither rule guesses a property's meaning from its name.
        names.update(
            p["name"]
            for p in run.registry.property_names
            if p["kind"] == item["kind"] and p.get("scheduled_role") in ("start", "due", "end")
        )
        names = {
            declared["name"]
            for name in names
            if (declared := run.registry.property_name(item["kind"], name))
            and declared.get("value_type") == "time"
        }
        fields, terminal, reopened = [], False, False
        relevant = []
        for entry in entries:
            field, value = entry["field"], entry.get("new")
            if field == "properties":
                old, new = entry.get("old") or {}, value or {}
                changed = sorted(name for name in names if old.get(name) != new.get(name))
                if not changed:
                    continue
                fields.extend(changed)
            elif field == "status_role":
                terminal = value in TERMINAL_ROLES
            elif field == "end_class":
                if not value:
                    continue
                terminal = True
            elif field == "state":
                if value not in ("weakening", "superseded"):
                    continue
            elif field == "reopened":
                reopened = True
            relevant.append(entry)
        if not relevant:
            continue
        entry = relevant[-1]
        operation = entry.get("operation") or {}
        evidence_id = entry.get("evidence_id")
        proofs = []
        for ref in operation.get("evidence_refs") or []:
            polarity = "contradicts" if operation.get("op") == "refute" else "supports"
            if ref.get("polarity") != polarity:
                continue
            if ref.get("ev_id"):
                proofs.append(run.observation(ref["ev_id"]))
            else:
                rows = run.tx.run("MATCH (e:Evidence {id: $id}) RETURN e", id=ref["evidence_id"])
                if not rows:
                    raise Refused("missing_provenance", "evidence_not_found", "no active evidence")
                check_reused_evidence(run, [row["e"] for row in rows])
                proofs.extend(row["e"] for row in rows)
        proof = max(
            proofs,
            key=lambda row: (
                RANK.get(row.get("source_type"), -1)
                if row.get("derivation") not in ("inferred", "rolled_up")
                else -1
            ),
            default={},
        )
        source_type = proof.get("source_type", "said")
        if proof.get("derivation") in ("inferred", "rolled_up"):
            source_type = "said"
        when = entry.get("world_time")
        stop = next(
            (
                e.get("new")
                for e in run.cascade_entries
                if e["entity_id"] == item_id
                and e.get("reason_id") == reason_id
                and e["field"] == "stopped_being_true"
                and e.get("new")
            ),
            None,
        )
        if stop is not None:
            stop = (
                {k: v for k, v in stop.items() if k != "words_id"}
                if isinstance(stop, dict)
                else stop
            )
            when = sort_key(parse_clock(stop))
        result.append(
            {
                "item_id": item_id,
                "change_set_id": run.stamp.change_set_id,
                "world_time": when,
                "source_type": source_type,
                "terminal": terminal,
                "reopened": reopened,
                "fields": fields,
                "end_clock": stop,
                "person_correction": run.caller_kind == "person"
                and operation.get("op") in ("correct", "refute", "supersede"),
                "evidence_id": evidence_id,
            }
        )
    return result + list(run.cache.get("cascade_pending", ())) + typed_changes(run)


def typed_changes(run) -> list[dict]:
    """The code-held D9 handoff. Public batches cannot write run.cache or its authority flag."""
    changes = []
    for raw in run.cache.get("belief_cascade_changes", ()):
        change = dict(raw)
        if change["change_set_id"] != run.stamp.change_set_id:
            raise ValueError("a belief trigger must belong to this change set")
        found = run.tx.run("MATCH (i:Item {id: $id}) RETURN i.kind AS kind", id=change["item_id"])
        if not found or found[0]["kind"] != KIND_BELIEF:
            raise ValueError("a belief trigger must name a belief in this world")
        if change.get("derivation") in ("inferred", "rolled_up"):
            change["source_type"] = min((change["source_type"], "said"), key=RANK.__getitem__)
        changes.append(change)
    return changes


def _marker(run, item_id: str, field: str, value: dict, *, inverse_of=None):
    run.entry(
        entity_type="item",
        entity_id=item_id,
        field=field,
        new=value,
        world_time=None,
        inverse_of=inverse_of,
    )


def _proposal(run, suggestion: dict, mark_ids: list[str], held: dict) -> str:
    proposal_id = run.new_id()
    target = suggestion["target_item"]
    detail = {
        **(suggestion.get("detail") or {}),
        "trigger_id": suggestion["trigger_id"],
        "trigger_change_set": suggestion.get("trigger_change_set", run.stamp.change_set_id),
        "action": suggestion["action"],
        "link_ids": suggestion.get("link_ids", []),
        "target_version": held["items"].get(target, {}).get("version"),
        "trigger_source_type": suggestion.get("source_type", "said"),
        "trigger_change": suggestion.get("trigger_change")
        or (suggestion.get("detail") or {}).get("trigger_change")
        or {},
    }
    # Status vocabulary is stored as a value, never copied into JSON metadata.
    operations = []
    if suggestion.get("suggested_status"):
        operations.append(
            {
                "op": "change_status",
                "item": target,
                "status_value_id": run.text(suggestion["suggested_status"]),
            }
        )
    row = compact(
        {
            "id": proposal_id,
            "rule": suggestion["rule"],
            "mark_ids": mark_ids,
            "target_item": target,
            "suggested_operations": encode(operations),
            "stakes": "high",
            "state": "open",
            "proposal_type": suggestion["proposal_type"],
            "detail": encode(detail),
            "version": 1,
            "touched_at": run.at,
        }
    )
    run.tx.run("CREATE (p:CascadeProposal) SET p += $row", row=row)
    _marker(run, target, "cascade_proposal", {"id": proposal_id, "action": suggestion["action"]})
    return proposal_id


def _mark(run, mark: dict, held: dict) -> str:
    from grove.writer import apply as ap

    affected, trigger = mark["affected_item"], mark["trigger_id"]
    old = run.tx.run(
        "MATCH (m:Impact {trigger_change_set: $change_set, affected_item: $affected}) "
        "RETURN m.link_id AS id",
        change_set=mark["trigger_change_set"],
        affected=affected,
    )
    if old:
        return old[0]["id"]
    link_id = run.new_id()
    when = mark.get("world_time")
    start = Clock("unknown") if when is None else Clock("instant", when, when)
    reason_id = run.text("A change to " + held["items"][trigger]["title"] + " affects this item.")
    row = compact(
        {
            "id": link_id,
            "from_item": affected,
            "to_item": trigger,
            "meaning": MEANING_AFFECTED_BY,
            **clock_columns("became_true", start),
            "recorded": run.at,
            "provenance_reason": reason_id,
            "version": 1,
            "touched_at": run.at,
            "active_key": (
                f"{run.stamp.world_id}|{affected}|{MEANING_AFFECTED_BY}|{trigger}|"
                f"{mark['trigger_change_set']}"
            ),
        }
    )
    run.tx.run(ap._ADD_LINK, a=affected, b=trigger, row=row)
    run.entry(
        entity_type="link",
        entity_id=link_id,
        field="row",
        new=row_json("links", row, run.stamp.world_id),
        world_time=when,
        reason_id=reason_id,
    )
    run.tx.run(
        "CREATE (m:Impact) SET m += $row",
        row={
            "link_id": link_id,
            "trigger_change_set": mark["trigger_change_set"],
            "affected_item": affected,
            "rule": "R2",
            "depth": mark["depth"],
            "path": mark["path"],
            "stakes": "low",
            "trigger_source_type": mark["source_type"],
            "state": mark["state"],
            "version": 1,
            "touched_at": run.at,
        },
    )
    _marker(run, affected, "cascade_mark", {"id": link_id, "trigger_id": trigger})
    return link_id


def apply_cascade(run) -> None:
    """Called only after the ordinary batch passes. No model runs in this transaction."""
    from grove.cascade import plan
    from grove.writer import apply as ap

    changes = _changes(run)
    if not changes:
        return
    held = snapshot(
        run.tx, sorted({c["item_id"] for c in changes}), now=run.at, registry=run.registry
    )
    proposed = plan(held, changes)
    for change in changes:
        _marker(run, change["item_id"], "cascade_trigger", change)
    suggestions = list(proposed.proposals)
    failed_downstream = set()
    for ending in proposed.endings:
        when = ending["world_time"]
        stop = ending.get("end_clock") or {
            "earliest": when.isoformat(),
            "latest": when.isoformat(),
            "precision": "instant",
        }
        operation = {
            "op": "end_link",
            "link": ending["link_id"],
            "end_class": "ended_in_the_world",
            "stopped_being_true": stop,
            "reason": "Ended with " + held["items"][ending["trigger_id"]]["title"],
        }
        try:
            prepared = ap._prepare_end_link(run, operation)
        except Refused as error:
            failed = next(link for link in held["links"] if link["id"] == ending["link_id"])
            meaning = held["meanings"][failed["meaning"]]
            failed_downstream.add(
                failed["from_item" if meaning["downstream_end"] == "from" else "to_item"]
            )
            suggestions.append(
                {
                    "rule": "R1",
                    "proposal_type": "cascade_failed",
                    "action": "review",
                    "target_item": ending["trigger_id"],
                    "trigger_id": ending["trigger_id"],
                    "trigger_change_set": ending["change_set_id"],
                    "link_ids": [ending["link_id"]],
                    "mark_targets": [],
                    "detail": {"code": error.code, "rule": error.rule},
                }
            )
            continue
        ap._write_end_link(run, operation, prepared)
        run.entry(
            entity_type="link",
            entity_id=ending["link_id"],
            field="cascade_end",
            new={"trigger_id": ending["trigger_id"]},
            world_time=when,
        )
    mark_ids = {}
    for mark in proposed.marks:
        mark_ids[(mark["trigger_id"], mark["affected_item"])] = _mark(run, mark, held)
    for suggestion in suggestions:
        if suggestion["action"] == "resume" and suggestion["target_item"] in failed_downstream:
            suggestion = {**suggestion, "action": "review"}
            suggestion.pop("suggested_status", None)
        trigger = next(c for c in changes if c["item_id"] == suggestion["trigger_id"])
        suggestion = {
            **suggestion,
            "trigger_change": {
                key: trigger.get(key)
                for key in ("world_time", "terminal", "reopened", "person_correction", "end_clock")
            },
        }
        ids = [
            mark_ids[(suggestion["trigger_id"], target)]
            for target in suggestion.get("mark_targets", [])
            if (suggestion["trigger_id"], target) in mark_ids
        ]
        proposal_id = _proposal(run, suggestion, ids, held)
        if ids:
            run.tx.run(
                "MATCH (m:Impact) WHERE m.link_id IN $ids SET m.proposal_id = $proposal",
                ids=ids,
                proposal=proposal_id,
            )
    # The end of an affected item is an explicit resolution. The mark remains history.
    ended = [
        c["item_id"]
        for c in changes
        if held["items"][c["item_id"]].get("end_class")
        or held["items"][c["item_id"]].get("status_role") in TERMINAL_ROLES
    ]
    if ended:
        run.tx.run(
            "MATCH (m:Impact) WHERE m.affected_item IN $ids AND m.state IN $states "
            "SET m.state = 'resolved_revised', m.resolved_by = 'writer', m.resolved_at = $at, "
            "m.version = m.version + 1, m.touched_at = $at",
            ids=ended,
            states=["open", "proposed", "applied"],
            at=run.at,
        )


def undo_marks(run, change_set: str) -> None:
    from grove.writer.cascade_refutations import undo_refutation_reports

    undo_refutation_reports(run, change_set)
    rows = run.tx.run("MATCH (m:Impact {trigger_change_set: $id}) RETURN m", id=change_set)
    for found in rows:
        mark = found["m"]
        run.tx.run(
            "MATCH (m:Impact {link_id: $id}) SET m.state = 'undone', m.resolved_by = $actor, "
            "m.resolved_at = $at, m.version = m.version + 1, m.touched_at = $at",
            id=mark["link_id"],
            actor=run.stamp.actor,
            at=run.at,
        )
        _marker(
            run,
            mark["affected_item"],
            "cascade_disposition",
            {"id": mark["link_id"], "state": "undone"},
            inverse_of=change_set,
        )
    for found in run.tx.run("MATCH (p:CascadeProposal {state: 'open'}) RETURN p"):
        row = found["p"]
        if json.loads(row["detail"]).get("trigger_change_set") == change_set:
            run.tx.run(
                "MATCH (p:CascadeProposal {id: $id}) SET p.state = 'stale', "
                "p.version = p.version + 1, p.touched_at = $at",
                id=row["id"],
                at=run.at,
            )


def health(source) -> dict:
    from grove.store.current import read_rows

    registry = Registry.load(source)
    meanings = [
        key for key, value in registry.meanings.items() if value.get("carries_consequences")
    ]
    bad = read_rows(
        source,
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.active_key IS NOT NULL "
        "AND l.meaning IN $meanings AND (a.end_class IS NOT NULL OR b.end_class IS NOT NULL "
        "OR a.status_role IN $roles OR b.status_role IN $roles) RETURN count(l) AS n",
        meanings=meanings,
        roles=sorted(TERMINAL_ROLES),
    )[0]["n"]
    marks = read_rows(source, "MATCH (m:Impact {state: 'open'}) RETURN m.touched_at AS at")
    proposals = read_rows(
        source, "MATCH (p:CascadeProposal) RETURN p.state AS state, p.proposal_type AS type"
    )
    return {
        "live_links_to_ended": bad,
        "open_marks": marks,
        "waiting_proposals": sum(r["state"] == "open" for r in proposals),
        "cascade_failures": sum(r["type"] == "cascade_failed" for r in proposals),
    }
