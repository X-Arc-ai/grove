"""A belief and its item share an ID, but never a projection version or field head."""

import pytest

from grove.store.primitives import KIND_BELIEF
from grove.store.replay import ReplayRefused
from tests.store.test_replay import Book, recorded, world


def belief_book():
    book = Book()
    item_id = book.id("belief")
    item_op = book.op()
    book.row(
        item_op,
        "item",
        "items",
        {"id": item_id, "kind": KIND_BELIEF, "title": "Short notes help review"},
    )
    belief_op = book.op()
    book.row(
        belief_op,
        "belief",
        "beliefs",
        {
            "item_id": item_id,
            "state": "tentative",
            "state_since": recorded(2).isoformat(),
            "origin": "earned_from_episodes",
            "supporting_episodes": ["episode-a", "episode-b"],
        },
    )
    return book, item_id


def test_shared_item_and_belief_id_has_independent_versions_and_touched_times():
    book, item_id = belief_book()
    book.add(book.op(), "belief", item_id, "state", "established", "tentative")
    book.add(book.op(), "item", item_id, "summary", "A useful review pattern")
    book.add(book.op(), "belief", item_id, "state", "weakening", "established")

    projection = book.rebuild()

    assert projection.items[item_id]["version"] == 2
    assert projection.items[item_id]["touched_at"] == recorded(4)
    belief = projection.rows["beliefs"][item_id]
    assert belief["version"] == 3
    assert belief["touched_at"] == recorded(5)
    assert belief["state"] == "weakening"
    assert projection.items[item_id]["summary"] == "A useful review pattern"
    earlier = book.rebuild(known_at=recorded(3))
    assert earlier.rows["beliefs"][item_id]["state"] == "established"
    assert "summary" not in earlier.items[item_id]


def test_belief_end_class_does_not_replace_the_items_end_world_time():
    book, item_id = belief_book()
    book.add(book.op(), "item", item_id, "end_class", "superseded", world_time=world(5))
    book.add(book.op(), "belief", item_id, "end_class", "replaced", world_time=world(8))

    projection = book.rebuild()

    assert projection.ended_at[item_id] == world(5)
    assert projection.items[item_id]["end_class"] == "superseded"
    assert projection.rows["beliefs"][item_id]["end_class"] == "replaced"


def test_confirmed_support_cannot_turn_a_rebuilt_belief_into_an_observation():
    book, item_id = belief_book()
    book.row(
        book.op(),
        "evidence",
        "evidence",
        {
            "id": book.id("evidence"),
            "target_type": "belief",
            "target_id": item_id,
            "polarity": "supports",
            "active": True,
            "source_type": "confirmed",
            "derivation": "observed",
            "speaker_kind": "person",
        },
    )

    assert book.rebuild().items[item_id]["derivation"] == "inferred"


def test_belief_empty_episode_set_round_trips_the_canonical_absent_property():
    book, item_id = belief_book()
    book.add(book.op(), "belief", item_id, "contradicting_episodes", ["episode-c"], [])
    book.add(book.op(), "belief", item_id, "contradicting_episodes", [], ["episode-c"])

    assert not book.rebuild().rows["beliefs"][item_id].get("contradicting_episodes")


def test_belief_retest_recipe_round_trips_json_and_instants():
    book, item_id = belief_book()
    book.add(book.op(), "belief", item_id, "retest_how", {"source_id": "source-a"})
    book.add(
        book.op(),
        "belief",
        item_id,
        "retest_how",
        '{"source_id": "source-b"}',
        {"source_id": "source-a"},
    )
    book.add(
        book.op(),
        "belief",
        item_id,
        "state_since",
        recorded(4).isoformat(),
        recorded(2).isoformat(),
    )

    belief = book.rebuild().rows["beliefs"][item_id]
    assert belief["retest_how"] == '{"source_id": "source-b"}'
    assert belief["state_since"] == recorded(4)


@pytest.mark.parametrize("collision", ["belief", "evidence"])
def test_only_the_item_belief_pair_can_share_an_id(collision):
    book, item_id = belief_book()
    table = "beliefs" if collision == "belief" else "evidence"
    row = {"item_id" if collision == "belief" else "id": item_id}
    book.row(book.op(), collision, table, row)

    with pytest.raises(ReplayRefused, match="id_not_new"):
        book.rebuild()


def test_belief_state_replay_rejects_a_value_outside_the_state_machine():
    book, item_id = belief_book()
    book.add(book.op(), "belief", item_id, "state", "confirmed", "tentative")

    with pytest.raises(ReplayRefused, match="bad_belief_value"):
        book.rebuild()
