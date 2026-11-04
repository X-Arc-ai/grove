"""The connector interface (A.4, D10): what a parser gives the pipeline, and the rules it follows.

A connector type declares its parser, its locator format, its message key and its batch rule,
which cuts episodes (A.4). Here a ConnectorSpec holds them for one upload format (`provider`).
The type-level fields are code, not rows (S27). docs/ingest.md is the contract for the lanes that
add connectors.

Rules every parser keeps:
- A message key comes from message_key() and a chat's locator from chat_locator(). Both are
  opaque fingerprints of the source's own ids, so no name or term can ride in them (store-map
  open point, spec line 2505). Each is one word (the prefix glued to the hex), so a rename form
  or a denylist term, which matches whole words only, cannot match a part of it; register()
  refuses a prefix whose key or locator screening would change.
- Read through the upload's budget (Upload.budget, grove.ingest.archive): one upload expands to
  at most MAX_EXPANSION times its size (MAX_READ_BYTES at most), MAX_RECORDS lines, MAX_CHATS
  chats, MAX_MESSAGES messages and MAX_TEXT_CHARS characters of text, whatever the file holds.
  A parser charges a chat or a message, with its text, before it keeps one.
- Text goes through clean_text() first: the store and the model layer refuse lone surrogates.
- A message is dated by its own time, never by the upload's (D10, "Order and time").
- Speaker and modality come from the source's metadata (D5). Machine text is never filed under
  speaker_kind person.
"""

import hashlib
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import BinaryIO

from grove.screening import Denylist, Message, RenameMap, Screener, ScreeningConfig
from grove.store.enums import ENUMS

SPEAKER_KINDS = frozenset(ENUMS["speaker_kind"])
MODALITIES = frozenset(ENUMS["modality"])
CONNECTOR_TYPES = frozenset(ENUMS["connector_type"])

# A code: machine text, the form of grove.writer.sources' parser_version check.
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_PREFIX_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz")
_KEY_HEX = 32
_SEPARATOR = "\x1f"
# A UTF-16 half with no partner: JSON can spell one, UTF-8 cannot hold it.
_SURROGATE = re.compile("[\ud800-\udfff]")
_EARLIEST = datetime.min.replace(tzinfo=UTC)

# What one upload may expand to, whatever its size on the wire: the bytes read out of archives
# and compressed streams, the JSON lines read (blank ones too), and the chats and messages a
# parser makes. Past any, the upload is refused, so a small crafted file cannot take the API's
# memory without a bound. An export larger than this goes in parts: a re-import
# is idempotent.
MAX_READ_BYTES = 768 * 1024 * 1024
# A real export compresses well under this ratio; a crafted one (a zip of zeros, of newlines,
# of one word again and again) is far past it. Every upload may read at least MIN_READ_BYTES.
MAX_EXPANSION = 64
MIN_READ_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 1_000_000
MAX_CHATS = 10_000
MAX_MESSAGES = 1_000_000
# The text charged per upload. Synthetic screening probes measured about 0.3 million
# characters a second; total request time also includes parsing and store writes.
MAX_TEXT_CHARS = 50_000_000
_TOO_LARGE = "the export holds more than one upload takes: upload it in parts"
# The screener's working copies of a text take many times its size (a non-ASCII text keeps two
# offsets a character in each of two copies), so the pipeline screens at most this much of one
# message, and of one sender.
MAX_MESSAGE_CHARS = 100_000
MAX_SENDER_CHARS = 200
# A chat's new messages are stored in parts of at most this many capped text and sender
# characters, one source each. JSON metadata and screening replacements add to the property.
MAX_SOURCE_CHARS = 4_000_000
_CUT_MARK = "[cut: the message is longer than Grove keeps]"


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
    """The message key: `<prefix><32 hex>`, one word, a fingerprint of the source's own ids for
    one message (A.4: for Claude Code the session id and the record's uuid). Same parts, same
    key, so a re-import is idempotent; a later export that repeats a message repeats its key.
    No separator: a prefix that stood as a word of its own ("cc") would match a rename form."""
    _check_prefix(prefix)
    if not parts:
        raise ValueError("a message key needs the ids it stands for")
    return f"{prefix}{_fingerprint(parts)}"


def chat_locator(prefix: str, chat_id: object) -> str:
    """The external locator of one chat (one source per chat and read): `<prefix><32 hex>`, one
    word. Each chat gets its own, so two chats never share a locator (store-map open point)."""
    _check_prefix(prefix)
    if chat_id is None or str(chat_id) == "":
        raise ValueError("a chat locator needs the chat's id")
    return f"{prefix}{_fingerprint(('chat', chat_id))}"


def clean_text(value: object) -> str:
    """Text as the store can hold it: a lone surrogate becomes U+FFFD, NUL is dropped, and line
    ends are LF. Anything that is not text gives the empty string."""
    if not isinstance(value, str):
        return ""
    text = _SURROGATE.sub("\ufffd", value)
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def kept_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """At most `limit` characters of a text, for the screener. A longer text is cut at its last
    whitespace before the limit, so no word, and so no secret, is cut in half and stored as a part
    the detectors cannot know; a mark says that the rest is gone. Deterministic, so a re-import
    keeps the same text."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = len(head)
    while end > 0 and not head[end - 1].isspace():
        end -= 1
    kept = head[:end].rstrip()
    return f"{kept}\n{_CUT_MARK}" if kept else _CUT_MARK


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


@dataclass(frozen=True, slots=True)
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
        # The parser retains these objects until every member is read. Bound retained text
        # here as well as at screening, so earlier members cannot retain huge Unicode strings.
        object.__setattr__(self, "text", kept_text(self.text))
        if self.sender is not None:
            object.__setattr__(self, "sender", kept_text(self.sender, MAX_SENDER_CHARS))

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


class ReadBudget:
    """What one upload may still expand to. The archive readers charge it; past a limit the
    upload is NotAnExport, and nothing has been written yet."""

    __slots__ = ("bytes_left", "records_left", "chats_left", "messages_left", "text_left")

    def __init__(
        self,
        *,
        max_bytes: int = MAX_READ_BYTES,
        max_records: int = MAX_RECORDS,
        max_chats: int = MAX_CHATS,
        max_messages: int = MAX_MESSAGES,
        max_text: int = MAX_TEXT_CHARS,
    ) -> None:
        self.bytes_left = max_bytes
        self.records_left = max_records
        self.chats_left = max_chats
        self.messages_left = max_messages
        self.text_left = max_text

    @classmethod
    def for_size(cls, size: int) -> "ReadBudget":
        """The budget of an upload of `size` bytes: it may read MAX_EXPANSION times its size out
        of archives and compressed streams, MIN_READ_BYTES at least and MAX_READ_BYTES at most."""
        return cls(max_bytes=min(MAX_READ_BYTES, max(MIN_READ_BYTES, MAX_EXPANSION * size)))

    def take_bytes(self, count: int) -> None:
        self.bytes_left -= count
        if self.bytes_left < 0:
            raise NotAnExport(_TOO_LARGE)

    def take_record(self) -> None:
        self.records_left -= 1
        if self.records_left < 0:
            raise NotAnExport(_TOO_LARGE)

    def take_chat(self) -> None:
        """Before a parser makes a new chat."""
        self.chats_left -= 1
        if self.chats_left < 0:
            raise NotAnExport(_TOO_LARGE)

    def take_message(self, chars: int) -> None:
        """Before a parser keeps a new message of `chars` characters (the screener reads at most
        MAX_MESSAGE_CHARS of it)."""
        self.messages_left -= 1
        self.text_left -= min(chars, MAX_MESSAGE_CHARS)
        if self.messages_left < 0 or self.text_left < 0:
            raise NotAnExport(_TOO_LARGE)

    def __repr__(self) -> str:
        return (
            f"ReadBudget(bytes_left={self.bytes_left}, records_left={self.records_left}, "
            f"chats_left={self.chats_left}, messages_left={self.messages_left}, "
            f"text_left={self.text_left})"
        )


@dataclass
class Upload:
    """The uploaded file: a seekable binary stream, its size and its read budget. Its name is
    never read: a file name can hold a name (R1, D14), so a parser knows the format by the bytes.
    """

    stream: BinaryIO
    size: int
    budget: ReadBudget | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = ReadBudget.for_size(self.size)

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


def in_time_order(messages: Sequence[ParsedMessage]) -> list[ParsedMessage]:
    """The messages in their own time order (D10). Every parser sorts each chat: a chat merged
    from more than one file comes in archive order, and a file's own stamps need not rise. The
    sort is stable, and a message with no time keeps its place after the message it follows."""
    keyed = []
    last = _EARLIEST
    for index, message in enumerate(messages):
        if message.at is not None:
            last = message.at
        keyed.append((last, index, message))
    keyed.sort(key=lambda row: (row[0], row[1]))
    return [message for _, _, message in keyed]


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
_SAMPLE_IDS = ("sample", "0123456789", "a1b2c3d4e5")


def _screening_keeps(prefix: str) -> bool:
    """Whether screening leaves this prefix's keys and locators as they are: with the detectors
    alone, and with the prefix itself as a rename form and as a denylist term."""
    configs = (
        ScreeningConfig(),
        ScreeningConfig(rename=RenameMap.from_mapping({prefix: "Person A"})),
        ScreeningConfig(denylist=Denylist([prefix])),
    )
    for config in configs:
        screener = Screener(config)
        for sample in _SAMPLE_IDS:
            locator = chat_locator(prefix, sample)
            key = message_key(prefix, sample)
            screened = screener.screen_source(locator, [Message(key=key, sender=None, text="ok")])
            if screened.locator != locator or [m.key for m in screened.messages] != [key]:
                return False
    return True


def register(spec: ConnectorSpec) -> ConnectorSpec:
    """Adds a provider. A key prefix whose keys or locators screening would change is refused,
    because the pipeline fails such a chat closed (take_in, KeyChanged)."""
    if not isinstance(spec, ConnectorSpec):
        raise TypeError("register takes a ConnectorSpec")
    if not _screening_keeps(spec.key_prefix):
        raise ValueError("screening changes this key prefix's keys: choose another prefix")
    known = _SPECS.get(spec.provider)
    if known is not None and known is not spec:
        raise ValueError(f"provider {spec.provider} is registered already")
    _SPECS[spec.provider] = spec
    return spec


def spec_for(provider: str) -> ConnectorSpec | None:
    return _SPECS.get(provider) if isinstance(provider, str) else None


def providers() -> tuple[str, ...]:
    return tuple(sorted(_SPECS))
