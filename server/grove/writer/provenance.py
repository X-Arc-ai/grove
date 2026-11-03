"""Provenance (D2): an evidence row names an episode of a stored source of this world, read from
that source, and a locator that resolves inside it.

Locator schemes are developer config per connector type (S27):
`line:N` or `line:N-M` (1-based lines of the content), `char:A-B` (0 <= A < B <= length),
`key:<token>` (a token of 4 or more characters found in the content), and `whole`.
"""

import json
import re
import zoneinfo
from datetime import UTC, date, datetime
from typing import Any

from grove.writer.clocks import parse_instant
from grove.writer.errors import Refused
from grove.writer.source_types import RowFacts, derive_row_source_type, weakest
from grove.writer.tx import WorldTx

LOCATOR_SCHEMES: dict[str, frozenset[str]] = {
    "calendar": frozenset({"key", "whole"}),
    "whatsapp": frozenset({"line"}),
    "past_ai_conversations": frozenset({"key", "line"}),
    "voice_notes": frozenset({"char", "whole"}),
    "notes_files_releases": frozenset({"line", "char", "whole"}),
    "screenshots": frozenset({"whole"}),
    "git": frozenset({"key"}),
    "web_pages": frozenset({"char", "whole"}),
    "ai_host": frozenset({"whole", "char"}),
    "person_in_views": frozenset({"whole", "char"}),
    "reminders": frozenset({"key", "line", "whole"}),
    "identity": frozenset({"key", "whole"}),
}
# Speaker kinds that cannot decide: a decision is attributed only to a person who spoke the
# deciding sentence (D5 rule 4). An AI host's proposal that the person accepts is the person's
# "yes", a row of its own.
_NOT_DECIDERS = frozenset({"ai_host", "automated"})
_ANSWER_RELAYS = frozenset({"answer", "correction"})
_RELAYED = frozenset({"ai_host", "person_in_views"})
_REQUEST_OF_SOURCE = (
    "MATCH (r:Request {source_id: $id}) RETURN r.verbatim AS verbatim, "
    "r.host_message_id AS host_message_id, r.relay AS relay"
)
MIN_KEY_LENGTH = 4
_LINES = re.compile(r"(\d+)(?:-(\d+))?")
_CHARS = re.compile(r"(\d+)-(\d+)")
_SCRIPTED = "scripted"


def _unresolvable(rule: str, message: str, **details: Any) -> Refused:
    return Refused("unresolvable_locator", rule, message, details)


def check_locator(locator: str, *, content: str | None, connector_type: str) -> None:
    allowed = LOCATOR_SCHEMES.get(connector_type, frozenset())
    scheme, _, rest = locator.partition(":")
    if scheme not in allowed:
        raise _unresolvable(
            "scheme_not_allowed",
            f"a {connector_type} source has no {scheme!r} locators",
            locator=locator,
            valid=sorted(allowed),
        )
    if scheme == "whole":
        if rest:
            raise _unresolvable("bad_locator", "whole takes nothing after it", locator=locator)
        return
    if content is None:
        raise _unresolvable(
            "pointer_only", "a source held as a pointer resolves only `whole`", locator=locator
        )
    if scheme == "line":
        found = _LINES.fullmatch(rest)
        if found is None:
            raise _unresolvable("bad_locator", "a line locator is line:N or line:N-M")
        first = int(found.group(1))
        last = int(found.group(2) or first)
        count = len(content.splitlines())
        if not 1 <= first <= last <= count:
            raise _unresolvable(
                "line_out_of_range", f"the source has {count} lines", locator=locator
            )
    elif scheme == "char":
        found = _CHARS.fullmatch(rest)
        if found is None:
            raise _unresolvable("bad_locator", "a char locator is char:A-B")
        start, end = int(found.group(1)), int(found.group(2))
        if not 0 <= start < end <= len(content):
            raise _unresolvable(
                "char_out_of_range", f"the source has {len(content)} characters", locator=locator
            )
    elif scheme == "key":
        if len(rest) < MIN_KEY_LENGTH or rest not in content:
            raise _unresolvable("key_not_found", "the key does not occur in the source")


def connector_type_of(connector: dict[str, Any]) -> str:
    """A scripted connector stands in for the type it imitates (A.6)."""
    kind = connector.get("type")
    if kind == _SCRIPTED:
        settings = json.loads(connector.get("settings") or "{}")
        return settings.get("imitates") or _SCRIPTED
    return kind


def _speaker_meta(source: dict[str, Any], message_key: str) -> dict[str, Any]:
    """The speaker that the connector's code stored for this message (S46), or {}."""
    meta = json.loads(source.get("speakers") or "{}")
    found = meta.get(message_key)
    return found if isinstance(found, dict) else {}


def _local_day(at: datetime, zone: str | None) -> date:
    try:
        tz = zoneinfo.ZoneInfo(zone) if zone else UTC
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return at.astimezone(tz).date()


def calendar_logged(
    connector: dict[str, Any], source: dict[str, Any], source_time: datetime, zone: str | None
) -> bool:
    """D5's log setting: an entry dated before the read, on a day the person keeps as a log (from
    the setting's start date, or a day marked as logged). Anything else stays planned."""
    settings = json.loads(connector.get("settings") or "{}")
    read_at = source.get("received_at")
    if read_at is None or not source_time < read_at:
        return False
    day = _local_day(source_time, zone)
    logged_days = {str(value) for value in settings.get("logged_days") or []}
    if day.isoformat() in logged_days:
        return True
    start = settings.get("log_from")
    if not isinstance(start, str):
        return False
    try:
        return day >= date.fromisoformat(start)
    except ValueError:
        return False


def _relay(tx: WorldTx, source_id: str) -> tuple[str | None, bool]:
    """The relay and the verbatim flag of the request that stored this source (S48)."""
    rows = tx.run(_REQUEST_OF_SOURCE, id=source_id)
    if not rows:
        return None, False
    row = rows[0]
    return row.get("relay") or "remember", bool(row.get("verbatim") and row.get("host_message_id"))


def _one(tx: WorldTx, cypher: str, **params: Any) -> dict[str, Any] | None:
    rows = tx.run(cypher, **params)
    return rows[0]["n"] if rows else None


def resolve_observation(tx: WorldTx, observation: dict[str, Any]) -> dict[str, Any]:
    """The observation as the columns of an evidence row (no id, target or polarity yet)."""
    source = _one(tx, "MATCH (n:Source {id: $id}) RETURN n", id=observation["source_id"])
    if source is None:
        raise Refused(
            "missing_provenance",
            "source_not_in_world",
            "the cited source is not a stored source of this world",
            {"ev_id": observation["ev_id"]},
        )
    if source.get("forget_state") == "forgotten":
        raise Refused(
            "unresolvable_locator",
            "source_forgotten",
            "the cited source was forgotten",
            {"ev_id": observation["ev_id"]},
        )
    episode = _one(tx, "MATCH (n:Episode {id: $id}) RETURN n", id=observation["episode_id"])
    if episode is None:
        raise Refused(
            "missing_provenance",
            "episode_not_in_world",
            "the cited episode is not an episode of this world",
            {"ev_id": observation["ev_id"]},
        )
    if observation["source_id"] not in (episode.get("source_ids") or []):
        raise Refused(
            "missing_provenance",
            "episode_not_from_source",
            "the episode was not read from the cited source",
            {"ev_id": observation["ev_id"]},
        )
    connector = _one(tx, "MATCH (n:Connector {id: $id}) RETURN n", id=source.get("connector_id"))
    if connector is None:
        raise Refused(
            "missing_provenance",
            "connector_not_in_world",
            "the source's connector is not in this world",
            {"ev_id": observation["ev_id"]},
        )
    connector_type = connector_type_of(connector)
    check_locator(
        observation["locator"], content=source.get("content"), connector_type=connector_type
    )
    for key in ("speaker_item", "original_author"):
        if observation.get(key) is not None:
            if _one(tx, "MATCH (n:Item {id: $id}) RETURN n", id=observation[key]) is None:
                raise Refused(
                    "not_found",
                    key,
                    f"{key} names no item of this world",
                    {"item": observation[key]},
                )

    source_time = parse_instant(observation["source_time"])
    statement_class = observation["statement_class"]
    # Speaker and modality come from metadata first (D5): the connector's code stored them on the
    # source, and they win over the caller's classification.
    meta = _speaker_meta(source, observation["message_key"])
    if meta and meta.get("locator") != observation["locator"]:
        # The metadata names where its message sits in the source; a row that borrows the key
        # for another place would take another message's speaker (S46).
        raise Refused(
            "invalid_arguments",
            "message_key_locator",
            "the message key names another place in the source than this locator",
            {"ev_id": observation["ev_id"]},
        )
    speaker_kind = meta.get("kind") or observation["speaker_kind"]
    original_author = meta.get("original_author") or observation.get("original_author")
    sender = meta.get("sender")
    relay, verbatim = (None, False)
    if connector_type in _RELAYED:
        relay, verbatim = _relay(tx, observation["source_id"])
    if connector_type == "ai_host":
        if relay in _ANSWER_RELAYS and not verbatim:
            raise Refused(
                "verbatim_required",
                "verbatim_required",
                "an answer or a correction from a host carries the person's own words and the "
                "id of the host message",
                {"ev_id": observation["ev_id"]},
            )
        # The person speaks through the host only in their own words (D5); else the host does.
        speaker_kind = "person" if verbatim else "ai_host"
    if speaker_kind == "forwarded" and not original_author:
        raise Refused(
            "invalid_arguments",
            "original_author_required",
            "a forwarded message keeps its original author",
            {"ev_id": observation["ev_id"]},
        )
    if statement_class == "decision" and speaker_kind in _NOT_DECIDERS:
        raise Refused(
            "invalid_arguments",
            "decider_not_speaker",
            "a decision names its decider as the speaker of the deciding sentence; a proposal "
            "nobody accepted stays the proposer's statement",
            {"ev_id": observation["ev_id"], "speaker_kind": speaker_kind},
        )
    if original_author is not None and original_author != observation.get("original_author"):
        if _one(tx, "MATCH (n:Item {id: $id}) RETURN n", id=original_author) is None:
            raise Refused(
                "not_found",
                "original_author",
                "original_author names no item of this world",
                {"item": original_author},
            )

    derivation = observation["derivation"]
    premises = list(observation.get("premises") or [])
    if derivation == "observed":
        facts = RowFacts(
            modality=observation["modality"],
            whole=observation["locator"] == "whole",
            automated=meta.get("kind") == "automated",
            logged=(
                connector_type == "calendar"
                and calendar_logged(
                    connector, source, source_time, observation.get("person_timezone")
                )
            ),
            relay=relay,
            verbatim=verbatim,
        )
        source_type = derive_row_source_type(connector_type, statement_class, facts)
        premises = []
    else:
        rows = tx.run(
            "MATCH (e:Evidence) WHERE e.id IN $ids AND e.active = true "
            "RETURN e.id AS id, e.source_type AS source_type",
            ids=premises,
        )
        found = {row["id"]: row["source_type"] for row in rows}
        missing = [premise for premise in premises if premise not in found]
        if missing:
            raise Refused(
                "inferred_evidence",
                "premise_not_found",
                "a premise is not an active evidence row of this world",
                {"premises": missing},
            )
        source_type = weakest([found[premise] for premise in premises])
    return {
        "episode_id": observation["episode_id"],
        "source_id": observation["source_id"],
        "locator": observation["locator"],
        "message_key": observation["message_key"],
        "source_time": source_time,
        "source_offset_minutes": observation.get("source_offset_minutes"),
        "person_timezone": observation.get("person_timezone"),
        "statement_class": statement_class,
        "source_type": source_type,
        "derivation": derivation,
        "premises": premises,
        "speaker_kind": speaker_kind,
        "speaker_item": observation.get("speaker_item"),
        "original_author": original_author,
        "sender": sender,
        "modality": observation["modality"],
    }


# The columns an existing row lends to a new row that cites it by evidence_id.
OBSERVATION_COLUMNS = (
    "episode_id",
    "source_id",
    "locator",
    "message_key",
    "source_time",
    "source_offset_minutes",
    "person_timezone",
    "statement_class",
    "source_type",
    "derivation",
    "premises",
    "speaker_kind",
    "speaker_item",
    "original_author",
    "sender",
    "modality",
)
