"""One batch end to end: two items, a link and evidence, committed and read back as a receipt."""

from datetime import timedelta

from grove.store.asof import item_as_of
from grove.store.query import WorldQuery
from grove.writer import (
    Receipt,
    record_connector,
    record_episode,
    record_source,
    render_receipt,
    short_id,
)
from tests.writer.helpers import (
    T0,
    apply,
    at_minute,
    clock_at,
    create_item,
    create_link,
    iso,
    new_op_id,
    observation,
    read,
    supports,
)

RECHECK = iso(T0 + timedelta(days=14))


def test_one_batch_writes_items_link_and_evidence_and_reads_back_its_receipt(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item(
            "task",
            "Book the harbour hall for Tamsin",
            "e1",
            temp_id="$hall",
            status="open",
            recheck_by=RECHECK,
            became_true=clock_at(at_minute(2), words="this morning"),
        ),
        create_item("person", "Tamsin Okonkwo", "e1", temp_id="$tamsin"),
        create_link("$hall", "assigned to", "$tamsin", "e2", recheck_by=RECHECK),
    ]
    evidence = [observation(chat, "e1", 2), observation(chat, "e2", 3)]

    receipt = apply(driver, world_id, ops, evidence, request_id="req-smoke-1")

    assert receipt.state == "saved"
    assert receipt.request_id == "req-smoke-1"
    assert [(line.symbol, line.entity_type) for line in receipt.lines] == [
        ("+", "item"),
        ("+", "item"),
        (">", "link"),
    ]
    hall, tamsin = receipt.lines[0].entity_id, receipt.lines[1].entity_id
    link_id = receipt.lines[2].entity_id

    items = {row["i"]["id"]: row["i"] for row in read(driver, world_id, "MATCH (i:Item) RETURN i")}
    assert items[hall]["title"] == "Book the harbour hall for Tamsin"
    assert items[hall]["status"] == "open"
    assert items[hall]["status_role"] == "open"
    assert items[hall]["version"] == 1
    assert items[hall]["derived_source_type"] == "reported_done"
    assert items[hall]["derivation"] == "observed"
    assert items[hall]["kind"] == seeded["kinds"]["task"]
    assert items[tamsin]["kind"] == seeded["kinds"]["person"]

    (link,) = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK]->(b:Item) RETURN l, a.id AS a, b.id AS b",
    )
    assert (link["a"], link["b"]) == (hall, tamsin)
    assert link["l"]["id"] == link_id
    assert link["l"]["meaning"] == seeded["meanings"]["assigned to"]
    assert link["l"]["active_key"] == f"{world_id}|{hall}|{link['l']['meaning']}|{tamsin}"

    rows = read(driver, world_id, "MATCH (e:Evidence) RETURN e")
    targets = sorted((row["e"]["target_type"], row["e"]["target_id"]) for row in rows)
    assert targets == sorted([("item", hall), ("item", tamsin), ("link", link_id)])
    assert {row["e"]["request_id"] for row in rows} == {"req-smoke-1"}
    assert items[hall]["provenance_evidence_id"] in {row["e"]["id"] for row in rows}

    entries = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {change_set_id: $id}) RETURN c ORDER BY c.sequence",
        id=receipt.change_set_id,
    )
    assert {entry["c"]["commit_id"] for entry in entries} == {receipt.commit_id}
    assert {entry["c"]["actor"] for entry in entries} == {"person:t"}

    text = render_receipt(driver, world_id, receipt)
    lines = text.splitlines()
    assert lines[0] == (
        f"saved (3), commit {short_id(receipt.commit_id)}, request {short_id('req-smoke-1')}"
    )
    assert lines[1] == f"  + item {short_id(hall)} (task): Book the harbour hall for Tamsin"
    assert lines[2] == f"  + item {short_id(tamsin)} (person): Tamsin Okonkwo"
    assert lines[3] == (
        f"  > link {short_id(link_id)}: {short_id(hall)} -[assigned to]-> {short_id(tamsin)}"
    )
    assert Receipt.from_json(receipt.to_json()) == receipt


def test_a_second_batch_changes_status_attaches_evidence_and_replays_as_of(driver, seeded, chat):
    world_id = seeded["world_id"]
    first = apply(
        driver,
        world_id,
        [create_item("task", "Print the menus", "e1", status="open", recheck_by=RECHECK)],
        [observation(chat, "e1", 10)],
    )
    task = first.lines[0].entity_id
    status = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task,
        "status": "in progress",
        "evidence_refs": supports("e2"),
        "reason": "Ilse says she started",
    }
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": task,
        "ev_id": "e3",
        "polarity": "supports",
    }
    second = apply(
        driver, world_id, [status], [observation(chat, "e2", 20), observation(chat, "e3", 21)]
    )
    third = apply(driver, world_id, [attach], [observation(chat, "e3", 21)])

    assert [(line.symbol, line.field) for line in second.lines] == [("*", "status")]
    assert [(line.symbol, line.entity_id) for line in third.lines] == [("=", task)]
    shown = render_receipt(driver, world_id, second).splitlines()
    assert shown[1] == f"  * item {short_id(task)}: status=in progress"
    shown = render_receipt(driver, world_id, third).splitlines()
    assert shown[1].startswith(f"  = item {short_id(task)} evidence ")
    assert shown[1].endswith("(supports, reported_done)")

    query = WorldQuery(driver, world_id)
    assert item_as_of(query, task, at_minute(15))["status"] == "open"
    assert item_as_of(query, task, at_minute(25))["status"] == "in progress"
    assert item_as_of(query, task, at_minute(5)) is None


def test_end_reopen_and_end_evidence_render_their_lines(driver, seeded, chat):
    world_id = seeded["world_id"]
    made = apply(
        driver,
        world_id,
        [create_item("place", "Okonkwo boathouse", "e1")],
        [observation(chat, "e1", 4)],
    )
    place = made.lines[0].entity_id
    (row,) = read(driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=place)
    end = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": place,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(6)),
        "evidence_refs": supports("e2"),
        "reason": "Rowan says the boathouse closed",
    }
    ended = apply(driver, world_id, [end], [observation(chat, "e2", 6)])
    reopen = {"op": "reopen_item", "op_id": new_op_id(), "item": place, "reason": "it reopened"}
    reopened = apply(driver, world_id, [reopen], [])
    drop = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": row["e"]["id"],
        "end_reason": "correction",
    }
    dropped = apply(driver, world_id, [drop], [])

    s = short_id
    assert render_receipt(driver, world_id, ended).splitlines()[1:] == [
        f"  x item {s(place)} (ended in the world)"
    ]
    assert render_receipt(driver, world_id, reopened).splitlines()[1:] == [
        f"  ^ item {s(place)} reopened"
    ]
    assert render_receipt(driver, world_id, dropped).splitlines()[1:] == [
        f"  - evidence {s(row['e']['id'])} (correction)"
    ]
    (after,) = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=place)
    assert after["i"].get("end_class") is None
    assert after["i"].get("stopped_being_true_precision") is None
    assert after["i"]["version"] == 3


def test_a_later_cut_of_the_same_window_keeps_the_episode_id(driver, seeded):
    world_id = seeded["world_id"]
    connector = record_connector(driver, world_id, "whatsapp")
    first = record_source(
        driver,
        world_id,
        connector_id=connector,
        external_locator="export-a.txt",
        content="one\ntwo",
        parser_version="test-1",
    )
    second = record_source(
        driver,
        world_id,
        connector_id=connector,
        external_locator="export-b.txt",
        content="one\ntwo\nthree",
        parser_version="test-1",
    )
    window = {"connector_id": connector, "first_message_key": "k-1", "cut_rule": "chat_window_90m"}
    episode = record_episode(
        driver,
        world_id,
        source_ids=[first],
        last_message_key="k-2",
        start_time=T0,
        end_time=at_minute(2),
        **window,
    )
    again = record_episode(
        driver,
        world_id,
        source_ids=[second],
        last_message_key="k-3",
        start_time=T0,
        end_time=at_minute(3),
        **window,
    )
    assert again == episode
    (row,) = read(driver, world_id, "MATCH (e:Episode) RETURN e")
    assert row["e"]["source_ids"] == [first, second]
    assert row["e"]["last_message_key"] == "k-3"
    assert row["e"]["version"] == 2
