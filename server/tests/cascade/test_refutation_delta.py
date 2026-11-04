"""Independent controls for polarity, proof identity, and refutation clocks."""

import pytest

from grove.writer import BatchDeclined
from tests.cascade.test_demo import frozen  # noqa: F401
from tests.cascade.test_writer import graph, impacts, links
from tests.writer.helpers import (
    LEARNER,
    apply,
    at_minute,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)
from tests.writer.test_contract_d2 import evidence_rows, item_row
from tests.writer.test_contract_d3 import contradicts, refute

pytestmark = pytest.mark.usefixtures("frozen")


def ending(driver, world, target, entity_type):
    [entry] = read(
        driver,
        world,
        "MATCH (e:ChangeEntry {entity_type: $entity_type, entity_id: $id, field: 'end_class'}) "
        "MATCH (p:Evidence {id: e.evidence_id}) "
        "RETURN e.world_time AS at, p.id AS proof, p.source_time AS proof_at, "
        "p.polarity AS polarity, p.message_key AS message_key",
        id=target,
        entity_type=entity_type,
    )
    return entry


@pytest.mark.parametrize("entity_type", ["item", "link"])
@pytest.mark.parametrize("reverse", [False, True])
def test_latest_equal_rank_contrary_proof_is_order_independent(
    driver, seeded, chat, entity_type, reverse
):
    world, root, child, leaf, _, ids = graph(driver, seeded, chat)
    page = make_source(
        driver,
        world,
        "web_pages",
        ["An earlier contrary claim.", "A later contrary claim.", "A supporting claim."],
    )
    observations = [
        observation(page, "early", 1, at=at_minute(25), locator="char:0-26"),
        observation(page, "late", 2, at=at_minute(35), locator="char:27-50"),
        observation(page, "support", 3, at=at_minute(50), locator="char:51-70"),
    ]
    refs = contradicts("early", "late") + supports("support")
    if reverse:
        refs.reverse()
    target = root if entity_type == "item" else ids[0]
    apply(driver, world, [refute(target, refs)], observations, caller=LEARNER)
    entry = ending(driver, world, target, entity_type)
    assert entry["at"] == entry["proof_at"] == at_minute(35), entry
    assert entry["polarity"] == "contradicts", entry
    assert entry["message_key"] == observations[1]["message_key"], entry
    if entity_type == "item":
        stored = links(driver, world)
        assert stored[ids[0]]["stopped_being_true_earliest"] == at_minute(35)
        assert {m["affected_item"] for m in impacts(driver, world)} == {child, leaf}
        assert {stored[m["link_id"]]["became_true_earliest"] for m in impacts(driver, world)} == {
            at_minute(35)
        }
    else:
        assert links(driver, world)[target]["end_class"] == "refuted"
        assert not links(driver, world)[target].get("active_key")
    print("equal_rank", entity_type, reverse, "proof_time=09:35", "support_time=09:50")


def test_deferred_reports_keep_supports_and_only_queue_distinct_contrary_rows(driver, seeded, chat):
    world, root, child, leaf, _, ids = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The repair remains needed."])
    rows = [
        observation(chat, "first", 20, at=at_minute(20)),
        observation(chat, "second", 21, at=at_minute(21)),
        observation(page, "support", 1, at=at_minute(50), locator="char:0-26"),
    ]
    apply(
        driver,
        world,
        [refute(root, contradicts("first", "first", "second") + supports("support"))],
        rows,
        caller=LEARNER,
    )
    assert not item_row(driver, world, root).get("end_class")
    queued = read(
        driver,
        world,
        "MATCH (c:Contradiction {target: $target, status: 'open'}) "
        "MATCH (e:Evidence {id: c.fresh_evidence_id}) RETURN e",
        target=root,
    )
    assert len(queued) == 2
    assert {r["e"]["polarity"] for r in queued} == {"contradicts"}
    assert {r["e"]["message_key"] for r in queued} == {
        rows[0]["message_key"],
        rows[1]["message_key"],
    }
    supported = [
        e for e in evidence_rows(driver, world, root) if e["message_key"] == rows[2]["message_key"]
    ]
    assert len(supported) == 1 and supported[0]["polarity"] == "supports"
    stored = links(driver, world)
    assert stored[ids[0]]["active_key"]
    assert {m["affected_item"] for m in impacts(driver, world)} == {child, leaf}
    assert {m["trigger_source_type"] for m in impacts(driver, world)} == {"reported_done"}
    assert {stored[m["link_id"]]["became_true_earliest"] for m in impacts(driver, world)} == {
        at_minute(21)
    }
    print("deferred", "contradictions=2", "support_rows=1", "mark_time=09:21")


@pytest.mark.parametrize("entity_type", ["item", "link"])
def test_existing_evidence_refs_keep_proof_identity_and_time(driver, seeded, chat, entity_type):
    world, root, _, _, _, ids = graph(driver, seeded, chat)
    target = root if entity_type == "item" else ids[0]
    page = make_source(driver, world, "web_pages", ["A contrary claim.", "A supporting claim."])
    rows = [
        observation(page, "contrary", 1, at=at_minute(25), locator="char:0-17"),
        observation(page, "support", 2, at=at_minute(50), locator="char:18-37"),
    ]
    attach = [
        {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": target,
            "ev_id": key,
            "polarity": polarity,
        }
        for key, polarity in [("contrary", "contradicts"), ("support", "supports")]
    ]
    apply(driver, world, attach, rows, caller=LEARNER)
    held = read(
        driver,
        world,
        "MATCH (e:Evidence {target_id: $target, source_id: $source}) RETURN e",
        target=target,
        source=page["source_id"],
    )
    assert len(held) == 2
    refs = [{"evidence_id": r["e"]["id"], "polarity": r["e"]["polarity"]} for r in held]
    proof = next(r["e"]["id"] for r in held if r["e"]["polarity"] == "contradicts")
    apply(driver, world, [refute(target, refs)], [], caller=LEARNER)
    entry = ending(driver, world, target, entity_type)
    assert entry["at"] == entry["proof_at"] == at_minute(25), entry
    assert entry["proof"] == proof and entry["polarity"] == "contradicts", entry
    print("existing_evidence", entity_type, "proof_identity_preserved", "proof_time=09:25")


@pytest.mark.parametrize("derivation", ["rolled_up", "inferred"])
def test_link_refutation_cannot_authorize_from_one_row_and_record_a_weaker_row(
    driver, seeded, chat, derivation
):
    from tests.writer.helpers import create_link, created_id

    world, root, child, _, _, _ = graph(driver, seeded, chat)
    page = make_source(driver, world, "web_pages", ["The child is part of the repair plan."])
    standing = observation(page, "standing", 1, at=at_minute(20), locator="char:0-37")
    receipt = apply(driver, world, [create_link(child, "part of", root, "standing")], [standing])
    target = created_id(receipt, ">", "link")
    [premise] = read(
        driver,
        world,
        "MATCH (e:Evidence {target_id: $target, polarity: 'supports'}) RETURN e",
        target=target,
    )
    assert premise["e"]["source_type"] == "confirmed"
    reminder = make_source(driver, world, "reminders", ["Review the repair link."])
    derived = observation(
        chat, "rolled", 20, at=at_minute(30), derivation=derivation, premises=[premise["e"]["id"]]
    )
    planned = observation(reminder, "planned", 1, at=at_minute(40))
    if derivation == "inferred":
        with pytest.raises(BatchDeclined):
            apply(
                driver,
                world,
                [refute(target, contradicts("rolled", "planned"))],
                [derived, planned],
                caller=LEARNER,
            )
        assert links(driver, world)[target]["active_key"]
        assert not read(
            driver,
            world,
            "MATCH (e:ChangeEntry {entity_type: 'link', entity_id: $target, "
            "field: 'end_class'}) RETURN e",
            target=target,
        )
        return
    apply(
        driver,
        world,
        [refute(target, contradicts("rolled", "planned"))],
        [derived, planned],
        caller=LEARNER,
    )
    [proof] = read(
        driver,
        world,
        "MATCH (e:ChangeEntry {entity_type: 'link', entity_id: $target, field: 'end_class'}) "
        "MATCH (p:Evidence {id: e.evidence_id}) RETURN e.world_time AS at, "
        "p.source_type AS source_type, p.derivation AS derivation, p.polarity AS polarity",
        target=target,
    )
    observed = {
        "end_class": links(driver, world)[target].get("end_class"),
        "standing_rank": "confirmed",
        "proof_source_type": proof["source_type"],
        "proof_derivation": proof["derivation"],
        "proof_time": proof["at"].isoformat(),
    }
    assert proof["source_type"] == "confirmed", observed
    assert proof["derivation"] == "rolled_up", observed
    assert proof["at"] == at_minute(30), observed
    assert proof["polarity"] == "contradicts", observed
