"""Screen material content before a transaction; never retain the original binary."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from grove.ingest.material_files import MaterialInvalid, ParsedMaterial
from grove.ingest.upload_policy import screener_for_upload
from grove.screening import Message, ScreenedSource

TITLE_PARSER = "material.title.v1"
NOTE_PARSER = "material.note.v1"


def uuid_key(value):
    if type(value) is not str or len(value) != 36:
        raise MaterialInvalid("a canonical UUID is required")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise MaterialInvalid("a canonical UUID is required") from None
    return value


def bounded_text(value, *, title=False):
    limit, byte_limit = (512, 2048) if title else (16000, 65536)
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise MaterialInvalid("material text is empty or exceeds its limit")
    try:
        if len(value.encode("utf-8")) > byte_limit:
            raise ValueError
    except (UnicodeError, ValueError):
        raise MaterialInvalid("material text exceeds its limit") from None
    return value


def key_hash(world_id, key, operation):
    return hashlib.sha256(
        json.dumps([world_id, operation, uuid_key(key)], separators=(",", ":")).encode()
    ).hexdigest()


def input_digest(title, body, operation):
    raw = body if type(body) is bytes else body.encode("utf-8")
    return hashlib.sha256(
        json.dumps([operation, title], ensure_ascii=False, separators=(",", ":")).encode()
        + b"\0"
        + raw
    ).hexdigest()


@dataclass(frozen=True, repr=False)
class PreparedMaterial:
    kind: str
    format: str
    parser: str
    title: ScreenedSource = field(repr=False)
    bodies: tuple[ScreenedSource, ...] = field(repr=False)
    at: datetime
    input_bytes: int
    part_count: int
    held_count: int


def prepare(world_id, material_id, revision, title, body, at):
    bounded_text(title, title=True)
    screener = screener_for_upload(world_id)
    prefix = f"material:{material_id}:{revision}"
    screened_title = screener.screen_source(prefix + ":title", [Message("title", None, title)])
    if type(body) is ParsedMaterial:
        kind, format_, parser = "file", body.format, body.parser
        messages = [
            Message(part.key, None, part.text, modality="text", source_facts=part.source_facts)
            for part in body.parts
        ]
        identity = ("document", parser)
        input_bytes = sum(len(m.text.encode()) for m in messages)
    else:
        bounded_text(body)
        kind, format_, parser = "note", "note", NOTE_PARSER
        messages = [Message("note", None, body, at=at, speaker_kind="person", modality="text")]
        identity = None
        input_bytes = len(body.encode())
    groups, group, units, facts = [], [], 0, 0
    for message in messages:
        size = len(message.key) + len(message.text) + (len(message.sender) if message.sender else 0)
        fact_size = message.source_facts.encoded_size if message.source_facts else 0
        if size + len(prefix) + 16 > 31250 or fact_size > 2048000:
            raise MaterialInvalid("material block exceeds the screening limit")
        if group and (units + size + len(prefix) + 16 > 31250 or facts + fact_size > 2048000):
            groups.append(group)
            group, units, facts = [], 0, 0
        group.append(message)
        units += size
        facts += fact_size
    if group:
        groups.append(group)
    if len(groups) > 64:
        raise MaterialInvalid("material has too many source groups")
    bodies = tuple(
        screener.screen_source(prefix + f":body:{i}", g, source_identity=identity)
        for i, g in enumerate(groups)
    )
    sizes = [len(s.content().encode()) for s in (screened_title, *bodies)]
    if any(n > 4000000 for n in sizes) or sum(sizes) > 16 * 1024 * 1024:
        raise MaterialInvalid("material exceeds the retained byte limit")
    return PreparedMaterial(
        kind,
        format_,
        parser,
        screened_title,
        bodies,
        at,
        input_bytes,
        len(messages),
        len(messages) if kind == "file" else 0,
    )
