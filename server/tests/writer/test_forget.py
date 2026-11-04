"""D14 integration: durable approval, redacted create values and both D3 restore entries."""

import json

import pytest

from grove.ids import new_id
from grove.store.d3_fingerprint import read_once
from grove.store.query import WorldQuery
from grove.writer.forget import forget, read_inventory
from grove.writer.forget_ledger import LedgerUnavailable, read_ledger
from grove.writer.restore import (
    load_snapshot,
    replay_ledger,
    restore,
    restore_loaded,
    take_snapshot,
)
from grove.writer.testing import plant_node
from tests.writer.helpers import apply, create_item, created_id, observation
from tests.writer.test_restore import purge_world_rows


def approve(driver, world, target):
    from grove.writer.forget_approvals import mint_approval

    return mint_approval(driver, world, "u_t_owner", target).token


def erase(driver, world, target, tmp_path):
    return forget(
        driver,
        world,
        target,
        actor="u_t_owner",
        approval_token=approve(driver, world, target),
        ledger_dir=tmp_path,
    )


def inventory(driver, world):
    return read_once(driver, world, read_inventory)


@pytest.fixture
def subject(driver, seeded, chat):
    world = seeded["world_id"]
    receipt = apply(
        driver,
        world,
        [create_item("place", "Cobalt orchard", "e1", summary="Quiet trees near the hill")],
        [observation(chat, "e1", 1)],
    )
    return world, created_id(receipt)


def test_item_forget_removes_alias_position_and_history_text(driver, subject, tmp_path):
    world, item_id = subject
    alias = plant_node(
        driver,
        world,
        "Alias",
        target_type="item",
        target_id=item_id,
        phrase="Cobalt orchard alternate",
        normalised_phrase="cobalt orchard alternate",
    )
    position = plant_node(driver, world, "MapPosition", item_id=item_id, x=1.0, y=2.0)
    before = inventory(driver, world)
    receipt = erase(driver, world, {"type": "item", "item_id": item_id}, tmp_path)
    after = inventory(driver, world)
    assert after["change_entries"] == before["change_entries"]
    assert item_id not in {r["id"] for r in after["items"]}
    assert alias["id"] not in {r["id"] for r in after["aliases"]}
    assert position["id"] not in {r["id"] for r in after["map_positions"]}
    assert "Cobalt orchard" not in json.dumps(after, default=str)
    assert "Cobalt orchard" not in json.dumps(receipt)
    assert "Cobalt orchard" not in json.dumps(read_ledger(tmp_path, world))


def test_field_forget_redacts_only_one_create_column(driver, subject, tmp_path):
    world, item_id = subject
    before = inventory(driver, world)
    erase(driver, world, {"type": "field_value", "item_id": item_id, "field": "title"}, tmp_path)
    after = inventory(driver, world)
    row = next(r for r in after["items"] if r["id"] == item_id)
    assert row["title"] == "[forgotten]"
    assert row["summary"] == "Quiet trees near the hill"
    assert before["change_entries"] == after["change_entries"]
    assert "Cobalt orchard" not in json.dumps(after, default=str)


def test_named_value_reaches_indirect_value_and_run_record(driver, subject, tmp_path):
    world, _ = subject
    value = plant_node(driver, world, "HistoryValue", value="Cobalt orchard is the location")
    question = plant_node(driver, world, "Question", ask=value["id"])
    plant_node(
        driver, world, "RunRecord", proposed_operations=json.dumps({"body": "Cobalt orchard"})
    )
    erase(driver, world, {"type": "named_value", "value": "Cobalt orchard"}, tmp_path)
    after = inventory(driver, world)
    assert "Cobalt orchard" not in json.dumps(after, default=str)
    assert next(r for r in after["questions"] if r["id"] == question["id"])["ask"] == value["id"]
    assert (
        next(r for r in after["history_values"] if r["id"] == value["id"])["value"] == "[forgotten]"
    )


@pytest.mark.parametrize("preloaded", [False, True])
def test_old_snapshot_with_latest_ledger_cannot_restore_forgotten_value(
    driver, subject, tmp_path, preloaded
):
    world, item_id = subject
    snapshot = take_snapshot(driver, world)
    erase(driver, world, {"type": "field_value", "item_id": item_id, "field": "title"}, tmp_path)
    ledger = read_ledger(tmp_path, world)
    purge_world_rows(driver, world)
    if preloaded:
        load_snapshot(driver, world, snapshot)
        result = restore_loaded(driver, world, snapshot, ledger)
    else:
        result = restore(driver, world, snapshot, ledger)
    after = inventory(driver, world)
    assert "Cobalt orchard" not in json.dumps(after, default=str)
    assert (
        next(r for r in after["items"] if r["id"] == item_id)["summary"]
        == "Quiet trees near the hill"
    )
    assert result["entries"] == snapshot["fingerprint"]["entries"]


def test_replay_is_idempotent(driver, subject, tmp_path):
    world, item_id = subject
    erase(driver, world, {"type": "item", "item_id": item_id}, tmp_path)
    ledger = read_ledger(tmp_path, world)
    before = inventory(driver, world)
    replay_ledger(driver, world, ledger)
    once = inventory(driver, world)
    replay_ledger(driver, world, ledger)
    assert once == inventory(driver, world)
    assert before == once


def test_invalid_later_ledger_row_prevents_all_replay(driver, subject, tmp_path):
    world, item_id = subject
    snapshot = take_snapshot(driver, world)
    erase(driver, world, {"type": "item", "item_id": item_id}, tmp_path)
    ledger = read_ledger(tmp_path, world)
    purge_world_rows(driver, world)
    load_snapshot(driver, world, snapshot)
    before = inventory(driver, world)
    ledger.append({**ledger[0], "id": new_id(world), "plan": "{}"})
    with pytest.raises(ValueError):
        replay_ledger(driver, world, ledger)
    assert inventory(driver, world) == before


def test_no_durable_ledger_means_no_erasure_or_used_approval(driver, subject, monkeypatch):
    world, item_id = subject
    target = {"type": "item", "item_id": item_id}
    token = approve(driver, world, target)
    before = inventory(driver, world)
    monkeypatch.delenv("GROVE_FORGET_LEDGER_DIR", raising=False)
    with pytest.raises(LedgerUnavailable):
        forget(driver, world, target, actor="u_t_owner", approval_token=token)
    assert inventory(driver, world) == before


def test_corrupt_ledger_refuses_next_erasure(driver, subject, tmp_path):
    world, item_id = subject
    erase(driver, world, {"type": "field_value", "item_id": item_id, "field": "title"}, tmp_path)
    ledger_file = next(tmp_path.glob("*.json"))
    ledger_file.write_text("{}")
    target = {"type": "item", "item_id": item_id}
    token = approve(driver, world, target)
    before = inventory(driver, world)
    with pytest.raises(LedgerUnavailable):
        forget(driver, world, target, actor="u_t_owner", approval_token=token, ledger_dir=tmp_path)
    assert inventory(driver, world) == before


def test_ledger_files_are_private(driver, subject, tmp_path):
    world, item_id = subject
    erase(driver, world, {"type": "item", "item_id": item_id}, tmp_path)
    assert all(p.stat().st_mode & 0o077 == 0 for p in tmp_path.iterdir())


def test_wrong_world_target_changes_neither_world(driver, subject, make_world, tmp_path):
    from grove.writer.errors import InvalidRecord
    from grove.writer.worlds import create_world

    world, item_id = subject
    other = make_world()
    create_world(driver, other, owner_user_id="u_t_owner")
    before = inventory(driver, world)
    with pytest.raises(InvalidRecord):
        erase(driver, other, {"type": "item", "item_id": item_id}, tmp_path)
    assert inventory(driver, world) == before
    assert WorldQuery(driver, other).read("MATCH (i:Item) RETURN i") == []
