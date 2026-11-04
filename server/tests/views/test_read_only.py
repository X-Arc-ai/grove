"""The view reads open read transactions only; the one write goes through grove.writer.positions.

The app runs on a driver whose sessions are in READ access mode and expose execute_read and
nothing else, so a write path of any kind fails the request.
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grove.api.app import create_app
from grove.views.session import SESSION_COOKIE
from grove.writer.testing import plant_node, plant_session, set_self_item
from tests.views.conftest import plant_small_world

SERVER = Path(__file__).resolve().parents[2]
READ_ACCESS = "READ"
_READ_ENTRY = "execute_read"


class ReadOnlySession:
    def __init__(self, session, calls: list[str]) -> None:
        self._session = session
        self._calls = calls

    def __enter__(self):
        self._session.__enter__()
        return self

    def __exit__(self, *exc):
        return self._session.__exit__(*exc)

    def __getattr__(self, name):
        self._calls.append(name)
        if name != _READ_ENTRY:
            raise PermissionError(f"the views opened {name} on the store")
        return getattr(self._session, name)


class ReadOnlyDriver:
    def __init__(self, driver) -> None:
        self._driver = driver
        self.calls: list[str] = []
        self.modes: list[str] = []

    def session(self, **options):
        options["default_access_mode"] = READ_ACCESS
        self.modes.append(options["default_access_mode"])
        return ReadOnlySession(self._driver.session(**options), self.calls)


def test_every_view_read_runs_in_read_mode_only(driver, world):
    small = plant_small_world(driver, world)
    set_self_item(driver, world, small.maya["id"])
    token = plant_session(driver, world)
    guarded = ReadOnlyDriver(driver)
    client = TestClient(
        create_app(driver=guarded), raise_server_exceptions=False, cookies={SESSION_COOKIE: token}
    )
    for path in (
        "/v1/privacy",
        "/v1/views/session",
        "/v1/views/graph",
        "/v1/views/graph?as_of=2026-10-01T00:00:00Z&known_as_of=2026-10-01T00:00:00Z",
        f"/v1/views/node/{small.venue['id']}",
        "/v1/views/schema",
        "/v1/views/health",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    assert guarded.calls and set(guarded.calls) == {_READ_ENTRY}
    assert set(guarded.modes) == {READ_ACCESS}


def test_the_guard_sees_a_write(driver, world):
    """The positions endpoint writes, so on the read-only driver it fails: the guard is real."""
    item = plant_node(driver, world, "Item")["id"]
    token = plant_session(driver, world)
    guarded = ReadOnlyDriver(driver)
    client = TestClient(
        create_app(driver=guarded), raise_server_exceptions=False, cookies={SESSION_COOKIE: token}
    )
    response = client.post(
        "/v1/views/positions", json={"positions": [{"id": item, "x": 1, "y": 1}]}
    )
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal",
        "message": "the server failed",
        "details": {},
        "retryable": False,
    }
    assert set(guarded.calls) - {_READ_ENTRY}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_views_package_never_imports_the_writer():
    files = sorted((SERVER / "grove" / "views").glob("*.py"))
    assert files
    for path in files:
        assert not any(name.startswith("grove.writer") for name in imports_of(path)), path.name


def assert_api_writer_boundary(files):
    allowed = {
        # The app maps the shared refusal type without opening a writer operation.
        "app.py": {"grove.writer.errors"},
        "views.py": {"grove.writer.positions"},
        "auth.py": {"grove.writer.accounts", "grove.writer.errors"},
        "oauth.py": {"grove.writer.oauth"},
        # D14 remains an explicit authenticated API, never an import from the view package.
        "forget.py": {
            "grove.writer.forget",
            "grove.writer.forget_approvals",
            "grove.writer.forget_ledger",
            "grove.writer.errors",
        },
        # D6's explicit explanation POST uses only its explanation writer.
        "cascade.py": {"grove.writer.cascade_explanations"},
        # Authenticated connector consent/settings/sync use only their connection writer.
        "connectors.py": {"grove.writer.connectors", "grove.writer.errors"},
        # brain propose and brain finish of a live pass (runner): the writer's public API, the
        # run record's account of a proposal, a receipt for the agent, the request id check.
        "internal.py": {"grove.writer", "grove.writer.receipts", "grove.writer.requests"},
    }
    assert files
    for path in files:
        writer = {name for name in imports_of(path) if name.startswith("grove.writer")}
        assert writer <= allowed.get(path.name, set()), path.name


def test_api_writer_imports_stay_within_the_explicit_route_boundary():
    assert_api_writer_boundary(sorted((SERVER / "grove" / "api").glob("*.py")))


def test_api_app_can_import_the_shared_writer_error_type(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("from grove.writer.errors import InvalidRecord")
    assert_api_writer_boundary([app])


@pytest.mark.parametrize(
    ("route", "source"),
    [
        ("app.py", "from grove.writer.accounts import create_account"),
        ("cascade.py", "from grove.writer.accounts import create_account"),
        ("unapproved.py", "from grove.writer.connectors import connection"),
        ("connectors.py", "from grove.writer.accounts import create_account"),
        ("views.py", "import grove.writer.connectors"),
        ("app.py", "from grove.writer.accounts import create_account"),
        ("forget.py", "from grove.writer.connectors import connection"),
    ],
)
def test_api_writer_boundary_rejects_unapproved_routes_and_imports(tmp_path, route, source):
    planted = tmp_path / route
    planted.write_text(source)
    with pytest.raises(AssertionError, match=route):
        assert_api_writer_boundary([planted])
