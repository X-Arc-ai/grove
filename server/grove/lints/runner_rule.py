"""Runner rule: a job runs on GitHub's hosted runners unless the repo variable names a pool.

`Validate` runs on `${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}`. While the repo is private, the
variable may name a self-hosted pool. Before the repo goes public the variable is deleted, and every
job then falls back to `ubuntu-latest`. That fallback holds only when no workflow names a runner in
any other way, so this lint reads every workflow file and allows two `runs-on` values only:

    ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}
    ubuntu-latest

A value is compared after its quotes and a trailing comment are dropped and its spaces and tabs
collapsed. Any other whitespace (a no-break space, for example) stays and makes the value differ.

The project carries no YAML parser, so the lint reads the files line by line. On one line it
fails closed: a structural line it cannot read is a finding, never a skip. The content of a block
scalar (`run: |`, `key: >-`) is text, not structure, and is not read. Keys compare without case.

Findings:
    runner/not-allowed     any other value: a pool label, `self-hosted`, a list, an expression
    runner/block-form      `runs-on:` with its value on the lines below it (a list, `group:`, a
                           plain scalar that goes on in the next line)
    runner/reusable-call   a job that calls a workflow of another repository (runner out of reach)
    runner/symlink         a workflow file, or the workflows dir, that is a symbolic link
    runner/unreadable      a line the scanner cannot read: an explicit `?` key, a tag or an anchor
                           or alias in a key's place, a merge key `<<`, a double-quoted key with
                           an escape, a flow mapping that holds `runs-on` or `uses` on that line,
                           a job that is only an alias, a column-0 line that is not a key, or a
                           line break that YAML does not break at (NEL, U+2028, U+2029, VT, FF)

Out of reach: YAML structure that spans lines, which only a parser can follow (a flow collection
or a quoted scalar that goes on over several lines, with a `runs-on` or `uses` on a later line),
and a workflow file that a job writes at run time. Each of these is a deliberate act, not an
accident. The guarantee for a public repo does not rest on this lint: the variable and the pool
are removed before the repo goes public, so a label that no runner carries makes a job queue in
plain view and never run on a self-hosted runner.
"""

import re
from pathlib import Path

from grove.lints._scan import Finding, list_files, read_text

WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
LINKED_DIRS = (".github", ".github/workflows")

ALLOWED = frozenset({"${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}", "ubuntu-latest"})

_KEY = re.compile(
    r"""^(?P<indent>[ ]*)(?P<item>-[ ]+)?"""
    r"""(?P<q>["']?)(?P<key>[A-Za-z0-9_-]+)(?P=q)[ \t]*:(?P<rest>.*)$"""
)
_UNREADABLE = re.compile(r"""^[ ]*(?:-[ ]+)?(?:[?!&*]|<<|"[^"\n]*\\)""")
# A `{` that is not part of `${{`, with `runs-on` or `uses` anywhere after it on the line.
_FLOW_KEY = re.compile(r"""(?<![${])\{(?!\{).*\b(?:runs-on|uses)\b""", re.IGNORECASE)
_ODD_BREAK = re.compile("[\x85\u2028\u2029\x0b\x0c\x1c-\x1e]")
_BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")
_COMMENT = re.compile(r"(^|[ \t])#.*$")
_DOCUMENT_MARK = re.compile(r"^(?:---|\.\.\.)(?:[ \t]+#.*)?$")


def _value(rest: str) -> str:
    """The scalar after a key: no trailing comment, no outer quotes, spaces and tabs collapsed."""
    text = _COMMENT.sub("", rest).strip(" \t")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip(" \t")
    return re.sub(r"[ \t]+", " ", text)


def _is_workflow(rel: str) -> bool:
    return rel.startswith(WORKFLOW_DIR) and rel.lower().endswith(WORKFLOW_SUFFIXES)


def _check_text(rel: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    in_jobs = False
    job_indent: int | None = None
    key_indent: int | None = None
    block_column: int | None = None  # inside a block scalar: lines indented past this are text
    runs_on: tuple[int, int] | None = None  # (line, key column) of a runs-on with a plain value
    # Split only where YAML breaks a line. str.splitlines also breaks at NEL, U+2028 and others,
    # where a YAML parser reads one line: such a line is a finding below.
    for number, line in enumerate(re.split(r"\r\n|\r|\n", text), start=1):
        if _ODD_BREAK.search(line):
            findings.append(Finding(rel, number, "runner/unreadable"))
            continue
        stripped = line.strip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_column is not None:
            if indent > block_column:
                continue
            block_column = None
        if runs_on is not None:
            if indent > runs_on[1]:
                findings.append(Finding(rel, runs_on[0], "runner/block-form"))
            runs_on = None
        match = _KEY.match(line)
        if indent == 0:
            if _FLOW_KEY.search(line):
                findings.append(Finding(rel, number, "runner/unreadable"))
            if match and not match.group("item"):
                in_jobs = match.group("key").lower() == "jobs"
                job_indent = key_indent = None
            elif not _DOCUMENT_MARK.match(line):
                findings.append(Finding(rel, number, "runner/unreadable"))
                in_jobs = False
            if match and _BLOCK_SCALAR.match(_value(match.group("rest"))):
                block_column = 0
            continue
        if not match:
            if _UNREADABLE.match(line) or _FLOW_KEY.search(line):
                findings.append(Finding(rel, number, "runner/unreadable"))
            continue
        key, rest = match.group("key").lower(), match.group("rest")
        column = indent + len(match.group("item") or "")
        value = _value(rest)
        if _BLOCK_SCALAR.match(value):
            block_column = column
        elif _FLOW_KEY.search(rest):
            findings.append(Finding(rel, number, "runner/unreadable"))
        if key == "runs-on":
            if not value:
                findings.append(Finding(rel, number, "runner/block-form"))
            elif value not in ALLOWED:
                findings.append(Finding(rel, number, "runner/not-allowed"))
            else:
                runs_on = (number, column)
            continue
        if not in_jobs or match.group("item"):
            continue
        if job_indent is None or indent <= job_indent:
            job_indent, key_indent = indent, None
            if value.startswith("*"):
                findings.append(Finding(rel, number, "runner/unreadable"))
            continue
        if key_indent is None:
            key_indent = indent
        if indent == key_indent and key == "uses" and not value.startswith("./"):
            findings.append(Finding(rel, number, "runner/reusable-call"))
    return findings


def check(root: Path, base: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel in list_files(root):
        path = root / rel
        if rel in LINKED_DIRS and path.is_symlink():
            findings.append(Finding(rel, 1, "runner/symlink"))
            continue
        if not _is_workflow(rel):
            continue
        if path.is_symlink():
            findings.append(Finding(rel, 1, "runner/symlink"))
        elif path.is_file():
            findings += _check_text(rel, read_text(root, rel))
    return findings
