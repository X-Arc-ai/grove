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
from tests.writer.test_material_forget import approve_material

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
