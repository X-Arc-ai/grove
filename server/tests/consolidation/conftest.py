from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.runner.passes import PassSettings
from tests.runner.helpers import new_auth, seeded_world


@pytest.fixture
def frozen():
    value = clock.TestClock()
    value.set(datetime(2026, 11, 4, 12, tzinfo=UTC))
    with clock.using(value):
        yield value


@pytest.fixture
def seeded(driver, make_world, frozen):
    return seeded_world(driver, make_world())


@pytest.fixture
def rig(driver, seeded):
    auth, token = new_auth()
    settings = PassSettings(only_worlds=frozenset({seeded}))
    app = create_app(
        ApiSettings(public_base_url="http://127.0.0.1:8000"),
        driver=driver,
        runner_auth=auth,
        pass_settings=settings,
    )
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, auth, token, settings
