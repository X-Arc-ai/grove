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
from grove.writer.oauth import revoke_grant
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
    return Person(account.user_id, account.world_id, "unused", account.self_item_id, "You")


def rpc(method, params=None):
    return {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}


def test_demo_tools_real_scope_owner_and_revocation(driver, demo, hosts_plant):
    client_id = hosts_plant.client()
    token = issue(driver, demo, client_id, scopes=("grove.read",))
    app = create_app(ApiSettings(BASE), driver=driver)
    with TestClient(app) as web:
        response = web.post("/mcp", json=rpc("tools/list"), headers=bearer(token.access))
        assert response.status_code == 200
        assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["store", "recall"]
        batch = [rpc("ping"), rpc("tools/call", {"name": "store", "arguments": {}})]
        refused = web.post("/mcp", json=batch, headers=bearer(token.access))
        assert refused.status_code == 403
        assert WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r.id AS id") == []
        other = hosts_plant.person()
        foreign = issue(driver, other, hosts_plant.client())
        refused = web.post("/mcp", json=rpc("tools/list"), headers=bearer(foreign.access))
        assert refused.status_code == 403
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
            driver, demo.world_id, demo.user_id, token.client_id,
            frozenset({"grove.read", "grove.write"}), token.grant_id,
        )
        complete(context, result["request_id"], title=DEMO_TEXT)
        saved = web.post("/mcp", json=call, headers=bearer(token.access))
        assert saved.json()["result"]["structuredContent"]["state"] == "saved"
        recalled = web.post(
            "/mcp", json=rpc("tools/call", {"name": "recall", "arguments": {"about": "Cedar"}}),
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
        return revoke_grant(
            driver, grant_id=token.grant_id, world_id=demo.world_id, at=clock.now()
        )

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
