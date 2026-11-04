"""Select retained legacy messages for the Learner without inventing provenance.

This is a projection, not a source admission token. Typed exports remain unavailable.
The writer repeats its own checks when a proposal commits.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import Refused

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,1023}\Z")
_SPEAKERS = frozenset(ENUMS["speaker_kind"])


@dataclass(frozen=True, repr=False)
class SelectedInput:
    messages: tuple[dict[str, Any], ...]
    held_count: int
    reasons: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.held_count == 0 and not self.reasons


def select_input(source: dict, connector: dict, request: dict) -> SelectedInput:
    """Return eligible source messages and explicit accounting for unprocessed input."""
    from grove.writer.message_binding import retained_messages
    from grove.writer.provenance import LOCATOR_SCHEMES, connector_type_of

    try:
        messages = retained_messages(source, connector)
    except Refused:
        return SelectedInput((), 0, ("source_unavailable",))
    if source.get("forget_state") == "forgotten" or messages is None:
        return SelectedInput((), 0, ("source_unavailable",))
    try:
        speakers = json.loads(source.get("speakers") or "{}")
        if not isinstance(speakers, dict):
            raise ValueError
        kind = connector_type_of(connector)
        if not isinstance(kind, str):
            raise ValueError
    except (TypeError, ValueError):
        return SelectedInput((), len(messages), ("source_metadata",))
    selected: list[dict[str, Any]] = []
    held = 0
    reasons: set[str] = set()
    for message in messages:
        key, text = message.get("key"), message.get("text")
        if not isinstance(text, str) or not isinstance(key, str) or not _KEY.fullmatch(key):
            held += 1
            reasons.add("message_metadata")
            continue
        if not text.strip():
            continue
        meta = speakers.get(key, {})
        if not isinstance(meta, dict):
            held += 1
            reasons.add("speaker_unknown")
            continue
        speaker = meta["kind"] if "kind" in meta else message.get("speaker_kind")
        if kind == "person_in_views":
            speaker = "person"
        elif kind == "ai_host":
            speaker = (
                "person"
                if request.get("verbatim") is True and request.get("host_message_id")
                else "ai_host"
            )
        modality = message.get("modality") or "text"
        original_author = meta.get("original_author")
        if (
            not isinstance(speaker, str)
            or speaker not in _SPEAKERS
            or (
                speaker == "forwarded"
                and (not isinstance(original_author, str) or not _KEY.fullmatch(original_author))
            )
        ):
            held += 1
            reasons.add("speaker_unknown")
            continue
        if modality != "text":
            held += 1
            reasons.add("media_unavailable")
            continue
        moment = message.get("at")
        if moment is None and kind in {"ai_host", "person_in_views"}:
            moment = request.get("said_at")
        try:
            at = parse_instant(moment)
            if kind not in {"calendar", "reminders"}:
                received = source.get("received_at")
                if received is not None and at > parse_instant(received):
                    raise ValueError
        except (ClockError, ValueError, TypeError):
            held += 1
            reasons.add("source_time_unknown")
            continue
        allowed = LOCATOR_SCHEMES.get(kind, ())
        locator = meta.get("locator")
        if locator is None:
            if "key" in allowed:
                locator = "key:" + key
            elif "whole" in allowed:
                locator = "whole"
        if (
            not isinstance(locator, str)
            or not _KEY.fullmatch(locator)
            or locator.partition(":")[0] not in allowed
            or (locator.startswith("key:") and locator != "key:" + key)
        ):
            held += 1
            reasons.add("locator_unavailable")
            continue
        selected.append(
            {
                "key": key,
                "text": text,
                "at": at.isoformat(),
                "locator": locator,
                "speaker_kind": speaker,
                "modality": modality,
                "original_author": original_author,
            }
        )
    selected.sort(key=lambda message: parse_instant(message["at"]))
    return SelectedInput(tuple(selected), held, tuple(sorted(reasons)))
