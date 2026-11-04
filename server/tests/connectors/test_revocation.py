# ruff: noqa: F811
"""Independent synthetic expiry/disconnect control. No production mutation."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from grove import clock
from grove.connectors.service import sync_calendar
from grove.writer.connectors import ConnectionBusy, connection
from tests.connectors.test_calendar import event
from tests.connectors.test_connections import connect, deps  # noqa: F401
from tests.connectors.test_git import repository  # noqa: F401
from tests.ingest.conftest import counts, make_screener


def options(deps, fake_google):
    return dict(
        http=deps.http,
        client=deps.client,
        vault=deps.vault,
        screener=make_screener(),
        base_url=fake_google.base,
    )


def no_sources(driver, world):
    stored = counts(driver, world)
    assert stored["sources"] == stored["episodes"] == stored["receipts"] == 0


def test_expired_calendar_sync_cannot_read_or_store_after_disconnect(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
):
    client = client_for(seeded_world)
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    observed = {"disconnect_status": None, "provider_pages_after_disconnect": 0}

    def answer(_path, query):
        if "pageToken" not in query:
            test_clock.advance(seconds=601)
            response = client.post(f"/v1/connectors/{chosen}/disconnect", json={})
            observed["disconnect_status"] = response.status_code
            return 200, {
                "items": [event("before-disconnect")],
                "nextPageToken": "second-page",
                "timeZone": "UTC",
            }
        assert connection(driver, seeded_world, chosen)["connection_status"] == "disconnected"
        observed["provider_pages_after_disconnect"] += 1
        return 200, {
            "items": [event("after-disconnect")],
            "nextSyncToken": "end",
            "timeZone": "UTC",
        }

    fake_google.answer = answer
    with pytest.raises(ConnectionBusy):
        sync_calendar(
            driver,
            seeded_world,
            chosen,
            http=deps.http,
            client=deps.client,
            vault=deps.vault,
            screener=make_screener(),
            base_url=fake_google.base,
        )
    stored = counts(driver, seeded_world)
    after = connection(driver, seeded_world, chosen)
    assert after["connection_status"] == "disconnected"
    assert not after.get("credential_ciphertext") and not after.get("sync_cursor")
    observed["sources_after_disconnect"] = stored["sources"]
    observed["episodes_after_disconnect"] = stored["episodes"]
    assert observed == {
        "disconnect_status": 200,
        "provider_pages_after_disconnect": 0,
        "sources_after_disconnect": 0,
        "episodes_after_disconnect": 0,
    }


def test_expiry_without_disconnect_discards_response_and_releases_owner(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
):
    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)

    def answer(*_):
        test_clock.advance(seconds=601)
        return 200, {"items": [event()], "nextPageToken": "more", "timeZone": "UTC"}

    fake_google.answer = answer
    before = len(fake_google.requests)
    with pytest.raises(ConnectionBusy):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    assert len(fake_google.requests) == before + 1
    no_sources(driver, seeded_world)
    row = connection(driver, seeded_world, chosen)
    assert row["connection_status"] == "connected" and row["last_outcome"] == "failed"
    assert not row.get("sync_owner") and not row.get("sync_until")
    assert not row.get("sync_cursor") and row["failed_count"] == 1
    fake_google.answer = lambda *_: (
        200,
        {"items": [event()], "nextSyncToken": "recovered", "timeZone": "UTC"},
    )
    assert (
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))["episodes_added"]
        == 1
    )


@pytest.mark.parametrize("admitted", [False, True])
def test_concurrent_disconnect_orders_request_admission_and_discards_old_response(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
    monkeypatch,
    admitted,
):
    import grove.connectors.service as service

    client = client_for(seeded_world)
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    reached, release = threading.Event(), threading.Event()
    original = service.check_sync
    calls = 0

    def barrier(*args):
        nonlocal calls
        calls += 1
        # Refresh is unnecessary: its two authority checks precede the first page admission.
        if calls == 3:
            if admitted:
                original(*args)
            reached.set()
            assert release.wait(10), "request admission barrier was not released"
            if not admitted:
                original(*args)
        else:
            original(*args)

    monkeypatch.setattr(service, "check_sync", barrier)
    fake_google.answer = lambda *_: (
        200,
        {"items": [event()], "nextPageToken": "later", "timeZone": "UTC"},
    )
    before = len(fake_google.requests)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            sync_calendar, driver, seeded_world, chosen, **options(deps, fake_google)
        )
        try:
            assert reached.wait(10)
            test_clock.advance(seconds=601)
            assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
            disconnected = connection(driver, seeded_world, chosen)
        finally:
            release.set()
        with pytest.raises(ConnectionBusy):
            future.result(timeout=10)
    # An admission committed before revocation may send later. It cannot authorize another
    # page or accept this response. No test assumes cancellation of a dispatched request.
    assert len(fake_google.requests) == before + int(admitted)
    no_sources(driver, seeded_world)
    assert connection(driver, seeded_world, chosen) == disconnected


@pytest.mark.parametrize("point", ["before_first", "between_writes", "after_last"])
def test_disconnect_fences_each_atomic_source_write_and_completion(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
    monkeypatch,
    point,
):
    import grove.ingest.take_in as pipeline

    client = client_for(seeded_world)
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (
        200,
        {"items": [event("one"), event("two")], "nextSyncToken": "end", "timeZone": "UTC"},
    )
    original = pipeline.write_chat_delta
    committed = 0
    disconnected = None

    def revoke():
        nonlocal disconnected
        test_clock.advance(seconds=601)
        assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
        disconnected = connection(driver, seeded_world, chosen)

    def write(*args, **kwargs):
        nonlocal committed
        if point == "before_first" or (point == "between_writes" and committed == 1):
            revoke()
        result = original(*args, **kwargs)
        committed += 1
        if point == "after_last" and committed == 2:
            revoke()
        return result

    monkeypatch.setattr(pipeline, "write_chat_delta", write)
    with pytest.raises(ConnectionBusy):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    expected = {"before_first": 0, "between_writes": 1, "after_last": 2}[point]
    stored = counts(driver, seeded_world)
    assert stored["sources"] == stored["episodes"] == stored["receipts"] == expected
    assert connection(driver, seeded_world, chosen) == disconnected


def test_source_transaction_commits_before_waiting_disconnect_returns(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
    monkeypatch,
):
    import grove.writer.connectors as connections
    import grove.writer.ingest as writer

    client = client_for(seeded_world)
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (
        200,
        {"items": [event()], "nextSyncToken": "end", "timeZone": "UTC"},
    )
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    write_original, lock_original = writer.write_screened_source, connections._locked

    def held_write(*args, **kwargs):
        entered.set()
        assert release.wait(10), "source transaction barrier was not released"
        return write_original(*args, **kwargs)

    def mark_lock(*args):
        if threading.current_thread().name == "disconnect-probe":
            attempting.set()
        return lock_original(*args)

    monkeypatch.setattr(writer, "write_screened_source", held_write)
    monkeypatch.setattr(connections, "_locked", mark_lock)
    result = []

    def disconnect():
        connections.disconnect(driver, seeded_world, chosen)
        result.append(counts(driver, seeded_world))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            sync_calendar, driver, seeded_world, chosen, **options(deps, fake_google)
        )
        thread = threading.Thread(target=disconnect, name="disconnect-probe")
        try:
            assert entered.wait(10)
            test_clock.advance(seconds=601)
            thread.start()
            assert attempting.wait(10)
            assert not result
        finally:
            release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        with pytest.raises(ConnectionBusy):
            future.result(timeout=10)
    assert result[0]["sources"] == result[0]["episodes"] == 1
    assert connection(driver, seeded_world, chosen)["connection_status"] == "disconnected"


def test_owner_replacement_cannot_be_overwritten_by_old_failure_cleanup(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
):
    from grove.writer.connectors import finish_sync, start_sync

    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    successor = []

    def answer(*_):
        test_clock.advance(seconds=601)
        _, owner = start_sync(driver, seeded_world, chosen)
        successor.append((owner, connection(driver, seeded_world, chosen)))
        return 200, {"items": [event()], "nextSyncToken": "stale", "timeZone": "UTC"}

    fake_google.answer = answer
    with pytest.raises(ConnectionBusy):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    assert connection(driver, seeded_world, chosen) == successor[0][1]
    no_sources(driver, seeded_world)
    finish_sync(driver, seeded_world, chosen, successor[0][0], outcome="failed")


def test_refresh_response_after_disconnect_cannot_restore_credentials(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
    monkeypatch,
):
    from grove.connectors import google

    client = client_for(seeded_world, expires_at=clock.now() + timedelta(days=1))
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    test_clock.advance(seconds=3571)
    fake_google.token_answer = {
        "access_token": "synthetic-replacement",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    original = google._post
    disconnected = []

    def revoke_after_response(*args, **kwargs):
        body = original(*args, **kwargs)
        test_clock.advance(seconds=601)
        assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
        disconnected.append(connection(driver, seeded_world, chosen))
        return body

    monkeypatch.setattr(google, "_post", revoke_after_response)
    before = len(fake_google.requests)
    with pytest.raises(ConnectionBusy):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    assert len(fake_google.requests) == before + 1
    assert connection(driver, seeded_world, chosen) == disconnected[0]
    no_sources(driver, seeded_world)


def test_failed_response_releases_lease_and_preserves_cursor(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
):
    from grove.connectors.calendar import CalendarRefused

    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (200, {"items": [], "nextSyncToken": "before"})
    sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    fake_google.answer = lambda *_: (500, {})
    with pytest.raises(CalendarRefused):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    row = connection(driver, seeded_world, chosen)
    assert row["sync_cursor"] == "before" and row["last_outcome"] == "failed"
    assert not row.get("sync_owner") and not row.get("sync_until")


def test_authority_is_world_scoped_and_missing_owner_cannot_bypass_source_fence(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    make_world,
):
    from grove.connectors.calendar import SPEC, event_chat
    from grove.ids import new_id
    from grove.ingest import ReadBudget
    from grove.ingest.take_in import take_parsed
    from grove.writer.connectors import check_sync, start_sync
    from grove.writer.errors import InvalidRecord
    from grove.writer.testing import seed_test_world

    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    _, owner = start_sync(driver, seeded_world, chosen)
    other = make_world()
    seed_test_world(driver, other, owner_user_id=new_id())
    with pytest.raises(InvalidRecord):
        check_sync(driver, other, chosen, owner)
    chat = event_chat("primary", event(), "UTC", ReadBudget())
    for supplied in (None, "different-owner"):
        with pytest.raises(ConnectionBusy):
            take_parsed(
                driver,
                seeded_world,
                SPEC,
                chosen,
                [chat],
                screener=make_screener(),
                sync_owner=supplied,
                record_coverage=False,
            )
    no_sources(driver, seeded_world)
    no_sources(driver, other)


def test_git_disconnect_between_commits_uses_the_same_atomic_authority_fence(
    driver,
    seeded_world,
    client_for,
    deps,
    repository,
    test_clock,
    monkeypatch,
):
    import grove.ingest.take_in as pipeline
    from grove.connectors.service import sync_git

    path, _, commit = repository
    first = commit("First synthetic change")
    commit("Second synthetic change", [first], at="2026-10-02T08:00:00Z")
    deps.repositories[(seeded_world, "sample")] = path
    client = client_for(seeded_world)
    chosen = client.post("/v1/connectors/git", json={"repository": "sample"}).json()["id"]
    original = pipeline.write_chat_delta
    committed = 0
    disconnected = []

    def write(*args, **kwargs):
        nonlocal committed
        if committed:
            test_clock.advance(seconds=601)
            assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
            disconnected.append(connection(driver, seeded_world, chosen))
        result = original(*args, **kwargs)
        committed += 1
        return result

    monkeypatch.setattr(pipeline, "write_chat_delta", write)
    with pytest.raises(ConnectionBusy):
        sync_git(driver, seeded_world, chosen, path=path, screener=make_screener())
    stored = counts(driver, seeded_world)
    assert stored["sources"] == stored["episodes"] == stored["receipts"] == 1
    assert connection(driver, seeded_world, chosen) == disconnected[0]


def test_successful_multi_page_sync_and_replay_preserve_counts_and_cursor(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
):
    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)

    def answer(_path, query):
        if "pageToken" not in query:
            return 200, {"items": [event("one")], "nextPageToken": "second", "timeZone": "UTC"}
        return 200, {"items": [event("two")], "nextSyncToken": "after-two", "timeZone": "UTC"}

    fake_google.answer = answer
    result = sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    assert result["sources_added"] == result["episodes_added"] == 2
    before = counts(driver, seeded_world)
    assert (
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))["sources_added"]
        == 0
    )
    assert counts(driver, seeded_world) == before
    row = connection(driver, seeded_world, chosen)
    assert row["sync_cursor"] == "after-two" and row["last_outcome"] == "nothing_new"
    assert not row.get("sync_owner")


def test_revoked_410_response_cannot_admit_reset_request(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
):
    client = client_for(seeded_world)
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))

    def answer(*_):
        test_clock.advance(seconds=601)
        assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
        return 410, {}

    fake_google.answer = answer
    before = len(fake_google.requests)
    with pytest.raises(ConnectionBusy):
        sync_calendar(driver, seeded_world, chosen, **options(deps, fake_google))
    assert len(fake_google.requests) == before + 1
    no_sources(driver, seeded_world)
