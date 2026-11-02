"""Text from a connector reaches the sources table through the screener only (D14, line 2505).

record_source takes text as given, for the writer's own tests. This walk fails when code outside
the writer package reaches for it, so no later lane stores raw text by calling the wrong function.
"""

import ast
from pathlib import Path

import pytest

import grove

UNSCREENED = "record_source"


def reaches(root: Path) -> list[str]:
    """Each place outside the writer package that names the unscreened function."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] == "writer":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            else:
                continue
            if UNSCREENED in names:
                found.append(f"{relative}:{node.lineno}")
    return found


def test_no_code_outside_the_writer_reaches_the_unscreened_function():
    assert reaches(Path(grove.__file__).parent) == []


@pytest.mark.parametrize(
    "line",
    [
        "from grove.writer import record_source",
        "from grove.writer.sources import record_source as keep",
        "import grove.writer\ngrove.writer.record_source(driver, world_id)",
        "from grove import writer\nwriter.sources.record_source(driver, world_id)",
        "record_source(driver, world_id)",
    ],
)
def test_the_walk_sees_each_form_in_a_new_file(tmp_path, line):
    (tmp_path / "ingest").mkdir()
    (tmp_path / "ingest" / "fresh.py").write_text(line + "\n", encoding="utf-8")
    assert len(reaches(tmp_path)) == 1


def test_the_walk_leaves_the_writer_package_and_the_screened_ops_alone(tmp_path):
    (tmp_path / "writer").mkdir()
    (tmp_path / "writer" / "sources.py").write_text("def record_source(): ...\n", encoding="utf-8")
    (tmp_path / "ingest.py").write_text(
        "from grove.writer import add_screened_source\n", encoding="utf-8"
    )
    assert reaches(tmp_path) == []
