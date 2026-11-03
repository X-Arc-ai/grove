"""The writer rule: outside the writer's folders nothing opens a write path to the store.

Neo4j Community has no roles, so this lint stands where a grant would. A string (text or bytes)
counts as Cypher when a clause pattern matches it after its literals are blanked; then any write
word in it is a finding. Prose has no clause pattern, so prose stays clean. A clause pattern
allows a path variable (`MATCH p = (`) and the Cypher 25 match modes and selectors (ANY, ALL,
SHORTEST, REPEATABLE ELEMENTS, DIFFERENT RELATIONSHIPS, with their counts) before the pattern.

Production code under server/grove/ may not import the tests package, so it cannot reach a write
helper in server/tests/writer/.

Python means a .py or .pyw file, or a file of any other name whose first line runs python.

Inside the writer package, a second rule holds D8's mechanical boundary (the Oct 2 drill): write
Cypher that names a registry label (kinds, meanings, property names, the ontology change log) sits
only in the ontology operations and the world seed, and write Cypher that names the judgments label
only in the judgments writer (S20). An alias row is left out: other operations write item aliases.

Evasions this lint cannot close: getattr, importlib or __import__ with a name built at run time;
exec or eval of built text; Cypher assembled by join, format, % or +=, or held in named constants
that are joined later; a write word cut in two by an f-string hole; a fragment that has a write
word and no clause pattern of its own; a shell script with no .sh or .bash suffix and no python
first line; Cypher in a file type outside the walk, such as YAML; the query of a procedure that
writes; a tests module reached through a symlink, which the walk skips.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from grove.lints._scan import (
    CYPHER_SUFFIXES,
    Finding,
    is_python,
    line_of,
    mask_cypher,
    parse_python,
    read_text,
    source_files,
    string_values,
)

ALLOWED_PREFIXES = ("server/grove/writer/", "server/migrations/", "server/tests/writer/")
WRITER_PREFIX = "server/grove/writer/"
# D8 (line 1641): the registry changes only through the ontology operations.
GUARDED_LABELS = {
    "Kind": ("server/grove/writer/ontology.py", "server/grove/writer/worlds.py"),
    "Meaning": ("server/grove/writer/ontology.py", "server/grove/writer/worlds.py"),
    "PropertyName": ("server/grove/writer/ontology.py",),
    "OntologyChange": ("server/grove/writer/ontology.py",),
    "Judgment": ("server/grove/writer/judgments.py",),
}
_LABEL = re.compile(r":\s*`?(" + "|".join(GUARDED_LABELS) + r")\b")
NEO4J_PREFIX = "server/grove/store/"
PRODUCTION_PREFIX = "server/grove/"
TESTS_PACKAGE = "tests"

_SHELL_SUFFIXES = (".sh", ".bash")
_ENTRY_POINTS = frozenset(
    "_".join(parts)
    for parts in (("execute", "write"), ("execute", "query"), ("begin", "transaction"))
)

_MODE_WORDS = r"any|all|shortest|repeatable|different|elements?|relationships?|paths?|groups?|\d+"
_PATH = r"\s+\w+\s*="
_MATCH_HEAD = rf"(?:\s+(?:{_MODE_WORDS})\b|{_PATH})*"
_SHAPE = re.compile(
    "|".join(
        (
            rf"\b(?:optional\s+)?match{_MATCH_HEAD}\s*\(",
            rf"\b(?:create|merge)(?:{_PATH})?\s*\(",
            r"\bcreate\s+(?:constraint|index|fulltext|text|range|point|lookup|vector|or\s+replace)\b",
            r"\bdrop\s+(?:constraint|index)\b",
            r"\bunwind\b.+\bas\b",
            r"\bcall\s*\{",
            r"\bforeach\s*\(",
            r"\bload\s+csv\b",
        )
    ),
    re.IGNORECASE | re.DOTALL,
)
_WRITE_WORD = re.compile(
    r"\b(?:create|merge|set|delete|remove|detach|foreach|drop)\b", re.IGNORECASE
)
_HEREDOC = re.compile(r"<<-?\s*\\?(['\"]?)([^\s'\"<>|;&()]+)\1")


def _writes(start: int, text: str, *, whole_file: bool = False) -> set[int]:
    masked = mask_cypher(text)
    if not whole_file and not _SHAPE.search(masked):
        return set()
    return {line_of(start, masked, match.start()) for match in _WRITE_WORD.finditer(masked)}


def _names(node: ast.AST) -> tuple[str | None, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (node.attr,)
    if isinstance(node, ast.alias):
        return (node.name, node.asname)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return (node.name,)
    return ()


def _imports_neo4j(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "neo4j" for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return node.module.split(".")[0] == "neo4j"
    return False


def _imports_tests(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == TESTS_PACKAGE for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return node.module.split(".")[0] == TESTS_PACKAGE
    return False


def _check_tests_imports(root: Path, rel: str) -> list[Finding]:
    tree = parse_python(root, rel)
    if tree is None:
        return [Finding(rel, 1, "writer/unparsable")]
    return [
        Finding(rel, node.lineno, "writer/tests-import")
        for node in ast.walk(tree)
        if _imports_tests(node)
    ]


def _check_python(root: Path, rel: str) -> list[Finding]:
    tree = parse_python(root, rel)
    if tree is None:
        return [Finding(rel, 1, "writer/unparsable")]

    findings = []
    for node in ast.walk(tree):
        if not _ENTRY_POINTS.isdisjoint(_names(node)):
            findings.append(Finding(rel, node.lineno, "writer/entry-point"))
        if _imports_neo4j(node) and not rel.startswith(NEO4J_PREFIX):
            findings.append(Finding(rel, node.lineno, "writer/neo4j-import"))
    for start, text in string_values(tree):
        if text.strip() in _ENTRY_POINTS:
            findings.append(Finding(rel, start, "writer/entry-point"))
        findings += [Finding(rel, line, "writer/cypher") for line in _writes(start, text)]
    return findings


def _check_registry(root: Path, rel: str) -> list[Finding]:
    """Write Cypher in the writer package that names a guarded label outside its owner files."""
    tree = parse_python(root, rel)
    if tree is None:
        return []
    findings = []
    for start, text in string_values(tree):
        if not _writes(start, text):
            continue
        masked = mask_cypher(text)
        for match in _LABEL.finditer(masked):
            if rel not in GUARDED_LABELS[match.group(1)]:
                findings.append(
                    Finding(rel, line_of(start, masked, match.start()), "writer/registry")
                )
    return findings


def _closing_quote(text: str, start: int) -> int:
    quote = text[start]
    position = start + 1
    while position < len(text) and text[position] != quote:
        position += 2 if text[position] == "\\" and quote == '"' else 1
    return min(position, len(text))


def _shell_chunks(text: str) -> Iterator[tuple[int, str]]:
    """Quoted strings, heredoc bodies and what lies between them, as (offset, text).

    Each one is judged alone: a whole-file search would pair a shell `set -e` with a read query.
    """
    plain = position = 0
    while position < len(text):
        char = text[position]
        heredoc = _HEREDOC.match(text, position) if char == "<" else None
        if char == "\\":
            position += 2
        elif char == "#" and (position == 0 or text[position - 1].isspace()):
            yield plain, text[plain:position]
            end = text.find("\n", position)
            plain = position = len(text) if end < 0 else end
        elif char in "'\"":
            end = _closing_quote(text, position)
            yield plain, text[plain:position]
            yield position + 1, text[position + 1 : end]
            plain = position = end + 1
        elif heredoc:
            yield plain, text[plain:position]
            body = text.find("\n", heredoc.end()) + 1 or len(text)
            delimiter = re.escape(heredoc.group(2))
            close = re.compile(rf"^\t*{delimiter}[ \t]*$", re.MULTILINE).search(text, body)
            yield body, text[body : close.start() if close else len(text)]
            plain = position = close.end() if close else len(text)
        else:
            position += 1
    yield plain, text[plain:]


def _check_shell(rel: str, text: str) -> list[Finding]:
    findings = []
    for offset, chunk in _shell_chunks(text):
        start = line_of(1, text, offset)
        findings += [Finding(rel, line, "writer/cypher") for line in _writes(start, chunk)]
    return findings


def check(root: Path, base: str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for rel in source_files(root):
        python = not rel.endswith(CYPHER_SUFFIXES) and is_python(root, rel)
        if python and rel.startswith(PRODUCTION_PREFIX):
            findings += _check_tests_imports(root, rel)
        if python and rel.startswith(WRITER_PREFIX):
            findings += _check_registry(root, rel)
        if rel.startswith(ALLOWED_PREFIXES):
            continue
        if rel.endswith(CYPHER_SUFFIXES):
            lines = _writes(1, read_text(root, rel), whole_file=True)
            findings += [Finding(rel, line, "writer/cypher") for line in lines]
        elif python:
            findings += _check_python(root, rel)
        elif rel.endswith(_SHELL_SUFFIXES):
            findings += _check_shell(rel, read_text(root, rel))
    return findings
