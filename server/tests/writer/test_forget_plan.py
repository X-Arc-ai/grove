"""Exact source selections, complete history coverage and replay-safe D14 plans."""

import json
from datetime import UTC, datetime

import pytest

from grove.store.query import WorldQuery
from grove.store.replay import FORGOTTEN_MARKER, rebuild
from grove.writer.errors import InvalidRecord
from grove.writer.forget import read_inventory, write_inventory
from grove.writer.forget_plan import WORLD_TABLES, build_plan, digest, transform
from grove.writer.forget_sources import locator_key, message_locator_key
from grove.writer.testing import plant_node
from grove.writer.tx import WorldWriter
from tests.writer.forget_drill import assert_forget_drill, reimport_forget_drill, seed_forget_drill

AT = datetime(2026, 10, 20, tzinfo=UTC)


def inventory(**rows):
    return {name: rows.get(name, []) for name in WORLD_TABLES}


def source(content):
    return {
        "id": "source-id",
        "world_id": "world-id",
        "external_locator": "opaque-source",
        "content": content,
        "forget_state": "kept",
    }


def messages():
    return [
        {
            "key": "one",
            "sender": "First Sender",
            "text": "Selected private sentence",
            "at": AT.isoformat(),
            "speaker_kind": "person",
            "modality": "text",
        },
        {
            "key": "two",
            "sender": "Other Sender",
            "text": "Retained unrelated sentence",
            "at": AT.isoformat(),
            "speaker_kind": "person",
            "modality": "text",
        },
    ]


@pytest.mark.parametrize(
    "locator", ["key:one", "message:one", "one", "line:1", "line:1-1", "evidence-locator"]
)
def test_source_message_selection_preserves_unrelated_message_and_codes(locator):
    original = messages()
    before = inventory(
        sources=[source(json.dumps(original))],
        evidence=[
            {
                "id": "ev",
                "source_id": "source-id",
                "locator": "evidence-locator",
                "message_key": "one",
            }
        ],
        history_values=[{"id": "code", "value": "person"}, {"id": "key", "value": "text"}],
    )
    plan = build_plan(before, {"type": "source_span", "source_id": "source-id", "locator": locator})
    after, _ = transform(before, plan, AT)
    assert json.loads(after["sources"][0]["content"]) == original[1:]
    assert after["history_values"] == before["history_values"]
    assert plan["source_ids"] == []
    assert plan["locators"] == [message_locator_key("opaque-source", "one")]
    assert "Selected" not in json.dumps(plan) and '"one"' not in json.dumps(plan)
    assert digest("person") not in plan["hashes"] and digest("text") not in plan["hashes"]
    assert transform(after, plan, AT)[0] == after


@pytest.mark.parametrize(
    "locator,expected",
    [
        ("line:2", "keep first\n[forgotten]\nkeep last\n"),
        ("char:11-17", "keep first\n      \nkeep last\n"),
    ],
)
def test_plain_source_range_does_not_expand_to_source(locator, expected):
    before = inventory(sources=[source("keep first\nsecret\nkeep last\n")])
    plan = build_plan(before, {"type": "source_span", "source_id": "source-id", "locator": locator})
    after, _ = transform(before, plan, AT)
    assert after["sources"][0]["content"] == expected
    assert after["sources"][0]["external_locator"] == "opaque-source"
    assert plan["locators"] == [locator_key("opaque-source")]
    assert transform(after, plan, AT)[0] == after


def test_whole_source_replay_does_not_hash_locator_twice():
    before = inventory(sources=[source(json.dumps(messages()))])
    plan = build_plan(before, {"type": "source_span", "source_id": "source-id"})
    after, _ = transform(before, plan, AT)
    assert after["sources"][0]["content"] == "[]"
    assert after["sources"][0]["external_locator"] == locator_key("opaque-source")
    again, counts = transform(after, plan, AT)
    assert again == after and counts == {}


def test_field_erasure_reaches_old_create_columns_and_keeps_other_columns():
    old = {"table": "items", "row": {"id": "item", "title": "old secret", "summary": "keep"}}
    before = inventory(
        items=[{"id": "item", "title": "current secret", "summary": "keep"}],
        history_values=[
            {"id": "row", "value": json.dumps(old)},
            {"id": "old", "value": json.dumps("old secret")},
            {"id": "new", "value": json.dumps("current secret")},
            {"id": "copy", "value": "old secret in another home"},
        ],
        change_entries=[
            {"entity_id": "item", "field": "row", "new_value_id": "row"},
            {"entity_id": "item", "field": "title", "old_value_id": "old", "new_value_id": "new"},
        ],
    )
    plan = build_plan(before, {"type": "field_value", "item_id": "item", "field": "title"})
    after, _ = transform(before, plan, AT)
    assert after["items"] == [{"id": "item", "title": FORGOTTEN_MARKER, "summary": "keep"}]
    assert json.loads(after["history_values"][0]["value"])["row"]["summary"] == "keep"
    assert "old secret" not in json.dumps(
        after, default=str
    ) and "current secret" not in json.dumps(after, default=str)
    assert after["change_entries"] == before["change_entries"]


def test_source_end_values_do_not_resurrect_evidence():
    before = inventory(
        sources=[source(json.dumps(messages()))],
        evidence=[
            {
                "id": "ev",
                "source_id": "source-id",
                "message_key": "one",
                "locator": "key:one",
                "active": True,
            }
        ],
        change_entries=[
            {"entity_id": "ev", "field": "end_reason", "old_value_id": "old", "new_value_id": "new"}
        ],
        history_values=[{"id": "old", "value": '"correction"'}, {"id": "new", "value": "null"}],
    )
    after, _ = transform(
        before,
        build_plan(before, {"type": "source_span", "source_id": "source-id", "locator": "key:one"}),
        AT,
    )
    assert [v["value"] for v in after["history_values"]] == ['"forget"', '"forget"']
    assert after["evidence"][0]["active"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(version=True),
        lambda p: p["locators"].append("private source"),
        lambda p: p["hashes"].append("not a digest"),
        lambda p: p["delete"].append({"table": "change_entries", "key": {"sequence": "1"}}),
        lambda p: p["source_spans"].append(
            {
                "source_id": "s",
                "source_key": locator_key("s"),
                "mode": "char",
                "start": True,
                "end": 2,
            }
        ),
        lambda p: p["source_spans"].append(
            {
                "source_id": "s",
                "source_key": locator_key("s"),
                "mode": "messages",
                "message_keys": ["raw key"],
            }
        ),
        lambda p: p["item_fields"].append({"id": "i", "field": "kind"}),
    ],
)
def test_invalid_plan_refuses_before_mutation(mutate):
    before = inventory()
    plan = build_plan(before, {"type": "named_value", "value": "private"})
    mutate(plan)
    with pytest.raises(ValueError):
        transform(before, plan, AT)
    assert before == inventory()


def test_full_fixture_plan_erasure_and_reimport(driver, make_world):
    world = make_world()
    fixture = seed_forget_drill(driver, world)
    baseline = read_inventory(WorldQuery(driver, world))
    rebuild(baseline["change_entries"], {v["id"]: v["value"] for v in baseline["history_values"]})
    for target in fixture["targets"]:

        def work(tx, target=target):
            tx.lock_world()
            before = read_inventory(tx)
            plan = build_plan(before, target)
            after, _ = transform(before, plan, AT)
            rebuild(after["change_entries"], {v["id"]: v["value"] for v in after["history_values"]})
            write_inventory(tx, before, after)
            return plan

        plan = WorldWriter(driver, world).transaction(work)
        plant_node(driver, world, "ForgetLedger", locators=plan["locators"])
    assert_forget_drill(driver, fixture)
    reimport_forget_drill(driver, fixture)
    reimport_forget_drill(driver, fixture)


def test_malformed_legacy_identity_refuses_without_erasure_or_value_in_error():
    before = inventory(items=[{"id": "private-person-key", "title": "private person"}])
    with pytest.raises(InvalidRecord) as raised:
        build_plan(before, {"type": "named_value", "value": "private person"})
    assert raised.value.code == "unavailable" and raised.value.rule == "forget_identity"
    assert "private" not in str(raised.value)
    assert before["items"][0]["title"] == "private person"


def test_named_value_redacts_json_keys_registry_names_and_status_consistently():
    word = "Confidential canary"
    before = inventory(
        kinds=[
            {
                "id": "kind",
                "name": word + " kind",
                "status_vocabulary": json.dumps([{"status": word, "role": "open"}]),
            }
        ],
        items=[{"id": "item", "status": word, "properties": json.dumps({word: "other"})}],
        history_values=[
            {"id": "status", "value": json.dumps(word)},
            {
                "id": "kindrow",
                "value": json.dumps(
                    {
                        "table": "kinds",
                        "row": {
                            "id": "kind",
                            "name": word + " kind",
                            "status_vocabulary": json.dumps([{"status": word, "role": "open"}]),
                        },
                    }
                ),
            },
        ],
        change_entries=[{"entity_id": "item", "field": "status", "new_value_id": "status"}],
    )
    plan = build_plan(before, {"type": "named_value", "value": word})
    after, _ = transform(before, plan, AT)
    assert word not in json.dumps(after, default=str)
    status = json.loads(after["kinds"][0]["status_vocabulary"])[0]
    assert status["role"] == "open"
    assert status["status"] == after["items"][0]["status"]
    assert status["status"] == json.loads(after["history_values"][0]["value"])
    assert (
        after["kinds"][0]["name"] == json.loads(after["history_values"][1]["value"])["row"]["name"]
    )


def test_named_value_reaches_metadata_in_live_and_historical_rows():
    word = "Private connector detail"
    before = inventory(
        connectors=[{"id": "connector", "settings": json.dumps({"label": word})}],
        history_values=[
            {
                "id": "sourcecopy",
                "value": json.dumps(
                    {
                        "table": "sources",
                        "row": {"id": "source", "external_locator": word, "content": "retained"},
                    }
                ),
            }
        ],
    )
    after, _ = transform(before, build_plan(before, {"type": "named_value", "value": word}), AT)
    assert word not in json.dumps(after, default=str)
    assert json.loads(after["history_values"][0]["value"])["row"]["content"] == "retained"


def test_full_fixture_transform_covers_every_value_home_without_replay(driver, make_world):
    """Exercise the plan independently of the paired-identity replay regression."""
    world = make_world()
    fixture = seed_forget_drill(driver, world)
    before = read_inventory(WorldQuery(driver, world))
    after = before
    for target in fixture["targets"]:
        plan = build_plan(after, target)
        after, _ = transform(after, plan, AT)
        assert transform(after, plan, AT)[0] == after
    serialized = json.dumps(after, default=str).casefold()
    for value in fixture["canaries"]:
        assert value.casefold() not in serialized
    assert after["change_entries"] == before["change_entries"]
    assert len(json.loads(after["sources"][0]["content"])) == 1
    assert after["support_changed"][0]["belief_id"] == fixture["ids"]["belief"]
