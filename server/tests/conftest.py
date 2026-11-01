"""The live store fixtures. A store that cannot be reached is a failure, never a skip."""

import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from grove.store.connection import open_driver
from grove.writer.migrate import run_migrations
from grove.writer.testing import purge_test_world
from tests._redact import pytest_make_collect_report, pytest_runtest_makereport  # noqa: F401

_DEV_ENV_FILE = Path.home() / ".grove" / "neo4j-dev.env"
_SLICE = re.compile(r"[a-z0-9-]+")


def _load_dev_auth() -> None:
    """Read the dev env file only when the variable is unset. Nothing here prints a value."""
    if "NEO4J_AUTH" in os.environ or not _DEV_ENV_FILE.exists():
        return
    for line in _DEV_ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name == "NEO4J_AUTH":
            os.environ["NEO4J_AUTH"] = value.strip().strip("'\"")


def slice_id() -> str:
    explicit = os.environ.get("GROVE_SLICE_ID")
    if explicit:
        # A world named from a bad slice commits, and then purge refuses it and it leaks.
        if _SLICE.fullmatch(explicit) is None:
            raise ValueError("GROVE_SLICE_ID must match [a-z0-9-]+, for example foundation")
        return explicit
    done = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    )
    branch = re.fullmatch(r"[^/]+/(.+)-\d{8}", done.stdout.strip())
    if branch and _SLICE.fullmatch(branch.group(1)):
        return branch.group(1)
    return "local"


@pytest.fixture(scope="session")
def driver():
    _load_dev_auth()
    with open_driver() as opened:
        opened.verify_connectivity()
        yield opened


@pytest.fixture(scope="session")
def migrated(driver):
    return run_migrations(driver)


@pytest.fixture
def make_world(driver, migrated):
    made = []

    def make() -> str:
        world_id = f"t_{slice_id()}_{uuid4()}"
        made.append(world_id)
        return world_id

    yield make
    for world_id in made:
        purge_test_world(driver, world_id)


@pytest.fixture
def world(make_world):
    return make_world()
