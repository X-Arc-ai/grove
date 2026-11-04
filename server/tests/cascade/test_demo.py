"""The D6 reported restriction beat through ordinary writer operations and D4 verdicts."""

import json

import pytest

from grove import clock
from grove.clock import TestClock
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from grove.writer.cascade import health
from grove.writer.registry import Registry
from grove.writer.restore import rebuild_in_place
from grove.writer.testing import plant_node
from tests.cascade.test_writer import complete, graph, impacts, links
from tests.writer.helpers import (
    LEARNER,
    apply,
    at_minute,
    clock_at,
    create_link,
    make_source,
    observation,
    read,
)
from tests.writer.test_contract_d2 import RECHECK, item_row, make_item, make_task
from tests.writer.test_contract_d3 import contradicts, refute, undo
from tests.writer.test_verify import piece, verify


@pytest.fixture
def frozen():
    fixed = TestClock()
    fixed.set(at_minute(60))
    with clock.using(fixed):
        yield fixed


def verdict(driver, world, item, obs, decision):
    call = plant_node(
        driver,
        world,
        "ModelCall",
        model="judge-test",
        job="verify",
        outcome="ok",
        at=clock.now(world),
    )["id"]
    return plant_node(
        driver,
        world,
        "Judgment",
        role="first_judge",
        subject=f"item:{item}",
        verdict=decision,
        source_id=obs["source_id"],
        locator=obs["locator"],
        model_call_id=call,
        at=clock.now(world),
    )["id"]


def beat(driver, seeded, chat):
    w = seeded["world_id"]
    registry = Registry.load(WorldQuery(driver, w))
    meaning, _ = registry.resolve_meaning("depends on")
    alias, _ = registry.resolve_meaning("gated on")
    assert meaning["id"] == alias["id"]
    assert meaning["status"] == "canonical" and meaning["carries_consequences"]
    assert meaning["downstream_end"] == "from"
    claim = make_item(
        driver,
        w,
        chat,
        1,
        kind="assumption",
        title="Prism works in our region",
        status="holds",
        recheck_by=RECHECK,
    )
    demo = make_task(driver, w, chat, 2, title="Record the Prism demo route")
    film = make_task(driver, w, chat, 3, title="Record the film plan")
    build = make_task(driver, w, chat, 4, title="Build the demo capture")
    unrelated = make_task(driver, w, chat, 5, title="Check the garden lights")
    apply(
        driver,
        w,
        [
            create_link(demo, "depends on", claim, "path", recheck_by=RECHECK),
            create_link(film, "depends on", demo, "path", recheck_by=RECHECK),
            create_link(build, "gated on", demo, "path", recheck_by=RECHECK),
            create_link(unrelated, "part of", claim, "path"),
        ],
        [observation(chat, "path", 10)],
    )
    report = observation(chat, "report", 20, statement_class="report_state")
    receipt = apply(driver, w, [refute(claim, contradicts("report"))], [report], caller=LEARNER)
    return w, claim, {demo, film, build}, unrelated, receipt


@pytest.mark.parametrize("states_restriction", [False, True])
def test_reported_restriction_then_fetched_page_preserves_the_gate(
    driver, frozen, seeded, chat, states_restriction
):
    w, claim, dependents, unrelated, reported = beat(driver, seeded, chat)
    assert not item_row(driver, w, claim).get("end_class")
    earlier = impacts(driver, w)
    assert {m["affected_item"] for m in earlier} == dependents
    assert {m["trigger_source_type"] for m in earlier} == {"reported_done"}
    assert unrelated not in dependents
    rows = read(driver, w, "MATCH (c:Contradiction {status: 'open'}) RETURN c")
    assert len(rows) == 1 and rows[0]["c"]["target"] == claim
    proofs = read(
        driver, w, "MATCH (e:Evidence {id: $id}) RETURN e", id=rows[0]["c"]["fresh_evidence_id"]
    )
    assert proofs[0]["e"]["polarity"] == "contradicts"
    assert all(link.get("active_key") for link in links(driver, w).values())
    assert any(line.symbol == "?" for line in reported.lines)

    text = (
        "Prism is available only in Northland." if states_restriction else "Prism has a new logo."
    )
    page = make_source(driver, w, "web_pages", [text])
    observed = observation(page, "page", 1, at=at_minute(50), locator=f"char:0-{len(text)}")
    judged = verdict(
        driver, w, claim, observed, "contradicts" if states_restriction else "not_about_claim"
    )
    if states_restriction:
        apply(
            driver,
            w,
            [refute(claim, contradicts("page")), verify(claim, [piece("page", judged)], "moved")],
            [observed],
            caller=LEARNER,
        )
        assert item_row(driver, w, claim)["end_class"] == "refuted"
        incident = [
            link
            for link in links(driver, w).values()
            if link["to_item"] == claim and link["meaning"] == seeded["meanings"]["depends on"]
        ]
        assert len(incident) == 1 and not incident[0].get("active_key")
        assert incident[0]["stopped_being_true_earliest"] == at_minute(50)
        assert {m["affected_item"] for m in impacts(driver, w)} == dependents
        assert len(impacts(driver, w)) == 6
        assert {m["link_id"] for m in earlier} <= {m["link_id"] for m in impacts(driver, w)}
        assert not read(driver, w, "MATCH (c:Contradiction {status: 'open'}) RETURN c")
        assert health(WorldQuery(driver, w))["live_links_to_ended"] == 0
    else:
        # A claimed confirmation fails its bound D4 judgment and rolls back a paired refute.
        with pytest.raises(BatchDeclined):
            apply(
                driver,
                w,
                [
                    refute(claim, contradicts("page")),
                    verify(claim, [piece("page", judged)], "moved"),
                ],
                [observed],
                caller=LEARNER,
            )
        apply(
            driver,
            w,
            [verify(claim, [piece("page", judged)], "deferred", recheck_by=RECHECK)],
            [observed],
            caller=LEARNER,
        )
        assert not item_row(driver, w, claim).get("end_class")
        assert all(link.get("active_key") for link in links(driver, w).values())
        assert len(impacts(driver, w)) == 3
        assert len(read(driver, w, "MATCH (c:Contradiction {status: 'open'}) RETURN c")) == 1


def test_undo_report_closes_its_queue_and_preserves_undone_marks(driver, frozen, seeded, chat):
    w, _, _, _, receipt = beat(driver, seeded, chat)
    apply(driver, w, [undo(receipt.change_set_id)], [])
    assert {m["state"] for m in impacts(driver, w)} == {"undone"}
    assert not read(driver, w, "MATCH (c:Contradiction {status: 'open'}) RETURN c")


def test_retire_and_verify_defer_link_validation_to_same_transaction_cascade(
    driver, frozen, seeded, chat
):
    from tests.writer.helpers import new_op_id, supports

    w, root, _, _, _, ids = graph(driver, seeded, chat)
    page = make_source(driver, w, "web_pages", ["The repair route is no longer needed."])
    obs = observation(page, "retire", 1, at=at_minute(50), locator="char:0-20")
    judged = verdict(driver, w, root, obs, "not_about_claim")
    apply(
        driver,
        w,
        [
            {
                "op": "end_item",
                "op_id": new_op_id(),
                "item": root,
                "end_class": "retired",
                "stopped_being_true": clock_at(at_minute(50)),
                "evidence_refs": supports("retire"),
                "reason": "This route is no longer needed.",
            },
            verify(root, [piece("retire", judged)], "retired"),
        ],
        [obs],
        caller=LEARNER,
    )
    assert not links(driver, w)[ids[0]].get("active_key")
    assert {m["depth"] for m in impacts(driver, w)} == {1, 2}


def test_rebuild_keeps_impact_and_proposal_snapshots(driver, seeded, chat):
    from grove.writer.restore import restore, take_snapshot
    from tests.writer.test_restore import purge_world_rows

    w, root, *_ = graph(driver, seeded, chat)
    complete(driver, w, root)
    before = json.dumps(
        read(driver, w, "MATCH (m:Impact) RETURN m ORDER BY m.link_id"), default=str
    )
    proposals = json.dumps(
        read(driver, w, "MATCH (p:CascadeProposal) RETURN p ORDER BY p.id"), default=str
    )
    saved = json.loads(json.dumps(take_snapshot(driver, w)))
    rebuild_in_place(driver, w)
    assert (
        json.dumps(read(driver, w, "MATCH (m:Impact) RETURN m ORDER BY m.link_id"), default=str)
        == before
    )
    assert (
        json.dumps(read(driver, w, "MATCH (p:CascadeProposal) RETURN p ORDER BY p.id"), default=str)
        == proposals
    )
    purge_world_rows(driver, w)
    restore(driver, w, saved, [])
    assert (
        json.dumps(read(driver, w, "MATCH (m:Impact) RETURN m ORDER BY m.link_id"), default=str)
        == before
    )
    assert (
        json.dumps(read(driver, w, "MATCH (p:CascadeProposal) RETURN p ORDER BY p.id"), default=str)
        == proposals
    )


def test_new_kind_accepts_validated_propagating_date_declarations(driver, seeded, chat):
    from tests.writer.helpers import create_item
    from tests.writer.test_ontology import judged, propose

    w = seeded["world_id"]
    vocabulary = [{"status": "ready", "role": "open"}]
    fields = [{"name": "delivery", "role": "due"}]
    ids = judged(
        driver,
        w,
        name="delivery route",
        word_type="kind",
        neighbour=seeded["kinds"]["task"],
        fields={"status_vocabulary": (vocabulary, "fits")},
    )
    op = propose(
        "definition",
        ids,
        word_type="kind",
        name="delivery route",
        description="A route with a declared delivery date.",
        neighbours=[
            {"word": "task", "distinction": "A route declares a delivery."},
            {"word": "plan", "distinction": "A route is one delivery."},
        ],
        declared={
            "lifecycle_class": "work_with_state",
            "status_vocabulary": vocabulary,
            "propagating_fields": fields,
        },
    )
    apply(
        driver,
        w,
        [
            op,
            create_item(
                "delivery route",
                "Deliver the orchard tools",
                "definition",
                status="ready",
                recheck_by=RECHECK,
            ),
        ],
        [observation(chat, "definition", 1)],
        caller=LEARNER,
    )
    kind = Registry.load(WorldQuery(driver, w)).resolve_kind("delivery route")
    assert json.loads(kind["propagating_fields"]) == fields


@pytest.mark.parametrize(
    "fields",
    [
        None,
        ["delivery"],
        [{"name": "delivery", "role": "owner"}],
        [{"name": "delivery", "role": []}],
        [{"name": "delivery", "role": "due"}] * 2,
    ],
)
def test_invalid_propagating_declarations_are_refused(fields):
    from grove.writer.errors import Refused
    from grove.writer.ontology import _declared_kind

    with pytest.raises(Refused):
        _declared_kind(
            {
                "lifecycle_class": "work_with_state",
                "status_vocabulary": [{"status": "ready", "role": "open"}],
                "propagating_fields": fields,
            }
        )
