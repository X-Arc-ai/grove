"""Ordered extraction progress and durable intents; no outbox drainer exists."""

from datetime import timedelta

from grove import clock
from grove.extraction import durable_codec as codec
from grove.extraction.durable_types import ConsumerReceipt, HandoffRef
from grove.ids import new_id
from grove.writer import learner_requests as boundary
from grove.writer.learner_values import read_private_value_in_tx, write_private_value_in_tx


def _complete(row):
    return (
        row.get("completed_generation") == row["required_generation"]
        and row.get("completion_id") is not None
    )


def _ordered(tx, anchor, profile):
    rows = boundary._rows(
        tx, "LearnerEpisodeProgress", stream_anchor_source_id=anchor, profile=profile
    )
    if len(rows) > 256:
        codec.refuse("order_window_unavailable")
    rows.sort(key=lambda r: r["position"])
    for position, row in enumerate(rows):
        if row["position"] != position or row.get("predecessor_progress_id") != (
            rows[position - 1]["id"] if position else None
        ):
            codec.refuse("order_window_unavailable")
        if row["connector_id"] != rows[0]["connector_id"]:
            codec.refuse("lineage_unavailable")
    return rows


def register_progress_in_tx(tx, stamp, work, order):
    codec._int(order.position, 0, 255)
    manifest = codec.load_json(
        read_private_value_in_tx(tx, order.complete_manifest_value_id, "handoff")
    )
    codec._closed(manifest, ("schema", "stream_anchor_source_id", "episode_ids"))
    episodes = manifest["episode_ids"]
    if (
        manifest["schema"] != "grove.learner.order.v1"
        or manifest["stream_anchor_source_id"] != order.stream_anchor_source_id
        or type(episodes) is not list
        or not 1 <= len(episodes) <= 256
        or len(set(episodes)) != len(episodes)
        or order.position >= len(episodes)
        or episodes[order.position] != work["episode_id"]
    ):
        codec.refuse("order_window_unavailable")
    anchor = boundary._one(tx, "Source", order.stream_anchor_source_id)
    source = boundary._one(tx, "Source", work["source_id"])
    if anchor["connector_id"] != work["connector_id"] or anchor.get(
        "external_locator"
    ) != source.get("external_locator"):
        codec.refuse("lineage_unavailable")
    rows = _ordered(tx, order.stream_anchor_source_id, work["profile"])
    if any(
        row["episode_id"] != episodes[row["position"]]
        for row in rows
        if row["position"] < len(episodes)
    ) or len(rows) > len(episodes):
        codec.refuse("order_window_unavailable")
    if order.position:
        if len(rows) < order.position:
            codec.refuse("order_window_unavailable")
        previous = rows[order.position - 1]
        if (
            previous["id"] != order.predecessor_progress_id
            or previous["version"] != order.expected_predecessor_version
        ):
            codec.refuse("stale_claim")
    elif (
        order.predecessor_progress_id is not None or order.expected_predecessor_version is not None
    ):
        codec.refuse("order_window_unavailable")
    old = rows[order.position] if len(rows) > order.position else None
    if old is not None and old["required_work_id"] == work["id"]:
        return old
    if old is not None and not _complete(old):
        codec.refuse("order_window_unavailable")
    episode = boundary._one(tx, "Episode", work["episode_id"])
    row = {
        "id": new_id(tx.world_id) if old is None else old["id"],
        "world_id": tx.world_id,
        "version": 1 if old is None else codec._int(old["version"] + 1, 1),
        "touched_at": stamp.at,
        "episode_id": work["episode_id"],
        "connector_id": work["connector_id"],
        "stream_anchor_source_id": order.stream_anchor_source_id,
        "profile": work["profile"],
        "position": order.position,
        "predecessor_progress_id": order.predecessor_progress_id,
        "required_generation": work["generation"],
        "required_work_id": work["id"],
        "historical_contribution": "none" if old is None else old["historical_contribution"],
        "source_start": episode.get("start_time"),
        "created_at": stamp.at if old is None else old["created_at"],
    }
    return boundary._put(tx, "LearnerEpisodeProgress", row, old)


def check_predecessor_in_tx(tx, work):
    found = boundary._rows(tx, "LearnerEpisodeProgress", required_work_id=work["id"])
    if len(found) != 1:
        codec.refuse("order_window_unavailable")
    row = found[0]
    if row["required_generation"] != work["generation"] or row["episode_id"] != work["episode_id"]:
        codec.refuse("order_window_unavailable")
    ordered = _ordered(tx, row["stream_anchor_source_id"], row["profile"])
    if any(not _complete(previous) for previous in ordered[: row["position"]]):
        codec.refuse("order_window_unavailable")
    return row


def complete_progress_in_tx(tx, stamp, work, completion):
    row = check_predecessor_in_tx(tx, work)
    if _complete(row):
        if row["completion_id"] != completion["id"]:
            codec.refuse("snapshot_inconsistent")
        return row
    outcome, previous = completion["outcome"], row["historical_contribution"]
    if outcome not in {"done_empty", "done_extracted"} or previous not in {
        "none",
        "done_empty",
        "done_extracted",
    }:
        codec.refuse("snapshot_inconsistent")
    empty = int(previous == "none" and outcome == "done_empty") - int(
        previous == "done_empty" and outcome == "done_extracted"
    )
    extracted = int(previous != "done_extracted" and outcome == "done_extracted")
    connector = boundary._one(tx, "Connector", work["connector_id"])
    counts = {
        "held_nothing_count": codec._int(connector.get("held_nothing_count", 0) + empty),
        "extracted_count": codec._int(connector.get("extracted_count", 0) + extracted),
    }
    boundary._put(
        tx,
        "Connector",
        {
            **connector,
            **counts,
            "version": codec._int(connector["version"] + 1, 1),
            "touched_at": stamp.at,
        },
        connector,
    )
    contribution = "done_extracted" if previous == "done_extracted" else outcome
    return boundary._put(
        tx,
        "LearnerEpisodeProgress",
        {
            **row,
            "completed_generation": work["generation"],
            "completion_id": completion["id"],
            "historical_contribution": contribution,
            "version": codec._int(row["version"] + 1, 1),
            "touched_at": stamp.at,
        },
        row,
    )


def read_extraction_progress_in_tx(tx, connector_id):
    rows = boundary._rows(tx, "LearnerEpisodeProgress", connector_id=connector_id)
    episodes = boundary._rows(tx, "Episode", connector_id=connector_id)
    if len(rows) > 256 or {row["episode_id"] for row in rows} != {row["id"] for row in episodes}:
        codec.refuse("order_window_unavailable")
    cursors = []
    for anchor, profile in sorted({(r["stream_anchor_source_id"], r["profile"]) for r in rows}):
        ordered = _ordered(tx, anchor, profile)
        cursor = 0
        for row in ordered:
            if not _complete(row):
                break
            cursor += 1
        cursors.append((anchor, cursor))
    pending = [r for r in rows if not _complete(r)]
    known = [r["source_start"] for r in pending if r.get("source_start") is not None]
    since = clock.now(tx.world_id) - timedelta(days=7)
    work_ids = {w["id"] for w in boundary._rows(tx, "LearnerWork", connector_id=connector_id)}
    completions = []
    for work_id in work_ids:
        completions.extend(boundary._rows(tx, "LearnerCompletion", work_id=work_id))
    return {
        "cursors": tuple(cursors),
        "pending": len(pending),
        "unknown_pending": sum(r.get("source_start") is None for r in pending),
        "oldest_unprocessed_item": min(known) if known else None,
        "created_items_7d": sum(
            r["created_item_count"] for r in completions if r["completed_at"] >= since
        ),
    }


def enqueue_handoffs_in_tx(tx, stamp, work, completion, target_ids, contradiction_ids=()):
    targets, contradictions = tuple(sorted(set(target_ids))), tuple(sorted(set(contradiction_ids)))
    if len(targets) > 256 or len(contradictions) > 256:
        codec.refuse("order_window_unavailable")
    if not targets:
        return ()
    result = []
    for kind in ("curator_dirty", "connector_dirty"):
        payload = {
            "schema": "grove.learner.dirty.v1",
            "kind": kind,
            "work_id": work["id"],
            "execution_id": completion["execution_id"],
            "completion_id": completion["id"],
            "commit_id": stamp.commit_id,
            "change_set_id": stamp.change_set_id,
            "target_ids": list(targets),
            "contradiction_ids": list(contradictions),
        }
        prior = boundary._rows(
            tx, "LearnerHandoff", execution_id=completion["execution_id"], kind=kind
        )
        if prior:
            if (
                len(prior) != 1
                or codec.load_json(
                    read_private_value_in_tx(tx, prior[0]["payload_value_id"], "handoff")
                )
                != payload
            ):
                codec.refuse("snapshot_inconsistent")
            result.append(prior[0]["id"])
            continue
        from grove.writer.learner_execution import _reserve_values

        encoded = codec.canonical(payload, codec.LIMITS["handoff"])
        _reserve_values(tx, work["id"], [("handoff", encoded)])
        value_id = write_private_value_in_tx(tx, stamp, "handoff", encoded)
        row = boundary._newrow(
            tx,
            work_id=work["id"],
            execution_id=completion["execution_id"],
            completion_id=completion["id"],
            kind=kind,
            contract_version=1,
            payload_value_id=value_id,
            target_ids=list(targets),
            state="pending",
            created_at=stamp.at,
        )
        boundary._put(tx, "LearnerHandoff", row)
        result.append(row["id"])
    return tuple(result)


def acknowledge_handoff_in_tx(tx, session, handoff_id, expected_version, receipt):
    tx.lock_world()
    row = boundary._one(tx, "LearnerHandoff", handoff_id)
    boundary._require_consumer_in_tx(tx, session, row, receipt)
    if (
        type(receipt) is not ConsumerReceipt
        or receipt.handoff_id != handoff_id
        or receipt.kind != row["kind"]
        or receipt.contract_version != 1
        or receipt.destination_ids != tuple(row.get("target_ids", ()))
    ):
        codec.refuse("snapshot_inconsistent")
    codec._id(receipt.accepted_transaction_id)
    if row["state"] == "acknowledged":
        if row["consumer_receipt_id"] != receipt.accepted_transaction_id:
            codec.refuse("snapshot_inconsistent")
        return HandoffRef(row["id"], row["version"], row["state"], row["consumer_receipt_id"])
    if (
        row["state"] != "pending"
        or row["version"] != expected_version
        or row["kind"] == "curator_question_v1"
    ):
        codec.refuse("stale_claim")
    at = clock.now(tx.world_id)
    row = boundary._put(
        tx,
        "LearnerHandoff",
        {
            **row,
            "version": codec._int(row["version"] + 1, 1),
            "state": "acknowledged",
            "consumer_receipt_id": receipt.accepted_transaction_id,
            "acknowledged_at": at,
            "touched_at": at,
        },
        row,
    )
    return HandoffRef(row["id"], row["version"], row["state"], row["consumer_receipt_id"])
