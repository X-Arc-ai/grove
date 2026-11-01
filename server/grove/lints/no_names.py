"""R1: the banned words stay out of the repo, its paths, its commits and the PR text.

The words are built from parts, so this file and the tests never hold them whole.
Output never holds a matched word: a path that contains one is printed with the word blanked.

With a base, the diff and the paths of every commit in the range are searched too: history goes
public with the repo, so a word that one commit adds and a later commit removes or renames away
still counts.

A merge that GitHub commits carries the account's login as the author name and the email that
GitHub picks: the noreply form, or the account's verified address (seen on this repo's test merge).
Every other commit carries the noreply form for author and committer.

Evasions this lint cannot close: a word split, encoded (base64, UTF-16) or spelled with
look-alike letters; text on GitHub that no event passes to the job, such as a PR comment.
"""

import os
import re
from pathlib import Path

from grove.lints._scan import Finding, LintError, git, list_files

_PARTS = (("ham", "za"), ("ham", "py"), ("aa", "tar"))
WORDS = tuple("".join(parts) for parts in _PARTS)

_PATTERN = "|".join(WORDS)
_WORD = re.compile(_PATTERN, re.IGNORECASE)

ACCOUNT = ("anas-xarc", "261762116+anas-xarc@users.noreply.github.com")
GITHUB_MERGE_COMMITTER = ("GitHub", "noreply@github.com")
GITHUB_MERGE_AUTHORS = frozenset({ACCOUNT, ("anas-xarc", "anas@x-arc.ai")})

_ENV_NAMES = ("PR_TITLE", "PR_BODY", "GITHUB_HEAD_REF")
_LOG_FIELDS = 7
_LOG_FORMAT = "%x00".join(("%H", "%P", "%an", "%ae", "%cn", "%ce", "%B"))


def _blank(path: str) -> str:
    return _WORD.sub(lambda match: "#" * len(match.group()), path)


def _content(root: Path) -> list[Finding]:
    out = git(
        root,
        *("grep", "--untracked", "--no-color", "-a", "-n", "-z", "-i", "-E", "-e", _PATTERN),
        ok=(0, 1),
    )
    findings = []
    for record in out.split(b"\n"):
        if not record:
            continue
        fields = record.split(b"\0", 2)
        if len(fields) < 3 or not fields[1].isdigit():
            raise LintError("git grep printed a record the lint cannot read")
        path = _blank(fields[0].decode(errors="replace"))
        findings.append(Finding(path, int(fields[1]), "no-names/content"))
    return findings


def _paths(root: Path) -> list[Finding]:
    return [
        Finding(_blank(rel), 1, "no-names/path") for rel in list_files(root) if _WORD.search(rel)
    ]


def _commits(root: Path, base: str) -> list[Finding]:
    out = git(
        root, "log", "-z", f"--format=tformat:{_LOG_FORMAT}", "--end-of-options", f"{base}..HEAD"
    )
    fields = out.decode(errors="replace").split("\0")[:-1]
    if len(fields) % _LOG_FIELDS:
        raise LintError("git log printed a record the lint cannot read")

    findings = []
    for start in range(0, len(fields), _LOG_FIELDS):
        sha, parents, *people, message = fields[start : start + _LOG_FIELDS]
        where = f"commit/{sha[:12]}"
        author, committer = tuple(people[:2]), tuple(people[2:])
        by_github = len(parents.split()) >= 2 and committer == GITHUB_MERGE_COMMITTER

        if author not in (GITHUB_MERGE_AUTHORS if by_github else {ACCOUNT}):
            findings.append(Finding(where, 1, "no-names/commit-author"))
        if committer != ACCOUNT and not by_github:
            findings.append(Finding(where, 1, "no-names/commit-committer"))
        for number, line in enumerate(message.splitlines(), start=1):
            if _WORD.search(line):
                findings.append(Finding(where, number, "no-names/commit-message"))
    return findings


def _diffs(root: Path, base: str) -> list[Finding]:
    out = git(
        root,
        *("log", "-a", "-i", "-G", _PATTERN, "--format=tformat:%H"),
        *("--end-of-options", f"{base}..HEAD"),
    )
    return [
        Finding(f"commit/{sha[:12]}", 1, "no-names/commit-diff")
        for sha in out.decode(errors="replace").split()
    ]


def _history_paths(root: Path, base: str) -> list[Finding]:
    """-G reads diff text only, so a path that a later commit renames away needs its own walk."""
    out = git(
        root,
        *("log", "--format=", "--name-only", "--no-renames", "-z"),
        *("--end-of-options", f"{base}..HEAD"),
    )
    paths = {path.strip("\n") for path in out.decode(errors="replace").split("\0")}
    return [
        Finding(_blank(path), 1, "no-names/commit-path")
        for path in sorted(paths)
        if path and _WORD.search(path)
    ]


def _environment() -> list[Finding]:
    findings = []
    for name in _ENV_NAMES:
        for number, line in enumerate(os.environ.get(name, "").splitlines(), start=1):
            if _WORD.search(line):
                findings.append(Finding(f"env/{name}", number, "no-names/env"))
    return findings


def check(root: Path, base: str | None = None) -> list[Finding]:
    findings = _content(root) + _paths(root) + _environment()
    if base:
        findings += _commits(root, base) + _diffs(root, base) + _history_paths(root, base)
    return findings
