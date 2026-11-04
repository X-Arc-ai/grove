#!/usr/bin/env python3
"""Small host-only migration barrier. No automatic retry or baseline rollback.

The shell deploy owns the host lock. This process owns recovery admission and
the existing seed lock. The installed copy outlives checkout replacement.
"""

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Use the same clock provider as the application. Installation copies this
# standard-library-only module beside the guard, outside the moving checkout.
CLOCK_SOURCE = Path(__file__).with_name("grove-clock.py")
if not CLOCK_SOURCE.is_file():
    CLOCK_SOURCE = Path(__file__).resolve().parents[1] / "server/grove/clock.py"
_clock_spec = importlib.util.spec_from_file_location("grove_cutover_clock", CLOCK_SOURCE)
clock = importlib.util.module_from_spec(_clock_spec)
_clock_spec.loader.exec_module(clock)
PREFLIGHT_SOURCE = Path(__file__).with_name("grove-materials-preflight.py")
if not PREFLIGHT_SOURCE.is_file():
    PREFLIGHT_SOURCE = Path(__file__).with_name("materials_preflight.py")

WRITERS = ("runner", "gateway", "api")
UNITS = ("grove-services.service", "grove-backup.service")
SHA = re.compile(r"[0-9a-f]{40}")


class Refused(RuntimeError):
    pass


def paths():
    return {
        "src": Path(os.environ.get("GROVE_SRC", "/opt/grove/src")),
        "state": Path(os.environ.get("GROVE_STATE", "/var/lib/grove")),
        "systemd": Path(os.environ.get("GROVE_SYSTEMD_DIR", "/etc/systemd/system")),
        "installed": Path(
            os.environ.get("GROVE_CUTOVER_INSTALLED", "/usr/local/libexec/grove-cutover.py")
        ),
    }


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path):
    if path.is_dir():
        return
    durable_directory(path.parent)
    path.mkdir(mode=0o700)
    sync_dir(path.parent)


def atomic(path, data, mode=0o600):
    durable_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".cutover-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_path():
    return paths()["state"] / "cutover.json"


def guard():
    # Even an empty, corrupt or dangling state path refuses recovery.
    if os.path.lexists(state_path()):
        raise Refused("maintenance_active")


@contextlib.contextmanager
def file_lock(path, *, existing=False, shared=False, seconds=30):
    flags = os.O_RDONLY if existing else os.O_RDWR | os.O_CREAT
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_nlink != 1:
            raise Refused("unsafe_lock_file")
        end = clock.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock.monotonic() >= end:
                    raise Refused("lock_deadline") from None
                time.sleep(0.05)
        named = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
            raise Refused("lock_inode_changed")
        yield fd
    finally:
        os.close(fd)


class Commands:
    def __init__(self, seconds=3000):
        self.deadline = clock.monotonic() + seconds
        if os.environ.get("GROVE_CUTOVER_DEADLINE"):
            self.deadline = min(self.deadline, float(os.environ["GROVE_CUTOVER_DEADLINE"]))

    def run(self, args, seconds=30, *, capture=True):
        remaining = min(seconds, self.deadline - clock.monotonic())
        if remaining <= 0:
            raise Refused("attempt_deadline")
        # Output stays private in memory. Never echo docker configuration or errors.
        proc = subprocess.Popen(
            [str(a) for a in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise Refused("command_deadline") from None
        if proc.returncode:
            raise Refused("command_failed")
        return output.decode() if output is not None else ""


def install():
    p = paths()
    preflight = PREFLIGHT_SOURCE.read_bytes()
    durable_directory(p["state"])
    atomic(p["installed"].with_name("grove-clock.py"), CLOCK_SOURCE.read_bytes(), 0o600)
    atomic(p["installed"].with_name("grove-materials-preflight.py"), preflight, 0o600)
    atomic(p["installed"], Path(__file__).read_bytes(), 0o700)
    # Drop-ins survive the old parent's install-host.sh fallback. ExecStopPost
    # needs its own guard: systemd still calls it when ExecStart is refused.
    for unit, action in ((UNITS[0], "services-up"), (UNITS[1], "backup")):
        body = "[Service]\nExecStart=\nExecStart=/usr/bin/python3 {} recover {}\n".format(
            p["installed"], action
        )
        if action == "backup":
            body += "ExecStopPost=\nExecStopPost=/usr/bin/python3 {} recover services-up\n".format(
                p["installed"]
            )
        atomic(p["systemd"] / (unit + ".d") / "90-cutover.conf", body.encode(), 0o644)
    Commands().run(["systemctl", "daemon-reload"])


def recover(action):
    p = paths()
    guard()
    # Admission stays held until the legacy script exits, including its traps.
    # That script alone owns the host lock; there is no nested host lock here.
    with file_lock(p["state"] / "cutover-admission.lock", shared=True):
        guard()
        env_backup = dict(os.environ)
        try:
            os.environ.update(GROVE_SERVICES_WAIT_SECONDS="30", GROVE_BACKUP_WAIT_SECONDS="30")
            Commands(2900).run([p["src"] / "deploy" / (action + ".sh")], 2800, capture=False)
        finally:
            os.environ.clear()
            os.environ.update(env_backup)


def host_lock_held():
    if os.environ.get("GROVE_LOCK_HELD") != "1":
        raise Refused("host_lock_required")
    try:
        held = os.fstat(9)
        named = os.stat(os.environ.get("GROVE_LOCK_FILE", "/run/grove/deploy.lock"))
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise Refused("host_lock_identity")
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise Refused("host_lock_required") from None


def unique_manifest_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("baseline_manifest_duplicate_key")
        result[key] = value
    return result


@contextlib.contextmanager
def private_baseline_file(path):
    raw = str(path)
    path = Path(raw)
    if not path.is_absolute() or raw != str(path) or ".." in path.parts or path == Path("/"):
        raise Refused("baseline_path_invalid")
    with contextlib.ExitStack() as stack:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        chain = []
        for part in path.parts[1:-1]:
            parent = fd
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            chain.append((parent, part, fd))
        parent = fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise Refused("baseline_file_not_private")
        yield fd, before
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, key) != getattr(info, key)
            for info in (os.fstat(fd), named)
            for key in fields
        ):
            raise Refused("baseline_file_changed")
        for parent, part, child in chain:
            named, opened = os.stat(part, dir_fd=parent, follow_symlinks=False), os.fstat(child)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise Refused("baseline_path_changed")


def container_snapshot(row):
    return {
        **{
            key: row[key]
            for key in (
                "id",
                "name",
                "project",
                "service",
                "image",
                "running",
                "status",
                "health",
                "restart",
                "restart_count",
            )
        },
        "oneoff": row["oneoff"].lower(),
        "networks": sorted(row["networks"]),
    }


class BaselineManifest:
    """Exact observation binding, never a deployment grant or service allowlist."""

    def __init__(self, path, digest, *, target, baseline, request, smoke_hash, seed, state):
        if not path or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused("baseline_manifest_binding_required")
        self.path, self.digest = path, digest
        self.data = json.loads(self.read(), object_pairs_hook=unique_manifest_object)
        data = self.data
        expected = {
            "version",
            "target",
            "baseline",
            "request",
            "smoke_sha256",
            "locks",
            "services",
            "retained",
        }
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or type(data["version"]) is not int
            or data["version"] != 1
            or (data["target"], data["baseline"], data["request"], data["smoke_sha256"])
            != (target, baseline, request, smoke_hash)
        ):
            raise Refused("baseline_manifest_request_mismatch")
        self.lock_paths = {
            "host": os.environ.get("GROVE_LOCK_FILE", "/run/grove/deploy.lock"),
            "admission": str(state / "cutover-admission.lock"),
            "seed": str(seed),
        }
        if not isinstance(data["locks"], dict) or set(data["locks"]) != set(self.lock_paths):
            raise Refused("baseline_manifest_locks_invalid")
        if not isinstance(data["services"], dict) or set(data["services"]) != {*WRITERS, "neo4j"}:
            raise Refused("baseline_manifest_services_invalid")
        if not isinstance(data["retained"], list) or len(data["retained"]) > 32:
            raise Refused("baseline_manifest_retained_invalid")
        all_rows = []
        for name, binding in data["services"].items():
            if not isinstance(binding, dict) or set(binding) != {"revision", "container"}:
                raise Refused("baseline_manifest_service_invalid")
            revision, row = binding["revision"], binding["container"]
            self.validate_snapshot(row)
            if row["service"] != name or row["oneoff"] != "false":
                raise Refused("baseline_manifest_service_invalid")
            if name == "neo4j":
                if revision is not None or not row["running"] or row["health"] != "healthy":
                    raise Refused("baseline_manifest_store_invalid")
            elif not isinstance(revision, str) or not SHA.fullmatch(revision):
                raise Refused("baseline_manifest_revision_invalid")
            all_rows.append(row)
        for row in data["retained"]:
            self.validate_snapshot(row)
            if (
                row["oneoff"] != "true"
                or row["running"]
                or row["status"] != "exited"
                or row["restart"] != {"Name": "no", "MaximumRetryCount": 0}
            ):
                raise Refused("baseline_manifest_retained_not_stopped")
            all_rows.append(row)
        for key in ("id", "name"):
            if len({row[key] for row in all_rows}) != len(all_rows):
                raise Refused("baseline_manifest_duplicate_container")
        self.retained = {row["id"]: row for row in data["retained"]}
        self.verify_locks()

    @staticmethod
    def validate_snapshot(row):
        keys = {
            "id",
            "name",
            "project",
            "service",
            "image",
            "running",
            "status",
            "health",
            "restart",
            "restart_count",
            "oneoff",
            "networks",
        }
        if not isinstance(row, dict) or set(row) != keys:
            raise Refused("baseline_manifest_container_invalid")
        if (
            not isinstance(row["id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["id"])
            or not isinstance(row["image"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["image"])
            or not isinstance(row["name"], str)
            or not re.fullmatch(r"/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", row["name"])
            or row["project"] != "grove"
            or row["service"] not in (*WRITERS, "neo4j")
            or type(row["running"]) is not bool
            or row["status"] not in ("running", "exited", "created")
            or row["running"] != (row["status"] == "running")
            or row["health"] not in (None, "healthy", "unhealthy", "starting")
            or row["oneoff"] not in ("true", "false")
            or type(row["restart_count"]) is not int
            or row["restart_count"] < 0
        ):
            raise Refused("baseline_manifest_container_invalid")
        policy, networks = row["restart"], row["networks"]
        if (
            not isinstance(policy, dict)
            or set(policy) != {"Name", "MaximumRetryCount"}
            or policy["Name"] not in ("no", "always", "unless-stopped", "on-failure")
            or type(policy["MaximumRetryCount"]) is not int
            or policy["MaximumRetryCount"] < 0
            or not isinstance(networks, list)
            or len(networks) > 16
            or any(
                not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name)
                for name in networks
            )
            or networks != sorted(set(networks))
        ):
            raise Refused("baseline_manifest_container_invalid")

    def read(self):
        with private_baseline_file(self.path) as (fd, info):
            if not 0 < info.st_size <= 65536:
                raise Refused("baseline_manifest_size_invalid")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(65537)
            if len(data) != info.st_size or hashlib.sha256(data).hexdigest() != self.digest:
                raise Refused("baseline_manifest_hash_mismatch")
        return data

    def verify_locks(self):
        identities = set()
        for name, path in self.lock_paths.items():
            binding = self.data["locks"][name]
            if (
                not isinstance(binding, dict)
                or set(binding) != {"path", "device", "inode"}
                or binding["path"] != path
                or type(binding["device"]) is not int
                or type(binding["inode"]) is not int
            ):
                raise Refused("baseline_manifest_lock_mismatch")
            with private_baseline_file(path) as (_, info):
                identity = (info.st_dev, info.st_ino)
                if identity != (binding["device"], binding["inode"]) or identity in identities:
                    raise Refused("baseline_manifest_lock_mismatch")
                identities.add(identity)

    def verify(self):
        self.read()
        self.verify_locks()


class Cutover:
    def __init__(
        self,
        target,
        baseline,
        request,
        seed,
        smoke,
        smoke_hash,
        commands=None,
        *,
        baseline_manifest=None,
        baseline_manifest_sha256=None,
    ):
        if not SHA.fullmatch(target) or not SHA.fullmatch(baseline):
            raise Refused("full_sha_required")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request):
            raise Refused("request_id_required")
        self.p = paths()
        self.commands = commands or Commands()
        self.seed, self.smoke, self.smoke_hash = Path(seed), Path(smoke), smoke_hash
        if not self.seed.is_absolute() or not self.smoke.is_absolute():
            raise Refused("absolute_request_paths_required")
        self.data = {
            "version": 1,
            "request": request,
            "target": target,
            "baseline": baseline,
            "stage": "preparation",
            "migration": "grove-migrate-" + request,
            "writers": {},
            "target_writers": {},
            "images": {},
        }
        self.override = self.p["state"] / "cutover-compose.json"
        self.manifest = None
        if baseline_manifest is not None or baseline_manifest_sha256 is not None:
            self.manifest = BaselineManifest(
                baseline_manifest,
                baseline_manifest_sha256,
                target=target,
                baseline=baseline,
                request=request,
                smoke_hash=smoke_hash,
                seed=self.seed,
                state=self.p["state"],
            )
            self.data["baseline_manifest_sha256"] = self.manifest.digest

    def save(self, stage=None):
        if stage:
            self.data["stage"] = stage
        atomic(state_path(), (json.dumps(self.data, sort_keys=True) + "\n").encode())

    def run(self, *args, seconds=30):
        return self.commands.run(args, seconds)

    def compose(self, *args, seconds=30, override=False):
        command = [
            "docker",
            "compose",
            "--project-name",
            "grove",
            "--file",
            self.p["src"] / "deploy/compose.yml",
        ]
        if override:
            command += ["--file", self.override]
        return self.run(*command, *args, seconds=seconds)

    def inspect(self, identifier):
        # Never retrieve Config.Env. This projection contains no credentials.
        template = (
            '{"id":{{json .Id}},"image":{{json .Image}},'
            '"name":{{json .Name}},"restart_count":{{json .RestartCount}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
            '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
            '"running":{{json .State.Running}},"status":{{json .State.Status}},'
            '"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}},'
            '"restart":{{json .HostConfig.RestartPolicy}},'
            '"networks":{{json .NetworkSettings.Networks}}}'
        )
        return json.loads(self.run("docker", "inspect", "--format", template, identifier))

    def image(self, service, sha):
        name = "grove-runner" if service == "runner" else "grove-api"
        identifier = self.run(
            "docker", "image", "inspect", "--format", "{{.Id}}", name + ":" + sha
        ).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise Refused("image_identity_invalid")
        return identifier

    def inventory(self, *, migration=False):
        if self.manifest:
            self.manifest.verify()
        identifiers = set(
            self.run(
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=grove",
            ).split()
        )
        for network in ("grove_store", "grove_runner_v2", "grove_runner"):
            identifiers.update(
                self.run(
                    "docker", "ps", "--all", "--quiet", "--filter", "network=" + network
                ).split()
            )
        found = {}
        retained = set()
        for identifier in sorted(identifiers):
            row = self.inspect(identifier)
            if self.manifest and row["id"] in self.manifest.retained:
                if container_snapshot(row) != self.manifest.retained[row["id"]]:
                    raise Refused("retained_container_changed")
                retained.add(row["id"])
                continue
            if migration and row["id"] == self.data.get("migration_id"):
                continue
            service = row["service"]
            if (
                row["project"] != "grove"
                or service not in (*WRITERS, "neo4j")
                or row["oneoff"].lower() != "false"
                or service in found
            ):
                raise Refused("unexplained_container")
            found[service] = row
        if set(found) != {*WRITERS, "neo4j"}:
            raise Refused("incomplete_container_inventory")
        if self.manifest:
            if retained != set(self.manifest.retained):
                raise Refused("retained_container_missing")
            if (
                container_snapshot(found["neo4j"])
                != self.manifest.data["services"]["neo4j"]["container"]
            ):
                raise Refused("store_identity_changed")
        return found

    def verify_baseline(self, rows):
        for service in WRITERS:
            row = rows[service]
            revision = self.data["baseline"]
            if self.manifest:
                binding = self.manifest.data["services"][service]
                revision = binding["revision"]
                if container_snapshot(row) != binding["container"]:
                    raise Refused("baseline_identity_mismatch")
            if row["image"] != self.image(service, revision):
                raise Refused("baseline_identity_mismatch")

    def helpers_idle(self):
        for unit in UNITS:
            values = self.run(
                "systemctl", "show", unit, "--property=ActiveState,MainPID,ControlPID"
            )
            props = dict(line.split("=", 1) for line in values.splitlines() if "=" in line)
            if (
                props.get("ActiveState") not in ("inactive", "failed")
                or props.get("MainPID") != "0"
                or props.get("ControlPID") != "0"
            ):
                raise Refused("recovery_helper_active")
        jobs = self.run("systemctl", "list-jobs", "--no-legend", "--no-pager")
        if any(unit in jobs for unit in UNITS):
            raise Refused("recovery_helper_queued")
        processes = self.run("ps", "-eo", "args=")
        for name in ("backup.sh", "services-up.sh", "restore-check.sh"):
            if str(self.p["src"] / "deploy" / name) in processes:
                raise Refused("legacy_helper_active")

    def verify_smoke(self):
        if (
            self.smoke.is_symlink()
            or not self.smoke.is_file()
            or not re.fullmatch(r"[0-9a-f]{64}", self.smoke_hash)
        ):
            raise Refused("request_smoke_invalid")
        if hashlib.sha256(self.smoke.read_bytes()).hexdigest() != self.smoke_hash:
            raise Refused("request_smoke_changed")

    def writers_stopped(self, *, migration=False):
        rows = self.inventory(migration=migration)
        for service in WRITERS:
            row = rows[service]
            if (
                row["running"]
                or row["status"] not in ("exited", "created")
                or row["restart"]["Name"] != "no"
                or row["id"] != self.data["writers"][service]["id"]
                or row["image"] != self.data["writers"][service]["image"]
            ):
                raise Refused("writer_not_excluded")
        if not rows["neo4j"]["running"] or rows["neo4j"]["health"] != "healthy":
            raise Refused("store_not_healthy")
        if rows["neo4j"]["id"] != self.data["store_id"]:
            raise Refused("store_identity_changed")

    def candidate_identity(self, *, started):
        rows = self.inventory(migration=True)
        for service in WRITERS:
            row = rows[service]
            if self.manifest and self.data["target_writers"].get(service, row["id"]) != row["id"]:
                raise Refused("target_identity_mismatch")
            self.data["target_writers"][service] = row["id"]
            if (
                row["image"] != self.data["images"][service]
                or row["restart"]["Name"] != "no"
                or row["running"] != started
            ):
                raise Refused("target_identity_mismatch")
            if started and service != "runner" and row["health"] != "healthy":
                raise Refused("target_not_healthy")

    def stop_migration(self):
        identifier = self.data.get("migration_id")
        if identifier:
            self.run("docker", "stop", "--time", "10", identifier, seconds=20)
            row = self.inspect(identifier)
            if row["running"] or row["status"] not in ("exited", "created"):
                raise Refused("migration_terminal_unknown")
            self.data["migration_terminal"] = row["status"]

    def fail(self, reason):
        self.data["failed_stage"] = self.data["stage"]
        self.data["failure"] = reason
        self.save("failed")
        # Cleanup has its own small budget after the attempt deadline. It never
        # starts a container, removes a container, or retries a migration.
        self.commands.deadline = clock.monotonic() + 100
        self.data["transaction_completion"] = "unknown"
        try:
            self.stop_migration()
        except Exception:
            self.data["migration_terminal"] = "unknown"
        for identifier in self.data["target_writers"].values():
            try:
                self.run("docker", "update", "--restart=no", identifier)
                self.run("docker", "stop", "--time", "10", identifier, seconds=20)
            except Exception:
                self.data["target_stop"] = "unknown"
        self.save()

    def execute(self):
        guard()
        self.verify_smoke()
        if self.manifest:
            self.manifest.verify()
        base = os.environ.get("GROVE_CUTOVER_PUBLIC_BASE_URL", "")
        if not re.fullmatch(r"https://[a-z0-9.-]+", base):
            raise Refused("public_base_required")
        with (
            file_lock(
                self.p["state"] / "cutover-admission.lock", existing=self.manifest is not None
            ),
            file_lock(self.seed, existing=True),
        ):
            guard()
            self.helpers_idle()
            rows = self.inventory()
            if not rows["neo4j"]["running"] or rows["neo4j"]["health"] != "healthy":
                raise Refused("store_not_healthy")
            self.data["store_id"] = rows["neo4j"]["id"]
            self.verify_baseline(rows)
            for service in WRITERS:
                row = rows[service]
                policy = row["restart"]
                if policy["Name"] not in (
                    "no",
                    "always",
                    "unless-stopped",
                    "on-failure",
                ):
                    raise Refused("restart_policy_invalid")
                self.data["writers"][service] = {
                    "id": row["id"],
                    "image": row["image"],
                    "restart": policy,
                }
            self.compose("build", "api", "runner", seconds=900)
            self.data["images"] = {
                service: self.image(service, self.data["target"]) for service in WRITERS
            }
            # A build can take minutes. Bind the last observation before service changes too.
            if self.manifest:
                self.helpers_idle()
                self.verify_baseline(self.inventory())
            override = {
                "services": {
                    service: {"restart": "no", "image": image}
                    for service, image in self.data["images"].items()
                }
            }
            atomic(self.override, json.dumps(override).encode())
            self.save("maintenance")
            try:
                self.helpers_idle()
                for service in WRITERS:
                    self.run(
                        "docker",
                        "update",
                        "--restart=no",
                        self.data["writers"][service]["id"],
                    )
                self.run("docker", "stop", "--time", "40", rows["runner"]["id"], seconds=50)
                self.run(
                    "docker",
                    "stop",
                    "--time",
                    "30",
                    rows["gateway"]["id"],
                    rows["api"]["id"],
                    seconds=40,
                )
                self.writers_stopped()
                self.save("writers_excluded")
                name = self.data["migration"]
                self.compose(
                    "run",
                    "--detach",
                    "--no-deps",
                    "-T",
                    "--name",
                    name,
                    "--entrypoint",
                    "python",
                    "api",
                    "-c",
                    "import signal; signal.pause()",
                    override=True,
                )
                migration = self.inspect(name)
                self.data["migration_id"] = migration["id"]
                self.save()
                if (
                    migration["image"] != self.data["images"]["api"]
                    or migration["restart"]["Name"] != "no"
                ):
                    raise Refused("migration_identity_mismatch")
                self.writers_stopped(migration=True)
                self.run(
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-m",
                    "grove.writer.migrate",
                    "--check-quiescent",
                    seconds=30,
                )
                self.save("migration_started")
                self.run(
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-m",
                    "grove.writer.migrate",
                    seconds=900,
                )
                self.save("reconciliation_started")
                self.run(
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-m",
                    "grove.writer.migrate",
                    "--quiescent-reconcile",
                    seconds=60,
                )
                self.run(
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-m",
                    "grove.writer.migrate",
                    "--check-quiescent",
                    seconds=30,
                )
                self.data["transaction_completion"] = "confirmed"
                self.stop_migration()
                self.save("migration_complete")
                self.writers_stopped(migration=True)
                attempt_deadline = self.commands.deadline
                self.commands.deadline = min(attempt_deadline, clock.monotonic() + 180)
                self.compose(
                    "up",
                    "--no-start",
                    "--no-build",
                    "--no-deps",
                    "--force-recreate",
                    "api",
                    "gateway",
                    "runner",
                    override=True,
                    seconds=180,
                )
                self.candidate_identity(started=False)
                self.save("target_created")
                self.compose(
                    "start",
                    "--wait",
                    "--wait-timeout",
                    "170",
                    "api",
                    "gateway",
                    "runner",
                    override=True,
                    seconds=180,
                )
                self.candidate_identity(started=True)
                self.save("target_started")
                self.commands.deadline = min(attempt_deadline, clock.monotonic() + 120)
                self.verify_smoke()
                self.run(
                    "bash",
                    self.p["src"] / "deploy/smoke.sh",
                    base,
                    self.data["target"],
                    seconds=120,
                )
                self.run("bash", self.smoke, self.data["target"], seconds=120)
                self.candidate_identity(started=True)
                self.save("smoke_passed")
                self.commands.deadline = attempt_deadline
                for service, identifier in self.data["target_writers"].items():
                    policy = self.data["writers"][service]["restart"]
                    value = policy["Name"]
                    if value == "on-failure" and policy["MaximumRetryCount"]:
                        value += ":" + str(policy["MaximumRetryCount"])
                    self.run("docker", "update", "--restart=" + value, identifier)
                    if self.inspect(identifier)["restart"] != policy:
                        raise Refused("restart_restore_failed")
                if self.manifest:
                    final = self.inventory(migration=True)
                    for service in WRITERS:
                        row = final[service]
                        if (
                            row["id"] != self.data["target_writers"][service]
                            or row["image"] != self.data["images"][service]
                            or not row["running"]
                            or row["status"] != "running"
                            or row["restart"] != self.data["writers"][service]["restart"]
                        ):
                            raise Refused("target_identity_mismatch")
                if self.data["baseline"] != self.data["target"]:
                    atomic(
                        self.p["state"] / "previous.sha",
                        (self.data["baseline"] + "\n").encode(),
                    )
                atomic(
                    self.p["state"] / "deployed.sha",
                    (self.data["target"] + "\n").encode(),
                )
                self.save("complete")
                os.replace(state_path(), self.p["state"] / "cutover-last.json")
                sync_dir(self.p["state"])
            except BaseException as exc:
                self.fail(str(exc) if isinstance(exc, Refused) else "cutover_interrupted")
                raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "check", "recover", "run", "launch"))
    parser.add_argument("arguments", nargs="*")
    parser.add_argument(
        "--baseline-manifest", default=os.environ.get("GROVE_CUTOVER_BASELINE_MANIFEST")
    )
    parser.add_argument(
        "--baseline-manifest-sha256", default=os.environ.get("GROVE_CUTOVER_BASELINE_SHA256")
    )
    args = parser.parse_args(argv)
    try:
        manifest_supplied = (
            args.baseline_manifest is not None or args.baseline_manifest_sha256 is not None
        )
        if manifest_supplied:
            if (
                args.action not in ("run", "launch")
                or not args.baseline_manifest
                or not args.baseline_manifest_sha256
            ):
                raise Refused("baseline_manifest_binding_required")
            os.environ["GROVE_CUTOVER_BASELINE_MANIFEST"] = args.baseline_manifest
            os.environ["GROVE_CUTOVER_BASELINE_SHA256"] = args.baseline_manifest_sha256
        if args.action == "check":
            guard()
        elif args.action == "install":
            install()
        elif args.action == "recover":
            if args.arguments not in (["backup"], ["services-up"]):
                raise Refused("recovery_action_invalid")
            recover(args.arguments[0])
        elif args.action == "launch":
            guard()
            if len(args.arguments) != 1 or not SHA.fullmatch(args.arguments[0]):
                raise Refused("full_sha_required")
            # The installed copy runs before even a retained old deploy parent.
            # Its failure cannot reach that parent's automatic installer fallback.
            spec = importlib.util.spec_from_file_location(
                "grove_materials_preflight", PREFLIGHT_SOURCE
            )
            preflight = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(preflight)
            if preflight.main() != 0:
                return 78
            os.environ["GROVE_DEPLOY_WAIT_SECONDS"] = "30"
            launcher = Commands()
            # The inner deadline leaves 100 seconds for cleanup before this
            # outer watchdog. Neither timeout permits a container retry.
            os.environ["GROVE_CUTOVER_DEADLINE"] = str(clock.monotonic() + 2800)
            launcher.run(
                [paths()["src"] / "deploy/deploy.sh", args.arguments[0]],
                2900,
                capture=False,
            )
        else:
            host_lock_held()
            if not os.environ.get("GROVE_CUTOVER_DEADLINE"):
                raise Refused("bounded_launch_required")
            if len(args.arguments) != 2:
                raise Refused("target_and_baseline_required")
            target, baseline = args.arguments
            cutover = Cutover(
                target,
                baseline,
                os.environ.get("GROVE_CUTOVER_REQUEST", ""),
                os.environ.get("GROVE_CUTOVER_SEED_LOCK", ""),
                os.environ.get("GROVE_CUTOVER_SMOKE_SCRIPT", ""),
                os.environ.get("GROVE_CUTOVER_SMOKE_SHA256", ""),
                baseline_manifest=args.baseline_manifest,
                baseline_manifest_sha256=args.baseline_manifest_sha256,
            )
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Refused("terminated")))
            cutover.execute()
    except (Refused, OSError, ValueError, KeyError, TypeError, RecursionError):
        print(
            "cutover_refused; preserve maintenance and inspect private state",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
