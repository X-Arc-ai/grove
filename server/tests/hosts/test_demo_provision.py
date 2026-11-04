"""Focused demo account, intake and seed controls against the real local store."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from grove import clock
from grove.demo.provision import private_email_hash, provision
from grove.demo_policy import (
    DEMO_SEED_CLIENT,
    DEMO_TEXT,
    DEMO_VERSION,
    DemoDenied,
    configured_email_hash,
    is_demo_world,
    require_designation,
)
from grove.ids import new_id
from grove.mcp.store import StoreHandler, request_identity
from grove.mcp.tools import ToolContext
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.session import find_session, token_hash
from grove.writer import Caller, enqueue_request
from grove.writer.accounts import create_session, email_hash, ensure_account, ensure_demo_account
from grove.writer.ingest import ensure_connector
from grove.writer.sources import (
    add_screened_source,
    record_connector,
    record_source,
    write_screened_source,
)
from grove.writer.testing import purge_test_account, purge_test_world
from grove.writer.tx import WorldWriter
from tests.auth.conftest import Accounts
from tests.hosts.test_store_component import complete


@pytest.fixture
def demo_accounts(driver, migrated, monkeypatch):
    accounts = Accounts()
    address = accounts.address()
    hashed = email_hash(address)
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
    yield accounts, address, hashed
    for world_id in accounts.worlds:
        purge_test_world(driver, world_id)
    for value in accounts.hashes:
        purge_test_account(driver, value)


@pytest.fixture
def demo(driver, demo_accounts):
    accounts, _, hashed = demo_accounts
    return ensure_demo_account(driver, hashed, world_id_for=accounts.world_id_for)


def screen(text=DEMO_TEXT):
    return Screener(ScreeningConfig()).screen_source(
        "", [Message(key="cedar-1", sender=None, text=text, at=clock.now())]
    )


def connector(driver, world_id, client="demo-check"):
    actor = "host:" + hashlib.sha256(client.encode()).hexdigest()
    connector_id = ensure_connector(
        driver, world_id, "ai_host", actor, batch_rule="host_request", auth_reference="oauth"
    )
    return actor, connector_id


def queue(driver, demo, *, text=DEMO_TEXT, request_id="demo-check-1", pass_id=True):
    actor, connector_id = connector(driver, demo.world_id)
    screened = screen(text)

    def source(tx):
        source_id = new_id(tx.world_id)
        write_screened_source(
            tx,
            screened,
            source_id=source_id,
            connector_id=connector_id,
            parser_version="mcp-store-1",
            demo_request_id=request_id if pass_id else None,
        )
        return source_id

    return enqueue_request(
        driver,
        demo.world_id,
        Caller("ai_host", frozenset({"grove.read", "grove.write"}), actor),
        request_id,
        write_source=source,
        content_key=hashlib.sha256(text.encode()).hexdigest(),
        verbatim=True,
        host_message_id="cedar-1",
    )


def test_fresh_provision_is_concurrent_idempotent_and_normal_session_selects_it(
    driver, demo_accounts
):
    accounts, _, hashed = demo_accounts
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: ensure_demo_account(driver, hashed, world_id_for=accounts.world_id_for),
                range(2),
            )
        )
    assert results[0].user_id == results[1].user_id
    assert results[0].world_id == results[1].world_id
    account = results[0]
    row = require_designation(driver, account.world_id, account.user_id)
    assert row["demo_version"] == DEMO_VERSION
    assert row["self_item_id"] == account.self_item_id
    normal = ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)
    assert normal.user_id == account.user_id and normal.world_id == account.world_id
    create_session(
        driver,
        account.user_id,
        token_hash("demo-session"),
        method="email",
        expires_at=clock.now() + timedelta(hours=1),
    )
    session = find_session(PlatformQuery(driver), "demo-session", clock.now())
    assert session.world_id == account.world_id


def test_existing_ordinary_account_cannot_be_designated(driver, demo_accounts):
    accounts, _, hashed = demo_accounts
    account = ensure_account(driver, hashed, method="email", world_id_for=accounts.world_id_for)
    with pytest.raises(DemoDenied):
        ensure_demo_account(driver, hashed, world_id_for=accounts.world_id_for)
    assert not is_demo_world(driver, account.world_id)


def test_wrong_owner_and_missing_configuration_refuse_disclosure(driver, demo, monkeypatch):
    with pytest.raises(DemoDenied):
        require_designation(driver, demo.world_id, new_id())
    monkeypatch.delenv("GROVE_DEMO_EMAIL_HASH")
    assert is_demo_world(driver, demo.world_id)
    with pytest.raises(DemoDenied):
        require_designation(driver, demo.world_id, demo.user_id)
    with pytest.raises(DemoDenied):
        record_connector(driver, demo.world_id, "notes_files_releases")


@pytest.mark.parametrize("value", [" ", "A" * 64, "a" * 63, "a" * 65])
def test_malformed_hash_refuses(value, monkeypatch):
    monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", value)
    with pytest.raises(DemoDenied):
        configured_email_hash()


def test_approved_source_has_exact_request_binding_and_real_saved_receipt(driver, demo):
    first = queue(driver, demo)
    assert first.api_state == "saving"
    assert queue(driver, demo).request_id == first.request_id
    sources = WorldQuery(driver, demo.world_id).read(
        "MATCH (s:Source) WHERE s.demo_version IS NOT NULL RETURN s"
    )
    assert len(sources) == 1
    assert sources[0]["s"]["demo_request_id"] == first.request_id
    assert first.source_id == sources[0]["s"]["id"]
    context = ToolContext(
        driver,
        demo.world_id,
        demo.user_id,
        "demo-check",
        frozenset({"grove.read", "grove.write"}),
        "demo-grant",
    )
    complete(context, first.request_id)
    assert queue(driver, demo).api_state == "saved"


@pytest.mark.parametrize(
    "text,pass_id", [(DEMO_TEXT + " Extra material.", True), (DEMO_TEXT, False)]
)
def test_unapproved_or_unbound_source_rolls_back_request(driver, demo, text, pass_id):
    with pytest.raises(DemoDenied):
        queue(driver, demo, text=text, pass_id=pass_id)
    assert not WorldQuery(driver, demo.world_id).read("MATCH (r:Request) RETURN r")


def test_detached_source_and_other_connector_paths_refuse(driver, demo):
    _, connector_id = connector(driver, demo.world_id)
    with pytest.raises(DemoDenied):
        add_screened_source(
            WorldWriter(driver, demo.world_id),
            screen(),
            connector_id=connector_id,
            parser_version="mcp-store-1",
        )
    with pytest.raises(DemoDenied):
        record_source(
            driver,
            demo.world_id,
            connector_id=connector_id,
            external_locator="manual",
            content=DEMO_TEXT,
            parser_version="mcp-store-1",
        )
    with pytest.raises(DemoDenied):
        ensure_connector(driver, demo.world_id, "ai_host", "upload", batch_rule="chat")


def test_private_email_file_requires_private_mode_and_config_match(
    demo_accounts, monkeypatch, tmp_path
):
    _, address, hashed = demo_accounts
    file = tmp_path / "demo-email"
    file.write_text(address)
    file.chmod(0o600)
    monkeypatch.setenv("GROVE_DEMO_EMAIL_FILE", str(file))
    assert private_email_hash() == hashed
    file.chmod(0o644)
    with pytest.raises(DemoDenied):
        private_email_hash()


def test_command_never_calls_a_queued_request_ready(driver, demo_accounts):
    accounts, _, hashed = demo_accounts
    handler = StoreHandler(screener=Screener(ScreeningConfig()), wait_seconds=0)
    result = provision(
        driver,
        hashed,
        wait_seconds=0,
        world_id_for=accounts.world_id_for,
        handler=handler,
    )
    assert result == {"state": "saving", "saved_requests": 0}
    account = ensure_demo_account(driver, hashed, world_id_for=accounts.world_id_for)
    context = ToolContext(
        driver,
        account.world_id,
        account.user_id,
        DEMO_SEED_CLIENT,
        frozenset({"grove.read", "grove.write"}),
        "operator-demo-provision",
    )
    complete(context, request_identity(context, f"{DEMO_VERSION}-0"))
    repeated = provision(
        driver, hashed, wait_seconds=0, world_id_for=accounts.world_id_for, handler=handler
    )
    assert repeated == {"state": "ready", "saved_requests": 1}
    retained = WorldQuery(driver, account.world_id).read(
        "MATCH (s:Source) WHERE s.demo_version IS NOT NULL RETURN s"
    )
    assert len(retained) == 1
