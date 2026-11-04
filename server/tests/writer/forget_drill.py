"""One synthetic D14 fixture shared by logical and physical restore drills.

Every value-id column points to a canary-bearing HistoryValue. Every text home has
current and historical copies. Controls use distinct text and original row bytes.
"""

import json
from datetime import UTC, datetime

from grove.ids import new_id
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.normalise import normalise_phrase
from grove.store.primitives import KIND_BELIEF
from grove.store.query import WorldQuery
from grove.writer.forget import read_inventory
from grove.writer.forget_plan import WORLD_TABLES
from grove.writer.forget_sources import ForgottenSource
from grove.writer.history import Stamp, add_entry, add_text, row_json
from grove.writer.ingest import write_chat_delta
from grove.writer.restore import rebuild_in_place
from grove.writer.sources import record_connector, record_episode, record_source
from grove.writer.testing import refuse_real_world
from grove.writer.tx import WorldWriter
from grove.writer.worlds import create_world

AT = datetime(2026, 10, 20, 9, tzinfo=UTC)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        default=lambda part: part.isoformat() if hasattr(part, "isoformat") else str(part),
    )


def seed_forget_drill(driver, world_id):
    """Seed all four targets in an empty synthetic world; return only JSON-safe metadata."""
    refuse_real_world(world_id)
    create_world(driver, world_id, owner_user_id="u_forget_drill")
    suffix = world_id.rsplit("_", 1)[-1].replace("-", "")
    canaries = [f"D14{category}{suffix}" for category in ("Named", "Item", "Field", "Source")]
    named, item_word, field_word, source_word = canaries
    retained_word = f"Retained{suffix}"
    ids = {
        name: new_id(world_id)
        for name in (
            "kind",
            "meaning",
            "property",
            "item",
            "field",
            "control",
            "belief",
            "link",
            "alias",
            "named_alias",
            "evidence",
            "control_evidence",
            "item_evidence",
            "inferred",
            "hold",
            "mention",
            "control_hold",
            "control_mention",
            "position",
            "capability",
        )
    }
    connector = record_connector(driver, world_id, "past_ai_conversations")
    external = "drill:" + suffix
    messages = [
        {
            "key": "selected:" + suffix,
            "sender": None,
            "text": source_word,
            "at": AT.isoformat(),
            "speaker_kind": "person",
            "modality": "text",
        },
        {
            "key": "retained:" + suffix,
            "sender": None,
            "text": retained_word,
            "at": AT.isoformat(),
            "speaker_kind": "person",
            "modality": "text",
        },
        {
            "key": "named:" + suffix,
            "sender": named,
            "text": named + " source copy",
            "at": AT.isoformat(),
            "speaker_kind": "person",
            "modality": "text",
        },
    ]
    source = record_source(
        driver,
        world_id,
        connector_id=connector,
        external_locator=external,
        content=json.dumps(messages),
        parser_version="drill.v1",
    )
    episode = record_episode(
        driver,
        world_id,
        connector_id=connector,
        source_ids=[source],
        first_message_key=messages[0]["key"],
        last_message_key=messages[-1]["key"],
        start_time=AT,
        end_time=AT,
        cut_rule="drill.v1",
    )
    ids.update(source=source, episode=episode, connector=connector)
    retained = []
    coverage = []

    def work(tx):
        tx.lock_world()
        stamp = Stamp(world_id, AT, "person:drill", new_id(world_id), new_id(world_id))

        def add(name, row, *, history=True, keep=False):
            held = WORLD_TABLES[name]
            row = dict(row)
            row.setdefault("version", 1) if any(
                "version" in c.props for c in held.columns
            ) else None
            # Every pointer to prose has its own physical row and unique canary suffix.
            for column in held.columns:
                if column.type == "value_id":
                    for prop in column.props:
                        row.setdefault(prop, add_text(tx, stamp, named + " " + name + " " + prop))
                        coverage.append(name + "." + prop)
            if name == "links":
                tx.run(
                    "MATCH (a:Item {id: $first}), (b:Item {id: $last}) "
                    "CREATE (a)-[n:LINK]->(b) SET n += $row",
                    first=row["from_item"],
                    last=row["to_item"],
                    row=row,
                )
            else:
                tx.run(f"CREATE (n:{held.label}) SET n += $row", row=row)
            if history and held.group == "projection":
                entity = {
                    "items": "item",
                    "links": "link",
                    "evidence": "evidence",
                    "beliefs": "belief",
                }.get(name, "registry_row")
                key = row.get("id", row.get("item_id"))
                add_entry(
                    tx,
                    stamp,
                    entity_type=entity,
                    entity_id=key,
                    field="row",
                    new=row_json(name, row, world_id),
                    world_time=AT,
                    reason_id=add_text(tx, stamp, named + " create reason"),
                )
            if keep:
                retained.append(
                    {
                        "table": name,
                        "key": row.get("id", row.get("item_id")),
                        "fields": {
                            k: v
                            for k, v in row.items()
                            if k
                            not in {
                                p for c in held.columns if c.type == "value_id" for p in c.props
                            }
                        },
                    }
                )
            return row

        add(
            "kinds",
            {
                "id": ids["kind"],
                "name": named + " kind",
                "lifecycle_class": "work_with_state",
                "status_vocabulary": json.dumps(
                    [{"status": named, "role": "open"}, {"status": "retained", "role": "open"}]
                ),
                "status": "canonical",
                "origin": "minted",
                "required_fields": "[]",
                "required_links": "[]",
            },
        )
        add(
            "meanings",
            {
                "id": ids["meaning"],
                "name": named + " meaning",
                "status": "canonical",
                "direction": "from_to",
                "cardinality": "many",
                "temporal_class": "state",
                "from_kinds": '{"any":true}',
                "to_kinds": '{"any":true}',
            },
        )
        add(
            "property_names",
            {
                "id": ids["property"],
                "kind": ids["kind"],
                "name": named + " property",
                "value_type": "text",
                "temporal_class": "permanent",
                "worked_example": ids["evidence"],
            },
        )
        add(
            "items",
            {
                "id": ids["item"],
                "kind": ids["kind"],
                "title": item_word,
                "summary": item_word + " summary",
                "body": item_word + " body",
                "properties": json.dumps({ids["property"]: item_word}),
                "property_values": [item_word],
                "search_terms": [item_word + " search"],
                "recorded": AT,
                "became_true_precision": "unknown",
            },
        )
        add(
            "items",
            {
                "id": ids["field"],
                "kind": ids["kind"],
                "title": retained_word + " field owner",
                "body": field_word + " previous",
                "summary": retained_word,
                "recorded": AT,
                "became_true_precision": "unknown",
            },
        )
        add_entry(
            tx,
            stamp,
            entity_type="item",
            entity_id=ids["field"],
            field="body",
            old=field_word + " previous",
            new=field_word,
            world_time=AT,
            reason_id=add_text(tx, stamp, named + " edit reason"),
        )
        tx.run("MATCH (n:Item {id: $id}) SET n.body = $body", id=ids["field"], body=field_word)
        coverage.extend(
            ["change_entries.old_value_id", "change_entries.new_value_id", "change_entries.reason"]
        )
        add(
            "items",
            {
                "id": ids["control"],
                "kind": ids["kind"],
                "title": retained_word,
                "body": named + " current body",
                "summary": named + " current summary",
                "status": named,
                "properties": json.dumps({ids["property"]: named}),
                "property_values": [named],
                "search_terms": [named + " search"],
                "recorded": AT,
                "became_true_precision": "unknown",
            },
        )
        add(
            "items",
            {
                "id": ids["belief"],
                "kind": KIND_BELIEF,
                "title": retained_word + " belief",
                "recorded": AT,
                "became_true_precision": "unknown",
            },
        )
        add(
            "beliefs",
            {"item_id": ids["belief"], "state": "established", "origin": "earned_from_episodes"},
        )
        add(
            "links",
            {
                "id": ids["link"],
                "from_item": ids["item"],
                "to_item": ids["control"],
                "meaning": ids["meaning"],
                "recorded": AT,
                "became_true_precision": "unknown",
            },
        )
        add(
            "aliases",
            {
                "id": ids["alias"],
                "phrase": item_word + " alias",
                "normalised_phrase": normalise_phrase(item_word + " alias"),
                "target_type": "item",
                "target_id": ids["item"],
                "reversed": False,
            },
        )
        add(
            "aliases",
            {
                "id": ids["named_alias"],
                "phrase": named + " alias",
                "normalised_phrase": normalise_phrase(named + " alias"),
                "target_type": "item",
                "target_id": ids["control"],
                "reversed": False,
                "worked_example": ids["evidence"],
            },
        )
        add(
            "capabilities",
            {
                "id": ids["capability"],
                "connector_type": "past_ai_conversations",
                "registry_id": ids["kind"],
                "capability": "shows_said",
                "origin": "the_person",
            },
        )
        for label, target, key in (
            ("evidence", "belief", 0),
            ("control_evidence", "control", 1),
            ("item_evidence", "item", 1),
        ):
            add(
                "evidence",
                {
                    "id": ids[label],
                    "target_type": "belief" if target == "belief" else "item",
                    "target_id": ids[target],
                    "source_id": source,
                    "episode_id": episode,
                    "message_key": messages[key]["key"],
                    "locator": "key:" + messages[key]["key"],
                    "active": True,
                    "polarity": "supports",
                    "recorded": AT,
                    "source_type": "said",
                    "derivation": "observed",
                    "speaker_kind": "person",
                    "modality": "text",
                    "statement_class": "report_state",
                },
            )
        add(
            "holds",
            {
                "id": ids["hold"],
                "episode_id": episode,
                "locator": "key:" + messages[0]["key"],
                "state": "open",
            },
        )
        add(
            "unresolved_mentions",
            {
                "id": ids["mention"],
                "episode_id": episode,
                "locator": "key:" + messages[0]["key"],
                "state": "open",
            },
        )
        add(
            "holds",
            {
                "id": ids["control_hold"],
                "episode_id": episode,
                "locator": "key:" + messages[1]["key"],
                "state": "open",
            },
            keep=True,
        )
        add(
            "unresolved_mentions",
            {
                "id": ids["control_mention"],
                "episode_id": episode,
                "locator": "key:" + messages[1]["key"],
                "state": "open",
            },
            keep=True,
        )
        add("map_positions", {"id": ids["position"], "item_id": ids["item"], "x": 1.0, "y": 2.0})
        add(
            "run_records",
            {
                "id": new_id(world_id),
                "proposed_operations": json.dumps({"ops": [{"summary": named}]}),
            },
        )
        # All remaining snapshot value-id columns: reasons, lessons, questions and audits.
        covered = {field.split(".")[0] for field in coverage}
        for name, held in WORLD_TABLES.items():
            if held.group != "snapshot" or name in covered | {"change_entries", "history_values"}:
                continue
            if any(c.type == "value_id" for c in held.columns):
                keys = {key: new_id(world_id) for key in held.uniques[0].props if key != "world_id"}
                add(name, keys)
        # Free text, old scalar columns, escaped JSON, and nested row images all remain physical.
        add_text(tx, stamp, named + " old scalar")
        add_text(tx, stamp, json.dumps({"nested": [named + ' escaped "value"']}))
        for name, image in (
            (
                "sources",
                {"id": source, "content": json.dumps(messages), "external_locator": external},
            ),
            (
                "run_records",
                {"id": new_id(world_id), "proposed_operations": json.dumps({"summary": named})},
            ),
        ):
            add_text(tx, stamp, json.dumps(row_json(name, image, world_id)))
        add_text(tx, stamp, retained_word + " unchanged history")

    WorldWriter(driver, world_id, timeout=600).transaction(work)
    # A physical restore reconstructs these rows through the same replay. Establish
    # that canonical form before the baseline records exact control-world bytes.
    rebuild_in_place(driver, world_id)
    return {
        "world_id": world_id,
        "targets": [
            {"type": "named_value", "value": named},
            {"type": "item", "item_id": ids["item"]},
            {"type": "field_value", "item_id": ids["field"], "field": "body"},
            {"type": "source_span", "source_id": source, "locator": "key:" + messages[0]["key"]},
        ],
        "canaries": canaries,
        "retained_word": retained_word,
        "retained": retained,
        "ids": ids,
        "coverage": sorted(set(coverage)),
        "import_payload": {"external_locator": external, "messages": messages[:2]},
    }


def assert_forget_drill(driver, fixture):
    """Scan every property of every row, then assert positive retained/cascade controls."""
    rows = read_inventory(WorldQuery(driver, fixture["world_id"]))
    physical = _canonical(rows)
    for canary in fixture["canaries"]:
        assert canary.casefold() not in physical.casefold(), "a forbidden value survived"
    ids = fixture["ids"]

    def by_id(name):
        return {r.get("id", r.get("item_id")): r for r in rows[name]}

    assert ids["item"] not in by_id("items")
    assert ids["link"] not in by_id("links")
    assert ids["alias"] not in by_id("aliases")
    assert ids["position"] not in by_id("map_positions")
    assert ids["hold"] not in by_id("holds")
    assert ids["mention"] not in by_id("unresolved_mentions")
    assert ids["control_hold"] in by_id("holds")
    assert ids["control_mention"] in by_id("unresolved_mentions")
    assert by_id("items")[ids["field"]]["summary"] == fixture["retained_word"]
    source = by_id("sources")[ids["source"]]
    messages = json.loads(source["content"])
    assert len(messages) == 1 and messages[0]["text"] == fixture["retained_word"]
    assert source["forget_state"] != "forgotten"
    evidence = by_id("evidence")[ids["evidence"]]
    assert evidence["active"] is False and evidence["end_reason"] == "forget"
    assert by_id("evidence")[ids["control_evidence"]]["active"] is True
    assert ids["item_evidence"] not in by_id("evidence")
    assert any(r.get("belief_id") == ids["belief"] for r in rows["support_changed"])
    assert not any(
        r.get("worked_example") == ids["evidence"] for current in rows.values() for r in current
    )
    for held in fixture["retained"]:
        actual = by_id(held["table"])[held["key"]]
        for field, value in held["fields"].items():
            assert actual[field] == value
    assert fixture["retained_word"] in physical
    return {
        "tables": len(rows),
        "rows": sum(map(len, rows.values())),
        "value_columns": len(fixture["coverage"]),
    }


def reimport_forget_drill(driver, fixture):
    """An actual import writer rejects the original export atomically on every attempt."""
    payload = fixture["import_payload"]
    screened = Screener(ScreeningConfig()).screen_source(
        payload["external_locator"],
        [
            Message(
                row["key"],
                row.get("sender"),
                row["text"],
                at=AT,
                speaker_kind=row["speaker_kind"],
                modality=row["modality"],
            )
            for row in payload["messages"]
        ],
    )
    query = WorldQuery(driver, fixture["world_id"])
    before = _canonical(read_inventory(query))
    try:
        write_chat_delta(
            driver,
            fixture["world_id"],
            screened,
            connector_id=fixture["ids"]["connector"],
            parser_version="drill.v1",
            delta_keys=[m["key"] for m in payload["messages"]],
            episodes=[],
            newest_at=AT,
        )
    except ForgottenSource:
        pass
    else:
        raise AssertionError("the erased export reached the source writer")
    assert _canonical(read_inventory(query)) == before
    assert_forget_drill(driver, fixture)
