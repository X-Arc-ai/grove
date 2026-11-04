"""Exact invented bytes keep real screening active with absent global policy files."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.api.errors import ApiError
from grove.demo.provision import provision
from grove.demo.screening import lumen_fixture, new_demo_screener
from grove.demo_policy import DEMO_TEXT
from grove.mcp.store import DemoStoreHandler, StoreHandler
from grove.mcp.tools import ToolContext
from grove.screening import Message, Screener
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE, token_hash
from grove.writer.accounts import create_session
from grove.writer.upload_tokens import mint_upload_token
from tests.hosts.test_demo_transport import demo as demo
from tests.hosts.test_mcp_transport import BASE


@pytest.fixture
def missing_globals(monkeypatch):
    monkeypatch.setenv("GROVE_ENV", "production")
    monkeypatch.delenv("GROVE_RENAME_MAP", raising=False)
    monkeypatch.delenv("GROVE_DENYLIST", raising=False)


def test_exact_lumen_file_uses_real_screening_and_parser(
    driver, person, missing_globals, monkeypatch
):
    calls = []
    original = Screener.screen_source

    def observe(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Screener, "screen_source", observe)
    app = create_app(ApiSettings(BASE), driver=driver)
    with TestClient(app, base_url=BASE) as web:
        web.cookies.update(person.cookies)
        response = web.post(
            "/v1/sources/upload?provider=claude_ai",
            content=lumen_fixture(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
    assert calls
    sources = WorldQuery(driver, person.world_id).read("MATCH (s:Source) RETURN s.content AS text")
    assert len(sources) == 1
    assert "fictional Lumen workshop" in sources[0]["text"]
    assert WorldQuery(driver, person.world_id).read("MATCH (r:Request) RETURN r.id") == []


@pytest.mark.parametrize("variant", ["changed", "oversized", "encoded", "provider", "token"])
def test_other_upload_inputs_keep_the_configuration_refusal(
    driver, person, missing_globals, variant
):
    body = lumen_fixture()
    headers = {"content-type": "application/json"}
    provider = "claude_ai"
    if variant == "changed":
        body = body.replace(b"teal", b"blue")
    elif variant == "oversized":
        body += b" "
    elif variant == "encoded":
        headers["content-encoding"] = "identity"
    elif variant == "provider":
        provider = "chatgpt"
    else:
        issued = mint_upload_token(driver, person.world_id)
        headers["authorization"] = f"Bearer {issued.token}"
    app = create_app(ApiSettings(BASE), driver=driver)
    with TestClient(app, base_url=BASE) as web:
        web.cookies.update(person.cookies)
        response = web.post(
            f"/v1/sources/upload?provider={provider}", content=body, headers=headers
        )
    assert response.status_code == 503
    assert WorldQuery(driver, person.world_id).read("MATCH (s:Source) RETURN s.id") == []


def test_cedar_world_refuses_lumen_upload(driver, demo, missing_globals):
    session_token = "invented-demo-session-" + uuid4().hex
    create_session(
        driver,
        demo.user_id,
        token_hash(session_token),
        method="email",
        expires_at=clock.now() + timedelta(minutes=5),
    )
    app = create_app(ApiSettings(BASE), driver=driver)
    with TestClient(app, base_url=BASE) as web:
        web.cookies.set(SESSION_COOKIE, session_token)
        response = web.post(
            "/v1/sources/upload?provider=claude_ai",
            content=lumen_fixture(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 403


def test_cedar_store_and_seed_use_fresh_real_policy(driver, demo, missing_globals):
    context = ToolContext(
        driver, demo.world_id, demo.user_id, "demo-test", frozenset({"grove.write"}), "test"
    )
    args = {"text": DEMO_TEXT, "verbatim": False, "idempotency_key": "direct"}
    assert DemoStoreHandler(wait_seconds=0)(context, args)["state"] == "saving"
    with pytest.raises(ApiError):
        DemoStoreHandler(wait_seconds=0)(context, {**args, "text": DEMO_TEXT + " Other text."})
    with pytest.raises(ApiError) as refused:
        StoreHandler(wait_seconds=0)(context, args)
    assert refused.value.code == "unavailable"
    # Obtain only the already verified configured hash, never the account's address.
    from grove.demo_policy import configured_email_hash

    result = provision(
        driver, configured_email_hash(), wait_seconds=0, world_id_for=lambda _: demo.world_id
    )
    assert result["state"] == "saving"
    first, second = new_demo_screener(), new_demo_screener()
    assert first is not second
    assert first._config is not second._config
    screened = first.screen_source(
        "", [Message(key="test", sender=None, text="Demo Restricted Name", at=clock.now())]
    )
    assert screened.messages[0].text == "Demo Participant"
