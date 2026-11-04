"""Inactive transaction-local erasure for exact legacy/v2 ledger extensions.

The caller owns the publication lease and all current owner checks. This module
never reads or writes a file, creates an identity, or selects a clock instant.
The existing compiler does not cover the later Learner/question families, so
those occupied families refuse even if an external owner claims otherwise.
"""

import copy
import json
import math
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256

from grove.authority.codec import DecoderBudget, record_metrics
from grove.authority.legacy import MAX_FILE, MAX_ROW, decode_legacy, legacy_row, legacy_row_span
from grove.authority.publication import PublicationRefused
from grove.authority.records import SCHEMA_NAMES, SCHEMAS, TYPES, check_scalar
from grove.store.replay import rebuild
from grove.writer.forget import read_inventory, validate_ledger_rows, write_inventory
from grove.writer.forget_ledger import _json
from grove.writer.forget_plan import WORLD_TABLES, transform, validate_plan

_CONTROL_TABLES = frozenset(
    {"authority_projections", "authority_operations", "authority_approval_uses"}
)
_UNSUPPORTED_TABLES = frozenset(
    {
        "learner_work",
        "learner_executions",
        "learner_calls",
        "learner_dispositions",
        "learner_completions",
        "learner_episode_progress",
        "learner_handoffs",
        "question_bindings",
        "question_work",
        "question_applications",
        "question_events",
        "curator_legwork",
        "curator_escalations",
        "curator_run_order",
    }
)
_PROTECTED = _CONTROL_TABLES | {"change_entries", "forget_ledger", "forget_approvals"}
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024
_MAX_INVENTORY_ROWS = 32768


def _refuse(code="conflict"):
    raise PublicationRefused(code, "outcome") from None


def check_binding(binding, tx):
    # Deferred only to keep this two-file component importable during integration.
    from grove.writer.authority_records import check_binding as check

    return check(binding, tx)


def _detach(record):
    name = SCHEMA_NAMES.get(type(record))
    if name is None:
        return record
    return TYPES[name](**{key: _detach(getattr(record, key)) for key, _ in SCHEMAS[name]})


def _plain(value, depth=0, budget=None):
    budget = [_MAX_INVENTORY_BYTES] if budget is None else budget
    budget[0] -= 64 + (6 * len(value) if type(value) is str else 0)
    if budget[0] < 0 or depth > 40:
        _refuse("capacity")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _refuse()
        return value
    if type(value) is str:
        if len(value) > _MAX_INVENTORY_BYTES:
            _refuse("capacity")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            _refuse()
        return {"$instant": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if type(value) in (list, tuple):
        if len(value) > _MAX_INVENTORY_ROWS:
            _refuse("capacity")
        return [_plain(part, depth + 1, budget) for part in value]
    if type(value) is dict:
        if len(value) > 4096 or any(type(key) is not str or len(key) > 1024 for key in value):
            _refuse("capacity")
        budget[0] -= sum(6 * len(key) for key in value)
        if budget[0] < 0:
            _refuse("capacity")
        return {key: _plain(part, depth + 1, budget) for key, part in value.items()}
    _refuse()


def _canonical(value):
    raw = json.dumps(_plain(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    if len(raw) > _MAX_INVENTORY_BYTES:
        _refuse("capacity")
    return raw.encode("ascii")


def _inventory_digest(inventory):
    """Compare every domain byte, not the text-free D3 fingerprint."""
    if type(inventory) is not dict or set(inventory) - set(WORLD_TABLES) - _CONTROL_TABLES:
        _refuse()
    digest = sha256(b"grove.authority.legacy-base.v1\x00")
    size = count = 0
    for name in sorted(set(WORLD_TABLES) - _CONTROL_TABLES):
        rows = inventory.get(name, [])
        if type(rows) is not list:
            _refuse()
        count += len(rows)
        if count > _MAX_INVENTORY_ROWS:
            _refuse("capacity")
        encoded = []
        for row in rows:
            raw = _canonical(row)
            size += len(raw)
            if size > _MAX_INVENTORY_BYTES:
                _refuse("capacity")
            encoded.append(raw)
        digest.update(name.encode("ascii") + b"\x00")
        for raw in sorted(encoded):
            digest.update(str(len(raw)).encode("ascii") + b":" + raw)
        digest.update(b"\x00")
    return digest.hexdigest()


def _closure(inventory, world_id):
    for name, rows in inventory.items():
        if name in _UNSUPPORTED_TABLES and rows:
            _refuse("adapter_missing")
        for row in rows:
            if row.get("world_id", world_id) != world_id:
                _refuse()
    for request in inventory.get("requests", []):
        if request.get("protocol") in {"question_answer_v1", "learner_execution_v1"} or any(
            request.get(key) is not None
            for key in ("learner_work_id", "learner_completion_id", "application_id")
        ):
            _refuse("adapter_missing")


def _ledger(binding):
    return TYPES["Ledger"](
        format=binding.format,
        count=binding.count,
        file_digest=binding.file_digest,
        rows_digest=binding.rows_digest,
    )


def _native_row(world_id, raw):
    original = json.loads(raw) if type(raw) is bytes else copy.deepcopy(raw)
    row = validate_ledger_rows(world_id, [original])[0]
    # Validation normalizes plans. Preserve an already stored plan string exactly.
    if type(original.get("plan")) is str:
        row["plan"] = original["plan"]
    if type(row.get("receipt")) is dict:
        row["receipt"] = json.dumps(row["receipt"], sort_keys=True, separators=(",", ":"))
    row["world_id"] = world_id
    return row


def _row_comparison(row):
    return _canonical(
        {key: value for key, value in row.items() if value is not None and value != []}
    )


def _references(inventory, row):
    if "plan" not in row:
        table = "items" if row["target_type"] == "item" else "history_values"
        known = {part["id"] for part in inventory.get(table, [])}
        named = set(row.get("internal_ids") or [])
        if not named or not named <= known:
            _refuse("adapter_missing")
        if table == "history_values" and any(
            entry.get("field") == "row" and entry.get("new_value_id") in named
            for entry in inventory.get("change_entries", [])
        ):
            _refuse()
        return
    plan = json.loads(row["plan"])
    validate_plan(plan)
    references = {
        "sources": set(plan["source_ids"])
        | set(plan["source_exact"])
        | {part["source_id"] for part in plan["source_spans"]},
        "history_values": set(plan["mark_values"])
        | {part["id"] for part in plan["history_fields"] + plan["evidence_values"]},
        "items": {part["id"] for part in plan["item_fields"]},
        "evidence": set(plan["end_evidence"]),
    }
    for name, needed in references.items():
        if not needed <= {part["id"] for part in inventory.get(name, [])}:
            _refuse("adapter_missing")
    for part in plan["delete"]:
        if part["table"] in _PROTECTED | _UNSUPPORTED_TABLES:
            _refuse("adapter_missing")
        if (
            sum(
                all(str(row.get(key)) == value for key, value in part["key"].items())
                for row in inventory.get(part["table"], [])
            )
            != 1
        ):
            _refuse("adapter_missing")


def _transform_domain(inventory, plan, at):
    # Control rows are never inputs to a text selector or its generic metadata walk.
    domain = {name: rows for name, rows in inventory.items() if name not in _CONTROL_TABLES}
    after, counts = transform(domain, plan, at)
    for name in _CONTROL_TABLES:
        if name in inventory:
            after[name] = copy.deepcopy(inventory[name])
    return after, counts


@dataclass(frozen=True, slots=True)
class LegacyPlanEvidence:
    world_id: str
    before_ledger: object
    after_ledger: object
    raw_row: bytes = field(repr=False)
    effect: object = field(repr=False)
    base_digest: str
    at: str


@dataclass(frozen=True, slots=True)
class LegacyApplyResult:
    row_id: str
    plan_version: str
    before_digest: str
    after_digest: str
    counts: tuple


def _check_evidence(evidence, binding):
    if type(evidence) is not LegacyPlanEvidence or evidence.world_id != binding.world_id:
        _refuse("binding")
    check_scalar("AT", evidence.at)
    if evidence.at != binding.at:
        _refuse("binding")
    check_scalar("H", evidence.base_digest)
    if type(evidence.raw_row) is not bytes or not 0 < len(evidence.raw_row) <= MAX_ROW:
        _refuse("capacity")
    if type(evidence.effect) is not TYPES["ForgetApply"]:
        _refuse("binding")
    record_metrics(evidence.effect)
    decoded = json.loads(evidence.raw_row)
    row_digest = sha256(_json(decoded).encode("ascii")).hexdigest()
    envelope = (
        b'{"format":"grove.forget-ledger.v1","world_id":'
        + json.dumps(evidence.world_id).encode("ascii")
        + b',"count":1,"rows":['
        + evidence.raw_row
        + b'],"sha256":"'
        + sha256(("[" + _json(decoded) + "]").encode("ascii")).hexdigest().encode("ascii")
        + b'"}'
    )
    with closing(DecoderBudget()) as budget:
        held = decode_legacy(envelope, world_id=evidence.world_id, budget=budget)
        reference = legacy_row(held, 0)
        if row_digest != evidence.effect.row.row_digest or any(
            getattr(reference, key) != getattr(evidence.effect.row, key)
            for key in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
        ):
            _refuse("binding")
    if (evidence.effect.before_ledger, evidence.effect.after_ledger) != (
        evidence.before_ledger,
        evidence.after_ledger,
    ):
        _refuse("binding")


def prepare_legacy_forget(tx, binding, before_raw: bytes, after_raw: bytes, effect):
    """Admit the exact extension and current inventory without any mutation."""
    owner = check_binding(binding, tx)
    try:
        check_scalar("AT", binding.at)
        if type(effect) is not TYPES["ForgetApply"]:
            _refuse("binding")
        record_metrics(effect)
        if effect.row.plan_version not in {"legacy", "v2"}:
            _refuse("adapter_missing")
        if any(
            type(raw) is not bytes or not 0 < len(raw) <= MAX_FILE
            for raw in (before_raw, after_raw)
        ):
            _refuse("capacity")
        inventory = read_inventory(tx)
        _closure(inventory, binding.world_id)
        owner._validate_legacy_in_tx(tx, effect, inventory)
        existing = {}
        for row in inventory.get("forget_ledger", []):
            if row["id"] in existing:
                _refuse()
            existing[row["id"]] = _row_comparison(_native_row(binding.world_id, row))
        spans = []
        with closing(DecoderBudget()) as budget:
            before = decode_legacy(before_raw, world_id=binding.world_id, budget=budget)
            before_ledger = _ledger(before.binding)
            if before_ledger != effect.before_ledger or len(existing) != before.binding.count:
                _refuse()
            for index in range(before.binding.count):
                start, end = legacy_row_span(before, index)
                spans.append((start, end))
                row = _native_row(binding.world_id, before_raw[start:end])
                if existing.get(row["id"]) != _row_comparison(row):
                    _refuse()
        with closing(DecoderBudget()) as budget:
            after = decode_legacy(after_raw, world_id=binding.world_id, budget=budget)
            after_ledger = _ledger(after.binding)
            if after_ledger != effect.after_ledger or after.binding.count != len(spans) + 1:
                _refuse()
            for index, (start, end) in enumerate(spans):
                new_start, new_end = legacy_row_span(after, index)
                if memoryview(before_raw)[start:end] != memoryview(after_raw)[new_start:new_end]:
                    _refuse()
            reference = legacy_row(after, len(spans))
            if any(
                getattr(reference, key) != getattr(effect.row, key)
                for key in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
            ):
                _refuse()
            start, end = legacy_row_span(after, len(spans))
            raw_row = bytes(memoryview(after_raw)[start:end])
        row = _native_row(binding.world_id, raw_row)
        if row["id"] in existing:
            _refuse()
        _references(inventory, row)
        evidence = LegacyPlanEvidence(
            binding.world_id,
            _detach(before_ledger),
            _detach(after_ledger),
            raw_row,
            _detach(effect),
            _inventory_digest(inventory),
            binding.at,
        )
        if "plan" in row:
            after, _ = _transform_domain(inventory, json.loads(row["plan"]), row["approved_at"])
            validate_legacy_result(inventory, after, evidence)
        else:
            rebuild(
                inventory["change_entries"],
                {part["id"]: part["value"] for part in inventory["history_values"]},
            )
        return evidence
    except PublicationRefused:
        raise
    except Exception:
        _refuse()


def validate_legacy_result(before, after, evidence):
    """Reject changed history/control facts and invalid D3 reconstruction."""
    try:
        _closure(after, evidence.world_id)
        for name in _PROTECTED:
            if before.get(name, []) != after.get(name, []):
                _refuse()
        rebuild(
            after["change_entries"], {row["id"]: row["value"] for row in after["history_values"]}
        )
        return _inventory_digest(after)
    except PublicationRefused:
        raise
    except Exception:
        _refuse()


def apply_legacy_forget_in_tx(tx, binding, evidence):
    """Apply only within the caller's terminal effect/approval/outcome transaction."""
    owner = check_binding(binding, tx)
    try:
        _check_evidence(evidence, binding)
        before = read_inventory(tx)
        _closure(before, binding.world_id)
        owner._validate_legacy_in_tx(tx, evidence.effect, before)
        if _inventory_digest(before) != evidence.base_digest:
            _refuse()
        row = _native_row(binding.world_id, evidence.raw_row)
        _references(before, row)
        if any(prior["id"] == row["id"] for prior in before.get("forget_ledger", [])):
            _refuse()
        if "plan" in row:
            after, counts = _transform_domain(before, json.loads(row["plan"]), row["approved_at"])
            after_digest = validate_legacy_result(before, after, evidence)
            write_inventory(tx, before, after)
            observed = read_inventory(tx)
            if _inventory_digest(observed) != after_digest:
                _refuse()
            validate_legacy_result(before, observed, evidence)
        else:
            from grove.writer.restore import _apply_legacy

            execution = dict(row)
            if execution.get("approved_at") is None:
                execution["approved_at"] = datetime.fromisoformat(evidence.at)
            _apply_legacy(tx, [execution])
            after = read_inventory(tx)
            after_digest = validate_legacy_result(before, after, evidence)
            counts = {"legacy": len(execution.get("internal_ids") or [])}
        tx.run(
            "CREATE (n:ForgetLedger) SET n += $row",
            row={key: value for key, value in row.items() if key != "world_id"},
        )
        return LegacyApplyResult(
            row["id"],
            evidence.effect.row.plan_version,
            evidence.base_digest,
            after_digest,
            tuple(sorted(counts.items())),
        )
    except PublicationRefused:
        raise
    except Exception:
        _refuse()
