"""Invented exports through real retention, legacy queue and progress projection.

The tests never start OpenCode or call a model. A deterministic writer proposal
exercises attribution; actual model extraction remains a separate acceptance step.
"""

import json
from datetime import timedelta
from itertools import islice

import pytest

from grove import clock
from grove.ingest.import_parts import (
    MAX_IMPORT_PARTS,
    MAX_PART_BYTES,
    MAX_PART_MESSAGES,
    ImportLimit,
)
from grove.ingest.import_progress import read_import_progress, source_state
from grove.ingest.take_in import take_in
from grove.models.config import load_catalog
from grove.models.gateway_auth import IMPORT_PROFILE, verify
from grove.runner.brain import pass_input
from grove.runner.passes import PassSettings, caller_for, end_pass, run_record, start_pass
from grove.store.query import WorldQuery
from grove.writer import apply_request, finish_request, import_queue, request_state
from grove.writer.errors import InvalidRecord
from grove.writer.import_queue import request_id_for
from grove.writer.testing import plant_node
from grove.writer.tx import WorldWriter
from tests.hosts import conftest as host_fixtures
from tests.ingest.conftest import STORE_TIME, counts, make_screener, upload_of
from tests.ingest.test_chatgpt import Conversation, message
from tests.ingest.test_claude_ai import conversation, record
from tests.runner.helpers import new_auth, queue_store

hosts_plant = host_fixtures.hosts_plant


def export(provider="claude_ai", *, count=1, chat="orchard", text=None, untimed=False):
    words = text or "Mira Vale tends the invented pear orchard."
    if provider == "claude_ai":
        records = [record(f"m{n}", "human", seconds=n, text=words) for n in range(count)]
        if untimed:
            records[0]["created_at"] = None
        doc = conversation(chat, records)
    else:
        tree = Conversation(chat)
        for n in range(count):
            tree.add(message(f"m{n}", "user", [words], seconds=None if untimed else n))
        doc = tree.export()
    return json.dumps([doc]).encode()


def ingest(driver, world, provider="claude_ai", **options):
    return take_in(
        driver, world, provider, upload_of(export(provider, **options)), screener=make_screener()
    )


def request_rows(driver, world):
    return [
        row["r"]
        for row in WorldQuery(driver, world).read("MATCH (r:Request) RETURN r ORDER BY r.id")
    ]


def grant_for(driver, world):
    auth = new_auth()[0]
    grant = start_pass(driver, auth, world, agent="learner", settings=PassSettings())
    return auth, grant


def proposal(driver, world, grant):
    form = pass_input(driver, world, run_record(driver, world, grant.run_id))["batch_form"]
    batch = form["batch"]
    batch["ops"] = [batch["ops"][0] | {"title": "Mira Vale"}]
    return form["request_id"], batch


@pytest.mark.parametrize("provider", ["claude_ai", "chatgpt"])
def test_export_queues_once_and_overlap_adds_only_new_keys(
    driver, seeded_world, test_clock, provider
):
    first = ingest(driver, seeded_world, provider)
    assert first.requests_added == first.sources_added == 1
    assert first.held_sources == 0
    rows = request_rows(driver, seeded_world)
    assert [r["id"] for r in rows] == [request_id_for(first.source_ids[0])]
    assert rows[0]["status"] == "queued" and rows[0].get("protocol") is None
    assert "Mira Vale" not in json.dumps(rows, default=str)
    again = ingest(driver, seeded_world, provider)
    assert again.state == "nothing_new" and again.requests_added == 0
    overlap = ingest(driver, seeded_world, provider, count=2)
    assert overlap.messages_read == overlap.sources_added == overlap.requests_added == 1
    assert len(request_rows(driver, seeded_world)) == 2
    progress = read_import_progress(driver, seeded_world, provider)
    assert progress["queued_count"] == progress["requests_total"] == 2
    assert not any(progress["graph"].values())


def test_parts_preserve_whole_messages_and_one_logical_episode(driver, seeded_world, test_clock):
    result = ingest(driver, seeded_world, count=MAX_PART_MESSAGES + 1)
    assert result.sources_added == result.requests_added == 2
    assert result.episodes_added == 1
    rows = WorldQuery(driver, seeded_world).read("MATCH (s:Source) RETURN s.content AS content")
    assert sum(len(json.loads(row["content"])) for row in rows) == MAX_PART_MESSAGES + 1
    assert all(len(row["content"].encode()) <= MAX_PART_BYTES for row in rows)
    assert all(len(json.loads(row["content"])) <= MAX_PART_MESSAGES for row in rows)
    assert all(
        m["text"] == "Mira Vale tends the invented pear orchard."
        for row in rows
        for m in json.loads(row["content"])
    )


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"untimed": True}, "source_time_unknown"),
        ({"text": "pear " * 8000}, "input_too_large"),
    ],
)
def test_ineligible_input_is_retained_and_never_claimed(
    driver, seeded_world, test_clock, options, reason
):
    result = ingest(driver, seeded_world, **options)
    assert result.sources_added == result.requests_added == result.held_sources == 1
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["state"] == "held" and progress["held_count"] == 1
    assert progress["sources"][0]["reason_code"] == reason
    assert grant_for(driver, seeded_world)[1] is None
    assert not any(progress["graph"].values())


def test_over_limit_export_has_no_partial_source_or_request_write(driver, seeded_world, test_clock):
    docs = [json.loads(export(chat=f"orchard-{n}"))[0] for n in range(MAX_IMPORT_PARTS + 1)]
    with pytest.raises(ImportLimit):
        take_in(
            driver,
            seeded_world,
            "claude_ai",
            upload_of(json.dumps(docs).encode()),
            screener=make_screener(),
        )
    found = counts(driver, seeded_world)
    assert found["sources"] == found["episodes"] == found["receipts"] == 0
    assert request_rows(driver, seeded_world) == []


def test_queue_failure_rolls_back_only_its_chat(driver, seeded_world, test_clock, monkeypatch):
    original = import_queue.enqueue_import_parts_in_tx
    calls = []

    def enqueue(tx, source_ids, provider):
        result = original(tx, source_ids, provider)
        calls.append(source_ids)
        if len(calls) == 2:
            raise InvalidRecord("invalid_arguments", "test_queue_failure", "invented queue failure")
        return result

    monkeypatch.setattr(import_queue, "enqueue_import_parts_in_tx", enqueue)
    docs = [json.loads(export(chat=chat))[0] for chat in ("first", "second")]
    result = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(json.dumps(docs).encode()),
        screener=make_screener(),
    )
    assert result.failed_chats == 1 and result.state == "taken_in"
    assert result.requests_added == result.sources_added == 1
    found = counts(driver, seeded_world)
    assert found["sources"] == found["episodes"] == found["receipts"] == 1
    assert len(request_rows(driver, seeded_world)) == 1


@pytest.mark.parametrize("provider", ["claude_ai", "chatgpt"])
def test_partial_upload_receipt_stays_separate_from_complete_provider_history(
    driver, seeded_world, client_for, test_clock, monkeypatch, record_property, provider
):
    owner = client_for(seeded_world)
    reminders = "/v1/sources/pending-exports"
    requested = owner.post(f"{reminders}/{provider}", json={})
    assert requested.status_code == 200
    pending = requested.json()
    original = import_queue.enqueue_import_parts_in_tx
    calls = []

    def enqueue(tx, source_ids, selected_provider):
        result = original(tx, source_ids, selected_provider)
        calls.append(source_ids)
        if len(calls) == 2:
            raise InvalidRecord("invalid_arguments", "test_queue_failure", "invented queue failure")
        return result

    monkeypatch.setattr(import_queue, "enqueue_import_parts_in_tx", enqueue)
    docs = [json.loads(export(provider, chat=chat))[0] for chat in ("first", "second")]
    response = owner.post(
        f"/v1/sources/upload?provider={provider}",
        content=json.dumps(docs).encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["state"] == "taken_in"
    assert receipt["failed_chats"] == 1
    assert receipt["sources_added"] == receipt["processing"]["requests_added"] == 1
    assert "reminder" not in receipt
    assert owner.get(reminders).json() == pending

    before = owner.get(receipt["processing"]["status_url"])
    assert before.status_code == 200
    progress = before.json()
    rows = request_rows(driver, seeded_world)
    assert len(rows) == 1
    # Keep the observed stored/native comparison separate from the admission
    # result. The receipt contains booleans only, never retained source values.
    temporal = WorldQuery(driver, seeded_world).read(
        "MATCH (r:Request {id: $id}) "
        "RETURN r.said_at < $at AS stored_before_native, "
        "r.said_at = $at AS same_native_value, "
        "r.said_at.epochSeconds = $at.epochSeconds AS same_epoch_second, "
        "r.said_at.nanosecond = $at.nanosecond AS same_nanosecond",
        id=rows[0]["id"],
        at=rows[0]["said_at"],
    )[0]
    record_property("request_time_roundtrip", json.dumps(temporal, sort_keys=True))
    assert temporal["same_epoch_second"] and temporal["same_nanosecond"], temporal
    classification = {
        "progress": progress["state"],
        "queued": progress["queued_count"],
        "held": progress["held_count"],
        "requests": len(rows),
        "request_states": [row["status"] for row in rows],
        "request_errors": [row.get("last_error_code") for row in rows],
        "eligibility": [
            import_queue.readiness_reason(WorldQuery(driver, seeded_world), row) for row in rows
        ],
    }
    assert classification == {
        "progress": "queued",
        "queued": 1,
        "held": 0,
        "requests": 1,
        "request_states": ["queued"],
        "request_errors": [None],
        "eligibility": [None],
    }, classification

    # A real saved no-op completes the only retained Request. The failed chat
    # never entered provider history and must not become a successful upload.
    _, grant = grant_for(driver, seeded_world)
    assert grant is not None, classification
    finish_request(
        driver, seeded_world, caller_for("learner"), grant.input_ids[0], worker=grant.run_id
    )
    end_pass(driver, seeded_world, grant.run_id, outcome="ok")
    status = owner.get(receipt["processing"]["status_url"])
    assert status.status_code == 200
    history = status.json()
    assert history["state"] == "complete"
    assert history["requests_total"] == history["held_nothing_count"] == 1
    assert history["failed_count"] == history["extracted_count"] == 0
    assert not any(history["graph"].values())
    assert "failed_chats" not in history
    assert owner.get(reminders).json() == pending


def test_an_ordinary_pass_keeps_its_token_cap(driver, seeded_world, test_clock):
    queue_store(driver, seeded_world)
    auth, grant = grant_for(driver, seeded_world)
    claims = verify(auth.gateway_secret, load_catalog(), grant.gateway_token)
    assert claims.profile is None and claims.token_cap == PassSettings().token_cap
    assert run_record(driver, seeded_world, grant.run_id)["token_cap"] == 300_000


def test_one_import_per_pass_keeps_message_order_and_signed_limits(
    driver, seeded_world, test_clock
):
    result = ingest(driver, seeded_world, count=MAX_PART_MESSAGES + 1)
    auth, first = grant_for(driver, seeded_world)
    assert first.input_ids == [request_id_for(result.source_ids[0])]
    assert first.time_cap_seconds == PassSettings().time_cap_seconds == 600
    claims = verify(auth.gateway_secret, load_catalog(), first.gateway_token)
    assert claims.profile == IMPORT_PROFILE and claims.token_cap is None
    assert "token_cap" not in run_record(driver, seeded_world, first.run_id)
    packet = pass_input(driver, seeded_world, run_record(driver, seeded_world, first.run_id))
    # The source's exact keys, speaker and dates travel into the proposal form.
    request_id, batch = proposal(driver, seeded_world, first)
    evidence = batch["evidence"][0]
    source = WorldQuery(driver, seeded_world).read(
        "MATCH (s:Source {id: $id}) RETURN s.content AS content", id=result.source_ids[0]
    )[0]
    message0 = json.loads(source["content"])[0]
    assert evidence["message_key"] == message0["key"]
    assert evidence["source_time"] == message0["at"]
    assert evidence["speaker_kind"] == message0["speaker_kind"] == "person"
    assert packet["batch_form"]["request_id"] == request_id
    finish_request(driver, seeded_world, caller_for("learner"), request_id, worker=first.run_id)
    end_pass(driver, seeded_world, first.run_id, outcome="ok")
    second = start_pass(driver, auth, seeded_world, agent="learner")
    assert second.input_ids == [request_id_for(result.source_ids[1])]


def test_interactive_requests_and_other_provider_are_not_starved(driver, seeded_world, test_clock):
    ingest(driver, seeded_world, "claude_ai", count=MAX_PART_MESSAGES + 1)
    ingest(driver, seeded_world, "chatgpt")
    interactive = {
        queue_store(
            driver, seeded_world, request_id=f"ordinary-{n}", at=STORE_TIME - timedelta(days=1)
        )
        for n in range(6)
    }
    auth, first = grant_for(driver, seeded_world)
    assert len(first.input_ids) == 5
    imports = [key for key in first.input_ids if key.startswith("import:")]
    assert len(imports) == 1 and len(interactive.intersection(first.input_ids)) == 4
    first_provider = next(
        r["caller"] for r in request_rows(driver, seeded_world) if r["id"] == imports[0]
    )
    for key in first.input_ids:
        finish_request(driver, seeded_world, caller_for("learner"), key, worker=first.run_id)
    end_pass(driver, seeded_world, first.run_id, outcome="ok")
    second = start_pass(driver, auth, seeded_world, agent="learner")
    second_import = next(key for key in second.input_ids if key.startswith("import:"))
    second_provider = next(
        r["caller"] for r in request_rows(driver, seeded_world) if r["id"] == second_import
    )
    assert second_provider != first_provider


def test_later_uploads_older_part_precedes_later_pending_parts(driver, seeded_world, test_clock):
    initial = ingest(driver, seeded_world, count=130)
    added = conversation(
        "orchard",
        [
            record(
                "older-added", "human", seconds=10.5, text="Mira Vale added an older orchard note."
            )
        ],
    )
    older = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(json.dumps([added]).encode()),
        screener=make_screener(),
    )
    query = WorldQuery(driver, seeded_world)
    expected = [initial.source_ids[0], older.source_ids[0], *initial.source_ids[1:]]
    for source_id in expected:
        pending = [row for row in request_rows(driver, seeded_world) if row["status"] != "saved"]
        current = request_id_for(source_id)
        for row in pending:
            assert import_queue.readiness_reason(query, row) == (
                None if row["id"] == current else "earlier_pending_time"
            )
        _, grant = grant_for(driver, seeded_world)
        assert grant is not None and grant.input_ids == [current]
        finish_request(driver, seeded_world, caller_for("learner"), current, worker=grant.run_id)
        end_pass(driver, seeded_world, grant.run_id, outcome="ok")
    assert grant_for(driver, seeded_world)[1] is None


@pytest.mark.parametrize("provider", ["claude_ai", "chatgpt"])
def test_equal_time_parts_keep_episode_order(driver, seeded_world, test_clock, provider):
    if provider == "claude_ai":
        doc = conversation(
            "equal-time",
            [
                record(f"m{n}", "human", seconds=0, text="Mira Vale tends pears.")
                for n in range(MAX_PART_MESSAGES + 1)
            ],
        )
    else:
        tree = Conversation("equal-time")
        for n in range(MAX_PART_MESSAGES + 1):
            tree.add(message(f"m{n}", "user", ["Mira Vale tends pears."], seconds=0))
        doc = tree.export()
    result = take_in(
        driver,
        seeded_world,
        provider,
        upload_of(json.dumps([doc]).encode()),
        screener=make_screener(),
    )
    assert result.requests_added == result.sources_added == 2
    query = WorldQuery(driver, seeded_world)
    ids = [request_id_for(key) for key in result.source_ids]
    rows = {row["id"]: row for row in request_rows(driver, seeded_world)}
    assert rows[ids[0]]["said_at"] == rows[ids[1]]["said_at"]
    assert import_queue.readiness_reason(query, rows[ids[0]]) is None
    assert import_queue.readiness_reason(query, rows[ids[1]]) == "earlier_pending_episode"
    for request_id in ids:
        _, grant = grant_for(driver, seeded_world)
        assert grant is not None and grant.input_ids == [request_id]
        finish_request(driver, seeded_world, caller_for("learner"), request_id, worker=grant.run_id)
        end_pass(driver, seeded_world, grant.run_id, outcome="ok")
    assert grant_for(driver, seeded_world)[1] is None


@pytest.mark.parametrize("provider", ["claude_ai", "chatgpt"])
def test_equal_time_candidate_pages_return_each_request_once(
    driver, seeded_world, test_clock, monkeypatch, provider
):
    monkeypatch.setattr(import_queue, "READ_PAGE", 2)
    docs = [json.loads(export(provider, chat=f"page-{n}"))[0] for n in range(5)]
    result = take_in(
        driver,
        seeded_world,
        provider,
        upload_of(json.dumps(docs).encode()),
        screener=make_screener(),
    )
    assert result.requests_added == 5
    expected = sorted(request_id_for(key) for key in result.source_ids)
    # One extra result detects a repeated page without an unbounded iterator.
    rows = list(
        islice(
            import_queue._import_candidates(WorldQuery(driver, seeded_world), STORE_TIME),
            len(expected) + 1,
        )
    )
    assert [row["r"]["id"] for row in rows] == expected


def test_progress_requires_a_real_noop_receipt_or_attributed_commit(
    driver, seeded_world, test_clock
):
    ingest(driver, seeded_world)
    auth, grant = grant_for(driver, seeded_world)
    key = grant.input_ids[0]
    finish_request(driver, seeded_world, caller_for("learner"), key, worker=grant.run_id)
    end_pass(driver, seeded_world, grant.run_id, outcome="ok")
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["state"] == "complete" and progress["held_nothing_count"] == 1
    assert progress["extracted_count"] == 0 and not any(progress["graph"].values())
    ingest(driver, seeded_world, chat="second")
    grant = start_pass(driver, auth, seeded_world, agent="learner")
    key, batch = proposal(driver, seeded_world, grant)
    saved = apply_request(
        driver, seeded_world, caller_for("learner"), key, batch, run_id=grant.run_id
    )
    assert saved.api_state == "saved"
    plant_node(driver, seeded_world, "Item", title="Unrelated invented item")
    progress = read_import_progress(driver, seeded_world, "claude_ai", limit=1)
    assert progress["extracted_count"] == progress["held_nothing_count"] == 1
    assert progress["graph"]["items_created"] == 1
    assert progress["sources_total"] == 2 and len(progress["sources"]) == 1
    assert progress["next"] == progress["sources"][0]["source_id"]
    again = apply_request(
        driver, seeded_world, caller_for("learner"), key, batch, run_id=grant.run_id
    )
    assert again.api_state == "saved"
    assert read_import_progress(driver, seeded_world, "claude_ai")["graph"] == progress["graph"]


def end_part(driver, world, request_id, source_id, ending):
    """Leave one part in the state that its real path leaves it in."""

    def work(tx):
        if ending in ("forgotten", "span_forgotten"):
            tx.run(
                "MATCH (s:Source {id: $id}) SET s.forget_state = $state, s.content = '[]'",
                id=source_id,
                state=ending,
            )
        elif ending == "retrying":
            tx.run(
                "MATCH (r:Request {id: $id}) SET r.status = 'failed', r.retry_at = $later",
                id=request_id,
                later=clock.now(world) + timedelta(hours=1),
            )
        else:
            tx.run(
                "MATCH (r:Request {id: $id}) SET r.status = $status, r.last_error_code = $code",
                id=request_id,
                status="declined" if ending == "declined" else "failed",
                code=ending,
            )

    WorldWriter(driver, world).transaction(work)


@pytest.mark.parametrize(
    "ending,holds",
    [
        ("attempts_exhausted", False),
        ("declined", False),
        ("forgotten", False),
        ("span_forgotten", False),
        ("retrying", True),
    ],
)
def test_only_a_part_that_can_still_run_holds_the_parts_after_it(
    driver, seeded_world, test_clock, ending, holds
):
    result = ingest(driver, seeded_world, count=2 * MAX_PART_MESSAGES + 1)
    ids = [request_id_for(key) for key in result.source_ids]
    assert len(ids) == 3
    end_part(driver, seeded_world, ids[0], result.source_ids[0], ending)
    query = WorldQuery(driver, seeded_world)
    rows = {row["id"]: row for row in request_rows(driver, seeded_world)}
    reason = import_queue.readiness_reason(query, rows[ids[1]])
    if holds:
        assert reason in ("earlier_pending_time", "earlier_pending_episode")
        return
    assert reason is None
    # The next part runs, and the part after it still waits for that one.
    assert import_queue.readiness_reason(query, rows[ids[2]]) in (
        "earlier_pending_time",
        "earlier_pending_episode",
    )
    _, grant = grant_for(driver, seeded_world)
    assert grant is not None and grant.input_ids == [ids[1]]


def test_a_held_message_does_not_stop_the_rest_of_its_conversation(
    driver, seeded_world, test_clock
):
    long_text = "Mira Vale tends the invented pear orchard. " * 900
    doc = conversation(
        "orchard",
        [
            record("m0", "human", seconds=0, text=long_text),
            record("m1", "human", seconds=1, text="Mira Vale picks the pears."),
        ],
    )
    result = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(json.dumps([doc]).encode()),
        screener=make_screener(),
    )
    assert result.requests_added == 2 and result.held_sources == 1
    later = request_id_for(result.source_ids[1])
    assert (
        import_queue.readiness_reason(
            WorldQuery(driver, seeded_world),
            next(row for row in request_rows(driver, seeded_world) if row["id"] == later),
        )
        is None
    )
    _, grant = grant_for(driver, seeded_world)
    assert grant is not None and grant.input_ids == [later]
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["state"] == "partial" and progress["held_count"] == 1


def test_lease_recovery_and_retry_do_not_duplicate_work(driver, seeded_world, test_clock):
    result = ingest(driver, seeded_world)
    auth, first = grant_for(driver, seeded_world)
    test_clock.advance(seconds=PassSettings().lease_seconds + 1)
    second = start_pass(driver, auth, seeded_world, agent="learner")
    assert second.input_ids == first.input_ids == [request_id_for(result.source_ids[0])]
    end_pass(driver, seeded_world, second.run_id, outcome="killed")
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["retry_wait_count"] == 1
    assert progress["sources"][0]["retry_at"] is not None
    assert request_state(driver, seeded_world, first.input_ids[0]).attempts == 2
    assert len(request_rows(driver, seeded_world)) == 1


def test_saved_label_without_receipt_is_not_success():
    from tests.runner.test_learner_input import CONNECTOR, READ, source

    stored = source()
    request = {"id": request_id_for(stored["id"]), "status": "saved"}
    assert source_state(stored, CONNECTOR, request, READ)[0] == "unavailable"


def test_missing_commit_effects_cannot_be_relabelled_as_a_noop():
    from tests.runner.test_learner_input import CONNECTOR, READ, source

    stored = source()
    request = {
        "id": request_id_for(stored["id"]),
        "status": "saved",
        "commit_id": "missing",
        "change_set_id": "missing",
    }
    request["receipt"] = json.dumps(
        {
            "request_id": request["id"],
            "state": "saved",
            "commit_id": "missing",
            "change_set_id": "missing",
            "lines": [],
        }
    )
    assert source_state(stored, CONNECTOR, request, READ)[0] == "unavailable"


def test_unknown_speaker_is_a_hold_not_a_success():
    from tests.runner.test_learner_input import CONNECTOR, READ, message, source

    stored = source([message(speaker_kind=None)])
    assert source_state(stored, CONNECTOR, None, READ) == ("held", "speaker_unknown", None)


def test_fully_kept_out_upload_has_no_request_or_graph_work(driver, seeded_world, test_clock):
    result = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(export(text="Orchard secret phrase")),
        screener=make_screener(denylist=["Orchard secret phrase"]),
    )
    assert result.messages_kept == result.requests_added == 0
    assert grant_for(driver, seeded_world)[1] is None
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["requests_total"] == 0 and not any(progress["graph"].values())
    assert progress["sources"][0]["state"] == "kept_out"


def test_concurrent_world_change_never_returns_mixed_progress(
    driver, seeded_world, test_clock, monkeypatch
):
    import grove.ingest.import_progress as progress

    ingest(driver, seeded_world)
    original = progress._generation
    calls = []

    def changed(*args):
        calls.append(True)
        return original(*args) + int(len(calls) > 1)

    monkeypatch.setattr(progress, "_generation", changed)
    with pytest.raises(progress.ProgressUnavailable, match="progress_changed"):
        progress.read_import_progress(driver, seeded_world, "claude_ai")


def test_progress_api_is_session_only_world_scoped_and_value_free(
    driver, seeded_world, make_world, client_for, anonymous, test_clock
):
    owner = client_for(seeded_world)
    uploaded = owner.post(
        "/v1/sources/upload?provider=claude_ai",
        content=export(),
        headers={"content-type": "application/json"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["processing"]["requests_added"] == 1
    path = "/v1/sources/import-progress?provider=claude_ai"
    response = owner.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["queued_count"] == 1
    assert "Mira" not in response.text and "orchard" not in response.text
    assert anonymous.get(path).status_code == 401
    assert owner.get(path, headers={"authorization": "Bearer gut_invented"}).status_code == 401
    other = client_for(make_world()).get(path)
    assert other.status_code == 200 and other.json()["sources_total"] == 0
    for suffix in ("&world_id=foreign", "&provider=chatgpt", "&limit=51", "&after=not-a-source"):
        assert owner.get(path + suffix).status_code == 400


def test_historical_retention_is_not_backfilled(driver, seeded_world, test_clock):
    connector = plant_node(
        driver, seeded_world, "Connector", type="past_ai_conversations", provider="claude_ai"
    )
    plant_node(
        driver,
        seeded_world,
        "Source",
        connector_id=connector["id"],
        content=json.dumps(
            [
                {
                    "key": "old",
                    "text": "Invented old orchard",
                    "at": "2026-10-01T09:00:00+00:00",
                    "speaker_kind": "person",
                    "modality": "text",
                }
            ]
        ),
        received_at=STORE_TIME,
        parser_version="claude_ai.v1",
    )
    progress = read_import_progress(driver, seeded_world, "claude_ai")
    assert progress["held_count"] == 1 and progress["requests_total"] == 0
    assert progress["sources"][0]["reason_code"] == "not_scheduled"
    assert grant_for(driver, seeded_world)[1] is None


@pytest.fixture
def muse_connection(driver, hosts_plant, client_for, test_clock):
    from grove.writer.oauth import grant_consent

    person = hosts_plant.person()
    client_id = hosts_plant.client(host="Untrusted <b>Muse</b> label")
    grant_id = grant_consent(
        driver,
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        scopes=["grove.read", "grove.write"],
        at=STORE_TIME,
    )
    return person, client_id, grant_id, client_for(person.world_id, user_id=person.user_id)


def test_muse_requires_explicit_connection_and_empty_connection_has_no_work(muse_connection):
    person, client_id, _, web = muse_connection
    path = "/v1/sources/import-progress"
    missing = web.get(path, params={"provider": "muse"})
    assert missing.status_code == 503 and "muse_selection_required" in missing.text
    result = web.get(path, params={"provider": "muse", "client_id": client_id})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["state"] == "idle"
    assert result.json()["sources_total"] == result.json()["requests_total"] == 0
    assert not any(result.json()["graph"].values())
    assert "Untrusted" not in result.text
    assert (
        web.get(path, params={"provider": "claude_ai", "client_id": client_id}).status_code == 400
    )
    assert web.get(path, params={"provider": "muse", "client_id": "Muse"}).status_code == 403


def test_muse_only_counts_selected_hosts_real_requests_and_commits(
    driver, muse_connection, hosts_plant
):
    from grove.mcp.store import StoreHandler
    from grove.mcp.tools import ToolContext
    from tests.hosts.test_store_component import complete

    person, client_id, grant_id, web = muse_connection
    context = ToolContext(
        driver,
        person.world_id,
        person.user_id,
        client_id,
        frozenset({"grove.read", "grove.write"}),
        grant_id,
    )
    result = StoreHandler(screener=make_screener(), wait_seconds=0)(
        context,
        {
            "text": "Mira Vale tends an invented orchard.",
            "verbatim": True,
            "host_message_id": "muse-message",
            "idempotency_key": "muse-save",
        },
    )
    before = web.get(
        "/v1/sources/import-progress", params={"provider": "muse", "client_id": client_id}
    ).json()
    assert before["requests_total"] == before["queued_count"] == 1
    assert before["sources"][0]["request_id"] == result["request_id"]
    complete(context, result["request_id"], title="Mira Vale")
    after = web.get(
        "/v1/sources/import-progress", params={"provider": "muse", "client_id": client_id}
    ).json()
    assert after["state"] == "complete" and after["extracted_count"] == 1
    assert after["graph"]["items_created"] == 1
    # An unrelated host with the same self-declared name never enters the scope.
    other_id = hosts_plant.client(host="Untrusted <b>Muse</b> label")
    from grove.writer.oauth import grant_consent

    grant_consent(
        driver,
        client_id=other_id,
        user_id=person.user_id,
        world_id=person.world_id,
        scopes=["grove.read"],
        at=STORE_TIME,
    )
    other = web.get(
        "/v1/sources/import-progress", params={"provider": "muse", "client_id": other_id}
    ).json()
    assert other["state"] == "idle" and other["requests_total"] == 0
    assert not any(other["graph"].values())


@pytest.mark.parametrize("reason", ["revoked", "scope", "user", "world"])
def test_muse_refuses_revoked_scope_user_and_world_mismatches(
    driver, muse_connection, hosts_plant, reason
):
    from grove.writer.oauth import grant_consent, revoke_grant

    person, client_id, grant_id, web = muse_connection
    revoke_grant(driver, grant_id=grant_id, world_id=person.world_id, at=STORE_TIME)
    if reason != "revoked":
        other = hosts_plant.person()
        grant_consent(
            driver,
            client_id=client_id,
            user_id=other.user_id if reason == "user" else person.user_id,
            world_id=other.world_id if reason == "world" else person.world_id,
            scopes=["grove.write"] if reason == "scope" else ["grove.read"],
            at=STORE_TIME,
        )
    response = web.get(
        "/v1/sources/import-progress", params={"provider": "muse", "client_id": client_id}
    )
    assert response.status_code == 403


def test_muse_revocation_during_progress_read_invalidates_the_answer(
    driver, muse_connection, monkeypatch
):
    import grove.ingest.import_progress as progress
    from grove.writer.oauth import revoke_grant

    person, client_id, grant_id, web = muse_connection
    original = progress._generation
    reads = []

    def generation(*args):
        reads.append(True)
        if len(reads) == 2:
            revoke_grant(driver, grant_id=grant_id, world_id=person.world_id, at=STORE_TIME)
        return original(*args)

    monkeypatch.setattr(progress, "_generation", generation)
    assert (
        web.get(
            "/v1/sources/import-progress", params={"provider": "muse", "client_id": client_id}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("client_id", ["", "x" * 2049, "🌳" * 513, "\ud800"])
def test_muse_bounds_client_id_before_any_grant_read_or_hash(monkeypatch, client_id):
    import grove.ingest.import_progress as progress

    monkeypatch.setattr(progress, "list_grants", lambda *args: pytest.fail("unbounded grant read"))
    with pytest.raises(progress.ProgressSelectionDenied):
        progress.read_import_progress(None, "unused", "muse", client_id=client_id, user_id="unused")
