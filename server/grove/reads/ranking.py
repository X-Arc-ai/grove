"""D7 ranking over complete, already captured candidates. These helpers never page inputs.

Eligibility is applied before ranks are assigned. Latest is a separate ordering, with no
similarity or recency term mixed into reciprocal rank fusion.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def fuse(
    retrievers: Sequence[Iterable[str]],
    eligible: frozenset[str],
    *,
    exact_aliases: frozenset[str] = frozenset(),
    weights: Sequence[float] | None = None,
    k: int = 60,
) -> tuple[str, ...]:
    """RRF, with an exact-alias boost and stable ID tie breaks. No input score is exposed."""
    if type(k) is not int or k < 1:
        raise ValueError("the RRF constant must be a positive integer")
    if weights is None:
        weights = (1.0,) * len(retrievers)
    if len(weights) != len(retrievers) or any(
        isinstance(weight, bool)
        or not isinstance(weight, int | float)
        or not math.isfinite(weight)
        or weight <= 0
        for weight in weights
    ):
        raise ValueError("each retriever needs one positive finite weight")
    scores: dict[str, float] = {}
    for candidates, weight in zip(retrievers, weights, strict=True):
        seen: set[str] = set()
        rank = 0
        for item_id in candidates:
            if item_id not in eligible or item_id in seen:
                continue
            seen.add(item_id)
            rank += 1
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    # Identity can retrieve a candidate even when lexical analysis drops every query token.
    for item_id in exact_aliases & eligible:
        scores[item_id] = scores.get(item_id, 0.0) + 1.0
    return tuple(sorted(scores, key=lambda item_id: (-scores[item_id], item_id)))


def _time(value: object) -> datetime:
    if value is None:
        return _MIN_TIME
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("ranking requires captured aware datetimes")
    return value.astimezone(UTC)


def latest(items: Mapping[str, Mapping[str, Any]], eligible: frozenset[str]) -> tuple[str, ...]:
    """Newest became-true lower bound, then recorded, then ID. Unknown clocks sort last.

    An upper bound alone is the documented fallback. A fully unknown clock remains unknown;
    recording time breaks ties but never turns it into a known world-time claim.
    """
    rows = sorted(eligible & items.keys())

    def key(item_id: str) -> tuple[bool, datetime, datetime]:
        row = items[item_id]
        became_true = row.get("became_true_earliest") or row.get("became_true_latest")
        known = row.get("became_true_mode") != "unknown" and became_true is not None
        return known, _time(became_true) if known else _MIN_TIME, _time(row.get("recorded"))

    # Stable sorting retains ascending IDs when both dates tie, without lossy timestamps.
    return tuple(sorted(rows, key=key, reverse=True))
