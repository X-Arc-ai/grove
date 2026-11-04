"""D6 proposal decisions and explicit mark resolutions, inside the ordinary writer.

A stale target saves a stale decision and recomputes suggestions from the current graph. It
never raises a stale error that would roll that state back, and never applies the new plan.
A decision cannot grant the caller an operation or a target outside its existing authority.
"""

import json
from copy import deepcopy
from typing import Any

from grove.cascade import plan
from grove.store.cascade_reads import snapshot
from grove.writer import apply as ap
from grove.writer import cascade as stored
from grove.writer.clocks import parse_instant
from grove.writer.errors import Refused
from grove.writer.history import encode
from grove.writer.ops import OPS, _check_op
from grove.writer.provenance import connector_type_of
from grove.writer.verify import can_confirm

_OPEN_MARKS = frozenset({"open", "applied", "proposed"})
_DISPOSITIONS = {
    "still_holding": "resolved_still_holding",
    "revised": "resolved_revised",
    "dismissed": "dismissed",
}
_LIVE_CONNECTORS = frozenset({"web_pages", "git", "calendar"})


def _refuse(rule: str, message: str, **details: Any) -> None:
    raise Refused("invalid_arguments", rule, message, details)


def _caller(run, operation: str) -> None:
    if run.caller_kind not in OPS[operation].callers:
        raise Refused(
            "scope_denied",
            "caller_kind",
            f"{operation} does not accept a caller of kind {run.caller_kind}",
            {"caller_kind": run.caller_kind},
        )


def _proposal(run, op: dict) -> tuple[dict, dict, dict]:
    _caller(run, op["op"])
    rows = run.tx.run("MATCH (p:CascadeProposal {id: $id}) RETURN p", id=op["proposal"])
    if not rows:
        raise Refused("not_found", "proposal_not_found", "no proposal in this world", {})
    proposal = rows[0]["p"]
    ap._check_version(op, proposal)
    if proposal["state"] != "open":
        _refuse("proposal_not_open", "only an open proposal accepts a decision")
    targets = run.tx.run("MATCH (i:Item {id: $id}) RETURN i", id=proposal["target_item"])
    target = targets[0]["i"] if targets else {}
    detail = json.loads(proposal.get("detail") or "{}")
    return proposal, target, detail


def _original_change(detail: dict) -> dict | None:
    original = detail.get("trigger_change")
    if not isinstance(original, dict):
        return None
    when = original.get("world_time")
    return {
        **deepcopy(original),
        "item_id": detail["trigger_id"],
        "change_set_id": detail["trigger_change_set"],
        "source_type": detail.get("trigger_source_type", "said"),
        "world_time": parse_instant(when) if when is not None else None,
    }


def _current_plan(run, detail: dict):
    change = _original_change(detail)
    if change is None:
        return None, None, None
    held = snapshot(run.tx, [change["item_id"]], now=run.at, registry=run.registry)
    if change["item_id"] not in held["items"]:
        return None, held, change
    return plan(held, [change]), held, change


def _mark_suggestion(mark: dict) -> dict:
    return {
        "rule": "R2",
        "proposal_type": "cascade",
        "target_item": mark["affected_item"],
        "trigger_id": mark["trigger_id"],
        "trigger_change_set": mark["trigger_change_set"],
        "action": "mark",
        "link_ids": list(mark["path"]),
        "mark_targets": [],
        "world_time": mark.get("world_time"),
        "source_type": mark["source_type"],
        "detail": {"depth": mark["depth"]},
    }


def _recompute(run, proposal: dict, detail: dict) -> tuple[list[str], str | None]:
    result, held, change = _current_plan(run, detail)
    if change is None:
        # Old proposal metadata cannot supply an original time. Preserve the stale state and
        # expose the missing contract; never replace an unknown event time with the clock.
        return [], "missing_trigger_change"
    if result is None:
        return [], "trigger_absent"
    candidates = list(result.proposals)
    if proposal["rule"] == "R2":
        candidates.extend(_mark_suggestion(mark) for mark in result.marks)
    if proposal["rule"] == "R1":
        for ending in result.endings:
            candidates.append(
                {
                    "rule": "R1",
                    "proposal_type": "cascade",
                    "target_item": ending["trigger_id"],
                    "trigger_id": ending["trigger_id"],
                    "trigger_change_set": ending["change_set_id"],
                    "action": "end_link",
                    "link_ids": [ending["link_id"]],
                    "mark_targets": [],
                    "world_time": ending.get("world_time"),
                    "source_type": change["source_type"],
                    "end_clock": ending.get("end_clock"),
                    "detail": {},
                }
            )
    created = []
    seen = set()
    for suggestion in candidates:
        if (
            suggestion["target_item"] != proposal["target_item"]
            or suggestion["rule"] != proposal["rule"]
        ):
            continue
        key = (suggestion["action"], tuple(suggestion.get("link_ids") or []))
        if key in seen:
            continue
        seen.add(key)
        suggestion["detail"] = {
            **suggestion.get("detail", {}),
            "trigger_change": deepcopy(detail["trigger_change"]),
            "recomputed_from": proposal["id"],
        }
        existing_marks = [
            row["id"]
            for row in run.tx.run(
                "MATCH (m:Impact {trigger_change_set: $change_set}) "
                "WHERE m.affected_item IN $targets RETURN m.link_id AS id",
                change_set=detail["trigger_change_set"],
                targets=suggestion.get("mark_targets") or [],
            )
        ]
        created.append(stored._proposal(run, suggestion, existing_marks, held))
    return created, None


def _save_decision(
    run, proposal: dict, state: str, reason: str, *, detail=None, sequences=()
) -> None:
    reason_id = run.text(reason)
    columns = {
        "state": state,
        "decided_by": run.stamp.actor,
        "decision_reason": reason_id,
        "resulting_commit": run.stamp.commit_id,
        "version": proposal.get("version", 0) + 1,
        "touched_at": run.at,
    }
    if detail is not None:
        columns["detail"] = encode(detail)
    run.tx.run(
        "MATCH (p:CascadeProposal {id: $id}) SET p += $columns", id=proposal["id"], columns=columns
    )
    stored._marker(
        run,
        proposal["target_item"],
        "cascade_disposition",
        {
            "id": proposal["id"],
            "kind": "proposal",
            "state": state,
            "operation_sequences": list(sequences),
        },
    )


def _stale(run, proposal: dict, target: dict, detail: dict, op: dict) -> bool:
    if target and target.get("version") == detail.get("target_version"):
        return False
    replacements, error = _recompute(run, proposal, detail)
    updated = {**detail, "recomputed_proposals": replacements}
    if error is not None:
        updated["recompute_error"] = error
    _save_decision(run, proposal, "stale", op["reason"], detail=updated)
    return True


def reject_proposal(run, index: int, op: dict) -> None:
    proposal, target, detail = _proposal(run, op)
    run.summaries[index] = f"proposal {run.label(op['proposal'])}"
    if not _stale(run, proposal, target, detail, op):
        _save_decision(run, proposal, "rejected", op["reason"])


def _operations(run, proposal: dict, detail: dict, op: dict) -> list[dict]:
    action = detail["action"]
    if "apply_mark" in op and action != "mark":
        _refuse("proposal_action", "apply_mark only accepts a mark proposal")
    if "operations" in op:
        operations = deepcopy(op["operations"])
    else:
        operations = []
        for suggested in json.loads(proposal.get("suggested_operations") or "[]"):
            if suggested.get("op") != "change_status":
                _refuse("proposal_operation", "the stored operation needs explicit approval")
            found = run.tx.run(
                "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value",
                id=suggested.get("status_value_id"),
            )
            if not found:
                _refuse("proposal_value_absent", "the proposed status value is absent")
            operations.append(
                {
                    "op": "change_status",
                    "op_id": f"{op['op_id']}:status",
                    "item": suggested["item"],
                    "status": found[0]["value"],
                    "evidence_refs": deepcopy(op.get("evidence_refs") or []),
                    "reason": op["reason"],
                }
            )
    if action in {"review", "wide_impact", "mark"} and operations:
        _refuse("proposal_action", "this proposal has no ordinary writer operation")
    if action in {"resume", "end_link", "reopen_links"} and not operations:
        _refuse("proposal_operations_required", "acceptance needs explicit writer operations")
    if action == "mark" and op.get("apply_mark") is not True:
        _refuse("proposal_mark_approval", "a mark proposal needs apply_mark true")
    if action not in {"resume", "end_link", "reopen_links", "review", "wide_impact", "mark"}:
        _refuse("proposal_action", "this proposal action has no decision handler")
    for operation in operations:
        _check_op(operation)
        _caller(run, operation["op"])
        _operation_scope(run, proposal, detail, operation)
    return operations


def _operation_scope(run, proposal: dict, detail: dict, operation: dict) -> None:
    action, name = detail["action"], operation["op"]
    if action == "resume":
        if name != "change_status" or operation["item"] != proposal["target_item"]:
            _refuse("proposal_scope", "a resume operation changes only its proposal target")
        return
    ids = detail.get("link_ids") or []
    if action == "end_link":
        if name != "end_link" or operation["link"] not in ids:
            _refuse("proposal_scope", "an ending names only a link of its proposal")
        return
    if action != "reopen_links" or name != "create_link":
        _refuse("proposal_scope", "the operation does not implement this proposal")
    meaning, reversed_ = run.registry.resolve_meaning(operation["meaning"])
    source, target = operation["from"], operation["to"]
    if reversed_:
        source, target = target, source
    found = run.tx.run(
        "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.id IN $ids AND l.from_item = $source "
        "AND l.to_item = $target AND l.meaning = $meaning RETURN l.id AS id",
        ids=ids,
        source=source,
        target=target,
        meaning=meaning["id"],
    )
    if not found:
        _refuse("proposal_scope", "a reopened link keeps its original endpoints and meaning")


def _apply_mark(run, proposal: dict, detail: dict) -> None:
    result, held, change = _current_plan(run, detail)
    if result is None:
        _refuse("proposal_obsolete", "the proposal cannot be recomputed from its original trigger")
    target = proposal["target_item"]
    candidates = [mark for mark in result.marks if mark["affected_item"] == target]
    for suggestion in result.proposals:
        if (
            suggestion["rule"] != "R2"
            or suggestion["action"] != "mark"
            or suggestion["target_item"] != target
        ):
            continue
        when = change["world_time"]
        latest = held["latest_change"].get(target)
        candidates.append(
            {
                "trigger_id": change["item_id"],
                "trigger_change_set": change["change_set_id"],
                "affected_item": target,
                "depth": suggestion["detail"]["depth"],
                "path": list(suggestion["link_ids"]),
                "source_type": change["source_type"],
                "world_time": when,
                "state": "born_resolved"
                if when is not None and latest is not None and latest > when
                else "open",
            }
        )
    if not candidates:
        _refuse("proposal_obsolete", "the target is no longer on the consequence path")
    mark_id = stored._mark(run, candidates[0], held)
    run.tx.run(
        "MATCH (m:Impact {link_id: $id}) SET m.proposal_id = $proposal",
        id=mark_id,
        proposal=proposal["id"],
    )
    run.tx.run(
        "MATCH (p:CascadeProposal {id: $id}) SET p.mark_ids = $ids",
        id=proposal["id"],
        ids=sorted({*proposal.get("mark_ids", []), mark_id}),
    )


def accept_proposal(run, index: int, op: dict) -> None:
    proposal, target, detail = _proposal(run, op)
    run.summaries[index] = f"proposal {run.label(op['proposal'])}"
    if _stale(run, proposal, target, detail, op):
        return
    operations = _operations(run, proposal, detail, op)
    before = run.tx.run(
        "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN coalesce(max(e.sequence), 0) AS last",
        id=run.stamp.change_set_id,
    )[0]["last"]
    previous = run.current_op
    try:
        for operation in operations:
            run.current_op = operation
            ap._HANDLERS[operation["op"]](run, index, operation)
        if detail["action"] == "mark":
            _apply_mark(run, proposal, detail)
    finally:
        run.current_op = previous
    sequences = [
        row["sequence"]
        for row in run.tx.run(
            "MATCH (e:ChangeEntry {change_set_id: $id}) WHERE e.sequence > $after "
            "RETURN e.sequence AS sequence ORDER BY e.sequence",
            id=run.stamp.change_set_id,
            after=before,
        )
    ]
    _save_decision(run, proposal, "accepted", op["reason"], sequences=sequences)


def _live_evidence(run, mark: dict, refs: list[dict]) -> list:
    plans = ap._plans(run, refs)
    link = run.tx.run("MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=mark["link_id"])
    created = link[0]["l"].get("recorded") if link else None
    for proof in plans:
        row = proof.columns
        if row.get("derivation") != "observed" or row.get("source_type") != "confirmed":
            continue
        rows = run.tx.run(
            "MATCH (s:Source {id: $id}) MATCH (c:Connector {id: s.connector_id}) RETURN s, c",
            id=row["source_id"],
        )
        if not rows:
            continue
        source, connector = rows[0]["s"], rows[0]["c"]
        fetched = source.get("received_at")
        if connector_type_of(connector) not in _LIVE_CONNECTORS:
            continue
        if fetched is None or created is None or fetched < created or fetched > run.at:
            continue
        if can_confirm(run, row, "item", mark["affected_item"]):
            return plans
    raise Refused(
        "missing_provenance",
        "mark_live_source",
        "cleanup resolves a mark with a live source fetched after the mark",
        {},
    )


def resolve_mark(run, index: int, op: dict) -> None:
    _caller(run, "resolve_mark")
    rows = run.tx.run("MATCH (m:Impact {link_id: $id}) RETURN m", id=op["mark"])
    if not rows:
        raise Refused("not_found", "mark_not_found", "no mark in this world", {})
    mark = rows[0]["m"]
    ap._check_version(op, mark)
    if mark["state"] not in _OPEN_MARKS:
        _refuse("mark_not_open", "only an open mark accepts a disposition")
    plans = (
        _live_evidence(run, mark, op.get("evidence_refs") or [])
        if run.caller_kind == "cleanup"
        else ap._plans(run, op.get("evidence_refs") or [])
    )
    reason_id = run.text(op["reason"])
    if plans:
        ap._write_evidence(run, plans, "item", mark["affected_item"], reason_id)
    state = _DISPOSITIONS[op["disposition"]]
    run.tx.run(
        "MATCH (m:Impact {link_id: $id}) SET m.state = $state, m.resolved_by = $actor, "
        "m.resolved_at = $at, m.resolution_reason = $reason, m.touched_at = $at, "
        "m.version = m.version + 1",
        id=mark["link_id"],
        state=state,
        actor=run.stamp.actor,
        at=run.at,
        reason=reason_id,
    )
    stored._marker(
        run,
        mark["affected_item"],
        "cascade_disposition",
        {
            "id": mark["link_id"],
            "kind": "mark",
            "state": state,
        },
    )
    run.summaries[index] = f"mark {run.label(op['mark'])}"


HANDLERS = {
    "accept_proposal": accept_proposal,
    "reject_proposal": reject_proposal,
    "resolve_mark": resolve_mark,
}
