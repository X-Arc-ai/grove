"""Bound complete legacy import messages without creating screening authority."""

import json

from grove.ingest.connector import NotAnExport
from grove.learner_input import select_input
from grove.screening import Message, ScreenedSource, Screener

IMPORT_PROVIDERS = frozenset({"chatgpt", "claude_ai"})
MAX_PART_BYTES = 32_768
MAX_PART_MESSAGES = 64
MAX_IMPORT_PARTS = 512


class ImportLimit(NotAnExport):
    def __init__(self) -> None:
        super().__init__("the export exceeds 512 new source parts; upload a smaller export")


def screen_import_parts(
    screener: Screener, locator: str, messages: list[Message]
) -> list[ScreenedSource]:
    """Screen bounded groups; only the Screener constructs the final sealed values.

    Check actual serialized bytes after screening as replacements may expand text.
    A single oversized message remains intact, and admission holds that source.
    Intermediate screening results never enter the store or receipt totals.
    """
    parts: list[ScreenedSource] = []

    def screen(group: list[Message]) -> None:
        result = screener.screen_source(locator, group)
        if len(group) > 1 and len(result.content().encode("utf-8")) > MAX_PART_BYTES:
            middle = len(group) // 2
            screen(group[:middle])
            screen(group[middle:])
        else:
            parts.append(result)
            if len(parts) > MAX_IMPORT_PARTS:
                raise ImportLimit()

    group: list[Message] = []
    size = 2
    for message in messages:
        row = {
            "key": message.key,
            "sender": message.sender,
            "text": message.text,
            "at": message.at.isoformat() if message.at is not None else None,
            "speaker_kind": message.speaker_kind,
            "modality": message.modality,
        }
        length = len(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        if group and (len(group) == MAX_PART_MESSAGES or size + 2 + length > MAX_PART_BYTES):
            screen(group)
            group, size = [], 2
        size += length + (2 if group else 0)
        group.append(message)
    if group:
        screen(group)
    return parts


def input_status(source: dict, connector: dict, request: dict | None = None) -> str | None:
    """Return a closed hold code, kept_out, or None for complete eligible input."""
    if source.get("forget_state") in {"forgotten", "span_forgotten"}:
        return "source_unavailable"
    content = source.get("content")
    if not isinstance(content, str):
        return "source_unavailable"
    if len(content) > MAX_PART_BYTES or len(content.encode("utf-8")) > MAX_PART_BYTES:
        return "input_too_large"
    selected = select_input(source, connector, request or {})
    if not selected.complete:
        return selected.reasons[0] if selected.reasons else "source_unavailable"
    if len(selected.messages) > MAX_PART_MESSAGES:
        return "input_too_large"
    return None if selected.messages else "kept_out"
