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


def source_erasure_case(driver, context, kind):
    """Use real material writers; return one supported D14 Source target."""
    from uuid import uuid4

    from grove.ingest.material_files import parse_file
    from grove.ingest.materials import input_digest, key_hash
    from grove.writer.materials import save

    session, cookie, ledger = context
    if kind in {"file", "multi_file", "message", "title"}:
        body = (
            ("Amber " * 3000 + "\n\n" + "Violet " * 3000).encode()
            if kind == "multi_file"
            else b"Erase the first paragraph.\n\nKeep the second paragraph."
        )
        item = save(
            driver,
            session,
            token_hash(cookie),
            title="Document heading",
            body=parse_file(body, "text/plain"),
            request_key_hash=key_hash(session.world_id, str(uuid4()), "file"),
            input_digest=input_digest("Document heading", body, "file"),
            input_bytes=len(body),
            ledger_dir=ledger,
        )["id"]
    else:
        item = write_note(driver, context, title="Earlier heading", body="Erase earlier words.")[
            "id"
        ]
        write_note(
            driver,
            context,
            title="Later heading",
            body="Retain later words.",
            material_id=item,
            expected_revision=1,
        )
    revisions = [
        r["m"]
        for r in WorldQuery(driver, session.world_id).read(
            "MATCH (m:MaterialRevision) RETURN m ORDER BY m.revision"
        )
    ]
    row = revisions[-1] if kind == "edited_current" else revisions[0]
    if kind == "multi_file":
        assert len(row["source_ids"]) == 2
    target = {
        "type": "source_span",
        "source_id": row["title_source_id"] if kind == "title" else row["source_ids"][0],
    }
    if kind == "message":
        target["locator"] = "key:part:1"
    return item, target, revisions


def erase_source(driver, context, target):
    session, _, ledger = context
    approval = mint_approval(driver, session.world_id, session.user_id, target)
    return forget(
        driver,
        session.world_id,
        target,
        approval_token=approval.token,
        actor=session.user_id,
        ledger_dir=ledger,
    )


def assert_material_counts(inventory):
    import json

    sources = {r["id"]: r for r in inventory["sources"]}
    for row in inventory["material_revisions"]:
        messages = [
            m
            for sid in row["source_ids"]
            if sources[sid].get("forget_state") != "forgotten"
            for m in json.loads(sources[sid]["content"])
        ]
        assert row["part_count"] == len(messages)
        assert row["retained_characters"] == sum(len(m["text"]) for m in messages)
        assert row["held_count"] == (len(messages) if row["kind"] == "file" else 0)


@pytest.mark.parametrize(
    "kind", ["file", "multi_file", "edited_old", "edited_current", "message", "title"]
)
def test_source_erasure_preserves_material_scope_and_counts(driver, material_world, kind):
    from grove.store.d3_fingerprint import read_once
    from grove.store.material_snapshot import validate_material_snapshot
    from grove.writer.forget import read_inventory

    item, target, revisions = source_erasure_case(driver, material_world, kind)
    world = material_world[0].world_id
    before = read_once(driver, world, read_inventory)
    erase_source(driver, material_world, target)
    after = read_once(driver, world, read_inventory)
    untouched = {s["id"]: s for s in before["sources"] if s["id"] != target["source_id"]}
    assert {s["id"]: s for s in after["sources"] if s["id"] in untouched} == untouched
    assert [r["state"] for r in after["material_revisions"]] == [
        r["state"] for r in before["material_revisions"]
    ]
    assert_material_counts(after)
    validate_material_snapshot(after)
    shown = detail(driver, material_world[0], material_world[1], item)["material"]
    assert shown["counts"]["parts"] == len(shown["parts"])
    assert shown["counts"]["retained_characters"] == sum(len(p["text"]) for p in shown["parts"])


def test_erasing_all_sources_of_one_revision_preserves_family_order(driver, material_world):
    from grove.store.d3_fingerprint import read_once
    from grove.store.material_snapshot import validate_material_snapshot
    from grove.writer.forget import read_inventory

    _, target, revisions = source_erasure_case(driver, material_world, "edited_old")
    erase_source(driver, material_world, target)
    erase_source(
        driver,
        material_world,
        {
            "type": "source_span",
            "source_id": revisions[0]["title_source_id"],
        },
    )
    inventory = read_once(driver, material_world[0].world_id, read_inventory)
    rows = sorted(inventory["material_revisions"], key=lambda r: r["revision"])
    assert [r["state"] for r in rows] == ["superseded", "current"]
    assert [r["part_count"] for r in rows] == [0, 1]
    validate_material_snapshot(inventory)
