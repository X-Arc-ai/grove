"""Source-only connector configuration checks. No host or provider account is used."""
# ruff: noqa: F811

import base64
from pathlib import Path

import pytest
import yaml

from grove.connectors.secrets import SecretUnavailable, Vault, load_vault
from tests.connectors.conftest import fake_google  # noqa: F401
from tests.connectors.test_connections import connect, deps  # noqa: F401
from tests.ingest.conftest import (  # noqa: F401
    app,
    client_for,
    seeded_world,
    test_clock,
)

ROOT = Path(__file__).resolve().parents[3]


def test_calendar_key_mount_and_poll_are_api_only_and_preserve_existing_topology():
    config = yaml.safe_load((ROOT / "deploy/compose.yml").read_text())
    api = config["services"]["api"]
    assert api["environment"]["GROVE_CONNECTOR_KEY_FILE"] == "/run/grove/connectors/key"
    assert api["environment"]["GROVE_CONNECTOR_POLL"] == "1"
    mounts = [m for m in api["volumes"] if m["target"] == "/run/grove/connectors"]
    assert mounts == [
        {
            "type": "bind",
            "source": "/etc/grove/connectors",
            "target": "/run/grove/connectors",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    # Git provisioning is optional: no map or clone mount is a Calendar prerequisite.
    assert "GROVE_GIT_REPOSITORIES_FILE" not in api["environment"]
    assert all("repos" not in m["target"] for m in api["volumes"])
    for name in ("gateway", "runner", "neo4j"):
        service = config["services"][name]
        assert not any(k.startswith("GROVE_CONNECTOR_") for k in service["environment"])
        assert all("/connectors" not in str(m) for m in service.get("volumes", []))
    assert api["networks"] == ["store", "runner_v2", "egress"]
    assert config["networks"]["runner_v2"] == {
        "name": "grove_runner_v2",
        "internal": True,
        "driver_opts": {"com.docker.network.bridge.name": "grove-runner"},
    }


@pytest.mark.parametrize(
    "kind", ["absent", "symlink", "directory", "public", "oversized", "bad_key"]
)
def test_private_key_loader_refuses_invalid_files_without_values_in_errors(
    tmp_path, monkeypatch, kind
):
    key = tmp_path / "key"
    if kind == "symlink":
        target = tmp_path / "target"
        target.touch(mode=0o600)
        target.write_bytes(base64.urlsafe_b64encode(b"v" * 32))
        key.symlink_to(target)
    elif kind == "directory":
        key.mkdir(mode=0o700)
    elif kind != "absent":
        key.touch(mode=0o600)
        data = b"x" * 129 if kind == "oversized" else base64.urlsafe_b64encode(b"v" * 32)
        key.write_bytes(b"invalid" if kind == "bad_key" else data)
        if kind == "public":
            key.chmod(0o644)
    monkeypatch.setenv("GROVE_CONNECTOR_KEY_FILE", str(key))
    with pytest.raises(SecretUnavailable) as caught:
        load_vault()
    assert str(caught.value) == ""


def test_absent_key_refuses_consent_before_provider_call_or_connection_write(
    driver,
    seeded_world,
    client_for,
    deps,
    fake_google,
    monkeypatch,
    tmp_path,
):
    from tests.ingest.conftest import counts

    deps.vault = None
    monkeypatch.setenv("GROVE_CONNECTOR_KEY_FILE", str(tmp_path / "absent"))
    client = client_for(seeded_world)
    before = counts(driver, seeded_world)
    response = client.post("/v1/connectors/google/start", json={})
    assert response.status_code == 503
    assert fake_google.requests == [] and counts(driver, seeded_world) == before


def test_private_key_allows_calendar_consent_without_optional_git_map(
    driver,
    seeded_world,
    client_for,
    deps,
    fake_google,
    monkeypatch,
    tmp_path,
):
    directory = tmp_path / "connectors"
    directory.mkdir(mode=0o700)
    key = directory / "key"
    key.touch(mode=0o600)
    key.write_bytes(base64.urlsafe_b64encode(b"v" * 32))
    monkeypatch.setenv("GROVE_CONNECTOR_KEY_FILE", str(key))
    monkeypatch.delenv("GROVE_GIT_REPOSITORIES_FILE", raising=False)
    deps.vault = None
    assert isinstance(load_vault(), Vault)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert key.stat().st_mode & 0o777 == 0o600
    chosen, _ = connect(client_for(seeded_world), fake_google, driver, seeded_world)
    assert chosen and not (directory / "repositories.json").exists()
    dockerfile = (ROOT / "deploy/api.Dockerfile").read_text()
    assert "--uid 10001 --gid 10001" in dockerfile and "USER grove-api" in dockerfile
