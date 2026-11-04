"""The Codex parser (A.4, D5, D14): speakers, the drop rules, keys and the store path.

Every rollout here is built in code from invented ids, invented people and invented text (R3).
Marker strings stand for text that must never reach a message: each test plants them and then
proves they are absent from every message text.
"""

import hashlib
import io
import json
import random
import re
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import zstandard

from grove.ingest import NotAnExport, ParsedChat, ReadBudget, Upload, chat_locator, message_key
from grove.ingest.parsers import codex
from grove.ingest.take_in import NOTHING_NEW, TAKEN_IN, take_in
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from tests.ingest.conftest import counts, make_screener, upload_of

SESSION = "7a1e4c2b-3d5f-4a6b-8c9d-0e1f2a3b4c01"
OTHER_SESSION = "7a1e4c2b-3d5f-4a6b-8c9d-0e1f2a3b4c02"
OLD_SESSION = "7a1e4c2b-3d5f-4a6b-8c9d-0e1f2a3b4c03"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
OLD_T0 = datetime(2025, 5, 1, 10, 0, tzinfo=UTC)

INSTRUCTIONS = "zqy-instructions-marker-11"
DEVELOPER = "zqy-developer-marker-12"
USER_INSTRUCTIONS = "zqy-user-instructions-marker-13"
ENVIRONMENT = "zqy-environment-marker-14"
SHELL_COMMAND = "zqy-user-shell-command-marker-15"
AGENTS = "zqy-agents-md-marker-16"
TURN_CONTEXT = "zqy-turn-context-marker-17"
EVENT_USER = "zqy-event-user-marker-18"
REASONING = "zqy-reasoning-marker-19"
ENCRYPTED = "zqy-encrypted-marker-20"
EVENT_REASONING = "zqy-event-reasoning-marker-21"
TOOL_OUTPUT = "zqy-tool-output-marker-22"
PATCH_BODY = "zqy-patch-body-marker-23"
PATCH_OK = "zqy-patch-ok-marker-24"
EVENT_AGENT = "zqy-event-agent-marker-25"
COMPACTED = "zqy-compacted-marker-26"
SYSTEM = "zqy-system-marker-27"
BASE64 = "WlFZLWJhc2U2NC1tYXJrZXItMjg"
OK_OUTPUT = "zqy-ok-output-marker-29"
DROPPED_MARKERS = (
    INSTRUCTIONS,
    DEVELOPER,
    USER_INSTRUCTIONS,
    ENVIRONMENT,
    SHELL_COMMAND,
    AGENTS,
    TURN_CONTEXT,
    EVENT_USER,
    REASONING,
    ENCRYPTED,
    EVENT_REASONING,
    TOOL_OUTPUT,
    PATCH_BODY,
    PATCH_OK,
    EVENT_AGENT,
    COMPACTED,
    SYSTEM,
    BASE64,
    OK_OUTPUT,
)


def _at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def _ts(minute: int, base: datetime = T0) -> str:
    return (base + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.123Z")


def _line(minute: int, kind: str, payload: dict) -> dict:
    return {"timestamp": _ts(minute), "type": kind, "payload": payload}


def _meta(
    session: str = SESSION, *, originator: str = "codex_cli_rs", source: object = "cli"
) -> dict:
    return _line(
        0,
        "session_meta",
        {
            "id": session,
            "timestamp": _ts(0),
            "cwd": "/work/seed-shop",
            "originator": originator,
            "cli_version": "0.50.0",
            "source": source,
            "instructions": INSTRUCTIONS,
        },
    )


def _item(minute: int, payload: dict) -> dict:
    return _line(minute, "response_item", payload)


def _event(minute: int, payload: dict) -> dict:
    return _line(minute, "event_msg", payload)


def _message(role: str, *blocks: dict) -> dict:
    return {"type": "message", "role": role, "content": list(blocks)}


def _text_in(text: str) -> dict:
    return {"type": "input_text", "text": text}


def _text_out(text: str) -> dict:
    return {"type": "output_text", "text": text}


def _shell(command: list[str], call: str) -> dict:
    arguments = json.dumps({"command": command, "workdir": "/work/seed-shop"})
    return {"type": "function_call", "name": "shell", "arguments": arguments, "call_id": call}


def _output(output, call: str = "call_1", kind: str = "function_call_output") -> dict:
    return {"type": kind, "call_id": call, "output": output}


def _exec_output(text: str, exit_code: int) -> str:
    return json.dumps({"output": text, "metadata": {"exit_code": exit_code, "duration_seconds": 1}})


def _jsonl(lines: list[dict]) -> bytes:
    return "".join(json.dumps(line) + "\n" for line in lines).encode()


def _zst(data: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(data)


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def _parse(data: bytes) -> list[ParsedChat]:
    return list(codex.parse(upload_of(data)))


def _only(data: bytes) -> ParsedChat:
    chats = _parse(data)
    assert len(chats) == 1
    return chats[0]


def _rows(chat: ParsedChat) -> list[tuple[str, str]]:
    return [(message.speaker_kind, message.text) for message in chat.messages]


PATCH = (
    "*** Begin Patch\n*** Add File: src/inventory.py\n+" + PATCH_BODY + "\n"
    "*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch"
)


def _rollout_lines() -> list[dict]:
    """A small realistic rollout: work, tools, an image and every kind of injected text."""
    return [
        _meta(),
        _item(0, _message("developer", _text_in(DEVELOPER))),
        _item(0, _message("user", _text_in(f"<user_instructions>\n{USER_INSTRUCTIONS}\n"))),
        _item(
            0,
            _message(
                "user", _text_in(f"<environment_context>\n{ENVIRONMENT}\n</environment_context>")
            ),
        ),
        _line(0, "turn_context", {"cwd": "/work/seed-shop", "note": TURN_CONTEXT}),
        _item(
            1,
            _message(
                "user",
                _text_in(f"# AGENTS.md instructions for /work/seed-shop\n\n{AGENTS}"),
                _text_in("Add a seed inventory page."),
            ),
        ),
        _event(1, {"type": "user_message", "message": EVENT_USER}),
        _item(
            2,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": REASONING}],
                "encrypted_content": ENCRYPTED,
            },
        ),
        _event(2, {"type": "agent_reasoning", "text": EVENT_REASONING}),
        _item(3, _shell(["bash", "-lc", "ls src"], "call_1")),
        _item(3, _output(_exec_output(TOOL_OUTPUT, 0), "call_1")),
        _item(4, _shell(["bash", "-lc", "cat src/missing.py"], "call_2")),
        _item(4, _output(_exec_output("cat: src/missing.py: No such file", 1), "call_2")),
        _item(
            5,
            {"type": "custom_tool_call", "name": "apply_patch", "input": PATCH, "call_id": "c3"},
        ),
        _item(5, _output(_exec_output(PATCH_OK, 0), "c3", "custom_tool_call_output")),
        _item(6, {"type": "web_search_call", "action": {"type": "search", "query": "seed damp"}}),
        _item(
            7,
            {
                "type": "local_shell_call",
                "call_id": "c4",
                "status": "completed",
                "action": {"type": "exec", "command": ["pytest", "-q"]},
            },
        ),
        _item(7, _output("Exit code: 2\nWall time: 0.3 seconds\nOutput:\nFAILED test_a", "c4")),
        _item(
            8,
            _message(
                "user",
                _text_in("Here is the label design."),
                {"type": "input_image", "image_url": f"data:image/png;base64,{BASE64}"},
            ),
        ),
        _item(9, _message("assistant", _text_out("The page lists each seed with its stock."))),
        _event(9, {"type": "agent_message", "message": EVENT_AGENT}),
        _event(9, {"type": "token_count", "info": {"total": 12}}),
        _line(10, "compacted", {"message": COMPACTED}),
        _item(10, _message("system", _text_in(SYSTEM))),
        _item(11, _message("user", _text_in(f"<user_shell_command>{SHELL_COMMAND}"))),
        _item(12, _output({"content": "permission denied", "success": False}, "c5")),
        _item(13, _output("error: the sandbox refused the write", "c6")),
        _item(14, _output(_exec_output(f"Error log is empty {OK_OUTPUT}", 0), "c7")),
    ]


EXPECTED = [
    (6, "person", "Add a seed inventory page.", 1),
    (10, "ai_host", "[tool shell] bash -lc ls src", 3),
    (12, "ai_host", "[tool shell] bash -lc cat src/missing.py", 4),
    (13, "automated", "[tool error] cat: src/missing.py: No such file", 4),
    (14, "ai_host", "[tool apply_patch] src/inventory.py, src/app.py", 5),
    (16, "ai_host", "[tool web_search] seed damp", 6),
    (17, "ai_host", "[tool local_shell] pytest -q", 7),
    (
        18,
        "automated",
        "[tool error] Exit code: 2 Wall time: 0.3 seconds Output: FAILED test_a",
        7,
    ),
    (19, "person", "Here is the label design.\n\n[attached an image]", 8),
    (20, "ai_host", "The page lists each seed with its stock.", 9),
    (26, "automated", "[tool error] permission denied", 12),
    (27, "automated", "[tool error] error: the sandbox refused the write", 13),
]


def _at_ms(minute: int) -> datetime:
    return _at(minute) + timedelta(milliseconds=123)


# 1. A realistic export


def test_parse_reads_messages_in_order_with_their_own_times(test_clock):
    chat = _only(_jsonl(_rollout_lines()))

    assert chat.chat_id == SESSION
    assert [(m.speaker_kind, m.text, m.at) for m in chat.messages] == [
        (speaker, text, _at_ms(minute)) for _, speaker, text, minute in EXPECTED
    ]
    assert {m.modality for m in chat.messages} == {"text"}
    assert {m.sender for m in chat.messages} == {None}
    # The message's own time, never the time of the upload.
    assert all(m.at != test_clock.now() for m in chat.messages)
    assert chat.started_at == _at_ms(0)
    assert chat.ended_at == _at_ms(14)


def test_old_format_rollout():
    lines = [
        {"id": OLD_SESSION, "timestamp": _ts(0, OLD_T0), "instructions": INSTRUCTIONS},
        {"record_type": "state"},
        _message("user", _text_in("Plan the greenhouse layout.")),
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": REASONING}]},
        _message("assistant", _text_out("Here is a layout in three rows.")),
        {"type": "function_call", "name": "shell", "arguments": '{"command":["ls"]}'},
        {"type": "function_call_output", "call_id": "c", "output": "failed: no such dir"},
        {"record_type": "state"},
    ]

    chat = _only(_jsonl(lines))

    assert chat.chat_id == OLD_SESSION
    assert _rows(chat) == [
        ("person", "Plan the greenhouse layout."),
        ("ai_host", "Here is a layout in three rows."),
        ("ai_host", "[tool shell] ls"),
        ("automated", "[tool error] failed: no such dir"),
    ]
    # Old lines carry no time: the chat falls back to the session meta's own start time.
    assert [m.at for m in chat.messages] == [None] * 4
    assert chat.started_at == OLD_T0 + timedelta(milliseconds=123)
    assert [m.key for m in chat.messages] == [
        message_key("cx", OLD_SESSION, number) for number in (3, 5, 6, 7)
    ]


# 2. Planted controls (D5)


def test_assistant_text_is_never_person():
    chat = _only(_jsonl(_rollout_lines()))

    by_key = {m.key: m for m in chat.messages}
    for number in (10, 12, 14, 16, 17, 20):
        assert by_key[message_key("cx", SESSION, number)].speaker_kind == "ai_host"


def test_failed_tool_output_is_automated_and_a_good_one_is_dropped():
    chat = _only(_jsonl(_rollout_lines()))

    automated = [m.text for m in chat.messages if m.speaker_kind == "automated"]
    assert automated == [
        "[tool error] cat: src/missing.py: No such file",
        "[tool error] Exit code: 2 Wall time: 0.3 seconds Output: FAILED test_a",
        "[tool error] permission denied",
        "[tool error] error: the sandbox refused the write",
    ]


def test_dropped_text_never_appears_in_any_message():
    chat = _only(_jsonl(_rollout_lines()))

    texts = [m.text for m in chat.messages]
    for marker in DROPPED_MARKERS:
        assert all(marker not in text for text in texts), marker


@pytest.mark.parametrize(
    ("originator", "source"),
    [
        ("codex_exec", "exec"),
        ("codex_exec", "cli"),
        ("codex_sdk_ts", "cli"),
        ("codex_cli_rs", "exec"),
        ("codex_cli_rs", "mcp"),
    ],
)
def test_program_run_session_has_no_person_message(originator, source):
    lines = [
        _meta(originator=originator, source=source),
        _item(1, _message("user", _text_in("Summarise the open tasks."))),
        _item(2, _message("assistant", _text_out("There are two open tasks."))),
        _item(3, _message("user", _text_in("Write them to a file."))),
    ]

    assert _rows(_only(_jsonl(lines))) == [
        ("automated", "Summarise the open tasks."),
        ("ai_host", "There are two open tasks."),
        ("automated", "Write them to a file."),
    ]


@pytest.mark.parametrize(("originator", "source"), [("codex_vscode", "vscode"), ("x", "cli")])
def test_interactive_session_stays_person(originator, source):
    lines = [
        _meta(originator=originator, source=source),
        _item(1, _message("user", _text_in("Rename the stock column."))),
    ]

    assert _rows(_only(_jsonl(lines))) == [("person", "Rename the stock column.")]


def test_attachment_is_only_the_fact():
    lines = [
        _meta(),
        _item(1, _message("user", {"type": "input_image", "image_url": f"data:,{BASE64}"})),
    ]

    chat = _only(_jsonl(lines))

    assert _rows(chat) == [("person", "[attached an image]")]
    assert all(BASE64 not in m.text for m in chat.messages)


def test_failure_rules_of_tool_output():
    outputs = [
        ("Exit code: 0\nOutput:\nok", False),
        ("Exit code: 1\nOutput:\nbad", True),
        ("  Aborted by the user", True),
        ("FAILED to start", True),
        ("all good", False),
        (_exec_output("Error log is empty", 0), False),
        (_exec_output("fine", 127), True),
        (json.dumps({"output": "error: x"}), True),
        ({"content": "done", "success": True}, False),
        ({"content": "error looks bad but worked", "success": True}, False),
        ({"content": "no", "success": False}, True),
        (None, False),
    ]
    lines = [_meta()] + [
        _item(n, _output(output, f"c{n}")) for n, (output, _) in enumerate(outputs)
    ]

    chat = _only(_jsonl(lines))

    failed_lines = {n + 2 for n, (_, failed) in enumerate(outputs) if failed}
    assert {message_key("cx", SESSION, number) for number in failed_lines} == {
        m.key for m in chat.messages
    }
    assert {m.speaker_kind for m in chat.messages} == {"automated"}


def test_tool_digests():
    long_command = ["bash", "-lc", "echo " + "z" * 300]
    lines = [
        _meta(),
        _item(1, _shell(long_command, "c1")),
        _item(
            2,
            {
                "type": "function_call",
                "name": "apply_patch",
                "arguments": json.dumps(
                    {"input": "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch"}
                ),
            },
        ),
        _item(3, {"type": "function_call", "name": "view_image", "arguments": '{"x": 1}'}),
        _item(4, {"type": "function_call", "name": "lookup", "arguments": "not json"}),
        _item(5, {"type": "custom_tool_call", "name": "apply_patch", "input": "no files"}),
        _item(6, {"type": "function_call", "arguments": '{"query": "seeds"}'}),
        _item(
            7,
            {
                "type": "web_search_call",
                "action": {"type": "open_page", "url": "https://example.org/seeds"},
            },
        ),
        _item(8, {"type": "web_search_call", "action": {"type": "find", "pattern": "damp"}}),
    ]

    chat = _only(_jsonl(lines))

    assert [m.text for m in chat.messages] == [
        "[tool shell] " + " ".join(long_command)[:120],
        "[tool apply_patch] old.txt",
        '[tool view_image] {"x":1}',
        "[tool lookup] not json",
        "[tool apply_patch] no files",
        "[tool tool] seeds",
        "[tool web_search] https://example.org/seeds",
        '[tool web_search] {"pattern":"damp","type":"find"}',
    ]


# 3. Keys


def test_keys_are_deterministic_unique_and_prefixed():
    data = _jsonl(_rollout_lines())

    first = [m.key for m in _only(data).messages]
    second = [m.key for m in _only(data).messages]

    assert first == second
    assert len(set(first)) == len(first)
    assert all(re.fullmatch(r"cx[0-9a-f]{32}", key) for key in first)
    assert first == [message_key("cx", SESSION, number) for number, *_ in EXPECTED]


def test_screening_leaves_keys_unchanged():
    chat = _only(_jsonl(_rollout_lines()))
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

    screened = Screener(ScreeningConfig()).screen_source(chat_locator("cx", SESSION), inputs)

    assert [m.key for m in screened.messages] == [m.key for m in inputs]


def test_chat_id_without_session_meta_is_a_fingerprint_of_the_first_line():
    lines = [_item(1, _message("user", _text_in("Count the seed packets.")))]
    data = _jsonl(lines)

    chat = _only(data)

    first_line = data.split(b"\n")[0]
    assert chat.chat_id == "first-line-" + hashlib.sha256(first_line).hexdigest()
    assert _only(data + _jsonl([_item(2, _message("user", _text_in("More.")))])).chat_id == (
        chat.chat_id
    )


# 4. A grown export


def test_grown_rollout_keeps_old_keys_and_adds_new_ones():
    lines = _rollout_lines()
    early = _only(_jsonl(lines[:12]))
    grown = _only(_jsonl(lines))

    early_keys = [m.key for m in early.messages]
    grown_keys = [m.key for m in grown.messages]
    assert grown_keys[: len(early_keys)] == early_keys
    assert len(grown_keys) > len(early_keys)


# 5. Format errors and archives


@pytest.mark.parametrize(
    "data",
    [
        random.Random(11).randbytes(2048),
        b"",
        json.dumps(
            {"type": "user", "uuid": "u1", "sessionId": "s1", "message": {"content": "hi"}}
        ).encode(),
        json.dumps([{"title": "Seed plan", "mapping": {}, "create_time": 1.0}]).encode(),
        json.dumps({"id": "conv-1", "title": "Seed plan", "mapping": {}}).encode(),
        json.dumps({"id": "item-1", "timestamp": "last spring"}).encode(),
        _zip([]),
        _zip([("notes.txt", b"plain words")]),
        _zst(b"plain words\n"),
        b"\x28\xb5\x2f\xfd" + b"\x00" * 32,
    ],
    ids=[
        "random",
        "empty",
        "claude-code-line",
        "chat-export",
        "one-chat-object",
        "id-without-time",
        "empty-zip",
        "no-jsonl",
        "zst-of-text",
        "broken-zst",
    ],
)
def test_not_an_export(data):
    with pytest.raises(NotAnExport) as raised:
        _parse(data)
    assert "zqy" not in str(raised.value)


def test_cut_last_line_is_skipped():
    whole = _jsonl(_rollout_lines())
    cut = whole + b'{"timestamp":"2026-10-01T10:00:00Z","type":"response_item","payload":{"ty'

    assert _rows(_only(cut)) == _rows(_only(whole))


def test_valid_export_with_no_chat_yields_nothing():
    lines = [_meta(), _event(1, {"type": "token_count"}), _line(1, "turn_context", {})]

    assert _parse(_jsonl(lines)) == []


def test_compressed_rollout_reads_like_the_plain_one():
    data = _jsonl(_rollout_lines())

    assert _rows(_only(_zst(data))) == _rows(_only(data))
    assert [m.key for m in _only(_zst(data)).messages] == [m.key for m in _only(data).messages]


def test_zip_of_rollouts_merges_one_session_without_twins():
    plain = _jsonl(_rollout_lines()[:7])
    other = _jsonl(
        [_meta(OTHER_SESSION), _item(1, _message("user", _text_in("Start the seed tracker.")))]
    )
    data = _zip(
        [
            ("sessions/2026/10/01/rollout-a.jsonl", plain),
            ("sessions/2026/10/01/rollout-b.jsonl.zst", _zst(other)),
            ("archived/rollout-a.jsonl.zst", _zst(plain)),
            ("__MACOSX/._rollout-a.jsonl", b"\x00\x05\x16\x07"),
        ]
    )

    chats = _parse(data)

    assert [chat.chat_id for chat in chats] == [SESSION, OTHER_SESSION]
    assert _rows(chats[0]) == [("person", "Add a seed inventory page.")]
    assert _rows(chats[1]) == [("person", "Start the seed tracker.")]


def test_text_is_cleaned():
    lines = [_meta(), _item(1, _message("user", _text_in("One\r\ntwo\x00 and \ud800 half")))]

    (message,) = _only(_jsonl(lines)).messages

    assert message.text == "One\ntwo and � half"


# 6. End to end through the store


def _stored(driver, world_id: str) -> list[list[dict]]:
    rows = WorldQuery(driver, world_id).read(
        "MATCH (s:Source) RETURN s.content AS content ORDER BY s.id"
    )
    return [json.loads(row["content"]) for row in rows]


def _two_rollouts(extra: list[dict] | None = None) -> bytes:
    first = _rollout_lines()[:12] + (extra or [])
    second = [
        _meta(OTHER_SESSION),
        _item(40, _message("user", _text_in("Start the seed tracker."))),
        _item(41, _message("assistant", _text_out("I made the tracker table."))),
    ]
    return _zip([("a.jsonl", _jsonl(first)), ("b.jsonl.zst", _zst(_jsonl(second)))])


def test_take_in_stores_each_chat_once_and_a_reimport_changes_nothing(
    driver, seeded_world, test_clock
):
    before = counts(driver, seeded_world)

    result = take_in(
        driver, seeded_world, "codex", upload_of(_two_rollouts()), screener=make_screener()
    )

    after = counts(driver, seeded_world)
    assert result.state == TAKEN_IN
    assert (result.chats, result.sources_added, result.episodes_added) == (2, 2, 2)
    assert after["sources"] == before["sources"] + 2
    assert after["episodes"] == before["episodes"] + 2
    rows = [row for content in _stored(driver, seeded_world) for row in content]
    assert all(row["speaker_kind"] in {"person", "ai_host", "automated"} for row in rows)
    assert {row["modality"] for row in rows} == {"text"}
    texts = {row["text"] for row in rows}
    assert "Start the seed tracker." in texts
    assert "Add a seed inventory page." in texts
    starts = WorldQuery(driver, seeded_world).read("MATCH (e:Episode) RETURN e.start_time AS start")
    # Each episode starts at its first message's own time, not at the store time.
    assert sorted(row["start"] for row in starts) == [_at_ms(1), _at_ms(40)]

    again = take_in(
        driver, seeded_world, "codex", upload_of(_two_rollouts()), screener=make_screener()
    )

    assert again.state == NOTHING_NEW
    assert counts(driver, seeded_world) == after
    assert (again.sources_added, again.episodes_added) == (0, 0)

    grown = _two_rollouts([_item(50, _message("user", _text_in("Now add frost alerts.")))])
    third = take_in(driver, seeded_world, "codex", upload_of(grown), screener=make_screener())

    final = counts(driver, seeded_world)
    assert third.state == TAKEN_IN
    assert (third.sources_added, third.episodes_added) == (1, 1)
    assert final["sources"] == after["sources"] + 1
    assert final["episodes"] == after["episodes"] + 1
    newest = [content for content in _stored(driver, seeded_world) if len(content) == 1]
    assert [row["text"] for content in newest for row in content] == ["Now add frost alerts."]


def test_take_in_screens_invented_names(driver, seeded_world, test_clock):
    lines = [
        _meta(),
        _item(1, _message("user", _text_in("Ask Brannagh Quill about the soil test."))),
        _item(2, _message("user", _text_in("Odile Farrow wants the tomato bed moved."))),
        _item(3, _message("assistant", _text_out("I moved the tomato bed."))),
    ]
    screener = make_screener(denylist=["Brannagh Quill"], rename={"Odile Farrow": "Person A"})

    result = take_in(driver, seeded_world, "codex", upload_of(_jsonl(lines)), screener=screener)

    assert result.state == TAKEN_IN
    (content,) = _stored(driver, seeded_world)
    raw = json.dumps(content)
    assert "Brannagh" not in raw and "Quill" not in raw
    assert "Odile" not in raw and "Farrow" not in raw
    assert [(row["speaker_kind"], row["text"]) for row in content] == [
        ("person", "Person A wants the tomato bed moved."),
        ("ai_host", "I moved the tomato bed."),
    ]


@pytest.mark.parametrize(
    "source",
    [
        {"subagent": "review"},
        {"subagent": {"thread_spawn": {"parent_thread_id": "t-0001", "depth": 1}}},
        {"subagent": "compact"},
    ],
)
def test_a_subagent_session_files_its_prompt_under_the_ai_host(source):
    """A subagent's prompt is the parent agent's text, never the person's (D5)."""
    lines = [
        _meta(source=source),
        _item(1, _message("user", _text_in("Review the diff against main."))),
        _item(2, _message("assistant", _text_out("The diff looks fine."))),
    ]

    assert _rows(_only(_jsonl(lines))) == [
        ("ai_host", "Review the diff against main."),
        ("ai_host", "The diff looks fine."),
    ]


def test_an_exit_code_of_thousands_of_digits_is_no_crash():
    """int() of more than 4300 digits raises: the code takes at most 9 digits."""
    lines = [
        _meta(),
        _item(1, _output("Exit code: " + "7" * 5000 + "\nOutput:\nx")),
        _item(2, _output("Exit code: 3\nOutput:\nno such file", "call_2")),
    ]

    (chat,) = _parse(_jsonl(lines))

    assert [m.key for m in chat.messages] == [message_key("cx", SESSION, 3)]
    assert chat.messages[0].speaker_kind == "automated"


def test_many_compressed_rollouts_share_one_read_budget():
    """Each .zst member once had its own 256 MiB cap, so a small zip of many members expanded
    without bound. Every decompressed byte now counts against the upload."""
    body = _jsonl(_rollout_lines())
    packed = _zst(body)
    members = [(f"sessions/rollout-{n}.jsonl.zst", packed) for n in range(4)]
    room = 4 * len(packed) + 3 * len(body) + len(body) // 2

    def parse_with(member_count):
        archive_bytes = _zip(members[:member_count])
        upload = Upload(
            io.BytesIO(archive_bytes), len(archive_bytes), budget=ReadBudget(max_bytes=room)
        )
        return list(codex.parse(upload))

    assert len(parse_with(3)) == 1
    with pytest.raises(NotAnExport):
        parse_with(4)


def test_the_lines_of_one_upload_count_against_its_budget():
    data = _jsonl(_rollout_lines())
    # Every line counts, the empty one after the last newline too.
    lines = len(data.split(b"\n"))

    def parse_with(budget):
        return list(codex.parse(Upload(io.BytesIO(data), len(data), budget=budget)))

    assert parse_with(ReadBudget(max_records=lines))
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_records=lines - 1))


def test_a_rollout_listed_after_its_continuation_still_reads_in_time_order():
    first = _jsonl([_meta(), _item(1, _message("user", _text_in("Count the seed packets.")))])
    later = [
        _meta(),
        _item(1, _message("user", _text_in("Count the seed packets."))),
        _item(30, _message("assistant", _text_out("There are forty packets."))),
    ]
    data = _zip([("b/rollout-later.jsonl", _jsonl(later)), ("a/rollout-first.jsonl", first)])

    (chat,) = _parse(data)

    times = [m.at for m in chat.messages]
    assert times == sorted(times)
    assert _rows(chat) == [
        ("person", "Count the seed packets."),
        ("ai_host", "There are forty packets."),
    ]


def test_lines_whose_stamps_fall_are_read_in_time_order():
    """D10: a message is placed by its own time, not by its line."""
    lines = [
        _meta(),
        _item(40, _message("user", _text_in("Label the trays."))),
        _item(5, _message("user", _text_in("Fill the trays first."))),
    ]

    chat = _only(_jsonl(lines))

    assert [m.text for m in chat.messages] == ["Fill the trays first.", "Label the trays."]
    assert [m.key for m in chat.messages] == [
        message_key("cx", SESSION, 3),
        message_key("cx", SESSION, 2),
    ]


def test_the_chats_and_messages_of_one_upload_count_against_its_budget():
    first = _jsonl([_meta(), _item(1, _message("user", _text_in("Count the packets.")))])
    second = _jsonl(
        [_meta(OTHER_SESSION), _item(1, _message("user", _text_in("Sort the packets.")))]
    )
    data = _zip([("a/rollout-1.jsonl", first), ("b/rollout-2.jsonl", second)])

    def parse_with(budget):
        return list(codex.parse(Upload(io.BytesIO(data), len(data), budget=budget)))

    assert len(parse_with(ReadBudget(max_chats=2, max_messages=2))) == 2
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_chats=1))
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_messages=1))
