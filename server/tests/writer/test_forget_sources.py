"""D14 re-import controls: latest ledger, world isolation and source-write races."""

import json
import re
import threading
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.ids import new_id
from grove.ingest import chat_locator, message_key
from grove.ingest import take_in as take_in_module
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer.errors import InvalidRecord
from grove.writer.forget_sources import (
    ForgottenSource,
    blocked_message_keys,
    locator_key,
    message_locator_key,
    source_is_forgotten,
)
from grove.writer.ingest import EpisodeCut, ensure_connector, write_chat_delta
from grove.writer.sources import add_screened_source, record_source
from grove.writer.testing import held_transaction, plant_node, seed_test_world
from grove.writer.tx import WorldWriter
from tests.ingest.test_take_in import export, run, sources

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def world(driver, make_world):
    result = make_world()
    seed_test_world(driver, result, owner_user_id=new_id())
    return result


def ledger(driver, world, *locators):
    return plant_node(
        driver,
        world,
        "ForgetLedger",
        target_type="source_span",
        internal_ids=[],
        locators=list(locators),
        requested_by="person",
        approved_at=clock.now(world),
    )


def connector(driver, world):
    return ensure_connector(driver, world, "past_ai_conversations", "zz_forget", batch_rule="v1")


def test_locator_hash_is_opaque_stable_and_unambiguous():
    raw = "Synthetic private project"
    assert locator_key(raw) == locator_key(raw)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", locator_key(raw))
    assert raw not in locator_key(raw)
    assert locator_key(raw) != message_locator_key(raw, "")
    assert message_locator_key("a\x1fb", "c") != message_locator_key("a", "b\x1fc")
    assert message_locator_key("a", "b") != message_locator_key("b", "a")
    assert locator_key("\ud800") != locator_key("\ufffd")


@pytest.mark.parametrize("hashed", [False, True])
def test_whole_source_gate_reads_legacy_and_hashed_locators(driver, world, hashed):
    place = "synthetic-source"
    ledger(driver, world, locator_key(place) if hashed else place)
    query = WorldQuery(driver, world)
    assert source_is_forgotten(query, place)
    assert blocked_message_keys(query, place, ["a", "b"]) == {"a", "b"}
    assert not source_is_forgotten(query, "other-source")


def test_message_gate_does_not_drop_other_messages_or_sources(driver, world):
    ledger(driver, world, message_locator_key("chat", "one"))
    query = WorldQuery(driver, world)
    assert not source_is_forgotten(query, "chat")
    assert blocked_message_keys(query, "chat", ["one", "two"]) == {"one"}
    assert blocked_message_keys(query, "other", ["one", "two"]) == set()


def test_source_gate_is_world_scoped(driver, world, make_world):
    other = make_world()
    ledger(driver, other, locator_key("chat"))
    assert not source_is_forgotten(WorldQuery(driver, world), "chat")


def test_invalid_ledger_locator_shape_fails_closed_without_values(driver, world):
    plant_node(driver, world, "ForgetLedger", locators=42)
    with pytest.raises(InvalidRecord) as raised:
        source_is_forgotten(WorldQuery(driver, world), "synthetic-private-value")
    assert raised.value.rule == "forget_ledger"
    assert "synthetic-private-value" not in str(raised.value)


@pytest.mark.parametrize("empty", [False, True])
def test_screened_source_writer_rejects_forgotten_source_atomically(driver, world, empty):
    source_connector = connector(driver, world)
    ledger(driver, world, locator_key("chat"))
    screened = Screener(ScreeningConfig()).screen_source(
        "chat", [] if empty else [Message("one", None, "Synthetic forgotten sentence")]
    )
    with pytest.raises(ForgottenSource):
        add_screened_source(
            WorldWriter(driver, world), screened, connector_id=source_connector, parser_version="v1"
        )
    query = WorldQuery(driver, world)
    assert query.read("MATCH (s:Source) RETURN s") == []
    assert query.read("MATCH (s:ScreeningReceipt) RETURN s") == []


def test_raw_source_fixture_writer_also_rejects_forgotten_source(driver, world):
    source_connector = connector(driver, world)
    ledger(driver, world, locator_key("chat"))
    with pytest.raises(ForgottenSource):
        record_source(
            driver,
            world,
            connector_id=source_connector,
            external_locator="chat",
            parser_version="v1",
            content="Synthetic forgotten sentence",
        )
    assert sources(driver, world) == []


def test_direct_chat_writer_checks_message_tombstones(driver, world):
    source_connector = connector(driver, world)
    ledger(driver, world, message_locator_key("chat", "one"))
    screened = Screener(ScreeningConfig()).screen_source(
        "chat", [Message("one", None, "Synthetic forgotten sentence")]
    )
    with pytest.raises(ForgottenSource):
        write_chat_delta(
            driver,
            world,
            screened,
            connector_id=source_connector,
            parser_version="v1",
            delta_keys=["one"],
            episodes=[EpisodeCut("one", "one", T0, T0, "v1")],
            newest_at=T0,
        )
    assert sources(driver, world) == []
    assert WorldQuery(driver, world).read("MATCH (e:Episode) RETURN e") == []


def test_reupload_of_erased_chat_writes_no_source_or_receipt(driver, world):
    data = export({"id": "forgotten", "lines": ["Synthetic forgotten sentence", "Reply"]})
    first = run(driver, world, data)
    assert first.sources_added == 1
    place = chat_locator("zc", "forgotten")
    ledger(driver, world, locator_key(place))
    # The cascade owns deletion; this fixture models its committed result.
    WorldWriter(driver, world).transaction(lambda tx: tx.run("MATCH (s:Source) DETACH DELETE s"))
    before = WorldQuery(driver, world).read("MATCH (r:ScreeningReceipt) RETURN r")
    again = run(driver, world, data)
    assert again.sources_added == 0 and again.failed_chats == 0
    assert sources(driver, world) == []
    assert WorldQuery(driver, world).read("MATCH (r:ScreeningReceipt) RETURN r") == before


def test_reupload_of_one_erased_message_keeps_new_unrelated_content(driver, world):
    place = chat_locator("zc", "partly-forgotten")
    ledger(driver, world, message_locator_key(place, message_key("zc", "partly-forgotten", 0)))
    data = export({"id": "partly-forgotten", "lines": ["Forgotten canary", "Retained reply"]})
    first = run(driver, world, data)
    assert first.messages_kept == 1
    assert [m["text"] for m in json.loads(sources(driver, world)[0]["content"])] == [
        "Retained reply"
    ]
    assert run(driver, world, data).sources_added == 0


def test_forgotten_message_never_reaches_screening(driver, world, monkeypatch):
    place = chat_locator("zc", "before-screen")
    ledger(driver, world, message_locator_key(place, message_key("zc", "before-screen", 0)))
    seen = []
    original = take_in_module._messages

    def observe(delta):
        seen.extend(message.text for message in delta)
        return original(delta)

    monkeypatch.setattr(take_in_module, "_messages", observe)
    run(driver, world, export({"id": "before-screen", "lines": ["Forgotten canary", "Kept"]}))
    assert seen == ["Kept"]


def test_forget_after_pipeline_read_is_rechecked_by_the_writer(driver, world, monkeypatch):
    original = take_in_module.write_chat_delta
    attempts = []

    def after_forget(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            ledger(driver, world, locator_key(chat_locator("zc", "race")))
        return original(*args, **kwargs)

    monkeypatch.setattr(take_in_module, "write_chat_delta", after_forget)
    result = run(driver, world, export({"id": "race", "lines": ["Forgotten canary"]}))
    assert result.sources_added == 0 and result.failed_chats == 0
    assert sources(driver, world) == []


def test_source_write_waits_for_a_concurrent_forget_world_lock(driver, world):
    source_connector = connector(driver, world)
    screened = Screener(ScreeningConfig()).screen_source("chat", [Message("one", None, "Canary")])
    errors = []
    done = threading.Event()

    def write():
        try:
            add_screened_source(
                WorldWriter(driver, world),
                screened,
                connector_id=source_connector,
                parser_version="v1",
            )
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    with held_transaction(driver) as held:
        held.run(
            "MATCH (w:World {id: $world}) SET w.lock_marker = true REMOVE w.lock_marker",
            world=world,
        )
        worker = threading.Thread(target=write)
        worker.start()
        try:
            assert held.blocks(done.is_set), "the source write did not wait on the world lock"
            held.run(
                "CREATE (f:ForgetLedger {world_id: $world, id: $id, locators: $locators})",
                world=world,
                id=new_id(world),
                locators=[locator_key("chat")],
            )
            held.commit()
        finally:
            # Release the transaction before joining if the assertion failed.
            if not held.tx.closed():
                held.tx.rollback()
            worker.join(timeout=10)
    assert done.is_set()
    assert len(errors) == 1 and isinstance(errors[0], ForgottenSource)
    assert sources(driver, world) == []


def test_recovered_old_source_refuses_raw_locator_and_preserves_other_source(driver, world):
    """A restored source discovered by predicates needs no mutable ledger extension."""
    from grove.writer.forget import read_inventory, write_inventory
    from grove.writer.forget_plan import WORLD_TABLES, build_plan, transform

    place = "old-export-before-approval"
    source_connector = connector(driver, world)
    current = {name: [] for name in WORLD_TABLES}
    plan = build_plan(current, {"type": "named_value", "value": "Amber Finch"})
    approved = ledger(driver, world)
    plant_node(
        driver,
        world,
        "Source",
        connector_id=source_connector,
        external_locator=place,
        forget_state="kept",
        content=json.dumps(
            [
                {"key": "one", "text": "Earlier Amber Finch boarded the ferry", "sender": None},
                {"key": "two", "text": "Retain the ordinary shipping note", "sender": None},
            ]
        ),
    )

    def recover(tx):
        tx.lock_world()
        before = read_inventory(tx)
        after, _ = transform(before, plan, T0)
        write_inventory(tx, before, after)

    WorldWriter(driver, world).transaction(recover)
    query = WorldQuery(driver, world)
    assert source_is_forgotten(query, place)
    assert blocked_message_keys(query, place, ["one", "two", "new"]) == {"one", "two", "new"}
    assert not source_is_forgotten(query, "unrelated-export")
    before = read_inventory(query)
    with pytest.raises(ForgottenSource):
        record_source(
            driver,
            world,
            connector_id=source_connector,
            external_locator=place,
            content="Amber Finch could return",
            parser_version="v1",
        )
    assert read_inventory(query) == before
    assert before["forget_ledger"] == [approved]
    assert [m["text"] for m in json.loads(before["sources"][0]["content"])] == [
        "Retain the ordinary shipping note"
    ]


def test_known_partial_source_remains_a_message_specific_gate(driver, world):
    place = "known-partial-source"
    ledger(driver, world, message_locator_key(place, "one"))
    plant_node(
        driver,
        world,
        "Source",
        external_locator=place,
        forget_state="span_forgotten",
        content=json.dumps([{"key": "two", "text": "Retained message"}]),
    )
    query = WorldQuery(driver, world)
    assert not source_is_forgotten(query, place)
    assert blocked_message_keys(query, place, ["one", "two", "new"]) == {"one"}
