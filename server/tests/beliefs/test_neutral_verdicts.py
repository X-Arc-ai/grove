"""Retest neutrality is semantic; exact recorded judgments remain unchanged."""

import pytest

from grove.writer import BatchDeclined
from tests.beliefs.test_guards import retest_case
from tests.beliefs.test_state_machine import CONNECTOR, attach, op, state
from tests.writer.belief_helpers import set_confirmation, set_judgment_verdict
from tests.writer.helpers import T0, apply, read


def set_verdict_pair(driver, world_id, ids, verdicts):
    for judgment_id, verdict in zip(ids, verdicts, strict=True):
        set_judgment_verdict(driver, world_id, [judgment_id], verdict)


def assert_verdicts_retained(driver, world_id, ids, verdicts):
    stored = read(
        driver,
        world_id,
        "MATCH (j:Judgment) WHERE j.id IN $ids RETURN j.id AS id, j.verdict AS verdict",
        ids=ids,
    )
    assert {row["id"]: row["verdict"] for row in stored} == dict(zip(ids, verdicts, strict=True))


@pytest.mark.parametrize(
    "verdicts",
    [("complements", "not_about_claim"), ("not_about_claim", "complements")],
)
def test_mixed_neutral_retest_finishes_without_a_conflict(driver, seeded, verdicts):
    w = seeded["world_id"]
    belief, ev, operation, ids, _ = retest_case(driver, w)
    set_verdict_pair(driver, w, ids, verdicts)
    set_confirmation(driver, w, belief, T0)
    before = state(driver, w, belief)
    apply(driver, w, [operation], [ev], caller=CONNECTOR)
    saved = state(driver, w, belief)
    assert saved["state"] == before["state"]
    assert saved["last_retest_outcome"] == "complements"
    assert set(saved["last_retest_judgment_ids"]) == set(ids)
    assert saved["last_retest_source_ids"] == [ev["source_id"]]
    assert not read(driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=ev["source_id"])
    assert not read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item["last_confirmed"] == T0
    assert item["recheck_by"].isoformat() == operation["recheck_by"]
    assert_verdicts_retained(driver, w, ids, verdicts)


@pytest.mark.parametrize(
    "verdicts",
    [
        ("complements", "supports"),
        ("supports", "complements"),
        ("complements", "contradicts"),
        ("contradicts", "complements"),
    ],
)
def test_complement_and_substantive_verdict_queue_a_bound_disagreement(driver, seeded, verdicts):
    w = seeded["world_id"]
    belief, ev, operation, ids, _ = retest_case(driver, w)
    set_verdict_pair(driver, w, ids, verdicts)
    set_confirmation(driver, w, belief, T0)
    before = state(driver, w, belief)
    apply(driver, w, [operation], [ev], caller=CONNECTOR)
    saved = state(driver, w, belief)
    assert saved["state"] == before["state"]
    assert saved["last_retest_outcome"] == "disagreement"
    [row] = read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)
    assert row["c"]["fresh_source_id"] == ev["source_id"]
    assert row["c"]["fresh_locator"] == ev["locator"]
    assert set(row["c"]["judgment_ids"]) == set(ids)
    assert not read(driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=ev["source_id"])
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item["last_confirmed"] == T0
    assert item["recheck_by"].isoformat() == operation["recheck_by"]
    assert_verdicts_retained(driver, w, ids, verdicts)


@pytest.mark.parametrize("existing_evidence", [False, True])
@pytest.mark.parametrize(
    "verdicts",
    [
        ("complements", "complements"),
        ("not_about_claim", "not_about_claim"),
        ("complements", "not_about_claim"),
    ],
)
def test_open_conflict_refuses_wholly_neutral_judges(driver, seeded, verdicts, existing_evidence):
    w = seeded["world_id"]
    belief, ev, _, ids, _ = retest_case(driver, w)
    set_verdict_pair(driver, w, ids, verdicts)
    fresh_ref = {"ev_id": ev["ev_id"]}
    if existing_evidence:
        attach(driver, w, belief, ev)
        [row] = read(driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=ev["source_id"])
        fresh_ref = {"fresh_evidence_id": row["e"]["id"]}
    before = state(driver, w, belief)
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [op("open_contradiction", target=belief, judgment_ids=ids, **fresh_ref)],
            [ev],
            caller=CONNECTOR,
        )
    assert error.value.refusals[0].rule == "belief_complement"
    assert state(driver, w, belief) == before
    assert not read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)
    assert_verdicts_retained(driver, w, ids, verdicts)
