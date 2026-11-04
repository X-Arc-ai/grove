"""Ordinary uploads use real mandatory screening, independent of global film rules.

Every export and secret-shaped value here is synthetic. No provider or model is called.
"""

import asyncio
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from grove.api import uploads
from grove.api.errors import ApiError
from grove.config import ConfigError
from grove.ingest import Upload, spec_for
from grove.ingest.upload_policy import ORDINARY_UPLOAD_POLICY, screener_for_upload
from grove.screening import Message, ScreeningConfig
from grove.store.errors import ScopeError
from tests.screening._plants import CARD_DIGITS, OPENAI_KEY

WORLD_A = "t_upload-policy_00000000-0000-4000-8000-000000000001"
WORLD_B = "t_upload-policy_00000000-0000-4000-8000-000000000002"
AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def export_bytes(provider, texts, *, zipped=False):
    """A real supported export shape, not a test-only parser format."""
    if provider == "claude_ai":
        document = [
            {
                "uuid": "ordinary-garden-chat",
                "chat_messages": [
                    {
                        "uuid": f"garden-message-{index}",
                        "sender": "human",
                        "text": text,
                        "created_at": (AT + timedelta(minutes=index)).isoformat(),
                    }
                    for index, text in enumerate(texts)
                ],
            }
        ]
    else:
        assert provider == "chatgpt"
        mapping = {}
        for index, text in enumerate(texts):
            mapping[f"garden-node-{index}"] = {
                "parent": f"garden-node-{index - 1}" if index else None,
                "children": [f"garden-node-{index + 1}"] if index + 1 < len(texts) else [],
                "message": {
                    "id": f"garden-message-{index}",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [text]},
                    "create_time": (AT + timedelta(minutes=index)).timestamp(),
                },
            }
        document = [
            {
                "id": "ordinary-garden-chat",
                "mapping": mapping,
                "current_node": f"garden-node-{len(texts) - 1}",
            }
        ]
    raw = json.dumps(document).encode()
    if not zipped:
        return raw
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversations.json", raw)
    return buffer.getvalue()


def secret_texts():
    return [
        f"Use api_key={OPENAI_KEY}",
        f"Card: {CARD_DIGITS}",
        "Your verification code is 482910",
        "Reset your password at https://app.example/r/synthetic-link",
        "Ask garden@example.invalid or +44 7700 900123 or @garden_helper",
        "The herb garden needs a cedar planter.",
    ]


@pytest.mark.parametrize("environment", ["unset", "missing", "unrelated"])
def test_global_film_configuration_never_selects_an_account_policy(
    monkeypatch, tmp_path, environment
):
    monkeypatch.setenv("GROVE_ENV", "production")
    for name in ("GROVE_RENAME_MAP", "GROVE_DENYLIST"):
        monkeypatch.delenv(name, raising=False)
    if environment != "unset":
        rename, deny = tmp_path / "rename.json", tmp_path / "deny.txt"
        monkeypatch.setenv("GROVE_RENAME_MAP", str(rename))
        monkeypatch.setenv("GROVE_DENYLIST", str(deny))
        if environment == "unrelated":
            rename.write_text('{"garden": "different account"}')
            deny.write_text("cedar planter")
            rename.chmod(0o600)
            deny.chmod(0o600)

    def not_a_global_consumer(*_args, **_kwargs):
        raise AssertionError("an account upload must not read global film rules")

    monkeypatch.setattr(ScreeningConfig, "from_environment", not_a_global_consumer)
    result = screener_for_upload(WORLD_A).screen_text("A garden needs a cedar planter.")
    assert ORDINARY_UPLOAD_POLICY == "ordinary_upload_v1"
    assert result.messages[0].text == "A garden needs a cedar planter."
    assert not result.counts


@pytest.mark.parametrize("world", [None, "", "other-account", "a" * 10000, WORLD_A.upper()])
def test_invalid_internal_world_is_refused(world):
    with pytest.raises(ScopeError):
        screener_for_upload(world)


def test_concurrent_worlds_have_independent_policies_and_no_owner_exemptions():
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(screener_for_upload, (WORLD_A, WORLD_B)))
    assert first is not second
    assert first._config is not second._config
    assert first._config.rename is not second._config.rename
    assert first._config.denylist is not second._config.denylist
    for screen in (first, second):
        assert not screen._config.own_identifiers
        assert not screen._config.sensitive_classes
        result = screen.screen_text("My address is owner@example.invalid")
        assert result.messages[0].text == "My address is [email 1]"
        assert dict(result.counts) == {"contact_email": 1}


@pytest.mark.parametrize("provider", ["claude_ai", "chatgpt"])
@pytest.mark.parametrize("zipped", [False, True], ids=["json", "zip"])
def test_real_export_parsers_retain_only_mandatory_screened_text(provider, zipped):
    raw = export_bytes(provider, secret_texts(), zipped=zipped)
    chats = list(spec_for(provider).parse(Upload(io.BytesIO(raw), len(raw))))
    assert len(chats) == 1
    result = screener_for_upload(WORLD_A).screen_source(
        "ordinary-chat",
        [
            Message(message.key, message.sender, message.text, message.at)
            for message in chats[0].messages
        ],
    )
    assert len(result.messages) == 4
    assert dict(result.counts) == {
        "credential": 1,
        "payment_card": 1,
        "one_time_code": 1,
        "reset_link": 1,
        "contact_email": 1,
        "contact_phone": 1,
        "contact_handle": 1,
    }
    content = result.content()
    for value in (OPENAI_KEY, CARD_DIGITS, "482910", "synthetic-link", "garden@example.invalid"):
        assert value not in content
    assert "[credential]" in content
    assert "[card]" in content
    assert "[email 1]" in content and "[phone 1]" in content and "[handle 1]" in content
    assert "cedar planter" in content


@pytest.mark.parametrize("fault", ["config", "type", "invalid-result"])
def test_policy_fault_refuses_before_body_even_for_the_old_lumen_exception(monkeypatch, fault):
    received = []

    async def identity(_request):
        return WORLD_A

    async def receive():
        received.append(True)
        raise AssertionError("policy faults must not read any upload bytes")

    def broken(_world):
        if fault == "invalid-result":
            return object()
        error = ConfigError if fault == "config" else TypeError
        raise error("synthetic private policy detail")

    monkeypatch.setattr(uploads, "_upload_world", identity)
    monkeypatch.setattr(uploads, "is_demo_world", lambda *_: False)
    monkeypatch.setattr(uploads, "screener_for_upload", broken)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"provider=claude_ai",
            "app": SimpleNamespace(state=SimpleNamespace(driver=object())),
        },
        receive=receive,
    )
    with pytest.raises(ApiError) as refused:
        asyncio.run(uploads.post_upload(request))
    assert refused.value.status == 503
    assert refused.value.message == "screening is not configured"
    assert not received
