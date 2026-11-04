"""Read the settled world for a consolidation pass, with all prose inside data blocks.

The successful pass is the cursor. A failed or killed pass never hides unprocessed changes.
Histories are complete for each selected item: an oversized packet refuses, never truncates.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from grove import clock
from grove.prompts import DataBlock, render_blocks
from grove.store.primitives import MEANING_DESCRIBES
from grove.store.query import WorldQuery

CHANGED_ITEMS = 10
QUESTION_AGE = timedelta(days=7)
MAX_PACKET_BYTES = 2 * 1024 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PacketTooLarge(ValueError):
    """The complete packet does not fit; no partial history is supplied."""


def successful_cursor(query: WorldQuery) -> datetime:
    rows = query.read(
        "MATCH (r:RunRecord {agent: 'connector', outcome: 'ok'}) "
        "RETURN r.started AS started ORDER BY r.started DESC LIMIT 1"
    )
    return rows[0]["started"] if rows else EPOCH


def work(driver: Any, world_id: str) -> dict[str, Any]:
    """Code computes the P0 triggers without model text or mutations."""
    query = WorldQuery(driver, world_id)
    since, now = successful_cursor(query), clock.now(world_id)
    changed = query.read(
        "MATCH (c:ChangeEntry) WHERE c.recorded_at > $since "
        "AND c.entity_type IN ['item', 'link', 'belief'] "
        "AND (c.actor IS NULL OR c.actor <> 'agent:connector') "
        "RETURN c.entity_type AS type, c.entity_id AS id, max(c.sequence) AS sequence "
        "ORDER BY type, id",
        since=since,
    )
    contradictions = [
        row["c"]
        for row in query.read(
            "MATCH (c:Contradiction {status: 'open'}) RETURN c ORDER BY c.detected_at, c.id"
        )
    ]
    support = [
        row["s"]
        for row in query.read(
            "MATCH (s:SupportChanged) WHERE s.taken_at IS NULL "
            "RETURN s ORDER BY s.queued_at, s.belief_id"
        )
    ]
    beliefs = [
        row
        for row in query.read(
            "MATCH (b:Belief) MATCH (i:Item {id: b.item_id}) "
            "WHERE b.state <> 'superseded' RETURN b, i ORDER BY b.item_id"
        )
    ]
    anchors = query.read(
        "MATCH (b:Item)-[l:LINK]->(s:Item) "
        "WHERE l.meaning = $meaning AND l.active_key IS NOT NULL "
        "RETURN b.id AS belief, s.id AS subject ORDER BY b.id, s.id",
        meaning=MEANING_DESCRIBES,
    )
    subjects: dict[str, list[str]] = {}
    for row in anchors:
        subjects.setdefault(row["belief"], []).append(row["subject"])
    retest, drift = [], []
    for row in beliefs:
        belief, item = row["b"], row["i"]
        belief_id = belief["item_id"]
        if belief.get("externally_testable") and item.get("recheck_by") is not None:
            if item["recheck_by"] <= now:
                retest.append(belief_id)
        # A subject's new episode which the belief has not counted is drift. The successful
        # cursor suppresses an already-reviewed episode, but never a failed pass's work.
        after = max(belief.get("last_support_at") or EPOCH, since)
        subject_ids = subjects.get(belief_id, [])
        if subject_ids:
            rows = query.read(
                "MATCH (e:Evidence) WHERE e.target_id IN $subjects AND e.active = true "
                "AND e.source_time > $after RETURN e.episode_id AS episode",
                subjects=subject_ids,
                after=after,
            )
            counted = set(belief.get("supporting_episodes") or []) | set(
                belief.get("contradicting_episodes") or []
            )
            if any(row["episode"] not in counted for row in rows):
                drift.append(belief_id)
    changed_items = [row["id"] for row in changed if row["type"] == "item"]
    triggers = []
    if contradictions:
        triggers.append({"kind": "contradiction", "refs": [row["id"] for row in contradictions]})
    if len(changed_items) >= CHANGED_ITEMS:
        triggers.append({"kind": "revisions", "refs": changed_items})
    drift_refs = sorted(set(drift) | {row["belief_id"] for row in support})
    if drift_refs:
        triggers.append({"kind": "drift", "refs": drift_refs})
    if retest:
        triggers.append({"kind": "retest_due", "refs": retest})
    return {
        "since": since,
        "now": now,
        "changed": changed,
        "contradictions": contradictions,
        "support_changed": support,
        "beliefs": beliefs,
        "subjects": subjects,
        "retest_due": retest,
        "drift": drift,
        "triggers": triggers,
    }


def job_trigger(pending: dict[str, Any]) -> str:
    """A failed job is not recreated until its input changes. Time passing alone is no retry."""
    value = {
        "changed": pending["changed"],
        "contradictions": [
            {key: row.get(key) for key in ("id", "version", "detected_at")}
            for row in pending["contradictions"]
        ],
        "support": [
            {key: row.get(key) for key in ("belief_id", "cause_change_id")}
            for row in pending["support_changed"]
        ],
        "beliefs": [
            {
                "id": row["b"]["item_id"],
                "version": row["i"].get("version"),
                "recheck_by": row["i"].get("recheck_by"),
            }
            for row in pending["beliefs"]
            if row["b"]["item_id"] in pending["drift"] + pending["retest_due"]
        ],
    }
    return "consolidation:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:32]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=lambda v: v.isoformat())


def packet(driver: Any, world_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """The contradiction queue comes first, followed by support, drift, and retests."""
    from grove.consolidation.schema import empty_envelope, operation_catalogue

    query, pending = WorldQuery(driver, world_id), work(driver, world_id)
    blocks: list[DataBlock] = []

    def block(kind: str, ref: str | None, value: object, speaker: str = "automated") -> int:
        blocks.append(DataBlock(kind, speaker, None, ref, _json(value)))
        return len(blocks)

    contradictions = []
    for row in pending["contradictions"]:
        contradictions.append(
            {
                "contradiction_id": row["id"],
                "target": row.get("target"),
                "standing_target_id": row.get("standing_target_id"),
                "standing_change_id": row.get("standing_change_id"),
                "fresh_evidence_id": row.get("fresh_evidence_id"),
                "fresh_source_id": row.get("fresh_source_id"),
                "detected_at": row["detected_at"].isoformat(),
                "must_ask": row["detected_at"] <= pending["now"] - QUESTION_AGE,
                "block": block("question", row["id"], row),
            }
        )
    beliefs = []
    selected = {row["id"] for row in pending["changed"]}
    for row in pending["contradictions"]:
        selected.update(
            value for value in (row.get("target"), row.get("standing_target_id")) if value
        )
    for row in pending["beliefs"]:
        belief, item = row["b"], row["i"]
        item_id = belief["item_id"]
        selected.add(item_id)
        selected.update(pending["subjects"].get(item_id, []))
        beliefs.append(
            {
                "belief_id": item_id,
                "version": item.get("version"),
                "state": belief["state"],
                "subject_refs": pending["subjects"].get(item_id, []),
                "retest_due": item_id in pending["retest_due"],
                "drift": item_id in pending["drift"],
                "block": block("belief", item_id, {"belief": belief, "item": item}),
            }
        )
    # Include both ends and the full link history for each changed link.
    links = query.read(
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.id IN $ids "
        "RETURN a.id AS from_id, b.id AS to_id, l",
        ids=[row["id"] for row in pending["changed"] if row["type"] == "link"],
    )
    for row in links:
        selected.update((row["from_id"], row["to_id"]))
        block("item", row["l"]["id"], row["l"])
    # The safety-net packet includes incoming dependents even when a meaning's consequence
    # flag is wrong. A recorded re-judgment can report the gap instead of trusting that flag.
    dependents = []
    for row in query.read(
        "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE b.id IN $ids "
        "AND l.active_key IS NOT NULL RETURN a.id AS dependent, b.id AS changed, l",
        ids=[row["id"] for row in pending["changed"] if row["type"] == "item"],
    ):
        selected.update((row["dependent"], row["l"]["id"]))
        dependents.append(
            {
                "dependent": row["dependent"],
                "changed": row["changed"],
                "link_id": row["l"]["id"],
                "block": block("item", row["l"]["id"], row["l"]),
            }
        )
    histories = []
    for item_id in sorted(selected):
        rows = query.read("MATCH (i:Item {id: $id}) RETURN i", id=item_id)
        if rows:
            block("item", item_id, rows[0]["i"])
        changes = query.read(
            "MATCH (c:ChangeEntry {entity_id: $id}) RETURN c ORDER BY c.sequence", id=item_id
        )
        values = sorted(
            {
                value
                for row in changes
                for key, value in row["c"].items()
                if key in ("old_value_id", "new_value_id", "reason") and value
            }
        )
        texts = query.read("MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v", ids=values)
        histories.append(
            {
                "item_id": item_id,
                "entries": len(changes),
                "block": block("item", item_id, {"history": changes, "values": texts}),
            }
        )
    evidence = query.read(
        "MATCH (e:Evidence) WHERE e.target_id IN $ids OR e.id IN $fresh "
        "RETURN e ORDER BY e.source_time, e.id",
        ids=sorted(selected),
        fresh=[
            row["fresh_evidence_id"]
            for row in pending["contradictions"]
            if row.get("fresh_evidence_id")
        ],
    )
    evidence_rows = []
    sources = set()
    for entry in evidence:
        row = entry["e"]
        if row.get("source_id"):
            sources.add(row["source_id"])
        evidence_rows.append(
            {
                "evidence_id": row["id"],
                "target_id": row.get("target_id"),
                "episode_id": row.get("episode_id"),
                "source_id": row.get("source_id"),
                "block": block("source", row["id"], row),
            }
        )
    sources.update(
        row["fresh_source_id"] for row in pending["contradictions"] if row.get("fresh_source_id")
    )
    for row in query.read(
        "MATCH (s:Source) WHERE s.id IN $ids RETURN s ORDER BY s.id", ids=sorted(sources)
    ):
        source = row["s"]
        block("source", source["id"], source)
    run = {"run_id": record["id"], "triggers": pending["triggers"]}
    result = {
        "schema": "grove.consolidate.v1",
        "run": run,
        "contradictions": contradictions,
        "support_changed": [
            {"target": row["belief_id"], "cause_change_id": row["cause_change_id"]}
            for row in pending["support_changed"]
        ],
        "changed": pending["changed"],
        "dependents": dependents,
        "beliefs": beliefs,
        "histories": histories,
        "evidence": evidence_rows,
        "operation_catalogue": operation_catalogue(),
        "batch_form": {"batch": empty_envelope(run)},
        "data": render_blocks(blocks),
    }
    if len(_json(result).encode("utf-8")) > MAX_PACKET_BYTES:
        raise PacketTooLarge("the complete consolidation packet exceeds its byte limit")
    return result
