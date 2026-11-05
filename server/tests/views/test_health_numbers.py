"""The health answer on a planted world: every number comes from stored rows.

The clock is fixed, so each expected number below is worked out by hand from the rows planted here.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from grove import clock
from grove.store.errors import STORE_UNAVAILABLE
from grove.store.query import WorldQuery
from grove.writer.testing import plant_link, plant_node
from tests.views.conftest import plant_keyed_link

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SINCE_TEXT = "2026-10-19T12:00:00.000Z"
OCT_10 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
OCT_19_EARLY = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
OCT_19_LATE = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
OCT_20 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen():
    fixed = clock.TestClock()
    fixed.set(NOW)
    with clock.using(fixed):
        yield fixed


def plant_kind(driver, world, name="task"):
    return plant_node(driver, world, "Kind", name=name, status_vocabulary="[]")["id"]


def plant_meaning(driver, world):
    return plant_node(driver, world, "Meaning", name="depends on", cardinality="many")["id"]


def plant_item(driver, world, kind, title, recorded, **props):
    row = plant_node(driver, world, "Item", kind=kind, title=title, recorded=recorded, **props)
    return row["id"]


def plant_connector(driver, world, provider, outcome):
    return plant_node(
        driver,
        world,
        "Connector",
        type="upload",
        provider=provider,
        instance_key=provider,
        last_outcome=outcome,
    )


def health(client):
    response = client.get("/v1/views/health")
    assert response.status_code == 200
    return response.json()


def plant_three_days(driver, world):
    """Five active items and three active links, on 10, 19 and 20 October, plus ended rows."""
    task, place = plant_kind(driver, world), plant_kind(driver, world, "place")
    meaning = plant_meaning(driver, world)
    # The world time is a year older than the record time: only the record time places a row.
    long_ago = datetime(2025, 10, 1, 9, 0, tzinfo=UTC)
    lease = plant_item(driver, world, task, "Harbor lease", OCT_10, became_true_earliest=long_ago)
    chart = plant_item(driver, world, task, "Tide chart", OCT_10)
    survey = plant_item(driver, world, place, "Dock survey", OCT_19_EARLY)
    permit = plant_item(driver, world, task, "Crane permit", OCT_19_LATE)
    repair = plant_item(driver, world, task, "Pier repair", OCT_20)
    plant_item(driver, world, task, "Ruled out berth", OCT_19_LATE, end_class="refuted")

    plant_keyed_link(driver, world, lease, chart, meaning, recorded=OCT_10)
    plant_keyed_link(
        driver, world, chart, survey, meaning, recorded=OCT_19_EARLY, became_true_earliest=long_ago
    )
    plant_keyed_link(driver, world, survey, permit, meaning, recorded=OCT_20)
    # An ended link keeps its row and loses active_key. Its day holds no other row.
    plant_link(
        driver,
        world,
        permit,
        repair,
        meaning=meaning,
        recorded=datetime(2026, 10, 18, 10, 0, tzinfo=UTC),
        end_class="ended_in_the_world",
    )
    return lease


def test_growth_additions_and_totals_from_stored_rows(driver, make_world, client_for):
    world = make_world()
    plant_three_days(driver, world)

    payload = health(client_for(world))

    assert (payload["nodes"], payload["edges"]) == (5, 3)
    assert payload["by_type"] == {"task": 4, "place": 1}
    assert payload["growth"] == [
        {"at": "2026-10-10", "nodes": 2, "edges": 1},
        {"at": "2026-10-19", "nodes": 4, "edges": 2},
        {"at": "2026-10-20", "nodes": 5, "edges": 3},
    ]
    assert payload["additions"] == {"nodes": 2, "edges": 1, "since": SINCE_TEXT}


def test_another_world_does_not_leak(driver, make_world, client_for):
    world, other = make_world(), make_world()
    lease = plant_three_days(driver, world)
    kind, meaning = plant_kind(driver, other), plant_meaning(driver, other)
    first = plant_item(driver, other, kind, "Orchard deed", OCT_19_LATE)
    second = plant_item(driver, other, kind, "Well report", datetime(2026, 10, 12, tzinfo=UTC))
    plant_keyed_link(driver, other, first, second, meaning, recorded=OCT_20)
    plant_connector(driver, other, "chatgpt", "failed")
    # A link that crosses from the other world into this one belongs to neither count.
    plant_link(
        driver,
        other,
        first,
        lease,
        to_world=world,
        meaning=meaning,
        active_key=f"{other}|crossing",
        recorded=OCT_20,
    )

    payload = health(client_for(world))

    assert (payload["nodes"], payload["edges"]) == (5, 3)
    assert [point["at"] for point in payload["growth"]] == [
        "2026-10-10",
        "2026-10-19",
        "2026-10-20",
    ]
    assert payload["additions"] == {"nodes": 2, "edges": 1, "since": SINCE_TEXT}
    assert payload["checks"] == {"graph": "ready", "sources": "ready"}

    theirs = health(client_for(other))
    assert (theirs["nodes"], theirs["edges"]) == (2, 1)
    assert theirs["growth"] == [
        {"at": "2026-10-12", "nodes": 1, "edges": 0},
        {"at": "2026-10-19", "nodes": 2, "edges": 0},
        {"at": "2026-10-20", "nodes": 2, "edges": 1},
    ]
    assert theirs["checks"]["sources"] == "attention"


def test_an_empty_world_answers_one_point_with_its_totals(make_world, client_for):
    payload = health(client_for(make_world()))
    assert payload["growth"] == [{"at": "2026-10-20", "nodes": 0, "edges": 0}]
    assert payload["additions"] == {"nodes": 0, "edges": 0, "since": SINCE_TEXT}


def test_only_ended_rows_count_as_no_change(driver, make_world, client_for):
    world = make_world()
    kind = plant_kind(driver, world)
    plant_item(driver, world, kind, "Ruled out berth", OCT_10, end_class="refuted")

    payload = health(client_for(world))

    assert (payload["nodes"], payload["by_type"]) == (0, {})
    assert payload["growth"] == [{"at": "2026-10-20", "nodes": 0, "edges": 0}]


def test_a_row_with_no_record_time_stays_in_the_totals(driver, make_world, client_for):
    undated, dated = make_world(), make_world()
    kind = plant_kind(driver, undated)
    plant_node(driver, undated, "Item", kind=kind, title="Old ledger")
    kind = plant_kind(driver, dated)
    plant_node(driver, dated, "Item", kind=kind, title="Old ledger")
    plant_item(driver, dated, kind, "Harbor lease", OCT_10)

    assert health(client_for(undated))["growth"] == [{"at": "2026-10-20", "nodes": 1, "edges": 0}]
    assert health(client_for(dated))["growth"] == [{"at": "2026-10-10", "nodes": 2, "edges": 0}]


def test_growth_keeps_the_last_thirty_change_days_of_both_tables(
    driver, make_world, client_for, monkeypatch
):
    """Items change on odd days and links on even days: 70 days, 35 of each table."""
    world = make_world()
    kind, meaning = plant_kind(driver, world), plant_meaning(driver, world)
    start = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    first = plant_item(driver, world, kind, "Harbor lease", start - timedelta(days=30))
    second = plant_item(driver, world, kind, "Tide chart", start - timedelta(days=30))
    item_times, link_times = [start - timedelta(days=30)] * 2, []
    for day in range(70):
        at = start + timedelta(days=day)
        if day % 2:
            plant_item(driver, world, kind, f"Crate {day}", at)
            item_times.append(at)
        else:
            plant_link(
                driver,
                world,
                first,
                second,
                meaning=meaning,
                active_key=f"{world}|{day}",
                recorded=at,
            )
            link_times.append(at)

    client = client_for(world)
    real, day_rows = WorldQuery.read, []

    def keep_day_rows(self, cypher, /, **params):
        rows = real(self, cypher, **params)
        if "UNION ALL" in cypher:
            day_rows.extend(rows)
        return rows

    monkeypatch.setattr(WorldQuery, "read", keep_day_rows)

    growth = health(client)["growth"]

    days = [(start + timedelta(days=day)).date().isoformat() for day in range(40, 70)]
    assert [point["at"] for point in growth] == days
    for point in growth:
        end = date.fromisoformat(point["at"])
        assert point["nodes"] == sum(at.date() <= end for at in item_times)
        assert point["edges"] == sum(at.date() <= end for at in link_times)
    assert (growth[-1]["nodes"], growth[-1]["edges"]) == (37, 35)
    # The store answers 30 days of each table at most, never every day of the world.
    assert len(day_rows) == 60


def test_a_day_ends_at_midnight_utc(driver, make_world, client_for):
    world = make_world()
    kind = plant_kind(driver, world)
    plant_item(driver, world, kind, "Late", datetime(2026, 10, 9, 23, 59, 59, tzinfo=UTC))
    plant_item(driver, world, kind, "Early", datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    # 22:00 on the 9th at this offset is 03:00 on the 10th in UTC.
    offset = timezone(timedelta(hours=-5))
    plant_item(driver, world, kind, "Offset", datetime(2026, 10, 9, 22, 0, tzinfo=offset))

    assert health(client_for(world))["growth"] == [
        {"at": "2026-10-09", "nodes": 1, "edges": 0},
        {"at": "2026-10-10", "nodes": 3, "edges": 0},
    ]


def test_additions_run_after_since_up_to_now(driver, make_world, client_for):
    world = make_world()
    kind, meaning = plant_kind(driver, world), plant_meaning(driver, world)
    since, tick = NOW - timedelta(hours=24), timedelta(milliseconds=1)
    first = plant_item(driver, world, kind, "At since", since)
    second = plant_item(driver, world, kind, "Just after since", since + tick)
    third = plant_item(driver, world, kind, "At now", NOW)
    plant_item(driver, world, kind, "Just after now", NOW + tick)
    plant_keyed_link(driver, world, first, second, meaning, recorded=since)
    plant_keyed_link(driver, world, second, third, meaning, recorded=NOW)

    payload = health(client_for(world))

    assert payload["additions"] == {"nodes": 2, "edges": 1, "since": SINCE_TEXT}
    assert (payload["nodes"], payload["edges"]) == (4, 2)


def test_the_answer_moves_with_the_clock(driver, make_world, client_for, frozen):
    world = make_world()
    plant_three_days(driver, world)
    frozen.advance(hours=24)

    payload = health(client_for(world))

    assert payload["additions"] == {"nodes": 0, "edges": 0, "since": "2026-10-20T12:00:00.000Z"}
    assert payload["generated_at"] == "2026-10-21T12:00:00.000Z"


@pytest.mark.parametrize("outcome", ["took_in", "nothing_new", "denied", "kept_out", None])
def test_sources_are_ready_with_no_failed_connector(driver, make_world, client_for, outcome):
    world = make_world()
    plant_connector(driver, world, "chatgpt", outcome)
    assert health(client_for(world))["checks"] == {"graph": "ready", "sources": "ready"}


def test_one_failed_connector_asks_for_attention(driver, make_world, client_for):
    world = make_world()
    plant_connector(driver, world, "chatgpt", "took_in")
    plant_connector(driver, world, "codex", "failed")

    payload = health(client_for(world))

    assert payload["checks"] == {"graph": "ready", "sources": "attention"}
    assert payload["status"] == "healthy"
    assert payload["overall"] == {"state": "healthy"}


@pytest.mark.parametrize("words", ["i.kind AS kind", "RETURN count(l)", "UNION ALL", ":Connector"])
def test_a_failed_store_read_answers_no_ready_check(make_world, client_for, monkeypatch, words):
    """graph reads ready only after the reads worked: any failed read refuses the whole answer."""
    client = client_for(make_world())
    real = WorldQuery.read

    def fail_one(self, cypher, /, **params):
        if words in cypher:
            raise STORE_UNAVAILABLE[0]("the store went away")
        return real(self, cypher, **params)

    monkeypatch.setattr(WorldQuery, "read", fail_one)

    response = client.get("/v1/views/health")

    assert response.status_code == 503
    assert "checks" not in response.json()
