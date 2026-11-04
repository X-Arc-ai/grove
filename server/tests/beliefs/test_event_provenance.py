"""Facts emitted by D9's inert D6 seam, without a cascade implementation."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from grove.writer import apply as writer_apply
from grove.writer import beliefs
from tests.beliefs.test_state_machine import CONNECTOR, data, establish, fixture, judges, op
from tests.writer.helpers import LEARNER, T0, apply, create_item, created_id, read, supports


@pytest.fixture
def staged(monkeypatch):
    events = []
    finish = writer_apply._finish

    def capture(run, operations):
        finish(run, operations)
        if not run.refused:
            events.extend(beliefs.cascade_changes(run))

    monkeypatch.setattr(writer_apply, "_finish", capture)
    return events


def event_run():
    return SimpleNamespace(
        cache={}, at=T0 + timedelta(days=10), stamp=SimpleNamespace(change_set_id="change")
    )


def event_row(identifier, episode, at, *, source_type="confirmed", derivation="observed"):
    return {
        "id": identifier,
        "episode_id": episode,
        "source_time": at,
        "source_type": source_type,
        "derivation": derivation,
    }


@pytest.mark.parametrize("previous_state", ["tentative", "weakening"])
def test_transition_event_uses_only_newly_consumed_episodes(monkeypatch, previous_state):
    run = event_run()
    belief = {"item_id": "belief", "state": previous_state, "transition_episodes": ["old"]}
    old = event_row("old-evidence", "old", T0)
    new = event_row("new-evidence", "new", T0 + timedelta(hours=1), source_type="said")

    def update(_run, target, fields, _reason, **_metadata):
        target.update(fields)

    monkeypatch.setattr(beliefs, "set_fields", update)
    beliefs._transition(run, belief, "established", [old, new], None)
    [event] = beliefs.cascade_changes(run)
    assert event["evidence_ids"] == ["new-evidence"]
    assert event["episode_ids"] == ["new"]
    assert event["original_source_type"] == "said"
    assert event["world_time"] == new["source_time"]
    assert belief["transition_episodes"] == ["new", "old"]


@pytest.mark.parametrize("premise_derivation", ["observed", "inferred", "rolled_up"])
def test_automatic_regularity_never_inherits_confirmed_premise_authority(premise_derivation):
    run = event_run()
    row = event_row("evidence", "episode", T0, derivation=premise_derivation)
    beliefs._stage_change(
        run, "belief", "tentative", "established", [row], None, cause="evidence_transition"
    )
    [event] = beliefs.cascade_changes(run)
    assert event["source_type"] == "said"
    assert event["derivation"] == "inferred"
    assert event["original_source_type"] == "confirmed"
    assert event["original_derivation"] == premise_derivation
    assert event["person_correction"] is False


def test_inferred_event_does_not_upgrade_planned_premise_authority():
    run = event_run()
    row = event_row("evidence", "episode", T0, source_type="planned", derivation="inferred")
    beliefs._stage_change(
        run, "belief", "established", "superseded", [row], None, cause="belief_end"
    )
    [event] = beliefs.cascade_changes(run)
    assert event["source_type"] == "planned"
    assert event["derivation"] == "inferred"


def test_trusted_person_event_keeps_authority_and_null_optional_fields():
    run = event_run()
    row = event_row("evidence", "episode", T0)
    beliefs._stage_change(
        run,
        "belief",
        "superseded",
        "established",
        [row],
        None,
        cause="person_correction",
        person_correction=True,
    )
    [event] = beliefs.cascade_changes(run)
    assert event["source_type"] == "confirmed"
    assert event["derivation"] == "observed"
    assert event["person_correction"] is True
    assert event["reopened"] is True
    assert event["reason_id"] is None
    assert event["end_clock"] is None
    assert event["world_time"] == T0


def test_no_effective_state_change_emits_no_event():
    run = event_run()
    beliefs._stage_change(
        run,
        "belief",
        "established",
        "established",
        [],
        None,
        cause="person_correction",
        person_correction=True,
    )
    assert beliefs.cascade_changes(run) == ()


def test_actual_promotion_cites_new_report_not_old_confirmed_support(driver, seeded, staged):
    w = seeded["world_id"]
    _, first = data(driver, w, 1, connector="web_pages")
    _, second = data(driver, w, 2, connector="web_pages")
    subject = created_id(apply(driver, w, [create_item("organisation", "River", "e1")], [first]))
    receipt = apply(
        driver,
        w,
        [
            op(
                "mint_belief",
                title="River supplies quiet rooms",
                subject_refs=[subject],
                evidence_refs=supports("e1", "e2"),
            )
        ],
        [first, second],
        caller=LEARNER,
    )
    belief = created_id(receipt, entity_type="belief")
    _, new = data(driver, w, 3)
    apply(
        driver,
        w,
        [op("promote_belief", target=belief, evidence_refs=supports("e3"))],
        [new],
        caller=CONNECTOR,
    )
    [event] = staged
    [evidence] = read(
        driver,
        w,
        "MATCH (e:Evidence {target_id:$target,source_id:$source}) RETURN e",
        target=belief,
        source=new["source_id"],
    )
    assert event["evidence_ids"] == [evidence["e"]["id"]]
    assert event["episode_ids"] == [new["episode_id"]]
    assert event["world_time"] == T0 + timedelta(hours=3)
    assert event["original_source_type"] == evidence["e"]["source_type"] == "reported_done"
    assert event["derivation"] == "inferred"


@pytest.mark.parametrize(
    "end_clock,expected_time",
    [
        ({"precision": "instant", "earliest": T0.isoformat(), "latest": T0.isoformat()}, T0),
        (
            {
                "precision": "day",
                "earliest": T0.isoformat(),
                "latest": (T0 + timedelta(days=1)).isoformat(),
            },
            T0,
        ),
        ("unknown", None),
    ],
)
def test_actual_historical_end_uses_its_full_clock_not_the_later_source(
    driver, seeded, staged, end_clock, expected_time
):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, personal = data(driver, w, 6, connector="person_in_views")
    apply(
        driver,
        w,
        [
            op(
                "correct_belief",
                target=belief,
                correction="not_any_more",
                stopped_being_true=end_clock,
                evidence_refs=supports("e6"),
            )
        ],
        [personal],
    )
    [event] = staged
    assert event["world_time"] == expected_time
    assert event["end_clock"] == end_clock
    assert event["person_correction"] is True
    assert event["terminal"] is True


def test_actual_attachment_without_reason_emits_a_valid_optional_null(driver, seeded, staged):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    staged.clear()
    _, contrary = data(driver, w, 4)
    operation = {
        "op": "attach_evidence",
        "op_id": "attach",
        "target": belief,
        "ev_id": "e4",
        "polarity": "contradicts",
        "judgment_ids": judges(driver, w, belief, contrary),
    }
    apply(driver, w, [operation], [contrary], caller=CONNECTOR)
    [event] = staged
    assert event["reason_id"] is None
    assert event["end_clock"] is None
    assert event["world_time"] == T0 + timedelta(hours=4)
    assert event["after_state"] == "weakening"


def test_reused_personal_source_event_cites_existing_evidence(driver, seeded, staged):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, personal = data(driver, w, 6, connector="person_in_views")
    for correction in ("not_any_more", "right"):
        apply(
            driver,
            w,
            [
                op(
                    "correct_belief",
                    target=belief,
                    correction=correction,
                    evidence_refs=supports("e6"),
                )
            ],
            [personal],
        )
    evidence = read(
        driver,
        w,
        "MATCH (e:Evidence {target_id:$target,source_id:$source}) RETURN e.id AS id",
        target=belief,
        source=personal["source_id"],
    )
    assert len(evidence) == 1
    assert len(staged) == 2
    for event in staged:
        assert event["evidence_ids"] == [evidence[0]["id"]]


def test_personal_retirement_event_cites_reused_evidence(driver, seeded, staged):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, personal = data(driver, w, 6, connector="person_in_views")
    apply(
        driver,
        w,
        [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e6"))],
        [personal],
    )
    apply(
        driver,
        w,
        [
            op(
                "retire_belief",
                target=belief,
                end_class="person_retired",
                evidence_refs=supports("e6"),
            )
        ],
        [personal],
    )
    [evidence] = read(
        driver,
        w,
        "MATCH (e:Evidence {target_id:$target,source_id:$source}) RETURN e.id AS id",
        target=belief,
        source=personal["source_id"],
    )
    assert len(staged) == 2
    assert staged[-1]["evidence_ids"] == [evidence["id"]]
    assert staged[-1]["person_correction"] is True
