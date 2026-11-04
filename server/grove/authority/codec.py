"""Bounded canonical codec. No I/O, clock, runtime consumer, or authorization.

Encoding returns a validated chunk iterable. Journal decoding retains the exact
immutable input and a packed offset table, not a materialized event history.
Every decoded record remains tied to its explicit reader budget.
"""

import json
from array import array
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType

from .records import (
    PROFILE,
    SCHEMA_NAMES,
    SCHEMAS,
    TYPES,
    AuthorityCodecRefused,
    Metrics,
    UntrustedEvent,
    UntrustedJournal,
    UntrustedManifest,
    check_record,
    check_scalar,
    refuse,
)

PARTITIONS = MappingProxyType(
    {
        "input": 33048576,
        "index": 4194304,
        "scratch": 4194304,
        "manifest": 4194304,
        "reducer": 12582912,
        "encoder": 1048576,
        "metadata": 2097152,
    }
)
MAX_ALLOCATION = 67108864
ZERO_DIGEST = "0" * 64


class _Reservation:
    __slots__ = ("budget", "part", "size")

    def __init__(self, budget, part, size=0):
        self.budget, self.part, self.size = budget, part, 0
        self.grow(size)

    def grow(self, amount):
        self.budget._charge(self.part, amount)
        self.size += amount

    def release(self):
        if self.size:
            self.budget._used[self.part] -= self.size
            self.size = 0

    def __del__(self):
        self.release()


class DecoderBudget:
    """One reader; fixed byte partitions and an explicit cancellation counter.

    close() invalidates every attached result. Caller input buffers count in full,
    including bytes outside an accepted slice. Distinct retained inputs remain
    charged until close; request-level admission is the future adapter's job.
    """

    __slots__ = ("_used", "_peak", "_inputs", "_operations", "_limit", "_closed", "_cancelled")

    def __init__(self, *, max_operations=64000000):
        if type(max_operations) is not int or not 1 <= max_operations <= 64000000:
            refuse("scalar")
        self._used = dict.fromkeys(PARTITIONS, 0)
        self._peak = dict(self._used)
        self._inputs = {}
        self._operations, self._limit = 0, max_operations
        self._closed = self._cancelled = False
        self._charge("metadata", 4096)

    def tick(self, amount=1):
        if type(amount) is not int or amount < 0:
            refuse("scalar")
        if self._closed or self._cancelled:
            refuse("cancelled")
        self._operations += amount
        if self._operations > self._limit:
            refuse("cancelled")

    def cancel(self):
        self._cancelled = True

    def close(self):
        self._closed = True
        self._inputs.clear()
        self._used["input"] = 0

    @property
    def usage(self):
        return tuple(self._used.items())

    @property
    def peak(self):
        return tuple(self._peak.items())

    @property
    def operations(self):
        return self._operations

    def _charge(self, part, amount):
        self.tick()
        if type(part) is not str or part not in PARTITIONS or type(amount) is not int:
            refuse("scalar")
        if (
            amount < 0
            or self._used[part] + amount > PARTITIONS[part]
            or sum(self._used.values()) + amount > MAX_ALLOCATION
        ):
            refuse("allocation_limit")
        self._used[part] += amount
        self._peak[part] = max(self._peak[part], self._used[part])

    def reserve(self, part, amount=0):
        return _Reservation(self, part, amount)

    def input(self, data, maximum):
        self.tick()
        if type(data) is bytes:
            root = data
        elif (
            type(data) is memoryview
            and data.readonly
            and type(data.obj) is bytes
            and data.ndim == 1
            and data.c_contiguous
            and data.itemsize == 1
            and data.format == "B"
        ):
            root = data.obj
        else:
            refuse("input_type")
        if len(data) > maximum:
            refuse("byte_limit")
        if id(root) not in self._inputs:
            # Charge before the view, table entry, or retained reference is made.
            self._charge("input", len(root) + 64)
            try:
                self._charge("metadata", 192)
            except BaseException:
                self._used["input"] -= len(root) + 64
                raise
            self._inputs[id(root)] = root
        return memoryview(data)


def _budget(budget):
    if type(budget) is not DecoderBudget:
        refuse("input_type")
    budget.tick()
    return budget


def _bounded_input(function):
    """Discard this call's input admission on failure, preserving older results."""

    def bounded(data, *, budget, profile=PROFILE):
        budget = _budget(budget)
        root = data.obj if type(data) is memoryview else data
        identity = id(root)
        existed = identity in budget._inputs
        failure = None
        try:
            return function(data, budget=budget, profile=profile)
        except AuthorityCodecRefused as error:
            failure = error.code
            if not existed and identity in budget._inputs:
                admitted = budget._inputs.pop(identity)
                budget._used["input"] -= len(admitted) + 64
                budget._used["metadata"] -= 192
                del admitted
        # Do not chain a parser exception or retain its frames in the new error.
        data = root = None
        raise AuthorityCodecRefused(failure)

    return bounded


# Each token uses five fixed-width unsigned integers: kind,start,end,next,count.
# The tape stores no input-derived dicts or strings. Keys are checked against the
# selected closed schema before any value subtree becomes a Python record.
_OBJECT, _ARRAY, _STRING, _NUMBER, _NULL, _TRUE, _FALSE = range(7)


class _Tape:
    __slots__ = (
        "data",
        "budget",
        "reservation",
        "tokens",
        "pos",
        "start",
        "max_bytes",
        "max_nodes",
        "max_depth",
        "depth",
    )

    def __init__(
        self, data, budget, reservation, *, start=0, max_bytes=262143, max_nodes=8192, max_depth=12
    ):
        self.data, self.budget, self.reservation = data, budget, reservation
        self.pos = self.start = start
        self.max_bytes, self.max_nodes, self.max_depth = max_bytes, max_nodes, max_depth
        self.depth = 0
        reservation.grow(512)
        self.tokens = array("I")

    def byte(self):
        self.budget.tick()
        if self.pos >= len(self.data):
            refuse("noncanonical")
        if self.pos - self.start >= self.max_bytes:
            refuse("event_limit")
        return self.data[self.pos]

    def expect(self, byte):
        if self.byte() != byte:
            refuse("noncanonical")
        self.pos += 1

    def node(self, kind, start):
        if len(self.tokens) // 5 >= self.max_nodes:
            refuse("node_limit")
        # 20 tape bytes, dynamic-array slack, and allocator/header margin.
        self.reservation.grow(40)
        index = len(self.tokens) // 5
        self.tokens.extend((kind, start, 0, 0, 0))
        return index

    def finish(self, index, count=0):
        self.tokens[index * 5 + 2] = self.pos
        self.tokens[index * 5 + 3] = len(self.tokens) // 5
        self.tokens[index * 5 + 4] = count
        return index

    def token(self, index):
        offset = index * 5
        return tuple(self.tokens[offset : offset + 5])

    def text(self, index, *, key=False):
        kind, start, end, _, _ = self.token(index)
        if kind != _STRING:
            refuse("scalar")
        size = end - start - 2
        if not key:
            self.reservation.grow(96 + size * 2)
        return bytes(self.data[start + 1 : end - 1]).decode("ascii")

    def scan(self, depth=0, *, key=False):
        char = self.byte()
        start = self.pos
        if char == 34:
            index = self.node(_STRING, start)
            self.pos += 1
            maximum = 64 if key else 1024
            while self.byte() != 34:
                char = self.data[self.pos]
                if char < 32 or char > 126 or char == 92:
                    refuse("noncanonical")
                if self.pos - start - 1 >= maximum:
                    refuse("token_limit")
                self.pos += 1
            self.pos += 1
            return self.finish(index)
        if 48 <= char <= 57:
            index = self.node(_NUMBER, start)
            self.pos += 1
            while self.pos < len(self.data) and 48 <= self.data[self.pos] <= 57:
                self.budget.tick()
                if self.pos - start >= 19:
                    refuse("token_limit")
                self.pos += 1
            if self.pos - start > 1 and char == 48:
                refuse("noncanonical")
            return self.finish(index)
        for literal, kind in ((b"null", _NULL), (b"true", _TRUE), (b"false", _FALSE)):
            if char == literal[0]:
                index = self.node(kind, start)
                for part in literal:
                    self.expect(part)
                return self.finish(index)
        if char not in (123, 91):
            refuse("noncanonical")
        depth += 1
        if depth > self.max_depth:
            refuse("depth_limit")
        self.depth = max(self.depth, depth)
        kind, close = (_OBJECT, 125) if char == 123 else (_ARRAY, 93)
        index = self.node(kind, start)
        self.pos += 1
        count, previous = 0, None
        while self.byte() != close:
            if count:
                self.expect(44)
            if kind == _OBJECT:
                key_index = self.scan(depth, key=True)
                text = self.text(key_index, key=True)
                if previous is not None and text <= previous:
                    refuse("duplicate_key" if text == previous else "noncanonical")
                previous = text
                self.expect(58)
            self.scan(depth)
            count += 1
        self.pos += 1
        return self.finish(index, count)

    def materialize(self, index, spec):
        self.budget.tick()
        kind, start, end, following, count = self.token(index)
        if type(spec) is tuple:
            tag = spec[0]
            if tag == "nullable":
                return None if kind == _NULL else self.materialize(index, spec[1])
            if tag == "union":
                if kind != _OBJECT:
                    refuse("scalar")
                fields = self.fields(index)
                if "kind" not in fields:
                    refuse("unsupported_variant")
                variant = self.text(fields["kind"])
                matches = [name for name in spec[1] if variant in dict(SCHEMAS[name])["kind"][1]]
                if len(matches) != 1:
                    refuse("unsupported_variant")
                return self.materialize(index, matches[0])
            if tag == "array":
                if kind != _ARRAY or not spec[3] <= count <= spec[2]:
                    refuse("scalar")
                self.reservation.grow(128 + count * 24)
                values, child = [], index + 1
                for _ in range(count):
                    values.append(self.materialize(child, spec[1]))
                    child = self.tokens[child * 5 + 3]
                result = tuple(values)
                _check_order(result, spec[4])
                return result
        if type(spec) is str and spec in SCHEMAS:
            if kind != _OBJECT:
                refuse("scalar")
            fields = self.fields(index)
            schema = SCHEMAS[spec]
            if tuple(fields) != tuple(key for key, _ in schema):
                refuse("unknown_field")
            self.reservation.grow(192 + 32 * len(fields))
            values = {}
            for key, child_spec in schema:
                if type(child_spec) is tuple and child_spec[0] == "dependent":
                    discriminator = values[child_spec[1]]
                    child_spec = dict(child_spec[2])[discriminator]
                values[key] = self.materialize(fields[key], child_spec)
            record = TYPES[spec](
                **values, _span=(self.data, start, end, spec), _reservation=self.reservation
            )
            return check_record(record)
        if kind == _STRING:
            value = self.text(index)
        elif kind == _NUMBER:
            self.reservation.grow(64)
            value = int(bytes(self.data[start:end]))
        elif kind in (_TRUE, _FALSE):
            value = kind == _TRUE
        else:
            refuse("scalar")
        return check_scalar(spec, value)

    def fields(self, index):
        _, _, _, _, count = self.token(index)
        # Schema objects have at most twenty fields. Unknown field floods never
        # allocate a large dictionary, even in an otherwise small event.
        if count > 20:
            refuse("unknown_field")
        fields, child = {}, index + 1
        for _ in range(count):
            key = self.text(child, key=True)
            fields[key] = child + 1
            child = self.tokens[(child + 1) * 5 + 3]
        return fields


def _check_order(values, order):
    if not order:
        return
    previous = None
    for value in values:
        key = value if order == ("$",) else tuple(getattr(value, key) for key in order)
        if previous is not None and key <= previous:
            refuse("scalar")
        previous = key


def _parse(
    data,
    schema,
    budget,
    *,
    start=0,
    end=None,
    part="scratch",
    byte_limit=262143,
    node_limit=8192,
    depth_limit=12,
):
    reservation = budget.reserve(part)
    try:
        tape = _Tape(
            data,
            budget,
            reservation,
            start=start,
            max_bytes=byte_limit,
            max_nodes=node_limit,
            max_depth=depth_limit,
        )
        tape.scan()
        if end is not None and tape.pos != end:
            refuse("noncanonical")
        record = tape.materialize(0, schema)
        metrics = Metrics(tape.pos - start, len(tape.tokens) // 5, tape.depth)
        return record, metrics, tape.pos
    except BaseException:
        reservation.release()
        raise


@_bounded_input
def decode_event(data, *, budget, profile=PROFILE):
    budget = _budget(budget)
    if type(profile) is not TYPES["Profile"] or profile != PROFILE:
        refuse("unsupported_variant")
    data = budget.input(data, 262143)
    event, metrics, _ = _parse(data, "Event", budget, end=len(data))
    try:
        _event_checks(event, metrics)
    except BaseException:
        event._reservation.release()
        raise
    return event


def _event_checks(event, metrics):
    if metrics.bytes + 1 > 262144 or metrics.nodes > 8192:
        refuse("event_limit")
    if event.kind == "seal":
        if event.actor_id is not None or event.approved_at is not None:
            refuse("scalar")
        if metrics.bytes + 1 > 1024 or metrics.nodes > 64:
            refuse("event_limit")
    if event.digest != event_digest(event):
        refuse("digest")
    if hasattr(event.payload, "intent"):
        intent = event.payload.intent
        if intent.intent_digest != intent_digest(event.world_id, intent):
            refuse("digest")
        auth = intent.authorization
        if auth.kind == "owner_approval":
            if (
                event.actor_id != auth.user_id
                or event.approved_at != auth.approved_at
                or auth.payload_digest != choice_digest(event.world_id, intent)
            ):
                refuse("digest")
        elif event.actor_id is not None or event.approved_at is not None:
            refuse("scalar")


@_bounded_input
def decode_manifest(data, *, budget, profile=PROFILE):
    budget = _budget(budget)
    if type(profile) is not TYPES["Profile"] or profile != PROFILE:
        refuse("unsupported_variant")
    data = budget.input(data, 1048576)
    record, _, _ = _parse(
        data,
        "Manifest",
        budget,
        end=len(data),
        part="manifest",
        byte_limit=1048576,
        node_limit=262144,
        depth_limit=6,
    )
    try:
        if record.schema_profile != profile or record.checksum != manifest_checksum(record):
            refuse("digest")
    except BaseException:
        record._reservation.release()
        raise
    return record


@_bounded_input
def scan_journal(data, *, budget, profile=PROFILE):
    budget = _budget(budget)
    if type(profile) is not TYPES["Profile"] or profile != PROFILE:
        refuse("unsupported_variant")
    data = budget.input(data, 16000000)
    index_reservation = budget.reserve("index", 1024)
    event = None
    header_reservation = None
    try:
        if data[:11] != b'{"events":[':
            refuse("noncanonical")
        offsets, ids = array("I"), set()
        pos, nodes, depth, count = 11, 3, 2, 0
        previous = ZERO_DIGEST
        world = None
        while pos < len(data) and data[pos] != 93:
            if count:
                if data[pos] != 44:
                    refuse("noncanonical")
                pos += 1
            if count == 32768:
                refuse("journal_limit")
            start = pos
            event, metrics, pos = _parse(data, "Event", budget, start=pos, depth_limit=10)
            _event_checks(event, metrics)
            nodes += metrics.nodes
            depth = max(depth, metrics.depth + 2)
            if nodes > 262144:
                refuse("journal_limit")
            if event.sequence != count + 1 or event.previous_digest != previous:
                refuse("sequence")
            if count == 0:
                if event.kind not in ("world_birth", "adopt_world"):
                    refuse("transition")
                world = event.world_id
            elif event.kind in ("world_birth", "adopt_world"):
                refuse("transition")
            if event.world_id != world:
                refuse("identity")
            # Packed 128-bit UUID keys, bounded set/table overhead charged first.
            index_reservation.grow(192)
            identity = int(event.event_id.replace("-", ""), 16)
            if identity in ids:
                refuse("identity")
            ids.add(identity)
            offsets.extend((start, pos, metrics.nodes, metrics.depth))
            previous = complete_event_digest(event)
            count += 1
            event = None
        if count == 0 or pos >= len(data) or data[pos : pos + 2] != b"],":
            refuse("noncanonical")
        # The bounded header has no large subtree. Parse it without copying the
        # journal: each field has a selected schema before materialization.
        header_reservation = budget.reserve("index", 1024)
        header_values = {}
        pos += 2
        for number, (key, spec) in enumerate(SCHEMAS["JournalHeader"]):
            if number:
                if data[pos : pos + 1] != b",":
                    refuse("noncanonical")
                pos += 1
            prefix = ('"' + key + '":').encode("ascii")
            if data[pos : pos + len(prefix)] != prefix:
                refuse("unknown_field")
            pos += len(prefix)
            tape = _Tape(data, budget, header_reservation, start=pos, max_depth=10)
            tape.scan(1)
            header_values[key] = tape.materialize(0, spec)
            nodes += 1 + len(tape.tokens) // 5
            depth = max(depth, tape.depth)
            pos = tape.pos
        if data[pos:] != b"}":
            refuse("noncanonical")
        header = TYPES["JournalHeader"](**header_values, _reservation=header_reservation)
        if header.world_id != world or header.schema_profile != profile:
            refuse("identity")
        if nodes > 262144:
            refuse("journal_limit")
        # Immutable offset bytes; include simultaneous array and bytes allocation.
        index_reservation.grow(len(offsets) * offsets.itemsize + 64)
        packed = offsets.tobytes()
        return UntrustedJournal(
            header, Metrics(len(data), nodes, depth, count), data, packed, budget, index_reservation
        )
    except BaseException:
        index_reservation.release()
        if event is not None:
            event._reservation.release()
        if header_reservation is not None:
            header_reservation.release()
        raise


def journal_event(journal, index):
    if type(journal) is not UntrustedJournal or type(index) is not int:
        refuse("input_type")
    _budget(journal._budget)
    if not 0 <= index < journal.metrics.events:
        refuse("sequence")
    offsets = memoryview(journal._offsets).cast("I")
    start, end = offsets[index * 4 : index * 4 + 2]
    return _parse(journal._data, "Event", journal._budget, start=start, end=end)[0]


def _spec_for(record):
    name = SCHEMA_NAMES.get(type(record))
    if name is None:
        refuse("input_type")
    return name


def _validate(value, spec, budget, metrics, depth=0):
    budget.tick()
    metrics[1] += 1
    if metrics[1] > 262144:
        refuse("node_limit")
    if type(spec) is tuple:
        tag = spec[0]
        if tag == "nullable":
            if value is None:
                return
            metrics[1] -= 1
            return _validate(value, spec[1], budget, metrics, depth)
        if tag == "union":
            name = _spec_for(value)
            if name not in spec[1]:
                refuse("unsupported_variant")
            metrics[1] -= 1
            return _validate(value, name, budget, metrics, depth)
        if tag == "array":
            if type(value) is not tuple or not spec[3] <= len(value) <= spec[2]:
                refuse("scalar")
            depth += 1
            if depth > 12:
                refuse("depth_limit")
            metrics[2] = max(metrics[2], depth)
            for child in value:
                _validate(child, spec[1], budget, metrics, depth)
            _check_order(value, spec[4])
            return
    if type(spec) is str and spec in SCHEMAS:
        if type(value) is not TYPES[spec]:
            refuse("input_type")
        depth += 1
        if depth > 12:
            refuse("depth_limit")
        metrics[2] = max(metrics[2], depth)
        for key, child_spec in SCHEMAS[spec]:
            metrics[1] += 1
            if type(child_spec) is tuple and child_spec[0] == "dependent":
                discriminator = getattr(value, child_spec[1])
                child_spec = dict(child_spec[2]).get(discriminator)
                if child_spec is None:
                    refuse("unsupported_variant")
            _validate(getattr(value, key), child_spec, budget, metrics, depth)
        check_record(value)
        return
    check_scalar(spec, value)


def _tokens(value, *, omit=None):
    """Only called after whole-record validation; tokens are at most 1026 bytes."""
    if value is None:
        yield b"null"
    elif type(value) is bool:
        yield b"true" if value else b"false"
    elif type(value) is int:
        yield str(value).encode("ascii")
    elif type(value) is str:
        yield ('"' + value + '"').encode("ascii")
    elif type(value) is tuple:
        yield b"["
        for index, child in enumerate(value):
            if index:
                yield b","
            yield from _tokens(child)
        yield b"]"
    else:
        yield b"{"
        count = 0
        for key, _ in SCHEMAS[_spec_for(value)]:
            if key == omit:
                continue
            if count:
                yield b","
            yield ('"' + key + '":').encode("ascii")
            yield from _tokens(getattr(value, key))
            count += 1
        yield b"}"


def _record_budget(record):
    reservation = getattr(record, "_reservation", None)
    return reservation.budget if type(reservation) is _Reservation else DecoderBudget()


def record_metrics(record, *, budget=None):
    budget = _record_budget(record) if budget is None else _budget(budget)
    numbers = [0, 0, 0]
    _validate(record, _spec_for(record), budget, numbers)
    reservation = budget.reserve("encoder", 16384)
    try:
        for chunk in _tokens(record):
            budget.tick()
            numbers[0] += len(chunk)
            if numbers[0] > 1048576:
                refuse("byte_limit")
        return Metrics(*numbers)
    finally:
        reservation.release()


@dataclass(frozen=True, slots=True)
class CanonicalChunks:
    metrics: Metrics
    _record: object = field(repr=False)
    _budget: object = field(repr=False, compare=False)

    def __iter__(self):
        reservation = self._budget.reserve("encoder", 65536)
        try:
            if type(self._record) is UntrustedJournal:
                # Already validated immutable bytes; slices share the retained buffer.
                for start in range(0, len(self._record._data), 65536):
                    self._budget.tick()
                    yield self._record._data[start : start + 65536]
            else:
                for chunk in _tokens(self._record):
                    self._budget.tick()
                    yield chunk
        finally:
            reservation.release()


def encode_event(record, *, budget):
    budget = _budget(budget)
    if type(record) is not UntrustedEvent:
        refuse("input_type")
    metrics = record_metrics(record, budget=budget)
    _event_checks(record, metrics)
    return CanonicalChunks(metrics, record, budget)


def encode_manifest(record, *, budget):
    budget = _budget(budget)
    if type(record) is not UntrustedManifest:
        refuse("input_type")
    metrics = record_metrics(record, budget=budget)
    if metrics.bytes > 1048576 or metrics.depth > 6:
        refuse("manifest_limit")
    if record.checksum != manifest_checksum(record):
        refuse("digest")
    return CanonicalChunks(metrics, record, budget)


def encode_journal(record, *, budget):
    budget = _budget(budget)
    if type(record) is not UntrustedJournal or record._budget is not budget:
        refuse("input_type")
    checked = scan_journal(record._data, budget=budget)
    if (
        checked.header != record.header
        or checked.metrics != record.metrics
        or checked._offsets != record._offsets
    ):
        refuse("identity")
    return CanonicalChunks(record.metrics, record, budget)


def _hash_record(record, omit=None):
    budget = _record_budget(record)
    record_metrics(record, budget=budget)
    reservation = budget.reserve("encoder", 16384)
    try:
        digest = sha256()
        for chunk in _tokens(record, omit=omit):
            budget.tick()
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        reservation.release()


def event_digest(record):
    if type(record) is not UntrustedEvent:
        refuse("input_type")
    return _hash_record(record, "digest")


def complete_event_digest(record):
    if type(record) is not UntrustedEvent:
        refuse("input_type")
    return _hash_record(record)


def manifest_checksum(record):
    if type(record) is not UntrustedManifest:
        refuse("input_type")
    return _hash_record(record, "checksum")


def typed_digest(domain, parts, *, budget=None):
    """Hash already typed parts; only fixed, compiled domains are accepted."""
    domains = frozenset(
        (
            "grove.authority.intent.v1",
            "grove.authority.choice.v1",
            "grove.authority.projection.v1",
            "grove.authority.rules.v1",
            "grove.authority.reference.v1",
            "grove.authority.fact-set.v1",
            "grove.authority.lineage-inputs.v1",
            "grove.authority.seed-bindings.v1",
            "grove.authority.capsule-binding.v1",
        )
    )
    if type(domain) is not str or domain not in domains or type(parts) is not tuple:
        refuse("input_type")
    budget = DecoderBudget() if budget is None else _budget(budget)
    reservation = budget.reserve("encoder", 16384)
    try:
        digest = sha256()
        digest.update(b"[")
        digest.update(('"' + domain + '"').encode("ascii"))
        size = len(domain) + 3
        for part in parts:
            _digest_part(part, budget, [0], 0)
            digest.update(b",")
            for chunk in _tokens(part):
                budget.tick()
                size += len(chunk)
                if size > 16000000:
                    refuse("byte_limit")
                digest.update(chunk)
        digest.update(b"]")
        return digest.hexdigest()
    finally:
        reservation.release()


def _digest_part(part, budget, count, depth):
    budget.tick()
    count[0] += 1
    if count[0] > 262144:
        refuse("node_limit")
    if depth > 12:
        refuse("depth_limit")
    if part is None or type(part) is bool:
        return
    if type(part) is int:
        check_scalar("U", part)
    elif type(part) is str:
        if len(part) > 1024 or not part.isascii() or any(ord(c) < 32 or c in '\\"' for c in part):
            refuse("scalar")
    elif type(part) is tuple:
        if len(part) > 4096:
            refuse("node_limit")
        for child in part:
            _digest_part(child, budget, count, depth + 1)
    else:
        record_metrics(part, budget=budget)


def intent_digest(world_id, intent):
    check_scalar("ID", world_id)
    record_metrics(intent)
    digest = sha256()
    for chunk in (b'["grove.authority.intent.v1",', *_tokens(world_id), b","):
        digest.update(chunk)
    for chunk in _tokens(intent, omit="intent_digest"):
        digest.update(chunk)
    digest.update(b"]")
    return digest.hexdigest()


def choice_digest(world_id, intent):
    check_scalar("ID", world_id)
    record_metrics(intent)
    return typed_digest(
        "grove.authority.choice.v1",
        (
            world_id,
            intent.base_sequence,
            intent.base_head_digest,
            intent.expected_installation_epoch,
            intent.expected_authority_epoch,
            intent.expected_erasure_revision,
            intent.effects,
        ),
        budget=_record_budget(intent),
    )


def identity_digest(kind, value, *, source_id=None, budget):
    """One-use private tuple conversion, never a journal string or normalizer.

    Caller adapters still validate their provider's URL/key grammar and tighter
    transport bound. The complete private tuple is at most 16,384 UTF-8 bytes;
    message keys also have A2's 1024-character ceiling and allow its empty key.
    """
    budget = _budget(budget)
    if type(kind) is not str or kind not in ("client", "resource", "key") or type(value) is not str:
        refuse("input_type")
    maximum = 1024 if kind == "key" else 16384
    if len(value) > maximum or (kind != "key" and not value):
        refuse("token_limit")
    if kind == "key":
        check_scalar("ID", source_id)
    elif source_id is not None:
        refuse("scalar")
    reservation = budget.reserve("encoder", 131072)
    try:
        parts = [f"grove.authority.{kind}.v1"]
        if kind == "key":
            parts.append(source_id)
        parts.append(value)
        size = 2 + len(parts) - 1
        for part in parts:
            size += 2
            for char in part:
                point = ord(char)
                if 0xD800 <= point <= 0xDFFF:
                    refuse("scalar")
                if char in '\\"\b\f\n\r\t':
                    size += 2
                elif point < 32:
                    size += 6
                else:
                    size += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
                budget.tick()
                if size > 16384:
                    refuse("byte_limit")
        if size > 16384:
            refuse("byte_limit")
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        budget.tick(len(encoded))
        return sha256(encoded).hexdigest()
    finally:
        reservation.release()
