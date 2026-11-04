import ast
from pathlib import Path

import pytest

from grove.extraction.contracts import ExtractionRefused
from grove.extraction.runtime import require_runtime


def test_runtime_stays_unavailable_without_registration(monkeypatch):
    monkeypatch.setenv("GROVE_LEARNER_ENABLED", "1")
    with pytest.raises(ExtractionRefused, match="learner_authority_unavailable"):
        require_runtime()
    with pytest.raises(TypeError):
        require_runtime(lambda: True)
    root = Path(__file__).resolve().parents[2] / "grove"
    for directory in ("api", "runner"):
        for path in (root / directory).glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module not in {
                        "grove.extraction.runtime",
                        "grove.extraction.execution_state",
                    }
