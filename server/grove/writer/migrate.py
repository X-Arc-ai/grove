"""The migration runner: python -m grove.writer.migrate [--dir <path>] [--require-empty].

Migrations run from the checkout's `server/` folder: the .cypher files are not in the wheel, so the
default directory exists only in a checkout, and a missing directory is an error, never zero files.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

from neo4j import Driver, Query, Transaction

from grove import clock
from grove.store.connection import DATABASE, open_driver
from grove.store.scope import literal_spans

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_SCOPE = "grove"
AWAIT_SECONDS = 300
_MAX_GENERATION = 2**63 - 1
_GENERATION_VALUES = "CYPHER 25 MATCH (w:World) RETURN w.read_generation AS generation"
_INITIALIZE_GENERATIONS = (
    "CYPHER 25 MATCH (w:World) WHERE w.read_generation IS NULL SET w.read_generation = 0"
)
_OTHER_TRANSACTIONS = (
    "SHOW TRANSACTIONS YIELD database, metaData "
    "WHERE database = $database AND coalesce(metaData.grove_cutover_probe, '') <> $probe "
    "RETURN count(*) AS active_transactions"
)


class MigrationDrift(RuntimeError):
    """An applied file changed. A migration is never edited: add a new one."""


class StoreNotEmpty(RuntimeError):
    """--require-empty found a node."""


class MigrationsMissing(RuntimeError):
    """The directory does not exist. An existing empty directory is valid."""


class CutoverRefused(RuntimeError):
    """A quiescent cutover check failed; messages never contain store values."""


def check_quiescent(driver: Driver) -> dict[str, int]:
    """Count all other database transactions using an account with full transaction visibility.

    The host owns writer exclusion. This probe only checks its drain; it cannot prevent a new
    writer from starting after the query. A fresh nonce excludes this query alone.
    """
    probe = uuid4().hex
    query = Query(_OTHER_TRANSACTIONS, metadata={"grove_cutover_probe": probe}, timeout=30.0)
    with driver.session(database=DATABASE) as session:
        result = session.run(query, database=DATABASE, probe=probe)
        row = result.single(strict=True)
        result.consume()
    count = row["active_transactions"] if row is not None else None
    if type(count) is not int or count < 0:
        raise CutoverRefused("transaction count is invalid")
    if count:
        raise CutoverRefused("other database transactions remain")
    return {"active_transactions": count}


def _generation_counts(tx: Transaction, *, allow_null: bool) -> dict[str, int]:
    worlds, missing = 0, 0
    result = tx.run(_GENERATION_VALUES)
    for row in result:
        worlds += 1
        value = row["generation"]
        if value is None:
            missing += 1
        elif type(value) is not int or not 0 <= value < _MAX_GENERATION:
            raise CutoverRefused("world generations are invalid or exhausted")
    result.consume()
    if missing and not allow_null:
        raise CutoverRefused("world generations remain missing")
    return {"worlds": worlds, "missing": missing}


def reconcile_generations(driver: Driver) -> dict[str, int]:
    """Explicit NULL-only repair under the host maintenance barrier, independent of the ledger.

    An unmanaged transaction avoids automatic retries after uncertain outcomes. Both scans and
    the repair share one transaction. No world identifiers or other properties are fetched.
    """
    with driver.session(database=DATABASE) as session:
        with session.begin_transaction(timeout=60.0) as tx:
            before = _generation_counts(tx, allow_null=True)
            tx.run(_INITIALIZE_GENERATIONS).consume()
            after = _generation_counts(tx, allow_null=False)
            if after["worlds"] != before["worlds"]:
                raise CutoverRefused("world count changed during reconciliation")
            tx.commit()
    return {"worlds": after["worlds"], "initialized": before["missing"], "missing": 0}


def split_statements(text: str) -> list[str]:
    """Split on `;` outside literals and comments; drop parts that hold only comments."""
    masked = list(text)
    for start, end, kind in literal_spans(text):
        masked[start:end] = (" " if kind == "comment" else "x") * (end - start)

    statements, start = [], 0
    for position, char in enumerate([*masked, ";"]):
        if char == ";":
            if "".join(masked[start:position]).strip():
                statements.append(text[start:position].strip())
            start = position + 1
    return statements


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ledger(driver: Driver, scope: str) -> dict[str, str]:
    with driver.session(database=DATABASE) as session:
        rows = session.run(
            "CYPHER 25 MATCH (m:Migration {scope: $scope}) "
            "RETURN m.name AS name, m.checksum AS sum",
            scope=scope,
        )
        return {row["name"]: row["sum"] for row in rows}


def _store_has_nodes(driver: Driver) -> bool:
    with driver.session(database=DATABASE) as session:
        return session.run("CYPHER 25 MATCH (n) RETURN n LIMIT 1").peek() is not None


def _apply(driver: Driver, statements: list[str], row: dict[str, object]) -> None:
    # Schema and data cannot share a transaction, so each statement commits alone. The ledger row
    # comes last: a file that failed halfway runs again, and IF NOT EXISTS makes that safe.
    with driver.session(database=DATABASE) as session:
        for statement in statements:
            session.run(f"CYPHER 25 {statement}").consume()
        session.run("CYPHER 25 CALL db.awaitIndexes($seconds)", seconds=AWAIT_SECONDS).consume()
        # Two runs at once both read an empty ledger. MERGE on the unique name lets the second wait
        # for the first and match its row; a row with another checksum is drift, not a success.
        recorded = session.run(
            "CYPHER 25 MERGE (m:Migration {name: $name}) ON CREATE SET m += $row "
            "RETURN m.checksum AS checksum",
            name=row["name"],
            row={**row, "applied_at": clock.now()},
        ).single()["checksum"]
    if recorded != row["checksum"]:
        raise MigrationDrift(f"{row['name']} was applied by another run with another checksum")


def run_migrations(
    driver: Driver,
    directory: Path = MIGRATIONS_DIR,
    *,
    scope: str = DEFAULT_SCOPE,
    require_empty: bool = False,
) -> dict[str, list[str]]:
    if not directory.is_dir():
        raise MigrationsMissing(f"no migrations directory at {directory}")
    if require_empty and _store_has_nodes(driver):
        raise StoreNotEmpty("the store holds nodes")

    files = [
        (path.name, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.cypher"))
    ]
    # Rows with no file here are ignored: every lane adds migrations to one shared store.
    ledger = _ledger(driver, scope)
    for name, text in files:
        recorded = ledger.get(f"{scope}/{name}")
        if recorded is not None and recorded != checksum(text):
            raise MigrationDrift(f"{scope}/{name} changed after it was applied")

    applied, skipped = [], []
    for name, text in files:
        key = f"{scope}/{name}"
        if key in ledger:
            skipped.append(name)
            continue
        row = {"name": key, "scope": scope, "checksum": checksum(text)}
        _apply(driver, split_statements(text), row)
        applied.append(name)
    return {"applied": applied, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m grove.writer.migrate")
    parser.add_argument("--dir", type=Path, default=MIGRATIONS_DIR)
    parser.add_argument("--require-empty", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-quiescent", action="store_true")
    modes.add_argument("--quiescent-reconcile", action="store_true")
    args = parser.parse_args(argv)

    if args.check_quiescent or args.quiescent_reconcile:
        if args.require_empty:
            parser.error("--require-empty cannot be combined with a quiescent operation")
        try:
            with open_driver() as driver:
                result = (
                    check_quiescent(driver)
                    if args.check_quiescent
                    else reconcile_generations(driver)
                )
        except CutoverRefused as refused:
            print(refused, file=sys.stderr)
            return 2
        except Exception:
            # Driver exceptions may include query parameters or store values.
            print("quiescent operation failed", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0

    with open_driver() as driver:
        try:
            result = run_migrations(driver, args.dir, require_empty=args.require_empty)
        except (StoreNotEmpty, MigrationsMissing) as refused:
            print(refused, file=sys.stderr)
            return 2
        except MigrationDrift as drift:
            print(drift, file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
