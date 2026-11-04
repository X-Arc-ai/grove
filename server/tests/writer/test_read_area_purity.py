"""Structural read purity control in the namespace for prohibited-entry-point fixtures."""

import ast
from pathlib import Path


def test_area_module_has_no_storage_clock_or_model_calls():
    # A structural purity guard complements the immutable-value behavior tests above.
    from grove.store import areas

    source = ast.parse(Path(areas.__file__).read_text())
    calls = [node for node in ast.walk(source) if isinstance(node, ast.Call)]
    forbidden = {"execute_write", "execute_read", "session", "run", "now", "monotonic"}
    assert not any(
        isinstance(node.func, ast.Attribute) and node.func.attr in forbidden for node in calls
    )
