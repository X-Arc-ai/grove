"""Closed, inactive question contracts. These records confer no runtime authority.

Only structural references belong here. Source owns answer text; HistoryValue
owns screened generated text and operation payloads. JSON methods return detached
dicts, while Python constructors require immutable tuples and typed children.
"""

import json
import re
from dataclasses import MISSING, dataclass, fields
from datetime import UTC, datetime, timezone
from functools import cache
from types import UnionType
from typing import ClassVar, Self, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

from grove.cleanup.access import CleanupRefused

MAX_ID_CHARS = 128
MAX_VERSION = 2**63 - 1
MAX_RECORD_BYTES = 256 * 1024
MAX_ANSWER_BYTES = 16 * 1024
MAX_ANSWER_TEXT_CHARS = 2000
MAX_ANSWER_TEXT_BYTES = 8192
_STRUCTURAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+~=-]*", re.ASCII)


def structural(value: str, *, limit: int = MAX_ID_CHARS) -> str:
    if type(value) is not str or not 0 < len(value) <= limit:
        raise CleanupRefused("question_reference_invalid")
    if _STRUCTURAL.fullmatch(value) is None:
        raise CleanupRefused("question_reference_invalid")
    return value


def version(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_VERSION:
        raise CleanupRefused("question_version_invalid")
    return value


def instant(value: datetime) -> datetime:
    # Fixed offsets and ZoneInfo cannot retain a caller's mutable tzinfo object.
    if type(value) is not datetime or type(value.tzinfo) not in (timezone, ZoneInfo):
        raise CleanupRefused("question_time_invalid")
    try:
        if value.utcoffset() is None:
            raise ValueError
        value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise CleanupRefused("question_time_invalid") from None
    return value


def answer_text(value: str) -> str:
    """Validate transient exact text, without trimming, retaining, or hashing it."""
    if type(value) is not str or not 0 < len(value) <= MAX_ANSWER_TEXT_CHARS:
        raise CleanupRefused("question_answer_text_invalid")
    try:
        if len(value.encode("utf-8")) > MAX_ANSWER_TEXT_BYTES:
            raise ValueError
    except (ValueError, UnicodeError):
        raise CleanupRefused("question_answer_text_invalid") from None
    return value


def unique(values: tuple, *, limit: int, minimum: int = 0) -> None:
    # All members have already passed recursive type/bound validation before hashing.
    if not minimum <= len(values) <= limit or len(set(values)) != len(values):
        raise CleanupRefused("question_collection_invalid")


def targets(values: tuple, *, minimum: int = 0) -> None:
    unique(values, limit=20, minimum=minimum)
    keys = tuple((v.kind, v.id, v.slot) for v in values)
    unique(keys, limit=20, minimum=minimum)


@cache
def _schema(cls):
    hints = get_type_hints(cls)
    return tuple((field, hints[field.name]) for field in fields(cls))


def _value(annotation, value, *, wire: bool, name: str):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        if value is None and type(None) in args:
            return None
        return _value(args[0], value, wire=wire, name=name)
    if origin is tuple:
        if type(value) is not (list if wire else tuple) or len(value) > 100:
            raise CleanupRefused("question_collection_invalid")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_value(args[0], v, wire=wire, name=name) for v in value)
        if len(args) != len(value):
            raise CleanupRefused("question_tuple_invalid")
        return tuple(_value(t, v, wire=wire, name=name) for t, v in zip(args, value, strict=True))
    if annotation is str:
        return structural(value, limit=256 if name == "locator" else MAX_ID_CHARS)
    if annotation is int:
        if name == "capture_generation":
            if type(value) is not int or not 0 <= value <= MAX_VERSION:
                raise CleanupRefused("question_generation_invalid")
            return value
        return version(value)
    if annotation is datetime:
        if wire:
            if type(value) is not str or len(value) > 40 or "T" not in value:
                raise CleanupRefused("question_time_invalid")
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise CleanupRefused("question_time_invalid") from None
        return instant(value)
    if isinstance(annotation, type) and issubclass(annotation, Record):
        if wire:
            return annotation.from_json(value)
        if type(value) is not annotation:
            raise CleanupRefused("question_nested_type_invalid")
        value._validate()
        return value
    raise CleanupRefused("question_type_invalid")


def _json_bound(value, limit: int) -> None:
    """Count canonical UTF-8 size before encoding; reject deep or wide raw input."""
    remaining = limit

    def spend(count):
        nonlocal remaining
        remaining -= count
        if remaining < 0:
            raise CleanupRefused("question_json_too_large")

    def visit(node, depth):
        if depth > 12:
            raise CleanupRefused("question_json_too_deep")
        if node is None:
            spend(4)
        elif type(node) is str:
            if len(node) > 256:
                raise CleanupRefused("question_json_string_invalid")
            try:
                size = len(node.encode("utf-8")) + 2
            except UnicodeError:
                raise CleanupRefused("question_json_invalid_utf8") from None
            # JSON escapes quotes, slashes and controls, including the short escapes.
            for char in node:
                if char in '\\"':
                    size += 1
                elif ord(char) < 32:
                    size += 1 if char in "\b\f\n\r\t" else 5
            spend(size)
        elif type(node) is int:
            if not 0 <= node <= MAX_VERSION:
                raise CleanupRefused("question_version_invalid")
            spend(len(str(node)))
        elif type(node) is list:
            if len(node) > 100:
                raise CleanupRefused("question_collection_invalid")
            spend(2 + max(0, len(node) - 1))
            for child in node:
                visit(child, depth + 1)
        elif type(node) is dict:
            if len(node) > 32:
                raise CleanupRefused("question_fields_invalid")
            spend(2 + max(0, len(node) - 1) + len(node))
            for key, child in node.items():
                if type(key) is not str:
                    raise CleanupRefused("question_fields_invalid")
                visit(key, depth + 1)
                visit(child, depth + 1)
        else:
            raise CleanupRefused("question_json_type_invalid")

    visit(value, 0)


def _wire(value):
    if isinstance(value, Record):
        return {field.name: _wire(getattr(value, field.name)) for field, _ in _schema(type(value))}
    if type(value) is tuple:
        return [_wire(child) for child in value]
    if type(value) is datetime:
        return value.astimezone(UTC).isoformat()
    return value


class Record:
    __slots__ = ()
    max_bytes: ClassVar[int] = MAX_RECORD_BYTES

    def __post_init__(self):
        self._validate()

    def _validate(self):
        for field, annotation in _schema(type(self)):
            _value(annotation, getattr(self, field.name), wire=False, name=field.name)
        # Aggregate limits apply before cross-member checks hash any child record.
        _json_bound(_wire(self), self.max_bytes)
        self._check()

    def _check(self):
        pass

    def to_json(self) -> dict:
        self._validate()
        result = _wire(self)
        _json_bound(result, self.max_bytes)
        return result

    @classmethod
    def from_json(cls, raw: dict) -> Self:
        if type(raw) is not dict:
            raise CleanupRefused("question_fields_invalid")
        schema = _schema(cls)
        allowed = {field.name for field, _ in schema}
        # Validate keys before building a set from any caller-supplied strings.
        if len(raw) > len(allowed) or any(
            type(key) is not str or len(key) > 64 or key not in allowed for key in raw
        ):
            raise CleanupRefused("question_fields_invalid")
        if any(field.name not in raw and field.default is MISSING for field, _ in schema):
            raise CleanupRefused("question_fields_invalid")
        _json_bound(raw, cls.max_bytes)
        return cls(
            **{
                field.name: _value(annotation, raw[field.name], wire=True, name=field.name)
                for field, annotation in schema
                if field.name in raw
            }
        )


def canonical_json(record: Record) -> str:
    if not isinstance(record, Record):
        raise CleanupRefused("question_record_invalid")
    return json.dumps(record.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_json(cls: type[Record], raw: str | bytes) -> Record:
    """Refuse oversize input before UTF-8 decoding or invoking the JSON parser."""
    if not isinstance(cls, type) or not issubclass(cls, Record) or cls is Record:
        raise CleanupRefused("question_record_invalid")
    if type(raw) not in (str, bytes) or len(raw) > cls.max_bytes:
        raise CleanupRefused("question_json_too_large")
    try:
        if type(raw) is bytes:
            raw = raw.decode("utf-8", errors="strict")
        elif len(raw.encode("utf-8")) > cls.max_bytes:
            raise CleanupRefused("question_json_too_large")
    except UnicodeError:
        raise CleanupRefused("question_json_invalid_utf8") from None

    def pairs(entries):
        result = {}
        if len(entries) > 32:
            raise CleanupRefused("question_fields_invalid")
        for key, value in entries:
            structural(key, limit=64)
            if key in result:
                raise CleanupRefused("question_json_duplicate_key")
            result[key] = value
        return result

    def integer(token):
        if len(token) > 19:
            raise CleanupRefused("question_version_invalid")
        value = int(token)
        if not 0 <= value <= MAX_VERSION:
            raise CleanupRefused("question_version_invalid")
        return value

    def invalid_number(_):
        raise CleanupRefused("question_json_number_invalid")

    try:
        document = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=invalid_number,
            parse_constant=invalid_number,
        )
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, CleanupRefused):
            raise
        raise CleanupRefused("question_json_invalid") from None
    return cls.from_json(document)


@dataclass(frozen=True, slots=True)
class TargetV1(Record):
    kind: str
    id: str
    version: int
    slot: str = "claim"

    def _check(self):
        if self.kind not in ("item", "link"):
            raise CleanupRefused("question_target_kind_invalid")


@dataclass(frozen=True, slots=True)
class SourceBindingV1(Record):
    source_id: str
    source_version: int
    episode_id: str
    episode_version: int
    message_key: str
    locator: str
    source_time: datetime


@dataclass(frozen=True, slots=True)
class PlanV1(Record):
    plan_id: str
    action: str
    obligation_ids: tuple[str, ...]
    targets: tuple[TargetV1, ...]
    batch_value_id: str | None = None

    def _check(self):
        if self.action not in (
            "confirm",
            "apply_batch",
            "return_to_sources",
            "resolution_transfer",
        ):
            raise CleanupRefused("question_plan_action_invalid")
        if (self.action == "apply_batch") != (self.batch_value_id is not None):
            raise CleanupRefused("question_plan_batch_invalid")
        unique(self.obligation_ids, limit=100, minimum=1)
        targets(self.targets, minimum=1)


@dataclass(frozen=True, slots=True)
class QuestionBindingV1(Record):
    question_id: str
    revision: int
    topic_key: str
    targets: tuple[TargetV1, ...]
    plans: tuple[PlanV1, ...]
    legwork_ids: tuple[str, ...]

    def _check(self):
        targets(self.targets, minimum=1)
        unique(self.plans, limit=20, minimum=1)
        unique(tuple(plan.plan_id for plan in self.plans), limit=20, minimum=1)
        unique(self.legwork_ids, limit=50, minimum=1)
        all_targets = set(self.targets)
        if any(not set(plan.targets) <= all_targets for plan in self.plans):
            raise CleanupRefused("question_plan_target_unbound")
        if len({key for plan in self.plans for key in plan.obligation_ids}) > 100:
            raise CleanupRefused("question_obligation_bound")


@dataclass(frozen=True, slots=True)
class AnswerCommandV1(Record):
    request_id: str
    question_id: str
    binding_revision: int
    plan_id: str
    variant: str
    source: SourceBindingV1
    principal_id: str
    host_message_id: str | None = None
    max_bytes: ClassVar[int] = MAX_ANSWER_BYTES

    def _check(self):
        if self.variant not in ("choose_option", "text", "return_to_sources"):
            raise CleanupRefused("question_answer_variant_invalid")


@dataclass(frozen=True, slots=True)
class QuestionApplicationV1(Record):
    application_id: str
    request_id: str
    question_id: str
    binding_revision: int
    principal_id: str
    source: SourceBindingV1
    plan_id: str
    commit_id: str
    change_set_id: str
    resolved_ids: tuple[str, ...]
    pending_ids: tuple[str, ...]
    transferred_ids: tuple[str, ...]
    effects: tuple[tuple[str, str, int, int, str], ...]

    def _check(self):
        for group in (self.resolved_ids, self.pending_ids, self.transferred_ids):
            unique(group, limit=100)
        resolved, pending, transferred = map(
            set, (self.resolved_ids, self.pending_ids, self.transferred_ids)
        )
        if (
            resolved & (pending | transferred)
            or not 1 <= len(resolved | pending | transferred) <= 100
        ):
            raise CleanupRefused("question_collection_invalid")
        unique(self.effects, limit=100)
        unique(tuple((effect[0], effect[1], effect[4]) for effect in self.effects), limit=100)
        for kind, _, before, after, _ in self.effects:
            if kind not in ("item", "link") or after <= before:
                raise CleanupRefused("question_effect_invalid")


def validate_coverage(binding: QuestionBindingV1, application: QuestionApplicationV1) -> None:
    """Check exact answer accounting; transferred work is never resolved work."""
    if type(binding) is not QuestionBindingV1 or type(application) is not QuestionApplicationV1:
        raise CleanupRefused("question_nested_type_invalid")
    binding._validate()
    application._validate()
    if (binding.question_id, binding.revision) != (
        application.question_id,
        application.binding_revision,
    ):
        raise CleanupRefused("question_application_binding_invalid")
    plan = next((p for p in binding.plans if p.plan_id == application.plan_id), None)
    if plan is None:
        raise CleanupRefused("question_application_plan_invalid")
    obligations = {key for p in binding.plans for key in p.obligation_ids}
    covered = set(application.resolved_ids + application.transferred_ids)
    expected_pending = (obligations - covered) | set(application.transferred_ids)
    if covered != set(plan.obligation_ids) or set(application.pending_ids) != expected_pending:
        raise CleanupRefused("question_application_coverage_invalid")
    if plan.action in ("return_to_sources", "resolution_transfer"):
        if application.resolved_ids or application.effects:
            raise CleanupRefused("question_transfer_cannot_resolve")
    elif application.transferred_ids or not application.effects:
        raise CleanupRefused("question_application_effect_required")
    if plan.action == "confirm":
        expected = {(target.kind, target.id, target.version) for target in plan.targets}
        actual = {(effect[0], effect[1], effect[2]) for effect in application.effects}
        if actual != expected:
            raise CleanupRefused("question_application_effect_unbound")
