"""The first component remains disconnected from every production flow."""

import ast
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "grove"
AUTHORITY = SOURCE / "authority"
INACTIVE_WRITER = frozenset(
    {
        "authority_records.py",
        "authority_outcomes.py",
        "authority_adapter.py",
        "authority_forget.py",
    }
)


def test_package_has_only_the_frozen_component_modules():
    assert {path.name for path in AUTHORITY.glob("*.py")} == {
        "__init__.py",
        "scalars.py",
        "capacity.py",
        "lease.py",
        "records.py",
        "codec.py",
        "transitions.py",
        "legacy.py",
        "legacy_reader.py",
        "publication.py",
        "publication_files.py",
        "publisher.py",
    }
    tree = ast.parse((AUTHORITY / "__init__.py").read_text())
    assert len(tree.body) == 1
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)


def test_no_runtime_source_imports_or_registers_authority():
    for path in SOURCE.rglob("*.py"):
        if AUTHORITY in path.parents or (
            path.parent == SOURCE / "writer" and path.name in INACTIVE_WRITER
        ):
            continue
        assert "grove.authority" not in path.read_text(), path.relative_to(SOURCE)
        for name in INACTIVE_WRITER:
            assert name.removesuffix(".py") not in path.read_text(), path.relative_to(SOURCE)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "authority", path.relative_to(SOURCE)
                assert not (
                    node.module == "grove"
                    and any(alias.name == "authority" for alias in node.names)
                )


def test_component_has_no_activation_environment_or_write_api():
    forbidden = {
        "environ",
        "getenv",
        "mkdir",
        "makedirs",
        "creat",
        "write",
        "write_text",
        "write_bytes",
        "fsync",
        "fdatasync",
        "replace",
        "rename",
        "unlink",
        "remove",
        "truncate",
        "ftruncate",
        "O_CREAT",
        "O_WRONLY",
        "O_RDWR",
        "O_TRUNC",
        "O_APPEND",
        "dump",
        "dumps",
        "load",
        "loads",
    }
    allowed_imports = {
        "re",
        "dataclasses",
        "errno",
        "fcntl",
        "math",
        "os",
        "stat",
        "threading",
        "time",
        "grove",
        "scalars",
    }
    for name in ("__init__.py", "scalars.py", "capacity.py", "lease.py"):
        path = AUTHORITY / name
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (path.name, node.attr)
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed_imports for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module in allowed_imports
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "/var/" not in node.value and "/etc/" not in node.value


def test_plain_package_import_does_not_import_submodules():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import grove.authority; "
            "assert not any(n.startswith('grove.authority.') for n in sys.modules)",
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )


def test_no_runtime_module_imports_or_constructs_authority_writer():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import grove.writer.api; "
            "assert not any(n.startswith('grove.writer.authority_') for n in sys.modules)",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
