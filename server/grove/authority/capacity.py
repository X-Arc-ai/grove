"""Arithmetic over caller-measured usage, never a writer capability or a parser."""

from dataclasses import dataclass

from .scalars import ScalarRefused, validate_count


@dataclass(frozen=True, slots=True)
class JournalUsage:
    bytes: int
    events: int
    nodes: int

    def __post_init__(self) -> None:
        validate_usage(self)


class CapacityRefused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"usage:{reason}")


def validate_usage(usage: object) -> JournalUsage:
    if type(usage) is not JournalUsage:
        raise CapacityRefused("type")
    try:
        for dimension in ("bytes", "events", "nodes"):
            validate_count(getattr(usage, dimension))
    except (ScalarRefused, AttributeError):
        raise CapacityRefused("counter") from None
    return usage


HARD = JournalUsage(16_000_000, 32_768, 262_144)
SOFT = JournalUsage(14_000_000, 30_000, 230_000)
RESTRICTIVE_PAIR_RESERVE = JournalUsage(524_288, 2, 16_384)
SEAL_RESERVE = JournalUsage(1_024, 1, 64)
COMPLETE_RESERVE = JournalUsage(525_312, 3, 16_448)
_DIMENSIONS = ("bytes", "events", "nodes")


def saturation_reasons(usage: JournalUsage) -> frozenset[str]:
    validate_usage(usage)
    reasons = set()
    for dimension in _DIMENSIONS:
        count = getattr(usage, dimension)
        if count >= getattr(SOFT, dimension):
            reasons.add(f"{dimension}_soft_limit")
        if getattr(HARD, dimension) - count < getattr(COMPLETE_RESERVE, dimension):
            reasons.add(f"{dimension}_reserve_shortfall")
    return frozenset(reasons)


def assess_ordinary_append(current: JournalUsage, projected: JournalUsage) -> str:
    """Assess supplied totals in the specified order; do not authorize an append."""
    validate_usage(current)
    validate_usage(projected)
    if any(getattr(projected, d) < getattr(current, d) for d in _DIMENSIONS):
        raise CapacityRefused("nonmonotonic")
    if any(getattr(current, d) > getattr(HARD, d) for d in _DIMENSIONS):
        return "current_hard_limit"
    if saturation_reasons(current):
        return "current_saturated"
    if any(getattr(projected, d) > getattr(HARD, d) for d in _DIMENSIONS):
        return "projected_hard_limit"
    if any(
        getattr(HARD, d) - getattr(projected, d) < getattr(COMPLETE_RESERVE, d) for d in _DIMENSIONS
    ):
        return "projected_reserve_shortfall"
    return "arithmetic_fits"
