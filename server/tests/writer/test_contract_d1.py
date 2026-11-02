"""D1 contract tests at the writer: kinds, meanings, aliases, slots, world-time order, evidence.

Each test follows one sentence of D1's "How to test it" and the rules table of the writer's
README. A refusal test asserts the code and that the store did not change; a positive test reads
the stored rows back. Every name is invented.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove.store.asof import item_as_of
from grove.store.primitives import KIND_BELIEF, PRIMITIVE_MEANING_IDS
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    CURATOR,
    PERSON,
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
)

RECHECK = iso(T0 + timedelta(days=30))
# Columns of an evidence row (B.1). None of them may sit on a link.
EVIDENCE_FIELDS = frozenset(
    {
        "polarity",
        "target_id",
        "target_type",
        "message_key",
        "episode_id",
        "source_id",
        "locator",
        "statement_class",
        "source_type",
        "speaker_kind",
        "premises",
    }
)


# --- local builders -------------------------------------------------------------------------


@pytest.fixture
def cast(driver, seeded, chat) -> dict[str, Any]:
    """Two people, two places, a plan and two tasks, all cited from line 1 of the chat."""
    world_id = seeded["world_id"]
    work = {"status": "open", "recheck_by": RECHECK}
    ops = [
        create_item("person", "Rowan Okonkwo", "e0"),
        create_item("person", "Ilse Tamsin", "e0"),
        create_item("place", "Harbour flat", "e0"),
        create_item("place", "Hillside cottage", "e0"),
        create_item("plan", "Spring reunion", "e0", status="proposed", recheck_by=RECHECK),
        create_item("task", "Book the venue", "e0", **work),
        create_item("task", "Print the invitations", "e0", **work),
    ]
    apply(driver, world_id, ops, [observation(chat, "e0", 1)])
    titles = {
        "Rowan Okonkwo": "rowan",
        "Ilse Tamsin": "ilse",
        "Harbour flat": "harbour",
        "Hillside cottage": "hillside",
        "Spring reunion": "reunion",
        "Book the venue": "venue",
        "Print the invitations": "invites",
    }
    rows = read(driver, world_id, "MATCH (i:Item) RETURN i.title AS title, i.id AS id")
    ids = {titles[row["title"]]: row["id"] for row in rows if row["title"] in titles}
    assert len(ids) == len(titles)
    return {"world_id": world_id, "chat": chat, "meanings": seeded["meanings"], **ids}


def declined(driver, world_id: str, ops: list[dict], evidence: list[dict]) -> list:
    """Apply a batch that must be declined; assert nothing was written; return the refusals."""
    before = count_rows(driver, world_id)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, world_id, ops, evidence)
    assert count_rows(driver, world_id) == before
    return caught.value.refusals


def codes(refusals: list) -> list[tuple[str, str]]:
    return [(refusal.op_id, refusal.code) for refusal in refusals]


def links(driver, world_id: str) -> list[dict]:
    """Every link of the world, with its endpoints read from the pattern itself."""
    rows = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK]->(b:Item) RETURN l AS link, a.id AS src, b.id AS dst",
    )
    return [{**row["link"], "_src": row["src"], "_dst": row["dst"]} for row in rows]


def links_between(driver, world_id: str, src: str, meaning_id: str) -> list[dict]:
    return [
        link
        for link in links(driver, world_id)
        if link["_src"] == src and link["meaning"] == meaning_id
    ]


def item(driver, world_id: str, item_id: str) -> dict:
    rows = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    assert len(rows) == 1
    return rows[0]["i"]


def evidence_on(driver, world_id: str, target_id: str) -> list[dict]:
    rows = read(driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=target_id)
    return [row["e"] for row in rows]


def entries(driver, world_id: str, entity_id: str) -> list[dict]:
    rows = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {entity_id: $id}) RETURN c ORDER BY c.sequence",
        id=entity_id,
    )
    return [row["c"] for row in rows]


def history_value(driver, world_id: str, value_id: str) -> Any:
    rows = read(
        driver, world_id, "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value", id=value_id
    )
    assert len(rows) == 1
    return json.loads(rows[0]["value"])


def instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC)


def attach(target: str, ev_id: str, polarity: str = "supports") -> dict[str, Any]:
    return {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": target,
        "ev_id": ev_id,
        "polarity": polarity,
    }


def end_item(item_id: str, ev_id: str, stopped: dict) -> dict[str, Any]:
    return {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": item_id,
        "end_class": "ended_in_the_world",
        "stopped_being_true": stopped,
        "evidence_refs": supports(ev_id),
        "reason": "the message says it is over",
    }


def plant_state_property(driver, world_id: str, kind_id: str, name: str) -> dict:
    """A :PropertyName of temporal class state; only the ontology lane mints these for real."""
    return plant_node(
        driver,
        world_id,
        "PropertyName",
        kind=kind_id,
        name=name,
        value_type="number",
        temporal_class="state",
        default_horizon=7,
        status="canonical",
        origin="minted",
        minted_by="test",
        version=1,
    )


# --- a kind, a meaning and a status must exist after alias resolution ------------------------


@pytest.mark.parametrize("meaning", ["adores", "supports", "contradicts"])
def test_unknown_meaning_is_refused(driver, cast, meaning):
    # Support and contradiction are polarities of evidence rows, never meanings (D1).
    world_id = cast["world_id"]
    op = create_link(cast["rowan"], meaning, cast["ilse"], "e1", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "unknown_meaning")]


def test_unknown_kind_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    op = create_item("lighthouse keeper", "Tamsin at the point", "e1")
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "unknown_kind")]


def test_unknown_status_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    op = create_item("task", "Sand the boat", "e1", status="paused", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "unknown_status")]


# --- an alias resolves to its canonical meaning; a reversed alias swaps the endpoints --------

# (alias, from key, to key, canonical meaning, stored from key, stored to key, needs a re-check)
ALIASES = [
    ("needs", "invites", "venue", "depends on", "invites", "venue", True),
    ("needed by", "invites", "venue", "depends on", "venue", "invites", True),
    ("blocked by", "invites", "venue", "blocks", "venue", "invites", True),
    ("owns", "rowan", "harbour", "owned by", "harbour", "rowan", True),
    ("includes", "reunion", "venue", "part of", "venue", "reunion", False),
]


@pytest.mark.parametrize(
    ("alias", "src", "dst", "canonical", "stored_src", "stored_dst", "state"),
    ALIASES,
    ids=[row[0] for row in ALIASES],
)
def test_alias_resolves_to_canonical_meaning(
    driver, cast, alias, src, dst, canonical, stored_src, stored_dst, state
):
    world_id = cast["world_id"]
    extra = {"recheck_by": RECHECK} if state else {}
    op = create_link(cast[src], alias, cast[dst], "e1", **extra)
    apply(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])

    stored = links(driver, world_id)
    assert len(stored) == 1
    link = stored[0]
    assert link["meaning"] == cast["meanings"][canonical]
    assert (link["from_item"], link["to_item"]) == (cast[stored_src], cast[stored_dst])
    assert (link["_src"], link["_dst"]) == (cast[stored_src], cast[stored_dst])


# --- endpoint kinds must match the meaning ----------------------------------------------------


@pytest.mark.parametrize(
    ("meaning", "src", "dst"),
    [("lives in", "reunion", "harbour"), ("assigned to", "rowan", "ilse")],
    ids=["lives in from a plan", "assigned to from a person"],
)
def test_wrong_source_kind_is_refused(driver, cast, meaning, src, dst):
    world_id = cast["world_id"]
    op = create_link(cast[src], meaning, cast[dst], "e1", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "endpoint_kind_mismatch")]
    assert refusals[0].rule == "from_kind"


def test_wrong_target_kind_is_refused(driver, cast):
    # lives in takes a place at its target, never a person.
    world_id = cast["world_id"]
    op = create_link(cast["rowan"], "lives in", cast["ilse"], "e1", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "endpoint_kind_mismatch")]


# --- a one-of-a-kind slot ends the prior link at the new link's start -------------------------

SLOTS = [
    ("lives in", "rowan", "harbour", "hillside"),
    ("assigned to", "venue", "rowan", "ilse"),
]


@pytest.mark.parametrize(("meaning", "src", "first", "second"), SLOTS, ids=[s[0] for s in SLOTS])
def test_second_link_into_slot_ends_the_first_at_its_start(
    driver, cast, meaning, src, first, second
):
    world_id = cast["world_id"]
    meaning_id = cast["meanings"][meaning]
    old_op = create_link(
        cast[src],
        meaning,
        cast[first],
        "e1",
        recheck_by=RECHECK,
        became_true=clock_at(at_minute(10)),
    )
    apply(driver, world_id, [old_op], [observation(cast["chat"], "e1", 2)])
    (old,) = links_between(driver, world_id, cast[src], meaning_id)

    new_op = create_link(
        cast[src],
        meaning,
        cast[second],
        "e2",
        recheck_by=RECHECK,
        became_true=clock_at(at_minute(30)),
    )
    receipt = apply(driver, world_id, [new_op], [observation(cast["chat"], "e2", 3)])

    by_target = {
        link["to_item"]: link for link in links_between(driver, world_id, cast[src], meaning_id)
    }
    assert set(by_target) == {cast[first], cast[second]}
    ended, active = by_target[cast[first]], by_target[cast[second]]
    assert ended["id"] == old["id"]
    assert ended["end_class"] == "superseded"
    assert ended.get("active_key") is None
    for bound in ("earliest", "latest", "precision"):
        assert ended[f"stopped_being_true_{bound}"] == active[f"became_true_{bound}"]
    assert instant(ended["stopped_being_true_earliest"]) == at_minute(30)
    assert active.get("end_class") is None
    assert active.get("active_key") is not None
    # Same transaction: the old link's ending was committed by the new link's commit.
    assert receipt.commit_id in {
        entry["commit_id"] for entry in entries(driver, world_id, old["id"])
    }


@pytest.mark.parametrize(("meaning", "src", "first", "second"), SLOTS, ids=[s[0] for s in SLOTS])
def test_late_link_with_earlier_start_is_stored_already_ended(
    driver, cast, meaning, src, first, second
):
    world_id = cast["world_id"]
    meaning_id = cast["meanings"][meaning]
    newer = create_link(
        cast[src],
        meaning,
        cast[second],
        "e1",
        recheck_by=RECHECK,
        became_true=clock_at(at_minute(30)),
    )
    apply(driver, world_id, [newer], [observation(cast["chat"], "e1", 2)])

    late = create_link(
        cast[src],
        meaning,
        cast[first],
        "e2",
        recheck_by=RECHECK,
        became_true=clock_at(at_minute(10)),
    )
    apply(driver, world_id, [late], [observation(cast["chat"], "e2", 3)])

    by_target = {
        link["to_item"]: link for link in links_between(driver, world_id, cast[src], meaning_id)
    }
    stays, stored_ended = by_target[cast[second]], by_target[cast[first]]
    assert stays.get("end_class") is None
    assert stays.get("active_key") is not None
    assert stays.get("stopped_being_true_earliest") is None
    assert stays.get("stopped_being_true_latest") is None
    assert stored_ended["end_class"] == "superseded"
    assert stored_ended.get("active_key") is None
    assert instant(stored_ended["became_true_earliest"]) == at_minute(10)
    assert instant(stored_ended["stopped_being_true_latest"]) == at_minute(30)


# --- an update older than the head's value goes to history only -------------------------------


def make_task(driver, world_id: str, src: dict, title: str, line: int, **fields: Any) -> str:
    op = create_item("task", title, "e1", status="open", recheck_by=RECHECK, **fields)
    apply(driver, world_id, [op], [observation(src, "e1", line)])
    (row,) = read(driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id", t=title)
    return row["id"]


def test_older_status_change_goes_to_history_only(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_id = make_task(driver, world_id, chat, "Hire a photographer", 30)
    head = item(driver, world_id, task_id)

    op = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task_id,
        "status": "blocked",
        "evidence_refs": supports("e2"),
        "reason": "an earlier message said it waited on the budget",
    }
    apply(driver, world_id, [op], [observation(chat, "e2", 5)])

    after = item(driver, world_id, task_id)
    assert after["status"] == "open"
    assert after["status_since"] == head["status_since"]
    (entry,) = [e for e in entries(driver, world_id, task_id) if e["field"] == "status"]
    assert entry["history_only"] is True
    assert instant(entry["world_time"]) == at_minute(5)
    assert history_value(driver, world_id, entry["new_value_id"]) == "blocked"

    query = WorldQuery(driver, world_id)
    assert item_as_of(query, task_id, at_minute(5))["status"] == "blocked"
    assert item_as_of(query, task_id, at_minute(35))["status"] == "open"


def test_older_field_update_goes_to_history_only(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_id = make_task(
        driver, world_id, chat, "Order the flowers", 30, summary="Tamsin asks for white lilies"
    )
    head = item(driver, world_id, task_id)

    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": task_id,
        "version": head["version"],
        "fields": {"summary": "Tamsin asks for red tulips"},
        "evidence_refs": supports("e2"),
        "reason": "an earlier message named other flowers",
    }
    apply(driver, world_id, [op], [observation(chat, "e2", 5)])

    after = item(driver, world_id, task_id)
    assert after["summary"] == "Tamsin asks for white lilies"
    (entry,) = [e for e in entries(driver, world_id, task_id) if e["field"] == "summary"]
    assert entry["history_only"] is True
    assert instant(entry["world_time"]) == at_minute(5)
    assert history_value(driver, world_id, entry["new_value_id"]) == "Tamsin asks for red tulips"

    query = WorldQuery(driver, world_id)
    assert item_as_of(query, task_id, at_minute(5))["summary"] == "Tamsin asks for red tulips"
    assert item_as_of(query, task_id, at_minute(35))["summary"] == "Tamsin asks for white lilies"


# --- a state property only on a record, one per record ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "fields"),
    [("task", {"status": "open", "recheck_by": RECHECK}), ("person", {})],
    ids=["work with a state", "lasting thing"],
)
def test_state_property_on_item_that_is_not_a_record_is_refused(driver, seeded, chat, kind, fields):
    world_id = seeded["world_id"]
    plant_state_property(driver, world_id, seeded["kinds"][kind], "balance")
    op = create_item(kind, "Okonkwo savings pot", "e1", properties={"balance": 40}, **fields)
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "state_property_on_item")]


def test_record_may_hold_one_state_property(driver, seeded, chat):
    world_id = seeded["world_id"]
    plant_state_property(driver, world_id, seeded["kinds"]["event"], "attendance")
    op = create_item(
        "event",
        "Quiz night at the harbour",
        "e1",
        properties={"attendance": 40},
        recheck_by=RECHECK,
    )
    apply(driver, world_id, [op], [observation(chat, "e1", 2)])

    (row,) = read(
        driver, world_id, "MATCH (i:Item {title: $t}) RETURN i", t="Quiz night at the harbour"
    )
    assert row["i"]["kind"] == seeded["kinds"]["event"]
    assert json.loads(row["i"]["properties"]) == {"attendance": 40}


def test_record_with_two_state_properties_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    plant_state_property(driver, world_id, seeded["kinds"]["event"], "attendance")
    plant_state_property(driver, world_id, seeded["kinds"]["event"], "takings")
    op = create_item(
        "event",
        "Quiz night at the harbour",
        "e1",
        properties={"attendance": 40, "takings": 310},
        recheck_by=RECHECK,
    )
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "state_property_on_item")]


# --- one active link per (from, meaning, to): a restatement adds evidence ----------------------


def test_restated_link_adds_evidence_and_no_link(driver, cast):
    world_id = cast["world_id"]
    first = create_link(cast["venue"], "part of", cast["reunion"], "e1")
    apply(driver, world_id, [first], [observation(cast["chat"], "e1", 2)])
    (link,) = links(driver, world_id)
    assert len(evidence_on(driver, world_id, link["id"])) == 1
    before = count_rows(driver, world_id)

    again = create_link(cast["venue"], "part of", cast["reunion"], "e2")
    receipt = apply(driver, world_id, [again], [observation(cast["chat"], "e2", 3)])

    assert count_rows(driver, world_id)["LINK"] == before["LINK"]
    assert [row["id"] for row in links(driver, world_id)] == [link["id"]]
    rows = evidence_on(driver, world_id, link["id"])
    assert len(rows) == 2
    assert {row["target_type"] for row in rows} == {"link"}
    assert {row["polarity"] for row in rows} == {"supports"}
    assert not [line for line in receipt.lines if line.symbol == ">"]


# --- an evidence row targets an item, a link or a belief, never the links table --------------


def plant_belief(driver, world_id: str) -> str:
    belief = plant_node(
        driver, world_id, "Item", kind=KIND_BELIEF, title="Rowan prefers quiet venues", version=1
    )
    plant_node(driver, world_id, "Belief", item_id=belief["id"], state="tentative", version=1)
    return belief["id"]


def test_evidence_targets_item_link_and_belief(driver, cast):
    world_id = cast["world_id"]
    apply(
        driver,
        world_id,
        [create_link(cast["venue"], "part of", cast["reunion"], "e1")],
        [observation(cast["chat"], "e1", 2)],
    )
    (link,) = links(driver, world_id)
    belief_id = plant_belief(driver, world_id)
    before = count_rows(driver, world_id)

    ops = [attach(cast["ilse"], "e2"), attach(link["id"], "e2"), attach(belief_id, "e2")]
    apply(driver, world_id, ops, [observation(cast["chat"], "e2", 3)])

    after = count_rows(driver, world_id)
    assert after["LINK"] == before["LINK"]
    assert after["Evidence"] == before["Evidence"] + 3
    for target, target_type in (
        (cast["ilse"], "item"),
        (link["id"], "link"),
        (belief_id, "belief"),
    ):
        rows = [r for r in evidence_on(driver, world_id, target) if r["locator"] == "line:3"]
        assert [(r["target_type"], r["polarity"], r["active"]) for r in rows] == [
            (target_type, "supports", True)
        ]


def test_no_link_holds_evidence(driver, cast):
    world_id = cast["world_id"]
    apply(
        driver,
        world_id,
        [create_link(cast["venue"], "part of", cast["reunion"], "e1")],
        [observation(cast["chat"], "e1", 2)],
    )
    (link,) = links(driver, world_id)
    before = count_rows(driver, world_id)
    ops = [attach(link["id"], "e2"), attach(link["id"], "e3", "contradicts")]
    apply(
        driver,
        world_id,
        ops,
        [observation(cast["chat"], "e2", 3), observation(cast["chat"], "e3", 4)],
    )
    assert count_rows(driver, world_id)["LINK"] == before["LINK"]

    rows = raw(
        driver,
        "MATCH ()-[r {world_id: $w}]->() RETURN type(r) AS type, keys(r) AS keys, "
        "r.meaning AS meaning",
        w=world_id,
    )
    meanings = set(cast["meanings"].values()) | set(PRIMITIVE_MEANING_IDS)
    assert rows
    for row in rows:
        assert row["type"] == "LINK"
        assert not EVIDENCE_FIELDS & set(row["keys"])
        assert row["meaning"] in meanings


# --- a link of a state meaning needs a re-check by --------------------------------------------


def test_state_link_without_recheck_is_refused(driver, cast):
    world_id = cast["world_id"]
    op = create_link(cast["rowan"], "lives in", cast["harbour"], "e1")
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "missing_recheck")]


def test_permanent_link_without_recheck_is_accepted(driver, cast):
    world_id = cast["world_id"]
    op = create_link(cast["venue"], "part of", cast["reunion"], "e1")
    apply(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    (link,) = links(driver, world_id)
    assert link["meaning"] == cast["meanings"]["part of"]
    assert link.get("recheck_by") is None


# --- no new consequence link to an ended item; evidence on it is accepted ---------------------


@pytest.fixture
def ended_venue(driver, cast) -> dict[str, Any]:
    world_id = cast["world_id"]
    op = end_item(cast["venue"], "e1", clock_at(at_minute(15)))
    apply(driver, world_id, [op], [observation(cast["chat"], "e1", 15)])
    assert item(driver, world_id, cast["venue"])["end_class"] == "ended_in_the_world"
    return cast


@pytest.mark.parametrize(
    ("meaning", "src", "dst"),
    [
        ("depends on", "invites", "venue"),
        ("blocks", "invites", "venue"),
        ("assigned to", "venue", "rowan"),
    ],
    ids=["depends on", "blocks", "assigned to"],
)
def test_consequence_link_to_ended_item_is_refused(driver, ended_venue, meaning, src, dst):
    cast = ended_venue
    world_id = cast["world_id"]
    op = create_link(cast[src], meaning, cast[dst], "e2", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e2", 16)])
    assert codes(refusals) == [(op["op_id"], "ended_target")]


def test_evidence_on_ended_item_is_accepted(driver, ended_venue):
    cast = ended_venue
    world_id = cast["world_id"]
    links_before = count_rows(driver, world_id)["LINK"]
    apply(driver, world_id, [attach(cast["venue"], "e2")], [observation(cast["chat"], "e2", 16)])

    rows = [r for r in evidence_on(driver, world_id, cast["venue"]) if r["locator"] == "line:16"]
    assert [(r["target_type"], r["polarity"], r["active"]) for r in rows] == [
        ("item", "supports", True)
    ]
    assert count_rows(driver, world_id)["LINK"] == links_before


# --- a claim about now on an item needs a re-check by (D4's field-based cases) ----------------


def test_open_task_without_recheck_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    op = create_item("task", "Sand the hull", "e1", status="open")
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "missing_recheck")]
    assert refusals[0].rule == "claim_about_now"


def test_begun_bounded_period_without_recheck_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    op = create_item("trip", "Rowan in Lisbon", "e1", became_true=clock_at(at_minute(2)))
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "missing_recheck")]
    assert refusals[0].rule == "claim_about_now"


def test_record_with_a_state_property_without_recheck_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    plant_state_property(driver, world_id, seeded["kinds"]["event"], "attendance")
    op = create_item("event", "Quiz night", "e1", properties={"attendance": 40})
    refusals = declined(driver, world_id, [op], [observation(chat, "e1", 2)])
    assert codes(refusals) == [(op["op_id"], "missing_recheck")]
    assert refusals[0].rule == "claim_about_now"


def test_terminal_task_and_unbegun_period_need_no_recheck(driver, seeded, chat):
    world_id = seeded["world_id"]
    ops = [
        create_item("task", "Sand the hull", "e1", status="cancelled"),
        create_item("trip", "Rowan in Lisbon", "e1"),
        create_item("event", "Quiz night", "e1"),
    ]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 2)])
    assert [line.symbol for line in receipt.lines] == ["+", "+", "+"]


def test_update_that_clears_the_recheck_of_an_open_task_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    task_id = make_task(driver, world_id, chat, "Hire a photographer", 3)
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": task_id,
        "version": item(driver, world_id, task_id)["version"],
        "fields": {"recheck_by": None},
        "evidence_refs": supports("e2"),
        "reason": "the agent drops the re-check",
    }
    refusals = declined(driver, world_id, [op], [observation(chat, "e2", 5)])
    assert codes(refusals) == [(op["op_id"], "missing_recheck")]
    assert item(driver, world_id, task_id)["recheck_by"] is not None


def test_reopen_to_a_live_status_without_recheck_is_refused(driver, seeded, chat):
    world_id = seeded["world_id"]
    op = create_item("task", "Sand the hull", "e1", status="cancelled")
    apply(driver, world_id, [op], [observation(chat, "e1", 2)])
    (row,) = read(driver, world_id, "MATCH (i:Item {title: 'Sand the hull'}) RETURN i.id AS id")
    reopen = {"op": "reopen_item", "op_id": new_op_id(), "item": row["id"], "reason": "back on"}
    refusals = declined(driver, world_id, [reopen], [])
    assert codes(refusals) == [(reopen["op_id"], "missing_recheck")]


# --- a terminal status ends the item for consequence links ------------------------------------

GIT_TOKEN = "9b2c7e44"


def git_observation(driver, world_id: str, ev_id: str, line: int) -> dict:
    git = make_source(driver, world_id, "git", [f"commit {GIT_TOKEN} Book the venue"])
    return observation(
        git,
        ev_id,
        line,
        locator=f"key:{GIT_TOKEN}",
        message_key=GIT_TOKEN,
        statement_class="report_happened",
    )


@pytest.fixture(params=["cancelled", "done"])
def venue_at_terminal_status(request, driver, cast) -> dict[str, Any]:
    world_id = cast["world_id"]
    evidence = [observation(cast["chat"], "e1", 15, statement_class="report_happened")]
    refs = ["e1"]
    if request.param == "done":
        evidence.append(git_observation(driver, world_id, "g1", 15))
        refs.append("g1")
    op = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": cast["venue"],
        "status": request.param,
        "evidence_refs": supports(*refs),
        "reason": "the message says so",
    }
    apply(driver, world_id, [op], evidence)
    row = item(driver, world_id, cast["venue"])
    assert row["status_role"] == request.param
    assert row.get("end_class") is None
    return cast


@pytest.mark.parametrize("meaning", ["depends on", "blocks"])
def test_consequence_link_to_a_terminal_item_is_refused(driver, venue_at_terminal_status, meaning):
    cast = venue_at_terminal_status
    world_id = cast["world_id"]
    op = create_link(cast["invites"], meaning, cast["venue"], "e2", recheck_by=RECHECK)
    refusals = declined(driver, world_id, [op], [observation(cast["chat"], "e2", 16)])
    assert codes(refusals) == [(op["op_id"], "ended_target")]


# --- a history-only properties update holds only what was true at its time --------------------


def plant_property(driver, world_id: str, kind_id: str, name: str) -> dict:
    return plant_node(
        driver,
        world_id,
        "PropertyName",
        kind=kind_id,
        name=name,
        value_type="text",
        temporal_class="permanent",
        status="canonical",
        origin="minted",
        minted_by="test",
        version=1,
    )


def test_older_properties_update_does_not_show_later_keys(driver, seeded, chat):
    world_id = seeded["world_id"]
    person = seeded["kinds"]["person"]
    plant_property(driver, world_id, person, "nickname")
    plant_property(driver, world_id, person, "role")
    apply(
        driver, world_id, [create_item("person", "Ilse Tamsin", "e1")], [observation(chat, "e1", 1)]
    )
    (row,) = read(driver, world_id, "MATCH (i:Item {title: 'Ilse Tamsin'}) RETURN i.id AS id")
    ilse = row["id"]

    def update(properties: dict, ev_id: str, line: int) -> None:
        op = {
            "op": "update_item",
            "op_id": new_op_id(),
            "item": ilse,
            "version": item(driver, world_id, ilse)["version"],
            "fields": {"properties": properties},
            "evidence_refs": supports(ev_id),
            "reason": "the message names it",
        }
        apply(driver, world_id, [op], [observation(chat, ev_id, line)])

    update({"nickname": "Ils"}, "e2", 30)
    update({"role": "host"}, "e3", 5)

    assert json.loads(item(driver, world_id, ilse)["properties"]) == {"nickname": "Ils"}
    query = WorldQuery(driver, world_id)
    assert json.loads(item_as_of(query, ilse, at_minute(6))["properties"]) == {"role": "host"}
    assert json.loads(item_as_of(query, ilse, at_minute(31))["properties"]) == {"nickname": "Ils"}


# --- a reopen with no evidence takes no processing time as its world time ---------------------


@pytest.mark.parametrize("caller", [PERSON, CURATOR], ids=["person", "cleanup"])
def test_status_after_a_reopen_without_evidence_moves_the_head(driver, seeded, chat, caller):
    world_id = seeded["world_id"]
    task_id = make_task(driver, world_id, chat, "Hire a photographer", 1)
    cancel = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task_id,
        "status": "cancelled",
        "evidence_refs": supports("e2"),
        "reason": "Rowan called it off",
    }
    apply(driver, world_id, [cancel], [observation(chat, "e2", 2)])
    reopen = {"op": "reopen_item", "op_id": new_op_id(), "item": task_id, "reason": "back on"}
    apply(driver, world_id, [reopen], [], caller=caller)
    started = {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": task_id,
        "status": "in progress",
        "evidence_refs": supports("e3"),
        "reason": "Ilse started",
    }
    receipt = apply(driver, world_id, [started], [observation(chat, "e3", 20)], caller=caller)

    after = item(driver, world_id, task_id)
    assert after["status"] == "in progress"
    assert instant(after["status_since"]) == at_minute(20)
    assert [line.history_only for line in receipt.lines if line.field == "status"] == [False]
