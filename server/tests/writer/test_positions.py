"""The writer of :MapPosition, live: insert-only, own world only, checked input, race-safe."""

import math
import threading
from datetime import UTC, datetime

import pytest

import grove.writer.positions as positions_module
from grove import clock
from grove.clock import TestClock
from grove.store.connection import DATABASE
from grove.store.errors import Neo4jError, ScopeError
from grove.writer.positions import (
    MAX_POSITIONS,
    InvalidPositions,
    check_positions,
    place_items,
)
from grove.writer.testing import plant_node

ROUNDS = 12


def rows(driver, world_id):
    with driver.session(database=DATABASE) as session:
        found = session.run(
            "MATCH (p:MapPosition {world_id: $w}) "
            "RETURN p.id AS id, p.item_id AS item_id, p.x AS x, p.y AS y, p.placed_at AS at "
            "ORDER BY p.item_id",
            w=world_id,
        )
        return [record.data() for record in found]


def place(driver, world_id, *triples):
    return place_items(driver, world_id, [{"id": i, "x": x, "y": y} for i, x, y in triples])


def test_first_call_places_and_a_later_call_keeps(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    b = plant_node(driver, world, "Item")["id"]
    assert place(driver, world, (a, 1.25, -2.5)) == {"placed": 1, "kept": 0, "unknown": 0}
    assert place(driver, world, (a, 9.0, 9.0), (b, 3.0, 4.0)) == {
        "placed": 1,
        "kept": 1,
        "unknown": 0,
    }
    found = {row["item_id"]: (row["x"], row["y"]) for row in rows(driver, world)}
    assert found == {a: (1.25, -2.5), b: (3.0, 4.0)}


def test_a_position_never_moves_and_keeps_its_id_and_time(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    place(driver, world, (a, 7.0, 8.0))
    before = rows(driver, world)
    for x in (0.0, -1e7, 1e7, 7.000001):
        assert place(driver, world, (a, x, x))["kept"] == 1
    assert rows(driver, world) == before


def test_only_items_of_the_own_world_are_placed(driver, make_world):
    mine, other = make_world(), make_world()
    own = plant_node(driver, mine, "Item")["id"]
    foreign = plant_node(driver, other, "Item")["id"]
    kind = plant_node(driver, mine, "Kind", name="task")["id"]
    result = place(driver, mine, (own, 1, 1), (foreign, 2, 2), (kind, 3, 3), ("nothing", 4, 4))
    assert result == {"placed": 1, "kept": 0, "unknown": 3}
    assert [row["item_id"] for row in rows(driver, mine)] == [own]
    assert rows(driver, other) == []


def test_a_repeat_inside_one_call_places_once(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    assert place(driver, world, (a, 1, 1), (a, 2, 2), (a, 3, 3)) == {
        "placed": 1,
        "kept": 2,
        "unknown": 0,
    }
    assert [(row["x"], row["y"]) for row in rows(driver, world)] == [(1.0, 1.0)]


def test_placed_at_comes_from_the_clock_provider(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    at = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
    test_clock = TestClock()
    test_clock.set(at, world)
    with clock.using(test_clock):
        place(driver, world, (a, 1, 1))
    found = rows(driver, world)[0]
    assert found["at"].to_native() == at
    assert found["id"]


def test_coordinates_are_stored_as_floats(driver, world):
    a = plant_node(driver, world, "Item")["id"]
    place(driver, world, (a, 3, -4))
    found = rows(driver, world)[0]
    assert (found["x"], found["y"]) == (3.0, -4.0)
    assert isinstance(found["x"], float) and isinstance(found["y"], float)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "a string",
        {"id": "a", "x": 1, "y": 1},
        [{"id": "a", "x": 1, "y": 1}] * (MAX_POSITIONS + 1),
    ],
    ids=["empty", "text", "one object", "too many"],
)
def test_a_list_that_is_not_1_to_5000_entries_is_refused(driver, world, bad):
    with pytest.raises(InvalidPositions) as refused:
        place_items(driver, world, bad)
    assert refused.value.field == "positions"


# Each bad entry comes second, after a valid one: the call refuses all of it, so nothing lands.
@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"id": "a", "x": math.nan, "y": 1}, "positions[1].x"),
        ({"id": "a", "x": 1, "y": math.inf}, "positions[1].y"),
        ({"id": "a", "x": -math.inf, "y": 1}, "positions[1].x"),
        ({"id": "a", "x": 1e7 + 1, "y": 1}, "positions[1].x"),
        ({"id": "a", "x": 1, "y": True}, "positions[1].y"),
        ({"id": "a", "x": "1", "y": 1}, "positions[1].x"),
        ({"id": 1, "x": 1, "y": 1}, "positions[1].id"),
        ({"id": None, "x": 1, "y": 1}, "positions[1].id"),
        ({"id": "", "x": 1, "y": 1}, "positions[1].id"),
        ({"id": "a" * 129, "x": 1, "y": 1}, "positions[1].id"),
        ({"id": "a", "x": 1}, "positions[1]"),
        ({"id": "a", "x": 1, "y": 1, "world_id": "w"}, "positions[1]"),
        (("a", 1, 1), "positions[1]"),
    ],
)
def test_an_invalid_entry_is_refused_before_any_write(driver, world, entry, field):
    a = plant_node(driver, world, "Item")["id"]
    with pytest.raises(InvalidPositions) as refused:
        place_items(driver, world, [{"id": a, "x": 0, "y": 0}, entry])
    assert refused.value.field == field
    assert rows(driver, world) == []


def test_check_positions_keeps_order_and_bounds():
    assert check_positions([{"id": "b", "x": 1e7, "y": -1e7}, {"id": "a", "x": 0, "y": 0.5}]) == [
        ("b", 1e7, -1e7),
        ("a", 0.0, 0.5),
    ]
    assert len(check_positions([{"id": "a", "x": 0, "y": 0}] * MAX_POSITIONS)) == MAX_POSITIONS


def test_a_real_world_id_is_refused(driver):
    with pytest.raises(ScopeError):
        place_items(driver, "not-a-world", [{"id": "a", "x": 0, "y": 0}])


def race(driver, world_id, item_id):
    """Two threads place one item at once, with different coordinates."""
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run(x):
        try:
            barrier.wait()
            results.append(place(driver, world_id, (item_id, x, x)))
        except Exception as error:  # noqa: BLE001 - the test reports every failure
            errors.append(error)

    threads = [threading.Thread(target=run, args=(x,)) for x in (1.0, 2.0)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def test_two_threads_placing_one_item_end_with_one_position(driver, world):
    for _ in range(ROUNDS):
        item = plant_node(driver, world, "Item")["id"]
        results, errors = race(driver, world, item)
        assert errors == []
        assert sorted(r["placed"] for r in results) == [0, 1]
        assert sorted(r["kept"] for r in results) == [0, 1]
        mine = [row for row in rows(driver, world) if row["item_id"] == item]
        assert len(mine) == 1
        assert mine[0]["x"] == mine[0]["y"] in (1.0, 2.0)


def test_a_constraint_error_counts_as_kept(driver, world, monkeypatch):
    """A create that loses the race on the key runs again and finds the winner's row."""
    a = plant_node(driver, world, "Item")["id"]
    place(driver, world, (a, 1.0, 1.0))
    real = positions_module.WorldWriter
    calls = []

    class LosesOnce(real):
        def transaction(self, work):
            calls.append(1)
            if len(calls) == 1:
                raise positions_module.ConstraintError("lost the race on the key")
            return super().transaction(work)

    monkeypatch.setattr(positions_module, "WorldWriter", LosesOnce)
    assert place(driver, world, (a, 5.0, 5.0)) == {"placed": 0, "kept": 1, "unknown": 0}
    assert len(calls) == 2
    assert [(row["x"], row["y"]) for row in rows(driver, world)] == [(1.0, 1.0)]


def test_the_key_refuses_a_second_row_for_one_item(driver, world):
    """The store itself backs insert-only: a second row for one item breaks the constraint."""
    a = plant_node(driver, world, "Item")["id"]
    place(driver, world, (a, 1.0, 1.0))
    with pytest.raises(Neo4jError), driver.session(database=DATABASE) as session:
        session.run(
            "CREATE (:MapPosition {id: 'second', world_id: $w, item_id: $i, x: 2.0, y: 2.0})",
            w=world,
            i=a,
        ).consume()
    assert len(rows(driver, world)) == 1
