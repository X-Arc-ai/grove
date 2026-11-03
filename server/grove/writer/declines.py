"""The decline log (D2): one :Decline per refused operation, ids and codes only.

A refusal's message and details can repeat a caller's phrase, and a forget cannot reach a row that
holds it (D14), so neither is stored. An op id comes from a caller, so one that could hold text is
stored by its hash. The near-duplicate candidates are ids and scores: a replayed decline reads them
back, so a repair has its data.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from neo4j import Driver

from grove.store.query import WorldQuery
from grove.store.scope import check_world_id
from grove.writer.errors import BatchDeclined, Refusal
from grove.writer.ops import OPS
from grove.writer.tx import WorldTx

BATCH_OP_ID = "batch"
UNKNOWN_OP = "unknown"
_SAFE_OP_ID = re.compile(r"[A-Za-z0-9_.:$-]{1,80}")
_OPAQUE_PREFIX = "opaque:"
_OPAQUE_LENGTH = 16
NEAR_DUPLICATE = "near_duplicate"

_MERGE_ROWS = (
    "UNWIND $rows AS row MERGE (d:Decline {request_id: $request_id, op_id: row.op_id}) SET d += row"
)


def stored_op_id(op_id: str | None) -> str:
    """The form of an op id in a row: never free text, and never the sentinel of a batch fault."""
    if op_id is None:
        return BATCH_OP_ID
    if op_id != BATCH_OP_ID and _SAFE_OP_ID.fullmatch(op_id):
        return op_id
    digest = hashlib.sha256(op_id.encode("utf-8", "surrogatepass")).hexdigest()
    return _OPAQUE_PREFIX + digest[:_OPAQUE_LENGTH]


def _stored_op(op: str | None) -> str:
    return op if op in OPS else UNKNOWN_OP


def _candidates(refusal: Refusal) -> list[dict[str, Any]]:
    found = refusal.details.get("candidates")
    if not isinstance(found, list):
        return []
    return [
        {"item_id": entry["item_id"], "score": entry["score"]}
        for entry in found
        if isinstance(entry, dict) and "item_id" in entry and "score" in entry
    ]


def stored_decline(declined: BatchDeclined) -> dict[str, Any]:
    """What a declined request keeps in its receipt column."""
    return {
        "state": declined.receipt.state,
        "batch_size": declined.receipt.batch_size,
        "request_id": declined.receipt.request_id,
        "refusals": [
            {
                "op_id": stored_op_id(refusal.op_id),
                "op": _stored_op(refusal.op),
                "code": refusal.code,
                "rule": refusal.rule,
            }
            for refusal in declined.refusals
        ],
    }


def _encoded(candidates: list[dict[str, Any]]) -> str | None:
    return json.dumps(candidates, sort_keys=True) if candidates else None


def write_decline_rows(tx: WorldTx, request_id: str, refusals: list[Refusal], at: datetime) -> None:
    """One row per refused operation. A later decline of the same operation overwrites its row."""
    rows = [
        {
            "op_id": stored_op_id(refusal.op_id),
            "rule": refusal.rule,
            "code": refusal.code,
            "nearduplicate_candidate_ids": _encoded(_candidates(refusal)),
            "override_used": False,
            "recorded_at": at,
        }
        for refusal in refusals
    ]
    tx.run(_MERGE_ROWS, rows=rows, request_id=request_id)


def write_override_row(
    tx: WorldTx, request_id: str, op_id: str, candidates: list[dict[str, Any]], at: datetime
) -> None:
    """A near-duplicate that passed on a named distinct_from stays in the log (spec 764, 774)."""
    row = {
        "op_id": stored_op_id(op_id),
        "rule": NEAR_DUPLICATE,
        "code": NEAR_DUPLICATE,
        "nearduplicate_candidate_ids": _encoded(candidates),
        "override_used": True,
        "recorded_at": at,
    }
    tx.run(_MERGE_ROWS, rows=[row], request_id=request_id)


def candidates_by_op(query: WorldQuery, request_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = query.read(
        "MATCH (d:Decline {request_id: $id}) WHERE coalesce(d.override_used, false) = false "
        "RETURN d.op_id AS op_id, d.nearduplicate_candidate_ids AS candidates",
        id=request_id,
    )
    return {row["op_id"]: json.loads(row["candidates"] or "[]") for row in rows}


def declines_of(stored: dict[str, Any], candidates: dict[str, list]) -> list[dict[str, Any]]:
    """The refusals of a stored decline with their candidates: ids and codes for an API answer."""
    return [
        {**refusal, "candidates": candidates.get(refusal["op_id"], [])}
        for refusal in stored["refusals"]
    ]


def declines_from(declined: BatchDeclined) -> list[dict[str, Any]]:
    """The same answer from the exception itself, for a decline whose log may not have landed."""
    candidates = {
        stored_op_id(refusal.op_id): _candidates(refusal) for refusal in declined.refusals
    }
    return declines_of(stored_decline(declined), candidates)


def rebuilt_decline(stored: dict[str, Any], candidates: dict[str, list]) -> BatchDeclined:
    """The BatchDeclined of a replay, with no write. The message says only what the codes say."""
    refusals = []
    for refusal in declines_of(stored, candidates):
        op_id = None if refusal["op_id"] == BATCH_OP_ID else refusal["op_id"]
        op = None if refusal["op"] == UNKNOWN_OP else refusal["op"]
        details = {"candidates": refusal["candidates"]} if refusal["candidates"] else {}
        message = f"this batch was declined before: {refusal['code']} ({refusal['rule']})"
        refusals.append(Refusal(op_id, op, refusal["code"], refusal["rule"], message, details))
    return BatchDeclined(
        refusals, batch_size=stored["batch_size"], request_id=stored.get("request_id")
    )


def decline_log(
    driver: Driver,
    world_id: str,
    *,
    rule: str | None = None,
    request_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """The log for the clean-up agent, newest first. Override rows are in it, with their flag."""
    check_world_id(world_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit is a positive whole number")
    rows = WorldQuery(driver, world_id).read(
        "MATCH (d:Decline) WHERE ($rule IS NULL OR d.rule = $rule) "
        "AND ($request_id IS NULL OR d.request_id = $request_id) "
        "RETURN d ORDER BY d.recorded_at DESC, d.request_id, d.op_id LIMIT $limit",
        rule=rule,
        request_id=request_id,
        limit=limit,
    )
    return [
        {
            "request_id": row["d"]["request_id"],
            "op_id": row["d"]["op_id"],
            "rule": row["d"]["rule"],
            "code": row["d"].get("code"),
            "candidates": json.loads(row["d"].get("nearduplicate_candidate_ids") or "[]"),
            "override_used": bool(row["d"].get("override_used")),
            "recorded_at": row["d"].get("recorded_at"),
        }
        for row in rows
    ]
