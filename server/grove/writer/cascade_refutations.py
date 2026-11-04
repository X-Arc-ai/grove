"""D6's ordinary-item refutation gate. D9 owns the separate belief state machine."""

from grove.writer.errors import Refused
from grove.writer.history import compact
from grove.writer.source_types import RANK


def _observed_rank(source_type, derivation):
    return RANK.get(source_type, -1) if derivation == "observed" else -1


def refutation_proof(plans, *, rank=_observed_rank):
    """Bind authority, provenance and world time to the same contradictory proof."""
    contrary = [plan for plan in plans if plan.polarity == "contradicts"]
    if not contrary:
        raise Refused(
            "missing_provenance",
            "refute_evidence",
            "a refutation needs contradictory evidence",
            {},
        )
    return max(
        contrary,
        key=lambda plan: (
            rank(plan.columns.get("source_type"), plan.columns.get("derivation")),
            plan.source_time,
        ),
    )


def defer_refutation(run, index, op, item, plans) -> bool:
    """A report attaches its contradiction and proposes an ending; it does not end a claim."""
    from grove.writer import apply as ap

    proof = refutation_proof(plans)
    source_type = (
        proof.columns["source_type"] if proof.columns.get("derivation") == "observed" else "said"
    )
    if run.caller_kind == "person" or source_type == "confirmed":
        return False
    item_id = item["id"]
    reason_id = run.text(op["reason"])
    contrary = [plan for plan in plans if plan.polarity == "contradicts"]
    ap._write_evidence(run, plans, "item", item_id, reason_id)
    standing = run.tx.run(
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id}) "
        "WHERE e.history_only = false RETURN max(e.sequence) AS sequence",
        id=item_id,
    )[0]["sequence"]
    for report in contrary:
        if run.tx.run(
            "MATCH (c:Contradiction {target: $target, fresh_evidence_id: $evidence, "
            "status: 'open'}) RETURN c.id AS id",
            target=item_id,
            evidence=report.id,
        ):
            continue
        run.tx.run(
            "CREATE (c:Contradiction) SET c += $row",
            row=compact(
                {
                    "id": run.new_id(),
                    "target": item_id,
                    "standing_target_id": item_id,
                    "standing_change_id": standing,
                    "fresh_evidence_id": report.id,
                    "detected_by": run.stamp.actor,
                    "detected_at": run.at,
                    "request_id": run.stamp.request_id,
                    "status": "open",
                    "version": 1,
                    "touched_at": run.at,
                }
            ),
        )
    run.cache.setdefault("cascade_pending", []).append(
        {
            "item_id": item_id,
            "change_set_id": run.stamp.change_set_id,
            "world_time": proof.source_time,
            "source_type": source_type,
            "terminal": True,
            "reopened": False,
            "person_correction": False,
            "end_clock": None,
            "fields": [],
        }
    )
    run.touch(item_id, index)
    return True


def settle_refutation(run, item_id, reason_id):
    """A confirmed refutation closes its ordinary-item report queue in the same commit."""
    run.tx.run(
        "MATCH (c:Contradiction {target: $id, status: 'open'}) "
        "SET c.status = 'resolved', c.resolution = $reason, c.resolved_by = $actor, "
        "c.resolved_at = $at, c.version = c.version + 1, c.touched_at = $at",
        id=item_id,
        reason=reason_id,
        actor=run.stamp.actor,
        at=run.at,
    )


def undo_refutation_reports(run, change_set):
    """Undo retains the queue row and the evidence history, with an explicit disposition."""
    run.tx.run(
        "MATCH (e:ChangeEntry {change_set_id: $id, entity_type: 'evidence', field: 'row'}) "
        "MATCH (c:Contradiction {fresh_evidence_id: e.entity_id, status: 'open'}) "
        "SET c.status = 'resolved', c.resolution = $reason, c.resolved_by = $actor, "
        "c.resolved_at = $at, c.version = c.version + 1, c.touched_at = $at",
        id=change_set,
        reason=run.text("The report was undone."),
        actor=run.stamp.actor,
        at=run.at,
    )
