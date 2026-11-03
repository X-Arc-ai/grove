"""deploy/runner-secrets.sh: the runner's three secret files, made once, kept, and never shown."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy.conftest import DEPLOY

FILES = ["gateway.env", "runner-auth.env", "runner.env"]


def _run(etc: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY / "runner-secrets.sh")],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "LC_ALL": "C",
            "GROVE_ETC_DIR": str(etc),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _value(path: Path, name: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    (value,) = [line.split("=", 1)[1] for line in lines if line.startswith(name + "=")]
    return value


def _snapshot(etc: Path) -> dict[str, bytes]:
    return {name: (etc / name).read_bytes() for name in sorted(os.listdir(etc))}


@pytest.fixture
def etc(tmp_path: Path) -> Path:
    folder = tmp_path / "etc"
    folder.mkdir(mode=0o700)
    return folder


def test_a_first_run_makes_the_three_files_with_mode_600_and_shows_no_value(etc, tmp_path):
    done = _run(etc, tmp_path)
    assert done.returncode == 0, done.stderr
    # No temporary file stays behind.
    assert sorted(os.listdir(etc)) == FILES
    for name in FILES:
        assert oct((etc / name).stat().st_mode & 0o777) == "0o600", name
        assert f"{name} created" in done.stdout, name

    secret = _value(etc / "gateway.env", "GROVE_GATEWAY_SECRET")
    token = _value(etc / "runner.env", "GROVE_RUNNER_TOKEN")
    digest = _value(etc / "runner-auth.env", "GROVE_RUNNER_TOKEN_SHA256")
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", token)
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()

    shown = done.stdout + done.stderr
    for value in (secret, token, digest):
        assert value not in shown


def test_a_second_run_keeps_the_secret_and_the_token_byte_for_byte(etc, tmp_path):
    assert _run(etc, tmp_path).returncode == 0
    before = _snapshot(etc)
    again = _run(etc, tmp_path)
    assert again.returncode == 0, again.stderr
    assert _snapshot(etc) == before
    for name in FILES:
        assert f"{name} kept" in again.stdout, name
    shown = again.stdout + again.stderr
    assert _value(etc / "gateway.env", "GROVE_GATEWAY_SECRET") not in shown
    assert _value(etc / "runner.env", "GROVE_RUNNER_TOKEN") not in shown


def test_the_hash_follows_the_token_in_runner_env(etc, tmp_path):
    assert _run(etc, tmp_path).returncode == 0
    secret = (etc / "gateway.env").read_bytes()
    token = "tok-" + "q" * 44
    (etc / "runner.env").write_text(f"# set by hand\nGROVE_RUNNER_TOKEN={token}\n")
    (etc / "runner.env").chmod(0o600)

    done = _run(etc, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "runner.env kept" in done.stdout
    assert "runner-auth.env created" in done.stdout
    assert _value(etc / "runner.env", "GROVE_RUNNER_TOKEN") == token
    digest = _value(etc / "runner-auth.env", "GROVE_RUNNER_TOKEN_SHA256")
    assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert (etc / "gateway.env").read_bytes() == secret
    assert token not in done.stdout + done.stderr


def test_a_runner_env_from_bootstrap_gets_its_token_and_keeps_its_comment(etc, tmp_path):
    comment = "# The runner token only. deploy/runner-secrets.sh writes it."
    (etc / "runner.env").write_text(comment + "\n")
    (etc / "runner.env").chmod(0o600)
    (etc / "gateway.env").write_text("# no secret yet\nGROVE_GATEWAY_SECRET=\n")
    (etc / "gateway.env").chmod(0o600)

    done = _run(etc, tmp_path)
    assert done.returncode == 0, done.stderr
    lines = (etc / "runner.env").read_text().splitlines()
    assert lines[0] == comment
    assert len(lines) == 2 and lines[1].startswith("GROVE_RUNNER_TOKEN=")
    gateway = (etc / "gateway.env").read_text().splitlines()
    assert gateway[0] == "# no secret yet"
    assert re.fullmatch(r"[0-9a-f]{64}", _value(etc / "gateway.env", "GROVE_GATEWAY_SECRET"))


def test_a_missing_folder_is_refused_and_not_made(tmp_path):
    missing = tmp_path / "no-such-etc"
    done = _run(missing, tmp_path)
    assert done.returncode != 0
    assert "does not exist" in done.stderr
    assert not missing.exists()


def test_a_short_value_stops_the_run_and_changes_nothing(etc, tmp_path):
    (etc / "runner.env").write_text("GROVE_RUNNER_TOKEN=short-value\n")
    (etc / "runner.env").chmod(0o600)
    assert _run(etc, tmp_path).returncode != 0
    assert (etc / "runner.env").read_text() == "GROVE_RUNNER_TOKEN=short-value\n"
    assert not (etc / "runner-auth.env").exists()
    # The gateway file comes first and is complete; no temporary file stays behind.
    assert sorted(os.listdir(etc)) == ["gateway.env", "runner.env"]


def test_no_value_reaches_a_command_line_or_the_environment():
    text = (DEPLOY / "runner-secrets.sh").read_text(encoding="utf-8")
    # The values are made and read inside python3, which writes them to a file and nowhere else.
    assert "openssl" not in text
    assert "export " not in text
    assert not re.search(r"=\$\(python3", text)
    assert text.count("python3 -I -c") == 1
    assert '>"$tmp"' in text
    assert "umask 077" in text
