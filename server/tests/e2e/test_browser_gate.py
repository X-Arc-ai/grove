"""The browser tests never hide a run that tests nothing, and never get in a default run's way.

Each run here is a real pytest in a child process, with Playwright made unimportable and an
unreachable store, so neither depends on what the machine has installed or runs.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2]
MODULE = "tests/e2e/test_views_browser.py"
# Spelled out, not imported from the module: a module that fails or skips on import must not take
# this test down with it.
RIGHT_COMMAND = "uv run --with playwright==1.58.0 python -m pytest -m browser tests/e2e"


@pytest.fixture
def no_playwright(tmp_path) -> dict[str, str]:
    """An environment where `import playwright` raises ImportError, whatever is installed."""
    package = tmp_path / "playwright"
    package.mkdir()
    (package / "__init__.py").write_text('raise ImportError("no Playwright here")\n')
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "GROVE_NEO4J_URI": "bolt://127.0.0.1:1"}
    env.pop("GROVE_E2E_SHOTS", None)
    return env


def _pytest(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", MODULE, "-q", "-p", "no:cacheprovider", *args],
        cwd=SERVER,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_command_in_the_message_is_the_one_the_docs_name():
    docs = (SERVER.parent / "docs" / "views.md").read_text(encoding="utf-8")
    assert RIGHT_COMMAND in docs
    module = (SERVER / MODULE).read_text(encoding="utf-8")
    assert f'RIGHT_COMMAND = "{RIGHT_COMMAND}"' in module
    assert "python -m pytest -m browser tests/e2e -q" in module


def test_a_browser_run_with_no_playwright_fails_and_names_the_command(no_playwright):
    run = _pytest(no_playwright, "-m", "browser")
    assert run.returncode == pytest.ExitCode.TESTS_FAILED, run.stdout
    assert RIGHT_COMMAND in run.stdout
    assert "skipped" not in run.stdout
    assert re.search(r"\b[1-9]\d* errors?\b", run.stdout), run.stdout


def test_a_default_run_with_no_playwright_collects_and_deselects_with_no_failure(no_playwright):
    run = _pytest(no_playwright)
    assert run.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, run.stdout
    assert re.search(r"\b[1-9]\d* deselected\b", run.stdout), run.stdout
    for word in ("skipped", "error", "failed"):
        assert word not in run.stdout.lower(), run.stdout
