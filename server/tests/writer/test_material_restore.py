"""Material snapshots refuse malformed families and replay the latest real erasure."""

import copy

import pytest

from grove.store.d3_fingerprint import read_once
from grove.store.material_snapshot import validate_material_snapshot
from grove.store.query import WorldQuery
from grove.views.session import token_hash
from grove.writer.forget import forget, read_inventory
from grove.writer.forget_ledger import read_ledger
from grove.writer.restore import restore, take_snapshot
from tests.writer import test_materials
from tests.writer.test_material_forget import (
    approve_material,
    assert_material_counts,
    erase_source,
    source_erasure_case,
)

material_world = test_materials.material_world
write_note = test_materials.write_note


def test_material_snapshot_validation_and_latest_restore(driver, material_world):
    session, cookie, ledger = material_world
    item = write_note(driver, material_world)["id"]
    inventory = read_once(driver, session.world_id, read_inventory)
    validate_material_snapshot(inventory)
    bad = copy.deepcopy(inventory)
    bad["material_revisions"][0]["source_ids"] = [bad["material_revisions"][0]["id"]]
    with pytest.raises(ValueError):
        validate_material_snapshot(bad)
    snapshot = take_snapshot(driver, session.world_id)
    target, approval = approve_material(driver, material_world, item)
    forget(
        driver,
        session.world_id,
        target,
        approval_token=approval,
        actor=session.user_id,
        material_session=session,
        session_hash=token_hash(cookie),
        ledger_dir=ledger,
    )
    from tests.writer.test_restore import purge_world_rows

    purge_world_rows(driver, session.world_id)
    restore(driver, session.world_id, snapshot, read_ledger(ledger, session.world_id))
    rows = WorldQuery(driver, session.world_id).read(
        "MATCH (m:MaterialRevision) RETURN m.state AS state"
    )
    assert rows and all(r["state"] == "deleted" for r in rows)
    assert all(
        r["s"].get("forget_state") == "forgotten"
        for r in WorldQuery(driver, session.world_id).read("MATCH (s:Source) RETURN s")
    )


@pytest.mark.parametrize(
    "kind", ["file", "multi_file", "edited_old", "edited_current", "message", "title"]
)
@pytest.mark.parametrize("snapshot_after", [False, True])
def test_partial_material_erasure_restores_with_latest_ledger(
    driver, material_world, kind, snapshot_after
):
    from tests.writer.test_restore import purge_world_rows

    world = material_world[0].world_id
    _, target, _ = source_erasure_case(driver, material_world, kind)
    earlier = take_snapshot(driver, world)
    erase_source(driver, material_world, target)
    expected = read_once(driver, world, read_inventory)
    snapshot = take_snapshot(driver, world) if snapshot_after else earlier
    ledger = read_ledger(material_world[2], world)
    purge_world_rows(driver, world)
    result = restore(driver, world, snapshot, ledger)
    assert result["fingerprint"]
    actual = read_once(driver, world, read_inventory)
    assert sorted(actual["sources"], key=lambda r: r["id"]) == sorted(
        expected["sources"], key=lambda r: r["id"]
    )
    assert sorted(actual["material_revisions"], key=lambda r: r["id"]) == sorted(
        expected["material_revisions"], key=lambda r: r["id"]
    )
    assert_material_counts(actual)
    validate_material_snapshot(actual)


@pytest.mark.parametrize("field", ["part_count", "retained_characters", "held_count"])
def test_material_snapshot_refuses_false_retained_counts(driver, material_world, field):
    source_erasure_case(driver, material_world, "file")
    inventory = read_once(driver, material_world[0].world_id, read_inventory)
    inventory["material_revisions"][0][field] += 1
    with pytest.raises(ValueError, match="material"):
        validate_material_snapshot(inventory)


def test_post_material_erasure_snapshot_preserves_canonical_ledger(driver, material_world):
    from grove.writer.forget_ledger import checked_under_lock, locked_ledger
    from tests.writer.test_restore import purge_world_rows

    session, cookie, ledger = material_world
    item = write_note(driver, material_world)["id"]
    target, approval = approve_material(driver, material_world, item)
    forget(
        driver,
        session.world_id,
        target,
        approval_token=approval,
        actor=session.user_id,
        material_session=session,
        session_hash=token_hash(cookie),
        ledger_dir=ledger,
    )
    snapshot = take_snapshot(driver, session.world_id)
    latest = read_ledger(ledger, session.world_id)
    assert latest[0]["internal_ids"] == []
    purge_world_rows(driver, session.world_id)
    restore(driver, session.world_id, snapshot, latest)
    rows = WorldQuery(driver, session.world_id).read("MATCH (f:ForgetLedger) RETURN f")
    with locked_ledger(ledger) as root:
        checked_under_lock(root, session.world_id, [r["f"] for r in rows])
