"""D6 through the real world transaction, scoped synthetic worlds only."""

import pytest

from grove import clock
from grove.store.cascade_reads import what_it_breaks
from grove.store.primitives import MEANING_AFFECTED_BY
from grove.store.query import WorldQuery
from grove.store.replay import read_history, rebuild
from grove.writer import apply as ap
from grove.writer import render_receipt
from grove.writer.cascade import health
from grove.writer.errors import Refused
from tests.writer.helpers import (
    LEARNER,
    apply,
    at_minute,
    clock_at,
    create_link,
    make_source,
    new_op_id,
    observation,
    read,
    request_key,
    world_dump,
)
from tests.writer.test_contract_d2 import RECHECK, change_status, item_row, make_task
from tests.writer.test_contract_d3 import undo


def graph(driver, seeded, chat):
    world = seeded["world_id"]
    root = make_task(driver, world, chat, 1, title="Repair the garden pump")
    child = make_task(driver, world, chat, 2, title="Paint the north wall")
    leaf = make_task(driver, world, chat, 3, title="Arrange the final inspection")
    history = make_task(driver, world, chat, 4, title="Record the survey measurements")
    ev = observation(chat, "wires", 10)
    receipt = apply(
        driver,
        world,
        [
            create_link(child, "depends on", root, "wires", recheck_by=RECHECK),
            create_link(leaf, "depends on", child, "wires", recheck_by=RECHECK),
            create_link(history, "part of", root, "wires"),
        ],
        [ev],
    )
    links = [line.entity_id for line in receipt.lines if line.symbol == ">"]
    return world, root, child, leaf, history, links


def complete(driver, world, root, *, status="done", request_id=None):
    page = make_source(driver, world, "web_pages", ["The pump repair passed the final inspection."])
    ev = observation(
        page, "confirmed", 1, at=at_minute(30), statement_class="report_state", locator="char:0-20"
    )
    op = change_status(root, status, "confirmed", stopped_being_true=clock_at(at_minute(25)))
    receipt = apply(driver, world, [op], [ev], caller=LEARNER, request_id=request_id)
    return receipt, op, ev


def links(driver, world):
    return {
        r["l"]["id"]: r["l"]
        for r in read(driver, world, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l")
    }


def impacts(driver, world):
    return [r["m"] for r in read(driver, world, "MATCH (m:Impact) RETURN m")]


def test_terminal_ends_wiring_and_marks_exact_dependents_in_one_commit(driver, seeded, chat):
    w, root, child, leaf, history, ids = graph(driver, seeded, chat)
    receipt, _, _ = complete(driver, w, root)
    held = links(driver, w)
    assert held[ids[0]]["end_class"] == "ended_in_the_world"
    assert held[ids[0]]["stopped_being_true_earliest"] == at_minute(25)
    assert held[ids[1]]["active_key"]
    assert held[ids[2]]["active_key"]
    marks = impacts(driver, w)
    assert {m["affected_item"] for m in marks} == {child, leaf}
    assert {m["trigger_change_set"] for m in marks} == {receipt.change_set_id}
    assert all(m["stakes"] == "low" and m["trigger_source_type"] == "confirmed" for m in marks)
    assert {m["depth"] for m in marks} == {1, 2}
    assert any(line.symbol == "?" for line in receipt.lines)
    assert sum(line.symbol == "~" for line in receipt.lines) == 2
    assert "~ item" in render_receipt(driver, w, receipt)
    assert "? proposal" in render_receipt(driver, w, receipt)
    assert health(WorldQuery(driver, w))["live_links_to_ended"] == 0


def test_reported_gate_keeps_links_then_confirmation_retains_mark_history(driver, seeded, chat):
    w, root, child, leaf, _, ids = graph(driver, seeded, chat)
    ev = observation(chat, "report", 20, statement_class="report_happened")
    apply(driver, w, [change_status(root, "reported done", "report")], [ev], caller=LEARNER)
    assert links(driver, w)[ids[0]]["active_key"]
    earlier = impacts(driver, w)
    assert {m["affected_item"] for m in earlier} == {child, leaf}
    assert {m["trigger_source_type"] for m in earlier} == {"reported_done"}
    complete(driver, w, root)
    assert not links(driver, w)[ids[0]].get("active_key")
    assert {m["link_id"] for m in earlier} <= {m["link_id"] for m in impacts(driver, w)}
    assert len(impacts(driver, w)) == 4


def test_cascade_validation_failure_commits_trigger_and_records_failure(
    driver, seeded, chat, monkeypatch
):
    w, root, _, _, _, ids = graph(driver, seeded, chat)
    before = links(driver, w)[ids[0]]

    def refused(*args):
        raise Refused("end_before_start", "planted_cascade_failure", "planted failure")

    monkeypatch.setattr(ap, "_prepare_end_link", refused)
    complete(driver, w, root)
    assert item_row(driver, w, root)["status"] == "done"
    assert links(driver, w)[ids[0]] == before
    assert health(WorldQuery(driver, w))["cascade_failures"] == 1
    rows = read(driver, w, "MATCH (p:CascadeProposal {proposal_type: 'cascade_failed'}) RETURN p")
    assert len(rows) == 1
    assert "planted_cascade_failure" in rows[0]["p"]["detail"]


def test_replay_undo_and_rebuild_preserve_the_cascade_history(driver, seeded, chat):
    w, root, _, _, _, ids = graph(driver, seeded, chat)
    key = request_key()
    receipt, op, ev = complete(driver, w, root, request_id=key)
    before = world_dump(driver, w)
    replayed = apply(driver, w, [op], [ev], caller=LEARNER, request_id=key)
    assert replayed == receipt
    assert world_dump(driver, w) == before
    apply(driver, w, [undo(receipt.change_set_id)], [])
    assert links(driver, w)[ids[0]]["active_key"]
    assert {m["state"] for m in impacts(driver, w)} == {"undone"}
    mark_ids = {m["link_id"] for m in impacts(driver, w)}
    assert all(links(driver, w)[key]["meaning"] == MEANING_AFFECTED_BY for key in mark_ids)
    entries, values = read_history(WorldQuery(driver, w))
    rebuilt = rebuild(entries, values)
    assert rebuilt.links[ids[0]]["active_key"]
    assert mark_ids <= set(rebuilt.links)


def test_simulation_equals_real_marks_and_leaves_every_row_unchanged(driver, seeded, chat):
    w, root, child, leaf, _, _ = graph(driver, seeded, chat)
    before = world_dump(driver, w)
    predicted = what_it_breaks(
        WorldQuery(driver, w),
        root,
        hypothetical={
            "change_set_id": "simulation",
            "world_time": at_minute(25),
            "source_type": "confirmed",
            "terminal": True,
            "person_correction": False,
            "reopened": False,
        },
        now=clock.now(w),
    )
    assert world_dump(driver, w) == before
    assert {m["affected_item"] for m in predicted.marks} == {child, leaf}
    complete(driver, w, root)
    assert {(m["affected_item"], tuple(m["path"])) for m in predicted.marks} == {
        (m["affected_item"], tuple(m["path"])) for m in impacts(driver, w)
    }


def test_reopen_only_proposes_restoration(driver, seeded, chat):
    w, root, _, _, _, ids = graph(driver, seeded, chat)
    complete(driver, w, root)
    receipt = apply(
        driver,
        w,
        [
            {
                "op": "reopen_item",
                "op_id": new_op_id(),
                "item": root,
                "reason": "The inspection requires another repair.",
            }
        ],
        [],
    )
    assert not links(driver, w)[ids[0]].get("active_key")
    assert any(line.symbol == "?" and line.field == "reopen_links" for line in receipt.lines)


def test_simulation_cannot_read_another_world(driver, seeded, chat, make_world):
    w, root, *_ = graph(driver, seeded, chat)
    other = make_world()
    with pytest.raises(ValueError, match="no item"):
        what_it_breaks(WorldQuery(driver, other), root, hypothetical={}, now=clock.now(w))
