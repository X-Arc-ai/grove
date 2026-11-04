"""Flat grove.extract.v1 schema fitted to the current writer operation catalogue.

The supported profile is explicit. A field has one source name in OPS and a deliberate
wire adapter below; a new writer field cannot silently appear in an extraction prompt.
"""

from copy import deepcopy

from grove.extraction.contracts import RegistrySnapshot, validate_registry
from grove.store.enums import ENUMS
from grove.writer.ops import OPS, UPDATE_FIELDS

SCHEMA_NAME = "grove.extract.v1"
SUPPORTED_OPS = (
    "create_item",
    "update_item",
    "change_status",
    "end_item",
    "create_link",
    "end_link",
    "attach_evidence",
    "refute",
    "supersede",
)
UNSUPPORTED_BRANCHES = (
    "mentions",
    "contradictions",
    "questions",
    "belief_candidates",
    "ontology_proposals",
)
TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
LONG_TEXT = {"type": "string", "minLength": 1, "maxLength": 16384}
NULL = {"type": "null"}


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def array(items: dict, maximum: int = 256, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum, "minItems": minimum}


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, NULL]}


def enum(values) -> dict:
    # An empty registry must offer no valid value rather than invent a word.
    return {"type": "string", "enum": list(values)} if values else {"not": {}}


BOUND = nullable({"type": "string", "minLength": 1, "maxLength": 256})
CLOCK = {
    "anyOf": [
        {"const": "unknown"},
        obj({"earliest": BOUND, "latest": BOUND, "precision": enum(ENUMS["precision"])}),
    ]
}
REF = obj({"ev_id": TEXT, "polarity": enum(ENUMS["polarity"])})
SEARCH_TERM = obj(
    {
        "text": TEXT,
        "type": enum(("paraphrase", "synonym", "translation", "role_phrase")),
        "language": {"type": "string", "minLength": 2, "maxLength": 32},
    }
)
VALUE = {"type": ["string", "number", "boolean", "null"], "maxLength": 16384}
FIELDS = array(obj({"name": TEXT, "value": VALUE}), 64)
HOW = obj({"connector": TEXT, "locator_or_query": TEXT, "compare": TEXT})
RECHECK = nullable(obj({"by_proposed": TEXT, "how": HOW, "reason": TEXT}))
EVIDENCE = obj(
    {
        "ev_id": TEXT,
        "locator": TEXT,
        "statement_class": enum(ENUMS["statement_class"]),
        "derivation": enum(("observed", "inferred")),
        "premises": array(TEXT, 32),
        "speaker_fallback": nullable(obj({"kind": enum(ENUMS["speaker_kind"]), "name": TEXT})),
        "modality_fallback": nullable(enum(ENUMS["modality"])),
        "time_expression": nullable(TEXT),
        "became_true": CLOCK,
        "scheduled_times": array(
            obj(
                {
                    "role": enum(ENUMS["scheduled_role"]),
                    "earliest": BOUND,
                    "latest": BOUND,
                    "precision": enum(ENUMS["precision"]),
                    "words": TEXT,
                }
            ),
            3,
        ),
        "recheck": RECHECK,
    }
)

# Generated operation fields derive from the writer's required and optional tuples.
# Version, confirmation calls and clocks are supplied by code from the source snapshot.
CODE_FIELDS = {"version", "confirmed_by_call", "became_true", "recheck_by", "recheck_how"}
WIRE_NAMES = {
    "item": "target_ref",
    "link": "target_ref",
    "target": "target_ref",
    "from": "from_ref",
    "to": "to_ref",
    "successor": "successor_ref",
    "properties": "fields",
}
FIELD_SCHEMAS = {
    "op_id": TEXT,
    "temp_id": {"type": "string", "pattern": r"^\$.+", "maxLength": 1024},
    "title": TEXT,
    "summary": nullable(LONG_TEXT),
    "body": nullable(LONG_TEXT),
    "evidence_refs": array(REF, 64, 1),
    "reason": TEXT,
    "fields": FIELDS,
    "search_terms": array(SEARCH_TERM, 32, 1),
    "end_class": enum(
        (
            "ended_in_the_world",
            "refuted",
            "superseded",
            "retired",
        )
    ),
    "stopped_being_true": CLOCK,
    "distinct_from": array(obj({"item": TEXT, "reason": TEXT}), 32),
    "original_phrase": TEXT,
}


def operation_schema(name: str, registry: RegistrySnapshot) -> dict:
    spec = OPS[name]
    if "update" not in spec.callers:
        raise ValueError("the extraction profile exceeds the writer caller boundary")
    fields = {"op": {"const": name}}
    required = ["op", "relation_to_standing", "evidence_refs", "reason"]
    for field in (*spec.required, *spec.optional):
        if field in CODE_FIELDS or field in {"ev_id", "evidence_id", "polarity", "judgment_ids"}:
            continue
        wire = WIRE_NAMES.get(field, field)
        schema = FIELD_SCHEMAS.get(wire)
        if field == "kind":
            schema = enum(registry.kinds)
        elif field == "meaning":
            schema = enum(registry.meanings)
        elif field == "status":
            schema = enum(
                sorted({status for values in registry.kinds.values() for status in values})
            )
        elif wire.endswith("_ref"):
            schema = TEXT
        if schema is None:
            raise ValueError("an extraction adapter is required for the writer field")
        fields[wire] = deepcopy(schema)
        if field in spec.required:
            required.append(wire)
    fields["relation_to_standing"] = enum(
        ("confirms", "refines", "contradicts", "complements", "new")
    )
    fields["evidence_refs"] = array(REF, 64, 1)
    fields["reason"] = TEXT
    if name == "create_item":
        required.extend(("search_terms", "summary"))
    if name == "update_item":
        # The writer stores these inside fields. The flat wire format retains typed terms.
        assert "search_terms" in UPDATE_FIELDS
        fields["search_terms"] = array(SEARCH_TERM, 32, 1)
    return obj(fields, list(dict.fromkeys(required)))


def extraction_schema(registry: RegistrySnapshot) -> dict:
    validate_registry(registry)
    properties = {
        "schema": {"const": SCHEMA_NAME},
        "episode": obj({"episode_id": TEXT, "source_id": TEXT}),
        "evidence": array(EVIDENCE),
        "operations": array(
            {"anyOf": [operation_schema(name, registry) for name in SUPPORTED_OPS]}
        ),
        "declines": array(obj({"locator": TEXT, "what_it_would_have_done": TEXT, "why_not": TEXT})),
    }
    # A non-empty unsupported branch is never thrown away. The pipeline returns unsupported.
    for branch in UNSUPPORTED_BRANCHES:
        properties[branch] = array(obj({"reason": TEXT, "locator": TEXT}), 128)
    return obj(properties)


TRIAGE_SCHEMA = obj(
    {
        "schema": {"const": "grove.triage.v1"},
        "has_candidates": {"type": "boolean"},
        "reviewed_locators": array(TEXT, 128, 1),
        "candidates": array(
            obj(
                {
                    "locator": TEXT,
                    "statement_class": enum(ENUMS["statement_class"]),
                    "speaker_kind": enum(ENUMS["speaker_kind"]),
                    "modality": enum(ENUMS["modality"]),
                    "machine_written": {"type": "boolean"},
                    "forwarded": {"type": "boolean"},
                }
            )
        ),
        "redactions": array(
            obj(
                {
                    "locator": TEXT,
                    "start": {"type": "integer", "minimum": 0, "maximum": 16384},
                    "end": {"type": "integer", "minimum": 1, "maximum": 16384},
                    "category": TEXT,
                }
            )
        ),
    }
)
