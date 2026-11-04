"""Regressions for the independent R4 source-polarity and source-time findings."""

import pytest

from tests.cascade.test_demo import frozen  # noqa: F401
from tests.cascade.test_writer import graph, impacts, links
from tests.writer.helpers import (
    LEARNER,
    apply,
    at_minute,
    create_link,
    created_id,
    make_source,
    observation,
    read,
    supports,
)
from tests.writer.test_contract_d2 import evidence_rows, item_row, one, refused
from tests.writer.test_contract_d3 import contradicts, refute

pytestmark = pytest.mark.usefixtures("frozen")


def test_report_with_confirmed_support_cannot_refute_the_item(driver, seeded, chat):
    world, root, child, leaf, _, ids = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The repair remains needed."])
    confirmed_support = observation(page, "support", 1, at=at_minute(30), locator="whole")
    contrary_report = observation(chat, "contrary", 20, at=at_minute(20))
    receipt = apply(
        driver,
        world,
        [refute(root, contradicts("contrary") + supports("support"))],
        [contrary_report, confirmed_support],
        caller=LEARNER,
    )
    row = item_row(driver, world, root)
    queued = read(driver, world, "MATCH (c:Contradiction {status: 'open'}) RETURN c")
    observed = {
        "end_class": row.get("end_class"),
        "open_contradictions": len(queued),
        "consequence_link_live": bool(links(driver, world)[ids[0]].get("active_key")),
        "mark_sources": sorted({m["trigger_source_type"] for m in impacts(driver, world)}),
    }
    assert not row.get("end_class"), observed
    assert len(queued) == 1, observed
    assert observed["consequence_link_live"], observed
    assert {m["affected_item"] for m in impacts(driver, world)} == {child, leaf}
    assert observed["mark_sources"] == ["reported_done"]
    support_rows = [
        e for e in evidence_rows(driver, world, root) if e["source_id"] == page["source_id"]
    ]
    assert len(support_rows) == 1 and support_rows[0]["polarity"] == "supports"
    [queued_proof] = read(
        driver,
        world,
        "MATCH (e:Evidence {id: $id}) RETURN e",
        id=queued[0]["c"]["fresh_evidence_id"],
    )
    assert queued_proof["e"]["polarity"] == "contradicts"
    assert queued_proof["e"]["source_id"] == chat["source_id"]
    assert receipt.change_set_id


@pytest.mark.parametrize("later_polarity", ["supports", "contradicts"])
def test_refutation_cascade_uses_contrary_evidence_time(driver, seeded, chat, later_polarity):
    world, root, _, _, _, ids = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The repair never was needed."])
    confirmed_contrary = observation(page, "contrary", 1, at=at_minute(30), locator="whole")
    later_support = observation(chat, "support", 20, at=at_minute(50))
    apply(
        driver,
        world,
        [
            refute(
                root, contradicts("contrary") + [{"ev_id": "support", "polarity": later_polarity}]
            )
        ],
        [confirmed_contrary, later_support],
        caller=LEARNER,
    )
    row = links(driver, world)[ids[0]]
    assert item_row(driver, world, root)["end_class"] == "refuted"
    assert row["stopped_being_true_earliest"] == at_minute(30), {
        "observed_stop": row["stopped_being_true_earliest"].isoformat(),
        "expected_contrary_time": at_minute(30).isoformat(),
        "unrelated_support_time": at_minute(50).isoformat(),
    }
    [entry] = read(
        driver,
        world,
        "MATCH (e:ChangeEntry {entity_type: 'item', entity_id: $id, field: 'end_class'}) "
        "MATCH (p:Evidence {id: e.evidence_id}) RETURN e.world_time AS at, p.source_id AS source",
        id=root,
    )
    assert entry == {"at": at_minute(30), "source": page["source_id"]}
    stored_links = links(driver, world)
    assert {stored_links[m["link_id"]]["became_true_earliest"] for m in impacts(driver, world)} == {
        at_minute(30)
    }


def test_support_alone_cannot_request_refutation(driver, seeded, chat):
    world, root, _, _, _, ids = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The repair remains needed."])
    proof = observation(page, "support", 1, at=at_minute(30), locator="whole")
    one(refused(driver, world, [refute(root, supports("support"))], [proof]), "missing_provenance")
    assert not item_row(driver, world, root).get("end_class")
    assert links(driver, world)[ids[0]]["active_key"]
    assert not impacts(driver, world)


def test_confirmed_support_does_not_raise_link_refutation_rank(driver, seeded, chat):
    world, root, child, _, _, _ = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The child is part of the repair plan."])
    proof = observation(page, "standing", 1, at=at_minute(20), locator="whole")
    receipt = apply(driver, world, [create_link(child, "part of", root, "standing")], [proof])
    link = created_id(receipt, ">", "link")
    report = observation(chat, "contrary", 20, at=at_minute(30))
    support = observation(page, "support", 1, at=at_minute(40), locator="whole")
    one(
        refused(
            driver,
            world,
            [refute(link, contradicts("contrary") + supports("support"))],
            [report, support],
            caller=LEARNER,
        ),
        "invalid_arguments",
    )
    assert links(driver, world)[link]["active_key"]
