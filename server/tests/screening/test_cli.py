"""`python -m grove.screening counts` prints the three counts and nothing else, and writes nothing.

Synthetic files only: no test here reads the real rename map or denylist.
"""

import json
import os
import subprocess
import sys

import pytest

from grove.config import DENYLIST_VAR, RENAME_MAP_VAR
from grove.screening.__main__ import main
from tests.screening._plants import private_file

RENAME = {
    "Orla Vance": "Co-founder",
    "Orla": "Co-founder",
    "ORLA": "Co-founder",
    "Tamsin": "Designer",
}
DENYLIST = (
    "# outside names\nBrightwell Logistics\nHalvard Quist  # trailing comment\n"
    "brightwell logistics\n"
)
PLANTED = ("Orla", "Vance", "Co-founder", "Tamsin", "Designer", "Brightwell", "Halvard", "Quist")
EXPECTED = {"rename_forms": 3, "labels": 2, "denylist_terms": 2}


@pytest.fixture
def files(tmp_path):
    folder = tmp_path / "lists"
    folder.mkdir(mode=0o700)
    rename = private_file(folder, "rename.json", json.dumps(RENAME))
    denylist = private_file(folder, "denylist.txt", DENYLIST)
    return rename, denylist


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(RENAME_MAP_VAR, raising=False)
    monkeypatch.delenv(DENYLIST_VAR, raising=False)
    return monkeypatch


def assert_only_counts(out: str, expected: dict[str, int]) -> None:
    assert out == json.dumps(expected) + "\n"
    for planted in PLANTED:
        assert planted.lower() not in out.lower()


def test_counts_from_the_flags(files, clean_env, capsys):
    rename, denylist = files
    assert main(["counts", "--rename", str(rename), "--denylist", str(denylist)]) == 0
    out, err = capsys.readouterr()
    assert_only_counts(out, EXPECTED)
    assert err == ""


def test_counts_from_the_variables(files, clean_env, capsys):
    rename, denylist = files
    clean_env.setenv(RENAME_MAP_VAR, str(rename))
    clean_env.setenv(DENYLIST_VAR, str(denylist))
    assert main(["counts"]) == 0
    assert_only_counts(capsys.readouterr().out, EXPECTED)


def test_a_flag_replaces_its_variable_and_the_other_falls_back(files, clean_env, capsys):
    rename, denylist = files
    clean_env.setenv(RENAME_MAP_VAR, str(rename.parent / "absent.json"))
    clean_env.setenv(DENYLIST_VAR, str(denylist))
    assert main(["counts", "--rename", str(rename)]) == 0
    assert_only_counts(capsys.readouterr().out, EXPECTED)


def test_unset_variables_count_zero(clean_env, capsys):
    assert main(["counts"]) == 0
    assert_only_counts(
        capsys.readouterr().out, {"rename_forms": 0, "labels": 0, "denylist_terms": 0}
    )


@pytest.mark.parametrize(
    "fault, name",
    [("open-file", "--denylist"), ("bad-json", "--rename")],
    ids=["group-readable-denylist", "malformed-rename-map"],
)
def test_a_bad_file_exits_2_and_names_only_the_flag(files, clean_env, capsys, fault, name):
    rename, denylist = files
    if fault == "open-file":
        denylist.chmod(0o644)
    else:
        rename.write_text('{"Orla Vance": "Co-founder"', encoding="utf-8")
    assert main(["counts", "--rename", str(rename), "--denylist", str(denylist)]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert name in err
    for planted in (*PLANTED, "lists"):
        assert planted.lower() not in err.lower()


def test_a_path_with_an_unknown_user_exits_2_and_names_only_the_variable(clean_env, capsys):
    clean_env.setenv(RENAME_MAP_VAR, "~pathmarker-q7/rename.json")
    assert main(["counts"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert RENAME_MAP_VAR in err
    assert "pathmarker-q7" not in err


def test_the_command_line_prints_the_counts_and_writes_nothing(files, tmp_path):
    rename, denylist = files
    work = tmp_path / "work"
    work.mkdir()
    env = {
        key: value for key, value in os.environ.items() if key not in (RENAME_MAP_VAR, DENYLIST_VAR)
    }
    env |= {"TMPDIR": str(work), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(work)}
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "grove.screening",
            "counts",
            "--rename",
            str(rename),
            "--denylist",
            str(denylist),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert_only_counts(done.stdout, EXPECTED)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
