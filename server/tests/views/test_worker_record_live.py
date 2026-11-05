"""The worker record as real Cypher on the dev store, in real t_ worlds with planted rows.

These reads return scalars, which the WorldLeak guard does not see, so only the scoped patterns
keep one World from another. The two-world tests below are the proof of that.

A forget deletes the item row and its links and never rewrites a history entry
(grove/writer/forget.py), so an entry that names an id with no row is the store after a forget.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.query import CYPHER_PREFIX
from grove.store.scope import scope_cypher
from grove.views.session import ViewSession
from grove.views.workers import (
    _CHANGED_RUNS,
    _TRAIL_COUNT,
    _TRAIL_IDS,
    TRAIL_ITEMS,
    TRAIL_RUNS,
    worker_record_payload,
)
from grove.writer.testing import plant_link, plant_node
from tests.views.conftest import plant_keyed_link

NOW = datetime(2026, 11, 4, 18, 0, 0, 123456, tzinfo=UTC)
ROUTE = "/v1/views/workers/record"


def stamp(at):
    return at.isoformat(timespec="microseconds").replace("+00:00", "Z")


def ago(**delta):
    return NOW - timedelta(**delta)


def plant_item(driver, world, **props):
    return plant_node(driver, world, "Item", title="Invented title", **props)["id"]


def plant_run(driver, world, started, agent="learner", outcome="ok", **props):
    finished = None if outcome == "running" else started + timedelta(minutes=1)
    return plant_node(
        driver,
        world,
        "RunRecord",
        agent=agent,
        outcome=outcome,
        started=started,
        finished=finished,
        **props,
    )


def plant_change(driver, world, run_id, entity_id, sequence, entity_type="item"):
    plant_node(
        driver,
        world,
        "ChangeEntry",
        run_id=run_id,
        entity_type=entity_type,
        entity_id=entity_id,
        sequence=sequence,
        field="title",
        recorded_at=NOW,
    )


def read(driver, world):
    return worker_record_payload(driver, ViewSession("user", world, None), now=NOW)


def learner(driver, world):
    return read(driver, world)["agents"][0]


def test_a_read_returns_only_its_own_world(driver, make_world):
    mine, other = make_world(), make_world()
    own_item = plant_item(driver, mine, touched_at=ago(days=1))
    foreign_item = plant_item(driver, other, touched_at=ago(days=1))
    foreign_end = plant_item(driver, other, touched_at=ago(days=2))
    foreign_link = plant_keyed_link(driver, other, foreign_item, foreign_end, "m")["id"]

    # The other World's runs are newer, so a missing stamp on RunRecord changes last_run.
    foreign_run = plant_run(driver, other, ago(minutes=1), outcome="failed")["id"]
    plant_change(driver, other, foreign_run, foreign_item, 1)

    # The trail of this World holds its own item, then an entry that names the other World's
    # item and one that names its link. Those two are rows of this World with foreign targets.
    own_run = plant_run(driver, mine, ago(hours=5))["id"]
    plant_change(driver, mine, own_run, own_item, 1)
    plant_change(driver, mine, own_run, foreign_item, 2)
    plant_change(driver, mine, own_run, foreign_link, 3, entity_type="link")
    # An entry of the other World under this World's run id, which names this World's item.
    plant_change(driver, other, own_run, own_item, 2)
    plant_change(driver, other, own_run, foreign_end, 3)

    # Newer runs of this World. Each has changes only through a foreign target or a foreign
    # entry, so none may be the trail.
    item_trap = plant_run(driver, mine, ago(hours=4))["id"]
    plant_change(driver, mine, item_trap, foreign_item, 4)
    link_trap = plant_run(driver, mine, ago(hours=3))["id"]
    plant_change(driver, mine, link_trap, foreign_link, 5, entity_type="link")
    entry_trap = plant_run(driver, mine, ago(hours=2))["id"]
    plant_change(driver, other, entry_trap, own_item, 4)

    result = read(driver, mine)
    agent = result["agents"][0]
    assert agent["last_run"]["started_at"] == stamp(ago(hours=2))
    assert agent["last_run"]["outcome"] == "ok"
    assert agent["trail"]["started_at"] == stamp(ago(hours=5))
    assert agent["trail"]["item_ids"] == [own_item]
    assert agent["trail"]["changed_items"] == 1
    assert result["freshness"]["total_items"] == 1
    assert result["freshness"]["newest_change_at"] == stamp(ago(days=1))
    assert result["freshness"]["oldest_change_at"] == stamp(ago(days=1))
    assert foreign_item not in json.dumps(result)

    seen = read(driver, other)
    assert seen["agents"][0]["last_run"]["outcome"] == "failed"
    assert seen["agents"][0]["trail"]["started_at"] == stamp(ago(minutes=1))
    assert seen["agents"][0]["trail"]["item_ids"] == [foreign_item]
    assert seen["freshness"]["total_items"] == 2
    assert own_item not in json.dumps(seen)


def test_the_http_route_reads_the_world_of_the_session(driver, make_world, client_for, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda *_args: NOW)
    mine, other = make_world(), make_world()
    own_item = plant_item(driver, mine, touched_at=ago(days=1))
    foreign_item = plant_item(driver, other, touched_at=ago(days=1))
    own_run = plant_run(driver, mine, ago(hours=1), agent="connector")["id"]
    plant_change(driver, mine, own_run, own_item, 1)
    foreign_run = plant_run(driver, other, ago(hours=1), agent="connector")["id"]
    plant_change(driver, other, foreign_run, foreign_item, 1)

    response = client_for(mine).get(ROUTE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema"] == "grove.worker-record.v1"
    assert body["agents"][2]["trail"]["item_ids"] == [own_item]
    assert foreign_item not in response.text and foreign_run not in response.text
    assert own_run not in response.text
    assert client_for(other).get(ROUTE).json()["agents"][2]["trail"]["item_ids"] == [foreign_item]


def operators(plan):
    yield plan["operatorType"].split("@")[0]
    for child in plan.get("children", []):
        yield from operators(child)


@pytest.mark.parametrize("query", [_CHANGED_RUNS, _TRAIL_IDS, _TRAIL_COUNT])
def test_a_change_read_seeks_each_row_and_never_scans_the_links(driver, world, query):
    """With the entry bound straight into the link pattern the planner scans every link of the
    store once per entry. This reads the plan only: EXPLAIN runs nothing."""
    with driver.session(database=DATABASE) as session:
        summary = session.run(
            CYPHER_PREFIX + "EXPLAIN " + scope_cypher(query, "read"),
            world_id=world,
            run_id="a",
            run_ids=["a"],
            limit=TRAIL_ITEMS,
        ).consume()
    found = set(operators(summary.plan))
    assert "DirectedRelationshipUniqueIndexSeek" in found
    assert "NodeUniqueIndexSeek" in found
    assert not found & {
        "AllNodesScan",
        "CartesianProduct",
        "DirectedAllRelationshipsScan",
        "DirectedRelationshipIndexScan",
        "DirectedRelationshipTypeScan",
        "NodeByLabelScan",
        "UndirectedRelationshipTypeScan",
    }


def test_a_link_entry_gives_both_endpoints(driver, world):
    start, end = plant_item(driver, world), plant_item(driver, world)
    link = plant_keyed_link(driver, world, start, end, "m")["id"]
    run = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, run, link, 1, entity_type="link")
    trail = learner(driver, world)["trail"]
    assert trail["item_ids"] == sorted([start, end])
    assert trail["changed_items"] == 2
    assert trail["has_more"] is False


def test_an_ended_link_still_names_its_endpoints(driver, world):
    start, end = plant_item(driver, world), plant_item(driver, world)
    ended = plant_link(driver, world, start, end, meaning="m")["id"]
    run = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, run, ended, 1, entity_type="link")
    assert learner(driver, world)["trail"]["item_ids"] == sorted([start, end])


def test_an_item_of_an_item_entry_and_a_link_entry_counts_once(driver, world):
    start, end = plant_item(driver, world), plant_item(driver, world)
    link = plant_keyed_link(driver, world, start, end, "m")["id"]
    run = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, run, link, 1, entity_type="link")
    plant_change(driver, world, run, start, 2)
    trail = learner(driver, world)["trail"]
    assert trail["changed_items"] == 2
    assert trail["item_ids"] == [start, end]


def test_a_removed_item_and_a_removed_link_are_dropped(driver, world):
    kept = plant_item(driver, world)
    run = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, run, new_id(world), 1)
    plant_change(driver, world, run, new_id(world), 2, entity_type="link")
    plant_change(driver, world, run, kept, 3)
    plant_change(driver, world, run, new_id(world), 4)
    trail = learner(driver, world)["trail"]
    assert trail["item_ids"] == [kept]
    assert trail["changed_items"] == 1
    assert trail["has_more"] is False


def test_a_run_whose_items_are_all_removed_is_not_the_trail(driver, world):
    kept = plant_item(driver, world)
    older = plant_run(driver, world, ago(hours=2))["id"]
    plant_change(driver, world, older, kept, 1)
    gone = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, gone, new_id(world), 2)
    plant_change(driver, world, gone, new_id(world), 3, entity_type="link")
    agent = learner(driver, world)
    assert agent["last_run"]["started_at"] == stamp(ago(hours=1))
    assert agent["trail"]["started_at"] == stamp(ago(hours=2))
    assert agent["trail"]["item_ids"] == [kept]


def test_the_newest_run_with_changes_is_the_trail(driver, world):
    newest_change, older_change = plant_item(driver, world), plant_item(driver, world)
    oldest = plant_run(driver, world, ago(hours=4))["id"]
    plant_change(driver, world, oldest, older_change, 1)
    changed = plant_run(driver, world, ago(hours=3), outcome="failed")["id"]
    plant_change(driver, world, changed, newest_change, 2)
    # Entries of other kinds are no change of an item, even when the id is an item's.
    other_kinds = plant_run(driver, world, ago(hours=2))["id"]
    plant_change(driver, world, other_kinds, older_change, 3, entity_type="evidence")
    plant_change(driver, world, other_kinds, older_change, 4, entity_type="belief")
    plant_run(driver, world, ago(hours=1), outcome="running")
    agent = learner(driver, world)
    assert agent["last_run"] == {
        "outcome": "running",
        "started_at": stamp(ago(hours=1)),
        "finished_at": None,
    }
    assert agent["trail"]["outcome"] == "failed"
    assert agent["trail"]["started_at"] == stamp(ago(hours=3))
    assert agent["trail"]["finished_at"] == stamp(ago(hours=3) + timedelta(minutes=1))
    assert agent["trail"]["item_ids"] == [newest_change]


def test_a_running_run_is_a_trail_with_no_finish_time(driver, world):
    item = plant_item(driver, world)
    run = plant_run(driver, world, ago(minutes=1), outcome="running")["id"]
    plant_change(driver, world, run, item, 1)
    trail = learner(driver, world)["trail"]
    assert trail["outcome"] == "running" and trail["finished_at"] is None
    assert trail["item_ids"] == [item]


def test_two_runs_with_one_start_order_by_id(driver, world):
    first, second = plant_item(driver, world), plant_item(driver, world)
    at = ago(hours=1)
    low = plant_run(driver, world, at, id="00000000-0000-7000-8000-000000000001")["id"]
    high = plant_run(driver, world, at, id="00000000-0000-7000-8000-000000000002")["id"]
    plant_change(driver, world, low, first, 1)
    plant_change(driver, world, high, second, 2)
    assert learner(driver, world)["trail"]["item_ids"] == [second]


@pytest.mark.parametrize("newer,found", [(TRAIL_RUNS - 1, True), (TRAIL_RUNS, False)])
def test_the_trail_looks_at_the_newest_twenty_runs_only(driver, world, newer, found):
    item = plant_item(driver, world)
    run = plant_run(driver, world, ago(days=2))["id"]
    plant_change(driver, world, run, item, 1)
    for hour in range(newer):
        plant_run(driver, world, ago(hours=hour + 1))
    agent = learner(driver, world)
    assert agent["last_run"]["started_at"] == stamp(ago(hours=1))
    assert (agent["trail"] is not None) is found
    assert TRAIL_RUNS == 20


def test_each_agent_has_its_own_runs(driver, world):
    item_a, item_b = plant_item(driver, world), plant_item(driver, world)
    run_a = plant_run(driver, world, ago(hours=2), agent="curator")["id"]
    plant_change(driver, world, run_a, item_a, 1)
    run_b = plant_run(driver, world, ago(hours=1), agent="connector", outcome="timeout")["id"]
    plant_change(driver, world, run_b, item_b, 2)
    plant_run(driver, world, ago(minutes=1), agent="another-agent")
    agents = read(driver, world)["agents"]
    assert [agent["agent"] for agent in agents] == ["learner", "curator", "connector"]
    assert agents[0]["last_run"] is None and agents[0]["trail"] is None
    assert agents[1]["trail"]["item_ids"] == [item_a]
    assert agents[2]["trail"]["item_ids"] == [item_b]
    assert agents[2]["last_run"]["outcome"] == "timeout"


def test_item_ids_are_newest_first_by_each_items_newest_entry(driver, world):
    first, second, third = (plant_item(driver, world) for _ in range(3))
    run = plant_run(driver, world, ago(hours=1))["id"]
    plant_change(driver, world, run, first, 1)
    plant_change(driver, world, run, second, 2)
    plant_change(driver, world, run, third, 3)
    plant_change(driver, world, run, first, 4)
    assert learner(driver, world)["trail"]["item_ids"] == [first, third, second]


def test_more_items_than_the_page_keep_the_newest_and_the_true_total(driver, world):
    run = plant_run(driver, world, ago(hours=1))["id"]
    items = [plant_item(driver, world) for _ in range(TRAIL_ITEMS + 2)]
    for sequence, item in enumerate(items, start=1):
        plant_change(driver, world, run, item, sequence)
    trail = learner(driver, world)["trail"]
    assert trail["changed_items"] == TRAIL_ITEMS + 2
    assert trail["item_ids"] == list(reversed(items))[:TRAIL_ITEMS]
    assert trail["has_more"] is True


def test_a_page_of_exactly_fifty_has_no_more(driver, world):
    run = plant_run(driver, world, ago(hours=1))["id"]
    for sequence in range(1, TRAIL_ITEMS + 1):
        plant_change(driver, world, run, plant_item(driver, world), sequence)
    trail = learner(driver, world)["trail"]
    assert trail["changed_items"] == TRAIL_ITEMS and trail["has_more"] is False


def test_freshness_is_exact_at_the_thirty_day_edge(driver, world):
    edge = ago(days=30)
    plant_item(driver, world, touched_at=NOW)
    plant_item(driver, world, touched_at=edge)
    plant_item(driver, world, touched_at=edge - timedelta(microseconds=1))
    plant_item(driver, world, touched_at=NOW + timedelta(microseconds=1))
    plant_item(driver, world)
    plant_item(driver, world, touched_at="2026-11-04T18:00:00Z")
    assert read(driver, world)["freshness"] == {
        "total_items": 6,
        "dated_items": 4,
        "window_days": 30,
        "fresh_items": 2,
        "newest_change_at": stamp(NOW + timedelta(microseconds=1)),
        "oldest_change_at": stamp(edge - timedelta(microseconds=1)),
    }


def test_freshness_of_an_empty_world_has_no_time(driver, world):
    result = read(driver, world)
    assert result["freshness"] == {
        "total_items": 0,
        "dated_items": 0,
        "window_days": 30,
        "fresh_items": 0,
        "newest_change_at": None,
        "oldest_change_at": None,
    }
    assert [agent["last_run"] for agent in result["agents"]] == [None, None, None]


def test_a_world_of_undated_items_has_no_time(driver, world):
    plant_item(driver, world)
    assert read(driver, world)["freshness"] == {
        "total_items": 1,
        "dated_items": 0,
        "window_days": 30,
        "fresh_items": 0,
        "newest_change_at": None,
        "oldest_change_at": None,
    }


def test_the_payload_holds_no_text_and_no_run_id(driver, world):
    item = plant_item(driver, world, summary="Invented summary", touched_at=ago(days=1))
    run = plant_run(
        driver,
        world,
        ago(hours=1),
        proposed_operations=json.dumps({"body": "Invented operations"}),
        input_ids=["invented-input"],
    )["id"]
    plant_change(driver, world, run, item, 1)
    text = json.dumps(read(driver, world))
    assert item in text
    assert run not in text
    assert "Invented" not in text and "invented-input" not in text and world not in text
