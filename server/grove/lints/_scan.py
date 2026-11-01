"""What the lints share: the file walk, Python string folding, the Cypher mask."""

import ast
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CYPHER_SUFFIXES = (".cypher", ".cql")
PYTHON_SUFFIXES = (".py", ".pyw")

_PYTHON_SHEBANG = re.compile(rb"#![^\n]*\bpython")

# In a UTF-8 locale git's regex skips a line that holds an invalid UTF-8 byte; the words are ASCII.
_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


class LintError(RuntimeError):
    """The lint could not run. Never a pass: the caller exits 2."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def git(root: Path, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=_GIT_ENV)
    if done.returncode not in ok:
        detail = done.stderr.decode(errors="replace").strip()
        raise LintError(f"git {args[0]} exited {done.returncode}: {detail}")
    return done.stdout


def repo_root(root: str | None) -> Path:
    start = Path(root) if root else Path.cwd()
    return Path(git(start, "rev-parse", "--show-toplevel").decode().strip())


def list_files(root: Path) -> list[str]:
    """Every tracked and every new file, so a planted file is seen before its first commit."""
    out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({name for name in out.decode(errors="replace").split("\0") if name})


def source_files(root: Path) -> Iterator[str]:
    for rel in list_files(root):
        path = root / rel
        if path.is_file() and not path.is_symlink():
            yield rel


def is_python(root: Path, rel: str) -> bool:
    """A .py or .pyw file, or a script of any other name whose first line runs python."""
    if rel.endswith(PYTHON_SUFFIXES):
        return True
    with (root / rel).open("rb") as handle:
        return bool(_PYTHON_SHEBANG.match(handle.readline(256)))


def read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def parse_python(root: Path, rel: str) -> ast.Module | None:
    """None when the file does not parse: the caller reports that, so it cannot hide code."""
    try:
        return ast.parse((root / rel).read_bytes())
    except (SyntaxError, ValueError):
        return None


def line_of(start: int, text: str, position: int) -> int:
    return start + text.count("\n", 0, position)


def _is_string_expression(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str | bytes)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_string_expression(node.left) or _is_string_expression(node.right)
    return False


def _fold(node: ast.AST, holes: list[ast.AST]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return node.value.decode(errors="replace")
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold(part, holes) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _fold(node.left, holes) + _fold(node.right, holes)
    holes.append(node.value if isinstance(node, ast.FormattedValue) else node)
    return " "


def string_values(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Each string expression as (line, text): + folded, f-string holes and other operands blank.

    A hole can hold a string of its own, so the walk goes on inside the holes.
    """
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if _is_string_expression(node):
            holes: list[ast.AST] = []
            yield node.lineno, _fold(node, holes)
            stack.extend(holes)
        else:
            stack.extend(ast.iter_child_nodes(node))


def mask_cypher(text: str) -> str:
    """Blank Cypher literals, quoted names and comments; length and line breaks stay."""
    out = list(text)
    position, size = 0, len(text)
    while position < size:
        char = text[position]
        if text.startswith("//", position):
            end = text.find("\n", position)
            end = size if end < 0 else end
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            end = size if end < 0 else end + 2
        elif char in "'\"`":
            end = position + 1
            while end < size and text[end] != char:
                end += 2 if text[end] == "\\" and char != "`" else 1
            end = min(end + 1, size)
        else:
            position += 1
            continue
        for index in range(position, end):
            if out[index] != "\n":
                out[index] = " "
        position = end
    return "".join(out)
