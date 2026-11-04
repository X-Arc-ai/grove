"""Bounded private Learner values and explicit erasure closure, without authority."""

from grove.extraction import durable_codec as codec
from grove.writer.history import add_text


def _check(kind, raw):
    if kind not in codec.LIMITS:
        codec.refuse("value_kind")
    value = codec.load_json(raw, codec.LIMITS[kind])
    exact = codec.canonical(value, codec.LIMITS[kind])
    given = raw.encode("utf-8") if type(raw) is str else raw
    if exact != given:
        codec.refuse("value_not_canonical")
    codec.value_references(kind, raw)
    return exact


def write_private_value_in_tx(tx, stamp, kind, raw):
    """The caller must reserve the complete prospective work size before writes."""
    raw = _check(kind, raw)
    return add_text(tx, stamp, raw.decode("utf-8"))


def read_private_value_in_tx(tx, value_id, kind):
    codec._id(value_id)
    rows = tx.run("MATCH (v:HistoryValue {id: $id}) RETURN v", id=value_id)
    if (
        len(rows) != 1
        or rows[0]["v"].get("version") != 1
        or rows[0]["v"].get("forgotten_at") is not None
    ):
        codec.refuse("erased_dependency")
    return _check(kind, rows[0]["v"].get("value"))


def walk_value_closure(roots, values):
    """Follow only declared per-kind children; reject cycles, mixed kinds and excess."""
    seen, active, kinds = set(), set(), {}
    total = 0

    def visit(key, kind, depth):
        nonlocal total
        codec._id(key)
        if depth > 16 or key in active:
            codec.refuse("value_cycle")
        if key in kinds and kinds[key] != kind:
            codec.refuse("value_kind_conflict")
        kinds[key] = kind
        if key in seen:
            return
        raw = values.get(key)
        if raw is None:
            codec.refuse("erased_dependency")
        raw = _check(kind, raw)
        total += len(raw)
        if total > codec.MAX_WORK_BYTES or len(seen) + len(active) >= 16384:
            codec.refuse("work_bounds")
        active.add(key)
        for child, child_kind in codec.value_references(kind, raw).items():
            visit(child, child_kind, depth + 1)
        active.remove(key)
        seen.add(key)

    for key, kind in roots.items():
        visit(key, kind, 0)
    return frozenset(seen)


def collect_values_in_tx(tx, roots):
    values, kinds, pending = {}, {}, list(roots.items())
    total = 0
    while pending:
        key, kind = pending.pop()
        if key in kinds:
            if kinds[key] != kind:
                codec.refuse("value_kind_conflict")
            continue
        kinds[key] = kind
        raw = read_private_value_in_tx(tx, key, kind)
        total += len(raw)
        if total > codec.MAX_WORK_BYTES or len(values) >= 16384:
            codec.refuse("work_bounds")
        values[key] = raw
        pending.extend(codec.value_references(kind, raw).items())
    walk_value_closure(roots, values)
    return values
