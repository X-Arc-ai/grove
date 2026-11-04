"""Undo and restore actual D9 operations, including the evidence hooks and lineage."""

import pytest

from grove.store.d3_fingerprint import live_fingerprint
from grove.store.primitives import MEANING_SUPERSEDED_BY
from grove.writer.restore import rebuild_in_place, restore, take_snapshot
from grove.writer.testing import plant_node
from tests.beliefs.test_state_machine import (
    CONNECTOR,
    attach,
    data,
    establish,
    fixture,
    judges,
    op,
    state,
)
from tests.writer.helpers import apply, create_item, created_id, read, supports
from tests.writer.test_contract_d3 import entries_of_set, undo
from tests.writer.test_restore import change_entries, purge_world_rows


def content(row):
    return {
        key: value
        for key, value in row.items()
        if key not in {"version", "touched_at"} and value != []
    }


def current(driver, w, belief_ids):
    """The prior semantic state: all claim columns and every active evidence/anchor identity."""
    queries = {
        "items": "MATCH (n:Item) WHERE n.id IN $ids RETURN n",
        "beliefs": "MATCH (n:Belief) WHERE n.item_id IN $ids RETURN n",
        "evidence": "MATCH (n:Evidence) WHERE n.target_id IN $ids AND n.active = true RETURN n",
        "links": "MATCH (:Item)-[n:LINK]->(:Item) WHERE n.from_item IN $ids "
        "AND n.active_key IS NOT NULL RETURN n",
    }
    return {
        name: {
            row["n"].get("item_id") if name == "beliefs" else row["n"]["id"]: content(row["n"])
            for row in read(driver, w, query, ids=belief_ids)
        }
        for name, query in queries.items()
    }


def check_restore(driver, w):
    before = live_fingerprint(driver, w)
    snapshot = take_snapshot(driver, w)
    rebuild_in_place(driver, w)
    assert live_fingerprint(driver, w) == before
    purge_world_rows(driver, w)
    restore(driver, w, snapshot, [])
    assert live_fingerprint(driver, w) == before


def equal_state(actual, expected):
    for table, rows in expected.items():
        assert set(actual[table]) == set(rows), table
        for row_id, row in rows.items():
            for field in sorted(row.keys() | actual[table][row_id].keys()):
                assert actual[table][row_id].get(field) == row.get(field), (table, row_id, field)


def undo_and_check(driver, w, receipt, before, belief_ids):
    original = change_entries(driver, w)
    inverse = apply(driver, w, [undo(receipt.change_set_id)], [])
    equal_state(current(driver, w, belief_ids), before)
    assert change_entries(driver, w)[: len(original)] == original
    entries = entries_of_set(driver, w, inverse.change_set_id)
    assert entries
    assert {entry["inverse_of"] for entry in entries} == {receipt.change_set_id}
    check_restore(driver, w)
    equal_state(current(driver, w, belief_ids), before)


def test_mint_undo_ends_its_evidence_and_anchors_without_deleting_history(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, receipt = fixture(driver, w)
    original = change_entries(driver, w)
    minted = current(driver, w, [belief])
    assert len(minted["evidence"]) == 2
    assert len(minted["links"]) == 1

    inverse = apply(driver, w, [undo(receipt.change_set_id)], [])

    ended = state(driver, w, belief)
    assert ended["state"] == "superseded"
    assert ended["end_class"] == "wrong"
    assert not ended.get("supporting_episodes")
    assert not ended.get("contradicting_episodes")
    saved = current(driver, w, [belief])
    assert saved["items"][belief]["end_class"] == "refuted"
    assert not saved["links"]
    assert not saved["evidence"]
    assert change_entries(driver, w)[: len(original)] == original
    assert {entry["inverse_of"] for entry in entries_of_set(driver, w, inverse.change_set_id)} == {
        receipt.change_set_id
    }
    check_restore(driver, w)
    assert current(driver, w, [belief]) == saved


def test_promotion_undo_restores_the_two_episodes_and_tentative_state(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    before = current(driver, w, [belief])
    _, third = data(driver, w, 3)

    receipt = apply(
        driver,
        w,
        [op("promote_belief", target=belief, evidence_refs=supports("e3"))],
        [third],
        caller=CONNECTOR,
    )

    assert state(driver, w, belief)["state"] == "established"
    assert len(state(driver, w, belief)["supporting_episodes"]) == 3
    undo_and_check(driver, w, receipt, before, [belief])


@pytest.mark.parametrize("transition", ["weaken", "restore"])
def test_mechanical_transition_undo_restores_prior_counts_and_active_evidence(
    driver, seeded, transition
):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    _, contrary = data(driver, w, 4)
    verdicts = judges(driver, w, belief, contrary)
    if transition == "restore":
        attach(driver, w, belief, contrary, "contradicts", judgment_ids=verdicts)
        assert state(driver, w, belief)["state"] == "weakening"
    before = current(driver, w, [belief])

    if transition == "weaken":
        receipt = attach(driver, w, belief, contrary, "contradicts", judgment_ids=verdicts)
        assert state(driver, w, belief)["state"] == "weakening"
    else:
        _, support = data(driver, w, 5)
        receipt = attach(driver, w, belief, support)
        assert state(driver, w, belief)["state"] == "established"

    undo_and_check(driver, w, receipt, before, [belief])


@pytest.mark.parametrize("correction", ["right", "not_any_more", "wrong", "restate"])
def test_person_correction_undo_restores_the_prior_claim_and_restatement_lineage(
    driver, seeded, correction
):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    before = current(driver, w, [belief])
    _, person = data(driver, w, 5, connector="person_in_views")
    extra = {"title": "River supplies calm rooms"} if correction == "restate" else {}

    receipt = apply(
        driver,
        w,
        [
            op(
                "correct_belief",
                target=belief,
                correction=correction,
                evidence_refs=supports("e5"),
                **extra,
            )
        ],
        [person],
    )
    successor = state(driver, w, belief).get("successor")

    undo_and_check(driver, w, receipt, before, [belief])
    if correction == "restate":
        assert successor is not None
        retained = current(driver, w, [successor])
        assert retained["beliefs"][successor]["state"] == "superseded"
        assert retained["items"][successor]["end_class"] == "refuted"
        assert not retained["evidence"]
        assert not retained["links"]


def test_belief_merge_undo_restores_both_claims_evidence_and_subject_anchors(driver, seeded):
    w = seeded["world_id"]
    survivor, _, _, _, _ = fixture(driver, w)
    _, first = data(driver, w, 11)
    _, second = data(driver, w, 12)
    subject = created_id(
        apply(driver, w, [create_item("organisation", "River Annex", "e11")], [first])
    )
    mint = apply(
        driver,
        w,
        [
            op(
                "mint_belief",
                title="River offers quiet rooms",
                subject_refs=[subject],
                evidence_refs=supports("e11", "e12"),
            )
        ],
        [first, second],
        caller=CONNECTOR,
    )
    absorbed = created_id(mint, entity_type="belief")
    verdicts = []
    pair = "|".join(sorted((f"belief:{survivor}", f"belief:{absorbed}")))
    for n in range(2):
        call = plant_node(driver, w, "ModelCall", model=f"judge-{n}", outcome="ok")
        verdict = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role="same_belief",
            subject=pair,
            verdict="same",
        )
        verdicts.append(verdict["id"])
    before = current(driver, w, [survivor, absorbed])

    receipt = apply(
        driver,
        w,
        [op("merge_beliefs", survivor=survivor, absorbed=absorbed, judgment_ids=verdicts)],
        [],
        caller=CONNECTOR,
    )

    assert state(driver, w, absorbed)["successor"] == survivor
    assert len(current(driver, w, [survivor])["evidence"]) == 4
    lineage = read(
        driver,
        w,
        "MATCH (:Item)-[l:LINK {from_item: $id, meaning: $meaning}]->(:Item) "
        "WHERE l.active_key IS NOT NULL RETURN l",
        id=absorbed,
        meaning=MEANING_SUPERSEDED_BY,
    )
    assert len(lineage) == 1
    undo_and_check(driver, w, receipt, before, [survivor, absorbed])
