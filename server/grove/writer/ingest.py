"""The writes of an upload (A.4, D10, D13): the connector row, one chat's new messages as a
screened source parts with their logical episodes, and the connector's coverage.

Screening is code only and finishes before a transaction opens (SC1). One chat's delta commits
whole: its sources, their screening receipts, its episodes and the connector's watermark move in one
transaction, under the connector row's lock, so two uploads of one export cannot both store the
same messages. The pipeline is grove.ingest.take_in; docs/ingest.md has the contract.
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.screening import ScreenedSource
from grove.store.enums import ENUMS
from grove.writer.connectors import ConnectionBusy, require_sync
from grove.writer.errors import InvalidRecord
from grove.writer.history import compact
from grove.writer.sources import write_screened_source
from grove.writer.tx import WorldTx, WorldWriter

_FIRST_VERSION = 1
_WATERMARK_PREFIX = "at:"
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")

_FIND_CONNECTOR = (
    "MATCH (c:Connector {type: $type, provider: $provider}) RETURN c.id AS id ORDER BY c.id LIMIT 1"
)
_ADD_CONNECTOR = "CREATE (c:Connector) SET c += $row"
# A marker set and removed in one statement takes the row's write lock and changes nothing, as
# WorldTx.lock_world does for the world (S41).
_LOCK_CONNECTOR = (
    "MATCH (c:Connector {id: $id}) SET c.lock_marker = true REMOVE c.lock_marker "
    "RETURN c.watermark AS watermark, c.oldest_unprocessed_item AS oldest, "
    "c.read_count AS read_count, c.provider AS provider"
)
_STORED = (
    "MATCH (s:Source {connector_id: $connector, external_locator: $locator}) "
    "RETURN s.content AS content"
)
_FIND_EPISODE = (
    "MATCH (e:Episode {connector_id: $connector, first_message_key: $key}) RETURN e.id AS id"
)
_ADD_EPISODE = "CREATE (e:Episode) SET e += $row"
_MOVE_CONNECTOR = (
    "MATCH (c:Connector {id: $id}) SET c += $columns, c.version = coalesce(c.version, 0) + 1"
)
_READ_OUTCOME = (
    "MATCH (c:Connector {id: $id}) SET c += $columns, "
    "c.failed_count = coalesce(c.failed_count, 0) + $failed, "
    "c.version = coalesce(c.version, 0) + 1 RETURN c.id AS id"
)


class DeltaMoved(RuntimeError):
    """Another upload stored some of this chat's new messages first. Read the delta again."""


@dataclass(frozen=True)
class EpisodeCut:
    """One episode of a chat's delta, cut by the connector's rule (D10): its range and times."""

    first_message_key: str
    last_message_key: str
    start_time: datetime
    end_time: datetime
    cut_rule: str


def _code(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= 80
        or not value[0].isalnum()
        or not set(value) <= _CODE_CHARS
    ):
        raise InvalidRecord("invalid_arguments", "type", f"{name} must be a code")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidRecord("invalid_arguments", "type", f"{name} must be an aware datetime")
    return value


def ensure_connector(
    driver: Driver,
    world_id: str,
    connector_type: str,
    provider: str,
    *,
    batch_rule: str,
    auth_reference: str = "upload",
) -> str:
    """The world's connector row for this type and provider, made once. The world lock makes
    two first uploads that race make one row."""
    if connector_type not in ENUMS["connector_type"]:
        raise InvalidRecord("invalid_arguments", "enum", "connector_type is not a connector type")
    _code(provider, "provider")
    _code(batch_rule, "batch_rule")
    _code(auth_reference, "auth_reference")

    def work(tx: WorldTx) -> str:
        tx.lock_world()
        found = tx.run(_FIND_CONNECTOR, type=connector_type, provider=provider)
        if found:
            return found[0]["id"]
        at = clock.now(tx.world_id)
        row = {
            "id": new_id(tx.world_id),
            "type": connector_type,
            "provider": provider,
            "auth_reference": auth_reference,
            "batch_rule": batch_rule,
            "read_count": 0,
            "failed_count": 0,
            "version": _FIRST_VERSION,
            "touched_at": at,
        }
        tx.run(_ADD_CONNECTOR, row=row)
        return row["id"]

    return WorldWriter(driver, world_id).transaction(work)


def keys_of_content(content: object) -> set[str]:
    """The message keys of one stored source's content (the screener's JSON array)."""
    if not isinstance(content, str) or not content:
        return set()
    try:
        rows = json.loads(content)
    except ValueError:
        return set()
    if not isinstance(rows, list):
        return set()
    return {row["key"] for row in rows if isinstance(row, dict) and isinstance(row.get("key"), str)}


def stored_keys(tx_or_query: Any, connector_id: str, locator: str) -> tuple[set[str], int]:
    """The keys already stored for one chat, and how many sources hold the chat. Takes a WorldTx
    or a WorldQuery: the same read before screening and again under the lock."""
    if isinstance(tx_or_query, WorldTx):
        rows = tx_or_query.run(_STORED, connector=connector_id, locator=locator)
    else:
        rows = tx_or_query.read(_STORED, connector=connector_id, locator=locator)
    keys: set[str] = set()
    for row in rows:
        keys |= keys_of_content(row["content"])
    return keys, len(rows)


def write_chat_delta(
    driver: Driver,
    world_id: str,
    screened: ScreenedSource | Sequence[ScreenedSource],
    *,
    connector_id: str,
    parser_version: str,
    delta_keys: Sequence[str],
    episodes: Sequence[EpisodeCut],
    newest_at: datetime | None,
    calendar_events: dict[str, dict] | None = None,
    speakers: dict[str, dict] | None = None,
    sync_owner: str | None = None,
) -> dict[str, Any] | None:
    """One chat's new messages, in one transaction: its source parts and receipts, its episodes
    and the connector's watermark and coverage. Raises DeltaMoved when another upload stored any of
    `delta_keys` first. A delta that screening dropped whole writes its empty source only for a
    chat with no source yet, so a re-import stays idempotent; then it returns None."""
    if type(screened) is ScreenedSource:
        parts = [screened]
    elif isinstance(screened, (list, tuple)) and screened:
        parts = list(screened)
    else:
        raise TypeError("write_chat_delta takes screened source parts from the Screener")
    if any(type(part) is not ScreenedSource for part in parts):
        raise TypeError("write_chat_delta takes screened source parts from the Screener")
    locator = parts[0].locator
    if any(part.locator != locator for part in parts):
        raise InvalidRecord("invalid_arguments", "sources", "source parts need one locator")
    keys = frozenset(delta_keys)
    if not keys:
        raise InvalidRecord("invalid_arguments", "type", "a delta holds at least one key")
    messages = [
        (message.key, index) for index, part in enumerate(parts) for message in part.messages
    ]
    if bool(messages) != bool(episodes):
        raise InvalidRecord(
            "invalid_arguments", "episodes", "kept messages need episodes, and no messages none"
        )
    cuts = [_checked_cut(cut) for cut in episodes]
    positions = {key: index for index, (key, _) in enumerate(messages)}
    if len(positions) != len(messages) or not positions.keys() <= keys:
        raise InvalidRecord("invalid_arguments", "sources", "source keys need a unique delta")
    for facts in (calendar_events, speakers):
        if facts is not None and (
            not isinstance(facts, dict) or not facts.keys() <= positions.keys()
        ):
            raise InvalidRecord(
                "invalid_arguments", "message_facts", "facts need retained message keys"
            )
    cut_parts = []
    next_position = 0
    for cut in cuts:
        first = positions.get(cut.first_message_key)
        last = positions.get(cut.last_message_key)
        if first != next_position or last is None or last < first:
            raise InvalidRecord(
                "invalid_arguments", "episodes", "episode ranges must cover the delta"
            )
        cut_parts.append(list(dict.fromkeys(index for _, index in messages[first : last + 1])))
        next_position = last + 1
    if next_position != len(messages):
        raise InvalidRecord("invalid_arguments", "episodes", "episode ranges must cover the delta")
    if newest_at is not None:
        _aware(newest_at, "newest_at")

    def work(tx: WorldTx) -> dict[str, Any] | None:
        if sync_owner is not None:
            require_sync(tx, connector_id, sync_owner)
        locked = tx.run(_LOCK_CONNECTOR, id=connector_id)
        if not locked:
            raise InvalidRecord("not_found", "connector_not_found", "no such connector here")
        if locked[0]["provider"] in ("google_calendar", "git") and sync_owner is None:
            raise ConnectionBusy()
        stored, sources = stored_keys(tx, connector_id, locator)
        if stored & keys:
            raise DeltaMoved()
        if not messages and sources:
            return None
        source_ids = []
        counts: Counter[str] = Counter()
        for part in parts:
            part_keys = {message.key for message in part.messages}
            source_id = new_id(tx.world_id)
            written = write_screened_source(
                tx,
                part,
                source_id=source_id,
                connector_id=connector_id,
                parser_version=parser_version,
                calendar_events=(
                    {key: value for key, value in calendar_events.items() if key in part_keys}
                    if calendar_events is not None
                    else None
                ),
                speakers=(
                    {key: value for key, value in speakers.items() if key in part_keys}
                    if speakers is not None
                    else None
                ),
            )
            source_ids.append(source_id)
            counts.update(written["counts"])
        at = clock.now(tx.world_id)
        episode_ids = [
            _write_episode(tx, connector_id, [source_ids[index] for index in indices], cut, at)
            for cut, indices in zip(cuts, cut_parts, strict=True)
        ]
        _move_connector(tx, connector_id, locked[0], cuts, newest_at, at, len(episode_ids))
        return {
            "source_id": source_ids[0],
            "source_ids": source_ids,
            "counts": dict(counts),
            "kept_messages": len(messages),
            "episode_ids": episode_ids,
        }

    return WorldWriter(driver, world_id).transaction(work)


def _checked_cut(cut: object) -> EpisodeCut:
    if not isinstance(cut, EpisodeCut):
        raise TypeError("episodes holds EpisodeCut rows")
    _code(cut.first_message_key, "first_message_key")
    _code(cut.last_message_key, "last_message_key")
    _code(cut.cut_rule, "cut_rule")
    if _aware(cut.end_time, "end_time") < _aware(cut.start_time, "start_time"):
        raise InvalidRecord("end_before_start", "end_before_start", "the episode ends first")
    return cut


def _write_episode(
    tx: WorldTx, connector_id: str, source_ids: list[str], cut: EpisodeCut, at: datetime
) -> str:
    """Keyed on (connector, first message key): an episode that exists stays as it is."""
    found = tx.run(_FIND_EPISODE, connector=connector_id, key=cut.first_message_key)
    if found:
        return found[0]["id"]
    row = compact(
        {
            "id": new_id(tx.world_id),
            "connector_id": connector_id,
            "source_ids": source_ids,
            "first_message_key": cut.first_message_key,
            "last_message_key": cut.last_message_key,
            "start_time": cut.start_time,
            "end_time": cut.end_time,
            "cut_rule": cut.cut_rule,
            "read_at": at,
            "version": _FIRST_VERSION,
            "touched_at": at,
        }
    )
    tx.run(_ADD_EPISODE, row=row)
    return row["id"]


def _watermark_time(watermark: object) -> datetime | None:
    if not isinstance(watermark, str) or not watermark.startswith(_WATERMARK_PREFIX):
        return None
    try:
        moment = datetime.fromisoformat(watermark.removeprefix(_WATERMARK_PREFIX))
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def _move_connector(
    tx: WorldTx,
    connector_id: str,
    current: dict[str, Any],
    cuts: list[EpisodeCut],
    newest_at: datetime | None,
    at: datetime,
    episodes_added: int,
) -> None:
    columns: dict[str, Any] = {
        "touched_at": at,
        "read_count": (current.get("read_count") or 0) + episodes_added,
    }
    known = _watermark_time(current.get("watermark"))
    if newest_at is not None and (known is None or newest_at > known):
        columns["watermark"] = _WATERMARK_PREFIX + newest_at.isoformat()
    starts = [cut.start_time for cut in cuts]
    oldest = current.get("oldest")
    if starts:
        earliest = min(starts)
        if oldest is None or earliest < oldest:
            columns["oldest_unprocessed_item"] = earliest
    tx.run(_MOVE_CONNECTOR, id=connector_id, columns=columns)


def record_read(
    driver: Driver,
    world_id: str,
    connector_id: str,
    *,
    outcome: str,
    failed: int = 0,
    attempted_at: datetime | None = None,
) -> None:
    """The coverage of one read (D13): when it was tried, when it last worked, its outcome as a
    code, and the failed count. `held nothing` and `extracted` belong to the update agent."""
    if outcome not in ENUMS["connector_outcome"]:
        raise InvalidRecord("invalid_arguments", "enum", "outcome is not a connector outcome")
    if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
        raise InvalidRecord("invalid_arguments", "type", "failed is a count")

    def work(tx: WorldTx) -> None:
        at = clock.now(tx.world_id)
        columns: dict[str, Any] = {
            "last_attempted_read": attempted_at or at,
            "last_outcome": outcome,
            "touched_at": at,
        }
        if outcome != "failed":
            columns["last_successful_read"] = at
        if not tx.run(_READ_OUTCOME, id=connector_id, columns=columns, failed=failed):
            raise InvalidRecord("not_found", "connector_not_found", "no such connector here")

    WorldWriter(driver, world_id).transaction(work)
