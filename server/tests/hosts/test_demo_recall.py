"""Restricted recall uses actual queued requests and writer commits in invented worlds."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.api.errors import ApiError
from grove.demo_policy import DEMO_TEXT
from grove.ids import new_id
from grove.mcp.store import _digest
from grove.mcp.tools import ToolContext
from grove.reads.demo_current import (
    _CAPTURE,
    MAX_ITEMS,
    MAX_ROWS,
    MAX_TEXT,
    MAX_VALUE,
    DemoRecallHandler,
    _validate,
)
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.current import SUCCESSOR_MEANINGS
from grove.store.query import WorldQuery
from grove.writer import Caller, apply_batch, enqueue_request
from grove.writer.accounts import email_hash, ensure_demo_account
from grove.writer.ingest import ensure_connector
from grove.writer.sources import write_screened_source
from grove.writer.testing import plant_node, purge_test_account, purge_test_world
from tests.auth.conftest import Accounts
from tests.hosts.test_store_component import complete
from tests.writer.helpers import world_dump


@pytest.fixture
def demo_context(driver, migrated, monkeypatch):
    accounts = Accounts()
    provider = clock.TestClock()
    provider.set(datetime(2026, 11, 4, 12, tzinfo=UTC))
    with clock.using(provider):
        hashed = email_hash(accounts.address())
        monkeypatch.setenv("GROVE_DEMO_EMAIL_HASH", hashed)
        account = ensure_demo_account(driver, hashed, world_id_for=accounts.world_id_for)
        try:
            yield (
                ToolContext(
                    driver,
                    account.world_id,
                    account.user_id,
                    "recall-check",
                    frozenset({"grove.read", "grove.write"}),
                    "check-grant",
                ),
                account,
            )
        finally:
            for world_id in accounts.worlds:
                purge_test_world(driver, world_id)
            for hashed_address in accounts.hashes:
                purge_test_account(driver, hashed_address)


def queue(context):
    """The same screened callback and queue as production, with no model substitute in code."""
    actor = "host:" + _digest(context.client_id)
    connector = ensure_connector(
        context.driver,
        context.world_id,
        "ai_host",
        actor,
        batch_rule="host_request",
        auth_reference="oauth",
    )
    request_id = "demo-check:" + new_id(context.world_id)
    screened = Screener(ScreeningConfig()).screen_source(
        "",
        [Message(key="cedar-message", sender=None, text=DEMO_TEXT, at=clock.now(context.world_id))],
    )

    def source(tx):
        source_id = new_id(tx.world_id)
        write_screened_source(
            tx,
            screened,
            source_id=source_id,
            connector_id=connector,
            parser_version="mcp-store-1",
            demo_request_id=request_id,
        )
        return source_id

    result = enqueue_request(
        context.driver,
        context.world_id,
        Caller("ai_host", context.scopes, actor),
        request_id,
        write_source=source,
        content_key="cedar-check",
        verbatim=False,
    )
    assert result.api_state == "saving"
    return request_id


def capture(context, account):
    return WorldQuery(context.driver, context.world_id).read(
        _CAPTURE,
        own=account.self_item_id,
        item_cap=MAX_ITEMS + 1,
        row_cap=MAX_ROWS + 1,
        text_cap=MAX_TEXT,
        value_cap=MAX_VALUE,
        successors=list(SUCCESSOR_MEANINGS),
    )[0]


def test_saved_graph_recall_is_pure_and_excludes_identity(demo_context):
    context, account = demo_context
    request_id = queue(context)
    handler = DemoRecallHandler()
    assert handler(context, {"about": "Cedar"})["items"] == []
    complete(context, request_id, title=DEMO_TEXT)
    before = world_dump(context.driver, context.world_id)
    result = handler(context, {"about": "Which room is assigned to Cedar?"})
    assert result["answer"] == "found"
    assert [row["title"] for row in result["items"]] == [DEMO_TEXT]
    assert all(row["id"] != account.self_item_id for row in result["items"])
    assert result["items"][0]["provenance"][0]["request_id"] == request_id
    assert world_dump(context.driver, context.world_id) == before
    miss = handler(context, {"about": "Juniper"})
    assert miss["items"] == [] and miss["available_topics"] == ["Cedar demo"]


def test_later_field_change_with_original_provenance_refuses(demo_context):
    context, _ = demo_context
    request_id = queue(context)
    complete(context, request_id, title=DEMO_TEXT)
    query = WorldQuery(context.driver, context.world_id)
    item = query.read("MATCH (i:Item {title: $title}) RETURN i", title=DEMO_TEXT)[0]["i"]
    apply_batch(
        context.driver,
        context.world_id,
        Caller("person", context.scopes, "person:test"),
        {
            "ops": [
                {
                    "op": "update_item",
                    "op_id": "change",
                    "item": item["id"],
                    "version": item["version"],
                    "fields": {"title": "Unapproved later assertion"},
                    "evidence_refs": [
                        {"evidence_id": item["provenance_evidence_id"], "polarity": "supports"}
                    ],
                    "reason": "test a later assertion",
                }
            ]
        },
    )
    with pytest.raises(ApiError, match="provenance"):
        DemoRecallHandler()(context, {})


def test_additional_evidence_without_approved_creation_refuses(demo_context):
    context, _ = demo_context
    request_id = queue(context)
    complete(context, request_id, title=DEMO_TEXT)
    existing = WorldQuery(context.driver, context.world_id).read(
        "MATCH (e:Evidence {request_id: $request}) RETURN e",
        request=request_id,
    )[0]["e"]
    extra = {key: value for key, value in existing.items() if key not in {"id", "world_id"}}
    extra["message_key"] = "unapproved-extra"
    plant_node(context.driver, context.world_id, "Evidence", **extra)
    with pytest.raises(ApiError, match="provenance"):
        DemoRecallHandler()(context, {})


@pytest.mark.parametrize("fault", ["receipt", "source", "derived", "snapshot", "foreign"])
def test_incomplete_or_changed_provenance_refuses(demo_context, fault):
    context, account = demo_context
    request_id = queue(context)
    complete(context, request_id, title=DEMO_TEXT)
    bundle = deepcopy(capture(context, account))
    if fault == "receipt":
        bundle["requests"][0]["receipt"] = None
    elif fault == "source":
        bundle["sources"][0]["demo_request_id"] = "not-approved"
    elif fault == "derived":
        bundle["evidence"][0]["derivation"] = "inferred"
    elif fault == "snapshot":
        bundle["items"][0]["summary"] = "a later unchecked field"
    else:
        bundle["evidence"][0]["world_id"] = "another-world"
    with pytest.raises(ApiError, match="provenance"):
        _validate(bundle, context.world_id, clock.now(context.world_id))


def test_scope_and_unsupported_parameters_refuse(demo_context):
    context, _ = demo_context
    handler = DemoRecallHandler()
    with pytest.raises(ApiError) as error:
        handler(replace(context, scopes=frozenset({"grove.write"})), {})
    assert error.value.code == "scope_denied"
    for args in (
        {"cursor": "x"},
        {"time": {"mode": "latest"}},
        {"about": "x" * 257},
        {"limit": True},
        {"limit": MAX_ITEMS + 1},
        {"areas": ["Cedar"]},
    ):
        with pytest.raises(ApiError) as error:
            handler(context, args)
        assert error.value.code == "invalid_arguments"
