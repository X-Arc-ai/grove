"""The pure rebuild of D3, its checks, the record axis and the D3 fingerprint.

The pure tests build a history in memory, entry by entry, in the entry vocabulary of the
writer's README, and never reach the store. The live tests write through the writer's own
operations, so this folder holds no write Cypher. Every name is invented.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from typing import Any

import pytest

from grove.store.d3_fingerprint import VERSION, fingerprint, live_fingerprint
from grove.store.primitives import MEANING_CORRECTED_BY
from grove.store.query import WorldQuery
from grove.store.replay import (
    FORGOTTEN_MARKER,
    Projection,
    ReplayRefused,
    known_as_of,
    rebuild,
    record_axis,
)
from grove.writer.restore import replay_ledger
from grove.writer.worlds import create_world
from tests.writer.helpers import (
    CHAT_LINES,
    apply,
    at_minute,
    clock_at,
    create_item,
    create_link,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

WORLD = "t_writer-b_0198f3a0-0000-7000-8000-000000000001"
# Record times (when the history was written) and world times (what the entries refer to).
R0 = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
W0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
_ABSENT = object()
# A work item's re-check horizon, which the writer asks for on a claim about now.
RECHECK = "2026-11-20T09:00:00+00:00"


def iso(at: datetime) -> str:
    return at.isoformat()


def recorded(minute: int) -> datetime:
    return R0 + timedelta(minutes=minute)


def world(minute: int) -> datetime:
    return W0 + timedelta(minutes=minute)


def minute_clock(at: datetime, **extra: Any) -> dict[str, Any]:
    return {"precision": "minute", "earliest": iso(at), "latest": iso(at), **extra}


# --- a history in memory ------------------------------------------------------------------------


class Book:
    """A world's history as the writer appends it: entries in sequence, values by id. Each
    operation has its own change set, its own reason value and its own record minute."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.values: dict[str, str | None] = {}
        self.minute = 0
        self._ids = count(1)

    def id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def value(self, value: Any) -> str:
        value_id = self.id("v")
        self.values[value_id] = json.dumps(value, sort_keys=True)
        return value_id

    def op(self) -> tuple[str, str]:
        self.minute += 1
        reason = self.id("v")
        self.values[reason] = "the message states it"
        return self.id("cs"), reason

    def add(
        self,
        op: tuple[str, str],
        entity_type: str,
        entity_id: str,
        field: str,
        new: Any = _ABSENT,
        old: Any = _ABSENT,
        *,
        world_time: datetime | None = None,
        history_only: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        entry = {
            "sequence": len(self.entries) + 1,
            "world_id": WORLD,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field": field,
            "recorded_at": recorded(self.minute),
            "change_set_id": op[0],
            "reason": op[1],
            "actor": "person:t",
            "history_only": history_only,
            **extra,
        }
        if world_time is not None:
            entry["world_time"] = world_time
        if new is not _ABSENT:
            entry["new_value_id"] = self.value(new)
        if old is not _ABSENT and old is not None:
            entry["old_value_id"] = self.value(old)
        self.entries.append(entry)
        return entry

    def row(
        self,
        op: tuple[str, str],
        entity_type: str,
        table: str,
        row: dict[str, Any],
        *,
        world_time: datetime | None = None,
    ) -> dict[str, Any]:
        row = {"world_id": WORLD, **row}
        key = row.get("id", row.get("item_id"))
        new = {"table": table, "row": row}
        return self.add(op, entity_type, key, "row", new, world_time=world_time)

    def rebuild(self, **options: Any) -> Projection:
        return rebuild(self.entries, self.values, **options)


def _vocabulary(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"status": status, "role": role} for status, role in pairs])


def registry(book: Book) -> dict[str, str]:
    """Kinds and meanings, as the seed writes them: registry rows with row entries."""
    op = book.op()
    ids = {name: book.id("k") for name in ("task", "place", "event")}
    kinds = {
        "task": ("work_with_state", _vocabulary(("open", "open"), ("done", "done"))),
        "place": ("lasting_thing", "[]"),
        "event": ("record", "[]"),
    }
    for name, (lifecycle, vocabulary) in kinds.items():
        row = {
            "id": ids[name],
            "name": name,
            "lifecycle_class": lifecycle,
            "status_vocabulary": vocabulary,
            "status": "canonical",
            "version": 1,
        }
        book.row(op, "registry_row", "kinds", row, world_time=recorded(book.minute))
    ends = {
        "part of": ({"any": True}, {"any": True}),
        "held at": ({"any": True}, {"kinds": [ids["place"]], "classes": []}),
    }
    for name, (start, end) in ends.items():
        ids[name] = book.id("m")
        row = {
            "id": ids[name],
            "name": name,
            "from_kinds": json.dumps(start),
            "to_kinds": json.dumps(end),
            "cardinality": "many",
            "status": "canonical",
            "version": 1,
        }
        book.row(op, "registry_row", "meanings", row)
    return ids


def item(
    book: Book, op: tuple[str, str], kind: str, title: str, *, world_time=None, **columns: Any
) -> str:
    item_id = book.id("i")
    row = {
        "id": item_id,
        "kind": kind,
        "title": title,
        "became_true_precision": "unknown",
        "recorded": iso(recorded(book.minute)),
        "touched_at": iso(recorded(book.minute)),
        "version": 1,
        **columns,
    }
    book.row(op, "item", "items", row, world_time=world_time or world(book.minute))
    return item_id


def link(book: Book, op: tuple[str, str], a: str, meaning: str, b: str, **columns: Any) -> str:
    link_id = book.id("l")
    row = {
        "id": link_id,
        "from_item": a,
        "meaning": meaning,
        "to_item": b,
        "active_key": f"{WORLD}|{a}|{meaning}|{b}",
        "became_true_precision": "unknown",
        "recorded": iso(recorded(book.minute)),
        "version": 1,
        **columns,
    }
    book.row(op, "link", "links", row, world_time=world(book.minute))
    return link_id


def evidence(
    book: Book, op: tuple[str, str], target_type: str, target_id: str, **columns: Any
) -> str:
    evidence_id = book.id("e")
    row = {
        "id": evidence_id,
        "target_type": target_type,
        "target_id": target_id,
        "polarity": "supports",
        "active": True,
        "source_type": "reported_done",
        "derivation": "observed",
        "speaker_kind": "person",
        "modality": "text",
        "source_time": iso(world(book.minute)),
        "recorded": iso(recorded(book.minute)),
        "version": 1,
        **columns,
    }
    book.row(op, "evidence", "evidence", row)
    return evidence_id


@pytest.fixture
def book() -> Book:
    return Book()


@pytest.fixture
def reg(book: Book) -> dict[str, str]:
    return registry(book)


# --- version, touched_at and the history-only rule ----------------------------------------------


def test_version_counts_operations_not_entries(book, reg):
    op = book.op()
    venue = item(book, op, reg["task"], "Book the venue", status="open", status_role="open")
    two = book.op()
    book.add(two, "item", venue, "summary", "Ilse asks for a hall", world_time=world(20))
    book.add(two, "item", venue, "title", "Book the harbour venue", "Book the venue")
    late = book.op()
    book.add(
        late, "item", venue, "summary", "a garden first", world_time=world(5), history_only=True
    )
    done = book.op()
    for field, value in (("status", "done"), ("status_role", "done"), ("status_since", world(30))):
        book.add(done, "item", venue, field, iso(value) if field == "status_since" else value)
    hall_op = book.op()
    hall = item(book, hall_op, reg["place"], "Harbour hall")
    part = link(book, hall_op, venue, reg["part of"], hall)
    book.add(hall_op, "link", part, "no_head_update", "the record stands as written")
    book.add(book.op(), "link", part, "restated", True)
    last = book.minute

    built = book.rebuild()

    row = built.items[venue]
    # The row, the batch of two, the status: the history-only entry moved nothing.
    assert row["version"] == 3
    assert row["summary"] == "Ilse asks for a hall"
    assert row["title"] == "Book the harbour venue"
    assert row["status"] == "done" and row["status_since"] == world(30)
    assert built.links[part]["version"] == 1
    assert built.links[part]["touched_at"] == recorded(last)
    assert built.items[hall]["touched_at"] == recorded(last - 1)
    assert (built.last_sequence, built.entries) == (len(book.entries), len(book.entries))
    assert built.world_id == WORLD


def test_rebuild_reads_the_whole_entry_vocabulary(book, reg):
    op = book.op()
    venue = item(book, op, reg["task"], "Book the venue", status="open", status_role="open")
    hall = item(book, op, reg["place"], "Harbour hall")
    party = item(book, op, reg["event"], "The spring party")
    part = link(book, op, venue, reg["part of"], hall)
    claim = evidence(book, op, "item", venue, source_type="confirmed")
    on_link = evidence(book, op, "link", part, source_type="said")

    changes = book.op()
    words = book.value("this morning")
    started = minute_clock(world(3), words="this morning", words_id=words)
    book.add(changes, "item", venue, "became_true", started, "unknown", world_time=world(3))
    book.add(changes, "item", venue, "properties", {"colour": "blue", "seats": 40})
    book.add(changes, "item", venue, "recheck_how", {"ask": "the hall"})
    book.add(changes, "item", venue, "recheck_by", iso(world(600)))
    book.add(changes, "item", venue, "merged_into", hall)

    ended = book.op()
    book.add(ended, "link", part, "end_class", "ended_in_the_world")
    book.add(ended, "link", part, "stopped_being_true", minute_clock(world(9)))
    book.add(ended, "evidence", claim, "end_reason", "correction")
    after_ending = book.rebuild()
    assert "active_key" not in after_ending.links[part]
    assert after_ending.evidence[claim]["active"] is False
    assert after_ending.evidence[claim]["ended_at"] == recorded(book.minute)
    # The claim fields follow the active supporting rows: the confirmed row ended.
    assert "derived_source_type" not in after_ending.items[venue]

    undone = book.op()
    book.add(undone, "link", part, "end_class", None, "ended_in_the_world", inverse_of=ended[0])
    book.add(undone, "link", part, "stopped_being_true", None, inverse_of=ended[0])
    book.add(undone, "evidence", claim, "end_reason", None, "correction", inverse_of=ended[0])

    spent = book.op()
    book.add(spent, "item", party, "end_class", "spent")
    book.add(spent, "item", party, "lesson_in", venue)

    succession = book.op()
    head = item(book, succession, reg["event"], "The summer party")
    corrected = link(book, succession, party, MEANING_CORRECTED_BY, head)
    book.add(succession, "link", corrected, "no_head_update", "the head already holds it")

    named = book.op()
    alias = book.id("a")
    alias_row = {"id": alias, "phrase": "the hall", "target_type": "item", "target_id": hall}
    book.row(named, "registry_row", "aliases", {**alias_row, "status": "canonical"})
    moved = book.op()
    book.add(moved, "registry_row", alias, "target_id", venue, hall)
    book.add(moved, "registry_row", alias, "status", "retired", "canonical")

    built = book.rebuild()

    row = built.items[venue]
    assert row["became_true_precision"] == "minute"
    assert row["became_true_earliest"] == row["became_true_latest"] == world(3)
    assert row["became_true_words"] == words
    assert row["properties"] == json.dumps({"colour": "blue", "seats": 40}, sort_keys=True)
    assert row["property_values"] == ["blue", "40"]
    assert json.loads(row["recheck_how"]) == {"ask": "the hall"}
    assert row["recheck_by"] == world(600)
    assert row["derived_source_type"] == "confirmed" and row["derivation"] == "observed"
    assert (row["speaker"], row["modality"]) == ("person", "text")
    # The marker holds no column.
    assert "merged_into" not in row
    # The undo put the link back: its active key, no stop clock, and the evidence row again.
    restored = built.links[part]
    assert restored["active_key"] == f"{WORLD}|{venue}|{reg['part of']}|{hall}"
    assert "end_class" not in restored and "stopped_being_true_precision" not in restored
    assert restored["derived_source_type"] == "said" and "speaker" not in restored
    assert built.evidence[claim]["active"] is True and "ended_at" not in built.evidence[claim]
    assert built.evidence[on_link]["active"] is True
    # retire_record: the record axis only, no stop clock.
    assert built.items[party]["end_class"] == "spent"
    assert built.items[party]["lesson_in"] == venue
    assert "stopped_being_true_precision" not in built.items[party]
    assert built.links[corrected]["meaning"] == MEANING_CORRECTED_BY
    assert built.links[corrected]["version"] == 1
    assert built.rows["aliases"][alias]["target_id"] == venue
    assert built.rows["aliases"][alias]["status"] == "retired"
    assert built.rows["aliases"][alias]["version"] == 2


def test_known_at_replays_only_what_was_recorded(book, reg):
    op = book.op()
    venue = item(book, op, reg["task"], "Book the venue", status="open", status_role="open")
    created_at = book.minute
    book.add(book.op(), "item", venue, "title", "Book the harbour venue", "Book the venue")

    then = book.rebuild(known_at=recorded(created_at))
    now = book.rebuild()

    assert then.items[venue]["title"] == "Book the venue"
    assert then.items[venue]["version"] == 1
    assert then.last_sequence == len(book.entries) - 1
    assert now.items[venue]["title"] == "Book the harbour venue"
    assert book.rebuild(known_at=recorded(0) - timedelta(seconds=1)).items == {}


# --- the checks ---------------------------------------------------------------------------------


def _venue(book: Book, reg: dict[str, str]) -> dict[str, str]:
    op = book.op()
    venue = item(book, op, reg["task"], "Book the venue", status="open", status_role="open")
    hall = item(book, op, reg["place"], "Harbour hall")
    part = link(book, op, venue, reg["part of"], hall)
    claim = evidence(book, op, "item", venue)
    book.add(book.op(), "item", venue, "title", "Book the harbour venue", "Book the venue")
    return {"venue": venue, "hall": hall, "part": part, "claim": claim}


def _row(book: Book, entity_type: str, table: str, **row: Any) -> dict[str, Any]:
    return book.row(book.op(), entity_type, table, {"id": book.id("x"), "version": 1, **row})


Corruption = Callable[[Book, dict[str, str], dict[str, str]], dict[str, Any]]
CORRUPT: dict[str, Corruption] = {}


def corrupt(rule: str) -> Callable[[Corruption], Corruption]:
    def register(plant: Corruption) -> Corruption:
        CORRUPT[rule] = plant
        return plant

    return register


@corrupt("old_value")
def _old_value(b, r, s):
    return b.add(b.op(), "item", s["venue"], "title", "Venue booked", "A title it never had")


@corrupt("unknown_kind")
def _unknown_kind(b, r, s):
    return _row(b, "item", "items", kind="k-none", title="Silver hull")


@corrupt("blank_title")
def _blank_title(b, r, s):
    return _row(b, "item", "items", kind=r["place"], title="   ")


@corrupt("missing_end")
def _missing_end(b, r, s):
    return _row(b, "link", "links", from_item=s["venue"], meaning=r["part of"], to_item="i-none")


@corrupt("endpoint_kind")
def _endpoint_kind(b, r, s):
    return _row(b, "link", "links", from_item=s["hall"], meaning=r["held at"], to_item=s["venue"])


@corrupt("unknown_meaning")
def _unknown_meaning(b, r, s):
    return _row(b, "link", "links", from_item=s["hall"], meaning="m-none", to_item=s["venue"])


@corrupt("missing_target")
def _missing_target(b, r, s):
    return _row(b, "evidence", "evidence", target_type="item", target_id="i-none")


@corrupt("id_not_new")
def _id_not_new(b, r, s):
    row = {"id": s["hall"], "kind": r["place"], "title": "Harbour hall"}
    return b.row(b.op(), "item", "items", row)


@corrupt("unknown_table")
def _unknown_table(b, r, s):
    return _row(b, "item", "sources", kind=r["place"], title="Harbour hall")


@corrupt("table_entity")
def _table_entity(b, r, s):
    return _row(b, "item", "links", kind=r["place"], title="Harbour hall")


@corrupt("world_id")
def _world_id(b, r, s):
    return _row(b, "item", "items", kind=r["place"], title="Harbour hall", world_id="t_other")


@corrupt("unknown_end_class")
def _unknown_end_class(b, r, s):
    return b.add(b.op(), "item", s["hall"], "end_class", "vanished")


@corrupt("unknown_status")
def _unknown_status(b, r, s):
    return b.add(b.op(), "item", s["venue"], "status", "half done")


@corrupt("unknown_end_reason")
def _unknown_end_reason(b, r, s):
    return b.add(b.op(), "evidence", s["claim"], "end_reason", "lost")


@corrupt("missing_entity")
def _missing_entity(b, r, s):
    return b.add(b.op(), "item", "i-none", "title", "Ghost hall")


@corrupt("unknown_field")
def _unknown_field(b, r, s):
    return b.add(b.op(), "item", s["hall"], "colour", "blue")


@corrupt("older_than_head")
def _older_than_head(b, r, s):
    before = W0 - timedelta(days=1)
    return b.add(b.op(), "item", s["venue"], "title", "Book a hall", world_time=before)


@corrupt("end_before_start")
def _end_before_start(b, r, s):
    b.add(b.op(), "item", s["hall"], "became_true", minute_clock(world(50)))
    return b.add(b.op(), "item", s["hall"], "stopped_being_true", minute_clock(world(40)))


@corrupt("active_key_taken")
def _active_key_taken(b, r, s):
    b.add(b.op(), "link", s["part"], "end_class", "ended_in_the_world")
    key = f"{WORLD}|{s['venue']}|{r['part of']}|{s['hall']}"
    ends = {"from_item": s["venue"], "meaning": r["part of"], "to_item": s["hall"]}
    _row(b, "link", "links", **ends, active_key=key)
    return b.add(b.op(), "link", s["part"], "end_class", None, "ended_in_the_world")


@corrupt("clock")
def _clock(b, r, s):
    shown = {"precision": "minute", "earliest": "soon", "latest": "later"}
    return b.add(b.op(), "item", s["hall"], "became_true", shown)


@corrupt("sequence")
def _sequence(b, r, s):
    b.entries.append(dict(b.entries[-1]))
    return b.entries[-1]


@corrupt("missing_value")
def _missing_value(b, r, s):
    return b.add(b.op(), "item", s["hall"], "title", new_value_id="v-none")


@pytest.mark.parametrize("rule", sorted(CORRUPT))
def test_rebuild_refuses_an_inconsistent_history(book, reg, rule):
    held = _venue(book, reg)
    good = book.rebuild()
    bad = CORRUPT[rule](book, reg, held)

    with pytest.raises(ReplayRefused) as refused:
        book.rebuild()

    assert (refused.value.rule, refused.value.sequence) == (rule, bad["sequence"])
    assert str(bad["sequence"]) in str(refused.value)
    # Not validated, the replay leaves the entry out and rebuilds the rest.
    lenient = book.rebuild(validate=False)
    assert lenient.items[held["venue"]]["title"] == good.items[held["venue"]]["title"]


# --- forgotten values ---------------------------------------------------------------------------


def test_a_forgotten_value_never_comes_back(book, reg):
    op = book.op()
    venue = item(book, op, reg["task"], "Book the venue", status="open", status_role="open")
    flat = item(book, op, reg["place"], "Harbour flat")
    on_flat = evidence(book, op, "item", flat)
    renamed = book.add(
        book.op(), "item", venue, "title", "Book the harbour venue", "Book the venue"
    )
    # A later operation still names the forgotten text as its old value.
    book.add(book.op(), "item", venue, "title", "Book the lake venue", "Book the harbour venue")
    book.add(book.op(), "item", flat, "summary", "a flat by the water")
    flat_row = next(e for e in book.entries if e["entity_id"] == flat and e["field"] == "row")
    evidence_row = next(e for e in book.entries if e["entity_id"] == on_flat)

    book.values[renamed["new_value_id"]] = FORGOTTEN_MARKER
    before_last = book.rebuild(known_at=renamed["recorded_at"])
    book.values[flat_row["new_value_id"]] = FORGOTTEN_MARKER
    book.values[evidence_row["new_value_id"]] = FORGOTTEN_MARKER
    built = book.rebuild()

    assert before_last.items[venue]["title"] == FORGOTTEN_MARKER
    assert built.items[venue]["title"] == "Book the lake venue"
    assert flat not in built.items and on_flat not in built.evidence
    assert built.forgotten == {flat, on_flat}


# --- the record axis, pure ----------------------------------------------------------------------


def test_record_axis_keeps_what_had_not_ended_yet(book, reg):
    op = book.op()
    later = R0 + timedelta(days=2)
    flat = item(book, op, reg["place"], "Harbour flat")
    gone = item(book, op, reg["place"], "Hillside cottage")
    never = item(book, op, reg["place"], "Silver hull")
    unclocked = item(book, op, reg["place"], "Old barn")
    party = item(book, op, reg["event"], "The spring party")
    alone = item(book, op, reg["event"], "The autumn party")
    head = item(book, op, reg["event"], "The summer party")
    future_link = link(book, op, flat, reg["part of"], gone)
    ending = book.op()
    book.add(ending, "item", flat, "end_class", "ended_in_the_world")
    book.add(ending, "item", flat, "stopped_being_true", minute_clock(later))
    book.add(ending, "item", gone, "end_class", "ended_in_the_world")
    book.add(ending, "item", gone, "stopped_being_true", minute_clock(world(1)))
    book.add(ending, "item", never, "end_class", "refuted")
    book.add(ending, "item", unclocked, "end_class", "retired")
    book.add(ending, "link", future_link, "end_class", "superseded")
    book.add(ending, "link", future_link, "stopped_being_true", minute_clock(later))
    book.add(ending, "item", party, "end_class", "spent")
    book.add(ending, "item", alone, "end_class", "spent")
    corrected = link(book, book.op(), party, MEANING_CORRECTED_BY, head)
    at = recorded(book.minute) + timedelta(hours=1)

    view = record_axis(book.rebuild(), at)

    assert view.axis == "record" and view.at == at
    # A stop clock wholly after the time read: the row still stood then.
    assert view.items[flat].standing.state == "current"
    assert view.links[future_link].standing.state == "current"
    # Ended before, refuted (never true), ended with no stop clock, spent: none stood.
    assert not {gone, never, unclocked, alone} & set(view.items)
    # A spent record that a successor stands over is kept, with the successor on it.
    assert view.items[party].standing.state == "ended"
    assert [s.item_id for s in view.items[party].standing.successors] == [head]
    assert view.links[corrected].standing.state == "current"
    assert view.items[head].standing.state == "current"


# --- the D3 fingerprint, pure (rule 7) ----------------------------------------------------------

T = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def sample() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rows = {
        "items": {
            "i-1": {
                "id": "i-1",
                "title": "Harbour flat",
                "summary": "a flat by the water",
                "status": "open",
                "version": 2,
                "became_true_precision": "minute",
                "became_true_earliest": T,
                "became_true_latest": T,
                "recorded": T,
                "last_confirmed": T + timedelta(hours=1),
                "recheck_by": T + timedelta(days=30),
                "touched_at": T,
            },
        },
        "links": {
            "l-1": {
                "id": "l-1",
                "end_class": "superseded",
                "became_true_precision": "unknown",
                "stopped_being_true_precision": "minute",
                "stopped_being_true_earliest": T,
                "stopped_being_true_latest": T,
                "recorded": T,
                "version": 2,
                "touched_at": T,
            },
        },
        "evidence": {"e-1": {"id": "e-1", "active": True, "recorded": T, "version": 1}},
        "beliefs": {"i-2": {"item_id": "i-2", "touched_at": T, "version": 1}},
    }
    history = {"last_sequence": 40, "entries": 40}
    tables = {
        "history_values": {"count": 90, "newest": "v-90"},
        "sources": {"count": 2, "newest": "s-2"},
    }
    return rows, history, tables


def _set(path: str, value: Any) -> Callable[[dict, dict, dict], None]:
    part, *keys = path.split(".")

    def change(rows: dict, history: dict, tables: dict) -> None:
        held = {"rows": rows, "history": history, "tables": tables}[part]
        for key in keys[:-1]:
            held = held[key]
        held[keys[-1]] = value

    return change


MOVES = {
    "a start bound": _set("rows.items.i-1.became_true_earliest", T - timedelta(minutes=1)),
    "a clock precision": _set("rows.items.i-1.became_true_precision", "day"),
    "a stop clock": _set("rows.links.l-1.stopped_being_true_latest", T + timedelta(minutes=1)),
    "a recorded time": _set("rows.evidence.e-1.recorded", T + timedelta(minutes=1)),
    "a last-confirmed time": _set("rows.items.i-1.last_confirmed", T + timedelta(hours=2)),
    "a re-check horizon": _set("rows.items.i-1.recheck_by", T + timedelta(days=31)),
    "a status": _set("rows.items.i-1.status", "done"),
    "an end class": _set("rows.links.l-1.end_class", "refuted"),
    "an end class set": _set("rows.items.i-1.end_class", "ended_in_the_world"),
    "a version": _set("rows.items.i-1.version", 3),
    "an evidence row's active flag": _set("rows.evidence.e-1.active", False),
    "a row more": _set("rows.items.i-3", {"id": "i-3", "version": 1}),
    "a snapshot table count": _set("tables.sources.count", 3),
    "a snapshot table's newest id": _set("tables.sources.newest", "s-3"),
    "the last sequence": _set("history.last_sequence", 41),
    "the count of entries": _set("history.entries", 41),
}
STAYS = {
    "a title": _set("rows.items.i-1.title", "Harbour flat by the lake"),
    "a summary": _set("rows.items.i-1.summary", FORGOTTEN_MARKER),
    "touched_at": _set("rows.items.i-1.touched_at", T + timedelta(days=1)),
    "the same instant at another offset": _set(
        "rows.items.i-1.became_true_earliest", T.astimezone(timezone(timedelta(hours=2)))
    ),
    "the same instant as ISO text": _set("rows.items.i-1.recorded", iso(T)),
}


@pytest.mark.parametrize("change", sorted(MOVES))
def test_the_fingerprint_moves_with_what_d3_names(change):
    rows, history, tables = sample()
    before = fingerprint(rows, history, tables)
    MOVES[change](rows, history, tables)
    assert fingerprint(rows, history, tables)["fingerprint"] != before["fingerprint"]


@pytest.mark.parametrize("change", sorted(STAYS))
def test_the_fingerprint_leaves_text_and_touched_at_out(change):
    rows, history, tables = sample()
    before = fingerprint(rows, history, tables)
    STAYS[change](rows, history, tables)
    assert fingerprint(rows, history, tables) == before


def test_the_fingerprint_shape_and_forgotten_ids():
    rows, history, tables = sample()
    found = fingerprint(rows, history, tables)
    assert set(found) == {"version", "fingerprint", "rows", "last_sequence", "entries", "tables"}
    assert found["version"] == VERSION
    assert found["rows"]["items"] == 1 and found["rows"]["beliefs"] == 1
    assert (found["last_sequence"], found["entries"]) == (40, 40)
    assert found["tables"]["sources"] == {"count": 2, "newest": "s-2"}

    left_out = fingerprint(rows, history, tables, forgotten=["i-1"])
    rows["items"].pop("i-1")
    assert left_out == fingerprint(rows, history, tables)
    assert left_out["fingerprint"] != found["fingerprint"]


# --- the live store -----------------------------------------------------------------------------


def _recorded_at(query: WorldQuery, entity_id: str, field: str) -> datetime:
    (row,) = query.read(
        "MATCH (e:ChangeEntry {entity_id: $id, field: $field}) RETURN e.recorded_at AS at",
        id=entity_id,
        field=field,
    )
    return row["at"]


def _between(start: datetime, end: datetime) -> datetime:
    return start + (end - start) / 2


def _ids(driver, world_id: str) -> dict[str, str]:
    rows = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS title, i.id AS id")
    return {row["title"]: row["id"] for row in rows}


@pytest.fixture
def live(driver, world) -> dict[str, Any]:
    """A seeded world and one chat source to cite."""
    create_world(driver, world, owner_user_id="u_t_owner")
    return {"world_id": world, "chat": make_source(driver, world, "whatsapp", CHAT_LINES)}


def test_known_as_of_is_the_head_at_record_time(driver, live):
    world_id, chat = live["world_id"], live["chat"]
    query = WorldQuery(driver, world_id)
    apply(
        driver,
        world_id,
        [
            create_item("task", "Book the venue", "e1", status="open", recheck_by=RECHECK),
            create_item("plan", "Spring reunion", "e1", status="proposed", recheck_by=RECHECK),
        ],
        [observation(chat, "e1", 10)],
    )
    ids = _ids(driver, world_id)
    venue, reunion = ids["Book the venue"], ids["Spring reunion"]
    apply(
        driver,
        world_id,
        [create_link(venue, "part of", reunion, "e2")],
        [observation(chat, "e2", 11)],
    )
    (row,) = read(
        driver, world_id, "MATCH (:Item {id: $a})-[l:LINK]->(:Item) RETURN l.id AS id", a=venue
    )
    part_of = row["id"]
    rename = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": venue,
        "version": 1,
        "fields": {"title": "Book the harbour venue"},
        "evidence_refs": supports("e3"),
        "reason": "the message states it",
    }
    apply(
        driver,
        world_id,
        [rename, create_item("person", "Ilse Tamsin", "e3")],
        [observation(chat, "e3", 20)],
    )
    ilse = _ids(driver, world_id)["Ilse Tamsin"]
    end = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": part_of,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(30)),
        "evidence_refs": supports("e4"),
        "reason": "the venue left the reunion",
    }
    apply(driver, world_id, [end], [observation(chat, "e4", 31)])

    r1 = _recorded_at(query, venue, "row")
    linked = _recorded_at(query, part_of, "row")
    r2 = _recorded_at(query, venue, "title")
    r3 = _recorded_at(query, part_of, "end_class")
    assert r1 < linked < r2 < r3

    assert venue not in known_as_of(query, r1 - timedelta(microseconds=1)).items
    assert known_as_of(query, r1).items[venue].row["title"] == "Book the venue"

    first = known_as_of(query, _between(linked, r2))
    assert first.axis == "record" and first.at == _between(linked, r2)
    assert first.items[venue].row["title"] == "Book the venue"
    assert first.items[venue].standing.state == "current"
    assert ilse not in first.items
    assert first.links[part_of].standing.state == "current"

    second = known_as_of(query, _between(r2, r3))
    assert second.items[venue].row["title"] == "Book the harbour venue"
    assert ilse in second.items
    # The stop clock (minute 30) is long past at that time, but Grove had not recorded the
    # ending yet: it believed the link then.
    assert second.links[part_of].standing.state == "current"
    assert "end_class" not in second.links[part_of].row

    after = known_as_of(query, r3)
    assert part_of not in after.links
    assert after.items[venue].row["title"] == "Book the harbour venue"
    assert after.items[reunion].standing.state == "current"


def test_the_live_fingerprint_leaves_a_history_values_text_out(driver, live):
    world_id, chat = live["world_id"], live["chat"]
    query = WorldQuery(driver, world_id)
    apply(
        driver,
        world_id,
        [create_item("task", "Book the venue", "e1", status="open", recheck_by=RECHECK)],
        [observation(chat, "e1", 10)],
    )
    venue = _ids(driver, world_id)["Book the venue"]
    rename = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": venue,
        "version": 1,
        "fields": {"title": "Book the harbour venue"},
        "evidence_refs": supports("e2"),
        "reason": "the message states it",
    }
    apply(driver, world_id, [rename], [observation(chat, "e2", 20)])
    (entry,) = query.read(
        "MATCH (e:ChangeEntry {entity_id: $id, field: 'title'}) RETURN e.new_value_id AS id",
        id=venue,
    )
    before = live_fingerprint(driver, world_id)

    replay_ledger(driver, world_id, [{"target_type": "field_value", "internal_ids": [entry["id"]]}])

    (value,) = query.read("MATCH (v:HistoryValue {id: $id}) RETURN v.value AS v", id=entry["id"])
    assert value["v"] == FORGOTTEN_MARKER
    (row,) = query.read("MATCH (i:Item {id: $id}) RETURN i.title AS title", id=venue)
    assert row["title"] == FORGOTTEN_MARKER
    assert live_fingerprint(driver, world_id) == before
