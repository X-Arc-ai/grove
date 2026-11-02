"""Connectors, sources and episodes (D2): the rows an evidence row cites.

Each call is one transaction of its own. These are snapshot rows (D3), so they carry a version and
a touched-at time (S12) and no change entries. Screening runs before record_source.
"""

import json
from datetime import datetime
from typing import Any

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import InvalidRecord
from grove.writer.history import compact
from grove.writer.tx import WorldTx, WorldWriter

_FIRST_VERSION = 1
_KEPT = "kept"


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
) -> str:
    _text(connector_id, "connector_id")
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
