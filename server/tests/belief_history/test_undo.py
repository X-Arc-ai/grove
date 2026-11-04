"""D3 inverses of D9 history, independent of the transition producer.

These fixtures plant history with the writer's history primitives, like the D3 restore drill.
They test the consumer against stored state, rather than duplicate the D9 transition rules.
Operation-level D9 tests separately prove which transitions may produce this history.
"""

import json

import pytest

from grove.store.d3_fingerprint import live_fingerprint
from grove.writer.restore import rebuild_in_place, restore, take_snapshot
from tests.writer.belief_history_helpers import belief_row, forget_value, move
from tests.writer.helpers import apply, at_minute, read
from tests.writer.test_contract_d2 import one, refused
from tests.writer.test_contract_d3 import entries_of_set, undo
from tests.writer.test_restore import change_entries, purge_world_rows


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
    forget_value(driver, w, entry["old_value_id"])
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
