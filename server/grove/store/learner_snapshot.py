"""Side-effect-free validation of inactive Learner snapshot and erasure closure."""

from grove.extraction import durable_codec as codec
from grove.extraction import execution_codec as legacy
from grove.extraction.dispositions import plan_completion
from grove.extraction.execution_state import advance
from grove.extraction.execution_types import CompletionPlan, ExecutionState
from grove.store.catalogue import LEARNER_TABLES
from grove.store.enums import ENUMS
from grove.writer.learner_values import walk_value_closure

_OPTIONAL = {
    "learner_work": {"retry_at", "completion_id", "supersedes_work_id"},
    "learner_executions": {
        "predecessor_id",
        "proposal_value_id",
        "batch_value_id",
        "triage_value_id",
    },
    "learner_calls": {
        "result_value_id",
        "model_call_id",
        "sent_at",
        "settled_at",
        "validation_code",
    },
    "learner_dispositions": {"hold_id", "mention_id", "handoff_id"},
    "learner_completions": set(),
    "learner_episode_progress": {
        "predecessor_progress_id",
        "completed_generation",
        "completion_id",
        "source_start",
    },
    "learner_handoffs": {
        "completion_id",
        "disposition_id",
        "consumer_receipt_id",
        "acknowledged_at",
    },
}
_ROOT_KINDS = {
    "initial_input_value_id": "binding",
    "event_value_id": "event",
    "state_value_id": "state",
    "binding_value_id": "binding",
    "proposal_value_id": "proposal",
    "batch_value_id": "batch",
    "triage_value_id": "triage",
    "request_value_id": "call_request",
    "result_value_id": "call_result",
    "payload_value_id": "handoff",
}


def _fail():
    codec.refuse("snapshot_inconsistent")


def _normal(tables):
    return {
        name: list(rows.values()) if isinstance(rows, dict) else rows
        for name, rows in tables.items()
    }


def _index(rows):
    result = {}
    for row in rows:
        key = row.get("id")
        if key is None or key in result:
            _fail()
        result[key] = row
    return result


def _roots(tables, work_id=None):
    roots = {}
    for table in LEARNER_TABLES:
        for row in tables.get(table.name, []):
            if (
                work_id is not None
                and (row["id"] if table.name == "learner_work" else row.get("work_id")) != work_id
            ):
                continue
            for column in table.columns:
                if column.type != "value_id":
                    continue
                for prop in column.props:
                    if row.get(prop) is None:
                        continue
                    kind = _ROOT_KINDS.get(prop)
                    if prop == "binding_value_id" and table.name == "learner_calls":
                        kind = "call_binding"
                    if prop == "detail_value_id":
                        kind = (
                            "completion" if table.name == "learner_completions" else "disposition"
                        )
                    if kind is None or row[prop] in roots and roots[row[prop]] != kind:
                        _fail()
                    roots[row[prop]] = kind
    return roots


def validate_learner_value_closure(tables):
    tables = _normal(tables)
    rows = _index(tables.get("history_values", []))
    roots = _roots(tables)
    values = {key: row.get("value") for key, row in rows.items()}
    closure = set()
    covered_roots = set()
    for work in tables.get("learner_work", []):
        work_roots = _roots(tables, work["id"])
        covered_roots.update(work_roots)
        closure.update(walk_value_closure(work_roots, values))
    if roots.keys() - covered_roots:
        _fail()
    for key in closure:
        if rows[key].get("version") != 1 or rows[key].get("forgotten_at") is not None:
            codec.refuse("erased_dependency")


def _shapes(tables):
    worlds = set()
    for table in LEARNER_TABLES:
        fields = {prop: col.type for col in table.columns for prop in col.props}
        arrays = {key for key, kind in fields.items() if kind == "ids"}
        required = fields.keys() - _OPTIONAL[table.name] - arrays
        unique = [set() for _ in table.uniques]
        for row in tables.get(table.name, []):
            if not required <= row.keys() or row.keys() - fields.keys():
                _fail()
            worlds.add(row["world_id"])
            for key, value in row.items():
                kind = fields[key]
                if value is None:
                    if key not in _OPTIONAL[table.name]:
                        _fail()
                elif kind in {"id", "value_id", "code"}:
                    codec._id(
                        value, 80 if key in {"run_id", "route", "model", "job", "effort"} else 128
                    )
                elif kind == "int":
                    codec._int(
                        value,
                        1
                        if key
                        in {
                            "version",
                            "generation",
                            "binding_revision",
                            "source_version",
                            "episode_version",
                            "required_generation",
                            "completed_generation",
                            "contract_version",
                        }
                        else 0,
                    )
                elif kind == "ids":
                    maximum = (
                        1536
                        if key == "dependency_ids"
                        else 1024
                        if key == "evidence_ids"
                        else 500
                        if key == "operation_ids"
                        else 256
                    )
                    if (
                        type(value) is not list
                        or len(value) > maximum
                        or len(set(value)) != len(value)
                    ):
                        _fail()
                    for item in value:
                        codec._id(item)
                elif kind.startswith("enum:"):
                    if value not in ENUMS[kind[5:]]:
                        _fail()
                elif kind == "instant":
                    if not hasattr(value, "utcoffset") or value.utcoffset() is None:
                        _fail()
                elif kind == "locator":
                    if (
                        type(value) is not str
                        or not value.startswith("key:")
                        or not 5 <= len(value) <= 1024
                    ):
                        _fail()
                else:
                    _fail()
            for constraint, seen in zip(table.uniques, unique, strict=True):
                value = tuple(row.get(key) for key in constraint.props)
                if None not in value:
                    if value in seen:
                        _fail()
                    seen.add(value)
    if len(worlds) > 1:
        _fail()


def validate_learner_snapshot(tables):
    tables = _normal(tables)
    _shapes(tables)
    validate_learner_value_closure(tables)
    values = {r["id"]: r["value"] for r in tables.get("history_values", [])}
    works = _index(tables.get("learner_work", []))
    requests = _index(tables.get("requests", []))
    sources, episodes = _index(tables.get("sources", [])), _index(tables.get("episodes", []))
    connectors = _index(tables.get("connectors", []))
    runs = _index(tables.get("run_records", tables.get("runs", [])))
    completions = _index(tables.get("learner_completions", []))
    executions = _index(tables.get("learner_executions", []))
    dispositions = _index(tables.get("learner_dispositions", []))
    all_rows = {r["id"]: r for rows in tables.values() for r in rows if "id" in r}
    calls = _index(tables.get("learner_calls", []))
    used_completions, used_dispositions = set(), set()
    plans = {}
    for row in completions.values():
        detail = codec.load_json(values[row["detail_value_id"]])
        codec._closed(
            detail, ("plan", "effect_ledger", "request_version", "work_version", "run_version")
        )
        plans[row["id"]] = legacy._decode(detail["plan"], CompletionPlan)
    for name in (
        "learner_executions",
        "learner_calls",
        "learner_dispositions",
        "learner_completions",
        "learner_handoffs",
    ):
        if any(row["work_id"] not in works for row in tables.get(name, [])):
            _fail()
    for request in requests.values():
        if (
            request.get("protocol") == "learner_execution_v1"
            and request.get("learner_work_id") not in works
        ):
            _fail()
    latest_states = {}
    for work in works.values():
        if (
            work["source_id"] not in sources
            or work["episode_id"] not in episodes
            or work["connector_id"] not in connectors
        ):
            codec.refuse("erased_dependency")
        request = requests.get(work["request_id"])
        if (
            request is None
            or request.get("protocol") != "learner_execution_v1"
            or request.get("learner_work_id") != work["id"]
        ):
            _fail()
        chain = sorted(
            (r for r in executions.values() if r["work_id"] == work["id"]),
            key=lambda r: r["ordinal"],
        )
        if not 1 <= len(chain) <= 64 or chain[-1]["id"] != work["latest_execution_id"]:
            _fail()
        previous = None
        for ordinal, row in enumerate(chain):
            if (
                row["ordinal"] != ordinal
                or row["version"] != 1
                or row.get("predecessor_id") != (chain[ordinal - 1]["id"] if ordinal else None)
                or row["request_id"] != request["id"]
                or row["run_id"] not in runs
            ):
                _fail()
            state = codec.rehydrate_state(values[row["state_value_id"]], values, completions=plans)
            witness = codec.decode_run_witness(
                codec.load_json(values[row["state_value_id"]])["run_witness"]
            )
            actual_run = runs.get(witness.actual_run_id)
            if (
                actual_run is None
                or actual_run.get("version", 0) < witness.actual_row_version
                or actual_run.get("agent") != "learner"
                or tuple(actual_run.get("input_ids", [])) != witness.input_ids
                or witness.protocol_run_version != state.run_version
                or row["run_id"] != witness.actual_run_id
            ):
                _fail()
            if (
                any(ref.id not in all_rows for ref in state.binding.context.dependencies)
                or state.binding.source.screening_id not in all_rows
            ):
                codec.refuse("erased_dependency")
            source = sources[work["source_id"]]
            if (
                state.binding.source.id,
                state.binding.episode.id,
                state.binding.producer.id,
                request.get("source_id"),
                request.get("content_key"),
                request.get("caller"),
            ) != (
                work["source_id"],
                work["episode_id"],
                work["connector_id"],
                work["source_id"],
                state.binding.work.content_key,
                "learner",
            ):
                _fail()
            if (
                source.get("forget_state") != "retained"
                or source.get("content") != state.binding.source.content
                or source.get("speakers") != state.binding.source.speakers
            ):
                codec.refuse("erased_dependency")
            if (
                state.revision != ordinal
                or state.status != row["state"]
                or state.claim_epoch != row["claim_epoch"]
                or state.binding.world_id != work["world_id"]
                or state.binding.work.id != work["id"]
                or state.binding.work.request_id != request["id"]
            ):
                _fail()
            if sorted({v.id for v in state.binding.context.dependencies}) != row.get(
                "dependency_ids", []
            ):
                _fail()
            if ordinal == 0:
                initial = codec.load_json(values[row["event_value_id"]])
                codec._closed(initial, ("kind", "binding_value_id", "order"))
                if (
                    row["event_kind"] != "initial"
                    or initial["kind"] != "initial"
                    or initial["binding_value_id"] != work["initial_input_value_id"]
                    or state != ExecutionState(state.binding, run_version=state.binding.run.version)
                ):
                    _fail()
            else:
                event = codec.rehydrate_event(values[row["event_value_id"]], values)
                if event.kind != row["event_kind"] or advance(previous, event) != state:
                    _fail()
            if (
                row["binding_revision"]
                != 1 + sum(r["event_kind"] == "rebind" for r in chain[: ordinal + 1])
                or row["binding_revision"] > 2
            ):
                _fail()
            previous = state
        latest_states[work["id"]] = state
        if {c.id for c in (*state.prior_invocations, *state.invocations)} != {
            c["id"] for c in calls.values() if c["work_id"] == work["id"]
        }:
            _fail()
        if (
            work["version"],
            request["version"],
            work["state"],
            request["status"],
            request.get("repairs", 0),
        ) != (
            state.work_version,
            state.request_version,
            state.status,
            state.request_status,
            state.writer_repairs,
        ):
            _fail()
        if (
            work.get("completion_id") != state.completion_id
            or request.get("learner_completion_id") != state.completion_id
        ):
            _fail()
        if state.completion_id is not None:
            used_completions.add(state.completion_id)
            row = completions.get(state.completion_id)
            if (
                row is None
                or row["work_id"] != work["id"]
                or row["request_id"] != request["id"]
                or row["execution_id"] != chain[-1]["id"]
                or row["outcome"] != state.status
            ):
                _fail()
            if state.completion != plan_completion(state.binding, state.proposal, state.triage):
                _fail()
            if row["message_count"] != len(state.binding.inventory) or (
                row["blank_count"],
                row["empty_count"],
                row["extracted_count"],
                row["operation_count"],
            ) != (
                state.completion.blank_messages,
                state.completion.empty_messages,
                state.completion.extracted_messages,
                len(state.proposal.operations),
            ):
                _fail()
            held = sorted(
                (dispositions.get(key) for key in row.get("disposition_ids", [])),
                key=lambda r: -1 if r is None else r["message_ordinal"],
            )
            used_dispositions.update(row.get("disposition_ids", []))
            if len(held) != row["message_count"] or any(r is None for r in held):
                _fail()
            for i, (disposition, expected, message) in enumerate(
                zip(held, state.proposal.dispositions, state.binding.inventory, strict=True)
            ):
                if (
                    disposition["message_ordinal"] != i
                    or disposition["execution_id"] != row["execution_id"]
                    or disposition["work_id"] != work["id"]
                    or disposition["locator"] != message.locator
                    or disposition["state"] != expected.state
                    or any(disposition.get(k) for k in ("hold_id", "mention_id", "handoff_id"))
                ):
                    _fail()
                if codec.load_json(values[disposition["detail_value_id"]]) != {
                    "message": codec.plain(message),
                    "disposition": codec.plain(expected),
                }:
                    _fail()
            detail = codec.load_json(values[row["detail_value_id"]])
            if (detail["request_version"], detail["work_version"], detail["run_version"]) != (
                state.request_version,
                state.work_version,
                state.run_version,
            ):
                _fail()
            effects = detail["effect_ledger"]
            if [effect["op_id"] for effect in effects] != [
                op.id for op in state.proposal.operations
            ]:
                _fail()
            entries = [
                e
                for e in tables.get("change_entries", [])
                if e.get("commit_id") == row["commit_id"]
            ]
            sequences = {
                e["sequence"] for e in entries if e.get("change_set_id") == row["change_set_id"]
            }
            if any(not set(effect["entry_sequences"]) <= sequences for effect in effects):
                _fail()
            for effect in effects:
                codec._closed(
                    effect, ("op_id", "target_type", "target_id", "evidence_ids", "entry_sequences")
                )
                target_table = {"item": "items", "link": "links"}.get(effect["target_type"])
                if target_table is None or effect["target_id"] not in _index(
                    tables.get(target_table, [])
                ):
                    codec.refuse("erased_dependency")
                evidence = _index(tables.get("evidence", []))
                if not effect["evidence_ids"] or any(
                    key not in evidence
                    or evidence[key].get("target_id") != effect["target_id"]
                    or evidence[key].get("source_id") != work["source_id"]
                    or evidence[key].get("episode_id") != work["episode_id"]
                    for key in effect["evidence_ids"]
                ):
                    codec.refuse("erased_dependency")
            if row["created_item_count"] != len(
                {
                    e["entity_id"]
                    for e in entries
                    if e["entity_type"] == "item" and e["field"] == "row"
                }
            ):
                _fail()
    if used_completions != completions.keys() or used_dispositions != dispositions.keys():
        _fail()
    for call in tables.get("learner_calls", []):
        state = latest_states[call["work_id"]]
        found = [c for c in (*state.prior_invocations, *state.invocations) if c.id == call["id"]]
        if (
            len(found) != 1
            or call["prepared_execution_id"] not in executions
            or call["latest_execution_id"] not in executions
        ):
            _fail()
        actual = found[0]
        if (
            call["state"],
            call["ordinal"],
            call.get("model_call_id"),
            call["run_id"],
            call["claim_epoch"],
        ) != (
            actual.state,
            actual.ordinal,
            actual.model_call_id,
            actual.run_id,
            actual.claim_epoch,
        ):
            _fail()
        restored = codec._invocations(
            [
                {
                    "call_id": call["id"],
                    "request_value_id": call["request_value_id"],
                    "result_value_id": call.get("result_value_id"),
                    "binding_value_id": call["binding_value_id"],
                }
            ],
            values,
        )[0]
        if restored != actual:
            _fail()
    progresses = tables.get("learner_episode_progress", [])
    groups = {}
    for row in progresses:
        work = works.get(row["required_work_id"])
        if (
            work is None
            or work["generation"] != row["required_generation"]
            or work["episode_id"] != row["episode_id"]
            or row["stream_anchor_source_id"] not in sources
        ):
            _fail()
        if row.get("completed_generation") is not None:
            completion = completions.get(row.get("completion_id"))
            if (
                completion is None
                or completion["work_id"] != work["id"]
                or row["completed_generation"] != work["generation"]
            ):
                _fail()
        elif row.get("completion_id") is not None:
            _fail()
        groups.setdefault((row["stream_anchor_source_id"], row["profile"]), []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: r["position"])
        incomplete = False
        for i, row in enumerate(group):
            if row["position"] != i or row.get("predecessor_progress_id") != (
                group[i - 1]["id"] if i else None
            ):
                _fail()
            done = row.get("completed_generation") == row["required_generation"]
            if incomplete and done:
                _fail()
            incomplete |= not done
    for handoff in tables.get("learner_handoffs", []):
        completion = completions.get(handoff.get("completion_id"))
        if (
            completion is None
            or handoff.get("disposition_id") is not None
            or handoff["kind"] == "curator_question_v1"
            or handoff["execution_id"] != completion["execution_id"]
            or handoff["work_id"] != completion["work_id"]
            or handoff["contract_version"] != 1
        ):
            _fail()
        payload = codec.load_json(values[handoff["payload_value_id"]])
        codec._closed(
            payload,
            (
                "schema",
                "kind",
                "work_id",
                "execution_id",
                "completion_id",
                "commit_id",
                "change_set_id",
                "target_ids",
                "contradiction_ids",
            ),
        )
        if (
            payload["schema"] != "grove.learner.dirty.v1"
            or payload["completion_id"] != completion["id"]
            or payload["target_ids"] != handoff.get("target_ids", [])
            or payload["kind"] != handoff["kind"]
        ):
            _fail()
        if (handoff["state"] == "acknowledged") != (
            handoff.get("consumer_receipt_id") is not None
            and handoff.get("acknowledged_at") is not None
        ):
            _fail()
