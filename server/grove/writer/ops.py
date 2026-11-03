"""The operations of this lane (B.2) and the checks that need no store.

`check_shape` runs before the store is touched: the batch's form, each operation's fields and
types, the keys a caller may never send, the observations an operation cites, and the order of
temp ids. A refusal here names the operation; a fault of the whole batch has op_id None.
"""

import zoneinfo
from dataclasses import dataclass
from typing import Any

from grove.store.enums import ENUMS
from grove.store.normalise import reads_blank
from grove.writer.clocks import ClockError, parse_clock, parse_instant
from grove.writer.errors import Refusal, Refused

MAX_OPS = 500
TEMP_PREFIX = "$"


@dataclass(frozen=True)
class OpSpec:
    callers: frozenset[str]
    required: tuple[str, ...]
    optional: tuple[str, ...]
    symbol: str


def _callers(letters: str) -> frozenset[str]:
    kinds = {
        "U": "update",
        "C": "cleanup",
        "K": "consolidation",
        "H": "ai_host",
        "P": "person",
        "W": "writer",
    }
    return frozenset(kinds[letter] for letter in letters)


OPS: dict[str, OpSpec] = {
    "create_item": OpSpec(
        _callers("UCKP"),
        ("op_id", "kind", "title", "became_true", "evidence_refs", "reason"),
        (
            "temp_id",
            "summary",
            "body",
            "properties",
            "search_terms",
            "status",
            "recheck_by",
            "recheck_how",
            "distinct_from",
        ),
        "+",
    ),
    "update_item": OpSpec(
        _callers("UCKP"),
        ("op_id", "item", "version", "fields", "evidence_refs", "reason"),
        (),
        "*",
    ),
    "change_status": OpSpec(
        _callers("UCKPW"),
        ("op_id", "item", "status", "evidence_refs", "reason"),
        ("version", "stopped_being_true", "recheck_by"),
        "*",
    ),
    "end_item": OpSpec(
        _callers("UCKP"),
        ("op_id", "item", "end_class", "stopped_being_true", "evidence_refs", "reason"),
        ("version",),
        "x",
    ),
    "reopen_item": OpSpec(
        _callers("CKP"),
        ("op_id", "item", "reason"),
        ("evidence_refs", "recheck_by", "version"),
        "^",
    ),
    "create_link": OpSpec(
        _callers("UCKPH"),
        ("op_id", "from", "meaning", "to", "became_true", "evidence_refs", "reason"),
        ("temp_id", "recheck_by", "recheck_how", "original_phrase"),
        ">",
    ),
    "end_link": OpSpec(
        _callers("UCKPW"),
        ("op_id", "link", "end_class", "stopped_being_true", "reason"),
        ("evidence_refs",),
        "x",
    ),
    # ev_id or evidence_id is required, one of the two; reason is accepted for the change entry.
    "attach_evidence": OpSpec(
        _callers("UCKPW"),
        ("op_id", "target", "polarity"),
        ("ev_id", "evidence_id", "judgment_ids", "reason"),
        "=",
    ),
    "end_evidence": OpSpec(
        _callers("CKP"),
        ("op_id", "evidence", "end_reason"),
        ("reason",),
        "-",
    ),
    # D8's ontology operations (B.2: propose U C K; the rest K P). grove.writer.ontology checks
    # the declared fields, the neighbours and the judgments; the shape is checked here.
    "propose_word": OpSpec(
        _callers("UCK"),
        (
            "op_id",
            "word_type",
            "name",
            "description",
            "reason",
            "worked_example",
            "neighbours",
            "declared",
            "judgment_ids",
        ),
        ("kind", "proposed_capability", "reversed"),
        "o",
    ),
    "promote_word": OpSpec(
        _callers("KP"),
        ("op_id", "word_type", "word", "reason"),
        ("kind",),
        "o",
    ),
}

UPDATE_FIELDS = (
    "title",
    "summary",
    "body",
    "properties",
    "search_terms",
    "became_true",
    "recheck_by",
    "recheck_how",
)
LINK_END_CLASSES = ("ended_in_the_world", "refuted", "superseded", "retired")
OBSERVATION_REQUIRED = (
    "ev_id",
    "episode_id",
    "source_id",
    "locator",
    "message_key",
    "source_time",
    "statement_class",
    "derivation",
    "speaker_kind",
    "modality",
)
OBSERVATION_OPTIONAL = (
    "premises",
    "speaker_item",
    "original_author",
    "source_offset_minutes",
    "person_timezone",
)
# A missing one of these means the row cannot name where it came from.
_PROVENANCE_KEYS = ("episode_id", "source_id", "locator", "message_key", "source_time")
_REF_KEYS = frozenset({"ev_id", "evidence_id", "polarity"})
_DISTINCT_KEYS = frozenset({"item", "reason"})
# Fields that take an item or link id, and so also a temp id of this batch.
REF_FIELDS = ("item", "link", "target", "from", "to")

# Keys no caller may send, wherever they sit (not inside properties, which are the item's data).
_FORBIDDEN: dict[str, tuple[str, str]] = {
    "source_type": ("caller_source_type", "caller_source_type"),
    "derived_source_type": ("caller_source_type", "caller_source_type"),
    **{
        key: ("caller_score", "caller_score")
        for key in (
            "score",
            "scores",
            "weight",
            "weights",
            "band",
            "load_bearing",
            "agree",
            "disagree",
            "ratio",
        )
    },
    "verdict": ("unrecorded_verdict", "caller_verdict"),
    "verdicts": ("unrecorded_verdict", "caller_verdict"),
    "last_confirmed": ("invalid_arguments", "verify_only"),
    "confirmed_against": ("invalid_arguments", "verify_only"),
    **{
        key: ("invalid_arguments", "writer_field")
        for key in (
            "id",
            "world_id",
            "recorded",
            "touched_at",
            "provenance_evidence_id",
            "provenance_reason",
            "status_role",
            "status_since",
            "active_key",
            "property_values",
        )
    },
}
# On an operation the writer also derives these from the evidence; on an observation the first
# two are the caller's own classification.
_OP_ONLY_FORBIDDEN: dict[str, tuple[str, str]] = {
    "derivation": ("caller_source_type", "caller_source_type"),
    "speaker": ("caller_source_type", "caller_source_type"),
    "modality": ("caller_source_type", "caller_source_type"),
}


def is_temp(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TEMP_PREFIX)


def _forbidden(keys: Any, table: dict[str, tuple[str, str]], where: str) -> None:
    for key in keys:
        if key in table:
            code, rule = table[key]
            raise Refused(code, rule, f"a caller may not send {key!r} ({where})", {"field": key})


def _text(value: Any, name: str, *, rule: str = "type") -> str:
    if not isinstance(value, str) or reads_blank(value):
        raise Refused("invalid_arguments", rule, f"{name} must be non-empty text", {"field": name})
    return value


def _clock(value: Any, name: str) -> None:
    code = "missing_became_true" if name == "became_true" else "invalid_arguments"
    try:
        parse_clock(value)
    except ClockError as error:
        raise Refused(code, error.rule, f"{name}: {error}", {"field": name}) from error


def _instant(value: Any, name: str) -> None:
    try:
        parse_instant(value)
    except ClockError as error:
        raise Refused(
            "invalid_arguments", error.rule, f"{name}: {error}", {"field": name}
        ) from error


def _enum(value: Any, name: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise Refused(
            "invalid_arguments",
            "enum",
            f"{name} must be one of {list(allowed)}",
            {"field": name, "valid": list(allowed)},
        )


def _string_list(value: Any, name: str) -> None:
    ok = isinstance(value, list) and all(isinstance(v, str) and not reads_blank(v) for v in value)
    if not ok:
        raise Refused("invalid_arguments", "type", f"{name} is a list of text", {"field": name})


def _recheck_how(value: Any) -> None:
    if not isinstance(value, dict | str) or (isinstance(value, str) and reads_blank(value)):
        raise Refused(
            "invalid_arguments",
            "type",
            "recheck_how is an object (connector, locator or query, what to compare) or a code",
            {"field": "recheck_how"},
        )


def _version(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused(
            "invalid_arguments", "type", "version is a whole number", {"field": "version"}
        )


def _refs(op: dict[str, Any]) -> list[dict[str, Any]]:
    refs = op.get("evidence_refs")
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise Refused("invalid_arguments", "type", "evidence_refs is a list", {})
    for ref in refs:
        if not isinstance(ref, dict):
            raise Refused("invalid_arguments", "type", "an evidence ref is an object", {})
        _forbidden(ref, _FORBIDDEN, "in an evidence ref")
        extra = set(ref) - _REF_KEYS
        if extra:
            raise Refused(
                "invalid_arguments",
                "unknown_field",
                f"an evidence ref has no field {sorted(extra)[0]!r}",
                {"field": sorted(extra)[0]},
            )
        if ("ev_id" in ref) == ("evidence_id" in ref):
            raise Refused(
                "invalid_arguments",
                "evidence_ref",
                "an evidence ref names ev_id or evidence_id",
                {},
            )
        _text(ref.get("ev_id", ref.get("evidence_id")), "ev_id")
        _enum(ref.get("polarity"), "polarity", ENUMS["polarity"])
    return refs


def _distinct_from(value: Any) -> None:
    if not isinstance(value, list):
        raise Refused("invalid_arguments", "type", "distinct_from is a list", {})
    for entry in value:
        if not isinstance(entry, dict):
            raise Refused("invalid_arguments", "type", "a distinct_from entry is an object", {})
        _forbidden(entry, _FORBIDDEN, "in distinct_from")
        extra = set(entry) - _DISTINCT_KEYS
        if extra:
            raise Refused(
                "invalid_arguments",
                "unknown_field",
                f"distinct_from has no field {sorted(extra)[0]!r}",
                {"field": sorted(extra)[0]},
            )
        _text(entry.get("item"), "distinct_from.item")
        _text(entry.get("reason"), "distinct_from.reason", rule="distinct_reason")


def _update_fields(fields: Any) -> None:
    if not isinstance(fields, dict) or not fields:
        raise Refused("invalid_arguments", "type", "fields is a non-empty object", {})
    _forbidden(fields, _FORBIDDEN, "in fields")
    _forbidden(fields, _OP_ONLY_FORBIDDEN, "in fields")
    for key in fields:
        if key not in UPDATE_FIELDS:
            raise Refused(
                "invalid_arguments",
                "unknown_field",
                f"update_item may not change {key!r}",
                {"field": key, "valid": list(UPDATE_FIELDS)},
            )
    for key, value in fields.items():
        if key == "title" and (not isinstance(value, str) or reads_blank(value)):
            raise Refused("invalid_arguments", "required_field", "title may not be blank", {})
        if key in ("summary", "body") and value is not None and not isinstance(value, str):
            raise Refused("invalid_arguments", "type", f"{key} is text", {"field": key})
        if key == "properties" and not isinstance(value, dict):
            raise Refused("invalid_arguments", "type", "properties is an object", {})
        if key == "search_terms" and value is not None:
            _string_list(value, key)
        if key == "became_true":
            _clock(value, key)
        if key == "recheck_by" and value is not None:
            _instant(value, key)
        if key == "recheck_how" and value is not None:
            _recheck_how(value)


def _check_fields(name: str, op: dict[str, Any]) -> None:
    if name in ("create_item", "create_link"):
        if op.get("became_true") is None:
            raise Refused(
                "missing_became_true",
                "became_true_required",
                'became_true is a clock or "unknown", never left out',
                {},
            )
        _clock(op["became_true"], "became_true")
    if "stopped_being_true" in op:
        _clock(op["stopped_being_true"], "stopped_being_true")
    for key in ("kind", "title", "meaning", "reason", "status", "original_phrase", "op_id"):
        if key in op:
            rule = "required_field" if key == "title" else "type"
            _text(op[key], key, rule=rule)
    for key in (*REF_FIELDS, "evidence", "ev_id", "evidence_id"):
        if key in op:
            _text(op[key], key)
    for key in ("summary", "body"):
        if op.get(key) is not None and not isinstance(op[key], str):
            raise Refused("invalid_arguments", "type", f"{key} is text", {"field": key})
    if op.get("properties") is not None and not isinstance(op["properties"], dict):
        raise Refused("invalid_arguments", "type", "properties is an object", {})
    if op.get("search_terms") is not None:
        _string_list(op["search_terms"], "search_terms")
    if op.get("recheck_by") is not None:
        _instant(op["recheck_by"], "recheck_by")
    if op.get("recheck_how") is not None:
        _recheck_how(op["recheck_how"])
    if "distinct_from" in op:
        _distinct_from(op["distinct_from"])
    if "version" in op:
        _version(op["version"])
    if "end_class" in op:
        _enum(op["end_class"], "end_class", LINK_END_CLASSES)
    if "end_reason" in op:
        _enum(op["end_reason"], "end_reason", ENUMS["evidence_end_reason"])
    if "polarity" in op:
        _enum(op["polarity"], "polarity", ENUMS["polarity"])
    if "judgment_ids" in op:
        _string_list(op["judgment_ids"], "judgment_ids")
    if "temp_id" in op and (not is_temp(op["temp_id"]) or len(op["temp_id"]) < 2):
        raise Refused("invalid_arguments", "temp_id", f"a temp id starts with {TEMP_PREFIX!r}", {})
    if name == "update_item":
        _update_fields(op["fields"])
    if name == "attach_evidence" and ("ev_id" in op) == ("evidence_id" in op):
        raise Refused(
            "invalid_arguments", "missing_field", "attach_evidence names ev_id or evidence_id", {}
        )
    if name in ("propose_word", "promote_word"):
        _word_op_fields(name, op)


def _word_op_fields(name: str, op: dict[str, Any]) -> None:
    """The form of an ontology operation; the registry checks run in grove.writer.ontology."""
    _enum(op.get("word_type"), "word_type", ("kind", "meaning", "property_name"))
    if (op.get("word_type") == "property_name") != ("kind" in op):
        raise Refused(
            "invalid_arguments",
            "missing_field" if op.get("word_type") == "property_name" else "unknown_field",
            "a property name names its kind, and only a property name does",
            {"field": "kind"},
        )
    if name == "promote_word":
        _text(op.get("word"), "word")
        return
    for key in ("name", "description"):
        _text(op.get(key), key)
    example = op.get("worked_example")
    if (
        not isinstance(example, dict)
        or len(example) != 1
        or not ({"ev_id", "evidence_id"} & set(example))
    ):
        raise Refused(
            "invalid_arguments",
            "worked_example",
            "worked_example is {ev_id} or {evidence_id}: the evidence row of a use",
            {"field": "worked_example"},
        )
    _text(next(iter(example.values())), "worked_example")
    if not isinstance(op.get("neighbours"), list):
        raise Refused("invalid_arguments", "type", "neighbours is a list", {"field": "neighbours"})
    if not isinstance(op.get("declared"), dict):
        raise Refused("invalid_arguments", "type", "declared is an object", {"field": "declared"})


def _check_op(op: Any) -> None:
    if not isinstance(op, dict):
        raise Refused("invalid_arguments", "type", "an operation is an object", {})
    name = op.get("op")
    if name not in OPS:
        raise Refused(
            "invalid_arguments",
            "unknown_op",
            f"no operation {name!r} in this writer",
            {"valid": sorted(OPS)},
        )
    spec = OPS[name]
    _forbidden(op, _FORBIDDEN, "on an operation")
    _forbidden(op, _OP_ONLY_FORBIDDEN, "on an operation")
    if "version" in op and "version" not in spec.required + spec.optional:
        raise Refused(
            "invalid_arguments", "writer_field", f"{name} takes no version", {"field": "version"}
        )
    refs = _refs(op)
    if name == "update_item" and isinstance(op.get("fields"), dict):
        _forbidden(op["fields"], _FORBIDDEN, "in fields")
        _forbidden(op["fields"], _OP_ONLY_FORBIDDEN, "in fields")
    if isinstance(op.get("distinct_from"), list):
        for entry in op["distinct_from"]:
            if isinstance(entry, dict):
                _forbidden(entry, _FORBIDDEN, "in distinct_from")
    allowed = {"op", *spec.required, *spec.optional}
    for key in op:
        if key not in allowed:
            raise Refused(
                "invalid_arguments",
                "unknown_field",
                f"{name} has no field {key!r}",
                {"field": key, "valid": sorted(allowed - {"op"})},
            )
    for key in spec.required:
        if key in op and op[key] is not None:
            continue
        if key == "became_true":
            continue  # _check_fields names it
        if key == "evidence_refs":
            raise Refused(
                "missing_provenance", "evidence_refs_required", f"{name} must cite evidence", {}
            )
        raise Refused("invalid_arguments", "missing_field", f"{name} needs {key}", {"field": key})
    if "evidence_refs" in spec.required and not refs:
        raise Refused(
            "missing_provenance", "evidence_refs_required", f"{name} must cite evidence", {}
        )
    if name in ("create_item", "create_link") and not any(
        ref["polarity"] == "supports" for ref in refs
    ):
        raise Refused(
            "missing_provenance",
            "no_supporting_evidence",
            "a claim names at least one row that supports it",
            {},
        )
    _check_fields(name, op)


def check_observation(observation: Any) -> None:
    """The form of one observation; the store checks (source, episode, locator) come later."""
    if not isinstance(observation, dict):
        raise Refused("invalid_arguments", "type", "an observation is an object", {})
    _forbidden(observation, _FORBIDDEN, "on an observation")
    allowed = {*OBSERVATION_REQUIRED, *OBSERVATION_OPTIONAL}
    for key in observation:
        if key not in allowed:
            raise Refused(
                "invalid_arguments",
                "unknown_field",
                f"an observation has no field {key!r}",
                {"field": key, "valid": sorted(allowed)},
            )
    for key in _PROVENANCE_KEYS:
        value = observation.get(key)
        if not isinstance(value, str) or reads_blank(value):
            raise Refused(
                "missing_provenance",
                f"missing_{key}",
                f"an evidence row names its {key}",
                {"ev_id": observation.get("ev_id"), "field": key},
            )
    for key in ("statement_class", "derivation", "speaker_kind", "modality"):
        if key not in observation:
            raise Refused(
                "invalid_arguments", "missing_field", f"an observation needs {key}", {"field": key}
            )
        _enum(observation[key], key, ENUMS[key])
    _instant(observation["source_time"], "source_time")
    premises = observation.get("premises")
    if premises is not None:
        if not isinstance(premises, list) or not all(isinstance(p, str) for p in premises):
            raise Refused("invalid_arguments", "type", "premises is a list of evidence ids", {})
    if observation["derivation"] != "observed" and not premises:
        raise Refused(
            "inferred_evidence",
            "premises_required",
            "an inferred or rolled-up row names its premises",
            {"ev_id": observation.get("ev_id")},
        )
    for key in ("speaker_item", "original_author"):
        if observation.get(key) is not None:
            _text(observation[key], key)
    offset = observation.get("source_offset_minutes")
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
        raise Refused("invalid_arguments", "type", "source_offset_minutes is whole minutes", {})
    zone = observation.get("person_timezone")
    if zone is not None:
        try:
            zoneinfo.ZoneInfo(_text(zone, "person_timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            raise Refused(
                "invalid_arguments", "timezone", "person_timezone is an IANA code", {}
            ) from error


def cited_ev_ids(op: dict[str, Any]) -> list[str]:
    cited = [ref["ev_id"] for ref in op.get("evidence_refs") or [] if "ev_id" in ref]
    if op.get("op") == "attach_evidence" and isinstance(op.get("ev_id"), str):
        cited.append(op["ev_id"])
    example = op.get("worked_example") if op.get("op") == "propose_word" else None
    if isinstance(example, dict) and isinstance(example.get("ev_id"), str):
        cited.append(example["ev_id"])
    return cited


def temp_refs(op: dict[str, Any]) -> list[str]:
    """Every temp id an operation reads (not the one it declares)."""
    found = [op[key] for key in REF_FIELDS if is_temp(op.get(key))]
    for entry in op.get("distinct_from") or []:
        if isinstance(entry, dict) and is_temp(entry.get("item")):
            found.append(entry["item"])
    return found


def batch_ops(batch: Any) -> list[Any]:
    """The operations of a batch, or [] when the batch has none to name."""
    if isinstance(batch, dict) and isinstance(batch.get("ops"), list):
        return batch["ops"]
    return []


def op_label(op: Any) -> tuple[str | None, str | None]:
    if not isinstance(op, dict):
        return None, None
    op_id = op.get("op_id") if isinstance(op.get("op_id"), str) else None
    name = op.get("op") if isinstance(op.get("op"), str) else None
    return op_id, name


def shape_refusals(batch: Any) -> tuple[list[Refusal], dict[int, Refusal]]:
    """(faults of the whole batch, refusals by operation index)."""
    if not isinstance(batch, dict):
        return [Refusal(None, None, "invalid_arguments", "type", "a batch is an object", {})], {}
    extra = set(batch) - {"ops", "evidence"}
    if extra:
        key = sorted(extra)[0]
        return [
            Refusal(None, None, "invalid_arguments", "unknown_field", f"a batch has no {key!r}", {})
        ], {}
    ops = batch.get("ops")
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        message = f"ops is a list of 1 to {MAX_OPS} operations"
        return [Refusal(None, None, "invalid_arguments", "ops_count", message, {})], {}
    evidence = batch.get("evidence", [])
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        message = "evidence is a list of observations"
        return [Refusal(None, None, "invalid_arguments", "type", message, {})], {}

    faults: list[Refusal] = []
    observations: dict[str, Any] = {}
    for observation in evidence:
        ev_id = observation.get("ev_id") if isinstance(observation, dict) else None
        if not isinstance(ev_id, str) or reads_blank(ev_id):
            message = "every observation has an ev_id"
            faults.append(Refusal(None, None, "invalid_arguments", "missing_field", message, {}))
        elif ev_id in observations:
            faults.append(
                Refusal(
                    None,
                    None,
                    "invalid_arguments",
                    "duplicate_ev_id",
                    f"ev_id {ev_id!r} is used twice",
                    {"ev_id": ev_id},
                )
            )
        else:
            observations[ev_id] = observation
    seen_ids: set[str] = set()
    for op in ops:
        op_id, _ = op_label(op)
        if op_id is not None and op_id in seen_ids:
            faults.append(
                Refusal(
                    None,
                    None,
                    "invalid_arguments",
                    "duplicate_op_id",
                    f"op_id {op_id!r} is used twice",
                    {"op_id": op_id},
                )
            )
        if op_id is not None:
            seen_ids.add(op_id)
    if faults:
        return faults, {}

    observation_faults: dict[str, Refused | None] = {}
    by_index: dict[int, Refusal] = {}
    declared: dict[str, int] = {}
    for index, op in enumerate(ops):
        op_id, name = op_label(op)
        try:
            _check_op(op)
            for ev_id in cited_ev_ids(op):
                if ev_id not in observations:
                    raise Refused(
                        "missing_provenance",
                        "unknown_ev_id",
                        f"no observation {ev_id!r} in this batch",
                        {"ev_id": ev_id},
                    )
                if ev_id not in observation_faults:
                    try:
                        check_observation(observations[ev_id])
                        observation_faults[ev_id] = None
                    except Refused as refused:
                        observation_faults[ev_id] = refused
                fault = observation_faults[ev_id]
                if fault is not None:
                    raise fault
            for temp in temp_refs(op):
                if temp not in declared:
                    raise Refused(
                        "invalid_arguments",
                        "unknown_temp_id",
                        f"temp id {temp} is not declared by an earlier operation",
                        {"temp_id": temp},
                    )
            temp = op.get("temp_id")
            if temp is not None:
                if temp in declared:
                    raise Refused(
                        "invalid_arguments",
                        "duplicate_temp_id",
                        f"temp id {temp} is declared twice",
                        {"temp_id": temp},
                    )
                declared[temp] = index
        except Refused as refused:
            by_index[index] = refused.refusal(op_id, name)
            temp = op.get("temp_id") if isinstance(op, dict) else None
            if is_temp(temp) and temp not in declared:
                declared[temp] = index
    return [], by_index


def check_shape(batch: Any) -> list[Refusal]:
    """Every refusal the form of the batch earns, before the store is touched."""
    faults, by_index = shape_refusals(batch)
    return faults + [by_index[index] for index in sorted(by_index)]
