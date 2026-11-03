"""The claude.ai export parser: the current branch, the speaker rules (D5), the planted controls,
stable keys, format errors and the store end to end.

Every export here is built in code from invented chats (R3). Marker strings stand for text that
must never reach a message: thinking, tool results, extracted file contents.
"""

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove.ingest import NotAnExport, archive, chat_locator, message_key, spec_for
from grove.ingest.parsers import claude_ai
from grove.ingest.take_in import take_in
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from tests.ingest.conftest import STORE_TIME, counts, make_screener, upload_of

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ROOT = "00000000-0000-4000-8000-000000000000"

THINKING = "MARK-THINKING-5B3"
SUMMARY = "MARK-THINKING-SUMMARY-8C1"
RESULT = "MARK-TOOL-RESULT-2D7"
EXTRACTED = "MARK-EXTRACTED-6E4"
OFF_BRANCH = "MARK-OFF-BRANCH-9F2"
PREVIEW = "MARK-PREVIEW-BYTES-4G6"
PROJECT = "MARK-PROJECT-DOC-1H9"
USERS = "MARK-USERS-FILE-7J5"
MARKERS = (THINKING, SUMMARY, RESULT, EXTRACTED, OFF_BRANCH, PREVIEW, PROJECT, USERS)

INVENTED_NAME = "Quillon Marsh"
BEES = "5d2c1a4e-0000-4000-8000-00000000a001"
TEA = "5d2c1a4e-0000-4000-8000-00000000b002"


def moment(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(seconds: float) -> str:
    return moment(seconds).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def text_block(text: str, seconds: float) -> dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "start_timestamp": iso(seconds),
        "stop_timestamp": iso(seconds),
        "citations": [],
    }


def record(
    uuid: str,
    sender: str,
    *,
    seconds: float,
    content: list[dict[str, Any]] | None = None,
    text: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
    parent: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "uuid": uuid,
        "text": text if text is not None else "",
        "sender": sender,
        "created_at": iso(seconds),
        "updated_at": iso(seconds),
        "attachments": attachments or [],
        "files": files or [],
    }
    if content is not None:
        row["content"] = content
    if parent is not None:
        row["parent_message_uuid"] = parent
    return row


def conversation(uuid: str, records: list[dict[str, Any]], *, name: str = "Untitled") -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": iso(0),
        "updated_at": iso(3600),
        "account": {"uuid": "acct-0001"},
        "chat_messages": records,
    }


def bees_records() -> list[dict[str, Any]]:
    """A realistic chat: attachments, thinking, a search, a failed fetch, a retried answer off
    the current branch, and two records in the older flat-text form."""
    return [
        record(
            "b-01",
            "human",
            seconds=10,
            text="Help me plan a reading list on bees.",
            content=[text_block("Help me plan a reading list on bees.", 10)],
            attachments=[
                {
                    "file_name": "bee-notes.txt",
                    "file_size": 120,
                    "file_type": "txt",
                    "extracted_content": f"My notes. {EXTRACTED}",
                }
            ],
            files=[
                {
                    "file_name": "hive.png",
                    "file_uuid": "f-0001",
                    "preview_url": f"data:image/png;base64,{PREVIEW}",
                }
            ],
            parent=ROOT,
        ),
        record(
            "b-02",
            "assistant",
            seconds=20,
            content=[
                {
                    "type": "thinking",
                    "thinking": THINKING,
                    "summaries": [{"summary": SUMMARY}],
                    "cut_off": False,
                },
                text_block("Let me search for recent titles.", 20),
                {
                    "type": "tool_use",
                    "name": "web_search",
                    "input": {"query": "best books about bees 2026"},
                },
                {
                    "type": "tool_result",
                    "name": "web_search",
                    "content": [{"type": "knowledge", "title": "Bees", "text": RESULT}],
                    "is_error": False,
                },
                text_block("Here are three titles.", 21),
            ],
            parent="b-01",
        ),
        # A retried answer to the first question: the current branch does not hold it.
        record(
            "b-02r",
            "assistant",
            seconds=25,
            content=[text_block(f"Another list. {OFF_BRANCH}", 25)],
            parent="b-01",
        ),
        record(
            "b-03",
            "human",
            seconds=30,
            content=[text_block("Can you fetch the second link?", 30)],
            parent="b-02",
        ),
        record(
            "b-04",
            "assistant",
            seconds=40,
            content=[
                {
                    "type": "tool_use",
                    "name": "web_fetch",
                    "input": {"url": "https://example.org/bees"},
                },
                {
                    "type": "tool_result",
                    "name": "web_fetch",
                    "content": [{"type": "text", "text": "Fetch failed: the page answered 403."}],
                    "is_error": True,
                },
                {
                    "type": "tool_use",
                    "name": "artifacts",
                    "input": {"id": "plan", "type": "text/markdown", "title": "Bee list"},
                },
                text_block("I could not open that page.", 41),
            ],
            parent="b-03",
        ),
        record("b-05", "human", seconds=50, text="Thanks, that helps.", parent="b-04"),
        record("b-06", "assistant", seconds=60, text="You are welcome.", parent="b-05"),
    ]


# (speaker_kind, text, seconds) of the bees chat's current branch.
BEES_EXPECTED = [
    (
        "person",
        "Help me plan a reading list on bees.\n\n[attached file: bee-notes.txt]"
        "\n\n[attached file: hive.png]",
        10,
    ),
    (
        "ai_host",
        "Let me search for recent titles.\n\n[tool web_search] best books about bees 2026"
        "\n\nHere are three titles.",
        20,
    ),
    ("person", "Can you fetch the second link?", 30),
    (
        "ai_host",
        "[tool web_fetch] https://example.org/bees\n\n"
        '[tool artifacts] {"id":"plan","type":"text/markdown","title":"Bee list"}\n\n'
        "I could not open that page.",
        40,
    ),
    ("automated", "[tool error] Fetch failed: the page answered 403.", 40),
    ("person", "Thanks, that helps.", 50),
    ("ai_host", "You are welcome.", 60),
]


def tea_records() -> list[dict[str, Any]]:
    """An older export: no parent links, so the list order is the thread."""
    return [
        record("t-01", "human", seconds=200, text="How long should green tea steep?"),
        record("t-02", "assistant", seconds=210, text="About two minutes."),
    ]


def as_json(*rows: dict[str, Any]) -> bytes:
    return json.dumps(list(rows)).encode()


def as_zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def export_zip(conversations: bytes) -> bytes:
    """The export as the site sends it: conversations.json among files that are never read."""
    return as_zip(
        [
            ("users.json", json.dumps([{"uuid": "u-1", "full_name": USERS}]).encode()),
            ("projects.json", json.dumps([{"uuid": "p-1", "docs": [PROJECT]}]).encode()),
            ("conversations.json", conversations),
        ]
    )


def standard() -> bytes:
    return as_json(conversation(BEES, bees_records()), conversation(TEA, tea_records()))


def parse(data: bytes) -> list:
    return list(claude_ai.parse(upload_of(data)))


def summary(chat) -> list[tuple[str, str, float]]:
    return [(m.speaker_kind, m.text, (m.at - T0).total_seconds()) for m in chat.messages]


def all_texts(chats) -> list[str]:
    return [m.text for chat in chats for m in chat.messages]


# 1. A realistic export.


def test_spec_is_registered():
    spec = spec_for("claude_ai")
    assert spec is claude_ai.SPEC
    assert spec.connector_type == "past_ai_conversations"
    assert spec.parser_version == "claude_ai.v1"
    assert spec.batch_rule == "session_delta.v1"
    assert spec.key_prefix == "cai"


def test_a_realistic_export_reads_the_current_branch_in_order():
    chats = parse(standard())

    assert [chat.chat_id for chat in chats] == [BEES, TEA]
    bees, tea = chats
    assert summary(bees) == [(kind, text, float(sec)) for kind, text, sec in BEES_EXPECTED]
    assert summary(tea) == [
        ("person", "How long should green tea steep?", 200.0),
        ("ai_host", "About two minutes.", 210.0),
    ]
    assert bees.started_at == moment(0)
    assert bees.ended_at == moment(3600)
    for chat in chats:
        for parsed in chat.messages:
            assert parsed.sender is None
            assert parsed.modality == "text"
            assert parsed.at != STORE_TIME


def test_the_zip_reads_only_conversations_json():
    bare = parse(standard())
    zipped = parse(export_zip(standard()))
    assert [summary(chat) for chat in zipped] == [summary(chat) for chat in bare]
    for text in all_texts(zipped):
        assert USERS not in text and PROJECT not in text


def test_a_member_that_only_ends_like_conversations_json_is_not_read():
    decoy = conversation(
        "5d2c1a4e-0000-4000-8000-00000000c003",
        [record("x-01", "human", seconds=1, text=f"An old copy. {PROJECT}")],
    )
    data = as_zip(
        [
            ("old_conversations.json", as_json(decoy)),
            ("conversations.json", as_json(conversation(TEA, tea_records()))),
        ]
    )
    assert [chat.chat_id for chat in parse(data)] == [TEA]


def test_a_parent_outside_the_list_ends_the_chain():
    records = bees_records()
    # The question the chain starts from points at a record the export does not hold.
    records[3]["parent_message_uuid"] = "b-gone"
    (chat,) = parse(as_json(conversation(BEES, records)))
    assert [m.text for m in chat.messages][0] == "Can you fetch the second link?"


def test_a_parent_cycle_ends_the_walk():
    records = bees_records()
    records[0]["parent_message_uuid"] = "b-06"
    (chat,) = parse(as_json(conversation(BEES, records)))
    assert summary(chat) == [(kind, text, float(sec)) for kind, text, sec in BEES_EXPECTED]


def test_the_flat_text_stands_in_only_when_there_is_no_content_list():
    records = [
        record(
            "f-01",
            "assistant",
            seconds=5,
            text=f"Flat copy. {THINKING}",
            content=[{"type": "thinking", "thinking": THINKING}],
        ),
        record("f-02", "assistant", seconds=6, text="Flat only.", content=[]),
    ]
    (chat,) = parse(as_json(conversation(TEA, records)))
    assert [m.text for m in chat.messages] == ["Flat only."]


# 2. Planted controls (D5, D14).


def test_planted_text_never_reaches_a_message():
    texts = all_texts(parse(export_zip(standard())))
    assert texts
    for marker in MARKERS:
        assert all(marker not in text for text in texts), marker


def test_the_assistant_is_never_the_person_and_a_failed_tool_is_automated():
    (bees, _tea) = parse(standard())
    for parsed in bees.messages:
        if parsed.text.startswith("[tool error]"):
            assert parsed.speaker_kind == "automated"
        if "[tool " in parsed.text and "[tool error]" not in parsed.text:
            assert parsed.speaker_kind == "ai_host"
    assert [m.speaker_kind for m in bees.messages if "403" in m.text] == ["automated"]
    assert all(
        m.speaker_kind != "person" for m in bees.messages if m.text.startswith(("[tool", "Here"))
    )


def test_a_chat_of_assistant_records_only_has_no_person_message():
    records = [
        record(
            "a-01",
            "assistant",
            seconds=5,
            content=[
                {"type": "tool_use", "name": "web_search", "input": {"query": "pollen"}},
                {
                    "type": "tool_result",
                    "name": "web_search",
                    "content": "the search service is down",
                    "is_error": True,
                },
            ],
        ),
        record("a-02", "system", seconds=6, text="A note the program added."),
    ]
    (chat,) = parse(as_json(conversation(TEA, records)))
    assert [(m.speaker_kind, m.text) for m in chat.messages] == [
        ("ai_host", "[tool web_search] pollen"),
        ("automated", "[tool error] the search service is down"),
    ]


def test_an_attachment_is_only_the_fact():
    (bees, _tea) = parse(standard())
    first = bees.messages[0]
    assert "[attached file: bee-notes.txt]" in first.text
    assert "[attached file: hive.png]" in first.text
    for text in all_texts([bees]):
        assert EXTRACTED not in text and PREVIEW not in text and "My notes" not in text


def test_two_failed_results_of_one_record_are_one_automated_message():
    records = [
        record(
            "e-01",
            "assistant",
            seconds=5,
            content=[
                {"type": "tool_result", "name": "web_fetch", "content": [], "is_error": True},
                {
                    "type": "tool_result",
                    "name": "web_fetch",
                    "content": "Error " + "z" * 400,
                    "is_error": True,
                },
                {"type": "tool_result", "name": "web_fetch", "content": "ok", "is_error": "yes"},
            ],
        )
    ]
    (chat,) = parse(as_json(conversation(TEA, records)))
    (only,) = chat.messages
    assert only.speaker_kind == "automated"
    first, second = only.text.split("\n")
    assert first == "[tool error] web_fetch"
    assert second.startswith("[tool error] Error zzz")
    assert len(second) == len("[tool error] ") + 200


def test_a_long_tool_input_folds_to_one_short_line():
    records = [
        record(
            "l-01",
            "assistant",
            seconds=5,
            content=[
                {
                    "type": "tool_use",
                    "name": "repl",
                    "input": {"code": "\n".join(f"line_{n} = {n}" for n in range(100))},
                }
            ],
        )
    ]
    (chat,) = parse(as_json(conversation(TEA, records)))
    (only,) = chat.messages
    assert only.text.startswith('[tool repl] {"code":"line_0 = 0\\nline_1 = 1')
    assert "\n" not in only.text
    assert len(only.text) == len("[tool repl] ") + 120


def test_text_is_cleaned_and_a_blank_message_is_skipped():
    # JSON can spell a lone surrogate; the store refuses one, so it must arrive as U+FFFD.
    records = [
        record("w-01", "human", seconds=1, content=[text_block("  \n\t", 1)]),
        record("w-02", "human", seconds=2, text="Line one\r\nLine two\x00 and \ud800 here"),
        record("w-03", "assistant", seconds=3, content=[text_block("Fine.\r\n", 3)]),
    ]
    (chat,) = parse(as_json(conversation(TEA, records)))
    assert [m.text for m in chat.messages] == ["Line one\nLine two and � here", "Fine."]


# 3. Keys.


def test_keys_are_stable_unique_prefixed_and_survive_screening():
    data = export_zip(standard())
    first, second = parse(data), parse(data)
    assert [[m.key for m in c.messages] for c in first] == [
        [m.key for m in c.messages] for c in second
    ]
    screener = Screener(ScreeningConfig())
    for chat in first:
        keys = [m.key for m in chat.messages]
        assert len(set(keys)) == len(keys)
        assert all(re.fullmatch(r"cai[0-9a-f]{32}", key) for key in keys)
        locator = chat_locator("cai", chat.chat_id)
        screened = screener.screen_source(
            locator,
            [
                Message(
                    key=m.key,
                    sender=m.sender,
                    text=m.text,
                    at=m.at,
                    speaker_kind=m.speaker_kind,
                    modality=m.modality,
                )
                for m in chat.messages
            ],
        )
        assert screened.locator == locator
        assert [m.key for m in screened.messages] == keys


def test_the_key_names_the_record_and_the_speaker_kind():
    (bees, _tea) = parse(standard())
    fetch = [m for m in bees.messages if m.at == moment(40)]
    assert [m.key for m in fetch] == [
        message_key("cai", BEES, "b-04", "ai_host"),
        message_key("cai", BEES, "b-04", "automated"),
    ]


# 4. A grown export.


def grown_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    before = bees_records()
    after = bees_records() + [
        record("b-07", "human", seconds=70, text="Add one about wasps.", parent="b-06"),
        record("b-08", "assistant", seconds=80, text="Try a field guide.", parent="b-07"),
    ]
    return conversation(BEES, before), conversation(BEES, after)


def test_a_grown_export_keeps_the_old_keys_and_adds_new_ones():
    before, after = grown_pair()
    (old,) = parse(as_json(before))
    (new,) = parse(as_json(after))
    old_keys = [m.key for m in old.messages]
    assert [m.key for m in new.messages][: len(old_keys)] == old_keys
    assert [m.text for m in new.messages[len(old_keys) :]] == [
        "Add one about wasps.",
        "Try a field guide.",
    ]


def test_one_chat_in_two_files_is_merged_without_a_key_twice():
    before, after = grown_pair()
    data = as_zip(
        [
            ("conversations.json", as_json(before, conversation(TEA, tea_records()))),
            ("more/conversations.json", as_json(after)),
        ]
    )
    chats = parse(data)
    assert [chat.chat_id for chat in chats] == [BEES, TEA]
    (expected,) = parse(as_json(after))
    assert [m.key for m in chats[0].messages] == [m.key for m in expected.messages]


# 5. Format errors.


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(bytes(range(1, 256)) * 3, id="random-bytes"),
        pytest.param(b"", id="empty-file"),
        pytest.param(as_zip([]), id="empty-zip"),
        pytest.param(as_zip([("users.json", b"[]")]), id="zip-without-conversations"),
        pytest.param(
            json.dumps(
                [{"id": "c-1", "conversation_id": "c-1", "mapping": {}, "current_node": None}]
            ).encode(),
            id="chatgpt-export",
        ),
        pytest.param(b'{"chat_messages": []}', id="json-object"),
        pytest.param(b'["one", "two"]', id="json-of-strings"),
        pytest.param(b'{"type":"user","sessionId":"s-1"}\n', id="json-lines"),
        pytest.param(b"PK\x03\x04 not really a zip", id="broken-zip"),
    ],
)
def test_a_file_of_another_kind_is_not_an_export(data):
    with pytest.raises(NotAnExport):
        parse(data)


def test_a_cut_document_is_not_an_export_and_the_error_holds_no_value():
    data = standard()
    with pytest.raises(NotAnExport) as caught:
        parse(data[: len(data) // 2])
    assert "bees" not in str(caught.value).lower()
    assert caught.value.__cause__ is None


def test_an_empty_export_yields_no_chat():
    assert parse(b"[]") == []
    assert parse(export_zip(b"[]")) == []


# 6. End to end through the store.


def stored_rows(driver, world_id: str) -> list[dict[str, Any]]:
    rows = WorldQuery(driver, world_id).read("MATCH (s:Source) RETURN s.content AS content")
    return [message for row in rows for message in json.loads(row["content"])]


def test_take_in_stores_each_chat_once_and_a_reimport_changes_nothing(
    driver, seeded_world, test_clock
):
    before, after = grown_pair()
    first_upload = export_zip(as_json(before, conversation(TEA, tea_records())))

    result = take_in(
        driver, seeded_world, "claude_ai", upload_of(first_upload), screener=make_screener()
    )
    assert result.state == "taken_in"
    assert (result.chats, result.sources_added, result.episodes_added) == (2, 2, 2)
    first = counts(driver, seeded_world)
    assert first == {"connectors": 1, "sources": 2, "episodes": 2, "receipts": 2}

    rows = stored_rows(driver, seeded_world)
    assert len(rows) == len(BEES_EXPECTED) + 2
    for row in rows:
        assert row["speaker_kind"] in {"person", "ai_host", "automated"}
        assert row["modality"] == "text"
        assert re.fullmatch(r"cai[0-9a-f]{32}", row["key"])
    assert {row["speaker_kind"] for row in rows} == {"person", "ai_host", "automated"}
    assert all(marker not in row["text"] for row in rows for marker in MARKERS)

    episodes = WorldQuery(driver, seeded_world).read(
        "MATCH (e:Episode) RETURN e.start_time AS start, e.end_time AS end ORDER BY start"
    )
    assert [(row["start"], row["end"]) for row in episodes] == [
        (moment(10), moment(60)),
        (moment(200), moment(210)),
    ]

    again = take_in(
        driver, seeded_world, "claude_ai", upload_of(first_upload), screener=make_screener()
    )
    assert again.state == "nothing_new"
    assert (again.sources_added, again.episodes_added) == (0, 0)
    assert counts(driver, seeded_world) == first

    grown = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(export_zip(as_json(after, conversation(TEA, tea_records())))),
        screener=make_screener(),
    )
    assert grown.state == "taken_in"
    assert (grown.sources_added, grown.episodes_added, grown.messages_read) == (1, 1, 2)
    assert counts(driver, seeded_world) == {
        "connectors": 1,
        "sources": 3,
        "episodes": 3,
        "receipts": 3,
    }
    texts = [row["text"] for row in stored_rows(driver, seeded_world)]
    assert texts.count("Try a field guide.") == 1
    assert texts.count("Thanks, that helps.") == 1


def named_chat() -> dict[str, Any]:
    return conversation(
        TEA,
        [
            record("n-01", "human", seconds=10, text=f"Ask {INVENTED_NAME} which tea to buy."),
            record("n-02", "assistant", seconds=20, text="A sencha is a safe start."),
        ],
    )


def test_take_in_drops_a_denylisted_name(driver, seeded_world, test_clock):
    result = take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(as_json(named_chat())),
        screener=make_screener(denylist=[INVENTED_NAME]),
    )
    assert result.state == "taken_in"
    assert [row["text"] for row in stored_rows(driver, seeded_world)] == [
        "A sencha is a safe start."
    ]
    content = WorldQuery(driver, seeded_world).read("MATCH (s:Source) RETURN s.content AS content")
    assert all("quillon" not in row["content"].lower() for row in content)


def test_take_in_renames_a_name(driver, seeded_world, test_clock):
    take_in(
        driver,
        seeded_world,
        "claude_ai",
        upload_of(as_json(named_chat())),
        screener=make_screener(rename={INVENTED_NAME: "Friend A"}),
    )
    rows = stored_rows(driver, seeded_world)
    assert rows[0]["text"] == "Ask Friend A which tea to buy."
    assert rows[0]["speaker_kind"] == "person"
    assert all("quillon" not in row["text"].lower() for row in rows)


def test_a_document_past_the_value_cap_is_refused_before_it_is_parsed(monkeypatch):
    data = as_json(named_chat())
    monkeypatch.setattr(archive, "MAX_DOCUMENT_VALUES", archive.value_bound(data) - 1)
    with pytest.raises(NotAnExport):
        parse(data)
    monkeypatch.setattr(archive, "MAX_DOCUMENT_VALUES", archive.value_bound(data))
    assert parse(data)


def test_take_in_stores_when_the_rename_map_holds_the_key_prefix(driver, seeded_world, test_clock):
    """ "Cai" as a surname in the rename map once changed every key ("cai-...") and locator, and
    every chat failed closed. The key is one word now."""
    screener = make_screener(rename={"Cai": "Person C"})
    data = as_json(named_chat())

    result = take_in(driver, seeded_world, "claude_ai", upload_of(data), screener=screener)

    assert result.state == "taken_in"
    assert result.failed_chats == 0 and result.sources_added == 1
    rows = stored_rows(driver, seeded_world)
    assert [row["speaker_kind"] for row in rows] == ["person", "ai_host"]
    again = take_in(driver, seeded_world, "claude_ai", upload_of(data), screener=screener)
    assert again.state == "nothing_new"
