"""Private, write-ahead D14 ledger, kept outside database snapshots.

An approved intent is fsynced before the database transaction commits. A database failure
can leave an extra approved intent, never a committed erasure with no restore instruction.
Recovery applies such an intent too. The file contains no target text or source content.
"""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from grove.store.scope import check_world_id

FORMAT = "grove.forget-ledger.v1"
ENV = "GROVE_FORGET_LEDGER_DIR"


class LedgerUnavailable(RuntimeError):
    """No private and durable ledger is available; the caller must not erase anything."""


def _json(value: Any) -> str:
    def native(part):
        if isinstance(part, datetime | date) or hasattr(part, "isoformat"):
            return part.isoformat()
        raise TypeError("invalid ledger value")

    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=native
    )


def _root(path: Path | str | None) -> Path:
    configured = str(path) if path is not None else os.environ.get(ENV)
    if not configured:
        raise LedgerUnavailable("forget ledger is not configured")
    root = Path(configured)
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        raise LedgerUnavailable("forget ledger path is not private")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LedgerUnavailable("forget ledger directory is not private")
    except OSError:
        raise LedgerUnavailable("forget ledger directory is unavailable") from None
    return root


def _name(world_id: str) -> str:
    check_world_id(world_id)
    return hashlib.sha256(world_id.encode()).hexdigest() + ".json"


@contextmanager
def locked_ledger(path: Path | str | None):
    root = _root(path)
    try:
        fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield root
    except OSError:
        raise LedgerUnavailable("forget ledger lock is unavailable") from None


def _read(root: Path, world_id: str, *, missing: bool = False) -> list[dict]:
    path = root / _name(world_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing:
            return []
        raise LedgerUnavailable("latest forget ledger is absent") from None
    except OSError:
        raise LedgerUnavailable("latest forget ledger is unavailable") from None
    try:
        with os.fdopen(fd, "r") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16_000_000:
                raise ValueError
            data = json.load(handle)
        rows = data["rows"]
        if (
            data["format"] != FORMAT
            or data["world_id"] != world_id
            or not isinstance(rows, list)
            or data["count"] != len(rows)
            or data["sha256"] != hashlib.sha256(_json(rows).encode()).hexdigest()
            or len({row["id"] for row in rows}) != len(rows)
        ):
            raise ValueError
        return rows
    except (ValueError, TypeError, KeyError):
        raise LedgerUnavailable("latest forget ledger failed validation") from None


def read_ledger(path: Path | str, world_id: str) -> list[dict]:
    with locked_ledger(path) as root:
        return _read(root, world_id)


def append_intent(root: Path, world_id: str, row: dict, database_rows: list[dict]) -> None:
    """Caller holds locked_ledger. Refuse a missing older intent; never repair by dropping it."""
    rows = _read(root, world_id, missing=not database_rows)
    indexed = {part["id"]: part for part in rows}
    for prior in database_rows:
        if prior["id"] not in indexed or _json(prior) != _json(indexed[prior["id"]]):
            raise LedgerUnavailable("latest forget ledger does not cover the database")
    if row["id"] in indexed:
        if _json(row) != _json(indexed[row["id"]]):
            raise LedgerUnavailable("approved intent changed during retry")
        return
    rows.append(row)
    payload = _json(
        {
            "format": FORMAT,
            "world_id": world_id,
            "count": len(rows),
            "rows": rows,
            "sha256": hashlib.sha256(_json(rows).encode()).hexdigest(),
        }
    )
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".ledger-", dir=root)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / _name(world_id))
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise LedgerUnavailable("forget intent could not be persisted") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
