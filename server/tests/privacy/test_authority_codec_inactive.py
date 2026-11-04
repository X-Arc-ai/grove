"""The new pure modules do not acquire any product capability."""

import ast
import subprocess
import sys
from pathlib import Path

from grove.authority import codec, legacy, records, transitions

PURE = {"records.py", "codec.py", "transitions.py", "legacy.py"}
FOUNDATION = {"__init__.py", "scalars.py", "capacity.py", "lease.py"}
READERS = {"legacy_reader.py"}
ROOT = Path(__file__).resolve().parents[2] / "grove"


def test_exact_pure_and_foundation_sets_cover_every_authority_module():
    assert {path.name for path in (ROOT / "authority").glob("*.py")} == PURE | FOUNDATION | READERS
    assert PURE.isdisjoint(FOUNDATION)
    assert READERS.isdisjoint(PURE | FOUNDATION)


def test_pure_modules_have_only_closed_imports_and_no_io_clock_environment_or_process():
    allowed = {
        "re",
        "json",
        "array",
        "dataclasses",
        "hashlib",
        "types",
        "scalars",
        "records",
        "capacity",
        "codec",
    }
    forbidden = {
        "open",
        "read_text",
        "read_bytes",
        "write",
        "write_bytes",
        "write_text",
        "load",
        "loads",
        "dump",
        "exec",
        "eval",
        "compile",
        "__import__",
        "environ",
        "getenv",
        "now",
        "today",
        "time",
        "monotonic",
        "perf_counter",
        "sleep",
        "socket",
        "connect",
        "acquire",
        "flock",
        "system",
        "popen",
        "spawn",
        "run",
        "unlink",
        "mkdir",
        "fsync",
    }
    for name in sorted(PURE):
        module_allowed = allowed | ({"datetime"} if name == "legacy.py" else set())
        tree = ast.parse((ROOT / "authority" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name in module_allowed for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module in module_allowed
            if isinstance(node, ast.Call):
                called = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else (node.func.id if isinstance(node.func, ast.Name) else "")
                )
                if (
                    called == "compile"
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "re"
                ):
                    continue
                assert called not in forbidden, (name, called)


def test_product_sources_do_not_import_or_register_new_modules():
    for path in ROOT.rglob("*.py"):
        if "authority" in path.relative_to(ROOT).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("grove.authority")
                assert not (
                    node.module == "grove" and any(n.name == "authority" for n in node.names)
                )
            if isinstance(node, ast.Import):
                assert not any(n.name.startswith("grove.authority") for n in node.names)


def test_plain_package_import_still_does_not_load_a_codec_or_lease():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import grove.authority; "
            "assert not any(k.startswith('grove.authority.') for k in sys.modules)",
        ],
        capture_output=True,
        check=True,
        timeout=5,
    )


def test_api_outputs_expose_no_authorization_or_persistence_method():
    for module in (codec, legacy, records, transitions):
        for name in (
            "authorize",
            "activate",
            "publish",
            "write_journal",
            "read_ledger",
            "genesis",
            "read_packet_barrier",
            "resolve_disclosure_policy",
            "filter_capture",
            "check_cursor",
        ):
            assert not hasattr(module, name)
