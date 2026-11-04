"""Native regression controls for the fixed synthetic demo adapter."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from grove import clock
from grove.demo import corpus
from grove.demo_policy import DEMO_VERSION, DemoDenied
from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer import demo_seed
from grove.writer.api import prepare_batch
from grove.writer.errors import BatchDeclined
from grove.writer.forget_sources import ForgottenSource, locator_key
from grove.writer.registry import Registry
from grove.writer.testing import plant_node, set_self_item
from grove.writer.tx import WorldWriter
from grove.writer.worlds import create_world, load_starter_set


@pytest.fixture
def designated(driver, world, monkeypatch):
    hashed, owner = "a" * 64, new_id()
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
    created = create_world(driver, world, owner_user_id=owner)
    own = plant_node(
        driver, world, "Item", kind=created["kinds"]["person"], title="Synthetic owner", version=1
    )
    set_self_item(driver, world, own["id"])
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 CREATE (u:User) SET u = $row",
            row={
                "id": owner,
                "email_hash": hashed,
                "created_at": clock.now(),
                "demo_version": DEMO_VERSION,
                "demo_world_id": world,
                "planted_for": world,
            },
        ).consume()
    return world, hashed, owner


def _generation(driver, world):
    return PlatformQuery(driver).read(
        "MATCH (w:World {id: $id}) RETURN w.read_generation AS generation",
        id=world,
    )[0]["generation"]


def test_native_first_story_real_history_receipt_and_repeat(driver, designated):
    world, hashed, _ = designated
    story = corpus.stories()[0]
    result = demo_seed.seed_story(driver, hashed, story.key)
    assert result == {
        "state": "saved",
        "story": story.key,
        "items": 52,
        "links": 156,
        "replayed": False,
    }
    query = WorldQuery(driver, world)
    counts = query.read("MATCH (i:Item) RETURN count(i) AS n")[0]["n"]
    assert counts == 53
    receipt = query.read("MATCH (r:Request) RETURN r.receipt AS receipt")[0]["receipt"]
    assert len(json.loads(receipt)["lines"]) == 208
    generation = _generation(driver, world)
    assert demo_seed.seed_story(driver, hashed, story.key)["replayed"] is True
    inspected = demo_seed.inspect_seed(driver, hashed)
    assert inspected["items"] == 52 and inspected["links"] == 156
    assert inspected["state"] == "partial"
    assert _generation(driver, world) == generation


def test_unknown_story_and_wrong_hash_refuse_before_writes(driver, designated):
    world, hashed, _ = designated
    before = _generation(driver, world)
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, "unknown")
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, "b" * 64, corpus.stories()[0].key)
    assert _generation(driver, world) == before


def test_pure_fixed_batch_uses_checked_writer_shape_and_no_person_claim():
    class NoAliases:
        def read(self, *args, **kwargs):
            return []

    starter = load_starter_set()
    kinds = {}
    for kind in starter["kinds"]:
        row_id = new_id()
        kinds[row_id] = {**kind, "id": row_id, "status": "canonical"}
    registry = Registry(kinds, {}, [], [])
    story = corpus.stories()[0]
    batch = demo_seed._batch(NoAliases(), registry, story, new_id(), new_id(), {})
    assert len(batch["ops"]) == 208
    assert len(batch["evidence"]) == 52
    prepared = prepare_batch(demo_seed._CALLER, batch, request_id="synthetic-shape")
    assert prepared.caller.kind == "update"
    assert all(
        row["speaker_kind"] == "ai_host" and row["statement_class"] == "report_state"
        for row in batch["evidence"]
    )
    assert all(row["op"] in {"create_item", "create_link"} for row in batch["ops"])


def test_pure_request_identity_is_world_bound_and_fixed():
    story = corpus.stories()[0]
    a, b = "t_demo_" + new_id(), "t_demo_" + new_id()
    assert demo_seed.request_identity(a, story.key) != demo_seed.request_identity(b, story.key)
    assert demo_seed.request_identity(a, story.key) == demo_seed.request_identity(a, story.key)
    assert len(demo_seed.content_identity(story.key)) == 64
    with pytest.raises(DemoDenied):
        demo_seed.request_identity(a, "replacement-corpus")


def test_pure_corrupt_boolean_version_is_refused():
    with pytest.raises(DemoDenied):
        demo_seed._matches({"version": True}, {"version": 1})


def test_pure_bridge_touch_changes_no_claim_and_cannot_hide_content_changes():
    original = {
        "table": "items",
        "row": {
            "title": "Synthetic plan",
            "summary": "Fixed text",
            "recorded": corpus.SOURCE_TIME,
            "touched_at": corpus.SOURCE_TIME,
        },
    }
    current = {
        "table": "items",
        "row": {**original["row"], "touched_at": "2026-10-21T12:00:00+00:00"},
    }
    demo_seed._check_creation("item", original, current)
    current["row"]["summary"] = "Unapproved change"
    with pytest.raises(DemoDenied):
        demo_seed._check_creation("item", original, current)


def test_pure_own_item_guard_requires_a_live_person():
    class Reader:
        def read(self, *args, **kwargs):
            return [{"id": "own", "kind": "event", "end_class": None}]

    with pytest.raises(DemoDenied):
        demo_seed._own_item(Reader(), {"self_item_id": None})
    with pytest.raises(DemoDenied):
        demo_seed._own_item(Reader(), {"self_item_id": "own"})


def test_native_final_designation_recheck_rolls_back_the_whole_story(
    driver, designated, monkeypatch
):
    world, hashed, owner = designated
    original = demo_seed.execute_prepared_in_tx
    before = _generation(driver, world)

    def changed_after_execution(tx, *args, **kwargs):
        original(tx, *args, **kwargs)
        tx.unscoped().run(
            "CYPHER 25 MATCH (u:User {id: $id}) SET u.demo_version = 'corrupt'", id=owner
        ).consume()

    monkeypatch.setattr(demo_seed, "execute_prepared_in_tx", changed_after_execution)
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, corpus.stories()[0].key)
    assert _generation(driver, world) == before
    assert WorldQuery(driver, world).read("MATCH (i:Item) RETURN count(i) AS n")[0]["n"] == 1
    assert demo_seed.resolve_target(driver, hashed)["demo_version"] == DEMO_VERSION


def test_native_corrupt_source_and_receipt_refuse_replay(driver, designated):
    world, hashed, _ = designated
    story = corpus.stories()[0]
    demo_seed.seed_story(driver, hashed, story.key)
    before = _generation(driver, world)
    with driver.session(database=DATABASE) as session:
        source = session.run(
            "CYPHER 25 MATCH (s:Source {world_id: $world}) RETURN s.content AS content", world=world
        ).single()["content"]
        session.run(
            "CYPHER 25 MATCH (s:Source {world_id: $world}) SET s.content = 'corrupt'", world=world
        ).consume()
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, story.key)
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (s:Source {world_id: $world}) SET s.content = $content",
            world=world,
            content=source,
        ).consume()
        session.run(
            "CYPHER 25 MATCH (r:Request {world_id: $world}) SET r.receipt = '{}'", world=world
        ).consume()
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, story.key)
    assert _generation(driver, world) == before


def test_native_receipt_attachment_can_resume_after_interruption(driver, designated, monkeypatch):
    world, hashed, _ = designated
    story = corpus.stories()[0]
    original = demo_seed.read_receipt

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic interruption after commit")

    monkeypatch.setattr(demo_seed, "read_receipt", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        demo_seed.seed_story(driver, hashed, story.key)
    pending = demo_seed.inspect_seed(driver, hashed)
    assert pending["state"] == "partial" and pending["completed_stories"] == 0
    assert pending["saved_requests"] == 1
    monkeypatch.setattr(demo_seed, "read_receipt", original)
    assert demo_seed.seed_story(driver, hashed, story.key)["replayed"] is True
    assert demo_seed.inspect_seed(driver, hashed)["items"] == 52


def test_native_unrelated_collision_and_forgotten_source_preserve_existing_data(driver, designated):
    world, hashed, _ = designated
    story = corpus.stories()[0]
    kind = Registry.load(WorldQuery(driver, world)).resolve_kind(story.items[0].kind)["id"]
    planted = plant_node(driver, world, "Item", kind=kind, title=story.items[0].title, version=1)
    before = _generation(driver, world)
    with pytest.raises(BatchDeclined) as refused:
        demo_seed.seed_story(driver, hashed, story.key)
    assert any(row.rule == "near_duplicate" for row in refused.value.refusals)
    query = WorldQuery(driver, world)
    items = query.read("MATCH (i:Item) RETURN i.id AS id")
    assert len(items) == 2 and {"id": planted["id"]} in items
    assert query.read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 0
    assert _generation(driver, world) == before
    plant_node(driver, world, "ForgetLedger", locators=[locator_key("synthetic:" + story.key)])
    with pytest.raises(ForgottenSource):
        demo_seed.seed_story(driver, hashed, story.key)
    assert _generation(driver, world) == before


def test_native_second_story_bridge_has_real_endpoints(driver, designated):
    world, hashed, _ = designated
    first, second = corpus.stories()[:2]
    demo_seed.seed_story(driver, hashed, first.key)
    demo_seed.seed_story(driver, hashed, second.key)
    report = demo_seed.inspect_seed(driver, hashed)
    assert (report["completed_stories"], report["items"], report["links"]) == (2, 104, 313)
    before = _generation(driver, world)
    assert demo_seed.seed_story(driver, hashed, second.key)["replayed"] is True
    assert _generation(driver, world) == before


@pytest.mark.parametrize("pointer", [None, "absent", "wrong-kind"])
def test_native_missing_or_corrupt_own_item_refuses_before_writes(driver, designated, pointer):
    world, hashed, _ = designated
    if pointer == "absent":
        pointer = new_id()
    elif pointer == "wrong-kind":
        kind = Registry.load(WorldQuery(driver, world)).resolve_kind("event")["id"]
        pointer = plant_node(driver, world, "Item", kind=kind, title="Synthetic event", version=1)[
            "id"
        ]
    set_self_item(driver, world, pointer)
    before = _generation(driver, world)
    with pytest.raises(DemoDenied):
        demo_seed.resolve_target(driver, hashed)
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, corpus.stories()[0].key)
    assert _generation(driver, world) == before
    assert WorldQuery(driver, world).read("MATCH (s:Source) RETURN count(s) AS n")[0]["n"] == 0


def test_native_owner_lock_serializes_designation_mutation(driver, designated):
    world, hashed, owner = designated
    target = demo_seed.resolve_target(driver, hashed)
    locked, release, attempted, changed = Event(), Event(), Event(), Event()
    before = _generation(driver, world)

    def hold():
        def work(tx):
            demo_seed._guard(tx, target, lock=True)
            locked.set()
            assert release.wait(5)
            demo_seed._guard(tx, target)
            raise demo_seed._ReadOnly(None)

        try:
            WorldWriter(driver, world).transaction(work)
        except demo_seed._ReadOnly:
            pass

    def mutate():
        attempted.set()
        with driver.session(database=DATABASE) as session:
            session.run(
                "CYPHER 25 MATCH (u:User {id: $id}) SET u.demo_version = 'changed'", id=owner
            ).consume()
        changed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(hold)
        try:
            assert locked.wait(5)
            mutation = pool.submit(mutate)
            assert attempted.wait(5)
            assert not changed.wait(0.1)
        finally:
            release.set()
        held.result(timeout=5)
        mutation.result(timeout=5)
    with pytest.raises(DemoDenied):
        demo_seed.seed_story(driver, hashed, corpus.stories()[0].key)
    assert _generation(driver, world) == before
