"""Run the published smoke's Python with controlled HTTP responses, never a network."""

import hashlib
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/request8-public-smoke.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = "https://app.grove.x-arc.ai"
ASSET = b"console.log('synthetic');"
ASSET_PATH = f"/assets/grove/{hashlib.sha256(ASSET).hexdigest()}.js"
PAGES = ("/login", "/signup", "/signin", "/logout")
REFUSALS = {
    "/v1/views/session": 401,
    "/v1/views/pipedream/accounts": 401,
    "/assets/grove/manifest.json": 404,
    "/assets/grove/": 404,
}


class Response(io.BytesIO):
    def __init__(self, status, headers, body):
        super().__init__(body)
        self.status = status
        self.headers = headers


@pytest.fixture
def smoke(monkeypatch, capsys):
    responses = {
        "/healthz": (200, {}, json.dumps({"status": "ok", "store": "ok", "build": SHA}).encode()),
        ASSET_PATH: (200, {"Cache-Control": "public, max-age=31536000, immutable"}, ASSET),
        **{path: (status, {}, b"anonymous refusal") for path, status in REFUSALS.items()},
    }
    for path in PAGES:
        responses[path] = (
            200,
            {"Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8"},
            f'<script src="{ASSET_PATH}"></script>'.encode(),
        )
    calls = []

    def urlopen(request, *, timeout):
        assert request.full_url.startswith(BASE + "/")
        assert request.get_method() == "GET"
        assert dict(request.header_items()) == {"User-agent": "grove-request8-smoke"}
        assert 0 < timeout <= 10
        path = request.full_url.removeprefix(BASE)
        calls.append(path)
        value = responses[path]
        if isinstance(value, Exception):
            raise value
        status, headers, body = value
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "refusal", headers, io.BytesIO(body))
        return Response(status, headers, body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    payload = SCRIPT.read_text().split("<<'PYSMOKE'\n", 1)[1].rsplit("\nPYSMOKE", 1)[0]

    def run(sha=SHA):
        monkeypatch.setattr(sys, "argv", ["-", sha])
        code = 0
        try:
            exec(compile(payload, str(SCRIPT), "exec"), {})
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr().out
        return code, json.loads(output) if output else None

    return run, responses, calls


@pytest.mark.parametrize("args", [[], [SHA, SHA]])
def test_shell_requires_exactly_one_argument(args, tmp_path):
    # A Python launch here would fail the assertion even if the script changed.
    python = tmp_path / "python3"
    python.write_text("#!/bin/sh\nexit 99\n")
    python.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 2
    assert result.stdout == result.stderr == b""


@pytest.mark.parametrize("sha", ["", "a" * 39, "a" * 41, "A" * 40, SHA + "\n"])
def test_invalid_sha_refuses_before_http(smoke, sha):
    run, _, calls = smoke
    assert run(sha) == (2, None)
    assert calls == []


def test_public_smoke_checks_exact_build_pages_assets_and_anonymous_refusals(smoke):
    run, _, calls = smoke
    code, result = run()
    assert code == 0
    assert result["state"] == "pass"
    assert result["target"] == SHA
    assert result["asset_count"] == 1
    assert calls == ["/healthz", *PAGES, ASSET_PATH, *REFUSALS]
    assert result["checks"] == [
        {"path": path, "status": REFUSALS.get(path, 200)} for path in calls
    ]


@pytest.mark.parametrize(
    "fault",
    ["build", "page_cache", "page_type", "asset_absent", "asset_hash", "asset_cache",
     "anonymous", "body_cap", "transport"],
)
def test_smoke_fails_on_contract_violation_without_response_disclosure(smoke, fault):
    run, responses, calls = smoke
    if fault == "build":
        responses["/healthz"] = (200, {}, b'{"status":"ok","store":"ok","build":"wrong"}')
    elif fault in {"page_cache", "page_type"}:
        status, headers, body = responses["/login"]
        headers["Cache-Control" if fault == "page_cache" else "Content-Type"] = "wrong"
    elif fault == "asset_absent":
        for path in PAGES:
            status, headers, _ = responses[path]
            responses[path] = (status, headers, b"no assets")
    elif fault in {"asset_hash", "asset_cache"}:
        status, headers, body = responses[ASSET_PATH]
        if fault == "asset_hash":
            body = b"changed asset"
        else:
            headers["Cache-Control"] = "no-store"
        responses[ASSET_PATH] = (status, headers, body)
    elif fault == "anonymous":
        responses["/v1/views/session"] = (200, {}, b"unexpected account data")
    elif fault == "body_cap":
        responses["/healthz"] = (200, {}, b"x" * 4097)
    else:
        responses["/healthz"] = urllib.error.URLError("private transport detail")
    code, result = run()
    assert code == 1
    assert result == {"state": "fail", "target": SHA, "completed_checks": result["completed_checks"]}
    assert all(set(check) == {"path", "status"} for check in result["completed_checks"])
    assert calls


def test_expired_budget_refuses_before_http(smoke, monkeypatch):
    run, _, calls = smoke
    ticks = iter([0, 106])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    assert run() == (1, {"state": "fail", "target": SHA, "completed_checks": []})
    assert calls == []
