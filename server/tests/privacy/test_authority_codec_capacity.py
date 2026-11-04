"""Independent resource boundaries; sample measurements are not worst-case proof."""

import json
import sys
import tracemalloc
from dataclasses import replace

import pytest

from grove.authority.capacity import (
    COMPLETE_RESERVE,
    HARD,
    SOFT,
    JournalUsage,
    assess_ordinary_append,
    saturation_reasons,
)
from grove.authority.codec import (
    MAX_ALLOCATION,
    PARTITIONS,
    DecoderBudget,
    _Tape,
    decode_event,
    decode_manifest,
    record_metrics,
    scan_journal,
)
from grove.authority.records import AuthorityCodecRefused
from grove.authority.transitions import assess_append
from tests.privacy.test_authority_codec import H, Journal, canonical, effect, resign, uid


def count_nodes(value):
    if type(value) is dict:
        return 1 + sum(1 + count_nodes(child) for child in value.values())
    if type(value) is list:
        return 1 + sum(map(count_nodes, value))
    return 1


def test_strict_maximum_seal_matches_accepted_complete_arithmetic():
    fixture = Journal()
    fixture.seal()
    raw = fixture.events[-1]
    raw.update(world_id="w" * 128, sequence=32768, event_id=uid(500))
    resign(raw)
    event = decode_event(canonical(raw), budget=DecoderBudget())
    metrics = record_metrics(event)
    assert (metrics.bytes, metrics.nodes) == (488, 23)
    assert metrics.bytes + 1 == 489
    assert COMPLETE_RESERVE == JournalUsage(525312, 3, 16448)


def test_complete_wrapper_nodes_bytes_and_append_comma_are_measured():
    fixture = Journal()
    raw = fixture.raw()
    journal = scan_journal(raw, budget=DecoderBudget())
    assert journal.metrics.nodes == count_nodes(json.loads(raw))
    assert journal.metrics.bytes == len(raw)
    fixture.seal()
    after = scan_journal(fixture.raw(), budget=DecoderBudget())
    assert after.metrics.bytes - journal.metrics.bytes == len(canonical(fixture.events[-1])) + 1
    assert after.metrics.nodes - journal.metrics.nodes == count_nodes(fixture.events[-1])


@pytest.mark.parametrize("dimension", ["bytes", "events", "nodes"])
def test_every_soft_boundary_and_complete_reserve_equality(dimension):
    values = {"bytes": 0, "events": 0, "nodes": 0}
    values[dimension] = getattr(SOFT, dimension) - 1
    assert f"{dimension}_soft_limit" not in saturation_reasons(JournalUsage(**values))
    values[dimension] += 1
    assert f"{dimension}_soft_limit" in saturation_reasons(JournalUsage(**values))
    values[dimension] = getattr(HARD, dimension) - getattr(COMPLETE_RESERVE, dimension)
    assert f"{dimension}_reserve_shortfall" not in saturation_reasons(JournalUsage(**values))
    values[dimension] += 1
    assert f"{dimension}_reserve_shortfall" in saturation_reasons(JournalUsage(**values))


@pytest.mark.parametrize("dimension", ["bytes", "events", "nodes"])
def test_ordinary_projection_keeps_the_whole_reserve(dimension):
    values = {"bytes": 0, "events": 0, "nodes": 0}
    values[dimension] = getattr(HARD, dimension) - getattr(COMPLETE_RESERVE, dimension)
    assert (
        assess_ordinary_append(JournalUsage(0, 0, 0), JournalUsage(**values)) == "arithmetic_fits"
    )
    values[dimension] += 1
    assert (
        assess_ordinary_append(JournalUsage(0, 0, 0), JournalUsage(**values))
        == "projected_reserve_shortfall"
    )


@pytest.mark.parametrize("part,limit", PARTITIONS.items())
def test_partition_refuses_one_past_before_charge(part, limit):
    budget = DecoderBudget()
    prior = dict(budget.usage)[part]
    reservation = budget.reserve(part, limit - prior)
    assert dict(budget.usage)[part] == limit
    with pytest.raises(AuthorityCodecRefused, match="allocation_limit"):
        reservation.grow(1)
    assert dict(budget.usage)[part] == limit
    reservation.release()


def test_cancel_and_operation_budget_are_value_free_and_cannot_go_backwards():
    budget = DecoderBudget(max_operations=2)
    budget.tick()
    with pytest.raises(AuthorityCodecRefused, match="cancelled"):
        budget.tick()
    budget = DecoderBudget()
    with pytest.raises(AuthorityCodecRefused, match="scalar"):
        budget.tick(-1)
    budget.cancel()
    with pytest.raises(AuthorityCodecRefused, match="cancelled"):
        scan_journal(Journal().raw(), budget=budget)


@pytest.mark.parametrize(
    "value", [bytearray(b"{}"), memoryview(bytearray(b"{}")), "{}", iter([b"{}"])]
)
def test_mutable_or_convertible_input_never_enters_the_scanner(value):
    with pytest.raises(AuthorityCodecRefused, match="input_type"):
        decode_event(value, budget=DecoderBudget())


def test_readonly_slice_counts_the_whole_underlying_input():
    raw = Journal().raw()
    root = raw + b"x" * 1000
    budget = DecoderBudget()
    journal = scan_journal(memoryview(root)[: len(raw)], budget=budget)
    assert journal.metrics.bytes == len(raw)
    assert dict(budget.usage)["input"] >= len(root)
    budget.close()
    from grove.authority.codec import journal_event

    with pytest.raises(AuthorityCodecRefused, match="cancelled"):
        journal_event(journal, 0)


def test_input_byte_limit_precedes_a_copy_or_parse():
    budget = DecoderBudget()
    with pytest.raises(AuthorityCodecRefused, match="byte_limit"):
        scan_journal(b"x" * 16000001, budget=budget)
    assert dict(budget.usage)["input"] == 0
    with pytest.raises(AuthorityCodecRefused, match="byte_limit"):
        decode_manifest(b"x" * 1048577, budget=budget)


def scan_tokens(raw, **limits):
    budget = DecoderBudget()
    reservation = budget.reserve("scratch")
    tape = _Tape(memoryview(raw), budget, reservation, **limits)
    tape.scan()
    return tape


def test_node_cap_equality_and_one_past_are_independent_of_bytes():
    raw = b"[" + b",".join([b"0"] * 8191) + b"]"
    assert len(scan_tokens(raw).tokens) // 5 == 8192
    with pytest.raises(AuthorityCodecRefused, match="node_limit"):
        scan_tokens(raw[:-1] + b",0]")


def test_container_depth_caps_count_the_root_and_ignore_scalar_leaves():
    assert scan_tokens(b"[" * 12 + b"0" + b"]" * 12).depth == 12
    with pytest.raises(AuthorityCodecRefused, match="depth_limit"):
        scan_tokens(b"[" * 13 + b"0" + b"]" * 13)
    assert scan_tokens(b"[" * 6 + b"0" + b"]" * 6, max_depth=6).depth == 6
    with pytest.raises(AuthorityCodecRefused, match="depth_limit"):
        scan_tokens(b"[" * 7 + b"0" + b"]" * 7, max_depth=6)


def test_string_and_key_token_limits_precede_materialization():
    assert len(scan_tokens(b'"' + b"x" * 1024 + b'"').tokens) == 5
    with pytest.raises(AuthorityCodecRefused, match="token_limit"):
        scan_tokens(b'"' + b"x" * 1025 + b'"')
    scan_tokens(b'{"' + b"x" * 64 + b'":0}')
    with pytest.raises(AuthorityCodecRefused, match="token_limit"):
        scan_tokens(b'{"' + b"x" * 65 + b'":0}')


def test_manifest_world_count_is_checked_before_typed_head_allocation():
    fixture = Journal()
    manifest = fixture.manifest()
    empty = {
        "world_id": "w",
        "owner_id": "o",
        "origin_event_id": None,
        "origin_digest": None,
        "sequence": 0,
        "head_digest": None,
        "ledger": None,
        "authority_epoch": 0,
        "erasure_revision": 0,
        "release_state": "uninitialized",
        "pending_id": None,
    }
    manifest["worlds"] = [{**empty, "world_id": f"w{n:04}"} for n in range(2049)]
    manifest["checksum"] = H
    with pytest.raises(AuthorityCodecRefused, match="scalar"):
        decode_manifest(canonical(manifest), budget=DecoderBudget())


def test_append_assessment_counts_two_complete_envelopes():
    fixture = Journal()
    before = fixture.decode()
    fixture.prepare([effect("class_create", 20, class_id="c", label_value_id="v")])
    fixture.commit()
    records = tuple(
        decode_event(canonical(raw), budget=before.journal._budget) for raw in fixture.events[-2:]
    )
    assessment = assess_append(before, records, budget=before.journal._budget)
    assert assessment.projected.bytes == len(fixture.raw())
    assert assessment.projected.nodes == count_nodes(json.loads(fixture.raw()))
    assert assessment.decision == "arithmetic_fits_only"
    assert assessment.requirements


def test_forged_journal_metrics_cannot_be_encoded_as_validated_output():
    from grove.authority.codec import encode_journal

    budget = DecoderBudget()
    journal = scan_journal(Journal().raw(), budget=budget)
    forged = replace(journal, metrics=replace(journal.metrics, bytes=0))
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        encode_journal(forged, budget=budget)


def test_sample_peak_allocation_is_measured_separately_from_partition_arithmetic():
    fixture = Journal()
    # Scanner measurement only. Repeated seals are intentionally not a legal
    # transition history and never constitute a reducer or runtime acceptance.
    for _ in range(126):
        fixture.seal()
    raw = fixture.raw()
    tracemalloc.start()
    budget = DecoderBudget()
    journal = scan_journal(raw, budget=budget)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = peak + sys.getsizeof(raw)
    assert journal.metrics.events == 128
    assert total < MAX_ALLOCATION
    print(
        json.dumps(
            {
                "measurement": "128_event_scanner_sample_only",
                "input_bytes": len(raw),
                "traced_peak_bytes": peak,
                "peak_plus_input_bytes": total,
                "charged_partition_peaks": dict(budget.peak),
                "maximum_shape_conformance": "NOT_MEASURED",
            },
            sort_keys=True,
        )
    )


def test_failed_decode_releases_input_and_scratch_and_has_no_parser_context():
    budget = DecoderBudget()
    raw = Journal().events[0]
    raw["digest"] = H
    with pytest.raises(AuthorityCodecRefused, match="digest") as caught:
        decode_event(canonical(raw), budget=budget)
    assert caught.value.__context__ is None
    assert dict(budget.usage)["scratch"] == 0
    assert dict(budget.usage)["input"] == 0
    valid = Journal().raw()
    scan_journal(valid, budget=budget)
    before = dict(budget.usage)["input"]
    with pytest.raises(AuthorityCodecRefused):
        scan_journal(valid[:-1], budget=budget)
    assert dict(budget.usage)["input"] == before


def test_append_refuses_a_completion_for_another_intent():
    fixture = Journal()
    before = fixture.decode()
    fixture.prepare([effect("class_create", 20, class_id="c", label_value_id="v")])
    fixture.commit()
    raw = fixture.events[-1]
    raw["payload"]["completion"]["intent_id"] = uid(1000)
    resign(raw)
    records = tuple(
        decode_event(canonical(raw), budget=before.journal._budget) for raw in fixture.events[-2:]
    )
    with pytest.raises(AuthorityCodecRefused, match="identity"):
        assess_append(before, records, budget=before.journal._budget)


def test_input_charge_rolls_back_when_metadata_partition_is_full():
    budget = DecoderBudget()
    reservation = budget.reserve(
        "metadata", PARTITIONS["metadata"] - dict(budget.usage)["metadata"]
    )
    with pytest.raises(AuthorityCodecRefused, match="allocation_limit"):
        decode_event(canonical(Journal().events[0]), budget=budget)
    assert dict(budget.usage)["input"] == 0
    reservation.release()


def test_small_legal_reduction_peak_and_object_sizes_are_measured():
    from grove.authority.transitions import reduce_journal

    fixture = Journal()
    for index in range(8):
        fixture.prepare(
            [
                effect(
                    "class_create", 100 + index, class_id=f"c-{index}", label_value_id=f"v-{index}"
                )
            ]
        )
        fixture.commit()
    raw, manifest_bytes = fixture.raw(), canonical(fixture.manifest())
    tracemalloc.start()
    budget = DecoderBudget()
    journal = scan_journal(raw, budget=budget)
    manifest = decode_manifest(manifest_bytes, budget=budget)
    result = reduce_journal(journal, manifest=manifest, budget=budget)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = peak + sys.getsizeof(raw) + sys.getsizeof(manifest_bytes)
    assert result.state_digest == fixture.state_digest()
    assert total < MAX_ALLOCATION
    print(
        json.dumps(
            {
                "measurement": "18_event_legal_reduction_sample_only",
                "traced_peak_bytes": peak,
                "peak_plus_inputs_bytes": total,
                "journal_object_bytes": sys.getsizeof(journal),
                "offset_table_object_bytes": sys.getsizeof(journal._offsets),
                "reduction_object_bytes": sys.getsizeof(result),
                "charged_partition_peaks": dict(budget.peak),
                "maximum_shape_conformance": "NOT_MEASURED",
            },
            sort_keys=True,
        )
    )
