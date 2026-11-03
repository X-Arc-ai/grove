"""Fixtures of the runner tests. The store fixtures (driver, make_world) come from tests/."""

from datetime import UTC, datetime

import pytest

from grove import clock
from tests.runner.helpers import seeded_world

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
def frozen():
    """A test clock for the whole process, set to T0."""
    test_clock = clock.TestClock()
    test_clock.set(T0)
    with clock.using(test_clock):
        yield test_clock


@pytest.fixture
def seeded(driver, make_world):
    return seeded_world(driver, make_world())
