"""A failing test must not print an R1 word into the CI log, through its id or its report."""

import os
import subprocess
import sys
from pathlib import Path

from tests.lints.conftest import BANNED_WORDS

SERVER = Path(__file__).resolve().parents[2]
WORD = BANNED_WORDS[1]


BLANK = "#" * len(WORD)


def _holds_a_word(text: str, *own_paths: Path) -> bool:
    # A machine's own home, checkout or temp path can hold a word; that is the machine, not the
    # report.
    for path in (*own_paths, SERVER.parent, Path.home()):
        text = text.replace(str(path), "<path>")
    folded = text.casefold()
    return any(word in folded for word in BANNED_WORDS)


def test_no_test_id_holds_a_word():
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=SERVER,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout[-2000:]
    assert "test_word_in_a_new_file[form0]" in done.stdout
    assert not _holds_a_word(done.stdout + done.stderr)


def _run_planted(tmp_path: Path, *plugin: str) -> subprocess.CompletedProcess:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_planted.py").write_text(
        "def test_planted():\n"
        f"    print('captured {WORD.title()} line')\n"
        f"    assert 'planted {WORD}' == 'planted nobody'\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [
            *(sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *plugin),
            *("-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path)),
            "test_planted.py",
        ],
        # From the temp folder, pytest prints the test path relative to it; PYTHONPATH finds the
        # plugin.
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(SERVER), os.environ.get("PYTHONPATH")))
            ),
        },
        capture_output=True,
        text=True,
    )


def test_a_failure_report_is_blanked(tmp_path):
    done = _run_planted(tmp_path, "-p", "tests._redact")
    assert done.returncode == 1
    assert f"planted {BLANK}" in done.stdout
    assert f"captured {BLANK}" in done.stdout
    assert not _holds_a_word(done.stdout + done.stderr, tmp_path, tmp_path.resolve())


def test_the_control_run_shows_the_word(tmp_path):
    done = _run_planted(tmp_path)
    assert done.returncode == 1
    assert f"planted {WORD}" in done.stdout
    assert f"captured {WORD.title()}" in done.stdout
