"""Small synthetic drills using the real local writer and controlled page/model replies."""

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from grove.cleanup.access import CleanupRefused
from grove.cleanup.ambiguity import narrow
from grove.cleanup.questions import Question
from grove.cleanup.verification import (
    apply_dispositions,
    fetch_and_retain,
    judge_fetches,
    retained_piece,
)
from grove.ids import new_id
from grove.screening import Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined, apply_request, enqueue_request
from grove.writer.cleanup import close_question, raise_question, retain_ambiguity
from tests.models.test_judge import NOW, Provider, ferry_claim, glimmer, spark, verdicts
from tests.writer.helpers import CURATOR, LEARNER, PERSON, make_source, observation, stale_update

SCREENER = Screener(ScreeningConfig())


def page(driver, w, access):
    return fetch_and_retain(
        driver,
        w,
        "run1",
        "https://public.example/status",
        hosts=frozenset({"public.example"}),
        screener=SCREENER,
        access=access,
        resolver=lambda *_: ["8.8.8.8"],
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/plain"}, text="The ferry is open."
            )
        ),
    )


def proposal(claim, disposition, pieces=(), operations=(), evidence=()):
    return {
        "schema": "grove.cleanup.v1",
        "run": {"run_id": "run1", "work": "lane_batch", "area_id": None},
        "operations": list(operations),
        "evidence": list(evidence),
        "dispositions": [
            {
                "op": "verify",
                "op_id": "v1",
                "claim": claim,
                "pieces": list(pieces),
                "disposition": disposition,
                "reason": "The retained source supports this disposition.",
                "recheck_by": (NOW + timedelta(days=1)).isoformat(),
            }
        ],
    }


def test_source_and_episode_exist_before_isolated_judges(driver, w, access, make_layer):
    claim = ferry_claim(driver, w)
    retained = page(driver, w, access)
    provider = Provider(
        spark(verdicts((2, "supports", 0.9))), glimmer(verdicts((2, "supports", 0.8)))
    )
    query = WorldQuery(driver, w)
    # This read occurs before either model request.
    assert retained_piece(query, retained).source_id == retained.source_id
    first, second = judge_fetches(
        make_layer(provider), driver, w, "run1", claim, (retained,), borderline=True, access=access
    )
    assert first.model_call_id != second.model_call_id
    assert second.job == "second_opinion" and second.correlated is False
    bodies = provider.bodies()
    assert "The ferry is open." in json.dumps(bodies[0])
    assert first.judgment_ids[0] not in json.dumps(bodies[1])
    assert query.read("MATCH (j:Judgment) RETURN count(j) AS n")[0]["n"] == 2
    with pytest.raises(CleanupRefused):
        retained_piece(query, replace(retained, observed_at=NOW + timedelta(seconds=1)))


@pytest.mark.parametrize("disposition", ["confirmed", "deferred", "moved", "retired"])
def test_four_dispositions_use_the_real_d4_writer(driver, w, access, make_layer, disposition):
    claim = ferry_claim(driver, w)
    retained = page(driver, w, access)
    verdict = {"moved": "contradicts", "deferred": "not_about_claim"}.get(disposition, "supports")
    first, _ = judge_fetches(
        make_layer(Provider(spark(verdicts((2, verdict, 0.9))))),
        driver,
        w,
        "run1",
        claim,
        (retained,),
        access=access,
    )
    observation = retained.observation("page1")
    pieces = [{"ev_id": "page1", "judgment_id": first.judgment_ids[0]}]
    operations = []
    if disposition == "moved":
        operations = [
            {
                "op": "change_status",
                "op_id": "change1",
                "item": claim,
                "status": "blocked",
                "recheck_by": (NOW + timedelta(days=1)).isoformat(),
                "evidence_refs": [{"ev_id": "page1", "polarity": "supports"}],
                "reason": "The route is blocked.",
            }
        ]
    if disposition == "retired":
        operations = [
            {
                "op": "end_item",
                "op_id": "end1",
                "item": claim,
                "end_class": "retired",
                "stopped_being_true": "unknown",
                "evidence_refs": [{"ev_id": "page1", "polarity": "supports"}],
                "reason": "The route no longer matters.",
            }
        ]
    document = proposal(claim, disposition, pieces, operations, [observation])
    request_id = new_id(w)
    receipt = apply_dispositions(
        driver, w, "run1", document, claims={claim}, request_id=request_id, access=access
    )
    assert receipt.commit_id
    repeated = apply_dispositions(
        driver, w, "run1", document, claims={claim}, request_id=request_id, access=access
    )
    assert repeated.commit_id == receipt.commit_id
    query = WorldQuery(driver, w)
    checks = query.read("MATCH (v:Verification {claim_id: $id}) RETURN v", id=claim)
    assert checks[-1]["v"]["disposition"] == disposition
    assert len(checks) == 1
    current = query.read("MATCH (i:Item {id: $id}) RETURN i", id=claim)[0]["i"]
    assert (current.get("last_confirmed") is not None) is (disposition == "confirmed")


def test_invented_judgment_and_missing_authority_do_not_apply(driver, w, access):
    claim = ferry_claim(driver, w)
    retained = page(driver, w, access)
    document = proposal(
        claim,
        "confirmed",
        [{"ev_id": "page1", "judgment_id": "invented"}],
        evidence=[retained.observation("page1")],
    )
    with pytest.raises(CleanupRefused, match="authority"):
        apply_dispositions(driver, w, "run1", document, claims={claim}, request_id=new_id(w))
    with pytest.raises(BatchDeclined):
        apply_dispositions(
            driver, w, "run1", document, claims={claim}, request_id=new_id(w), access=access
        )
    assert WorldQuery(driver, w).read("MATCH (v:Verification) RETURN count(v) AS n")[0]["n"] == 0


def test_legwork_question_deduplicates_and_waits_for_applied_verbatim_answer(
    driver, w, access, make_layer
):
    claim = ferry_claim(driver, w)
    retained = page(driver, w, access)
    first, _ = judge_fetches(
        make_layer(Provider(spark(verdicts((2, "not_about_claim", 0.9))))),
        driver,
        w,
        "run1",
        claim,
        (retained,),
        access=access,
    )
    question = Question(
        (claim,),
        "Which departure do you prefer?",
        "decision",
        ((retained.source_id, first.judgment_ids[0]),),
        "The timetable cannot choose a preference.",
        ("confirm", "deny"),
    )
    question_id = raise_question(
        driver, w, "run1", CURATOR, question, screener=SCREENER, access=access
    )
    assert (
        raise_question(driver, w, "run1", CURATOR, question, screener=SCREENER, access=access)
        == question_id
    )
    with pytest.raises(CleanupRefused, match="not_applied"):
        close_question(
            driver, w, "run1", PERSON, question_id, answer_request_id="missing", access=access
        )
    query = WorldQuery(driver, w)
    assert query.read("MATCH (q:Question) RETURN count(q) AS n")[0]["n"] == 1
    close_question(
        driver,
        w,
        "run1",
        CURATOR,
        question_id,
        withdraw_reason="The decision is no longer needed.",
        screener=SCREENER,
        access=access,
    )
    assert (
        query.read("MATCH (q:Question {id: $id}) RETURN q.state AS state", id=question_id)[0][
            "state"
        ]
        == "withdrawn"
    )


def test_a_source_that_settles_the_claim_cannot_be_question_legwork(driver, w, access, make_layer):
    claim = ferry_claim(driver, w)
    retained = page(driver, w, access)
    first, _ = judge_fetches(
        make_layer(Provider(spark(verdicts((2, "supports", 0.9))))),
        driver,
        w,
        "run1",
        claim,
        (retained,),
        access=access,
    )
    question = Question(
        (claim,),
        "Is the route open?",
        "fact_only_the_person_holds",
        ((retained.source_id, first.judgment_ids[0]),),
        "A source was checked.",
    )
    with pytest.raises(CleanupRefused, match="legwork_not_settled"):
        raise_question(driver, w, "run1", CURATOR, question, screener=SCREENER, access=access)
    assert WorldQuery(driver, w).read("MATCH (q:Question) RETURN count(q) AS n")[0]["n"] == 0


def test_unresolved_context_is_durable_and_waiting_links_do_not_apply(driver, w, access):
    first, second = (
        ferry_claim(driver, w),
        ferry_claim(driver, w, "The second ferry leaves at 08:00"),
    )
    retained = page(driver, w, access)
    result = narrow((first, second))
    args = dict(
        episode_id=retained.episode_id,
        source_id=retained.source_id,
        locator="whole",
        result=result,
        waiting_links=("link-op1",),
        access=access,
    )
    mention = retain_ambiguity(driver, w, "run1", CURATOR, **args)
    assert retain_ambiguity(driver, w, "run1", CURATOR, **args) == mention
    query = WorldQuery(driver, w)
    row = query.read("MATCH (m:UnresolvedMention {id: $id}) RETURN m", id=mention)[0]["m"]
    assert row["state"] == "open" and row["waiting_links"] == ["link-op1"]
    assert query.read("MATCH ()-[l:LINK]->() RETURN count(l) AS n")[0]["n"] == 0


def test_missing_authority_or_screening_opens_no_socket(driver, w):
    requests = []
    for screener in (None, SCREENER):
        with pytest.raises(CleanupRefused):
            fetch_and_retain(
                driver,
                w,
                "run1",
                "https://public.example/status",
                hosts=frozenset({"public.example"}),
                screener=screener,
                resolver=lambda *_: ["8.8.8.8"],
                transport=httpx.MockTransport(lambda request: requests.append(request)),
            )
    assert not requests


def test_question_closes_only_after_its_subject_change_and_replay_is_safe(
    driver, w, access, make_layer
):
    claim = ferry_claim(driver, w)
    other = ferry_claim(driver, w, "The second ferry leaves at 08:00")
    retained = page(driver, w, access)
    first, _ = judge_fetches(
        make_layer(Provider(spark(verdicts((2, "not_about_claim", 0.9))))),
        driver,
        w,
        "run1",
        claim,
        (retained,),
        access=access,
    )
    question_id = raise_question(
        driver,
        w,
        "run1",
        CURATOR,
        Question(
            (claim,),
            "Which departure do you prefer?",
            "decision",
            ((retained.source_id, first.judgment_ids[0]),),
            "A timetable cannot choose the person's preference.",
        ),
        screener=SCREENER,
        access=access,
    )
    source = make_source(driver, w, "person_in_views", ["I prefer the early ferry."])

    def apply_answer(subject):
        request_id = new_id(w)
        enqueue_request(
            driver,
            w,
            PERSON,
            request_id,
            source_id=source["source_id"],
            relay="answer",
            about=[question_id],
        )
        batch = stale_update(source, subject, 1)
        batch["ops"][0]["fields"]["recheck_by"] = (NOW + timedelta(days=1)).isoformat()
        batch["evidence"] = [observation(source, "e1", 1, at=NOW, locator="whole")]
        result = apply_request(driver, w, LEARNER, request_id, batch)
        assert result.api_state == "saved", result
        return request_id

    unrelated = apply_answer(other)
    with pytest.raises(CleanupRefused, match="subject_not_applied"):
        close_question(
            driver, w, "run1", PERSON, question_id, answer_request_id=unrelated, access=access
        )
    applied = apply_answer(claim)
    for _ in range(2):
        assert (
            close_question(
                driver, w, "run1", PERSON, question_id, answer_request_id=applied, access=access
            )
            == question_id
        )
    row = WorldQuery(driver, w).read("MATCH (q:Question {id: $id}) RETURN q", id=question_id)[0][
        "q"
    ]
    assert row["state"] == "answered" and row["version"] == 2
