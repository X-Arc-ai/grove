from copy import deepcopy

import pytest

from grove.cleanup.access import CleanupRefused, hold
from grove.cleanup.ambiguity import narrow
from grove.cleanup.contracts import escalation, reject_contamination, to_batch
from grove.cleanup.questions import Question


def document(disposition="deferred"):
    return {
        "schema": "grove.cleanup.v1",
        "run": {"run_id": "r1", "work": "lane_batch", "area_id": None},
        "operations": [],
        "evidence": [],
        "dispositions": [
            {
                "op": "verify",
                "op_id": "v1",
                "claim": "claim1",
                "pieces": [],
                "disposition": disposition,
                "reason": "No confirming source was available.",
                "recheck_by": "2027-03-15T09:00:00+00:00",
            }
        ],
    }


def test_deferral_translates_without_a_fake_confirmation():
    data = document()
    data["dispositions"][0]["pieces"] = [{"evidence_id": "evidence1", "judgment_id": "judgment1"}]
    batch = to_batch(data, run_id="r1", claims=frozenset({"claim1"}))
    assert batch["ops"][0]["disposition"] == "deferred"
    batch["ops"][0]["reason"] = "Changed copy"
    assert data["dispositions"][0]["reason"] != "Changed copy"


@pytest.mark.parametrize("field", ["score", "verdict", "band", "load_bearing", "source_type"])
def test_agent_cannot_supply_judge_or_writer_authority(field):
    data = document()
    data["dispositions"][0][field] = 1
    with pytest.raises(CleanupRefused):
        to_batch(data, run_id="r1", claims=frozenset({"claim1"}))


@pytest.mark.parametrize("disposition", ["confirmed", "moved"])
def test_confirmation_and_move_need_evidence(disposition):
    with pytest.raises(CleanupRefused, match="evidence_floor"):
        to_batch(document(disposition), run_id="r1", claims=frozenset({"claim1"}))


def test_run_scope_duplicate_claims_and_area_work_refuse():
    data = document()
    with pytest.raises(CleanupRefused):
        to_batch(data, run_id="r2", claims=frozenset({"claim1"}))
    with pytest.raises(CleanupRefused):
        to_batch(data, run_id="r1", claims=frozenset({"other"}))
    data["dispositions"].append(deepcopy(data["dispositions"][0]))
    with pytest.raises(CleanupRefused):
        to_batch(data, run_id="r1", claims=frozenset({"claim1"}))
    data = document()
    data["run"]["work"] = "area_audit"
    with pytest.raises(CleanupRefused):
        to_batch(data, run_id="r1", claims=frozenset({"claim1"}))


def test_repeated_item_boilerplate_refuses_before_a_write():
    operations = [
        {
            "op": "update_item",
            "item": f"i{n}",
            "fields": {"summary": "The entire project remains valid and fully confirmed."},
        }
        for n in range(3)
    ]
    with pytest.raises(CleanupRefused, match="repeated_prose"):
        reject_contamination(operations)
    operations[-1]["fields"]["summary"] = "A different source-specific fact."
    reject_contamination(operations)


def test_ambiguity_uses_intersected_context_and_never_candidate_order():
    assert narrow(("b", "a")).resolved_to is None
    resolved = narrow(("b", "a"), participants=frozenset({"a", "b"}), valid_at=frozenset({"a"}))
    assert resolved.resolved_to == "a" and resolved.context == ("participants", "time")
    contradiction = narrow(("a", "b"), participants=frozenset({"a"}), co_mentions=frozenset({"b"}))
    assert contradiction.resolved_to is None and contradiction.candidates == ("a", "b")


def test_third_deferral_escalates_and_confirmation_resets_the_streak():
    assert escalation(("deferred",) * 3, person_only=True) == "question"
    assert escalation(("deferred",) * 3, person_only=False) == "retirement_proposal"
    assert escalation(("deferred", "confirmed", "deferred"), person_only=True) is None


def test_no_question_without_legwork_and_no_boolean_authority():
    with pytest.raises(CleanupRefused, match="legwork"):
        Question(("a",), "Choose a venue.", "decision", (), "Only a decision remains.")
    for absent in (None, True, False):
        with pytest.raises(CleanupRefused, match="authority_unavailable"):
            hold(absent, "00000000-0000-7000-8000-000000000001", "r1", "fetch")
