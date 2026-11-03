"""Connectors, sources and episodes (D2, D14): the rows an evidence row cites.

Each record_ call is one transaction of its own. These are snapshot rows (D3), so they carry a
version and a touched-at time (S12) and no change entries.

Text from a connector reaches :Source only through write_screened_source or add_screened_source.
They take a ScreenedSource, which only the Screener makes. The receipt holds counts per class, never
a value. record_source takes text as given, for the writer's own tests: a test fails when code
outside the writer package reaches for it (tests/screening/test_source_path.py).

Screening is code only and never waits on a model, so it finishes before the writer opens a
transaction. The caller mints the source id with new_id(world_id), screens, and then commits the
source, its receipt and the request row that names the source in one write transaction (ruling SC1
of docs/store-map.md). A request never names a source that is not there, and raw text is never
stored first.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.screening import ScreenedSource
from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import InvalidRecord
from grove.writer.history import compact
from grove.writer.tx import WorldTx, WorldWriter

_FIRST_VERSION = 1
_KEPT = "kept"
_SCREENED_WHERE = "server"


def _now(world_id: str) -> datetime:
    return clock.now(world_id)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecord("invalid_arguments", "type", f"{name} must be non-empty text")
    return value


def _instant(value: Any, name: str) -> datetime:
    try:
        return parse_instant(value)
    except ClockError as error:
        raise InvalidRecord("invalid_arguments", error.rule, f"{name}: {error}") from error


_SPEAKER_KINDS = frozenset(ENUMS["speaker_kind"])
_SPEAKER_KEYS = frozenset({"kind", "sender", "original_author"})
_META_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def speakers_json(speakers: Any) -> str | None:
    """The speakers the connector's code read from a source's own metadata (S46): message key to
    {kind, sender, original_author}. `sender` is a code the connector chose (never an address),
    and `original_author` an item id. Only code that parses a source sets these, so the writer
    lets them win over a caller's classification (D5: metadata first)."""
    if speakers is None:
        return None
    if not isinstance(speakers, Mapping):
        raise InvalidRecord("invalid_arguments", "type", "speakers maps message keys to speakers")
    checked: dict[str, dict[str, str]] = {}
    for key, entry in speakers.items():
        if not isinstance(key, str) or _META_CODE.fullmatch(key) is None:
            raise InvalidRecord("invalid_arguments", "type", "a speakers key is a message key")
        if not isinstance(entry, Mapping) or set(entry) - _SPEAKER_KEYS:
            raise InvalidRecord(
                "invalid_arguments", "type", "a speaker is {kind, sender, original_author}"
            )
        if entry.get("kind") not in _SPEAKER_KINDS:
            raise InvalidRecord("invalid_arguments", "enum", "a speaker kind is a speaker_kind")
        for name in ("sender", "original_author"):
            value = entry.get(name)
            if value is not None and (
                not isinstance(value, str) or _META_CODE.fullmatch(value) is None
            ):
                raise InvalidRecord("invalid_arguments", "type", f"{name} is a code")
        checked[key] = {name: value for name, value in entry.items() if value is not None}
    return json.dumps(checked, sort_keys=True) if checked else None


def _connector(tx: WorldTx, connector_id: str) -> dict[str, Any]:
    rows = tx.run("MATCH (c:Connector {id: $id}) RETURN c", id=connector_id)
    if not rows:
        raise InvalidRecord("not_found", "connector_not_found", "no such connector in this world")
    return rows[0]["c"]


def record_connector(
    driver: Driver,
    world_id: str,
    type: str,
    *,
    settings: dict[str, Any] | None = None,
    staleness_limit: int | None = None,
) -> str:
    if type not in ENUMS["connector_type"]:
        raise InvalidRecord(
            "invalid_arguments", "enum", f"connector type must be one of {ENUMS['connector_type']}"
        )
    if settings is not None and not isinstance(settings, dict):
        raise InvalidRecord("invalid_arguments", "type", "settings is an object")
    if staleness_limit is not None and (
        isinstance(staleness_limit, bool) or not isinstance(staleness_limit, int)
    ):
        raise InvalidRecord("invalid_arguments", "type", "staleness_limit is whole days")

    def work(tx: WorldTx) -> str:
        at = _now(world_id)
        row = compact(
            {
                "id": new_id(world_id),
                "type": type,
                "settings": json.dumps(settings, sort_keys=True) if settings else None,
                "staleness_limit": staleness_limit,
                "version": _FIRST_VERSION,
                "touched_at": at,
            }
        )
        tx.run("CREATE (c:Connector) SET c += $row", row=row)
        return row["id"]

    return WorldWriter(driver, world_id).transaction(work)


def record_source(
    driver: Driver,
    world_id: str,
    *,
    connector_id: str,
    external_locator: str,
    content: str | None = None,
    content_pointer: str | None = None,
    parser_version: str,
    untrusted: bool = False,
    speakers: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    _text(connector_id, "connector_id")
    speakers_text = speakers_json(speakers)
    _text(external_locator, "external_locator")
    _text(parser_version, "parser_version")
    if (content is None) == (content_pointer is None):
        raise InvalidRecord(
            "invalid_arguments", "content_or_pointer", "a source holds its content or a pointer"
        )
    if content is not None and not isinstance(content, str):
        raise InvalidRecord("invalid_arguments", "type", "content is text")
    if content_pointer is not None:
        _text(content_pointer, "content_pointer")

    def work(tx: WorldTx) -> str:
        _connector(tx, connector_id)
        at = _now(world_id)
        row = compact(
            {
                "id": new_id(world_id),
                "connector_id": connector_id,
                "external_locator": external_locator,
                "received_at": at,
                "content": content,
                "content_pointer": content_pointer,
                "parser_version": parser_version,
                "untrusted": bool(untrusted),
                "speakers": speakers_text,
                "forget_state": _KEPT,
                "version": _FIRST_VERSION,
                "touched_at": at,
            }
        )
        tx.run("CREATE (s:Source) SET s += $row", row=row)
        return row["id"]

    return WorldWriter(driver, world_id).transaction(work)


def record_episode(
    driver: Driver,
    world_id: str,
    *,
    connector_id: str,
    source_ids: list[str],
    first_message_key: str,
    last_message_key: str,
    start_time: datetime | str,
    end_time: datetime | str,
    cut_rule: str,
    run_id: str | None = None,
) -> str:
    """Keyed on (connector, first message key): a later cut of the same window keeps the id,
    adds its sources and extends the range (line 761)."""
    _text(connector_id, "connector_id")
    _text(first_message_key, "first_message_key")
    _text(last_message_key, "last_message_key")
    _text(cut_rule, "cut_rule")
    if not isinstance(source_ids, list) or not source_ids:
        raise InvalidRecord("invalid_arguments", "type", "source_ids is a non-empty list")
    for source_id in source_ids:
        _text(source_id, "source_ids")
    start, end = _instant(start_time, "start_time"), _instant(end_time, "end_time")
    if end < start:
        raise InvalidRecord(
            "end_before_start", "end_before_start", "the episode ends before it starts"
        )
    if run_id is not None:
        _text(run_id, "run_id")

    def work(tx: WorldTx) -> str:
        _connector(tx, connector_id)
        found = tx.run(
            "MATCH (s:Source) WHERE s.id IN $ids RETURN s.id AS id, s.connector_id AS connector",
            ids=source_ids,
        )
        by_id = {row["id"]: row["connector"] for row in found}
        for source_id in source_ids:
            if by_id.get(source_id) != connector_id:
                raise InvalidRecord(
                    "missing_provenance",
                    "source_not_of_connector",
                    "every source of an episode is a source of its connector",
                )
        at = _now(world_id)
        existing = tx.run(
            "MATCH (e:Episode {connector_id: $connector, first_message_key: $key}) "
            "SET e.touched_at = $at RETURN e",
            connector=connector_id,
            key=first_message_key,
            at=at,
        )
        if not existing:
            row = compact(
                {
                    "id": new_id(world_id),
                    "connector_id": connector_id,
                    "source_ids": list(dict.fromkeys(source_ids)),
                    "first_message_key": first_message_key,
                    "last_message_key": last_message_key,
                    "start_time": start,
                    "end_time": end,
                    "cut_rule": cut_rule,
                    "read_at": at,
                    "run_id": run_id,
                    "version": _FIRST_VERSION,
                    "touched_at": at,
                }
            )
            tx.run("CREATE (e:Episode) SET e += $row", row=row)
            return row["id"]
        episode = existing[0]["e"]
        columns: dict[str, Any] = {
            "source_ids": list(dict.fromkeys([*(episode.get("source_ids") or []), *source_ids])),
            "read_at": at,
        }
        if episode.get("end_time") is None or end > episode["end_time"]:
            columns |= {"end_time": end, "last_message_key": last_message_key}
        if episode.get("start_time") is None or start < episode["start_time"]:
            columns["start_time"] = start
        if run_id is not None:
            columns["run_id"] = run_id
        tx.run(
            "MATCH (e:Episode {id: $id}) SET e += $columns, e.version = coalesce(e.version, 0) + 1",
            id=episode["id"],
            columns=columns,
        )
        return episode["id"]

    return WorldWriter(driver, world_id).transaction(work)


_CLASSES = frozenset(ENUMS["screening_class"])
# The text form of grove.ids.new_id: lowercase UUID, version 7, the RFC variant (S4).
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# A code is machine text. The form keeps free text, and so a value, out of parser_version.
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

_ADD_SOURCE = "CREATE (s:Source) SET s += $row"
_ADD_RECEIPT = "CREATE (r:ScreeningReceipt) SET r += $row"


def _world_of(tx: WorldTx) -> str:
    # The world id is bound by the layer and never passed as a parameter.
    return tx.world_id


def _check_id(name: str, value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} is not an id from new_id")
    return value


def _checked_counts(counts: object) -> dict[str, int]:
    """The receipt's counts. No message names a key or a value: a bad key may be a value."""
    if not isinstance(counts, Mapping):
        raise TypeError("the counts of a screened source must be a mapping")
    checked: dict[str, int] = {}
    for key, value in counts.items():
        if type(key) is not str or key not in _CLASSES:
            raise ValueError("the counts hold a key outside the screening_class codes")
        # type() and not isinstance(): True is an int to isinstance.
        if type(value) is not int:
            raise TypeError("a count of a screened source is not an int")
        # The screener keeps only the classes that occurred.
        if value < 1:
            raise ValueError("a count of a screened source is below 1")
        checked[key] = value
    return checked


def write_screened_source(
    tx: WorldTx,
    screened: ScreenedSource,
    *,
    source_id: str,
    connector_id: str,
    parser_version: str,
    speakers: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Write one :Source and its :ScreeningReceipt in the caller's transaction.

    It returns the source id, the counts per class and the number of kept messages: no text.
    """
    if type(tx) is not WorldTx:
        raise TypeError("write_screened_source takes a WorldTx")
    # type() and not isinstance(): a subclass could carry text that never went through screening.
    if type(screened) is not ScreenedSource:
        raise TypeError("write_screened_source takes a ScreenedSource from the Screener")
    _check_id("source_id", source_id)
    _check_id("connector_id", connector_id)
    if type(parser_version) is not str or _CODE.fullmatch(parser_version) is None:
        raise ValueError("parser_version is not a code")
    counts = _checked_counts(screened.counts)
    speakers_text = speakers_json(speakers)
    _connector(tx, connector_id)

    at = clock.now(_world_of(tx))
    tx.run(
        _ADD_SOURCE,
        row={
            "id": source_id,
            "connector_id": connector_id,
            "external_locator": screened.locator,
            "received_at": at,
            "content": screened.content(),
            "parser_version": parser_version,
            "untrusted": True,
            **({"speakers": speakers_text} if speakers_text else {}),
            "forget_state": _KEPT,
            "version": _FIRST_VERSION,
            "touched_at": at,
        },
    )
    tx.run(
        _ADD_RECEIPT,
        row={
            "source_id": source_id,
            "counts_per_class": json.dumps(counts, sort_keys=True),
            "screened_at": at,
            "screened_where": _SCREENED_WHERE,
        },
    )
    return {"source_id": source_id, "counts": counts, "kept_messages": len(screened.messages)}


def add_screened_source(
    writer: WorldWriter,
    screened: ScreenedSource,
    *,
    connector_id: str,
    parser_version: str,
    source_id: str | None = None,
    speakers: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """write_screened_source in a transaction of its own. With no source_id it mints one."""

    def work(tx: WorldTx) -> dict[str, Any]:
        # Minted inside the work: a retried transaction mints again, and only the id that
        # committed is returned.
        chosen = new_id(_world_of(tx)) if source_id is None else source_id
        return write_screened_source(
            tx,
            screened,
            source_id=chosen,
            connector_id=connector_id,
            parser_version=parser_version,
            speakers=speakers,
        )

    return writer.transaction(work)
