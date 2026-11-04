"""Strict D11 core translation to the published D4/D3 writer operations.

Unsupported Area work fails explicitly. No model-supplied score or verdict is
translated. The writer re-reads the cited judgments and enforces its own bands.
"""

import copy
import json

from grove.cleanup.access import CleanupRefused
from grove.writer.ops import OPS, shape_refusals

SCHEMA = "grove.cleanup.v1"
MAX_OPERATIONS = 100
MAX_BYTES = 256 * 1024
DISPOSITIONS = frozenset({"confirmed", "moved", "retired", "deferred"})
ALLOWED = frozenset(
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
        "resolve_mark",
        "propose_word",
    }
)


def to_batch(document: dict, *, run_id: str, claims: frozenset[str]) -> dict:
    try:
        encoded = json.dumps(document, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CleanupRefused("cleanup_json_invalid") from None
    if len(encoded) > MAX_BYTES:
        raise CleanupRefused("cleanup_packet_too_large")
    if type(document) is not dict or set(document) != {
        "schema",
        "run",
        "dispositions",
        "operations",
        "evidence",
    }:
        raise CleanupRefused("cleanup_fields_invalid")
    if document["schema"] != SCHEMA or document["run"] != {
        "run_id": run_id,
        "work": "lane_batch",
        "area_id": None,
    }:
        raise CleanupRefused("cleanup_run_invalid_or_area_unavailable")
    dispositions, operations, evidence = (
        document[key] for key in ("dispositions", "operations", "evidence")
    )
    if any(type(value) is not list for value in (dispositions, operations, evidence)):
        raise CleanupRefused("cleanup_lists_invalid")
    if not 1 <= len(dispositions) <= 12 or len(operations) + len(dispositions) > MAX_OPERATIONS:
        raise CleanupRefused("cleanup_batch_bound")
    seen = set()
    for disposition in dispositions:
        if type(disposition) is not dict or disposition.get("op") != "verify":
            raise CleanupRefused("cleanup_disposition_invalid")
        claim = disposition.get("claim")
        if not isinstance(claim, str) or claim not in claims or claim in seen:
            raise CleanupRefused("cleanup_claim_not_offered_or_duplicate")
        seen.add(claim)
        if disposition.get("disposition") not in DISPOSITIONS:
            raise CleanupRefused("cleanup_disposition_invalid")
        if disposition["disposition"] == "deferred" and not disposition.get("recheck_by"):
            raise CleanupRefused("cleanup_deferral_needs_date")
        if disposition["disposition"] in ("confirmed", "moved") and not disposition.get("pieces"):
            raise CleanupRefused("cleanup_evidence_floor")
    for operation in operations:
        name = operation.get("op") if isinstance(operation, dict) else None
        if name not in ALLOWED or "cleanup" not in OPS[name].callers:
            raise CleanupRefused("cleanup_operation_unavailable")
    batch = {"ops": copy.deepcopy(operations + dispositions), "evidence": copy.deepcopy(evidence)}
    faults, by_index = shape_refusals(batch)
    if faults or by_index:
        raise CleanupRefused("cleanup_writer_shape_refused")
    reject_contamination(batch["ops"])
    return batch


def reject_contamination(operations: list[dict]) -> None:
    """Refuse identical generated item prose across three or more targets.

    This bounded detector complements D3 run undo; it does not claim semantic
    detection of paraphrases. It never compares source quotes or reasons.
    """
    uses: dict[str, set[str]] = {}
    for operation in operations:
        fields = operation.get("fields", {}) if operation.get("op") == "update_item" else operation
        target = operation.get("item") or operation.get("temp_id") or operation.get("op_id")
        for key in ("title", "summary", "body"):
            value = fields.get(key) if isinstance(fields, dict) else None
            if isinstance(value, str):
                normal = " ".join(value.casefold().split())
                if len(normal) >= 24:
                    uses.setdefault(normal, set()).add(str(target))
    if any(len(targets) >= 3 for targets in uses.values()):
        raise CleanupRefused("cleanup_repeated_prose")


def escalation(dispositions: tuple[str, ...], *, person_only: bool) -> str | None:
    """Newest first, from the writer's recorded verification rows."""
    count = 0
    for disposition in dispositions:
        if disposition not in DISPOSITIONS:
            raise CleanupRefused("cleanup_history_invalid")
        if disposition != "deferred":
            break
        count += 1
    if count < 3:
        return None
    return "question" if person_only else "retirement_proposal"
