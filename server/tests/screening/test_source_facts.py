"""Real in-memory screening retains facts only beside retained text messages."""

import io
import json
from dataclasses import replace

import pytest

from grove.ingest.connector import Upload, chat_locator
from grove.ingest.documents import parse_text
from grove.ingest.whatsapp import parse_export
from grove.screening import Denylist, Message, RenameMap, Screener, ScreeningConfig
from grove.screening.screener import ScreenedMessage
from grove.source_facts import DOCUMENT_TEXT, WHATSAPP_TEXT


def parsed(data):
    raw = data.encode()
    return parse_export(Upload(io.BytesIO(raw), len(raw))).as_chat()


def messages(chat):
    return [
        Message(m.key, m.sender, m.text, m.at, m.speaker_kind, m.modality, m.source_facts)
        for m in chat.messages
    ]


def test_only_retained_rows_keep_facts_and_unknown_speakers_stay_null():
    chat = parsed(
        "13/04/2026, 12:00 - Orla: A café choice.\n"
        "13/04/2026, 09:00 - Cai: hidden orchard.\n"
        "13/04/2026, 10:00 - Orla: Final choice.\n"
    )
    screener = Screener(
        ScreeningConfig(
            denylist=Denylist(["hidden"]), rename=RenameMap.from_mapping({"Orla": "Person A"})
        )
    )
    result = screener.screen_source(
        chat_locator("wa", chat.chat_id), messages(chat), source_identity=WHATSAPP_TEXT
    )
    assert len(result.messages) == 2
    assert [m.key for m in result.messages] == [chat.messages[0].key, chat.messages[2].key]
    assert [m.source_facts.locator.line_start for m in result.messages] == [1, 3]
    assert all(m.sender == "Person A" and m.speaker_kind is None for m in result.messages)
    rows = json.loads(result.content())
    assert all(r["speaker_kind"] is None and r["at"] is None for r in rows)
    assert rows[0]["source_facts"] == chat.messages[0].source_facts.as_row()
    assert chat.messages[1].key not in result.content()


def test_whole_source_and_one_time_code_drops_keep_no_facts():
    chat = parsed("13/04/2026, 10:00 - Orla: Your verification code is 123456.\n")
    screener = Screener(ScreeningConfig(denylist=Denylist(["hidden"])))
    for locator in ("hidden", chat_locator("wa", chat.chat_id)):
        result = screener.screen_source(locator, messages(chat), source_identity=WHATSAPP_TEXT)
        assert result.messages == ()
        assert result.content() == "[]"


def test_sender_text_key_and_external_locator_still_use_real_screening():
    chat = parsed("13/04/2026, 10:00 - Orla: Orla chose the orchard.\n")
    message = replace(messages(chat)[0], key="Orla")
    screener = Screener(ScreeningConfig(rename=RenameMap.from_mapping({"Orla": "Person A"})))
    result = screener.screen_source("Orla", [message], source_identity=WHATSAPP_TEXT)
    assert result.locator == "Person A"
    assert result.messages[0].key == result.messages[0].sender == "Person A"
    assert result.messages[0].text == "Person A chose the orchard."
    assert result.messages[0].source_facts is message.source_facts


def test_legacy_serialized_bytes_remain_unchanged_with_no_facts():
    result = Screener(ScreeningConfig()).screen_source("old", [Message("k", None, "A note.")])
    assert result.content() == '[{"at": null, "key": "k", "sender": null, "text": "A note."}]'
    assert result.messages[0].source_facts is None
    assert ScreenedMessage(
        "k", None, "A note.", speaker_kind="third_party", modality="text"
    ).as_row() == {
        "key": "k",
        "sender": None,
        "text": "A note.",
        "at": None,
        "speaker_kind": "third_party",
        "modality": "text",
    }


def test_invalid_or_mixed_facts_refuse_before_any_screening(monkeypatch):
    chat = parsed("13/04/2026, 10:00 - Orla: A note.\n")
    typed = messages(chat)[0]
    screener = Screener(ScreeningConfig())
    calls = []
    monkeypatch.setattr(screener, "_screen_message", lambda *args: calls.append(1))
    for identity, rows in (
        (None, [typed]),
        (DOCUMENT_TEXT, [typed]),
        (WHATSAPP_TEXT, [typed, Message("legacy", None, "A note.")]),
    ):
        with pytest.raises(ValueError):
            screener.screen_source("x", rows, source_identity=identity)
    assert calls == []
    for cls in (Message, ScreenedMessage):
        with pytest.raises(ValueError):
            cls("x", None, "A note.", modality="text", source_facts=typed.source_facts.as_row())
        with pytest.raises(ValueError):
            cls("x", None, "A note.", source_facts=typed.source_facts)


def test_text_document_path_keeps_byte_facts_and_null_source_time():
    data = "# Café\nA note.\n# Next\nA second note.".encode()
    chat = parse_text(Upload(io.BytesIO(data), len(data)), markdown=True).as_chat()
    result = Screener(ScreeningConfig()).screen_source(
        chat_locator("doc", chat.chat_id),
        messages(chat),
        source_identity=DOCUMENT_TEXT,
    )
    assert [m.source_facts for m in result.messages] == [m.source_facts for m in chat.messages]
    assert all(m.at is None and m.speaker_kind is None for m in result.messages)
