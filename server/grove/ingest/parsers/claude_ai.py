"""The claude.ai export parser (A.4, D5, D14): conversations.json, alone or inside the export zip.

A retried or edited message starts a branch. When the messages carry parent links, only the
chain that ends at the last message is read, so a discarded draft never reaches the store.
Thinking and successful tool results are dropped, and an attachment is kept only as the fact
of it: its extracted text is the file's content, not something said in the chat (D14). The
export's other files (users.json, projects.json) are never opened.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from grove.ingest import archive
from grove.ingest.connector import (
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    Upload,
    clean_text,
    clip,
    in_time_order,
    message_key,
    register,
    utc_from_iso,
)

PROVIDER = "claude_ai"
KEY_PREFIX = "cai"
PARSER_VERSION = "claude_ai.v1"

_DOCUMENT = "conversations.json"
_NOT_AN_EXPORT = "the file is not a claude.ai export"
_BOM = b"\xef\xbb\xbf"
# The input fields that say most about a tool call, best first.
_TELLING = ("description", "command", "file_path", "path", "prompt", "query", "q", "pattern", "url")
_TOOL_INPUT = 120
_TOOL_ERROR = 200
_NAME = 120


def _text_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _base_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _documents(upload: Upload) -> Iterator[bytes]:
    """The bytes of each conversations.json in the upload, one at a time."""
    if archive.is_zip(upload):
        found = False
        for member in archive.zip_members(upload, suffixes=(_DOCUMENT,)):
            if _base_name(member.path) == _DOCUMENT:
                found = True
                data = member.data
                del member
                yield data
                del data
        if not found:
            raise NotAnExport(_NOT_AN_EXPORT)
        return
    data = upload.read_all(archive.MAX_MEMBER_BYTES)
    # A cheap look first, so a large file of another kind is not parsed as JSON.
    if not data.removeprefix(_BOM).lstrip().startswith(b"["):
        raise NotAnExport(_NOT_AN_EXPORT)
    yield data


def _conversations(upload: Upload) -> Iterator[dict[str, Any]]:
    """The conversation rows of every conversations.json in the upload, in file order. One
    document is parsed at a time, and dropped before the next."""
    rows = False
    found = False
    for blob in _documents(upload):
        document = archive.json_document(blob)
        del blob
        if not isinstance(document, list):
            raise NotAnExport(_NOT_AN_EXPORT)
        for row in document:
            rows = True
            if isinstance(row, dict) and _is_conversation(row):
                found = True
                yield row
        del document
    # One odd row does not block the export, but rows with no conversation shape at all are
    # another provider's file.
    if rows and not found:
        raise NotAnExport(_NOT_AN_EXPORT)


def _is_conversation(row: dict[str, Any]) -> bool:
    return isinstance(row.get("uuid"), str) and isinstance(row.get("chat_messages"), list)


def _thread(records: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, dict[str, Any]]]:
    """The records of the chat's current branch, oldest first, each with its list position."""
    if not records or not any(
        isinstance(record.get("parent_message_uuid"), str) for _, record in records
    ):
        return records
    by_uuid: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, record in records:
        uuid = _text_id(record.get("uuid"))
        if uuid is not None:
            by_uuid.setdefault(uuid, (position, record))
    chain = [records[-1]]
    seen: set[str] = set()
    while True:
        record = chain[-1][1]
        uuid = _text_id(record.get("uuid"))
        if uuid is not None:
            seen.add(uuid)
        parent = _text_id(record.get("parent_message_uuid"))
        # A parent outside the list is the export's root marker; a seen one would loop.
        if parent is None or parent in seen or parent not in by_uuid:
            break
        chain.append(by_uuid[parent])
    chain.reverse()
    return chain


def _join(pieces: list[object]) -> str:
    cleaned = (clean_text(piece).strip() for piece in pieces)
    return "\n\n".join(piece for piece in cleaned if piece)


def _block_text(value: object) -> str:
    """The text of a tool result's content: a string, or its text blocks."""
    if isinstance(value, str):
        return clean_text(value).strip()
    if not isinstance(value, list):
        return ""
    return _join(
        [
            block.get("text")
            for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    )


def _telling(value: object) -> str:
    """The most telling part of a tool call's input, on one line of at most 120 characters."""
    if isinstance(value, dict):
        for name in _TELLING:
            field = value.get(name)
            if isinstance(field, str) and field.strip():
                return clip(clean_text(field), _TOOL_INPUT)
    if isinstance(value, str):
        return clip(clean_text(value), _TOOL_INPUT)
    if value is None:
        return ""
    compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return clip(clean_text(compact), _TOOL_INPUT)


def _tool_name(block: dict[str, Any]) -> str:
    return clip(clean_text(block.get("name")), _NAME) or "unknown"


def _attachments(record: dict[str, Any]) -> list[object]:
    """One line per attachment and file. extracted_content is never read: it is the file."""
    lines: list[object] = []
    for field in ("attachments", "files"):
        items = record.get(field)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = clip(clean_text(item.get("file_name")), _NAME)
            lines.append(f"[attached file: {name}]" if name else "[attached a file]")
    return lines


def _content_blocks(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The content list, or None when there is none and the flat text stands for it."""
    content = record.get("content")
    if not isinstance(content, list) or not content:
        return None
    return [block for block in content if isinstance(block, dict)]


def _messages(chat_id: str, position: int, record: dict[str, Any]) -> list[ParsedMessage]:
    sender = record.get("sender")
    # A record with no uuid falls back to its list place, which a grown export keeps.
    uuid = _text_id(record.get("uuid")) or f"#{position}"
    at = utc_from_iso(record.get("created_at"))
    blocks = _content_blocks(record)

    def out(text: str, speaker_kind: str) -> list[ParsedMessage]:
        if not text:
            return []
        key = message_key(KEY_PREFIX, chat_id, uuid, speaker_kind)
        return [ParsedMessage(key, text, at, speaker_kind)]

    if sender == "human":
        if blocks is None:
            pieces: list[object] = [record.get("text")]
        else:
            pieces = [block.get("text") for block in blocks if block.get("type") == "text"]
        return out(_join(pieces + _attachments(record)), "person")
    if sender != "assistant":
        return []
    if blocks is None:
        return out(_join([record.get("text")]), "ai_host")
    spoken: list[object] = []
    errors: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            spoken.append(block.get("text"))
        elif kind == "tool_use":
            spoken.append(f"[tool {_tool_name(block)}] {_telling(block.get('input'))}")
        elif kind == "tool_result" and block.get("is_error") is True:
            detail = _block_text(block.get("content")) or _tool_name(block)
            errors.append(f"[tool error] {clip(detail, _TOOL_ERROR)}")
        # Thinking, successful tool results and any other block are not speech (D5).
    return out(_join(spoken), "ai_host") + out("\n".join(errors), "automated")


class _Chat:
    """One chat as it builds up over the upload's files, with no key twice."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.messages: list[ParsedMessage] = []
        self.keys: set[str] = set()
        self.started: datetime | None = None
        self.ended: datetime | None = None

    def add(
        self, messages: list[ParsedMessage], started: datetime | None, ended: datetime | None
    ) -> None:
        for message in messages:
            if message.key not in self.keys:
                self.keys.add(message.key)
                self.messages.append(message)
        if started is not None and (self.started is None or started < self.started):
            self.started = started
        if ended is not None and (self.ended is None or ended > self.ended):
            self.ended = ended

    def parsed(self) -> ParsedChat:
        # One chat in two files joins in file order: sort by time (D10).
        messages = tuple(in_time_order(self.messages))
        return ParsedChat(self.chat_id, messages, self.started, self.ended)


def parse(upload: Upload) -> Iterator[ParsedChat]:
    """One ParsedChat per conversation, in the order the export first lists them."""
    chats: dict[str, _Chat] = {}
    for conversation in _conversations(upload):
        chat_id = conversation["uuid"]
        if not chat_id:
            continue
        records = [
            (position, record)
            for position, record in enumerate(conversation["chat_messages"])
            if isinstance(record, dict)
        ]
        messages: list[ParsedMessage] = []
        for position, record in _thread(records):
            messages.extend(_messages(chat_id, position, record))
        chat = chats.setdefault(chat_id, _Chat(chat_id))
        chat.add(
            messages,
            utc_from_iso(conversation.get("created_at")),
            utc_from_iso(conversation.get("updated_at")),
        )
    for chat in chats.values():
        yield chat.parsed()


SPEC = register(
    ConnectorSpec(
        provider=PROVIDER,
        connector_type="past_ai_conversations",
        parser_version=PARSER_VERSION,
        batch_rule="session_delta.v1",
        key_prefix=KEY_PREFIX,
        parse=parse,
    )
)
