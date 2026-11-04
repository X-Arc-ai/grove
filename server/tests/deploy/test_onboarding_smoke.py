"""The release smoke follows the actual authenticated onboarding entry contract."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("location,code", [("/login", 0), ("/demo/onboarding", 1)])
def test_root_redirect_contract(tmp_path, location, code):
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlsplit\n"
        "args=sys.argv; path=urlsplit(args[-1]).path; status=200\n"
        "headers='Content-Type: text/html\\r\\n'; body=b'<html>Grove</html>'\n"
        "if path=='/healthz': body=json.dumps(dict(status='ok',store='ok',build='candidate')).encode()\n"
        "elif path=='/': status=303; headers='Location: '+os.environ['ROOT_LOCATION']+'\\r\\nCache-Control: no-store\\r\\n'\n"
        "elif path.startswith('/.well-known/'):\n"
        " body=json.dumps(dict(resource='https://grove.test/mcp',authorization_servers=['https://grove.test'])).encode()\n"
        "elif path=='/mcp': status=401; headers='WWW-Authenticate: Bearer resource_metadata=\"https://grove.test/.well-known/oauth-protected-resource\"\\r\\n'\n"
        "Path(args[args.index('--output')+1]).write_bytes(body)\n"
        "Path(args[args.index('--dump-header')+1]).write_text(headers)\n"
        "print(status,end='')\n"
    )
    curl.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/smoke.sh"), "https://grove.test", "candidate"],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "ROOT_LOCATION": location,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == code, result.stdout + result.stderr
