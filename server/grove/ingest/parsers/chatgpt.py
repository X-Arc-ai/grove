"""The ChatGPT export parser (A.4, D5, D14): conversations.json, alone or inside the export zip.

A conversation is a tree, because an edit or a regenerated answer starts a new branch. Only the
branch the person last saw is read, from current_node back to the root, so a discarded draft
never reaches the store. Custom instructions, hidden and system messages, reasoning, browsing
output and successful tool output are dropped: they are program text, not what the person or
the assistant said. The export's other files (chat.html, user.json, images) are never opened.
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
    bounded_id,
    clean_text,
    clip,
    in_time_order,
    message_key,
    register,
    utc_from_epoch,
)

PROVIDER = "chatgpt"
KEY_PREFIX = "gpt"
PARSER_VERSION = "chatgpt.v1"

_DOCUMENT = "conversations.json"
_NOT_AN_EXPORT = "the file is not a ChatGPT export"
_BOM = b"\xef\xbb\xbf"

# Reasoning, custom instructions, browsing and quote displays, and error banners: never speech.
_DROPPED_TYPES = frozenset(
    {
        "user_editable_context",
        "thoughts",
        "reasoning_recap",
        "tether_browsing_display",
        "tether_quote",
        "sonic_webpage",
        "system_error",
    }
)
_SPOKEN_TYPES = frozenset({"text", "multimodal_text"})
# The input fields that say most about a tool call, best first.
_TELLING = ("description", "command", "file_path", "path", "prompt", "query", "q", "pattern", "url")
_TOOL_INPUT = 120
_TOOL_ERROR = 200
_NAME = 120
_ALL = "all"


def _text_id(value: object) -> str | None:
    return bounded_id(value) if isinstance(value, str) and value else None


def _base_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _documents(upload: Upload) -> Iterator[bytes]:
    """The bytes of each conversations.json in the upload, one at a time."""
    if archive.is_zip(upload):
        found = False
        for member in archive.zip_members(upload, suffixes=(_DOCUMENT,)):
            # The base name must match whole: shared_conversations.json ends the same way but
            # holds only share links, never a conversation.
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
    return isinstance(row.get("mapping"), dict)


def _created(node: object) -> float:
    message = node.get("message") if isinstance(node, dict) else None
    value = message.get("create_time") if isinstance(message, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return float("-inf")
    return float(value)


def _latest_leaf(mapping: dict[str, Any]) -> str | None:
    """The fallback thread end when current_node is lost: the leaf written last."""
    best: str | None = None
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        children = node.get("children")
        if isinstance(children, list) and any(
            isinstance(child, str) and isinstance(mapping.get(child), dict) for child in children
        ):
            continue
        if best is None or _created(node) > _created(mapping[best]):
            best = node_id
    return best


def _thread(mapping: dict[str, Any], current: object) -> list[tuple[str, dict[str, Any]]]:
    """The nodes from the root to the current node. A cycle or a lost parent ends the walk."""
    node_id = (
        current if isinstance(current, str) and isinstance(mapping.get(current), dict) else None
    )
    if node_id is None:
        node_id = _latest_leaf(mapping)
    path: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    while isinstance(node_id, str) and node_id not in seen:
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        seen.add(node_id)
        path.append((node_id, node))
        node_id = node.get("parent")
    path.reverse()
    return path


def _join(pieces: list[object]) -> str:
    cleaned = (clean_text(piece).strip() for piece in pieces)
    return "\n\n".join(piece for piece in cleaned if piece)


def _parts(content: dict[str, Any]) -> list[Any]:
    parts = content.get("parts")
    return parts if isinstance(parts, list) else []


def _content_text(content: dict[str, Any]) -> str:
    """The plain text of a content block: its string parts, or its text field (code, output)."""
    strings = [part for part in _parts(content) if isinstance(part, str)]
    if strings:
        return _join(strings)
    return clean_text(content.get("text")).strip()


def _telling(body: str) -> str:
    """The most telling part of a tool call's input, on one line of at most 120 characters."""
    text = body.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, dict):
            for name in _TELLING:
                field = value.get(name)
                if isinstance(field, str) and field.strip():
                    return clip(clean_text(field), _TOOL_INPUT)
            compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
            return clip(clean_text(compact), _TOOL_INPUT)
    return clip(text, _TOOL_INPUT)


def _spoken(content: dict[str, Any], *, images: bool) -> tuple[list[object], list[object]]:
    """The text pieces and the voice transcripts of a text or multimodal message, in order."""
    pieces: list[object] = []
    voice: list[object] = []
    for part in _parts(content):
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, dict):
            kind = part.get("content_type")
            if kind == "audio_transcription":
                voice.append(part.get("text"))
            elif kind == "image_asset_pointer" and images:
                # The fact of the image only: its bytes live in another file that is not read.
                pieces.append("[attached an image]")
    return pieces, voice


def _attachments(metadata: dict[str, Any]) -> list[object]:
    lines: list[object] = []
    attachments = metadata.get("attachments")
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, dict):
            continue
        name = clip(clean_text(attachment.get("name")), _NAME)
        lines.append(f"[attached file: {name}]" if name else "[attached a file]")
    return lines


class _Reader:
    """Reads one message of one conversation into zero, one or two parsed messages."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id

    def read(self, node_id: str, node: dict[str, Any]) -> list[ParsedMessage]:
        message = node.get("message")
        if not isinstance(message, dict):
            return []
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        content = message.get("content")
        content = content if isinstance(content, dict) else {}
        content_type = content.get("content_type")
        if (
            role == "system"
            or metadata.get("is_visually_hidden_from_conversation")
            or metadata.get("is_user_system_message")
            or content_type in _DROPPED_TYPES
        ):
            return []
        message_id = _text_id(message.get("id")) or bounded_id(node_id)
        at = utc_from_epoch(message.get("create_time"))
        if role == "user":
            return self._user(message_id, at, content, content_type, metadata)
        if role == "assistant":
            return self._assistant(message_id, at, message, content, content_type)
        if role == "tool":
            return self._tool(message_id, at, content, metadata)
        return []

    def _out(
        self,
        parts: tuple[object, ...],
        text: str,
        at: datetime | None,
        speaker_kind: str,
        modality: str = "text",
    ) -> list[ParsedMessage]:
        if not text:
            return []
        key = message_key(KEY_PREFIX, self.chat_id, *parts)
        return [ParsedMessage(key, text, at, speaker_kind, modality)]

    def _user(
        self,
        message_id: str,
        at: datetime | None,
        content: dict[str, Any],
        content_type: object,
        metadata: dict[str, Any],
    ) -> list[ParsedMessage]:
        if content_type not in _SPOKEN_TYPES:
            return []
        pieces, voice = _spoken(content, images=True)
        pieces.extend(_attachments(metadata))
        return self._out((message_id,), _join(pieces), at, "person") + self._out(
            (message_id, "voice"), _join(voice), at, "person", "voice"
        )

    def _assistant(
        self,
        message_id: str,
        at: datetime | None,
        message: dict[str, Any],
        content: dict[str, Any],
        content_type: object,
    ) -> list[ParsedMessage]:
        recipient = _text_id(message.get("recipient")) or _ALL
        if recipient == _ALL and content_type in _SPOKEN_TYPES:
            pieces, voice = _spoken(content, images=False)
            return self._out((message_id,), _join(pieces), at, "ai_host") + self._out(
                (message_id, "voice"), _join(voice), at, "ai_host", "voice"
            )
        if recipient == _ALL and content_type != "code":
            return []
        name = recipient if recipient != _ALL else _text_id(content.get("language")) or "code"
        body = _telling(_content_text(content))
        digest = f"[tool {clip(clean_text(name), _NAME)}] {body}".strip()
        return self._out((message_id,), digest, at, "ai_host")

    def _tool(
        self,
        message_id: str,
        at: datetime | None,
        content: dict[str, Any],
        metadata: dict[str, Any],
    ) -> list[ParsedMessage]:
        aggregate = metadata.get("aggregate_result")
        status = aggregate.get("status") if isinstance(aggregate, dict) else None
        text = _content_text(content)
        failed = (isinstance(status, str) and status != "success") or text.startswith(
            ("Traceback", "Error")
        )
        if not failed:
            # Successful tool output is the program's, and it is never kept (D5).
            return []
        detail = text or (clean_text(status) if isinstance(status, str) else "")
        digest = f"[tool error] {clip(detail, _TOOL_ERROR)}".strip()
        return self._out((message_id,), digest, at, "automated")


class _Chat:
    """One chat as it builds up over the upload's files, with no key twice."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.messages: list[ParsedMessage] = []
        self.keys: set[str] = set()
        self.started: datetime | None = None
        self.ended: datetime | None = None

    def add(
        self,
        messages: list[ParsedMessage],
        started: datetime | None,
        ended: datetime | None,
        upload: Upload,
    ) -> None:
        for message in messages:
            if message.key not in self.keys:
                upload.budget.take_message(len(message.text))
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
        chat_id = _text_id(conversation.get("conversation_id")) or _text_id(conversation.get("id"))
        if chat_id is None:
            continue
        reader = _Reader(chat_id)
        messages: list[ParsedMessage] = []
        for node_id, node in _thread(conversation["mapping"], conversation.get("current_node")):
            messages.extend(reader.read(node_id, node))
        chat = chats.get(chat_id)
        if chat is None:
            upload.budget.take_chat()
            chat = chats[chat_id] = _Chat(chat_id)
        chat.add(
            messages,
            utc_from_epoch(conversation.get("create_time")),
            utc_from_epoch(conversation.get("update_time")),
            upload,
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
