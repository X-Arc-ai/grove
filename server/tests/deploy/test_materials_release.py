"""Offline material-release prerequisites. No Docker, database, or host access."""

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"


@pytest.fixture
def preflight(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "materials_preflight", DEPLOY / "materials_preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Only this in-process fixture substitutes the operating-system identity.
    monkeypatch.setattr(module, "API_UID", os.getuid())
    return module


def journal_file(root, rows=None):
    world = "t_materials_00000000-0000-4000-8000-000000000001"
    rows = [] if rows is None else rows
    payload = {
        "format": "grove.forget-ledger.v1",
        "world_id": world,
        "rows": rows,
        "count": len(rows),
        "sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    path = root / (hashlib.sha256(world.encode()).hexdigest() + ".json")
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


@pytest.fixture
def journal(tmp_path):
    root = tmp_path.resolve() / "journal"
    root.mkdir(mode=0o700)
    (root / ".lock").touch(mode=0o600)
    journal_file(root)
    return root


def snapshot(root):
    result = {}
    for path in [root, *sorted(root.iterdir())]:
        info = path.lstat()
        result[path.name] = (
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if path.is_file() else None,
        )
    return result


def test_preflight_reads_existing_journal_without_changing_it(preflight, journal):
    before = snapshot(journal)
    assert preflight.check(journal) == {"files": 1, "rows": 0}
    assert snapshot(journal) == before


def test_preflight_never_bootstraps_missing_directory_or_lock(preflight, journal):
    absent = journal.parent / "absent"
    with pytest.raises(preflight.Refused):
        preflight.check(absent)
    assert not absent.exists()
    (journal / ".lock").unlink()
    before = snapshot(journal)
    with pytest.raises(preflight.Refused):
        preflight.check(journal)
    assert snapshot(journal) == before


@pytest.mark.parametrize("configured", [None, "", "relative/journal", "/", "/a/../b"])
def test_preflight_refuses_unconfigured_or_noncanonical_path(preflight, configured):
    with pytest.raises(preflight.Refused):
        preflight.check(configured)


@pytest.mark.parametrize("which,mode", [("directory", 0o750), (".lock", 0o640), ("ledger", 0o400)])
def test_preflight_does_not_repair_private_modes(preflight, journal, which, mode):
    path = (
        journal
        if which == "directory"
        else (next(journal.glob("*.json")) if which == "ledger" else journal / which)
    )
    path.chmod(mode)
    before = snapshot(journal)
    with pytest.raises(preflight.Refused):
        preflight.check(journal)
    assert snapshot(journal) == before


def test_preflight_requires_api_owner(preflight, journal, monkeypatch):
    monkeypatch.setattr(preflight, "API_UID", os.getuid() + 1)
    with pytest.raises(preflight.Refused):
        preflight.check(journal)


@pytest.mark.parametrize("which", ["root", "parent", "lock", "ledger"])
def test_preflight_refuses_every_symlink_position(preflight, journal, which):
    if which in ("root", "parent"):
        link = journal.parent / "alias"
        link.symlink_to(journal if which == "root" else journal.parent, target_is_directory=True)
        configured = link if which == "root" else link / journal.name
    else:
        path = journal / ".lock" if which == "lock" else next(journal.glob("*.json"))
        moved = journal.parent / "moved"
        path.rename(moved)
        path.symlink_to(moved)
        configured = journal
    with pytest.raises(preflight.Refused):
        preflight.check(configured)


@pytest.mark.parametrize(
    "fault",
    [
        "checksum",
        "count",
        "filename",
        "duplicate_id",
        "duplicate_key",
        "unknown",
        "temporary",
        "hardlink",
        "oversize",
    ],
)
def test_preflight_preserves_and_refuses_untrusted_contents(preflight, journal, fault, monkeypatch):
    path = next(journal.glob("*.json"))
    data = json.loads(path.read_text())
    if fault == "checksum":
        data["sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif fault == "count":
        data["count"] = True
        path.write_text(json.dumps(data))
    elif fault == "filename":
        path.rename(journal / ("0" * 64 + ".json"))
    elif fault == "duplicate_id":
        journal_file(journal, [{"id": "same"}, {"id": "same"}])
    elif fault == "duplicate_key":
        path.write_text('{"format":"other",' + path.read_text()[1:])
    elif fault in ("unknown", "temporary"):
        (journal / ("unknown" if fault == "unknown" else ".ledger-interrupted")).touch(mode=0o600)
    elif fault == "hardlink":
        os.link(path, journal.parent / "other-name")
    else:
        monkeypatch.setattr(preflight, "MAX_FILE_BYTES", 8)
    before = snapshot(journal)
    with pytest.raises(preflight.Refused):
        preflight.check(journal)
    assert snapshot(journal) == before


def test_preflight_refuses_busy_existing_lock_without_wait(preflight, journal):
    with (journal / ".lock").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(preflight.Refused):
            preflight.check(journal)


def test_preflight_refuses_directory_permission_drift(preflight, journal, monkeypatch):
    original = preflight.envelope

    def change(data, name):
        journal.chmod(0o750)
        return original(data, name)

    monkeypatch.setattr(preflight, "envelope", change)
    with pytest.raises(preflight.Refused, match="journal_directory_changed"):
        preflight.check(journal)
    assert journal.stat().st_mode & 0o777 == 0o750


def test_preflight_cli_missing_configuration_is_private(tmp_path):
    env = {**os.environ}
    env.pop("GROVE_FORGET_LEDGER_HOST_DIR", None)
    done = subprocess.run(
        [sys.executable, str(DEPLOY / "materials_preflight.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert "materials_preflight_refused: journal_not_configured" in done.stderr


def test_compose_mounts_durable_journal_only_in_api():
    services = yaml.safe_load((DEPLOY / "compose.yml").read_text())["services"]
    api = services["api"]
    destination = api["environment"]["GROVE_FORGET_LEDGER_DIR"]
    matches = [m for m in api["volumes"] if isinstance(m, dict) and m["target"] == destination]
    assert len(matches) == 1
    assert matches[0] == {
        "type": "bind",
        "source": "${GROVE_FORGET_LEDGER_HOST_DIR:?set the existing private journal directory}",
        "target": destination,
        "read_only": False,
        "bind": {"create_host_path": False},
    }
    assert destination.startswith("/run/grove/")
    assert api["read_only"] is True
    assert api["tmpfs"] == ["/tmp:size=320m"]
    assert api["mem_limit"] == api["memswap_limit"] == "4g"
    assert api["cap_drop"] == ["ALL"]
    for name in ("runner", "gateway", "neo4j"):
        assert "GROVE_FORGET_LEDGER" not in json.dumps(services[name])
    assert services["runner"]["networks"] == ["runner_v2"]
    assert services["runner"]["user"] == "10002:10002"


def location_for(path):
    text = (DEPLOY / "nginx/grove-https.conf.template").read_text()
    locations = re.findall(r"location\s+(?:(=|\^~)\s+)?([^\s{]+)\s*\{([^{}]*)\}", text)
    exact = [body for kind, route, body in locations if kind == "=" and route == path]
    if exact:
        return exact[0]
    prefixes = [
        (route, body) for kind, route, body in locations if kind != "=" and path.startswith(route)
    ]
    return max(prefixes, key=lambda pair: len(pair[0]))[1]


@pytest.mark.parametrize(
    "path,limit",
    [
        ("/v1/materials/files", "16m"),
        ("/v1/materials/notes", "70k"),
        ("/v1/materials/notes/00000000-0000-4000-8000-000000000001", "70k"),
        ("/v1/materials/00000000-0000-4000-8000-000000000001/delete", "4k"),
        ("/v1/materials/00000000-0000-4000-8000-000000000001/delete-confirmations", "4k"),
    ],
)
def test_nginx_streams_each_material_mutation_at_its_api_limit(path, limit):
    body = location_for(path)
    assert f"client_max_body_size {limit};" in body
    assert "proxy_request_buffering off;" in body
    assert "proxy_pass http://127.0.0.1:8000;" in body
    # HTTP/1.1 also prevents a chunked request from forcing nginx buffering.
    assert "proxy_http_version 1.1;" in (DEPLOY / "nginx/grove-https.conf.template").read_text()


def test_nginx_keeps_source_upload_and_internal_refusal():
    upload = location_for("/v1/sources/upload")
    assert "client_max_body_size 256m;" in upload
    assert "proxy_request_buffering off;" in upload
    assert "return 404;" in location_for("/v1/internal/runner/due")
    text = (DEPLOY / "nginx/grove-https.conf.template").read_text()
    assert "server_name __GROVE_HOST__;" in text
    assert "proxy_set_header Host $host;" in text


def test_apply_refuses_missing_journal_before_any_host_mutation(box):
    from tests.deploy.conftest import SHA

    (box.root / "fake/HEAD").write_text(SHA)
    before = snapshot(box.etc)
    done = box.run("apply.sh", SHA, "develop", GROVE_LOCK_HELD="1", GROVE_FORGET_LEDGER_HOST_DIR="")
    assert done.returncode == 78
    assert "journal_not_configured" in done.stderr
    assert snapshot(box.etc) == before
    assert not any(
        call.startswith(("docker ", "chown ", "install ", "systemctl ", "nginx "))
        for call in box.calls()
    )


def test_parent_preserves_services_when_journal_preflight_refuses(box):
    from tests.deploy.conftest import SHA

    box.network_revisions()
    before = box.network_state()
    done = box.run("deploy.sh", SHA, GROVE_FORGET_LEDGER_HOST_DIR="")
    assert done.returncode == 78
    assert "journal_not_configured" in done.stderr
    assert box.network_state() == before
    assert not any(call.startswith(("docker ", "systemctl ", "nginx ")) for call in box.calls())


def test_later_apply_failure_cannot_use_journal_refusal_status(box):
    from tests.deploy.conftest import SHA

    (box.root / "fake/HEAD").write_text(SHA)
    (box.src / "deploy/install-host.sh").write_text("#!/bin/sh\nexit 78\n")
    done = box.run("apply.sh", SHA, "develop", GROVE_LOCK_HELD="1")
    assert done.returncode == 1
    assert "journal_not_configured" not in done.stderr


@pytest.mark.parametrize("keep_checkout", [True, False])
def test_installed_preflight_stops_before_retained_old_parent(box, keep_checkout):
    from tests.deploy.conftest import SHA
    from tests.deploy.test_cutover_scripts import BASELINE_PARENT

    installed = box.root / "installed/cutover.py"
    done = box.run("install-host.sh")
    assert done.returncode == 0, done.stderr
    helper = installed.with_name("grove-materials-preflight.py")
    assert helper.read_bytes() == (box.src / "deploy/materials_preflight.py").read_bytes()
    assert helper.stat().st_mode & 0o777 == 0o600
    (box.src / "deploy/deploy.sh").write_text(BASELINE_PARENT)
    if not keep_checkout:
        shutil.rmtree(box.src)
    before = box.calls()
    done = subprocess.run(
        [sys.executable, str(installed), "launch", SHA],
        env={**box.env, "GROVE_FORGET_LEDGER_HOST_DIR": ""},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert done.returncode == 78
    assert "journal_not_configured" in done.stderr
    assert box.calls() == before


@pytest.mark.parametrize("binding_source", ["environment", "cli"])
@pytest.mark.parametrize("old_parent", [False, True])
@pytest.mark.parametrize("bad_hash", [False, True])
def test_manifest_survives_real_launcher_parent_apply_installer(
    box, binding_source, old_parent, bad_hash
):
    from tests.deploy.conftest import OTHER_SHA, SHA, _write_fake
    from tests.deploy.test_cutover import container, manifest_snapshot
    from tests.deploy.test_cutover_scripts import BASELINE_PARENT

    source = box.src / "deploy/cutover.py"
    # Keep the real parser, launcher, parent, apply, installer and manifest
    # validator. Only the final Docker protocol is replaced at its boundary;
    # test_cutover exercises that protocol with its stateful command model.
    capture = """if __name__ == "__main__":
    SEED_OWNER = (os.getuid(), os.getgid())  # scratch-only ownership substitution
    def capture_run(self):
        atomic(paths()["state"] / "captured-run.json", json.dumps({
            "manifest": self.manifest.data,
            "digest": self.manifest.digest,
            "path": os.environ.get("GROVE_CUTOVER_BASELINE_MANIFEST"),
            "env_digest": os.environ.get("GROVE_CUTOVER_BASELINE_SHA256"),
            "target": self.data["target"],
            "baseline": self.data["baseline"],
        }).encode())
    Cutover.execute = capture_run
    raise SystemExit(main())
"""
    original = 'if __name__ == "__main__":\n    raise SystemExit(main())\n'
    assert source.read_text().count(original) == 1
    source.write_text(source.read_text().replace(original, capture))
    installed = Path(box.env["GROVE_CUTOVER_INSTALLED"])
    bootstrap = box.run("install-host.sh")
    assert bootstrap.returncode == 0, bootstrap.stderr

    revisions = box.root / "binding-revisions"
    shutil.copytree(box.src / "deploy", revisions / SHA / "deploy")
    if old_parent:
        (box.src / "deploy/deploy.sh").write_text(BASELINE_PARENT)
    shutil.copytree(box.src / "deploy", revisions / OTHER_SHA / "deploy")
    box.env["FAKE_REVISIONS"] = str(revisions)
    box.deployed(OTHER_SHA)
    (box.root / "fake/HEAD").write_text(OTHER_SHA)
    lock_paths = {
        "host": Path(box.env["GROVE_LOCK_FILE"]),
        "admission": box.state / "cutover-admission.lock",
        "seed": box.root / "seed.lock",
    }
    locks = {}
    for name, path in lock_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644 if name == "host" else 0o600)
        info = path.stat()
        locks[name] = {
            "path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": info.st_mode & 0o7777,
        }
    smoke = box.root / "request-smoke.sh"
    smoke.write_text("exit 0\n")
    smoke_hash = hashlib.sha256(smoke.read_bytes()).hexdigest()
    services = {}
    for name in ("api", "runner", "gateway", "neo4j"):
        row = container(name)
        row.update(
            id=hashlib.sha256(name.encode()).hexdigest(), name="/grove-" + name, restart_count=0
        )
        services[name] = {
            "revision": None if name == "neo4j" else OTHER_SHA,
            "container": manifest_snapshot(row),
        }
    manifest = {
        "version": 1,
        "request": "binding-test",
        "target": SHA,
        "baseline": OTHER_SHA,
        "smoke_sha256": smoke_hash,
        "locks": locks,
        "services": services,
        "retained": [],
    }
    observed = box.root / "observed-containers.json"
    observed.write_text(json.dumps(services))
    _write_fake(
        box.root / "bin",
        "docker",
        """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\\n")
services = json.loads(Path(os.environ["FAKE_BASELINE_OBSERVATION"]).read_text())
if args[0] == "ps":
    print("\\n".join(binding["container"]["id"] for binding in services.values()))
elif args[0] == "inspect":
    row = next(b["container"] for b in services.values() if b["container"]["id"] == args[-1])
    print(json.dumps(row))
elif args[:2] == ["image", "inspect"]:
    name, revision = args[-1].split(":")
    binding = services["runner" if name == "grove-runner" else "api"]
    assert revision == binding["revision"]
    print(binding["container"]["image"])
else:
    raise AssertionError(args)
""",
    )
    path = box.root / "baseline.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if bad_hash:
        digest = "0" * 64
    env = {
        **box.env,
        "GROVE_CUTOVER_REQUEST": "binding-test",
        "GROVE_CUTOVER_SEED_LOCK": str(lock_paths["seed"]),
        "GROVE_CUTOVER_SMOKE_SCRIPT": str(smoke),
        "GROVE_CUTOVER_SMOKE_SHA256": smoke_hash,
        "FAKE_BASELINE_OBSERVATION": str(observed),
    }
    args = [sys.executable, str(installed), "launch", SHA]
    if binding_source == "environment":
        env.update(GROVE_CUTOVER_BASELINE_MANIFEST=str(path), GROVE_CUTOVER_BASELINE_SHA256=digest)
    else:
        args += ["--baseline-manifest", str(path), "--baseline-manifest-sha256", digest]
    calls_before = len(box.calls())
    done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    if bad_hash:
        assert done.returncode == 1
        assert box.calls()[calls_before:] == []
        assert not (box.state / "captured-run.json").exists()
        assert (box.root / "fake/HEAD").read_text() == OTHER_SHA
        return
    assert done.returncode == 0, done.stderr
    captured = json.loads((box.state / "captured-run.json").read_text())
    assert captured == {
        "manifest": manifest,
        "digest": digest,
        "path": str(path),
        "env_digest": digest,
        "target": SHA,
        "baseline": OTHER_SHA,
    }
    calls = box.calls()[calls_before:]
    assert "systemctl reload-or-restart nginx" in calls
    assert all(
        call.startswith(("docker ps ", "docker inspect ", "docker image inspect "))
        for call in calls
        if call.startswith("docker ")
    )
    assert max(i for i, call in enumerate(calls) if call.startswith("docker ")) < calls.index(
        "systemctl reload-or-restart nginx"
    )
    assert not any("checkout --quiet --detach " + OTHER_SHA in call for call in calls)
    assert (box.root / "fake/HEAD").read_text() == SHA
