"""One private, shared file lock for the restricted demo's outbound authority.

Every API process must use the same existing inode. Deployment creates the file before
startup and must never replace it while a process serves requests. This is deliberately
smaller than the general privacy/release protocol.
"""

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass

from grove.config import ConfigError


@dataclass(frozen=True)
class DemoConfiguration:
    email_hash: str
    guard_file: str


def configuration() -> DemoConfiguration | None:
    from grove.demo_policy import DemoDenied, configured_email_hash

    try:
        hashed = configured_email_hash()
    except DemoDenied:
        raise ConfigError("demo configuration is invalid") from None
    path = os.environ.get("GROVE_DEMO_GUARD_FILE")
    if hashed is None:
        if path:
            raise ConfigError("demo configuration is incomplete")
        return None
    if not path or not os.path.isabs(path):
        raise ConfigError("demo configuration needs an absolute guard file")
    return DemoConfiguration(hashed, path)


class ReleaseFile:
    def __init__(self, config: DemoConfiguration) -> None:
        self.config = config
        self.fd: int | None = None
        try:
            # flock is advisory and does not modify the inode. A read-only bind mount can
            # therefore share this private file with every API/provision process.
            self.fd = os.open(config.guard_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.check()
        except (OSError, ConfigError):
            self.close()
            raise ConfigError("the demo guard file is unavailable or unsafe") from None

    def check(self) -> None:
        assert self.fd is not None
        held = os.fstat(self.fd)
        named = os.stat(self.config.guard_file, follow_symlinks=False)
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or held.st_mode & 0o077
            or held.st_nlink != 1
            or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
            or configuration() != self.config
        ):
            raise ConfigError("the demo guard file changed or is unsafe")

    def try_shared(self) -> bool:
        assert self.fd is not None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        self.check()
        return True

    def exclusive(self) -> None:
        assert self.fd is not None
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        self.check()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@contextmanager
def exclusive_authority():
    """Cover every OAuth writer transaction, including replay-triggered family revokes."""
    config = configuration()
    if config is None:
        yield
        return
    guard = ReleaseFile(config)
    try:
        guard.exclusive()
        yield
    finally:
        guard.close()
