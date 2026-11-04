"""Private local file mechanics, with no bootstrap or default resource path.

The session owns candidate handles. Successful replacement means file fsync,
same-directory replace, directory fsync and exact final inspection all finished.
A failed post-replace step requires recovery and never restores old bytes.
"""

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
from uuid import uuid4

from .codec import DecoderBudget
from .legacy_reader import ledger_name
from .publication import (
    MAX_CANDIDATES,
    MAX_CHUNK,
    MAX_ENTRIES,
    MAX_TEMP_BYTES,
    ObservedFile,
    PublicationBindings,
    PublicationRefused,
    refuse,
)

LIMITS = {"manifest": 1048576, "journal": 16000000, "legacy": 16000000}
SLOTS = {"manifest": 3, "journal": 2, "legacy": 1}
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _fixed(stage):
    def decorator(function):
        @wraps(function)
        def checked(*args, **kwargs):
            code = None
            try:
                return function(*args, **kwargs)
            except PublicationRefused as error:
                code = error.code
            except FileNotFoundError:
                code = "missing"
            except OSError:
                code = "io"
            args = kwargs = None
            raise PublicationRefused(code, stage)

        return checked

    return decorator


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink


def _version(info):
    return _identity(info) + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(info, *, private):
    if not stat.S_ISDIR(info.st_mode):
        refuse("file_type", "open")
    if private:
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            refuse("private", "open")
    elif info.st_uid not in (0, os.getuid()) or (
        info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
    ):
        refuse("private", "open")


def _regular(info, device, role, *, empty=False):
    if not stat.S_ISREG(info.st_mode):
        refuse("file_type")
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_dev != device
    ):
        refuse("private")
    if not (0 if empty else 1) <= info.st_size <= LIMITS[role]:
        refuse("capacity")


def _observation(role, info, digest):
    return ObservedFile(
        role,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        digest,
    )


@dataclass(frozen=True, slots=True)
class OwnedCandidate:
    """Opaque identity checked against the creating file context on every use."""

    role: str
    name: str
    observation: ObservedFile
    owner: object


class PublicationFiles:
    @_fixed("open")
    def __init__(self, root, bindings, lease, check):
        if type(bindings) is not PublicationBindings or os.getuid() != os.geteuid():
            refuse("binding", "open")
        if type(root) is not str or not root.startswith("/") or len(root.encode()) > 4096:
            refuse("binding", "open")
        parts = root.split("/")[1:]
        if (
            not parts
            or len(parts) > 64
            or any(not p or p in (".", "..") or len(p.encode()) > 255 or "\0" in p for p in parts)
        ):
            refuse("binding", "open")
        self.bindings, self.lease, self._check = bindings, lease, check
        self._fds, self._links, self._candidates = [], [], {}
        self._pid = os.getpid()
        self._closed = False
        self._bytes = 0
        self._root = self._authority = None
        try:
            parent = os.open("/", FLAGS | os.O_DIRECTORY)
            self._fds.append(parent)
            _directory(os.fstat(parent), private=False)
            for index, part in enumerate(parts):
                child = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=parent)
                self._fds.append(child)
                info = os.fstat(child)
                _directory(info, private=index == len(parts) - 1)
                self._links.append((parent, part, child, info))
                parent = child
            self._root = parent
            info = os.fstat(parent)
            if (info.st_dev, info.st_ino) != (
                bindings.root_identity.device,
                bindings.root_identity.inode,
            ):
                refuse("binding", "open")
            child = os.open("authority", FLAGS | os.O_DIRECTORY, dir_fd=parent)
            self._fds.append(child)
            info = os.fstat(child)
            _directory(info, private=True)
            self._links.append((parent, "authority", child, info))
            self._authority = child
            if (info.st_dev, info.st_ino) != (
                bindings.authority_identity.device,
                bindings.authority_identity.inode,
            ):
                refuse("binding", "open")
            self.check()
            self._inventory(self._root, authority=False)
            self._inventory(self._authority, authority=True)
        except BaseException:
            self._drop()
            raise

    def _inventory(self, directory, *, authority):
        # Iteration is bounded; never materialize an arbitrary directory list.
        with os.scandir(directory) as entries:
            for number, entry in enumerate(entries, 1):
                if number > MAX_ENTRIES:
                    refuse("capacity", "open")
                name = entry.name
                fixed = name in (
                    {".lock", "authority", "authority-manifest.json"} if not authority else set()
                )
                final = (
                    len(name) == 69
                    and name.endswith(".json")
                    and all(c in "0123456789abcdef" for c in name[:64])
                )
                temporary = (
                    name.startswith(".publication-") and name.endswith(".tmp") and len(name) < 100
                )
                if not (fixed or final or temporary):
                    refuse("recovery_required", "open")

    def check(self):
        if self._closed or self._pid != os.getpid():
            refuse("binding", "inspect")
        self._check()
        self.lease.check_identity()
        if self.lease.lock_identity != self.bindings.lock_identity:
            refuse("changed")
        for parent, name, child, saved in self._links:
            if (
                _identity(os.fstat(child))[:5] != _identity(saved)[:5]
                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))[:5]
                != _identity(saved)[:5]
            ):
                refuse("changed")

    def _location(self, role, world_id):
        if role not in LIMITS:
            refuse("binding")
        if role == "manifest":
            return self._root, "authority-manifest.json"
        return (self._authority if role == "journal" else self._root), ledger_name(world_id)

    def _open(self, directory, name, role):
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        _regular(entry, self.bindings.root_identity.device, role)
        fd = os.open(name, FLAGS, dir_fd=directory)
        try:
            info = os.fstat(fd)
            _regular(info, self.bindings.root_identity.device, role)
            if _version(info) != _version(entry):
                refuse("changed")
        except BaseException:
            os.close(fd)
            raise
        return fd, info

    def _observe(self, directory, name, role):
        fd, before = self._open(directory, name, role)
        try:
            digest, size = sha256(), 0
            while size < before.st_size:
                self.check()
                chunk = os.read(fd, min(MAX_CHUNK, before.st_size - size))
                if not chunk:
                    refuse("changed")
                size += len(chunk)
                digest.update(chunk)
            if (
                os.read(fd, 1)
                or _version(os.fstat(fd)) != _version(before)
                or _version(os.stat(name, dir_fd=directory, follow_symlinks=False))
                != _version(before)
            ):
                refuse("changed")
            return _observation(role, before, digest.hexdigest())
        finally:
            os.close(fd)

    @_fixed("inspect")
    def observe(self, role, world_id):
        self.check()
        return self._observe(*self._location(role, world_id), role)

    @_fixed("inspect")
    def confirm(self, observation, world_id):
        if self.observe(observation.role, world_id) != observation:
            refuse("changed")

    @_fixed("stage")
    def candidate(self, candidate):
        if (
            type(candidate) is not OwnedCandidate
            or candidate.owner is not self
            or self._candidates.get(candidate.name) is not candidate
        ):
            refuse("binding", "stage")
        self.check()
        directory = self._authority if candidate.role == "journal" else self._root
        if self._observe(directory, candidate.name, candidate.role) != candidate.observation:
            refuse("changed", "stage")
        return directory, candidate.name

    @contextmanager
    def read(self, role, world_id, budget, *, candidate=None):
        code = None
        try:
            with self._read(role, world_id, budget, candidate=candidate) as captured:
                yield captured
            return
        except PublicationRefused as error:
            code = error.code
        except FileNotFoundError:
            code = "missing"
        except OSError:
            code = "io"
        raise PublicationRefused(code, "inspect")

    @contextmanager
    def _read(self, role, world_id, budget, *, candidate=None):
        """One accounted immutable buffer. No view outlives this budget."""
        if type(budget) is not DecoderBudget:
            refuse("binding")
        self.check()
        directory, name = (
            self.candidate(candidate) if candidate is not None else self._location(role, world_id)
        )
        if candidate is not None and candidate.role != role:
            refuse("binding")
        fd, before = self._open(directory, name, role)
        allocation = None
        raw = None
        try:
            # Charge the mutable capture, immutable result, and one read chunk
            # before allocating. Combined large files may refuse this transient
            # peak; a wire-size limit is never a promise of successful admission.
            allocation = budget.reserve("input", 2 * before.st_size + MAX_CHUNK + 256)
            buffer = bytearray(before.st_size)
            offset = 0
            while offset < before.st_size:
                self.check()
                budget.tick(min(MAX_CHUNK, before.st_size - offset))
                chunk = os.read(fd, min(MAX_CHUNK, before.st_size - offset))
                if not chunk:
                    refuse("changed")
                buffer[offset : offset + len(chunk)] = chunk
                offset += len(chunk)
            if os.read(fd, 1):
                refuse("changed")
            raw = bytes(buffer)
            buffer = chunk = None
            if _version(os.fstat(fd)) != _version(before) or _version(
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            ) != _version(before):
                refuse("changed")
            observation = _observation(role, before, sha256(raw).hexdigest())
            # Transfer input accounting to DecoderBudget.input before yielding.
            allocation.release()
            budget.input(raw, LIMITS[role])
            yield raw, observation
            self.check()
            if self._observe(directory, name, role) != observation:
                refuse("changed")
        finally:
            raw = buffer = chunk = None
            if allocation is not None:
                allocation.release()
            os.close(fd)

    @_fixed("stage")
    def stage_bytes(self, role, raw):
        if role not in LIMITS or type(raw) is not bytes or not 0 < len(raw) <= LIMITS[role]:
            refuse("capacity", "stage")
        return self._stage(
            role, (memoryview(raw)[n : n + MAX_CHUNK] for n in range(0, len(raw), MAX_CHUNK))
        )

    def _stage(self, role, chunks):
        """Internal encoder stream only. Public inputs use stage_bytes."""
        self.check()
        if (
            role not in LIMITS
            or len(self._candidates) >= MAX_CANDIDATES
            or sum(c.role == role for c in self._candidates.values()) >= SLOTS[role]
        ):
            refuse("capacity", "stage")
        directory = self._authority if role == "journal" else self._root
        name = f".publication-{uuid4()}-{role}.tmp"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        size, digest = 0, sha256()
        complete = False
        try:
            for chunk in chunks:
                self.check()
                if type(chunk) not in (bytes, memoryview) or not 0 < len(chunk) <= MAX_CHUNK:
                    refuse("capacity", "stage")
                if (
                    size + len(chunk) > LIMITS[role]
                    or self._bytes + size + len(chunk) > MAX_TEMP_BYTES
                ):
                    refuse("capacity", "stage")
                view = memoryview(chunk)
                offset = 0
                while offset < len(view):
                    self.check()
                    try:
                        written = os.write(fd, view[offset:])
                    except InterruptedError:
                        continue
                    if written <= 0 or written > len(view) - offset:
                        refuse("io", "stage")
                    offset += written
                size += len(chunk)
                digest.update(chunk)
            if not size:
                refuse("candidate", "stage")
            os.fsync(fd)
            info = os.fstat(fd)
            _regular(info, self.bindings.root_identity.device, role)
            if info.st_size != size:
                refuse("changed", "stage")
            # Close failure also prevents admission of a candidate.
            os.close(fd)
            fd = None
            self.check()
            observation = self._observe(directory, name, role)
            if observation.digest != digest.hexdigest() or observation.size != size:
                refuse("changed", "stage")
            handle = OwnedCandidate(role, name, observation, self)
            self._candidates[name] = handle
            self._bytes += size
            complete = True
            return handle
        finally:
            if fd is not None:
                os.close(fd)
            if not complete:
                os.unlink(name, dir_fd=directory)

    @_fixed("stage")
    def replace(self, candidate, expected, world_id):
        self.check()
        directory, name = self.candidate(candidate)
        if expected.role != candidate.role:
            refuse("binding", "stage")
        target_dir, target = self._location(candidate.role, world_id)
        if target_dir != directory:
            refuse("binding", "stage")
        self.confirm(expected, world_id)
        self.check()
        try:
            os.replace(name, target, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            refuse("recovery_required", "stage")
        self._candidates.pop(name)
        self._bytes -= candidate.observation.size
        try:
            os.fsync(directory)
            self.check()
            final = self.observe(candidate.role, world_id)
            if final != candidate.observation:
                # Rename changes ctime on some filesystems. Every other field and
                # the content must still identify the staged inode exactly.
                fields = (
                    "role",
                    "device",
                    "inode",
                    "mode",
                    "owner_uid",
                    "link_count",
                    "size",
                    "mtime_ns",
                    "digest",
                )
                if any(getattr(final, f) != getattr(candidate.observation, f) for f in fields):
                    refuse("changed", "stage")
            return final
        except BaseException:
            refuse("recovery_required", "stage")

    @_fixed("recovery")
    def sync_final(self, observations, world_id):
        """Recovery re-establishes durability only for the exact current files."""
        for observation in observations:
            self.confirm(observation, world_id)
            directory, name = self._location(observation.role, world_id)
            fd, _ = self._open(directory, name, observation.role)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
            self.confirm(observation, world_id)

    @_fixed("stage")
    def discard(self, candidate):
        directory, name = self.candidate(candidate)
        os.unlink(name, dir_fd=directory)
        self._candidates.pop(name)
        self._bytes -= candidate.observation.size

    def _drop(self):
        self._closed = True
        failure = False
        for fd in reversed(self._fds):
            try:
                os.close(fd)
            except OSError:
                failure = True
        self._fds.clear()
        if failure:
            refuse("io", "close")

    def close(self):
        if self._closed:
            return
        # A fork child closes its copies only; it must never unlink parent files.
        if self._pid != os.getpid():
            self._drop()
            return
        failure = False
        try:
            self.check()
            for handle in tuple(self._candidates.values()):
                self.discard(handle)
        except (OSError, PublicationRefused):
            failure = True
        finally:
            self._drop()
        if failure:
            refuse("recovery_required", "close")
