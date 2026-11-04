"""Identifiers cannot multiply hash work or reach the store above their size bound."""

import hashlib
import io
import json

import pytest

from grove.ingest import NotAnExport, ParsedChat, Upload, chat_locator, message_key, spec_for
from grove.ingest.take_in import take_in
from tests.ingest.conftest import counts, make_screener

CASES = (
    ("claude_code", "session"),
    ("claude_code", "message"),
    ("codex", "wrapped"),
    ("codex", "legacy"),
    ("chatgpt", "conversation"),
    ("chatgpt", "fallback"),
    ("chatgpt", "message"),
    ("chatgpt", "node"),
    ("claude_ai", "conversation"),
    ("claude_ai", "message"),
)
STAMP = "2026-10-01T09:00:00Z"


def export(provider, field, identifier):
    chat = (
        identifier
        if field in {"session", "wrapped", "legacy", "conversation", "fallback"}
        else "chat"
    )
    message_id = identifier if field in {"message", "node"} else "message"
    if provider == "claude_code":
        rows = [
            {
                "type": "user",
                "userType": "external",
                "sessionId": chat,
                "uuid": message_id,
                "timestamp": STAMP,
                "message": {"role": "user", "content": "hello"},
            }
        ]
    elif provider == "codex":
        meta = {"id": chat, "timestamp": STAMP, "source": "cli"}
        message = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
        rows = (
            [meta, message]
            if field == "legacy"
            else [
                {"type": "session_meta", "timestamp": STAMP, "payload": meta},
                {"type": "response_item", "timestamp": STAMP, "payload": message},
            ]
        )
    elif provider == "chatgpt":
        node_id = message_id if field == "node" else "node"
        message = {
            "author": {"role": "user"},
            "create_time": 1790845200,
            "content": {"content_type": "text", "parts": ["hello"]},
        }
        if field != "node":
            message["id"] = message_id
        row = {
            "id": chat,
            "mapping": {node_id: {"message": message, "parent": None}},
            "current_node": node_id,
        }
        if field != "fallback":
            row["conversation_id"] = chat
        return json.dumps([row]).encode()
    else:
        return json.dumps(
            [
                {
                    "uuid": chat,
                    "chat_messages": [
                        {
                            "uuid": message_id,
                            "sender": "human",
                            "text": "hello",
                            "created_at": STAMP,
                        }
                    ],
                }
            ]
        ).encode()
    return b"\n".join(json.dumps(row).encode() for row in rows)


@pytest.mark.parametrize("provider,field", CASES)
def test_oversized_ids_are_refused_before_hash_work_or_store_writes(
    provider, field, driver, seeded_world, test_clock, monkeypatch
):
    identifier = "x" * 1025
    data = export(provider, field, identifier)
    before = counts(driver, seeded_world)
    hashed = []
    original = hashlib.sha256

    def observed(data=b"", *args, **kwargs):
        hashed.append(len(data))
        return original(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", observed)
    with pytest.raises(NotAnExport, match="identifier"):
        take_in(
            driver,
            seeded_world,
            provider,
            Upload(io.BytesIO(data), len(data)),
            screener=make_screener(),
        )
    assert max(hashed, default=0) < len(identifier)
    assert counts(driver, seeded_world) == before


@pytest.mark.parametrize("provider,field", CASES)
def test_identifiers_at_the_limit_preserve_valid_messages(provider, field):
    data = export(provider, field, "x" * 1024)
    (chat,) = spec_for(provider).parse(Upload(io.BytesIO(data), len(data)))
    (message,) = chat.messages
    assert message.text == "hello"
    assert message.speaker_kind == "person"
    assert message.at is not None or field == "legacy"


def test_valid_identifiers_keep_the_original_keys_and_locators():
    assert (
        message_key("gpt", "compat-chat", "compat-message") == "gpt8e214f58bd9538c1bcf1ef684a28d95d"
    )
    assert chat_locator("gpt", "compat-chat") == "gpt0f70abef58383605dc220e5eedf090cd"


@pytest.mark.parametrize("use", ["message", "locator", "chat"])
def test_connector_interface_refuses_oversized_identifiers(use):
    identifier = "x" * 1025
    with pytest.raises(NotAnExport, match="identifier"):
        if use == "message":
            message_key("gpt", "chat", identifier)
        elif use == "locator":
            chat_locator("gpt", identifier)
        else:
            ParsedChat(identifier, ())
