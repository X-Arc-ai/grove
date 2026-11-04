"""The ChatGPT export parser: the current branch, the speaker rules (D5), the planted controls,
stable keys, format errors and the store end to end.

Every export here is built in code from invented chats (R3). Marker strings stand for text that
must never reach a message: reasoning, custom instructions, tool output, file contents.
"""

import copy
import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from grove.ingest import (
    NotAnExport,
    ReadBudget,
    Upload,
    archive,
    chat_locator,
    message_key,
    spec_for,
)
from grove.ingest.parsers import chatgpt
from grove.ingest.take_in import take_in
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import WorldQuery
from tests.ingest.conftest import STORE_TIME, counts, make_screener, upload_of

T0 = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

THINKING = "MARK-THINKING-7Q2"
RECAP = "MARK-RECAP-4K8"
PROFILE = "MARK-PROFILE-9D1"
INSTRUCTIONS = "MARK-INSTRUCTIONS-3F6"
SYSTEM = "MARK-SYSTEM-2W5"
TOOL_OUTPUT = "MARK-TOOL-OUTPUT-8H3"
BROWSING = "MARK-BROWSING-5J7"
EXTRACTED = "MARK-EXTRACTED-1L4"
INJECTED = "MARK-INJECTED-6P9"
OFF_BRANCH = "MARK-OFF-BRANCH-0R2"
IMAGE_BYTES = "TUFSSy1CQVNFNjQtSU1BR0UtQllURVM="
HTML_PAGE = "MARK-HTML-PAGE-3T8"
SHARED = "MARK-SHARED-LINK-7V1"
DALLE_OUTPUT = "MARK-DALLE-OUTPUT-2Y6"
MARKERS = (
    THINKING,
    RECAP,
    PROFILE,
    INSTRUCTIONS,
    SYSTEM,
    TOOL_OUTPUT,
    BROWSING,
    EXTRACTED,
    INJECTED,
    OFF_BRANCH,
    IMAGE_BYTES,
    HTML_PAGE,
    SHARED,
    DALLE_OUTPUT,
)

INVENTED_NAME = "Quillon Marsh"


def moment(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def epoch(seconds: float) -> float:
    return moment(seconds).timestamp()


def message(
    message_id: str,
    role: str,
    parts: list[Any] | None = None,
    *,
    seconds: float | None,
    content_type: str = "text",
    content: dict[str, Any] | None = None,
    recipient: str = "all",
    metadata: dict[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": role, "name": name, "metadata": {}},
        "create_time": None if seconds is None else epoch(seconds),
        "update_time": None,
        "content": content or {"content_type": content_type, "parts": parts or []},
        "status": "finished_successfully",
        "end_turn": True,
        "weight": 1.0,
        "metadata": metadata or {},
        "recipient": recipient,
        "channel": None,
    }


class Conversation:
    """A conversation tree as the export writes it. add() hangs a node under the last one."""

    def __init__(self, chat_id: str, *, title: str = "Untitled", with_conversation_id=True):
        self.chat_id = chat_id
        self.title = title
        self.with_conversation_id = with_conversation_id
        self.mapping: dict[str, dict[str, Any]] = {}
        self.current: str | None = None
        self.root = self.add(None, parent=None)

    def add(self, body: dict[str, Any] | None, *, parent: str | None | bool = True) -> str:
        node_id = f"{self.chat_id}-node-{len(self.mapping):02d}"
        parent_id = self.current if parent is True else parent
        self.mapping[node_id] = {
            "id": node_id,
            "parent": parent_id,
            "children": [],
            "message": body,
        }
        if parent_id is not None:
            self.mapping[parent_id]["children"].append(node_id)
        self.current = node_id
        return node_id

    def side(self, body: dict[str, Any], *, parent: str) -> str:
        """A branch node that leaves the current node where it was."""
        keep = self.current
        node_id = self.add(body, parent=parent)
        self.current = keep
        return node_id

    def export(self, *, current: object = True) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.chat_id,
            "title": self.title,
            "create_time": epoch(0),
            "update_time": epoch(3600),
            "mapping": copy.deepcopy(self.mapping),
            "current_node": self.current if current is True else current,
            "default_model_slug": "gpt-5",
            "is_archived": False,
        }
        if self.with_conversation_id:
            row["conversation_id"] = self.chat_id
        return row


def lisbon() -> Conversation:
    """A realistic chat: hidden context, reasoning, an image, code runs, browsing, voice, an
    image tool, and an edited and a regenerated branch off the current one."""
    chat = Conversation("c0a1b2c3-0000-4000-8000-00000000a001", title="Lisbon trip")
    chat.add(
        message(
            "m-system",
            "system",
            [SYSTEM],
            seconds=None,
            metadata={"is_visually_hidden_from_conversation": True},
        )
    )
    chat.add(
        message(
            "m-context",
            "user",
            seconds=1,
            content={
                "content_type": "user_editable_context",
                "user_profile": PROFILE,
                "user_instructions": INSTRUCTIONS,
            },
            metadata={"is_visually_hidden_from_conversation": True},
        )
    )
    context = chat.current
    chat.add(message("m-ask", "user", ["Plan two days in Lisbon for me."], seconds=10))
    chat.add(
        message(
            "m-thoughts",
            "assistant",
            seconds=12,
            content={
                "content_type": "thoughts",
                "thoughts": [{"summary": "Planning", "content": THINKING}],
            },
        )
    )
    chat.add(
        message(
            "m-recap",
            "assistant",
            seconds=13,
            content={"content_type": "reasoning_recap", "content": f"Thought for 4s {RECAP}"},
        )
    )
    recap = chat.current
    chat.add(message("m-day-one", "assistant", ["Day one: Alfama and the castle."], seconds=20))
    chat.add(
        message(
            "m-photo",
            "user",
            [
                "Here is the tram map.",
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-T1",
                    "size_bytes": 2048,
                    "width": 512,
                    "height": 512,
                    "data": IMAGE_BYTES,
                },
            ],
            seconds=30,
            content_type="multimodal_text",
            metadata={
                "attachments": [
                    {
                        "id": "file-T1",
                        "name": "tram-map.png",
                        "mime_type": "image/png",
                        "size": 2048,
                        "text": EXTRACTED,
                    }
                ]
            },
        )
    )
    chat.add(
        message(
            "m-code-ok",
            "assistant",
            seconds=40,
            recipient="python",
            content={
                "content_type": "code",
                "language": "unknown",
                "text": "rows = load_map('tram-map.png')\nprint(len(rows))",
            },
        )
    )
    chat.add(
        message(
            "m-out-ok",
            "tool",
            seconds=41,
            name="python",
            content={"content_type": "execution_output", "text": f"412 {TOOL_OUTPUT}"},
            metadata={"aggregate_result": {"status": "success", "code": "rows"}},
        )
    )
    chat.add(
        message(
            "m-code-bad",
            "assistant",
            seconds=42,
            recipient="python",
            content={"content_type": "code", "language": "unknown", "text": "total = 1 / 0"},
        )
    )
    chat.add(
        message(
            "m-out-bad",
            "tool",
            seconds=43,
            name="python",
            content={
                "content_type": "execution_output",
                "text": "Traceback (most recent call last):\n  line 1\n"
                "ZeroDivisionError: division by zero",
            },
            metadata={"aggregate_result": {"status": "failed_with_in_kernel_exception"}},
        )
    )
    chat.add(
        message(
            "m-search",
            "assistant",
            ['{"search_query": [{"q": "lisbon tram 28 hours"}]}'],
            seconds=50,
            recipient="web.run",
        )
    )
    chat.add(
        message(
            "m-browse",
            "tool",
            seconds=51,
            name="web.run",
            content={"content_type": "tether_browsing_display", "result": BROWSING},
        )
    )
    chat.add(
        message(
            "m-quote",
            "tool",
            seconds=52,
            name="myfiles_browser",
            content={
                "content_type": "tether_quote",
                "url": "file-T2",
                "domain": "notes.txt",
                "text": EXTRACTED,
                "title": "notes.txt",
            },
        )
    )
    chat.add(message("m-tram", "assistant", ["Tram 28 runs from early morning."], seconds=60))
    chat.add(
        message(
            "m-injected",
            "user",
            [INJECTED],
            seconds=65,
            metadata={"is_user_system_message": True},
        )
    )
    chat.add(
        message(
            "m-voice-in",
            "user",
            [
                {"content_type": "audio_transcription", "text": "Can you read the plan back?"},
                {"content_type": "audio_asset_pointer", "asset_pointer": "sediment://file-A1"},
            ],
            seconds=70,
            content_type="multimodal_text",
        )
    )
    chat.add(
        message(
            "m-voice-out",
            "assistant",
            [
                {"content_type": "audio_transcription", "text": "Sure. Day one is Alfama."},
                {"content_type": "audio_asset_pointer", "asset_pointer": "sediment://file-A2"},
            ],
            seconds=72,
            content_type="multimodal_text",
        )
    )
    chat.add(
        message(
            "m-dalle",
            "assistant",
            ['{"prompt": "A yellow tram on a hill", "size": "1024x1024"}'],
            seconds=80,
            recipient="dalle.text2im",
        )
    )
    chat.add(
        message(
            "m-dalle-out",
            "tool",
            [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-D1"},
                DALLE_OUTPUT,
            ],
            seconds=81,
            name="dalle.text2im",
            content_type="multimodal_text",
        )
    )
    chat.add(message("m-picture", "assistant", ["Here is the picture."], seconds=85))
    # An edit of the first question and a regenerated first answer: off the current branch.
    edited = chat.side(
        message("m-edit", "user", [f"Plan three days. {OFF_BRANCH}"], seconds=90), parent=context
    )
    chat.side(message("m-edit-answer", "assistant", [OFF_BRANCH], seconds=91), parent=edited)
    chat.side(message("m-regen", "assistant", [OFF_BRANCH], seconds=95), parent=recap)
    return chat


# (speaker_kind, modality, text, seconds) of the Lisbon chat's current branch.
LISBON = [
    ("person", "text", "Plan two days in Lisbon for me.", 10),
    ("ai_host", "text", "Day one: Alfama and the castle.", 20),
    (
        "person",
        "text",
        "Here is the tram map.\n\n[attached an image]\n\n[attached file: tram-map.png]",
        30,
    ),
    ("ai_host", "text", "[tool python] rows = load_map('tram-map.png') print(len(rows))", 40),
    ("ai_host", "text", "[tool python] total = 1 / 0", 42),
    (
        "automated",
        "text",
        "[tool error] Traceback (most recent call last): line 1 "
        "ZeroDivisionError: division by zero",
        43,
    ),
    ("ai_host", "text", '[tool web.run] {"search_query":[{"q":"lisbon tram 28 hours"}]}', 50),
    ("ai_host", "text", "Tram 28 runs from early morning.", 60),
    ("person", "voice", "Can you read the plan back?", 70),
    ("ai_host", "voice", "Sure. Day one is Alfama.", 72),
    ("ai_host", "text", "[tool dalle.text2im] A yellow tram on a hill", 80),
    ("ai_host", "text", "Here is the picture.", 85),
]


def garden() -> Conversation:
    """An older row with no conversation_id: the chat id is the row's id."""
    chat = Conversation(
        "c0a1b2c3-0000-4000-8000-00000000b002", title="Shade herbs", with_conversation_id=False
    )
    chat.add(message("g-ask", "user", ["Which herbs grow in shade?"], seconds=200))
    chat.add(message("g-answer", "assistant", ["Mint and parsley do well."], seconds=210))
    return chat


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
            ("user.json", json.dumps({"id": "user-1", "chatgpt_plus_user": True}).encode()),
            ("chat.html", f"<html><body>{HTML_PAGE}</body></html>".encode()),
            ("message_feedback.json", b"[]"),
            (
                "shared_conversations.json",
                json.dumps([{"id": "s-1", "conversation_id": "c-x", "title": SHARED}]).encode(),
            ),
            ("conversations.json", conversations),
            ("file-T1-tram-map.png", IMAGE_BYTES.encode()),
        ]
    )


def parse(data: bytes) -> list:
    return list(chatgpt.parse(upload_of(data)))


def summary(chat) -> list[tuple[str, str, str, float]]:
    return [
        (m.speaker_kind, m.modality, m.text, (m.at - T0).total_seconds()) for m in chat.messages
    ]


def all_texts(chats) -> list[str]:
    return [m.text for chat in chats for m in chat.messages]


# 1. A realistic export.


def test_spec_is_registered():
    spec = spec_for("chatgpt")
    assert spec is chatgpt.SPEC
    assert spec.connector_type == "past_ai_conversations"
    assert spec.parser_version == "chatgpt.v1"
    assert spec.batch_rule == "session_delta.v1"
    assert spec.key_prefix == "gpt"


def test_a_realistic_export_reads_the_current_branch_in_order():
    chats = parse(as_json(lisbon().export(), garden().export()))

    assert [chat.chat_id for chat in chats] == [
        "c0a1b2c3-0000-4000-8000-00000000a001",
        "c0a1b2c3-0000-4000-8000-00000000b002",
    ]
    trip, herbs = chats
    assert summary(trip) == [(kind, mod, text, float(sec)) for kind, mod, text, sec in LISBON]
    assert summary(herbs) == [
        ("person", "text", "Which herbs grow in shade?", 200.0),
        ("ai_host", "text", "Mint and parsley do well.", 210.0),
    ]
    assert trip.started_at == moment(0)
    assert trip.ended_at == moment(3600)
    for chat in chats:
        for parsed in chat.messages:
            assert parsed.sender is None
            assert parsed.at != STORE_TIME
            assert parsed.text == parsed.text.strip() and parsed.text


def test_the_zip_reads_only_conversations_json():
    bare = parse(as_json(lisbon().export(), garden().export()))
    zipped = parse(export_zip(as_json(lisbon().export(), garden().export())))

    assert [summary(chat) for chat in zipped] == [summary(chat) for chat in bare]
    assert [[m.key for m in chat.messages] for chat in zipped] == [
        [m.key for m in chat.messages] for chat in bare
    ]
    for text in all_texts(zipped):
        assert HTML_PAGE not in text and SHARED not in text and IMAGE_BYTES not in text


def test_a_member_that_only_ends_like_conversations_json_is_not_read():
    # shared_conversations.json ends the same way; even with a conversation's shape it is skipped.
    decoy = Conversation("c0a1b2c3-0000-4000-8000-0000000f0012")
    decoy.add(message("x-ask", "user", [f"A shared copy. {SHARED}"], seconds=1))
    data = as_zip(
        [
            ("shared_conversations.json", as_json(decoy.export())),
            ("conversations.json", as_json(garden().export())),
        ]
    )
    assert [chat.chat_id for chat in parse(data)] == [garden().chat_id]


def test_conversation_id_wins_over_the_row_id():
    row = garden().export()
    row["conversation_id"] = "c0a1b2c3-0000-4000-8000-00000000c003"
    (chat,) = parse(as_json(row))
    assert chat.chat_id == "c0a1b2c3-0000-4000-8000-00000000c003"


def branched() -> Conversation:
    chat = Conversation("c0a1b2c3-0000-4000-8000-00000000d004")
    ask = chat.add(message("b-ask", "user", ["Name a river in Portugal."], seconds=10))
    chat.add(message("b-first", "assistant", [f"The Douro. {OFF_BRANCH}"], seconds=20))
    chat.add(message("b-second", "assistant", ["The Tagus."], seconds=30), parent=ask)
    return chat


@pytest.mark.parametrize("current", [None, "node-that-is-gone"])
def test_a_lost_current_node_reads_the_latest_leaf(current):
    (chat,) = parse(as_json(branched().export(current=current)))
    assert [m.text for m in chat.messages] == ["Name a river in Portugal.", "The Tagus."]


def test_a_parent_cycle_ends_the_walk():
    chat = branched()
    row = chat.export()
    first = next(node for node in row["mapping"].values() if node["parent"] is None)
    first["parent"] = row["current_node"]
    (parsed,) = parse(as_json(row))
    assert [m.text for m in parsed.messages] == ["Name a river in Portugal.", "The Tagus."]


# 2. Planted controls (D5, D14).


def test_planted_text_never_reaches_a_message():
    chats = parse(export_zip(as_json(lisbon().export(), garden().export())))
    texts = all_texts(chats)
    assert texts
    for marker in MARKERS:
        assert all(marker not in text for text in texts), marker


def test_the_assistant_is_never_the_person_and_a_failed_run_is_automated():
    (chat,) = parse(as_json(lisbon().export()))
    by_text = {m.text: m for m in chat.messages}
    for parsed in chat.messages:
        if parsed.text.startswith("[tool error]"):
            assert parsed.speaker_kind == "automated"
        elif parsed.text.startswith("[tool "):
            assert parsed.speaker_kind == "ai_host"
    assert by_text["Day one: Alfama and the castle."].speaker_kind == "ai_host"
    assert by_text["Sure. Day one is Alfama."].speaker_kind == "ai_host"
    assert [m.speaker_kind for m in chat.messages if "Traceback" in m.text] == ["automated"]


def test_a_chat_of_program_text_only_has_no_person_message():
    chat = Conversation("c0a1b2c3-0000-4000-8000-00000000e005")
    chat.add(
        message(
            "p-context",
            "user",
            [INSTRUCTIONS],
            seconds=1,
            metadata={"is_user_system_message": True},
        )
    )
    chat.add(
        message(
            "p-hidden",
            "user",
            [INJECTED],
            seconds=2,
            metadata={"is_visually_hidden_from_conversation": True},
        )
    )
    chat.add(message("p-answer", "assistant", ["Noted."], seconds=3))
    (parsed,) = parse(as_json(chat.export()))
    assert [m.speaker_kind for m in parsed.messages] == ["ai_host"]


def test_an_attachment_is_only_the_fact():
    (chat,) = parse(export_zip(as_json(lisbon().export())))
    photo = next(m for m in chat.messages if m.text.startswith("Here is the tram map."))
    assert "[attached an image]" in photo.text
    assert "[attached file: tram-map.png]" in photo.text
    for text in all_texts([chat]):
        assert IMAGE_BYTES not in text and EXTRACTED not in text and "file-service" not in text


@pytest.mark.parametrize(
    "content_type",
    [
        "user_editable_context",
        "thoughts",
        "reasoning_recap",
        "tether_browsing_display",
        "tether_quote",
        "sonic_webpage",
        "system_error",
    ],
)
def test_a_dropped_content_type_is_dropped_whatever_it_holds(content_type):
    # A quoted file or a fetched page can start with "Error": it is still not a failed run.
    planted = f"Error log of the day. {INJECTED}"
    chat = Conversation("c0a1b2c3-0000-4000-8000-0000000c0009")
    for number, role in enumerate(("user", "assistant", "tool")):
        chat.add(
            message(
                f"d-{role}",
                role,
                seconds=number,
                content={"content_type": content_type, "parts": [planted], "text": planted},
                metadata={"aggregate_result": {"status": "failed"}} if role == "tool" else None,
            )
        )
    chat.add(message("d-answer", "assistant", ["Done."], seconds=9))
    (parsed,) = parse(as_json(chat.export()))
    assert [m.text for m in parsed.messages] == ["Done."]


def test_a_message_with_no_id_takes_the_node_id():
    chat = Conversation("c0a1b2c3-0000-4000-8000-0000000d0010")
    body = message("unused", "user", ["Is the ferry running?"], seconds=5)
    del body["id"]
    node_id = chat.add(body)
    (parsed,) = parse(as_json(chat.export()))
    (only,) = parsed.messages
    assert only.key == message_key("gpt", chat.chat_id, node_id)


def test_a_failed_run_with_no_text_keeps_the_status_code():
    chat = Conversation("c0a1b2c3-0000-4000-8000-00000000f006")
    chat.add(
        message(
            "s-out",
            "tool",
            seconds=5,
            name="python",
            content={"content_type": "execution_output", "text": ""},
            metadata={"aggregate_result": {"status": "failed_with_in_kernel_exception"}},
        )
    )
    chat.add(
        message(
            "s-err",
            "tool",
            ["Error: the sandbox stopped"],
            seconds=6,
            name="browser",
        )
    )
    (parsed,) = parse(as_json(chat.export()))
    assert [(m.speaker_kind, m.text) for m in parsed.messages] == [
        ("automated", "[tool error] failed_with_in_kernel_exception"),
        ("automated", "[tool error] Error: the sandbox stopped"),
    ]


def test_a_long_tool_input_folds_to_one_short_line():
    chat = Conversation("c0a1b2c3-0000-4000-8000-0000000a0007")
    code = "values = [\n" + ",\n".join(f"    {n}" for n in range(200)) + "\n]"
    chat.add(
        message(
            "l-code",
            "assistant",
            seconds=5,
            recipient="python",
            content={"content_type": "code", "language": "python", "text": code},
        )
    )
    chat.add(
        message(
            "l-out",
            "tool",
            seconds=6,
            name="python",
            content={"content_type": "execution_output", "text": "Traceback " + "x" * 400},
        )
    )
    (parsed,) = parse(as_json(chat.export()))
    call, error = parsed.messages
    assert call.text.startswith("[tool python] values = [ 0, 1, 2,")
    assert "\n" not in call.text
    assert len(call.text) == len("[tool python] ") + 120
    assert "\n" not in error.text
    assert len(error.text) == len("[tool error] ") + 200


def test_text_is_cleaned_and_a_blank_message_is_skipped():
    # JSON can spell a lone surrogate; the store refuses one, so it must arrive as U+FFFD.
    chat = Conversation("c0a1b2c3-0000-4000-8000-0000000e0011")
    chat.add(message("w-blank", "user", ["   ", "\n\t"], seconds=1))
    chat.add(message("w-odd", "user", ["Line one\r\nLine two\x00 and \ud800 here"], seconds=2))
    chat.add(message("w-answer", "assistant", ["Fine.\r\n"], seconds=3))
    (parsed,) = parse(as_json(chat.export()))
    assert [m.text for m in parsed.messages] == ["Line one\nLine two and � here", "Fine."]


# 3. Keys.


def test_keys_are_stable_unique_prefixed_and_survive_screening():
    data = export_zip(as_json(lisbon().export(), garden().export()))
    first, second = parse(data), parse(data)
    assert [[m.key for m in c.messages] for c in first] == [
        [m.key for m in c.messages] for c in second
    ]
    screener = Screener(ScreeningConfig())
    for chat in first:
        keys = [m.key for m in chat.messages]
        assert len(set(keys)) == len(keys)
        assert all(re.fullmatch(r"gpt[0-9a-f]{32}", key) for key in keys)
        locator = chat_locator("gpt", chat.chat_id)
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


def test_a_voice_message_has_its_own_key():
    (chat,) = parse(as_json(lisbon().export()))
    voice = next(m for m in chat.messages if m.text == "Can you read the plan back?")
    assert voice.key == message_key("gpt", chat.chat_id, "m-voice-in", "voice")
    ask = next(m for m in chat.messages if m.text == "Plan two days in Lisbon for me.")
    assert ask.key == message_key("gpt", chat.chat_id, "m-ask")


# 4. A grown export.


def grown_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    chat = lisbon()
    before = chat.export()
    chat.add(message("m-more", "user", ["Add a third day, please."], seconds=100))
    chat.add(message("m-more-answer", "assistant", ["Day three: Belem."], seconds=110))
    return before, chat.export()


def test_a_grown_export_keeps_the_old_keys_and_adds_new_ones():
    before, after = grown_pair()
    (old,) = parse(as_json(before))
    (new,) = parse(as_json(after))
    old_keys = [m.key for m in old.messages]
    new_keys = [m.key for m in new.messages]
    assert new_keys[: len(old_keys)] == old_keys
    assert [m.text for m in new.messages[len(old_keys) :]] == [
        "Add a third day, please.",
        "Day three: Belem.",
    ]


def test_one_chat_in_two_files_is_merged_without_a_key_twice():
    before, after = grown_pair()
    data = as_zip(
        [
            ("conversations.json", as_json(before, garden().export())),
            ("part-2/conversations.json", as_json(after)),
        ]
    )
    chats = parse(data)
    assert [chat.chat_id for chat in chats] == [before["conversation_id"], garden().chat_id]
    (expected,) = parse(as_json(after))
    assert [m.key for m in chats[0].messages] == [m.key for m in expected.messages]


def test_one_chat_twice_in_one_file_is_one_chat():
    before, after = grown_pair()
    (chat,) = parse(as_json(before, after))
    (expected,) = parse(as_json(after))
    assert [m.key for m in chat.messages] == [m.key for m in expected.messages]


# 5. Format errors.


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(bytes(range(1, 256)) * 3, id="random-bytes"),
        pytest.param(b"", id="empty-file"),
        pytest.param(as_zip([]), id="empty-zip"),
        pytest.param(as_zip([("chat.html", b"<html></html>")]), id="zip-without-conversations"),
        pytest.param(
            json.dumps(
                [{"uuid": "x-1", "name": "Bees", "chat_messages": [], "created_at": "x"}]
            ).encode(),
            id="claude-ai-export",
        ),
        pytest.param(b'{"conversations": []}', id="json-object"),
        pytest.param(b"[1, 2, 3]", id="json-of-numbers"),
        pytest.param(b'{"type":"user","sessionId":"s-1"}\n', id="json-lines"),
        pytest.param(b"PK\x03\x04 not really a zip", id="broken-zip"),
    ],
)
def test_a_file_of_another_kind_is_not_an_export(data):
    with pytest.raises(NotAnExport):
        parse(data)


def test_a_cut_document_is_not_an_export_and_the_error_holds_no_value():
    data = as_json(lisbon().export(), garden().export())
    cut = data[: len(data) // 2]
    with pytest.raises(NotAnExport) as caught:
        parse(cut)
    assert "Lisbon" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_an_empty_export_yields_no_chat():
    assert parse(b"[]") == []
    assert parse(as_zip([("conversations.json", b"[]")])) == []


# 6. End to end through the store.


def stored_rows(driver, world_id: str) -> list[dict[str, Any]]:
    rows = WorldQuery(driver, world_id).read("MATCH (s:Source) RETURN s.content AS content")
    return [message for row in rows for message in json.loads(row["content"])]


def test_take_in_stores_each_chat_once_and_a_reimport_changes_nothing(
    driver, seeded_world, test_clock
):
    before, after = grown_pair()
    first_upload = export_zip(as_json(before, garden().export()))

    result = take_in(
        driver, seeded_world, "chatgpt", upload_of(first_upload), screener=make_screener()
    )
    assert result.state == "taken_in"
    assert (result.chats, result.sources_added, result.episodes_added) == (2, 2, 2)
    first = counts(driver, seeded_world)
    assert first == {"connectors": 1, "sources": 2, "episodes": 2, "receipts": 2}

    rows = stored_rows(driver, seeded_world)
    assert len(rows) == len(LISBON) + 2
    for row in rows:
        assert row["speaker_kind"] in {"person", "ai_host", "automated"}
        assert row["modality"] in {"text", "voice"}
        assert re.fullmatch(r"gpt[0-9a-f]{32}", row["key"])
    assert {row["modality"] for row in rows} == {"text", "voice"}
    assert all(marker not in row["text"] for row in rows for marker in MARKERS)

    episodes = WorldQuery(driver, seeded_world).read(
        "MATCH (e:Episode) RETURN e.start_time AS start, e.end_time AS end ORDER BY start"
    )
    assert [(row["start"], row["end"]) for row in episodes] == [
        (moment(10), moment(85)),
        (moment(200), moment(210)),
    ]

    again = take_in(
        driver, seeded_world, "chatgpt", upload_of(first_upload), screener=make_screener()
    )
    assert again.state == "nothing_new"
    assert (again.sources_added, again.episodes_added) == (0, 0)
    assert counts(driver, seeded_world) == first

    grown = take_in(
        driver,
        seeded_world,
        "chatgpt",
        upload_of(export_zip(as_json(after, garden().export()))),
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
    assert texts.count("Day three: Belem.") == 1
    assert texts.count("Plan two days in Lisbon for me.") == 1


def named_chat() -> Conversation:
    chat = Conversation("c0a1b2c3-0000-4000-8000-0000000b0008")
    chat.add(message("n-ask", "user", [f"Ask {INVENTED_NAME} about the tram pass."], seconds=10))
    chat.add(message("n-answer", "assistant", ["A day pass costs little."], seconds=20))
    return chat


def test_take_in_drops_a_denylisted_name(driver, seeded_world, test_clock):
    data = as_json(named_chat().export())
    result = take_in(
        driver,
        seeded_world,
        "chatgpt",
        upload_of(data),
        screener=make_screener(denylist=[INVENTED_NAME]),
    )
    assert result.state == "taken_in"
    rows = stored_rows(driver, seeded_world)
    assert [row["text"] for row in rows] == ["A day pass costs little."]
    content = WorldQuery(driver, seeded_world).read("MATCH (s:Source) RETURN s.content AS content")
    assert all("quillon" not in row["content"].lower() for row in content)


def test_take_in_renames_a_name(driver, seeded_world, test_clock):
    data = as_json(named_chat().export())
    take_in(
        driver,
        seeded_world,
        "chatgpt",
        upload_of(data),
        screener=make_screener(rename={INVENTED_NAME: "Friend A"}),
    )
    rows = stored_rows(driver, seeded_world)
    assert rows[0]["text"] == "Ask Friend A about the tram pass."
    assert rows[0]["speaker_kind"] == "person"
    assert all("quillon" not in row["text"].lower() for row in rows)


def test_a_system_message_with_text_and_no_hidden_flag_is_dropped():
    """The role alone drops it: a system message is the program's instruction (D5), whatever its
    metadata says."""
    chat = Conversation("c0a1b2c3-0000-4000-8000-00000000e101")
    chat.add(message("s-plain", "system", [SYSTEM], seconds=1))
    chat.add(message("u-1", "user", ["Plan the Sintra day."], seconds=2))
    chat.add(message("a-1", "assistant", ["Sintra first, then Cascais."], seconds=3))

    (parsed,) = parse(as_json(chat.export()))

    assert [(m.speaker_kind, m.text) for m in parsed.messages] == [
        ("person", "Plan the Sintra day."),
        ("ai_host", "Sintra first, then Cascais."),
    ]
    assert all(SYSTEM not in m.text for m in parsed.messages)


def test_a_document_past_the_value_cap_is_refused_before_it_is_parsed(monkeypatch):
    data = as_json(lisbon().export())
    monkeypatch.setattr(archive, "MAX_DOCUMENT_VALUES", archive.value_bound(data) - 1)
    with pytest.raises(NotAnExport):
        parse(data)
    with pytest.raises(NotAnExport):
        parse(export_zip(data))
    monkeypatch.setattr(archive, "MAX_DOCUMENT_VALUES", archive.value_bound(data))
    assert parse(data)


def test_one_chat_in_two_files_reads_in_its_own_time_order():
    """The later file in the archive holds the earlier part of the chat (D10)."""
    chat_id = "c0a1b2c3-0000-4000-8000-00000000e201"
    later = Conversation(chat_id)
    later.add(message("m-3", "user", ["Book the tram."], seconds=300))
    later.add(message("m-4", "assistant", ["Tram 28 is booked."], seconds=400))
    earlier = Conversation(chat_id)
    earlier.add(message("m-1", "user", ["Plan the morning."], seconds=100))
    earlier.add(message("m-2", "assistant", ["Start at the castle."], seconds=200))
    data = as_zip(
        [
            ("a/conversations.json", as_json(later.export())),
            ("b/conversations.json", as_json(earlier.export())),
        ]
    )

    (chat,) = parse(data)

    assert [m.text for m in chat.messages] == [
        "Plan the morning.",
        "Start at the castle.",
        "Book the tram.",
        "Tram 28 is booked.",
    ]


def test_the_chats_and_messages_of_one_upload_count_against_its_budget():
    first = Conversation("c0a1b2c3-0000-4000-8000-00000000e301")
    first.add(message("q-1", "user", ["Pack the bags."], seconds=1))
    second = Conversation("c0a1b2c3-0000-4000-8000-00000000e302")
    second.add(message("q-2", "user", ["Print the tickets."], seconds=2))
    data = as_json(first.export(), second.export())

    def parse_with(budget):
        return list(chatgpt.parse(Upload(io.BytesIO(data), len(data), budget=budget)))

    assert len(parse_with(ReadBudget(max_chats=2, max_messages=2))) == 2
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_chats=1))
    with pytest.raises(NotAnExport):
        parse_with(ReadBudget(max_messages=1))
