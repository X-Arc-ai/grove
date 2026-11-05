"""Private, write-ahead D14 ledger, kept outside database snapshots.

An approved intent is fsynced before the database transaction commits. A database failure
can leave an extra approved intent, never a committed erasure with no restore instruction.
Recovery applies such an intent too. The file contains no target text or source content.

A whole-world erasure (grove.writer.world_delete) adds its rows to the same file, because the
deploy preflight refuses any other entry in this folder. The file stays append-only: a finished
erasure ends the rows before it, since the world they name is gone and the new world keeps the id.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from grove.store.errors import ScopeError
from grove.store.scope import check_world_id
from grove.writer.errors import LedgerUnavailable

FORMAT = "grove.forget-ledger.v1"
ENV = "GROVE_FORGET_LEDGER_DIR"
WORLD_TARGET = "world"
ERASURE_STARTED = "started"
ERASURE_FINISHED = "finished"
_FILE_NAME = re.compile(r"[0-9a-f]{64}\.json")


def _json(value: Any) -> str:
    def native(part):
        if isinstance(part, (datetime, date)) or hasattr(part, "isoformat"):
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


def _load(path: Path) -> Any:
    """The parsed file. Raises FileNotFoundError, or LedgerUnavailable for an unsafe file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError:
        raise LedgerUnavailable("latest forget ledger is unavailable") from None
    try:
        with os.fdopen(fd, "r") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16_000_000:
                raise ValueError
            return json.load(handle)
    except (ValueError, TypeError):
        raise LedgerUnavailable("latest forget ledger failed validation") from None


def _read(root: Path, world_id: str, *, missing: bool = False) -> list[dict]:
    try:
        data = _load(root / _name(world_id))
    except FileNotFoundError:
        if missing:
            return []
        raise LedgerUnavailable("latest forget ledger is absent") from None
    try:
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


def _standing(rows: list[dict]) -> list[dict]:
    """The forget rows of the world that stands now. A started erasure ends nothing: its attempt
    can fail with the world and its rows still in place."""
    kept: list[dict] = []
    for row in rows:
        if row.get("target_type") != WORLD_TARGET:
            kept.append(row)
        elif row.get("state") == ERASURE_FINISHED:
            kept = []
    return kept


def check_ledger(path: Path | str | None, world_id: str) -> None:
    """Refuses a ledger that a later append would refuse: unsafe, unset or damaged."""
    with locked_ledger(path) as root:
        _read(root, world_id, missing=True)


def read_ledger(path: Path | str, world_id: str) -> list[dict]:
    with locked_ledger(path) as root:
        return _standing(_read(root, world_id))


def world_erasures(root: Path) -> dict[str, list[dict]]:
    """Caller holds locked_ledger. Every world with an erasure row, and those rows, in order."""
    found: dict[str, list[dict]] = {}
    for name in sorted(entry.name for entry in os.scandir(root)):
        if _FILE_NAME.fullmatch(name) is None:
            continue
        try:
            world_id = _load(root / name)["world_id"]
            if _name(world_id) != name:
                raise ValueError
        except (ValueError, TypeError, KeyError, ScopeError):
            raise LedgerUnavailable("latest forget ledger failed validation") from None
        rows = [row for row in _read(root, world_id) if row.get("target_type") == WORLD_TARGET]
        if rows:
            found[world_id] = rows
    return found


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


def checked_under_lock(root: Path, world_id: str, database_rows: list[dict]) -> list[dict]:
    """Caller owns the ledger lock. No disclosure while an approved intent is pending."""
    from grove.writer.forget import validate_ledger_rows

    try:
        external = validate_ledger_rows(
            world_id, _standing(_read(root, world_id, missing=not database_rows))
        )
        committed = validate_ledger_rows(world_id, database_rows)
        if {r["id"]: _json(r) for r in external} != {r["id"]: _json(r) for r in committed}:
            raise ValueError
        return external
    except (ValueError, KeyError, TypeError):
        raise LedgerUnavailable("the material ledger requires recovery") from None


@contextmanager
def material_request_guard(
    driver, world_id: str, request_ids: list[str] | None = None, *, due_at: datetime | None = None
):
    """Serialize material Request work with canonical erasure, before any World lock.

    Request source identities are immutable. A claim supplies its exact candidate ids;
    a pass supplies its frozen input ids. Ordinary nonmaterial Requests need no ledger.
    Only start preflight supplies due_at; completed or deferred material is not its work.
    The caller keeps this context through its read or writer transaction.
    """
    from grove.store.query import WorldQuery
    from grove.writer.requests import _DUE

    query = WorldQuery(driver, world_id)
    due_filter = " AND (" + _DUE + ")" if due_at is not None else ""
    material = query.read(
        "MATCH (r:Request), (s:Source) WHERE r.source_id = s.id "
        "AND s.parser_version = 'material.note.v1' "
        "AND ($ids IS NULL OR r.id IN $ids)" + due_filter + " RETURN r.id AS id LIMIT 1",
        ids=request_ids,
        now=due_at,
    )
    if not material:
        yield
        return
    with locked_ledger(None) as root:
        checked_under_lock(
            root, world_id, [r["f"] for r in query.read("MATCH (f:ForgetLedger) RETURN f")]
        )
        yield
