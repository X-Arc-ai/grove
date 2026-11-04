"""Read-only capture of an existing private ledger. Never current-authority proof.

Descriptor checks detect observed replacement and metadata change. They do not
exclude concurrent writers. No lease, snapshot capability, principal or consent
is acquired here, and no runtime consumer imports this adapter.
"""

import os
import stat
from hashlib import sha256

from .codec import DecoderBudget
from .legacy import MAX_FILE, check_world, decode_legacy
from .records import AuthorityCodecRefused

_CODES = frozenset({"path", "missing", "private", "file_type", "changed", "size", "io"})


class LegacyReadRefused(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else "io"
        super().__init__(self.code)


def _refuse(code):
    raise LegacyReadRefused(code) from None


def ledger_name(world_id):
    """Same exact-world filename algorithm as the published legacy writer."""
    check_world(world_id)
    return sha256(world_id.encode("utf-8")).hexdigest() + ".json"


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def _version(info):
    # Reading may change atime; it is deliberately not part of this comparison.
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(info, *, private):
    if not stat.S_ISDIR(info.st_mode):
        _refuse("file_type")
    if private:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            _refuse("private")
    elif info.st_uid not in {0, os.getuid()} or (
        info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
    ):
        _refuse("private")


def _file(info):
    if not stat.S_ISREG(info.st_mode):
        _refuse("file_type")
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        _refuse("private")
    if not 0 < info.st_size <= MAX_FILE:
        _refuse("size")


def _capture(root, world_id, budget):
    if type(root) is not str or not root.startswith("/") or len(root) > 4096:
        _refuse("path")
    parts = root.split("/")[1:]
    if (
        not parts
        or len(parts) > 64
        or any(
            not part or part in {".", ".."} or len(part) > 255 or "\x00" in part for part in parts
        )
    ):
        _refuse("path")
    name = ledger_name(world_id)
    if type(budget) is not DecoderBudget:
        raise AuthorityCodecRefused("input_type")
    budget.tick()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    metadata = budget.reserve("metadata", 32768)
    descriptors, links = [], []
    reservation = None
    try:
        parent = os.open("/", flags | os.O_DIRECTORY)
        descriptors.append(parent)
        _directory(os.fstat(parent), private=False)
        for index, part in enumerate(parts):
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(child)
            info = os.fstat(child)
            _directory(info, private=index == len(parts) - 1)
            observed = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if _identity(observed) != _identity(info):
                _refuse("changed")
            links.append((parent, part, info))
            parent = child
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _file(entry)
        fd = os.open(name, flags, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        _file(before)
        if _version(entry) != _version(before):
            _refuse("changed")
        if _version(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _version(before):
            _refuse("changed")
        # Reserve before os.read allocates immutable bytes. No bytearray or join
        # keeps a second complete input alive. A short regular-file read refuses.
        reservation = budget.reserve("input", before.st_size + 64)
        budget.tick(before.st_size)
        raw = os.read(fd, before.st_size)
        if len(raw) != before.st_size or os.read(fd, 1):
            _refuse("size")
        after = os.fstat(fd)
        if _version(after) != _version(before) or _version(
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        ) != _version(before):
            _refuse("changed")
        for directory_fd, part, original in links:
            if _identity(os.stat(part, dir_fd=directory_fd, follow_symlinks=False)) != _identity(
                original
            ):
                _refuse("changed")
        return raw
    finally:
        if reservation is not None:
            reservation.release()
        close_failed = False
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                close_failed = True
        metadata.release()
        if close_failed:
            _refuse("io")


def read_legacy(root, *, world_id, budget):
    """Return validated captured bytes only; missing or unsafe paths refuse.

    This function does not use or manufacture a ledger lock. A later trusted
    owner must acquire a real lease/stable snapshot before any live operation.
    """
    code, codec_code, raw = None, None, None
    try:
        raw = _capture(root, world_id, budget)
        return decode_legacy(raw, world_id=world_id, budget=budget)
    except LegacyReadRefused as error:
        code = error.code
    except AuthorityCodecRefused as error:
        codec_code = error.code
    except FileNotFoundError:
        code = "missing"
    except (OSError, ValueError, OverflowError):
        code = "io"
    root = world_id = raw = None
    if codec_code is not None:
        raise AuthorityCodecRefused(codec_code)
    raise LegacyReadRefused(code)
