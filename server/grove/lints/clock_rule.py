"""R2: only server/grove/clock.py reads a clock, in Python or through the database.

An AST walk of every Python file resolves import aliases, so the check follows what a name
refers to and not how it is spelled. Python strings and Cypher files are searched for the
no-argument temporal functions and their transaction, statement and realtime forms, and for
those functions with only a timezone map, which read the clock as well.

Out of reach: JavaScript under web/, and any file that is neither Python nor Cypher.

Evasions this lint cannot close: getattr with a name, importlib or __import__ of a module, a
subclass of a clock type that calls the parent, a loop object kept on an attribute, exec or eval
of built text, a Cypher call assembled by join or format, and a clock read through a third-party
library.
"""

import ast
import re
from pathlib import Path

from grove.lints._scan import (
    CYPHER_SUFFIXES,
    Finding,
    line_of,
    mask_cypher,
    parse_python,
    read_text,
    source_files,
    string_values,
)

ALLOWED_PATH = "server/grove/clock.py"

_LOOP_GETTERS = frozenset({"asyncio.get_event_loop", "asyncio.get_running_loop"})
_LOOP = "asyncio.<loop>"

_BANNED = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.process_time_ns",
        "time.clock_gettime",
        "time.clock_gettime_ns",
        "time.localtime",
        "time.gmtime",
        "time.ctime",
        "time.asctime",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "uuid.uuid1",
        "uuid.uuid6",
        "uuid.uuid7",
        f"{_LOOP}.time",
    }
)
_STRFTIME = "time.strftime"
_CLOCK_MODULES = frozenset({"time", "datetime", "uuid", "asyncio"})

_TEMPORAL = "date|datetime|time|localtime|localdatetime"
_CYPHER_READ = re.compile(
    rf"(?<![\w.])(?:"
    rf"(?:{_TEMPORAL}|timestamp)\s*\(\s*(?:\{{\s*timezone\s*:[^}}]*\}}\s*)?\)"
    rf"|(?:{_TEMPORAL})\s*\.\s*(?:transaction|statement|realtime)\s*\("
    rf")",
    re.IGNORECASE,
)


def _dotted(node: ast.AST, names: dict[str, str]) -> str | None:
    """The dotted target of an expression, through the module's aliases; None when unknown."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, names)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call) and _dotted(node.func, names) in _LOOP_GETTERS:
        return _LOOP
    return None


def _aliases(tree: ast.Module) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = _dotted(node.value, names)
            if isinstance(target, ast.Name) and value:
                if value.split(".")[0] in _CLOCK_MODULES:
                    names[target.id] = value
    return names


def _python_reads(tree: ast.Module) -> list[tuple[int, str]]:
    names = _aliases(tree)
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*" and node.module in _CLOCK_MODULES:
                    reads.append((node.lineno, "clock/star-import"))
                elif f"{node.module}.{alias.name}" in _BANNED:
                    reads.append((node.lineno, "clock/python"))
        elif isinstance(node, ast.Name | ast.Attribute):
            if _dotted(node, names) in _BANNED:
                reads.append((node.lineno, "clock/python"))
        elif isinstance(node, ast.Call):
            if _dotted(node.func, names) == _STRFTIME and len(node.args) < 2:
                reads.append((node.lineno, "clock/python"))
    return reads


def _cypher_reads(start: int, text: str, masked: str | None = None) -> list[tuple[int, str]]:
    """Match on the raw text: a mask would turn a call with a literal into one with no argument."""
    return [
        (line_of(start, text, match.start()), "clock/cypher")
        for match in _CYPHER_READ.finditer(text)
        if masked is None or masked[match.start()] != " "
    ]


def _check_python(root: Path, rel: str) -> list[Finding]:
    tree = parse_python(root, rel)
    if tree is None:
        return [Finding(rel, 1, "clock/unparsable")]
    reads = _python_reads(tree)
    for line, text in string_values(tree):
        reads += _cypher_reads(line, text)
    return [Finding(rel, line, rule) for line, rule in reads]


def check(root: Path, base: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel in source_files(root):
        if rel == ALLOWED_PATH:
            continue
        if rel.endswith(".py"):
            findings += _check_python(root, rel)
        elif rel.endswith(CYPHER_SUFFIXES):
            text = read_text(root, rel)
            reads = _cypher_reads(1, text, mask_cypher(text))
            findings += [Finding(rel, line, rule) for line, rule in reads]
    return findings
