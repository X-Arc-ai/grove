"""Synthetic protocol documents; no database, provider, or admission fixtures."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.execution_codec import (
    binding_digest,
    decode_binding,
    decode_proposal,
    encode_binding,
    proposal_digest,
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document(text="I chose Cedar."):
    rows = [
        {
            "key": "msg1",
            "text": text,
            "at": "2026-11-01T09:00:00+01:00",
            "speaker_kind": "person",
            "modality": "text",
        }
    ]
    speakers = {
        "msg1": {
            "kind": "person",
            "locator": "key:msg1",
            "sender": "person-1",
            "original_author": None,
        }
    }
    content, metadata = packed(rows), packed(speakers)
    categories = ("registry", "target", "alias", "neighbor", "evidence", "history")
    return {
        "profile": "grove.learner.legacy.v1",
        "codec": 1,
        "source_revision": "a" * 40,
        "schema_digest": digest("schema"),
        "prompt_digests": [
            {"category": s, "digest": digest(s)} for s in ("triage", "extraction", "adjudication")
        ],
        "translator": "grove.extract.v1",
        "world_id": "world-1",
        "principal": {
            "principal_id": "principal-1",
            "grant_id": "grant-1",
            "policy_revision": 1,
            "privacy_revision": 1,
            "origin_id": "upload-1",
            "admission_id": "admission-1",
            "world_kind": "ordinary",
        },
        "run": {
            "id": "run-1",
            "version": 1,
            "agent": "learner",
            "input_ids": ["request-1"],
            "outcome": "running",
            "deadline": 1000,
            "token_cap": 10000,
            "pass_id": "pass-1",
        },
        "work": {
            "id": "work-1",
            "version": 1,
            "request_id": "request-1",
            "request_version": 1,
            "content_key": "content-1",
            "source_id": "source-1",
            "claim_epoch": 0,
            "claimed_by": None,
            "lease_deadline": 900,
            "transport_attempts": 0,
            "regrounds": 0,
            "writer_repairs": 0,
        },
        "producer": {
            "id": "connector-1",
            "version": 1,
            "connector_type": "past_ai_conversations",
            "provider": "chatgpt",
            "parser": "chatgpt.v1",
            "batch_rule": "session_delta.v1",
        },
        "source": {
            "id": "source-1",
            "version": 1,
            "connector_id": "connector-1",
            "content": content,
            "speakers": metadata,
            "content_digest": digest(content),
            "metadata_digest": digest(metadata),
            "screening_id": "screen-1",
            "received_at": "2026-11-01T10:00:00+00:00",
            "episode_ids": ["episode-1"],
            "forget_state": "retained",
        },
        "episode": {
            "id": "episode-1",
            "version": 1,
            "connector_id": "connector-1",
            "source_ids": ["source-1"],
            "first_key": "msg1",
            "last_key": "msg1",
            "cut_rule": "session_delta.v1",
            "start": rows[0]["at"],
            "end": rows[0]["at"],
        },
        "inventory": [
            {
                "key": "msg1",
                "locator": "key:msg1",
                "text_digest": digest(text),
                "source_time": rows[0]["at"],
                "speaker_kind": "person",
                "modality": "text",
                "sender": "person-1",
                "original_author": None,
                "eligibility": "eligible",
                "reason": None,
            }
        ],
        "selected_keys": ["msg1"] if text.strip() else [],
        "context": {
            "capture_id": "capture-1",
            "read_generation": 3,
            "history_sequence": 2,
            "dependencies": [],
            "memberships": [{"category": c, "ids": [], "digest": digest("[]")} for c in categories],
        },
        "model_steps": [
            {
                "stage": "triage",
                "route": "modal",
                "model": "muse-glimmer-30b",
                "job": "triage",
                "effort": "low",
                "max_calls": 2,
                "request_limit": 1048576,
                "output_limit": 524288,
            },
            {
                "stage": "extraction",
                "route": "meta",
                "model": "muse-spark-1.3",
                "job": "extraction",
                "effort": "minimal",
                "max_calls": 5,
                "request_limit": 1048576,
                "output_limit": 524288,
            },
            {
                "stage": "adjudication",
                "route": "meta",
                "model": "muse-spark-1.3",
                "job": "extraction",
                "effort": "low",
                "max_calls": 2,
                "request_limit": 1048576,
                "output_limit": 524288,
            },
        ],
        "writer_ops": [
            "create_item",
            "update_item",
            "change_status",
            "end_item",
            "create_link",
            "end_link",
            "attach_evidence",
            "refute",
            "supersede",
        ],
    }


def binding(**changes):
    data = document()
    data.update(changes)
    return decode_binding(packed(data))


def test_binding_rejects_duplicate_unknown_and_missing_fields():
    data = document()
    with pytest.raises(ExtractionRefused):
        decode_binding('{"codec":1,' + packed(data)[1:])
    for changed in ({**data, "admitted": True}, {k: v for k, v in data.items() if k != "world_id"}):
        with pytest.raises(ExtractionRefused):
            decode_binding(packed(changed))
    data["source"]["source_facts"] = {}
    with pytest.raises(ExtractionRefused):
        decode_binding(packed(data))


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.0, None, "1"])
def test_binding_rejects_bool_counters_overflow_and_aggregate_limits(value):
    data = document()
    data["work"]["claim_epoch"] = value
    with pytest.raises(ExtractionRefused):
        decode_binding(packed(data))


@pytest.mark.parametrize(
    "raw", [" " * 1048577, b"\xff", "[" * 17 + "]" * 17, '{"x":NaN}', '{"x":123456789012345678901}']
)
def test_codec_rejects_size_depth_unicode_and_numeric_inputs(raw):
    with pytest.raises(ExtractionRefused):
        decode_binding(raw)


def test_nested_input_mutation_cannot_change_binding():
    data = document()
    result = decode_binding(packed(data))
    before = binding_digest(result)
    data["inventory"][0]["key"] = "changed"
    data["writer_ops"].append("forget")
    assert binding_digest(result) == before
    assert isinstance(result.inventory, tuple)
    with pytest.raises(FrozenInstanceError):
        result.inventory[0].key = "changed"
    assert decode_binding(encode_binding(result)) == result


def test_identity_fields_do_not_collide_at_separators():
    left, right = document(), document()
    left["world_id"], left["work"]["id"] = "a:b", "c"
    right["world_id"], right["work"]["id"] = "a", "b:c"
    assert binding_digest(decode_binding(packed(left))) != binding_digest(
        decode_binding(packed(right))
    )
    left["world_id"] = "a\x1fb"
    with pytest.raises(ExtractionRefused):
        decode_binding(packed(left))


def test_direct_mutable_or_oversized_records_cannot_be_encoded():
    value = binding()
    for invalid in (
        replace(value, inventory=list(value.inventory)),
        replace(value, world_id="a" * 1025),
    ):
        with pytest.raises(ExtractionRefused):
            encode_binding(invalid)


def test_proposal_is_closed_canonical_and_domain_separated():
    proposal = {
        "binding_digest": binding_digest(binding()),
        "operations": [],
        "evidence": [],
        "dispositions": [],
        "invocations": [],
    }
    decoded = decode_proposal(packed(proposal))
    assert isinstance(decoded.operations, tuple)
    assert proposal_digest(decoded) != binding_digest(binding())
    for invalid in ({**proposal, "questions": []}, {**proposal, "operations": [{}]}):
        with pytest.raises(ExtractionRefused):
            decode_proposal(packed(invalid))
    bad = deepcopy(proposal)
    bad["binding_digest"] = "x" * 64
    with pytest.raises(ExtractionRefused):
        decode_proposal(packed(bad))


def test_nested_collection_and_metadata_limits_precede_hashing(monkeypatch):
    data = document()
    data["context"]["dependencies"] = [{"category": "target", "id": "target", "version": 1}] * 2049
    with pytest.raises(ExtractionRefused):
        decode_binding(packed(data))
    value = binding()
    oversized = replace(value, writer_ops=("a" * 1024,) * 2048)
    with monkeypatch.context() as patch:
        patch.setattr(hashlib, "sha256", lambda *_: pytest.fail("hash before bounds"))
        with pytest.raises(ExtractionRefused):
            binding_digest(oversized)
    data = document()
    data["world_id"] = "\\ud800"
    with pytest.raises(ExtractionRefused):
        decode_binding(packed(data))
