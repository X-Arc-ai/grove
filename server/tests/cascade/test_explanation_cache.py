"""D6 explanation cache: read purity, history packets, one write, and bounded retries."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from grove import clock
from grove.cascade_jobs import (
    explain_pending,
    load_explanation,
    packet_fingerprint,
    read_explanation,
    worlds_with_pending_explanations,
)
from grove.clock import TestClock
from grove.ids import new_id
from grove.models.errors import ProviderError
from grove.models.explain_break import Explanation, explain
from grove.screening import Screener, ScreeningConfig
from grove.store.d3_fingerprint import live_fingerprint, read_once
from grove.store.primitives import MEANING_AFFECTED_BY
from grove.store.query import WorldQuery
from grove.writer.cascade_explanations import (
    cache_explanation,
    claim_explanation_job,
    renew_explanation_job,
    request_explanation,
)
from grove.writer.runs import claim_job, end_job, enqueue_job
from grove.writer.testing import plant_link, plant_node

NOW = datetime(2027, 2, 10, 12, tzinfo=UTC)
WORLD_TIME = datetime(2027, 2, 1, 12, tzinfo=UTC)
SCREEN = Screener(ScreeningConfig())


@pytest.fixture
def frozen():
    controlled = TestClock()
    controlled.set(NOW)
    with clock.using(controlled):
        yield controlled


@pytest.fixture
def graph(driver, make_world, frozen, request):
    world = make_world()
    plant_node(driver, world, "World", next_sequence=3)
    trigger = plant_node(driver, world, "Item", title="Access rule", status="cancelled", version=3)
    affected = plant_node(driver, world, "Item", title="Film route", status="active", version=1)
    description = plant_node(driver, world, "HistoryValue", value="Needs the upstream item")
    meaning = plant_node(
        driver,
        world,
        "Meaning",
        name="depends on",
        carries_consequences=True,
        downstream_end="from",
        status="canonical",
        description=description["id"],
    )
    path_link = plant_link(
        driver,
        world,
        affected["id"],
        trigger["id"],
        meaning=meaning["id"],
        active_key="synthetic-link",
        version=1,
    )
    mark = plant_link(
        driver,
        world,
        affected["id"],
        trigger["id"],
        meaning=MEANING_AFFECTED_BY,
        became_true_earliest=WORLD_TIME,
        became_true_latest=WORLD_TIME,
        became_true_precision="instant",
        recorded=NOW,
        version=1,
    )
    change_set = new_id(world)
    create_value = plant_node(
        driver,
        world,
        "HistoryValue",
        value=json.dumps(
            {
                "table": "items",
                "row": {
                    "title": "Access rule",
                    "status": "active",
                    "last_confirmed": "2027-01-20T12:00:00+00:00",
                    "recorded": "2027-01-01T12:00:00+00:00",
                },
            }
        ),
    )
    active = plant_node(driver, world, "HistoryValue", value=json.dumps("active"))
    reported = plant_node(driver, world, "HistoryValue", value=json.dumps("reported done"))
    cancelled = plant_node(driver, world, "HistoryValue", value=json.dumps("cancelled"))
    for sequence, field_name, before, after, changed in [
        (1, "row", None, create_value["id"], new_id(world)),
        (2, "status", active["id"], reported["id"], change_set),
        (3, "status", reported["id"], cancelled["id"], new_id(world)),
    ]:
        props = {
            "sequence": sequence,
            "field": field_name,
            "entity_type": "item",
            "entity_id": trigger["id"],
            "new_value_id": after,
            "change_set_id": changed,
            "world_time": WORLD_TIME,
            "recorded_at": NOW,
        }
        if before:
            props["old_value_id"] = before
        plant_node(driver, world, "ChangeEntry", **props)
    proposal = {"proposal_id": new_id(world)} if getattr(request, "param", False) else {}
    plant_node(
        driver,
        world,
        "Impact",
        link_id=mark["id"],
        affected_item=affected["id"],
        trigger_change_set=change_set,
        trigger_source_type="reported_done",
        state="open",
        path=[path_link["id"]],
        depth=1,
        stakes="low",
        rule="R2",
        version=1,
        touched_at=NOW,
        **proposal,
    )
    return {
        "world": world,
        "trigger": trigger["id"],
        "affected": affected["id"],
        "mark": mark["id"],
        "path": path_link["id"],
        "change_set": change_set,
    }


def loaded(driver, graph):
    return read_once(driver, graph["world"], lambda source: load_explanation(source, graph["mark"]))


class Layer:
    def __init__(self, graph, error=None):
        self.graph = graph
        self.error = error
        self.calls = 0

    def respond(self, world_id, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        mark_id = kw["schema"]["properties"]["mark_id"]["enum"][0]
        item_tokens = kw["schema"]["properties"]["parts"]["items"]["enum"]
        affected = next(
            token
            for token in item_tokens
            if token.startswith("item:") and token != "item:" + self.graph["trigger"]
        )
        return SimpleNamespace(
            model_call_id=new_id(world_id),
            data={
                "mark_id": mark_id,
                "parts": [
                    affected,
                    "depends on",
                    "item:" + self.graph["trigger"],
                    ",",
                    "which has",
                    "source",
                    "time",
                    ".",
                ],
            },
        )


def result(driver, graph):
    packet = loaded(driver, graph).packet
    return explain(Layer(graph), graph["world"], packet, screener=SCREEN)


def test_packet_uses_trigger_change_history_and_value_descriptions(driver, graph):
    packet = loaded(driver, graph).packet
    assert packet["trigger"]["old"]["status"] == "active"
    assert packet["trigger"]["new"]["status"] == "reported done"
    assert packet["trigger"]["new"]["status"] != "cancelled"
    assert packet["trigger"]["clocks"]["last_confirmed"] == "2027-01-20T12:00:00+00:00"
    assert packet["trigger"]["world_time"] == WORLD_TIME.isoformat()
    assert packet["trigger"]["source_type"] == "reported_done"
    assert packet["meanings"][0]["description"] == "Needs the upstream item"
    assert [item["id"] for item in packet["path_items"]] == [graph["trigger"], graph["affected"]]


def test_uncached_read_is_immediate_and_fingerprint_pure(driver, graph):
    before = live_fingerprint(driver, graph["world"])
    response = read_explanation(driver, graph["world"], graph["mark"], screener=SCREEN)
    assert response.needs_explanation
    assert response.by == "template"
    assert "a reported change" in response.text
    assert "2027-02-01" in response.text
    assert live_fingerprint(driver, graph["world"]) == before
    assert (
        WorldQuery(driver, graph["world"]).read("MATCH (j:Job) RETURN count(j) AS n")[0]["n"] == 0
    )


def test_cache_once_preserves_reach_and_adds_history_marker(driver, graph):
    query = WorldQuery(driver, graph["world"])
    before_links = query.read("MATCH (:Item)-[l:LINK]->(:Item) RETURN l ORDER BY l.id")
    before_items = query.read("MATCH (i:Item) RETURN i ORDER BY i.id")
    packet = loaded(driver, graph).packet
    outcome = result(driver, graph)
    args = dict(expected_version=1, expected_packet=packet_fingerprint(packet), screener=SCREEN)
    assert cache_explanation(driver, graph["world"], graph["mark"], outcome, **args) == "saved"
    after = live_fingerprint(driver, graph["world"])
    assert cache_explanation(driver, graph["world"], graph["mark"], outcome, **args) == "cached"
    assert live_fingerprint(driver, graph["world"]) == after
    assert query.read("MATCH (:Item)-[l:LINK]->(:Item) RETURN l ORDER BY l.id") == before_links
    assert query.read("MATCH (i:Item) RETURN i ORDER BY i.id") == before_items
    mark = query.read("MATCH (m:Impact {link_id: $id}) RETURN m", id=graph["mark"])[0]["m"]
    assert mark["state"] == "open" and mark["path"] == [graph["path"]]
    assert mark["version"] == 2 and mark["touched_at"] == NOW
    history = query.read("MATCH (e:ChangeEntry {field: 'cascade_explanation'}) RETURN e")
    assert len(history) == 1 and history[0]["e"]["entity_id"] == graph["mark"]
    read_before = live_fingerprint(driver, graph["world"])
    response = read_explanation(driver, graph["world"], graph["mark"], screener=SCREEN)
    assert not response.needs_explanation and response.by == "spark"
    assert response.text == outcome.text
    assert live_fingerprint(driver, graph["world"]) == read_before


def test_concurrent_cache_writers_save_one_value(driver, graph):
    outcome = result(driver, graph)
    args = dict(
        expected_version=1,
        expected_packet=packet_fingerprint(loaded(driver, graph).packet),
        screener=SCREEN,
    )

    def save(_):
        return cache_explanation(driver, graph["world"], graph["mark"], outcome, **args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, range(2)))
    assert sorted(outcomes) == ["cached", "saved"]
    rows = WorldQuery(driver, graph["world"]).read(
        "MATCH (e:ChangeEntry {field: 'cascade_explanation'}) RETURN count(e) AS n"
    )
    assert rows[0]["n"] == 1


@pytest.mark.parametrize("version, fingerprint", [(9, None), (1, "changed")])
def test_stale_packet_or_mark_does_not_write(driver, graph, version, fingerprint):
    packet = loaded(driver, graph).packet
    before = live_fingerprint(driver, graph["world"])
    assert (
        cache_explanation(
            driver,
            graph["world"],
            graph["mark"],
            result(driver, graph),
            expected_version=version,
            expected_packet=fingerprint or packet_fingerprint(packet),
            screener=SCREEN,
        )
        == "stale"
    )
    assert live_fingerprint(driver, graph["world"]) == before


def test_explicit_request_uses_jobs_and_model_runs_after_transaction(driver, graph):
    job_id = request_explanation(driver, graph["world"], graph["mark"])
    assert request_explanation(driver, graph["world"], graph["mark"]) == job_id
    layer = Layer(graph)
    report = explain_pending(layer, driver, graph["world"], screener=SCREEN)
    assert (report.selected, report.saved, report.failed, report.stale) == (1, 1, 0, 0)
    assert report.requested_job_id == job_id and layer.calls == 1
    assert request_explanation(driver, graph["world"], graph["mark"]) is None
    assert explain_pending(layer, driver, graph["world"], screener=SCREEN).selected == 0
    assert layer.calls == 1
    [row] = WorldQuery(driver, graph["world"]).read("MATCH (j:Job {id: $id}) RETURN j", id=job_id)
    assert row["j"]["state"] == "done"


def test_failed_model_keeps_template_and_job_retries(driver, graph, frozen):
    job_id = request_explanation(driver, graph["world"], graph["mark"])
    layer = Layer(graph, error=ProviderError("synthetic outage"))
    report = explain_pending(layer, driver, graph["world"], screener=SCREEN)
    assert report.failed == 1 and report.saved == 0
    assert read_explanation(driver, graph["world"], graph["mark"], screener=SCREEN).by == "template"
    assert loaded(driver, graph).cached is None
    assert explain_pending(layer, driver, graph["world"], screener=SCREEN).selected == 0
    frozen.advance(minutes=2)
    layer.error = None
    assert explain_pending(layer, driver, graph["world"], screener=SCREEN).saved == 1
    assert layer.calls == 2
    [row] = WorldQuery(driver, graph["world"]).read("MATCH (j:Job {id: $id}) RETURN j", id=job_id)
    assert row["j"]["state"] == "done" and row["j"]["attempts"] == 2


def test_other_world_cannot_read_enqueue_or_cache(driver, graph, make_world):
    other = make_world()
    plant_node(driver, other, "World", next_sequence=0)
    before = live_fingerprint(driver, other)
    assert read_explanation(driver, other, graph["mark"], screener=SCREEN) is None
    assert request_explanation(driver, other, graph["mark"]) is None
    assert (
        cache_explanation(
            driver,
            other,
            graph["mark"],
            result(driver, graph),
            expected_version=1,
            expected_packet="unused",
            screener=SCREEN,
        )
        == "missing"
    )
    assert live_fingerprint(driver, other) == before


def test_template_is_never_permanently_cached(driver, graph):
    before = live_fingerprint(driver, graph["world"])
    assert (
        cache_explanation(
            driver,
            graph["world"],
            graph["mark"],
            Explanation("A change occurred."),
            expected_version=1,
            expected_packet="unused",
            screener=SCREEN,
        )
        == "refused"
    )
    assert live_fingerprint(driver, graph["world"]) == before


@pytest.mark.parametrize("graph", [True], indirect=True)
def test_proposal_mark_is_processed_without_a_read_request(driver, graph):
    layer = Layer(graph)
    report = explain_pending(layer, driver, graph["world"], screener=SCREEN)
    assert report.requested_job_id is None
    assert report.selected == 1 and report.saved == 1
    assert layer.calls == 1


def test_model_call_never_holds_writer_transaction(driver, graph, monkeypatch):
    from grove.writer.tx import WorldWriter

    active = False
    original = WorldWriter.transaction

    def transaction(self, work):
        def wrapped(tx):
            nonlocal active
            assert not active
            active = True
            try:
                return work(tx)
            finally:
                active = False

        return original(self, wrapped)

    monkeypatch.setattr(WorldWriter, "transaction", transaction)

    class CheckedLayer(Layer):
        def respond(self, *args, **kwargs):
            assert not active
            return super().respond(*args, **kwargs)

    request_explanation(driver, graph["world"], graph["mark"])
    assert explain_pending(CheckedLayer(graph), driver, graph["world"], screener=SCREEN).saved == 1


def test_limited_job_progresses_past_already_cached_references(driver, graph):
    world = graph["world"]
    meaning = WorldQuery(driver, world).read("MATCH (m:Meaning) RETURN m.id AS id")[0]["id"]
    affected = plant_node(driver, world, "Item", title="Release task", status="active", version=1)
    edge = plant_link(
        driver,
        world,
        affected["id"],
        graph["trigger"],
        meaning=meaning,
        active_key="second-synthetic-link",
        version=1,
    )
    mark = plant_link(
        driver,
        world,
        affected["id"],
        graph["trigger"],
        meaning=MEANING_AFFECTED_BY,
        became_true_earliest=WORLD_TIME,
        became_true_latest=WORLD_TIME,
        became_true_precision="instant",
        recorded=NOW,
        version=1,
    )
    plant_node(
        driver,
        world,
        "Impact",
        link_id=mark["id"],
        affected_item=affected["id"],
        trigger_change_set=graph["change_set"],
        trigger_source_type="reported_done",
        state="open",
        path=[edge["id"]],
        depth=1,
        stakes="low",
        rule="R2",
        version=1,
        touched_at=NOW,
    )
    job = request_explanation(driver, world, graph["mark"])
    assert request_explanation(driver, world, mark["id"]) == job
    layer = Layer(graph)
    assert explain_pending(layer, driver, world, limit=1, screener=SCREEN).saved == 1
    assert explain_pending(layer, driver, world, limit=1, screener=SCREEN).saved == 1
    assert explain_pending(layer, driver, world, limit=1, screener=SCREEN).selected == 0
    assert layer.calls == 2


def test_expired_job_recovers_and_late_holder_cannot_finish_replacement(driver, graph, frozen):
    world = graph["world"]
    job_id = request_explanation(driver, world, graph["mark"])
    first = claim_explanation_job(driver, world, worker="dispatcher")
    assert first["id"] == job_id
    assert claim_explanation_job(driver, world, worker="dispatcher") is None
    frozen.advance(minutes=31)
    replacement = claim_explanation_job(driver, world, worker="dispatcher")
    assert replacement["id"] == job_id and replacement["attempts"] == 2
    assert replacement["claimed_by"] != first["claimed_by"]
    assert not renew_explanation_job(driver, world, job_id, worker=first["claimed_by"])
    assert end_job(driver, world, job_id, worker=first["claimed_by"], done=True) == "claimed"
    assert renew_explanation_job(driver, world, job_id, worker=replacement["claimed_by"])
    frozen.advance(minutes=29)
    assert claim_explanation_job(driver, world, worker="dispatcher") is None
    frozen.advance(minutes=2)
    assert explain_pending(Layer(graph), driver, world, screener=SCREEN).saved == 1
    row = WorldQuery(driver, world).read("MATCH (j:Job {id: $id}) RETURN j", id=job_id)[0]["j"]
    assert row["state"] == "done" and row["attempts"] == 3


def test_recovery_only_changes_expired_explanation_jobs_in_the_world(
    driver, graph, make_world, frozen
):
    world, other = graph["world"], make_world()
    plant_node(driver, other, "World", next_sequence=0)
    for scoped, agent in [(world, "learner"), (other, "explain_break")]:
        enqueue_job(driver, scoped, agent=agent, trigger="test", input_references=[], priority=0)
        claim_job(driver, scoped, agent=agent, worker="other-worker")
    before = WorldQuery(driver, world).read("MATCH (j:Job) RETURN j ORDER BY j.id")
    other_before = live_fingerprint(driver, other)
    exhausted = plant_node(
        driver,
        world,
        "Job",
        agent_or_job_type="explain_break",
        state="claimed",
        attempts=3,
        claimed_at=NOW,
        claimed_by="dead-worker",
        version=1,
        touched_at=NOW,
    )
    frozen.advance(minutes=31)
    assert claim_explanation_job(driver, world, worker="dispatcher") is None
    after = WorldQuery(driver, world).read("MATCH (j:Job) RETURN j ORDER BY j.id")
    assert [row for row in after if row["j"]["id"] != exhausted["id"]] == before
    failed = next(row["j"] for row in after if row["j"]["id"] == exhausted["id"])
    assert failed["state"] == "failed" and failed["version"] == 2
    assert failed.get("claimed_by") is None and failed["touched_at"] == clock.now(world)
    assert live_fingerprint(driver, other) == other_before


def test_recovered_job_merges_into_existing_queue(driver, graph, frozen):
    world = graph["world"]
    old_id = request_explanation(driver, world, graph["mark"])
    claim_explanation_job(driver, world, worker="dispatcher")
    queued_id = request_explanation(driver, world, graph["mark"])
    assert old_id != queued_id
    frozen.advance(minutes=31)
    report = explain_pending(Layer(graph), driver, world, screener=SCREEN)
    assert report.requested_job_id == queued_id and report.saved == 1
    rows = WorldQuery(driver, world).read("MATCH (j:Job) RETURN j")
    assert len(rows) == 2 and all(row["j"]["state"] == "done" for row in rows)


@pytest.mark.parametrize("graph", [False, True], indirect=True)
def test_dispatcher_inventory_is_read_only_and_filters_pending_work(
    driver, graph, make_world, request
):
    world, empty = graph["world"], make_world()
    plant_node(driver, empty, "World", next_sequence=0)
    enqueue_job(driver, empty, agent="learner", trigger="test", input_references=[], priority=0)
    if not loaded(driver, graph).mark.get("proposal_id"):
        request_explanation(driver, world, graph["mark"])
    before = {scope: live_fingerprint(driver, scope) for scope in (world, empty)}
    pending = worlds_with_pending_explanations(driver)
    assert isinstance(pending, tuple) and world in pending and empty not in pending
    assert {scope: live_fingerprint(driver, scope) for scope in (world, empty)} == before
    assert explain_pending(Layer(graph), driver, world, screener=SCREEN).saved == 1
    assert world not in worlds_with_pending_explanations(driver)
