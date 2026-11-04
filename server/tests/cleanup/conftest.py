"""Synthetic-only component fixtures. No production authority is provided."""

from contextlib import contextmanager

import pytest

from tests.models.test_judge import frozen, make_layer  # noqa: F401
from tests.writer.helpers import seeded  # noqa: F401


class SyntheticAuthority:
    def __init__(self, world_id):
        self.world_id = world_id
        self.calls = []

    @contextmanager
    def hold(self, world_id, run_id, action, references):
        assert world_id == self.world_id
        self.calls.append((run_id, action, references))
        yield


@pytest.fixture
def w(frozen, seeded):  # noqa: F811
    return seeded["world_id"]


@pytest.fixture
def access(w):
    return SyntheticAuthority(w)
