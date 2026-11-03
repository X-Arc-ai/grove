"""Claude Code sessions (A.4, D5): one `.jsonl` session file, or a zip that holds many.

Each line of a session file is one record. A chat is one session id, so a subagent file under
`<session>/subagents/` joins its parent's chat. The key of a message is the session id, the
record's uuid and the speaker kind: one record gives at most one message per speaker kind.

What stays (D5, D14): the person's own words and the fact of an attachment, the assistant's
visible text, a one-line digest of each tool call and of each failed tool result. Thinking,
successful tool output and every text a program put in the person's slot are dropped.
"""

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
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

PROVIDER = "claude_code"
KEY_PREFIX = "cc"
PARSER_VERSION = "claude_code.v1"

PERSON = "person"
AI_HOST = "ai_host"
AUTOMATED = "automated"

_SUFFIXES = (".jsonl",)
# Record types a Claude Code session file holds. Only user and assistant records give messages.
_KNOWN_TYPES = frozenset(
    (
        "user",
        "assistant",
        "summary",
        "system",
        "file-history-snapshot",
        "queue-operation",
        "progress",
    )
)
_NO_SESSION_TYPES = frozenset({"summary", "file-history-snapshot"})
_TOOL_DIGEST = 120
_ERROR_DIGEST = 200
_NAME_LIMIT = 64
# The input fields that say most about a tool call, best first.
_TELLING_INPUTS = (
    "description",
    "command",
    "file_path",
    "notebook_path",
    "prompt",
    "query",
    "pattern",
    "url",
)
_INTERRUPTED = frozenset(
    {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}
)


def _replace_spans(text: str, tag: str, replace: Callable[[str], str]) -> str:
    """Each `<tag>...</tag>` span, leftmost first and up to the nearest closer, replaced by
    `replace(inner text)`. One pass: an opener with no closer after it ends the search, since no
    later opener has one either. (A lazy `<tag>.*?</tag>` regex scans to the end of the text from
    every unclosed opener, which made a crafted line cost quadratic time.)"""
    opener, closer = f"<{tag}>", f"</{tag}>"
    out: list[str] = []
    position = 0
    while True:
        start = text.find(opener, position)
        if start < 0:
            break
        end = text.find(closer, start + len(opener))
        if end < 0:
            break
        out.append(text[position:start])
        out.append(replace(text[start + len(opener) : end]))
        position = end + len(closer)
    if not out:
        return text
    out.append(text[position:])
    return "".join(out)


def _cut_spans(text: str, tag: str) -> str:
    return _replace_spans(text, tag, lambda _inner: "")


def _first_inner(text: str, tag: str) -> str | None:
    """The text inside the first closed `<tag>...</tag>` span, or None."""
    opener, closer = f"<{tag}>", f"</{tag}>"
    start = text.find(opener)
    if start < 0:
        return None
    end = text.find(closer, start + len(opener))
    return None if end < 0 else text[start + len(opener) : end]


# Text a program put inside the person's message: cut out whole.
_INJECTED_TAGS = (
    "system-reminder",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "bash-stdout",
    "bash-stderr",
    "command-message",
)
_TOOL_ERROR_TAG = re.compile(r"</?tool_use_error>")


def _person_text(raw: str) -> str:
    """The person's own words in one text part: injected spans cut, a command call as `/x a b`,
    a shell escape as `! cmd`. A part that is only program output or an interrupt mark is empty."""
    text = clean_text(raw)
    for tag in _INJECTED_TAGS:
        text = _cut_spans(text, tag)
    name = _first_inner(text, "command-name")
    if name is not None:
        command = name.strip()
        if command and not command.startswith("/"):
            command = "/" + command
        args = _first_inner(text, "command-args")
        call = " ".join(part for part in (command, args.strip() if args else "") if part)
        rest = _cut_spans(_cut_spans(text, "command-name"), "command-args").strip()
        text = "\n\n".join(part for part in (call, rest) if part)
    text = _replace_spans(text, "bash-input", lambda inner: "! " + inner.strip()).strip()
    return "" if text in _INTERRUPTED else text


def _tool_digest(block: dict[str, Any]) -> str:
    name = block.get("name")
    name = clip(clean_text(name), _NAME_LIMIT) if isinstance(name, str) else ""
    payload = block.get("input")
    shown = ""
    if isinstance(payload, dict):
        for field_name in _TELLING_INPUTS:
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                shown = value
                break
    if not shown and payload is not None:
        shown = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"[tool {name or 'tool'}] {clip(clean_text(shown), _TOOL_DIGEST)}".rstrip()


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _error_digest(block: dict[str, Any]) -> str:
    # A tool result can carry a reminder the program added to it: no tool wrote that text.
    text = _cut_spans(clean_text(_result_text(block.get("content"))), "system-reminder")
    text = _TOOL_ERROR_TAG.sub("", text)
    return f"[tool error] {clip(text, _ERROR_DIGEST)}".rstrip()


def _blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _user_parts(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(the slot's text parts in block order, the failed tool result digests)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    parts: list[str] = []
    errors: list[str] = []
    for block in _blocks(content):
        kind = block.get("type")
        if kind == "text":
            text = _person_text(block.get("text")) if isinstance(block.get("text"), str) else ""
            if text:
                parts.append(text)
        elif kind == "image":
            parts.append("[attached an image]")
        elif kind == "document":
            parts.append("[attached a document]")
        elif kind == "tool_result" and block.get("is_error") is True:
            errors.append(_error_digest(block))
    return parts, errors


def _slot_speaker(record: dict[str, Any]) -> str | None:
    """Who wrote the text in a user record's slot, or None when a program injected it."""
    if record.get("isMeta") is True or record.get("isCompactSummary") is True:
        return None
    if record.get("userType") != "external":
        return None
    if record.get("isSidechain") is True:
        # The main agent wrote a subagent's prompt.
        return AI_HOST
    entrypoint = record.get("entrypoint")
    if isinstance(entrypoint, str) and entrypoint.startswith("sdk"):
        # A program ran claude (for example `claude -p`): no person typed this (D5).
        return AUTOMATED
    return PERSON


def _assistant_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts: list[str] = []
    digests: list[str] = []
    for block in _blocks(content):
        kind = block.get("type")
        if kind == "text":
            text = clean_text(block.get("text")).strip()
            if text:
                texts.append(text)
        elif kind == "tool_use":
            digests.append(_tool_digest(block))
        # thinking and redacted_thinking are dropped: the assistant never showed them.
    return "\n\n".join(part for part in (*texts, "\n".join(digests)) if part)


def _messages_of(record: dict[str, Any]) -> list[tuple[str, str]]:
    """(speaker kind, text) for each message one record gives, in order."""
    kind = record.get("type")
    if kind == "assistant":
        text = _assistant_text(record)
        return [(AI_HOST, text)] if text else []
    if kind != "user":
        return []
    parts, errors = _user_parts(record)
    error_text = "\n".join(errors)
    speaker = _slot_speaker(record)
    text = "\n\n".join(parts).strip() if speaker is not None else ""
    if speaker == AUTOMATED and text and error_text:
        # One record gives one message per speaker kind, so a program's prompt and a failed
        # result in the same record share one message; two would share one key.
        return [(AUTOMATED, f"{error_text}\n\n{text}")]
    out: list[tuple[str, str]] = []
    if error_text:
        out.append((AUTOMATED, error_text))
    if speaker is not None and text:
        out.append((speaker, text))
    return out


@dataclass
class _Chat:
    chat_id: str
    messages: list[ParsedMessage] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    times: list[datetime] = field(default_factory=list)

    def add(self, record: dict[str, Any], uuid: str, at: datetime | None) -> None:
        for speaker, text in _messages_of(record):
            key = message_key(KEY_PREFIX, self.chat_id, uuid, speaker)
            if key in self.keys:
                # The same record in two files of one upload.
                continue
            self.keys.add(key)
            self.messages.append(ParsedMessage(key=key, text=text, at=at, speaker_kind=speaker))

    def parsed(self) -> ParsedChat:
        # A subagent file and its parent's file join one chat in archive order: sort by time.
        return ParsedChat(
            chat_id=self.chat_id,
            messages=tuple(in_time_order(self.messages)),
            started_at=min(self.times) if self.times else None,
            ended_at=max(self.times) if self.times else None,
        )


def _is_record(value: object) -> bool:
    if not isinstance(value, dict) or value.get("type") not in _KNOWN_TYPES:
        return False
    return isinstance(value.get("sessionId"), str) or value["type"] in _NO_SESSION_TYPES


def _records(data: bytes, upload: Upload) -> Iterator[dict[str, Any]]:
    """The file's Claude Code records in line order, one at a time."""
    for _, value in archive.json_lines(data, budget=upload.budget):
        if _is_record(value):
            yield value


def _files(upload: Upload) -> Iterable[bytes]:
    if archive.is_zip(upload):
        return (member.data for member in archive.zip_members(upload, suffixes=_SUFFIXES))
    return [upload.read_all(archive.MAX_MEMBER_BYTES)]


def parse(upload: Upload) -> Iterator[ParsedChat]:
    chats: dict[str, _Chat] = {}
    recognised = False
    for data in _files(upload):
        for record in _records(data, upload):
            recognised = True
            session = record.get("sessionId")
            uuid = record.get("uuid")
            if not isinstance(session, str) or not session or not isinstance(uuid, str) or not uuid:
                continue
            chat = chats.setdefault(session, _Chat(session))
            at = utc_from_iso(record.get("timestamp"))
            if at is not None:
                chat.times.append(at)
            chat.add(record, uuid, at)
    if not recognised:
        raise NotAnExport("the file is not a Claude Code session export")
    for chat in chats.values():
        if chat.messages:
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
