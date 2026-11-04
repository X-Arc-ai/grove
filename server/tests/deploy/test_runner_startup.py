"""Model Compose dependency policy with real runner polling and synthetic HTTP.

These controls do not prove native Docker orchestration. The dependency evaluator
reads the deployed YAML; the transport independently models API availability.
"""

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from neo4j import GraphDatabase

from grove.runner import loop

REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ServiceState:
    started: bool
    healthy: bool


def dependencies_ready(dependencies: dict, states: dict[str, ServiceState]) -> bool:
    """Evaluate declared conditions without an expected API condition in the model."""
    ready = []
    for name, dependency in dependencies.items():
        state = states[name]
        condition = dependency["condition"]
        if condition == "service_started":
            ready.append(state.started)
        elif condition == "service_healthy":
            ready.append(state.started and state.healthy)
        else:
            raise AssertionError(f"unsupported dependency condition: {condition}")
    return all(ready)


class Startup:
    def __init__(self, settings: loop.LoopSettings, effects: dict[str, int]) -> None:
        compose = yaml.safe_load((REPO / "deploy" / "compose.yml").read_text())
        self.dependencies = compose["services"]["runner"]["depends_on"]
        self.settings = settings
        self.effects = effects
        self.states = {
            "api": ServiceState(False, False),
            "gateway": ServiceState(True, False),
        }
        self.runner = None
        self.due_results = []
        self.transport_error = False

    def request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/passes"):
            self.effects["pass_starts"] += 1
            raise AssertionError("unexpected pass start")
        assert request.method == "GET"
        assert request.url.path == "/v1/internal/runner/due"
        if not self.states["api"].healthy or self.transport_error:
            self.due_results.append("ConnectError")
            raise httpx.ConnectError("synthetic API unavailable", request=request)
        self.due_results.append(200)
        return httpx.Response(200, json={"due": []})

    def advance(self, api: ServiceState) -> None:
        self.states["api"] = api
        if self.runner is None and dependencies_ready(self.dependencies, self.states):
            client = httpx.Client(
                base_url=f"{self.settings.api_url}/v1/internal/runner",
                transport=httpx.MockTransport(self.request),
                trust_env=False,
            )
            self.runner = loop.Runner(self.settings, loop.RunnerApi(self.settings, client))
            # The real service polls immediately after its local ready event.
            assert self.runner.tick() == 0
            assert self.runner.running() == 0

    def outcome(self) -> str:
        """Called when the bounded synthetic startup schedule ends."""
        return "started" if self.runner is not None else "startup_failed"


@pytest.fixture
def startup(tmp_path, monkeypatch):
    effects = {"pass_starts": 0, "processes": 0, "database_calls": 0, "network_calls": 0}

    def forbidden(name):
        def reject(*args, **kwargs):
            effects[name] += 1
            raise AssertionError(f"unexpected {name}")

        return reject

    monkeypatch.setattr(loop, "run_pass", forbidden("pass_starts"))
    monkeypatch.setattr(loop.subprocess, "Popen", forbidden("processes"))
    monkeypatch.setattr(GraphDatabase, "driver", forbidden("database_calls"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden("network_calls"))
    monkeypatch.setattr("socket.socket.connect", forbidden("network_calls"))
    monkeypatch.setattr("socket.socket.connect_ex", forbidden("network_calls"))
    settings = loop.LoopSettings(
        api_url="http://api.test:8000",
        runner_token="synthetic-runner-token",
        gateway_url="http://gateway.test:18300/v1",
        home=tmp_path,
        config_dir=REPO / "runner",
        opencode_bin="unused-opencode",
        brain_bin="unused-brain",
        shell_bin="unused-shell",
    )
    model = Startup(settings, effects)
    try:
        yield model
    finally:
        if model.runner is not None:
            model.runner.api.close()
        # No network, database or child process means no real model call can occur.
        assert effects == dict.fromkeys(effects, 0)


def test_delayed_api_health_prevents_early_due_poll(startup, capsys):
    startup.advance(ServiceState(started=True, healthy=False))
    early_start = startup.runner is not None
    early_requests = list(startup.due_results)
    early_log = capsys.readouterr().out

    startup.advance(ServiceState(started=True, healthy=True))

    assert not early_start, (early_requests, early_log)
    assert early_requests == []
    assert early_log == ""
    assert startup.outcome() == "started"
    assert startup.due_results == [200]
    assert capsys.readouterr().out == ""


def test_unhealthy_api_exhausts_startup_without_runner_or_due_call(startup, capsys):
    for state in (
        ServiceState(started=False, healthy=False),
        ServiceState(started=True, healthy=False),
        ServiceState(started=True, healthy=False),
    ):
        startup.advance(state)

    assert startup.outcome() == "startup_failed"
    assert startup.runner is None
    assert startup.due_results == []
    assert capsys.readouterr().out == ""


def test_transport_error_after_api_health_still_emits_due_failed(startup, capsys):
    startup.advance(ServiceState(started=True, healthy=True))
    assert startup.outcome() == "started"
    assert startup.due_results == [200]
    assert capsys.readouterr().out == ""

    startup.transport_error = True
    assert startup.runner.tick() == 0

    assert startup.due_results == [200, "ConnectError"]
    assert capsys.readouterr().out == "grove-runner due_failed error=ConnectError\n"
    assert startup.runner.running() == 0
