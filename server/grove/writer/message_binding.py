"""Exact retained-message checks shared by direct, reused and replayed evidence.

Typed text persistence has no admission contract yet. Neither a valid outer legacy row nor a
saved request can confer that authority. All reads use the caller's world-scoped transaction.
"""

import json
from datetime import datetime
from typing import Any

from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import Refused

MAX_DEPTH = 16
MAX_ROWS = 512
MAX_CONTENT_CHARS = 4_000_000
MAX_MESSAGES = 100_000
_TYPED_PARSERS = frozenset({"whatsapp.export.v1", "document.text.v1"})
_TYPED_PROVIDERS = frozenset({"whatsapp", "document"})
_READ = {
    "Source": "MATCH (n:Source {id: $id}) RETURN n",
    "Connector": "MATCH (n:Connector {id: $id}) RETURN n",
    "Episode": "MATCH (n:Episode {id: $id}) RETURN n",
    "Evidence": "MATCH (n:Evidence {id: $id}) RETURN n",
}


def _bad(rule: str) -> Refused:
    return Refused("missing_provenance", rule, "the retained source does not prove this evidence")


def _object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _bad("source_content_invalid")
        result[key] = value
    return result


def retained_messages(source: dict, connector: dict) -> list[dict] | None:
    """Return unchanged legacy message dictionaries; None denotes raw text or a pointer.

    Detect typed identities even for empty, malformed or pointer-only sources. A typed fact
    on any retained row rejects the complete source, including rows outside the selected range.
    """
    if (
        source.get("parser_version") in _TYPED_PARSERS
        or connector.get("provider") in _TYPED_PROVIDERS
        or "source_facts" in source
    ):
        raise _bad("typed_source_unadmitted")
    content = source.get("content")
    if content is None:
        return None
    if type(content) is not str or len(content) > MAX_CONTENT_CHARS:
        raise _bad("source_content_invalid")
    if not content.lstrip().startswith(("[", "{")):
        return None
    try:
        decoded = json.loads(content, object_pairs_hook=_object)
    except (ValueError, RecursionError):
        raise _bad("source_content_invalid") from None
    pending = [(decoded, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_MESSAGES * 16:
            raise _bad("source_content_invalid")
        if isinstance(value, dict):
            if "source_facts" in value:
                raise _bad("typed_source_unadmitted")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    if type(decoded) is not list or len(decoded) > MAX_MESSAGES:
        raise _bad("source_content_invalid")
    keys = set()
    for message in decoded:
        if type(message) is not dict:
            raise _bad("source_content_invalid")
        key = message.get("key")
        if type(key) is not str or not 1 <= len(key) <= 1024 or key in keys:
            raise _bad("source_content_invalid")
        if type(message.get("text")) is not str or "at" not in message:
            raise _bad("source_content_invalid")
        keys.add(key)
    return decoded


def _read(reader: Any, cypher: str, **params: Any) -> list[dict]:
    method = getattr(reader, "run", None) or reader.read
    return method(cypher, **params)


def _one(reader: Any, label: str, row_id: str) -> dict:
    rows = _read(reader, _READ[label], id=row_id)
    if len(rows) != 1:
        raise _bad("provenance_not_found")
    row = rows[0]["n"]
    world_id = getattr(reader, "world_id", None)
    if world_id is not None and row.get("world_id", world_id) != world_id:
        raise _bad("provenance_not_found")
    if row.get("forget_state") == "forgotten":
        raise _bad("source_forgotten")
    return row


def _source(reader: Any, source_id: str) -> tuple[dict, dict, list[dict] | None]:
    source = _one(reader, "Source", source_id)
    connector = _one(reader, "Connector", source.get("connector_id"))
    return source, connector, retained_messages(source, connector)


def _range(reader: Any, source: dict, episode: dict, message_key: str) -> None:
    ids = episode.get("source_ids")
    if type(ids) is not list or not 1 <= len(ids) <= MAX_ROWS or len(set(ids)) != len(ids):
        raise _bad("episode_message_range")
    positions = {}
    selected = None
    for source_id in ids:
        part, connector, messages = _source(reader, source_id)
        if (
            messages is None
            or part.get("connector_id") != source.get("connector_id")
            or episode.get("connector_id") != part.get("connector_id")
        ):
            raise _bad("episode_message_range")
        for message in messages:
            key = message["key"]
            if key in positions or len(positions) >= MAX_MESSAGES:
                raise _bad("episode_message_range")
            positions[key] = len(positions)
            if source_id == source["id"] and key == message_key:
                selected = positions[key]
    first, last = episode.get("first_message_key"), episode.get("last_message_key")
    if episode.get("cut_rule") == "one_request" and len(ids) == 1:
        # Legacy request Episodes namespace only their endpoints, not retained message keys.
        prefix = source["id"] + ":"
        first = first.removeprefix(prefix) if isinstance(first, str) else first
        last = last.removeprefix(prefix) if isinstance(last, str) else last
    start, end = positions.get(first), positions.get(last)
    if start is None or end is None or selected is None or not start <= selected <= end:
        raise _bad("episode_message_range")


def check_observation_binding(
    reader: Any,
    observation: dict,
    *,
    source: dict | None = None,
    connector: dict | None = None,
    episode: dict | None = None,
) -> dict | None:
    """Bind a legacy structured row to the exact retained key, range, time and metadata."""
    from grove.writer.provenance import LOCATOR_SCHEMES, calendar_logged, connector_type_of

    if source is None or connector is None:
        source, connector, messages = _source(reader, observation.get("source_id"))
    else:
        messages = retained_messages(source, connector)
    episode = episode or _one(reader, "Episode", observation.get("episode_id"))
    if source["id"] not in (episode.get("source_ids") or []):
        raise _bad("episode_not_from_source")
    kind = connector_type_of(connector)
    # Calendar's accepted separate validator controls its schedule/occurrence semantics.
    if kind == "calendar":
        if observation.get("source_type") == "confirmed" and not calendar_logged(
            connector,
            source,
            parse_instant(observation["source_time"]),
            observation.get("person_timezone"),
            message_key=observation["message_key"],
            locator=observation["locator"],
        ):
            raise Refused(
                "invalid_arguments",
                "calendar_binding",
                "Calendar confirmation needs trusted event facts",
            )
        return None
    if messages is None:
        return None
    matches = [message for message in messages if message["key"] == observation.get("message_key")]
    if len(matches) != 1:
        raise _bad("message_key_not_retained")
    message = matches[0]
    _range(reader, source, episode, message["key"])
    try:
        meta = json.loads(source.get("speakers") or "{}")
        if type(meta) is not dict or type(meta.get(message["key"], {})) is not dict:
            raise ValueError
        meta = meta.get(message["key"], {})
    except (ValueError, TypeError):
        raise _bad("message_metadata_invalid") from None
    allowed = LOCATOR_SCHEMES.get(kind, ())
    locator = meta.get("locator")
    if locator is None:
        if "key" in allowed:
            locator = "key:" + message["key"]
        elif "whole" in allowed:
            locator = "whole"
    if locator is None or observation.get("locator") != locator:
        raise _bad("message_key_locator")
    moment = message.get("at")
    if moment is None and kind in {"ai_host", "person_in_views"}:
        requests = _read(
            reader,
            "MATCH (r:Request {source_id: $id}) RETURN r.said_at AS said_at "
            "ORDER BY r.created_at, r.id LIMIT 1",
            id=source["id"],
        )
        moment = requests[0].get("said_at") if requests else None
    try:
        retained_at, proposed_at = (
            parse_instant(moment),
            parse_instant(observation.get("source_time")),
        )
    except (ClockError, ValueError, TypeError):
        raise _bad("message_time_unknown") from None
    if retained_at != proposed_at:
        raise _bad("message_source_time")
    offset = observation.get("source_offset_minutes")
    if offset is not None:
        original = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
        if type(offset) is not int or offset != original.utcoffset().total_seconds() / 60:
            raise _bad("message_source_offset")
    if observation.get("person_timezone") is not None:
        raise _bad("message_person_timezone")
    speaker = meta.get("kind") or message.get("speaker_kind")
    if kind == "person_in_views":
        speaker = "person"
    # The accepted host relay check derives speaker from Request.verbatim and host_message_id.
    if kind != "ai_host":
        if speaker not in ENUMS["speaker_kind"]:
            raise _bad("message_speaker_unknown")
        if observation.get("speaker_kind") != speaker:
            raise _bad("message_speaker")
    modality = message.get("modality") or "text"
    if modality not in ENUMS["modality"] or observation.get("modality") != modality:
        raise _bad("message_modality")
    if observation.get("original_author") != meta.get("original_author"):
        raise _bad("message_original_author")
    return message


def check_evidence_graph(reader: Any, rows: list[dict], *, calendar_check=None) -> None:
    """Check every active premise, including premises hidden below legacy outer rows."""
    seen: dict[str, int] = {}
    active: set[str] = set()
    visits = 0

    def visit(row: dict, depth: int, *, stored: bool) -> None:
        nonlocal visits
        visits += 1
        if depth > MAX_DEPTH or visits > MAX_ROWS * 8:
            raise _bad("evidence_dependency_limit")
        world_id = getattr(reader, "world_id", None)
        if world_id is not None and row.get("world_id", world_id) != world_id:
            raise _bad("provenance_not_found")
        row_id = row.get("id") if stored else None
        if stored:
            if row.get("active") is not True:
                raise _bad("evidence_not_found")
            if row_id in active:
                raise _bad("evidence_dependency_cycle")
            if row_id in seen and seen[row_id] >= depth:
                return
            if not isinstance(row_id, str) or (row_id not in seen and len(seen) >= MAX_ROWS):
                raise _bad("evidence_dependency_limit")
            seen[row_id] = depth
            active.add(row_id)
        check_observation_binding(reader, row)
        if calendar_check is not None:
            calendar_check(row)
        premises = row.get("premises") or []
        if type(premises) is not list or len(premises) > MAX_ROWS:
            raise _bad("evidence_dependency_limit")
        for premise in premises:
            if type(premise) is not str or not 1 <= len(premise) <= 1024:
                raise _bad("evidence_not_found")
            visit(_one(reader, "Evidence", premise), depth + 1, stored=True)
        if stored:
            active.remove(row_id)

    if len(rows) > MAX_ROWS:
        raise _bad("evidence_dependency_limit")
    for row in rows:
        visit(row, 0, stored="id" in row)


def check_reused_evidence(run: Any, rows: list[dict]) -> None:
    from grove.writer.verify import check_calendar_confirmation

    check_evidence_graph(
        run.tx,
        rows,
        calendar_check=lambda row: check_calendar_confirmation(run, row),
    )


def check_batch_sources(reader: Any, batch: Any, request: dict | None = None) -> None:
    """Repeat source checks before replay and under the commit lock, even for unused rows."""
    if request and request.get("source_id"):
        source, connector, messages = _source(reader, request["source_id"])
        if messages is not None:
            from grove.learner_input import select_input

            if not select_input(source, connector, request).complete:
                raise _bad("request_source_held")
    if not isinstance(batch, dict):
        return
    observations = batch.get("evidence") or []
    if isinstance(observations, list):
        valid = [row for row in observations if isinstance(row, dict) and row.get("source_id")]
        check_evidence_graph(reader, valid)
    pending = [(batch.get("ops") or [], 0)]
    evidence_ids = set()
    while pending:
        value, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise _bad("evidence_dependency_limit")
        if isinstance(value, dict):
            if "evidence_id" in value:
                evidence_id = value["evidence_id"]
                if type(evidence_id) is not str or not 1 <= len(evidence_id) <= 1024:
                    raise _bad("evidence_not_found")
                evidence_ids.add(evidence_id)
                if len(evidence_ids) > MAX_ROWS:
                    raise _bad("evidence_dependency_limit")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    check_evidence_graph(reader, [_one(reader, "Evidence", row_id) for row_id in evidence_ids])
