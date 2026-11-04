"""D9 regression controls for authority, ending evidence, re-tests and atomic refusal."""

import json
from datetime import timedelta

import pytest

from grove import clock
from grove.writer import BatchDeclined, beliefs
from grove.writer.requests import enqueue_request
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
from tests.writer.belief_helpers import set_belief_fields, set_confirmation, set_judgment_verdict
from tests.writer.helpers import (
    HOST,
    LEARNER,
    T0,
    apply,
    created_id,
    make_source,
    observation,
    read,
    request_key,
    supports,
)


def test_host_verbatim_correction_and_exact_restatement(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    source = make_source(driver, w, "ai_host", ["River supplies calm rooms"])
    enqueue_request(
        driver,
        w,
        HOST,
        request_key(),
        source_id=source["source_id"],
        relay="correction",
        verbatim=True,
        host_message_id="synthetic-message",
    )
    ev = observation(source, "person", 1, locator="whole", at=T0 + timedelta(hours=5))
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [
                op(
                    "correct_belief",
                    target=belief,
                    correction="restate",
                    title="River supplies loud rooms",
                    evidence_refs=supports("person"),
                )
            ],
            [ev],
            caller=HOST,
        )
    assert error.value.refusals[0].rule == "verbatim_required"
    assert state(driver, w, belief)["state"] == "tentative"
    apply(
        driver,
        w,
        [
            op(
                "correct_belief",
                target=belief,
                correction="restate",
                title="River supplies calm rooms",
                evidence_refs=supports("person"),
            )
        ],
        [ev],
        caller=HOST,
    )
    successor = state(driver, w, belief)["successor"]
    assert state(driver, w, successor)["state"] == "established"
    assert state(driver, w, successor)["held_by_the_person"] == T0 + timedelta(hours=5)


def test_right_after_not_any_more_clears_world_end(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    for number, action in ((5, "not_any_more"), (6, "right")):
        _, ev = data(driver, w, number, connector="person_in_views")
        apply(
            driver,
            w,
            [
                op(
                    "correct_belief",
                    target=belief,
                    correction=action,
                    evidence_refs=supports(ev["ev_id"]),
                )
            ],
            [ev],
        )
    item = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=belief)[0]["i"]
    assert item.get("end_class") is None
    assert item.get("stopped_being_true_precision") is None
    assert state(driver, w, belief)["state"] == "established"


@pytest.mark.parametrize("action", ["retire_belief", "supersede_belief"])
def test_agent_end_requires_new_evidence_and_preserves_lineage(driver, seeded, action):
    w = seeded["world_id"]
    belief, subject, _, rows, _ = fixture(driver, w)
    successor = created_id(
        apply(
            driver,
            w,
            [
                op(
                    "mint_belief",
                    title="River supplies calm spaces",
                    subject_refs=[subject],
                    evidence_refs=supports("e1", "e2"),
                )
            ],
            rows,
            caller=LEARNER,
        ),
        entity_type="belief",
    )
    fields = {"successor": successor} if action == "supersede_belief" else {}
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [
                op(
                    action,
                    target=belief,
                    end_class="replaced",
                    evidence_refs=supports("e1"),
                    **fields,
                )
            ],
            [rows[0]],
            caller=CONNECTOR,
        )
    assert error.value.refusals[0].rule == "belief_end_evidence"
    assert state(driver, w, belief)["state"] == "tentative"
    _, fresh = data(driver, w, 6)
    apply(
        driver,
        w,
        [op(action, target=belief, end_class="replaced", evidence_refs=supports("e6"), **fields)],
        [fresh],
        caller=CONNECTOR,
    )
    assert state(driver, w, belief)["state"] == "superseded"
    assert len(read(driver, w, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=belief)) == 3
    if fields:
        assert state(driver, w, belief)["successor"] == successor
        assert read(
            driver,
            w,
            "MATCH (:Item)-[l:LINK {from_item: $id, to_item: $next}]->(:Item) RETURN l",
            id=belief,
            next=successor,
        )


def pair_judges(driver, w, first, second):
    result = []
    for number in range(2):
        call = plant_node(driver, w, "ModelCall", model=f"model-{number}", outcome="ok")
        row = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role="same_belief",
            subject="|".join(sorted((f"belief:{first}", f"belief:{second}"))),
            verdict="same",
            at=clock.now(w),
        )
        result.append(row["id"])
    return result


def test_merge_two_calls_preserves_support_and_lineage(driver, seeded):
    w = seeded["world_id"]
    first, subject, _, rows, _ = fixture(driver, w)
    second = created_id(
        apply(
            driver,
            w,
            [
                op(
                    "mint_belief",
                    title="River supplies calm spaces",
                    subject_refs=[subject],
                    evidence_refs=supports("e1", "e2"),
                )
            ],
            rows,
            caller=LEARNER,
        ),
        entity_type="belief",
    )
    ids = pair_judges(driver, w, first, second)
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [op("merge_beliefs", survivor=first, absorbed=second, judgment_ids=[ids[0], ids[0]])],
            [],
            caller=CONNECTOR,
        )
    assert error.value.refusals[0].rule == "two_models"
    assert state(driver, w, second)["state"] == "tentative"
    apply(
        driver,
        w,
        [op("merge_beliefs", survivor=first, absorbed=second, judgment_ids=ids)],
        [],
        caller=CONNECTOR,
    )
    assert state(driver, w, second)["successor"] == first
    assert len(state(driver, w, first)["supporting_episodes"]) == 2


def test_disagreement_is_atomic_and_can_open_durable_conflict(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    _, fresh = data(driver, w, 6)
    ids = judges(driver, w, belief, fresh)
    # Replace the second recorded decision without changing its evidence binding.
    set_judgment_verdict(driver, w, [ids[1]], "supports")
    before = state(driver, w, belief)
    with pytest.raises(BatchDeclined) as error:
        attach(driver, w, belief, fresh, "contradicts", judgment_ids=ids)
    assert error.value.refusals[0].rule == "judges_disagree"
    assert state(driver, w, belief) == before
    assert not read(
        driver, w, "MATCH (e:Evidence {target_id: $id, polarity:'contradicts'}) RETURN e", id=belief
    )
    apply(
        driver,
        w,
        [op("open_contradiction", target=belief, ev_id="e6", judgment_ids=ids)],
        [fresh],
        caller=CONNECTOR,
    )
    conflict = read(driver, w, "MATCH (c:Contradiction) RETURN c")[0]["c"]
    assert conflict["fresh_source_time"] == T0 + timedelta(hours=6)
    assert conflict["fresh_episode_id"] == fresh["episode_id"]
    assert conflict["judgment_ids"] == ids
    assert conflict.get("fresh_evidence_id") is None


def retest_case(driver, w, *, weakening=True):
    belief, _, _, _ = establish(driver, w)
    _, contrary = data(driver, w, 5)
    if weakening:
        attach(
            driver,
            w,
            belief,
            contrary,
            "contradicts",
            judgment_ids=judges(driver, w, belief, contrary),
        )
    set_belief_fields(driver, w, belief, externally_testable=True)
    at = clock.now(w)
    source = make_source(driver, w, "web_pages", ["River supplies quiet rooms"])
    stored_source = read(driver, w, "MATCH (s:Source {id:$id}) RETURN s", id=source["source_id"])[
        0
    ]["s"]
    set_belief_fields(
        driver,
        w,
        belief,
        retest_how=json.dumps(
            {"connector": source["connector_id"], "locator": stored_source["external_locator"]}
        ),
    )
    ev = observation(source, "retest", 1, locator="whole", at=at)
    ids = []
    for number, role in enumerate(("first_judge", "second_judge")):
        call = plant_node(driver, w, "ModelCall", model=f"model-{number}", outcome="ok")
        row = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role=role,
            subject=f"item:{belief}",
            verdict="supports",
            source_id=source["source_id"],
            locator="whole",
            at=at,
        )
        ids.append(row["id"])
    operation = op(
        "retest_belief",
        target=belief,
        pieces=[{"ev_id": "retest", "judgment_id": ids[0]}],
        second_judge=[ids[1]],
        recheck_by=(at + timedelta(days=7)).isoformat(),
    )
    return belief, ev, operation, ids, at


def test_retest_restores_and_moves_confirmation_from_source_time(driver, seeded):
    w = seeded["world_id"]
    belief, ev, operation, _, at = retest_case(driver, w)
    apply(driver, w, [operation], [ev], caller=CONNECTOR)
    assert state(driver, w, belief)["state"] == "established"
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item["last_confirmed"] == at
    assert item["derivation"] == "inferred"


def test_retest_with_second_disagreement_cannot_stamp_confirmation(driver, seeded):
    w = seeded["world_id"]
    belief, ev, operation, ids, _ = retest_case(driver, w)
    set_judgment_verdict(driver, w, [ids[1]], "contradicts")
    before = state(driver, w, belief)
    apply(driver, w, [operation], [ev], caller=CONNECTOR)
    assert state(driver, w, belief)["state"] == before["state"]
    assert state(driver, w, belief)["last_retest_outcome"] == "disagreement"
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item.get("last_confirmed") is None
    assert item["recheck_by"].isoformat() == operation["recheck_by"]
    queued = read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)[0]["c"]
    assert set(queued["judgment_ids"]) == set(ids)
    assert queued["fresh_source_id"] == ev["source_id"]
    assert not read(driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=ev["source_id"])


def test_cascade_contract_stages_current_evidence_without_running_a_planner():
    from types import SimpleNamespace

    run = SimpleNamespace(cache={}, stamp=SimpleNamespace(change_set_id="change"), at=T0)
    beliefs._stage_change(
        run,
        "belief",
        "established",
        "weakening",
        [
            {
                "id": "e",
                "episode_id": "episode",
                "source_time": T0,
                "source_type": "confirmed",
                "derivation": "inferred",
            }
        ],
        "reason",
        cause="evidence_transition",
    )
    [event] = beliefs.cascade_changes(run)
    assert event["source_type"] == "said"
    assert event["original_source_type"] == "confirmed"
    assert event["person_correction"] is False
    assert event["terminal"] is False
    assert event["evidence_ids"] == ["e"]
    event["source_type"] = "confirmed"
    assert beliefs.cascade_changes(run)[0]["source_type"] == "said"


def test_retest_refuses_another_declared_source(driver, seeded):
    w = seeded["world_id"]
    belief, ev, operation, _, _ = retest_case(driver, w)
    set_belief_fields(
        driver,
        w,
        belief,
        retest_how=json.dumps({"connector": "other", "locator": "https://example.invalid/other"}),
    )
    with pytest.raises(BatchDeclined) as error:
        apply(driver, w, [operation], [ev], caller=CONNECTOR)
    assert error.value.refusals[0].rule == "belief_retest_method"
    assert state(driver, w, belief)["state"] == "weakening"


def test_third_party_statement_cannot_be_a_person_correction(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, ev = data(driver, w, 6, connector="person_in_views")
    ev["speaker_kind"] = "third_party"
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e6"))],
            [ev],
        )
    assert error.value.refusals[0].rule == "verbatim_required"
    assert state(driver, w, belief)["state"] == "tentative"


def test_right_after_restatement_ends_the_old_successor_link(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, ev = data(driver, w, 6, connector="person_in_views")
    apply(
        driver,
        w,
        [
            op(
                "correct_belief",
                target=belief,
                correction="restate",
                title="River supplies calm rooms",
                evidence_refs=supports("e6"),
            )
        ],
        [ev],
    )
    _, ev = data(driver, w, 7, connector="person_in_views")
    apply(
        driver,
        w,
        [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e7"))],
        [ev],
    )
    from grove.store.primitives import MEANING_SUPERSEDED_BY

    assert not read(
        driver,
        w,
        "MATCH (:Item)-[l:LINK {from_item:$id, meaning:$meaning}]->(:Item) "
        "WHERE l.active_key IS NOT NULL RETURN l",
        id=belief,
        meaning=MEANING_SUPERSEDED_BY,
    )
    assert state(driver, w, belief).get("successor") is None
    assert state(driver, w, belief)["state"] == "established"


def test_negative_retest_weakens_due_belief_and_preserves_confirmation(driver, seeded):
    w = seeded["world_id"]
    belief, ev, operation, ids, _ = retest_case(driver, w, weakening=False)
    set_judgment_verdict(driver, w, ids, "contradicts")
    set_confirmation(driver, w, belief, T0)
    apply(driver, w, [operation], [ev], caller=CONNECTOR)
    assert state(driver, w, belief)["state"] == "weakening"
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item["last_confirmed"] == T0
    assert item["recheck_by"].isoformat() == operation["recheck_by"]
    assert not read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)
    [contrary] = read(
        driver,
        w,
        "MATCH (e:Evidence {source_id:$id, target_id:$target}) RETURN e",
        id=ev["source_id"],
        target=belief,
    )
    assert contrary["e"]["polarity"] == "contradicts"
    assert set(contrary["e"]["judgment_ids"]) == set(ids)


def test_observed_evidence_cannot_retype_a_describes_anchor(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    from grove.store.primitives import MEANING_DESCRIBES

    [row] = read(
        driver,
        w,
        "MATCH (:Item)-[l:LINK {from_item:$id,meaning:$meaning}]->(:Item) RETURN l",
        id=belief,
        meaning=MEANING_DESCRIBES,
    )
    link = row["l"]["id"]
    _, ev = data(driver, w, 7)
    attach(driver, w, link, ev)
    [current] = read(driver, w, "MATCH (:Item)-[l:LINK {id:$id}]->(:Item) RETURN l", id=link)
    assert current["l"]["derivation"] == "inferred"


@pytest.mark.parametrize("verdict", ["complements", "not_about_claim"])
def test_neutral_retest_records_check_without_evidence_or_confirmation(driver, seeded, verdict):
    w = seeded["world_id"]
    belief, ev, operation, ids, _ = retest_case(driver, w)
    set_judgment_verdict(driver, w, ids, verdict)
    receipt = apply(driver, w, [operation], [ev], caller=CONNECTOR)
    saved = state(driver, w, belief)
    assert saved["state"] == "weakening"
    assert saved["last_retest_outcome"] == verdict
    assert set(saved["last_retest_judgment_ids"]) == set(ids)
    assert saved["last_retest_source_ids"] == [ev["source_id"]]
    assert not read(driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=ev["source_id"])
    assert not read(driver, w, "MATCH (c:Contradiction {target:$id}) RETURN c", id=belief)
    item = read(driver, w, "MATCH (i:Item {id:$id}) RETURN i", id=belief)[0]["i"]
    assert item.get("last_confirmed") is None
    assert item["recheck_by"].isoformat() == operation["recheck_by"]
    assert any(line.field == "last_retest_outcome" for line in receipt.lines)


def test_confirmed_source_without_capability_cannot_override_person(driver, seeded):
    from grove.store.primitives import KIND_BELIEF

    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, personal = data(driver, w, 5, connector="person_in_views")
    apply(
        driver,
        w,
        [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e5"))],
        [personal],
    )
    plant_node(
        driver,
        w,
        "Capability",
        connector_type="git",
        registry_id=KIND_BELIEF,
        capability="none",
        origin="the_person",
    )
    source = make_source(driver, w, "git", ["commit abcdef123 records a different service"])
    fresh = observation(
        source,
        "git",
        1,
        locator="key:abcdef123",
        statement_class="report_happened",
        at=T0 + timedelta(hours=6),
    )
    attach(driver, w, belief, fresh, "contradicts", judgment_ids=judges(driver, w, belief, fresh))
    saved = state(driver, w, belief)
    assert saved["state"] == "established"
    assert saved["held_by_the_person"] == T0 + timedelta(hours=5)
    assert saved.get("person_change_notice") is not True
    [evidence] = read(
        driver, w, "MATCH (e:Evidence {source_id:$id}) RETURN e", id=source["source_id"]
    )
    assert evidence["e"]["source_type"] == "confirmed"
    # A newer independently capable source can still change the person's decision.
    _, capable = data(driver, w, 7, connector="web_pages")
    attach(
        driver, w, belief, capable, "contradicts", judgment_ids=judges(driver, w, belief, capable)
    )
    assert state(driver, w, belief)["state"] == "weakening"
    assert state(driver, w, belief)["person_change_notice"] is True
