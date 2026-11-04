"""Real one-use material approval and all-revision erasure."""

import pytest

from grove.store.query import WorldQuery
from grove.views.materials import detail
from grove.views.session import token_hash
from grove.writer.forget import forget
from grove.writer.forget_approvals import mint_approval
from grove.writer.forget_ledger import LedgerUnavailable
from grove.writer.materials import MaterialConflict
from tests.writer import test_materials

material_world = test_materials.material_world
write_note = test_materials.write_note


def approve_material(driver, context, item, revision=1):
    session, cookie, ledger = context
    target = {"type": "material", "material_id": item, "revision": revision}
    approval = mint_approval(
        driver,
        session.world_id,
        session.user_id,
        target,
        material_session=session,
        session_hash=token_hash(cookie),
    )
    return target, approval.token


def test_all_material_revisions_are_erased(driver, material_world):
    session, cookie, ledger = material_world
    item = write_note(driver, material_world)["id"]
    write_note(
        driver,
        material_world,
        body="A second private revision.",
        material_id=item,
        expected_revision=1,
    )
    target, approval = approve_material(driver, material_world, item, 2)
    receipt = forget(
        driver,
        session.world_id,
        target,
        approval_token=approval,
        actor=session.user_id,
        material_session=session,
        session_hash=token_hash(cookie),
        ledger_dir=ledger,
    )
    assert receipt["receipt_id"]
    q = WorldQuery(driver, session.world_id)
    assert all(r["m"]["state"] == "deleted" for r in q.read("MATCH (m:MaterialRevision) RETURN m"))
    assert all(
        r["s"].get("forget_state") == "forgotten" for r in q.read("MATCH (s:Source) RETURN s")
    )
    assert q.read("MATCH (r:Request) RETURN count(r) AS n")[0]["n"] == 0
    assert q.read("MATCH (e:Episode) RETURN count(e) AS n")[0]["n"] == 0


def test_stale_material_approval_refuses_after_edit(driver, material_world):
    session, cookie, ledger = material_world
    item = write_note(driver, material_world)["id"]
    target, approval = approve_material(driver, material_world, item)
    write_note(driver, material_world, material_id=item, expected_revision=1)
    with pytest.raises(MaterialConflict):
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


def test_pending_external_intent_blocks_material_disclosure_and_write(
    driver, material_world, monkeypatch
):
    session, cookie, ledger = material_world
    item = write_note(driver, material_world)["id"]
    target, approval = approve_material(driver, material_world, item)
    # Inject a native transaction failure after the fsynced intent, before database mutation.
    import importlib

    module = importlib.import_module("grove.writer.forget")

    def fail(*args):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(module, "write_inventory", fail)
    with pytest.raises(RuntimeError):
        module.forget(
            driver,
            session.world_id,
            target,
            approval_token=approval,
            actor=session.user_id,
            material_session=session,
            session_hash=token_hash(cookie),
            ledger_dir=ledger,
        )
    with pytest.raises(LedgerUnavailable):
        detail(driver, session, cookie, item, ledger_dir=ledger)
    with pytest.raises(LedgerUnavailable):
        write_note(driver, material_world)
    with pytest.raises(LedgerUnavailable):
        approve_material(driver, material_world, item)


def test_deleted_idempotency_key_cannot_restore_content(driver, material_world):
    from uuid import uuid4

    session, cookie, ledger = material_world
    key = str(uuid4())
    item = write_note(driver, material_world, key=key)["id"]
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
    with pytest.raises(MaterialConflict):
        write_note(driver, material_world, key=key)
