"""Inactive protected Learner boundary. No production authority provider exists.

The owner checks deliberately refuse. Synthetic tests replace these private checks;
no environment setting, supplied object, or stored reference can install a provider.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from grove import clock
from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.durable_types import ExpectedHead, RunWitness
from grove.extraction.execution_profile import check_dependencies, check_legacy_profile
from grove.extraction.execution_types import CompletionPlan
from grove.extraction.runtime import require_runtime
from grove.ids import new_id
from grove.writer.history import Stamp, compact
from grove.writer.learner_values import collect_values_in_tx

PROTOCOL = "learner_execution_v1"
_LABELS = frozenset(
    {
        "LearnerWork",
        "LearnerExecution",
        "LearnerCall",
        "LearnerDisposition",
        "LearnerCompletion",
        "LearnerEpisodeProgress",
        "LearnerHandoff",
        "Request",
        "RunRecord",
        "Source",
        "Episode",
        "Connector",
        "ScreeningReceipt",
        "HistoryValue",
        "ChangeEntry",
        "Evidence",
        "Item",
        "Link",
        "ModelCall",
        "World",
    }
)


def refuse_runtime():
    require_runtime()


def _require_authority_in_tx(tx, session, purpose, binding, run):
    """Reserved for the real D14 owner; protocol-shaped objects cannot pass."""
    require_runtime()


def _require_attempt_in_tx(tx, session, attempt, request, result):
    """Reserved for reciprocal Models metering and unchanged screening evidence."""
    require_runtime()


def _require_consumer_in_tx(tx, session, row, receipt):
    require_runtime()


def _rows(tx, label, **where):
    if label not in _LABELS or any(not key.isidentifier() for key in where):
        raise ValueError("invalid internal Learner query")
    predicate = ", ".join(f"{key}: ${key}" for key in where)
    pattern = (
        f"()-[n:LINK {{{predicate}}}]->()" if label == "Link" else f"(n:{label} {{{predicate}}})"
    )
    rows = tx.run(f"MATCH {pattern} RETURN n LIMIT 2049", **where)
    if len(rows) > 2048:
        codec.refuse("order_window_unavailable")
    return [row["n"] for row in rows]


def _one(tx, label, row_id, *, optional=False):
    codec._id(row_id)
    rows = _rows(tx, label, id=row_id)
    if optional and not rows:
        return None
    if len(rows) != 1:
        codec.refuse("erased_dependency")
    row = rows[0]
    if row.get("world_id", tx.world_id) != tx.world_id:
        codec.refuse("lineage_unavailable")
    return row


def _put(tx, label, row, old=None):
    if label not in _LABELS:
        raise ValueError("invalid internal Learner label")
    row = compact({**row, "world_id": tx.world_id})
    if old is None:
        tx.run(f"CREATE (n:{label}) SET n += $row", row=row)
    else:
        tx.run(
            f"MATCH (n:{label} {{id: $id}}) SET n += $row",
            id=row["id"],
            row={**{key: None for key in old if key not in row}, **row},
        )
    return row


def _stamp(tx, event_id, run_id, request_id):
    return Stamp(
        tx.world_id,
        clock.now(tx.world_id),
        "learner",
        event_id,
        event_id,
        request_id,
        run_id,
        "update",
    )


def _newrow(tx, **columns):
    return {
        "id": new_id(tx.world_id),
        "version": 1,
        "touched_at": clock.now(tx.world_id),
        **columns,
    }


def _epoch(value):
    return int(value.timestamp())


def _instant(epoch):
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC)


def _same_instant(left, right):
    from grove.writer.clocks import parse_instant

    return parse_instant(left) == parse_instant(right)


def _check_source_in_tx(tx, binding):
    check_legacy_profile(binding)
    source = _one(tx, "Source", binding.source.id)
    connector = _one(tx, "Connector", binding.producer.id)
    episode = _one(tx, "Episode", binding.episode.id)
    s, p, e = binding.source, binding.producer, binding.episode
    if any(
        source.get(k) != value
        for k, value in {
            "version": s.version,
            "connector_id": s.connector_id,
            "content": s.content,
            "speakers": s.speakers,
            "parser_version": p.parser,
            "forget_state": s.forget_state,
        }.items()
    ):
        codec.refuse("lineage_unavailable")
    if any(
        connector.get(k) != value
        for k, value in {
            "type": p.connector_type,
            "provider": p.provider,
            "batch_rule": p.batch_rule,
        }.items()
    ):
        codec.refuse("lineage_unavailable")
    # Connector counters can advance independently of its immutable producer identity.
    if connector.get("version", 0) < p.version:
        codec.refuse("lineage_unavailable")
    if not _same_instant(source.get("received_at"), s.received_at):
        codec.refuse("lineage_unavailable")
    if (
        any(
            episode.get(k) != value
            for k, value in {
                "version": e.version,
                "connector_id": e.connector_id,
                "first_message_key": e.first_key,
                "last_message_key": e.last_key,
                "cut_rule": e.cut_rule,
            }.items()
        )
        or tuple(episode.get("source_ids", ())) != e.source_ids
    ):
        codec.refuse("lineage_unavailable")
    if not _same_instant(episode.get("start_time"), e.start) or not _same_instant(
        episode.get("end_time"), e.end
    ):
        codec.refuse("lineage_unavailable")
    actual_episodes = tx.run(
        "MATCH (n:Episode) WHERE $source_id IN n.source_ids RETURN n LIMIT 3", source_id=s.id
    )
    if tuple(sorted(row["n"]["id"] for row in actual_episodes)) != tuple(sorted(s.episode_ids)):
        codec.refuse("lineage_unavailable")
    _one(tx, "ScreeningReceipt", s.screening_id)


def check_run_witness_in_tx(tx, witness, binding, *, ended=False):
    witness = codec.decode_run_witness(witness)
    row = _one(tx, "RunRecord", witness.actual_run_id)
    if (
        row.get("version") != witness.actual_row_version
        or row.get("agent") != "learner"
        or tuple(row.get("input_ids", ())) != witness.input_ids
        or binding.work.request_id not in witness.input_ids
    ):
        codec.refuse("stale_claim")
    now = _epoch(clock.now(tx.world_id))
    cap = codec._int(row.get("token_cap"), 1)
    time_cap = codec._int(row.get("time_cap"), 1)
    deadline = _epoch(row["started"] + timedelta(seconds=time_cap))
    if ended:
        if row.get("outcome") == "running":
            codec.refuse("worker_cleanup_required")
    elif row.get("outcome") != "running" or now >= min(
        deadline, binding.run.deadline, binding.work.lease_deadline
    ):
        codec.refuse("stale_claim")
    # A replacement worker cannot renew the original deadline or token allowance.
    return replace(
        binding.run,
        id=witness.actual_run_id,
        version=witness.protocol_run_version,
        pass_id=witness.pass_id,
        input_ids=witness.input_ids,
        outcome=row["outcome"],
        deadline=min(deadline, binding.run.deadline),
        token_cap=min(cap, binding.run.token_cap),
    )


def _load_work(tx, work_id):
    work = _one(tx, "LearnerWork", work_id)
    request = _one(tx, "Request", work["request_id"])
    row = _one(tx, "LearnerExecution", work["latest_execution_id"])
    if (
        request.get("protocol") != PROTOCOL
        or request.get("learner_work_id") != work_id
        or row["work_id"] != work_id
        or row["request_id"] != request["id"]
    ):
        codec.refuse("protected_request")
    roots = {row["state_value_id"]: "state", row["event_value_id"]: "event"}
    completion = None
    if work.get("completion_id"):
        completion = _one(tx, "LearnerCompletion", work["completion_id"])
        roots[completion["detail_value_id"]] = "completion"
    values = collect_values_in_tx(tx, roots)
    completions = {}
    if completion is not None:
        detail = codec.load_json(values[completion["detail_value_id"]])
        completions[completion["id"]] = legacy._decode(detail["plan"], CompletionPlan)
    state = codec.rehydrate_state(values[row["state_value_id"]], values, completions=completions)
    witness = codec.decode_run_witness(
        codec.load_json(values[row["state_value_id"]])["run_witness"]
    )
    if (
        work["source_id"],
        work["episode_id"],
        work["connector_id"],
        request.get("source_id"),
        request.get("content_key"),
        request.get("caller"),
    ) != (
        state.binding.source.id,
        state.binding.episode.id,
        state.binding.producer.id,
        state.binding.source.id,
        state.binding.work.content_key,
        "learner",
    ):
        codec.refuse("protected_request")
    if (
        work["version"],
        request["version"],
        work["state"],
        request["status"],
        row["ordinal"],
        row["claim_epoch"],
        request.get("repairs", 0),
    ) != (
        state.work_version,
        state.request_version,
        state.status,
        state.request_status,
        state.revision,
        state.claim_epoch,
        state.writer_repairs,
    ):
        codec.refuse("snapshot_inconsistent")
    return work, request, row, state, witness, values


def check_current_claim_in_tx(
    tx, session, head, run, *, purpose="transition", ended=False, replay=False
):
    if type(head) is not ExpectedHead or type(run) is not RunWitness:
        codec.refuse("stale_claim")
    tx.lock_world()
    work, request, row, state, witness, values = _load_work(tx, head.work_id)
    if (
        head.work_version,
        head.execution_id,
        head.execution_ordinal,
        head.request_id,
        head.request_version,
        head.claim_epoch,
    ) != (
        work["version"],
        row["id"],
        row["ordinal"],
        request["id"],
        request["version"],
        state.claim_epoch,
    ):
        codec.refuse("stale_claim")
    current = _require_authority_in_tx(tx, session, purpose, state.binding, run)
    if purpose not in {"stale", "rebind"}:
        check_dependencies(state.binding, current, replay=replay)
    else:
        check_legacy_profile(current)
    _check_source_in_tx(tx, current)
    if ended:
        if run.actual_run_id != state.claimed_by:
            codec.refuse("stale_claim")
    elif state.claimed_by is not None and run != witness:
        codec.refuse("stale_claim")
    actual = check_run_witness_in_tx(tx, run, state.binding, ended=ended)
    if run.protocol_run_version != state.run_version:
        codec.refuse("stale_claim")
    current = replace(
        current,
        run=actual,
        work=replace(
            current.work,
            version=state.work_version,
            request_version=state.request_version,
            claim_epoch=state.claim_epoch,
            claimed_by=state.claimed_by,
            transport_attempts=state.transport_attempts,
            regrounds=state.regrounds,
            writer_repairs=state.writer_repairs,
        ),
    )
    return work, request, row, state, current, values


def transaction(driver, world_id, command):
    """No executable callback or protocol object can open the missing owner boundary."""
    refuse_runtime()
