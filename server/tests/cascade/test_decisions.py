"""Proposal and mark decisions against scoped synthetic Neo4j worlds."""

import json
import os

import pytest

from grove import clock
from grove.writer import BatchDeclined, Caller
from grove.writer import apply as ap
from grove.writer import cascade_decisions as decisions
from grove.writer.ops import check_shape
from grove.writer.testing import plant_link, plant_node
from grove.writer.worlds import create_world
from tests.cascade.test_writer import complete, graph, impacts, links
from tests.writer.helpers import (
    CURATOR,
    HOST,
    PERSON,
    READ_ONLY,
    apply,
    at_minute,
    clock_at,
    create_link,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)
from tests.writer.test_contract_d2 import (
    RECHECK,
    change_status,
    item_row,
    make_task,
    update_item,
)

CONSOLIDATOR = Caller("consolidation", {"grove.propose"}, "agent:connector")


@pytest.fixture(autouse=True)
def component_dispatch(monkeypatch):
    """Only the isolated component run needs this seam, before the root dispatch commit."""
    if os.environ.get("GROVE_DECISION_COMPONENT_TEST") == "1":
        for name, handler in decisions.HANDLERS.items():
            monkeypatch.setitem(ap._HANDLERS, name, handler)


def proposal(
    driver,
    world,
    target,
    *,
    action="resume",
    root=None,
    link_ids=(),
    rule="R4",
    status="in progress",
    target_version=None,
    trigger_change=None,
):
    target_row = item_row(driver, world, target)
    status_id = plant_node(driver, world, "HistoryValue", value=status)["id"]
    operations = (
        [{"op": "change_status", "item": target, "status_value_id": status_id}]
        if action == "resume"
        else []
    )
    detail = {
        "trigger_id": root or target,
        "trigger_change_set": "synthetic-trigger-change",
        "action": action,
        "link_ids": list(link_ids),
        "target_version": target_row["version"] if target_version is None else target_version,
        "trigger_source_type": "said",
        "trigger_change": trigger_change
        or {
            "world_time": at_minute(25).isoformat(),
            "terminal": False,
            "reopened": False,
            "person_correction": False,
        },
    }
    return plant_node(
        driver,
        world,
        "CascadeProposal",
        rule=rule,
        target_item=target,
        suggested_operations=json.dumps(operations),
        detail=json.dumps(detail),
        stakes="high",
        state="open",
        proposal_type="cascade",
        version=1,
        touched_at=clock.now(world),
    )["id"]


def decide(proposal_id, action="accept_proposal", **fields):
    return {
        "op": action,
        "op_id": new_op_id(),
        "proposal": proposal_id,
        "reason": "The decision follows the current evidence.",
        **fields,
    }


def resolve(mark_id, disposition="still_holding", **fields):
    return {
        "op": "resolve_mark",
        "op_id": new_op_id(),
        "mark": mark_id,
        "disposition": disposition,
        "reason": "The current source resolves this consequence.",
        **fields,
    }


def proposal_row(driver, world, proposal_id):
    return read(driver, world, "MATCH (p:CascadeProposal {id: $id}) RETURN p", id=proposal_id)[0][
        "p"
    ]


def disposition_rows(driver, world, commit):
    return read(
        driver,
        world,
        "MATCH (e:ChangeEntry {commit_id: $commit, field: 'cascade_disposition'}) "
        "MATCH (v:HistoryValue {id: e.new_value_id}) RETURN e, v.value AS value",
        commit=commit,
    )


def assert_rule(caught, rule):
    assert rule in {refusal.rule for refusal in caught.value.refusals}


def test_accept_resume_uses_ordinary_status_writer_and_records_proposal_provenance(
    driver, seeded, chat
):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target)
    ev = observation(chat, "resume", 30)
    receipt = apply(driver, w, [decide(proposed, evidence_refs=supports("resume"))], [ev])
    assert item_row(driver, w, target)["status"] == "in progress"
    stored = proposal_row(driver, w, proposed)
    assert stored["state"] == "accepted"
    assert stored["decided_by"] == PERSON.actor
    assert stored["resulting_commit"] == receipt.commit_id
    assert stored["version"] == 2
    assert stored["decision_reason"] != "The decision follows the current evidence."
    (marker,) = disposition_rows(driver, w, receipt.commit_id)
    value = json.loads(marker["value"])
    assert value["kind"] == "proposal" and value["state"] == "accepted"
    assert value["operation_sequences"]
    assert any(line.symbol == "*" and line.field == "status" for line in receipt.lines)


def test_accepted_terminal_status_still_requires_ordinary_confirmation(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target)
    ev = observation(chat, "done", 30, statement_class="report_happened")
    nested = change_status(target, "done", "done", stopped_being_true=clock_at(at_minute(30)))
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, operations=[nested])], [ev])
    assert_rule(caught, "terminal_needs_confirmation")
    assert item_row(driver, w, target)["status"] == "open"
    assert proposal_row(driver, w, proposed)["state"] == "open"


def test_accept_cannot_change_another_item(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    other = make_task(driver, w, chat, 2, title="Install the garden fence")
    proposed = proposal(driver, w, target)
    ev = observation(chat, "move", 30)
    with pytest.raises(BatchDeclined) as caught:
        apply(
            driver,
            w,
            [decide(proposed, operations=[change_status(other, "in progress", "move")])],
            [ev],
        )
    assert_rule(caught, "proposal_scope")
    assert item_row(driver, w, other)["status"] == "open"
    assert proposal_row(driver, w, proposed)["state"] == "open"


def test_host_accept_does_not_gain_change_status_privilege(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target)
    ev = observation(chat, "resume", 30)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, evidence_refs=supports("resume"))], [ev], caller=HOST)
    assert_rule(caught, "caller_kind")
    assert proposal_row(driver, w, proposed)["state"] == "open"


def test_read_only_caller_cannot_decide(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target, action="review")
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed)], [], caller=READ_ONLY)
    assert_rule(caught, "write_scope")
    assert proposal_row(driver, w, proposed)["state"] == "open"


def test_wrong_world_cannot_read_or_decide_proposal(driver, seeded, chat, make_world):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target, action="review")
    other = make_world()
    create_world(driver, other, owner_user_id="u_t_other")
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, other, [decide(proposed)], [])
    assert_rule(caught, "proposal_not_found")
    assert proposal_row(driver, w, proposed)["state"] == "open"


def test_reject_records_actor_reason_and_never_changes_target(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    before = item_row(driver, w, target)
    proposed = proposal(driver, w, target)
    receipt = apply(driver, w, [decide(proposed, "reject_proposal")], [], caller=HOST)
    row = proposal_row(driver, w, proposed)
    assert row["state"] == "rejected" and row["decided_by"] == HOST.actor
    assert row["resulting_commit"] == receipt.commit_id
    assert item_row(driver, w, target) == before
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, "reject_proposal")], [])
    assert_rule(caught, "proposal_not_open")


@pytest.mark.parametrize("caller", [PERSON, HOST, CONSOLIDATOR])
def test_review_acceptance_has_no_implicit_item_write(driver, seeded, chat, caller):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    before = item_row(driver, w, target)
    proposed = proposal(driver, w, target, action="review")
    apply(driver, w, [decide(proposed)], [], caller=caller)
    assert item_row(driver, w, target) == before
    assert proposal_row(driver, w, proposed)["state"] == "accepted"


@pytest.mark.parametrize("decision", ["accept_proposal", "reject_proposal"])
def test_stale_target_commits_stale_marker_and_recomputes_without_writes(
    driver, seeded, chat, monkeypatch, decision
):
    w, root, target, *_ = graph(driver, seeded, chat)
    proposed = proposal(driver, w, target, action="mark", root=root, rule="R2")
    ev = observation(chat, "changed", 30)
    apply(
        driver,
        w,
        [
            update_item(
                target,
                item_row(driver, w, target)["version"],
                {"summary": "The revised inspection instructions."},
                "changed",
            )
        ],
        [ev],
    )
    before = item_row(driver, w, target)
    original = decisions.plan
    observed = []

    def checked(snapshot, changes):
        observed.append(changes)
        return original(snapshot, changes)

    monkeypatch.setattr(decisions, "plan", checked)
    receipt = apply(driver, w, [decide(proposed, decision)], [])
    row = proposal_row(driver, w, proposed)
    assert row["state"] == "stale"
    assert row["resulting_commit"] == receipt.commit_id
    detail = json.loads(row["detail"])
    (replacement,) = detail["recomputed_proposals"]
    assert proposal_row(driver, w, replacement)["state"] == "open"
    assert observed[0][0]["world_time"] == at_minute(25)
    assert item_row(driver, w, target) == before
    assert not impacts(driver, w)
    assert (
        json.loads(disposition_rows(driver, w, receipt.commit_id)[0]["value"])["state"] == "stale"
    )


def test_stale_recompute_does_not_reactivate_old_r1_endings(driver, seeded, chat):
    w, root, target, _, _, ids = graph(driver, seeded, chat)
    complete(driver, w, root)
    proposed = proposal(
        driver,
        w,
        target,
        action="review",
        root=root,
        target_version=0,
        trigger_change={
            "world_time": at_minute(25).isoformat(),
            "terminal": True,
            "reopened": False,
            "person_correction": False,
        },
    )
    apply(driver, w, [decide(proposed)], [])
    assert not links(driver, w)[ids[0]].get("active_key")
    row = proposal_row(driver, w, proposed)
    assert row["state"] == "stale"
    assert not json.loads(row["detail"])["recomputed_proposals"]


def test_stale_legacy_proposal_never_invents_original_time(driver, seeded, chat):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    row = plant_node(
        driver,
        w,
        "CascadeProposal",
        target_item=target,
        state="open",
        version=1,
        rule="R4",
        suggested_operations="[]",
        detail=json.dumps({"target_version": 0, "action": "review", "trigger_id": target}),
    )
    apply(driver, w, [decide(row["id"])], [])
    stored = proposal_row(driver, w, row["id"])
    assert stored["state"] == "stale"
    assert json.loads(stored["detail"])["recompute_error"] == "missing_trigger_change"


@pytest.mark.parametrize(
    "disposition,state",
    [
        ("still_holding", "resolved_still_holding"),
        ("revised", "resolved_revised"),
        ("dismissed", "dismissed"),
    ],
)
def test_person_resolves_mark_with_author_time_and_reason(driver, seeded, chat, disposition, state):
    w, root, *_ = graph(driver, seeded, chat)
    complete(driver, w, root)
    mark = impacts(driver, w)[0]
    receipt = apply(driver, w, [resolve(mark["link_id"], disposition)], [])
    row = read(driver, w, "MATCH (m:Impact {link_id: $id}) RETURN m", id=mark["link_id"])[0]["m"]
    assert row["state"] == state
    assert row["resolved_by"] == PERSON.actor
    assert row["resolved_at"]
    assert row["resolution_reason"]
    assert row["version"] == mark["version"] + 1
    value = json.loads(disposition_rows(driver, w, receipt.commit_id)[0]["value"])
    assert value == {"id": mark["link_id"], "kind": "mark", "state": state}


def test_cleanup_cannot_resolve_mark_without_live_source(driver, seeded, chat):
    w, root, *_ = graph(driver, seeded, chat)
    complete(driver, w, root)
    mark = impacts(driver, w)[0]
    ev = observation(chat, "old-chat", 30)
    for refs, evidence in [([], []), (supports("old-chat"), [ev])]:
        with pytest.raises(BatchDeclined) as caught:
            apply(
                driver, w, [resolve(mark["link_id"], evidence_refs=refs)], evidence, caller=CURATOR
            )
        assert_rule(caught, "mark_live_source")
    assert (
        read(
            driver, w, "MATCH (m:Impact {link_id: $id}) RETURN m.state AS state", id=mark["link_id"]
        )[0]["state"]
        == "open"
    )


def test_cleanup_can_resolve_with_new_live_confirming_source(driver, seeded, chat):
    w, root, *_ = graph(driver, seeded, chat)
    complete(driver, w, root)
    mark = impacts(driver, w)[0]
    source = make_source(
        driver, w, "web_pages", ["The current inspection confirms the planned work."]
    )
    received = read(
        driver, w, "MATCH (s:Source {id: $id}) RETURN s.received_at AS at", id=source["source_id"]
    )[0]["at"]
    ev = observation(source, "fresh", 1, at=received, locator="char:0-20")
    apply(
        driver, w, [resolve(mark["link_id"], evidence_refs=supports("fresh"))], [ev], caller=CURATOR
    )
    assert (
        read(
            driver, w, "MATCH (m:Impact {link_id: $id}) RETURN m.state AS state", id=mark["link_id"]
        )[0]["state"]
        == "resolved_still_holding"
    )


def test_cleanup_cannot_resolve_with_source_received_before_mark(driver, seeded, chat):
    w, root, *_ = graph(driver, seeded, chat)
    source = make_source(driver, w, "web_pages", ["An earlier inspection record."])
    complete(driver, w, root)
    mark = impacts(driver, w)[0]
    ev = observation(source, "old-page", 1, at=at_minute(30), locator="char:0-20")
    with pytest.raises(BatchDeclined) as caught:
        apply(
            driver,
            w,
            [resolve(mark["link_id"], evidence_refs=supports("old-page"))],
            [ev],
            caller=CURATOR,
        )
    assert_rule(caught, "mark_live_source")
    assert (
        read(
            driver, w, "MATCH (m:Impact {link_id: $id}) RETURN m.state AS state", id=mark["link_id"]
        )[0]["state"]
        == "open"
    )


def test_provisional_mark_requires_explicit_apply_mark(driver, seeded, chat):
    w = seeded["world_id"]
    root = make_task(driver, w, chat, 1)
    target = make_task(driver, w, chat, 2, title="Prepare the inspection record")
    original = read(driver, w, "MATCH (m:Meaning {name: 'depends on'}) RETURN m")[0]["m"]
    meaning = plant_node(
        driver,
        w,
        "Meaning",
        **{
            k: v
            for k, v in original.items()
            if k not in {"id", "world_id", "name", "aliases", "status"}
        },
        name="requires inspection of",
        aliases=[],
        status="provisional",
    )
    link = plant_link(
        driver,
        w,
        target,
        root,
        meaning=meaning["id"],
        active_key=f"{target}|{meaning['id']}|{root}",
    )
    proposed = proposal(
        driver, w, target, action="mark", root=root, rule="R2", link_ids=[link["id"]]
    )
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed)], [])
    assert_rule(caught, "proposal_mark_approval")
    assert not impacts(driver, w)
    apply(driver, w, [decide(proposed, apply_mark=True)], [])
    assert {mark["affected_item"] for mark in impacts(driver, w)} == {target}
    assert proposal_row(driver, w, proposed)["state"] == "accepted"


@pytest.mark.parametrize("approval", [True, False])
def test_apply_mark_cannot_authorize_a_different_action(driver, seeded, chat, approval):
    w = seeded["world_id"]
    target = make_task(driver, w, chat, 1)
    proposed = proposal(driver, w, target, action="review")
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, apply_mark=approval)], [])
    assert_rule(caught, "proposal_action")


def test_end_link_acceptance_is_scoped_and_uses_ordinary_clock_validation(driver, seeded, chat):
    w, root, _, _, _, ids = graph(driver, seeded, chat)
    proposed = proposal(driver, w, root, action="end_link", root=root, link_ids=[ids[0]], rule="R1")
    operation = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": ids[1],
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(30)),
        "reason": "The relationship ended.",
    }
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, operations=[operation])], [])
    assert_rule(caught, "proposal_scope")
    operation["link"] = ids[0]
    apply(driver, w, [decide(proposed, operations=[operation])], [])
    assert not links(driver, w)[ids[0]].get("active_key")
    assert links(driver, w)[ids[1]]["active_key"]


def test_reopen_proposal_only_recreates_original_link_identity(driver, seeded, chat):
    w, root, target, _, other, ids = graph(driver, seeded, chat)
    ev = observation(chat, "end", 30)
    apply(
        driver,
        w,
        [
            {
                "op": "end_link",
                "op_id": new_op_id(),
                "link": ids[0],
                "end_class": "ended_in_the_world",
                "stopped_being_true": clock_at(at_minute(30)),
                "reason": "The earlier arrangement ended.",
            }
        ],
        [],
    )
    proposed = proposal(driver, w, root, action="reopen_links", root=root, link_ids=[ids[0]])
    operation = create_link(other, "depends on", root, "end", recheck_by=RECHECK)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, w, [decide(proposed, operations=[operation])], [ev])
    assert_rule(caught, "proposal_scope")
    operation["from"] = target
    apply(driver, w, [decide(proposed, operations=[operation])], [ev])
    assert any(
        row["from_item"] == target and row["to_item"] == root and row.get("active_key")
        for row in links(driver, w).values()
    )


def test_nested_shape_checks_sources_and_writer_only_fields():
    operation = change_status("target", "done", "missing")
    errors = check_shape({"ops": [decide("proposal", operations=[operation])], "evidence": []})
    assert {error.rule for error in errors} == {"unknown_ev_id"}
    operation["source_type"] = "confirmed"
    errors = check_shape({"ops": [decide("proposal", operations=[operation])], "evidence": []})
    assert {error.rule for error in errors} == {"caller_source_type"}


def test_nested_operations_cannot_expand_batch_limit_or_recurse():
    nested = [
        {
            "op": "end_link",
            "op_id": f"end-{n}",
            "link": "link",
            "end_class": "ended_in_the_world",
            "stopped_being_true": "unknown",
            "reason": "The relationship ended.",
        }
        for n in range(500)
    ]
    errors = check_shape({"ops": [decide("proposal", operations=nested)], "evidence": []})
    assert {error.rule for error in errors} == {"ops_count"}
    errors = check_shape(
        {"ops": [decide("proposal", operations=[decide("inner")])], "evidence": []}
    )
    assert {error.rule for error in errors} == {"proposal_operation"}
