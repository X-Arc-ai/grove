import json
from dataclasses import replace

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.execution_codec import decode_binding
from grove.extraction.execution_profile import check_dependencies, check_legacy_profile
from grove.extraction.execution_types import VersionRef
from tests.extraction.test_execution_codec import binding, digest, document, packed


def test_only_exact_four_legacy_producer_tuples_match():
    for provider in ("chatgpt", "claude_ai", "claude_code", "codex"):
        data = document()
        data["producer"].update(provider=provider, parser=provider + ".v1")
        check_legacy_profile(decode_binding(packed(data)))
    value = binding()
    for field, changed in (
        ("provider", "unknown"),
        ("parser", "chatgpt.v2"),
        ("batch_rule", "session_delta.v2"),
        ("connector_type", "ai_host"),
    ):
        with pytest.raises(ExtractionRefused):
            check_legacy_profile(
                replace(value, producer=replace(value.producer, **{field: changed}))
            )
    with pytest.raises(ExtractionRefused):
        check_legacy_profile(replace(value, episode=replace(value.episode, cut_rule="one_request")))


def test_typed_marker_anywhere_blocks_before_actions():
    data = document()
    rows = json.loads(data["source"]["content"])
    rows.append(
        {"key": "outside", "text": "outside", "at": rows[0]["at"], "extra": {"source_facts": {}}}
    )
    data["source"]["content"] = packed(rows)
    data["source"]["content_digest"] = digest(packed(rows))
    with pytest.raises(ExtractionRefused, match="typed_source"):
        check_legacy_profile(decode_binding(packed(data)))


def test_multipart_or_oversized_episode_is_not_a_successful_prefix():
    value = binding()
    invalid = [
        replace(value, episode=replace(value.episode, source_ids=("source-1", "source-2"))),
        replace(value, source=replace(value.source, episode_ids=("episode-1", "episode-2"))),
        replace(value, selected_keys=()),
    ]
    for item in invalid:
        with pytest.raises(ExtractionRefused):
            check_legacy_profile(item)
    data = document("x" * 16385)
    with pytest.raises(ExtractionRefused):
        check_legacy_profile(decode_binding(packed(data)))


def test_exact_source_time_speaker_and_locator_are_required():
    value = binding()
    for field, wrong in (
        ("source_time", "2026-11-01T10:00:00+00:00"),
        ("source_time", "2026-11-01T08:00:00+00:00"),
        ("speaker_kind", "ai_host"),
        ("sender", "display-name"),
        ("locator", "key:other"),
        ("original_author", "someone"),
        ("modality", "image"),
    ):
        with pytest.raises(ExtractionRefused):
            check_legacy_profile(
                replace(value, inventory=(replace(value.inventory[0], **{field: wrong}),))
            )


def test_context_membership_changes_require_reground():
    value = binding()
    memberships = list(value.context.memberships)
    memberships[2] = replace(memberships[2], ids=("alias-new",), digest=digest('["alias-new"]'))
    current = replace(
        value,
        context=replace(
            value.context,
            memberships=tuple(memberships),
            dependencies=(VersionRef("alias", "alias-new", 1),),
        ),
    )
    with pytest.raises(ExtractionRefused, match="dependencies_changed"):
        check_dependencies(value, current)


def test_meter_generation_change_alone_is_not_source_staleness():
    value = binding()
    check_dependencies(value, replace(value, context=replace(value.context, read_generation=90)))


def test_wrong_model_job_or_writer_operation_refuses():
    value = binding()
    for changed in (
        replace(value, writer_ops=value.writer_ops + ("forget",)),
        replace(
            value,
            model_steps=(
                replace(value.model_steps[0], job="triage_fallback"),
                *value.model_steps[1:],
            ),
        ),
    ):
        with pytest.raises(ExtractionRefused):
            check_legacy_profile(changed)


def test_all_inventory_limits_and_source_changes_refuse():
    value = binding()
    for wrong in (
        replace(value, run=replace(value.run, id="r" * 81)),
        replace(value, inventory=value.inventory * 129),
        replace(value, episode=replace(value.episode, first_key="missing")),
        replace(value, source=replace(value.source, content_digest=digest("wrong"))),
        replace(value, inventory=(replace(value.inventory[0], eligibility="held"),)),
        replace(value, work=replace(value.work, writer_repairs=2)),
    ):
        with pytest.raises(ExtractionRefused):
            check_legacy_profile(wrong)
    data = document()
    row = json.loads(data["source"]["content"])[0]
    row["at"] = None
    data["source"]["content"] = packed([row])
    data["source"]["content_digest"] = digest(data["source"]["content"])
    with pytest.raises(ExtractionRefused):
        check_legacy_profile(decode_binding(packed(data)))


def test_complete_message_and_utf8_aggregate_limits_refuse():
    data = document()
    row = json.loads(data["source"]["content"])[0]
    ref = data["inventory"][0]
    rows, refs, speakers = [], [], {}
    for i in range(5):
        key = f"message-{i}"
        rows.append({**row, "key": key, "text": "é" * 8000})
        refs.append({**ref, "key": key, "locator": "key:" + key, "text_digest": digest("é" * 8000)})
        speakers[key] = {
            "kind": "person",
            "sender": "person-1",
            "locator": "key:" + key,
            "original_author": None,
        }
    data["source"].update(
        content=packed(rows),
        content_digest=digest(packed(rows)),
        speakers=packed(speakers),
        metadata_digest=digest(packed(speakers)),
    )
    data["inventory"], data["selected_keys"] = refs, [r["key"] for r in rows]
    data["episode"].update(first_key=rows[0]["key"], last_key=rows[-1]["key"])
    with pytest.raises(ExtractionRefused, match="batch_text_bounds"):
        check_legacy_profile(decode_binding(packed(data)))
