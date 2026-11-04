"""Bounded legacy ledger validation, without I/O or authority.

The original bytes and legacy hash encoding are distinct from the authority
codec. A result describes captured data only. DecoderBudget owns admitted input
until close(); discard its results then. No decoded row history is retained.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from types import MappingProxyType

from .codec import DecoderBudget
from .records import AuthorityCodecRefused, refuse

FORMAT = "grove.forget-ledger.v1"
MAX_FILE = 16000000
MAX_ROW = 524288
MAX_EMBEDDED = 262144
MAX_ROWS = 32768
MAX_NODES = 262144
MAX_ARRAY = 4096
MAX_DEPTH = 12
MAX_INTEGER = 9223372036854775807
_WORLD = re.compile(r"(?:t_[a-z0-9-]+_)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_HASH = re.compile(r"[0-9a-f]{64}")
_LOCATOR = re.compile(r"sha256:[0-9a-f]{64}")

# Frozen compatibility profile; the committed catalogue parity test detects drift.
DELETE_KEYS = MappingProxyType(
    {
        **dict.fromkeys(
            "items ontology_change_log areas capabilities sources holds unresolved_mentions "
            "requests jobs run_records model_calls judgments cascade_proposals "
            "contradictions questions checks audit_runs findings curator_legwork".split(),
            (frozenset({"id"}),),
        ),
        "links": (frozenset({"id"}), frozenset({"active_key"})),
        "kinds": (frozenset({"id"}), frozenset({"name"})),
        "meanings": (frozenset({"id"}), frozenset({"name"})),
        "property_names": (frozenset({"id"}), frozenset({"kind", "name"})),
        "aliases": (
            frozenset({"id"}),
            frozenset({"normalised_phrase", "target_type", "target_id"}),
        ),
        "distinct_pairs": (frozenset({"item_a", "item_b"}),),
        "connectors": (frozenset({"id"}), frozenset({"provider", "instance_key"})),
        "episodes": (frozenset({"id"}), frozenset({"connector_id", "first_message_key"})),
        "evidence": (frozenset({"id"}), frozenset({"message_key", "target_id", "polarity"})),
        "screening_receipts": (frozenset({"source_id"}),),
        "declines": (frozenset({"request_id", "op_id"}),),
        "budgets": (frozenset({"day"}),),
        "horizon_stats": (frozenset({"meaning_or_property_name"}),),
        "impacts": (frozenset({"link_id"}), frozenset({"trigger_change_set", "affected_item"})),
        "beliefs": (frozenset({"item_id"}),),
        "support_changed": (frozenset({"belief_id"}),),
        "area_audits": (frozenset({"run_id"}),),
        "walk_rotation": (frozenset({"area_id"}),),
        "audit_grades": (frozenset({"run_id", "subject"}),),
        "map_positions": (frozenset({"id"}), frozenset({"item_id"})),
        "verifications": (frozenset({"id"}), frozenset({"attempt_id"})),
        "question_bindings": (
            frozenset({"id"}),
            frozenset({"question_id"}),
            frozenset({"open_topic_key"}),
        ),
        "question_work": (frozenset({"id"}), frozenset({"disposition_id"})),
        "question_applications": (frozenset({"id"}), frozenset({"request_id"})),
        "question_events": (frozenset({"id"}), frozenset({"sequence"})),
        "curator_escalations": (
            frozenset({"id"}),
            frozenset({"claim_type", "claim_id", "streak_start"}),
        ),
        "curator_run_order": (frozenset({"id"}), frozenset({"run_id"}), frozenset({"ordinal"})),
    }
)
WORLD_TABLES = frozenset(DELETE_KEYS) | {
    "change_entries",
    "forget_ledger",
    "forget_approvals",
    "history_values",
}
ITEM_FIELDS = frozenset("title summary body properties property_values search_terms status".split())
_S = ("string", 6144, 1024)
_KEY = ("string", 128, 64)
_LONG = ("string", MAX_ROW, MAX_EMBEDDED)
_N = ("number",)


def _object(fields, required=None):
    return ("object", MappingProxyType(fields), frozenset(fields if required is None else required))


def _list(shape):
    return ("array", shape)


_FIELD = _object({"id": _S, "field": _S})
_DELETE = _object(
    {
        "table": _S,
        "key": _object(
            dict.fromkeys(
                set().union(*(keys for variants in DELETE_KEYS.values() for keys in variants)), _S
            ),
            (),
        ),
    }
)
_SPAN = _object(
    {
        "source_id": _S,
        "source_key": _S,
        "mode": _S,
        "message_keys": _list(_S),
        "start": _N,
        "end": _N,
        "content_hash": _S,
    },
    {"source_id", "source_key", "mode"},
)
_PLAN = _object(
    {
        "version": _N,
        "hashes": _list(_S),
        "predicates": _list(_object({"length": _N, "sha256": _S})),
        "source_exact": _list(_S),
        "delete": _list(_DELETE),
        "mark_values": _list(_S),
        "history_fields": _list(_FIELD),
        "item_fields": _list(_FIELD),
        "source_ids": _list(_S),
        "source_spans": _list(_SPAN),
        "evidence_values": _list(_FIELD),
        "end_evidence": _list(_S),
        "clear_examples": _list(_S),
        "locators": _list(_S),
        "queue_beliefs": _list(_S),
    }
)
_DATE = ("date",)
_INSTANT = ("instant",)
_RECEIPT = _object(
    {
        "receipt_id": _S,
        "counts": _object(dict.fromkeys(WORLD_TABLES, _N), ()),
        "backups_clear_after": _DATE,
        "downloaded_exports": _S,
    }
)
_ROW = _object(
    {
        "id": _S,
        "world_id": _S,
        "target_type": _S,
        "internal_ids": _list(_S),
        "locators": _list(_S),
        "requested_by": _S,
        "approved_at": _INSTANT,
        "backups_clear_after": _DATE,
        "plan": ("embedded", _PLAN),
        "receipt": ("embedded", _RECEIPT),
    },
    {"id", "target_type"},
)


@dataclass(frozen=True, slots=True)
class LegacyBinding:
    format: str
    file_digest: str
    rows_digest: str
    count: int


@dataclass(frozen=True, slots=True)
class LegacyRowBinding:
    id: str
    plan_version: str
    row_digest: str
    plan_digest: str | None
    extension_digest: None = None
    _reservation: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class UntrustedLegacyLedger:
    binding: LegacyBinding
    world_id: str
    _data: object = field(repr=False)
    _offsets: object = field(repr=False)
    _budget: object = field(repr=False, compare=False)
    _reservation: object = field(repr=False, compare=False)


def check_world(world_id):
    if type(world_id) is not str or len(world_id) > 128 or not _WORLD.fullmatch(world_id):
        refuse("identity")
    return world_id


def _check_budget(budget):
    if type(budget) is not DecoderBudget:
        refuse("input_type")
    budget.tick()


class _Parser:
    """Closed-schema parser. Allocate at most one row, after charging each object."""

    def __init__(self, data, budget, arena, nodes, *, start=0, end=None):
        self.data, self.budget, self.arena, self.nodes = data, budget, arena, nodes
        self.pos = start
        self.end = len(data) if end is None else min(end, len(data))

    def peek(self):
        self.budget.tick()
        if self.pos >= self.end:
            refuse("noncanonical")
        return self.data[self.pos]

    def space(self):
        while self.pos < self.end and self.data[self.pos] in b" \t\r\n":
            self.budget.tick()
            self.pos += 1

    def take(self, byte):
        self.space()
        if self.peek() != byte:
            refuse("noncanonical")
        self.pos += 1

    def node(self):
        self.nodes[0] += 1
        if self.nodes[0] > MAX_NODES:
            refuse("node_limit")

    def string(self, shape):
        self.take(34)
        start = self.pos
        while True:
            if self.pos - start > shape[1]:
                refuse("token_limit")
            byte = self.peek()
            if byte == 34:
                break
            if byte < 32:
                refuse("noncanonical")
            if byte == 92:
                self.pos += 1
                self.peek()
            self.pos += 1
        size = self.pos - start
        self.arena.grow(256 + 12 * size)
        # The only JSON decoder receives an already bounded single string token.
        token = bytes(self.data[start : self.pos + 1]).decode("utf-8")
        value, end = json.decoder.scanstring(token, 0, True)
        if end != len(token) or len(value) > shape[2]:
            refuse("token_limit")
        self.pos += 1
        return value

    def value(self, shape, depth=0):
        self.space()
        self.node()
        kind = shape[0]
        if kind == "string":
            return self.string(shape)
        if kind == "number":
            start = self.pos
            while self.pos < self.end and 48 <= self.data[self.pos] <= 57:
                if self.pos - start >= 19:
                    refuse("token_limit")
                self.budget.tick()
                self.pos += 1
            if self.pos == start or (self.pos - start > 1 and self.data[start] == 48):
                refuse("scalar")
            self.arena.grow(64)
            number = int(bytes(self.data[start : self.pos]))
            if number > MAX_INTEGER:
                refuse("scalar")
            return number
        if kind in {"instant", "date"}:
            if self.peek() == 34:
                return self.string(_S)
            if self.peek() == 110:
                self.take_literal(b"null")
                return None
            return self.value(_object({"$t": _S, "v": _S}), depth)
        if kind == "embedded":
            if self.peek() == 34:
                return self.string(_LONG)
            return self.value(shape[1], depth)
        if depth >= MAX_DEPTH:
            refuse("depth_limit")
        if kind == "array":
            self.take(91)
            self.arena.grow(64)
            result = []
            self.space()
            while self.peek() != 93:
                if len(result) >= MAX_ARRAY:
                    refuse("node_limit")
                self.arena.grow(64)
                result.append(self.value(shape[1], depth + 1))
                self.space()
                if self.peek() != 44:
                    break
                self.pos += 1
                self.space()
                if self.peek() == 93:
                    refuse("noncanonical")
            self.take(93)
            return result
        if kind != "object":
            refuse("unsupported_variant")
        self.take(123)
        self.arena.grow(320)
        result = {}
        self.space()
        while self.peek() != 125:
            self.node()
            key = self.string(_KEY)
            if key in result:
                refuse("duplicate_key")
            if key not in shape[1]:
                refuse("unknown_field")
            self.arena.grow(160)
            self.take(58)
            result[key] = self.value(shape[1][key], depth + 1)
            self.space()
            if self.peek() != 44:
                break
            self.pos += 1
            self.space()
            if self.peek() == 125:
                refuse("noncanonical")
        self.take(125)
        if not shape[2] <= result.keys():
            refuse("unknown_field")
        return result

    def take_literal(self, literal):
        for byte in literal:
            if self.peek() != byte:
                refuse("scalar")
            self.pos += 1

    def finish(self):
        self.space()
        if self.pos != self.end:
            refuse("noncanonical")

    def embedded(self, value, shape):
        if type(value) is not str:
            return value
        # Charge UTF-8 conversion and parser state before conversion.
        self.arena.grow(512 + 4 * len(value))
        raw = value.encode("utf-8")
        if len(raw) > MAX_EMBEDDED:
            refuse("byte_limit")
        parser = _Parser(raw, self.budget, self.arena, self.nodes)
        result = parser.value(shape)
        parser.finish()
        return result


def _identifier(value):
    if not value or any(ord(c) < 32 for c in value):
        refuse("identity")


def _hash(value, locator=False):
    if not (_LOCATOR if locator else _HASH).fullmatch(value):
        refuse("digest")


def _date(value, *, instant=False):
    if value is None:
        return
    if type(value) is dict:
        if value["$t"] != ("instant" if instant else "date"):
            refuse("scalar")
        value = value["v"]
    if instant:
        at = datetime.fromisoformat(value.strip())
        if at.tzinfo is None or at.utcoffset() is None:
            refuse("scalar")
    else:
        date.fromisoformat(value)


def _validate_plan(plan):
    if plan["version"] != 2:
        refuse("unsupported_variant")
    for value in plan["hashes"]:
        _hash(value)
    for value in plan["locators"]:
        _hash(value, True)
    for part in plan["predicates"]:
        if part["length"] < 1:
            refuse("scalar")
        _hash(part["sha256"])
    for key in (
        "mark_values",
        "source_ids",
        "source_exact",
        "end_evidence",
        "clear_examples",
        "queue_beliefs",
    ):
        for value in plan[key]:
            _identifier(value)
    for part in plan["delete"]:
        if (
            part["table"] not in DELETE_KEYS
            or frozenset(part["key"]) not in DELETE_KEYS[part["table"]]
        ):
            refuse("unknown_field")
        for value in part["key"].values():
            _identifier(value)
    for key in ("history_fields", "item_fields", "evidence_values"):
        allowed = {"active", "end_reason", "ended_at"} if key == "evidence_values" else ITEM_FIELDS
        for part in plan[key]:
            _identifier(part["id"])
            if part["field"] not in allowed:
                refuse("unknown_field")
    whole = set()
    for part in plan["source_spans"]:
        mode = part["mode"]
        expected = {"source_id", "source_key", "mode"}
        _identifier(part["source_id"])
        _hash(part["source_key"], True)
        if mode == "whole":
            whole.add(part["source_id"])
        elif mode == "messages":
            expected.add("message_keys")
            if not part.get("message_keys"):
                refuse("scalar")
            for value in part["message_keys"]:
                _hash(value)
        elif mode in {"line", "char"}:
            expected |= {"start", "end"}
            if mode == "line" and "content_hash" in part:
                expected.add("content_hash")
                _hash(part["content_hash"])
            if (
                not expected <= part.keys()
                or part["start"] < (1 if mode == "line" else 0)
                or part["end"] < part["start"] + (mode == "char")
            ):
                refuse("scalar")
        else:
            refuse("unsupported_variant")
        if set(part) != expected:
            refuse("unknown_field")
    if set(plan["source_ids"]) != whole:
        refuse("identity")


def _chunks(value, budget):
    budget.tick()
    if value is None:
        yield b"null"
    elif type(value) is str:
        yield b'"'
        for start in range(0, len(value), 256):
            chunk = value[start : start + 256]
            budget.tick(len(chunk))
            yield json.dumps(chunk, ensure_ascii=True)[1:-1].encode("ascii")
        yield b'"'
    elif type(value) is int:
        yield str(value).encode("ascii")
    elif type(value) is list:
        yield b"["
        for index, part in enumerate(value):
            if index:
                yield b","
            yield from _chunks(part, budget)
        yield b"]"
    elif type(value) is dict:
        yield b"{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield b","
            yield from _chunks(key, budget)
            yield b":"
            yield from _chunks(value[key], budget)
        yield b"}"
    else:
        refuse("scalar")


def _digest(value, budget, *extra):
    reservation = budget.reserve("encoder", 16384)
    try:
        hashed = sha256()
        for chunk in _chunks(value, budget):
            hashed.update(chunk)
            for other in extra:
                other.update(chunk)
        return hashed.hexdigest()
    finally:
        reservation.release()


def _row(parser, world_id):
    row = parser.value(_ROW, 2)
    _identifier(row["id"])
    if row.get("world_id", world_id) != world_id:
        refuse("identity")
    if row["target_type"] not in {"item", "field_value", "named_value", "source_span"}:
        refuse("unsupported_variant")
    _date(row.get("approved_at"), instant=True)
    _date(row.get("backups_clear_after"))
    if "requested_by" in row:
        _identifier(row["requested_by"])
    version, plan_digest = "legacy", None
    if "plan" in row:
        if row.get("approved_at") is None:
            refuse("scalar")
        plan = parser.embedded(row["plan"], _PLAN)
        _validate_plan(plan)
        version, plan_digest = "v2", _digest(plan, parser.budget)
    elif row["target_type"] == "source_span":
        refuse("unsupported_variant")
    if "receipt" in row:
        receipt = parser.embedded(row["receipt"], _RECEIPT)
        if receipt["receipt_id"] != row["id"] or receipt["downloaded_exports"] != "out_of_reach":
            refuse("identity")
        if receipt["backups_clear_after"] is None:
            refuse("scalar")
        _date(receipt["backups_clear_after"])
    return row, version, plan_digest


def _rows(parser, world_id, offsets, index_reservation):
    parser.node()
    parser.take(91)
    count, hashed, identities = 0, sha256(b"["), set()
    identity_reservation = parser.budget.reserve("index", 256)
    try:
        parser.space()
        while parser.peek() != 93:
            if count >= MAX_ROWS:
                refuse("node_limit")
            arena = parser.budget.reserve("scratch", 1024)
            try:
                start = parser.pos
                current = _Parser(
                    parser.data,
                    parser.budget,
                    arena,
                    parser.nodes,
                    start=start,
                    end=start + MAX_ROW,
                )
                row, _, _ = _row(current, world_id)
                identity = row["id"]
                if identity in identities:
                    refuse("identity")
                identity_reservation.grow(256 + 4 * len(identity))
                identities.add(identity)
                if count:
                    hashed.update(b",")
                _digest(row, parser.budget, hashed)
                index_reservation.grow(32)
                offsets.extend(start.to_bytes(4, "little") + current.pos.to_bytes(4, "little"))
                parser.pos = current.pos
                count += 1
                del row, current, identity
            finally:
                arena.release()
            parser.space()
            if parser.peek() != 44:
                break
            parser.pos += 1
            parser.space()
            if parser.peek() == 93:
                refuse("noncanonical")
        parser.take(93)
        hashed.update(b"]")
        return count, hashed.hexdigest()
    finally:
        identities.clear()
        identity_reservation.release()


def _decode(data, world_id, budget):
    check_world(world_id)
    view = budget.input(data, MAX_FILE)
    arena = budget.reserve("scratch", 1024)
    index = budget.reserve("index", 1024)
    try:
        parser = _Parser(view, budget, arena, [0])
        offsets, envelope = bytearray(), {}
        parser.node()
        parser.take(123)
        while True:
            parser.node()
            key = parser.string(_KEY)
            if key in envelope:
                refuse("duplicate_key")
            if key not in {"format", "world_id", "count", "rows", "sha256"}:
                refuse("unknown_field")
            parser.take(58)
            envelope[key] = (
                _rows(parser, world_id, offsets, index)
                if key == "rows"
                else parser.value(_N if key == "count" else _S)
            )
            parser.space()
            if parser.peek() != 44:
                break
            parser.pos += 1
            parser.space()
        parser.take(125)
        parser.finish()
        if set(envelope) != {"format", "world_id", "count", "rows", "sha256"}:
            refuse("unknown_field")
        if envelope["format"] != FORMAT:
            refuse("unsupported_variant")
        if envelope["world_id"] != world_id:
            refuse("identity")
        count, rows_digest = envelope["rows"]
        if envelope["count"] != count or envelope["sha256"] != rows_digest:
            refuse("ledger_mismatch")
        budget.tick(len(view))
        binding = LegacyBinding(FORMAT, sha256(view).hexdigest(), rows_digest, count)
        return UntrustedLegacyLedger(binding, world_id, view, bytes(offsets), budget, index)
    except BaseException:
        index.release()
        raise
    finally:
        arena.release()


def decode_legacy(data, *, world_id, budget):
    """Validate all captured rows or refuse; no valid prefix can escape."""
    root, identity, existed = None, None, False
    code = None
    try:
        _check_budget(budget)
        root = data.obj if type(data) is memoryview else data
        identity = id(root)
        existed = identity in budget._inputs
        return _decode(data, world_id, budget)
    except AuthorityCodecRefused as error:
        code = error.code
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        code = "scalar"
    except MemoryError:
        code = "allocation_limit"
    if type(budget) is DecoderBudget and not existed and identity in budget._inputs:
        admitted = budget._inputs.pop(identity)
        budget._used["input"] -= len(admitted) + 64
        budget._used["metadata"] -= 192
        del admitted
    data = root = world_id = None
    raise AuthorityCodecRefused(code)


def _legacy_row(ledger, index):
    if type(ledger) is not UntrustedLegacyLedger or type(index) is not int:
        refuse("input_type")
    budget = ledger._budget
    _check_budget(budget)
    if not 0 <= index < ledger.binding.count:
        refuse("scalar")
    arena = budget.reserve("scratch", 1024)
    try:
        start = int.from_bytes(ledger._offsets[index * 8 : index * 8 + 4], "little")
        end = int.from_bytes(ledger._offsets[index * 8 + 4 : index * 8 + 8], "little")
        parser = _Parser(ledger._data, budget, arena, [0], start=start, end=end)
        row, version, plan_digest = _row(parser, ledger.world_id)
        parser.finish()
        row_digest = _digest(row, budget)
        reservation = budget.reserve("index", 1024 + len(row["id"]) * 4)
        return LegacyRowBinding(
            row["id"], version, row_digest, plan_digest, _reservation=reservation
        )
    finally:
        arena.release()


def legacy_row(ledger, index):
    """Materialize one bounded immutable reference, never a capability."""
    code = None
    try:
        return _legacy_row(ledger, index)
    except AuthorityCodecRefused as error:
        code = error.code
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        code = "scalar"
    ledger = None
    raise AuthorityCodecRefused(code)


def legacy_row_span(ledger, index):
    """Return the checked original row interval, only during its reader lifetime."""
    code = None
    try:
        if type(ledger) is not UntrustedLegacyLedger or type(index) is not int:
            refuse("input_type")
        _check_budget(ledger._budget)
        if not 0 <= index < ledger.binding.count:
            refuse("scalar")
        start = int.from_bytes(ledger._offsets[index * 8 : index * 8 + 4], "little")
        end = int.from_bytes(ledger._offsets[index * 8 + 4 : index * 8 + 8], "little")
        if not 0 <= start < end <= len(ledger._data):
            refuse("scalar")
        return start, end
    except AuthorityCodecRefused as error:
        code = error.code
    except (ValueError, TypeError, IndexError, OverflowError):
        code = "scalar"
    ledger = None
    raise AuthorityCodecRefused(code)
