"""Temp git repos and the CI lint command, for the red tests.

The tests plant Cypher and clock reads as text. Each snippet here holds a `~` that `plant` drops,
so these test files stay clean when the real lints walk them, with no exemption for tests.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER.parent

ACCOUNT = ("anas-xarc", "261762116+anas-xarc@users.noreply.github.com")
GITHUB = ("GitHub", "noreply@github.com")
# GitHub's test merge of PR 1 on this repo carries this author: the login and the verified address.
GITHUB_MERGE_AUTHOR = ("anas-xarc", "anas@x-arc.ai")
# git's regex in this locale skips a line with an invalid UTF-8 byte, unless the lint resets it.
UTF8_LOCALE = {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}

# Spelled apart from the lint on purpose: a neutered word list in the lint must not neuter these.
BANNED_WORDS = ("ha" + "mza", "ha" + "mpy", "aat" + "ar")

_DROPPED_ENV = ("PR_TITLE", "PR_BODY", "GITHUB_HEAD_REF")
_FINDING_LINE = re.compile(r"^.+:\d+: [a-z-]+/[a-z0-9-]+$")


def run_lint(name, *args, env=None):
    full_env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "grove.lints", name, *args],
        cwd=SERVER,
        env=full_env,
        capture_output=True,
        text=True,
    )


def assert_clean(result):
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def assert_red(result, expected):
    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stdout
    assert all(_FINDING_LINE.match(line) for line in result.stdout.splitlines()), result.stdout


class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", ACCOUNT[0])
        self.git("config", "user.email", ACCOUNT[1])
        self.plant("README.md", "A synthetic repo.\n")
        self.commit("Seed")
        self.git("branch", "base")

    def git(self, *args, env=None):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.path,
            env={**os.environ, **(env or {})},
            check=True,
            capture_output=True,
            text=True,
        )

    def plant(self, rel, text):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text.replace("~", ""), encoding="utf-8")

    @staticmethod
    def identity(author=ACCOUNT, committer=ACCOUNT):
        return {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }

    def commit(self, message, *, author=ACCOUNT, committer=ACCOUNT):
        self.git("add", "-A")
        env = self.identity(author, committer)
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)

    def merge_side_branch(self, *, author=ACCOUNT, committer=ACCOUNT):
        self.git("checkout", "-q", "-b", "side")
        self.commit("Side work")
        self.git("checkout", "-q", "main")
        self.commit("Main work")
        env = self.identity(author, committer)
        self.git("merge", "-q", "--no-ff", "-m", "Merge the side work", "side", env=env)

    def lint(self, name, *args, env=None):
        return run_lint(name, "--root", str(self.path), *args, env=env)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)
