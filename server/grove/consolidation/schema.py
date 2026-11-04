"""The versioned D12 envelope, translated to one writer batch with a queue guard.

Field catalogues come from the writer's OpSpec objects. The envelope adds order and run
binding; it never grants a caller an operation the writer would refuse.
"""

from typing import Any

from grove.consolidation.packet import QUESTION_AGE, work
from grove.writer.errors import BatchDeclined, Refusal
from grove.writer.ops import OPS, check_shape

SCHEMA = "grove.consolidate.v1"
SECTIONS = (
    "evidence",
    "resolutions",
    "belief_ops",
    "dispositions",
    "operations",
    "contradictions",
    "support_changes",
    "ontology_ops",
    "walk",
    "questions",
    "declines",
)
GENERIC = frozenset(
    {
        "create_item",
        "update_item",
        "change_status",
        "end_item",
        "reopen_item",
        "create_link",
        "end_link",
        "attach_evidence",
        "end_evidence",
        "correct",
        "refute",
        "supersede",
    }
)
BELIEF = {
    "mint": "mint_belief",
    "promote": "promote_belief",
    "weaken": "weaken_belief",
    "restore": "restore_belief",
    "supersede": "supersede_belief",
    "retire": "retire_belief",
    "merge": "merge_beliefs",
}
EXTRA = frozenset(
    {
        "verify",
        "retest_belief",
        "open_contradiction",
        "resolve_contradiction",
        "resolve_support_change",
    }
)
MAX_ENTRIES = 500


def refuse(rule: str, message: str) -> None:
    raise BatchDeclined([Refusal(None, None, "invalid_arguments", rule, message, {})], batch_size=1)


def operation_catalogue() -> dict[str, Any]:
    """The fields and caller allowlist derive from the same catalogue as writer validation."""
    return {
        name: {"required": ["op", *spec.required], "optional": list(spec.optional)}
        for name, spec in OPS.items()
        if name in GENERIC | set(BELIEF.values()) | EXTRA and "consolidation" in spec.callers
    }


def empty_envelope(run: dict[str, Any]) -> dict[str, Any]:
    return {"schema": SCHEMA, "run": run, **{key: [] for key in SECTIONS}}


def _object(row: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(row, dict) or set(row) - required - optional or required - set(row):
        refuse("consolidation_shape", f"{label} has missing or unknown fields")
    return row


def _rows(envelope: dict, name: str) -> list[dict]:
    rows = envelope.get(name, [])
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_ENTRIES
        or any(not isinstance(r, dict) for r in rows)
    ):
        refuse("consolidation_shape", f"{name} is a bounded list of objects")
    return rows


def _operation(row: dict, allowed: frozenset | set) -> dict:
    name = row.get("op")
    if not isinstance(name, str) or name not in allowed or name not in OPS:
        refuse("consolidation_operation", "the operation is not in this consolidation section")
    spec = OPS[name]
    if "consolidation" not in spec.callers:
        refuse("consolidation_operation", "the Connector cannot use this operation")
    _object(row, {"op", *spec.required}, set(spec.optional), name)
    if not isinstance(row.get("op_id"), str) or not row["op_id"].strip():
        refuse("consolidation_op_id", "an operation ID is non-empty text")
    if not isinstance(row.get("reason"), str) or not row["reason"].strip():
        refuse("consolidation_reason", "every proposed operation states a reason")
    for field in ("target", "survivor", "absorbed", "contradiction"):
        if field in row and (not isinstance(row[field], str) or not row[field].strip()):
            refuse("consolidation_shape", f"{field} is a non-empty reference")
    return dict(row)


def translate(
    driver: Any, world_id: str, record: dict[str, Any], envelope: object
) -> dict[str, Any]:
    """Validate the run echo and emit one guarded, contradiction-first writer batch."""
    if record.get("agent") != "connector" or record.get("outcome") != "running":
        refuse("consolidation_agent", "only a live Connector pass sends a consolidation envelope")
    doc = _object(envelope, {"schema", "run"}, set(SECTIONS), "consolidation envelope")
    if doc["schema"] != SCHEMA:
        refuse("consolidation_schema", "the Connector requires grove.consolidate.v1")
    pending = work(driver, world_id)
    expected = {"run_id": record["id"], "triggers": pending["triggers"]}
    if doc["run"] != expected:
        refuse("consolidation_run", "the run echo differs from the current code-owned packet")
    sections = {name: _rows(doc, name) for name in SECTIONS}
    for name in ("ontology_ops", "walk", "questions", "declines"):
        if sections[name]:
            refuse("consolidation_section", f"{name} is not implemented in this core envelope")
    candidates = []
    for row in sections["operations"]:
        candidates.append(_operation(row, GENERIC))
    for row in sections["belief_ops"]:
        converted = dict(row)
        if not isinstance(row.get("op"), str):
            refuse("consolidation_operation", "a belief operation has an operation code")
        converted["op"] = BELIEF.get(row.get("op"), row.get("op"))
        candidates.append(_operation(converted, set(BELIEF.values())))
    for row in sections["dispositions"]:
        candidates.append(_operation(row, {"verify", "retest_belief"}))
    for row in sections["contradictions"]:
        candidates.append(_operation(row, {"open_contradiction"}))
    support_ops = [
        _operation(row, {"resolve_support_change"}) for row in sections["support_changes"]
    ]
    by_id = {}
    for op in [*candidates, *support_ops]:
        op_id = op.get("op_id")
        if (
            not isinstance(op_id, str)
            or not op_id
            or op_id in by_id
            or op_id.startswith("consolidation:")
        ):
            refuse(
                "consolidation_op_id",
                "operation IDs must be unique and outside the reserved prefix",
            )
        by_id[op_id] = op
    queue = {row["id"]: row for row in pending["contradictions"]}
    resolutions = {}
    for row in sections["resolutions"]:
        op = _operation(row, {"resolve_contradiction"})
        contradiction_id = op["contradiction"]
        if not isinstance(contradiction_id, str) or contradiction_id in resolutions:
            refuse("consolidation_queue", "a contradiction receives exactly one resolution")
        resolutions[contradiction_id] = op
        if op["op_id"] in by_id or op["op_id"].startswith("consolidation:"):
            refuse("consolidation_op_id", "resolution operation IDs must be unique")
        by_id[op["op_id"]] = op
    if set(queue) != set(resolutions):
        refuse("consolidation_queue", "every open contradiction needs a resolution in this batch")
    support_refs = [
        {"target": row["belief_id"], "cause_change_id": row["cause_change_id"]}
        for row in pending["support_changed"]
    ]
    actual_support = [
        {"target": op["target"], "cause_change_id": op["cause_change_id"]} for op in support_ops
    ]
    if any(not isinstance(row["target"], str) for row in actual_support):
        refuse("consolidation_support", "a support disposition names a belief ID")
    if sorted(support_refs, key=lambda r: r["target"]) != sorted(
        actual_support, key=lambda r: r["target"]
    ):
        refuse("consolidation_support", "every pending support change needs a disposition")
    ordered = [
        {
            "op": "guard_consolidation",
            "op_id": "consolidation:guard",
            "run_id": record["id"],
            "contradiction_ids": list(queue),
            "support_change_refs": support_refs,
            "reason": "Validate this pass and its complete work queues before any change.",
        }
    ]
    used = set()
    for contradiction_id, conflict in queue.items():
        resolution = resolutions[contradiction_id]
        if (
            conflict["detected_at"] <= pending["now"] - QUESTION_AGE
            and resolution["outcome"] != "to_question"
        ):
            refuse(
                "consolidation_old_conflict",
                "a contradiction open for seven days must become a question",
            )
        ids = resolution["operation_ids"]
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            refuse("consolidation_resolution", "operation_ids is a list of operation IDs")
        if resolution["outcome"] == "to_question" and ids:
            refuse("consolidation_resolution", "a question leaves the standing claim unchanged")
        for op_id in ids:
            if op_id in used or op_id not in by_id or by_id[op_id] not in candidates:
                refuse(
                    "consolidation_resolution",
                    "a resolution cites an unused operation of this batch",
                )
            ordered.append(by_id[op_id])
            used.add(op_id)
        ordered.append(resolution)
    for support in support_ops:
        references = support.get("operation_ids", [])
        if not isinstance(references, list) or any(
            not isinstance(value, str) for value in references
        ):
            refuse("consolidation_support", "operation_ids is a list of operation IDs")
        for op_id in references:
            if op_id not in by_id or by_id[op_id] not in candidates:
                refuse(
                    "consolidation_support", "a support disposition cites this batch's operations"
                )
            if op_id not in used:
                ordered.append(by_id[op_id])
                used.add(op_id)
        ordered.append(support)
    tested = {op["target"] for op in candidates if op["op"] == "retest_belief"}
    if set(pending["retest_due"]) - tested:
        refuse("consolidation_retest", "every due belief retest needs a recorded disposition")
    reviewed = tested | {op["target"] for op in support_ops}
    reviewed.update(
        op["target"]
        for op in candidates
        if op["op"]
        in {
            "promote_belief",
            "weaken_belief",
            "restore_belief",
            "supersede_belief",
            "retire_belief",
        }
    )
    if set(pending["drift"]) - reviewed:
        refuse("consolidation_drift", "every drifting belief needs a recorded disposition")
    ordered.extend(op for op in candidates if op["op_id"] not in used)
    batch = {"ops": ordered, "evidence": sections["evidence"]}
    faults = check_shape(batch)
    if faults:
        raise BatchDeclined(faults, batch_size=len(ordered))
    return batch
