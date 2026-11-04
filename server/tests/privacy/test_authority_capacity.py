"""Fixed capacity arithmetic, including the complete restrictive and seal reserve."""

from dataclasses import FrozenInstanceError, fields, replace

import pytest

from grove.authority.capacity import (
    COMPLETE_RESERVE,
    HARD,
    RESTRICTIVE_PAIR_RESERVE,
    SEAL_RESERVE,
    SOFT,
    CapacityRefused,
    JournalUsage,
    assess_ordinary_append,
    saturation_reasons,
    validate_usage,
)

ZERO = JournalUsage(0, 0, 0)
DIMENSIONS = ("bytes", "events", "nodes")


def test_usage_is_frozen_exact_measured_fields():
    assert tuple(field.name for field in fields(JournalUsage)) == DIMENSIONS
    assert validate_usage(ZERO) is ZERO
    with pytest.raises(FrozenInstanceError):
        ZERO.bytes = 1
    assert not hasattr(ZERO, "__dict__")


def test_fixed_limits_and_complete_reserve():
    assert HARD == JournalUsage(16_000_000, 32_768, 262_144)
    assert SOFT == JournalUsage(14_000_000, 30_000, 230_000)
    assert RESTRICTIVE_PAIR_RESERVE == JournalUsage(524_288, 2, 16_384)
    assert SEAL_RESERVE == JournalUsage(1_024, 1, 64)
    assert COMPLETE_RESERVE == JournalUsage(525_312, 3, 16_448)
    for dimension in DIMENSIONS:
        assert getattr(COMPLETE_RESERVE, dimension) == (
            getattr(RESTRICTIVE_PAIR_RESERVE, dimension) + getattr(SEAL_RESERVE, dimension)
        )


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_soft_saturation_exact_boundary(dimension):
    boundary = getattr(SOFT, dimension)
    assert not saturation_reasons(replace(ZERO, **{dimension: boundary - 1}))
    for value in (boundary, boundary + 1):
        reasons = saturation_reasons(replace(ZERO, **{dimension: value}))
        assert reasons == frozenset({f"{dimension}_soft_limit"})


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_complete_reserve_exact_boundary(dimension):
    boundary = getattr(HARD, dimension) - getattr(COMPLETE_RESERVE, dimension)
    fits = replace(ZERO, **{dimension: boundary})
    short = replace(ZERO, **{dimension: boundary + 1})
    assert f"{dimension}_reserve_shortfall" not in saturation_reasons(fits)
    assert f"{dimension}_reserve_shortfall" in saturation_reasons(short)
    assert assess_ordinary_append(ZERO, fits) == "arithmetic_fits"
    assert assess_ordinary_append(ZERO, short) == "projected_reserve_shortfall"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_hard_limit_equality_and_overflow_order(dimension):
    equal = replace(ZERO, **{dimension: getattr(HARD, dimension)})
    above = replace(ZERO, **{dimension: getattr(HARD, dimension) + 1})
    huge = replace(ZERO, **{dimension: 2**63 - 1})
    assert assess_ordinary_append(ZERO, equal) == "projected_reserve_shortfall"
    assert assess_ordinary_append(ZERO, above) == "projected_hard_limit"
    assert assess_ordinary_append(equal, huge) == "current_saturated"
    assert assess_ordinary_append(above, huge) == "current_hard_limit"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_nonmonotonic_refuses_before_saturation(dimension):
    with pytest.raises(CapacityRefused, match="^usage:nonmonotonic$"):
        assess_ordinary_append(HARD, replace(HARD, **{dimension: 0}))


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("bad", [True, "1", 1.0, -1, 2**63, None])
def test_malformed_counters_refuse(dimension, bad):
    with pytest.raises(CapacityRefused, match="^usage:counter$"):
        replace(ZERO, **{dimension: bad})


def test_usage_cannot_be_a_payload_or_subclass():
    class OtherUsage(JournalUsage):
        pass

    for bad in ({"bytes": 0, "events": 0, "nodes": 0}, (0, 0, 0), None):
        with pytest.raises(CapacityRefused, match="^usage:type$"):
            validate_usage(bad)
    with pytest.raises(CapacityRefused, match="^usage:type$"):
        OtherUsage(0, 0, 0)
    corrupted = JournalUsage(0, 0, 0)
    object.__setattr__(corrupted, "bytes", True)
    with pytest.raises(CapacityRefused, match="^usage:counter$"):
        assess_ordinary_append(HARD, corrupted)


def test_all_saturation_reasons_are_fixed_and_frozen():
    assert saturation_reasons(HARD) == frozenset(
        f"{dimension}_{reason}"
        for dimension in DIMENSIONS
        for reason in ("soft_limit", "reserve_shortfall")
    )
    assert assess_ordinary_append(ZERO, ZERO) == "arithmetic_fits"
