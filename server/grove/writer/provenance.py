"""Provenance (D2): an evidence row names an episode of a stored source of this world, read from
that source, and a locator that resolves inside it.

Locator schemes are developer config per connector type (S27):
`line:N` or `line:N-M` (1-based lines of the content), `char:A-B` (0 <= A < B <= length),
`key:<token>` (a token of 4 or more characters found in the content), and `whole`.
"""

import json
import re
from typing import Any

from grove.writer.clocks import parse_instant
from grove.writer.errors import Refused
from grove.writer.source_types import derive_row_source_type, weakest
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
}
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

    derivation = observation["derivation"]
    premises = list(observation.get("premises") or [])
    if derivation == "observed":
        source_type = derive_row_source_type(connector_type, observation["statement_class"])
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
        "source_time": parse_instant(observation["source_time"]),
        "source_offset_minutes": observation.get("source_offset_minutes"),
        "person_timezone": observation.get("person_timezone"),
        "statement_class": observation["statement_class"],
        "source_type": source_type,
        "derivation": derivation,
        "premises": premises,
        "speaker_kind": observation["speaker_kind"],
        "speaker_item": observation.get("speaker_item"),
        "original_author": observation.get("original_author"),
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
    "modality",
)
