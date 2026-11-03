"""Labelled near-duplicate pairs through the writer's gate and through the candidate band.

Appendix C: a candidate at trigram similarity 0.3 or more, a resolution at 0.8 or more. D10 layer
3 says why the pairs are labelled: successive states of one thing (revision N after N-1, weekly
reports) look alike and are not duplicates. D2 "How to test it": "a document's revision N after
revision N-1 passes with an override and appears in the decline log without one". The scores are
the ones DESIGN section 6 measured on this code; the per-kind calibration on the backfill's pairs
is ingest-b's. Every name is invented.
"""

from datetime import timedelta
from typing import Any

import pytest

from grove.store.query import WorldQuery
from grove.writer import BatchDeclined, apply_batch, decline_log, request_state
from grove.writer.neardup import CANDIDATE_THRESHOLD, DEFAULT_THRESHOLD, similarity
from grove.writer.resolver import Candidate, lexical_candidates
from tests.writer.helpers import (
    PERSON,
    T0,
    batch,
    create_item,
    created_id,
    iso,
    observation,
    read,
    request_key,
)

RECHECK = iso(T0 + timedelta(days=30))
WORK = {
    "task": {"status": "open", "recheck_by": RECHECK},
    "plan": {"status": "proposed", "recheck_by": RECHECK},
}

# (label, kind, the item that exists, the title proposed after it, the measured score)
SAME_AT_THE_GATE = [
    ("reworded", "task", "Fix the Ilse invoice mismatch", "Ilse invoice mismatch fix", 0.862),
    ("name reordered", "person", "Rowan Okonkwo", "Okonkwo, Rowan", 1.0),
    # Exactly at the gate: a pair at 0.8 is refused, so the gate is "0.8 or more" (Appendix C).
    ("gate edge", "task", "Paint the grey hull", "Paint the grey hull today", 0.8),
]
SAME_IN_THE_BAND = [
    (
        "reworded",
        "task",
        "Book the venue for the Okonkwo offsite",
        "Okonkwo offsite: book a venue",
        0.722,
    ),
    ("reworded", "plan", "Thursday demo rehearsal", "Rehearsal for the demo on Thursday", 0.727),
]
SUCCESSIVE = [
    ("revision", "observation", "Release notes revision 7", "Release notes revision 8", 0.84),
    ("weekly report", "observation", "Weekly report 2026-W43", "Weekly report 2026-W44", 0.833),
]
DIFFERENT = [
    ("another task", "task", "Book the venue", "Pay the venue deposit", 0.37),
    # Exactly at the band's floor: a pair at 0.3 is a candidate (Appendix C).
    ("band edge", "task", "Book the venue", "Venue hall", 0.3),
]
# A successive pair sits at the gate on purpose: only a named distinct_from lets it through.
LEVELS = [
    *((*pair, "gate") for pair in SAME_AT_THE_GATE),
    *((*pair, "candidate") for pair in SAME_IN_THE_BAND),
    *((*pair, "gate") for pair in SUCCESSIVE),
    *((*pair, "candidate") for pair in DIFFERENT),
]
PAIR = ("label", "kind", "first", "second", "score")


def ids(pairs: list[tuple]) -> list[str]:
    return [f"{kind}-{label.replace(' ', '-')}-{score}" for label, kind, _, _, score, *_ in pairs]


def proposal(chat: dict, kind: str, title: str, line: int, **fields: Any) -> tuple:
    """One create_item and the request id it is sent under, so the test can look it up."""
    ev = f"e{line}"
    op = create_item(kind, title, ev, **WORK.get(kind, {}), **fields)
    return request_key(), op, batch([op], [observation(chat, ev, line)])


def send(driver, world_id: str, request_id: str, sent: dict):
    return apply_batch(driver, world_id, PERSON, sent, request_id=request_id)


def existing(driver, world_id: str, chat: dict, kind: str, title: str) -> str:
    request_id, _, sent = proposal(chat, kind, title, 1)
    return created_id(send(driver, world_id, request_id, sent))


def band(driver, world_id: str, kind_id: str, phrase: str, **kw: Any) -> list[Candidate]:
    return lexical_candidates(WorldQuery(driver, world_id), kind_id, phrase, **kw)


def level(score: float) -> str:
    if score >= DEFAULT_THRESHOLD:
        return "gate"
    return "candidate" if score >= CANDIDATE_THRESHOLD else "new"


# --- the labels against the two levels ------------------------------------------------------


def test_the_two_levels_are_the_values_of_appendix_c():
    assert (DEFAULT_THRESHOLD, CANDIDATE_THRESHOLD) == (0.8, 0.3)


@pytest.mark.parametrize((*PAIR, "expected"), LEVELS, ids=ids(LEVELS))
def test_each_labelled_pair_sits_at_its_level(label, kind, first, second, score, expected):
    measured = similarity(first, second)

    assert round(measured, 3) == score
    assert similarity(second, first) == measured
    assert level(measured) == expected


# --- through the writer's gate --------------------------------------------------------------


@pytest.mark.parametrize(PAIR, SAME_AT_THE_GATE, ids=ids(SAME_AT_THE_GATE))
def test_a_same_pair_at_the_gate_is_refused_as_a_near_duplicate(
    driver, seeded, chat, label, kind, first, second, score
):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, kind, first)
    request_id, op, sent = proposal(chat, kind, second, 2)

    with pytest.raises(BatchDeclined) as declined:
        send(driver, world_id, request_id, sent)

    (refusal,) = declined.value.refusals
    assert (refusal.code, refusal.rule) == ("near_duplicate", "near_duplicate")
    assert refusal.details["candidates"] == [{"item_id": first_id, "score": score}]
    (logged,) = decline_log(driver, world_id, request_id=request_id)
    assert (logged["op_id"], logged["override_used"]) == (op["op_id"], False)
    assert logged["candidates"] == [{"item_id": first_id, "score": score}]
    found = band(driver, world_id, seeded["kinds"][kind], second)
    assert found[0] == Candidate(first_id, score, False)


@pytest.mark.parametrize(PAIR, SAME_IN_THE_BAND, ids=ids(SAME_IN_THE_BAND))
def test_a_same_pair_below_the_gate_comes_back_as_a_candidate(
    driver, seeded, chat, label, kind, first, second, score
):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, kind, first)
    request_id, _, sent = proposal(chat, kind, second, 2)

    found = band(driver, world_id, seeded["kinds"][kind], second)
    receipt = send(driver, world_id, request_id, sent)

    assert found == [Candidate(first_id, score, False)]
    assert receipt.state == "saved"
    assert decline_log(driver, world_id, request_id=request_id) == []


@pytest.mark.parametrize(PAIR, SUCCESSIVE, ids=ids(SUCCESSIVE))
def test_a_successive_pair_without_distinct_from_is_refused_and_logged(
    driver, seeded, chat, label, kind, first, second, score
):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, kind, first)
    request_id, op, sent = proposal(chat, kind, second, 2)

    with pytest.raises(BatchDeclined) as declined:
        send(driver, world_id, request_id, sent)

    assert [refusal.code for refusal in declined.value.refusals] == ["near_duplicate"]
    (logged,) = decline_log(driver, world_id, request_id=request_id)
    assert (logged["op_id"], logged["rule"], logged["code"]) == (
        op["op_id"],
        "near_duplicate",
        "near_duplicate",
    )
    assert logged["override_used"] is False
    assert logged["candidates"] == [{"item_id": first_id, "score": score}]
    assert request_state(driver, world_id, request_id).api_state == "declined"
    written = read(driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id", t=second)
    assert written == []


@pytest.mark.parametrize(PAIR, SUCCESSIVE, ids=ids(SUCCESSIVE))
def test_a_successive_pair_with_distinct_from_passes_and_logs_the_override(
    driver, seeded, chat, label, kind, first, second, score
):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, kind, first)
    distinct = [{"item": first_id, "reason": f"the next {label}, not the same one"}]
    request_id, op, sent = proposal(chat, kind, second, 2, distinct_from=distinct)

    receipt = send(driver, world_id, request_id, sent)

    second_id = created_id(receipt)
    assert receipt.state == "saved"
    (logged,) = decline_log(driver, world_id, request_id=request_id)
    assert (logged["op_id"], logged["rule"]) == (op["op_id"], "near_duplicate")
    assert logged["override_used"] is True
    assert logged["candidates"] == [{"item_id": first_id, "score": score}]
    assert request_state(driver, world_id, request_id).declines == []
    pairs = read(driver, world_id, "MATCH (d:DistinctPair) RETURN d.item_a AS a, d.item_b AS b")
    assert [{pair["a"], pair["b"]} for pair in pairs] == [{first_id, second_id}]
    kind_id = seeded["kinds"][kind]
    every = band(driver, world_id, kind_id, second)
    assert [candidate.item_id for candidate in every] == [second_id, first_id]
    assert band(driver, world_id, kind_id, second, exclude_distinct_from=second_id) == []


@pytest.mark.parametrize(PAIR, DIFFERENT, ids=ids(DIFFERENT))
def test_a_different_pair_in_the_band_is_a_candidate_and_no_refusal(
    driver, seeded, chat, label, kind, first, second, score
):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, kind, first)
    request_id, _, sent = proposal(chat, kind, second, 2)

    found = band(driver, world_id, seeded["kinds"][kind], second)

    assert found == [Candidate(first_id, score, False)]
    assert send(driver, world_id, request_id, sent).state == "saved"
    assert decline_log(driver, world_id, request_id=request_id) == []


# --- distinct_from overrides only the item it names ------------------------------------------


def test_distinct_from_overrides_only_the_item_it_names(driver, seeded, chat):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, "person", "Rowan Okonkwo")
    named_first = [{"item": first_id, "reason": "a namesake, not the same person"}]
    request_id, _, sent = proposal(chat, "person", "Okonkwo, Rowan", 2, distinct_from=named_first)
    second_id = created_id(send(driver, world_id, request_id, sent))
    request_id, op, sent = proposal(chat, "person", "Rowan Okonkwo", 3, distinct_from=named_first)

    with pytest.raises(BatchDeclined) as declined:
        send(driver, world_id, request_id, sent)

    (refusal,) = declined.value.refusals
    assert (refusal.code, refusal.rule) == ("near_duplicate", "near_duplicate")
    assert refusal.details["candidates"] == [{"item_id": second_id, "score": 1.0}]
    (logged,) = decline_log(driver, world_id, request_id=request_id)
    assert (logged["op_id"], logged["override_used"]) == (op["op_id"], False)


def test_distinct_from_on_an_unrelated_item_clears_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, "person", "Rowan Okonkwo")
    request_id, _, sent = proposal(chat, "person", "Ilse Tamsin", 2)
    unrelated = created_id(send(driver, world_id, request_id, sent))
    named = [{"item": unrelated, "reason": "not her"}]
    request_id, _, sent = proposal(chat, "person", "Okonkwo, Rowan", 3, distinct_from=named)

    with pytest.raises(BatchDeclined) as declined:
        send(driver, world_id, request_id, sent)

    (refusal,) = declined.value.refusals
    assert (refusal.code, refusal.rule) == ("near_duplicate", "near_duplicate")
    assert refusal.details["candidates"] == [{"item_id": first_id, "score": 1.0}]
    written = read(
        driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id", t="Okonkwo, Rowan"
    )
    assert written == []


# --- a title that reads the same is the same title --------------------------------------------

INVISIBLE = [
    ("direction mark", "\u200eRowan Okonkwo"),
    ("byte order mark", "\ufeffRowan Okonkwo"),
    ("soft hyphen", "Ro\u00adwan Okon\u00adkwo"),
    ("zero-width space in each word", "Ro\u200bwan Okon\u200bkwo"),
    ("zero-width space between letters", "\u200b".join("Rowan Okonkwo")),
]


@pytest.mark.parametrize(("label", "title"), INVISIBLE, ids=[label for label, _ in INVISIBLE])
def test_a_title_with_an_invisible_character_meets_the_gate(driver, seeded, chat, label, title):
    world_id = seeded["world_id"]
    first_id = existing(driver, world_id, chat, "person", "Rowan Okonkwo")
    request_id, _, sent = proposal(chat, "person", title, 2)

    with pytest.raises(BatchDeclined) as declined:
        send(driver, world_id, request_id, sent)

    (refusal,) = declined.value.refusals
    assert (refusal.code, refusal.rule) == ("near_duplicate", "near_duplicate")
    assert refusal.details["candidates"] == [{"item_id": first_id, "score": 1.0}]
    found = band(driver, world_id, seeded["kinds"]["person"], title)
    assert found == [Candidate(first_id, 1.0, False)]
