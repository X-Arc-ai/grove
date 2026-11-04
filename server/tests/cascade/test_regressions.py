"""D6 integration regressions from independent acceptance review.

These tests use real writer transactions and synthetic worlds. Registry variants enter
through test-only seed hooks, so the same production writer reads the changed rows.
"""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store.cascade_reads import what_it_breaks
from grove.store.query import WorldQuery
from grove.writer import apply as ap
from grove.writer import worlds
from grove.writer.cascade import health
from grove.writer.errors import Refused
from tests.writer.helpers import (
    CHAT_LINES,
    LEARNER,
    apply,
    clock_at,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    read,
    world_dump,
)
from tests.writer.test_contract_d2 import change_status, item_row, update_item

NOW = datetime(2027, 2, 1, 12, tzinfo=UTC)
RECHECK = (NOW + timedelta(days=60)).isoformat()


@pytest.fixture
def frozen():
    controlled = TestClock()
    controlled.set(NOW)
    with clock.using(controlled):
        yield controlled


@pytest.fixture
def case(driver, make_world, frozen):
    return _new_world(driver, make_world)


def _new_world(driver, make_world):
    world = make_world()
    seeded = worlds.create_world(driver, world, owner_user_id="u_cascade_regression")
    source = make_source(driver, world, "whatsapp", CHAT_LINES)
    return world, seeded, source


def _obs(source, key, day=-1, *, line=1, **fields):
    return observation(source, key, line, at=NOW + timedelta(days=day), **fields)


def _graph(driver, case, *, kind="task", dependency="depends on", history="part of"):
    world, _, source = case
    ids = {}
    for index, (name, title) in enumerate(
        (
            ("root", "Repair the orchard irrigation pump"),
            ("child", "Paint the north retaining wall"),
            ("leaf", "Arrange the garden inspection"),
            ("history", "Record the boundary survey"),
        ),
        1,
    ):
        ev = _obs(source, name, -100, line=index)
        receipt = apply(
            driver,
            world,
            [
                create_item(
                    kind,
                    title,
                    name,
                    status="in progress",
                    recheck_by=RECHECK,
                    became_true=clock_at(NOW - timedelta(days=100)),
                )
            ],
            [ev],
            caller=LEARNER,
        )
        ids[name] = created_id(receipt)
    ev = _obs(source, "links", -99, line=5)
    receipt = apply(
        driver,
        world,
        [
            create_link(ids["child"], dependency, ids["root"], "links", recheck_by=RECHECK),
            create_link(ids["leaf"], dependency, ids["child"], "links", recheck_by=RECHECK),
            create_link(ids["history"], history, ids["root"], "links"),
        ],
        [ev],
        caller=LEARNER,
    )
    ids["links"] = [line.entity_id for line in receipt.lines if line.symbol == ">"]
    return ids


def _links(driver, world):
    return {
        row["l"]["id"]: row["l"]
        for row in read(driver, world, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l")
    }


def _marks(driver, world, change_set):
    return [
        row["m"]
        for row in read(
            driver,
            world,
            "MATCH (m:Impact {trigger_change_set: $id}) RETURN m",
            id=change_set,
        )
    ]


def _proposals(driver, world, change_set):
    rows = read(driver, world, "MATCH (p:CascadeProposal) RETURN p")
    return [
        row["p"]
        for row in rows
        if json.loads(row["p"]["detail"])["trigger_change_set"] == change_set
    ]


def _confirmed(driver, world, *, day=-1):
    source = make_source(driver, world, "web_pages", ["The repair inspection passed."])
    return _obs(source, "confirmed", day, locator="whole")


def _complete(driver, world, root, *, day=-1):
    ev = _confirmed(driver, world, day=day)
    op = change_status(
        root,
        "done",
        "confirmed",
        stopped_being_true=clock_at(datetime.fromisoformat(ev["source_time"])),
    )
    return apply(driver, world, [op], [ev], caller=LEARNER)


def _set_status(driver, case, item, status, key, day):
    world, _, source = case
    return apply(
        driver,
        world,
        [change_status(item, status, key)],
        [_obs(source, key, day)],
        caller=LEARNER,
    )


def test_confirmed_contrary_row_does_not_authorize_reported_cascade(driver, case):
    world, _, source = case
    ids = _graph(driver, case)
    report = _obs(source, "report", -2)
    contrary = _confirmed(driver, world)
    op = change_status(ids["root"], "cancelled", "report")
    op["evidence_refs"].append({"ev_id": "confirmed", "polarity": "contradicts"})
    receipt = apply(driver, world, [op], [report, contrary], caller=LEARNER)
    assert _links(driver, world)[ids["links"][0]]["active_key"]
    assert {m["trigger_source_type"] for m in _marks(driver, world, receipt.change_set_id)} == {
        "reported_done"
    }
    assert any(p["rule"] == "R1" for p in _proposals(driver, world, receipt.change_set_id))


def test_older_unrelated_confirmation_does_not_authorize_new_report(driver, case):
    world, _, _ = case
    ids = _graph(driver, case)
    ev = _confirmed(driver, world, day=-20)
    apply(
        driver,
        world,
        [
            {
                "op": "attach_evidence",
                "op_id": new_op_id(),
                "target": ids["root"],
                "ev_id": "confirmed",
                "polarity": "supports",
            }
        ],
        [ev],
        caller=LEARNER,
    )
    receipt = _set_status(driver, case, ids["root"], "cancelled", "report", -1)
    assert item_row(driver, world, ids["root"])["derived_source_type"] == "confirmed"
    assert _links(driver, world)[ids["links"][0]]["active_key"]
    assert {m["trigger_source_type"] for m in _marks(driver, world, receipt.change_set_id)} == {
        "reported_done"
    }


@pytest.mark.parametrize(
    "kind, field",
    [
        ("task", "due"),
        ("plan", "scheduled for"),
        ("trip", "starts"),
        ("period", "ends"),
    ],
)
def test_declared_scheduled_field_triggers_r2_but_title_does_not(driver, case, kind, field):
    world, _, source = case
    ev = _obs(source, "create", -5)
    fields = {"recheck_by": RECHECK}
    if kind in ("task", "plan"):
        fields["status"] = "in progress" if kind == "task" else "active"
    root = created_id(
        apply(
            driver,
            world,
            [create_item(kind, "Schedule the harbor inspection", "create", **fields)],
            [ev],
            caller=LEARNER,
        )
    )
    child = created_id(
        apply(
            driver,
            world,
            [
                create_item(
                    "task",
                    "Prepare the harbor safety report",
                    "child",
                    status="open",
                    recheck_by=RECHECK,
                )
            ],
            [_obs(source, "child", -5, line=2)],
            caller=LEARNER,
        )
    )
    apply(
        driver,
        world,
        [create_link(child, "depends on", root, "link", recheck_by=RECHECK)],
        [_obs(source, "link", -4, line=3)],
        caller=LEARNER,
    )
    renamed = update_item(
        root,
        item_row(driver, world, root)["version"],
        {"title": "Schedule the revised harbor inspection"},
        "rename",
    )
    rename_receipt = apply(
        driver, world, [renamed], [_obs(source, "rename", -3, line=4)], caller=LEARNER
    )
    assert _marks(driver, world, rename_receipt.change_set_id) == []
    moved = update_item(
        root,
        item_row(driver, world, root)["version"],
        {"properties": {field: (NOW + timedelta(days=7)).isoformat()}},
        "date",
    )
    receipt = apply(driver, world, [moved], [_obs(source, "date", -2, line=5)], caller=LEARNER)
    assert {m["affected_item"] for m in _marks(driver, world, receipt.change_set_id)} == {child}
    assert all(p["rule"] != "R5" for p in _proposals(driver, world, receipt.change_set_id))


@pytest.mark.parametrize("previous", ["in progress", "reported done"])
def test_r4_restores_exact_prior_status_from_real_history(driver, case, previous):
    world, _, _ = case
    ids = _graph(driver, case)
    if previous == "reported done":
        _set_status(driver, case, ids["child"], previous, "prior", -4)
    _set_status(driver, case, ids["child"], "blocked", "blocked", -3)
    receipt = _complete(driver, world, ids["root"])
    proposals = [
        p
        for p in _proposals(driver, world, receipt.change_set_id)
        if p["rule"] == "R4" and p["target_item"] == ids["child"]
    ]
    assert len(proposals) == 1
    assert json.loads(proposals[0]["detail"])["action"] == "resume"
    [operation] = json.loads(proposals[0]["suggested_operations"])
    [value] = read(
        driver,
        world,
        "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value",
        id=operation["status_value_id"],
    )
    assert value["value"] == previous


def test_three_month_backfill_ends_wiring_and_resolves_old_marks(driver, case):
    world, _, source = case
    ids = _graph(driver, case)
    for line, name in enumerate(("child", "leaf"), 12):
        op = update_item(
            ids[name],
            item_row(driver, world, ids[name])["version"],
            {"summary": "The later plan already accounts for this change."},
            name,
        )
        apply(driver, world, [op], [_obs(source, name, -2, line=line)], caller=LEARNER)
    receipt = _complete(driver, world, ids["root"], day=-90)
    marks = _marks(driver, world, receipt.change_set_id)
    assert {m["affected_item"] for m in marks} == {ids["child"], ids["leaf"]}
    assert {m["state"] for m in marks} == {"born_resolved"}
    root_link = _links(driver, world)[ids["links"][0]]
    assert root_link["stopped_being_true_earliest"] == NOW - timedelta(days=90)
    assert health(WorldQuery(driver, world))["live_links_to_ended"] == 0


def test_real_provisional_dependency_yields_only_proposals(
    driver,
    make_world,
    frozen,
    monkeypatch,
):
    original = worlds._meaning_row

    def provisional(tx, declared, kind_ids, run):
        row = original(tx, declared, kind_ids, run)
        if declared["name"] == "depends on":
            row["status"] = "provisional"
        return row

    with monkeypatch.context() as patch:
        patch.setattr(worlds, "_meaning_row", provisional)
        case = _new_world(driver, make_world)
    world, seeded, _ = case
    [meaning] = read(
        driver, world, "MATCH (m:Meaning {id: $id}) RETURN m", id=seeded["meanings"]["depends on"]
    )
    assert meaning["m"]["status"] == "provisional"
    ids = _graph(driver, case)
    receipt = _complete(driver, world, ids["root"])
    assert _marks(driver, world, receipt.change_set_id) == []
    assert all(_links(driver, world)[key]["active_key"] for key in ids["links"])
    proposals = _proposals(driver, world, receipt.change_set_id)
    assert {p["rule"] for p in proposals} >= {"R1", "R2", "R4"}
    assert {p["target_item"] for p in proposals if p["rule"] == "R2"} == {ids["child"], ids["leaf"]}


def test_failure_after_cascade_writes_rolls_back_every_row(driver, case, monkeypatch):
    world, _, _ = case
    ids = _graph(driver, case)
    ev = _confirmed(driver, world)
    op = change_status(ids["root"], "done", "confirmed")
    before = world_dump(driver, world)

    def fail_after_writes():
        raise RuntimeError("planted after cascade writes")

    monkeypatch.setattr(ap, "_before_commit", fail_after_writes)
    with pytest.raises(RuntimeError, match="planted after cascade writes"):
        apply(driver, world, [op], [ev], caller=LEARNER)
    assert world_dump(driver, world) == before


def test_simulation_matches_stored_source_time_state_path_and_endings(driver, case):
    world, _, _ = case
    ids = _graph(driver, case)
    at = NOW - timedelta(days=1)
    before = world_dump(driver, world)
    predicted = what_it_breaks(
        WorldQuery(driver, world),
        ids["root"],
        hypothetical={
            "change_set_id": "simulation",
            "world_time": at,
            "source_type": "confirmed",
            "terminal": True,
            "reopened": False,
            "person_correction": False,
            "end_clock": clock_at(at),
        },
        now=NOW,
    )
    assert world_dump(driver, world) == before
    receipt = _complete(driver, world, ids["root"])
    stored = _links(driver, world)
    actual = {
        (
            m["affected_item"],
            m["depth"],
            tuple(m["path"]),
            m["state"],
            m["trigger_source_type"],
            stored[m["link_id"]]["became_true_earliest"],
        )
        for m in _marks(driver, world, receipt.change_set_id)
    }
    assert actual == {
        (
            m["affected_item"],
            m["depth"],
            tuple(m["path"]),
            m["state"],
            m["source_type"],
            m["world_time"],
        )
        for m in predicted.marks
    }
    assert {e["link_id"] for e in predicted.endings} == {
        key for key in ids["links"] if stored[key].get("end_class") == "ended_in_the_world"
    }
    for ending in predicted.endings:
        row = stored[ending["link_id"]]
        assert row["stopped_being_true_earliest"] == ending["world_time"]
        assert row["stopped_being_true_latest"] == ending["world_time"]


def test_renamed_ontology_keeps_writer_reach_and_endings(driver, make_world, frozen, monkeypatch):
    original = worlds.load_starter_set()
    kind_names = {row["name"]: f"invented kind {n}" for n, row in enumerate(original["kinds"])}
    meaning_names = {
        row["name"]: f"invented relation {n}" for n, row in enumerate(original["meanings"])
    }
    altered = copy.deepcopy(original)
    for row in altered["kinds"]:
        row["name"] = kind_names[row["name"]]
    for row in altered["meanings"]:
        row["name"] = meaning_names[row["name"]]
        for endpoint in ("from_kinds", "to_kinds"):
            if "kinds" in row[endpoint]:
                row[endpoint]["kinds"] = [kind_names[k] for k in row[endpoint]["kinds"]]
        row["aliases"] = []
    for row in altered["property_names"]:
        row["kind"] = kind_names[row["kind"]]
    results = []
    for renamed in (False, True):
        with monkeypatch.context() as patch:
            patch.setattr(
                worlds, "load_starter_set", lambda renamed=renamed: altered if renamed else original
            )
            case = _new_world(driver, make_world)
        world = case[0]
        ids = _graph(
            driver,
            case,
            kind=kind_names["task"] if renamed else "task",
            dependency=meaning_names["depends on"] if renamed else "depends on",
            history=meaning_names["part of"] if renamed else "part of",
        )
        receipt = _complete(driver, world, ids["root"])
        roles = {value: name for name, value in ids.items() if name != "links"}
        stored = _links(driver, world)
        results.append(
            (
                sorted(
                    (roles[m["affected_item"]], m["depth"], m["trigger_source_type"])
                    for m in _marks(driver, world, receipt.change_set_id)
                ),
                [bool(stored[key].get("active_key")) for key in ids["links"]],
            )
        )
    assert (
        results[0]
        == results[1]
        == ([("child", 1, "confirmed"), ("leaf", 2, "confirmed")], [False, True, True])
    )


def test_failed_r1_keeps_blocked_dependent_out_of_resume_proposals(driver, case, monkeypatch):
    world, _, _ = case
    ids = _graph(driver, case)
    _set_status(driver, case, ids["child"], "open", "previous", -5)
    _set_status(driver, case, ids["child"], "blocked", "blocked", -3)
    original = ap._prepare_end_link

    def refuse_one(run, operation):
        if operation["link"] == ids["links"][0]:
            raise Refused("end_before_start", "planted_r1_refusal", "planted refusal")
        return original(run, operation)

    monkeypatch.setattr(ap, "_prepare_end_link", refuse_one)
    receipt = _complete(driver, world, ids["root"])
    assert _links(driver, world)[ids["links"][0]]["active_key"]
    assert item_row(driver, world, ids["child"])["status"] == "blocked"
    proposals = _proposals(driver, world, receipt.change_set_id)
    assert any(p["proposal_type"] == "cascade_failed" for p in proposals)
    assert not any(json.loads(p["detail"])["action"] == "resume" for p in proposals)
