"""Pure backfill planning only. No jobs, background threads, or completion writes."""

import math
from dataclasses import dataclass

from grove.extraction.contracts import ExtractionRefused, bounded_text


@dataclass(frozen=True)
class BackfillEstimate:
    episodes: int
    extraction_slots: int
    seconds_per_pass: float
    tokens_per_pass: float
    reserved_passes: int = 2


def estimate_backfill(
    counts: dict[str, int], *, median_seconds: float, median_tokens: float, extraction_slots: int
) -> BackfillEstimate:
    if not isinstance(counts, dict) or len(counts) > 256:
        raise ExtractionRefused("backfill_counts")
    for source, count in counts.items():
        bounded_text(source)
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise ExtractionRefused("backfill_counts")
    for value in (median_seconds, median_tokens):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ExtractionRefused("backfill_measurement")
    if type(extraction_slots) is not int or not 1 <= extraction_slots <= 32:
        raise ExtractionRefused("backfill_slots")
    count = sum(counts.values())
    return BackfillEstimate(
        count, extraction_slots, count * median_seconds / extraction_slots, count * median_tokens
    )


def extraction_window(
    episode_ids: tuple[str, ...], *, committed_count: int, lookahead: int
) -> tuple[str, ...]:
    """One source's bounded lookahead. A pending earlier batch always keeps its position."""
    if not isinstance(episode_ids, tuple) or len(episode_ids) > 100_000:
        raise ExtractionRefused("backfill_episodes")
    for episode_id in episode_ids:
        bounded_text(episode_id)
    if len(set(episode_ids)) != len(episode_ids):
        raise ExtractionRefused("backfill_duplicate_episode")
    if type(committed_count) is not int or not 0 <= committed_count <= len(episode_ids):
        raise ExtractionRefused("backfill_watermark")
    if type(lookahead) is not int or not 1 <= lookahead <= 32:
        raise ExtractionRefused("backfill_lookahead")
    return episode_ids[committed_count : committed_count + lookahead]
