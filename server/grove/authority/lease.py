"""Local existing-object leases, with no default path or persistence operations.

The caller supplies an absolute path and its expected root identity. Each open
context permits one acquisition. Independent contexts may hold shared leases.
This is not the live demo guard, a distributed lock, or a database protocol.
"""

import errno
import fcntl
import math
import os
import stat
import threading
import time
from dataclasses import dataclass

from grove import clock

from .scalars import validate_count


class LeaseRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"lease:{reason}")


@dataclass(frozen=True, slots=True)
class RootIdentity:
    device: int
    inode: int

    def __post_init__(self) -> None:
        validate_count(self.device)
        validate_count(self.inode)


@dataclass(frozen=True, slots=True)
class LockIdentity:
    device: int
    inode: int

    def __post_init__(self) -> None:
        validate_count(self.device)
        validate_count(self.inode)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private(info: os.stat_result, *, directory: bool) -> None:
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type:
        raise LeaseRefused("object_type")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LeaseRefused("permissions")
    if not directory and info.st_nlink != 1:
        raise LeaseRefused("link_count")


class AuthorityLease:
    """Use open_existing_root; an acquisition context is single-use and PID-bound."""

    def __init__(self) -> None:
        self._fds: list[int] = []
        self._names: list[str] = []
        self._stats: list[os.stat_result] = []
        self._lock_fd: int | None = None
        self._lock_stat: os.stat_result | None = None
        self._pid = os.getpid()
        self._context = self
        self._state = "new"
        self._entered = False
        self._guard = threading.Lock()

    @property
    def root_identity(self) -> RootIdentity:
        self._check_owner()
        if not self._stats:
            raise LeaseRefused("closed")
        return RootIdentity(*_identity(self._stats[-1]))

    @property
    def lock_identity(self) -> LockIdentity:
        self._check_owner()
        if self._lock_stat is None:
            raise LeaseRefused("closed")
        return LockIdentity(*_identity(self._lock_stat))

    def _drop(self) -> None:
        # Close, never LOCK_UN: an inherited descriptor must not unlock its parent.
        descriptors = self._fds[:]
        if self._lock_fd is not None:
            descriptors.append(self._lock_fd)
        self._fds.clear()
        self._lock_fd = None
        self._state = "closed"
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    def _check_owner(self) -> None:
        if self._context is not self:
            raise LeaseRefused("context")
        if self._pid != os.getpid():
            self._drop()
            raise LeaseRefused("process")
        if self._state == "closed":
            raise LeaseRefused("closed")

    def _inspect(self) -> None:
        self._check_owner()
        if self._lock_fd is None or self._lock_stat is None or not self._fds:
            raise LeaseRefused("not_open")
        for index, descriptor in enumerate(self._fds):
            held = os.fstat(descriptor)
            saved = self._stats[index]
            named = (
                os.stat(self._names[index - 1], dir_fd=self._fds[index - 1], follow_symlinks=False)
                if index
                else os.stat("/")
            )
            if not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(held.st_mode):
                raise LeaseRefused("object_type")
            if _identity(held) != _identity(saved) or _identity(named) != _identity(saved):
                raise LeaseRefused("identity")
            if any(
                (info.st_uid, info.st_mode) != (saved.st_uid, saved.st_mode)
                for info in (held, named)
            ):
                raise LeaseRefused("permissions")
        _private(os.fstat(self._fds[-1]), directory=True)
        held = os.fstat(self._lock_fd)
        named = os.stat(".lock", dir_fd=self._fds[-1], follow_symlinks=False)
        for info in (held, named):
            if _identity(info) != _identity(self._lock_stat):
                raise LeaseRefused("identity")
        for info in (held, named):
            _private(info, directory=False)
            if info.st_mode != self._lock_stat.st_mode:
                raise LeaseRefused("permissions")

    def _verify(self) -> None:
        self._check_owner()
        try:
            self._inspect()
        except LeaseRefused:
            self._drop()
            raise
        except OSError:
            self._drop()
            raise LeaseRefused("filesystem") from None

    def check_identity(self) -> None:
        self._check_owner()
        if not self._guard.acquire(blocking=False):
            raise LeaseRefused("reentry")
        try:
            self._verify()
        finally:
            self._guard.release()

    def _acquire(self, operation: int, deadline: float) -> "AuthorityLease":
        self._check_owner()
        if not self._guard.acquire(blocking=False):
            raise LeaseRefused("reentry")
        try:
            if self._state != "new":
                raise LeaseRefused("reentry")
            self._state = "acquiring"
            try:
                try:
                    valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
                except OverflowError:
                    valid_deadline = False
                if not valid_deadline:
                    raise LeaseRefused("deadline")
                self._verify()
                while True:
                    if clock.monotonic() >= deadline:
                        raise LeaseRefused("deadline")
                    self._verify()
                    try:
                        fcntl.flock(self._lock_fd, operation | fcntl.LOCK_NB)
                    except OSError as error:
                        if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EINTR):
                            raise LeaseRefused("lock") from None
                    else:
                        if clock.monotonic() >= deadline:
                            raise LeaseRefused("deadline")
                        self._verify()
                        self._state = "held"
                        return self
                    remaining = deadline - clock.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.005, remaining))
            except BaseException:
                self._drop()
                raise
        finally:
            self._guard.release()

    def acquire_shared(self, deadline: float) -> "AuthorityLease":
        return self._acquire(fcntl.LOCK_SH, deadline)

    def acquire_exclusive(self, deadline: float) -> "AuthorityLease":
        return self._acquire(fcntl.LOCK_EX, deadline)

    def close(self) -> None:
        if self._state == "closed":
            return
        self._check_owner()
        if not self._guard.acquire(blocking=False):
            raise LeaseRefused("reentry")
        try:
            self._verify()
        finally:
            self._drop()
            self._guard.release()

    def __enter__(self) -> "AuthorityLease":
        self._check_owner()
        if not self._guard.acquire(blocking=False):
            raise LeaseRefused("reentry")
        try:
            if self._state != "held" or self._entered:
                raise LeaseRefused("reentry")
            self._verify()
            self._entered = True
            return self
        finally:
            self._guard.release()

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_existing_root(path: str | os.PathLike[str], expected: RootIdentity) -> AuthorityLease:
    """Open only existing objects; retain the ancestor chain for name checks."""
    if type(expected) is not RootIdentity:
        raise LeaseRefused("expected_identity")
    raw = os.fspath(path)
    if type(raw) is not str or not raw.startswith("/") or "\0" in raw:
        raise LeaseRefused("path")
    parts = raw.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise LeaseRefused("path")
    context = AuthorityLease()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY
    try:
        context._fds.append(os.open("/", flags))
        context._stats.append(os.fstat(context._fds[-1]))
        for part in parts:
            context._fds.append(os.open(part, flags, dir_fd=context._fds[-1]))
            context._names.append(part)
            context._stats.append(os.fstat(context._fds[-1]))
        root = context._stats[-1]
        _private(root, directory=True)
        if _identity(root) != (expected.device, expected.inode):
            raise LeaseRefused("identity")
        # NONBLOCK also prevents a replaced FIFO from hanging before fstat can refuse it.
        context._lock_fd = os.open(
            ".lock",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=context._fds[-1],
        )
        context._lock_stat = os.fstat(context._lock_fd)
        _private(context._lock_stat, directory=False)
        context.check_identity()
        return context
    except BaseException as error:
        context._drop()
        if isinstance(error, OSError):
            raise LeaseRefused("filesystem") from None
        raise


def acquire_shared(context: AuthorityLease, deadline: float) -> AuthorityLease:
    return context.acquire_shared(deadline)


def acquire_exclusive(context: AuthorityLease, deadline: float) -> AuthorityLease:
    return context.acquire_exclusive(deadline)


def check_identity(context: AuthorityLease) -> None:
    context.check_identity()


def close(context: AuthorityLease) -> None:
    context.close()
