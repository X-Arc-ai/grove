"""Pure consistency checks over untrusted snapshots, never current authority checks."""

from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import Refused
from grove.writer.message_binding import retained_messages

from .execution_codec import (
    canonical,
    encode_binding,
    load_json,
    plain,
    refuse,
    text_digest,
)
from .execution_types import ExecutionBinding

CATEGORIES = ("registry", "target", "alias", "neighbor", "evidence", "history")
WRITER_OPS = (
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
PROVIDERS = ("chatgpt", "claude_ai", "claude_code", "codex")
MODEL_STEPS = (
    ("triage", "modal", "muse-glimmer-30b", "triage", "low", 2),
    ("extraction", "meta", "muse-spark-1.3", "extraction", "minimal", 5),
    ("adjudication", "meta", "muse-spark-1.3", "extraction", "low", 2),
)


def _time(value):
    if type(value) is not str or not 1 <= len(value) <= 64:
        refuse("exact_source_time_required")
    try:
        return parse_instant(value)
    except (ClockError, ValueError, TypeError):
        refuse("exact_source_time_required")


def check_model_profile(binding):
    if len(binding.model_steps) != len(MODEL_STEPS):
        refuse("model_profile")
    for actual, expected in zip(binding.model_steps, MODEL_STEPS, strict=True):
        if (
            actual.stage,
            actual.route,
            actual.model,
            actual.job,
            actual.effort,
            actual.max_calls,
        ) != expected:
            refuse("model_profile")
        if not (1 <= actual.request_limit <= 1_048_576 and 1 <= actual.output_limit <= 524_288):
            refuse("model_bounds")


def check_writer_profile(binding):
    if binding.writer_ops != WRITER_OPS:
        refuse("writer_profile")


def check_inventory(binding):
    """Inspect the complete retained source before using any selected range."""
    source = binding.source
    # The smaller decoder limits depth/bytes before the shared guard parses again.
    content = load_json(source.content, 262_144)
    speakers = load_json(source.speakers, 262_144)
    try:
        rows = retained_messages(
            {"content": source.content, "parser_version": binding.producer.parser},
            {"provider": binding.producer.provider},
        )
    except Refused as exc:
        refuse("typed_source_unadmitted" if "typed" in exc.rule else "source_content_invalid")
    if rows is None or type(content) is not list or not 1 <= len(rows) <= 128:
        refuse("inventory_bounds")
    if type(speakers) is not dict or len(speakers) != len(rows):
        refuse("complete_speakers_required")
    if (
        text_digest(source.content) != source.content_digest
        or text_digest(source.speakers) != source.metadata_digest
    ):
        refuse("source_digest")
    if len(binding.inventory) != len(rows):
        refuse("complete_inventory_required")
    if set(speakers) != {row["key"] for row in rows}:
        refuse("complete_speakers_required")
    total, previous, selected = 0, None, []
    for row, ref in zip(rows, binding.inventory, strict=True):
        text = row["text"]
        if len(text) > 16_384:
            refuse("message_text_bounds")
        total += len(text.encode("utf-8"))
        if total > 65_536:
            refuse("batch_text_bounds")
        if not 4 <= len(ref.key) <= 1020 or row["key"] != ref.key:
            refuse("message_key")
        if ref.locator != "key:" + ref.key:
            refuse("message_locator")
        meta = speakers.get(ref.key)
        if type(meta) is not dict or set(meta) != {"kind", "locator", "sender", "original_author"}:
            refuse("speaker_metadata")
        if (
            ref.speaker_kind not in ENUMS["speaker_kind"]
            or ref.speaker_kind != meta["kind"]
            or ref.locator != meta["locator"]
            or ref.sender != meta["sender"]
            or ref.original_author != meta["original_author"]
            or ref.modality != "text"
            or row.get("modality", "text") != "text"
            or (row.get("speaker_kind") is not None and row["speaker_kind"] != ref.speaker_kind)
        ):
            refuse("exact_speaker_required")
        if ref.speaker_kind == "forwarded" and ref.original_author is None:
            refuse("original_author_required")
        instant = _time(row["at"])
        if (
            row["at"] != ref.source_time
            or instant > _time(source.received_at)
            or (previous is not None and instant < previous)
        ):
            refuse("exact_source_time_required")
        previous = instant
        if text_digest(text) != ref.text_digest:
            refuse("message_text_digest")
        if ref.eligibility != "eligible" or ref.reason is not None:
            refuse("held_input")
        if text.strip():
            selected.append(ref.key)
    if binding.selected_keys != tuple(selected):
        refuse("complete_selection_required")
    episode = binding.episode
    if (
        episode.first_key != rows[0]["key"]
        or episode.last_key != rows[-1]["key"]
        or _time(episode.start) != _time(rows[0]["at"])
        or _time(episode.end) != _time(rows[-1]["at"])
    ):
        refuse("complete_episode_required")
    return tuple(rows)


def _context(binding):
    ctx = binding.context
    if tuple(m.category for m in ctx.memberships) != CATEGORIES:
        refuse("context_categories")
    seen = set()
    for ref in ctx.dependencies:
        key = ref.category, ref.id
        if ref.category not in CATEGORIES or key in seen or ref.version < 1:
            refuse("context_dependencies")
        seen.add(key)
    for membership in ctx.memberships:
        if (
            len(membership.ids) > 256
            or len(set(membership.ids)) != len(membership.ids)
            or membership.digest != text_digest(canonical(list(membership.ids)).decode())
        ):
            refuse("context_membership")
        if any((membership.category, key) not in seen for key in membership.ids):
            refuse("context_versions_required")
        if {key for category, key in seen if category == membership.category} != set(
            membership.ids
        ):
            refuse("context_inventory_required")


def check_legacy_profile(binding: ExecutionBinding):
    encode_binding(binding)
    if (
        binding.profile != "grove.learner.legacy.v1"
        or binding.codec != 1
        or binding.translator != "grove.extract.v1"
    ):
        refuse("execution_profile")
    p, source, episode, work, run = (
        binding.producer,
        binding.source,
        binding.episode,
        binding.work,
        binding.run,
    )
    if (
        p.connector_type != "past_ai_conversations"
        or p.provider not in PROVIDERS
        or p.parser != p.provider + ".v1"
        or p.batch_rule != "session_delta.v1"
        or episode.cut_rule != "session_delta.v1"
    ):
        refuse("producer_profile")
    if (
        source.episode_ids != (episode.id,)
        or episode.source_ids != (source.id,)
        or episode.connector_id != p.id
        or source.connector_id != p.id
        or work.source_id != source.id
    ):
        refuse("single_complete_episode_required")
    if binding.principal.world_kind != "ordinary" or source.forget_state != "retained":
        refuse("source_unavailable")
    if (
        run.agent != "learner"
        or len(run.id) > 80
        or not run.id.isascii()
        or work.request_id not in run.input_ids
        or len(set(run.input_ids)) != len(run.input_ids)
    ):
        refuse("run_binding")
    if any(
        v < 1
        for v in (
            p.version,
            source.version,
            episode.version,
            run.version,
            work.version,
            work.request_version,
            run.token_cap,
        )
    ):
        refuse("version_required")
    if (
        work.transport_attempts > 5
        or work.regrounds > 1
        or work.writer_repairs > 1
        or work.lease_deadline > run.deadline
    ):
        refuse("protocol_bounds")
    if tuple(d.category for d in binding.prompt_digests) != (
        "triage",
        "extraction",
        "adjudication",
    ):
        refuse("prompt_profile")
    check_inventory(binding)
    _context(binding)
    check_model_profile(binding)
    check_writer_profile(binding)


def check_dependencies(bound, current, *, replay=False):
    """Compare supplied snapshots; a caller must separately establish their authority."""
    check_legacy_profile(bound)
    check_legacy_profile(current)
    before, after = plain(bound), plain(current)
    for value in (before, after):
        value.pop("run")
        work = value.pop("work")
        value["work_identity"] = [work[k] for k in ("id", "request_id", "content_key", "source_id")]
        value["context"].pop("read_generation")
        value["context"].pop("capture_id")
        if replay:
            # Committed operations necessarily changed their old target versions.
            value["context"] = None
    if before != after:
        refuse("dependencies_changed")
