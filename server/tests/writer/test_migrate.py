"""The migration runner. Tests pass their own ledger scope and a temp dir of harmless statements;
no test changes a real constraint or index."""

import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from grove.config import ConfigError, load_settings
from grove.store.catalogue import FULLTEXT_INDEXES, TABLES
from grove.store.connection import DATABASE
from grove.store.scope import literal_spans
from grove.writer import migrate
from grove.writer.migrate import (
    DEFAULT_SCOPE,
    MIGRATIONS_DIR,
    MigrationDrift,
    MigrationsMissing,
    StoreNotEmpty,
    checksum,
    main,
    run_migrations,
    split_statements,
)
from grove.writer.testing import plant_node

HARMLESS = "RETURN 1;\nRETURN 2 AS two;\n"


def ledger_rows(driver, scope):
    with driver.session(database=DATABASE) as session:
        rows = session.run(
            "MATCH (m:Migration {scope: $scope}) RETURN m ORDER BY m.name", scope=scope
        )
        return [dict(row["m"]) for row in rows]


@pytest.fixture
def scope(driver):
    name = f"t_migrate_{uuid4().hex}"
    assert name != DEFAULT_SCOPE
    yield name
    with driver.session(database=DATABASE) as session:
        session.run(
            "MATCH (m:Migration {scope: $scope}) DELETE m", scope=scope_name(name)
        ).consume()
    assert ledger_rows(driver, name) == []


def scope_name(name):
    assert name.startswith("t_migrate_"), "a test must never touch the real ledger"
    return name


def test_settings_parse_auth_and_hide_the_password(monkeypatch):
    monkeypatch.setenv("NEO4J_AUTH", "someone/not-a-real-secret")
    settings = load_settings()
    assert settings.neo4j_auth == ("someone", "not-a-real-secret")
    assert "not-a-real-secret" not in repr(settings)


def test_settings_none_means_no_auth(monkeypatch):
    monkeypatch.setenv("NEO4J_AUTH", "none")
    assert load_settings().neo4j_auth is None


@pytest.mark.parametrize("value", ["", "user", "user/", "/secret"])
def test_settings_refuse_a_malformed_auth_and_do_not_echo_it(monkeypatch, value):
    monkeypatch.setenv("NEO4J_AUTH", value)
    with pytest.raises(ConfigError) as refused:
        load_settings()
    assert "secret" not in str(refused.value)


def test_settings_need_auth_to_be_set(monkeypatch):
    monkeypatch.delenv("NEO4J_AUTH", raising=False)
    with pytest.raises(ConfigError, match="NEO4J_AUTH"):
        load_settings()


def test_split_ignores_semicolons_in_strings_and_comments():
    text = """// a; comment
    RETURN 'a;b' AS x; /* c;d */ RETURN "e;f" AS y;
    // only a comment; after the last
    """
    statements = split_statements(text)
    assert len(statements) == 2
    assert "'a;b'" in statements[0]
    assert '"e;f"' in statements[1]


def test_split_handles_escaped_quote_and_quoted_name():
    statements = split_statements("RETURN 'it\\'s;' AS `a;b`; RETURN 2")
    assert statements == ["RETURN 'it\\'s;' AS `a;b`", "RETURN 2"]


def test_split_drops_comment_only_parts_and_keeps_the_tail_with_no_semicolon():
    assert split_statements("// one\n;\n;RETURN 1") == ["RETURN 1"]


def test_every_schema_create_uses_if_not_exists():
    creates = 0
    for path in sorted(MIGRATIONS_DIR.glob("*.cypher")):
        for statement in split_statements(path.read_text(encoding="utf-8")):
            masked = list(statement)
            for start, end, _ in literal_spans(statement):
                masked[start:end] = " " * (end - start)
            body = "".join(masked).strip()
            if re.match(r"CREATE\b", body):
                creates += 1
                assert re.match(
                    r"CREATE\s+(?:\w+\s+)?(?:CONSTRAINT|INDEX)\s+\w+\s+IF NOT EXISTS\b", body
                ), f"{path.name}: {body[:70]}"
    assert creates > 40


def test_runner_applies_then_skips_and_writes_the_ledger(driver, tmp_path, scope):
    (tmp_path / "0002_b.cypher").write_text(HARMLESS)
    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")

    first = run_migrations(driver, tmp_path, scope=scope)
    assert first == {"applied": ["0001_a.cypher", "0002_b.cypher"], "skipped": []}

    rows = ledger_rows(driver, scope)
    assert [row["name"] for row in rows] == [f"{scope}/0001_a.cypher", f"{scope}/0002_b.cypher"]
    assert rows[1]["checksum"] == checksum(HARMLESS)
    assert rows[1]["scope"] == scope
    assert rows[1]["applied_at"] is not None

    second = run_migrations(driver, tmp_path, scope=scope)
    assert second == {"applied": [], "skipped": ["0001_a.cypher", "0002_b.cypher"]}


def test_a_file_that_sorts_before_the_newest_applied_one_still_applies(driver, tmp_path, scope):
    (tmp_path / "0002_b.cypher").write_text("RETURN 2;")
    run_migrations(driver, tmp_path, scope=scope)
    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")

    result = run_migrations(driver, tmp_path, scope=scope)
    assert result == {"applied": ["0001_a.cypher"], "skipped": ["0002_b.cypher"]}


def test_drift_stops_before_anything_runs(driver, tmp_path, scope):
    (tmp_path / "0002_b.cypher").write_text("RETURN 2;")
    run_migrations(driver, tmp_path, scope=scope)
    (tmp_path / "0002_b.cypher").write_text("RETURN 3;")
    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")

    with pytest.raises(MigrationDrift):
        run_migrations(driver, tmp_path, scope=scope)
    assert [row["name"] for row in ledger_rows(driver, scope)] == [f"{scope}/0002_b.cypher"]


def test_ledger_rows_with_no_file_are_ignored(driver, tmp_path, scope):
    other = tmp_path / "other"
    other.mkdir()
    (other / "0009_other_lane.cypher").write_text("RETURN 9;")
    run_migrations(driver, other, scope=scope)

    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")
    result = run_migrations(driver, tmp_path, scope=scope)
    assert result == {"applied": ["0001_a.cypher"], "skipped": []}


def test_a_failed_statement_stops_the_run_and_writes_no_row_for_that_file(driver, tmp_path, scope):
    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")
    (tmp_path / "0002_bad.cypher").write_text("RETURN 1; NOT CYPHER AT ALL;")
    (tmp_path / "0003_c.cypher").write_text("RETURN 3;")

    with pytest.raises(Exception, match=r"(?i)invalid|syntax"):
        run_migrations(driver, tmp_path, scope=scope)
    assert [row["name"] for row in ledger_rows(driver, scope)] == [f"{scope}/0001_a.cypher"]


def test_runs_at_once_on_one_scope_all_succeed_and_write_one_row_per_file(
    driver, tmp_path, scope, monkeypatch
):
    for number in range(1, 4):
        (tmp_path / f"000{number}_f.cypher").write_text(f"RETURN {number};")
    runs = 3
    # Every run reads the empty ledger before any run writes a row: the race of two lanes that
    # start tests at once, made certain.
    all_have_read = threading.Barrier(runs, timeout=30)
    real_ledger = migrate._ledger

    def ledger_then_wait(*args):
        rows = real_ledger(*args)
        all_have_read.wait()
        return rows

    monkeypatch.setattr(migrate, "_ledger", ledger_then_wait)
    with ThreadPoolExecutor(max_workers=runs) as pool:
        results = list(
            pool.map(lambda _: run_migrations(driver, tmp_path, scope=scope), range(runs))
        )

    files = ["0001_f.cypher", "0002_f.cypher", "0003_f.cypher"]
    assert results == [{"applied": files, "skipped": []}] * runs
    assert [row["name"] for row in ledger_rows(driver, scope)] == [f"{scope}/{f}" for f in files]


def test_a_run_that_finds_a_row_with_another_checksum_raises_drift(
    driver, tmp_path, scope, monkeypatch
):
    (tmp_path / "0001_a.cypher").write_text("RETURN 1;")
    run_migrations(driver, tmp_path, scope=scope)
    (tmp_path / "0001_a.cypher").write_text("RETURN 2;")
    # A run that read the ledger before the other run wrote its row.
    monkeypatch.setattr(migrate, "_ledger", lambda *args: {})

    with pytest.raises(MigrationDrift):
        run_migrations(driver, tmp_path, scope=scope)
    assert [row["checksum"] for row in ledger_rows(driver, scope)] == [checksum("RETURN 1;")]


def test_a_missing_directory_is_refused_and_an_empty_one_is_not(driver, tmp_path, scope):
    with pytest.raises(MigrationsMissing):
        run_migrations(driver, tmp_path / "absent", scope=scope)
    assert run_migrations(driver, tmp_path, scope=scope) == {"applied": [], "skipped": []}


def test_cli_exits_2_on_a_missing_directory_and_prints_no_json(driver, tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "grove.writer.migrate", "--dir", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2, done.stderr
    assert done.stdout == ""
    assert len(done.stderr.strip().splitlines()) == 1
    assert "absent" in done.stderr


def test_the_default_directory_is_the_checkouts_migrations_folder():
    server = Path(__file__).resolve().parents[2]
    assert MIGRATIONS_DIR == server / "migrations"
    assert MIGRATIONS_DIR.is_dir()
    assert sorted(MIGRATIONS_DIR.glob("*.cypher"))


def test_require_empty_refuses_a_store_with_a_node(driver, tmp_path, scope, world):
    plant_node(driver, world, "Item", title="anything")
    with pytest.raises(StoreNotEmpty):
        run_migrations(driver, tmp_path, scope=scope, require_empty=True)


def test_cli_prints_one_json_object(driver, tmp_path, capsys):
    # main() runs the default scope, so the CLI test points it at an empty dir: nothing applies
    # and the real ledger stays as it was.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--dir", str(empty)]) == 0
    assert json.loads(capsys.readouterr().out) == {"applied": [], "skipped": []}


def test_cli_exits_2_on_require_empty_with_nodes(driver, migrated, tmp_path, world):
    plant_node(driver, world, "Item", title="anything")
    empty = tmp_path / "empty"
    empty.mkdir()
    done = subprocess.run(
        [sys.executable, "-m", "grove.writer.migrate", "--dir", str(empty), "--require-empty"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2, done.stderr
    assert done.stdout == ""


def _live_schema(driver):
    with driver.session(database=DATABASE) as session:
        constraints = {
            row["name"]: tuple(row["properties"])
            for row in session.run("SHOW CONSTRAINTS YIELD name, properties")
        }
        indexes = {
            row["name"]: (row["type"], tuple(row["labelsOrTypes"]), tuple(row["properties"]))
            for row in session.run(
                "SHOW INDEXES YIELD name, type, labelsOrTypes, properties, owningConstraint "
                "WHERE type <> 'LOOKUP' AND owningConstraint IS NULL "
                "RETURN name, type, labelsOrTypes, properties"
            )
        }
    return constraints, indexes


def test_live_schema_matches_the_catalogue(driver, migrated):
    constraints, indexes = _live_schema(driver)
    for table in TABLES:
        for unique in table.uniques:
            assert constraints.get(unique.name) == unique.props, unique.name
        for index in table.indexes:
            kind, labels, props = indexes[index.name]
            assert kind == index.kind.upper(), index.name
            assert labels == (table.label,), index.name
            assert props == index.props, index.name
    for index in FULLTEXT_INDEXES:
        kind, labels, props = indexes[index.name]
        assert kind == "FULLTEXT"
        assert set(labels) == set(index.labels), index.name
        assert set(props) == set(index.props), index.name


def test_full_text_indexes_use_the_folding_analyzer(driver, migrated):
    with driver.session(database=DATABASE) as session:
        rows = session.run(
            "SHOW INDEXES YIELD name, options WHERE name IN $names",
            names=[index.name for index in FULLTEXT_INDEXES],
        )
        analyzers = {
            row["name"]: row["options"]["indexConfig"]["fulltext.analyzer"] for row in rows
        }
    assert analyzers == {index.name: index.analyzer for index in FULLTEXT_INDEXES}


def test_no_stray_schema_on_an_exclusive_store(driver, migrated):
    if os.environ.get("GROVE_STORE_EXCLUSIVE") != "1":
        pytest.skip("the shared store holds schema of other lanes")
    constraints, indexes = _live_schema(driver)
    expected_constraints = {unique.name for table in TABLES for unique in table.uniques}
    expected_indexes = {index.name for table in TABLES for index in table.indexes}
    expected_indexes |= {index.name for index in FULLTEXT_INDEXES}
    assert set(constraints) == expected_constraints
    assert set(indexes) == expected_indexes
