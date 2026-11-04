"""D14 schema boundary: exercise the actual CLI against independently changed checkouts."""

import subprocess
from pathlib import Path

import pytest

from grove.lints.schema_rule import check
from tests.lints.conftest import REPO_ROOT, assert_clean, assert_red, run_lint

CATALOGUE = "server/grove/store/catalogue.py"
ENUMS = "server/grove/store/enums.py"


@pytest.fixture
def schema_root(tmp_path):
    # No commits or identity changes are needed for the CLI to resolve this root.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for relative in (CATALOGUE, ENUMS):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((REPO_ROOT / relative).read_bytes())
    return tmp_path


def append_column(root: Path, kind: str, *, table_name: str = "questions") -> None:
    with (root / CATALOGUE).open("a") as handle:
        handle.write(
            "\nfrom dataclasses import replace\n"
            "TABLES = tuple(replace(t, columns=(*t.columns, "
            f"Column('private note', ('private_note',), {kind!r}))) "
            f"if t.name == {table_name!r} else t for t in TABLES)\n"
        )


def test_schema_cli_passes_current_catalogue():
    assert_clean(run_lint("schema", "--root", str(REPO_ROOT)))


@pytest.mark.parametrize("kind", ["text_home", "json_home"])
@pytest.mark.parametrize("table_name", ["questions", "items", "map_positions"])
def test_new_free_text_property_is_refused(schema_root, kind, table_name):
    assert_clean(run_lint("schema", "--root", str(schema_root)))
    append_column(schema_root, kind, table_name=table_name)
    result = run_lint("schema", "--root", str(schema_root))
    assert_red(result, "schema/text-outside-home")
    assert "private note" not in result.stdout + result.stderr
    assert "private_note" not in result.stdout + result.stderr


def test_new_table_is_in_the_schema_walk(schema_root):
    with (schema_root / CATALOGUE).open("a") as handle:
        handle.write(
            "\nfrom dataclasses import replace\n"
            "TABLES += (replace(TABLES[0], name='new_table', label='NewTable', "
            "columns=(Column('text', ('text',), 'text_home'),)),)\n"
        )
    assert_red(run_lint("schema", "--root", str(schema_root)), "schema/text-outside-home")


@pytest.mark.parametrize("kind", ["text", "str", "json", "new_type", ""])
def test_an_unknown_type_cannot_bypass_the_boundary(schema_root, kind):
    append_column(schema_root, kind)
    assert_red(run_lint("schema", "--root", str(schema_root)), "schema/unknown-type")


def test_value_id_is_the_safe_home_for_new_free_text_fields(schema_root):
    append_column(schema_root, "value_id")
    assert_clean(run_lint("schema", "--root", str(schema_root)))


@pytest.mark.parametrize("values", ["()", "('same', 'same')", "None", "('ok', 3)"])
def test_invalid_enum_cannot_be_called_a_closed_enumeration(schema_root, values):
    append_column(schema_root, "enum:synthetic")
    with (schema_root / ENUMS).open("a") as handle:
        handle.write(f"\nENUMS['synthetic'] = {values}\n")
    assert_red(run_lint("schema", "--root", str(schema_root)), "schema/invalid-enum")


def test_missing_enum_is_refused(schema_root):
    append_column(schema_root, "enum:not_declared")
    assert_red(run_lint("schema", "--root", str(schema_root)), "schema/invalid-enum")


def test_declared_closed_enum_is_permitted(schema_root):
    append_column(schema_root, "enum:synthetic")
    with (schema_root / ENUMS).open("a") as handle:
        handle.write("\nENUMS['synthetic'] = ('one', 'two')\n")
    assert_clean(run_lint("schema", "--root", str(schema_root)))


@pytest.mark.parametrize("relative", [CATALOGUE, ENUMS])
def test_missing_schema_input_never_passes(schema_root, relative):
    (schema_root / relative).unlink()
    result = run_lint("schema", "--root", str(schema_root))
    assert result.returncode == 2


def test_unreadable_schema_does_not_print_its_content(schema_root):
    (schema_root / CATALOGUE).write_text("raise RuntimeError('synthetic private text')\n")
    result = run_lint("schema", "--root", str(schema_root))
    assert result.returncode == 2
    assert "synthetic private text" not in result.stdout + result.stderr


def test_root_catalogue_is_not_a_cached_import(schema_root):
    assert check(schema_root) == []
    append_column(schema_root, "text_home")
    assert any(f.rule == "schema/text-outside-home" for f in check(schema_root))
    (schema_root / CATALOGUE).write_bytes((REPO_ROOT / CATALOGUE).read_bytes())
    assert check(schema_root) == []


def test_schema_lint_is_a_required_validate_step():
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/validate.yml").read_text())
    steps = workflow["jobs"]["validate"]["steps"]
    schema = [step for step in steps if step.get("run") == "uv run python -m grove.lints schema"]
    assert len(schema) == 1
    assert "if" not in schema[0]
    assert not schema[0].get("continue-on-error", False)
