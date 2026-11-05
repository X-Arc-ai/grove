"""The release smoke follows the actual authenticated onboarding entry contract."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# Answers as the app does for a client with no cookie: / redirects, the rest serve.
FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from urllib.parse import urlsplit

args = sys.argv
path = urlsplit(args[-1]).path
status = 200
headers = "Content-Type: text/html\r\n"
body = b"<html>Grove</html>"
if path == "/healthz":
    body = json.dumps(dict(status="ok", store="ok", build="candidate")).encode()
elif path == "/":
    status = int(os.environ["ROOT_STATUS"])
    headers = os.environ["ROOT_HEADERS"].replace("|", "\r\n") + "\r\n"
elif path.startswith("/.well-known/"):
    body = json.dumps(
        dict(resource="https://grove.test/mcp", authorization_servers=["https://grove.test"])
    ).encode()
elif path == "/mcp":
    status = 401
    headers = (
        "WWW-Authenticate: Bearer resource_metadata="
        '"https://grove.test/.well-known/oauth-protected-resource"\r\n'
    )
Path(args[args.index("--output") + 1]).write_bytes(body)
Path(args[args.index("--dump-header") + 1]).write_text(headers)
print(status, end="")
"""


def smoke(tmp_path, status, headers):
    curl = tmp_path / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o700)
    return subprocess.run(
        ["bash", str(ROOT / "deploy/smoke.sh"), "https://grove.test", "candidate"],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "ROOT_STATUS": str(status),
            "ROOT_HEADERS": headers,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_root_redirect_to_login_passes(tmp_path):
    done = smoke(tmp_path, 303, "Location: /login|Cache-Control: no-store")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok / redirects to /login" in done.stdout.splitlines()
    assert "ok /signin page" in done.stdout.splitlines()
    assert "ok /privacy page" in done.stdout.splitlines()


@pytest.mark.parametrize(
    "status,headers",
    [
        (303, "Location: /demo/onboarding|Cache-Control: no-store"),
        (303, "Location: /login/other|Cache-Control: no-store"),
        (303, "Location: /login"),
        (302, "Location: /login|Cache-Control: no-store"),
        (200, "Content-Type: text/html"),
    ],
)
def test_root_that_breaks_the_contract_fails_the_release(tmp_path, status, headers):
    done = smoke(tmp_path, status, headers)
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"https://grove.test/ answered {status} with no page redirect" in done.stderr
