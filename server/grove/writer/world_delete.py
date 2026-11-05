"""Delete my Grove: the one place that removes a real world. docs/forget-api.md has the contract.

The steps run in a fixed order and each one can run again, so a retry after a failure finishes
the job: the ledger record, the person's Pipedream accounts (grove.api.world_delete calls them),
the world's files, then the store. Sign-in then makes the new, empty world.

The ledger record is the write-ahead intent, as in D14: a failed attempt can leave an extra
record, never removed rows with no record. A second record, written when the rows are gone and
before the final commit, is what ends the old forget rows and what a later snapshot is measured
against. default_world_id gives the new world the same id, so these records are the only thing
that tells a restored backup of the old world from the new one.
"""

import os
import shutil
import stat
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work

from grove import clock
from grove.demo_policy import DESIGNATION_QUERY, has_designation
from grove.ids import new_id
from grove.store.catalogue import TABLES, WORLD_LABELS
from grove.store.connection import DATABASE
from grove.store.query import CYPHER_PREFIX, PlatformQuery
from grove.store.scope import check_world_id
from grove.writer.forget_ledger import (
    ERASURE_FINISHED,
    ERASURE_STARTED,
    WORLD_TARGET,
    LedgerUnavailable,
    append_intent,
    locked_ledger,
    world_erasures,
)
from grove.writer.tx import _LOCK_WORLD

FILES_ENV = "GROVE_AGENT_FILES_DIR"
BACKUPS_CLEAR_DAYS = 30

_TIMEOUT = 600.0
_BATCH = 1000
# Every label that holds world_id, world rows and platform rows alike. The last pass of
# _remove_rows names no label, so a label added later cannot survive a delete; this list only
# keeps the batches on the per-label indexes.
_LABELS = tuple(
    sorted(
        WORLD_LABELS
        | {
            held.label
            for held in TABLES
            if held.element == "node" and any("world_id" in c.props for c in held.columns)
        }
    )
)
# A session names a user, not a world, and it reaches the world through World.owner_user_id.
_REMOVE_SESSIONS = CYPHER_PREFIX + "MATCH (s:Session {user_id: $user_id}) DETACH DELETE s"
_REMOVE_WORLD = CYPHER_PREFIX + "MATCH (w:World {id: $world_id}) DETACH DELETE w"


class ErasureRefused(RuntimeError):
    """This person may not delete this world. Nothing was written."""


class FilesUnavailable(RuntimeError):
    """The world's files could not be reached or removed. No store row was removed for it."""


def _erasure_row(world_id: str, user_id: str, state: str) -> dict[str, Any]:
    """Ids and times only."""
    at = clock.now(world_id)
    return {
        "id": new_id(world_id),
        "target_type": WORLD_TARGET,
        "state": state,
        "world_id": world_id,
        "user_id": user_id,
        "erased_at": at.isoformat(),
        "backups_clear_after": (at.date() + timedelta(days=BACKUPS_CLEAR_DAYS)).isoformat(),
    }


def begin_erasure(
    driver: Driver, world_id: str, user_id: str, *, ledger_dir: Path | str | None = None
) -> str:
    """Step 1. Refuses a demo world and any world this user does not own, then fsyncs the record.
    Returns the address hash that sign-in needs to make the new world."""
    rows = PlatformQuery(driver).read(DESIGNATION_QUERY, world_id=world_id)
    if (
        len(rows) != 1
        or rows[0]["user_id"] != user_id
        or not rows[0]["email_hash"]
        or has_designation(rows[0])
    ):
        raise ErasureRefused("this account cannot delete this world")
    with locked_ledger(ledger_dir) as root:
        append_intent(root, world_id, _erasure_row(world_id, user_id, ERASURE_STARTED), [])
    return rows[0]["email_hash"]


def remove_world_files(world_id: str) -> None:
    """Step 3. The world id has a fixed shape with no separator, so this path is one level under
    the root. With no setting there is no folder. A root that is set and absent is refused: an
    unmounted disk must not read as files removed."""
    configured = os.environ.get(FILES_ENV)
    if not configured:
        return
    folder = Path(configured) / check_world_id(world_id)
    # rmtree works through directory descriptors here, so it unlinks a link and never follows one.
    if not shutil.rmtree.avoids_symlink_attacks or not Path(configured).is_dir():
        raise FilesUnavailable("the files folder is not available")
    try:
        if stat.S_ISDIR(os.lstat(folder).st_mode):
            shutil.rmtree(folder)
        else:
            folder.unlink()
    except FileNotFoundError:
        return
    except OSError:
        raise FilesUnavailable("the files could not be removed") from None


def _remove_batch(driver: Driver, statement: str, world_id: str) -> int:
    @unit_of_work(timeout=_TIMEOUT)
    def unit(tx: ManagedTransaction) -> int:
        return tx.run(statement, world_id=world_id, batch=_BATCH).single(strict=True)["removed"]

    with driver.session(database=DATABASE) as session:
        return session.execute_write(unit)


def _remove_rows(driver: Driver, world_id: str) -> None:
    patterns = [f"(n:{label} {{world_id: $world_id}})" for label in _LABELS]
    for pattern in (*patterns, "(n {world_id: $world_id})"):
        statement = (
            f"{CYPHER_PREFIX}MATCH {pattern} WITH n LIMIT $batch "
            "DETACH DELETE n RETURN count(*) AS removed"
        )
        while _remove_batch(driver, statement, world_id):
            pass


def _remove_world(
    driver: Driver, world_id: str, user_id: str, before_commit: Callable[[], None]
) -> None:
    """A batch commits alone, so a retry goes on from where a failure stopped. This transaction
    holds the world's lock for the whole of it, so no writer gets in between two batches, and
    only its commit ends the sessions and the :World row: until then the person's cookie still
    works for the retry. No node links to :World, so no batch waits for this lock."""
    with (
        driver.session(database=DATABASE) as session,
        session.begin_transaction(timeout=_TIMEOUT) as held,
    ):
        held.run(_LOCK_WORLD, world_id=world_id).consume()
        _remove_rows(driver, world_id)
        before_commit()
        held.run(_REMOVE_SESSIONS, user_id=user_id).consume()
        held.run(_REMOVE_WORLD, world_id=world_id).consume()
        held.commit()


def erase_world_rows(
    driver: Driver, world_id: str, user_id: str, *, ledger_dir: Path | str | None = None
) -> str:
    """Step 4. Returns backups_clear_after. The :User row stays, and with it the login.

    The ledger lock comes first, as in material_request_guard, which takes it before any World
    lock. It stays held for the whole delete, so every forget waits for this delete."""
    finished: dict[str, Any] = {}
    with locked_ledger(ledger_dir) as root:

        def close() -> None:
            # An upload that began before the delete may have finished since step 3.
            remove_world_files(world_id)
            row = _erasure_row(world_id, user_id, ERASURE_FINISHED)
            append_intent(root, world_id, row, [])
            finished.update(row)

        _remove_world(driver, world_id, user_id, close)
    return finished["backups_clear_after"]


def _erased_at(row: dict[str, Any]) -> datetime:
    try:
        at = datetime.fromisoformat(row["erased_at"])
        if at.tzinfo is None or not row["user_id"]:
            raise ValueError
        return at
    except (KeyError, TypeError, ValueError):
        raise LedgerUnavailable("latest forget ledger failed validation") from None


def _due(
    root: Path, snapshot_taken_at: datetime | None, world_id: str | None
) -> list[tuple[str, str]]:
    """(world, user) of each erasure that reaches the snapshot, by world id."""
    due = []
    for erased, rows in sorted(world_erasures(root).items()):
        # Every row is read first: a damaged row stops the replay before it removes anything.
        newest = max(rows, key=_erased_at)
        if world_id in (None, erased) and (
            snapshot_taken_at is None or _erased_at(newest) > snapshot_taken_at
        ):
            due.append((erased, newest["user_id"]))
    return due


def replay_world_erasures(
    driver: Driver,
    ledger_dir: Path | str | None,
    *,
    snapshot_taken_at: datetime | None,
    world_id: str | None = None,
) -> list[str]:
    """Removes the restored rows of every world erased after the snapshot, and makes nothing.
    Returns those worlds, sorted. A world with a newer snapshot stays as it is. A started erasure
    counts, as a D14 intent does: the person asked for it. None means the time is unknown, and
    every erasure counts.

    A dump holds every world, so the default reads every record. A logical snapshot holds one
    world and the rest of the store is live, so it must name world_id: a live world with a newer
    erasure holds the person's new data."""
    with locked_ledger(ledger_dir) as root:
        due = _due(root, snapshot_taken_at, world_id)
        for erased, user_id in due:
            _remove_world(driver, erased, user_id, lambda: None)
    return [erased for erased, _ in due]
