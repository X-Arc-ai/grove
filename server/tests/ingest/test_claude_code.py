"""The Claude Code parser (A.4, D5, D14): speakers, the drop rules, keys and the store path.

Every session here is built in code from invented ids, invented people and invented text (R3).
Marker strings stand for text that must never reach a message: each test plants them and then
proves they are absent from every message text.
"""

import io
import json
import random
import re
import signal
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from grove.ingest import NotAnExport, ParsedChat, ReadBudget, Upload, chat_locator, message_key
from grove.ingest.parsers import claude_code
from grove.ingest.take_in import NOTHING_NEW, TAKEN_IN, take_in
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from tests.ingest.conftest import counts, make_screener, upload_of

SESSION = "5f0c2a9e-1d3b-4c7a-9e21-7a4d0b6c1e01"
OTHER_SESSION = "5f0c2a9e-1d3b-4c7a-9e21-7a4d0b6c1e02"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

THINKING = "zqx-thinking-marker-81"
REDACTED = "zqx-redacted-marker-82"
TOOL_OUTPUT = "zqx-tool-output-marker-83"
META = "zqx-meta-marker-84"
COMPACT = "zqx-compact-marker-85"
REMINDER = "zqx-reminder-marker-86"
STDOUT = "zqx-stdout-marker-87"
STDERR = "zqx-stderr-marker-88"
CAVEAT = "zqx-caveat-marker-89"
BASH_OUT = "zqx-bash-out-marker-90"
BASH_ERR = "zqx-bash-err-marker-91"
COMMAND_MESSAGE = "zqx-command-message-marker-92"
SYSTEM = "zqx-system-marker-93"
SUMMARY = "zqx-summary-marker-94"
INTERNAL = "zqx-internal-marker-95"
SNAPSHOT = "zqx-snapshot-marker-96"
QUEUE = "zqx-queue-marker-97"
BASE64 = "WlFYLWJhc2U2NC1tYXJrZXItOTg"
DOC_TEXT = "zqx-document-text-marker-99"
SIGNATURE = "zqx-signature-marker-100"
DROPPED_MARKERS = (
    THINKING,
    REDACTED,
    TOOL_OUTPUT,
    META,
    COMPACT,
    REMINDER,
    STDOUT,
    STDERR,
    CAVEAT,
    BASH_OUT,
    BASH_ERR,
    COMMAND_MESSAGE,
    SYSTEM,
    SUMMARY,
    INTERNAL,
    SNAPSHOT,
    QUEUE,
    BASE64,
    DOC_TEXT,
    SIGNATURE,
)


def _at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def _ts(minute: int) -> str:
    return _at(minute).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _uuid(number: int, session: str = SESSION) -> str:
    return f"{session[:24]}{number:012d}"


def _record(kind: str, number: int, minute: int, content, *, session=SESSION, **extra) -> dict:
    record = {
        "type": kind,
        "uuid": _uuid(number, session),
        "parentUuid": _uuid(number - 1, session) if number > 1 else None,
        "sessionId": session,
        "timestamp": _ts(minute),
        "userType": "external",
        "entrypoint": "cli",
        "isSidechain": False,
        "cwd": "/work/garden-app",
        "version": "2.0.31",
        "message": {"role": kind, "content": content},
    }
    if kind == "assistant":
        record["message"]["model"] = "model-x"
    record.update(extra)
    return record


def _user(number: int, minute: int, content, **extra) -> dict:
    return _record("user", number, minute, content, **extra)


def _assistant(number: int, minute: int, blocks: list, **extra) -> dict:
    return _record("assistant", number, minute, blocks, **extra)


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(name: str, payload: dict, call: str = "toolu_01") -> dict:
    return {"type": "tool_use", "id": call, "name": name, "input": payload}


def _tool_result(content, *, is_error: bool | None = None, call: str = "toolu_01") -> dict:
    block = {"type": "tool_result", "tool_use_id": call, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return block


def _jsonl(records: list[dict]) -> bytes:
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def _parse(data: bytes) -> list[ParsedChat]:
    return list(claude_code.parse(upload_of(data)))


def _only(data: bytes) -> ParsedChat:
    chats = _parse(data)
    assert len(chats) == 1
    return chats[0]


def _rows(chat: ParsedChat) -> list[tuple[str, str]]:
    return [(message.speaker_kind, message.text) for message in chat.messages]


def _session_records() -> list[dict]:
    """A small realistic session: work, tools, attachments and every kind of injected text."""
    return [
        _user(1, 0, "Please add a watering schedule to the garden app."),
        _assistant(
            2,
            1,
            [
                {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
                _text("I will look at the schedule module first."),
            ],
        ),
        _assistant(3, 2, [_tool_use("Bash", {"command": "ls src", "description": "List files"})]),
        _user(
            4,
            3,
            [_tool_result(TOOL_OUTPUT, is_error=False)],
            # The record level copy of the output that real session files carry.
            toolUseResult={"stdout": TOOL_OUTPUT, "stderr": "", "interrupted": False},
        ),
        _assistant(
            5,
            4,
            [
                {"type": "redacted_thinking", "data": REDACTED},
                _tool_use("Read", {"file_path": "src/schedule.py"}, "toolu_02"),
            ],
        ),
        _user(
            6,
            5,
            [
                _tool_result(
                    [_text("<tool_use_error>File does not exist.</tool_use_error>")],
                    is_error=True,
                    call="toolu_02",
                )
            ],
        ),
        _user(
            7,
            6,
            [
                _text("Here is the sketch of the bed layout."),
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": BASE64},
                },
                {
                    "type": "document",
                    "source": {"type": "text", "media_type": "text/plain", "data": DOC_TEXT},
                },
            ],
        ),
        _user(8, 7, [_text(f"Base directory for this skill. {META}")], isMeta=True),
        _user(
            9,
            8,
            f"<command-message>{COMMAND_MESSAGE}</command-message>\n"
            "<command-name>/tidy</command-name>\n<command-args>src tests</command-args>",
        ),
        _user(10, 9, f"<local-command-stdout>{STDOUT}</local-command-stdout>"),
        _user(11, 10, f"<local-command-stderr>{STDERR}</local-command-stderr>"),
        _user(12, 11, "[Request interrupted by user]"),
        _user(13, 12, f"Use the rain sensor too.<system-reminder>{REMINDER}</system-reminder>"),
        _user(
            14,
            13,
            f"<bash-input>make test</bash-input><bash-stdout>{BASH_OUT}</bash-stdout>"
            f"<bash-stderr>{BASH_ERR}</bash-stderr>",
        ),
        {
            "type": "system",
            "uuid": _uuid(15),
            "sessionId": SESSION,
            "timestamp": _ts(14),
            "content": SYSTEM,
            "level": "info",
        },
        {"type": "summary", "summary": SUMMARY, "leafUuid": _uuid(14)},
        {
            "type": "file-history-snapshot",
            "messageId": _uuid(16),
            "snapshot": {"trackedFileBackups": {"src/schedule.py": SNAPSHOT}},
        },
        {"type": "queue-operation", "sessionId": SESSION, "timestamp": _ts(15), "content": QUEUE},
        _user(17, 16, COMPACT, isCompactSummary=True),
        _user(18, 17, INTERNAL, userType="internal"),
        _user(19, 18, [_text(f"<local-command-caveat>{CAVEAT}</local-command-caveat>")]),
        _assistant(
            20,
            19,
            [
                _text("The schedule now waters at dawn."),
                _tool_use("Edit", {"file_path": "src/schedule.py", "old_string": "a"}, "toolu_03"),
            ],
        ),
    ]


EXPECTED = [
    ("person", "Please add a watering schedule to the garden app.", 0, 1),
    ("ai_host", "I will look at the schedule module first.", 1, 2),
    ("ai_host", "[tool Bash] List files", 2, 3),
    ("ai_host", "[tool Read] src/schedule.py", 4, 5),
    ("automated", "[tool error] File does not exist.", 5, 6),
    (
        "person",
        "Here is the sketch of the bed layout.\n\n[attached an image]\n\n[attached a document]",
        6,
        7,
    ),
    ("person", "/tidy src tests", 8, 9),
    ("person", "Use the rain sensor too.", 12, 13),
    ("person", "! make test", 13, 14),
    ("ai_host", "The schedule now waters at dawn.\n\n[tool Edit] src/schedule.py", 19, 20),
]


# 1. A realistic export


def test_parse_reads_messages_in_order_with_their_own_times(test_clock):
    chat = _only(_jsonl(_session_records()))

    assert chat.chat_id == SESSION
    assert [(m.speaker_kind, m.text, m.at) for m in chat.messages] == [
        (speaker, text, _at(minute)) for speaker, text, minute, _ in EXPECTED
    ]
    assert {m.modality for m in chat.messages} == {"text"}
    assert {m.sender for m in chat.messages} == {None}
    # The message's own time, never the time of the upload.
    assert all(m.at != test_clock.now() for m in chat.messages)
    assert chat.started_at == _at(0)
    assert chat.ended_at == _at(19)


# 2. Planted controls (D5)


def test_assistant_text_is_never_person():
    records = _session_records()
    chat = _only(_jsonl(records))
    uuids = {record["uuid"] for record in records if record.get("type") == "assistant"}
    assistant_keys = {
        message_key("cc", SESSION, uuid, kind): kind
        for uuid in uuids
        for kind in ("person", "ai_host", "automated")
    }

    from_assistant = [m for m in chat.messages if m.key in assistant_keys]
    assert len(from_assistant) == 4
    assert {m.speaker_kind for m in from_assistant} == {"ai_host"}


def test_failed_tool_result_is_automated_and_a_good_one_is_dropped():
    chat = _only(_jsonl(_session_records()))

    automated = [m for m in chat.messages if m.speaker_kind == "automated"]
    assert [m.text for m in automated] == ["[tool error] File does not exist."]
    assert all(TOOL_OUTPUT not in m.text for m in chat.messages)


def test_dropped_text_never_appears_in_any_message():
    chat = _only(_jsonl(_session_records()))

    texts = [m.text for m in chat.messages]
    for marker in DROPPED_MARKERS:
        assert all(marker not in text for text in texts), marker


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-ts", "sdk-py"])
def test_program_run_session_has_no_person_message(entrypoint):
    records = [
        _user(1, 0, "Summarise the open pull requests.", entrypoint=entrypoint),
        _assistant(2, 1, [_text("There are two open.")], entrypoint=entrypoint),
        _user(3, 2, [_text("Write it to a file.")], entrypoint=entrypoint),
    ]

    chat = _only(_jsonl(records))

    assert [m.speaker_kind for m in chat.messages] == ["automated", "ai_host", "automated"]
    assert chat.messages[0].text == "Summarise the open pull requests."


@pytest.mark.parametrize("entrypoint", ["cli", "claude-vscode", None])
def test_interactive_entrypoints_stay_person(entrypoint):
    record = _user(1, 0, "Rename the beds module.", entrypoint=entrypoint)
    if entrypoint is None:
        del record["entrypoint"]

    assert _rows(_only(_jsonl([record]))) == [("person", "Rename the beds module.")]


def test_attachment_is_only_the_fact():
    records = [
        _user(
            1,
            0,
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": BASE64},
                }
            ],
        ),
        _user(
            2,
            1,
            [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": BASE64},
                },
                {"type": "document", "source": {"type": "text", "data": DOC_TEXT}},
            ],
        ),
    ]

    chat = _only(_jsonl(records))

    assert _rows(chat) == [
        ("person", "[attached an image]"),
        ("person", "[attached a document]\n\n[attached a document]"),
    ]
    assert all(BASE64 not in m.text and DOC_TEXT not in m.text for m in chat.messages)


def test_sidechain_prompt_is_ai_host_never_person():
    records = [
        _user(1, 0, "Survey the test folder.", isSidechain=True),
        _assistant(2, 1, [_text("The folder holds four tests.")], isSidechain=True),
        _assistant(
            3, 2, [_tool_use("Grep", {"pattern": "def test_", "path": "tests"})], isSidechain=True
        ),
        _user(4, 3, [_tool_result("no match", is_error=True)], isSidechain=True),
    ]

    chat = _only(_jsonl(records))

    assert _rows(chat) == [
        ("ai_host", "Survey the test folder."),
        ("ai_host", "The folder holds four tests."),
        ("ai_host", "[tool Grep] def test_"),
        ("automated", "[tool error] no match"),
    ]


def test_injected_records_keep_only_a_failed_tool_result():
    records = [
        _user(1, 0, [_text(META), _tool_result("exit 3", is_error=True)], isMeta=True),
        _user(
            2, 1, [_text(COMPACT), _tool_result("timed out", is_error=True)], isCompactSummary=True
        ),
        _user(3, 2, [_text(INTERNAL), _tool_result(TOOL_OUTPUT)], userType="internal"),
    ]

    chat = _only(_jsonl(records))

    assert _rows(chat) == [
        ("automated", "[tool error] exit 3"),
        ("automated", "[tool error] timed out"),
    ]


def test_person_slot_forms():
    records = [
        _user(
            1, 0, "<command-name>/clear</command-name>\n<command-message>clear</command-message>"
        ),
        _user(2, 1, "<command-name>review</command-name><command-args>42</command-args>"),
        _user(3, 2, "<bash-input>git status</bash-input><bash-stdout></bash-stdout>"),
        _user(4, 3, "[Request interrupted by user for tool use]"),
        _user(5, 4, [_text("[Request interrupted by user]"), _text("Stop and explain.")]),
        _user(6, 5, f"<system-reminder>{REMINDER}</system-reminder>"),
    ]

    assert _rows(_only(_jsonl(records))) == [
        ("person", "/clear"),
        ("person", "/review 42"),
        ("person", "! git status"),
        ("person", "Stop and explain."),
    ]


def test_tool_digest_picks_the_telling_input():
    long_command = "echo " + "x" * 300
    blocks = [
        _tool_use("Bash", {"command": "ls", "description": "List files"}),
        _tool_use("Bash", {"command": long_command}),
        _tool_use("Glob", {"pattern": "**/*.py"}),
        _tool_use("WebFetch", {"url": "https://example.org/a", "format": "md"}),
        _tool_use("TodoWrite", {"todos": [{"content": "water", "status": "done"}]}),
        _tool_use("Task", {"prompt": "Look\n\nat   this", "subagent_type": "x"}),
    ]

    chat = _only(_jsonl([_assistant(1, 0, blocks)]))

    lines = chat.messages[0].text.split("\n")
    assert lines[0] == "[tool Bash] List files"
    assert lines[1] == "[tool Bash] " + long_command[:120]
    assert lines[2] == "[tool Glob] **/*.py"
    assert lines[3] == "[tool WebFetch] https://example.org/a"
    assert lines[4] == '[tool TodoWrite] {"todos":[{"content":"water","status":"done"}]}'
    assert lines[5] == "[tool Task] Look at this"


def test_tool_error_digest_is_one_clipped_line():
    long_error = "line one\n" + "y" * 400
    chat = _only(_jsonl([_user(1, 0, [_tool_result(long_error, is_error=True)])]))

    (message,) = chat.messages
    assert message.speaker_kind == "automated"
    assert message.text == "[tool error] " + ("line one " + "y" * 400)[:200]
    assert "\n" not in message.text


def test_tool_error_digest_drops_a_reminder_the_program_added():
    content = [_text(f"Permission denied.<system-reminder>{REMINDER}</system-reminder>")]
    chat = _only(_jsonl([_user(1, 0, [_tool_result(content, is_error=True)])]))

    assert _rows(chat) == [("automated", "[tool error] Permission denied.")]


# 3. Keys


def test_keys_are_deterministic_unique_and_prefixed():
    data = _jsonl(_session_records())

    first = [m.key for m in _only(data).messages]
    second = [m.key for m in _only(data).messages]

    assert first == second
    assert len(set(first)) == len(first)
    assert all(re.fullmatch(r"cc[0-9a-f]{32}", key) for key in first)
    assert first == [
        message_key("cc", SESSION, _uuid(number), speaker) for speaker, _, _, number in EXPECTED
    ]


def test_one_record_gives_one_key_per_speaker_kind():
    record = _user(1, 0, [_tool_result("refused", is_error=True), _text("Try again please.")])

    chat = _only(_jsonl([record]))

    assert [(m.key, m.speaker_kind) for m in chat.messages] == [
        (message_key("cc", SESSION, _uuid(1), "automated"), "automated"),
        (message_key("cc", SESSION, _uuid(1), "person"), "person"),
    ]


def test_program_run_record_with_a_failed_result_gives_one_automated_message():
    record = _user(
        1, 0, [_tool_result("refused", is_error=True), _text("Try again.")], entrypoint="sdk-py"
    )

    chat = _only(_jsonl([record]))

    # The prompt and the error share the automated key, so neither may be lost to a twin.
    assert [(m.key, m.speaker_kind, m.text) for m in chat.messages] == [
        (
            message_key("cc", SESSION, _uuid(1), "automated"),
            "automated",
            "[tool error] refused\n\nTry again.",
        )
    ]


def test_screening_leaves_keys_unchanged():
    chat = _only(_jsonl(_session_records()))
    inputs = [
        Message(
            key=m.key,
            sender=m.sender,
            text=m.text,
            at=m.at,
            speaker_kind=m.speaker_kind,
            modality=m.modality,
        )
        for m in chat.messages
    ]

    screened = Screener(ScreeningConfig()).screen_source(chat_locator("cc", SESSION), inputs)

    assert [m.key for m in screened.messages] == [m.key for m in inputs]


# 4. A grown export


def test_grown_session_keeps_old_keys_and_adds_new_ones():
    records = _session_records()
    early = _only(_jsonl(records[:7]))
    grown = _only(_jsonl(records))

    early_keys = [m.key for m in early.messages]
    grown_keys = [m.key for m in grown.messages]
    assert grown_keys[: len(early_keys)] == early_keys
    assert len(grown_keys) > len(early_keys)


# 5. Format errors and archives


@pytest.mark.parametrize(
    "data",
    [
        random.Random(7).randbytes(2048),
        b"",
        json.dumps({"timestamp": _ts(0), "type": "session_meta", "payload": {"id": "x"}}).encode(),
        json.dumps([{"title": "Seed plan", "mapping": {}, "create_time": 1.0}]).encode(),
        _zip([]),
        _zip([("notes.txt", b"plain words")]),
        _zip([("session.jsonl", b"not json at all\n")]),
    ],
    ids=["random", "empty", "codex-line", "chat-export", "empty-zip", "no-jsonl", "no-record"],
)
def test_not_an_export(data):
    with pytest.raises(NotAnExport) as raised:
        _parse(data)
    assert "zqx" not in str(raised.value)


def test_cut_last_line_is_skipped():
    records = _session_records()
    whole = _jsonl(records)
    cut = whole + b'{"type":"user","uuid":"' + _uuid(99).encode() + b'","sessionId":"'

    assert _rows(_only(cut)) == _rows(_only(whole))


def test_valid_export_with_no_chat_yields_nothing():
    records = [
        {"type": "summary", "summary": SUMMARY, "leafUuid": _uuid(1)},
        {"type": "file-history-snapshot", "messageId": _uuid(2), "snapshot": {}},
    ]

    assert _parse(_jsonl(records)) == []


def test_zip_merges_files_of_one_session_in_file_order_without_twins():
    main = _jsonl(_session_records()[:3])
    subagent = _jsonl(
        [
            _user(50, 30, "Check the pump driver.", isSidechain=True, agentId="a1"),
            _assistant(51, 31, [_text("The driver is fine.")], isSidechain=True, agentId="a1"),
        ]
    )
    other = _jsonl([_user(1, 40, "Start the seed tracker.", session=OTHER_SESSION)])
    data = _zip(
        [
            (f"projects/garden/{SESSION}.jsonl", main),
            (f"projects/garden/{SESSION}/subagents/agent-a1.jsonl", subagent),
            (f"projects/garden/{OTHER_SESSION}.jsonl", other),
            (f"backup/{SESSION}.jsonl", main),
            ("__MACOSX/._x.jsonl", b"\x00\x05\x16\x07"),
        ]
    )

    chats = _parse(data)

    assert [chat.chat_id for chat in chats] == [SESSION, OTHER_SESSION]
    assert _rows(chats[0]) == [
        ("person", "Please add a watering schedule to the garden app."),
        ("ai_host", "I will look at the schedule module first."),
        ("ai_host", "[tool Bash] List files"),
        ("ai_host", "Check the pump driver."),
        ("ai_host", "The driver is fine."),
    ]
    assert _rows(chats[1]) == [("person", "Start the seed tracker.")]
    assert chats[0].ended_at == _at(31)


def test_text_is_cleaned():
    record = _user(1, 0, "Line one\r\nline two\x00 and a lone \ud800 half")

    (message,) = _only(_jsonl([record])).messages

    assert message.text == "Line one\nline two and a lone � half"


# 6. End to end through the store


def _stored(driver, world_id: str) -> list[list[dict]]:
    rows = WorldQuery(driver, world_id).read(
        "MATCH (s:Source) RETURN s.content AS content ORDER BY s.id"
    )
    return [json.loads(row["content"]) for row in rows]


def _two_sessions(extra: list[dict] | None = None) -> bytes:
    first = _session_records()[:7] + (extra or [])
    second = [
        _user(1, 40, "Start the seed tracker.", session=OTHER_SESSION),
        _assistant(2, 41, [_text("I made the tracker table.")], session=OTHER_SESSION),
    ]
    return _zip([("a.jsonl", _jsonl(first)), ("b.jsonl", _jsonl(second))])


def test_take_in_stores_each_chat_once_and_a_reimport_changes_nothing(
    driver, seeded_world, test_clock
):
    before = counts(driver, seeded_world)

    result = take_in(
        driver, seeded_world, "claude_code", upload_of(_two_sessions()), screener=make_screener()
    )

    after = counts(driver, seeded_world)
    assert result.state == TAKEN_IN
    assert (result.chats, result.sources_added, result.episodes_added) == (2, 2, 2)
    assert after["sources"] == before["sources"] + 2
    assert after["episodes"] == before["episodes"] + 2
    contents = _stored(driver, seeded_world)
    rows = [row for content in contents for row in content]
    assert all(row["speaker_kind"] in {"person", "ai_host", "automated"} for row in rows)
    assert {row["modality"] for row in rows} == {"text"}
    texts = {row["text"] for row in rows}
    assert "Start the seed tracker." in texts
    assert "I will look at the schedule module first." in texts
    starts = WorldQuery(driver, seeded_world).read("MATCH (e:Episode) RETURN e.start_time AS start")
    # Each episode starts at its first message's own time, not at the store time.
    assert sorted(row["start"] for row in starts) == [_at(0), _at(40)]

    again = take_in(
        driver, seeded_world, "claude_code", upload_of(_two_sessions()), screener=make_screener()
    )

    assert again.state == NOTHING_NEW
    assert counts(driver, seeded_world) == after
    assert (again.sources_added, again.episodes_added) == (0, 0)

    grown = _two_sessions([_user(30, 50, "Now add frost alerts.")])
    third = take_in(driver, seeded_world, "claude_code", upload_of(grown), screener=make_screener())

    final = counts(driver, seeded_world)
    assert third.state == TAKEN_IN
    assert (third.sources_added, third.episodes_added) == (1, 1)
    assert final["sources"] == after["sources"] + 1
    assert final["episodes"] == after["episodes"] + 1
    newest = [content for content in _stored(driver, seeded_world) if len(content) == 1]
    assert [row["text"] for content in newest for row in content] == ["Now add frost alerts."]


def test_take_in_screens_invented_names(driver, seeded_world, test_clock):
    records = [
        _user(1, 0, "Ask Brannagh Quill about the soil test."),
        _user(2, 1, "Odile Farrow wants the tomato bed moved."),
        _assistant(3, 2, [_text("I moved the tomato bed.")]),
    ]
    screener = make_screener(denylist=["Brannagh Quill"], rename={"Odile Farrow": "Person A"})

    result = take_in(
        driver, seeded_world, "claude_code", upload_of(_jsonl(records)), screener=screener
    )

    assert result.state == TAKEN_IN
    (content,) = _stored(driver, seeded_world)
    raw = json.dumps(content)
    assert "Brannagh" not in raw and "Quill" not in raw
    assert "Odile" not in raw and "Farrow" not in raw
    assert [(row["speaker_kind"], row["text"]) for row in content] == [
        ("person", "Person A wants the tomato bed moved."),
        ("ai_host", "I moved the tomato bed."),
    ]


def test_a_subagent_file_listed_first_still_reads_in_time_order():
    """The archive's file order says nothing about time (D10): the subagent's file comes first
    here, and its messages still sit between the parent's."""
    main = _jsonl(
        [
            _user(1, 0, "Plan the greenhouse heater."),
            _assistant(2, 1, [_text("I will ask a helper to measure first.")]),
            _assistant(3, 60, [_text("The heater plan is done.")]),
        ]
    )
    subagent = _jsonl(
        [
            _user(50, 10, "Measure the greenhouse.", isSidechain=True, agentId="a1"),
            _assistant(51, 20, [_text("It is four metres long.")], isSidechain=True, agentId="a1"),
        ]
    )
    data = _zip(
        [
            (f"projects/garden/{SESSION}/subagents/agent-a1.jsonl", subagent),
            (f"projects/garden/{SESSION}.jsonl", main),
        ]
    )

    (chat,) = _parse(data)

    assert _rows(chat) == [
        ("person", "Plan the greenhouse heater."),
        ("ai_host", "I will ask a helper to measure first."),
        ("ai_host", "Measure the greenhouse."),
        ("ai_host", "It is four metres long."),
        ("ai_host", "The heater plan is done."),
    ]
    times = [message.at for message in chat.messages]
    assert times == sorted(times)


def _within_seconds(seconds, work):
    """work(), or a TimeoutError after `seconds`: an alarm, since no test may read the clock."""

    def stop(signum, frame):
        raise TimeoutError("the work took too long")

    previous = signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)
    try:
        return work()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_unclosed_tags_in_the_person_slot_cost_linear_time():
    """A lazy <tag>.*?</tag> regex scans to the end of the text from each unclosed opener, so a
    crafted line of a few hundred KB held an upload slot for minutes. The filter is one pass."""
    text = (
        "<system-reminder>" * 15_000
        + "<command-name>" * 15_000
        + "<bash-input>" * 15_000
        + "Keep the seed list."
    )

    (message,) = _within_seconds(5, lambda: _only(_jsonl([_user(1, 0, text)])).messages)

    # No closer, so nothing is cut: the text is the person's as written.
    assert message.text == text
    assert message.speaker_kind == "person"


def test_the_linear_filter_cuts_what_the_spans_hold():
    text = (
        "<system-reminder>a</system-reminder>keep <system-reminder>b</system-reminder>this"
        "<system-reminder>open"
    )
    assert claude_code._person_text(text) == "keep this<system-reminder>open"
    call = "<command-name>plan</command-name><command-args>a b</command-args>then more"
    assert claude_code._person_text(call) == "/plan a b\n\nthen more"
    assert claude_code._person_text("<bash-input> ls -la </bash-input>") == "! ls -la"


def test_the_records_of_one_upload_count_against_its_budget():
    records = [_user(n, n, f"Note {n} on the herb bed.") for n in range(1, 11)]
    data = _jsonl(records)

    def parse_with(budget):
        return list(claude_code.parse(Upload(io.BytesIO(data), len(data), budget=budget)))

    assert len(parse_with(ReadBudget(max_records=10))[0].messages) == 10
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_records=9))


def test_take_in_stores_when_the_rename_map_holds_the_key_prefix(driver, seeded_world, test_clock):
    """ "CC" as a nickname in the rename map once changed every key ("cc-..."), and every chat
    failed closed. The key is one word now."""
    records = [
        _user(1, 0, "CC wants the tomato bed moved."),
        _assistant(2, 1, [_text("I moved the tomato bed.")]),
    ]
    screener = make_screener(rename={"CC": "Person D"})
    upload = _jsonl(records)

    result = take_in(driver, seeded_world, "claude_code", upload_of(upload), screener=screener)

    assert result.state == TAKEN_IN
    assert result.failed_chats == 0 and result.sources_added == 1
    (content,) = _stored(driver, seeded_world)
    assert [(row["speaker_kind"], row["text"]) for row in content] == [
        ("person", "Person D wants the tomato bed moved."),
        ("ai_host", "I moved the tomato bed."),
    ]
    again = take_in(driver, seeded_world, "claude_code", upload_of(upload), screener=screener)
    assert again.state == NOTHING_NEW
