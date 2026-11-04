"""D3 inverses of D9 history, independent of the transition producer.

These fixtures plant history with the writer's history primitives, like the D3 restore drill.
They test the consumer against stored state, rather than duplicate the D9 transition rules.
Operation-level D9 tests separately prove which transitions may produce this history.
"""

import json

import pytest

from grove import clock
from grove.ids import new_id
from grove.store.d3_fingerprint import live_fingerprint
from grove.store.primitives import KIND_BELIEF, MEANING_DESCRIBES
from grove.writer.history import Stamp, add_entry, add_text, row_json
from grove.writer.restore import rebuild_in_place, restore, take_snapshot
from grove.writer.tx import WorldWriter
from tests.writer.helpers import apply, at_minute, read
from tests.writer.test_contract_d2 import one, refused
from tests.writer.test_contract_d3 import entries_of_set, undo
from tests.writer.test_restore import change_entries, purge_world_rows


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


def content(row):
    return {key: value for key, value in row.items() if key not in {"version", "touched_at"}}


@pytest.mark.parametrize(
    "fields",
    [
        {
            "state": "established",
            "state_since": at_minute(3),
            "supporting_episodes": ["episode-a", "episode-b", "episode-c"],
            "last_support_at": at_minute(3),
        },
        {
            "state": "weakening",
            "state_since": at_minute(4),
            "contradicting_episodes": ["episode-d"],
            "last_contradiction_at": at_minute(4),
        },
        {
            "state": "superseded",
            "state_since": at_minute(5),
            "end_class": "replaced",
            "successor": "successor-belief",
        },
        {
            "state": "superseded",
            "state_since": at_minute(5),
            "end_class": "wrong",
            "supporting_episodes": [],
            "last_support_at": None,
        },
        {"held_by_the_person": at_minute(6), "origin": "persons_correction"},
        {"externally_testable": True, "retest_how": json.dumps({"source_id": "live-source"})},
    ],
)
def test_one_undo_restores_all_belief_fields_and_keeps_the_original_history(driver, held, fields):
    w, item_id, _ = held
    before = belief_row(driver, w, item_id)
    change = move(driver, w, item_id, fields)
    original = change_entries(driver, w)

    result = apply(driver, w, [undo(change)], [])

    restored = belief_row(driver, w, item_id)
    assert content(restored) == content(before)
    assert restored["version"] == before["version"] + 2
    assert change_entries(driver, w)[: len(original)] == original
    inverse = entries_of_set(driver, w, result.change_set_id)
    assert {entry["inverse_of"] for entry in inverse} == {change}
    assert {entry["field"] for entry in inverse} == set(fields)
    assert (
        read(driver, w, "MATCH (i:Item {id: $id}) RETURN i.version AS v", id=item_id)[0]["v"] == 1
    )


def test_mint_undo_retains_both_rows_and_appends_an_ended_belief(driver, held):
    w, item_id, change = held
    original = change_entries(driver, w)

    apply(driver, w, [undo(change)], [])

    belief = belief_row(driver, w, item_id)
    item = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)[0]["i"]
    assert (belief["state"], belief["end_class"], item["end_class"]) == (
        "superseded",
        "wrong",
        "refuted",
    )
    assert not belief.get("supporting_episodes")
    assert belief.get("last_support_at") is None
    assert belief["version"] == item["version"] == 2
    anchors = read(
        driver, w, "MATCH (:Item)-[l:LINK {from_item: $id}]->(:Item) RETURN l", id=item_id
    )
    assert len(anchors) == 1
    assert anchors[0]["l"]["end_class"] == "refuted"
    assert anchors[0]["l"].get("active_key") is None
    assert change_entries(driver, w)[: len(original)] == original
    assert one(refused(driver, w, [undo(change)], []), "invalid_arguments").rule == "already_undone"


def test_later_belief_change_refuses_undo_atomically(driver, held):
    w, item_id, _ = held
    change = move(driver, w, item_id, {"state": "established"})
    move(driver, w, item_id, {"state": "weakening"})
    before = belief_row(driver, w, item_id)

    refusal = one(refused(driver, w, [undo(change)], []), "version_conflict")

    assert refusal.rule == "undo_conflict"
    assert belief_row(driver, w, item_id) == before


def test_forgotten_belief_value_refuses_undo_atomically(driver, held):
    w, item_id, _ = held
    change = move(driver, w, item_id, {"state": "established"})
    entry = entries_of_set(driver, w, change)[0]
    WorldWriter(driver, w).transaction(
        lambda tx: tx.run(
            "MATCH (v:HistoryValue {id: $id}) SET v.value = '[forgotten]', v.forgotten_at = $at",
            id=entry["old_value_id"],
            at=clock.now(w),
        )
    )
    before = belief_row(driver, w, item_id)

    refusal = one(refused(driver, w, [undo(change)], []), "invalid_arguments")

    assert refusal.rule == "value_forgotten"
    assert belief_row(driver, w, item_id) == before


def test_belief_inverse_survives_rebuild_and_snapshot_restore(driver, held):
    w, item_id, _ = held
    change = move(driver, w, item_id, {"state": "established", "state_since": at_minute(3)})
    apply(driver, w, [undo(change)], [])
    before = belief_row(driver, w, item_id)
    fingerprint = live_fingerprint(driver, w)

    rebuild_in_place(driver, w)

    assert belief_row(driver, w, item_id) == before
    assert live_fingerprint(driver, w) == fingerprint
    snapshot = take_snapshot(driver, w)
    purge_world_rows(driver, w)
    restore(driver, w, snapshot, [])
    assert belief_row(driver, w, item_id) == before
    assert live_fingerprint(driver, w) == fingerprint
