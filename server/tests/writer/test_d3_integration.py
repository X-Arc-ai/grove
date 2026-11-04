"""D3 across the writer, the reads and the restore: the "How to test it" lines that need all three.

The record axis needs record times that differ, so the timed tests run the writer under a test
clock (grove.clock.TestClock): every batch is recorded at the time the test sets. All names are
invented.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from grove import clock
from grove.store.asof import world_as_of
from grove.store.axes import AsOf
from grove.store.current import standings
from grove.store.d3_fingerprint import fingerprint, live_fingerprint, projection_rows
from grove.store.history_reads import item_history
from grove.store.primitives import MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY
from grove.store.query import WorldQuery
from grove.store.replay import (
    FORGOTTEN_MARKER,
    MEASURES,
    known_as_of,
    read_history,
    rebuild,
)
from grove.views.graph import Window, graph_payload, node_payload
from grove.writer.restore import main, rebuild_in_place, restore, take_snapshot
from grove.writer.worlds import create_world
from tests.writer.helpers import (
    CHAT_LINES,
    LEARNER,
    PERSON,
    apply,
    at_minute,
    clock_at,
    create_item,
    create_link,
    created_id,
    make_source,
    observation,
)
from tests.writer.test_contract_d2 import (
    GIT_LINES,
    RECHECK,
    change_status,
    git_observation,
    make_item,
    one,
    refused,
    update_item,
)
from tests.writer.test_contract_d3 import (
    CONNECTOR,
    contradicts,
    correct,
    entries_of,
    merge,
    record_and_head,
    refute,
    retire,
    successor_links,
    supersede,
    undo,
)
from tests.writer.test_ontology import (
    GODPARENT,
    godparent_link,
    judged,
    meaning_row,
    mint_godparent,
    people,
    propose,
)
from tests.writer.test_restore import (
    history_values,
    last_confirmed,
    plant_ledger,
    purge_world_rows,
)

# The columns a rebuild cannot hold: touched_at, and the D8 measures (S47).
KEPT = frozenset({"touched_at", *MEASURES})


def day(number: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, number, hour, tzinfo=UTC)


@contextmanager
def recorded_at(at: datetime) -> Iterator[clock.TestClock]:
    test_clock = clock.TestClock()
    test_clock.set(at)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def timed(driver, make_world):
    """A world seeded on Oct 21 00:00 under a test clock, with one chat source; a test moves the
    clock with `timed["clock"].set(...)`."""
    world_id = make_world()
    with recorded_at(day(21, 0)) as test_clock:
        create_world(driver, world_id, owner_user_id="u_t_owner")
        src = make_source(driver, world_id, "whatsapp", CHAT_LINES)
        yield {"world_id": world_id, "clock": test_clock, "src": src}


def shown(answer: AsOf) -> tuple[dict[str, tuple], set[str]]:
    """What a snapshot is compared on: per item its state, summary, status and successors; the
    ids of the links that stood."""
    items = {
        item_id: (
            found.standing.state,
            found.row.get("summary"),
            found.row.get("status"),
            tuple(after.item_id for after in found.standing.successors),
        )
        for item_id, found in answer.items.items()
    }
    return items, set(answer.links)


def successor_link(driver, world_id: str, from_item: str) -> str:
    [row] = WorldQuery(driver, world_id).read(
        "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.from_item = $id AND l.meaning IN $m "
        "RETURN l.id AS id",
        id=from_item,
        m=[MEANING_CORRECTED_BY, MEANING_SUPERSEDED_BY],
    )
    return row["id"]


# Three corrections to one item and its links ----------------------------------------------------


def three_corrections(driver, timed) -> dict[str, str]:
    """A plan (the head) and the records about it. Each correction mints the new record, corrects
    the record before it, and moves the head in the same batch: its summary, its assignee link,
    its status."""
    w, src, test_clock = timed["world_id"], timed["src"], timed["clock"]
    test_clock.set(day(21, 1))
    r1 = make_item(
        driver,
        w,
        src,
        2,
        kind="observation",
        title="Ilse said the launch dinner is at the Oak Room",
    )
    head = make_item(
        driver,
        w,
        src,
        3,
        kind="plan",
        title="Launch dinner",
        summary="at the Oak Room",
        status="proposed",
        recheck_by=RECHECK,
    )
    rowan = make_item(driver, w, src, 4, kind="person", title="Rowan Okonkwo")
    ilse = make_item(driver, w, src, 6, kind="person", title="Ilse Varga")

    def assign(person: str, ev_id: str, minute: int) -> dict[str, Any]:
        return create_link(
            head,
            "assigned to",
            person,
            ev_id,
            became_true=clock_at(at_minute(minute)),
            recheck_by=RECHECK,
        )

    assigned = apply(driver, w, [assign(rowan, "e5", 5)], [observation(src, "e5", 5)])

    test_clock.set(day(22, 0))
    first = apply(
        driver,
        w,
        [
            create_item("observation", "Rowan said the dinner moved to the Birch Hall", "e10"),
            update_item(head, 1, {"summary": "at the Birch Hall"}, "e10"),
            correct(r1, head, "e10"),
        ],
        [observation(src, "e10", 10)],
    )
    r2 = created_id(first)

    test_clock.set(day(23, 0))
    second = apply(
        driver,
        w,
        [
            create_item("observation", "Ilse runs the dinner now, in the Cedar Room", "e20"),
            update_item(head, 2, {"summary": "in the Cedar Room"}, "e20"),
            assign(ilse, "e20", 20),
            correct(r2, head, "e20"),
        ],
        [observation(src, "e20", 20)],
    )
    r3 = created_id(second)

    test_clock.set(day(24, 0))
    third = apply(
        driver,
        w,
        [
            create_item("observation", "The dinner went back to the Oak Room, booked", "e30"),
            update_item(head, 3, {"summary": "at the Oak Room"}, "e30"),
            change_status(head, "active", "e30"),
            correct(r3, head, "e30"),
        ],
        [observation(src, "e30", 30)],
    )
    test_clock.set(day(25, 0))
    return {
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "r4": created_id(third),
        "head": head,
        "rowan": rowan,
        "ilse": ilse,
        "to_rowan": created_id(assigned, ">", "link"),
        "to_ilse": created_id(second, ">", "link", 0),
        "r1_by": successor_link(driver, w, r1),
        "r2_by": successor_link(driver, w, r2),
        "r3_by": successor_link(driver, w, r3),
    }


def test_three_corrections_world_axis_equals_hand_built(driver, timed):
    """D3 How to test it: after three corrections to one item and its links, as-of on the world
    axis equals a hand-built snapshot."""
    m = three_corrections(driver, timed)
    query = WorldQuery(driver, timed["world_id"])
    head, person = m["head"], ("current", None, None, ())
    over = ("overtaken", None, None, (head,))
    current = ("current", None, None, ())
    expected = {
        15: (
            {
                m["r1"]: over,
                m["r2"]: current,
                head: ("current", "at the Birch Hall", "proposed", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_rowan"], m["r1_by"]},
        ),
        25: (
            {
                m["r1"]: over,
                m["r2"]: over,
                m["r3"]: current,
                head: ("current", "in the Cedar Room", "proposed", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_ilse"], m["r1_by"], m["r2_by"]},
        ),
        35: (
            {
                m["r1"]: over,
                m["r2"]: over,
                m["r3"]: over,
                m["r4"]: current,
                head: ("current", "at the Oak Room", "active", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_ilse"], m["r1_by"], m["r2_by"], m["r3_by"]},
        ),
    }
    for minute, snapshot in expected.items():
        assert shown(world_as_of(query, at_minute(minute))) == snapshot, minute


def test_three_corrections_record_axis_equals_hand_built(driver, timed):
    """D3 How to test it: after three corrections, as-of on the record axis equals a hand-built
    snapshot: what Grove believed at each record time."""
    m = three_corrections(driver, timed)
    query = WorldQuery(driver, timed["world_id"])
    head, person = m["head"], ("current", None, None, ())
    over = ("overtaken", None, None, (head,))
    current = ("current", None, None, ())
    expected = {
        21: (
            {
                m["r1"]: current,
                head: ("current", "at the Oak Room", "proposed", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_rowan"]},
        ),
        22: (
            {
                m["r1"]: over,
                m["r2"]: current,
                head: ("current", "at the Birch Hall", "proposed", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_rowan"], m["r1_by"]},
        ),
        23: (
            {
                m["r1"]: over,
                m["r2"]: over,
                m["r3"]: current,
                head: ("current", "in the Cedar Room", "proposed", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_ilse"], m["r1_by"], m["r2_by"]},
        ),
        24: (
            {
                m["r1"]: over,
                m["r2"]: over,
                m["r3"]: over,
                m["r4"]: current,
                head: ("current", "at the Oak Room", "active", ()),
                m["rowan"]: person,
                m["ilse"]: person,
            },
            {m["to_ilse"], m["r1_by"], m["r2_by"], m["r3_by"]},
        ),
    }
    for number, snapshot in expected.items():
        assert shown(known_as_of(query, day(number))) == snapshot, number


def test_past_world_end_keeps_true_then_and_believed_then_apart(driver, timed):
    """D3 How to test it: a link ended with a past world end time keeps "what was true on the 1st"
    and "what Grove believed on the 1st" distinct and correct."""
    w, src, test_clock = timed["world_id"], timed["src"], timed["clock"]
    test_clock.set(day(21, 1))
    market = make_item(driver, w, src, 2, kind="place", title="Northgate market")
    depot = make_item(driver, w, src, 3, kind="place", title="Cedar Street depot")
    made = apply(
        driver, w, [create_link(market, "part of", depot, "e4")], [observation(src, "e4", 4)]
    )
    link = created_id(made, ">", "link")
    # Recorded Nov 3: the link stopped being true on Oct 28.
    test_clock.set(datetime(2026, 11, 3, 9, tzinfo=UTC))
    # The November message belongs to a new export read in November.
    src = make_source(driver, w, "whatsapp", CHAT_LINES)
    seen = observation(src, "e9", 9, at=datetime(2026, 11, 2, 18, tzinfo=UTC))
    apply(
        driver,
        w,
        [
            {
                "op": "end_link",
                "op_id": "o-end",
                "link": link,
                "end_class": "ended_in_the_world",
                "stopped_being_true": clock_at(datetime(2026, 10, 28, tzinfo=UTC), "day"),
                "evidence_refs": [{"ev_id": "e9", "polarity": "supports"}],
                "reason": "the market left the depot at the end of October",
            }
        ],
        [seen],
    )
    query = WorldQuery(driver, w)
    first = datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert link not in world_as_of(query, first).links  # true on the 1st: no
    assert link in known_as_of(query, first).links  # believed on the 1st: yes
    assert link in world_as_of(query, day(27)).links  # true on Oct 27: yes


def test_spent_record_reads_on_the_record_axis_until_its_retirement(driver, timed):
    """D3 How to test it: a record retired as spent appears in as-of reads before its retirement
    and has no stopped-being-true time; the world axis keeps its claim."""
    w, src, test_clock = timed["world_id"], timed["src"], timed["clock"]
    test_clock.set(day(21, 1))
    record = make_item(driver, w, src, 2, kind="observation", title="Rowan prefers morning slots")
    lesson = make_item(driver, w, src, 3, kind="person", title="Rowan Okonkwo")
    test_clock.set(day(23, 0))
    apply(driver, w, [retire(record, lesson)], [], caller=CONNECTOR)
    query = WorldQuery(driver, w)
    assert known_as_of(query, day(22)).items[record].standing.state == "current"
    assert record not in known_as_of(query, day(24)).items
    assert world_as_of(query, day(24)).items[record].standing.state == "current"
    assert world_as_of(query, day(24)).items[record].row.get("stopped_being_true_precision") is None


# The successor on every read path, each by name ---------------------------------------------------


@pytest.fixture
def corrected(driver, seeded, chat) -> dict[str, str]:
    w = seeded["world_id"]
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
    apply(
        driver,
        w,
        [update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"), correct(record, head, "e9")],
        [observation(chat, "e9", 9)],
    )
    return {
        "world_id": w,
        "record": record,
        "head": head,
        "link": successor_link(driver, w, record),
    }


def test_successor_on_read_path_standings(driver, corrected):
    query = WorldQuery(driver, corrected["world_id"])
    found = standings(query, [corrected["record"]])[corrected["record"]]
    assert [(s.item_id, s.link_id) for s in found.successors] == [
        (corrected["head"], corrected["link"])
    ]


def test_successor_on_read_path_world_as_of(driver, corrected):
    query = WorldQuery(driver, corrected["world_id"])
    found = world_as_of(query, at_minute(60)).items[corrected["record"]].standing
    assert found.state == "overtaken"
    assert [s.item_id for s in found.successors] == [corrected["head"]]


def test_successor_on_read_path_known_as_of(driver, corrected):
    query = WorldQuery(driver, corrected["world_id"])
    found = known_as_of(query, datetime(2100, 1, 1, tzinfo=UTC)).items[corrected["record"]]
    assert found.standing.state == "overtaken"
    assert [s.item_id for s in found.standing.successors] == [corrected["head"]]


def test_successor_on_read_path_item_history(driver, corrected):
    history = item_history(WorldQuery(driver, corrected["world_id"]), corrected["record"])
    assert [s["id"] for s in history.successors] == [corrected["head"]]
    assert corrected["link"] in {entry["entity_id"] for entry in history}


def test_successor_on_read_path_views_graph(driver, corrected):
    w = corrected["world_id"]
    payload = graph_payload(
        WorldQuery(driver, w),
        WorldQuery.primitives(driver),
        window=Window(),
        self_item_id=None,
        now=datetime(2026, 11, 3, tzinfo=UTC),
    )
    node = next(
        n["data"] for n in payload["elements"]["nodes"] if n["data"]["id"] == corrected["record"]
    )
    assert node["standing"] == "overtaken"
    assert [s["id"] for s in node["successors"]] == [corrected["head"]]


def test_successor_on_read_path_views_node(driver, corrected):
    w = corrected["world_id"]
    payload = node_payload(
        WorldQuery(driver, w),
        WorldQuery.primitives(driver),
        corrected["record"],
        window=Window(),
        self_item_id=None,
    )
    assert payload is not None
    assert [s["id"] for s in payload["node"]["successors"]] == [corrected["head"]]


# Both restore paths after every D3 operation ------------------------------------------------------


@pytest.fixture
def every_operation(driver, seeded, chat) -> dict[str, str]:
    """A world that went through each composite operation: a correction with its head update and
    the same correction again with no head update, a refuted item and a refuted link, a
    supersession, a merge kept, a merge undone, an undone automatic change, a spent record, a
    late terminal status and a clock with words."""
    w = seeded["world_id"]

    def obs(minute: int, **extra: Any) -> dict[str, Any]:
        return observation(chat, f"e{minute}", minute, **extra)

    def ev(minute: int) -> str:
        return f"e{minute}"

    record = make_item(
        driver, w, chat, 2, kind="observation", title="Ilse said the dinner is at the Oak Room"
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
    apply(
        driver,
        w,
        [
            update_item(head, 1, {"summary": "at the Birch Hall"}, ev(4)),
            correct(record, head, ev(4)),
        ],
        [obs(4)],
    )
    # The same correction again from a newer source, with no head update: it restates the link.
    apply(
        driver,
        w,
        [correct(record, head, ev(19), no_head_update_reason="the head already says it")],
        [obs(19)],
    )
    false_event = make_item(
        driver, w, chat, 5, kind="event", title="The venue walk-through took place"
    )
    apply(
        driver,
        w,
        [refute(false_event, contradicts(ev(6)))],
        [obs(6, statement_class="report_happened")],
    )
    market = make_item(driver, w, chat, 7, kind="place", title="Northgate market")
    depot = make_item(driver, w, chat, 8, kind="place", title="Cedar Street depot")
    linked = apply(driver, w, [create_link(market, "part of", depot, ev(9))], [obs(9)])
    apply(driver, w, [refute(created_id(linked, ">", "link"), contradicts(ev(10)))], [obs(10)])
    old_decision = make_item(
        driver, w, chat, 11, kind="decision", title="Launch venue decided: Oak Room"
    )
    apply(
        driver,
        w,
        [
            create_item("decision", "Birch Hall chosen instead", ev(12), temp_id="$d"),
            supersede(old_decision, "$d", ev(12)),
        ],
        [obs(12)],
    )
    survivor = make_item(driver, w, chat, 13, kind="place", title="Oak Room")
    absorbed = make_item(driver, w, chat, 14, kind="place", title="the room above the bakery")
    other = make_item(driver, w, chat, 15, kind="place", title="Upstairs hall at Cedar Street")
    apply(driver, w, [create_link(absorbed, "part of", depot, ev(16))], [obs(16)])
    apply(driver, w, [merge(absorbed, other)], [])
    undone = apply(driver, w, [merge(survivor, absorbed)], [])
    apply(driver, w, [undo(undone.change_set_id)], [])
    apply(driver, w, [merge(survivor, absorbed)], [])
    task = make_item(
        driver,
        w,
        chat,
        17,
        kind="task",
        title="Draft the venue list",
        status="open",
        recheck_by=RECHECK,
    )
    automatic = apply(
        driver,
        w,
        [
            update_item(task, 1, {"title": "Draft the long venue list"}, ev(18)),
            change_status(task, "in progress", ev(18)),
        ],
        [obs(18)],
        caller=LEARNER,
    )
    apply(driver, w, [undo(automatic.change_set_id)], [])
    apply(driver, w, [change_status(task, "in progress", ev(30))], [obs(30)])
    git = make_source(driver, w, "git", GIT_LINES)
    apply(
        driver,
        w,
        [
            change_status(
                task,
                "done",
                ev(20),
                "g1",
                stopped_being_true=clock_at(at_minute(21), words="that evening"),
            )
        ],
        [obs(20, statement_class="report_happened"), git_observation(git, "g1")],
    )
    apply(
        driver,
        w,
        [
            update_item(
                task, 5, {"became_true": clock_at(at_minute(17), words="this morning")}, ev(31)
            )
        ],
        [obs(31)],
    )
    spent = make_item(driver, w, chat, 32, kind="observation", title="Rowan prefers morning slots")
    lesson = make_item(driver, w, chat, 33, kind="person", title="Rowan Okonkwo")
    apply(driver, w, [retire(spent, lesson)], [], caller=CONNECTOR)
    return {
        "world_id": w,
        "record": record,
        "head": head,
        "survivor": survivor,
        "absorbed": absorbed,
        "task": task,
        "spent": spent,
    }


# D3 over D8's ontology operations (develop merged #13) ------------------------------------------


def earned_word(driver, w: str, chat) -> dict[str, Any]:
    """A meaning minted provisional by the Learner, then used in two more distinct episodes, so the
    writer promotes it canonical at the end of the third batch (D8's earning bar)."""
    mint, _ = mint_godparent(driver, w, chat)
    link = godparent_link(driver, w)
    [ends] = WorldQuery(driver, w).read(
        "MATCH (a:Item)-[l:LINK {id: $id}]->(b:Item) RETURN a.id AS a, b.id AS b", id=link["id"]
    )
    for kind, line, ev_id in (
        ("whatsapp", "a second invented chat", "s1"),
        ("notes_files_releases", "an invented note", "n1"),
    ):
        source = make_source(driver, w, kind, [f"{line} about the christening"])
        apply(
            driver,
            w,
            [create_link(ends["a"], GODPARENT, ends["b"], ev_id)],
            [observation(source, ev_id, 1)],
            caller=LEARNER,
        )
    return {"mint": mint.change_set_id, "meaning": meaning_row(driver, w, GODPARENT)}


def test_rebuild_and_restore_keep_a_word_minted_and_earned(driver, seeded, chat):
    """D3's rebuild and full restore over D8's mint and promotion: the fingerprints match (the
    worked example's entry is one operation with its mint, S47), and the restore keeps the
    word's measures, which no entry holds, from the snapshot."""
    w = seeded["world_id"]
    word = earned_word(driver, w, chat)["meaning"]
    assert (word["status"], word["distinct_episodes"], word["version"]) == ("canonical", 3, 2)
    query = WorldQuery(driver, w)
    entries, values = read_history(query)
    projection = rebuild(entries, values)
    live = live_fingerprint(driver, w)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live
    assert projection.rows["meanings"][word["id"]]["version"] == 2
    held = projection_rows(query)
    for table, rows in held.items():
        for row_id, row in rows.items():
            rebuilt = {k: v for k, v in projection.rows[table][row_id].items() if k not in KEPT}
            assert rebuilt == {k: v for k, v in row.items() if k not in KEPT}, (table, row_id)

    snapshot = json.loads(json.dumps(take_snapshot(driver, w)))
    purge_world_rows(driver, w)
    restore(driver, w, snapshot, [])

    after = meaning_row(driver, w, GODPARENT)
    assert {key: after.get(key) for key in MEASURES} == {key: word.get(key) for key in MEASURES}
    assert after["distinct_episodes"] == 3 and json.loads(after["earning_episodes"])


def test_an_undo_of_a_mint_retires_the_word_and_marks_its_change_log_row(driver, seeded, chat):
    """D3's undo over a D8 mint: the minted word is retired (the inverse O10 names), the change
    log row names the undo (O10's undone by), the rebuild matches, and an undo of that undo puts
    the word back and clears the mark."""
    w = seeded["world_id"]
    mint, _ = mint_godparent(driver, w, chat)

    first = apply(driver, w, [undo(mint.change_set_id)], [], caller=PERSON)

    assert meaning_row(driver, w, GODPARENT)["status"] == "retired"
    [log] = WorldQuery(driver, w).read("MATCH (c:OntologyChange {operation: 'propose'}) RETURN c")
    assert (log["c"]["undone_by"], log["c"]["version"]) == (first.change_set_id, 2)
    query = WorldQuery(driver, w)
    entries, values = read_history(query)
    projection = rebuild(entries, values)
    live = live_fingerprint(driver, w)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live

    second = apply(driver, w, [undo(first.change_set_id)], [], caller=PERSON)

    assert meaning_row(driver, w, GODPARENT)["status"] == "provisional"
    [log] = WorldQuery(driver, w).read("MATCH (c:OntologyChange {operation: 'propose'}) RETURN c")
    assert log["c"].get("undone_by") is None and log["c"]["version"] == 3
    assert log["c"]["change_set_id"] == mint.change_set_id

    # A third undo reverses the mint again: the row names it, down the chain of undos (R4).
    third = apply(driver, w, [undo(second.change_set_id)], [], caller=PERSON)

    assert meaning_row(driver, w, GODPARENT)["status"] == "retired"
    [log] = WorldQuery(driver, w).read("MATCH (c:OntologyChange {operation: 'propose'}) RETURN c")
    assert (log["c"]["undone_by"], log["c"]["version"]) == (third.change_set_id, 4)


def measures_of(driver, w: str) -> dict[str, dict[str, Any]]:
    """Every word row's D8 measures, read straight from the store."""
    held: dict[str, dict[str, Any]] = {}
    for label in ("Kind", "Meaning", "PropertyName"):
        for row in WorldQuery(driver, w).read(f"MATCH (n:{label}) RETURN n"):
            held[row["n"]["id"]] = {key: row["n"].get(key) for key in MEASURES}
    return held


def test_a_rebuild_in_place_and_the_rebuild_command_keep_the_measures(driver, seeded, chat, capsys):
    """S47, the production path (a dump loaded, then `rebuild --world`): the rebuild in place keeps
    every word's measures, the starter kinds' and the earned word's, which no entry holds (R4)."""
    w = seeded["world_id"]
    earned_word(driver, w, chat)
    before = measures_of(driver, w)
    assert before[meaning_row(driver, w, GODPARENT)["id"]]["distinct_episodes"] == 3
    assert any(held["uses"] for word_id, held in before.items())
    live = live_fingerprint(driver, w)

    rebuild_in_place(driver, w)

    assert measures_of(driver, w) == before
    assert main(["rebuild", "--world", w, "--expect", live["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["match"] is True
    assert measures_of(driver, w) == before


def test_an_undo_of_a_mint_with_a_capability_is_refused_and_the_history_replays(
    driver, seeded, chat
):
    """A capability row has no ending (no status column), so an undo cannot retire it: the undo of
    the mint that wrote one is refused by name and writes nothing, and the history still replays
    to the live fingerprint (R4)."""
    w = seeded["world_id"]
    capability = {"connector_type": "calendar", "capability": "confirms"}
    ids = judged(
        driver,
        w,
        fields={
            "temporal_class": ("permanent", "fits"),
            "proposed_capability": (capability, "fits"),
        },
    )
    ops = [
        *people("e1"),
        propose("e1", ids, proposed_capability=capability),
        create_link("$ilse", GODPARENT, "$teo", "e1"),
    ]
    mint = apply(driver, w, ops, [observation(chat, "e1", 1)], caller=LEARNER)

    refusal = one(refused(driver, w, [undo(mint.change_set_id)], []), "invalid_arguments")

    assert refusal.rule == "no_ending"
    assert meaning_row(driver, w, GODPARENT)["status"] == "provisional"
    entries, values = read_history(WorldQuery(driver, w))
    projection = rebuild(entries, values)
    live = live_fingerprint(driver, w)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live


def test_an_undo_of_a_mint_is_refused_while_a_later_link_uses_the_word(driver, seeded, chat):
    """D8 line 1615: a word is retired only when no active item or link uses it. A later batch's
    link with the minted word blocks the undo of the mint; the word and both links stay (R4)."""
    w = seeded["world_id"]
    mint, _ = mint_godparent(driver, w, chat)
    source = make_source(driver, w, "whatsapp", ["a second invented chat about the christening"])
    ops = [
        create_item("person", "Mirela Stone", "s1", temp_id="$m"),
        create_item("person", "Oskar Lind", "s1", temp_id="$o"),
        create_link("$m", GODPARENT, "$o", "s1"),
    ]
    apply(driver, w, ops, [observation(source, "s1", 1)], caller=LEARNER)
    word = meaning_row(driver, w, GODPARENT)
    links = WorldQuery(driver, w).read(
        "MATCH (:Item)-[l:LINK {meaning: $id}]->(:Item) RETURN l.id AS id, l.active_key AS key",
        id=word["id"],
    )
    assert len(links) == 2 and all(link["key"] for link in links)

    refusal = one(refused(driver, w, [undo(mint.change_set_id)], []), "version_conflict")

    assert refusal.rule == "word_in_use"
    assert meaning_row(driver, w, GODPARENT) == word


def test_rebuild_matches_live_after_every_d3_operation(driver, every_operation):
    """D3 How to test it, the rebuild path: replaying the live change history into a scratch store
    yields projections whose fingerprint equals the live one, every clock and ended link included,
    after each composite operation of this lane."""
    w = every_operation["world_id"]
    query = WorldQuery(driver, w)
    live = live_fingerprint(driver, w)
    entries, values = read_history(query)
    projection = rebuild(entries, values)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live
    held = projection_rows(query)
    for table, rows in held.items():
        assert set(projection.rows[table]) == set(rows), table
        for row_id, row in rows.items():
            # touched_at and the D8 measures are columns no entry holds (S47).
            rebuilt = {k: v for k, v in projection.rows[table][row_id].items() if k not in KEPT}
            assert rebuilt == {k: v for k, v in row.items() if k not in KEPT}, (table, row_id)
    task = projection.items[every_operation["task"]]
    assert task["became_true_words"] == held["items"][every_operation["task"]]["became_true_words"]
    assert projection.items[every_operation["spent"]]["lesson_in"]


def test_a_second_correction_with_no_head_update_rebuilds_to_the_live_fingerprint(
    driver, seeded, chat
):
    """S47: a correction that lands on the successor link of an earlier one restates it. The
    marker for no head update leaves the link's version as it was, live and in the rebuild, so
    the fingerprints match and the restore is not refused."""
    w = seeded["world_id"]
    record, head = record_and_head(driver, w, chat)
    apply(
        driver,
        w,
        [update_item(head, 1, {"summary": "at the Birch Hall"}, "e9"), correct(record, head, "e9")],
        [observation(chat, "e9", 9)],
    )
    apply(
        driver,
        w,
        [correct(record, head, "e10", no_head_update_reason="the head already says it")],
        [observation(chat, "e10", 10)],
    )
    [link] = successor_links(driver, w, record, MEANING_CORRECTED_BY)
    assert link["version"] == 1
    fields = [entry["field"] for entry in entries_of(driver, w, link["id"])]
    assert fields.count("no_head_update") == 1 and "restated" in fields
    query = WorldQuery(driver, w)
    entries, values = read_history(query)
    projection = rebuild(entries, values)
    assert projection.links[link["id"]]["version"] == 1
    live = live_fingerprint(driver, w)
    assert fingerprint(projection.rows, projection.history, live["tables"]) == live


def test_full_restore_after_every_d3_operation(driver, every_operation, chat):
    """D3 How to test it, the full restore: the snapshot restored, rebuilt and with the ledger
    replayed matches its fingerprint minus the ids forgotten since; every snapshot-only table's
    count matches; a value forgotten after the snapshot is absent; no last-confirmed time moves."""
    w = every_operation["world_id"]
    confirmed = last_confirmed(driver, w)
    snapshot = json.loads(json.dumps(take_snapshot(driver, w)))
    recorded = snapshot["fingerprint"]
    assert recorded == live_fingerprint(driver, w)
    apply(
        driver, w, [create_item("person", "Tamsin Achebe", "e40")], [observation(chat, "e40", 40)]
    )
    [head] = WorldQuery(driver, w).read(
        "MATCH (i:Item {id: $id}) RETURN i.provenance_reason AS reason", id=every_operation["head"]
    )
    plant_ledger(driver, w, "field_value", [head["reason"]])
    ledger = [row["f"] for row in WorldQuery(driver, w).read("MATCH (f:ForgetLedger) RETURN f")]
    purge_world_rows(driver, w)
    result = restore(driver, w, snapshot, ledger)
    assert result["tables"] == recorded["tables"]
    assert (result["last_sequence"], result["entries"]) == (
        recorded["last_sequence"],
        recorded["entries"],
    )
    assert history_values(driver, w)[head["reason"]]["value"] == FORGOTTEN_MARKER
    assert last_confirmed(driver, w) == confirmed
    # The restored world reads as the live one did: the merge holds, the undo holds.
    found = standings(
        WorldQuery(driver, w), [every_operation["record"], every_operation["absorbed"]]
    )
    assert found[every_operation["record"]].successors[0].item_id == every_operation["head"]
    assert found[every_operation["absorbed"]].successors[0].item_id == every_operation["survivor"]
