"""The first two D10/D11 ambiguity rungs, from owner-supplied context.

Context sets must be complete, time-correct identity evidence. A mention is never
resolved by candidate order or by a lexical score alone. No model runs here.
"""

from dataclasses import dataclass
from datetime import datetime

from grove.cleanup.access import CleanupRefused


@dataclass(frozen=True)
class Ambiguity:
    candidates: tuple[str, ...]
    resolved_to: str | None
    context: tuple[str, ...]


def narrow(
    candidates: tuple[str, ...],
    *,
    participants: frozenset[str] | None = None,
    co_mentions: frozenset[str] | None = None,
    valid_at: frozenset[str] | None = None,
) -> Ambiguity:
    if len(candidates) > 100 or len(candidates) != len(set(candidates)):
        raise CleanupRefused("ambiguity_candidates_invalid")
    if any(type(value) is not str or not 0 < len(value) <= 128 for value in candidates):
        raise CleanupRefused("ambiguity_candidate_invalid")
    remaining = set(candidates)
    used = []
    for name, context in (
        ("participants", participants),
        ("co_mentions", co_mentions),
        ("time", valid_at),
    ):
        if context is not None:
            if not isinstance(context, frozenset):
                raise CleanupRefused("ambiguity_context_invalid")
            remaining.intersection_update(context)
            used.append(name)
    # Conflicting context does not erase the original candidates from an unresolved row.
    kept = tuple(sorted(remaining or set(candidates)))
    resolved = next(iter(remaining)) if len(remaining) == 1 else None
    return Ambiguity(kept, resolved, tuple(used))


def expiry(at: datetime) -> datetime:
    from datetime import timedelta

    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise CleanupRefused("ambiguity_time_invalid")
    return at + timedelta(days=30)
