"""Raw historical fixtures for the belief history consumer tests."""

import pytest

from grove import clock
from grove.ids import new_id
from grove.store.primitives import KIND_BELIEF, MEANING_DESCRIBES
from grove.writer.history import Stamp, add_entry, add_text, row_json
from grove.writer.tx import WorldWriter
from tests.writer.helpers import at_minute, read


def stamp(world_id):
    return Stamp(
        world_id, clock.now(world_id), "agent:connector", new_id(world_id), new_id(world_id)
    )


def belief_row(driver, world_id, item_id):
    return read(driver, world_id, "MATCH (b:Belief {item_id: $id}) RETURN b", id=item_id)[0]["b"]


@pytest.fixture
def held(driver, seeded):
    """A minimal stored belief: both same-ID rows have their own create history."""
    w = seeded["world_id"]
    item_id = new_id(w)
    subject_id = new_id(w)
    made = stamp(w)

    def work(tx):
        tx.lock_world()
        reason = add_text(tx, made, "Two episodes taught the same lesson")
        subject = {
            "id": subject_id,
            "kind": seeded["kinds"]["person"],
            "title": "Rowan",
            "became_true_precision": "unknown",
            "recorded": made.at,
            "version": 1,
            "touched_at": made.at,
        }
        tx.run("CREATE (i:Item) SET i += $row", row=subject)
        add_entry(
            tx,
            stamp(w),
            entity_type="item",
            entity_id=subject_id,
            field="row",
            new=row_json("items", subject, w),
            world_time=made.at,
            reason_id=reason,
        )
        item = {
            "id": item_id,
            "kind": KIND_BELIEF,
            "title": "Short notes help review",
            "became_true_precision": "unknown",
            "derivation": "inferred",
            "speaker": "automated",
            "recorded": made.at,
            "version": 1,
            "touched_at": made.at,
        }
        belief = {
            "item_id": item_id,
            "state": "tentative",
            "state_since": made.at,
            "origin": "earned_from_episodes",
            "supporting_episodes": ["episode-a", "episode-b"],
            "last_support_at": at_minute(2),
            "externally_testable": False,
            "retrieved": 0,
            "cited": 0,
            "version": 1,
            "touched_at": made.at,
        }
        for entity, table, label, row in (
            ("item", "items", "Item", item),
            ("belief", "beliefs", "Belief", belief),
        ):
            tx.run(f"CREATE (n:{label}) SET n += $row", row=row)
            add_entry(
                tx,
                made,
                entity_type=entity,
                entity_id=item_id,
                field="row",
                new=row_json(table, row, w),
                world_time=made.at,
                reason_id=reason,
            )
        anchor = {
            "id": new_id(w),
            "from_item": item_id,
            "to_item": subject_id,
            "meaning": MEANING_DESCRIBES,
            "derivation": "inferred",
            "became_true_precision": "unknown",
            "active_key": f"{w}|{item_id}|{MEANING_DESCRIBES}|{subject_id}",
            "recorded": made.at,
            "version": 1,
            "touched_at": made.at,
        }
        tx.run(
            "MATCH (a:Item {id: $a}), (b:Item {id: $b}) CREATE (a)-[l:LINK]->(b) SET l += $row",
            a=item_id,
            b=subject_id,
            row=anchor,
        )
        add_entry(
            tx,
            made,
            entity_type="link",
            entity_id=anchor["id"],
            field="row",
            new=row_json("links", anchor, w),
            world_time=made.at,
            reason_id=reason,
        )

    WorldWriter(driver, w).transaction(work)
    return w, item_id, made.change_set_id


def move(driver, w, item_id, fields):
    changed = stamp(w)

    def work(tx):
        tx.lock_world()
        old = tx.run("MATCH (b:Belief {item_id: $id}) RETURN b", id=item_id)[0]["b"]
        reason = add_text(tx, changed, "The new evidence changes the belief")
        for field, value in fields.items():
            add_entry(
                tx,
                changed,
                entity_type="belief",
                entity_id=item_id,
                field=field,
                old=old.get(field),
                new=value,
                world_time=changed.at,
                reason_id=reason,
            )
        tx.run(
            "MATCH (b:Belief {item_id: $id}) SET b += $fields, "
            "b.version = b.version + 1, b.touched_at = $at",
            id=item_id,
            fields={key: None if value == [] else value for key, value in fields.items()},
            at=changed.at,
        )

    WorldWriter(driver, w).transaction(work)
    return changed.change_set_id


def forget_value(driver, world_id, value_id):
    WorldWriter(driver, world_id).transaction(
        lambda tx: tx.run(
            "MATCH (v:HistoryValue {id: $id}) SET v.value = '[forgotten]', v.forgotten_at = $at",
            id=value_id,
            at=clock.now(world_id),
        )
    )
