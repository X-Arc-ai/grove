from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api import internal
from grove.api.errors import ApiError
from grove.cleanup.access import CleanupRefused
from grove.cli import main as cli
from grove.runner import brain
from grove.runner.shell import Refused, parse


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/status", None),
        ("GET", "/pass", None),
        ("GET", "/items/00000000-0000-7000-8000-000000000001", None),
        ("POST", "/recall", {"q": "source"}),
        ("POST", "/propose", {"batch": {}}),
        ("POST", "/fetch", {"url": "https://public.example"}),
        ("POST", "/finish", {"request_id": "request"}),
    ],
)
def test_inactive_curator_refuses_before_legacy_reads_or_writes(method, path, body):
    app = FastAPI()
    app.include_router(internal.router)
    app.dependency_overrides[internal.live_pass] = lambda: SimpleNamespace(
        claims=SimpleNamespace(agent="curator")
    )

    @app.exception_handler(ApiError)
    async def handle(_, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"refused": True}, status_code=error.status)

    # No driver exists. Any accidental legacy call fails this test.
    response = TestClient(app).request(method, "/v1/internal/brain" + path, json=body)
    assert response.status_code == 503


def test_direct_pass_input_does_not_expose_legacy_curator_data():
    with pytest.raises(CleanupRefused):
        brain.pass_input(None, "test_world", {"agent": "curator"})


def test_fetch_cli_keeps_url_in_body_and_shell_keeps_its_guards(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_call", lambda *a, **k: calls.append((a, k)) or {})
    assert CliRunner().invoke(cli.brain, ["fetch", "https://public.example/status"]).exit_code == 0
    assert calls == [(("POST", "/fetch"), {"json": {"url": "https://public.example/status"}})]
    assert parse("brain fetch 'https://public.example/status?x=1&y=2'")[0][1] == "fetch"
    with pytest.raises(Refused):
        parse("brain fetch https://public.example; curl https://private.example")
