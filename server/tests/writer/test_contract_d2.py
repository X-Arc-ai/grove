"""D2's contract tests: what the writer refuses, and what it stores when it accepts.

Written from the spec's "How to test it" (D2, D5) and the README's rules table, not from the
implementation. Every refusal checks the exact code and that the world's rows did not change;
every acceptance reads the stored rows back. All names are invented.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove.ids import new_id
from grove.store.asof import item_as_of
from grove.store.connection import DATABASE
from grove.store.primitives import KIND_BELIEF, MEANING_CORRECTED_BY, MEANING_DESCRIBES
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from grove.writer.testing import plant_link, plant_node
from grove.writer.worlds import create_world
from tests.writer.helpers import (
    CHAT_LINES,
    LEARNER,
    PERSON,
    T0,
    apply,
    assert_only_decline_log,
    at_minute,
    clock_at,
    count_rows,
    create_item,
    create_link,
    created_id,
    iso,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

RECHECK = "2027-01-15T00:00:00+00:00"
GIT_TOKEN = "4f9c2ab1"
GIT_LINES = [
    f"commit {GIT_TOKEN} Draft the venue list for Rowan",
    "commit 7d01ce55 Add the catering order for Ilse",
]
WEB_LINES = [
    "Okonkwo hall bookings: the venue list is published",
    "Contact Tamsin for the floor plan",
]


# Local builders and readers ---------------------------------------------------------------


def refused(driver, world_id: str, ops: list[dict], evidence: list[dict], caller=PERSON):
    """Apply a batch that must be declined; check that no row of the world changed."""
    before = count_rows(driver, world_id)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, world_id, ops, evidence, caller=caller)
    assert_only_decline_log(driver, world_id, before, caught.value.refusals)
    return caught.value.refusals


def one(refusals, code: str):
    found = [refusal for refusal in refusals if refusal.code == code]
    assert found, f"no {code} among {[(r.code, r.rule) for r in refusals]}"
    return found[0]


def details_text(refusal) -> str:
    return json.dumps(refusal.details, default=str)


def item_row(driver, world_id: str, item_id: str) -> dict[str, Any]:
    rows = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    assert len(rows) == 1
    return rows[0]["i"]


def evidence_rows(driver, world_id: str, target_id: str) -> list[dict[str, Any]]:
    rows = read(driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e", id=target_id)
    return [row["e"] for row in rows]


def make_item(driver, world_id, src, line, kind="person", title="Rowan Okonkwo", **fields) -> str:
    ev = observation(src, f"s{line}", line)
    receipt = apply(driver, world_id, [create_item(kind, title, ev["ev_id"], **fields)], [ev])
    return created_id(receipt)


def make_task(driver, world_id, src, line, title="Draft the venue list", **fields) -> str:
    fields = {"status": "open", "recheck_by": RECHECK, **fields}
    return make_item(driver, world_id, src, line, kind="task", title=title, **fields)


def update_item(item: str, version: int, fields: dict, ev_id: str) -> dict[str, Any]:
    return {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item,
        "version": version,
        "fields": fields,
        "evidence_refs": supports(ev_id),
        "reason": "a later message restates it",
    }


def change_status(item: str, status: str, *ev_ids: str, **fields) -> dict[str, Any]:
    return {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": item,
        "status": status,
        "evidence_refs": supports(*ev_ids),
        "reason": "the message reports it",
        **fields,
    }


def git_observation(src, ev_id: str, **extra) -> dict[str, Any]:
    return observation(
        src,
        ev_id,
        1,
        locator=f"key:{GIT_TOKEN}",
        message_key=GIT_TOKEN,
        statement_class="artifact",
        **extra,
    )


def instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(UTC)


def version_of(driver, world_id: str, item_id: str) -> int:
    return item_row(driver, world_id, item_id)["version"]


# Kind, meaning, status, endpoints ----------------------------------------------------------


def test_unknown_meaning_is_refused_with_the_valid_list(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 3)
    op = create_link(a, "admires greatly", b, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "unknown_meaning")
    assert refusal.op_id == op["op_id"]
    assert "part of" in details_text(refusal)


def test_wrong_endpoint_kinds_are_refused(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 3)
    op = create_link(a, "lives in", b, "e1", recheck_by=RECHECK)
    refusal = one(refused(driver, w, [op], [ev]), "endpoint_kind_mismatch")
    assert refusal.op_id == op["op_id"]
    assert refusal.rule == "from_kind"


def test_unknown_kind_is_refused_with_the_valid_list(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("spaceship", "The Okonkwo shuttle", "e1")
    refusal = one(refused(driver, w, [op], [ev]), "unknown_kind")
    assert refusal.op_id == op["op_id"]
    assert "task" in details_text(refusal)
    assert "person" in details_text(refusal)


def test_unknown_status_is_refused_with_the_valid_list(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item(
        "task", "Draft the venue list", "e1", status="almost finished", recheck_by=RECHECK
    )
    refusal = one(refused(driver, w, [op], [ev]), "unknown_status")
    assert refusal.op_id == op["op_id"]
    assert "in progress" in details_text(refusal)


def test_inherited_reported_done_is_accepted_for_a_work_kind(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1, statement_class="report_happened")
    op = create_item(
        "task", "Draft the venue list", "e1", status="reported done", recheck_by=RECHECK
    )
    task = created_id(apply(driver, w, [op], [ev]))
    row = item_row(driver, w, task)
    assert row["status"] == "reported done"
    assert row["status_role"] == "reported_done"
    assert row["derived_source_type"] == "reported_done"


def test_reported_done_is_not_inherited_by_a_lasting_thing(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1, statement_class="report_happened")
    op = create_item("organisation", "Okonkwo Hall", "e1", status="reported done")
    one(refused(driver, w, [op], [ev]), "unknown_status")


# Provenance ---------------------------------------------------------------------------------


@pytest.mark.parametrize("refs", ["empty", "absent"])
def test_an_op_without_evidence_refs_is_refused(driver, seeded, chat, refs):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("person", "Ilse Tamsin", "e1", evidence_refs=[])
    if refs == "absent":
        del op["evidence_refs"]
    refusal = one(refused(driver, w, [op], [ev]), "missing_provenance")
    assert refusal.op_id == op["op_id"]


def test_an_observation_without_a_locator_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    del ev["locator"]
    one(
        refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev]), "missing_provenance"
    )


@pytest.mark.parametrize(
    "locator",
    [
        pytest.param(f"line:{len(CHAT_LINES) + 1}", id="past_last_line"),
        pytest.param(f"line:{len(CHAT_LINES) - 1}-{len(CHAT_LINES) + 2}", id="range_past_end"),
        pytest.param("char:0-5", id="scheme_not_allowed"),
        pytest.param("key:venue", id="key_scheme_not_allowed"),
    ],
)
def test_an_unresolvable_locator_is_refused(driver, seeded, chat, locator):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1, locator=locator)
    refusals = refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev])
    one(refusals, "unresolvable_locator")


def test_a_source_of_another_world_is_refused(driver, seeded, chat, make_world):
    w = seeded["world_id"]
    other = make_world()
    create_world(driver, other, owner_user_id="u_t_other")
    foreign = make_source(driver, other, "whatsapp", CHAT_LINES)
    other_before = count_rows(driver, other)
    ev = observation(foreign, "e1", 1)
    refusals = refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev])
    codes = {refusal.code for refusal in refusals}
    assert codes & {"unresolvable_locator", "missing_provenance"}, codes
    assert count_rows(driver, other) == other_before


def test_an_episode_not_read_from_the_cited_source_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    second = make_source(driver, w, "whatsapp", CHAT_LINES)
    ev = observation({**chat, "source_id": second["source_id"]}, "e1", 1)
    refusals = refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev])
    one(refusals, "missing_provenance")


# Source type --------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["source_type", "derived_source_type"])
@pytest.mark.parametrize("place", ["op", "observation"])
def test_a_caller_source_type_is_refused(driver, seeded, chat, place, key):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("person", "Ilse Tamsin", "e1")
    (op if place == "op" else ev)[key] = "confirmed"
    refusal = one(refused(driver, w, [op], [ev]), "caller_source_type")
    if place == "op":
        assert refusal.op_id == op["op_id"]


@pytest.mark.parametrize(
    ("connector", "statement_class", "speaker_kind", "expected"),
    [
        pytest.param("whatsapp", "report_happened", "person", "reported_done", id="chat_report"),
        pytest.param("whatsapp", "decision", "person", "said", id="chat_decision"),
        pytest.param("git", "artifact", "person", "confirmed", id="git_commit"),
        pytest.param("git", "report_happened", "person", "reported_done", id="git_message"),
        # The caller's speaker kind never raises the type (D2: derived from the connector and
        # the statement class, never accepted from a caller).
        pytest.param("whatsapp", "report_happened", "automated", "reported_done", id="automated"),
    ],
)
def test_the_source_type_is_derived_from_connector_and_statement(
    driver, seeded, chat, connector, statement_class, speaker_kind, expected
):
    w = seeded["world_id"]
    if connector == "git":
        ev = git_observation(make_source(driver, w, "git", GIT_LINES), "e1")
    else:
        ev = observation(chat, "e1", 4)
    ev |= {"statement_class": statement_class, "speaker_kind": speaker_kind}
    item = created_id(apply(driver, w, [create_item("event", "Venue deposit paid", "e1")], [ev]))
    [row] = evidence_rows(driver, w, item)
    assert row["source_type"] == expected
    assert row["derivation"] == "observed"
    assert row["speaker_kind"] == speaker_kind
    assert row["active"] is True
    stored = item_row(driver, w, item)
    assert stored["derived_source_type"] == expected
    assert stored["derivation"] == "observed"
    assert stored["speaker"] == speaker_kind
    assert stored["modality"] == "text"


def test_end_evidence_ends_the_row_and_recomputes_the_source_type(driver, seeded, chat):
    w = seeded["world_id"]
    git = make_source(driver, w, "git", GIT_LINES)
    said = observation(chat, "e1", 3, statement_class="report_happened")
    commit = git_observation(git, "g1")
    op = create_item("event", "Venue list committed", "e1", evidence_refs=supports("e1", "g1"))
    item = created_id(apply(driver, w, [op], [said, commit]))
    assert item_row(driver, w, item)["derived_source_type"] == "confirmed"
    [git_row] = [
        row for row in evidence_rows(driver, w, item) if row["source_id"] == git["source_id"]
    ]

    end = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": git_row["id"],
        "end_reason": "correction",
    }
    apply(driver, w, [end], [])
    rows = {row["id"]: row for row in evidence_rows(driver, w, item)}
    assert rows[git_row["id"]]["active"] is False
    assert rows[git_row["id"]]["end_reason"] == "correction"
    assert rows[git_row["id"]]["ended_at"] is not None
    [chat_row] = [row for row in rows.values() if row["id"] != git_row["id"]]
    assert chat_row["active"] is True
    assert item_row(driver, w, item)["derived_source_type"] == "reported_done"


# Clocks -------------------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", ["create_item", "create_link"])
def test_a_missing_became_true_is_refused(driver, seeded, chat, op_name):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 3)
    if op_name == "create_item":
        op = create_item("person", "Ilse Tamsin", "e1")
    else:
        a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
        b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
        op = create_link(a, "part of", b, "e1")
    del op["became_true"]
    refusal = one(refused(driver, w, [op], [ev]), "missing_became_true")
    assert refusal.op_id == op["op_id"]


def test_an_explicit_unknown_became_true_is_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("person", "Ilse Tamsin", "e1", became_true="unknown")
    item = created_id(apply(driver, w, [op], [ev]))
    row = item_row(driver, w, item)
    assert row["title"] == "Ilse Tamsin"
    assert row.get("became_true_earliest") is None
    assert row.get("became_true_latest") is None


def _before_start() -> dict:
    return clock_at(T0 - timedelta(days=1))


def test_end_item_before_its_start_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 10, became_true=clock_at(at_minute(10)))
    ev = observation(chat, "e1", 12, statement_class="report_happened")
    op = {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": item,
        "end_class": "ended_in_the_world",
        "stopped_being_true": _before_start(),
        "evidence_refs": supports("e1"),
        "reason": "the message says it ended",
    }
    refusal = one(refused(driver, w, [op], [ev]), "end_before_start")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, item).get("end_class") is None


def test_end_link_before_its_start_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 10)
    made = create_link(a, "part of", b, "e1", became_true=clock_at(at_minute(10)))
    link = created_id(apply(driver, w, [made], [ev]), ">", "link")
    op = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": link,
        "end_class": "ended_in_the_world",
        "stopped_being_true": _before_start(),
        "reason": "the message says it ended",
    }
    refusal = one(refused(driver, w, [op], []), "end_before_start")
    assert refusal.op_id == op["op_id"]


def test_a_terminal_status_stopped_before_its_start_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 10, became_true=clock_at(at_minute(10)))
    ev = observation(chat, "e1", 12, statement_class="report_happened")
    op = change_status(task, "cancelled", "e1", stopped_being_true=_before_start())
    refusal = one(refused(driver, w, [op], [ev]), "end_before_start")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, task)["status"] == "open"


# Updates ------------------------------------------------------------------------------------


# A text that reads blank: `str.strip` sees the first two and not the others.
BLANK = ["", "   ", "\u200b", "\ufeff\u200e", " \u00ad "]


@pytest.mark.parametrize("title", BLANK)
def test_an_update_that_blanks_the_title_is_refused(driver, seeded, chat, title):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin")
    ev = observation(chat, "e1", 5)
    op = update_item(item, version_of(driver, w, item), {"title": title}, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "required_field"
    assert item_row(driver, w, item)["title"] == "Ilse Tamsin"


@pytest.mark.parametrize("title", BLANK)
def test_a_create_with_a_blank_title_is_refused(driver, seeded, chat, title):
    w = seeded["world_id"]
    op = create_item("person", title, "e1")
    refusal = one(refused(driver, w, [op], [observation(chat, "e1", 5)]), "invalid_arguments")
    assert refusal.rule == "required_field"
    assert read(driver, w, "MATCH (i:Item) RETURN i.id AS id") == []


@pytest.mark.parametrize("reason", BLANK)
def test_a_blank_reason_on_an_operation_is_refused(driver, seeded, chat, reason):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin")
    op = update_item(item, version_of(driver, w, item), {"summary": "runs the venue"}, "e1")
    op["reason"] = reason
    refusal = one(refused(driver, w, [op], [observation(chat, "e1", 5)]), "invalid_arguments")
    assert refusal.rule == "type"


@pytest.mark.parametrize("where", ["create_item", "update_item_fields"])
def test_last_confirmed_outside_verify_is_refused(driver, seeded, chat, where):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 5)
    if where == "create_item":
        op = create_item("person", "Ilse Tamsin", "e1", last_confirmed=iso(at_minute(5)))
    else:
        item = make_item(driver, w, chat, 1)
        fields = {"last_confirmed": iso(at_minute(5))}
        op = update_item(item, version_of(driver, w, item), fields, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "verify_only"


@pytest.mark.parametrize("kind", ["event", "observation", "decision"])
def test_an_edit_to_a_record_claim_is_refused(driver, seeded, chat, kind):
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 1, kind=kind, title="The Okonkwo hall was chosen")
    ev = observation(chat, "e1", 5)
    fields = {"title": "The Tamsin hall was chosen"}
    op = update_item(record, version_of(driver, w, record), fields, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "record_immutable")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, record)["title"] == "The Okonkwo hall was chosen"


def test_search_terms_alone_on_a_record_are_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 1, kind="decision", title="The Okonkwo hall was chosen")
    ev = observation(chat, "e1", 5)
    fields = {"search_terms": ["venue choice", "hall booking"]}
    apply(driver, w, [update_item(record, version_of(driver, w, record), fields, "e1")], [ev])
    row = item_row(driver, w, record)
    assert sorted(row["search_terms"]) == ["hall booking", "venue choice"]
    assert row["title"] == "The Okonkwo hall was chosen"


def test_a_stale_version_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin")
    version = version_of(driver, w, item)
    first = observation(chat, "e1", 5)
    apply(driver, w, [update_item(item, version, {"summary": "runs the venue"}, "e1")], [first])
    assert version_of(driver, w, item) == version + 1

    second = observation(chat, "e2", 6)
    op = update_item(item, version, {"summary": "runs the catering"}, "e2")
    refusal = one(refused(driver, w, [op], [second]), "version_conflict")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, item)["summary"] == "runs the venue"


# Verdicts and scores -------------------------------------------------------------------------


@pytest.mark.parametrize("place", ["op", "evidence_ref", "observation"])
def test_a_caller_verdict_is_refused(driver, seeded, chat, place):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("person", "Ilse Tamsin", "e1")
    target = {"op": op, "evidence_ref": op["evidence_refs"][0], "observation": ev}[place]
    target["verdict"] = "supports"
    one(refused(driver, w, [op], [ev]), "unrecorded_verdict")


def _attach(target: str, ev_id: str, **fields) -> dict[str, Any]:
    return {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": target,
        "ev_id": ev_id,
        "polarity": "supports",
        **fields,
    }


def test_judgment_ids_naming_no_judgment_are_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1)
    ev = observation(chat, "e1", 2)
    op = _attach(item, "e1", judgment_ids=[new_id(w)])
    refusal = one(refused(driver, w, [op], [ev]), "unrecorded_verdict")
    assert refusal.op_id == op["op_id"]


def test_judgment_ids_naming_a_recorded_judgment_are_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1)
    call = plant_node(driver, w, "ModelCall", model="judge-a", job="verify", at=T0)
    judgment = plant_node(
        driver,
        w,
        "Judgment",
        model_call_id=call["id"],
        role="first_judge",
        subject=item,
        verdict="supports",
    )
    ev = observation(chat, "e1", 2)
    apply(driver, w, [_attach(item, "e1", judgment_ids=[judgment["id"]])], [ev])
    rows = evidence_rows(driver, w, item)
    assert len(rows) == 2
    assert {row["polarity"] for row in rows} == {"supports"}


@pytest.mark.parametrize(
    ("key", "value"),
    [("score", 0.92), ("weight", 3), ("band", "high"), ("load_bearing", True)],
)
@pytest.mark.parametrize("place", ["op", "evidence_ref", "observation"])
def test_a_caller_score_weight_band_or_load_bearing_flag_is_refused(
    driver, seeded, chat, place, key, value
):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item("person", "Ilse Tamsin", "e1")
    target = {"op": op, "evidence_ref": op["evidence_refs"][0], "observation": ev}[place]
    target[key] = value
    one(refused(driver, w, [op], [ev]), "caller_score")


# Terminal done ------------------------------------------------------------------------------


@pytest.mark.parametrize("statement_class", ["report_happened", "decision"])
def test_done_with_only_said_or_reported_evidence_is_refused(driver, seeded, chat, statement_class):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e1", 5, statement_class=statement_class)
    op = change_status(task, "done", "e1")
    refusal = one(refused(driver, w, [op], [ev]), "terminal_needs_confirmation")
    assert "reported done" in details_text(refusal)
    assert item_row(driver, w, task)["status"] == "open"


def test_create_done_with_only_reported_evidence_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 5, statement_class="report_happened")
    op = create_item("task", "Draft the venue list", "e1", status="done")
    refusal = one(refused(driver, w, [op], [ev]), "terminal_needs_confirmation")
    assert "reported done" in details_text(refusal)


def test_done_with_a_git_row_in_the_same_batch_is_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    git = make_source(driver, w, "git", GIT_LINES)
    said = observation(chat, "e1", 5, statement_class="report_happened")
    commit = git_observation(git, "g1")
    apply(driver, w, [change_status(task, "done", "e1", "g1")], [said, commit])
    row = item_row(driver, w, task)
    assert row["status"] == "done"
    assert row["status_role"] == "done"
    assert row["derived_source_type"] == "confirmed"
    sources = {r["source_id"]: r["source_type"] for r in evidence_rows(driver, w, task)}
    assert sources[git["source_id"]] == "confirmed"


def test_create_done_with_a_web_page_row_is_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    web = make_source(driver, w, "web_pages", WEB_LINES)
    said = observation(chat, "e1", 5, statement_class="report_happened")
    page = observation(web, "p1", 1, locator="char:0-20", statement_class="report_state")
    op = create_item(
        "task", "Publish the venue list", "e1", status="done", evidence_refs=supports("e1", "p1")
    )
    task = created_id(apply(driver, w, [op], [said, page]))
    row = item_row(driver, w, task)
    assert row["status"] == "done"
    assert row["status_role"] == "done"
    assert row["derived_source_type"] == "confirmed"
    assert len(evidence_rows(driver, w, task)) == 2


def confirmed_premise(driver, world_id: str, git) -> str:
    """One active evidence row of source type confirmed (a git commit), on a separate task."""
    commit = git_observation(git, "g0")
    op = create_item("task", "Merge the venue list", "g0", status="open", recheck_by=RECHECK)
    other = created_id(apply(driver, world_id, [op], [commit]))
    [row] = evidence_rows(driver, world_id, other)
    assert row["source_type"] == "confirmed"
    return row["id"]


def test_done_with_only_an_inferred_row_from_confirmed_premises_is_refused(driver, seeded, chat):
    # D5 rule 2: done needs "an observed confirming row, or ... the parts' confirming rows rolled
    # up"; a claim inferred from confirmed rows "is never read as confirmed".
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    premise = confirmed_premise(driver, w, make_source(driver, w, "git", GIT_LINES))
    ev = observation(chat, "e1", 5, derivation="inferred", premises=[premise])
    refusal = one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev]),
        "terminal_needs_confirmation",
    )
    assert "reported done" in details_text(refusal)
    assert item_row(driver, w, task)["status"] == "open"


def done_part_of(driver, world_id: str, git, parent: str, title="Merge the venue list") -> str:
    """A part of `parent` (step of), created done from a git commit; returns that confirming row."""
    commit = git_observation(git, "g0")
    ops = [
        create_item("task", title, "g0", temp_id="$part", status="done"),
        create_link("$part", "step of", parent, "g0"),
    ]
    receipt = apply(driver, world_id, ops, [commit])
    part = created_id(receipt)
    [row] = evidence_rows(driver, world_id, part)
    assert row["source_type"] == "confirmed"
    return row["id"]


def test_done_with_the_parts_confirming_rows_rolled_up_is_accepted(driver, seeded, chat):
    # The other half of D5 rule 2: "for a parent whose parts are all done, the parts' confirming
    # rows rolled up" (R3).
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    premise = done_part_of(driver, w, make_source(driver, w, "git", GIT_LINES), task)
    ev = observation(chat, "e1", 5, derivation="rolled_up", premises=[premise])
    apply(driver, w, [change_status(task, "done", "e1")], [ev], caller=LEARNER)
    row = item_row(driver, w, task)
    assert row["status"] == "done"
    [rolled] = [r for r in evidence_rows(driver, w, task) if r["derivation"] == "rolled_up"]
    assert rolled["source_type"] == "confirmed"
    assert rolled["premises"] == [premise]


def test_done_rolled_up_from_an_unrelated_items_row_is_refused(driver, seeded, chat):
    # A confirmed row of a task that is no part of this one proves nothing about it (R3).
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    premise = confirmed_premise(driver, w, make_source(driver, w, "git", GIT_LINES))
    ev = observation(chat, "e1", 5, derivation="rolled_up", premises=[premise])
    refusal = one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev], caller=LEARNER),
        "terminal_needs_confirmation",
    )
    assert "reported done" in details_text(refusal)
    assert item_row(driver, w, task)["status"] == "open"


def test_done_rolled_up_while_a_part_is_still_open_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    premise = done_part_of(driver, w, make_source(driver, w, "git", GIT_LINES), task)
    open_part = observation(chat, "e2", 3)
    apply(
        driver,
        w,
        [
            create_item(
                "task",
                "Print the seating plan",
                "e2",
                temp_id="$open",
                status="open",
                recheck_by=RECHECK,
            ),
            create_link("$open", "step of", task, "e2"),
        ],
        [open_part],
    )
    ev = observation(chat, "e1", 5, derivation="rolled_up", premises=[premise])
    one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev], caller=LEARNER),
        "terminal_needs_confirmation",
    )
    assert item_row(driver, w, task)["status"] == "open"


# Near duplicates ----------------------------------------------------------------------------

FIRST_TITLE = "Book the venue for the Okonkwo offsite"
REWORDED_TITLE = "Book a venue for the Okonkwo offsite"


def test_a_reworded_duplicate_title_is_refused_with_its_candidates(driver, seeded, chat):
    w = seeded["world_id"]
    first = make_task(driver, w, chat, 1, title=FIRST_TITLE)
    ev = observation(chat, "e1", 5)
    op = create_item("task", REWORDED_TITLE, "e1", status="open", recheck_by=RECHECK)
    refusal = one(refused(driver, w, [op], [ev]), "near_duplicate")
    assert refusal.op_id == op["op_id"]
    assert first in details_text(refusal)


def test_distinct_from_with_a_reason_accepts_and_stores_the_pair(driver, seeded, chat):
    w = seeded["world_id"]
    first = make_task(driver, w, chat, 1, title=FIRST_TITLE)
    ev = observation(chat, "e1", 5)
    reason = "the spring offsite needs its own booking"
    op = create_item(
        "task",
        REWORDED_TITLE,
        "e1",
        status="open",
        recheck_by=RECHECK,
        distinct_from=[{"item": first, "reason": reason}],
    )
    second = created_id(apply(driver, w, [op], [ev]))
    assert item_row(driver, w, second)["title"] == REWORDED_TITLE
    pairs = read(
        driver,
        w,
        "MATCH (d:DistinctPair) RETURN d.item_a AS a, d.item_b AS b, "
        "d.reason AS reason, d.recorded_by AS recorded_by",
    )
    assert len(pairs) == 1
    assert [pairs[0]["a"], pairs[0]["b"]] == sorted([first, second])
    assert pairs[0]["reason"] is not None
    assert pairs[0]["recorded_by"] == PERSON.actor


# Inferred evidence --------------------------------------------------------------------------


@pytest.mark.parametrize("derivation", ["inferred", "rolled_up"])
def test_an_inferred_row_without_premises_is_refused(driver, seeded, chat, derivation):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1, derivation=derivation, premises=[])
    one(
        refused(driver, w, [create_item("event", "Venue deposit paid", "e1")], [ev]),
        "inferred_evidence",
    )


def test_an_inferred_row_with_premises_is_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    base = make_item(driver, w, chat, 1, kind="event", title="Venue deposit paid")
    [premise] = evidence_rows(driver, w, base)
    ev = observation(chat, "e1", 2, derivation="inferred", premises=[premise["id"]])
    item = created_id(apply(driver, w, [create_item("event", "Venue secured", "e1")], [ev]))
    [row] = evidence_rows(driver, w, item)
    assert row["derivation"] == "inferred"
    assert row["premises"] == [premise["id"]]
    assert item_row(driver, w, item)["derivation"] == "inferred"


# A caller's speaker kind never confirms ---------------------------------------------------------


def test_an_automated_chat_report_does_not_confirm_done(driver, seeded, chat):
    # D2: the source type is "derived in code from the connector and the statement class, never
    # accepted from a caller"; the speaker kind on an observation is the caller's own word.
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e1", 5, statement_class="report_happened", speaker_kind="automated")
    refusal = one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev], caller=LEARNER),
        "terminal_needs_confirmation",
    )
    assert "reported done" in details_text(refusal)
    assert item_row(driver, w, task)["status"] == "open"


def test_an_automated_intention_does_not_create_a_done_task(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 5, statement_class="intention", speaker_kind="automated")
    op = create_item("task", "Ship the venue order", "e1", status="done")
    one(refused(driver, w, [op], [ev], caller=LEARNER), "terminal_needs_confirmation")


# A done that goes to history only still needs confirmation --------------------------------------


def task_in_progress_since_minute_30(driver, world_id: str, src) -> str:
    task = make_task(driver, world_id, src, 1)
    ev = observation(src, "s30", 30)
    apply(driver, world_id, [change_status(task, "in progress", "s30")], [ev])
    return task


def test_an_older_done_from_a_chat_report_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = task_in_progress_since_minute_30(driver, w, chat)
    ev = observation(chat, "e1", 15, statement_class="report_happened")
    one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev], caller=LEARNER),
        "terminal_needs_confirmation",
    )
    query = WorldQuery(driver, w)
    assert item_as_of(query, task, at_minute(17))["status"] == "open"
    assert item_row(driver, w, task)["status"] == "in progress"


def test_an_older_done_with_a_git_row_goes_to_history_only(driver, seeded, chat):
    w = seeded["world_id"]
    task = task_in_progress_since_minute_30(driver, w, chat)
    said = observation(chat, "e1", 15, statement_class="report_happened")
    commit = git_observation(make_source(driver, w, "git", GIT_LINES), "g1")
    receipt = apply(driver, w, [change_status(task, "done", "e1", "g1")], [said, commit])
    assert [line.history_only for line in receipt.lines if line.field == "status"] == [True]
    query = WorldQuery(driver, w)
    assert item_as_of(query, task, at_minute(17))["status"] == "done"
    assert item_row(driver, w, task)["status"] == "in progress"


# The two-judge gate on a contradiction of a belief (D9) -----------------------------------------


def plant_belief(driver, world_id: str) -> str:
    belief = plant_node(
        driver, world_id, "Item", kind=KIND_BELIEF, title="Rowan prefers quiet venues", version=1
    )
    plant_node(driver, world_id, "Belief", item_id=belief["id"], state="tentative", version=1)
    return belief["id"]


def plant_judgment(
    driver, world_id: str, model: str, verdict: str, subject: str, role="first_judge", **extra
) -> str:
    call = plant_node(driver, world_id, "ModelCall", model=model, job="judge", at=T0)
    judgment = plant_node(
        driver,
        world_id,
        "Judgment",
        model_call_id=call["id"],
        role=role,
        subject=subject,
        verdict=verdict,
        **extra,
    )
    return judgment["id"]


def _contradicts(target: str, ev_id: str, judgment_ids: list[str]) -> dict[str, Any]:
    return _attach(target, ev_id, polarity="contradicts", judgment_ids=judgment_ids)


@pytest.mark.parametrize(
    ("judges", "code", "rule"),
    [
        pytest.param([], "second_judge_required", "two_models", id="no_judge"),
        pytest.param([("judge-a", "contradicts")], "second_judge_required", "two_models", id="one"),
        pytest.param(
            [("judge-a", "contradicts"), ("judge-a", "contradicts")],
            "second_judge_required",
            "two_models",
            id="one_model",
        ),
        pytest.param(
            [("judge-a", "contradicts"), ("judge-b", "supports")],
            "judges_disagree",
            "judges_disagree",
            id="disagree",
        ),
        pytest.param(
            [("judge-a", "supports"), ("judge-b", "supports")],
            "judge_not_supports",
            "judge_not_contradicts",
            id="both_support",
        ),
    ],
)
def test_a_contradiction_of_a_belief_without_two_agreeing_models_is_refused(
    driver, seeded, chat, judges, code, rule
):
    w = seeded["world_id"]
    belief = plant_belief(driver, w)
    ids = [
        plant_judgment(driver, w, model, verdict, f"belief:{belief}") for model, verdict in judges
    ]
    ev = observation(chat, "e1", 2)
    refusal = one(refused(driver, w, [_contradicts(belief, "e1", ids)], [ev], caller=LEARNER), code)
    assert refusal.rule == rule
    assert evidence_rows(driver, w, belief) == []


@pytest.mark.parametrize(
    ("subject", "role", "rule"),
    [
        pytest.param("unrelated", "first_judge", "judgment_subject", id="another_subject"),
        pytest.param("belief", "field_fit", "judgment_role", id="not_a_judge_role"),
        pytest.param("belief", "first_judge", "judgment_evidence", id="another_evidence_row"),
    ],
)
def test_judgments_about_something_else_do_not_authorise_a_contradiction(
    driver, seeded, chat, subject, role, rule
):
    # D4: "Every verdict must match a judgment that the model-call layer recorded for that claim
    # and that evidence row".
    w = seeded["world_id"]
    belief = plant_belief(driver, w)
    other = make_item(driver, w, chat, 1)
    ref = f"belief:{belief}" if subject == "belief" else f"item:{other}"
    extra = {"evidence_id": new_id(w)} if rule == "judgment_evidence" else {}
    ids = [
        plant_judgment(driver, w, model, "contradicts", ref, role=role, **extra)
        for model in ("judge-a", "judge-b")
    ]
    ev = observation(chat, "e1", 2)
    refusal = one(
        refused(driver, w, [_contradicts(belief, "e1", ids)], [ev], caller=LEARNER),
        "unrecorded_verdict",
    )
    assert refusal.rule == rule
    assert evidence_rows(driver, w, belief) == []


def test_two_models_that_both_say_contradicts_are_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    belief = plant_belief(driver, w)
    ids = [
        plant_judgment(
            driver,
            w,
            "judge-a",
            "contradicts",
            f"belief:{belief}",
            source_id=chat["source_id"],
            locator="line:2",
        ),
        plant_judgment(
            driver,
            w,
            "judge-b",
            "contradicts",
            f"belief:{belief}",
            role="second_judge",
            source_id=chat["source_id"],
            locator="line:2",
        ),
    ]
    ev = observation(chat, "e1", 2)
    apply(driver, w, [_contradicts(belief, "e1", ids)], [ev], caller=LEARNER)
    [row] = evidence_rows(driver, w, belief)
    assert (row["target_type"], row["polarity"], row["active"]) == ("belief", "contradicts", True)


# Required fields and links, against the merged state ---------------------------------------------


def set_kind(driver, world_id: str, kind_id: str, **columns) -> None:
    with driver.session(database=DATABASE) as session:
        session.run(
            "CYPHER 25 MATCH (k:Kind {world_id: $w, id: $id}) SET k += $columns",
            w=world_id,
            id=kind_id,
            columns=columns,
        ).consume()


@pytest.mark.parametrize("summary", ["   ", "\u200b"])
def test_a_create_with_a_blank_required_field_is_refused(driver, seeded, chat, summary):
    w = seeded["world_id"]
    set_kind(driver, w, seeded["kinds"]["person"], required_fields='["summary"]')
    op = create_item("person", "Ilse Tamsin", "e1", summary=summary)
    refusal = one(refused(driver, w, [op], [observation(chat, "e1", 5)]), "invalid_arguments")
    assert refusal.rule == "required_field"


def test_a_create_without_a_required_field_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    set_kind(driver, w, seeded["kinds"]["person"], required_fields='["summary"]')
    ev = observation(chat, "e1", 1)
    refusal = one(
        refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev]), "invalid_arguments"
    )
    assert refusal.rule == "required_field"
    assert "summary" in details_text(refusal)


@pytest.mark.parametrize("blank", [None, "", "   ", "\u200b"])
def test_an_update_that_blanks_a_required_field_is_refused(driver, seeded, chat, blank):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin", summary="runs the venue")
    set_kind(driver, w, seeded["kinds"]["person"], required_fields='["summary"]')
    ev = observation(chat, "e1", 5)
    op = update_item(item, version_of(driver, w, item), {"summary": blank}, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "required_field"
    assert item_row(driver, w, item)["summary"] == "runs the venue"


def test_a_required_link_is_checked_at_the_end_of_the_batch(driver, seeded, chat):
    w = seeded["world_id"]
    lives_in = seeded["meanings"]["lives in"]
    set_kind(driver, w, seeded["kinds"]["person"], required_links=json.dumps([lives_in]))
    ev = observation(chat, "e1", 1)
    refusal = one(
        refused(driver, w, [create_item("person", "Ilse Tamsin", "e1")], [ev]), "invalid_arguments"
    )
    assert refusal.rule == "required_link"

    ops = [
        create_item("place", "Harbour flat", "e1", temp_id="$flat"),
        create_item("person", "Ilse Tamsin", "e1", temp_id="$ilse"),
        create_link("$ilse", "lives in", "$flat", "e1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, w, ops, [ev])
    assert [(line.symbol, line.entity_type) for line in receipt.lines] == [
        ("+", "item"),
        ("+", "item"),
        (">", "link"),
    ]


# Premises must be active rows of this world ------------------------------------------------------


@pytest.mark.parametrize("derivation", ["inferred", "rolled_up"])
def test_a_premise_that_names_no_row_is_refused(driver, seeded, chat, derivation):
    w = seeded["world_id"]
    missing = new_id(w)
    ev = observation(chat, "e1", 1, derivation=derivation, premises=[missing])
    refusal = one(
        refused(driver, w, [create_item("event", "Venue deposit paid", "e1")], [ev]),
        "inferred_evidence",
    )
    assert refusal.rule == "premise_not_found"
    assert missing in details_text(refusal)


def test_an_ended_premise_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    premise = done_part_of(driver, w, make_source(driver, w, "git", GIT_LINES), task)
    end = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": premise,
        "end_reason": "correction",
    }
    apply(driver, w, [end], [])
    ev = observation(chat, "e1", 5, derivation="rolled_up", premises=[premise])
    refusal = one(
        refused(driver, w, [change_status(task, "done", "e1")], [ev]), "inferred_evidence"
    )
    assert refusal.rule == "premise_not_found"
    assert item_row(driver, w, task)["status"] == "open"


# Citing an existing row by evidence_id ------------------------------------------------------------


def _cite(op: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    return {**op, "evidence_refs": [{"evidence_id": evidence_id, "polarity": "supports"}]}


def test_an_ended_row_cited_by_evidence_id_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin")
    other = make_item(driver, w, chat, 2, title="Rowan Okonkwo")
    [row] = evidence_rows(driver, w, item)
    end = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": row["id"],
        "end_reason": "forget",
    }
    apply(driver, w, [end], [])

    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": other,
        "evidence_id": row["id"],
        "polarity": "supports",
    }
    update = _cite(
        update_item(other, version_of(driver, w, other), {"summary": "a host"}, "x"), row["id"]
    )
    for op in (attach, update):
        refusal = one(refused(driver, w, [op], []), "missing_provenance")
        assert refusal.rule == "evidence_not_found"
    assert len(evidence_rows(driver, w, other)) == 1


def test_an_active_row_cited_by_evidence_id_lends_its_observation(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1, title="Ilse Tamsin")
    other = make_item(driver, w, chat, 2, title="Rowan Okonkwo")
    [row] = evidence_rows(driver, w, item)
    attach = {
        "op": "attach_evidence",
        "op_id": new_op_id(),
        "target": other,
        "evidence_id": row["id"],
        "polarity": "supports",
    }
    apply(driver, w, [attach], [])
    [copied] = [
        r for r in evidence_rows(driver, w, other) if r["message_key"] == row["message_key"]
    ]
    assert copied["id"] != row["id"]
    for key in ("locator", "source_id", "episode_id", "source_type", "statement_class"):
        assert copied[key] == row[key]


# Every change entry names a real evidence row ----------------------------------------------------


def entry_evidence_ids(driver, world_id: str, item_id: str) -> list[str]:
    rows = read(
        driver,
        world_id,
        "MATCH (c:ChangeEntry {entity_id: $id}) WHERE c.evidence_id IS NOT NULL "
        "RETURN c.evidence_id AS id",
        id=item_id,
    )
    return [row["id"] for row in rows]


def test_a_restated_observation_or_a_cited_row_names_the_existing_row(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 5)
    [row] = evidence_rows(driver, w, task)
    again = observation(chat, "e1", 5)  # the same message, as from an overlapping export
    apply(driver, w, [change_status(task, "in progress", "e1")], [again])
    cited = _cite(
        update_item(task, version_of(driver, w, task), {"summary": "Ilse"}, "x"), row["id"]
    )
    apply(driver, w, [cited], [])

    ids = entry_evidence_ids(driver, w, task)
    assert len(ids) >= 5
    known = {r["id"] for r in read(driver, w, "MATCH (e:Evidence) RETURN e.id AS id")}
    assert set(ids) <= known
    assert set(ids) == {row["id"]}
    assert item_row(driver, w, task)["provenance_evidence_id"] == row["id"]
    assert len(evidence_rows(driver, w, task)) == 1


def test_done_citing_the_items_own_confirmed_row_is_accepted(driver, seeded, chat):
    w = seeded["world_id"]
    commit = git_observation(make_source(driver, w, "git", GIT_LINES), "g1")
    op = create_item("task", "Merge the venue list", "g1", status="open", recheck_by=RECHECK)
    task = created_id(apply(driver, w, [op], [commit]))
    [row] = evidence_rows(driver, w, task)
    apply(driver, w, [_cite(change_status(task, "done"), row["id"])], [])
    stored = item_row(driver, w, task)
    assert (stored["status"], stored["derived_source_type"]) == ("done", "confirmed")
    assert set(entry_evidence_ids(driver, w, task)) == {row["id"]}


# A terminal status at create carries its end time (D1) --------------------------------------------


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_terminal_status_at_create_stores_its_end_time(driver, seeded, chat, status):
    w = seeded["world_id"]
    web = make_source(driver, w, "web_pages", WEB_LINES)
    said = observation(chat, "e1", 5, statement_class="report_happened")
    page = observation(web, "p1", 1, locator="char:0-20", statement_class="report_state")
    op = create_item(
        "task", "Publish the venue list", "e1", status=status, evidence_refs=supports("e1", "p1")
    )
    row = item_row(driver, w, created_id(apply(driver, w, [op], [said, page])))
    assert row["status"] == status
    assert row["stopped_being_true_precision"] == "before"
    assert instant(row["stopped_being_true_latest"]) == at_minute(5)
    assert row.get("stopped_being_true_earliest") is None


def test_a_terminal_status_at_create_before_its_start_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 5, statement_class="report_happened")
    op = create_item(
        "task", "Drop the venue list", "e1", status="cancelled", became_true=clock_at(at_minute(20))
    )
    one(refused(driver, w, [op], [ev]), "end_before_start")


# A primitive link moves only with its composite operation ----------------------------------------


@pytest.mark.parametrize(
    "meaning", [MEANING_DESCRIBES, MEANING_CORRECTED_BY], ids=["describes", "corrected_by"]
)
@pytest.mark.parametrize("caller", [LEARNER, PERSON], ids=["update", "person"])
def test_end_link_of_a_primitive_meaning_is_refused(driver, seeded, chat, meaning, caller):
    w = seeded["world_id"]
    a = plant_belief(driver, w) if meaning == MEANING_DESCRIBES else make_item(driver, w, chat, 1)
    b = make_item(driver, w, chat, 2, title="Ilse Tamsin")
    key = f"{w}|{a}|{meaning}|{b}"
    link = plant_link(
        driver, w, a, b, meaning=meaning, active_key=key, became_true_precision="unknown", version=1
    )
    op = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": link["id"],
        "end_class": "retired",
        "stopped_being_true": "unknown",
        "reason": "the agent tidies up",
    }
    refusal = one(refused(driver, w, [op], [], caller=caller), "unknown_meaning")
    assert refusal.rule == "primitive_meaning"
    [stored] = read(driver, w, "MATCH (:Item)-[l:LINK {id: $id}]->(:Item) RETURN l", id=link["id"])
    assert stored["l"]["active_key"] == key


# Work with a state carries a status at create (lane decision) -------------------------------------


@pytest.mark.parametrize("kind", ["task", "plan", "problem", "assumption"])
def test_a_work_item_without_a_status_is_refused_with_the_valid_list(driver, seeded, chat, kind):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    op = create_item(kind, "Book the harbour hall", "e1", recheck_by=RECHECK)
    refusal = one(refused(driver, w, [op], [ev]), "unknown_status")
    assert refusal.rule == "status_required"
    assert refusal.op_id == op["op_id"]
    assert "reported done" in details_text(refusal)


@pytest.mark.parametrize("kind", ["organisation", "person", "trip", "event"])
def test_other_kinds_keep_status_optional(driver, seeded, chat, kind):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 1)
    item = created_id(apply(driver, w, [create_item(kind, "Okonkwo Hall", "e1")], [ev]))
    assert item_row(driver, w, item).get("status") is None


# Second endings, live stops and original phrases --------------------------------------------


def _end_item_op(item: str, minute: int, ev_id: str) -> dict[str, Any]:
    return {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": item,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(minute)),
        "evidence_refs": supports(ev_id),
        "reason": "the message says it ended",
    }


def test_ending_an_item_twice_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 10, became_true=clock_at(at_minute(10)))
    first = observation(chat, "e1", 20, statement_class="report_happened")
    apply(driver, w, [_end_item_op(item, 20, "e1")], [first])
    stored = item_row(driver, w, item)["stopped_being_true_latest"]

    again = observation(chat, "e2", 30, statement_class="report_happened")
    refusal = one(refused(driver, w, [_end_item_op(item, 30, "e2")], [again]), "invalid_arguments")
    assert refusal.rule == "already_ended"
    assert item_row(driver, w, item)["stopped_being_true_latest"] == stored


def test_ending_a_link_twice_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 10)
    made = create_link(a, "part of", b, "e1", became_true=clock_at(at_minute(10)))
    link = created_id(apply(driver, w, [made], [ev]), ">", "link")

    def end(minute: int) -> dict[str, Any]:
        return {
            "op": "end_link",
            "op_id": new_op_id(),
            "link": link,
            "end_class": "ended_in_the_world",
            "stopped_being_true": clock_at(at_minute(minute)),
            "reason": "the message says it ended",
        }

    apply(driver, w, [end(20)], [])
    refusal = one(refused(driver, w, [end(30)], []), "invalid_arguments")
    assert refusal.rule == "already_ended"


def test_ending_an_evidence_row_twice_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 4)
    [row] = evidence_rows(driver, w, item)
    end = {"op": "end_evidence", "evidence": row["id"], "end_reason": "correction"}
    apply(driver, w, [{**end, "op_id": new_op_id()}], [])
    refusal = one(refused(driver, w, [{**end, "op_id": new_op_id()}], []), "invalid_arguments")
    assert refusal.rule == "already_ended"


def test_a_live_status_with_stopped_being_true_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 10, became_true=clock_at(at_minute(10)))
    ev = observation(chat, "e1", 12)
    op = change_status(task, "in progress", "e1", stopped_being_true=clock_at(at_minute(12)))
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "stopped_needs_terminal"
    assert item_row(driver, w, task)["status"] == "open"


def test_an_original_phrase_on_a_named_meaning_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 10)
    op = create_link(a, "part of", b, "e1", original_phrase="sits inside")
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "original_phrase"


def test_an_original_phrase_on_the_generic_meaning_is_stored(driver, seeded, chat):
    w = seeded["world_id"]
    a = make_item(driver, w, chat, 1, kind="place", title="Alfama")
    b = make_item(driver, w, chat, 2, kind="place", title="Lisbon")
    ev = observation(chat, "e1", 10)
    op = create_link(a, "related to", b, "e1", original_phrase="sits near", recheck_by=RECHECK)
    link = created_id(apply(driver, w, [op], [ev]), ">", "link")
    rows = read(driver, w, "MATCH ()-[l:LINK {id: $id}]->() RETURN l", id=link)
    assert rows[0]["l"]["original_phrase"] is not None


# B.2, one row at a time: the validations a batch sends with a field missing or wrong -------


END_FIELD_CASES = [
    pytest.param("end_class", None, "missing_field", id="no_end_class"),
    pytest.param("end_class", "because", "enum", id="bad_end_class"),
    pytest.param("stopped_being_true", None, "missing_field", id="no_end_time"),
]
RECORD_EDITS = [
    pytest.param({"summary": "The Tamsin hall was chosen"}, id="summary"),
    pytest.param({"body": "The Tamsin hall was chosen in the end"}, id="body"),
    pytest.param({"became_true": clock_at(at_minute(7))}, id="became_true"),
    pytest.param({"properties": {"venue": "Tamsin hall"}}, id="properties"),
    pytest.param({"recheck_by": RECHECK}, id="recheck_by"),
    pytest.param(
        {"title": "The Tamsin hall was chosen", "search_terms": ["hall"]},
        id="title_with_search_terms",
    ),
]


def _with_field(op: dict[str, Any], name: str, value: Any) -> dict[str, Any]:
    """The operation with one field replaced, or removed when the value is None."""
    changed = {key: val for key, val in op.items() if key != name}
    return changed if value is None else {**changed, name: value}


def _part_of_link(driver, world_id: str, src) -> str:
    a = make_item(driver, world_id, src, 1, kind="place", title="Alfama")
    b = make_item(driver, world_id, src, 2, kind="place", title="Lisbon")
    ev = observation(src, "l1", 3)
    made = create_link(a, "part of", b, "l1", became_true=clock_at(at_minute(3)))
    return created_id(apply(driver, world_id, [made], [ev]), ">", "link")


def _link_row(driver, world_id: str, link_id: str) -> dict[str, Any]:
    rows = read(driver, world_id, "MATCH ()-[l:LINK {id: $id}]->() RETURN l", id=link_id)
    assert len(rows) == 1
    return rows[0]["l"]


def test_a_kind_alias_resolves_to_its_canonical_kind(driver, seeded, chat):
    w = seeded["world_id"]
    plant_node(
        driver,
        w,
        "Alias",
        target_type="kind",
        target_id=seeded["kinds"]["task"],
        phrase="chore",
        normalised_phrase="chore",
        status="canonical",
        reversed=False,
    )
    ev = observation(chat, "e1", 1)
    op = create_item("chore", "Paint the mast", "e1", status="open", recheck_by=RECHECK)
    item = created_id(apply(driver, w, [op], [ev]))
    assert item_row(driver, w, item)["kind"] == seeded["kinds"]["task"]


@pytest.mark.parametrize(
    "entry",
    [{}, {"reason": "   "}, {"reason": "\u200b"}],
    ids=["absent", "blank", "format-characters"],
)
def test_distinct_from_without_a_reason_is_refused(driver, seeded, chat, entry):
    w = seeded["world_id"]
    first = make_task(driver, w, chat, 1, title=FIRST_TITLE)
    ev = observation(chat, "e1", 5)
    op = create_item(
        "task",
        REWORDED_TITLE,
        "e1",
        status="open",
        recheck_by=RECHECK,
        distinct_from=[{"item": first, **entry}],
    )
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == "distinct_reason"
    assert refusal.op_id == op["op_id"]


@pytest.mark.parametrize("fields", RECORD_EDITS)
def test_every_field_of_a_record_claim_is_refused(driver, seeded, chat, fields):
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 1, kind="decision", title="The Okonkwo hall was chosen")
    before = item_row(driver, w, record)
    ev = observation(chat, "e1", 5)
    op = update_item(record, before["version"], fields, "e1")
    refusal = one(refused(driver, w, [op], [ev]), "record_immutable")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, record) == before


def test_change_status_outside_the_vocabulary_is_refused_with_the_valid_list(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e1", 5)
    op = change_status(task, "almost finished", "e1")
    refusal = one(refused(driver, w, [op], [ev]), "unknown_status")
    assert refusal.op_id == op["op_id"]
    assert "in progress" in details_text(refusal)
    assert item_row(driver, w, task)["status"] == "open"


@pytest.mark.parametrize(("name", "value", "rule"), END_FIELD_CASES)
def test_end_item_without_a_valid_end_class_or_end_time_is_refused(
    driver, seeded, chat, name, value, rule
):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e1", 20, statement_class="report_happened")
    op = _with_field(_end_item_op(task, 20, "e1"), name, value)
    refusal = one(refused(driver, w, [op], [ev]), "invalid_arguments")
    assert refusal.rule == rule
    assert refusal.op_id == op["op_id"]
    assert refusal.details["field"] == name
    assert item_row(driver, w, task).get("end_class") is None


@pytest.mark.parametrize(("name", "value", "rule"), END_FIELD_CASES)
def test_end_link_without_a_valid_end_class_or_end_time_is_refused(
    driver, seeded, chat, name, value, rule
):
    w = seeded["world_id"]
    link = _part_of_link(driver, w, chat)
    whole = {
        "op": "end_link",
        "op_id": new_op_id(),
        "link": link,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(at_minute(20)),
        "reason": "the message says it ended",
    }
    op = _with_field(whole, name, value)
    refusal = one(refused(driver, w, [op], []), "invalid_arguments")
    assert refusal.rule == rule
    assert refusal.op_id == op["op_id"]
    assert refusal.details["field"] == name
    assert _link_row(driver, w, link)["active_key"] is not None


def test_reopen_without_a_reason_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    task = make_task(driver, w, chat, 1)
    ev = observation(chat, "e1", 20, statement_class="report_happened")
    apply(driver, w, [_end_item_op(task, 20, "e1")], [ev])
    op = {"op": "reopen_item", "op_id": new_op_id(), "item": task, "recheck_by": RECHECK}
    refusal = one(refused(driver, w, [op], []), "invalid_arguments")
    assert refusal.rule == "missing_field"
    assert refusal.details["field"] == "reason"
    assert item_row(driver, w, task)["end_class"] == "ended_in_the_world"


@pytest.mark.parametrize(
    ("value", "rule"),
    [(None, "missing_field"), ("because", "enum")],
    ids=["no_end_reason", "bad_end_reason"],
)
def test_end_evidence_without_a_valid_reason_is_refused(driver, seeded, chat, value, rule):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 4)
    [row] = evidence_rows(driver, w, item)
    whole = {
        "op": "end_evidence",
        "op_id": new_op_id(),
        "evidence": row["id"],
        "end_reason": "correction",
    }
    op = _with_field(whole, "end_reason", value)
    refusal = one(refused(driver, w, [op], []), "invalid_arguments")
    assert refusal.rule == rule
    assert refusal.details["field"] == "end_reason"
    [after] = evidence_rows(driver, w, item)
    assert after["active"] is True


def test_attach_evidence_to_an_absent_target_is_refused(driver, seeded, chat):
    w = seeded["world_id"]
    ev = observation(chat, "e1", 5)
    op = _attach(new_id(w), "e1")
    refusal = one(refused(driver, w, [op], [ev]), "not_found")
    assert refusal.rule == "target_not_found"
    assert refusal.op_id == op["op_id"]


def test_attach_evidence_is_unique_on_message_target_and_polarity(driver, seeded, chat):
    w = seeded["world_id"]
    item = make_item(driver, w, chat, 1)
    assert len(evidence_rows(driver, w, item)) == 1

    apply(driver, w, [_attach(item, "e1")], [observation(chat, "e1", 5)])
    assert len(evidence_rows(driver, w, item)) == 2

    same_message = observation(chat, "e2", 5)
    apply(driver, w, [_attach(item, "e2")], [same_message])
    assert len(evidence_rows(driver, w, item)) == 2

    other_polarity = observation(chat, "e3", 5)
    apply(driver, w, [_attach(item, "e3", polarity="contradicts")], [other_polarity])
    rows = evidence_rows(driver, w, item)
    assert len(rows) == 3
    assert sorted(row["polarity"] for row in rows) == ["contradicts", "supports", "supports"]


@pytest.mark.parametrize("op_name", ["change_status", "end_item", "reopen_item"])
def test_a_record_refuses_a_status_an_end_and_a_reopen(driver, seeded, chat, op_name):
    w = seeded["world_id"]
    record = make_item(driver, w, chat, 1, kind="decision", title="The Okonkwo hall was chosen")
    before = item_row(driver, w, record)
    evidence = [observation(chat, "e1", 20, statement_class="report_happened")]
    if op_name == "change_status":
        op = change_status(record, "done", "e1")
    elif op_name == "end_item":
        op = _end_item_op(record, 20, "e1")
    else:
        op = {"op": "reopen_item", "op_id": new_op_id(), "item": record, "reason": "a new message"}
        evidence = []
    refusal = one(refused(driver, w, [op], evidence), "record_immutable")
    assert refusal.op_id == op["op_id"]
    assert item_row(driver, w, record) == before


# Guards a neuter found without a test ------------------------------------------------------


def test_a_title_close_to_an_items_alias_is_a_near_duplicate(driver, seeded, chat):
    w = seeded["world_id"]
    first = make_task(driver, w, chat, 1, title="Harbour hall reservation")
    plant_node(
        driver,
        w,
        "Alias",
        target_type="item",
        target_id=first,
        phrase=FIRST_TITLE,
        normalised_phrase=FIRST_TITLE.lower(),
        status="canonical",
        reversed=False,
    )
    ev = observation(chat, "e1", 5)
    op = create_item("task", REWORDED_TITLE, "e1", status="open", recheck_by=RECHECK)
    refusal = one(refused(driver, w, [op], [ev]), "near_duplicate")
    assert first in details_text(refusal)


def test_a_forgotten_source_is_not_citable(driver, seeded, chat):
    w = seeded["world_id"]
    with driver.session(database=DATABASE) as session:
        session.run(
            "MATCH (s:Source {id: $id, world_id: $w}) SET s.forget_state = 'forgotten'",
            id=chat["source_id"],
            w=w,
        ).consume()
    ev = observation(chat, "e1", 5)
    op = create_item("person", "Rowan Okonkwo", "e1")
    refusal = one(refused(driver, w, [op], [ev]), "unresolvable_locator")
    assert refusal.rule == "source_forgotten"


def test_one_message_cited_twice_in_one_operation_writes_one_row(driver, seeded, chat):
    w = seeded["world_id"]
    first = observation(chat, "e1", 5)
    second = observation(chat, "e2", 5)
    assert first["message_key"] == second["message_key"]
    op = create_item("person", "Rowan Okonkwo", "e1", evidence_refs=supports("e1", "e2"))
    item = created_id(apply(driver, w, [op], [first, second]))
    [row] = evidence_rows(driver, w, item)
    assert item_row(driver, w, item)["provenance_evidence_id"] == row["id"]


def test_a_scripted_connector_stands_in_for_the_type_it_imitates(driver, seeded):
    w = seeded["world_id"]
    src = make_source(driver, w, "scripted", GIT_LINES, settings={"imitates": "git"})
    op = create_item("event", "Venue list committed", "g1")
    item = created_id(apply(driver, w, [op], [git_observation(src, "g1")]))
    [row] = evidence_rows(driver, w, item)
    assert row["source_type"] == "confirmed"
    assert item_row(driver, w, item)["derived_source_type"] == "confirmed"


# A late end time is kept in the history, never dropped (writer-b closed the open point) -----


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_an_end_time_on_a_change_that_goes_to_history_only_is_kept_in_history(
    driver, seeded, chat, status
):
    w = seeded["world_id"]
    task = task_in_progress_since_minute_30(driver, w, chat)
    said = observation(chat, "e1", 15, statement_class="report_happened")
    commit = git_observation(make_source(driver, w, "git", GIT_LINES), "g1")
    op = change_status(task, status, "e1", "g1", stopped_being_true=clock_at(at_minute(16)))
    apply(driver, w, [op], [said, commit])
    head = item_row(driver, w, task)
    assert head["status"] == "in progress"
    assert head.get("stopped_being_true_precision") is None
    [stop] = read(
        driver,
        w,
        "MATCH (e:ChangeEntry {entity_id: $id, field: 'stopped_being_true'}) RETURN e",
        id=task,
    )
    assert stop["e"]["history_only"] is True
    then = item_as_of(WorldQuery(driver, w), task, at_minute(17))
    assert then["status"] == status
    assert then["stopped_being_true_latest"] == iso(at_minute(16))
    # Once the later live status holds, the late end time no longer reads as the item's end.
    assert item_as_of(WorldQuery(driver, w), task, at_minute(31))["status"] == "in progress"
