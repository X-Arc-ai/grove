"""D3's contract tests: corrections never overwrite.

Written from the spec's D3 "How to test it" and B.2's rows for correct, refute, supersede, merge,
undo and retire_record. Each test names the line it checks. All names are invented.
"""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from grove.store.asof import item_as_of
from grove.store.current import current_items, standings
from grove.store.d3_fingerprint import fingerprint, live_fingerprint
from grove.store.primitives import MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY
from grove.store.query import WorldQuery
from grove.store.replay import _NO_VERSION, read_history, rebuild
from grove.writer import Caller, render_receipt
from grove.writer.composite import STILL_MARKERS, _successor_clock
from grove.writer.errors import Refused
from tests.writer.helpers import (
    LEARNER,
    PERSON,
    T0,
    apply,
    at_minute,
    clock_at,
    create_item,
    create_link,
    created_id,
    make_source,
    new_op_id,
    observation,
    raw,
    read,
    supports,
)
from tests.writer.test_contract_d2 import (
    GIT_LINES,
    RECHECK,
    change_status,
    evidence_rows,
    git_observation,
    item_row,
    make_item,
    make_task,
    one,
    refused,
    update_item,
)

CONNECTOR = Caller(
    kind="consolidation", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:connector"
)


# Builders -------------------------------------------------------------------------------------


def contradicts(*ev_ids: str) -> list[dict[str, str]]:
    return [{"ev_id": ev_id, "polarity": "contradicts"} for ev_id in ev_ids]


def correct(target: str, successor: str, *ev_ids: str, **fields: Any) -> dict[str, Any]:
    return {
        "op": "correct",
        "op_id": new_op_id(),
        "target": target,
        "successor": successor,
        "evidence_refs": supports(*ev_ids),
        "reason": "the world moved on since the record was written",
        **fields,
    }


def refute(target: str, refs: list[dict[str, str]], **fields: Any) -> dict[str, Any]:
    return {
        "op": "refute",
        "op_id": new_op_id(),
        "target": target,
        "evidence_refs": refs,
        "reason": "a later message says it never happened",
        **fields,
    }


def supersede(target: str, successor: str, *ev_ids: str, **fields: Any) -> dict[str, Any]:
    return {
        "op": "supersede",
        "op_id": new_op_id(),
        "target": target,
        "successor": successor,
        "evidence_refs": supports(*ev_ids),
        "reason": "the decision changed",
        **fields,
    }


def merge(survivor: str, absorbed: str, **fields: Any) -> dict[str, Any]:
    return {
        "op": "merge",
        "op_id": new_op_id(),
        "survivor": survivor,
        "absorbed": absorbed,
        "reason": "the two items name one place",
        **fields,
    }


def undo(change_set: str) -> dict[str, Any]:
    return {
        "op": "undo",
        "op_id": new_op_id(),
        "change_set": change_set,
        "reason": "the automatic change was wrong",
    }


def retire(record: str, lesson: str, **fields: Any) -> dict[str, Any]:
    return {
        "op": "retire_record",
        "op_id": new_op_id(),
        "record": record,
        "lesson": lesson,
        "reason": "its lesson now lives in the head",
        **fields,
    }


def props(driver, world_id: str, item_id: str) -> dict[str, Any]:
    rows = raw(
        driver,
        "MATCH (i:Item {id: $id, world_id: $w}) RETURN properties(i) AS p",
        id=item_id,
        w=world_id,
    )
    assert len(rows) == 1
    return dict(rows[0]["p"])


def entries_of(driver, world_id: str, entity_id: str) -> list[dict[str, Any]]:
    rows = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {entity_id: $id}) RETURN e ORDER BY e.sequence",
        id=entity_id,
    )
    return [row["e"] for row in rows]


def entries_of_set(driver, world_id: str, change_set: str) -> list[dict[str, Any]]:
    rows = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN e ORDER BY e.sequence",
        id=change_set,
    )
    return [row["e"] for row in rows]


def successor_links(driver, world_id: str, item_id: str, meaning: str) -> list[dict[str, Any]]:
    rows = read(
        driver,
        world_id,
        "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item = $id AND l.meaning = $m RETURN l",
        id=item_id,
        m=meaning,
    )
    return [row["l"] for row in rows]


def links_touching(driver, world_id: str, item_ids: list[str]) -> set[tuple]:
    rows = read(
        driver,
        world_id,
        "MATCH (:Item)-[l:LINK]->(:Item) WHERE (l.from_item IN $ids OR l.to_item IN $ids) "
        "AND l.active_key IS NOT NULL RETURN l.id AS id, l.from_item AS a, l.meaning AS m, "
        "l.to_item AS b",
        ids=item_ids,
    )
    return {(row["id"], row["a"], row["m"], row["b"]) for row in rows}


def item_aliases(driver, world_id: str) -> dict[str, tuple[str, str]]:
    rows = read(
        driver,
        world_id,
        "MATCH (a:Alias {target_type: 'item'}) RETURN a.id AS id, a.phrase AS phrase, "
        "a.target_id AS target, a.status AS status",
    )
    return {row["id"]: (row["phrase"], row["target"], row["status"]) for row in rows}


def usable_aliases(driver, world_id: str) -> set[tuple[str, str]]:
    return {
        (phrase, target)
        for phrase, target, status in item_aliases(driver, world_id).values()
        if status != "retired"
    }


# correct --------------------------------------------------------------------------------------


def record_and_head(driver, w, chat) -> tuple[str, str]:
    record = make_item(
        driver,
        w,
        chat,
        2,
        kind="observation",
        title="Ilse said the launch dinner is at the Oak Room",
    )
    head = make_item(
        driver,
        w,
        chat,
        3,
        kind="plan",
        title="Launch dinner",
        summary="at the Oak Room",
        status="proposed",
        recheck_by=RECHECK,
    )
    return record, head


def test_correction_keeps_record_bytes_and_moves_head_in_one_commit(driver, seeded, chat):
    """D3: a correction leaves the record's claim byte-identical and moves the head in the same
    commit."""
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    record_before = props(driver, w, record)
    record_entries = entries_of(driver, w, record)
    ev = observation(chat, "e9", 9)
    receipt = apply(
        driver,
        w,
        [update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"), correct(record, head, "e9")],
        [ev],
    )
    # The record: every property byte for byte, touched_at and version included, and no entry.
    assert props(driver, w, record) == record_before
    assert entries_of(driver, w, record) == record_entries
    assert item_row(driver, w, head)["summary"] == "at the Birch Hall"
    [link] = successor_links(driver, w, record, MEANING_CORRECTED_BY)
    assert link["to_item"] == head and link["active_key"]
    assert link["became_true_precision"] == "instant"
    # One commit holds the head's move and the lineage.
    in_set = entries_of_set(driver, w, receipt.change_set_id)
    assert {entry["commit_id"] for entry in in_set} == {receipt.commit_id}
    assert {(entry["entity_id"], entry["field"]) for entry in in_set} >= {
        (head, "summary"),
        (link["id"], "row"),
    }
    found = standings(WorldQuery(driver, w), [record])[record]
    assert found.state == "overtaken"
    assert [s.item_id for s in found.successors] == [head]
    text = render_receipt(driver, w, receipt)
    assert "-[corrected by]->" in text and "summary=at the Birch Hall" in text


def test_correction_without_a_head_update_must_say_why(driver, seeded, chat):
    """B.2: head updates, or a stated reason for none."""
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    ev = observation(chat, "e9", 9)
    refusal = one(refused(driver, w, [correct(record, head, "e9")], [ev]), "invalid_arguments")
    assert refusal.rule == "head_update_required"
    receipt = apply(
        driver,
        w,
        [correct(record, head, "e9", no_head_update_reason="the head already says it")],
        [ev],
    )
    [link] = successor_links(driver, w, record, MEANING_CORRECTED_BY)
    fields = {entry["field"] for entry in entries_of(driver, w, link["id"])}
    assert fields == {"row", "no_head_update"}
    assert receipt.lines[0].symbol == ">"


def test_correction_with_a_new_head_minted_in_the_batch(driver, seeded, chat):
    """B.2: the successor exists or is minted in the batch."""
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 2, kind="observation", title="Tamsin runs the floor plan")
    ev = observation(chat, "e9", 9)
    head = create_item("person", "Tamsin Achebe", "e9", temp_id="$h")
    receipt = apply(driver, w, [head, correct(record, "$h", "e9")], [ev])
    new_head = created_id(receipt)
    assert standings(WorldQuery(driver, w), [record])[record].successors[0].item_id == new_head


def test_a_successor_link_never_takes_effect_in_the_future(driver, seeded, chat):
    """S46: a successor link with a future start would overtake its item before the newer truth
    existed, so the writer refuses it."""
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    ev = observation(chat, "e9", 9, at=T0 + timedelta(days=400))
    ops = [
        update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"),
        correct(record, head, "e9"),
    ]
    refusal = one(refused(driver, w, ops, [ev]), "invalid_arguments")
    # D5 rejects this evidence before the composite operation can use its time.
    assert refusal.rule == "future_evidence"
    assert successor_links(driver, w, record, MEANING_CORRECTED_BY) == []


def test_successor_clock_itself_refuses_a_future_instant():
    # Keep the D3 guard directly covered even though D5 now rejects the batch earlier.
    with pytest.raises(Refused) as caught:
        _successor_clock(SimpleNamespace(at=T0), T0 + timedelta(days=1))
    assert caught.value.rule == "successor_in_future"


def test_an_ended_record_is_not_corrected(driver, seeded, chat):
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    ev = observation(chat, "e9", 9)
    apply(driver, w, [refute(record, contradicts("e9"))], [ev], caller=PERSON)
    ev2 = observation(chat, "e10", 10)
    ops = [
        update_item(head, 1, {"summary": "at the Birch Hall"}, "e10"),
        correct(record, head, "e10"),
    ]
    one(refused(driver, w, ops, [ev2]), "ended_target")


# refute ---------------------------------------------------------------------------------------


def test_refute_ends_a_claim_as_never_true(driver, seeded, chat):
    """D3 end classes: refuted means never true, so no world end time is stored."""
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 2, kind="event", title="The venue walk-through took place")
    ev = observation(chat, "e9", 9, statement_class="report_happened")
    receipt = apply(driver, w, [refute(item, contradicts("e9"))], [ev])
    row = item_row(driver, w, item)
    assert row["end_class"] == "refuted"
    assert row.get("stopped_being_true_precision") is None
    assert row["version"] == 2
    assert standings(WorldQuery(driver, w), [item])[item].state == "ended"
    assert {e["polarity"] for e in evidence_rows(driver, w, item)} == {"supports", "contradicts"}
    assert "(refuted)" in render_receipt(driver, w, receipt)
    again = observation(chat, "e10", 10)
    one(refused(driver, w, [refute(item, contradicts("e10"))], [again]), "invalid_arguments")


def test_refute_a_link_removes_it_from_the_active_set(driver, seeded, chat):
    w = seeded["world_id"]
    first = make_item(driver, w, chat, 2, kind="place", title="Northgate market")
    second = make_item(driver, w, chat, 3, kind="place", title="Cedar Street depot")
    ev = observation(chat, "e4", 4)
    receipt = apply(driver, w, [create_link(first, "part of", second, "e4")], [ev])
    link = created_id(receipt, ">", "link")
    ev2 = observation(chat, "e9", 9)
    apply(driver, w, [refute(link, contradicts("e9"))], [ev2])
    [row] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link)
    assert row["l"]["end_class"] == "refuted" and row["l"].get("active_key") is None


def test_refute_needs_evidence_at_the_claims_rank_unless_the_person_says_it(driver, seeded, chat):
    """B.2: evidence at or above the claim's rank (D9); the person's own word outranks all."""
    w = seeded["world_id"]
    git = make_source(driver, w, "git", GIT_LINES)
    receipt = apply(
        driver,
        w,
        [create_item("event", "Venue list committed", "g1")],
        [git_observation(git, "g1")],
    )
    item = created_id(receipt)
    assert item_row(driver, w, item)["derived_source_type"] == "confirmed"
    said = observation(chat, "e9", 9, statement_class="report_happened")
    refusal = one(
        refused(driver, w, [refute(item, contradicts("e9"))], [said], caller=LEARNER),
        "invalid_arguments",
    )
    assert refusal.rule == "refute_rank"
    assert refusal.details == {"claim": "confirmed", "evidence": "reported_done"}
    apply(driver, w, [refute(item, contradicts("e9"))], [said], caller=PERSON)
    assert item_row(driver, w, item)["end_class"] == "refuted"


def test_a_successor_link_is_not_refuted_by_hand(driver, seeded, chat):
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    ev = observation(chat, "e9", 9)
    apply(
        driver,
        w,
        [update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"), correct(record, head, "e9")],
        [ev],
    )
    [link] = successor_links(driver, w, record, MEANING_CORRECTED_BY)
    ev2 = observation(chat, "e10", 10)
    refusal = one(
        refused(driver, w, [refute(link["id"], contradicts("e10"))], [ev2]), "unknown_meaning"
    )
    assert refusal.rule == "primitive_meaning"


# supersede ------------------------------------------------------------------------------------


def test_supersede_ends_the_old_and_keeps_lineage(driver, seeded, chat):
    """D3: supersede ends the old item as superseded and adds "superseded by" to its successor."""
    w = seeded["world_id"]
    old = make_item(driver, w, chat, 2, kind="decision", title="Launch venue decided: Oak Room")
    ev = observation(chat, "e9", 9)
    new = create_item("decision", "Birch Hall chosen instead", "e9", temp_id="$d")
    receipt = apply(driver, w, [new, supersede(old, "$d", "e9")], [ev])
    successor = created_id(receipt)
    row = item_row(driver, w, old)
    assert row["end_class"] == "superseded"
    assert row["stopped_being_true_precision"] == "before"
    [link] = successor_links(driver, w, old, MEANING_SUPERSEDED_BY)
    assert link["to_item"] == successor and link["active_key"]
    found = standings(WorldQuery(driver, w), [old])[old]
    assert found.state == "ended" and [s.item_id for s in found.successors] == [successor]
    assert "(superseded)" in render_receipt(driver, w, receipt)
    ev2 = observation(chat, "e10", 10)
    third = create_item("decision", "Pinewood barn picked at last", "e10", temp_id="$e")
    one(refused(driver, w, [third, supersede(old, "$e", "e10")], [ev2]), "invalid_arguments")


def test_supersede_takes_a_decision_of_the_record_class_and_keeps_its_claim(driver, seeded, chat):
    """S46, spec lines 796 and 822: a replaced decision gets "superseded by", and the starter set
    makes a decision a record-class kind, so supersede takes a record-class item. Its claim stays
    as it was; only its ending is written. An overtaken observation is corrected instead (the
    caller's choice: the code names no kind but the primitive ones)."""
    w = seeded["world_id"]
    [kind] = read(
        driver,
        w,
        "MATCH (k:Kind {name: 'decision'}) RETURN k.lifecycle_class AS lifecycle",
    )
    assert kind["lifecycle"] == "record"
    old = make_item(driver, w, chat, 2, kind="decision", title="Launch venue decided: Oak Room")
    before = props(driver, w, old)
    ev = observation(chat, "e9", 9)
    new = create_item("decision", "Birch Hall chosen instead", "e9", temp_id="$d")
    apply(driver, w, [new, supersede(old, "$d", "e9")], [ev])
    after = props(driver, w, old)
    claim = ("title", "summary", "properties", "kind", "became_true_precision", "recorded")
    assert {key: after.get(key) for key in claim} == {key: before.get(key) for key in claim}
    assert after["end_class"] == "superseded"


# merge, then undo -----------------------------------------------------------------------------


def merge_world(driver, w, chat) -> dict[str, str]:
    survivor = make_item(driver, w, chat, 2, kind="place", title="Oak Room")
    absorbed = make_item(driver, w, chat, 3, kind="place", title="the room above the bakery")
    market = make_item(driver, w, chat, 4, kind="place", title="Northgate market")
    person = make_item(driver, w, chat, 5, kind="person", title="Rowan Okonkwo")
    hall = make_item(driver, w, chat, 6, kind="place", title="Upstairs hall at Cedar Street")
    evs = [observation(chat, f"e{n}", n) for n in (7, 8)]
    receipt = apply(
        driver,
        w,
        [
            create_link(absorbed, "part of", market, "e7"),
            create_link(person, "related to", absorbed, "e8", recheck_by=RECHECK),
        ],
        evs,
    )
    # An earlier merge gives the absorbed item an item alias of its own.
    apply(driver, w, [merge(absorbed, hall)], [])
    return {
        "survivor": survivor,
        "absorbed": absorbed,
        "market": market,
        "person": person,
        "hall": hall,
        "part_of": created_id(receipt, ">", "link", 0),
        "related": created_id(receipt, ">", "link", 1),
    }


def test_merge_repoints_links_and_turns_names_into_aliases(driver, seeded, chat):
    """D3 merge: the survivor keeps the links with the same clocks, the other ends as superseded
    by it, and its names become item aliases."""
    w = seeded["world_id"]
    made = merge_world(driver, w, chat)
    survivor, absorbed = made["survivor"], made["absorbed"]
    receipt = apply(driver, w, [merge(survivor, absorbed)], [])
    assert item_row(driver, w, absorbed)["end_class"] == "superseded"
    [link] = successor_links(driver, w, absorbed, MEANING_SUPERSEDED_BY)
    assert link["to_item"] == survivor
    active = links_touching(driver, w, [survivor, absorbed])
    triples = {(a, m, b) for _, a, m, b in active}
    meanings = {m for _, _, m, _ in active}
    assert any(a == survivor and b == made["market"] for a, _, b in triples)
    assert any(a == made["person"] and b == survivor for a, _, b in triples)
    assert not any(absorbed in (a, b) and m not in (MEANING_SUPERSEDED_BY,) for a, m, b in triples)
    assert meanings
    old = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=made["part_of"])[
        0
    ]["l"]
    copy = next(
        row["l"]
        for row in read(
            driver,
            w,
            "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item = $s AND l.to_item = $m "
            "AND l.active_key IS NOT NULL RETURN l",
            s=survivor,
            m=made["market"],
        )
    )
    assert old["end_class"] == "superseded" and old.get("stopped_being_true_precision") is None
    for column in ("became_true_precision", "recorded", "provenance_evidence_id"):
        assert copy.get(column) == old.get(column)
    assert evidence_rows(driver, w, copy["id"])
    assert usable_aliases(driver, w) == {
        ("the room above the bakery", survivor),
        ("Upstairs hall at Cedar Street", survivor),
    }
    assert "merged into" in render_receipt(driver, w, receipt)


def test_merge_then_undo_restores_both_items_links_and_aliases(driver, seeded, chat):
    """D3 How to test it: a merge followed by an undo restores both items, their links and their
    aliases."""
    w = seeded["world_id"]
    made = merge_world(driver, w, chat)
    survivor, absorbed = made["survivor"], made["absorbed"]
    links_before = links_touching(driver, w, [survivor, absorbed])
    aliases_before = usable_aliases(driver, w)
    survivor_before = props(driver, w, survivor)
    merged = apply(driver, w, [merge(survivor, absorbed)], [])
    undone = apply(driver, w, [undo(merged.change_set_id)], [])
    assert item_row(driver, w, absorbed).get("end_class") is None
    found = standings(WorldQuery(driver, w), [absorbed, survivor])
    assert found[absorbed].state == "current" and found[absorbed].successors == ()
    assert found[survivor].state == "current"
    assert links_touching(driver, w, [survivor, absorbed]) == links_before
    assert usable_aliases(driver, w) == aliases_before
    after = props(driver, w, survivor)
    assert {k: v for k, v in after.items() if k != "touched_at"} == {
        k: v for k, v in survivor_before.items() if k != "touched_at"
    }
    # Both changes stay in the history: the merge's entries, and the undo's inverse entries.
    assert entries_of_set(driver, w, merged.change_set_id)
    inverse = entries_of_set(driver, w, undone.change_set_id)
    assert inverse and {entry["inverse_of"] for entry in inverse} == {merged.change_set_id}
    assert render_receipt(driver, w, undone).splitlines()[1].strip().startswith("u change set")


def test_merge_refuses_two_lifecycle_classes(driver, seeded, chat):
    w = seeded["world_id"]
    place = make_item(driver, w, chat, 2, kind="place", title="Oak Room")
    task = make_task(driver, w, chat, 3)
    refusal = one(refused(driver, w, [merge(place, task)], []), "invalid_arguments")
    assert refusal.rule == "merge_lifecycle"


def test_merge_refuses_a_link_that_does_not_fit_the_survivor(driver, seeded, chat):
    """B.2 merge: every link fitting the survivor."""
    w = seeded["world_id"]
    person = make_item(driver, w, chat, 2, kind="person", title="Rowan Okonkwo")
    organisation = make_item(driver, w, chat, 3, kind="organisation", title="Okafor Catering")
    place = make_item(driver, w, chat, 4, kind="place", title="Oak Room")
    ev = observation(chat, "e5", 5)
    apply(driver, w, [create_link(person, "lives in", place, "e5", recheck_by=RECHECK)], [ev])
    # A person and an organisation are both lasting things, and "lives in" takes only a person.
    refusal = one(refused(driver, w, [merge(organisation, person)], []), "endpoint_kind_mismatch")
    assert refusal.rule == "merge_link_fit"


# undo -----------------------------------------------------------------------------------------


def test_undo_of_an_automatic_change_takes_one_call_and_keeps_both(driver, seeded, chat):
    """D3 How to test it: undoing an automatic change takes one call and leaves both changes in
    the history (D9 builds the belief form on this undo)."""
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    before = item_row(driver, w, task)
    ev = observation(chat, "e9", 9)
    automatic = apply(
        driver,
        w,
        [
            update_item(task, 1, {"title": "Draft the venue list and the menu"}, "e9"),
            change_status(task, "in progress", "e9"),
        ],
        [ev],
        caller=LEARNER,
    )
    changed = entries_of_set(driver, w, automatic.change_set_id)
    receipt = apply(driver, w, [undo(automatic.change_set_id)], [])
    after = item_row(driver, w, task)
    for column in ("title", "status", "status_role", "status_since"):
        assert after.get(column) == before.get(column)
    assert after["version"] == 4
    assert entries_of_set(driver, w, automatic.change_set_id) == changed
    inverse = entries_of_set(driver, w, receipt.change_set_id)
    assert {entry["inverse_of"] for entry in inverse} == {automatic.change_set_id}
    refusal = one(refused(driver, w, [undo(automatic.change_set_id)], []), "invalid_arguments")
    assert refusal.rule == "already_undone"


def test_undo_conflicts_when_the_row_moved_since(driver, seeded, chat):
    """B.2 undo: versions still match, or the undo itself conflicts and is declined."""
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    evs = [observation(chat, "e9", 9), observation(chat, "e10", 10)]
    automatic = apply(
        driver, w, [update_item(task, 1, {"title": "Draft the venue list now"}, "e9")], evs[:1]
    )
    apply(driver, w, [update_item(task, 2, {"summary": "for the launch"}, "e10")], evs[1:])
    refusal = one(refused(driver, w, [undo(automatic.change_set_id)], []), "version_conflict")
    assert refusal.rule == "undo_conflict"


def test_a_later_correction_with_no_head_update_does_not_block_the_undo_of_the_first(
    driver, seeded, chat
):
    """B.2 line 3173: an undo is refused only when a row it moved has moved since. A correction
    that lands on the same successor link with no head update restates the link and moves no
    row (S47), so the first correction stays undoable (R4, round two)."""
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    first = apply(
        driver,
        w,
        [update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"), correct(record, head, "e9")],
        [observation(chat, "e9", 9)],
    )
    second = apply(
        driver,
        w,
        [correct(record, head, "e10", no_head_update_reason="the head already says it")],
        [observation(chat, "e10", 10)],
    )
    [link] = successor_links(driver, w, record, MEANING_CORRECTED_BY)

    apply(driver, w, [undo(first.change_set_id)], [])

    [after] = read(driver, w, "MATCH ()-[l:LINK {id: $id}]->() RETURN l", id=link["id"])
    assert after["l"]["end_class"] == "refuted" and not after["l"].get("active_key")
    assert item_row(driver, w, head)["summary"] == "at the Oak Room"
    assert entries_of_set(driver, w, first.change_set_id)
    assert entries_of_set(driver, w, second.change_set_id)
    entries, values = read_history(WorldQuery(driver, w))
    projection = rebuild(entries, values)
    live = live_fingerprint(driver, w)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live


def test_the_undo_and_the_rebuild_leave_out_the_same_markers():
    """S47: the markers that move no row add no version in a rebuild and block no undo."""
    assert STILL_MARKERS == _NO_VERSION


def test_undo_of_a_late_value_restores_the_as_of_read(driver, seeded, chat):
    """D3: an undo appends the inverse; a late value's inverse is a late value at its own world
    time, so an as-of read there sees what stood before."""
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1, title="Draft the venue list")
    newer = observation(chat, "e30", 30)
    apply(driver, w, [update_item(task, 1, {"title": "Draft the venue shortlist"}, "e30")], [newer])
    late = observation(chat, "e20", 20)
    automatic = apply(
        driver,
        w,
        [update_item(task, 2, {"title": "Draft the long venue list"}, "e20")],
        [late],
        caller=LEARNER,
    )
    query = WorldQuery(driver, w)
    assert item_as_of(query, task, at_minute(25))["title"] == "Draft the long venue list"
    apply(driver, w, [undo(automatic.change_set_id)], [])
    assert item_as_of(query, task, at_minute(25))["title"] == "Draft the venue list"
    assert item_row(driver, w, task)["title"] == "Draft the venue shortlist"


def test_undo_of_a_creation_refutes_what_it_made(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e9", 9)
    made = apply(driver, w, [create_item("person", "Ilse Varga", "e9")], [ev], caller=LEARNER)
    item = created_id(made)
    apply(driver, w, [undo(made.change_set_id)], [])
    assert item_row(driver, w, item)["end_class"] == "refuted"
    assert all(not row["active"] for row in evidence_rows(driver, w, item))
    assert current_items(WorldQuery(driver, w), [item]) == []


def test_undo_takes_only_the_person_or_the_writer(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e9", 9)
    made = apply(driver, w, [create_item("person", "Ilse Varga", "e9")], [ev])
    refusal = one(
        refused(driver, w, [undo(made.change_set_id)], [], caller=LEARNER), "scope_denied"
    )
    assert refusal.op == "undo"


def test_the_starter_set_is_not_undone(driver, seeded):
    w = seeded["world_id"]
    [row] = read(
        driver,
        w,
        "MATCH (e:ChangeEntry {actor: 'seed'}) RETURN e.change_set_id AS id LIMIT 1",
    )
    refusal = one(refused(driver, w, [undo(row["id"])], []), "invalid_arguments")
    assert refusal.rule == "not_undoable"


# retire_record --------------------------------------------------------------------------------


def test_retired_record_leaves_current_keeps_history_and_as_of(driver, seeded, chat):
    """D3 How to test it: a record retired as spent leaves current() but appears in history and
    in as-of reads before its retirement, with no stopped-being-true time."""
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 2, kind="observation", title="Rowan prefers morning slots")
    lesson = make_item(driver, w, chat, 3, kind="person", title="Rowan Okonkwo")
    query = WorldQuery(driver, w)
    assert current_items(query, [record]) == [record]
    receipt = apply(driver, w, [retire(record, lesson)], [], caller=CONNECTOR)
    row = item_row(driver, w, record)
    assert row["end_class"] == "spent" and row["lesson_in"] == lesson
    assert row.get("stopped_being_true_precision") is None
    assert current_items(query, [record]) == []
    assert all(e["active"] for e in evidence_rows(driver, w, record))
    ending = [e for e in entries_of(driver, w, record) if e["field"] == "end_class"]
    assert len(ending) == 1 and ending[0].get("world_time") is None
    # On the world axis the claim holds at every time: spent is a record-axis ending.
    then = item_as_of(query, record, at_minute(60))
    assert then is not None and then.get("end_class") is None
    assert "(spent; lesson in" in render_receipt(driver, w, receipt)


def test_retire_record_takes_only_a_record_and_the_consolidation_agent(driver, seeded, chat):
    w = seeded["world_id"]
    person = make_item(driver, w, chat, 2, kind="person", title="Rowan Okonkwo")
    other = make_item(driver, w, chat, 3, kind="person", title="Ilse Varga")
    refusal = one(
        refused(driver, w, [retire(person, other)], [], caller=CONNECTOR), "invalid_arguments"
    )
    assert refusal.rule == "not_a_record"
    record = make_item(driver, w, chat, 4, kind="observation", title="Rowan prefers morning slots")
    one(refused(driver, w, [retire(record, other)], [], caller=PERSON), "scope_denied")


# current() ------------------------------------------------------------------------------------


def test_a_planned_trip_with_an_unknown_start_reads_planned(driver, seeded):
    """D3 How to test it: a planned trip with an unknown start is returned as planned, never by
    current()."""
    w = seeded["world_id"]
    calendar = make_source(driver, w, "calendar", ["Trip to the Lisbon fair, dates to confirm"])
    ev = observation(calendar, "c1", 1, statement_class="intention", locator="whole")
    trip = created_id(
        apply(driver, w, [create_item("trip", "Trip to the Lisbon fair", "c1")], [ev])
    )
    query = WorldQuery(driver, w)
    assert standings(query, [trip])[trip].state == "planned"
    assert current_items(query, [trip]) == []


def test_clock_value_keeps_the_words_id_for_a_rebuild(driver, seeded, chat):
    """S47: a clock entry holds the value id of its words, so a rebuild restores the column."""
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e9", 9)
    apply(
        driver,
        w,
        [update_item(task, 1, {"became_true": clock_at(at_minute(1), words="this morning")}, "e9")],
        [ev],
    )
    row = item_row(driver, w, task)
    [entry] = [e for e in entries_of(driver, w, task) if e["field"] == "became_true"]
    [value] = read(
        driver, w, "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS v", id=entry["new_value_id"]
    )
    assert f'"words_id": "{row["became_true_words"]}"' in value["v"]
