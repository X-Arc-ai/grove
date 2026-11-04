"""Synthetic and separately enabled native controls for inactive legacy erasure."""

import copy
import json
import os
from contextlib import closing
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime
from hashlib import sha256

import pytest

from grove.authority.codec import DecoderBudget
from grove.authority.legacy import decode_legacy, legacy_row
from grove.authority.publication import PublicationRefused
from grove.authority.records import TYPES
from grove.writer import authority_forget as subject
from grove.writer.forget_ledger import _json
from grove.writer.forget_plan import WORLD_TABLES, build_plan

WORLD = "t_forget_00000000-0000-4000-8000-000000000001"
AT = "2026-11-04T12:30:00.123456Z"
VALUE = "00000000-0000-4000-8000-000000000010"
ROW = "00000000-0000-4000-8000-000000000020"


@dataclass(frozen=True)
class SyntheticBinding:
    world_id: str = WORLD
    at: str = AT


class SyntheticOwner:
    def __init__(self):
        self.calls = 0
        self.refuse = False

    def _validate_legacy_in_tx(self, tx, effect, inventory):
        self.calls += 1
        if self.refuse:
            raise PublicationRefused("adapter_missing")


class SyntheticTx:
    def __init__(self, inventory, world_id=WORLD):
        self.inventory = copy.deepcopy(inventory)
        self.world_id = world_id
        self.writes = []

    def run(self, query, **params):
        assert query == "CREATE (n:ForgetLedger) SET n += $row"
        self.writes.append("ledger")
        self.inventory["forget_ledger"].append({**params["row"], "world_id": self.world_id})
        return []


def inventory(world=WORLD):
    result = {name: [] for name in WORLD_TABLES}
    result["history_values"] = [
        {"id": VALUE, "world_id": world, "value": "selected secret", "version": 1},
        {"id": "kept-value", "world_id": world, "value": "retained sentence", "version": 1},
    ]
    return result


def payload(rows, world=WORLD, *, spans=None):
    raw_rows = b",".join(_json(row).encode() for row in rows) if spans is None else b",".join(spans)
    return (
        b'{"format":"grove.forget-ledger.v1","world_id":'
        + json.dumps(world).encode()
        + b',"count":'
        + str(len(rows)).encode()
        + b',"rows":['
        + raw_rows
        + b'],"sha256":"'
        + sha256(_json(rows).encode()).hexdigest().encode()
        + b'"}'
    )


def effect_for(before_raw, after_raw, world=WORLD):
    with closing(DecoderBudget()) as budget:
        old = decode_legacy(before_raw, world_id=world, budget=budget)
        before = subject._ledger(old.binding)
    with closing(DecoderBudget()) as budget:
        new = decode_legacy(after_raw, world_id=world, budget=budget)
        after = subject._ledger(new.binding)
        last = legacy_row(new, new.binding.count - 1)
        row = TYPES["LedgerRowRef"](
            **{
                field: getattr(last, field)
                for field in ("id", "plan_version", "row_digest", "plan_digest", "extension_digest")
            }
        )
    return TYPES["ForgetApply"](
        effect_id="00000000-0000-4000-8000-000000000030",
        kind="forget_apply",
        row=row,
        before_ledger=before,
        after_ledger=after,
        expected_revision=0,
        revision=1,
    )


def make_extension(inv, *, legacy=False, prior=(), world=WORLD):
    row = {
        "id": ROW,
        "world_id": world,
        "target_type": "field_value" if legacy else "named_value",
        "internal_ids": [VALUE],
        "approved_at": AT,
        "requested_by": "owner",
    }
    if not legacy:
        row["plan"] = _json(build_plan(inv, {"type": "named_value", "value": "selected secret"}))
    before, after = payload(list(prior), world), payload([*prior, row], world)
    return row, before, after, effect_for(before, after, world)


@pytest.fixture
def synthetic(monkeypatch):
    inv = inventory()
    tx, owner, binding = SyntheticTx(inv), SyntheticOwner(), SyntheticBinding()

    def check(given, current):
        if given is not binding or current is not tx:
            raise PublicationRefused("binding")
        return owner

    def write(current, before, after):
        assert current is tx and current.inventory == before
        current.writes.append("domain")
        current.inventory = copy.deepcopy(after)

    monkeypatch.setattr(subject, "check_binding", check)
    monkeypatch.setattr(subject, "read_inventory", lambda current: copy.deepcopy(current.inventory))
    monkeypatch.setattr(subject, "write_inventory", write)
    return tx, owner, binding


def prepare(synthetic, **options):
    tx, owner, binding = synthetic
    row, before, after, effect = make_extension(tx.inventory, **options)
    evidence = subject.prepare_legacy_forget(tx, binding, before, after, effect)
    return evidence, row, before, after


def test_v2_erases_selected_value_and_preserves_other_value(synthetic):
    tx, owner, binding = synthetic
    evidence, row, before, after = prepare(synthetic)
    assert tx.writes == []
    result = subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert tx.writes == ["domain", "ledger"]
    assert tx.inventory["history_values"][0]["value"] == "[forgotten]"
    assert tx.inventory["history_values"][1]["value"] == "retained sentence"
    assert tx.inventory["forget_ledger"][0]["plan"] == row["plan"]
    assert result.row_id == ROW and result.plan_version == "v2"
    assert owner.calls == 2
    assert payload([row]) == after and payload([]) == before
    assert "selected secret" not in repr(evidence)
    with pytest.raises(FrozenInstanceError):
        evidence.world_id = "changed"


def test_base_mutation_of_text_refuses_even_when_d3_positions_do_not_change(synthetic):
    tx, _, binding = synthetic
    evidence, *_ = prepare(synthetic)
    tx.inventory["history_values"][1]["value"] = "changed sentence"
    with pytest.raises(PublicationRefused):
        subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert tx.writes == []


def test_authority_metadata_change_does_not_invalidate_domain_base(synthetic):
    tx, _, binding = synthetic
    for name in subject._CONTROL_TABLES:
        tx.inventory[name] = [{"world_id": WORLD, "id": "control", "fence": 1}]
    evidence, *_ = prepare(synthetic)
    for name in subject._CONTROL_TABLES:
        tx.inventory[name][0]["fence"] = 2
    subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert all(tx.inventory[name][0]["fence"] == 2 for name in subject._CONTROL_TABLES)


def test_changed_owned_row_or_time_cannot_reuse_evidence(synthetic):
    tx, _, binding = synthetic
    evidence, row, *_ = prepare(synthetic)
    row["plan"] = _json(
        build_plan(tx.inventory, {"type": "named_value", "value": "retained sentence"})
    )
    for altered in (
        replace(evidence, raw_row=_json(row).encode()),
        replace(evidence, at="2026-11-05T12:30:00.123456Z"),
    ):
        with pytest.raises(PublicationRefused):
            subject.apply_legacy_forget_in_tx(tx, binding, altered)
    assert tx.writes == []


def test_missing_owner_lineage_refuses_before_plan_or_apply(synthetic):
    tx, owner, binding = synthetic
    evidence, *_ = prepare(synthetic)
    owner.refuse = True
    with pytest.raises(PublicationRefused):
        prepare(synthetic)
    with pytest.raises(PublicationRefused):
        subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert tx.writes == []


@pytest.mark.parametrize("name", sorted(subject._UNSUPPORTED_TABLES))
def test_unknown_learner_or_question_closure_refuses_application(synthetic, name):
    tx, _, binding = synthetic
    evidence, *_ = prepare(synthetic)
    tx.inventory[name] = [{"id": "occupied", "world_id": WORLD}]
    with pytest.raises(PublicationRefused):
        prepare(synthetic)
    with pytest.raises(PublicationRefused):
        subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert tx.writes == []


@pytest.mark.parametrize("protocol", ["learner_execution_v1", "question_answer_v1"])
def test_orphaned_protected_request_is_not_empty_closure(synthetic, protocol):
    tx, _, _ = synthetic
    tx.inventory["requests"] = [{"id": "request", "world_id": WORLD, "protocol": protocol}]
    with pytest.raises(PublicationRefused):
        prepare(synthetic)
    assert tx.writes == []


def test_missing_source_reference_refuses_before_mutation(synthetic):
    tx, _, binding = synthetic
    row, before, _, _ = make_extension(tx.inventory)
    plan = json.loads(row["plan"])
    plan["source_exact"] = ["absent-source"]
    row["plan"] = _json(plan)
    after = payload([row])
    with pytest.raises(PublicationRefused):
        subject.prepare_legacy_forget(tx, binding, before, after, effect_for(before, after))
    assert tx.writes == []


def test_legacy_whole_row_value_refuses_before_any_write(synthetic):
    tx, _, binding = synthetic
    tx.inventory["change_entries"] = [{"field": "row", "new_value_id": VALUE}]
    row, before, after, effect = make_extension(tx.inventory, legacy=True)
    with pytest.raises(PublicationRefused):
        subject.prepare_legacy_forget(tx, binding, before, after, effect)
    assert tx.writes == []


def test_legacy_execution_uses_fixed_time_without_rewriting_external_row(synthetic, monkeypatch):
    from grove.writer import restore

    tx, _, binding = synthetic
    row, before, _, _ = make_extension(tx.inventory, legacy=True)
    row.pop("approved_at")
    after = payload([row])
    evidence = subject.prepare_legacy_forget(tx, binding, before, after, effect_for(before, after))
    observed = []

    def legacy(current, rows):
        observed.extend(rows)
        current.writes.append("legacy")
        current.inventory["history_values"][0]["value"] = "[forgotten]"

    monkeypatch.setattr(restore, "_apply_legacy", legacy)
    subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert observed[0]["approved_at"] == datetime.fromisoformat(AT)
    assert "approved_at" not in tx.inventory["forget_ledger"][0]
    assert json.loads(evidence.raw_row) == row
    assert tx.writes == ["legacy", "ledger"]


def test_exact_prior_row_spans_are_required(synthetic):
    tx, _, binding = synthetic
    prior = {"id": "prior", "target_type": "named_value"}
    tx.inventory["forget_ledger"] = [{**prior, "world_id": WORLD}]
    row, _, _, _ = make_extension(tx.inventory, prior=[prior])
    old_span = json.dumps(prior, indent=2).encode()
    before = payload([prior], spans=[old_span])
    after = payload([prior, row], spans=[old_span, _json(row).encode()])
    evidence = subject.prepare_legacy_forget(tx, binding, before, after, effect_for(before, after))
    assert evidence.raw_row == _json(row).encode()
    changed = payload([prior, row])
    with pytest.raises(PublicationRefused):
        subject.prepare_legacy_forget(tx, binding, before, changed, effect_for(before, changed))
    assert tx.writes == []


def test_existing_database_ledger_row_without_terminal_outcome_refuses(synthetic):
    tx, _, binding = synthetic
    evidence, row, *_ = prepare(synthetic)
    tx.inventory["forget_ledger"].append(subject._native_row(WORLD, row))
    with pytest.raises(PublicationRefused):
        subject.apply_legacy_forget_in_tx(tx, binding, evidence)
    assert tx.writes == []


@pytest.mark.parametrize("name", sorted(subject._PROTECTED))
def test_erasure_preserves_outcome_fence_approval_use_and_history_facts(synthetic, name):
    tx, _, _ = synthetic
    evidence, *_ = prepare(synthetic)
    before = copy.deepcopy(tx.inventory)
    after = copy.deepcopy(before)
    after.setdefault(name, []).append({"id": "changed", "world_id": WORLD})
    with pytest.raises(PublicationRefused):
        subject.validate_legacy_result(before, after, evidence)


def test_invalid_d3_reconstruction_refuses_before_write(synthetic):
    tx, _, _ = synthetic
    tx.inventory["change_entries"] = [{"sequence": 1, "entity_type": "unknown", "entity_id": "x"}]
    with pytest.raises(PublicationRefused):
        prepare(synthetic)
    assert tx.writes == []


def test_foreign_binding_refuses_before_inventory_read(synthetic, monkeypatch):
    tx, _, binding = synthetic
    row, before, after, effect = make_extension(tx.inventory)
    monkeypatch.setattr(subject, "read_inventory", lambda _: pytest.fail("inventory read"))
    with pytest.raises(PublicationRefused):
        subject.prepare_legacy_forget(tx, replace(binding, world_id="other"), before, after, effect)


@pytest.mark.skipif(
    os.environ.get("GROVE_AUTHORITY_NATIVE") != "1", reason="explicit native control"
)
@pytest.mark.parametrize("legacy", [False, True])
def test_native_legacy_v2_row_and_d3_history_remain_exact(
    driver, seeded, chat, monkeypatch, legacy
):
    from grove.store.d3_fingerprint import read_once
    from grove.writer.forget import read_inventory
    from grove.writer.tx import WorldWriter
    from tests.writer.helpers import apply, create_item, created_id, observation

    world = seeded["world_id"]
    receipt = apply(
        driver, world, [create_item("place", "Selected orchard", "e")], [observation(chat, "e", 1)]
    )
    item_id = created_id(receipt)
    before_inventory = read_once(driver, world, read_inventory)
    binding, owner = SyntheticBinding(world), SyntheticOwner()
    monkeypatch.setattr(
        subject,
        "check_binding",
        lambda bound, tx: (
            owner if bound is binding and tx.world_id == world else pytest.fail("binding")
        ),
    )
    row = {
        "id": ROW,
        "world_id": world,
        "target_type": "item",
        "internal_ids": [item_id],
        "approved_at": AT,
        "requested_by": "owner",
    }
    if not legacy:
        row["plan"] = _json(build_plan(before_inventory, {"type": "item", "item_id": item_id}))
    before_raw, after_raw = payload([], world), payload([row], world)
    effect = effect_for(before_raw, after_raw, world)
    writer = WorldWriter(driver, world)
    evidence = writer.transaction(
        lambda tx: subject.prepare_legacy_forget(tx, binding, before_raw, after_raw, effect)
    )
    writer.transaction(lambda tx: subject.apply_legacy_forget_in_tx(tx, binding, evidence))
    after_inventory = read_once(driver, world, read_inventory)
    assert after_inventory["change_entries"] == before_inventory["change_entries"]
    assert item_id not in {item["id"] for item in after_inventory["items"]}
    assert after_inventory["forget_ledger"][0]["id"] == ROW
    assert json.loads(evidence.raw_row) == json.loads(_json(row))
