"""D6 explanations: a model words one existing mark and cannot select its reach.

Item names are rendered locally from explicit path references. The rest of the sentence uses
closed connectives, so an arbitrary name cannot hide in an unchecked free-text fragment. Source
type and world time are also rendered locally: a reported change never becomes confirmed prose.
The caller caches a successful result through the writer after the cascade transaction commits.
This module neither reads nor writes the graph, and a failure always leaves a template available.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from grove.config import ConfigError
from grove.models.errors import ModelError
from grove.models.layer import ModelLayer
from grove.prompts import DataBlock, build_prompt
from grove.screening import Screener, ScreeningConfig

JOB = "explain_break"
PROMPT = "grove.explain-break.v1"
MAX_WORDS = 40
MAX_PACKET_BYTES = 24_000
_GENERIC = "This item is affected by a change to another item."
_SOURCE = {
    "planned": "a planned change",
    "said": "a stated change",
    "reported_done": "a reported change",
    "confirmed": "a confirmed change",
}
_PHRASES = ("depends on", "is affected by", "needs review after", "which has", "to", ",", ".")
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Explanation:
    text: str = field(repr=False)
    by: str = "template"
    model_call_id: str | None = None
    fallback_reason: str | None = None


class ExplanationInputError(ValueError):
    """The packet is invalid; the error contains no packet values."""


def _packet(packet: dict[str, Any]) -> tuple[dict[str, str], str, str]:
    """Validate the bounded, caller-owned packet without changing it."""
    try:
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False)
        mark_id = packet["mark_id"]
        trigger = packet["trigger"]
        affected = packet["affected"]
        path = packet["path_items"]
        meanings = packet["meanings"]
        if type(mark_id) is not str or _ID.fullmatch(mark_id) is None:
            raise ValueError
        if type(path) is not list or not 2 <= len(path) <= 4:
            raise ValueError
        if type(meanings) is not list or not 1 <= len(meanings) <= 3:
            raise ValueError
        names = {}
        for item in path:
            if type(item) is not dict or type(item.get("id")) is not str:
                raise ValueError
            if _ID.fullmatch(item["id"]) is None or type(item.get("title")) is not str:
                raise ValueError
            if item["id"] in names or not item["title"].strip():
                raise ValueError
            names[item["id"]] = item["title"]
        for item in (trigger, affected):
            if item["id"] not in names or item["title"] != names[item["id"]]:
                raise ValueError
        if trigger["id"] == affected["id"] or trigger["source_type"] not in _SOURCE:
            raise ValueError
        if len(encoded.encode("utf-8")) > MAX_PACKET_BYTES:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ExplanationInputError("invalid explanation packet") from None
    return names, trigger["id"], affected["id"]


def _screen(screener: Screener | None) -> Screener:
    return screener or Screener(ScreeningConfig.from_environment())


def _unchanged(text: str, screener: Screener) -> bool:
    screened = screener.screen_text(text)
    return (
        not screened.dropped_whole
        and not screened.counts
        and len(screened.messages) == 1
        and screened.messages[0].text == text
    )


def _one_sentence(text: str) -> bool:
    # A title containing sentence punctuation is deliberately not put into this short sentence.
    # It remains available on the item itself. No abbreviation exception can hide a second one.
    return (
        text.endswith(".")
        and not any(char in text[:-1] for char in ".!?\n\r")
        and len(text.split()) <= MAX_WORDS
        and not any(ord(char) < 32 for char in text)
    )


def _when(packet: dict[str, Any]) -> str:
    trigger = packet["trigger"]
    instant = trigger.get("world_time")
    if instant is None and isinstance(trigger.get("clocks"), dict):
        instant = trigger["clocks"].get("world_time")
    if type(instant) is not str:
        return "at an unknown time"
    try:
        parsed = datetime.fromisoformat(instant)
    except ValueError:
        return "at an unknown time"
    if parsed.utcoffset() is None:
        return "at an unknown time"
    return "on " + parsed.date().isoformat()


def template(packet: dict[str, Any], *, screener: Screener | None = None) -> str:
    """A model-free sentence; bad or private labels use a fixed, value-free fallback."""
    names, trigger_id, affected_id = _packet(packet)
    text = (
        f"{names[affected_id]} is affected by {_SOURCE[packet['trigger']['source_type']]} "
        f"to {names[trigger_id]} {_when(packet)}."
    )
    try:
        safe = _one_sentence(text) and _unchanged(text, _screen(screener))
    except ConfigError:
        safe = False
    return text if safe else _GENERIC


def response_schema(packet: dict[str, Any]) -> dict[str, Any]:
    names, _, _ = _packet(packet)
    return {
        "type": "object",
        "properties": {
            "mark_id": {"type": "string", "enum": [packet["mark_id"]]},
            "parts": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        *sorted("item:" + item_id for item_id in names),
                        *_PHRASES,
                        "source",
                        "time",
                    ],
                },
            },
        },
        "required": ["mark_id", "parts"],
        "additionalProperties": False,
    }


def validate(packet: dict[str, Any], data: Any, *, screener: Screener | None = None) -> str | None:
    """Return the screened sentence, or None. Never trust a provider's schema enforcement."""
    names, trigger_id, affected_id = _packet(packet)
    if type(data) is not dict or set(data) != {"mark_id", "parts"}:
        return None
    if type(data["mark_id"]) is not str or data["mark_id"] != packet["mark_id"]:
        return None
    parts = data["parts"]
    if type(parts) is not list or any(type(part) is not str for part in parts):
        return None
    # These sentence forms preserve the computed relation and its source qualifier. Spark chooses
    # the wording, not a new consequence, a stronger source type, or another item.
    affected, trigger = "item:" + affected_id, "item:" + trigger_id
    permitted = (
        [affected, "depends on", trigger, ",", "which has", "source", "time", "."],
        [affected, "is affected by", "source", "to", trigger, "time", "."],
        [affected, "needs review after", "source", "to", trigger, "time", "."],
    )
    if parts not in permitted:
        return None
    rendered = []
    for part in parts:
        if part.startswith("item:"):
            rendered.append(names[part[5:]])
        elif part == "source":
            rendered.append(_SOURCE[packet["trigger"]["source_type"]])
        elif part == "time":
            rendered.append(_when(packet))
        else:
            rendered.append(part)
    text = " ".join(rendered).replace(" ,", ",").replace(" .", ".")
    try:
        safe = _one_sentence(text) and _unchanged(text, _screen(screener))
    except ConfigError:
        return None
    return text if safe else None


def explain(
    layer: ModelLayer,
    world_id: str,
    packet: dict[str, Any],
    *,
    run_id: str | None = None,
    screener: Screener | None = None,
) -> Explanation:
    """Call Spark after commit. No output or provider failure changes the stored mark."""
    _packet(packet)
    fallback = template(packet, screener=screener)
    try:
        screening = _screen(screener)
    except ConfigError:
        return Explanation(fallback, fallback_reason="screening_unavailable")
    data = json.dumps(packet, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if not _unchanged(data, screening):
        return Explanation(fallback, fallback_reason="private_input")
    prompt = build_prompt(
        PROMPT,
        [
            DataBlock(
                kind="item",
                speaker="automated",
                source_type=packet["trigger"]["source_type"],
                ref=packet["mark_id"],
                text=data,
            )
        ],
    )
    try:
        result = layer.respond(
            world_id,
            job=JOB,
            effort="low",
            instructions=prompt.instructions,
            input=prompt.data,
            schema=response_schema(packet),
            schema_name="break_explanation",
            max_output_tokens=1024,
            run_id=run_id,
        )
    except ModelError:
        # Provider messages can hold prompt text. Keep a fixed code, never the provider message.
        return Explanation(fallback, fallback_reason="model_unavailable")
    text = validate(packet, result.data, screener=screening)
    if text is None:
        return Explanation(
            fallback, model_call_id=result.model_call_id, fallback_reason="invalid_output"
        )
    return Explanation(text, by="spark", model_call_id=result.model_call_id)
