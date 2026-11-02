"""D2 receipts: read back after the commit, matching the rows, printed in the fixed grammar.

Spec, D2 "Receipts": "After the commit the writer reads back the rows it committed, in a fresh
read, and returns a receipt in a fixed grammar". A stored receipt holds ids and codes only, so
render_receipt reads titles and names fresh.
"""

import json
import re
from typing import Any

import pytest

from grove.ids import new_id
from grove.writer import BatchDeclined, receipts, render_receipt
from tests.writer.helpers import (
    apply,
    at_minute,
    clock_at,
    count_rows,
    create_item,
    create_link,
    new_op_id,
    observation,
    read,
    supports,
)

short_id = receipts.short_id
RECHECK = "2027-06-01T00:00:00+00:00"
PLAN_TITLE = "Thursday demo rehearsal"
TASK_TITLE = "Draft the venue list"


def one(driver, world_id: str, cypher: str, **params: Any) -> dict:
    rows = read(driver, world_id, cypher, **params)
    assert len(rows) == 1, rows
    return rows[0]


def item_id(driver, world_id: str, title: str) -> str:
    return one(driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id", t=title)["id"]


def item(driver, world_id: str, ident: str) -> dict:
    return one(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=ident)["i"]


def link(driver, world_id: str, ident: str) -> dict:
    return one(driver, world_id, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=ident)[
        "l"
    ]


def links_between(driver, world_id: str, a: str, b: str) -> list[dict]:
    return [
        row["l"]
        for row in read(
            driver, world_id, "MATCH (:Item {id: $a})-[l:LINK]->(:Item {id: $b}) RETURN l", a=a, b=b
        )
    ]


def evidence_on(driver, world_id: str, target: str) -> list[dict]:
    cypher = "MATCH (e:Evidence {target_id: $id}) RETURN e"
    return [row["e"] for row in read(driver, world_id, cypher, id=target)]


def rendered(driver, world_id: str, receipt) -> list[str]:
    return render_receipt(driver, world_id, receipt).splitlines()


def entity_exists(driver, world_id: str, entity_type: str, ident: str) -> bool:
    patterns = {
        "item": "MATCH (n:Item {id: $id}) RETURN n.id AS id",
        "belief": "MATCH (n:Item {id: $id}) RETURN n.id AS id",
        "link": "MATCH (:Item)-[n:LINK {id: $id}]->(:Item) RETURN n.id AS id",
        "evidence": "MATCH (n:Evidence {id: $id}) RETURN n.id AS id",
    }
    return len(read(driver, world_id, patterns[entity_type], id=ident)) == 1


def plan_owned_by_rowan(driver, world_id: str, chat: dict, **kw: Any):
    """One batch: a plan, a person, and the plan owned by the person."""
    ops = [
        create_item(
            "plan", PLAN_TITLE, "e1", temp_id="$plan", status="proposed", recheck_by=RECHECK
        ),
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_link("$plan", "owned by", "$rowan", "e1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)], **kw)
    plan, rowan = item_id(driver, world_id, PLAN_TITLE), item_id(driver, world_id, "Rowan")
    (owns,) = links_between(driver, world_id, plan, rowan)
    return receipt, plan, rowan, owns["id"]


def update_title(ident: str, version: int, title: str, ev_id: str) -> dict:
    return {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": ident,
        "version": version,
        "fields": {"title": title},
        "evidence_refs": supports(ev_id),
        "reason": "the message renames it",
    }


def test_a_short_id_is_the_last_eight_hex_digits():
    ident = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
    assert short_id(ident) == "6789abcd"


def test_every_receipt_line_names_a_committed_row_in_its_stated_state(driver, seeded, chat):
    world_id = seeded["world_id"]
    receipt, plan, rowan, owns = plan_owned_by_rowan(driver, world_id, chat)

    assert receipt.state == "saved"
    for line in receipt.lines:
        assert entity_exists(driver, world_id, line.entity_type, line.entity_id), line
    created = sorted((ln.entity_type, ln.entity_id) for ln in receipt.lines if ln.symbol == "+")
    assert created == sorted([("item", plan), ("item", rowan)])
    assert item(driver, world_id, plan)["kind"] == seeded["kinds"]["plan"]
    assert item(driver, world_id, plan)["title"] == PLAN_TITLE
    assert item(driver, world_id, rowan)["kind"] == seeded["kinds"]["person"]
    made = [(ln.entity_type, ln.entity_id) for ln in receipt.lines if ln.symbol == ">"]
    assert made == [("link", owns)]
    row = link(driver, world_id, owns)
    assert (row["from_item"], row["meaning"], row["to_item"]) == (
        plan,
        seeded["meanings"]["owned by"],
        rowan,
    )
    assert row.get("end_class") is None
    assert row.get("active_key")


def test_the_receipt_is_built_from_the_change_entries_of_its_commit(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = new_id(world_id)
    receipt, plan, rowan, owns = plan_owned_by_rowan(driver, world_id, chat, request_id=request_id)

    entries = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {change_set_id: $cs}) "
        "RETURN c.commit_id AS commit_id, c.request_id AS request_id, c.entity_id AS entity_id",
        cs=receipt.change_set_id,
    )
    assert receipt.request_id == request_id
    assert entries
    assert {entry["commit_id"] for entry in entries} == {receipt.commit_id}
    assert {entry["request_id"] for entry in entries} == {request_id}
    assert {plan, rowan, owns} <= {entry["entity_id"] for entry in entries}


def test_render_prints_the_d2_grammar_for_a_small_batch(driver, seeded, chat):
    world_id = seeded["world_id"]
    request_id = new_id(world_id)
    receipt, plan, rowan, owns = plan_owned_by_rowan(driver, world_id, chat, request_id=request_id)
    s = short_id

    lines = rendered(driver, world_id, receipt)

    assert lines[0] == (
        f"saved ({len(receipt.lines)}), commit {s(receipt.commit_id)}, request {s(request_id)}"
    )
    assert len(lines) == 1 + len(receipt.lines)
    # Each create cites its evidence; any '=' line for that evidence is left to its own test.
    assert [line for line in lines[1:] if not line.startswith("  = ")] == [
        f"  + item {s(plan)} (plan): {PLAN_TITLE}",
        f"  + item {s(rowan)} (person): Rowan",
        f"  > link {s(owns)}: {s(plan)} -[owned by]-> {s(rowan)}",
    ]


def test_a_superseded_link_line_matches_the_ended_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = [
        create_item("task", TASK_TITLE, "e1", temp_id="$task", status="open", recheck_by=RECHECK),
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_link(
            "$task",
            "assigned to",
            "$rowan",
            "e1",
            became_true=clock_at(at_minute(1)),
            recheck_by=RECHECK,
        ),
    ]
    apply(driver, world_id, first, [observation(chat, "e1", 1)])
    task, rowan = item_id(driver, world_id, TASK_TITLE), item_id(driver, world_id, "Rowan")
    (old,) = links_between(driver, world_id, task, rowan)
    second = [
        create_item("person", "Ilse", "e5", temp_id="$ilse"),
        create_link(
            task,
            "assigned to",
            "$ilse",
            "e5",
            became_true=clock_at(at_minute(5)),
            recheck_by=RECHECK,
        ),
    ]

    receipt = apply(driver, world_id, second, [observation(chat, "e5", 5)])

    ilse = item_id(driver, world_id, "Ilse")
    (new,) = links_between(driver, world_id, task, ilse)
    ended = [
        (ln.entity_type, ln.entity_id, ln.end_class) for ln in receipt.lines if ln.symbol == "x"
    ]
    assert ended == [("link", old["id"], "superseded")]
    stored = link(driver, world_id, old["id"])
    assert stored["end_class"] == "superseded"
    assert stored.get("active_key") is None
    s = short_id
    lines = rendered(driver, world_id, receipt)
    assert (
        f"  x link {s(old['id'])}: {s(task)} -[assigned to]-> {s(rowan)} (superseded by new link)"
        in lines
    )
    assert f"  > link {s(new['id'])}: {s(task)} -[assigned to]-> {s(ilse)}" in lines


def test_render_reads_titles_fresh_and_the_stored_receipt_holds_no_title(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_op = create_item("task", TASK_TITLE, "e1", status="open", recheck_by=RECHECK)
    first = apply(driver, world_id, [task_op], [observation(chat, "e1", 1)])
    task = item_id(driver, world_id, TASK_TITLE)
    assert f"  + item {short_id(task)} (task): {TASK_TITLE}" in rendered(driver, world_id, first)
    version = item(driver, world_id, task)["version"]
    renamed = "Book the boathouse for Ilse"

    apply(
        driver, world_id, [update_title(task, version, renamed, "e2")], [observation(chat, "e2", 2)]
    )

    assert item(driver, world_id, task)["title"] == renamed
    lines = rendered(driver, world_id, first)
    assert f"  + item {short_id(task)} (task): {renamed}" in lines
    assert not any(TASK_TITLE in line for line in lines)
    stored = first.to_json()
    text = stored if isinstance(stored, str) else json.dumps(stored)
    assert "venue" not in text
    assert "boathouse" not in text


def test_a_declined_receipt_names_each_failing_operation_and_says_nothing_was_written(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    setup = [
        create_item("plan", PLAN_TITLE, "e1", status="proposed", recheck_by=RECHECK),
        create_item("place", "Okonkwo boathouse", "e1"),
    ]
    apply(driver, world_id, setup, [observation(chat, "e1", 1)])
    plan = item_id(driver, world_id, PLAN_TITLE)
    place = item_id(driver, world_id, "Okonkwo boathouse")
    ops = [
        create_link(plan, "lives in", place, "e2", recheck_by=RECHECK),
        create_item("person", "Tamsin", "e2"),
        create_item("spaceship", "The silver hull", "e2"),
    ]
    before = count_rows(driver, world_id)

    with pytest.raises(BatchDeclined) as declined:
        apply(driver, world_id, ops, [observation(chat, "e2", 2)])

    assert count_rows(driver, world_id) == before
    assert [(r.op_id, r.code) for r in declined.value.refusals] == [
        (ops[0]["op_id"], "endpoint_kind_mismatch"),
        (ops[2]["op_id"], "unknown_kind"),
    ]
    lines = rendered(driver, world_id, declined.value.receipt)
    assert lines[0] == "declined (batch of 3, nothing written)"
    assert len(lines) == 3
    s = short_id
    assert (
        lines[1]
        == f"  ! link {s(plan)} -[lives in]-> {s(place)}: endpoint_kind_mismatch (from_kind)"
    )
    assert lines[2].startswith("  ! ")
    assert re.search(r": unknown_kind\b", lines[2])


def test_a_restated_link_renders_already_known_and_adds_evidence_not_a_link(driver, seeded, chat):
    world_id = seeded["world_id"]
    _, plan, rowan, owns = plan_owned_by_rowan(driver, world_id, chat)
    again = create_link(plan, "owned by", rowan, "e2", recheck_by=RECHECK)

    receipt = apply(driver, world_id, [again], [observation(chat, "e2", 2)])

    assert [ln for ln in receipt.lines if ln.symbol == ">"] == []
    known = [(ln.entity_type, ln.entity_id) for ln in receipt.lines if ln.symbol == "="]
    assert known == [("link", owns)]
    s = short_id
    line = f"  = link {s(owns)}: {s(plan)} -[owned by]-> {s(rowan)} (already known; evidence added)"
    assert line in rendered(driver, world_id, receipt)
    assert [row["id"] for row in links_between(driver, world_id, plan, rowan)] == [owns]
    rows = evidence_on(driver, world_id, owns)
    assert len(rows) == 2
    assert all(row["active"] is True for row in rows)


def test_a_history_only_update_renders_the_head_is_newer(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_op = create_item("task", TASK_TITLE, "e5", status="open", recheck_by=RECHECK)
    apply(driver, world_id, [task_op], [observation(chat, "e5", 5)])
    task = item_id(driver, world_id, TASK_TITLE)
    version = item(driver, world_id, task)["version"]
    older = update_title(task, version, "Draft the guest list", "e2")

    receipt = apply(driver, world_id, [older], [observation(chat, "e2", 2)])

    assert item(driver, world_id, task)["title"] == TASK_TITLE
    changed = [(ln.entity_id, ln.field) for ln in receipt.lines if ln.symbol == "*"]
    assert changed == [(task, "title")]
    entries = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {change_set_id: $cs, entity_id: $id, field: 'title'}) "
        "RETURN c.history_only AS history_only",
        cs=receipt.change_set_id,
        id=task,
    )
    assert [entry["history_only"] for entry in entries] == [True]
    stars = [
        line
        for line in rendered(driver, world_id, receipt)
        if line.startswith(f"  * item {short_id(task)}: title=")
    ]
    # D1: "* item 44b0: status=open at 03-03, history only (the head is newer)".
    assert stars == [
        f"  * item {short_id(task)}: title=Draft the guest list at 10-20, "
        "history only (the head is newer)"
    ]


def test_a_history_only_status_change_renders_its_world_time(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_op = create_item("task", TASK_TITLE, "e5", status="open", recheck_by=RECHECK)
    apply(driver, world_id, [task_op], [observation(chat, "e5", 5)])
    task = item_id(driver, world_id, TASK_TITLE)
    older = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task,
        "status": "blocked",
        "evidence_refs": supports("e2"),
        "reason": "an earlier message said it waited",
    }

    receipt = apply(driver, world_id, [older], [observation(chat, "e2", 2)])

    assert (
        f"  * item {short_id(task)}: status=blocked at 10-20, history only (the head is newer)"
        in (rendered(driver, world_id, receipt))
    )
    [star] = [ln for ln in receipt.lines if ln.field == "status"]
    assert star.world_time is not None
    assert receipts.Receipt.from_json(receipt.to_json()) == receipt


def test_an_attached_evidence_line_matches_its_active_row(driver, seeded, chat):
    world_id = seeded["world_id"]
    apply(driver, world_id, [create_item("person", "Rowan", "e1")], [observation(chat, "e1", 1)])
    rowan = item_id(driver, world_id, "Rowan")
    seen = observation(chat, "e3", 3)
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": rowan,
        "ev_id": "e3",
        "polarity": "supports",
    }

    receipt = apply(driver, world_id, [attach], [seen])

    attached = [(ln.entity_type, ln.entity_id) for ln in receipt.lines if ln.symbol == "="]
    assert attached == [("item", rowan)]
    rows = [
        row
        for row in evidence_on(driver, world_id, rowan)
        if row["message_key"] == seen["message_key"]
    ]
    assert len(rows) == 1
    row = rows[0]
    assert (row["active"], row["polarity"], row["target_type"]) == (True, "supports", "item")
    s = short_id
    line = f"  = item {s(rowan)} evidence {s(row['id'])} (supports, {row['source_type']})"
    assert line in rendered(driver, world_id, receipt)
