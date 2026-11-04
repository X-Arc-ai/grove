#!/usr/bin/env python3
"""Read-only filesystem prerequisites for the API's durable erasure journal.

This does not establish database equality or authorize journal bootstrap/recovery.
The canonical application and separately authorized readiness checks remain required.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

API_UID = 10001
MAX_FILES = 256
MAX_ROWS = 32768
MAX_FILE_BYTES = 16_000_000
MAX_TOTAL_BYTES = 32_000_000
ENV = "GROVE_FORGET_LEDGER_HOST_DIR"


class Refused(RuntimeError):
    pass


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refused("journal_duplicate_key")
        value[key] = item
    return value


def identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextlib.contextmanager
def directory(configured):
    if not configured:
        raise Refused("journal_not_configured")
    raw = str(configured)
    root = Path(raw)
    if not root.is_absolute() or raw != str(root) or ".." in root.parts or root == Path("/"):
        raise Refused("journal_path_invalid")
    # Walk by descriptors. No component can resolve through a symlink.
    with contextlib.ExitStack() as stack:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        chain = []
        for part in root.parts[1:]:
            parent = fd
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            chain.append((parent, part, fd))
        info = os.fstat(fd)
        if info.st_uid != API_UID or stat.S_IMODE(info.st_mode) != 0o700:
            raise Refused("journal_directory_private_api_owner_required")
        yield fd
        if identity(info) != identity(os.fstat(fd)):
            raise Refused("journal_directory_changed")
        for parent, part, child in chain:
            named = os.stat(part, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise Refused("journal_path_changed")


@contextlib.contextmanager
def private_file(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != API_UID
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise Refused("journal_file_private_api_owner_required")
        yield fd, info
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if identity(info) != identity(os.fstat(fd)) or identity(info) != identity(named):
            raise Refused("journal_file_changed")
    finally:
        os.close(fd)


def envelope(data, name):
    def invalid_constant(_):
        raise Refused("journal_json_invalid")

    value = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict) or set(value) != {
        "format",
        "world_id",
        "rows",
        "count",
        "sha256",
    }:
        raise Refused("journal_envelope_invalid")
    world, rows = value["world_id"], value["rows"]
    if (
        value["format"] != "grove.forget-ledger.v1"
        or not isinstance(world, str)
        or not 1 <= len(world) <= 256
        or not isinstance(rows, list)
        or type(value["count"]) is not int
        or value["count"] != len(rows)
        or len(rows) > MAX_ROWS
    ):
        raise Refused("journal_envelope_invalid")
    if name != hashlib.sha256(world.encode()).hexdigest() + ".json":
        raise Refused("journal_filename_invalid")
    identifiers = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not row["id"]
            or row["id"] in identifiers
        ):
            raise Refused("journal_rows_invalid")
        identifiers.add(row["id"])
    canonical = json.dumps(
        rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    if value["sha256"] != hashlib.sha256(canonical.encode()).hexdigest():
        raise Refused("journal_checksum_invalid")
    return len(rows)


def check(configured):
    try:
        with directory(configured) as root, private_file(root, ".lock") as (lock, _):
            # Existing lock only. Do not create, truncate, chmod, or wait.
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with os.scandir(root) as entries:
                names = []
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > MAX_FILES + 1:
                        raise Refused("journal_file_limit")
            if ".lock" not in names:
                raise Refused("journal_lock_absent")
            files = sorted(name for name in names if name != ".lock")
            if any(not re.fullmatch(r"[0-9a-f]{64}\.json", name) for name in files):
                raise Refused("journal_unrecognized_entry")
            total_bytes = total_rows = 0
            for name in files:
                with private_file(root, name) as (fd, info):
                    total_bytes += info.st_size
                    if not 0 < info.st_size <= MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                        raise Refused("journal_byte_limit")
                    with os.fdopen(os.dup(fd), "rb") as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != info.st_size:
                        raise Refused("journal_file_changed")
                    total_rows += envelope(data, name)
                    if total_rows > MAX_ROWS:
                        raise Refused("journal_row_limit")
            with os.scandir(root) as entries:
                # The first census is bounded; refuse a new entry immediately.
                remaining = set(names)
                for entry in entries:
                    if entry.name not in remaining:
                        raise Refused("journal_directory_changed")
                    remaining.remove(entry.name)
                if remaining:
                    raise Refused("journal_directory_changed")
        return {"files": len(files), "rows": total_rows}
    except Refused:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise Refused("journal_unavailable_or_invalid") from None


def main():
    try:
        check(os.environ.get(ENV))
    except Refused as error:
        # Fixed reason codes only; never emit the path, world, or journal content.
        print("materials_preflight_refused: " + str(error), file=sys.stderr)
        return 1
    print("materials_preflight_ok: filesystem_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
