"""Inactive, owner-leased question transactions.

No route or scheduler calls this module. The explicit Access provider must own
principal validation, source admission, forget exclusion, and final release.
There is deliberately no default provider. Runtime activation still refuses.
"""

import json
from dataclasses import replace
from datetime import date, timedelta

from neo4j.exceptions import DriverError, IncompleteCommit, Neo4jError

from grove import clock
from grove.cleanup.access import CleanupRefused, hold
from grove.cleanup.legwork import LegworkAttemptV1
from grove.cleanup.question_intents import CuratorQuestionIntentV1, ResolutionTransferV1
from grove.cleanup.question_lifecycle import (
    AnswerCommandV1,
    QuestionApplicationV1,
    QuestionBindingV1,
    canonical_json,
    decode_json,
)
from grove.ids import new_id
from grove.screening import Screener
from grove.store.enums import ENUMS
from grove.writer.api import execute_prepared_in_tx, prepare_batch
from grove.writer.cleanup import _caller, _ordinary
from grove.writer.errors import WriteFailed, WriteOutcomeUnknown
from grove.writer.history import Stamp, add_text, encode
from grove.writer.message_binding import check_observation_binding
from grove.writer.tx import WorldWriter

PROTOCOL = "question_answer_v1"
_LABELS = frozenset(
    {
        "Question",
        "QuestionBinding",
        "QuestionWork",
        "QuestionApplication",
        "QuestionEvent",
        "CuratorLegwork",
        "CuratorEscalation",
        "CuratorRunOrder",
        "Request",
        "Source",
        "Episode",
        "Connector",
        "ScreeningReceipt",
        "HistoryValue",
        "Item",
        "Link",
        "ChangeEntry",
        "RunRecord",
    }
)


def _rows(tx, label, **where):
    if label not in _LABELS or any(not key.isidentifier() for key in where):
        raise ValueError("invalid internal query")
    predicate = ", ".join(f"{key}: ${key}" for key in where)
    pattern = (
        f"()-[n:LINK {{{predicate}}}]->()" if label == "Link" else f"(n:{label} {{{predicate}}})"
    )
    rows = tx.run(f"MATCH {pattern} RETURN n LIMIT 1001", **where)
    if len(rows) > 1000:
        raise CleanupRefused("question_page_bound")
    return [row["n"] for row in rows]


def _one(tx, label, row_id, *, optional=False):
    rows = _rows(tx, label, id=row_id)
    if len(rows) != 1:
        if optional and not rows:
            return None
        raise CleanupRefused("question_reference_missing")
    return rows[0]


def _stamp(world_id, run_id, caller, request_id=None):
    return Stamp(
        world_id,
        clock.now(world_id),
        caller.actor,
        new_id(world_id),
        new_id(world_id),
        request_id,
        run_id,
        caller.kind,
    )


def _put(tx, label, row, old=None):
    if label not in _LABELS:
        raise ValueError("invalid internal label")
    if old is None:
        tx.run(f"CREATE (n:{label}) SET n += $row", row=row)
    else:
        # Explicit nulls remove optional fields that disappeared in the new state.
        tx.run(
            f"MATCH (n:{label} {{id: $id}}) SET n += $row",
            id=row["id"],
            row={**{key: None for key in old if key not in row}, **row},
        )


def _event(
    tx, stamp, event, label, before, after, *, question_id=None, reason=None, application_id=None
):
    """Snapshot history contains structural references, never the referenced prose."""
    payload = encode({"label": label, "before": before, "after": after})
    if len(payload.encode("utf-8")) > 256 * 1024:
        raise CleanupRefused("question_history_bound")
    _put(
        tx,
        "QuestionEvent",
        {
            "id": new_id(stamp.world_id),
            "question_id": question_id,
            "work_id": after["id"] if label == "QuestionWork" else None,
            "event": event,
            "commit_id": stamp.commit_id,
            "change_set_id": stamp.change_set_id,
            "sequence": tx.next_sequence(),
            "at": stamp.at,
            "snapshot": payload,
            "reason": reason,
            "application_id": application_id,
            "version": 1,
            "touched_at": stamp.at,
        },
    )


def _change(
    tx, stamp, label, row, old=None, *, event, question_id=None, reason=None, application_id=None
):
    row = {
        key: value
        for key, value in {**row, "world_id": stamp.world_id}.items()
        if value is not None
    }
    if old is not None and row == old:
        return old
    row = {**row, "version": 1 if old is None else old["version"] + 1, "touched_at": stamp.at}
    _put(tx, label, row, old)
    _event(
        tx,
        stamp,
        event,
        label,
        old,
        row,
        question_id=question_id,
        reason=reason,
        application_id=application_id,
    )
    return row


def _safe_text(tx, stamp, screener, text):
    if (
        type(screener) is not Screener
        or type(text) is not str
        or not text.strip()
        or len(text) > 2000
    ):
        raise CleanupRefused("question_text_invalid")
    checked = screener.screen_text(text)
    if checked.dropped_whole or len(checked.messages) != 1:
        raise CleanupRefused("question_screening_refused")
    return add_text(tx, stamp, checked.messages[0].text)


def _targets(tx, targets):
    result = []
    for target in targets:
        row = _one(tx, "Item" if target.kind == "item" else "Link", target.id)
        if row.get("version") != target.version:
            raise CleanupRefused("question_target_version_conflict")
        result.append(row)
    return result


def _observation(source):
    return {
        "ev_id": "answer",
        "source_id": source.source_id,
        "episode_id": source.episode_id,
        "message_key": source.message_key,
        "locator": source.locator,
        "source_time": source.source_time.isoformat(),
        "statement_class": "report_state",
        "derivation": "observed",
        "speaker_kind": "person",
        "modality": "text",
    }


def _source(tx, source, *, command=None, caller=None, text=None):
    retained = _one(tx, "Source", source.source_id)
    episode = _one(tx, "Episode", source.episode_id)
    connector = _one(tx, "Connector", retained.get("connector_id"))
    if (
        retained.get("forget_state") != "kept"
        or retained.get("version") != source.source_version
        or episode.get("version") != source.episode_version
    ):
        raise CleanupRefused("question_source_version_conflict")
    if not _rows(tx, "ScreeningReceipt", source_id=source.source_id):
        raise CleanupRefused("question_source_not_screened")
    message = check_observation_binding(
        tx, _observation(source), source=retained, connector=connector, episode=episode
    )
    if message is None or source.source_time > clock.now(tx.world_id):
        raise CleanupRefused("question_exact_message_required")
    if text is not None and message.get("text") != text:
        raise CleanupRefused("question_answer_text_changed")
    if command is not None:
        expected = "person_in_views" if caller.kind == "person" else "ai_host"
        if connector.get("type") != expected or caller.actor != command.principal_id:
            raise CleanupRefused("question_answer_principal_mismatch")
        if caller.kind == "ai_host" and not (
            command.host_message_id
            and message.get("host_message_id") == command.host_message_id
            and message.get("verbatim") is True
            and message.get("relay") == "answer"
        ):
            raise CleanupRefused("question_answer_not_verbatim")
    return message


def _legwork(tx, binding):
    targets = {canonical_json(target) for target in binding.targets}
    covered = set()
    for attempt_id in binding.legwork_ids:
        row = _one(tx, "CuratorLegwork", attempt_id)
        attempt = decode_json(LegworkAttemptV1, row["attempt"])
        key = canonical_json(attempt.target)
        if key not in targets:
            raise CleanupRefused("question_legwork_target_mismatch")
        if attempt.result not in {"no_match", "no_eligible_source", "person_only"}:
            raise CleanupRefused("question_legwork_not_settled")
        covered.add(key)
    if covered != targets:
        raise CleanupRefused("question_legwork_incomplete")


def record_legwork(driver, world_id, caller, attempt, *, access=None):
    _caller(caller, {"cleanup"})
    if type(attempt) is not LegworkAttemptV1:
        raise CleanupRefused("question_legwork_invalid")
    refs = (attempt.work_id, attempt.target.id, attempt.authority_id, *attempt.scope_ids)
    with hold(access, world_id, attempt.run_id, "record_legwork", *refs):

        def work(tx):
            _ordinary(tx)
            _targets(tx, (attempt.target,))
            _one(tx, "HistoryValue", attempt.reason_value_id)
            for source in attempt.source_refs:
                _source(tx, source)
            old = _one(tx, "CuratorLegwork", attempt.attempt_id, optional=True)
            if old:
                if old["attempt"] != canonical_json(attempt):
                    raise CleanupRefused("question_legwork_conflict")
                return old["id"]
            stamp = _stamp(world_id, attempt.run_id, caller)
            _change(
                tx,
                stamp,
                "CuratorLegwork",
                {
                    "id": attempt.attempt_id,
                    "work_id": attempt.work_id,
                    "run_id": attempt.run_id,
                    "target": canonical_json(attempt.target),
                    "result": attempt.result,
                    "attempt": canonical_json(attempt),
                    "recorded_at": stamp.at,
                    "reason": attempt.reason_value_id,
                },
                event="legwork",
            )
            return attempt.attempt_id

        return WorldWriter(driver, world_id).transaction(work)


def _work_ids(binding):
    return tuple(sorted({part for plan in binding.plans for part in plan.obligation_ids}))


def _ensure_work(tx, stamp, binding, question_id):
    for work_id in _work_ids(binding):
        old = _one(tx, "QuestionWork", work_id, optional=True)
        if old is not None and (
            old.get("group_id") != binding.topic_key
            or old.get("question_id") not in (None, question_id)
        ):
            raise CleanupRefused("question_work_already_bound")
        if old is not None and old.get("state") in {"resolved", "cancelled"}:
            raise CleanupRefused("question_work_already_terminal")
        row = old or {"id": work_id, "group_id": binding.topic_key, "state": "waiting_person"}
        row = {**row, "question_id": question_id}
        bound_targets = {
            canonical_json(target)
            for plan in binding.plans
            if work_id in plan.obligation_ids
            for target in plan.targets
        }
        if len(bound_targets) == 1:
            target = next(iter(bound_targets))
            if row.get("target") not in (None, target):
                raise CleanupRefused("question_work_target_conflict")
            row["target"] = target
        # A multi-target obligation remains bound by its plan; the single-target
        # no-source command must refuse it until an owner splits that work.
        _change(tx, stamp, "QuestionWork", row, old, event="bind_work", question_id=question_id)


def create_or_join_question(
    driver,
    world_id,
    run_id,
    caller,
    binding,
    *,
    ask,
    kind,
    reason,
    screener,
    priority=0,
    identity=False,
    backfill_week=None,
    access=None,
):
    _caller(caller, {"cleanup"})
    if (
        type(binding) is not QuestionBindingV1
        or kind not in ENUMS["question_class"]
        or type(priority) is not int
        or not 0 <= priority <= 2
        or type(identity) is not bool
    ):
        raise CleanupRefused("question_input_invalid")
    if backfill_week is not None and type(backfill_week) is not date:
        raise CleanupRefused("question_backfill_week_invalid")
    with hold(
        access,
        world_id,
        run_id,
        "create_question",
        *(t.id for t in binding.targets),
        *binding.legwork_ids,
        *_work_ids(binding),
    ):

        def work(tx):
            _ordinary(tx)
            _targets(tx, binding.targets)
            _legwork(tx, binding)
            stamp = _stamp(world_id, run_id, caller)
            existing = _rows(tx, "QuestionBinding", open_topic_key=binding.topic_key)
            if existing:
                old = existing[0]
                current = decode_json(QuestionBindingV1, old["binding"])
                if current.targets != binding.targets or len(current.plans) != len(binding.plans):
                    raise CleanupRefused("question_rebind_required")
                plans = []
                for left, right in zip(current.plans, binding.plans, strict=True):
                    if replace(left, obligation_ids=right.obligation_ids) != right:
                        raise CleanupRefused("question_rebind_required")
                    plans.append(
                        replace(
                            left,
                            obligation_ids=tuple(
                                sorted(set(left.obligation_ids) | set(right.obligation_ids))
                            ),
                        )
                    )
                combined = replace(
                    current,
                    plans=tuple(plans),
                    legwork_ids=tuple(sorted(set(current.legwork_ids) | set(binding.legwork_ids))),
                )
                question = _one(tx, "Question", current.question_id)
                if combined == current and priority <= question["priority"]:
                    return {
                        "question_id": current.question_id,
                        "state": "open",
                        "work_ids": _work_ids(current),
                    }
                if combined != current:
                    combined = replace(combined, revision=current.revision + 1)
                _ensure_work(tx, stamp, combined, current.question_id)
                _change(
                    tx,
                    stamp,
                    "QuestionBinding",
                    {**old, "binding": canonical_json(combined), "revision": combined.revision},
                    old,
                    event="coalesce",
                    question_id=current.question_id,
                )
                _change(
                    tx,
                    stamp,
                    "Question",
                    {**question, "priority": max(priority, question["priority"])},
                    question,
                    event="coalesce",
                    question_id=current.question_id,
                )
                return {
                    "question_id": current.question_id,
                    "state": "open",
                    "work_ids": _work_ids(combined),
                }
            if binding.revision != 1 or _one(tx, "Question", binding.question_id, optional=True):
                raise CleanupRefused("question_identity_conflict")
            day = stamp.at.date()
            if backfill_week is not None and not backfill_week <= day < backfill_week + timedelta(
                days=7
            ):
                raise CleanupRefused("question_backfill_week_invalid")
            if identity and (
                len(_rows(tx, "QuestionBinding", identity_day=day)) >= 3
                or (
                    backfill_week is not None
                    and len(_rows(tx, "QuestionBinding", identity_week=backfill_week)) >= 10
                )
            ):
                _ensure_work(tx, stamp, binding, None)
                return {"question_id": None, "state": "cap_reached", "work_ids": _work_ids(binding)}
            ask_id, reason_id = (_safe_text(tx, stamp, screener, text) for text in (ask, reason))
            _ensure_work(tx, stamp, binding, binding.question_id)
            _change(
                tx,
                stamp,
                "Question",
                {
                    "id": binding.question_id,
                    "subject_items": [t.id for t in binding.targets if t.kind == "item"],
                    "ask": ask_id,
                    "class": kind,
                    "legwork_sources": [],
                    "legwork_reason": reason_id,
                    "answer_options": encode([p.plan_id for p in binding.plans]),
                    "raised_by": "curator",
                    "raised_at": stamp.at,
                    "priority": priority,
                    "state": "open",
                    "unblocks": list(_work_ids(binding)),
                },
                event="create",
                question_id=binding.question_id,
            )
            _change(
                tx,
                stamp,
                "QuestionBinding",
                {
                    "id": new_id(world_id),
                    "question_id": binding.question_id,
                    "revision": 1,
                    "topic_key": binding.topic_key,
                    "open_topic_key": binding.topic_key,
                    "binding": canonical_json(binding),
                    "state": "open",
                    "created_at": stamp.at,
                    "identity_day": day if identity else None,
                    "identity_week": backfill_week if identity else None,
                },
                event="create",
                question_id=binding.question_id,
            )
            return {
                "question_id": binding.question_id,
                "state": "open",
                "work_ids": _work_ids(binding),
            }

        return WorldWriter(driver, world_id).transaction(work)


def _binding(tx, command, *, replay=False):
    question = _one(tx, "Question", command.question_id)
    rows = _rows(tx, "QuestionBinding", question_id=command.question_id)
    if len(rows) != 1:
        raise CleanupRefused("question_binding_missing")
    stored = rows[0]
    binding = decode_json(QuestionBindingV1, stored["binding"])
    if not replay and (question["state"] != "open" or binding.revision != command.binding_revision):
        raise CleanupRefused("question_binding_version_conflict")
    plans = [p for p in binding.plans if p.plan_id == command.plan_id]
    if len(plans) != 1:
        raise CleanupRefused("question_plan_missing")
    plan = plans[0]
    if (command.variant == "return_to_sources") != (plan.action == "return_to_sources"):
        raise CleanupRefused("question_answer_variant_mismatch")
    if command.variant == "text" and plan.action != "apply_batch":
        raise CleanupRefused("question_text_plan_required")
    return question, stored, binding, plan


def _request(tx, command, caller):
    row = _one(tx, "Request", command.request_id)
    if (
        row.get("protocol") != PROTOCOL
        or row.get("answer_command") != canonical_json(command)
        or row.get("caller") != caller.actor
    ):
        raise CleanupRefused("idempotency_conflict")
    return row


def submit_answer(driver, world_id, run_id, caller, command, *, answer_text, access=None):
    _caller(caller, {"person", "ai_host"})
    if (
        type(command) is not AnswerCommandV1
        or type(answer_text) is not str
        or not answer_text.strip()
        or len(answer_text) > 2000
    ):
        raise CleanupRefused("question_answer_invalid")
    try:
        if len(answer_text.encode("utf-8")) > 8192:
            raise CleanupRefused("question_answer_invalid")
    except UnicodeError:
        raise CleanupRefused("question_answer_invalid") from None
    with hold(
        access,
        world_id,
        run_id,
        "submit_answer",
        command.principal_id,
        command.question_id,
        command.source.source_id,
        command.source.episode_id,
    ):

        def work(tx):
            _ordinary(tx)
            _source(tx, command.source, command=command, caller=caller, text=answer_text)
            old = _one(tx, "Request", command.request_id, optional=True)
            if old:
                return _request(tx, command, caller)["answer_state"]
            _, _, binding, plan = _binding(tx, command)
            _targets(tx, plan.targets)
            stamp = _stamp(world_id, run_id, caller, command.request_id)
            _change(
                tx,
                stamp,
                "Request",
                {
                    "id": command.request_id,
                    "caller": caller.actor,
                    "source_id": command.source.source_id,
                    "run_id": run_id,
                    "status": "queued",
                    "attempts": 0,
                    "repairs": 0,
                    "created_at": stamp.at,
                    "protocol": PROTOCOL,
                    "question_id": command.question_id,
                    "question_binding_revision": binding.revision,
                    "answer_command": canonical_json(command),
                    "answer_state": "accepted",
                    "principal_id": command.principal_id,
                    "relay": "answer",
                    "verbatim": True,
                    "host_message_id": command.host_message_id,
                    "said_at": command.source.source_time,
                    "about": [command.question_id],
                },
                event="accept_answer",
                question_id=command.question_id,
            )
            return "accepted"

        return WorldWriter(driver, world_id).transaction(work)


def _exact_batch(tx, plan, command):
    if plan.action == "confirm":
        if any(target.slot != "claim" for target in plan.targets):
            raise CleanupRefused("question_claim_slot_required")
        # D4 enforces the person-in-views-only shortcut, capabilities and horizon.
        ops = [
            {
                "op": "verify",
                "op_id": f"answer_{index}",
                "claim": target.id,
                "pieces": [{"ev_id": "answer", "answer": "supports"}],
                "disposition": "confirmed",
                "reason": "The person answered the bound question.",
            }
            for index, target in enumerate(plan.targets)
        ]
        for target in plan.targets:
            row = _one(tx, "Item" if target.kind == "item" else "Link", target.id)
            if row.get("recheck_by") is not None:
                # Renewal must be an explicit prebound batch, never a guessed delay.
                raise CleanupRefused("question_explicit_horizon_plan_required")
        return {"ops": ops, "evidence": [_observation(command.source)]}
    if plan.action != "apply_batch":
        return None
    value = _one(tx, "HistoryValue", plan.batch_value_id).get("value")
    if type(value) is not str or len(value) > 256 * 1024 or len(value.encode("utf-8")) > 256 * 1024:
        raise CleanupRefused("question_batch_value_invalid")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise CleanupRefused("question_batch_value_invalid")
            result[key] = item
        return result

    try:
        batch = json.loads(value, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        raise CleanupRefused("question_batch_value_invalid") from None
    observations = batch.get("evidence") if type(batch) is dict else None
    if not observations:
        raise CleanupRefused("question_batch_answer_evidence_required")
    expected = _observation(command.source)
    keys = ("source_id", "episode_id", "message_key", "locator", "source_time")
    if not any(
        all(observation.get(key) == expected[key] for key in keys) for observation in observations
    ):
        raise CleanupRefused("question_batch_answer_evidence_required")
    bound = {target.id for target in plan.targets}
    for op in batch.get("ops", []):
        selected = {op[key] for key in ("item", "link", "claim") if type(op.get(key)) is str}
        if not selected or not selected <= bound:
            raise CleanupRefused("question_batch_target_unbound")
    return batch


def _reconstruct(tx, row, command):
    stored = _one(tx, "QuestionApplication", row.get("application_id"))
    result = decode_json(QuestionApplicationV1, stored["application"])
    if (
        result.request_id != command.request_id
        or result.question_id != command.question_id
        or result.binding_revision != command.binding_revision
        or result.source != command.source
        or result.principal_id != command.principal_id
        or result.plan_id != command.plan_id
        or result.commit_id != row.get("commit_id")
        or result.change_set_id != row.get("change_set_id")
    ):
        raise CleanupRefused("question_application_mismatch")
    return result


def apply_answer(driver, world_id, run_id, caller, command, *, access=None):
    _caller(caller, {"person", "ai_host"})
    if type(command) is not AnswerCommandV1:
        raise CleanupRefused("question_answer_invalid")
    refs = (
        command.principal_id,
        command.question_id,
        command.source.source_id,
        command.source.episode_id,
    )
    stamp = _stamp(world_id, run_id, caller, command.request_id)
    with hold(access, world_id, run_id, "apply_answer", *refs):

        def work(tx):
            _ordinary(tx)
            _source(tx, command.source, command=command, caller=caller)
            request = _request(tx, command, caller)
            if request["answer_state"] == "applied":
                return _reconstruct(tx, request, command)
            if request["answer_state"] != "accepted":
                raise CleanupRefused("question_answer_not_applicable")
            question, binding_row, binding, plan = _binding(tx, command)
            before = _targets(tx, plan.targets)
            works = [_one(tx, "QuestionWork", work_id) for work_id in plan.obligation_ids]
            if any(
                row.get("question_id") != question["id"]
                or row.get("application_id")
                or row.get("state") in {"resolved", "cancelled"}
                for row in works
            ):
                raise CleanupRefused("question_obligation_not_open")
            application_id = new_id(world_id)
            batch = _exact_batch(tx, plan, command)
            effects = []
            with hold(
                access,
                world_id,
                run_id,
                "apply_answer_commit",
                *refs,
                *(target.id for target in plan.targets),
                *plan.obligation_ids,
            ):
                if batch is not None:
                    execute_prepared_in_tx(
                        tx,
                        stamp,
                        prepare_batch(caller, batch, request_id=command.request_id),
                        request,
                    )
                    entries = _rows(tx, "ChangeEntry", commit_id=stamp.commit_id)
                    for target, prior in zip(plan.targets, before, strict=True):
                        after = _one(tx, "Item" if target.kind == "item" else "Link", target.id)
                        if not any(
                            entry.get("entity_type") == target.kind
                            and entry.get("entity_id") == target.id
                            and not entry.get("history_only")
                            for entry in entries
                        ):
                            raise CleanupRefused("question_exact_effect_missing")
                        for op in batch["ops"]:
                            if target.id not in {op.get(key) for key in ("item", "link", "claim")}:
                                continue
                            effects.append(
                                (
                                    target.kind,
                                    target.id,
                                    prior["version"],
                                    after["version"],
                                    op["op_id"],
                                )
                            )
                transfer = plan.action in {"return_to_sources", "resolution_transfer"}
                for row in works:
                    columns = {
                        **row,
                        "state": "pending" if transfer else "resolved",
                        "application_id": application_id,
                    }
                    if transfer:
                        receipt = ResolutionTransferV1(
                            new_id(world_id),
                            application_id,
                            question["id"],
                            binding.revision,
                            command.source,
                            (row["id"],),
                            plan.targets,
                        )
                        columns["transfer"] = canonical_json(receipt)
                    _change(
                        tx,
                        stamp,
                        "QuestionWork",
                        columns,
                        row,
                        event="answer_transfer" if transfer else "resolve",
                        question_id=question["id"],
                        application_id=application_id,
                    )
                all_work = [_one(tx, "QuestionWork", work_id) for work_id in _work_ids(binding)]
                remaining = tuple(
                    sorted(row["id"] for row in all_work if not row.get("application_id"))
                )
                state = "open" if remaining else "answered"
                _change(
                    tx,
                    stamp,
                    "Question",
                    {
                        **question,
                        "state": state,
                        "answer_source_id": command.source.source_id,
                        "applied_at": stamp.at,
                        "receipt": encode({"application_id": application_id}),
                    },
                    question,
                    event="partial_answer" if remaining else "answer",
                    question_id=question["id"],
                    application_id=application_id,
                )
                if state == "answered":
                    terminal = {k: v for k, v in binding_row.items() if k != "open_topic_key"}
                    _change(
                        tx,
                        stamp,
                        "QuestionBinding",
                        {**terminal, "state": state},
                        binding_row,
                        event="answer",
                        question_id=question["id"],
                        application_id=application_id,
                    )
                result = QuestionApplicationV1(
                    application_id,
                    command.request_id,
                    question["id"],
                    binding.revision,
                    command.principal_id,
                    command.source,
                    plan.plan_id,
                    stamp.commit_id,
                    stamp.change_set_id,
                    () if transfer else plan.obligation_ids,
                    tuple(
                        sorted(
                            row["id"]
                            for row in all_work
                            if row.get("state") not in {"resolved", "cancelled"}
                        )
                    ),
                    plan.obligation_ids if transfer else (),
                    tuple(effects),
                )
                _change(
                    tx,
                    stamp,
                    "QuestionApplication",
                    {
                        "id": application_id,
                        "request_id": command.request_id,
                        "question_id": question["id"],
                        "binding_revision": binding.revision,
                        "principal_id": command.principal_id,
                        "source_binding": canonical_json(command.source),
                        "plan_id": plan.plan_id,
                        "commit_id": stamp.commit_id,
                        "change_set_id": stamp.change_set_id,
                        "application": canonical_json(result),
                        "applied_at": stamp.at,
                    },
                    event="application",
                    question_id=question["id"],
                    application_id=application_id,
                )
                _change(
                    tx,
                    stamp,
                    "Request",
                    {
                        **request,
                        "status": "saved",
                        "answer_state": "applied",
                        "application_id": application_id,
                        "commit_id": stamp.commit_id,
                        "change_set_id": stamp.change_set_id,
                        "committed_at": stamp.at,
                        "attempts": request["attempts"] + 1,
                    },
                    request,
                    event="answer_request",
                    question_id=question["id"],
                    application_id=application_id,
                )
                _source(tx, command.source, command=command, caller=caller)
                from grove.writer import apply as writer_apply

                writer_apply._before_commit()
            return result

        try:
            result = WorldWriter(driver, world_id).transaction(work)
        except IncompleteCommit as error:
            raise WriteOutcomeUnknown(
                "the question commit needs request reconciliation",
                commit_id=stamp.commit_id,
                change_set_id=stamp.change_set_id,
                request_id=command.request_id,
            ) from error
        except (Neo4jError, DriverError) as error:
            raise WriteFailed(
                "the question transaction did not commit", request_id=command.request_id
            ) from error
        from grove.writer import api as writer_api

        writer_api._after_commit()
        with hold(access, world_id, run_id, "release_answer", *refs, result.application_id):
            return result


def reconstruct_application(driver, world_id, run_id, caller, command, *, access=None):
    """Replay only: never cause a pending answer to apply."""
    _caller(caller, {"person", "ai_host"})
    with hold(
        access,
        world_id,
        run_id,
        "read_answer",
        command.principal_id,
        command.question_id,
        command.source.source_id,
    ):

        def work(tx):
            _ordinary(tx)
            _source(tx, command.source, command=command, caller=caller)
            row = _request(tx, command, caller)
            if row["answer_state"] != "applied":
                raise CleanupRefused("question_answer_pending")
            return _reconstruct(tx, row, command)

        return WorldWriter(driver, world_id).transaction(work)


def cancel_answer(driver, world_id, run_id, caller, command, *, access=None):
    _caller(caller, {"person", "ai_host"})
    with hold(access, world_id, run_id, "cancel_answer", command.principal_id, command.question_id):

        def work(tx):
            _ordinary(tx)
            _source(tx, command.source, command=command, caller=caller)
            row = _request(tx, command, caller)
            if row["answer_state"] == "applied":
                return _reconstruct(tx, row, command)
            if row["answer_state"] == "cancelled":
                return "cancelled"
            stamp = _stamp(world_id, run_id, caller, command.request_id)
            _change(
                tx,
                stamp,
                "Request",
                {**row, "status": "failed", "answer_state": "cancelled"},
                row,
                event="cancel_answer",
                question_id=command.question_id,
            )
            return "cancelled"

        return WorldWriter(driver, world_id).transaction(work)


def withdraw_question(
    driver,
    world_id,
    run_id,
    caller,
    question_id,
    revision,
    *,
    reason,
    outcomes,
    screener,
    access=None,
):
    _caller(caller, {"person", "cleanup"})
    with hold(access, world_id, run_id, "withdraw_question", question_id):

        def work(tx):
            _ordinary(tx)
            question = _one(tx, "Question", question_id)
            rows = _rows(tx, "QuestionBinding", question_id=question_id)
            if len(rows) != 1 or rows[0]["revision"] != revision or question["state"] != "open":
                raise CleanupRefused("question_binding_version_conflict")
            binding_row = rows[0]
            binding = decode_json(QuestionBindingV1, binding_row["binding"])
            work_rows = [_one(tx, "QuestionWork", wid) for wid in _work_ids(binding)]
            pending = [row for row in work_rows if not row.get("application_id")]
            if (
                type(outcomes) is not dict
                or set(outcomes) != {row["id"] for row in pending}
                or any(value not in {"cancel", "return_to_sources"} for value in outcomes.values())
            ):
                raise CleanupRefused("question_withdrawal_coverage")
            stamp = _stamp(world_id, run_id, caller)
            reason_id = _safe_text(tx, stamp, screener, reason)
            for row in pending:
                _change(
                    tx,
                    stamp,
                    "QuestionWork",
                    {
                        **row,
                        "state": "cancelled" if outcomes[row["id"]] == "cancel" else "pending",
                        "reason": reason_id,
                    },
                    row,
                    event="withdraw_work",
                    question_id=question_id,
                    reason=reason_id,
                )
            _change(
                tx,
                stamp,
                "Question",
                {**question, "state": "withdrawn", "withdraw_reason": reason_id},
                question,
                event="withdraw",
                question_id=question_id,
                reason=reason_id,
            )
            terminal = {key: value for key, value in binding_row.items() if key != "open_topic_key"}
            _change(
                tx,
                stamp,
                "QuestionBinding",
                {**terminal, "state": "withdrawn"},
                binding_row,
                event="withdraw",
                question_id=question_id,
                reason=reason_id,
            )
            return question_id

        return WorldWriter(driver, world_id).transaction(work)


def consume_question_intent(driver, world_id, caller, intent, *, access=None):
    """Retain every prospective obligation; no question or D10 effect is fabricated."""
    _caller(caller, {"cleanup"})
    if type(intent) is not CuratorQuestionIntentV1 or intent.world_id != world_id:
        raise CleanupRefused("question_intent_invalid")
    with hold(
        access,
        world_id,
        intent.run_id,
        "consume_question_intent",
        intent.execution_id,
        *(part.source.source_id for part in intent.parts),
    ):

        def work(tx):
            _ordinary(tx)
            _targets(tx, intent.targets)
            stamp = _stamp(world_id, intent.run_id, caller)
            for part in intent.parts:
                _source(tx, part.source)
                payload = _one(tx, "HistoryValue", part.payload_value_id)
                if (
                    payload.get("version") != part.payload_version
                    or payload.get("value") == "[forgotten]"
                ):
                    raise CleanupRefused("question_intent_payload_stale")
                old = _one(tx, "QuestionWork", part.obligation_id, optional=True)
                row = {
                    "id": part.obligation_id,
                    "group_id": intent.group_id,
                    "state": "pending",
                    "source_binding": canonical_json(part.source),
                    "disposition_id": part.disposition_id,
                    "intent_id": intent.intent_id,
                    "waiting_operation_id": part.waiting_operation_id,
                    "payload_value_id": part.payload_value_id,
                    "payload_version": part.payload_version,
                    "intent_binding": encode(
                        {
                            "execution_id": intent.execution_id,
                            "execution_version": intent.execution_version,
                            "request_id": intent.request_id,
                            "run_id": intent.run_id,
                            "disposition": part.disposition,
                        }
                    ),
                }
                if old is not None:
                    if any(old.get(key) != value for key, value in row.items() if key != "state"):
                        raise CleanupRefused("question_intent_replay_conflict")
                    continue
                _change(tx, stamp, "QuestionWork", row, event="intent")
            return tuple((part.disposition_id, part.obligation_id) for part in intent.parts)

        return WorldWriter(driver, world_id).transaction(work)
