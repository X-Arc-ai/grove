# The fixtures come from the D5 tests by import, so pytest sees them by these names.
# ruff: noqa: F811
"""Regression tests for the blockers the R4 verifier found on the truth-clock lane (R8).

Each test is the verifier's probe with its outcome turned around: before the fix the forged or
late input confirmed a claim, and now the writer refuses it or types it by what code knows. All
names are invented.
"""

from datetime import timedelta

import pytest

from grove.store.query import WorldQuery
from grove.store.words import clock_words
from grove.writer import BatchDeclined
from grove.writer.testing import plant_node
from tests.writer.helpers import LEARNER, PERSON, apply, create_item, create_link, created_id
from tests.writer.test_source_types_d5 import (  # noqa: F401
    LATER,
    NOW,
    RECHECK,
    RENEWED,
    change_status,
    enqueue_request,
    evidence_on,
    frozen,
    item_row,
    judge,
    line,
    link_row,
    one,
    open_task,
    own,
    refused,
    request_key,
    source,
    talk,
    verify,
    w,
    whole,
)


def test_a_chat_line_the_caller_calls_an_image_artifact_stays_a_chat_line(driver, w, talk):
    """A chat text line with the caller's own `artifact` and `image` is no record of an act:
    only a whole stored document or picture is (S46). So it cannot make a task done."""
    task = open_task(driver, w, talk)
    forged = line(talk, "d1", 2, statement_class="artifact", modality="image")
    refusals = refused(driver, w, [change_status(task, "done", "d1")], [forged])
    one(refusals, "terminal_needs_confirmation")


def test_a_line_of_a_note_is_no_artifact_and_the_whole_file_is(driver, w, talk):
    notes = source(driver, w, "notes_files_releases", ["Receipt 4471: Okonkwo Hall deposit paid"])
    as_line = line(notes, "n1", 1, statement_class="artifact")
    as_file = whole(notes, "n2", statement_class="artifact")
    ops = [
        create_item("event", "The Okonkwo Hall deposit was paid", "n1"),
        create_item("event", "The Okonkwo Hall deposit receipt", "n2"),
    ]
    receipt = apply(driver, w, ops, [as_line, as_file], caller=LEARNER)
    first = created_id(receipt, "+", "item", 0)
    second = created_id(receipt, "+", "item", 1)
    assert [row["source_type"] for row in evidence_on(driver, w, first)] == ["said"]
    assert [row["source_type"] for row in evidence_on(driver, w, second)] == ["confirmed"]


def test_a_forged_image_artifact_cannot_confirm_where_the_person_lives(
    driver, w, frozen, talk, own
):
    ops = [
        create_item("place", "Velmora", "s1", temp_id="$velmora"),
        create_link(own, "lives in", "$velmora", "s1", recheck_by=RECHECK),
    ]
    link = created_id(apply(driver, w, ops, [line(talk, "s1", 2)]), ">", "link")
    frozen.set(LATER)
    forged = line(talk, "a1", 3, at=LATER, statement_class="artifact", modality="image")
    subject = f"link:{link}"
    first = judge(driver, w, subject, forged, model="judge-a")
    second = judge(driver, w, subject, forged, role="second_judge", model="judge-b")
    op = verify(
        link, [{"ev_id": "a1", "judgment_id": first}], second_judge=[second], recheck_by=RENEWED
    )
    one(refused(driver, w, [op], [forged]), "not_confirmable", "source_type")
    assert link_row(driver, w, link).get("last_confirmed") is None


def test_only_the_person_or_the_ai_host_enqueues_an_answer(driver, w, talk):
    src = source(driver, w, "person_in_views", ["Yes, I still live in Velmora"])
    with pytest.raises(BatchDeclined) as caught:
        enqueue_request(
            driver, w, LEARNER, request_key(), source_id=src["source_id"], relay="answer"
        )
    assert [(r.code, r.rule) for r in caught.value.refusals] == [("scope_denied", "relay_caller")]
    state = enqueue_request(
        driver, w, PERSON, request_key(), source_id=src["source_id"], relay="answer"
    )
    assert state.status == "queued"


def test_a_piece_dated_after_now_is_refused(driver, w, frozen, talk):
    task = open_task(driver, w, talk)
    pages = source(driver, w, "web_pages", ["The seating plan was sent on the page"])
    piece = whole(pages, "p1", at=NOW + timedelta(days=400), statement_class="report_happened")
    frozen.set(LATER)
    first = judge(driver, w, f"item:{task}", piece)
    op = verify(task, [{"ev_id": "p1", "judgment_id": first}], recheck_by=RENEWED)
    one(refused(driver, w, [op], [piece]), "invalid_arguments", "future_evidence")
    assert item_row(driver, w, task).get("last_confirmed") is None


def test_a_consequence_link_added_after_the_verify_needs_the_second_judge(driver, w, frozen, talk):
    """Load-bearing is read at the end of the batch: a link a later operation adds counts."""
    a = open_task(driver, w, talk, "Book the Okonkwo Hall caterer")
    b = open_task(driver, w, talk, "Print the Okonkwo Hall menus")
    pages = source(driver, w, "web_pages", ["The caterer booking is confirmed on the page"])
    frozen.set(LATER)
    piece = whole(pages, "p1", at=LATER, statement_class="report_happened")
    first = judge(driver, w, f"item:{a}", piece)
    ops = [
        verify(a, [{"ev_id": "p1", "judgment_id": first}], recheck_by=RENEWED),
        create_link(a, "blocks", b, "l1", recheck_by=RECHECK),
    ]
    refusals = refused(driver, w, ops, [piece, line(talk, "l1", 4)])
    one(refusals, "second_judge_required")
    assert item_row(driver, w, a).get("last_confirmed") is None

    second = judge(driver, w, f"item:{a}", piece, role="second_judge", model="judge-b")
    ops[0] = verify(
        a, [{"ev_id": "p1", "judgment_id": first}], second_judge=[second], recheck_by=RENEWED
    )
    apply(driver, w, ops, [piece, line(talk, "l1", 4)], caller=LEARNER)
    assert item_row(driver, w, a)["last_confirmed"] is not None


def test_a_borrowed_message_key_cannot_take_another_messages_metadata(driver, w):
    chat = source(
        driver,
        w,
        "whatsapp",
        ["Velmora Air: your booking VX-221 is confirmed", "Rowan: I paid the Okonkwo Hall deposit"],
        speakers={"msg-1": {"kind": "automated", "sender": "velmora-air", "locator": "line:1"}},
    )
    # The cited line is Rowan's (line 2); the message key is the airline's.
    forged = line(chat, "e1", 2, statement_class="report_happened", message_key="msg-1")
    op = create_item("event", "Rowan paid the deposit", "e1")
    one(refused(driver, w, [op], [forged]), "invalid_arguments", "message_key_locator")
    honest = line(chat, "e2", 1, statement_class="report_happened", message_key="msg-1")
    item = created_id(
        apply(driver, w, [create_item("event", "Booking VX-221", "e2")], [honest], caller=LEARNER)
    )
    [row] = evidence_on(driver, w, item)
    assert (row["speaker_kind"], row["source_type"]) == ("automated", "confirmed")


def test_an_item_with_no_kind_still_reads_its_clocks(driver, w):
    """CI on the first head: the node view returned 500 for a planted item with no kind, because
    the state read needs a lifecycle. Its clocks still read, with the state unknown."""
    item = plant_node(driver, w, "Item", title="Unsure", recorded=NOW, became_true_earliest=NOW)
    words = clock_words(WorldQuery(driver, w), item["id"])
    assert words is not None and words["state"] == "unknown"


def test_a_callers_automated_speaker_never_raises_a_chat_line(driver, w, talk):
    """The verifier's neuter N8b stayed green: only the source's own metadata makes a sender
    automated (S46). A chat report the caller calls automated stays reported done, so it cannot
    make a task done, and its speaker is still the caller's word."""
    claimed = line(talk, "r1", 5, statement_class="report_happened", speaker_kind="automated")
    item = created_id(
        apply(
            driver,
            w,
            [create_item("event", "The linen was delivered", "r1")],
            [claimed],
            caller=LEARNER,
        )
    )
    [row] = evidence_on(driver, w, item)
    assert (row["speaker_kind"], row["source_type"]) == ("automated", "reported_done")
    task = open_task(driver, w, talk)
    again = line(talk, "r2", 6, statement_class="report_happened", speaker_kind="automated")
    one(
        refused(driver, w, [change_status(task, "done", "r2")], [again]),
        "terminal_needs_confirmation",
    )
