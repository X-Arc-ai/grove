"""Bounded private codecs and complete payload checks for the inactive Learner.

No persisted record, digest or successful check supplies runtime authority.
"""

import json
import re
from dataclasses import fields

from . import execution_codec as legacy
from .contracts import RegistrySnapshot, SourceBatch, SourceMessage, validate_registry
from .durable_types import (
    OperationMapEntry,
    PayloadEnvelope,
    RecordedAttemptWitness,
    RunWitness,
    StoredCallRequest,
    StoredCallResult,
    StoredInvocationRef,
)
from .execution_profile import WRITER_OPS, check_inventory, check_legacy_profile
from .execution_types import ExecutionEvent, ExecutionState, Invocation, TriageTrace

LIMITS = {
    "initial_input": 1_048_576,
    "binding": 1_048_576,
    "event": 1_048_576,
    "state": 262_144,
    "proposal": 524_288,
    "batch": 524_288,
    "triage": 262_144,
    "call_request": 524_288,
    "call_result": 524_288,
    "call_binding": 16_384,
    "disposition": 16_384,
    "completion": 262_144,
    "handoff": 262_144,
}
MAX_WORK_BYTES = 16 * 1_048_576
MAX_REVISIONS = 64
MAX_CALLS = 5
MAX_BINDINGS = 2
MAX_NODES = 16_384
MAX_MEMBERS = 2048
MAX_DEPTH = 16
DIGEST_NAMES = (
    "wire_request",
    "wire_response",
    "selected_output",
    "normalized_request",
    "normalized_output",
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
refuse = legacy.refuse
plain = legacy.plain


def _id(value, maximum=128):
    if type(value) is not str or len(value) > maximum or not _ID.fullmatch(value):
        refuse("identifier_bounds")
    return value


def _int(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        refuse("integer_bounds")
    return value


def _closed(value, names):
    if type(value) is not dict or set(value) != set(names):
        refuse("closed_fields_required")
    return value


def preflight_json(raw, maximum=1_048_576):
    """Validate tokens and allocation bounds before calling the JSON allocator."""
    if type(raw) not in (str, bytes) or len(raw) > maximum:
        refuse("document_bounds")
    try:
        text = raw.decode("utf-8") if type(raw) is bytes else raw
        if len(text.encode("utf-8")) > maximum:
            refuse("document_bounds")
    except UnicodeError:
        refuse("invalid_utf8")
    pos, nodes, size = 0, 0, len(text)

    def space():
        nonlocal pos
        while pos < size and text[pos] in " \t\r\n":
            pos += 1

    def string():
        nonlocal pos
        if pos >= size or text[pos] != '"':
            refuse("invalid_json")
        pos += 1
        while pos < size:
            char = text[pos]
            pos += 1
            if char == '"':
                return
            if ord(char) < 32:
                refuse("invalid_json")
            if char == "\\":
                if pos >= size or text[pos] not in '"\\/bfnrtu':
                    refuse("invalid_json")
                if text[pos] == "u":
                    if pos + 4 >= size or any(
                        c not in "0123456789abcdefABCDEF" for c in text[pos + 1 : pos + 5]
                    ):
                        refuse("invalid_json")
                    pos += 4
                pos += 1
        refuse("invalid_json")

    def value(depth):
        nonlocal pos, nodes
        nodes += 1
        if nodes > MAX_NODES:
            refuse("aggregate_bounds")
        space()
        if pos >= size:
            refuse("invalid_json")
        char = text[pos]
        if char in "[{":
            if depth >= MAX_DEPTH:
                refuse("nesting_bounds")
            pos += 1
            close = "]" if char == "[" else "}"
            space()
            if pos < size and text[pos] == close:
                pos += 1
                return
            members = 0
            while True:
                members += 1
                if members > MAX_MEMBERS:
                    refuse("collection_bounds")
                space()
                if char == "{":
                    string()
                    nodes += 1
                    if nodes > MAX_NODES:
                        refuse("aggregate_bounds")
                    space()
                    if pos >= size or text[pos] != ":":
                        refuse("invalid_json")
                    pos += 1
                value(depth + 1)
                space()
                if pos < size and text[pos] == close:
                    pos += 1
                    return
                if pos >= size or text[pos] != ",":
                    refuse("invalid_json")
                pos += 1
        elif char == '"':
            string()
        elif char in "-0123456789":
            start = pos
            if char == "-":
                pos += 1
            if pos >= size or text[pos] not in "0123456789":
                refuse("invalid_json")
            if text[pos] == "0":
                pos += 1
            else:
                while pos < size and text[pos] in "0123456789":
                    pos += 1
            token = text[start:pos]
            if len(token) > 20 or not -(2**63) <= int(token) <= 2**63 - 1:
                refuse("integer_bounds")
            if pos < size and text[pos] in ".eE":
                refuse("integer_required")
        else:
            for literal in ("true", "false", "null"):
                if text.startswith(literal, pos):
                    pos += len(literal)
                    return
            refuse("invalid_json")

    value(0)
    space()
    if pos != size:
        refuse("invalid_json")
    return text


def load_json(raw, maximum=1_048_576):
    text = preflight_json(raw, maximum)
    try:
        value = json.loads(text, object_pairs_hook=legacy._object)
        legacy._check_tree(value)
        # Keys need the same UTF-8 validation as values.
        pending = [value]
        while pending:
            child = pending.pop()
            if type(child) is dict:
                for key in child:
                    key.encode("utf-8")
                pending.extend(child.values())
            elif type(child) is list:
                pending.extend(child)
        return value
    except (UnicodeError, json.JSONDecodeError):
        refuse("invalid_json")


def canonical(value, maximum=1_048_576):
    """Bound direct values before encoding; the token preflight covers decoded input."""
    nodes, total = 0, 0

    def visit(child, depth):
        nonlocal nodes, total
        nodes += 1
        if nodes > MAX_NODES or total > maximum:
            refuse("aggregate_bounds")
        if type(child) in (dict, list, tuple):
            if depth >= MAX_DEPTH or len(child) > MAX_MEMBERS:
                refuse("nesting_bounds" if depth >= MAX_DEPTH else "collection_bounds")
            if type(child) is dict:
                for key, item in child.items():
                    if type(key) is not str:
                        refuse("invalid_document")
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            else:
                for item in child:
                    visit(item, depth + 1)
        elif type(child) is str:
            if len(child) > maximum:
                refuse("document_bounds")
            try:
                total += len(child.encode("utf-8"))
            except UnicodeError:
                refuse("invalid_utf8")
        elif type(child) is int:
            if not -(2**63) <= child <= 2**63 - 1:
                refuse("integer_bounds")
        elif child is not None and type(child) is not bool:
            refuse("invalid_document")

    visit(value, 0)
    if total > maximum:
        refuse("document_bounds")
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > maximum:
        refuse("document_bounds")
    return encoded


def decode_run_witness(value):
    if type(value) in (str, bytes):
        value = load_json(value, 4096)
    if type(value) is RunWitness:
        value = plain(value)
    _closed(value, (f.name for f in fields(RunWitness)))
    _id(value["actual_run_id"], 80)
    _id(value["pass_id"])
    _int(value["actual_row_version"], 1)
    _int(value["protocol_run_version"], 1)
    ids = value["input_ids"]
    if type(ids) is not list or not 1 <= len(ids) <= 5 or len(set(ids)) != len(ids):
        refuse("run_witness")
    for key in ids:
        _id(key)
    return RunWitness(**{**value, "input_ids": tuple(ids)})


def check_run_witness(witness, run):
    witness = decode_run_witness(witness)
    if (
        witness.actual_run_id,
        witness.protocol_run_version,
        witness.pass_id,
        witness.input_ids,
    ) != (run.id, run.version, run.pass_id, run.input_ids):
        refuse("run_witness")


def writer_schema_digest():
    from grove.writer.ops import OBSERVATION_OPTIONAL, OBSERVATION_REQUIRED, OPS

    return legacy.data_digest(
        "grove.learner.writer-schema.v1",
        {
            "operations": {
                name: {
                    "required": list(OPS[name].required),
                    "optional": list(OPS[name].optional),
                    "callers": sorted(OPS[name].callers),
                }
                for name in WRITER_OPS
            },
            "observation_required": list(OBSERVATION_REQUIRED),
            "observation_optional": list(OBSERVATION_OPTIONAL),
        },
    )


def payload_bytes(payload):
    if type(payload) is not PayloadEnvelope:
        refuse("payload_missing")
    return canonical(
        {
            "schema": payload.schema,
            "writer_revision": payload.writer_revision,
            "binding_digest": payload.binding_digest,
            "batch": load_json(payload.batch, LIMITS["batch"]),
            "operation_map": [plain(entry) for entry in payload.operation_map],
        },
        LIMITS["batch"],
    )


def decode_payload(raw):
    from grove.writer.ops import check_shape

    value = load_json(raw, LIMITS["batch"])
    _closed(value, ("schema", "writer_revision", "binding_digest", "batch", "operation_map"))
    if (
        value["schema"] != "grove.learner.payload.v1"
        or type(value["writer_revision"]) is not str
        or not _SHA.fullmatch(value["writer_revision"])
    ):
        refuse("payload_mismatch")
    if type(value["binding_digest"]) is not str or not _DIGEST.fullmatch(value["binding_digest"]):
        refuse("payload_mismatch")
    batch = _closed(value["batch"], ("ops", "evidence"))
    if (
        type(batch["ops"]) is not list
        or len(batch["ops"]) > 500
        or type(batch["evidence"]) is not list
        or len(batch["evidence"]) > 1024
    ):
        refuse("payload_mismatch")
    if not batch["ops"]:
        if batch["evidence"]:
            refuse("payload_mismatch")
    elif check_shape(batch):
        refuse("writer_shape")
    for op in batch["ops"]:
        if op["op"] not in WRITER_OPS:
            refuse("writer_profile")
        if op["op"] == "attach_evidence" and (
            "evidence_id" in op or "judgment_ids" in op or not op.get("ev_id")
        ):
            refuse("payload_mismatch")
    entries = value["operation_map"]
    if type(entries) is not list or len(entries) != len(batch["ops"]):
        refuse("payload_mismatch")
    mapped = []
    for index, entry in enumerate(entries):
        _closed(entry, ("descriptor_id", "operation_index"))
        _id(entry["descriptor_id"])
        if (
            _int(entry["operation_index"]) != index
            or entry["descriptor_id"] != batch["ops"][index]["op_id"]
        ):
            refuse("payload_mismatch")
        mapped.append(OperationMapEntry(**entry))
    return PayloadEnvelope(
        value["schema"],
        value["writer_revision"],
        value["binding_digest"],
        canonical(batch, LIMITS["batch"]).decode(),
        tuple(mapped),
    )


def check_descriptor_payloads(binding, proposal, payload):
    from grove.writer.ops import cited_ev_ids

    check_legacy_profile(binding)
    legacy.proposal_digest(proposal)
    payload = decode_payload(payload_bytes(payload))
    if (
        payload.binding_digest != legacy.binding_digest(binding)
        or proposal.binding_digest != payload.binding_digest
        or payload.writer_revision != binding.source_revision
        or binding.schema_digest != writer_schema_digest()
    ):
        refuse("payload_mismatch")
    batch = load_json(payload.batch, LIMITS["batch"])
    if len(proposal.operations) != len(batch["ops"]) or len(proposal.evidence) != len(
        batch["evidence"]
    ):
        refuse("payload_mismatch")
    by_locator = {ref.locator: ref for ref in binding.inventory}
    observed = {}
    for actual, expected in zip(batch["evidence"], proposal.evidence, strict=True):
        ref = by_locator.get(actual["locator"])
        if ref is None or (actual["ev_id"], actual["locator"], actual["statement_class"]) != (
            expected.id,
            expected.locator,
            expected.statement_class,
        ):
            refuse("payload_mismatch")
        if (
            actual["ev_id"] in observed
            or actual["source_id"] != binding.source.id
            or actual["episode_id"] != binding.episode.id
            or actual["message_key"] != ref.key
            or actual["source_time"] != ref.source_time
            or actual["speaker_kind"] != ref.speaker_kind
            or actual["modality"] != ref.modality
            or actual.get("speaker_item") != ref.sender
            or actual.get("original_author") != ref.original_author
            or actual["derivation"] != "observed"
            or actual.get("premises")
            or "source_offset_minutes" in actual
            or "person_timezone" in actual
        ):
            refuse("exact_provenance_required")
        observed[actual["ev_id"]] = actual
    used = set()
    for actual, expected, mapping in zip(
        batch["ops"], proposal.operations, payload.operation_map, strict=True
    ):
        cited = cited_ev_ids(actual)
        if (
            (actual["op_id"], actual["op"]) != (expected.id, expected.op)
            or mapping.descriptor_id != expected.id
            or len(cited) != len(set(cited))
            or tuple(cited) != expected.evidence_ids
            or not cited
            or any(key not in observed for key in cited)
        ):
            refuse("payload_mismatch")
        digest = legacy.data_digest(
            "grove.learner.operation.v1",
            {
                "operation": actual,
                "evidence": [row for row in batch["evidence"] if row["ev_id"] in cited],
            },
        )
        if digest != expected.payload_digest:
            refuse("payload_mismatch")
        if "confirmed_by_call" in actual and actual["confirmed_by_call"] not in {
            call.model_call_id
            for call in proposal.invocations
            if call.stage == "extraction" and call.state == "validated"
        }:
            refuse("call_evidence_missing")
        used.update(cited)
    if used != set(observed):
        refuse("payload_mismatch")
    return batch


def encode_call_binding(invocation, *, digests=None, owner_receipt_id=None):
    legacy.validate_record(invocation)
    value = plain(invocation)
    value.pop("request")
    value.pop("output")
    return canonical(
        {"invocation": value, "digests": digests, "owner_receipt_id": owner_receipt_id},
        LIMITS["call_binding"],
    )


def _refs(refs):
    result = []
    for ref in refs:
        if type(ref) is not StoredInvocationRef:
            refuse("call_reference")
        value = plain(ref)
        for item in value.values():
            if item is not None:
                _id(item)
        result.append(value)
    if len(result) > 5 or len({v["call_id"] for v in result}) != len(result):
        refuse("call_reference")
    return result


def _value(values, key, kind):
    _id(key)
    raw = values.get(key)
    if raw is None:
        refuse("erased_dependency")
    return load_json(raw, LIMITS[kind])


def _invocations(refs, values):
    if type(refs) is not list or len(refs) > 5:
        refuse("call_reference")
    result = []
    for ref in refs:
        _closed(ref, (f.name for f in fields(StoredInvocationRef)))
        req = _value(values, ref["request_value_id"], "call_request")
        _closed(req, (f.name for f in fields(StoredCallRequest)))
        stored = _value(values, ref["binding_value_id"], "call_binding")
        _closed(stored, ("invocation", "digests", "owner_receipt_id"))
        out = None
        if ref["result_value_id"] is not None:
            response = _value(values, ref["result_value_id"], "call_result")
            _closed(response, (f.name for f in fields(StoredCallResult)))
            out = response["normalized_output"]
        call = legacy._decode(
            {**stored["invocation"], "request": req["normalized_request"], "output": out},
            Invocation,
        )
        if call.id != ref["call_id"]:
            refuse("call_reference")
        result.append(call)
    if len({c.id for c in result}) != len(result):
        refuse("call_reference")
    return tuple(result)


def encode_stored_proposal(proposal, binding_value_id, invocation_refs):
    legacy.proposal_digest(proposal)
    _id(binding_value_id)
    if tuple(r.call_id for r in invocation_refs) != tuple(c.id for c in proposal.invocations):
        refuse("call_reference")
    return canonical(
        {
            "codec": 1,
            "binding_value_id": binding_value_id,
            "operations": plain(proposal.operations),
            "evidence": plain(proposal.evidence),
            "dispositions": plain(proposal.dispositions),
            "invocation_refs": _refs(invocation_refs),
        },
        LIMITS["proposal"],
    )


def rehydrate_proposal(raw, values):
    value = load_json(raw, LIMITS["proposal"])
    _closed(
        value,
        ("codec", "binding_value_id", "operations", "evidence", "dispositions", "invocation_refs"),
    )
    if type(value["codec"]) is not int or value["codec"] != 1:
        refuse("storage_codec")
    binding = legacy.decode_binding(canonical(_value(values, value["binding_value_id"], "binding")))
    return legacy.decode_proposal(
        canonical(
            {
                "binding_digest": legacy.binding_digest(binding),
                "operations": value["operations"],
                "evidence": value["evidence"],
                "dispositions": value["dispositions"],
                "invocations": plain(_invocations(value["invocation_refs"], values)),
            }
        )
    )


_STATE_CHILDREN = frozenset(
    {
        "binding",
        "invocations",
        "prior_invocations",
        "proposal",
        "completion",
        "completion_id",
        "triage",
        "predecessor",
        "declined_digests",
    }
)
_STATE_SCALARS = tuple(f.name for f in fields(ExecutionState) if f.name not in _STATE_CHILDREN)


def encode_stored_state(
    state,
    witness,
    binding_value_id,
    *,
    invocation_refs=(),
    prior_invocation_refs=(),
    proposal_value_id=None,
    triage_value_id=None,
):
    legacy.validate_record(state)
    witness = decode_run_witness(witness)
    if witness.protocol_run_version != state.run_version:
        refuse("run_witness")
    if tuple(r.call_id for r in invocation_refs) != tuple(c.id for c in state.invocations) or tuple(
        r.call_id for r in prior_invocation_refs
    ) != tuple(c.id for c in state.prior_invocations):
        refuse("call_reference")
    if (proposal_value_id is None) != (state.proposal is None) or (triage_value_id is None) != (
        state.triage is None
    ):
        refuse("storage_child_missing")
    return canonical(
        {
            "codec": 1,
            "binding_value_id": _id(binding_value_id),
            "run_witness": plain(witness),
            "scalars": {k: plain(getattr(state, k)) for k in _STATE_SCALARS},
            "invocation_refs": _refs(invocation_refs),
            "prior_invocation_refs": _refs(prior_invocation_refs),
            "proposal_value_id": proposal_value_id,
            "completion_id": state.completion_id,
            "triage_value_id": triage_value_id,
            "private_identity": {
                "predecessor": state.predecessor,
                "declined_digests": plain(state.declined_digests),
            },
        },
        LIMITS["state"],
    )


def rehydrate_state(raw, values, *, completions=None):
    value = load_json(raw, LIMITS["state"])
    _closed(
        value,
        (
            "codec",
            "binding_value_id",
            "run_witness",
            "scalars",
            "invocation_refs",
            "prior_invocation_refs",
            "proposal_value_id",
            "completion_id",
            "triage_value_id",
            "private_identity",
        ),
    )
    if type(value["codec"]) is not int or value["codec"] != 1:
        refuse("storage_codec")
    _closed(value["scalars"], _STATE_SCALARS)
    _closed(value["private_identity"], ("predecessor", "declined_digests"))
    witness = decode_run_witness(value["run_witness"])
    proposal_id, triage_id, completion_id = (
        value["proposal_value_id"],
        value["triage_value_id"],
        value["completion_id"],
    )
    completion = (completions or {}).get(completion_id)
    if completion_id is not None and completion is None:
        refuse("erased_dependency")
    state = ExecutionState(
        binding=legacy.decode_binding(
            canonical(_value(values, value["binding_value_id"], "binding"))
        ),
        **{**value["scalars"], "active_input_ids": tuple(value["scalars"]["active_input_ids"])},
        predecessor=value["private_identity"]["predecessor"],
        declined_digests=tuple(value["private_identity"]["declined_digests"]),
        invocations=_invocations(value["invocation_refs"], values),
        prior_invocations=_invocations(value["prior_invocation_refs"], values),
        proposal=None if proposal_id is None else rehydrate_proposal(values[proposal_id], values),
        triage=None
        if triage_id is None
        else legacy._decode(_value(values, triage_id, "triage"), TriageTrace),
        completion=completion,
        completion_id=completion_id,
    )
    legacy.validate_record(state)
    if witness.protocol_run_version != state.run_version or (
        state.claimed_by is not None
        and (
            witness.actual_run_id != state.claimed_by
            or witness.pass_id != state.active_pass_id
            or witness.input_ids != state.active_input_ids
        )
    ):
        refuse("run_witness")
    return state


def encode_stored_event(
    event,
    *,
    invocation_ref=None,
    proposal_value_id=None,
    triage_value_id=None,
    current_value_id=None,
):
    legacy.validate_record(event)
    value = plain(event)
    for key, ref in (
        ("invocation", None if invocation_ref is None else plain(invocation_ref)),
        ("proposal", proposal_value_id),
        ("triage", triage_value_id),
        ("current", current_value_id),
    ):
        if (value[key] is None) != (ref is None):
            refuse("storage_child_missing")
        value[key] = ref
    return canonical({"codec": 1, **value}, LIMITS["event"])


def rehydrate_event(raw, values):
    value = load_json(raw, LIMITS["event"])
    _closed(value, ("codec", *(f.name for f in fields(ExecutionEvent))))
    if type(value.pop("codec")) is not int or load_json(raw)["codec"] != 1:
        refuse("storage_codec")
    for key, kind, cls in (
        ("current", "binding", None),
        ("proposal", "proposal", None),
        ("triage", "triage", TriageTrace),
    ):
        ref = value[key]
        if ref is None:
            continue
        data = _value(values, ref, kind)
        value[key] = (
            legacy.decode_binding(canonical(data))
            if key == "current"
            else rehydrate_proposal(canonical(data), values)
            if key == "proposal"
            else legacy._decode(data, cls)
        )
    if value["invocation"] is not None:
        value["invocation"] = _invocations([value["invocation"]], values)[0]
    event = ExecutionEvent(**value)
    legacy.validate_record(event)
    return event


def value_references(kind, raw):
    """Closed child visitor. Only schema-declared value IDs are traversed."""
    value = load_json(raw, LIMITS[kind])
    refs = {}

    def add(key, child_kind):
        if key is not None:
            key = _id(key)
            if key in refs and refs[key] != child_kind:
                refuse("value_kind_conflict")
            refs[key] = child_kind

    def calls(items):
        if type(items) is not list or len(items) > 5:
            refuse("call_reference")
        for ref in items:
            _closed(ref, (f.name for f in fields(StoredInvocationRef)))
            add(ref["request_value_id"], "call_request")
            add(ref["result_value_id"], "call_result")
            add(ref["binding_value_id"], "call_binding")

    if kind == "state":
        _closed(
            value,
            (
                "codec",
                "binding_value_id",
                "run_witness",
                "scalars",
                "invocation_refs",
                "prior_invocation_refs",
                "proposal_value_id",
                "completion_id",
                "triage_value_id",
                "private_identity",
            ),
        )
        add(value["binding_value_id"], "binding")
        add(value["proposal_value_id"], "proposal")
        add(value["triage_value_id"], "triage")
        calls(value["invocation_refs"])
        calls(value["prior_invocation_refs"])
    elif kind == "proposal":
        _closed(
            value,
            (
                "codec",
                "binding_value_id",
                "operations",
                "evidence",
                "dispositions",
                "invocation_refs",
            ),
        )
        add(value["binding_value_id"], "binding")
        calls(value["invocation_refs"])
    elif kind == "event":
        if value.get("kind") == "initial":
            _closed(value, ("kind", "binding_value_id", "order"))
            add(value["binding_value_id"], "binding")
            order = value["order"]
            _closed(
                order,
                (
                    "stream_anchor_source_id",
                    "position",
                    "predecessor_progress_id",
                    "expected_predecessor_version",
                    "complete_manifest_value_id",
                ),
            )
            # The source-owner manifest is an existing private value, with a closed shape.
            add(order["complete_manifest_value_id"], "handoff")
        else:
            _closed(value, ("codec", *(f.name for f in fields(ExecutionEvent))))
            add(value["current"], "binding")
            add(value["proposal"], "proposal")
            add(value["triage"], "triage")
            if value["invocation"] is not None:
                calls([value["invocation"]])
    return refs


def _source_batch(binding):
    rows = check_inventory(binding)
    return SourceBatch(
        world_id=binding.world_id,
        run_id=binding.run.id,
        episode_id=binding.episode.id,
        source_id=binding.source.id,
        connector_type=binding.producer.connector_type,
        messages=tuple(
            SourceMessage(
                key=r.key,
                locator=r.locator,
                text=row["text"],
                source_time=r.source_time,
                speaker_kind=r.speaker_kind,
                modality=r.modality,
                speaker_item=r.sender,
                original_author=r.original_author,
            )
            for r, row in zip(binding.inventory, rows, strict=True)
        ),
        source_version=binding.source.version,
        episode_version=binding.episode.version,
        screened=True,
    )


def _selected_output(raw):
    wire = load_json(raw, LIMITS["call_result"])
    if type(wire) is not dict:
        refuse("wire_response")
    if "output" in wire and wire.get("status") == "completed":
        if type(wire["output"]) is not list:
            refuse("wire_response")
        texts = []
        for block in wire["output"]:
            if type(block) is not dict or block.get("type") not in {"message", "reasoning"}:
                refuse("wire_response")
            if block["type"] == "reasoning":
                continue
            if type(block.get("content")) is not list:
                refuse("wire_response")
            for part in block["content"]:
                if type(part) is not dict or part.get("type") != "output_text":
                    refuse("wire_response")
                texts.append(part.get("text"))
        if len(texts) == 1 and type(texts[0]) is str:
            return texts[0]
    choices = wire.get("choices")
    if (
        type(choices) is list
        and len(choices) == 1
        and type(choices[0]) is dict
        and choices[0].get("finish_reason") == "stop"
    ):
        message = choices[0].get("message")
        text = message.get("content") if type(message) is dict else None
        if type(text) is str and not message.get("refusal") and not message.get("tool_calls"):
            return text
    refuse("wire_response")


def check_transport_binding(binding, invocation, request, result, attempt, *, payload=None):
    """Validate bytes and deterministic translation. This is never a metering attestation."""
    from grove.extraction import pipeline
    from grove.extraction.execution_types import (
        ClassOutcome,
        Disposition,
        EvidenceRef,
        OperationRef,
    )
    from grove.extraction.schema import TRIAGE_SCHEMA, UNSUPPORTED_BRANCHES, extraction_schema
    from grove.models.layer import check_schema

    if (
        type(request) is not StoredCallRequest
        or type(result) is not StoredCallResult
        or type(attempt) is not RecordedAttemptWitness
    ):
        refuse("call_evidence_missing")
    if (
        type(request.wire_format_version) is not int
        or request.wire_format_version != 1
        or result.normalizer_revision != binding.source_revision
    ):
        refuse("call_evidence_missing")
    canonical(plain(request), LIMITS["call_request"])
    canonical(plain(result), LIMITS["call_result"])
    for text in (
        request.normalized_request,
        request.provider_request_body,
        result.provider_response_text,
        result.selected_output_text,
        result.normalized_output,
    ):
        if type(text) is not str or len(text.encode("utf-8")) > 262_144:
            refuse("call_value_bounds")
    if (
        invocation.request != request.normalized_request
        or invocation.output != result.normalized_output
        or _selected_output(result.provider_response_text) != result.selected_output_text
    ):
        refuse("call_bytes_mismatch")
    normalized = load_json(request.normalized_request)
    _closed(normalized, ("binding_digest", "stage", "payload"))
    body = load_json(request.provider_request_body)
    if type(body) is not dict:
        refuse("wire_request")
    if invocation.stage == "triage":
        messages = body.get("messages")
        if (
            type(messages) is not list
            or not messages
            or type(messages[-1]) is not dict
            or messages[-1].get("role") != "user"
        ):
            refuse("wire_request")
        packet = load_json(messages[-1]["content"])
    else:
        packet = load_json(body.get("input"))
    if (
        packet != normalized["payload"]
        or normalized["binding_digest"] != legacy.binding_digest(binding)
        or normalized["stage"] != invocation.stage
    ):
        refuse("wire_request")
    document = load_json(result.selected_output_text)
    source = _source_batch(binding)
    if type(packet) is not dict:
        refuse("wire_request")
    if invocation.stage in {"triage", "extraction"}:
        supplied = packet.get("source_data")
        actual_messages = [plain(message) for message in source.messages]
        if type(supplied) is not list or not supplied:
            refuse("source_binding")
        if invocation.stage == "triage":
            if supplied != actual_messages:
                refuse("source_binding")
        elif any(message not in actual_messages for message in supplied) or len(
            {message["locator"] for message in supplied}
        ) != len(supplied):
            refuse("source_binding")
    if invocation.stage == "triage":
        if not check_schema(TRIAGE_SCHEMA).is_valid(document):
            refuse("triage_schema")
        pipeline._check_triage(document, source)
        if document["redactions"]:
            refuse("redaction_required")
        expected = {
            "reviewed_locators": document["reviewed_locators"],
            "candidates": [
                {"locator": c["locator"], "statement_class": c["statement_class"]}
                for c in document["candidates"]
            ],
        }
    elif invocation.stage == "extraction":
        registry_data = packet.get("registry_data")
        if type(registry_data) is not dict:
            refuse("registry_payload_missing")
        try:
            _closed(
                registry_data, ("kinds", "meanings", "properties", "versions", "scheduled_fields")
            )
            registry = RegistrySnapshot(
                **{
                    **registry_data,
                    "kinds": {key: tuple(value) for key, value in registry_data["kinds"].items()},
                    "meanings": tuple(registry_data["meanings"]),
                    "properties": {
                        key: tuple(value) for key, value in registry_data["properties"].items()
                    },
                }
            )
            validate_registry(registry)
        except (TypeError, KeyError):
            refuse("registry_payload_missing")
        if not check_schema(extraction_schema(registry)).is_valid(document):
            refuse("extraction_schema")
        if any(document[k] for k in (*UNSUPPORTED_BRANCHES, "declines")):
            refuse("unsupported")
        if document["episode"] != {
            "episode_id": binding.episode.id,
            "source_id": binding.source.id,
        }:
            refuse("source_binding")
        allowed = {r.locator for r in binding.inventory if r.key in binding.selected_keys}
        observations, facts = pipeline._observations(document, source, allowed)
        operations = pipeline._operations(document, facts, registry, invocation.model_call_id)
        if payload is not None and load_json(payload.batch) != {
            "ops": operations,
            "evidence": observations,
        }:
            refuse("payload_mismatch")
        refs = tuple(
            OperationRef(
                op["op_id"],
                op["op"],
                legacy.data_digest(
                    "grove.learner.operation.v1",
                    {
                        "operation": op,
                        "evidence": [
                            ev for ev in observations if ev["ev_id"] in pipeline.cited_ev_ids(op)
                        ],
                    },
                ),
                tuple(pipeline.cited_ev_ids(op)),
            )
            for op in operations
        )
        evrefs = tuple(
            EvidenceRef(ev["ev_id"], ev["locator"], ev["statement_class"]) for ev in observations
        )
        dispositions = []
        for r in binding.inventory:
            classes = []
            for cls in dict.fromkeys(
                ev["statement_class"] for ev in observations if ev["locator"] == r.locator
            ):
                ids = tuple(
                    ev["ev_id"]
                    for ev in observations
                    if ev["locator"] == r.locator and ev["statement_class"] == cls
                )
                classes.append(
                    ClassOutcome(
                        cls,
                        cls,
                        "extracted",
                        tuple(op.id for op in refs if set(op.evidence_ids) & set(ids)),
                        ids,
                        None,
                    )
                )
            state = (
                "extracted"
                if classes
                else "ignored_blank"
                if r.key not in binding.selected_keys
                else "reviewed_empty"
            )
            dispositions.append(Disposition(r.key, r.locator, state, None, tuple(classes)))
        expected = {
            "operations": plain(refs),
            "evidence": plain(evrefs),
            "dispositions": plain(tuple(dispositions)),
        }
    else:
        # Adjudication is a closed, explicit classification result, never a dropped branch.
        _closed(document, ("classifications",))
        expected = document
    if load_json(result.normalized_output) != expected:
        refuse("normalization_mismatch")
    digests = {
        name: legacy.data_digest("grove.learner." + name.replace("_", "-") + ".v1", text)
        for name, text in zip(
            DIGEST_NAMES,
            (
                request.provider_request_body,
                result.provider_response_text,
                result.selected_output_text,
                request.normalized_request,
                result.normalized_output,
            ),
            strict=True,
        )
    }
    if (
        attempt.call_id,
        attempt.model_call_id,
        attempt.actual_run_id,
        attempt.claim_epoch,
        attempt.stage,
        attempt.route,
        attempt.model,
        attempt.job,
        attempt.effort,
        attempt.outcome,
    ) != (
        invocation.id,
        invocation.model_call_id,
        invocation.run_id,
        invocation.claim_epoch,
        invocation.stage,
        invocation.route,
        invocation.model,
        invocation.job,
        invocation.effort,
        "ok",
    ):
        refuse("call_evidence_missing")
    if any(getattr(attempt, name + "_digest") != digest for name, digest in digests.items()):
        refuse("call_evidence_missing")
    from grove.extraction.dispositions import check_invocation

    check_invocation(binding, invocation)
    return digests
