"""Reminder policy, real WorldWriter control flow, and isolated HTTP route regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from grove.api import uploads
from grove.api.errors import ApiError
from grove.demo.screening import lumen_fixture
from grove.ids import new_id
from grove.store.catalogue import TYPE_CLASSES, WORLDS
from grove.views.onboarding import (
    MAX_REVISION,
    InvalidPendingExports,
    PendingExportDenied,
    PendingExportSessionEnded,
    capture_pending_export,
    decode_state,
    pending_exports,
)
from grove.views.session import SESSION_COOKIE, ViewSession, token_hash
from grove.writer.onboarding import complete_export, request_export
from grove.writer.tx import WorldWriter
from tests.onboarding.memory_store import WORLD_A, WORLD_B, MemoryDriver

SESSION_A = ViewSession("owner-a", WORLD_A, None)
SESSION_B = ViewSession("owner-b", WORLD_B, None)
PREFIX = "/v1/sources/pending-exports"
GEN = "01234567-89ab-7cde-8f01-23456789abcd"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(
        WorldWriter,
        "transaction",
        lambda self, work: self._driver.transaction(work, self._world_id),
    )
    return MemoryDriver()


def request(store, provider="chatgpt", session=SESSION_A):
    return request_export(
        store,
        session,
        token_hash("synthetic-a" if session == SESSION_A else "synthetic-b"),
        provider,
    )


def client(store, cookie="synthetic-a"):
    app = FastAPI()
    app.state.driver = store
    app.state.settings = SimpleNamespace(public_base_url="http://testserver")
    app.include_router(uploads.router)

    @app.exception_handler(ApiError)
    async def error_handler(_, error):
        return JSONResponse(error.body(), status_code=error.status)

    return TestClient(app, cookies={SESSION_COOKIE: cookie})


def test_schema_is_bounded_non_text_metadata():
    columns = {prop: column for column in WORLDS.columns for prop in column.props}
    assert columns["pending_export_requests"].type == "json_ids"
    assert TYPE_CLASSES["json_ids"] == "a JSON string that holds only ids, codes and numbers"
    assert columns["pending_export_revision"].type == "int"
    assert WORLDS.scope == "platform"
    assert (
        decode_state(
            json.dumps({p: new_id() for p in ("chatgpt", "claude_ai", "whatsapp", "muse", "grok")}),
            5,
        )[1]
        == 5
    )


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '"x"',
        "null",
        '{"unknown":"' + GEN + '"}',
        '{"chatgpt":"filename.json"}',
        '{"chatgpt":{}}',
        '{"chatgpt":null}',
        '{"chatgpt":NaN}',
        '{"chatgpt":"' + GEN + '","chatgpt":"' + GEN + '"}',
        " " * 1025,
        42,
        "\ud800",
    ],
)
def test_malformed_or_oversized_state_refuses(raw):
    with pytest.raises(InvalidPendingExports):
        decode_state(raw, 0)


@pytest.mark.parametrize("revision", [True, False, -1, 1.0, "1", MAX_REVISION + 1])
def test_invalid_revision_refuses(revision):
    with pytest.raises(InvalidPendingExports):
        decode_state(None, revision)


def test_get_is_read_only_and_request_follows_account_between_clients(store):
    before = deepcopy(store.worlds)
    assert client(store).get(PREFIX).json() == dict(
        account="owner-a", revision=0, pending_exports={}
    )
    assert store.worlds == before
    assert not any(write for write, _ in store.queries)
    first = client(store).post(PREFIX + "/claude_ai", json={})
    assert first.status_code == 200
    assert client(store).get(PREFIX).json() == first.json()
    assert client(store, "synthetic-b").get(PREFIX).json()["pending_exports"] == {}
    assert store.worlds[WORLD_A]["read_generation"] == 1


def test_two_provider_requests_do_not_lose_writes(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda provider: request(store, provider), ["chatgpt", "claude_ai"]))
    state = pending_exports(store, SESSION_A)
    assert set(state["pending_exports"]) == {"chatgpt", "claude_ai"}
    assert state["revision"] == 2
    assert store.worlds[WORLD_A]["read_generation"] == 2


def test_old_upload_cannot_clear_new_request_and_noop_preserves_all_fields(store):
    first = request(store)
    old = capture_pending_export(store, WORLD_A, "chatgpt")
    second = request(store)
    assert first["pending_exports"] != second["pending_exports"]
    before = deepcopy(store.worlds)
    assert complete_export(store, old) == second
    assert store.worlds == before
    current = capture_pending_export(store, WORLD_A, "chatgpt")
    assert complete_export(store, current)["pending_exports"] == {}
    assert store.worlds[WORLD_A]["read_generation"] == 3


def test_absent_generation_never_clears_a_later_request(store):
    captured = capture_pending_export(store, WORLD_A, "chatgpt")
    latest = request(store)
    assert complete_export(store, captured) == latest
    assert capture_pending_export(store, WORLD_A, "codex") is None


@pytest.mark.parametrize("mutation", ["revoked", "expired", "owner"])
def test_writer_rechecks_actual_session_after_dependency_resolution(store, mutation):
    if mutation == "owner":
        store.worlds[WORLD_A]["account"] = "owner-b"
    elif mutation == "revoked":
        store.sessions[token_hash("synthetic-a")]["revoked"] = True
    else:
        store.sessions[token_hash("synthetic-a")]["expires"] = datetime(2000, 1, 1, tzinfo=UTC)
    before = deepcopy(store.worlds)
    with pytest.raises(PendingExportSessionEnded):
        request(store)
    assert store.worlds == before


@pytest.mark.parametrize("field", ["demo_version", "demo_world_id", "designated_user_id"])
def test_partial_cedar_designation_refuses_before_any_mutation(store, field):
    request(store)
    captured = capture_pending_export(store, WORLD_A, "chatgpt")
    store.worlds[WORLD_A][field] = "partial"
    before = deepcopy(store.worlds)
    with pytest.raises(PendingExportDenied):
        request(store)
    with pytest.raises(PendingExportDenied):
        complete_export(store, captured)
    assert store.worlds == before


def test_revision_exhaustion_rolls_back_and_corrupt_state_is_not_repaired(store):
    store.worlds[WORLD_A]["revision"] = MAX_REVISION
    before = deepcopy(store.worlds)
    with pytest.raises(InvalidPendingExports):
        request(store)
    assert store.worlds == before
    store.worlds[WORLD_A]["requests"] = '{"chatgpt":"private text"}'
    response = client(store).get(PREFIX)
    assert response.status_code == 503
    assert "private text" not in response.text


@pytest.mark.parametrize(
    ("path", "body", "headers", "status"),
    [
        ("chatgpt", {}, {"Authorization": "Bearer synthetic"}, 401),
        ("chatgpt", {}, {"Origin": "https://elsewhere.test"}, 403),
        ("chatgpt", {}, {"Sec-Fetch-Site": "cross-site"}, 403),
        ("chatgpt", {"generation": GEN}, {}, 400),
        ("chatgpt", {"world_id": WORLD_B}, {}, 400),
        ("chatgpt", [], {}, 400),
        ("claude", {}, {}, 400),
        ("codex", {}, {}, 400),
        ("chatgpt?account=owner-b", {}, {}, 400),
    ],
)
def test_post_refuses_unsupported_authority_and_input(store, path, body, headers, status):
    before = deepcopy(store.worlds)
    response = client(store).post(PREFIX + "/" + path, json=body, headers=headers)
    assert response.status_code == status
    assert store.worlds == before


def test_session_and_size_guards(store):
    assert client(store, "unknown").get(PREFIX).status_code == 401
    assert (
        client(store).get(PREFIX, headers={"Authorization": "Bearer synthetic"}).status_code == 401
    )
    assert (
        client(store)
        .post(PREFIX + "/chatgpt", content="{}", headers={"Content-Type": "text/plain"})
        .status_code
        == 400
    )
    assert (
        client(store)
        .post(PREFIX + "/chatgpt", content=" " * 1025, headers={"Content-Type": "application/json"})
        .status_code
        == 400
    )


def setup_import(monkeypatch, *, state="taken_in", failed=0, callback=None, lumen=False):
    if lumen:

        def missing():
            raise uploads._not_configured("screening is not configured")

        monkeypatch.setattr(uploads, "_screener", missing)
    else:
        monkeypatch.setattr(uploads, "_screener", lambda: object())
    calls = []

    def take_in(*args, **kwargs):
        calls.append(args[2])
        if callback:
            callback()
        return SimpleNamespace(
            to_api=lambda: dict(state=state, failed_chats=failed, sources_added=1)
        )

    monkeypatch.setattr(uploads, "take_in", take_in)
    return calls


@pytest.mark.parametrize("lumen", [False, True])
@pytest.mark.parametrize("state", ["taken_in", "nothing_new"])
def test_both_upload_paths_clear_only_after_success(store, monkeypatch, lumen, state):
    request(store, "claude_ai")
    calls = setup_import(monkeypatch, lumen=lumen, state=state)
    response = client(store).post(
        "/v1/sources/upload?provider=claude_ai",
        content=lumen_fixture() if lumen else b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert calls == ["claude_ai"]
    assert response.json()["state"] == state
    assert response.json()["reminder"]["snapshot"]["pending_exports"] == {}
    assert pending_exports(store, SESSION_A)["pending_exports"] == {}


@pytest.mark.parametrize(
    ("state", "failed"),
    [
        ("taken_in", 1),
        ("kept_out", 0),
        ("failed", 0),
        ("unknown", 0),
        ("taken_in", False),
        ("taken_in", None),
    ],
)
def test_partial_refused_or_malformed_results_keep_reminder(store, monkeypatch, state, failed):
    expected = request(store)
    setup_import(monkeypatch, state=state, failed=failed)
    response = client(store).post(
        "/v1/sources/upload?provider=chatgpt",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert pending_exports(store, SESSION_A) == expected


@pytest.mark.parametrize("lumen", [False, True])
def test_generation_is_captured_before_body_and_new_request_survives(store, monkeypatch, lumen):
    request(store, "claude_ai")
    original = uploads._spool_body

    async def body_after_new_request(*args):
        request(store, "claude_ai")
        return await original(*args)

    monkeypatch.setattr(uploads, "_spool_body", body_after_new_request)
    setup_import(monkeypatch, lumen=lumen)
    response = client(store).post(
        "/v1/sources/upload?provider=claude_ai",
        content=lumen_fixture() if lumen else b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["reminder"]["snapshot"]["revision"] == 2
    assert "claude_ai" in pending_exports(store, SESSION_A)["pending_exports"]


@pytest.mark.parametrize("lumen", [False, True])
def test_completion_failure_does_not_disguise_committed_import(store, monkeypatch, lumen):
    expected = request(store, "claude_ai")
    calls = setup_import(monkeypatch, lumen=lumen)

    def unavailable(*_):
        raise RuntimeError("synthetic completion outage")

    monkeypatch.setattr(uploads, "complete_export", unavailable)
    response = client(store).post(
        "/v1/sources/upload?provider=claude_ai",
        content=lumen_fixture() if lumen else b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert calls == ["claude_ai"]
    assert response.json() == dict(
        state="taken_in", failed_chats=0, sources_added=1, reminder={"status": "unavailable"}
    )
    assert pending_exports(store, SESSION_A) == expected


def test_upload_token_keeps_its_world_and_does_not_disclose_other_reminders(store, monkeypatch):
    expected_a = request(store)
    request(store, session=SESSION_B)
    monkeypatch.setattr(uploads, "find_upload_grant", lambda *_: SimpleNamespace(world_id=WORLD_B))
    setup_import(monkeypatch)
    response = client(store).post(
        "/v1/sources/upload?provider=chatgpt",
        content=b"{}",
        headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-upload"},
    )
    assert response.status_code == 200
    assert response.json()["reminder"] == {"status": "updated"}
    assert pending_exports(store, SESSION_A) == expected_a
    assert pending_exports(store, SESSION_B)["pending_exports"] == {}


def test_cedar_and_lumen_byte_guards_remain_before_import(store, monkeypatch):
    calls = setup_import(monkeypatch, lumen=True)
    response = client(store).post(
        "/v1/sources/upload?provider=claude_ai",
        content=lumen_fixture() + b" ",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 503
    store.worlds[WORLD_A]["demo_version"] = "cedar-v1"
    response = client(store).post(
        "/v1/sources/upload?provider=claude_ai",
        content=lumen_fixture(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert calls == []
