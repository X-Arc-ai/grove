"""D3's restore drill at the writer: the rebuild path, the full restore, and the forget ledger.

Each test follows one sentence of D3's "How to test it". The worlds hold a mixed history written
through the writer's own operations; a last-confirmed time is planted as a change entry and a
column with raw Cypher, because the verify lane that writes it is not built yet. Every name is
invented.
"""

import json
from datetime import timedelta
from typing import Any

import pytest

from grove import clock
from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.d3_fingerprint import (
    fingerprint,
    live_fingerprint,
    live_parts,
    projection_rows,
    read_once,
)
from grove.store.query import WorldQuery
from grove.store.replay import ReplayRefused, read_history, rebuild
from grove.writer.restore import (
    FORGOTTEN_MARKER,
    RestoreMismatch,
    WorldNotEmpty,
    load_snapshot,
    main,
    rebuild_in_place,
    replay_ledger,
    restore,
    take_snapshot,
)
from grove.writer.testing import plant_node, purge_test_world
from tests.writer.helpers import (
    T0,
    apply,
    at_minute,
    clock_at,
    count_rows,
    create_item,
    create_link,
    iso,
    make_source,
    new_op_id,
    observation,
    raw,
    read,
    supports,
    world_dump,
)

RECHECK = iso(T0 + timedelta(days=30))
CONFIRMED = T0 + timedelta(minutes=50)


# --- builders ---------------------------------------------------------------------------------


def op(name: str, **fields: Any) -> dict[str, Any]:
    return {"op": name, "op_id": new_op_id(), **fields}


def update(item_id: str, version: int, fields: dict[str, Any], ev_id: str) -> dict[str, Any]:
    return op(
        "update_item",
        item=item_id,
        version=version,
        fields=fields,
        evidence_refs=supports(ev_id),
        reason="the message states it",
    )


def ids_by_title(driver, world_id: str) -> dict[str, str]:
    rows = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS title, i.id AS id")
    return {row["title"]: row["id"] for row in rows}


def link_id(driver, world_id: str, src: str, meaning: str, dst: str) -> str:
    (row,) = read(
        driver,
        world_id,
        "MATCH (:Item {id: $a})-[l:LINK {meaning: $m}]->(:Item {id: $b}) RETURN l.id AS id",
        a=src,
        m=meaning,
        b=dst,
    )
    return row["id"]


def plant_entry(
    driver,
    world_id: str,
    *,
    entity_type: str,
    entity_id: str,
    field: str,
    new: Any,
    old: Any = None,
    world_time=None,
) -> int:
    """One change entry with its values, as the writer would append it: the next sequence of the
    world, a change set and a reason of its own. Returns the sequence."""
    at = clock.now(world_id)
    with driver.session(database=DATABASE) as session:
        sequence = session.run(
            "CYPHER 25 MATCH (w:World {id: $w}) SET w.next_sequence = w.next_sequence + 1 "
            "RETURN w.next_sequence AS s",
            w=world_id,
        ).single()["s"]

        def value(text: str) -> str:
            value_id = new_id(world_id)
            session.run(
                "CYPHER 25 CREATE (v:HistoryValue) SET v = $row",
                row={"id": value_id, "world_id": world_id, "value": text},
            ).consume()
            return value_id

        entry = {
            "sequence": sequence,
            "world_id": world_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field": field,
            "new_value_id": value(json.dumps(new, sort_keys=True)),
            "recorded_at": at,
            "reason": value("planted by the test"),
            "actor": "person:t",
            "commit_id": new_id(world_id),
            "change_set_id": new_id(world_id),
            "history_only": False,
        }
        if old is not None:
            entry["old_value_id"] = value(json.dumps(old, sort_keys=True))
        if world_time is not None:
            entry["world_time"] = world_time
        session.run("CYPHER 25 CREATE (e:ChangeEntry) SET e = $row", row=entry).consume()
    return sequence


def plant_last_confirmed(driver, world_id: str, item_id: str) -> None:
    """What verify will write: an entry for last_confirmed, the column, and one version bump."""
    plant_entry(
        driver,
        world_id,
        entity_type="item",
        entity_id=item_id,
        field="last_confirmed",
        new=iso(CONFIRMED),
        world_time=CONFIRMED,
    )
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (i:Item {world_id: $w, id: $id}) "
            "SET i.last_confirmed = $at, i.version = i.version + 1",
            w=world_id,
            id=item_id,
            at=CONFIRMED,
        ).consume()


def purge_world_rows(driver, world_id: str) -> None:
    """Every node of the world goes, and the :World row stays: the scratch store of a restore."""
    with driver.session(database=DATABASE) as session:
        session.run("CYPHER 25 MATCH (n {world_id: $w}) DETACH DELETE n", w=world_id).consume()


def world_rows(driver, world_id: str) -> int:
    return raw(driver, "MATCH (n {world_id: $w}) RETURN count(n) AS c", w=world_id)[0]["c"]


def next_sequence(driver, world_id: str) -> int:
    rows = raw(driver, "MATCH (w:World {id: $w}) RETURN w.next_sequence AS s", w=world_id)
    return rows[0]["s"]


def plant_ledger(driver, world_id: str, target_type: str, internal_ids: list[str]) -> dict:
    return plant_node(
        driver,
        world_id,
        "ForgetLedger",
        target_type=target_type,
        internal_ids=internal_ids,
        requested_by="person:t",
        approved_at=clock.now(world_id),
        backups_clear_after=clock.today(world_id) + timedelta(days=30),
    )


def last_confirmed(driver, world_id: str) -> dict[str, Any]:
    rows = read(driver, world_id, "MATCH (i:Item) RETURN i.id AS id, i.last_confirmed AS at")
    return {row["id"]: row["at"] for row in rows}


def change_entries(driver, world_id: str) -> list[dict[str, Any]]:
    rows = raw(
        driver,
        "MATCH (e:ChangeEntry {world_id: $w}) RETURN properties(e) AS p ORDER BY e.sequence",
        w=world_id,
    )
    return [row["p"] for row in rows]


def history_values(driver, world_id: str) -> dict[str, dict[str, Any]]:
    rows = raw(
        driver, "MATCH (v:HistoryValue {world_id: $w}) RETURN properties(v) AS p", w=world_id
    )
    return {row["p"]["id"]: row["p"] for row in rows}


# --- the mixed history -------------------------------------------------------------------------


@pytest.fixture
def mixed(driver, seeded, chat) -> dict[str, Any]:
    """Every kind of change the writer makes today, on a handful of items and links.

    A task is created, then updated twice in one batch, moved to a new status, and updated with
    older evidence (history only). A place is ended and reopened. A slot link is ended by a newer
    one. A link is restated from a second source, and that evidence row is ended. A link is ended
    with a past stop clock. Verify's last-confirmed time is planted on the task."""
    world_id = seeded["world_id"]
    meanings = seeded["meanings"]
    work = {"recheck_by": RECHECK}
    apply(
        driver,
        world_id,
        [
            create_item("task", "Book the venue", "e0", status="open", **work),
            create_item("plan", "Spring reunion", "e0", status="proposed", **work),
            create_item("plan", "Autumn picnic", "e0", status="proposed", **work),
            create_item("place", "Harbour flat", "e0"),
            create_item(
                "person",
                "Rowan Okonkwo",
                "e0",
                became_true=clock_at(at_minute(1), words="this morning"),
            ),
        ],
        [observation(chat, "e0", 10)],
    )
    ids = ids_by_title(driver, world_id)
    venue, reunion, picnic = ids["Book the venue"], ids["Spring reunion"], ids["Autumn picnic"]
    harbour = ids["Harbour flat"]

    apply(
        driver,
        world_id,
        [
            update(venue, 1, {"summary": "Ilse asks for a hall near the water"}, "e1"),
            update(venue, 2, {"title": "Book the harbour venue"}, "e1"),
        ],
        [observation(chat, "e1", 20)],
    )
    status = op(
        "change_status",
        item=venue,
        status="in progress",
        evidence_refs=supports("e2"),
        reason="the message says the booking started",
    )
    apply(driver, world_id, [status], [observation(chat, "e2", 30)])
    late = update(venue, 4, {"summary": "Ilse asked for a garden first"}, "e3")
    apply(driver, world_id, [late], [observation(chat, "e3", 15)])

    ended = op(
        "end_item",
        item=harbour,
        end_class="ended_in_the_world",
        stopped_being_true=clock_at(at_minute(35)),
        evidence_refs=supports("e4"),
        reason="the flat was let go",
    )
    apply(driver, world_id, [ended], [observation(chat, "e4", 36)])
    reopened = op(
        "reopen_item", item=harbour, evidence_refs=supports("e5"), reason="the flat is back"
    )
    apply(driver, world_id, [reopened], [observation(chat, "e5", 38)])

    first = create_link(venue, "step of", reunion, "e6", became_true=clock_at(at_minute(12)))
    apply(driver, world_id, [first], [observation(chat, "e6", 12)])
    second = create_link(venue, "step of", picnic, "e7", became_true=clock_at(at_minute(25)))
    apply(driver, world_id, [second], [observation(chat, "e7", 25)])
    apply(
        driver,
        world_id,
        [
            create_link(venue, "part of", reunion, "e8"),
            create_link(picnic, "part of", reunion, "e8"),
        ],
        [observation(chat, "e8", 13)],
    )
    other = make_source(driver, world_id, "whatsapp", ["a second invented export", "it holds"])
    restated = create_link(venue, "part of", reunion, "e9")
    apply(driver, world_id, [restated], [observation(other, "e9", 2)])

    part_of = meanings["part of"]
    venue_part = link_id(driver, world_id, venue, part_of, reunion)
    (row,) = read(
        driver,
        world_id,
        "MATCH (e:Evidence {target_id: $id, source_id: $s}) RETURN e.id AS id",
        id=venue_part,
        s=other["source_id"],
    )
    apply(
        driver,
        world_id,
        [op("end_evidence", evidence=row["id"], end_reason="correction", reason="misread")],
        [],
    )
    picnic_part = link_id(driver, world_id, picnic, part_of, reunion)
    end_link = op(
        "end_link",
        link=picnic_part,
        end_class="ended_in_the_world",
        stopped_being_true=clock_at(at_minute(40)),
        evidence_refs=supports("e10"),
        reason="the picnic was folded into the reunion",
    )
    apply(driver, world_id, [end_link], [observation(chat, "e10", 40, at=at_minute(41))])
    plant_last_confirmed(driver, world_id, venue)
    return {
        "world_id": world_id,
        "venue": venue,
        "reunion": reunion,
        "picnic": picnic,
        "harbour": harbour,
        "first_step": link_id(driver, world_id, venue, meanings["step of"], reunion),
        "picnic_part": picnic_part,
        "ended_evidence": row["id"],
    }


# --- the rebuild path ---------------------------------------------------------------------------


def test_rebuild_fingerprint_equals_live(driver, mixed):
    world_id = mixed["world_id"]
    query = WorldQuery(driver, world_id)
    live = live_fingerprint(driver, world_id)
    entries, values = read_history(query)
    projection = rebuild(entries, values)

    assert fingerprint(projection.rows, projection.history, live["tables"]) == live

    # Every row, every column but touched_at: the version, the clocks, the end classes, the
    # claim fields from the evidence, the active keys, the words the rows were written with.
    held = projection_rows(query)
    for table, rows in held.items():
        assert set(projection.rows[table]) == set(rows), table
        for row_id, row in rows.items():
            rebuilt = {k: v for k, v in projection.rows[table][row_id].items() if k != "touched_at"}
            assert rebuilt == {k: v for k, v in row.items() if k != "touched_at"}, (table, row_id)

    venue = projection.items[mixed["venue"]]
    # create, two updates in one batch, a status change, the planted verify; the history-only
    # update moved nothing.
    assert venue["version"] == held["items"][mixed["venue"]]["version"] == 5
    assert venue["last_confirmed"] == held["items"][mixed["venue"]]["last_confirmed"] == CONFIRMED
    assert venue["summary"] == "Ilse asks for a hall near the water"

    # Every clock and every ended link is in the fingerprint.
    parts = read_once(driver, world_id, live_parts)["projection"]
    first = parts["links"][mixed["first_step"]]
    assert first["end_class"] == "superseded"
    assert first["stopped_being_true_earliest"] == iso(at_minute(25))
    ended = parts["links"][mixed["picnic_part"]]
    assert ended["end_class"] == "ended_in_the_world"
    assert ended["stopped_being_true_latest"] == iso(at_minute(40))
    assert parts["items"][mixed["venue"]]["last_confirmed"] == iso(CONFIRMED)
    assert parts["evidence"][mixed["ended_evidence"]]["active"] is False
    harbour = parts["items"][mixed["harbour"]]
    assert "end_class" not in harbour and "stopped_being_true_precision" not in harbour


def test_rebuild_in_place_keeps_the_fingerprint_and_appends_nothing(driver, mixed, capsys):
    world_id = mixed["world_id"]
    live = live_fingerprint(driver, world_id)
    entries = change_entries(driver, world_id)
    sequence = next_sequence(driver, world_id)
    confirmed = last_confirmed(driver, world_id)

    rebuild_in_place(driver, world_id)

    assert live_fingerprint(driver, world_id) == live
    assert change_entries(driver, world_id) == entries
    assert next_sequence(driver, world_id) == sequence
    assert last_confirmed(driver, world_id) == confirmed

    assert main(["fingerprint", "--world", world_id]) == 0
    assert json.loads(capsys.readouterr().out) == live
    assert main(["rebuild", "--world", world_id, "--expect", live["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["match"] is True
    assert main(["rebuild", "--world", world_id, "--expect", "0" * 64]) == 3
    assert json.loads(capsys.readouterr().out)["match"] is False


# --- a history the writer could not have written is refused -----------------------------------


@pytest.fixture
def small(driver, seeded, chat) -> dict[str, Any]:
    world_id = seeded["world_id"]
    apply(
        driver,
        world_id,
        [create_item("place", "Hillside cottage", "e0")],
        [observation(chat, "e0", 10)],
    )
    return {
        "world_id": world_id,
        **seeded,
        "cottage": ids_by_title(driver, world_id)["Hillside cottage"],
    }


def plant_old_value(driver, small) -> int:
    return plant_entry(
        driver,
        small["world_id"],
        entity_type="item",
        entity_id=small["cottage"],
        field="title",
        old="A title the cottage never had",
        new="Hillside cottage by the lake",
        world_time=at_minute(50),
    )


def plant_unknown_kind(driver, small) -> int:
    world_id = small["world_id"]
    item_id = new_id(world_id)
    row = {"id": item_id, "world_id": world_id, "kind": new_id(world_id), "title": "Silver hull"}
    return plant_entry(
        driver,
        world_id,
        entity_type="item",
        entity_id=item_id,
        field="row",
        new={"table": "items", "row": {**row, "recorded": iso(T0), "version": 1}},
        world_time=T0,
    )


def plant_missing_end(driver, small) -> int:
    world_id = small["world_id"]
    link = new_id(world_id)
    row = {
        "id": link,
        "world_id": world_id,
        "from_item": small["cottage"],
        "meaning": small["meanings"]["part of"],
        "to_item": new_id(world_id),
        "became_true_precision": "unknown",
        "recorded": iso(T0),
        "version": 1,
    }
    return plant_entry(
        driver,
        world_id,
        entity_type="link",
        entity_id=link,
        field="row",
        new={"table": "links", "row": row},
        world_time=T0,
    )


@pytest.mark.parametrize(
    ("plant", "rule"),
    [
        (plant_old_value, "old_value"),
        (plant_unknown_kind, "unknown_kind"),
        (plant_missing_end, "missing_end"),
    ],
    ids=["an old value that does not match", "an unknown kind", "a link to a missing item"],
)
def test_rebuild_refuses_an_inconsistent_history(driver, small, plant, rule, capsys):
    world_id = small["world_id"]
    sequence = plant(driver, small)
    before = world_dump(driver, world_id)

    with pytest.raises(ReplayRefused) as refused:
        rebuild_in_place(driver, world_id)

    assert (refused.value.sequence, refused.value.rule) == (sequence, rule)
    assert world_dump(driver, world_id) == before
    assert main(["rebuild", "--world", world_id]) == 4
    assert json.loads(capsys.readouterr().err) == {"refused": {"rule": rule, "sequence": sequence}}
    assert world_dump(driver, world_id) == before


# --- the full restore -------------------------------------------------------------------------


def test_full_restore_matches_snapshot_fingerprint(driver, mixed, chat):
    world_id = mixed["world_id"]
    confirmed = last_confirmed(driver, world_id)
    snapshot = json.loads(json.dumps(take_snapshot(driver, world_id)))
    recorded = snapshot["fingerprint"]
    assert recorded == live_fingerprint(driver, world_id)
    # The recorded sizes are the store's own, read here without the fingerprint's code (R4).
    for label, name in (("Source", "sources"), ("HistoryValue", "history_values")):
        (size,) = read(
            driver, world_id, f"MATCH (n:{label}) RETURN count(n) AS count, max(n.id) AS newest"
        )
        assert size["count"] > 0
        assert recorded["tables"][name] == {"count": size["count"], "newest": size["newest"]}

    # Writes after the snapshot, which the restore does not bring back.
    apply(
        driver,
        world_id,
        [create_item("person", "Ilse Tamsin", "e20")],
        [observation(chat, "e20", 39)],
    )
    # Forgets after the snapshot: a value a projection column holds by id, the text a column
    # holds itself (the venue's title), and a whole item.
    (venue,) = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=mixed["venue"])
    reason_id = venue["i"]["provenance_reason"]
    (title,) = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {entity_id: $id, field: 'title'}) RETURN e.new_value_id AS id",
        id=mixed["venue"],
    )
    plant_ledger(driver, world_id, "field_value", [reason_id, title["id"]])
    plant_ledger(driver, world_id, "item", [mixed["picnic"]])
    ledger = [row["f"] for row in read(driver, world_id, "MATCH (f:ForgetLedger) RETURN f")]
    assert len(ledger) == 2

    purge_world_rows(driver, world_id)
    assert world_rows(driver, world_id) == 0

    result = restore(driver, world_id, snapshot, ledger)

    forgotten = set(result["forgotten"])
    assert {mixed["picnic"], mixed["picnic_part"]} <= forgotten
    history = {"last_sequence": recorded["last_sequence"], "entries": recorded["entries"]}
    expected = fingerprint(snapshot["projection"], history, recorded["tables"], forgotten=forgotten)
    assert result["fingerprint"] == expected["fingerprint"]
    assert result["fingerprint"] != recorded["fingerprint"]
    assert (
        live_fingerprint(driver, world_id, forgotten=forgotten)["fingerprint"]
        == (expected["fingerprint"])
    )
    # Every snapshot table, count and newest id, is as the snapshot holds it.
    assert result["tables"] == recorded["tables"]
    assert (result["last_sequence"], result["entries"]) == (
        recorded["last_sequence"],
        recorded["entries"],
    )
    assert next_sequence(driver, world_id) == recorded["last_sequence"]

    values = history_values(driver, world_id)
    (by_value,) = [row for row in ledger if row["target_type"] == "field_value"]
    assert values[reason_id]["value"] == FORGOTTEN_MARKER
    assert values[reason_id]["forgotten_at"] == by_value["approved_at"]
    picnic_entries = [
        e for e in change_entries(driver, world_id) if e["entity_id"] == mixed["picnic"]
    ]
    assert picnic_entries
    assert {values[e["new_value_id"]]["value"] for e in picnic_entries} == {FORGOTTEN_MARKER}
    titles = ids_by_title(driver, world_id)
    assert "Autumn picnic" not in titles and "Ilse Tamsin" not in titles
    # The forgotten title is absent: the value reads the marker, and so does the column.
    assert values[title["id"]]["value"] == FORGOTTEN_MARKER
    assert titles[FORGOTTEN_MARKER] == mixed["venue"]
    held = projection_rows(WorldQuery(driver, world_id))
    assert "Book the harbour venue" not in json.dumps(held, default=str)

    # Neither path moves a last-confirmed time.
    after = last_confirmed(driver, world_id)
    assert after == {key: value for key, value in confirmed.items() if key not in forgotten}
    assert after[mixed["venue"]] == CONFIRMED
    kept = read(driver, world_id, "MATCH (f:ForgetLedger) RETURN f.id AS id")
    assert {row["id"] for row in kept} == {row["id"] for row in ledger}

    # The test world's purge still removes all of it.
    purge_test_world(driver, world_id)
    assert world_rows(driver, world_id) == 0
    assert raw(driver, "MATCH (w:World {id: $w}) RETURN count(w) AS c", w=world_id)[0]["c"] == 0


def test_load_refuses_a_world_that_holds_rows(driver, mixed):
    world_id = mixed["world_id"]
    snapshot = take_snapshot(driver, world_id)
    before = world_dump(driver, world_id)
    with pytest.raises(WorldNotEmpty):
        load_snapshot(driver, world_id, snapshot)
    assert world_dump(driver, world_id) == before


def tamper_fingerprint(snapshot: dict, mixed: dict) -> None:
    snapshot["fingerprint"]["fingerprint"] = "0" * 64


def tamper_entry(snapshot: dict, mixed: dict) -> None:
    """The evidence ending now names another active row of the same world."""
    (ending,) = [
        row
        for row in snapshot["rows"]["change_entries"]
        if row["properties"]["field"] == "end_reason"
    ]
    other = next(
        key
        for key, part in sorted(snapshot["projection"]["evidence"].items())
        if part.get("active") and key != mixed["ended_evidence"]
    )
    ending["properties"]["entity_id"] = other
    mixed["tampered_to"] = other


@pytest.mark.parametrize("tamper", [tamper_fingerprint, tamper_entry], ids=["fingerprint", "entry"])
def test_restore_refuses_a_mismatch(driver, mixed, tamper):
    world_id = mixed["world_id"]
    snapshot = json.loads(json.dumps(take_snapshot(driver, world_id)))
    tamper(snapshot, mixed)
    purge_world_rows(driver, world_id)
    sequence = next_sequence(driver, world_id)

    with pytest.raises(RestoreMismatch) as caught:
        restore(driver, world_id, snapshot, [])

    if tamper is tamper_fingerprint:
        # Refused before any write: the world still holds no row, and its sequence is as it was.
        assert world_rows(driver, world_id) == 0
        assert next_sequence(driver, world_id) == sequence
        assert "nothing was loaded" in caught.value.problems[0]
        return
    # Refused after the restore: the store holds the loaded snapshot and the rebuilt projection,
    # for a person to read, and it does not go live.
    assert count_rows(driver, world_id)["ChangeEntry"] == snapshot["fingerprint"]["entries"]
    assert next_sequence(driver, world_id) == snapshot["fingerprint"]["last_sequence"]
    rows = read(driver, world_id, "MATCH (e:Evidence) RETURN e.id AS id, e.active AS active")
    active = {row["id"]: row["active"] for row in rows}
    assert active[mixed["ended_evidence"]] is True
    assert active[mixed["tampered_to"]] is False
    problems = " ".join(caught.value.problems)
    assert mixed["ended_evidence"] in problems and mixed["tampered_to"] in problems
    assert caught.value.found["fingerprint"] != caught.value.expected["fingerprint"]


def _newest_source(snapshot: dict) -> dict:
    return max(snapshot["rows"]["sources"], key=lambda row: row["properties"]["id"])


def lose_a_source(snapshot: dict) -> str:
    """The dump lost a row of a snapshot-only table: one source fewer."""
    count = len(snapshot["rows"]["sources"])
    snapshot["rows"]["sources"].remove(_newest_source(snapshot))
    return f"sources: {count - 1} rows, the snapshot holds {count}"


def swap_a_source_id(snapshot: dict) -> str:
    """A row of a snapshot-only table has another id, with the count as it was."""
    _newest_source(snapshot)["properties"]["id"] = "00000000-0000-7000-8000-000000000001"
    return "sources: the newest id differs"


@pytest.mark.parametrize("tamper", [lose_a_source, swap_a_source_id], ids=["lost", "swapped"])
def test_restore_refuses_a_snapshot_table_that_differs(driver, mixed, tamper):
    """D3: every snapshot-only table's count, and its newest id, matches after the restore. The
    rebuild never reads these tables, so only the live read of their sizes can catch a dump that
    lost or changed one of their rows (R4, round two)."""
    world_id = mixed["world_id"]
    snapshot = json.loads(json.dumps(take_snapshot(driver, world_id)))
    expected = tamper(snapshot)
    purge_world_rows(driver, world_id)

    with pytest.raises(RestoreMismatch) as caught:
        restore(driver, world_id, snapshot, [])

    assert caught.value.problems == [expected]


# --- the forget ledger --------------------------------------------------------------------------


def test_a_ledger_row_that_names_a_row_value_is_refused_and_marks_nothing(driver, mixed):
    """A create entry's value holds the whole row. A forget of one value that names it would
    remove the row, and the replay would then refuse the row's evidence and links. So the ledger
    replay refuses it by name (rule row_value) before it marks any value, also the other values
    of the same ledger, and the world stays as it was (R4, round two). Which column of a create
    value a forget reaches is the forget lane's design."""
    world_id = mixed["world_id"]
    (created,) = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {entity_id: $id, field: 'row'}) "
        "RETURN e.new_value_id AS id, e.sequence AS sequence",
        id=mixed["venue"],
    )
    (venue,) = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=mixed["venue"])
    reason_id = venue["i"]["provenance_reason"]
    values = history_values(driver, world_id)
    held = projection_rows(WorldQuery(driver, world_id))
    approved = clock.now(world_id)
    ledger = [
        {"target_type": "field_value", "internal_ids": [reason_id], "approved_at": approved},
        {"target_type": "named_value", "internal_ids": [created["id"]], "approved_at": approved},
    ]

    with pytest.raises(ReplayRefused) as caught:
        replay_ledger(driver, world_id, ledger)

    assert (caught.value.rule, caught.value.sequence) == ("row_value", created["sequence"])
    assert history_values(driver, world_id) == values
    assert projection_rows(WorldQuery(driver, world_id)) == held


def test_ledger_replay_changes_values_not_entries(driver, mixed):
    world_id = mixed["world_id"]
    entries = change_entries(driver, world_id)
    values = history_values(driver, world_id)
    (venue,) = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=mixed["venue"])
    reason_id = venue["i"]["provenance_reason"]
    approved = clock.now(world_id)
    ledger = [
        {"target_type": "field_value", "internal_ids": [reason_id], "approved_at": approved},
        {"target_type": "item", "internal_ids": [mixed["picnic"]], "approved_at": approved},
    ]

    forgotten = replay_ledger(driver, world_id, ledger)

    assert change_entries(driver, world_id) == entries
    after = history_values(driver, world_id)
    assert set(after) == set(values)
    changed = {key for key in values if after[key] != values[key]}
    assert reason_id in changed
    for key in changed:
        assert after[key]["value"] == FORGOTTEN_MARKER
        assert after[key]["forgotten_at"] == approved
    picnic_values = {
        entry[name]
        for entry in entries
        if entry["entity_id"] in forgotten
        for name in ("new_value_id", "old_value_id", "reason")
        if entry.get(name)
    }
    assert picnic_values <= changed
    assert {mixed["picnic"], mixed["picnic_part"]} <= forgotten
    held = projection_rows(WorldQuery(driver, world_id))
    assert not forgotten & {key for rows in held.values() for key in rows}

    # A later rebuild keeps the forget: the forgotten rows read the marker, so none comes back.
    fingerprint_after = live_fingerprint(driver, world_id)
    rebuild_in_place(driver, world_id)
    assert live_fingerprint(driver, world_id) == fingerprint_after
    assert change_entries(driver, world_id) == entries
