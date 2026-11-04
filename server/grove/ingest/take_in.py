"""The ingest pipeline (A.4, D10, D14): parse an upload, keep what each chat adds, screen it, cut
it into episodes and commit it through the writer, then record the connector's coverage.

Order per chat: the delta (the messages whose key is not stored yet), bounded source parts,
screening in code, the connector's episode cut, one write transaction for the whole delta.
Screening finishes before the transaction opens (SC1). A
re-import of the same export finds every key stored, so it writes no source, no receipt and no
episode: only the connector's coverage times move. docs/ingest.md has the contract.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from grove import clock
from grove.ingest.connector import (
    MAX_SENDER_CHARS,
    MAX_SOURCE_CHARS,
    ConnectorSpec,
    ParsedChat,
    ParsedMessage,
    Upload,
    kept_text,
    spec_for,
)
from grove.ingest.connector import chat_locator as _chat_locator
from grove.screening import Message, ScreenedSource, Screener
from grove.store.connection import Driver
from grove.store.query import WorldQuery
from grove.writer.errors import InvalidRecord
from grove.writer.ingest import (
    DeltaMoved,
    EpisodeCut,
    ensure_connector,
    record_read,
    stored_keys,
    write_chat_delta,
)

MAX_TRIES = 3
UNTIMED = ":untimed"

TAKEN_IN = "taken_in"
NOTHING_NEW = "nothing_new"
KEPT_OUT = "kept_out"
FAILED = "failed"
_OUTCOME = {TAKEN_IN: "took_in", NOTHING_NEW: "nothing_new", KEPT_OUT: "kept_out", FAILED: "failed"}


class UnknownProvider(ValueError):
    """No connector reads this provider code."""


class KeyChanged(RuntimeError):
    """Screening changed a message key or the locator. Keys are opaque, so this is a fault, and
    the chat is not stored (fail closed)."""


@dataclass
class TakeInResult:
    state: str
    provider: str
    connector_id: str
    chats: int = 0
    chats_new: int = 0
    sources_added: int = 0
    episodes_added: int = 0
    messages_read: int = 0
    messages_kept: int = 0
    failed_chats: int = 0
    screened: dict[str, int] = field(default_factory=dict)
    source_ids: list[str] = field(default_factory=list)
    episode_ids: list[str] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        """Counts and ids only: never text, never a key."""
        return {
            "state": self.state,
            "provider": self.provider,
            "connector_id": self.connector_id,
            "chats": self.chats,
            "chats_new": self.chats_new,
            "sources_added": self.sources_added,
            "episodes_added": self.episodes_added,
            "messages_read": self.messages_read,
            "messages_kept": self.messages_kept,
            "failed_chats": self.failed_chats,
            "screened": dict(sorted(self.screened.items())),
        }


def _messages(delta: list[ParsedMessage]) -> list[Message]:
    """The delta as the screener takes it, each text capped before a cut marker: the screener's
    working copies grow with the text, and one crafted message must not take the API's memory."""
    return [
        Message(
            key=message.key,
            sender=None if message.sender is None else kept_text(message.sender, MAX_SENDER_CHARS),
            text=kept_text(message.text),
            at=message.at,
            speaker_kind=message.speaker_kind,
            modality=message.modality,
        )
        for message in delta
    ]


def _kept(
    screened: ScreenedSource, locator: str, delta: list[ParsedMessage]
) -> list[ParsedMessage]:
    """The parsed messages screening kept, in order. Each kept key must be a delta key, once."""
    if screened.locator != locator:
        raise KeyChanged()
    by_key = {message.key: message for message in delta}
    kept: list[ParsedMessage] = []
    for message in screened.messages:
        original = by_key.pop(message.key, None)
        if original is None:
            raise KeyChanged()
        kept.append(original)
    return kept


def _times(group: list[ParsedMessage], chat: ParsedChat) -> tuple[datetime, datetime] | None:
    moments = [message.at for message in group if message.at is not None]
    if moments:
        return min(moments), max(moments)
    start = chat.started_at or chat.ended_at
    if start is None:
        return None
    return start, max(start, chat.ended_at or start)


def cut_episodes(
    spec: ConnectorSpec, chat: ParsedChat, kept: list[ParsedMessage], now: datetime
) -> list[EpisodeCut]:
    """The connector's cut rule over the kept messages. A group with no time of its own takes the
    chat's times; with none at all, the read time, and its cut rule says `:untimed`."""
    cuts: list[EpisodeCut] = []
    for group in spec.cut(kept):
        if not group:
            continue
        times = _times(group, chat)
        rule = spec.cut_rule if times is not None else spec.cut_rule + UNTIMED
        start, end = times if times is not None else (now, now)
        cuts.append(
            EpisodeCut(
                first_message_key=group[0].key,
                last_message_key=group[-1].key,
                start_time=start,
                end_time=end,
                cut_rule=rule,
            )
        )
    return cuts


def _size(message: ParsedMessage) -> int:
    sender = 0 if message.sender is None else len(kept_text(message.sender, MAX_SENDER_CHARS))
    return len(kept_text(message.text)) + sender


def parts_of(delta: list[ParsedMessage]) -> list[list[ParsedMessage]]:
    """Consecutive parts with at most MAX_SOURCE_CHARS of capped text and sender characters
    each (one message at least). JSON metadata and screening replacements are not counted."""
    parts: list[list[ParsedMessage]] = []
    size = 0
    for message in delta:
        length = _size(message)
        if not parts or size + length > MAX_SOURCE_CHARS:
            parts.append([])
            size = 0
        parts[-1].append(message)
        size += length
    return parts


def _take_chat(
    driver: Driver,
    world_id: str,
    spec: ConnectorSpec,
    connector_id: str,
    chat: ParsedChat,
    screener: Screener,
    result: TakeInResult,
) -> None:
    """Screen bounded source parts, cut episodes over the whole delta, then commit atomically.
    Source storage limits must not inflate the episode count used by evidence thresholds."""
    locator = _chat_locator(spec.key_prefix, chat.chat_id)
    query = WorldQuery(driver, world_id)
    for _ in range(MAX_TRIES):
        stored, _sources = stored_keys(query, connector_id, locator)
        delta = [message for message in chat.messages if message.key not in stored]
        if not delta:
            return
        screened_parts = []
        kept = []
        for part in parts_of(delta):
            screened = screener.screen_source(locator, _messages(part))
            kept.extend(_kept(screened, locator, part))
            screened_parts.append(screened)
        cuts = cut_episodes(spec, chat, kept, clock.now(world_id))
        moments = [message.at for message in delta if message.at is not None]
        try:
            written = write_chat_delta(
                driver,
                world_id,
                screened_parts,
                connector_id=connector_id,
                parser_version=spec.parser_version,
                delta_keys=[message.key for message in delta],
                episodes=cuts,
                newest_at=max(moments) if moments else None,
            )
        except DeltaMoved:
            continue
        if written is None:
            return
        result.chats_new += 1
        result.sources_added += len(written["source_ids"])
        result.episodes_added += len(written["episode_ids"])
        result.messages_read += len(delta)
        result.messages_kept += written["kept_messages"]
        result.source_ids.extend(written["source_ids"])
        result.episode_ids.extend(written["episode_ids"])
        for cls, count in written["counts"].items():
            result.screened[cls] = result.screened.get(cls, 0) + count
        return
    raise DeltaMoved()


def take_in(
    driver: Driver, world_id: str, provider: str, upload: Upload, *, screener: Screener
) -> TakeInResult:
    """Parse, screen and store one upload. A file that is not an export of `provider` raises
    NotAnExport before anything is written."""
    spec = spec_for(provider)
    if spec is None:
        raise UnknownProvider(provider)
    if not isinstance(screener, Screener):
        raise TypeError("take_in needs a Screener")
    attempted = clock.now(world_id)
    chats = list(spec.parse(upload))
    connector_id = ensure_connector(
        driver, world_id, spec.connector_type, spec.provider, batch_rule=spec.batch_rule
    )
    result = TakeInResult(state=NOTHING_NEW, provider=spec.provider, connector_id=connector_id)
    result.chats = len(chats)
    for chat in chats:
        try:
            _take_chat(driver, world_id, spec, connector_id, chat, screener, result)
        except (KeyChanged, DeltaMoved, InvalidRecord):
            result.failed_chats += 1
    if result.messages_kept:
        result.state = TAKEN_IN
    elif result.sources_added:
        result.state = KEPT_OUT
    elif result.failed_chats:
        result.state = FAILED
    record_read(
        driver,
        world_id,
        connector_id,
        outcome=_OUTCOME[result.state],
        failed=result.failed_chats,
        attempted_at=attempted,
    )
    return result
