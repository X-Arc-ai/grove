"""Undo and restore actual D9 operations, including the evidence hooks and lineage."""

import json
from datetime import timedelta

import pytest

from grove import clock
from grove.store.d3_fingerprint import live_fingerprint
from grove.store.primitives import MEANING_SUPERSEDED_BY
from grove.writer import record_connector, record_episode, record_source
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
from tests.writer.helpers import (
    EPISODE_FROM,
    EPISODE_TO,
    apply,
    create_item,
    created_id,
    make_source,
    observation,
    read,
    supports,
)
from tests.writer.test_contract_d3 import entries_of_set, undo
from tests.writer.test_restore import change_entries, purge_world_rows


@pytest.fixture(autouse=True)
def erasure_ledger(monkeypatch, tmp_path):
    """check_restore runs a restore, and a restore always reads the world erasures."""
    monkeypatch.setenv("GROVE_FORGET_LEDGER_DIR", str(tmp_path))


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
    extra = {"title": "River supplies calm rooms"} if correction == "restate" else {}
    source = make_source(driver, w, "person_in_views", [extra["title"]]) if extra else None
    _, person = data(driver, w, 5, source=source, connector="person_in_views")

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


@pytest.mark.parametrize("operation", ["supersede_belief", "retire_belief"])
def test_explicit_ending_undo_restores_the_claim_clocks_evidence_and_lineage(
    driver, seeded, operation
):
    w = seeded["world_id"]
    belief, subject, _, rows, _ = fixture(driver, w)
    targets = [belief]
    fields = {"end_class": "world_changed"}
    if operation == "supersede_belief":
        successor = created_id(
            apply(
                driver,
                w,
                [
                    op(
                        "mint_belief",
                        title="River offers calm rooms",
                        subject_refs=[subject],
                        evidence_refs=supports("e1", "e2"),
                    )
                ],
                rows,
                caller=CONNECTOR,
            ),
            entity_type="belief",
        )
        fields = {"end_class": "replaced", "successor": successor}
        targets.append(successor)
    before = current(driver, w, targets)
    _, evidence = data(driver, w, 7)

    receipt = apply(
        driver,
        w,
        [op(operation, target=belief, evidence_refs=supports("e7"), **fields)],
        [evidence],
        caller=CONNECTOR,
    )

    ended = state(driver, w, belief)
    assert ended["state"] == "superseded"
    assert ended["end_class"] == fields["end_class"]
    saved = current(driver, w, targets)
    assert len(saved["evidence"]) == len(before["evidence"]) + 1
    assert saved["items"][belief]["end_class"] == "superseded"
    if operation == "supersede_belief":
        assert ended["successor"] == fields["successor"]
        assert len(saved["links"]) == len(before["links"]) + 1
    else:
        assert saved["items"][belief]["stopped_being_true_latest"] is not None
    undo_and_check(driver, w, receipt, before, targets)


def test_source_retest_undo_restores_weakening_evidence_and_the_previous_horizon(driver, seeded):
    w = seeded["world_id"]
    _, first = data(driver, w, 1)
    _, second = data(driver, w, 2)
    subject = created_id(apply(driver, w, [create_item("organisation", "River", "e1")], [first]))
    connector = record_connector(driver, w, "web_pages")
    method = {"connector": connector, "locator": "https://example.invalid/river-rooms"}
    title = "River supplies quiet rooms"
    declaration = plant_node(
        driver,
        w,
        "HistoryValue",
        value=json.dumps(
            {
                "title": title,
                "subject_refs": [subject],
                "externally_testable": True,
                "retest_how": method,
            }
        ),
    )
    field_subject = f"value:{declaration['id']}"
    fit_ids = []
    for n in range(2):
        call = plant_node(driver, w, "ModelCall", model=f"field-judge-{n}", outcome="ok")
        judge = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role="field_fit",
            subject=field_subject,
            verdict="fits",
        )
        fit_ids.append(judge["id"])
    mint = apply(
        driver,
        w,
        [
            op(
                "mint_belief",
                title=title,
                subject_refs=[subject],
                evidence_refs=supports("e1", "e2"),
                externally_testable=True,
                retest_how=method,
                recheck_by=(clock.now(w) + timedelta(days=1)).isoformat(),
                field_subject=field_subject,
                judgment_ids=fit_ids,
            )
        ],
        [first, second],
        caller=CONNECTOR,
    )
    belief = created_id(mint, entity_type="belief")
    _, third = data(driver, w, 3)
    apply(
        driver,
        w,
        [op("promote_belief", target=belief, evidence_refs=supports("e3"))],
        [third],
        caller=CONNECTOR,
    )
    _, contrary = data(driver, w, 4)
    attach(
        driver, w, belief, contrary, "contradicts", judgment_ids=judges(driver, w, belief, contrary)
    )
    assert state(driver, w, belief)["state"] == "weakening"
    before = current(driver, w, [belief])

    # The new source arrives after the claim was minted and matches its declared method.
    at = clock.now(w)
    source_id = record_source(
        driver,
        w,
        connector_id=connector,
        external_locator=method["locator"],
        content=title,
        parser_version="test-1",
    )
    episode_id = record_episode(
        driver,
        w,
        connector_id=connector,
        source_ids=[source_id],
        first_message_key="source-retest",
        last_message_key="source-retest",
        start_time=EPISODE_FROM,
        end_time=EPISODE_TO,
        cut_rule="chat_window_90m",
    )
    source = {"source_id": source_id, "connector_id": connector, "episode_id": episode_id}
    evidence = observation(source, "retest", 1, locator="whole", at=at)
    verdicts = []
    for n, role in enumerate(("first_judge", "second_judge")):
        call = plant_node(driver, w, "ModelCall", model=f"source-judge-{n}", outcome="ok")
        judge = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role=role,
            subject=f"item:{belief}",
            verdict="supports",
            source_id=source_id,
            locator="whole",
            at=at,
        )
        verdicts.append(judge["id"])
    next_check = at + timedelta(days=7)
    horizon = next_check.isoformat()

    receipt = apply(
        driver,
        w,
        [
            op(
                "retest_belief",
                target=belief,
                pieces=[{"ev_id": "retest", "judgment_id": verdicts[0]}],
                second_judge=[verdicts[1]],
                recheck_by=horizon,
            )
        ],
        [evidence],
        caller=CONNECTOR,
    )

    assert state(driver, w, belief)["state"] == "established"
    saved = current(driver, w, [belief])
    assert saved["items"][belief]["last_confirmed"] == at
    assert saved["items"][belief]["recheck_by"] == next_check
    assert len(saved["evidence"]) == len(before["evidence"]) + 1
    undo_and_check(driver, w, receipt, before, [belief])
