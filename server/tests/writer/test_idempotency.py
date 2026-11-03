"""D2 "Idempotency" and D1's restatement (spec lines 682, 766, 774).

Line 774: "Idempotency: replaying a request with the same id changes nothing, and restating a known
link from a new source adds evidence and no link." Line 682: "restating an existing link adds an
evidence row targeting that link and creates no link." Line 766: the evidence key (world_id,
message key, target id, polarity) is unique, "so the same message can never support the same claim
twice, even when it arrives in two overlapping exports."
"""

from typing import Any

import pytest

from grove.writer import (
    BatchDeclined,
    apply_batch,
    apply_request,
    claim_next_request,
    enqueue_request,
    record_episode,
    record_source,
)
from tests.writer.helpers import (
    CHAT_LINES,
    HOST,
    LEARNER,
    PERSON,
    T0,
    at_minute,
    batch,
    count_rows,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    raw,
    read,
    request_key,
    request_row,
    supports,
    unknown_kind,
    world_dump,
)

RECHECK = "2027-06-01T00:00:00+00:00"
OTHER_LINES = [f"note {n}: an invented note about the Okonkwo offsite" for n in range(12)]


@pytest.fixture
def cast(driver, seeded, chat) -> dict[str, Any]:
    """A venue task, a name-card task and Tamsin, each from one line of the chat."""
    world_id = seeded["world_id"]
    work = {"status": "open", "recheck_by": RECHECK}
    receipt = apply_batch(
        driver,
        world_id,
        PERSON,
        batch(
            [
                create_item("task", "Book the venue for the Okonkwo offsite", "e1", **work),
                create_item("task", "Print the name cards for Rowan and Ilse", "e2", **work),
                create_item("person", "Tamsin", "e3"),
            ],
            [observation(chat, f"e{n}", n) for n in (1, 2, 3)],
        ),
    )
    venue, cards, tamsin = (created_id(receipt, nth=n) for n in range(3))
    return {"world_id": world_id, "chat": chat, "venue": venue, "cards": cards, "tamsin": tamsin}


def snapshot(driver, world_id: str) -> dict[str, Any]:
    """Row counts, the world's next sequence, every version, and every property of every row."""
    versions = raw(
        driver,
        "MATCH (n {world_id: $w}) WHERE n.version IS NOT NULL RETURN n.id AS id, n.version AS v "
        "UNION ALL MATCH ()-[l:LINK {world_id: $w}]->() RETURN l.id AS id, l.version AS v",
        w=world_id,
    )
    sequence = raw(driver, "MATCH (w:World {id: $w}) RETURN w.next_sequence AS n", w=world_id)
    return {
        "rows": count_rows(driver, world_id),
        "sequence": sequence[0]["n"],
        "versions": sorted((row["id"], row["v"]) for row in versions),
        "dump": world_dump(driver, world_id),
    }


def evidence_on(driver, world_id: str, target_id: str) -> list[dict[str, Any]]:
    return [
        row["e"]
        for row in read(
            driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=target_id
        )
    ]


def assert_unchanged_but(before: dict[str, int], after: dict[str, int], *labels: str) -> None:
    """Every row count is as it was, apart from these labels, each up by one."""
    for label in labels:
        assert after.get(label, 0) == before.get(label, 0) + 1, label
    assert {k: v for k, v in after.items() if k not in labels} == {
        k: v for k, v in before.items() if k not in labels
    }


def the_link(driver, world_id: str) -> dict[str, Any]:
    [row] = read(driver, world_id, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l")
    return row["l"]


# The same id and the same batch, twice ------------------------------------------------------


def a_request(cast: dict[str, Any]) -> dict[str, Any]:
    """A batch that creates, links and moves a version, so a second effect would show anywhere."""
    chat = cast["chat"]
    update = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": cast["tamsin"],
        "version": 1,
        "fields": {"summary": "Tamsin runs the seating plan"},
        "evidence_refs": supports("e7"),
        "reason": "the message states it",
    }
    return batch(
        [
            create_item(
                "task",
                "Draft the seating plan for Ilse",
                "e7",
                temp_id="$plan",
                status="open",
                recheck_by=RECHECK,
            ),
            create_link("$plan", "part of", cast["venue"], "e7"),
            create_link(cast["venue"], "assigned to", cast["tamsin"], "e7", recheck_by=RECHECK),
            update,
        ],
        [observation(chat, "e7", 7)],
    )


def test_a_direct_replay_with_the_same_id_and_batch_changes_nothing(driver, cast):
    world_id, request_id, sent = cast["world_id"], request_key(), a_request(cast)
    first = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = snapshot(driver, world_id)
    assert before["versions"] and dict(before["versions"])[cast["tamsin"]] == 2

    again = apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert again == first
    assert snapshot(driver, world_id) == before


def test_a_queued_replay_with_the_same_id_and_batch_changes_nothing(driver, cast):
    world_id, request_id, sent = cast["world_id"], request_key(), a_request(cast)
    enqueue_request(driver, world_id, HOST, request_id, source_id=cast["chat"]["source_id"])
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-1").id == request_id
    first = apply_request(driver, world_id, LEARNER, request_id, sent)
    assert first.api_state == "saved"
    before = snapshot(driver, world_id)

    again = apply_request(driver, world_id, LEARNER, request_id, sent)
    enqueued_again = enqueue_request(
        driver, world_id, HOST, request_id, source_id=cast["chat"]["source_id"]
    )

    assert again.receipt == first.receipt
    assert enqueued_again.receipt == first.receipt
    assert claim_next_request(driver, world_id, LEARNER, worker="pass-2") is None
    assert snapshot(driver, world_id) == before
    assert request_row(driver, world_id, request_id)["attempts"] == 1


def test_a_declined_batch_replayed_with_the_same_id_changes_nothing(driver, cast):
    world_id, request_id = cast["world_id"], request_key()
    sent = unknown_kind(cast["chat"])
    with pytest.raises(BatchDeclined) as first:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)
    before = snapshot(driver, world_id)

    with pytest.raises(BatchDeclined) as again:
        apply_batch(driver, world_id, PERSON, sent, request_id=request_id)

    assert [(r.op_id, r.code, r.rule) for r in again.value.refusals] == [
        (r.op_id, r.code, r.rule) for r in first.value.refusals
    ]
    assert snapshot(driver, world_id) == before


# Restating a known link from a new source ----------------------------------------------------


@pytest.mark.parametrize(
    ("source", "phrase", "target"),
    [
        ("cards", "part of", "venue"),
        ("cards", "within", "venue"),
        ("venue", "includes", "cards"),
    ],
    ids=["meaning", "alias", "reversed_alias"],
)
def test_restating_a_known_link_from_a_new_source_adds_one_evidence_row_and_no_link(
    driver, cast, source, phrase, target
):
    world_id, chat = cast["world_id"], cast["chat"]
    apply_batch(
        driver,
        world_id,
        PERSON,
        batch(
            [create_link(cast["cards"], "part of", cast["venue"], "e4")],
            [observation(chat, "e4", 4)],
        ),
    )
    link = the_link(driver, world_id)
    before = count_rows(driver, world_id)
    other = make_source(driver, world_id, "whatsapp", OTHER_LINES)
    request_id = request_key()

    receipt = apply_batch(
        driver,
        world_id,
        PERSON,
        batch(
            [create_link(cast[source], phrase, cast[target], "n3")],
            [observation(other, "n3", 3)],
        ),
        request_id=request_id,
    )

    after = count_rows(driver, world_id)
    assert after["LINK"] == before["LINK"] and after["Item"] == before["Item"]
    assert after["Evidence"] == before["Evidence"] + 1
    # The lock writes touched_at; nothing else of the link moves, last confirmed included (D1: a
    # mention is not a confirmation).
    held = the_link(driver, world_id)
    assert {k: v for k, v in held.items() if k != "touched_at"} == {
        k: v for k, v in link.items() if k != "touched_at"
    }
    [added] = [
        row
        for row in evidence_on(driver, world_id, link["id"])
        if row["id"] != link["provenance_evidence_id"]
    ]
    assert (added["target_type"], added["polarity"], added["active"]) == ("link", "supports", True)
    assert (added["source_id"], added["episode_id"]) == (other["source_id"], other["episode_id"])
    assert added["request_id"] == request_id
    assert [(line.symbol, line.entity_id, line.already_known) for line in receipt.lines] == [
        ("=", link["id"], True)
    ]


# The same message from an overlapping export ---------------------------------------------------


def overlapping_export(driver, world_id: str, chat: dict[str, str]) -> dict[str, str]:
    """A later export of the same chat with more lines: a new source of the same connector, cut
    into the same episode, which keeps its id."""
    [episode] = read(
        driver, world_id, "MATCH (e:Episode {id: $id}) RETURN e", id=chat["episode_id"]
    )
    lines = [*CHAT_LINES, "line 40: an invented late message about the venue"]
    source_id = record_source(
        driver,
        world_id,
        connector_id=chat["connector_id"],
        external_locator="export-overlap.txt",
        content="\n".join(lines),
        parser_version="test-1",
    )
    episode_id = record_episode(
        driver,
        world_id,
        connector_id=chat["connector_id"],
        source_ids=[source_id],
        first_message_key=episode["e"]["first_message_key"],
        last_message_key="k-last-overlap",
        start_time=T0,
        end_time=at_minute(len(lines)),
        cut_rule="chat_window_90m",
    )
    assert episode_id == chat["episode_id"]
    return {"source_id": source_id, "episode_id": episode_id}


def test_an_overlapping_message_restating_a_link_adds_no_row(driver, cast):
    world_id, chat = cast["world_id"], cast["chat"]
    first = observation(chat, "e4", 4)
    apply_batch(
        driver,
        world_id,
        PERSON,
        batch([create_link(cast["cards"], "part of", cast["venue"], "e4")], [first]),
    )
    link = the_link(driver, world_id)
    overlap = overlapping_export(driver, world_id, chat)
    before = count_rows(driver, world_id)
    again = observation(overlap, "e4", 4, message_key=first["message_key"])

    receipt = apply_batch(
        driver,
        world_id,
        PERSON,
        batch([create_link(cast["cards"], "part of", cast["venue"], "e4")], [again]),
    )

    # The request row is the request's own.
    after = count_rows(driver, world_id)
    assert_unchanged_but(before, after, "Request")
    [row] = evidence_on(driver, world_id, link["id"])
    assert row["source_id"] == chat["source_id"]
    # The lock writes touched_at; nothing else of the link moves.
    held = the_link(driver, world_id)
    assert {k: v for k, v in held.items() if k != "touched_at"} == {
        k: v for k, v in link.items() if k != "touched_at"
    }
    assert receipt.lines == ()


@pytest.mark.parametrize("op", ["restated_link", "attach_with_reason"])
def test_an_overlapping_message_writes_no_text_that_nothing_names(driver, cast, op):
    world_id, chat = cast["world_id"], cast["chat"]
    first = observation(chat, "e4", 4)
    link_op = create_link(cast["cards"], "part of", cast["venue"], "e4")
    apply_batch(driver, world_id, PERSON, batch([link_op], [first]))
    overlap = overlapping_export(driver, world_id, chat)
    before = count_rows(driver, world_id)
    again = observation(overlap, "e4", 4, message_key=first["message_key"])
    sent = create_link(cast["cards"], "part of", cast["venue"], "e4")
    if op == "attach_with_reason":
        sent = {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": the_link(driver, world_id)["id"],
            "ev_id": "e4",
            "polarity": "supports",
            "reason": "the export repeats the message",
        }

    receipt = apply_batch(driver, world_id, PERSON, batch([sent], [again]))

    assert receipt.lines == ()
    assert_unchanged_but(before, count_rows(driver, world_id), "Request")


def test_an_overlapping_message_attached_to_an_item_adds_no_row(driver, cast):
    world_id, chat = cast["world_id"], cast["chat"]
    overlap = overlapping_export(driver, world_id, chat)
    [known] = evidence_on(driver, world_id, cast["venue"])
    before = count_rows(driver, world_id)
    again = observation(overlap, "a1", 1, message_key=known["message_key"])
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": cast["venue"],
        "ev_id": "a1",
        "polarity": "supports",
    }

    receipt = apply_batch(driver, world_id, PERSON, batch([attach], [again]))

    assert_unchanged_but(before, count_rows(driver, world_id), "Request")
    assert receipt.lines == ()
    assert [row["id"] for row in evidence_on(driver, world_id, cast["venue"])] == [known["id"]]
