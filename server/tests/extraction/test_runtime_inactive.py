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


def test_durable_unit_has_no_production_provider_or_registration(monkeypatch):
    from grove.writer import learner_requests as boundary

    monkeypatch.setenv("GROVE_LEARNER_DURABLE", "1")
    for call in (
        lambda: boundary.refuse_runtime(),
        lambda: boundary.transaction(None, "world", object()),
        lambda: boundary._require_authority_in_tx(None, object(), "commit", None, None),
        lambda: boundary._require_attempt_in_tx(None, object(), None, None, None),
        lambda: boundary._require_consumer_in_tx(None, object(), None, None),
    ):
        with pytest.raises(ExtractionRefused, match="learner_authority_unavailable"):
            call()
    root = Path(__file__).resolve().parents[2] / "grove"
    for directory in ("api", "runner"):
        for path in (root / directory).glob("*.py"):
            assert "learner_execution" not in path.read_text()
