"""Direct restricted-demo transport and release checks against the real OAuth store."""

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.config import ConfigError
from grove.demo_guard import ReleaseFile, configuration
from grove.demo_policy import DEMO_TEXT
from grove.mcp.demo import DemoReleaseMiddleware
from grove.mcp.tools import ToolContext
from grove.store.query import WorldQuery
from grove.writer.accounts import email_hash, ensure_demo_account
from grove.writer.connectors import (
    check_sync,
    connection,
    consume_state,
    disconnect,
    ensure_connection,
    finish_sync,
    start_sync,
    update_connection,
)
from grove.writer.errors import InvalidRecord
from grove.writer.oauth import revoke_grant
from grove.writer.testing import plant_node, purge_test_account
from tests.hosts.conftest import Person
from tests.hosts.test_mcp_transport import BASE, bearer, issue
from tests.hosts.test_store_component import complete


@pytest.fixture
def demo(driver, make_world, monkeypatch, tmp_path):
    address = f"demo-{uuid4().hex}@example.invalid"
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", email_hash(address))
    path = tmp_path / "demo-release.lock"
    path.touch(mode=0o600)
    monkeypatch.setenv("GROVE_DEMO_GUARD_FILE", str(path))
    world = make_world()
    account = ensure_demo_account(driver, email_hash(address), world_id_for=lambda _: world)
    try:
        yield Person(account.user_id, account.world_id, "unused", account.self_item_id, "You")
    finally:
        purge_test_account(driver, email_hash(address))


def rpc(method, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}


def test_demo_tools_real_scope_owner_and_revocation(driver, demo, hosts_plant):
    client_id = hosts_plant.client()
    token = issue(driver, demo, client_id, scopes=("grove.read",))
    app = create_app(ApiSettings(BASE), driver=driver)
    with TestClient(app) as web:
        response = web.post("/mcp", json=rpc("tools/list"), headers=bearer(token.access))
        assert response.status_code == 200
        listed = response.json()["result"]["tools"]
        assert [tool["title"] for tool in listed] == [
            "Store the Cedar demo fact",
            "Recall the Cedar demo",
        ]
        batch = [rpc("ping"), rpc("tools/call", {"name": "store", "arguments": {}})]
        refused = web.post("/mcp", json=batch, headers=bearer(token.access))
        assert refused.status_code == 403
        assert WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r.id AS id") == []
        other = hosts_plant.person()
        foreign = issue(driver, other, hosts_plant.client())
        general = web.post("/mcp", json=rpc("tools/list"), headers=bearer(foreign.access))
        assert general.status_code == 200
        assert [tool["title"] for tool in general.json()["result"]["tools"]] == [
            "Store in Grove",
            "Recall from Grove",
        ]
        revoke_grant(driver, grant_id=token.grant_id, world_id=demo.world_id, at=clock.now())
        refused = web.post("/mcp", json=rpc("tools/list"), headers=bearer(token.access))
        assert refused.status_code == 401
        assert "invalid_token" in refused.headers["www-authenticate"]


def test_real_mcp_store_saved_receipt_and_recall(driver, demo, hosts_plant):
    token = issue(driver, demo, hosts_plant.client())
    app = create_app(ApiSettings(BASE), driver=driver)
    arguments = {"text": DEMO_TEXT, "verbatim": False, "idempotency_key": "cedar-demo"}
    call = rpc("tools/call", {"name": "store", "arguments": arguments})
    with TestClient(app) as web:
        queued = web.post("/mcp", json=call, headers=bearer(token.access))
        assert queued.status_code == 200
        result = queued.json()["result"]["structuredContent"]
        assert result["state"] == "saving"
        context = ToolContext(
            driver,
            demo.world_id,
            demo.user_id,
            token.client_id,
            frozenset({"grove.read", "grove.write"}),
            token.grant_id,
        )
        complete(context, result["request_id"], title=DEMO_TEXT)
        saved = web.post("/mcp", json=call, headers=bearer(token.access))
        assert saved.json()["result"]["structuredContent"]["state"] == "saved"
        recalled = web.post(
            "/mcp",
            json=rpc("tools/call", {"name": "recall", "arguments": {"about": "Cedar"}}),
            headers=bearer(token.access),
        )
        packet = recalled.json()["result"]["structuredContent"]
        assert packet["answer"] == "found"
        assert packet["items"][0]["title"] == DEMO_TEXT
        assert len(WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r.id")) == 1


def _scope(driver, token):
    return {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [(b"authorization", f"Bearer {token.access}".encode())],
        "app": SimpleNamespace(state=SimpleNamespace(driver=driver, settings=ApiSettings(BASE))),
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _exclusive_probe():
    """A separate process tries the same inode; no database or credentials cross the boundary."""
    code = (
        "import fcntl,os; "
        "f=open(os.environ['GROVE_DEMO_GUARD_FILE'],'r+'); "
        "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    return result.returncode == 0


def test_release_guard_covers_final_asgi_body_in_another_process(driver, demo, hosts_plant):
    token = issue(driver, demo, hosts_plant.client())
    executor = ThreadPoolExecutor(max_workers=1)
    entered = threading.Event()
    mutations = []

    def revoke():
        entered.set()
        return revoke_grant(driver, grant_id=token.grant_id, world_id=demo.world_id, at=clock.now())

    async def run():
        sent = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"fact":"Cedar"}'})

        async def send(message):
            assert not _exclusive_probe()
            if message["type"] == "http.response.body":
                pending = executor.submit(revoke)
                mutations.append(pending)
                assert entered.wait(2)
                with pytest.raises(TimeoutError):
                    pending.result(timeout=0.05)
            sent.append(message)

        middleware = DemoReleaseMiddleware(inner, configuration())
        await middleware(_scope(driver, token), _receive, send)
        assert sent[-1]["body"] == b'{"fact":"Cedar"}'
        assert _exclusive_probe()

    try:
        asyncio.run(run())
        assert mutations[0].result(timeout=5)
    finally:
        executor.shutdown(wait=True)


def test_expiry_during_serialization_releases_no_fact(driver, demo, hosts_plant):
    provider = clock.TestClock()
    provider.set(clock.now())
    with clock.using(provider):
        token = issue(driver, demo, hosts_plant.client(), access_lifetime=timedelta(seconds=1))

        async def run():
            sent = []

            async def inner(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                provider.set(clock.now() + timedelta(seconds=2))
                await send({"type": "http.response.body", "body": b'{"fact":"not released"}'})

            async def send(message):
                sent.append(message)

            middleware = DemoReleaseMiddleware(inner, configuration())
            await middleware(_scope(driver, token), _receive, send)
            assert sent[0]["status"] == 401
            assert b"not released" not in sent[-1]["body"]

        asyncio.run(run())


def test_native_cancellation_keeps_guard_until_owned_worker_ends(driver, demo, hosts_plant):
    token = issue(driver, demo, hosts_plant.client())

    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        sent = []

        async def inner(scope, receive, send):
            entered.set()
            await finish.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"abandoned"})

        async def send(message):
            sent.append(message)

        middleware = DemoReleaseMiddleware(inner, configuration())
        task = asyncio.create_task(middleware(_scope(driver, token), _receive, send))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not _exclusive_probe()
        finish.set()
        await asyncio.gather(*tuple(middleware.active))
        assert sent == []
        assert _exclusive_probe()

    asyncio.run(run())


def test_missing_and_unsafe_guard_configuration_refuses(demo, monkeypatch):
    path = os.environ["GROVE_DEMO_GUARD_FILE"]
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError):
        ReleaseFile(configuration())
    monkeypatch.delenv("GROVE_DEMO_GUARD_FILE")
    with pytest.raises(ConfigError):
        configuration()
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", "not-a-hash")
    with pytest.raises(ConfigError):
        configuration()


def test_incomplete_body_does_not_block_real_revocation(driver, demo, hosts_plant):
    token = issue(driver, demo, hosts_plant.client())

    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        sent = []

        async def receive():
            entered.set()
            await finish.wait()
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def inner(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message):
            sent.append(message)

        middleware = DemoReleaseMiddleware(inner, configuration())
        task = asyncio.create_task(middleware(_scope(driver, token), receive, send))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            assert _exclusive_probe(), "an unfinished body must not hold the release guard"
            assert revoke_grant(
                driver, grant_id=token.grant_id, world_id=demo.world_id, at=clock.now()
            )
        finally:
            finish.set()
            await task
        assert sent[0]["status"] == 401

    asyncio.run(run())


def test_incomplete_body_has_a_deadline_without_authority_lock(
    driver, demo, hosts_plant, monkeypatch
):
    from grove.mcp import demo as transport

    monkeypatch.setattr(transport, "BODY_TIMEOUT_SECONDS", 0.02)
    token = issue(driver, demo, hosts_plant.client())

    async def run():
        sent = []

        async def receive():
            await asyncio.Event().wait()

        async def inner(scope, receive, send):
            pytest.fail("incomplete request reached a handler")

        async def send(message):
            assert _exclusive_probe()
            sent.append(message)

        await DemoReleaseMiddleware(inner, configuration())(_scope(driver, token), receive, send)
        assert sent[0]["status"] == 408

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["google_calendar", "git"])
def test_designated_world_refuses_connection_attachment(driver, demo, provider):
    with pytest.raises(InvalidRecord) as refused:
        ensure_connection(driver, demo.world_id, provider, "invented-connection")
    assert refused.value.code == "scope_denied"


def test_designated_world_refuses_connection_sync(driver, demo):
    connector = plant_node(
        driver, demo.world_id, "Connector", provider="git", connection_status="connected"
    )
    with pytest.raises(InvalidRecord) as refused:
        start_sync(driver, demo.world_id, connector["id"])
    assert refused.value.code == "scope_denied"


def test_connection_authorization_refuses_but_safe_cleanup_remains(driver, demo, hosts_plant):
    held = plant_node(
        driver,
        demo.world_id,
        "Connector",
        provider="git",
        connection_status="connected",
        sync_owner="old-reader",
        credential_ciphertext="sealed-fixture",
    )["id"]
    with pytest.raises(InvalidRecord):
        consume_state(driver, demo.world_id, held, "fixture-state")
    with pytest.raises(InvalidRecord):
        update_connection(driver, demo.world_id, held, connection_status="connected")
    with pytest.raises(InvalidRecord):
        check_sync(driver, demo.world_id, held, "old-reader")
    with pytest.raises(InvalidRecord):
        finish_sync(driver, demo.world_id, held, "old-reader", outcome="failed", credential="new")
    finish_sync(driver, demo.world_id, held, "old-reader", outcome="failed")
    disconnect(driver, demo.world_id, held)
    row = connection(driver, demo.world_id, held)
    assert row["connection_status"] == "disconnected"
    assert row.get("credential_ciphertext") is None
    ordinary = hosts_plant.person()
    assert ensure_connection(driver, ordinary.world_id, "git", "ordinary-invented-repo")
