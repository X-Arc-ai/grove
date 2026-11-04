"""Pure text retention drafts, with no persistence or admission authority.

Only invented, in-memory screening rules enter this component. The current Screener
carries fact values without admitting them. No production consumer imports this module.
"""

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps

from grove.config import ConfigError
from grove.ingest.connector import (
    MAX_MESSAGE_CHARS,
    MAX_SENDER_CHARS,
    ParsedChat,
    ParsedMessage,
    ReadBudget,
    Upload,
    bounded_id,
    chat_locator,
    kept_text,
)
from grove.ingest.documents import parse_text
from grove.ingest.whatsapp import parse_export
from grove.screening import (
    CLASSES,
    Denylist,
    Message,
    RenameMap,
    ScreenedMessage,
    ScreenedSource,
    Screener,
    ScreeningConfig,
)
from grove.source_facts import DOCUMENT_TEXT, WHATSAPP_TEXT, TextSourceFacts, check_text_transport

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 1_000
MAX_KEY_BYTES = 1_024_000
MAX_RULE_BYTES = 32_768
MAX_SCREEN_UNITS = 31_250
MAX_GROUP_FACT_BYTES = 2_048_000
MAX_SCREEN_BYTES = 2_000_000
MAX_PART_BYTES = 4_000_000
MAX_PARTS = 64
MAX_TOTAL_BYTES = 16_000_000
_ERRORS = ("invalid_text_draft", "invalid_screening_rules", "text_retention_limit")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITIES = (
    ("whatsapp", "whatsapp", "whatsapp.export.v1", "wa", False),
    ("notes_files_releases", "document", "document.text.v1", "doc", True),
)
_CUT_RULES = ("whatsapp.export_90m.v1", "whatsapp.export_untimed.v1", "document.export.v1")
_ROW_FIELDS = {"key", "sender", "text", "at", "speaker_kind", "modality", "source_facts"}
_BINARY_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"%PDF-",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"OggS",
    b"ID3",
    b"\x1f\x8b",
    b"\x28\xb5\x2f\xfd",
)


class TextRetentionError(ValueError):
    """Closed refusal codes only, without source values or nested error context."""

    def __init__(self, code: str = "invalid_text_draft") -> None:
        self.code = code if type(code) is str and code in _ERRORS else _ERRORS[0]
        super().__init__(self.code)


def _safe(code="invalid_text_draft"):
    def decorate(function):
        @wraps(function)
        def checked(*args, **kwargs):
            failure = code
            try:
                return function(*args, **kwargs)
            except TextRetentionError as error:
                failure = error.code
            except (
                ValueError,
                TypeError,
                AttributeError,
                OverflowError,
                OSError,
                RecursionError,
                ConfigError,
            ):
                pass
            raise TextRetentionError(failure)

        return checked

    return decorate


def _repr(self) -> str:
    return f"{type(self).__name__}()"


def _limit(condition: bool) -> None:
    if not condition:
        raise TextRetentionError("text_retention_limit")


def _tuple(value, limit: int) -> None:
    if type(value) is not tuple:
        raise TextRetentionError()
    _limit(len(value) <= limit)


def _integer(value, maximum: int) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise TextRetentionError()


def _string(value, chars: int, byte_limit: int) -> int:
    if type(value) is not str:
        raise TextRetentionError()
    _limit(len(value) <= chars)
    size = len(value.encode("utf-8"))
    _limit(size <= byte_limit)
    return size


def _key(key: str) -> int:
    if type(key) is not str:
        raise TextRetentionError()
    bounded_id(key)  # Before encoding, retention, hashing, or delta lookup.
    return len(key.encode("utf-8"))


def _keys(keys: tuple[str, ...]) -> None:
    _tuple(keys, MAX_MESSAGES)
    total = 0
    seen = set()
    for key in keys:
        total += _key(key)
        _limit(total <= MAX_KEY_BYTES)
        if key in seen:
            raise TextRetentionError()
        seen.add(key)


def _at(at: datetime | None) -> None:
    if at is not None:
        if type(at) is not datetime or at.utcoffset() is None or at.microsecond:
            raise TextRetentionError()
        if at.astimezone(UTC).microsecond:
            raise TextRetentionError()


@dataclass(frozen=True, slots=True, repr=False)
class TextIdentity:
    connector_type: str
    provider: str
    parser_version: str
    key_prefix: str
    document: bool

    @_safe()
    def __post_init__(self) -> None:
        fields = (self.connector_type, self.provider, self.parser_version, self.key_prefix)
        if (
            type(self) is not TextIdentity
            or any(type(value) is not str or len(value) > 64 for value in fields)
            or type(self.document) is not bool
            or (*fields, self.document) not in _IDENTITIES
        ):
            raise TextRetentionError()

    __repr__ = _repr


WHATSAPP_IDENTITY = TextIdentity(*_IDENTITIES[0])
DOCUMENT_IDENTITY = TextIdentity(*_IDENTITIES[1])


def _identity(identity: TextIdentity) -> TextIdentity:
    if type(identity) is not TextIdentity:
        raise TextRetentionError()
    identity.__post_init__()
    return DOCUMENT_IDENTITY if identity.document else WHATSAPP_IDENTITY


@dataclass(frozen=True, slots=True, repr=False)
class DraftScreeningRules:
    rename_entries: tuple[tuple[str, str], ...]
    denylist_terms: tuple[str, ...]
    own_identifiers: tuple[str, ...]
    sensitive_classes: tuple[str, ...]

    @_safe("invalid_screening_rules")
    def __post_init__(self) -> None:
        if type(self) is not DraftScreeningRules:
            raise TextRetentionError("invalid_screening_rules")
        # Validate every container before any traversal or matcher construction.
        for values in (self.rename_entries, self.denylist_terms, self.own_identifiers):
            if type(values) is not tuple or len(values) > 32:
                raise TextRetentionError("invalid_screening_rules")
        if (
            type(self.sensitive_classes) is not tuple
            or len(self.sensitive_classes) > 1
            or any(type(value) is not str for value in self.sensitive_classes)
            or self.sensitive_classes not in ((), ("pay_amount",))
        ):
            raise TextRetentionError("invalid_screening_rules")
        total = 0

        def charge(value, chars, byte_limit):
            nonlocal total
            if type(value) is not str or len(value) > chars:
                raise TextRetentionError("invalid_screening_rules")
            size = len(value.encode("utf-8"))
            total += size
            if size > byte_limit or total > MAX_RULE_BYTES:
                raise TextRetentionError("invalid_screening_rules")

        for pair in self.rename_entries:
            if type(pair) is not tuple or len(pair) != 2:
                raise TextRetentionError("invalid_screening_rules")
            charge(pair[0], 128, 512)
            charge(pair[1], 64, 64)
        for values in (self.denylist_terms, self.own_identifiers):
            for value in values:
                charge(value, 128, 512)
        for value in self.sensitive_classes:
            charge(value, 128, 512)

    __repr__ = _repr


@_safe("invalid_screening_rules")
def _screener(rules: DraftScreeningRules) -> Screener:
    if type(rules) is not DraftScreeningRules:
        raise TextRetentionError("invalid_screening_rules")
    rules.__post_init__()
    return Screener(
        ScreeningConfig(
            rename=RenameMap(rules.rename_entries, source="draft rename rules"),
            denylist=Denylist(rules.denylist_terms, source="draft denylist rules"),
            own_identifiers=frozenset(rules.own_identifiers),
            sensitive_classes=frozenset(rules.sensitive_classes),
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class TextCountsDraft:
    parts: int
    episodes: int
    messages_read: int
    messages_kept: int
    unknown_time_messages: int
    unknown_speaker_messages: int
    held_source_messages: int

    @_safe()
    def __post_init__(self) -> None:
        if type(self) is not TextCountsDraft:
            raise TextRetentionError()
        _integer(self.parts, MAX_PARTS)
        for count in (
            self.episodes,
            self.messages_read,
            self.messages_kept,
            self.unknown_time_messages,
            self.unknown_speaker_messages,
            self.held_source_messages,
        ):
            _integer(count, MAX_MESSAGES)
        if not (
            self.episodes <= self.messages_kept <= self.messages_read
            and max(self.unknown_time_messages, self.unknown_speaker_messages)
            <= self.held_source_messages
            <= min(self.messages_kept, self.unknown_time_messages + self.unknown_speaker_messages)
            and bool(self.parts) == bool(self.messages_read)
            and bool(self.episodes) == bool(self.messages_kept)
        ):
            raise TextRetentionError()

    __repr__ = _repr


def _screening_counts(counts) -> None:
    _tuple(counts, len(CLASSES))
    previous = ""
    for entry in counts:
        if type(entry) is not tuple or len(entry) != 2:
            raise TextRetentionError()
        code, count = entry
        if type(code) is not str or code not in CLASSES or code <= previous:
            raise TextRetentionError()
        _integer(count, MAX_SCREEN_UNITS)
        if count == 0:
            raise TextRetentionError()
        previous = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TextRetentionError()
        result[key] = value
    return result


def _json_shape(text: str) -> None:
    """Bound decoded container allocation before json.loads, ignoring quoted syntax."""
    depth = tokens = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            tokens += 1
        elif char in "[{":
            depth += 1
            tokens += 1
        elif char in "]}":
            depth -= 1
        elif char in ",:":
            tokens += 1
        _limit(depth <= 8 and tokens <= 128 * MAX_MESSAGES)


def _fact_json(value) -> str:
    """Refuse oversized decoded fact scalars before another serialization allocation."""

    def check(item, depth=0):
        _limit(depth <= 5)
        if type(item) is dict:
            _limit(len(item) <= 8)
            for key, child in item.items():
                _string(key, 128, 512)
                check(child, depth + 1)
        elif type(item) is list:
            _limit(len(item) <= 3)
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            _string(item, 128, 512)
        elif item is not None and (type(item) is not int or abs(item) > (1 << 63) - 1):
            raise TextRetentionError()

    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_part(part) -> tuple[ScreenedMessage, ...]:
    if type(part) is not TextPartDraft:
        raise TextRetentionError()
    _keys(part.message_keys)
    _screening_counts(part.screening_counts)
    _integer(part.encoded_bytes, MAX_PART_BYTES)
    size = _string(part.content_json, MAX_PART_BYTES, MAX_PART_BYTES)
    if size != part.encoded_bytes:
        raise TextRetentionError()
    _json_shape(part.content_json)
    rows = json.loads(part.content_json, object_pairs_hook=_unique_object)
    if type(rows) is not list or len(rows) != len(part.message_keys):
        raise TextRetentionError()
    messages = []
    shape = None
    last_end = 0
    output_bytes = 0
    for key, row in zip(part.message_keys, rows, strict=True):
        if type(row) is not dict or set(row) != _ROW_FIELDS or row["key"] != key:
            raise TextRetentionError()
        output_bytes += _key(row["key"])
        output_bytes += _string(row["text"], MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
        if row["sender"] is not None:
            output_bytes += _string(row["sender"], MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
        _limit(output_bytes <= MAX_SCREEN_BYTES)
        # Current adapters establish no speaker authority. A decoded draft cannot add it.
        if (
            row["speaker_kind"] is not None
            or type(row["modality"]) is not str
            or row["modality"] != "text"
        ):
            raise TextRetentionError()
        at = row["at"]
        if at is not None:
            _string(at, 40, 40)
            at = datetime.fromisoformat(at)
            _at(at)
            if at.isoformat() != row["at"]:
                raise TextRetentionError()
        facts = TextSourceFacts.from_json(_fact_json(row["source_facts"]))
        identity = WHATSAPP_TEXT if facts.locator.kind == "line_bytes" else DOCUMENT_TEXT
        check_text_transport(identity, facts, at)
        if identity == DOCUMENT_TEXT and facts.time.status != "absent":
            raise TextRetentionError()
        current_shape = (identity, facts.artifact_sha256)
        if shape is not None and shape != current_shape or facts.locator.byte_start < last_end:
            raise TextRetentionError()
        shape, last_end = current_shape, facts.locator.byte_end
        messages.append(ScreenedMessage(key, row["sender"], row["text"], at, None, "text", facts))
    if not messages and not any(
        code in ("denylisted", "one_time_code", "reset_link") for code, _ in part.screening_counts
    ):
        raise TextRetentionError()
    # Exact existing serializer bytes, including spaces and explicit nested nulls.
    if json.dumps(rows, sort_keys=True, ensure_ascii=False) != part.content_json:
        raise TextRetentionError()
    return tuple(messages)


@dataclass(frozen=True, slots=True, repr=False)
class TextPartDraft:
    content_json: str
    message_keys: tuple[str, ...]
    encoded_bytes: int
    screening_counts: tuple[tuple[str, int], ...]

    @_safe()
    def __post_init__(self) -> None:
        _read_part(self)

    __repr__ = _repr


@dataclass(frozen=True, slots=True, repr=False)
class TextCutDraft:
    first_message_key: str
    last_message_key: str
    start_time: datetime | None
    end_time: datetime | None
    cut_rule: str
    part_indexes: tuple[int, ...]

    @_safe()
    def __post_init__(self) -> None:
        if type(self) is not TextCutDraft:
            raise TextRetentionError()
        _key(self.first_message_key)
        _key(self.last_message_key)
        _at(self.start_time)
        _at(self.end_time)
        if (
            (self.start_time is None) != (self.end_time is None)
            or type(self.cut_rule) is not str
            or self.cut_rule not in _CUT_RULES
        ):
            raise TextRetentionError()
        if self.start_time is not None and self.start_time > self.end_time:
            raise TextRetentionError()
        if self.cut_rule == _CUT_RULES[0] and self.start_time is None:
            raise TextRetentionError()
        if self.cut_rule == _CUT_RULES[1] and self.start_time is not None:
            raise TextRetentionError()
        _tuple(self.part_indexes, MAX_PARTS)
        previous = -1
        for index in self.part_indexes:
            _integer(index, MAX_PARTS - 1)
            if index <= previous:
                raise TextRetentionError()
            previous = index
        if not self.part_indexes:
            raise TextRetentionError()

    __repr__ = _repr


def _cuts(
    identity: TextIdentity, located: list[tuple[ScreenedMessage, int]]
) -> tuple[TextCutDraft, ...]:
    if not located:
        return ()
    timed = all(message.at is not None for message, _ in located)
    rule = _CUT_RULES[2] if identity.document else _CUT_RULES[0 if timed else 1]
    groups = [[]]
    previous = None
    for message, index in located:
        if (
            timed
            and not identity.document
            and previous is not None
            and message.at - previous > timedelta(minutes=90)
        ):
            groups.append([])
        groups[-1].append((message, index))
        previous = message.at
    cuts = []
    for group in groups:
        times = [message.at for message, _ in group] if timed else []
        cuts.append(
            TextCutDraft(
                group[0][0].key,
                group[-1][0].key,
                min(times) if times else None,
                max(times) if times else None,
                rule,
                tuple(dict.fromkeys(index for _, index in group)),
            )
        )
    return tuple(cuts)


def _counts(parts: int, cuts: int, read: int, located) -> TextCountsDraft:
    return TextCountsDraft(
        parts,
        cuts,
        read,
        len(located),
        sum(message.at is None for message, _ in located),
        sum(message.speaker_kind is None for message, _ in located),
        sum(message.at is None or message.speaker_kind is None for message, _ in located),
    )


@dataclass(frozen=True, slots=True, repr=False)
class TextDeltaDraft:
    identity: TextIdentity
    external_locator: str
    artifact_sha256: str
    delta_keys: tuple[str, ...]
    retained_keys: tuple[str, ...]
    parts: tuple[TextPartDraft, ...]
    cuts: tuple[TextCutDraft, ...]
    newest_at: datetime | None
    counts: TextCountsDraft
    status: str

    @_safe()
    def __post_init__(self) -> None:
        if type(self) is not TextDeltaDraft:
            raise TextRetentionError()
        identity = _identity(self.identity)
        if (
            type(self.artifact_sha256) is not str
            or len(self.artifact_sha256) != 64
            or _DIGEST.fullmatch(self.artifact_sha256) is None
        ):
            raise TextRetentionError()
        _key(self.external_locator)
        chat_id = bounded_id("export_digest:" + self.artifact_sha256)
        if self.external_locator != chat_locator(identity.key_prefix, chat_id):
            raise TextRetentionError()
        _keys(self.delta_keys)
        _keys(self.retained_keys)
        _tuple(self.parts, MAX_PARTS)
        _tuple(self.cuts, MAX_MESSAGES)
        _at(self.newest_at)
        if type(self.counts) is not TextCountsDraft:
            raise TextRetentionError()
        self.counts.__post_init__()
        located = []
        total = 0
        last_end = 0
        for index, part in enumerate(self.parts):
            if type(part) is not TextPartDraft:
                raise TextRetentionError()
            _integer(part.encoded_bytes, MAX_PART_BYTES)
            total += part.encoded_bytes
            _limit(total <= MAX_TOTAL_BYTES)
            messages = _read_part(part)
            _limit(len(located) + len(messages) <= MAX_MESSAGES)
            for message in messages:
                facts = message.source_facts
                check_text_transport(
                    (identity.provider, identity.parser_version), facts, message.at
                )
                if (
                    facts.artifact_sha256 != self.artifact_sha256
                    or facts.locator.byte_start < last_end
                ):
                    raise TextRetentionError()
                last_end = facts.locator.byte_end
                located.append((message, index))
        retained = tuple(message.key for message, _ in located)
        if retained != self.retained_keys:
            raise TextRetentionError()
        retained_set = set(retained)
        if tuple(key for key in self.delta_keys if key in retained_set) != retained:
            raise TextRetentionError()
        for cut in self.cuts:
            if type(cut) is not TextCutDraft:
                raise TextRetentionError()
            cut.__post_init__()
        if self.cuts != _cuts(identity, located):
            raise TextRetentionError()
        known = [message.at for message, _ in located if message.at is not None]
        if self.newest_at != (max(known) if known else None):
            raise TextRetentionError()
        if self.counts != _counts(len(self.parts), len(self.cuts), len(self.delta_keys), located):
            raise TextRetentionError()
        # Denylist counts always denote dropped messages. For an empty group, strong
        # code/link counts do too; one dropped message contributes to exactly one class.
        minimum_drops = sum(
            count
            for part in self.parts
            for code, count in part.screening_counts
            if code == "denylisted"
            or not part.message_keys
            and code in ("one_time_code", "reset_link")
        )
        if minimum_drops > len(self.delta_keys) - len(retained):
            raise TextRetentionError()
        expected = (
            "empty_delta" if not self.delta_keys else "draft_only" if retained else "kept_out"
        )
        if type(self.status) is not str or self.status != expected:
            raise TextRetentionError()
        if not self.delta_keys and (self.parts or self.cuts):
            raise TextRetentionError()
        if len(self.parts) > len(self.delta_keys):
            raise TextRetentionError()

    __repr__ = _repr


def _stored(keys: frozenset[str]) -> None:
    if type(keys) is not frozenset:
        raise TextRetentionError()
    _limit(len(keys) <= MAX_MESSAGES)
    total = 0
    for key in keys:
        total += _key(key)
        _limit(total <= MAX_KEY_BYTES)


def _parse(identity: TextIdentity, artifact: bytes) -> tuple[str, tuple[ParsedMessage, ...]]:
    # All cheap checks precede BytesIO, parser allocation and hashing.
    if type(artifact) is not bytes:
        raise TextRetentionError()
    _limit(len(artifact) <= MAX_ARTIFACT_BYTES)
    if artifact.startswith(_BINARY_SIGNATURES) or artifact[4:8] == b"ftyp":
        raise TextRetentionError()
    budget = ReadBudget.for_size(len(artifact))
    budget.messages_left = MAX_MESSAGES
    upload = Upload(io.BytesIO(artifact), len(artifact), budget)
    if identity.document:
        parsed = parse_text(upload, markdown=True, source_at=None)
        digest = parsed.sha256
    else:
        parsed = parse_export(upload)
        digest = parsed.artifact_sha256
    actual_digest = hashlib.sha256(artifact).hexdigest()
    if digest != actual_digest or parsed.parser_version != identity.parser_version:
        raise TextRetentionError()
    chat = parsed.as_chat()
    if type(chat) is not ParsedChat:
        raise TextRetentionError()
    _key(chat.chat_id)
    if chat.chat_id != "export_digest:" + actual_digest:
        raise TextRetentionError()
    _tuple(chat.messages, MAX_MESSAGES)
    total_keys = 0
    seen = set()
    last_end = 0
    line_count = sum(1 for _ in re.finditer(rb"\r\n|\r|\n", artifact))
    if artifact and not artifact.endswith((b"\r", b"\n")):
        line_count += 1
    for message in chat.messages:
        if type(message) is not ParsedMessage:
            raise TextRetentionError()
        total_keys += _key(message.key)
        _limit(total_keys <= MAX_KEY_BYTES)
        if message.key in seen:
            raise TextRetentionError()
        seen.add(message.key)
        # Parsers cap these strings already; this also validates unexpected adapter output.
        _string(message.text, MAX_MESSAGE_CHARS + 100, 4 * (MAX_MESSAGE_CHARS + 100))
        if message.sender is not None:
            _string(message.sender, MAX_SENDER_CHARS + 100, 4 * (MAX_SENDER_CHARS + 100))
        if (
            message.calendar_event is not None
            or message.speaker_kind is not None
            or message.modality != "text"
        ):
            raise TextRetentionError()
        _at(message.at)
        facts = message.source_facts
        check_text_transport((identity.provider, identity.parser_version), facts, message.at)
        if facts.artifact_sha256 != actual_digest or facts.locator.byte_start < last_end:
            raise TextRetentionError()
        if identity.document and facts.time.status != "absent":
            raise TextRetentionError()
        facts.locator.check_extent(byte_count=len(artifact), line_count=line_count)
        last_end = facts.locator.byte_end
    return actual_digest, chat.messages


def _groups(messages: tuple[ParsedMessage, ...], locator: str) -> tuple[tuple[Message, ...], ...]:
    groups = []
    group = []
    units = len(locator)
    fact_bytes = 0
    for message in messages:
        text = kept_text(message.text, MAX_MESSAGE_CHARS)
        sender = None if message.sender is None else kept_text(message.sender, MAX_SENDER_CHARS)
        row_units = len(message.key) + len(sender or "") + len(text)
        row_facts = message.source_facts.encoded_size
        _limit(len(locator) + row_units <= MAX_SCREEN_UNITS and row_facts <= MAX_GROUP_FACT_BYTES)
        if units + row_units > MAX_SCREEN_UNITS or fact_bytes + row_facts > MAX_GROUP_FACT_BYTES:
            _limit(len(groups) + 1 < MAX_PARTS)
            groups.append(tuple(group))
            group = []
            units, fact_bytes = len(locator), 0
        units += row_units
        fact_bytes += row_facts
        group.append(
            Message(
                message.key,
                sender,
                text,
                message.at,
                message.speaker_kind,
                message.modality,
                message.source_facts,
            )
        )
    if group:
        _limit(len(groups) < MAX_PARTS)
        groups.append(tuple(group))
    return tuple(groups)


def _screen_group(
    screener: Screener, identity: TextIdentity, locator: str, group
) -> ScreenedSource:
    result = screener.screen_source(
        locator,
        group,
        source_identity=(identity.provider, identity.parser_version),
    )
    if (
        type(result) is not ScreenedSource
        or type(result.locator) is not str
        or result.locator != locator
    ):
        raise TextRetentionError()
    _tuple(result.messages, len(group))
    position = 0
    output_bytes = _string(result.locator, MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
    for message in result.messages:
        if type(message) is not ScreenedMessage:
            raise TextRetentionError()
        _key(message.key)
        while position < len(group) and group[position].key != message.key:
            position += 1
        if position == len(group):
            raise TextRetentionError()
        original = group[position]
        position += 1
        if (
            message.at != original.at
            or message.source_facts != original.source_facts
            or message.speaker_kind is not None
            or message.modality != "text"
            or (message.sender is None) != (original.sender is None)
        ):
            raise TextRetentionError()
        _at(message.at)
        check_text_transport(
            (identity.provider, identity.parser_version), message.source_facts, message.at
        )
        output_bytes += _string(message.key, MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
        output_bytes += _string(message.text, MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
        if message.sender is not None:
            output_bytes += _string(message.sender, MAX_SCREEN_BYTES, MAX_SCREEN_BYTES)
        _limit(output_bytes <= MAX_SCREEN_BYTES)
    return result


def _pack_group(result: ScreenedSource, parts: list[TextPartDraft], total: int) -> int:
    counts = tuple(sorted(result.counts.items()))
    _screening_counts(counts)
    rows = []
    keys = []
    size = 2

    def finish():
        nonlocal total, rows, keys, size, counts
        _limit(len(parts) < MAX_PARTS and total + size <= MAX_TOTAL_BYTES)
        part = TextPartDraft("[" + ", ".join(rows) + "]", tuple(keys), size, counts)
        parts.append(part)
        total += size
        rows, keys, size, counts = [], [], 2, ()

    for message in result.messages:
        row = json.dumps(message.as_row(), sort_keys=True, ensure_ascii=False)
        row_bytes = len(row.encode("utf-8"))
        _limit(row_bytes + 2 <= MAX_PART_BYTES)
        next_size = size + row_bytes + (2 if rows else 0)
        if next_size > MAX_PART_BYTES:
            finish()
            next_size = 2 + row_bytes
        # Reserve the new part and its exact bytes before retaining this row.
        _limit(len(parts) < MAX_PARTS and total + next_size <= MAX_TOTAL_BYTES)
        rows.append(row)
        keys.append(message.key)
        size = next_size
    # Empty groups retain a single diagnostic part; no cut will name it.
    finish()
    return total


@_safe()
def prepare_text_delta(
    identity: TextIdentity,
    artifact: bytes,
    *,
    already_stored: frozenset[str],
    screening_rules: DraftScreeningRules,
) -> TextDeltaDraft:
    """Prepare an unadmitted memory-only draft from one exact text artifact.

    already_stored simulates delta selection. It cannot authorize a replay return or
    stand in for transaction-local storage and erasure checks.
    """
    identity = _identity(identity)
    _stored(already_stored)
    if type(screening_rules) is not DraftScreeningRules:
        raise TextRetentionError("invalid_screening_rules")
    screening_rules.__post_init__()
    digest, messages = _parse(identity, artifact)
    delta = tuple(message for message in messages if message.key not in already_stored)
    chat_id = bounded_id("export_digest:" + digest)
    locator = chat_locator(identity.key_prefix, chat_id)
    groups = _groups(delta, locator)
    screener = _screener(screening_rules)
    parts = []
    total = 0
    for group in groups:
        result = _screen_group(screener, identity, locator, group)
        total = _pack_group(result, parts, total)
    located = [(message, index) for index, part in enumerate(parts) for message in _read_part(part)]
    cuts = _cuts(identity, located)
    known = [message.at for message, _ in located if message.at is not None]
    return TextDeltaDraft(
        identity,
        locator,
        digest,
        tuple(message.key for message in delta),
        tuple(message.key for message, _ in located),
        tuple(parts),
        cuts,
        max(known) if known else None,
        _counts(len(parts), len(cuts), len(delta), located),
        "empty_delta" if not delta else "draft_only" if located else "kept_out",
    )
