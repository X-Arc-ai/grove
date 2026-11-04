"""Native material transactions, replay and session isolation."""

from uuid import uuid4

import pytest

from grove import clock
from grove.ids import new_id
from grove.ingest.materials import input_digest, key_hash
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.session import find_session, token_hash
from grove.writer.materials import MaterialConflict, MaterialSessionEnded, save
from grove.writer.testing import plant_session
from grove.writer.worlds import create_world


@pytest.fixture
def material_world(driver, make_world, tmp_path, monkeypatch):
    world = make_world()
    owner = new_id()
    create_world(driver, world, owner_user_id=owner)
    cookie = plant_session(driver, world, user_id=owner)
    session = find_session(PlatformQuery(driver), cookie, clock.now())
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    monkeypatch.setenv("GROVE_FORGET_LEDGER_DIR", str(ledger))
    return session, cookie, ledger


def write_note(
    driver, context, title="Cedar plan", body="Mira plans the Cedar launch.", key=None, **extra
):
    session, cookie, ledger = context
    return save(
        driver,
        session,
        token_hash(cookie),
        title=title,
        body=body,
        request_key_hash=key_hash(session.world_id, key or str(uuid4()), "note"),
        input_digest=input_digest(title, body, "note"),
        input_bytes=len(body.encode()),
        ledger_dir=ledger,
        **extra,
    )


def test_note_atomic_rows_and_exact_replay(driver, material_world):
    key = str(uuid4())
    one = write_note(driver, material_world, key=key)
    two = write_note(driver, material_world, key=key)
    assert one["id"] == two["id"] and one["fresh"] and not two["fresh"]
    q = WorldQuery(driver, material_world[0].world_id)
    assert q.read("MATCH (m:MaterialRevision) RETURN count(m) AS n")[0]["n"] == 1
    assert q.read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 2
    assert q.read("MATCH (r:Request) RETURN count(r) AS n")[0]["n"] == 1
    with pytest.raises(MaterialConflict):
        write_note(driver, material_world, body="Changed", key=key)


def test_edit_keeps_revision_and_rejects_stale(driver, material_world):
    one = write_note(driver, material_world)
    write_note(
        driver, material_world, body="A revised launch.", material_id=one["id"], expected_revision=1
    )
    with pytest.raises(MaterialConflict):
        write_note(driver, material_world, material_id=one["id"], expected_revision=1)
    q = WorldQuery(driver, material_world[0].world_id)
    rows = q.read("MATCH (m:MaterialRevision) RETURN m.state AS state ORDER BY m.revision")
    assert [r["state"] for r in rows] == ["superseded", "current"]


def test_invalid_session_rolls_back(driver, material_world):
    session, cookie, ledger = material_world
    with pytest.raises(MaterialSessionEnded):
        save(
            driver,
            session,
            "0" * 64,
            title="Title",
            body="Body",
            request_key_hash="a" * 64,
            input_digest="b" * 64,
            input_bytes=4,
            ledger_dir=ledger,
        )
    assert (
        WorldQuery(driver, session.world_id).read(
            "MATCH (m:MaterialRevision) RETURN count(m) AS n"
        )[0]["n"]
        == 0
    )


def test_concurrent_same_key_creates_once(driver, material_world):
    from concurrent.futures import ThreadPoolExecutor

    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write_note(driver, material_world, key=key), range(2)))
    assert len({r["id"] for r in results}) == 1
    assert sorted(r["fresh"] for r in results) == [False, True]


def test_concurrent_edit_has_one_stale_revision(driver, material_world):
    from concurrent.futures import ThreadPoolExecutor

    item = write_note(driver, material_world)["id"]

    def edit(index):
        try:
            write_note(
                driver,
                material_world,
                body=f"Revision {index}",
                material_id=item,
                expected_revision=1,
            )
            return "saved"
        except MaterialConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, range(2))) == ["saved", "stale"]
