"""deploy/runner-secrets.sh: the runner's three secret files, made once, kept, and never shown."""

import hashlib
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from grove.models.config import ModelSettings, load_model_settings
from grove.models.errors import NotConfigured
from grove.models.gateway import Gateway, create_app
from tests.deploy.conftest import DEPLOY

FILES = ["gateway.env", "runner-auth.env", "runner.env"]


def _run(etc: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY / "runner-secrets.sh"), *args],
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


def _unchanged_state(etc):
    # Read access times are filesystem-dependent. These fields detect writes and chmod/chown.
    return {
        path.name: (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_uid,
            path.stat().st_gid,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in etc.iterdir()
        if path.is_file() and not path.is_symlink()
    }


def test_check_valid_configuration_is_read_only_and_shows_no_values(etc, tmp_path):
    assert _run(etc, tmp_path).returncode == 0
    before = _unchanged_state(etc)
    done = _run(etc, tmp_path, "--check")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "runner_configuration_ok\n"
    assert done.stderr == ""
    assert _unchanged_state(etc) == before
    assert sorted(os.listdir(etc)) == FILES
    for name in FILES:
        value = (etc / name).read_text().splitlines()[-1].split("=", 1)[1]
        assert value not in done.stdout + done.stderr


@pytest.mark.parametrize("name", FILES)
def test_check_missing_file_refuses_without_initialization(etc, tmp_path, name):
    assert _run(etc, tmp_path).returncode == 0
    (etc / name).unlink()
    before = _unchanged_state(etc)
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == f"runner_configuration_invalid {name}\n"
    assert done.stdout == ""
    assert _unchanged_state(etc) == before
    assert not (etc / name).exists()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "x" * 31,
        "x" * 32 + " bad",
        "'" + "x" * 32 + "'",
        "x" * 32 + "#comment",
        "x" * 32 + "${UNDEFINED}",
        "x" * 32 + "\t",
        "x" * 32 + "\\",
        "x" * 32 + "\x00",
        "é" * 32,
    ],
)
@pytest.mark.parametrize(
    "name,key",
    [
        ("gateway.env", "GROVE_GATEWAY_SECRET"),
        ("runner.env", "GROVE_RUNNER_TOKEN"),
    ],
)
def test_check_rejects_malformed_or_short_values_without_writes(etc, tmp_path, name, key, value):
    assert _run(etc, tmp_path).returncode == 0
    (etc / name).write_text(f"{key}={value}\n")
    before = _unchanged_state(etc)
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == f"runner_configuration_invalid {name}\n"
    assert done.stdout == ""
    assert _unchanged_state(etc) == before


@pytest.mark.parametrize(
    "tail",
    [
        "GROVE_GATEWAY_SECRET=" + "d" * 64 + "\n",
        "export GROVE_GATEWAY_SECRET=" + "d" * 64 + "\n",
        "ANOTHER_VARIABLE=unexpected\n",
        "malformed line\n",
    ],
)
def test_check_rejects_duplicate_or_unknown_fields(etc, tmp_path, tail):
    assert _run(etc, tmp_path).returncode == 0
    with (etc / "gateway.env").open("a") as handle:
        handle.write(tail)
    before = _unchanged_state(etc)
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == "runner_configuration_invalid gateway.env\n"
    assert _unchanged_state(etc) == before


@pytest.mark.parametrize("digest", ["", "bad", "z" * 64, "0" * 64])
def test_check_refuses_invalid_or_mismatched_hash(etc, tmp_path, digest):
    assert _run(etc, tmp_path).returncode == 0
    (etc / "runner-auth.env").write_text(f"GROVE_RUNNER_TOKEN_SHA256={digest}\n")
    before = _unchanged_state(etc)
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == "runner_configuration_invalid runner-auth.env\n"
    assert _unchanged_state(etc) == before


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "open", "foreign"])
@pytest.mark.parametrize("name", FILES)
def test_check_refuses_unsafe_files_without_chmod_or_generation(etc, tmp_path, name, kind):
    assert _run(etc, tmp_path).returncode == 0
    path = etc / name
    if kind == "open":
        path.chmod(0o640)
    elif kind == "foreign":
        if os.geteuid() != 0:
            pytest.skip("changing ownership requires root")
        os.chown(path, 12345, 12345)
    else:
        path.unlink()
        if kind == "symlink":
            target = tmp_path / "elsewhere.env"
            target.write_text("unread foreign content\n")
            target.chmod(0o600)
            path.symlink_to(target)
        elif kind == "directory":
            path.mkdir(mode=0o700)
        else:
            os.mkfifo(path, 0o600)
    before = path.lstat()
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == f"runner_configuration_invalid {name}\n"
    after = path.lstat()
    assert (after.st_mode, after.st_uid, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_mode,
        before.st_uid,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def test_check_does_not_execute_shell_text(etc, tmp_path):
    assert _run(etc, tmp_path).returncode == 0
    sentinel = tmp_path / "must-not-exist"
    (etc / "runner.env").write_text(f'GROVE_RUNNER_TOKEN=$(touch "{sentinel}")\n')
    done = _run(etc, tmp_path, "--check")
    assert done.returncode != 0
    assert done.stderr == "runner_configuration_invalid runner.env\n"
    assert not sentinel.exists()


@pytest.mark.parametrize("secret", [None, b"s" * 31])
def test_real_gateway_refuses_missing_or_short_configuration(secret):
    driver = Mock()
    with pytest.raises(NotConfigured):
        Gateway(driver, settings=ModelSettings(gateway_secret=secret))
    assert driver.mock_calls == []


def test_initialization_then_check_allows_real_gateway_health_without_provider_or_store_calls(
    etc,
    tmp_path,
):
    assert _run(etc, tmp_path).returncode == 0
    assert _run(etc, tmp_path, "--check").returncode == 0
    env = {"GROVE_GATEWAY_SECRET": _value(etc / "gateway.env", "GROVE_GATEWAY_SECRET")}
    provider_calls = []

    def blocked_provider(request):
        provider_calls.append(request)
        raise AssertionError("provider calls are forbidden")

    driver = Mock()
    gateway = Gateway(
        driver, settings=load_model_settings(env), transport=httpx.MockTransport(blocked_provider)
    )
    with TestClient(create_app(gateway)) as client:
        response = client.get("/health")
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert client.get("/v1/models").status_code == 401
        assert client.post("/v1/chat/completions", json={}).status_code == 401
    assert provider_calls == []
    assert driver.mock_calls == []
