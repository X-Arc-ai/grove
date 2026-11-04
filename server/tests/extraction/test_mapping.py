"""Regression controls for all advertised writer operation mappings."""

import pytest

from grove.extraction import ExtractionRefused, extract_batch
from grove.writer.ops import check_shape
from tests.extraction.test_helpers import ReadSource, resolve
from tests.extraction.test_pipeline import (
    FakeLayer,
    document,
    evidence,
    message,
    operation,
    registry,
    source,
    triage,
)


@pytest.mark.parametrize(
    "name,target", [("end_item", "task-existing"), ("end_link", "link-existing")]
)
def test_end_operations_retain_exact_stop_and_current_writer_shape(name, target):
    wire = {
        "op_id": "end-1",
        "op": name,
        "target_ref": target,
        "end_class": "ended_in_the_world",
        "stopped_being_true": {"earliest": "msg", "latest": "msg", "precision": "instant"},
        "evidence_refs": operation()["evidence_refs"],
        "reason": "The source states the end.",
        "relation_to_standing": "refines",
    }
    known = registry(versions={"task-existing": 2, "link-existing": 3})
    plan = extract_batch(FakeLayer(documents=[document(operations=[wire])]), source(), known)
    assert plan.operations[0]["stopped_being_true"]["latest"] == "2026-10-01T09:00:00+00:00"
    assert check_shape(plan.to_batch()) == []


def test_stop_time_cannot_choose_first_of_different_message_times():
    batch = source(
        messages=(
            message(),
            message(key="m2", locator="msg:m2", source_time="2026-10-02T10:00:00+01:00"),
        )
    )
    wire = {
        "op_id": "end-1",
        "op": "end_item",
        "target_ref": "task-existing",
        "end_class": "ended_in_the_world",
        "stopped_being_true": {"earliest": "msg", "latest": "msg", "precision": "instant"},
        "evidence_refs": [
            {"ev_id": "e1", "polarity": "supports"},
            {"ev_id": "e2", "polarity": "supports"},
        ],
        "reason": "Two statements need distinct clocks.",
        "relation_to_standing": "refines",
    }
    data = document(
        evidence=[evidence(), evidence(ev_id="e2", locator="msg:m2")], operations=[wire]
    )
    with pytest.raises(ExtractionRefused, match="ambiguous_operation_clock"):
        extract_batch(FakeLayer(documents=[data], triages=[triage(batch)]), batch, registry())


def test_link_refute_and_supersede_map_current_writer_fields():
    common = {
        "evidence_refs": operation()["evidence_refs"],
        "reason": "The source states the change.",
        "relation_to_standing": "refines",
    }
    wire = [
        {
            **common,
            "op_id": "l1",
            "op": "create_link",
            "from_ref": "task-existing",
            "to_ref": "task-other",
            "meaning": "related",
            "temp_id": "$relation",
        },
        {**common, "op_id": "r1", "op": "refute", "target_ref": "task-existing"},
        {
            **common,
            "op_id": "s1",
            "op": "supersede",
            "target_ref": "task-existing",
            "successor_ref": "task-other",
        },
    ]
    known = registry(versions={"task-existing": 2, "task-other": 1})
    plan = extract_batch(FakeLayer(documents=[document(operations=wire)]), source(), known)
    assert plan.operations[0]["from"] == "task-existing"
    assert plan.operations[0]["to"] == "task-other"
    assert plan.operations[1]["target"] == "task-existing"
    assert plan.operations[2]["successor"] == "task-other"
    assert check_shape(plan.to_batch()) == []


@pytest.mark.parametrize(
    "text",
    [
        '{"schema":"grove.extract.v1","schema":"grove.extract.v1"}',
        '{"untrusted":NaN}',
        '{"untrusted":1e999}',
    ],
)
def test_noncanonical_json_never_becomes_a_writer_proposal(text):
    with pytest.raises(ExtractionRefused, match="model_output_schema"):
        extract_batch(FakeLayer(documents=[text]), source(), registry())


def test_missing_context_row_is_not_proof_of_a_single_survivor():
    aliases = [{"id": item, "phrase": "Cedar", "status": "canonical"} for item in ("i1", "i2")]
    context = [{"id": "i1", "shared": 0, "start": None, "stop": None}]
    with pytest.raises(ExtractionRefused, match="resolution_snapshot_changed"):
        resolve(ReadSource(aliases=aliases, context=context), "Cedar")
