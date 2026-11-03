"""The connector interface (A.4, D10): what a parser gives the pipeline, and the rules it follows.

A connector type declares its parser, its locator format, its message key and its batch rule,
which cuts episodes (A.4). Here a ConnectorSpec holds them for one upload format (`provider`).
The type-level fields are code, not rows (S27). docs/ingest.md is the contract for the lanes that
add connectors.

Rules every parser keeps:
- A message key comes from message_key() and a chat's locator from chat_locator(). Both are
  opaque fingerprints of the source's own ids, so no name or term can ride in them (store-map
  open point, spec line 2505), and the screener leaves them as they are.
- Text goes through clean_text() first: the store and the model layer refuse lone surrogates.
- A message is dated by its own time, never by the upload's (D10, "Order and time").
- Speaker and modality come from the source's metadata (D5). Machine text is never filed under
  speaker_kind person.
"""

import hashlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import BinaryIO

from grove.store.enums import ENUMS

SPEAKER_KINDS = frozenset(ENUMS["speaker_kind"])
MODALITIES = frozenset(ENUMS["modality"])
CONNECTOR_TYPES = frozenset(ENUMS["connector_type"])

# A code: machine text, the form of grove.writer.sources' parser_version check.
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_PREFIX_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz")
_KEY_HEX = 32
_SEPARATOR = "\x1f"


class NotAnExport(ValueError):
    """The upload is not an export this provider's parser reads. The message names the format,
    never a value from the file."""


def _is_code(value: object, *, limit: int = 64) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and value[0].isalnum()
        and set(value) <= _CODE_CHARS
    )


def _fingerprint(parts: Sequence[object]) -> str:
    joined = _SEPARATOR.join("" if part is None else str(part) for part in parts)
    # surrogatepass: an id read from JSON may hold a lone surrogate, and it must still hash.
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()[:_KEY_HEX]


def _check_prefix(prefix: str) -> None:
    if type(prefix) is not str or not 1 <= len(prefix) <= 8 or not set(prefix) <= _PREFIX_CHARS:
        raise ValueError("a key prefix is 1 to 8 lowercase letters")


def message_key(prefix: str, *parts: object) -> str:
    """The message key: `<prefix>-<32 hex>`, a fingerprint of the source's own ids for one
    message (A.4: for Claude Code the session id and the record's uuid). Same parts, same key, so
    a re-import is idempotent; a later export that repeats a message repeats its key."""
    _check_prefix(prefix)
    if not parts:
        raise ValueError("a message key needs the ids it stands for")
    return f"{prefix}-{_fingerprint(parts)}"


def chat_locator(prefix: str, chat_id: object) -> str:
    """The external locator of one chat (one source per chat and read): `<prefix>-<32 hex>`.
    Each chat gets its own, so two chats never share a locator (store-map open point)."""
    _check_prefix(prefix)
    if chat_id is None or str(chat_id) == "":
        raise ValueError("a chat locator needs the chat's id")
    return f"{prefix}-{_fingerprint(('chat', chat_id))}"


def clean_text(value: object) -> str:
    """Text as the store can hold it: a lone surrogate becomes U+FFFD, NUL is dropped, and line
    ends are LF. Anything that is not text gives the empty string."""
    if not isinstance(value, str):
        return ""
    text = value.encode("utf-8", "replace").decode("utf-8") if _has_surrogate(value) else value
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _has_surrogate(value: str) -> bool:
    return any("\ud800" <= char <= "\udfff" for char in value)


def clip(value: str, limit: int) -> str:
    """One line of at most `limit` characters: whitespace runs fold to one space."""
    return " ".join(value.split())[:limit]


def utc_from_epoch(value: object) -> datetime | None:
    """Seconds since 1970 (int or float, as the ChatGPT export writes them), or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def utc_from_iso(value: object) -> datetime | None:
    """An ISO 8601 time with an offset or a Z, in UTC; None for anything else. A time with no
    offset is refused: a guess at its zone would date the message wrongly (D10)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class ParsedMessage:
    """One message as a parser read it. Screening runs on it next; nothing here is stored yet."""

    key: str
    text: str
    at: datetime | None
    speaker_kind: str
    modality: str = "text"
    sender: str | None = None

    def __post_init__(self) -> None:
        if not _is_code(self.key, limit=80):
            raise ValueError("ParsedMessage.key must come from message_key()")
        if not isinstance(self.text, str):
            raise TypeError("ParsedMessage.text must be a str")
        if self.at is not None and (
            not isinstance(self.at, datetime) or self.at.utcoffset() is None
        ):
            raise ValueError("ParsedMessage.at must be an aware datetime or None")
        if self.speaker_kind not in SPEAKER_KINDS:
            raise ValueError("ParsedMessage.speaker_kind must be a speaker_kind code")
        if self.modality not in MODALITIES:
            raise ValueError("ParsedMessage.modality must be a modality code")
        if self.sender is not None and not isinstance(self.sender, str):
            raise TypeError("ParsedMessage.sender must be a str or None")

    def __repr__(self) -> str:
        return (
            f"ParsedMessage(speaker_kind={self.speaker_kind}, modality={self.modality}, "
            f"text_length={len(self.text)}, at={'set' if self.at else None})"
        )


@dataclass(frozen=True)
class ParsedChat:
    """One chat (a session, a conversation) and its messages in their own order."""

    chat_id: str
    messages: tuple[ParsedMessage, ...]
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chat_id, str) or not self.chat_id:
            raise ValueError("ParsedChat.chat_id must be non-empty text")
        keys = [message.key for message in self.messages]
        if len(set(keys)) != len(keys):
            raise ValueError("ParsedChat holds one message key twice")

    def __repr__(self) -> str:
        return f"ParsedChat(messages={len(self.messages)})"


@dataclass
class Upload:
    """The uploaded file: a seekable binary stream and its size. Its name is never read: a file
    name can hold a name (R1, D14), so a parser knows the format by the bytes."""

    stream: BinaryIO
    size: int

    def head(self, count: int = 8) -> bytes:
        self.stream.seek(0)
        data = self.stream.read(count)
        self.stream.seek(0)
        return data

    def read_all(self, limit: int) -> bytes:
        if self.size > limit:
            raise NotAnExport("the file is larger than this format allows")
        self.stream.seek(0)
        data = self.stream.read(limit + 1)
        if len(data) > limit:
            raise NotAnExport("the file is larger than this format allows")
        return data


def one_episode(messages: Sequence[ParsedMessage]) -> list[list[ParsedMessage]]:
    """A past AI conversation's rule (A.4): one session, or the part added since the last read,
    is one episode."""
    return [list(messages)] if messages else []


def silence_windows(minutes: int) -> Callable[[Sequence[ParsedMessage]], list[list[ParsedMessage]]]:
    """A chat's rule (A.4, engine 8.7): a window closes after `minutes` of silence. A message with
    no time joins the window it follows."""

    def cut(messages: Sequence[ParsedMessage]) -> list[list[ParsedMessage]]:
        windows: list[list[ParsedMessage]] = []
        last: datetime | None = None
        for message in messages:
            gap = (
                message.at is not None
                and last is not None
                and (message.at - last).total_seconds() > minutes * 60
            )
            if not windows or gap:
                windows.append([])
            windows[-1].append(message)
            if message.at is not None:
                last = message.at
        return windows

    return cut


@dataclass(frozen=True)
class ConnectorSpec:
    """One upload format. `parse` yields chats; `cut` cuts a chat's new messages into episodes."""

    provider: str
    connector_type: str
    parser_version: str
    batch_rule: str
    key_prefix: str
    parse: Callable[[Upload], Iterator[ParsedChat]] = field(repr=False)
    cut: Callable[[Sequence[ParsedMessage]], list[list[ParsedMessage]]] = field(
        default=one_episode, repr=False
    )
    cut_rule: str = "session_delta.v1"

    def __post_init__(self) -> None:
        if not _is_code(self.provider, limit=32) or not self.provider.islower():
            raise ValueError("a provider is a lowercase code")
        if self.connector_type not in CONNECTOR_TYPES:
            raise ValueError("connector_type must be a connector_type code")
        for name in ("parser_version", "batch_rule", "cut_rule"):
            if not _is_code(getattr(self, name)):
                raise ValueError(f"{name} must be a code")
        _check_prefix(self.key_prefix)


_SPECS: dict[str, ConnectorSpec] = {}


def register(spec: ConnectorSpec) -> ConnectorSpec:
    if not isinstance(spec, ConnectorSpec):
        raise TypeError("register takes a ConnectorSpec")
    known = _SPECS.get(spec.provider)
    if known is not None and known is not spec:
        raise ValueError(f"provider {spec.provider} is registered already")
    _SPECS[spec.provider] = spec
    return spec


def spec_for(provider: str) -> ConnectorSpec | None:
    return _SPECS.get(provider) if isinstance(provider, str) else None


def providers() -> tuple[str, ...]:
    return tuple(sorted(_SPECS))
