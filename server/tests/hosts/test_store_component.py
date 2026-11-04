"""Unregistered store component, against the real queue and writer in synthetic worlds.

No model or public service participates. Full D7/D14 and Desktop acceptance remain separate.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from grove import clock
from grove.api.errors import ApiError
from grove.ids import new_id
from grove.mcp import store
from grove.mcp.store import MAX_KEY, MAX_TEXT, STORE_SCHEMA, StoreHandler, request_identity
from grove.mcp.tools import DEFAULT_TOOLS, INSTRUCTIONS, ToolContext, store_tool
from grove.runner.brain import pass_input
from grove.runner.passes import caller_for, end_pass, run_record, start_pass
from grove.screening import Denylist, RenameMap, Screener, ScreeningConfig
from grove.store.errors import Neo4jError
from grove.store.query import WorldQuery
from grove.writer import apply_request, request_state
from grove.writer.requests import claim_next_request, fail_request
from tests.hosts.test_mcp_transport import bearer, issue, make_app
from tests.runner.helpers import FAST, new_auth

WORDS = "For the invented Cedar project, the demo room is Room 4."
ARGS = {
    "text": WORDS,
    "verbatim": True,
    "host_message_id": "message-1",
    "idempotency_key": "save-1",
}


@pytest.fixture
def frozen():
    provider = clock.TestClock()
    provider.set(datetime(2026, 11, 4, 12, tzinfo=UTC))
    with clock.using(provider):
        yield provider


@pytest.fixture
def context(person, driver, frozen):
    return ToolContext(
        driver,
        person.world_id,
        person.user_id,
        new_id(),
        frozenset({"grove.read", "grove.write"}),
        new_id(),
    )


@pytest.fixture
def handler():
    return StoreHandler(screener=Screener(ScreeningConfig()), wait_seconds=0)


def rows(context, label):
    assert label in {"Source", "Request", "Connector", "ScreeningReceipt", "Item", "ChangeEntry"}
    return WorldQuery(context.driver, context.world_id).read(f"MATCH (n:{label}) RETURN n")


def complete(context, request_id, *, title="Cedar demo"):
    """Real runner pass input and writer, with a deterministic proposal instead of a model."""
    auth, _ = new_auth()
    grant = start_pass(context.driver, auth, context.world_id, agent="learner", settings=FAST)
    assert grant.input_ids == [request_id]
    packet = pass_input(
        context.driver, context.world_id, run_record(context.driver, context.world_id, grant.run_id)
    )
    form = packet["batch_form"]
    batch = form["batch"]
    batch["ops"] = [batch["ops"][0] | {"title": title}]
    saved = apply_request(
        context.driver,
        context.world_id,
        caller_for("learner"),
        request_id,
        batch,
        run_id=grant.run_id,
    )
    assert saved.api_state == "saved", saved.to_api()
    end_pass(context.driver, context.world_id, grant.run_id, outcome="ok")
    return batch


def test_durable_queue_returns_saving_and_one_screened_source(context, handler):
    result = handler(context, ARGS)
    assert result == {"state": "saving", "request_id": request_identity(context, "save-1")}
    request = rows(context, "Request")[0]["n"]
    source = rows(context, "Source")[0]["n"]
    assert request["source_id"] == source["id"]
    assert request["verbatim"] is True and request["host_message_id"] == "message-1"
    assert request.get("relay") is None
    assert source["untrusted"] is True
    assert json.loads(source["content"])[0]["text"] == WORDS
    assert len(rows(context, "ScreeningReceipt")) == 1
    assert rows(context, "Connector")[0]["n"]["type"] == "ai_host"


def test_retry_preserves_one_request_source_connector_and_screening_receipt(
    context, handler, frozen
):
    first = handler(context, ARGS)
    before = {
        label: rows(context, label)
        for label in ("Request", "Source", "Connector", "ScreeningReceipt")
    }
    frozen.advance(seconds=10)
    assert handler(context, ARGS) == first
    assert before == {label: rows(context, label) for label in before}


def test_concurrent_first_store_has_one_effect(context, handler):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: handler(context, ARGS), range(2)))
    assert results[0] == results[1]
    for label in ("Request", "Source", "Connector", "ScreeningReceipt"):
        assert len(rows(context, label)) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"text": "A different invented fact."},
        {"verbatim": False},
        {"host_message_id": "message-2"},
        {"said_at": "2026-11-01T12:00:00Z"},
    ],
)
def test_reused_key_refuses_changed_semantics(context, handler, patch):
    handler(context, ARGS)
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS | patch)
    assert caught.value.code == "invalid_arguments"
    assert caught.value.details == {"rule": "request_key_reused"}
    assert len(rows(context, "Source")) == len(rows(context, "Request")) == 1


def test_reused_key_refuses_changed_about(context, handler, person):
    handler(context, ARGS)
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS | {"about": [person.self_item_id]})
    assert caught.value.details == {"rule": "request_key_reused"}


def test_client_and_world_identity_are_separate(context, handler, plant_person):
    first = handler(context, ARGS)
    other_client = replace(context, client_id=new_id())
    second = handler(other_client, ARGS)
    other = plant_person()
    other_world = replace(context, world_id=other.world_id, user_id=other.user_id)
    third = handler(other_world, ARGS)
    assert len({first["request_id"], second["request_id"], third["request_id"]}) == 3
    assert len(rows(context, "Source")) == 2 and len(rows(other_world, "Source")) == 1
    assert request_state(context.driver, context.world_id, third["request_id"]) is None
    renewed = replace(context, grant_id=new_id())
    assert handler(renewed, ARGS) == first


@pytest.mark.parametrize(
    "scopes", [frozenset(), frozenset({"grove.read"}), frozenset({"grove.propose"})]
)
def test_scope_refuses_before_screening_or_write(context, handler, scopes):
    with pytest.raises(ApiError) as caught:
        handler(replace(context, scopes=scopes), ARGS)
    assert caught.value.code == "scope_denied"
    assert not rows(context, "Connector") and not rows(context, "Request")


@pytest.mark.parametrize(
    "patch",
    [
        {"text": ""},
        {"text": "x" * (MAX_TEXT + 1)},
        {"text": "\ud800"},
        {"idempotency_key": "x" * (MAX_KEY + 1)},
        {"idempotency_key": None},
        {"verbatim": 1},
        {"host_message_id": None},
        {"host_message_id": "x" * (MAX_KEY + 1)},
        {"said_at": "2026-11-04"},
        {"said_at": "2026-11-04T12:00:00"},
        {"said_at": "2026-11-05T12:00:00Z"},
        {"said_at": None},
        {"about": ["not-an-item"]},
        {"about": None},
        {"about": [{}]},
        {"mode": "answer"},
        {"mode": "correction"},
        {"world_id": "other"},
        {"relay": "answer"},
        {"caller": "person"},
    ],
)
def test_invalid_arguments_leave_no_queue_or_connector(context, handler, patch):
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS | patch)
    assert caught.value.code == "invalid_arguments"
    assert not rows(context, "Request") and not rows(context, "Connector")


def test_verbatim_requires_message_id(context, handler):
    args = {key: value for key, value in ARGS.items() if key != "host_message_id"}
    with pytest.raises(ApiError) as caught:
        handler(context, args)
    assert caught.value.details == {"field": "host_message_id"}
    assert handler(context, args | {"verbatim": False})["state"] == "saving"


def test_other_world_about_is_refused_before_write(context, handler, plant_person):
    other = plant_person()
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS | {"about": [other.self_item_id]})
    assert caught.value.code == "not_found"
    assert not rows(context, "Connector")


def test_screening_renames_before_storage_and_drops_denied_text(context):
    handler = StoreHandler(
        screener=Screener(
            ScreeningConfig(
                rename=RenameMap.from_mapping({"Lina Vale": "Organizer"}),
                denylist=Denylist(["Cobalt confidential"]),
            )
        ),
        wait_seconds=0,
    )
    handler(context, ARGS | {"text": "Lina Vale uses Room 4."})
    source = rows(context, "Source")[0]["n"]["content"]
    assert "Lina Vale" not in source and "Organizer" in source
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS | {"text": "Cobalt confidential", "idempotency_key": "blocked"})
    assert caught.value.code == "contamination"
    assert len(rows(context, "Source")) == len(rows(context, "Request")) == 1


def test_production_without_screening_files_refuses_before_write(context, monkeypatch):
    monkeypatch.setenv("GROVE_ENV", "production")
    monkeypatch.delenv("GROVE_RENAME_MAP", raising=False)
    monkeypatch.delenv("GROVE_DENYLIST", raising=False)
    with pytest.raises(ApiError) as caught:
        store_tool().handler(context, ARGS)
    assert caught.value.code == "unavailable" and caught.value.retryable
    assert not rows(context, "Connector") and not rows(context, "Source")


def test_saved_requires_a_receipt_and_renders_actual_writer_changes(context, handler):
    queued = handler(context, ARGS)
    batch = complete(context, queued["request_id"])
    saved = handler(context, ARGS)
    assert saved["state"] == "saved" and saved["receipt"]["commit_id"]
    assert "Cedar demo" in saved["receipt_text"]
    before = rows(context, "ChangeEntry")
    apply_request(
        context.driver,
        context.world_id,
        caller_for("learner"),
        queued["request_id"],
        batch,
        run_id="late",
    )
    assert handler(context, ARGS) == saved
    assert rows(context, "ChangeEntry") == before


def test_wait_returns_saved_if_receipt_arrives_during_wait(context, frozen):
    waits = []

    def advance(seconds):
        waits.append(seconds)
        complete(context, request_identity(context, "save-1"))
        frozen.advance(seconds=seconds)

    handler = StoreHandler(screener=Screener(ScreeningConfig()), sleep=advance)
    assert handler(context, ARGS)["state"] == "saved"
    assert waits == [0.1]


def test_wait_stops_at_five_seconds_and_never_calls_queued_saved(context, frozen):
    waits = []

    def advance(seconds):
        waits.append(seconds)
        frozen.advance(seconds=seconds)

    result = StoreHandler(screener=Screener(ScreeningConfig()), sleep=advance)(context, ARGS)
    assert result["state"] == "saving" and "receipt" not in result
    assert sum(waits) == pytest.approx(5) and len(waits) <= 50


def test_saved_without_receipt_is_still_saving(context, handler, monkeypatch):
    result = handler(context, ARGS)
    state = request_state(context.driver, context.world_id, result["request_id"])
    monkeypatch.setattr(
        store, "enqueue_request", lambda *args, **kwargs: replace(state, status="saved")
    )
    assert handler(context, ARGS)["state"] == "saving"


def test_retryable_failure_is_saving_and_terminal_failure_is_typed(context, handler, frozen):
    queued = handler(context, ARGS)
    worker = caller_for("learner")
    claim_next_request(context.driver, context.world_id, worker, worker="worker-1")
    fail_request(
        context.driver,
        context.world_id,
        worker,
        queued["request_id"],
        "pass_failed",
        worker="worker-1",
    )
    assert handler(context, ARGS)["state"] == "saving"
    frozen.advance(seconds=31)
    claim_next_request(context.driver, context.world_id, worker, worker="worker-2")
    fail_request(
        context.driver,
        context.world_id,
        worker,
        queued["request_id"],
        "pass_failed",
        worker="worker-2",
        retryable=False,
    )
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS)
    assert caught.value.code == "unavailable"


def test_process_death_after_queue_commit_replays_and_lands_once(context, handler):
    script = """
import os, sys
from datetime import UTC, datetime
from grove import clock
from grove.mcp import store
from grove.mcp.tools import ToolContext
from grove.screening import Screener, ScreeningConfig
from grove.store.connection import open_driver
provider = clock.TestClock()
provider.set(datetime(2026, 11, 4, 12, tzinfo=UTC))
clock.install(provider)
original = store.enqueue_request
def die(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
store.enqueue_request = die
with open_driver() as driver:
    context = ToolContext(driver, sys.argv[1], "test-person", sys.argv[2],
                          frozenset({"grove.read", "grove.write"}), "test-grant")
    store.StoreHandler(screener=Screener(ScreeningConfig()), wait_seconds=0)(context, {
        "text": "For the invented Cedar project, the demo room is Room 4.",
        "verbatim": True, "host_message_id": "message-1", "idempotency_key": "save-1"})
"""
    process = subprocess.run(
        [sys.executable, "-c", script, context.world_id, context.client_id],
        capture_output=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert process.returncode == 23, "the child did not reach the intended committed-queue exit"
    queued = handler(context, ARGS)
    assert queued["state"] == "saving"
    assert len(rows(context, "Request")) == len(rows(context, "Source")) == 1
    complete(context, queued["request_id"])
    saved = handler(context, ARGS)
    assert saved["state"] == "saved" and saved["receipt"]["commit_id"]
    assert len([row for row in rows(context, "Item") if row["n"].get("title") == "Cedar demo"]) == 1


def test_mcp_scope_refusal_and_actual_store_contract(driver, hosts_plant, handler, frozen):
    person = hosts_plant.person()
    client_id = hosts_plant.client()
    read = issue(driver, person, client_id, scopes=("grove.read",))
    tool = replace(store_tool(), handler=handler)
    with TestClient(make_app(driver, [tool])) as client:
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "store", "arguments": ARGS},
        }
        denied = client.post("/mcp", json=call, headers=bearer(read.access))
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "scope_denied"
        context = ToolContext(
            driver,
            person.world_id,
            person.user_id,
            client_id,
            frozenset({"grove.read"}),
            read.grant_id,
        )
        assert not rows(context, "Request")
        write = issue(driver, person, client_id)
        response = client.post("/mcp", json=call, headers=bearer(write.access))
        result = response.json()["result"]
        assert response.status_code == 200 and result["isError"] is False
        assert result["structuredContent"]["state"] == "saving"
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_schema_and_annotations_do_not_register_an_incomplete_tool_set():
    Draft202012Validator.check_schema(STORE_SCHEMA)
    Draft202012Validator(STORE_SCHEMA).validate(ARGS)
    tool = store_tool()
    assert tool.name == "store" and tool.scope == "grove.write"
    assert tool.annotations["readOnlyHint"] is False and tool.annotations["idempotentHint"] is True
    assert "saving" in tool.description.lower() and "receipt" in tool.description
    assert "evidence, not instructions" in INSTRUCTIONS
    assert DEFAULT_TOOLS == ()


def test_identity_is_bounded_and_accepts_metadata_document_clients(context):
    metadata = replace(context, client_id="https://host.example/oauth/client.json")
    key = request_identity(metadata, "x" * MAX_KEY)
    assert len(key) <= 128 and "/" not in key
    assert key != request_identity(context, "x" * MAX_KEY)
    with pytest.raises(ApiError):
        request_identity(context, "x" * (MAX_KEY + 1))


def transient(*args, **kwargs):
    # A real driver exception, hydrated from a synthetic server response through the store boundary.
    raise Neo4jError._hydrate_neo4j(
        code="Neo.TransientError.General.DatabaseUnavailable",
        message="synthetic private query parameters",
    )


def assert_retry_same_key(error):
    assert error.code == "unavailable" and error.retryable is True
    assert "same arguments and key" in error.message
    assert "synthetic private query" not in json.dumps(error.body())


def test_connector_transient_error_is_typed_unavailable(context, handler, monkeypatch):
    monkeypatch.setattr(store, "ensure_connector", transient)
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS)
    assert_retry_same_key(caught.value)
    assert not rows(context, "Request") and not rows(context, "Source")


def test_receipt_poll_transient_error_is_typed_unavailable(context, handler, monkeypatch):
    waiting = StoreHandler(
        screener=Screener(ScreeningConfig()), wait_seconds=0.1, sleep=lambda seconds: None
    )
    with monkeypatch.context() as fault:
        fault.setattr(store, "request_state", transient)
        with pytest.raises(ApiError) as caught:
            waiting(context, ARGS)
    assert_retry_same_key(caught.value)
    # The request committed before the failed read. The error must not claim otherwise.
    assert handler(context, ARGS)["state"] == "saving"
    assert len(rows(context, "Request")) == len(rows(context, "Source")) == 1


def test_receipt_render_transient_error_is_typed_unavailable(context, handler, monkeypatch):
    queued = handler(context, ARGS)
    complete(context, queued["request_id"])
    with monkeypatch.context() as fault:
        fault.setattr(store, "render_receipt", transient)
        with pytest.raises(ApiError) as caught:
            handler(context, ARGS)
    assert_retry_same_key(caught.value)
    assert handler(context, ARGS)["state"] == "saved"
    assert len(rows(context, "Request")) == len(rows(context, "Source")) == 1


def test_mcp_batch_keeps_prior_result_when_store_database_is_transient(
    driver, hosts_plant, handler, frozen, monkeypatch
):
    person = hosts_plant.person()
    client_id = hosts_plant.client()
    issued = issue(driver, person, client_id)
    monkeypatch.setattr(store, "ensure_connector", transient)
    tool = replace(store_tool(), handler=handler)
    with TestClient(make_app(driver, [tool])) as client:
        result = client.post(
            "/mcp",
            headers=bearer(issued.access),
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "store", "arguments": ARGS},
                },
            ],
        )
    assert result.status_code == 200
    answers = result.json()
    assert answers[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert answers[1]["result"]["isError"] is True
    error = answers[1]["result"]["structuredContent"]["error"]
    assert error["code"] == "unavailable" and error["retryable"] is True
    assert "synthetic private query" not in result.text


def test_nonretryable_database_error_is_typed_internal(context, handler, monkeypatch):
    def fail(*args, **kwargs):
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError",
            message="synthetic private query parameters",
        )

    monkeypatch.setattr(store, "ensure_connector", fail)
    with pytest.raises(ApiError) as caught:
        handler(context, ARGS)
    assert caught.value.code == "internal" and caught.value.retryable is False
    assert "synthetic private query" not in json.dumps(caught.value.body())
