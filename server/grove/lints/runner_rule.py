"""Runner rule: a job runs on GitHub's hosted runners unless the repo variable names a pool.

`Validate` runs on `${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}`. While the repo is private, the
variable may name a self-hosted pool. Before the repo goes public the variable is deleted, and every
job then falls back to `ubuntu-latest`. That fallback holds only when no workflow names a runner in
any other way, so a job's `runs-on` may hold two values only:

    ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}
    ubuntu-latest

The lint parses every `.yml` and `.yaml` file under `.github/workflows/` with a YAML parser and
walks `jobs`, so quotes, comments, escapes, flow style, block scalars, anchors, aliases and merge
keys read as the parser reads them. The names `jobs`, `runs-on` and `uses` compare without case,
and spaces and tabs inside a value collapse. Any other difference in a value makes it differ.

Findings:
    runner/not-allowed     a job's `runs-on` that holds anything else: a pool label, `self-hosted`,
                           a list, a mapping, another expression, nothing
    runner/reusable-call   a job's `uses` that is not a workflow file in ./.github/workflows/, since
                           the runner of any other workflow is out of reach
    runner/symlink         a workflow file, `.github/workflows` or `.github` that is a symbolic link
    runner/unreadable      a file the parser refuses or that holds more than one document; a root,
                           `jobs` or job that is not a mapping; a key there that is not a scalar; a
                           job with neither `runs-on` nor `uses`; a tracked workflow that is not on
                           disk; or a line break that YAML versions read differently (NEL, U+2028,
                           U+2029, VT, FF, U+001C to U+001E)

Out of reach: a workflow file that a job writes at run time, and any text that GitHub's own parser
reads differently from PyYAML. The guarantee for a public repo does not rest on this lint: the
variable and the pool are removed before the repo goes public, so a label that no runner carries
makes a job queue in plain view and never run on a self-hosted runner.
"""

import re
from pathlib import Path

from grove.lints._scan import Finding, LintError, list_files, read_text

WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
LINKED_DIRS = (".github", ".github/workflows")

ALLOWED = frozenset({"${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}", "ubuntu-latest"})

_LOCAL_CALL = re.compile(r"^\./\.github/workflows/(?!.*\.\.)[^/]+\.ya?ml$", re.IGNORECASE)
_ODD_BREAK = re.compile("[\\x85\\u2028\\u2029\\x0b\\x0c\\x1c-\\x1e]")


def _load_yaml():
    """PyYAML is a dev dependency, imported here so that the other lints run without it."""
    try:
        import yaml
    except ImportError as error:
        raise LintError("the runner lint needs PyYAML: uv sync --frozen") from error
    return yaml


def _is_workflow(rel: str) -> bool:
    return rel.startswith(WORKFLOW_DIR) and rel.lower().endswith(WORKFLOW_SUFFIXES)


def _line(node) -> int:
    return node.start_mark.line + 1


def _scalar(yaml, node) -> str | None:
    """A scalar's text with spaces and tabs collapsed; None for a list or a mapping."""
    if not isinstance(node, yaml.ScalarNode):
        return None
    return re.sub(r"[ \t]+", " ", node.value)


def _check_job(yaml, loader, rel: str, name_node, job) -> list[Finding]:
    if not isinstance(job, yaml.MappingNode):
        return [Finding(rel, _line(name_node), "runner/unreadable")]
    loader.flatten_mapping(job)
    findings: list[Finding] = []
    has_runner = False
    for key_node, value_node in job.value:
        if not isinstance(key_node, yaml.ScalarNode):
            findings.append(Finding(rel, _line(key_node), "runner/unreadable"))
            continue
        key = key_node.value.lower()
        if key == "runs-on":
            has_runner = True
            if _scalar(yaml, value_node) not in ALLOWED:
                findings.append(Finding(rel, _line(key_node), "runner/not-allowed"))
        elif key == "uses":
            has_runner = True
            value = _scalar(yaml, value_node)
            if value is None or not _LOCAL_CALL.match(value):
                findings.append(Finding(rel, _line(key_node), "runner/reusable-call"))
    if not has_runner:
        findings.append(Finding(rel, _line(name_node), "runner/unreadable"))
    return findings


def _check_text(yaml, rel: str, text: str) -> list[Finding]:
    odd = _ODD_BREAK.search(text)
    if odd:
        return [Finding(rel, text.count("\n", 0, odd.start()) + 1, "runner/unreadable")]
    loader = yaml.SafeLoader(text)
    try:
        root = loader.get_single_node()
        if not isinstance(root, yaml.MappingNode):
            return [Finding(rel, 1, "runner/unreadable")]
        loader.flatten_mapping(root)
        findings: list[Finding] = []
        for key_node, value_node in root.value:
            if not isinstance(key_node, yaml.ScalarNode):
                findings.append(Finding(rel, _line(key_node), "runner/unreadable"))
            elif key_node.value.lower() == "jobs":
                if not isinstance(value_node, yaml.MappingNode):
                    findings.append(Finding(rel, _line(key_node), "runner/unreadable"))
                    continue
                loader.flatten_mapping(value_node)
                for name_node, job in value_node.value:
                    findings += _check_job(yaml, loader, rel, name_node, job)
        return findings
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        return [Finding(rel, mark.line + 1 if mark else 1, "runner/unreadable")]
    except RecursionError:  # nesting deeper than the parser's recursion
        return [Finding(rel, 1, "runner/unreadable")]
    finally:
        loader.dispose()


def check(root: Path, base: str | None = None) -> list[Finding]:
    yaml = _load_yaml()
    findings: list[Finding] = []
    for rel in list_files(root):
        path = root / rel
        if rel in LINKED_DIRS and path.is_symlink():
            findings.append(Finding(rel, 1, "runner/symlink"))
        elif not _is_workflow(rel):
            continue
        elif path.is_symlink():
            findings.append(Finding(rel, 1, "runner/symlink"))
        elif path.is_file():
            findings += _check_text(yaml, rel, read_text(root, rel))
        else:
            findings.append(Finding(rel, 1, "runner/unreadable"))
    return findings
