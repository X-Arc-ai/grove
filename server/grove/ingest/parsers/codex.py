"""Codex rollouts (A.4, D5): one `.jsonl` rollout, one `.jsonl.zst`, or a zip that holds many.

A chat is one session id (the session meta's id). The key of a message is the session id and the
line number in the decompressed rollout, so a rollout that grows keeps the keys of its old lines.

Two line formats exist. The current one wraps each line as {timestamp, type, payload}; the older
one starts with a bare session meta and has bare response items with no time after it.

What stays (D5, D14): the person's own words and the fact of an image, the assistant's visible
text, a one-line digest of each tool call and of each failed tool output. Reasoning, successful
tool output, developer and system messages and the instruction blocks Codex puts in the person's
slot are dropped. Event lines repeat the response items, so they are dropped too.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
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
    message_key,
    register,
    utc_from_iso,
)

PROVIDER = "codex"
KEY_PREFIX = "cx"
PARSER_VERSION = "codex.v1"

PERSON = "person"
AI_HOST = "ai_host"
AUTOMATED = "automated"

_SUFFIXES = (".jsonl", ".jsonl.zst")
_WRAPPED_TYPES = frozenset(
    ("session_meta", "response_item", "event_msg", "turn_context", "compacted")
)
_ITEM_TYPES = frozenset(
    (
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "local_shell_call",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
    )
)
_CALL_TYPES = frozenset(
    ("function_call", "local_shell_call", "custom_tool_call", "web_search_call")
)
_OUTPUT_TYPES = frozenset(("function_call_output", "custom_tool_call_output"))
# Blocks Codex itself puts in the person's message: instructions and context, never typed.
_INJECTED_PREFIXES = (
    "<user_instructions>",
    "<environment_context>",
    "<user_shell_command>",
    "# AGENTS.md instructions",
)
_PROGRAM_ORIGINATORS = ("codex_exec", "codex_sdk")
_PROGRAM_SOURCES = frozenset(("exec", "mcp"))
_FAILED_PREFIXES = ("error", "failed", "aborted")
_EXIT_CODE = re.compile(r"\A\s*Exit code:\s*(-?\d+)")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
_TOOL_DIGEST = 120
_ERROR_DIGEST = 200
_NAME_LIMIT = 64
# The argument fields that say most about a tool call, best first, after a shell command.
_TELLING_INPUTS = (
    "description",
    "file_path",
    "prompt",
    "query",
    "pattern",
    "url",
)


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _command(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return " ".join(value)
    return ""


def _patch_files(patch: object) -> str:
    """The files a patch names, so a digest shows the files and not the patch body."""
    return ", ".join(_PATCH_FILE.findall(patch)) if isinstance(patch, str) else ""


def _arguments_shown(name: str, arguments: object) -> str:
    parsed: object = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = arguments
    if not isinstance(parsed, dict):
        if name == "apply_patch":
            return _patch_files(parsed) or _command(parsed)
        return _command(parsed) or ("" if parsed is None else _compact(parsed))
    if name == "apply_patch":
        patch = parsed.get("input", parsed.get("patch"))
        shown = _patch_files(patch)
        if shown:
            return shown
    shown = _command(parsed.get("command"))
    if shown.strip():
        return shown
    for field_name in _TELLING_INPUTS:
        value = parsed.get(field_name)
        if isinstance(value, str) and value.strip():
            return value
    return _compact(parsed)


def _call_digest(item: dict[str, Any]) -> str:
    kind = item.get("type")
    name = item.get("name")
    name = name if isinstance(name, str) and name.strip() else ""
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    if kind == "local_shell_call":
        name = name or "local_shell"
        shown = _command(action.get("command"))
    elif kind == "web_search_call":
        name = name or "web_search"
        found = (action.get(key) for key in ("query", "url"))
        shown = next((value for value in found if isinstance(value, str) and value.strip()), "")
        shown = shown or (_compact(action) if action else "")
    elif kind == "custom_tool_call":
        raw = item.get("input")
        shown = (_patch_files(raw) if name == "apply_patch" else "") or _command(raw)
    else:
        shown = _arguments_shown(name, item.get("arguments"))
    label = clip(clean_text(name), _NAME_LIMIT) or "tool"
    return f"[tool {label}] {clip(clean_text(shown), _TOOL_DIGEST)}".rstrip()


def _exit_code(value: dict[str, Any]) -> object:
    metadata = value.get("metadata")
    code = metadata.get("exit_code") if isinstance(metadata, dict) else None
    return value.get("exit_code") if code is None else code


def _failure(output: object) -> str | None:
    """The text of a failed tool output, or None when the tool did not fail. An explicit exit
    code or success flag decides first; the text's own words decide only when neither is there,
    so a successful output that starts with "Error" is still dropped."""
    value: object = output
    if isinstance(output, str) and output.lstrip().startswith("{"):
        try:
            value = json.loads(output)
        except ValueError:
            value = output
    text = output if isinstance(output, str) else ""
    if isinstance(value, dict):
        inner = value.get("content", value.get("output"))
        text = inner if isinstance(inner, str) else ""
        code = _exit_code(value)
        has_code = isinstance(code, int) and not isinstance(code, bool)
        if value.get("success") is False or (has_code and code != 0):
            return text
        if has_code or value.get("success") is True:
            return None
    exit_line = _EXIT_CODE.match(text)
    if exit_line is not None:
        return text if int(exit_line.group(1)) != 0 else None
    return text if text.lstrip().lower().startswith(_FAILED_PREFIXES) else None


def _user_text(content: object) -> str:
    blocks: list[object] = [{"type": "input_text", "text": content}]
    if isinstance(content, list):
        blocks = content
    elif not isinstance(content, str):
        blocks = []
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "input_image":
            parts.append("[attached an image]")
        elif block.get("type") == "input_text":
            text = clean_text(block.get("text")).strip()
            if text and not text.startswith(_INJECTED_PREFIXES):
                parts.append(text)
    return "\n\n".join(parts)


def _assistant_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    texts = (
        clean_text(block.get("text")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text"
    )
    return "\n\n".join(text for text in texts if text)


def _item_message(item: dict[str, Any], slot: str) -> tuple[str, str] | None:
    """(speaker kind, text) of one response item, or None when it gives no message."""
    kind = item.get("type")
    if kind == "message":
        role = item.get("role")
        if role == "user":
            text = _user_text(item.get("content"))
            return (slot, text) if text else None
        if role == "assistant":
            text = _assistant_text(item.get("content"))
            return (AI_HOST, text) if text else None
        # developer and system messages are instructions to the model.
        return None
    if kind in _CALL_TYPES:
        return AI_HOST, _call_digest(item)
    if kind in _OUTPUT_TYPES:
        failed = _failure(item.get("output"))
        if failed is None:
            return None
        return AUTOMATED, f"[tool error] {clip(clean_text(failed), _ERROR_DIGEST)}".rstrip()
    # reasoning and anything unknown is dropped.
    return None


@dataclass
class _Rollout:
    """One rollout file: its chat id, who ran it, and its response items with line numbers."""

    chat_id: str
    program_run: bool
    times: list[datetime]
    items: list[tuple[int, datetime | None, dict[str, Any]]]


def _first_line(data: bytes) -> bytes:
    for raw in data.removeprefix(b"\xef\xbb\xbf").split(b"\n"):
        if raw.strip():
            return raw.strip()
    return b""


def _program_run(meta: dict[str, Any]) -> bool:
    originator = meta.get("originator")
    source = meta.get("source")
    return (isinstance(originator, str) and originator.startswith(_PROGRAM_ORIGINATORS)) or (
        isinstance(source, str) and source in _PROGRAM_SOURCES
    )


def _is_wrapped(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") in _WRAPPED_TYPES
        and isinstance(value.get("payload"), dict)
    )


def _is_old_meta(value: object) -> bool:
    return (
        isinstance(value, dict)
        and "type" not in value
        and "record_type" not in value
        and isinstance(value.get("id"), str)
        # A real session meta carries its start time; any JSON with an id field is no rollout.
        and utc_from_iso(value.get("timestamp")) is not None
    )


def _rollout(data: bytes) -> _Rollout | None:
    """The rollout in one decompressed file, or None when the file is not a Codex rollout."""
    lines = list(archive.json_lines(data))
    meta: dict[str, Any] | None = None
    times: list[datetime] = []
    items: list[tuple[int, datetime | None, dict[str, Any]]] = []
    if any(_is_wrapped(value) for _, value in lines):
        for number, value in lines:
            if not _is_wrapped(value):
                continue
            at = utc_from_iso(value.get("timestamp"))
            if at is not None:
                times.append(at)
            payload = value["payload"]
            if value["type"] == "session_meta" and meta is None:
                meta = payload
            elif value["type"] == "response_item":
                items.append((number, at, payload))
    elif lines and _is_old_meta(lines[0][1]):
        meta = lines[0][1]
        items = [
            (number, None, value)
            for number, value in lines[1:]
            if isinstance(value, dict) and value.get("type") in _ITEM_TYPES
        ]
    else:
        return None
    meta = meta or {}
    started = utc_from_iso(meta.get("timestamp"))
    if started is not None:
        times.append(started)
    chat_id = meta.get("id")
    if not isinstance(chat_id, str) or not chat_id:
        chat_id = "first-line-" + hashlib.sha256(_first_line(data)).hexdigest()
    return _Rollout(chat_id=chat_id, program_run=_program_run(meta), times=times, items=items)


@dataclass
class _Chat:
    chat_id: str
    messages: list[ParsedMessage] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    times: list[datetime] = field(default_factory=list)

    def add(self, rollout: _Rollout) -> None:
        self.times.extend(rollout.times)
        # A program ran this session: no person typed the text in the person's slot (D5).
        slot = AUTOMATED if rollout.program_run else PERSON
        for number, at, item in rollout.items:
            found = _item_message(item, slot)
            if found is None:
                continue
            key = message_key(KEY_PREFIX, self.chat_id, number)
            if key in self.keys:
                # The same rollout twice in one upload.
                continue
            self.keys.add(key)
            speaker, text = found
            self.messages.append(ParsedMessage(key=key, text=text, at=at, speaker_kind=speaker))

    def parsed(self) -> ParsedChat:
        return ParsedChat(
            chat_id=self.chat_id,
            messages=tuple(self.messages),
            started_at=min(self.times) if self.times else None,
            ended_at=max(self.times) if self.times else None,
        )


def _decompressed(data: bytes) -> bytes:
    return archive.zstd_decompress(data) if archive.is_zstd(data[:4]) else data


def _files(upload: Upload) -> Iterable[bytes]:
    if archive.is_zip(upload):
        return (
            _decompressed(member.data) for member in archive.zip_members(upload, suffixes=_SUFFIXES)
        )
    return [_decompressed(upload.read_all(archive.MAX_MEMBER_BYTES))]


def parse(upload: Upload) -> Iterator[ParsedChat]:
    chats: dict[str, _Chat] = {}
    recognised = False
    for data in _files(upload):
        rollout = _rollout(data)
        if rollout is None:
            continue
        recognised = True
        chats.setdefault(rollout.chat_id, _Chat(rollout.chat_id)).add(rollout)
    if not recognised:
        raise NotAnExport("the file is not a Codex rollout export")
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
