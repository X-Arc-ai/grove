"""Synthetic command controls for the migration barrier; never invoke Docker."""

import copy
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from grove import clock

MODULE = Path(__file__).resolve().parents[3] / "deploy/cutover.py"
spec = importlib.util.spec_from_file_location("cutover", MODULE)
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
BASE = "1" * 40
TARGET = "2" * 40


def image(service, target=False):
    return "sha256:" + ("4" if target else "3") * 63 + ("a" if service == "runner" else "b")


def container(service, *, target=False, oneoff=False):
    return {
        "id": ("new-" if target else "old-") + service,
        "project": "grove",
        "service": service,
        "image": image(service, target),
        "oneoff": str(oneoff),
        "running": not target,
        "status": "created" if target else "running",
        "health": "healthy",
        "restart": {"Name": "no" if target else "unless-stopped", "MaximumRetryCount": 0},
        "networks": {"grove_store": {}},
    }


class Commands:
    """Stateful process model: migration fails if any old writer can still write."""

    def __init__(self):
        self.rows = {name: container(name) for name in (*cutover.WRITERS, "neo4j")}
        self.calls = []
        self.failure = None
        self.deadline = clock.monotonic() + 3000
        self.generations = [None, 4]
        self.migration_applied = False

    def run(self, args, seconds=30, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append((args, seconds))
        joined = " ".join(args)
        if self.failure == "migration" and "exec" in args and args[-1] == "grove.writer.migrate":
            self.migration_applied = (
                True  # Some statements committed; no ledger or process success.
            )
            raise cutover.Refused("command_deadline")
        if self.failure == "smoke" and args[0] == "bash":
            raise cutover.Refused("command_failed")
        if args[:2] == ["systemctl", "show"]:
            state = "active" if self.failure == "helper" else "inactive"
            return f"ActiveState={state}\nMainPID=0\nControlPID=0\n"
        if args[:2] == ["systemctl", "list-jobs"]:
            return "1 grove-backup.service start waiting" if self.failure == "queued" else ""
        if args[0] == "ps":
            return (
                str(cutover.paths()["src"] / "deploy/backup.sh") if self.failure == "legacy" else ""
            )
        if args[:3] == ["docker", "image", "inspect"]:
            service, sha = args[-1].split(":")
            return image("runner" if service == "grove-runner" else "api", sha == TARGET)
        if args[:2] == ["docker", "ps"]:
            return "\n".join(row["id"] for row in self.rows.values())
        if args[:2] == ["docker", "inspect"]:
            key = args[-1]
            row = self.rows.get(key) or next(
                (row for row in self.rows.values() if row["id"] == key), None
            )
            if row is None:
                raise cutover.Refused("command_failed")
            return json.dumps(row)
        if args[:2] == ["docker", "update"]:
            row = next(row for row in self.rows.values() if row["id"] == args[-1])
            policy = args[2].split("=", 1)[1].split(":")
            row["restart"] = {
                "Name": policy[0],
                "MaximumRetryCount": int(policy[1]) if len(policy) > 1 else 0,
            }
            return ""
        if args[:2] == ["docker", "stop"]:
            for identifier in args[4:]:
                row = next(row for row in self.rows.values() if row["id"] == identifier)
                if self.failure != "surviving" or row["service"] != "api":
                    row.update(running=False, status="exited")
            return ""
        if args[:2] == ["docker", "exec"]:
            assert all(not self.rows[service]["running"] for service in cutover.WRITERS)
            assert all(self.rows[service]["restart"]["Name"] == "no" for service in cutover.WRITERS)
            if args[-1] == "--check-quiescent":
                if self.failure == "transactions":
                    raise cutover.Refused("command_failed")
            elif args[-1] == "--quiescent-reconcile":
                self.generations = [0 if value is None else value for value in self.generations]
            else:
                self.migration_applied = True
            return "{}"
        if args[:2] == ["docker", "compose"]:
            tail = args[2:]
            while tail[0].startswith("--"):
                tail = tail[2:]
            command = tail[0]
            if command == "run":
                row = container("api", target=True, oneoff=True)
                row.update(id="migration-id", running=True, status="running")
                self.rows[tail[tail.index("--name") + 1]] = row
            elif command == "create":
                assert self.migration_applied and self.generations == [0, 4]
                for service in cutover.WRITERS:
                    self.rows[service] = container(service, target=True)
                if self.failure == "identity":
                    self.rows["api"]["image"] = image("api")
            elif command == "start":
                for service in cutover.WRITERS:
                    self.rows[service].update(running=True, status="running")
            elif command not in ("build", "up"):
                raise AssertionError(joined)
            return ""
        if args[0] == "bash":
            assert all(self.rows[service]["running"] for service in cutover.WRITERS)
            assert all(
                self.rows[service]["image"] == image(service, True) for service in cutover.WRITERS
            )
            return ""
        raise AssertionError(joined)


@pytest.fixture
def protocol(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("GROVE_STATE", str(state))
    monkeypatch.setenv("GROVE_SRC", str(src))
    monkeypatch.setenv("GROVE_CUTOVER_PUBLIC_BASE_URL", "https://grove.test")
    seed = tmp_path / "seed.lock"
    seed.touch(mode=0o600)
    smoke = tmp_path / "requested-smoke.sh"
    smoke.write_text("exit 0\n")
    commands = Commands()
    instance = cutover.Cutover(
        TARGET,
        BASE,
        "request8-test",
        seed,
        smoke,
        hashlib.sha256(smoke.read_bytes()).hexdigest(),
        commands,
    )
    return instance, commands


def test_cutover_excludes_old_writers_and_checks_actual_images_before_release(protocol):
    instance, commands = protocol
    seed_inode = instance.seed.stat().st_ino
    neo4j = copy.deepcopy(commands.rows["neo4j"])
    instance.execute()
    assert commands.generations == [0, 4]
    assert instance.seed.stat().st_ino == seed_inode
    assert commands.rows["neo4j"] == neo4j
    assert not cutover.state_path().exists()
    result = json.loads((instance.p["state"] / "cutover-last.json").read_text())
    assert result["stage"] == "complete"
    assert result["transaction_completion"] == "confirmed"
    assert result["migration_terminal"] == "exited"
    assert (instance.p["state"] / "deployed.sha").read_text().strip() == TARGET
    assert (instance.p["state"] / "previous.sha").read_text().strip() == BASE
    assert all(
        commands.rows[name]["restart"]["Name"] == "unless-stopped" for name in cutover.WRITERS
    )
    calls = [" ".join(call) for call, _ in commands.calls]
    stop_runner = next(i for i, call in enumerate(calls) if "stop --time 40 old-runner" in call)
    stop_api = next(
        i for i, call in enumerate(calls) if "stop --time 30 old-gateway old-api" in call
    )
    migrate = next(i for i, call in enumerate(calls) if call.endswith("grove.writer.migrate"))
    assert stop_runner < stop_api < migrate
    assert sum(call.endswith("grove.writer.migrate") for call in calls) == 1
    assert sum(call.startswith("bash ") for call in calls) == 2
    assert not any(" rm " in call or " prune " in call or " down" in call for call in calls)


@pytest.mark.parametrize("failure", ["surviving", "transactions", "migration", "identity", "smoke"])
def test_cutover_retains_failure_and_refuses_automatic_retry(protocol, failure):
    instance, commands = protocol
    commands.failure = failure
    with pytest.raises(cutover.Refused):
        instance.execute()
    state = json.loads(cutover.state_path().read_text())
    assert state["stage"] == "failed"
    assert state["transaction_completion"] == "unknown"
    assert cutover.state_path().stat().st_mode & 0o777 == 0o600
    assert not (instance.p["state"] / "deployed.sha").exists()
    if failure in ("surviving", "transactions"):
        assert not commands.migration_applied
    count = len(commands.calls)
    with pytest.raises(cutover.Refused, match="maintenance_active"):
        instance.execute()
    assert len(commands.calls) == count
    if failure in ("identity", "smoke"):
        assert all(not commands.rows[name]["running"] for name in cutover.WRITERS)
    if failure == "migration":
        assert commands.migration_applied
        assert state["migration_terminal"] == "exited"
        assert "grove-migrate-request8-test" in commands.rows


@pytest.mark.parametrize("failure", ["helper", "queued", "legacy"])
def test_cutover_refuses_active_or_queued_recovery_before_maintenance(protocol, failure):
    instance, commands = protocol
    commands.failure = failure
    with pytest.raises(cutover.Refused):
        instance.execute()
    assert not cutover.state_path().exists()
    assert not commands.migration_applied


def test_cutover_refuses_unowned_writer(protocol):
    instance, commands = protocol
    commands.rows["foreign"] = {**container("api"), "id": "foreign", "project": "other"}
    with pytest.raises(cutover.Refused, match="unexplained_container"):
        instance.execute()
    assert not commands.migration_applied


def test_cutover_refuses_stale_migration_container(protocol):
    instance, commands = protocol
    commands.rows["migration"] = container("api", oneoff=True)
    commands.rows["migration"]["id"] = "stale-migration"
    with pytest.raises(cutover.Refused, match="unexplained_container"):
        instance.execute()
    assert not commands.migration_applied


def test_cutover_restores_exact_on_failure_restart_policy(protocol):
    instance, commands = protocol
    commands.rows["runner"]["restart"] = {"Name": "on-failure", "MaximumRetryCount": 7}
    instance.execute()
    assert commands.rows["runner"]["restart"] == {"Name": "on-failure", "MaximumRetryCount": 7}


def test_cutover_refuses_changed_request_smoke(protocol):
    instance, commands = protocol
    instance.smoke.write_text("exit 9\n")
    with pytest.raises(cutover.Refused, match="request_smoke_changed"):
        instance.execute()
    assert commands.calls == []


def test_cutover_deadlines_do_not_exceed_proposed_maxima(protocol):
    instance, commands = protocol
    instance.execute()
    for args, seconds in commands.calls:
        if "build" in args or args[-1] == "grove.writer.migrate":
            assert seconds <= 900
        elif "--quiescent-reconcile" in args:
            assert seconds <= 60
        elif args[0] == "bash":
            assert seconds <= 120
        elif "create" in args or "start" in args or "up" in args:
            assert seconds <= 180
        elif "stop" in args:
            assert seconds <= 50
        else:
            assert seconds <= 30


def test_command_deadline_kills_child_group_and_preserves_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_STATE", str(tmp_path))
    cutover.atomic(cutover.state_path(), b'{"stage":"migration_started"}')
    with pytest.raises(cutover.Refused, match="command_deadline"):
        cutover.Commands().run([sys.executable, "-c", "import time; time.sleep(30)"], 0.05)
    with pytest.raises(cutover.Refused, match="maintenance_active"):
        cutover.guard()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_process_loss_and_fresh_process_recovery_keep_barrier(tmp_path, sig):
    state = tmp_path / "cutover.json"
    program = (
        "import sys,time; from pathlib import Path; "
        f"sys.path.insert(0,{str(MODULE.parent)!r}); import cutover; "
        f"cutover.atomic(Path({str(state)!r}),b'{{}}'); print('ready',flush=True); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    child.send_signal(sig)
    child.wait(timeout=5)
    fresh = subprocess.run(
        [sys.executable, str(MODULE), "check"],
        env={**os.environ, "GROVE_STATE": str(tmp_path)},
        capture_output=True,
    )
    assert fresh.returncode == 1
    assert state.read_bytes() == b"{}"


@pytest.mark.parametrize("contents", [b"", b"{broken", b"{}"])
def test_corrupt_or_incomplete_state_blocks_recovery(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("GROVE_STATE", str(tmp_path))
    cutover.state_path().write_bytes(contents)
    with pytest.raises(cutover.Refused, match="maintenance_active"):
        cutover.guard()


def test_atomic_state_syncs_file_and_parent(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(
        cutover.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd))[-1]
    )
    path = tmp_path / "state.json"
    cutover.atomic(path, b"first")
    cutover.atomic(path, b"second")
    assert path.read_bytes() == b"second"
    assert len(synced) == 4
    assert path.stat().st_mode & 0o777 == 0o600


def test_seed_lock_cannot_be_replaced_or_created(tmp_path):
    absent = tmp_path / "absent"
    with pytest.raises(FileNotFoundError), cutover.file_lock(absent, existing=True):
        pass
    real = tmp_path / "real"
    real.touch(mode=0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError), cutover.file_lock(link, existing=True):
        pass


def test_new_guard_directories_sync_every_parent_entry(tmp_path, monkeypatch):
    synced = []
    real = cutover.sync_dir
    monkeypatch.setattr(cutover, "sync_dir", lambda path: (synced.append(path), real(path))[-1])
    folder = tmp_path / "systemd" / "grove-backup.service.d"
    cutover.atomic(folder / "90-cutover.conf", b"guard")
    assert synced == [tmp_path, tmp_path / "systemd", folder]


def test_seed_lock_deadline_is_bounded_and_preserves_inode(tmp_path):
    lock = tmp_path / "seed.lock"
    lock.touch(mode=0o600)
    inode = lock.stat().st_ino
    with cutover.file_lock(lock, existing=True):
        with (
            pytest.raises(cutover.Refused, match="lock_deadline"),
            cutover.file_lock(lock, existing=True, seconds=0),
        ):
            pass
    assert lock.stat().st_ino == inode
