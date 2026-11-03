"""The connector interface and the safe readers, with no store: keys, locators, text cleaning,
cut rules and archive caps."""

import io
import random
import string
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import zstandard

from grove.ingest import (
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    archive,
    chat_locator,
    clean_text,
    message_key,
    one_episode,
    register,
    silence_windows,
    utc_from_epoch,
    utc_from_iso,
)
from grove.screening import Message, Screener, ScreeningConfig
from tests.ingest.conftest import upload_of

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def test_message_keys_are_opaque_fingerprints():
    key = message_key("cc", "session-1", "record-1")
    assert key == message_key("cc", "session-1", "record-1")
    assert key != message_key("cc", "session-1", "record-2")
    assert key != message_key("cx", "session-1", "record-1")
    assert key.startswith("cc-") and len(key) == len("cc-") + 32
    # Parts are joined with a separator, so moving a boundary changes the key.
    assert message_key("cc", "ab", "c") != message_key("cc", "a", "bc")
    # A name in an id never shows in the key.
    assert "Orla" not in message_key("cc", "chat with Orla", "1")


def test_chat_locators_differ_per_chat_and_hold_no_id():
    assert chat_locator("gpt", "conv-1") != chat_locator("gpt", "conv-2")
    assert chat_locator("gpt", "conv-1") == chat_locator("gpt", "conv-1")
    assert "Kestrel" not in chat_locator("gpt", "Kestrel export")
    # A chat locator is no message key of the same id.
    assert chat_locator("gpt", "x") != message_key("gpt", "x")


@pytest.mark.parametrize("prefix", ["", "CC", "c-c", "toolongprefix", "c1"])
def test_a_key_prefix_is_a_few_lowercase_letters(prefix):
    with pytest.raises(ValueError):
        message_key(prefix, "x")


def test_screening_never_changes_a_key_or_a_locator():
    """Keys and locators are compared after screening; a changed one would store a chat twice.
    Ids here are digit-heavy on purpose: a phone, a card or a code shape must not form."""
    rng = random.Random(20261103)
    screener = Screener(ScreeningConfig())
    alphabet = string.digits * 4 + string.ascii_letters + "-_:"
    messages = []
    for index in range(3000):
        parts = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(2)]
        messages.append(Message(key=message_key("cc", *parts, index), sender=None, text="ok"))
    locator = chat_locator("cc", "4111111111111111")
    screened = screener.screen_source(locator, messages)
    assert screened.locator == locator
    assert [m.key for m in screened.messages] == [m.key for m in messages]
    assert dict(screened.counts) == {}


def test_clean_text_makes_text_the_store_can_hold():
    assert clean_text("cut emoji \ud83d here") == "cut emoji � here"
    assert clean_text("a\x00b\r\nc\rd") == "ab\nc\nd"
    assert clean_text(None) == "" and clean_text(42) == ""
    assert clean_text("plain") == "plain"


def test_times_read_with_an_offset_only():
    assert utc_from_iso("2026-10-01T09:00:00.000Z") == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert utc_from_iso("2026-10-01T11:00:00+02:00") == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    # No offset: a guess at the zone would date the message wrongly.
    assert utc_from_iso("2026-10-01T09:00:00") is None
    assert utc_from_iso("not a time") is None and utc_from_iso(None) is None
    assert utc_from_epoch(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert utc_from_epoch(True) is None and utc_from_epoch("1") is None
    assert utc_from_epoch(1e30) is None


def _msg(index: int, minutes: float | None) -> ParsedMessage:
    at = None if minutes is None else T0 + timedelta(minutes=minutes)
    return ParsedMessage(
        key=message_key("zz", index), text=str(index), at=at, speaker_kind="person"
    )


def test_one_episode_per_session_delta():
    messages = [_msg(0, 0), _msg(1, 500)]
    assert one_episode(messages) == [messages]
    assert one_episode([]) == []


def test_silence_windows_close_after_the_gap():
    cut = silence_windows(90)
    messages = [_msg(0, 0), _msg(1, 60), _msg(2, 150), _msg(3, 241), _msg(4, None), _msg(5, 250)]
    windows = cut(messages)
    # 60 -> 150 is exactly 90 minutes: still one window. 150 -> 241 is 91: a new one. A message
    # with no time joins the window it follows.
    assert [[m.text for m in w] for w in windows] == [["0", "1", "2"], ["3", "4", "5"]]


def test_parsed_rows_refuse_what_the_store_cannot_take():
    with pytest.raises(ValueError):
        ParsedMessage(key="has space", text="x", at=None, speaker_kind="person")
    with pytest.raises(ValueError):
        ParsedMessage(key=message_key("zz", 1), text="x", at=None, speaker_kind="robot")
    with pytest.raises(ValueError):
        ParsedMessage(
            key=message_key("zz", 1), text="x", at=datetime(2026, 1, 1), speaker_kind="person"
        )
    with pytest.raises(ValueError):
        ParsedChat(chat_id="c", messages=(_msg(1, 0), _msg(1, 1)))
    assert "zqzq" not in repr(
        ParsedMessage(key=message_key("zz", 1), text="zqzq", at=None, speaker_kind="person")
    )


def test_a_provider_registers_once():
    spec = ConnectorSpec(
        provider="zz_once",
        connector_type="past_ai_conversations",
        parser_version="zz.v1",
        batch_rule="session_delta.v1",
        key_prefix="zz",
        parse=lambda upload: iter(()),
    )
    assert register(spec) is spec
    assert register(spec) is spec
    with pytest.raises(ValueError):
        register(
            ConnectorSpec(
                provider="zz_once",
                connector_type="past_ai_conversations",
                parser_version="zz.v1",
                batch_rule="session_delta.v1",
                key_prefix="zz",
                parse=lambda upload: iter(()),
            )
        )
    with pytest.raises(ValueError):
        ConnectorSpec(
            provider="zz_bad",
            connector_type="no_such_type",
            parser_version="zz.v1",
            batch_rule="session_delta.v1",
            key_prefix="zz",
            parse=lambda upload: iter(()),
        )


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, data in members.items():
            handle.writestr(name, data)
    return buffer.getvalue()


def test_zip_members_reads_matching_files_only():
    data = _zip(
        {
            "export/conversations.json": b"[]",
            "__MACOSX/export/._conversations.json": b"junk",
            "export/._conversations.json": b"junk",
            "export/chat.html": b"<html>",
            "../escape.json": b"{}",
        }
    )
    upload = upload_of(data)
    assert archive.is_zip(upload)
    found = [(m.path, m.data) for m in archive.zip_members(upload, suffixes=(".json",))]
    # The member named ../escape.json is read into memory, never written to a path.
    assert found == [("export/conversations.json", b"[]"), ("../escape.json", b"{}")]


def test_zip_caps_count_the_bytes_actually_read():
    data = _zip({"a.json": b"x" * 5000, "b.json": b"y" * 5000})
    with pytest.raises(NotAnExport):
        list(archive.zip_members(upload_of(data), suffixes=(".json",), max_member_bytes=4000))
    with pytest.raises(NotAnExport):
        list(archive.zip_members(upload_of(data), suffixes=(".json",), max_total_bytes=8000))


def test_a_broken_zip_is_no_export():
    with pytest.raises(NotAnExport):
        list(archive.zip_members(upload_of(b"PK\x03\x04broken"), suffixes=(".json",)))


def test_zstd_reads_frames_and_caps_the_output():
    compressor = zstandard.ZstdCompressor()
    data = compressor.compress(b"line one\n") + compressor.compress(b"line two\n")
    assert archive.is_zstd(data)
    assert archive.zstd_decompress(data) == b"line one\nline two\n"
    big = compressor.compress(b"z" * 100_000)
    with pytest.raises(NotAnExport):
        archive.zstd_decompress(big, cap=1000)
    with pytest.raises(NotAnExport):
        archive.zstd_decompress(archive.ZSTD_MAGIC + b"not a frame")


def test_json_lines_number_every_line_and_skip_a_cut_one():
    data = b'{"a": 1}\n\n{"b": 2}\n{"cut": \n'
    assert list(archive.json_lines(data)) == [(1, {"a": 1}), (3, {"b": 2})]


def test_json_document_refuses_what_is_not_json():
    assert archive.json_document(b"\xef\xbb\xbf[1]") == [1]
    with pytest.raises(NotAnExport):
        archive.json_document(b"\x89PNG")
    with pytest.raises(NotAnExport):
        archive.json_document(b"[" * 100_000)


def test_upload_read_all_caps_the_size():
    upload = upload_of(b"12345")
    assert upload.read_all(5) == b"12345"
    with pytest.raises(NotAnExport):
        upload.read_all(4)
