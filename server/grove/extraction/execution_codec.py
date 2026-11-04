"""Closed bounded codecs. Canonical data is not an admission capability."""

import hashlib
import json
import re
import types
from dataclasses import fields, is_dataclass
from typing import get_args, get_origin, get_type_hints

from .contracts import ExtractionRefused
from .execution_types import ExecutionBinding, Proposal

MAX_BINDING_BYTES = 1_048_576
MAX_PROPOSAL_BYTES = 524_288
MAX_COUNTER = 2**63 - 1
MAX_DEPTH = 16
MAX_COLLECTION = 2048
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,1023}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def refuse(code):
    raise ExtractionRefused(code)


def _integer(raw):
    if len(raw) > 19:
        refuse("integer_bounds")
    value = int(raw)
    if not 0 <= value <= MAX_COUNTER:
        refuse("integer_bounds")
    return value


def _object(pairs):
    result = {}
    if len(pairs) > MAX_COLLECTION:
        refuse("collection_bounds")
    for key, value in pairs:
        if key in result:
            refuse("duplicate_field")
        result[key] = value
    return result


def load_json(raw, maximum=MAX_BINDING_BYTES):
    """Check the byte/character and nesting limits before JSON allocation."""
    if type(raw) not in (str, bytes) or len(raw) > maximum:
        refuse("document_bounds")
    try:
        text = raw.decode("utf-8") if type(raw) is bytes else raw
        if len(text.encode("utf-8")) > maximum:
            refuse("document_bounds")
    except UnicodeError:
        refuse("invalid_utf8")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                refuse("nesting_bounds")
        elif char in "]}":
            depth -= 1
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_int=_integer,
            parse_float=lambda _: refuse("integer_required"),
            parse_constant=lambda _: refuse("nonfinite_number"),
        )
        _check_tree(value)
        return value
    except (ValueError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, ExtractionRefused):
            raise
        refuse("invalid_json")


def _check_tree(value, depth=0):
    if depth > MAX_DEPTH:
        refuse("nesting_bounds")
    if type(value) in (list, dict):
        if len(value) > MAX_COLLECTION:
            refuse("collection_bounds")
        for child in value.values() if type(value) is dict else value:
            _check_tree(child, depth + 1)
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            refuse("invalid_utf8")


def canonical(value, maximum=MAX_BINDING_BYTES):
    _check_tree(value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        refuse("invalid_document")
    if len(encoded) > maximum:
        refuse("document_bounds")
    return encoded


def text_digest(value):
    if type(value) is not str or len(value) > MAX_BINDING_BYTES:
        refuse("text_bounds")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        refuse("invalid_utf8")
    if len(raw) > MAX_BINDING_BYTES:
        refuse("text_bounds")
    return hashlib.sha256(raw).hexdigest()


def data_digest(domain, value, maximum=MAX_BINDING_BYTES):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + canonical(value, maximum)).hexdigest()


def _string(value, name):
    if type(value) is not str:
        refuse("string_required")
    payloads = {"content", "speakers", "request", "output"}
    limit = 262_144 if name in payloads else 1024
    if not 1 <= len(value) <= limit:
        refuse("string_bounds")
    if name.endswith("digest"):
        if not _DIGEST.fullmatch(value):
            refuse("digest_required")
    elif name in payloads | {"source_time", "received_at", "start", "end"}:
        if name not in payloads and len(value) > 64:
            refuse("time_bounds")
    elif name == "source_revision":
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            refuse("source_revision_required")
    elif not _IDENTIFIER.fullmatch(value):
        refuse("identifier_bounds")
    return value


def _decode(value, cls, name="", depth=0):
    if depth > MAX_DEPTH:
        refuse("nesting_bounds")
    origin, args = get_origin(cls), get_args(cls)
    if origin is types.UnionType:
        if value is None and type(None) in args:
            return None
        return _decode(value, next(t for t in args if t is not type(None)), name, depth)
    if origin is tuple:
        if type(value) is not list or len(value) > MAX_COLLECTION:
            refuse("collection_bounds")
        return tuple(_decode(x, args[0], name, depth + 1) for x in value)
    if cls is str:
        return _string(value, name)
    if cls is int:
        if type(value) is not int or not 0 <= value <= MAX_COUNTER:
            refuse("integer_bounds")
        return value
    if is_dataclass(cls):
        hints = get_type_hints(cls)
        if type(value) is not dict or set(value) != set(hints):
            refuse("closed_fields_required")
        return cls(
            **{key: _decode(value[key], kind, key, depth + 1) for key, kind in hints.items()}
        )
    refuse("unsupported_type")


def plain(value, depth=0, _budget=None):
    """Bound direct Python records before serialization; reject mutable children."""
    if _budget is None:
        _budget = [0, 0]
    _budget[1] += 1
    if _budget[1] > 16_384:
        refuse("aggregate_bounds")
    if depth > MAX_DEPTH:
        refuse("nesting_bounds")
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name), depth + 1, _budget) for f in fields(value)}
    if type(value) is tuple:
        if len(value) > MAX_COLLECTION:
            refuse("collection_bounds")
        return [plain(x, depth + 1, _budget) for x in value]
    if value is None or type(value) is int:
        return value
    if type(value) is str and len(value) <= MAX_BINDING_BYTES:
        _budget[0] += len(value)
        if _budget[0] > MAX_BINDING_BYTES:
            refuse("aggregate_bounds")
        return value
    refuse("immutable_value_required")


def validate_record(value):
    return _decode(plain(value), type(value))


def decode_binding(raw) -> ExecutionBinding:
    return _decode(load_json(raw), ExecutionBinding)


def encode_binding(value: ExecutionBinding) -> bytes:
    if type(value) is not ExecutionBinding:
        refuse("binding_required")
    _decode(plain(value), ExecutionBinding)
    return canonical(plain(value))


def binding_digest(value: ExecutionBinding) -> str:
    return hashlib.sha256(b"grove.learner.binding.v1\0" + encode_binding(value)).hexdigest()


def decode_proposal(raw) -> Proposal:
    return _decode(load_json(raw, MAX_PROPOSAL_BYTES), Proposal)


def proposal_digest(value: Proposal) -> str:
    if type(value) is not Proposal:
        refuse("proposal_required")
    validate_record(value)
    return data_digest("grove.learner.proposal.v1", plain(value), MAX_PROPOSAL_BYTES)
