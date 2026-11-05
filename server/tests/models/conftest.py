"""Fixtures of the model tests."""

import pytest

from tests.runner.helpers import seeded_world


@pytest.fixture
def make_world(driver, make_world):
    """The meter records through the writer, and the writer locks a real World row."""

    def make() -> str:
        return seeded_world(driver, make_world())

    return make
