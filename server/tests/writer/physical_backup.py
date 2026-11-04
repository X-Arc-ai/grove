"""D14 offline backup drill. Runs only against the service of this Validate job.

This is an explicit command, not an automatically collected test. The pure tests in
test_ci_backup.py exercise the refusal boundary without touching Docker. No application
server is started: the scenario alone reads the service after restore preparation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from grove import clock

IMAGE = (
    "neo4j:2026.10.1-community@sha256:"
    "44ad2a2504fd7d27df1d1e976f2d481e4266a0814d3657f2d5df62124ce657f1"
)
URI = "bolt://127.0.0.1:57687"
DATA_LIMIT = 1024 * 1024 * 1024
RUNTIME_LIMIT = 1024 * 1024 * 1024
OUTPUT_LIMIT = 1024 * 1024
ARCHIVE_LIMIT = DATA_LIMIT + 16 * 1024 * 1024
AUX_LIMIT = 256 * 1024 * 1024
ENTRY_LIMIT = 65536
AUX_ENTRY_LIMIT = 8192
INITIAL_CAPACITY = 16 * 1024 * 1024 * 1024
INITIAL_INODES = 1048576
FREE_FLOOR = 1024 * 1024 * 1024
INODE_FLOOR = 65536
TRANSIENT_LIMIT = 2 * 1024 * 1024 * 1024
MANIFEST_LIMIT = 16 * 1024 * 1024
BUFFER_SIZE = 1024 * 1024
DATA_SLOTS = (
    "baseline-data",
    "old-dump",
    "restore-1-data",
    "next-data",
    "next-dump",
    "restore-3-data",
)
ARCHIVE_NAME = "transport.tar"
WORK_SECONDS = 476  # Keep four seconds inside the eight-minute work budget to reap a child.
DATA_SETTING = "server.directories.data"
TX_SETTING = "server.directories.transaction.logs.root"
_HEX = re.compile(r"[0-9a-f]{64}")
_NUMBER = re.compile(r"[0-9]{1,24}")
_NETWORK = re.compile(r"[a-zA-Z0-9_.-]{1,128}")
_INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Config.Image}},'
    '"image_id":{{json .Image}},"networks":{{json .NetworkSettings.Networks}},'
    '"ports":{{json .NetworkSettings.Ports}},"state":{{json .State}}}'
)


class Refused(RuntimeError):
    """A safe diagnostic code. Never include command output or fixture values."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            code = "invalid_refusal_code"
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Refused(code)


@dataclass(frozen=True)
class Identity:
    service: str
    network: str
    run_id: str
    attempt: str
    head: str
    runner_temp: Path

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Identity:
        require(env.get("CI") == "true" and env.get("GITHUB_ACTIONS") == "true", "not_actions")
        require(env.get("GITHUB_JOB") == "validate", "wrong_job")
        require(env.get("GITHUB_REPOSITORY") == "X-Arc-ai/grove", "wrong_repository")
        require(env.get("GITHUB_EVENT_NAME") == "pull_request", "not_pull_request")
        require(env.get("GROVE_STORE_EXCLUSIVE") == "1", "not_exclusive")
        require(env.get("GROVE_SLICE_ID") == "ci-forget", "wrong_slice")
        require(env.get("GROVE_NEO4J_URI") == URI, "wrong_database_uri")
        require(env.get("NEO4J_AUTH") == "none", "unexpected_auth")
        service, network = env.get("GROVE_DRILL_SERVICE_ID", ""), env.get("GROVE_DRILL_NETWORK", "")
        run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
        head = env.get("GROVE_DRILL_HEAD", "")
        require(_HEX.fullmatch(service) is not None, "invalid_service_id")
        require(_NETWORK.fullmatch(network) is not None, "invalid_network_id")
        require(_NUMBER.fullmatch(run_id) is not None, "invalid_run_id")
        require(_NUMBER.fullmatch(attempt) is not None, "invalid_run_attempt")
        require(re.fullmatch(r"[0-9a-f]{40}", head) is not None, "invalid_head")
        raw = Path(env.get("RUNNER_TEMP", ""))
        require(raw.is_absolute() and raw.is_dir() and not raw.is_symlink(), "invalid_runner_temp")
        root = raw.resolve(strict=True)
        require(root != Path("/") and os.access(root, os.W_OK), "invalid_runner_temp")
        return cls(service, network, run_id, attempt, head, root)

    @property
    def container_root(self) -> str:
        return f"/tmp/grove-d14-{self.run_id}-{self.attempt}"


def validate_service(
    identity: Identity, record: dict[str, Any], image_id: str | None = None
) -> str:
    require(record.get("id") == identity.service, "service_identity_changed")
    require(record.get("image") == IMAGE, "wrong_service_image")
    held = record.get("image_id", "")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", held) is not None, "invalid_image_id")
    require(image_id is None or image_id == held, "service_image_changed")
    networks = record.get("networks")
    require(isinstance(networks, dict) and len(networks) == 1, "wrong_service_network")
    key, value = next(iter(networks.items()))
    require(
        identity.network in (key, value.get("NetworkID")),
        "wrong_service_network",
    )
    ports = record.get("ports", {}).get("7687/tcp")
    require(isinstance(ports, list) and bool(ports), "wrong_service_port")
    require(
        all(
            p.get("HostPort") == "57687" and p.get("HostIp") in {"0.0.0.0", "::", "127.0.0.1"}
            for p in ports
        ),
        "wrong_service_port",
    )
    return held


def inside(root: Path, path: Path, *, must_exist: bool = False) -> Path:
    resolved = path.resolve(strict=must_exist)
    require(resolved != root and resolved.is_relative_to(root), "private_path_escape")
    return resolved


@dataclass(frozen=True)
class Usage:
    bytes: int = 0
    entries: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.bytes + other.bytes, self.entries + other.entries)


@dataclass(frozen=True)
class Capacity:
    bytes: int
    inodes: int


def capacity_requirement(charged: Usage, *, initial: bool = False) -> Capacity:
    require(
        type(charged.bytes) is int
        and type(charged.entries) is int
        and charged.bytes >= 0
        and charged.entries >= 0,
        "invalid_capacity_charge",
    )
    return Capacity(
        INITIAL_CAPACITY if initial else max(FREE_FLOOR, INITIAL_CAPACITY - charged.bytes),
        INITIAL_INODES if initial else max(INODE_FLOOR, INITIAL_INODES - charged.entries),
    )


def check_capacity(
    observations: dict[str, Capacity], charged: Usage, *, initial: bool = False
) -> Capacity:
    require(bool(observations), "capacity_unavailable")
    needed = capacity_requirement(charged, initial=initial)
    for found in observations.values():
        require(
            isinstance(found, Capacity)
            and type(found.bytes) is int
            and type(found.inodes) is int
            and found.bytes >= 0
            and found.inodes >= 0,
            "capacity_unavailable",
        )
        require(found.bytes >= needed.bytes, "capacity_bytes")
        require(found.inodes >= needed.inodes, "capacity_inodes")
    return needed


def parse_capacity(output: str) -> Capacity:
    fields = output.split()
    require(
        len(fields) == 3
        and all(value.isascii() and value.isdecimal() and len(value) <= 20 for value in fields),
        "capacity_unavailable",
    )
    blocks, block_size, inodes = map(int, fields)
    require(block_size > 0, "capacity_unavailable")
    return Capacity(blocks * block_size, inodes)


def local_capacity(path: Path) -> Capacity:
    try:
        found = os.statvfs(path)
        require(found.f_frsize > 0, "capacity_unavailable")
        result = Capacity(found.f_bavail * found.f_frsize, found.f_favail)
        require(result.bytes >= 0 and result.inodes >= 0, "capacity_unavailable")
        return result
    except (OSError, AttributeError, ValueError):
        raise Refused("capacity_unavailable") from None


def walk_tree(root: Path, path: Path, *, entry_limit: int | None = None) -> Iterator[Path]:
    """Traverse incrementally; no eager list can precede the entry limit."""
    maximum = ENTRY_LIMIT if entry_limit is None else entry_limit
    count = 0
    pending = [path]
    seen_directories: set[Path] = set()
    while pending:
        entry = pending.pop()
        resolved = inside(root, entry, must_exist=True)
        count += 1
        require(count <= maximum, "tree_entry_limit")
        mode = entry.stat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "tree_special_file")
        yield entry
        if stat.S_ISDIR(mode):
            require(resolved not in seen_directories, "tree_link_cycle")
            seen_directories.add(resolved)
            with os.scandir(entry) as children:
                for child in children:
                    require(count + len(pending) < maximum, "tree_entry_limit")
                    pending.append(Path(child.path))


def measure_tree(root: Path, path: Path, limit: int, *, entry_limit: int | None = None) -> Usage:
    size = count = 0
    for entry in walk_tree(root, path, entry_limit=entry_limit):
        count += 1
        if entry.is_file():
            size += entry.stat().st_size
            require(size <= limit, "copy_size_limit")
    return Usage(size, count)


def tree_size(root: Path, path: Path, limit: int) -> int:
    return measure_tree(root, path, limit).bytes


class BoundedWriter:
    def __init__(self, handle, limit: int | None = None, monitor: Callable[[], None] | None = None):
        self.handle = handle
        self.limit = ARCHIVE_LIMIT if limit is None else limit
        self.monitor = monitor
        self.written = 0

    def write(self, data: bytes) -> int:
        require(self.written + len(data) <= self.limit, "archive_size_limit")
        for offset in range(0, len(data), BUFFER_SIZE):
            if self.monitor is not None:
                self.monitor()
            piece = data[offset : offset + BUFFER_SIZE]
            count = self.handle.write(piece)
            require(count == len(piece), "archive_short_write")
            self.written += count
        return len(data)

    def tell(self) -> int:
        return self.written

    def flush(self) -> None:
        self.handle.flush()


@contextmanager
def archive_tree(
    root: Path,
    source: Path,
    member_root: str,
    uid: int,
    gid: int,
    *,
    monitor: Callable[[], None] | None = None,
):
    """Yield one bounded private tar file; every exit removes that exact file."""
    member = PurePosixPath(member_root)
    require(not member.is_absolute() and ".." not in member.parts, "archive_path_escape")
    require(
        member.parts and all(part not in {"", "."} for part in member.parts), "archive_path_escape"
    )
    require(type(uid) is int and type(gid) is int and uid > 0 and gid > 0, "invalid_service_owner")
    measure_tree(root, source, DATA_LIMIT)
    destination = root / ARCHIVE_NAME
    inside(root, destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", buffering=BUFFER_SIZE) as handle:
            writer = BoundedWriter(handle, monitor=monitor)
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for count in range(1, len(member.parts)):
                    parent = tarfile.TarInfo(str(PurePosixPath(*member.parts[:count])))
                    parent.type, parent.mode = tarfile.DIRTYPE, 0o700
                    parent.uid, parent.gid = uid, gid
                    archive.addfile(parent)
                payload = 0
                for count, path in enumerate(walk_tree(root, source), start=len(member.parts)):
                    require(count <= ENTRY_LIMIT, "tree_entry_limit")
                    require(not path.is_symlink(), "archive_link")
                    info_stat = path.lstat()
                    require(
                        stat.S_ISREG(info_stat.st_mode) or stat.S_ISDIR(info_stat.st_mode),
                        "archive_special_file",
                    )
                    require(
                        not stat.S_ISREG(info_stat.st_mode) or info_stat.st_nlink == 1,
                        "archive_hardlink",
                    )
                    name = str(member / path.relative_to(source).as_posix())
                    info = archive.gettarinfo(str(path), arcname=name)
                    require(info.isfile() or info.isdir(), "archive_special_file")
                    info.uid, info.gid, info.uname, info.gname = uid, gid, "", ""
                    info.mode = 0o700 if path.is_dir() else 0o600
                    info.pax_headers = {}
                    if path.is_file():
                        payload += info.size
                        require(payload <= DATA_LIMIT, "copy_size_limit")
                        with path.open("rb") as source_handle:
                            archive.addfile(info, source_handle)
                    else:
                        archive.addfile(info)
            handle.flush()
            os.fsync(handle.fileno())
        if monitor is not None:
            monitor()
        yield destination
    finally:
        destination.unlink()


def config_text(original: str, overrides: dict[str, str]) -> str:
    require(
        all("\n" not in value and "\r" not in value for value in overrides.values()),
        "invalid_config_value",
    )
    lines = [
        line for line in original.splitlines() if line.partition("=")[0].strip() not in overrides
    ]
    return "\n".join([*lines, *(f"{key}={value}" for key, value in overrides.items())]) + "\n"


def assert_effective_paths(found: dict[str, str], expected_data: str, expected_tx: str) -> None:
    require(found.get(DATA_SETTING) == expected_data, "effective_data_path_mismatch")
    require(found.get(TX_SETTING) == expected_tx, "effective_transaction_path_mismatch")


class Commands:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.deadline = clock.monotonic() + WORK_SECONDS
        self.receipts: list[dict[str, Any]] = []

    def run(
        self,
        tag: str,
        args: list[str],
        *,
        timeout: float = 60,
        data: bytes | None = None,
        file_stdin: Path | None = None,
        monitor: Callable[[], None] | None = None,
        env: dict[str, str] | None = None,
        allowed: tuple[int, ...] = (0,),
    ) -> str:
        remaining = self.deadline - clock.monotonic()
        require(remaining > 0, "drill_deadline")
        require(data is None, "memory_stdin_forbidden")
        deadline = min(self.deadline, clock.monotonic() + timeout)
        index = len(self.receipts)
        stdout, stderr = self.root / f"command-{index}.out", self.root / f"command-{index}.err"
        if file_stdin is not None:
            inside(self.root, file_stdin, must_exist=True)
            require(not file_stdin.is_symlink() and file_stdin.is_file(), "invalid_stdin_file")
            require(file_stdin.stat().st_size <= ARCHIVE_LIMIT, "archive_size_limit")
        if monitor is not None:
            monitor()
        input_context = file_stdin.open("rb") if file_stdin is not None else nullcontext(None)
        with input_context as input_handle, stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(
                args,
                stdin=input_handle if input_handle is not None else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=env,
                start_new_session=True,
            )
            try:
                while True:
                    require(
                        stdout.stat().st_size <= OUTPUT_LIMIT
                        and stderr.stat().st_size <= OUTPUT_LIMIT,
                        "command_output_limit",
                    )
                    if monitor is not None:
                        monitor()
                    if process.poll() is not None:
                        break
                    left = deadline - clock.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        process.wait(timeout=min(0.1, left))
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                self._stop_child(process)
                self.receipts.append({"stage": tag, "exit": process.returncode, "timeout": True})
                raise Refused("command_timeout") from None
            except BaseException:
                self._stop_child(process)
                self.receipts.append(
                    {"stage": tag, "exit": process.returncode, "interrupted": True}
                )
                raise
        self.receipts.append({"stage": tag, "exit": process.returncode})
        require(process.returncode in allowed, "command_failed")
        return stdout.read_text(errors="strict")

    @staticmethod
    def _stop_child(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise Refused("child_cleanup_timeout") from None


@contextmanager
def bounded(seconds: int):
    """Also bound database calls, Python work, and cleanup, not only child commands."""

    def expired(signum, frame):
        raise Refused("drill_deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous)


class Scenario(Protocol):
    def seed(self) -> None: ...
    def forget(self) -> None: ...
    def assert_old_dump_contains_canary(self) -> None: ...
    def restore_loaded(self, stage: str) -> None: ...
    def assert_clean(self, stage: str) -> None: ...
    def reconstruct_clean(self) -> None: ...
    def summary(self) -> dict[str, Any]: ...


class Drill:
    def __init__(self, identity: Identity, root: Path, commands: Commands) -> None:
        self.identity, self.root, self.commands = identity, root, commands
        self.image_id: str | None = None
        self.uid = self.gid = 0
        self.original_config = ""
        self.config_dir = "/var/lib/neo4j/conf"
        self.current_data, self.current_tx = "/data", "/data/transactions"
        self.stage = "preflight"
        self.runtime = root / "runtime"
        self.dumps: list[dict[str, Any]] = []
        self.source_sizes: list[dict[str, Any]] = []
        self.capacity_receipts: list[dict[str, Any]] = []
        self.retained_service: dict[str, Usage] = {}
        self.service_aux = Usage()
        self.service_aux_baseline: Usage | None = None
        self.service_surfaces: dict[str, str] = {}
        self.docker_log: Path | None = None
        self.docker_log_baseline = 0
        self.last_service_capacity: dict[str, Capacity] = {}
        self._last_monitor = 0.0

    def docker(self, tag: str, *args: str, **kwargs: Any) -> str:
        return self.commands.run(tag, ["docker", *args], **kwargs)

    def inspect(self) -> dict[str, Any]:
        value = json.loads(
            self.docker("inspect_service", "inspect", "--format", _INSPECT, self.identity.service)
        )
        self.image_id = validate_service(self.identity, value, self.image_id)
        return value

    def runner_usage(self) -> Usage:
        """Recount retained artifacts. Removing a tar immediately removes its charge."""
        retained, auxiliary = Usage(), Usage()
        count = 0
        with os.scandir(self.root) as children:
            for child in children:
                count += 1
                require(count <= AUX_ENTRY_LIMIT, "auxiliary_entry_limit")
                path = Path(child.path)
                if child.name == "runtime":
                    retained += measure_tree(self.root, path, RUNTIME_LIMIT)
                elif child.name in DATA_SLOTS:
                    retained += measure_tree(self.root, path, DATA_LIMIT)
                elif child.name == ARCHIVE_NAME:
                    require(path.is_file() and not path.is_symlink(), "invalid_archive_file")
                    retained += measure_tree(self.root, path, ARCHIVE_LIMIT, entry_limit=1)
                else:
                    auxiliary += measure_tree(
                        self.root, path, AUX_LIMIT, entry_limit=AUX_ENTRY_LIMIT
                    )
                    require(auxiliary.bytes <= AUX_LIMIT, "auxiliary_size_limit")
                    require(auxiliary.entries <= AUX_ENTRY_LIMIT, "auxiliary_entry_limit")
        return retained + auxiliary

    def charged_usage(self) -> Usage:
        total = self.runner_usage() + self.service_aux
        for held in self.retained_service.values():
            total += held
        if self.retained_service:
            total += Usage(0, 1 + len(self.retained_service))
        return total

    def _record_capacity(
        self, observations: dict[str, Capacity], charged: Usage, *, initial: bool = False
    ) -> None:
        needed = capacity_requirement(charged, initial=initial)
        # The receipt contains fixed labels and numbers, never paths or raw output.
        for label, value in observations.items():
            self.capacity_receipts.append(
                {
                    "stage": self.stage,
                    "surface": label,
                    "free_bytes": value.bytes,
                    "free_inodes": value.inodes,
                    "required_bytes": needed.bytes,
                    "required_inodes": needed.inodes,
                    "charged_bytes": charged.bytes,
                    "charged_entries": charged.entries,
                }
            )
        check_capacity(observations, charged, initial=initial)

    def runner_checkpoint(self, *, initial: bool = False) -> None:
        self._record_capacity(
            {"runner": local_capacity(self.root)}, self.charged_usage(), initial=initial
        )

    def monitor(self) -> None:
        # Command output limits are checked on every poll. Tree walks are throttled.
        now = clock.monotonic()
        require(now < self.commands.deadline, "drill_deadline")
        if now - self._last_monitor >= 0.25:
            self._last_monitor = now
            self.runner_checkpoint()

    def _remote_usage(self, path: str, limit: int, entries: int | None = None) -> Usage:
        entries = ENTRY_LIMIT if entries is None else entries
        output = self.docker(
            "service_tree_entries",
            "exec",
            self.identity.service,
            "find",
            "-L",
            path,
            "-printf",
            "%y %s\n",
        )
        count = size = 0
        for row in output.splitlines():
            fields = row.split()
            require(len(fields) == 2 and fields[0] in {"f", "d"}, "invalid_service_tree")
            raw = fields[1]
            require(raw.isascii() and raw.isdecimal() and len(raw) <= 20, "invalid_service_tree")
            count += 1
            require(count <= entries, "tree_entry_limit")
            if fields[0] == "f":
                size += int(raw)
                require(size <= limit, "copy_size_limit")
        require(count > 0, "invalid_service_tree")
        return Usage(size, count)

    def _service_capacity(self, path: str) -> Capacity:
        return parse_capacity(
            self.docker(
                "service_capacity",
                "exec",
                self.identity.service,
                "stat",
                "-f",
                "-c",
                "%a %S %d",
                path,
            )
        )

    def service_checkpoint(self, *, initial: bool = False) -> None:
        require(bool(self.service_surfaces), "service_capacity_unmeasured")
        # Preserve the original volume, even after a restored tree becomes active.
        self._remote_size("/data", DATA_LIMIT)
        self._remote_usage("/data", DATA_LIMIT)
        for path in self.retained_service:
            self._remote_size(path, DATA_LIMIT)
            self.retained_service[path] = self._remote_usage(path, DATA_LIMIT)
        auxiliary = Usage()
        counted: list[PurePosixPath] = []
        for label, path in self.service_surfaces.items():
            if label in {"service_tmp", "service_data"}:
                continue
            held = PurePosixPath(path)
            if any(held == parent or held.is_relative_to(parent) for parent in counted):
                continue
            usage = self._remote_usage(path, AUX_LIMIT, AUX_ENTRY_LIMIT)
            auxiliary += usage
            counted.append(held)
        if self.docker_log is None:
            raise Refused("unmeasured_log_capacity")
        try:
            info = self.docker_log.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unmeasured_log_capacity")
            auxiliary += Usage(max(0, info.st_size - self.docker_log_baseline), 1)
        except OSError:
            raise Refused("unmeasured_log_capacity") from None
        require(auxiliary.bytes <= AUX_LIMIT, "auxiliary_size_limit")
        require(auxiliary.entries <= AUX_ENTRY_LIMIT, "auxiliary_entry_limit")
        if self.service_aux_baseline is None:
            require(initial, "service_capacity_unmeasured")
            self.service_aux_baseline = auxiliary
        self.service_aux = Usage(
            max(0, auxiliary.bytes - self.service_aux_baseline.bytes),
            max(0, auxiliary.entries - self.service_aux_baseline.entries),
        )
        observations = {
            label: self._service_capacity(path) for label, path in self.service_surfaces.items()
        }
        observations["service_docker_log"] = local_capacity(self.docker_log)
        self.last_service_capacity = observations
        self._record_capacity(observations, self.charged_usage(), initial=initial)
        self.runner_checkpoint(initial=initial)

    def discover_service_surfaces(self) -> None:
        """Resolve only the validated service's actual configuration and log targets."""
        from grove.store.connection import open_driver

        names = ["server.directories.logs", "server.logs.config", "server.logs.user.config"]
        with open_driver(
            connection_timeout=3, connection_acquisition_timeout=3, max_transaction_retry_time=0
        ) as driver:
            with driver.session(database="neo4j") as session:
                rows = session.run(
                    "SHOW SETTINGS YIELD name, value WHERE name IN $names RETURN name, value",
                    names=names,
                ).data()
        found = {row["name"]: row["value"] for row in rows}
        self.service_surfaces = {
            "service_tmp": "/tmp",
            "service_data": "/data",
            "service_config": self.config_dir,
        }
        canonical = {}
        for name in names:
            raw = found.get(name)
            require(isinstance(raw, str) and raw.startswith("/"), "service_log_path_unmeasured")
            value = self.docker(
                "resolve_log_surface", "exec", self.identity.service, "readlink", "-f", raw
            ).strip()
            require(
                value.startswith("/") and value != "/" and "\n" not in value,
                "service_log_path_unmeasured",
            )
            canonical[name] = value
        logs = canonical["server.directories.logs"]
        self.service_surfaces["service_logs"] = logs
        for index, name in enumerate(names[1:]):
            path = canonical[name]
            self.service_surfaces[f"service_log_config_{index}"] = path
            content = self.docker("read_log_config", "exec", self.identity.service, "cat", path)
            try:
                config = ET.fromstring(content)
            except ET.ParseError:
                raise Refused("service_log_config_unmeasured") from None
            for element in config.iter():
                # File names and rotation patterns must resolve under measured logs.
                candidates = [
                    element.attrib[key]
                    for key in ("fileName", "filePattern")
                    if key in element.attrib
                ]
                if element.tag.lower() in {"filename", "filepattern"} and element.text:
                    candidates.append(element.text)
                for candidate in candidates:
                    resolved = candidate.replace("${config:server.directories.logs}", logs)
                    require(
                        "${" not in resolved
                        and PurePosixPath(resolved).is_absolute()
                        and PurePosixPath(resolved).is_relative_to(PurePosixPath(logs)),
                        "service_log_path_unmeasured",
                    )
        record = json.loads(
            self.docker(
                "service_log_identity",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"log":{{json .LogPath}},'
                '"driver":{{json .HostConfig.LogConfig}}}',
                self.identity.service,
            )
        )
        require(record.get("id") == self.identity.service, "service_identity_changed")
        logging = record.get("driver", {})
        require(logging.get("Type") == "json-file", "unmeasured_log_capacity")
        options = logging.get("Config", {})
        require(
            isinstance(options, dict) and options.get("max-size", "-1") == "-1",
            "unmeasured_log_capacity",
        )
        raw = record.get("log")
        require(
            isinstance(raw, str) and raw.startswith("/") and "\n" not in raw,
            "unmeasured_log_capacity",
        )
        # No host traversal or alternate namespace is attempted. If the exact path
        # is not visible in this existing namespace, this capability is refused.
        path = Path(raw)
        try:
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unmeasured_log_capacity")
            self.docker_log_baseline = info.st_size
            local_capacity(path)
        except OSError:
            raise Refused("unmeasured_log_capacity") from None
        self.docker_log = path

    def preflight(self) -> None:
        head = self.commands.run("checkout_head", ["git", "rev-parse", "HEAD"]).strip()
        require(head == self.identity.head, "checkout_head_mismatch")
        record = self.inspect()
        require(record["state"].get("Running") is True, "service_not_running")
        digests = json.loads(
            self.docker(
                "inspect_image",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                self.image_id or "",
            )
        )
        require(
            any(value.endswith(IMAGE.partition("@")[2]) for value in digests or []),
            "image_digest_unverified",
        )
        self.uid = int(
            self.docker("service_uid", "exec", self.identity.service, "id", "-u", "neo4j").strip()
        )
        self.gid = int(
            self.docker("service_gid", "exec", self.identity.service, "id", "-g", "neo4j").strip()
        )
        require(self.uid > 0 and self.gid > 0, "invalid_service_owner")
        self.docker(
            "fresh_service_root",
            "exec",
            self.identity.service,
            "test",
            "!",
            "-e",
            self.identity.container_root,
        )
        self.config_dir = self.docker(
            "service_config_path",
            "exec",
            self.identity.service,
            "readlink",
            "-f",
            "/var/lib/neo4j/conf",
        ).strip()
        require(
            self.config_dir in {"/var/lib/neo4j/conf", "/conf"}, "unexpected_service_config_path"
        )
        self.discover_service_surfaces()
        self.service_checkpoint(initial=True)
        self.runtime.mkdir(mode=0o700)
        for name, source in (
            ("bin", "/var/lib/neo4j/bin"),
            ("lib", "/var/lib/neo4j/lib"),
            ("conf", "/var/lib/neo4j/conf"),
            ("java", "/opt/java/openjdk"),
        ):
            self._remote_size(source, RUNTIME_LIMIT)
            self.runner_checkpoint()
            self.docker(
                "copy_runtime_" + name,
                "cp",
                "-L",
                self.identity.service + ":" + source,
                str(self.runtime / name),
                monitor=self.monitor,
            )
            tree_size(self.root, self.runtime, RUNTIME_LIMIT)
            self.runner_checkpoint()
        self.original_config = (self.runtime / "conf/neo4j.conf").read_text()
        self._admin_config(self.root / "runtime-check-data")
        version = self.commands.run(
            "java_version",
            [str(self.runtime / "java/bin/java"), "--version"],
            env=self._admin_env(),
            monitor=self.monitor,
        )
        require("25." in version, "wrong_java_version")
        version = self.commands.run(
            "admin_version",
            [str(self.runtime / "bin/neo4j-admin"), "--version"],
            env=self._admin_env(),
            monitor=self.monitor,
        )
        require(version.strip() == "2026.10.1", "wrong_admin_version")
        self.check_paths("/data", "/data/transactions")

    def _remote_size(self, path: str, limit: int) -> int:
        output = self.docker("source_size", "exec", self.identity.service, "du", "-sbL", path)
        fields = output.split(maxsplit=1)
        value = fields[0] if fields else ""
        require(
            value.isascii() and value.isdecimal() and len(value) <= 20,
            "invalid_copy_size_output",
        )
        size = int(value)
        self.source_sizes.append(
            {
                "stage": self.stage,
                "kind": "data" if path == self.current_data else "runtime",
                "bytes": size,
                "limit": limit,
            }
        )
        require(size <= limit, "copy_size_limit")
        return size

    def _admin_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "JAVA_HOME": str(self.runtime / "java"),
            "NEO4J_HOME": str(self.runtime),
            "NEO4J_CONF": str(self.runtime / "conf"),
            "HEAP_SIZE": "256m",
            "TMPDIR": str(self.root),
        }

    def _admin_config(self, data: Path) -> None:
        inside(self.root, data)
        directories = {
            DATA_SETTING: data,
            TX_SETTING: data / "transactions",
            "server.directories.logs": self.root / "admin-logs",
            "server.directories.run": self.root / "admin-run",
            "server.directories.import": self.root / "admin-import",
            "server.directories.plugins": self.root / "admin-plugins",
            "server.directories.dumps.root": self.root / "admin-dumps",
        }
        for path in directories.values():
            inside(self.root, path)
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        overrides = {name: str(path) for name, path in directories.items()}
        overrides.update(
            {
                "server.memory.heap.initial_size": "256m",
                "server.memory.heap.max_size": "256m",
                "server.memory.pagecache.size": "256m",
            }
        )
        # A copied server configuration can refer to host-absolute files. The admin
        # process gets an independent configuration whose paths are all private.
        for name, filename in (
            ("server.logs.config", "server-logs.xml"),
            ("server.logs.user.config", "user-logs.xml"),
        ):
            path = self.runtime / "conf" / filename
            if path.exists():
                overrides[name] = str(inside(self.root, path, must_exist=True))
        (self.runtime / "conf/neo4j.conf").write_text(config_text("", overrides))

    def stop(self) -> None:
        require(self.inspect()["state"].get("Running") is True, "service_not_running")
        self.service_checkpoint()
        self.docker("stop_service", "stop", "--timeout", "30", self.identity.service, timeout=45)
        state = self.inspect()["state"]
        require(
            not state.get("Running") and state.get("Status") == "exited", "service_did_not_stop"
        )
        require(
            not state.get("OOMKilled") and state.get("ExitCode") in (0, 143), "unclean_service_stop"
        )

    def check_paths(self, data: str, tx: str) -> None:
        from grove.store.connection import open_driver

        with open_driver(
            connection_timeout=3, connection_acquisition_timeout=3, max_transaction_retry_time=0
        ) as driver:
            with driver.session(database="neo4j") as session:
                rows = session.run(
                    "SHOW SETTINGS YIELD name, value WHERE name IN $names RETURN name, value",
                    names=[DATA_SETTING, TX_SETTING],
                ).data()
        found = {row["name"]: row["value"] for row in rows}
        # Neo4j may report its home-relative symlink. Resolve inside the exact service.
        canonical = {}
        for key in (DATA_SETTING, TX_SETTING):
            value = found.get(key, "")
            require(isinstance(value, str) and value.startswith("/"), "effective_path_absent")
            canonical[key] = self.docker(
                "resolve_effective_path", "exec", self.identity.service, "readlink", "-f", value
            ).strip()
        assert_effective_paths(canonical, data, tx)

    def start(self, data: str, tx: str) -> None:
        require(not self.inspect()["state"].get("Running"), "service_already_running")
        self.docker("start_service", "start", self.identity.service)
        deadline = min(clock.monotonic() + 60, self.commands.deadline)
        while clock.monotonic() < deadline:
            state = self.inspect()["state"]
            require(state.get("Running") is True, "service_exited_at_start")
            status = state.get("Health", {}).get("Status")
            if status == "healthy":
                self.check_paths(data, tx)
                self.current_data, self.current_tx = data, tx
                self.service_checkpoint()
                return
            require(status != "unhealthy", "service_unhealthy")
            time.sleep(0.5)
        raise Refused("service_readiness_timeout")

    def copy_stopped_data(self, destination: Path) -> None:
        require(not self.inspect()["state"].get("Running"), "copy_requires_stopped_service")
        require(not destination.exists(), "copy_destination_exists")
        inside(self.root, destination)
        self.runner_checkpoint()
        self.docker(
            "copy_stopped_data",
            "cp",
            self.identity.service + ":" + self.current_data,
            str(destination),
            monitor=self.monitor,
        )
        tree_size(self.root, destination, DATA_LIMIT)
        self.runner_checkpoint()

    def dump(self, data: Path, destination: Path) -> None:
        inside(self.root, destination)
        destination.mkdir(mode=0o700)
        self._admin_config(data)
        self.runner_checkpoint()
        self.commands.run(
            "admin_dump",
            [
                str(self.runtime / "bin/neo4j-admin"),
                "database",
                "dump",
                "neo4j",
                "--to-path=" + str(destination),
            ],
            timeout=90,
            env=self._admin_env(),
            monitor=self.monitor,
        )
        tree_size(self.root, destination, DATA_LIMIT)
        self.runner_checkpoint()
        files = sorted(destination.glob("*.dump"))
        require(len(files) == 1 and files[0].name == "neo4j.dump", "dump_file_absent")
        hasher = hashlib.sha256()
        with files[0].open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
        self.dumps.append(
            {
                "stage": self.stage,
                "sha256": hasher.hexdigest(),
                "bytes": files[0].stat().st_size,
            }
        )

    def load(self, source: Path, data: Path) -> None:
        require(not data.exists(), "load_destination_exists")
        self._admin_config(data)
        self.runner_checkpoint()
        self.commands.run(
            "admin_load",
            [
                str(self.runtime / "bin/neo4j-admin"),
                "database",
                "load",
                "neo4j",
                "--from-path=" + str(source),
            ],
            timeout=90,
            env=self._admin_env(),
            monitor=self.monitor,
        )
        tree_size(self.root, data, DATA_LIMIT)
        self.runner_checkpoint()

    def select_data(self, local: Path, number: int) -> tuple[str, str]:
        require(not self.inspect()["state"].get("Running"), "select_requires_stopped_service")
        require(number in (1, 2, 3), "invalid_restore_number")
        relative = f"grove-d14-{self.identity.run_id}-{self.identity.attempt}/restore-{number}/data"
        data = "/tmp/" + relative
        tx = data + "/transactions"
        require(bool(self.last_service_capacity), "service_capacity_unmeasured")
        self.runner_checkpoint()
        with archive_tree(
            self.root, local, relative, self.uid, self.gid, monitor=self.monitor
        ) as archive:
            self.runner_checkpoint()
            self.docker(
                "copy_loaded_data",
                "cp",
                "-a",
                "-",
                self.identity.service + ":/tmp",
                file_stdin=archive,
                monitor=self.monitor,
            )
        self.retained_service[data] = measure_tree(self.root, local, DATA_LIMIT)
        self.runner_checkpoint()
        config = self.root / (f"service-config-{number}")
        config.mkdir(mode=0o700)
        (config / "neo4j.conf").write_text(
            config_text(self.original_config, {DATA_SETTING: data, TX_SETTING: tx})
        )
        self.runner_checkpoint()
        with archive_tree(
            self.root, config, "conf", self.uid, self.gid, monitor=self.monitor
        ) as archive:
            self.runner_checkpoint()
            self.docker(
                "select_service_data",
                "cp",
                "-a",
                "-",
                self.identity.service + ":" + str(PurePosixPath(self.config_dir).parent),
                file_stdin=archive,
                monitor=self.monitor,
            )
        self.runner_checkpoint()
        return data, tx

    def execute(self, scenario: Scenario) -> dict[str, Any]:
        self.preflight()
        self.stage = "seed"
        scenario.seed()
        self._remote_size(self.current_data, DATA_LIMIT)
        self.stage = "old_dump"
        self.stop()
        baseline = self.root / "baseline-data"
        self.copy_stopped_data(baseline)
        old_dump = self.root / "old-dump"
        self.dump(baseline, old_dump)
        self.start("/data", "/data/transactions")
        self.stage = "authenticated_forget"
        scenario.forget()
        scenario.assert_clean("live")
        self.stage = "old_dump_load"
        self.stop()
        restored = self.root / "restore-1-data"
        self.load(old_dump, restored)
        self.start(*self.select_data(restored, 1))
        # The negative control observes the old dump before any ledger application.
        scenario.assert_old_dump_contains_canary()
        self.stage = "latest_ledger_replay"
        scenario.restore_loaded("old_dump")
        scenario.assert_clean("old_dump")
        self.stage = "clean_reconstruction"
        self.stop()
        clean = self.root / "restore-2-data"
        clean.mkdir(mode=0o700)
        self.start(*self.select_data(clean, 2))
        scenario.reconstruct_clean()
        scenario.assert_clean("clean_reconstruction")
        self._remote_size(self.current_data, DATA_LIMIT)
        self.stage = "next_dump"
        self.stop()
        next_data = self.root / "next-data"
        self.copy_stopped_data(next_data)
        next_dump = self.root / "next-dump"
        self.dump(next_data, next_dump)
        self.stage = "next_dump_load"
        final = self.root / "restore-3-data"
        self.load(next_dump, final)
        self.start(*self.select_data(final, 3))
        scenario.restore_loaded("next_dump")
        scenario.assert_clean("next_dump")
        self.service_checkpoint()
        self.stage = "complete"
        return scenario.summary()


def main() -> int:
    root: Path | None = None
    drill: Drill | None = None
    commands: Commands | None = None
    report: dict[str, Any] = {"status": "FAIL", "stage": "identity", "physical_acceptance": False}
    os.umask(0o077)
    try:
        identity = Identity.from_env(dict(os.environ))
        root = Path(tempfile.mkdtemp(prefix="grove-d14-", dir=identity.runner_temp))
        require(stat.S_IMODE(root.stat().st_mode) == 0o700, "private_directory_mode")
        commands = Commands(root)
        drill = Drill(identity, root, commands)
        from tests.writer.physical_scenario import make_scenario

        report.update(
            {
                "head": identity.head,
                "run_id": identity.run_id,
                "run_attempt": identity.attempt,
                "image": IMAGE,
                "service_identity_sha256": hashlib.sha256(identity.service.encode()).hexdigest(),
                "application_reads_released": False,
            }
        )
        with bounded(WORK_SECONDS):
            summary = drill.execute(make_scenario(root))
        report.update(
            {
                "status": "PASS",
                "stage": "complete",
                "physical_acceptance": True,
                "scenario": summary,
            }
        )
    except Refused as error:
        report["refusal"] = error.code
    except Exception as error:
        # An assertion may contain the synthetic value. Print only its class.
        report["refusal"] = "unexpected_error"
        report["error_type"] = type(error).__name__
    finally:
        if drill is not None:
            report["stage"] = drill.stage
            report["dumps"] = drill.dumps
            report["source_sizes"] = drill.source_sizes
            report["capacity"] = drill.capacity_receipts
        if commands is not None:
            report["commands"] = commands.receipts
        if root is not None:
            try:
                with bounded(60):
                    inside(identity.runner_temp, root, must_exist=True)
                    shutil.rmtree(root)
                report["private_files_removed"] = True
            except Exception:
                report.update(
                    {
                        "status": "FAIL",
                        "physical_acceptance": False,
                        "private_files_removed": False,
                        "cleanup_error": "private_cleanup_failed",
                    }
                )
        print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
