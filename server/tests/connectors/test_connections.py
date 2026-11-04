import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from grove import clock
from grove.api.connectors import ConnectorDeps
from grove.api.google import GoogleClient
from grove.connectors.calendar import CALENDAR_SCOPE, CalendarRefused
from grove.connectors.secrets import SecretUnavailable, Vault, load_vault
from grove.connectors.service import sync_calendar
from grove.ids import new_id
from grove.store.query import WorldQuery
from grove.writer.connectors import connection, ensure_connection
from grove.writer.testing import seed_test_world
from tests.connectors.test_calendar import event
from tests.ingest.conftest import counts, make_screener


@pytest.fixture
def deps(app, fake_google):
    with httpx.Client() as http:
        deps = ConnectorDeps(
            http=http,
            client=GoogleClient(
                "test-client",
                "synthetic-client-secret",
                fake_google.base + "/authorize",
                fake_google.base + "/token",
            ),
            vault=Vault(b"v" * 32),
            calendar_base=fake_google.base,
        )
        app.state.connectors = deps
        yield deps


def token_response(nonce):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": "https://accounts.google.com",
                    "aud": "test-client",
                    "sub": "invented-account",
                    "nonce": nonce,
                    "exp": clock.now().timestamp() + 3600,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": CALENDAR_SCOPE,
        "id_token": "test." + payload + ".test",
    }


def connect(client, fake_google, driver, world, **settings):
    response = client.post(
        "/v1/connectors/google/start", json={"calendar_id": "primary", **settings}
    )
    assert response.status_code == 200
    assert len(response.headers["set-cookie"].encode()) < 4096
    query = {
        k: v[0] for k, v in parse_qs(urlsplit(response.json()["authorization_url"]).query).items()
    }
    fake_google.token_answer = token_response(query["nonce"])
    callback = client.get(
        "/v1/connectors/google/callback",
        params={"code": "synthetic-code", "state": query["state"]},
        follow_redirects=False,
    )
    assert callback.status_code == 303 and callback.headers["location"] == "/?calendar=connected"
    rows = WorldQuery(driver, world).read(
        "MATCH (c:Connector {connection_status: 'connected'}) RETURN c.id AS id"
    )
    return rows[0]["id"], query


def test_separate_consent_pkce_scope_and_sealed_credentials(
    driver, seeded_world, client_for, fake_google, deps, test_clock
):
    client = client_for(seeded_world)
    chosen, query = connect(client, fake_google, driver, seeded_world, log_from="2026-10-01")
    assert query["scope"].split() == ["openid", CALENDAR_SCOPE]
    assert query["access_type"] == "offline" and query["code_challenge_method"] == "S256"
    assert query["redirect_uri"].endswith("/v1/connectors/google/callback")
    row = connection(driver, seeded_world, chosen)
    assert "synthetic-access" not in str(row) and "synthetic-refresh" not in str(row)
    tokens = deps.vault.open(row["credential_ciphertext"], world=seeded_world, reference=chosen)
    assert tokens["calendar_id"] == "primary"
    assert json.loads(row["settings"])["log_from"] == "2026-10-01"
    public = client.get("/v1/sources/connectors")
    assert public.headers["cache-control"] == "no-store"
    assert "ciphertext" not in public.text and "synthetic-refresh" not in public.text


def test_maximum_calendar_identifier_does_not_exceed_cookie_limit(
    driver, seeded_world, client_for, fake_google, deps
):
    calendar_id = "\u754c" * 1024
    chosen, _ = connect(
        client_for(seeded_world), fake_google, driver, seeded_world, calendar_id=calendar_id
    )
    row = connection(driver, seeded_world, chosen)
    tokens = deps.vault.open(row["credential_ciphertext"], world=seeded_world, reference=chosen)
    assert tokens["calendar_id"] == calendar_id


def test_callback_uses_consumed_settings_snapshot_during_a_second_consent(
    driver, seeded_world, client_for, fake_google, deps, monkeypatch
):
    from grove.connectors import google
    from grove.writer.connectors import update_connection

    original = google.exchange

    def change_pending(*args, **kwargs):
        rows = WorldQuery(driver, seeded_world).read(
            "MATCH (c:Connector {connection_status: 'pending'}) RETURN c.id AS id"
        )
        update_connection(driver, seeded_world, rows[0]["id"], settings='{"log_from":"2020-01-01"}')
        return original(*args, **kwargs)

    monkeypatch.setattr(google, "exchange", change_pending)
    chosen, _ = connect(
        client_for(seeded_world), fake_google, driver, seeded_world, log_from="2026-10-01"
    )
    assert (
        json.loads(connection(driver, seeded_world, chosen)["settings"])["log_from"] == "2026-10-01"
    )


@pytest.mark.parametrize(
    "attack", ["state", "cookie", "expired", "scope", "denied", "replay", "world"]
)
def test_consent_refuses_tamper_replay_expiry_missing_scope_and_world_switch(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    test_clock,
    make_world,
    attack,
):
    client = client_for(seeded_world)
    response = client.post("/v1/connectors/google/start", json={})
    query = {
        k: v[0] for k, v in parse_qs(urlsplit(response.json()["authorization_url"]).query).items()
    }
    fake_google.token_answer = token_response(query["nonce"])
    params = {"state": query["state"], "code": "synthetic-code"}
    cookie = client.cookies.get("grove_calendar")
    if attack == "state":
        params["state"] = "changed"
    elif attack == "cookie":
        client.cookies.clear()
        client = client_for(seeded_world)
        client.cookies.set("grove_calendar", "changed")
    elif attack == "expired":
        test_clock.advance(minutes=11)
    elif attack == "scope":
        fake_google.token_answer["scope"] = "openid"
    elif attack == "denied":
        params["error"] = "access_denied"
    elif attack == "world":
        other = make_world()
        seed_test_world(driver, other, owner_user_id=new_id())
        client = client_for(other)
        client.cookies.set("grove_calendar", cookie)
    elif attack == "replay":
        first = client.get("/v1/connectors/google/callback", params=params, follow_redirects=False)
        assert first.headers["location"] == "/?calendar=connected"
        client.cookies.set("grove_calendar", cookie)
    response = client.get("/v1/connectors/google/callback", params=params, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/?calendar=refused"
    assert "synthetic-code" not in response.text
    rows = WorldQuery(driver, seeded_world).read(
        "MATCH (c:Connector {connection_status: 'connected'}) RETURN c.id AS id"
    )
    assert len(rows) == (1 if attack == "replay" else 0)


def test_calendar_sync_replay_new_etag_and_410_are_idempotent(
    driver, seeded_world, client_for, fake_google, deps, test_clock
):
    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    current = event()
    fake_google.answer = lambda *_: (200, {"items": [current], "nextSyncToken": "sync-a"})
    options = dict(
        http=deps.http,
        client=deps.client,
        vault=deps.vault,
        screener=make_screener(),
        base_url=fake_google.base,
    )
    first = sync_calendar(driver, seeded_world, chosen, **options)
    assert first["episodes_added"] == 1 and first["sources_added"] == 1
    before = counts(driver, seeded_world)
    assert sync_calendar(driver, seeded_world, chosen, **options)["sources_added"] == 0
    assert counts(driver, seeded_world) == before

    def reset(_path, query):
        return (
            (410, {})
            if "syncToken" in query
            else (200, {"items": [current], "nextSyncToken": "sync-b"})
        )

    fake_google.answer = reset
    assert sync_calendar(driver, seeded_world, chosen, **options)["reset"] is True
    assert counts(driver, seeded_world) == before
    current = event(etag="revision-two", summary="A revised plan")
    assert sync_calendar(driver, seeded_world, chosen, **options)["sources_added"] == 1
    sources = WorldQuery(driver, seeded_world).read("MATCH (s:Source) RETURN s.content AS content")
    assert len(sources) == 2 and all("speaker_kind" in row["content"] for row in sources)


def test_failed_or_denied_read_preserves_cursor_and_success_time(
    driver, seeded_world, client_for, fake_google, deps, test_clock
):
    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    opts = dict(
        http=deps.http,
        client=deps.client,
        vault=deps.vault,
        screener=make_screener(),
        base_url=fake_google.base,
    )
    sync_calendar(driver, seeded_world, chosen, **opts)
    before = connection(driver, seeded_world, chosen)
    test_clock.advance(minutes=1)
    fake_google.answer = lambda *_: (403, {})
    with pytest.raises(CalendarRefused):
        sync_calendar(driver, seeded_world, chosen, **opts)
    after = connection(driver, seeded_world, chosen)
    assert after["sync_cursor"] == before["sync_cursor"]
    assert after["last_successful_read"] == before["last_successful_read"]
    assert after["last_outcome"] == "denied" and after["denied_count"] == 1


def test_refresh_retains_refresh_token_and_disconnect_blocks_further_reads(
    driver, seeded_world, client_for, fake_google, deps, test_clock
):
    client = client_for(seeded_world, expires_at=clock.now() + timedelta(days=1))
    chosen, _ = connect(client, fake_google, driver, seeded_world)
    test_clock.advance(hours=2)
    fake_google.token_answer = {
        "access_token": "synthetic-new-access",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    result = client.post(f"/v1/connectors/{chosen}/sync", json={})
    assert result.status_code == 200
    row = connection(driver, seeded_world, chosen)
    tokens = deps.vault.open(row["credential_ciphertext"], world=seeded_world, reference=chosen)
    assert tokens["refresh_token"] == "synthetic-refresh"
    assert tokens["access_token"] == "synthetic-new-access"
    assert client.post(f"/v1/connectors/{chosen}/disconnect", json={}).status_code == 200
    assert "credential_ciphertext" not in connection(driver, seeded_world, chosen)
    before = len(fake_google.requests)
    assert client.post(f"/v1/connectors/{chosen}/sync", json={}).status_code == 503
    assert len(fake_google.requests) == before


def test_connection_ownership_and_origin_before_provider_call(
    driver, seeded_world, make_world, client_for, fake_google, deps
):
    chosen = ensure_connection(driver, seeded_world, "google_calendar", "invented")
    other = make_world()
    seed_test_world(driver, other, owner_user_id=new_id())
    client = client_for(other)
    for action in ("sync", "settings", "disconnect"):
        assert client.post(f"/v1/connectors/{chosen}/{action}", json={}).status_code == 404
    response = client.post(
        "/v1/connectors/google/start", json={}, headers={"Origin": "https://outside.example"}
    )
    assert response.status_code == 403 and fake_google.requests == []


def test_vault_binds_world_and_reference_and_key_file_permissions(tmp_path, monkeypatch):
    vault = Vault(b"v" * 32)
    sealed = vault.seal({"token": "synthetic"}, world="one", reference="first")
    for world, ref in (("two", "first"), ("one", "second")):
        with pytest.raises(SecretUnavailable):
            vault.open(sealed, world=world, reference=ref)
    path = tmp_path / "key"
    path.touch(mode=0o600)
    path.write_bytes(base64.urlsafe_b64encode(b"v" * 32))
    monkeypatch.setenv("GROVE_CONNECTOR_KEY_FILE", str(path))
    assert isinstance(load_vault(), Vault)
    path.chmod(0o644)
    with pytest.raises(SecretUnavailable):
        load_vault()


def test_partial_commit_replays_without_losing_event_or_advancing_cursor(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    monkeypatch,
):
    import grove.ingest.take_in as pipeline
    from grove.writer.errors import InvalidRecord

    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (
        200,
        {
            "items": [event("first"), event("second")],
            "nextSyncToken": "after-two",
        },
    )
    original = pipeline.write_chat_delta
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise InvalidRecord("invalid_arguments", "planted", "planted writer refusal")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "write_chat_delta", fail_second)
    options = dict(
        http=deps.http,
        client=deps.client,
        vault=deps.vault,
        screener=make_screener(),
        base_url=fake_google.base,
    )
    with pytest.raises(CalendarRefused):
        sync_calendar(driver, seeded_world, chosen, **options)
    failed = connection(driver, seeded_world, chosen)
    assert not failed.get("sync_cursor") and not failed.get("last_successful_read")
    assert counts(driver, seeded_world)["sources"] == 1
    monkeypatch.setattr(pipeline, "write_chat_delta", original)
    assert sync_calendar(driver, seeded_world, chosen, **options)["sources_added"] == 1
    assert counts(driver, seeded_world)["episodes"] == 2
    assert connection(driver, seeded_world, chosen)["sync_cursor"] == "after-two"


def test_poll_ingests_without_manual_sync_and_respects_interval(
    driver,
    seeded_world,
    client_for,
    fake_google,
    deps,
    app,
    test_clock,
    monkeypatch,
):
    from grove.connectors import poll

    connect(client_for(seeded_world), fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (200, {"items": [event()], "nextSyncToken": "first"})

    class OnlyFixtureWorld:
        def __init__(self, _driver):
            pass

        def read(self, _query):
            return [{"id": seeded_world}]

    monkeypatch.setattr(poll, "PlatformQuery", OnlyFixtureWorld)
    assert poll.poll_once(driver, app.state.settings, deps) is True
    assert counts(driver, seeded_world)["sources"] == 1
    assert poll.poll_once(driver, app.state.settings, deps) is False
    test_clock.advance(seconds=301)
    assert poll.poll_once(driver, app.state.settings, deps) is True
    assert counts(driver, seeded_world)["sources"] == 1


def test_api_lifespan_starts_and_joins_automatic_sync(
    driver, seeded_world, client_for, fake_google, deps, app, monkeypatch
):
    import threading

    from fastapi.testclient import TestClient

    from grove.api.app import create_app
    from grove.connectors import poll

    connect(client_for(seeded_world), fake_google, driver, seeded_world)
    fake_google.answer = lambda *_: (200, {"items": [event()], "nextSyncToken": "automatic"})
    completed = threading.Event()
    original = poll._read_selected

    def read_selected(*args):
        result = original(*args)
        completed.set()
        return result

    class OnlyFixtureWorld:
        def __init__(self, _driver):
            pass

        def read(self, _query):
            return [{"id": seeded_world}]

    monkeypatch.setattr(poll, "PlatformQuery", OnlyFixtureWorld)
    monkeypatch.setattr(poll, "_read_selected", read_selected)
    monkeypatch.setenv("GROVE_CONNECTOR_POLL", "1")
    automatic = create_app(driver=driver, settings=app.state.settings, connector_deps=deps)
    with TestClient(automatic):
        assert completed.wait(10), "automatic sync did not finish"
        assert counts(driver, seeded_world)["sources"] == 1
    assert completed.is_set()


def test_poll_configuration_failure_does_not_starve_next_connection(
    driver, seeded_world, deps, app, monkeypatch
):
    from grove.connectors import poll
    from grove.writer.connectors import update_connection

    chosen = [ensure_connection(driver, seeded_world, "git", name) for name in ("bad", "good")]
    for item in chosen:
        update_connection(driver, seeded_world, item, connection_status="connected")
    seen = []

    class OnlyFixtureWorld:
        def __init__(self, _driver):
            pass

        def read(self, _query):
            return [{"id": seeded_world}]

    def fail_before_lease(_driver, _settings, _deps, _world, item, _row):
        seen.append(item)
        raise ValueError("synthetic missing configuration")

    monkeypatch.setattr(poll, "PlatformQuery", OnlyFixtureWorld)
    monkeypatch.setattr(poll, "_read_selected", fail_before_lease)
    for _ in chosen:
        with pytest.raises(ValueError, match="synthetic missing configuration"):
            poll.poll_once(driver, app.state.settings, deps)
    assert set(seen) == set(chosen)
    assert all(connection(driver, seeded_world, item)["failed_count"] == 1 for item in chosen)


def test_disconnect_revokes_expired_read_owner_before_it_can_restore_credentials(
    driver, seeded_world, test_clock
):
    from grove.writer.connectors import (
        ConnectionBusy,
        disconnect,
        finish_sync,
        start_sync,
        update_connection,
    )

    chosen = ensure_connection(driver, seeded_world, "google_calendar", "sample")
    update_connection(
        driver,
        seeded_world,
        chosen,
        connection_status="connected",
        credential_ciphertext="synthetic-ciphertext",
    )
    _, owner = start_sync(driver, seeded_world, chosen)
    with pytest.raises(ConnectionBusy):
        disconnect(driver, seeded_world, chosen)
    test_clock.advance(seconds=601)
    disconnect(driver, seeded_world, chosen)
    with pytest.raises(ConnectionBusy):
        finish_sync(
            driver,
            seeded_world,
            chosen,
            owner,
            outcome="nothing_new",
            credential="synthetic-ciphertext",
            cursor="stale-read",
        )
    after = connection(driver, seeded_world, chosen)
    assert after["connection_status"] == "disconnected"
    assert "credential_ciphertext" not in after
    assert "sync_cursor" not in after
