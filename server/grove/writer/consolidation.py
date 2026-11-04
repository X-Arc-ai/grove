"""The D12 safety-net dispositions, within the guarded writer transaction."""

import json
from datetime import datetime

from grove.consolidation.cascades import FIELDS, pending_proposals, pending_reviews, review_key
from grove.consolidation.packet import PacketTooLarge, successful_cursor
from grove.store.cascade_reads import snapshot
from grove.writer import cascade, cascade_decisions
from grove.writer.errors import Refused
from grove.writer.history import compact, encode


def refuse(rule, message):
    raise Refused("invalid_arguments", rule, message, {})


def guard(run, op):
    try:
        reviews = pending_reviews(
            run.tx, since=successful_cursor(run.tx), now=run.at, registry=run.registry
        )
    except PacketTooLarge:
        refuse("consolidation_stale", "dependency work now exceeds the complete packet bound")
    actual = {review_key(row): row for row in reviews}
    expected = {review_key(row): row for row in op.get("cascade_review_refs", [])}
    if actual != expected:
        refuse("consolidation_stale", "dependency work changed after the packet read")
    proposals = {row["id"]: row.get("version", 1) for row in pending_proposals(run.tx)}
    supplied = {row["id"]: row["version"] for row in op.get("cascade_proposal_refs", [])}
    if proposals != supplied:
        refuse("consolidation_stale", "cascade proposals changed after the packet read")
    ops = run.cache["all_ops"]
    decisions = [entry for entry in ops if entry.get("op") == "review_cascade"]
    if len(decisions) != len(actual) or {review_key(entry) for entry in decisions} != set(actual):
        refuse("consolidation_unresolved", "every dependent needs exactly one disposition")
    answers = [
        entry
        for entry in ops
        if entry.get("op") in {"accept_proposal", "reject_proposal", "question_proposal"}
    ]
    if len(answers) != len(proposals) or {entry["proposal"] for entry in answers} != set(proposals):
        refuse("consolidation_unresolved", "every cascade proposal needs exactly one decision")
    run.cache["cascade_review_work"] = actual


def _sources(run, op):
    sources = op.get("checked_source_ids") or []
    if not sources:
        refuse(
            "cascade_review_sources", "a disposition cites retained sources checked by this pass"
        )
    for source in sources:
        if not run.tx.run("MATCH (s:Source {id: $id}) RETURN s.id AS id", id=source):
            refuse("cascade_review_sources", "a disposition cites only sources of this world")
    return sources


def review_cascade(run, index, op):
    row = run.cache.get("cascade_review_work", {}).get(review_key(op))
    if row is None:
        refuse("cascade_review_scope", "the disposition must name this guarded pass's dependent")
    if op["review_token"] != row["review_token"]:
        refuse("consolidation_stale", "the dependency changed after the packet read")
    sources = _sources(run, op)
    done = run.cache["done_ops"]
    referenced = op.get("operation_ids", [])
    if any(key not in done for key in referenced):
        refuse("cascade_review_operations", "review operations must precede the disposition")
    relevant = [
        done[key]
        for key in referenced
        if done[key].get("target", done[key].get("item")) == op["target"]
    ]
    if op["disposition"] == "revised" and not any(
        entry.get("operation", {}).get("op_id") in referenced
        and entry.get("entity_id") == op["target"]
        and entry.get("field") in FIELDS
        and not entry.get("history_only")
        and entry.get("old") != entry.get("new")
        for entry in run.cascade_entries
    ):
        refuse("cascade_review_operations", "a revised dependent needs a committed revision")
    if op["disposition"] == "queued" and not any(
        entry["op"] == "open_contradiction" for entry in relevant
    ):
        refuse("cascade_review_operations", "a queued dependent needs a stored contradiction")
    if (row["flag_gap"] or row["missing_mark"]) and not op.get("gap_reason"):
        refuse("cascade_gap_reason", "the missed consequence flag needs a recorded gap reason")
    if row["missing_mark"]:
        mark = dict(row["mark"])
        if mark["world_time"] is not None:
            mark["world_time"] = datetime.fromisoformat(mark["world_time"])
        held = snapshot(run.tx, [row["trigger"]], now=run.at, registry=run.registry)
        cascade._mark(run, mark, held)
    cascade._marker(
        run,
        op["target"],
        "cascade_review",
        {
            **row,
            "disposition": op["disposition"],
            "source_ids": sources,
            "operation_ids": referenced,
            "reason_id": run.text(op["reason"]),
            "gap_reason_id": run.text(op["gap_reason"]) if op.get("gap_reason") else None,
        },
    )


def question_proposal(run, index, op):
    if "cascade_review_work" not in run.cache:
        refuse("consolidation_run", "a proposal question needs a guarded Connector pass")
    # Scope and stale-target handling remain the accepted D6 adapter's responsibility.
    proposal, target, detail = cascade_decisions._proposal(run, op)
    if cascade_decisions._stale(run, proposal, target, detail, op):
        return
    sources = _sources(run, op)
    options = op["options"]
    if len(options) != 2 or options[0].strip() == options[1].strip():
        refuse("cascade_question", "a question needs two different readings")
    question_id = run.new_id()
    reason_id = run.text(op["reason"])
    run.tx.run(
        "CREATE (q:Question) SET q += $row",
        row=compact(
            {
                "id": question_id,
                "subject_items": [proposal["target_item"]],
                "ask": run.text(op["question"]),
                "class": "fact_only_the_person_holds",
                "legwork_sources": sources,
                "legwork_reason": reason_id,
                "answer_options": json.dumps([{"value_id": run.text(value)} for value in options]),
                "raised_by": run.stamp.actor,
                "raised_at": run.at,
                "priority": 1,
                "state": "open",
                "version": 1,
                "touched_at": run.at,
            }
        ),
    )
    run.tx.run(
        "MATCH (p:CascadeProposal {id: $id}) SET p.detail = $detail, "
        "p.version = p.version + 1, p.touched_at = $at",
        id=proposal["id"],
        detail=encode({**detail, "question_id": question_id}),
        at=run.at,
    )
    cascade._marker(
        run,
        proposal["target_item"],
        "cascade_disposition",
        {
            "id": proposal["id"],
            "kind": "proposal",
            "state": "open",
            "question_id": question_id,
        },
    )


HANDLERS = {"review_cascade": review_cascade, "question_proposal": question_proposal}
