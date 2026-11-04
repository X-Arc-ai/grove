"""Beliefs earn state from distinct evidence and retain the person's authority."""

from datetime import timedelta

import pytest

from grove.store.primitives import KIND_BELIEF
from grove.writer import BatchDeclined, Caller
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    HOST,
    LEARNER,
    T0,
    apply,
    create_item,
    created_id,
    make_source,
    observation,
    read,
    supports,
)

CONNECTOR = Caller("consolidation", {"grove.propose", "grove.read"}, "agent:connector")


def op(name, **fields):
    return {
        "op": name,
        "op_id": name,
        "reason": "the retained sources justify this action",
        **fields,
    }


def data(driver, w, n, *, source=None, at=None, connector="whatsapp", statement="report_state"):
    source = source or make_source(
        driver, w, connector, [f"River supplies quiet rooms, observation {n}"]
    )
    locator = "whole" if connector in ("person_in_views", "ai_host", "web_pages") else "line:1"
    return source, observation(
        source,
        f"e{n}",
        1,
        locator=locator,
        at=at or T0 + timedelta(hours=n),
        message_key=f"message-{n}",
        statement_class=statement,
    )


def fixture(driver, w, count=2, **mint_fields):
    sources, rows = zip(*(data(driver, w, n) for n in range(1, count + 1)), strict=True)
    subject = created_id(apply(driver, w, [create_item("organisation", "River", "e1")], [rows[0]]))
    mint = op(
        "mint_belief",
        title="River supplies quiet rooms",
        subject_refs=[subject],
        evidence_refs=supports(*(row["ev_id"] for row in rows)),
        **mint_fields,
    )
    receipt = apply(driver, w, [mint], list(rows), caller=LEARNER)
    belief = created_id(receipt, entity_type="belief")
    return belief, subject, list(sources), list(rows), receipt


def state(driver, w, belief):
    return read(driver, w, "MATCH (b:Belief {item_id: $id}) RETURN b", id=belief)[0]["b"]


def attach(driver, w, belief, ev, polarity="supports", **extra):
    return apply(
        driver,
        w,
        [op("attach_evidence", target=belief, polarity=polarity, ev_id=ev["ev_id"], **extra)],
        [ev],
        caller=CONNECTOR,
    )


def judges(driver, w, belief, ev, verdict="contradicts", correlated=False):
    result = []
    for n, role in enumerate(("first_judge", "second_judge")):
        call = plant_node(
            driver, w, "ModelCall", model="spark" if correlated else f"judge-{n}", outcome="ok"
        )
        row = plant_node(
            driver,
            w,
            "Judgment",
            model_call_id=call["id"],
            role=role,
            subject=f"belief:{belief}",
            verdict=verdict,
            source_id=ev["source_id"],
            locator=ev["locator"],
            correlated=correlated and n == 1,
            at=T0 + timedelta(days=1),
        )
        result.append(row["id"])
    return result


def establish(driver, w):
    belief, subject, sources, rows, _ = fixture(driver, w)
    _, third = data(driver, w, 3)
    apply(
        driver,
        w,
        [op("promote_belief", target=belief, evidence_refs=supports("e3"))],
        [third],
        caller=CONNECTOR,
    )
    return belief, subject, sources, rows


def test_earning_counts_episodes_not_rows(driver, seeded):
    w = seeded["world_id"]
    source, first = data(driver, w, 1)
    subject = created_id(apply(driver, w, [create_item("organisation", "River", "e1")], [first]))
    repeated = [observation(source, f"e{n}", 1, message_key=f"repeat-{n}", at=T0) for n in range(3)]
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [
                op(
                    "mint_belief",
                    title="River supplies quiet rooms",
                    subject_refs=[subject],
                    evidence_refs=supports("e0", "e1", "e2"),
                )
            ],
            repeated,
            caller=LEARNER,
        )
    assert any(row.rule == "belief_earning" for row in error.value.refusals)
    assert not read(driver, w, "MATCH (b:Belief) RETURN b")


def test_two_episodes_mint_and_third_promotes_with_a_receipt(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, receipt = fixture(driver, w)
    assert state(driver, w, belief)["state"] == "tentative"
    assert len(state(driver, w, belief)["supporting_episodes"]) == 2
    assert receipt.state == "saved"
    _, third = data(driver, w, 3)
    receipt = apply(
        driver,
        w,
        [op("promote_belief", target=belief, evidence_refs=supports("e3"))],
        [third],
        caller=CONNECTOR,
    )
    assert state(driver, w, belief)["state"] == "established"
    assert any(line.entity_type == "belief" and line.field == "state" for line in receipt.lines)
    item = read(driver, w, "MATCH (i:Item {id: $id}) RETURN i", id=belief)[0]["i"]
    assert item["derivation"] == "inferred"
    assert item.get("last_confirmed") is None


def test_new_contradiction_weakens_and_new_source_restores_atomically(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    _, contrary = data(driver, w, 4)
    receipt = attach(
        driver, w, belief, contrary, "contradicts", judgment_ids=judges(driver, w, belief, contrary)
    )
    assert state(driver, w, belief)["state"] == "weakening"
    assert len(state(driver, w, belief)["contradicting_episodes"]) == 1
    changes = read(
        driver, w, "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN e", id=receipt.change_set_id
    )
    assert {row["e"]["entity_type"] for row in changes} >= {"belief", "evidence"}
    _, support = data(driver, w, 5)
    attach(driver, w, belief, support)
    assert state(driver, w, belief)["state"] == "established"


def test_replaying_an_episode_cannot_flap(driver, seeded):
    w = seeded["world_id"]
    belief, _, sources, rows = establish(driver, w)
    # A new message key from an episode used at promotion remains the same episode.
    same = observation(
        sources[0], "repeat", 1, at=T0 + timedelta(hours=8), message_key="another-key"
    )
    attach(driver, w, belief, same, "contradicts", judgment_ids=judges(driver, w, belief, same))
    assert state(driver, w, belief)["state"] == "established"
    before = state(driver, w, belief)
    attach(driver, w, belief, same, "contradicts", judgment_ids=judges(driver, w, belief, same))
    assert state(driver, w, belief) == before


def test_same_source_support_does_not_restore(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    source, contrary = data(driver, w, 4)
    attach(
        driver, w, belief, contrary, "contradicts", judgment_ids=judges(driver, w, belief, contrary)
    )
    _, same = data(driver, w, 5, source=source)
    attach(driver, w, belief, same)
    assert state(driver, w, belief)["state"] == "weakening"


def test_person_generalisation_mints_one_episode(driver, seeded):
    w = seeded["world_id"]
    _, row = data(driver, w, 1, connector="person_in_views")
    subject = created_id(apply(driver, w, [create_item("organisation", "River", "e1")], [row]))
    receipt = apply(
        driver,
        w,
        [
            op(
                "mint_belief",
                title="River supplies quiet rooms",
                subject_refs=[subject],
                evidence_refs=supports("e1"),
                generalisation=True,
            )
        ],
        [row],
    )
    assert state(driver, w, created_id(receipt, entity_type="belief"))["state"] == "tentative"


@pytest.mark.parametrize(
    "correction,expected",
    [
        ("right", "established"),
        ("not_any_more", "superseded"),
        ("wrong", "superseded"),
        ("restate", "superseded"),
    ],
)
def test_person_four_corrections_are_synchronous(driver, seeded, correction, expected):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, row = data(driver, w, 5, connector="person_in_views")
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
        [row],
    )
    saved = state(driver, w, belief)
    assert saved["state"] == expected
    assert saved["held_by_the_person"] == T0 + timedelta(hours=5)
    assert receipt.state == "saved"
    if correction == "restate":
        assert state(driver, w, saved["successor"])["origin"] == "persons_correction"


def test_host_without_verbatim_is_refused(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, row = data(driver, w, 5, connector="ai_host")
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e5"))],
            [row],
            caller=HOST,
        )
    assert error.value.refusals[0].rule == "verbatim_required"
    assert state(driver, w, belief)["state"] == "tentative"


def test_person_word_outweighs_new_said_but_new_confirmed_changes_it(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, person = data(driver, w, 5, connector="person_in_views")
    apply(
        driver,
        w,
        [op("correct_belief", target=belief, correction="right", evidence_refs=supports("e5"))],
        [person],
    )
    _, said = data(driver, w, 6)
    attach(driver, w, belief, said, "contradicts", judgment_ids=judges(driver, w, belief, said))
    assert state(driver, w, belief)["state"] == "established"
    # A real retained artifact is confirmed by the source-type mapping.
    source = make_source(driver, w, "git", ["commit abcdef123: River stopped the quiet service"])
    artifact = observation(
        source,
        "git",
        1,
        locator="key:abcdef123",
        statement_class="report_happened",
        at=T0 + timedelta(hours=7),
        message_key="git-commit",
    )
    attach(
        driver, w, belief, artifact, "contradicts", judgment_ids=judges(driver, w, belief, artifact)
    )
    assert state(driver, w, belief)["state"] == "weakening"
    assert state(driver, w, belief)["person_change_notice"] is True


def test_judgments_bind_exact_source_and_accept_correlated_fallback(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _ = establish(driver, w)
    _, row = data(driver, w, 4)
    _, other = data(driver, w, 5)
    with pytest.raises(BatchDeclined) as error:
        attach(driver, w, belief, row, "contradicts", judgment_ids=judges(driver, w, belief, other))
    assert error.value.refusals[0].rule == "judgment_evidence"
    ids = judges(driver, w, belief, row, correlated=True)
    attach(driver, w, belief, row, "contradicts", judgment_ids=ids)
    assert state(driver, w, belief)["state"] == "weakening"
    stored = read(
        driver,
        w,
        "MATCH (e:Evidence {target_id: $id, polarity: 'contradicts'}) RETURN e",
        id=belief,
    )
    assert stored[0]["e"]["judgment_ids"] == sorted(ids)


def test_duplicate_statement_and_generic_mint_bypass_refuse(driver, seeded):
    w = seeded["world_id"]
    belief, subject, _, rows, _ = fixture(driver, w)
    with pytest.raises(BatchDeclined) as error:
        apply(
            driver,
            w,
            [
                op(
                    "mint_belief",
                    title="RIVER supplies quiet rooms",
                    subject_refs=[subject],
                    evidence_refs=supports("e1", "e2"),
                )
            ],
            rows,
            caller=LEARNER,
        )
    assert error.value.refusals[0].rule == "belief_duplicate"
    with pytest.raises(BatchDeclined) as error:
        apply(driver, w, [create_item(KIND_BELIEF, "An unearned belief", "e1")], [rows[0]])
    assert error.value.refusals[0].rule in ("belief_transition", "primitive_kind")


def test_queue_stores_both_sides_and_question_values(driver, seeded):
    w = seeded["world_id"]
    belief, _, _, _, _ = fixture(driver, w)
    _, fresh = data(driver, w, 4)
    apply(
        driver,
        w,
        [
            op("open_contradiction", target=belief, ev_id="e4", temp_id="$conflict"),
            op(
                "resolve_contradiction",
                contradiction="$conflict",
                outcome="to_question",
                operation_ids=[],
                question="Which service applies?",
                options=["Quiet rooms", "Loud rooms"],
                checked_source_ids=[fresh["source_id"]],
            ),
        ],
        [fresh],
        caller=CONNECTOR,
    )
    conflict = read(driver, w, "MATCH (c:Contradiction) RETURN c")[0]["c"]
    assert conflict["status"] == "with_the_person"
    assert conflict["standing_target_id"] == belief
    assert conflict["fresh_source_id"] == fresh["source_id"]
    question = read(driver, w, "MATCH (q:Question {id: $id}) RETURN q", id=conflict["question_id"])[
        0
    ]["q"]
    assert "Quiet rooms" not in question["answer_options"]
    assert question["legwork_sources"] == [fresh["source_id"]]


def test_subject_change_queues_belief_in_same_transaction(driver, seeded):
    w = seeded["world_id"]
    belief, subject, _, _, _ = fixture(driver, w)
    _, fresh = data(driver, w, 4)
    receipt = apply(
        driver,
        w,
        [
            op(
                "update_item",
                item=subject,
                version=1,
                fields={"summary": "River moved the service"},
                evidence_refs=supports("e4"),
            )
        ],
        [fresh],
    )
    queued = read(driver, w, "MATCH (s:SupportChanged {belief_id: $id}) RETURN s", id=belief)[0][
        "s"
    ]
    cause = read(
        driver,
        w,
        "MATCH (e:ChangeEntry {sequence: $sequence}) RETURN e",
        sequence=queued["cause_change_id"],
    )[0]["e"]
    assert cause["change_set_id"] == receipt.change_set_id
    assert queued.get("taken_at") is None
