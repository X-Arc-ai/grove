"""The migration runner: python -m grove.writer.migrate [--dir <path>] [--require-empty]."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from neo4j import Driver

from grove import clock
from grove.store.connection import DATABASE, open_driver
from grove.store.scope import literal_spans

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_SCOPE = "grove"
AWAIT_SECONDS = 300


class MigrationDrift(RuntimeError):
    """An applied file changed. A migration is never edited: add a new one."""


class StoreNotEmpty(RuntimeError):
    """--require-empty found a node."""


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
        session.run(
            "CYPHER 25 CREATE (m:Migration) SET m = $row", row={**row, "applied_at": clock.now()}
        ).consume()


def run_migrations(
    driver: Driver,
    directory: Path = MIGRATIONS_DIR,
    *,
    scope: str = DEFAULT_SCOPE,
    require_empty: bool = False,
) -> dict[str, list[str]]:
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
    args = parser.parse_args(argv)

    with open_driver() as driver:
        try:
            result = run_migrations(driver, args.dir, require_empty=args.require_empty)
        except StoreNotEmpty as refused:
            print(refused, file=sys.stderr)
            return 2
        except MigrationDrift as drift:
            print(drift, file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
