"""Runner rule: a job runs on GitHub's hosted runners unless the repo variable names a pool.

`Validate` runs on `${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}`. While the repo is private, the
variable may name a self-hosted pool. Before the repo goes public the variable is deleted, and every
job then falls back to `ubuntu-latest`. That fallback holds only when no workflow names a runner in
any other way, so this lint reads every workflow file as text and allows two `runs-on` values only:

    ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}
    ubuntu-latest

A value is compared after its quotes and a trailing comment are dropped and its spaces collapsed.

Findings:
    runner/not-allowed     any other value: a pool label, `self-hosted`, a list, an expression
    runner/block-form      `runs-on:` with its value on the lines below it (a list, `group:`)
    runner/reusable-call   a job that calls a workflow of another repository (runner out of reach)
    runner/symlink         a workflow file that is a symbolic link (target out of reach)

Out of reach: a workflow file that a job writes at run time, and YAML written in flow style
(`jobs: {build: {runs-on: x}}`) or with anchors and aliases for `runs-on`. None of these is in the
repo, and each one is a deliberate act, not an accident.
"""

import re
from pathlib import Path

from grove.lints._scan import Finding, list_files, read_text

WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

ALLOWED = frozenset({"${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}", "ubuntu-latest"})

_KEY = re.compile(
    r"""^(?P<indent>[ ]*)(?P<item>-[ ]+)?["']?(?P<key>[A-Za-z0-9_-]+)["']?[ ]*:(?P<rest>.*)$"""
)
_COMMENT = re.compile(r"(^|[ \t])#.*$")


def _value(rest: str) -> str:
    """The scalar after a key: no trailing comment, no outer quotes, spaces collapsed."""
    text = _COMMENT.sub("", rest).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    return " ".join(text.split())


def _is_workflow(rel: str) -> bool:
    return rel.startswith(WORKFLOW_DIR) and rel.lower().endswith(WORKFLOW_SUFFIXES)


def _check_text(rel: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    in_jobs = False
    job_indent: int | None = None
    key_indent: int | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _KEY.match(line)
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jobs = bool(match and match.group("key") == "jobs" and not match.group("item"))
            job_indent = key_indent = None
            continue
        if not match:
            continue
        key, rest = match.group("key"), match.group("rest")
        if key == "runs-on":
            value = _value(rest)
            if not value:
                findings.append(Finding(rel, number, "runner/block-form"))
            elif value not in ALLOWED:
                findings.append(Finding(rel, number, "runner/not-allowed"))
            continue
        if not in_jobs or match.group("item"):
            continue
        if job_indent is None or indent <= job_indent:
            job_indent, key_indent = indent, None
            continue
        if key_indent is None:
            key_indent = indent
        if indent == key_indent and key == "uses" and not _value(rest).startswith("./"):
            findings.append(Finding(rel, number, "runner/reusable-call"))
    return findings


def check(root: Path, base: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel in list_files(root):
        if not _is_workflow(rel):
            continue
        path = root / rel
        if path.is_symlink():
            findings.append(Finding(rel, 1, "runner/symlink"))
        elif path.is_file():
            findings += _check_text(rel, read_text(root, rel))
    return findings
