"""Real local private fixtures; no database, host root, or persistent authority."""

import copy
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from grove import clock
from grove.authority import lease


@pytest.fixture
def private_root():
    # A project-local private fixture, independent of pytest's system temp root.
    with tempfile.TemporaryDirectory(prefix=".authority-test-", dir=Path(__file__).parent) as name:
        parent = Path(name).absolute()
        root = parent / "root"
        root.mkdir(mode=0o700)
        descriptor = os.open(root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        yield root


def expected(root):
    info = root.stat()
    return lease.RootIdentity(info.st_dev, info.st_ino)


def opened(root):
    return lease.open_existing_root(root, expected(root))


def acquire(root, mode):
    context = opened(root)
    return getattr(context, f"acquire_{mode}")(clock.monotonic() + 0.10)


def child_attempt(root, mode):
    identity = expected(root)
    program = """
import sys
from grove import clock
from grove.authority.lease import RootIdentity, LeaseRefused, open_existing_root
context = open_existing_root(sys.argv[1], RootIdentity(int(sys.argv[2]), int(sys.argv[3])))
try:
    with getattr(context, 'acquire_' + sys.argv[4])(clock.monotonic() + 0.10):
        print('acquired')
except LeaseRefused as error:
    print(error.reason)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(root), str(identity.device), str(identity.inode), mode],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert not result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    "holder,contender,answer",
    [
        ("shared", "shared", "acquired"),
        ("shared", "exclusive", "deadline"),
        ("exclusive", "shared", "deadline"),
        ("exclusive", "exclusive", "deadline"),
    ],
)
def test_separate_process_lock_matrix(private_root, holder, contender, answer):
    with acquire(private_root, holder):
        assert child_attempt(private_root, contender) == answer
    assert child_attempt(private_root, "exclusive") == "acquired"


def test_independent_shared_contexts_and_threads(private_root):
    with acquire(private_root, "shared"), acquire(private_root, "shared"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(acquire, private_root, "shared").result(timeout=2)
            other.close()
        with pytest.raises(lease.LeaseRefused, match="^lease:deadline$"):
            acquire(private_root, "exclusive")


def test_reentry_upgrade_downgrade_and_context_copy_refuse(private_root):
    with acquire(private_root, "shared") as context:
        for method in (context.acquire_shared, context.acquire_exclusive):
            with pytest.raises(lease.LeaseRefused, match="^lease:reentry$"):
                method(clock.monotonic() + 1)
        with pytest.raises(lease.LeaseRefused, match="^lease:reentry$"):
            context.__enter__()
        with pytest.raises(lease.LeaseRefused, match="^lease:context$"):
            copy.copy(context).close()
        assert child_attempt(private_root, "exclusive") == "deadline"
    with acquire(private_root, "exclusive") as context:
        with pytest.raises(lease.LeaseRefused, match="^lease:reentry$"):
            context.acquire_shared(clock.monotonic() + 1)


@pytest.mark.parametrize(
    "deadline", [0, -1, float("inf"), float("-inf"), float("nan"), True, "later", 10**1000]
)
def test_bad_deadline_refuses_before_flock_and_closes(private_root, monkeypatch, deadline):
    context = opened(private_root)
    descriptors = [*context._fds, context._lock_fd]
    monkeypatch.setattr(lease.fcntl, "flock", lambda *_: pytest.fail("flock was attempted"))
    with pytest.raises(lease.LeaseRefused, match="^lease:deadline$"):
        context.acquire_shared(deadline)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    with pytest.raises(lease.LeaseRefused, match="^lease:closed$"):
        context.acquire_shared(clock.monotonic() + 1)


def test_one_deadline_does_not_restart_on_contention(private_root, monkeypatch):
    context = opened(private_root)
    ticks = iter([0.0, 0.4, 0.8, 1.0])
    attempts = []

    def contended(*_):
        attempts.append(1)
        raise BlockingIOError(lease.errno.EAGAIN, "busy")

    monkeypatch.setattr(clock, "monotonic", lambda: next(ticks, 1.0))
    monkeypatch.setattr(lease.fcntl, "flock", contended)
    monkeypatch.setattr(lease.time, "sleep", lambda _: None)
    with pytest.raises(lease.LeaseRefused, match="^lease:deadline$"):
        context.acquire_exclusive(1.0)
    assert len(attempts) == 2
    assert context._state == "closed"


def test_same_context_cannot_close_or_check_during_acquisition(private_root, monkeypatch):
    context = opened(private_root)
    real_flock = lease.fcntl.flock

    def reenter(*args):
        for method in (context.close, context.check_identity, context.__enter__):
            with pytest.raises(lease.LeaseRefused, match="^lease:reentry$"):
                method()
        real_flock(*args)

    monkeypatch.setattr(lease.fcntl, "flock", reenter)
    with context.acquire_shared(clock.monotonic() + 1):
        assert context.root_identity == expected(private_root)


def test_replacement_before_after_acquire_and_release(private_root, monkeypatch):
    def replace_lock():
        replacement = private_root / "replacement"
        replacement.touch(mode=0o600)
        replacement.replace(private_root / ".lock")

    context = opened(private_root)
    replace_lock()
    with pytest.raises(lease.LeaseRefused, match="^lease:identity$"):
        context.acquire_shared(clock.monotonic() + 1)
    context = opened(private_root)
    real_flock = lease.fcntl.flock

    def swap_after_flock(*args):
        real_flock(*args)
        replace_lock()

    with monkeypatch.context() as scoped:
        scoped.setattr(lease.fcntl, "flock", swap_after_flock)
        with pytest.raises(lease.LeaseRefused, match="^lease:identity$"):
            context.acquire_shared(clock.monotonic() + 1)
    context = acquire(private_root, "exclusive")
    replace_lock()
    with pytest.raises(lease.LeaseRefused, match="^lease:identity$"):
        context.close()
    assert context._state == "closed"


@pytest.mark.parametrize("change", ["root", "parent", "root_mode", "lock_mode", "hardlink"])
def test_named_identity_and_permissions_are_rechecked(private_root, change):
    context = acquire(private_root, "shared")
    descriptors = [*context._fds, context._lock_fd]
    moved_parent = None
    if change == "root":
        private_root.rename(private_root.with_name("old"))
        private_root.mkdir(mode=0o700)
    elif change == "parent":
        parent = private_root.parent
        moved_parent = parent.with_name(parent.name + "-old")
        parent.rename(moved_parent)
        parent.mkdir(mode=0o700)
    elif change == "root_mode":
        private_root.chmod(0o750)
    elif change == "lock_mode":
        (private_root / ".lock").chmod(0o400)
    else:
        os.link(private_root / ".lock", private_root / "alias")
    try:
        with pytest.raises(lease.LeaseRefused):
            context.check_identity()
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        if moved_parent is not None:
            private_root.parent.rmdir()
            moved_parent.rename(private_root.parent)


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo", "public", "hardlink"])
def test_invalid_existing_lock_refuses_without_creation(private_root, kind):
    lock = private_root / ".lock"
    if kind != "public" and kind != "hardlink":
        lock.unlink()
    if kind == "symlink":
        target = private_root / "target"
        target.touch(mode=0o600)
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(lock, 0o600)
    elif kind == "public":
        lock.chmod(0o640)
    elif kind == "hardlink":
        os.link(lock, private_root / "alias")
    with pytest.raises(lease.LeaseRefused):
        opened(private_root)
    if kind == "missing":
        assert not lock.exists()


def test_root_identity_symlink_parent_and_owner_refuse(private_root, monkeypatch):
    with pytest.raises(lease.LeaseRefused, match="^lease:identity$"):
        lease.open_existing_root(private_root, lease.RootIdentity(0, 0))
    alias = private_root.parent / "alias"
    alias.symlink_to(private_root, target_is_directory=True)
    with pytest.raises(lease.LeaseRefused):
        lease.open_existing_root(alias, expected(private_root))
    parent_alias = private_root.parent / "parent-alias"
    parent_alias.symlink_to(private_root.parent, target_is_directory=True)
    with pytest.raises(lease.LeaseRefused):
        lease.open_existing_root(parent_alias / "root", expected(private_root))
    with monkeypatch.context() as scoped:
        scoped.setattr(lease.os, "geteuid", lambda: private_root.stat().st_uid + 1)
        with pytest.raises(lease.LeaseRefused, match="^lease:permissions$"):
            opened(private_root)
    private_root.chmod(0o750)
    with pytest.raises(lease.LeaseRefused, match="^lease:permissions$"):
        opened(private_root)


def test_context_exit_closes_on_body_failure(private_root):
    context = acquire(private_root, "exclusive")
    with pytest.raises(ValueError, match="^body$"):
        with context:
            raise ValueError("body")
    context.close()
    assert child_attempt(private_root, "exclusive") == "acquired"
    with pytest.raises(lease.LeaseRefused, match="^lease:closed$"):
        context.check_identity()


def test_forked_child_refuses_inherited_lease_without_unlocking_parent(private_root):
    with acquire(private_root, "exclusive") as context:
        child = os.fork()
        if child == 0:
            try:
                context.check_identity()
            except lease.LeaseRefused as error:
                os._exit(0 if error.reason == "process" else 2)
            os._exit(3)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        context.check_identity()
        assert child_attempt(private_root, "exclusive") == "deadline"
    assert child_attempt(private_root, "exclusive") == "acquired"
