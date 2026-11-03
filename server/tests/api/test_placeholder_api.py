"""The API's probes and discovery: the health probe, the protected-resource metadata, the
authorization server metadata, and the 401 of /mcp with no token. tests/hosts has the rest of
OAuth and /mcp."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings, load_api_settings, parse_base_url, parse_build_sha
from grove.config import ConfigError
from grove.store.connection import open_driver
from grove.store.probe import READ_ACCESS, DriverError, Neo4jError, store_reachable

SERVER = Path(__file__).resolve().parents[2]
SMOKE = SERVER.parent / "deploy" / "smoke.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def client(driver):
    settings = ApiSettings(public_base_url="https://grove.example", build_sha=SHA)
    # The fixture's driver stays open for other tests: the app gets one of its own.
    with TestClient(create_app(settings, driver_factory=open_driver)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path",
    ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"],
)
def test_the_metadata_names_the_mcp_resource_of_the_configured_host(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {
        "resource": "https://grove.example/mcp",
        "authorization_servers": ["https://grove.example"],
        "scopes_supported": ["grove.read", "grove.write", "grove.sensitive_write"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Grove",
    }


def test_the_host_name_comes_from_configuration_only(driver):
    for base in ("https://one.example", "https://two.example:8443"):
        app = create_app(ApiSettings(public_base_url=base), driver_factory=open_driver)
        with TestClient(app) as test_client:
            body = test_client.get("/.well-known/oauth-protected-resource").json()
            server = test_client.get("/.well-known/oauth-authorization-server").json()
        assert body["resource"] == f"{base}/mcp"
        assert body["authorization_servers"] == [base]
        assert server["issuer"] == base
        assert {server[name] for name in ("authorization_endpoint", "token_endpoint")} == {
            f"{base}/authorize",
            f"{base}/token",
        }
        assert server["registration_endpoint"] == f"{base}/register"
        assert server["revocation_endpoint"] == f"{base}/revoke"


def test_the_metadata_is_for_any_origin(client):
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ):
        response = client.get(path)
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_an_unauthenticated_mcp_call_gets_401_with_the_metadata_url(client, method):
    response = client.request(method, "/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://grove.example/.well-known/oauth-protected-resource", '
        'scope="grove.read grove.write"'
    )


def test_meta_s_fetcher_gets_the_same_metadata(client):
    plain = client.get("/.well-known/oauth-protected-resource")
    fetcher = client.get(
        "/.well-known/oauth-protected-resource", headers={"User-Agent": "meta-webindexer/1.1"}
    )
    assert fetcher.status_code == 200
    assert fetcher.json() == plain.json()


def test_health_says_ok_with_the_store_up_and_names_the_build(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "store": "ok", "build": SHA}
    assert response.headers["cache-control"] == "no-store"


def test_health_answers_503_fast_when_the_store_is_down(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setenv("GROVE_NEO4J_URI", f"bolt://127.0.0.1:{closed_port}")
    monkeypatch.setenv("NEO4J_AUTH", "none")
    settings = ApiSettings(public_base_url="https://grove.example")
    # The app starts with the store down: opening a driver does not connect.
    with TestClient(create_app(settings)) as test_client:
        started = clock.monotonic()
        response = test_client.get("/healthz")
        elapsed = clock.monotonic() - started
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "store": "unreachable", "build": None}
    assert elapsed < 10


def test_the_api_publishes_no_schema_or_docs(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "app.example",
        "ftp://app.example",
        "https://app.example/path",
        "https://app.example/?q=1",
        "https://app.example/#top",
        "https://user:pw@app.example",
        "http://app.example",
    ],
)
def test_a_base_url_that_is_not_an_https_origin_is_refused(value):
    with pytest.raises(ConfigError) as refused:
        parse_base_url(value)
    assert "pw" not in str(refused.value)


def test_a_base_url_is_kept_as_its_origin():
    assert parse_base_url("https://App.Example/") == "https://app.example"
    assert parse_base_url("https://app.example:8443") == "https://app.example:8443"
    assert parse_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"


def test_a_build_sha_is_a_full_sha_or_nothing():
    assert parse_build_sha(None) is None
    assert parse_build_sha("") is None
    assert parse_build_sha(SHA) == SHA
    for bad in (SHA[:12], SHA.upper(), SHA + "0"):
        with pytest.raises(ConfigError):
            parse_build_sha(bad)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def served(driver, tmp_path):
    """The real app under uvicorn, as on the box, with a loopback base URL."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    pages = tmp_path / "dist"
    pages.mkdir()
    for name in ("home.html", "index.html", "privacy.html"):
        (pages / name).write_text(f"<!doctype html><title>{name}</title>")
    env = {
        **os.environ,
        "GROVE_PUBLIC_BASE_URL": base,
        "GROVE_BUILD_SHA": SHA,
        "GROVE_WEB_DIST": str(pages),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "grove.api.app:create_app", "--factory"]
        + ["--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=SERVER,
        env=env,
    )
    try:
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_the_deploy_smoke_passes_against_the_real_app(served):
    done = subprocess.run(
        ["bash", str(SMOKE), served, SHA],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GROVE_POLL_SECONDS": "0.2"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        f"ok healthz build={SHA}",
        "ok / page",
        "ok /signin page",
        "ok /privacy page",
        "ok /.well-known/oauth-protected-resource",
        "ok /.well-known/oauth-protected-resource/mcp",
        "ok /mcp 401",
    ]


def test_the_real_app_answers_the_home_page_at_root_with_no_cookie(served):
    response = httpx.get(f"{served}/", timeout=10)
    assert response.status_code == 200
    assert "<title>home.html</title>" in response.text


def test_the_deploy_smoke_fails_against_the_real_app_serving_another_build(served):
    done = subprocess.run(
        ["bash", str(SMOKE), served, "f" * 40],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GROVE_SMOKE_TRIES": "2", "GROVE_POLL_SECONDS": "0"},
    )
    assert done.returncode == 1
    assert "did not answer ok for build" in done.stderr


class _Session:
    def __init__(self, error, record):
        self._error, self._record = error, record

    def __enter__(self):
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *details):
        return False

    def run(self, _query):
        return self

    def single(self):
        return self._record


class _Driver:
    def __init__(self, error=None, record=None):
        self._error, self._record = error, record
        self.options = {}
        self.closed = False

    def session(self, **options):
        self.options = options
        return _Session(self._error, self._record)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("error", [OSError("down"), Neo4jError(), DriverError()])
def test_the_probe_says_no_on_each_way_a_store_can_fail(error):
    assert store_reachable(_Driver(error=error)) is False


def test_the_probe_reads_in_read_mode_and_trusts_only_its_own_answer():
    driver = _Driver(record={"ok": 1})
    assert store_reachable(driver) is True
    assert driver.options["default_access_mode"] == READ_ACCESS
    assert store_reachable(_Driver(record={"ok": 2})) is False
    assert store_reachable(_Driver(record=None)) is False


def test_the_app_opens_its_driver_with_short_timeouts(monkeypatch):
    seen = {}

    def fake_open(**options):
        seen.update(options)
        return _Driver(record={"ok": 1})

    monkeypatch.setattr("grove.api.app.open_driver", fake_open)
    with TestClient(create_app(ApiSettings(public_base_url="https://grove.example"))):
        pass
    assert seen == {"connection_timeout": 5.0, "connection_acquisition_timeout": 10.0}


def test_the_app_closes_its_driver_at_shutdown():
    driver = _Driver(record={"ok": 1})
    settings = ApiSettings(public_base_url="https://grove.example")
    with TestClient(create_app(settings, driver_factory=lambda: driver)):
        assert driver.closed is False
    assert driver.closed is True


def test_production_refuses_a_missing_base_url_and_a_local_run_has_a_default(monkeypatch):
    monkeypatch.delenv("GROVE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("GROVE_ENV", "production")
    with pytest.raises(ConfigError):
        load_api_settings()
    monkeypatch.setenv("GROVE_ENV", "development")
    assert load_api_settings().public_base_url == "http://127.0.0.1:8000"


def test_the_merged_app_serves_the_views_beside_the_probes(driver, migrated):
    settings = ApiSettings(public_base_url="https://grove.example")
    with TestClient(create_app(settings, driver=driver)) as test_client:
        assert test_client.get("/healthz").status_code == 200
        metadata = "/.well-known/oauth-protected-resource"
        assert test_client.get(metadata).json()["resource"] == "https://grove.example/mcp"
        # The views' router, with its sign-in rule and its no-store middleware.
        views = test_client.get("/v1/views/health")
        assert views.status_code == 401
        assert views.headers["cache-control"] == "no-store"
    # The caller owns a driver it handed in: the app did not close it.
    driver.verify_connectivity()
