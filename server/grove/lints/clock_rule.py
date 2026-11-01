"""R2: only server/grove/clock.py reads a clock, in Python or through the database.

An AST walk of every Python file resolves import aliases, so the check follows what a name
refers to and not how it is spelled. Aliases are kept per scope (module, class, function), and a
lookup reads the scope and every scope around it, so an import inside a function cannot rewrite
the module's own name. An alias comes from an import, a plain, chained, annotated or tuple
assignment, a walrus, a default argument, or a class base (a subclass stands for its base).

A reference to datetime.datetime or datetime.date, or to a subclass of one, is a finding of its
own unless it is called (a constructor), sits in an annotation, is an argument of isinstance or
issubclass, is a side of an `is` comparison, or has an attribute read from it (now, today and
utcnow are findings as reads). So the class cannot leave in a value and read the clock elsewhere.

Python strings and Cypher files are searched for the no-argument temporal functions and their
transaction, statement and realtime forms, and for those functions with only a timezone map,
which read the clock as well.

Python means a .py or .pyw file, or a file of any other name whose first line runs python.
Out of reach: JavaScript under web/, and any file that is neither Python nor Cypher.

Evasions this lint cannot close: getattr with a name, importlib or __import__ of a module, vars()
or globals() lookups, exec or eval of built text; an alias kept on an attribute or in a container
(`self.loop = loop`, `mods = [time]`), or bound by a for loop, a with or except clause, a match
pattern, or a function's return value; the time module or a loop object handed on as an argument;
`type(moment).now()` on an instance; a Cypher call assembled by join or format; a clock read
through a third-party library.
"""

import ast
import re
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

ALLOWED_PATH = "server/grove/clock.py"

_LOOP_GETTERS = frozenset(
    {"asyncio.get_event_loop", "asyncio.get_running_loop", "asyncio.new_event_loop"}
)
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
        "time.thread_time",
        "time.thread_time_ns",
        "time.clock_gettime",
        "time.clock_gettime_ns",
        "time.localtime",
        "time.gmtime",
        "time.ctime",
        "time.asctime",
        "os.times",
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
_CLOCK_MODULES = frozenset({"time", "datetime", "uuid", "asyncio", "os"})
_CLOCK_CLASSES = frozenset({"datetime.datetime", "datetime.date"})
_TYPE_CHECKS = frozenset({"isinstance", "issubclass"})
# The longest banned target has three dots; a longer chain cannot reach one, and the cap ends
# the alias walk on a self-referencing assignment.
_MAX_DOTS = 3

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_TEMPORAL = "date|datetime|time|localtime|localdatetime"
_CYPHER_READ = re.compile(
    rf"(?<![\w.])(?:"
    rf"(?:{_TEMPORAL}|timestamp)\s*\(\s*(?:\{{\s*timezone\s*:[^}}]*\}}\s*)?\)"
    rf"|(?:{_TEMPORAL})\s*\.\s*(?:transaction|statement|realtime)\s*\("
    rf")",
    re.IGNORECASE,
)


class _Scope:
    def __init__(self, parent: "_Scope | None") -> None:
        self.parent = parent
        self.names: dict[str, set[str]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def lookup(self, name: str) -> set[str]:
        """Every target the name can hold here: a union, so a shadowed outer alias still counts."""
        found: set[str] = set()
        scope: _Scope | None = self
        while scope is not None:
            found |= scope.names.get(name, set())
            scope = scope.parent
        return found


def _annotation_parts(node: ast.AST) -> tuple[ast.AST | None, ...]:
    if isinstance(node, ast.arg | ast.AnnAssign):
        return (node.annotation,)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return (node.returns,)
    if isinstance(node, ast.TypeAlias):
        return (node.value,)
    if isinstance(node, ast.TypeVar):
        return (node.bound,)
    return ()


class _Aliases:
    """The scope and parent of every node of one file, and what each name can refer to."""

    def __init__(self, tree: ast.Module) -> None:
        self.root = _Scope(None)
        self.scope_of: dict[ast.AST, _Scope] = {}
        self.inner: dict[ast.AST, _Scope] = {}
        self.parent: dict[ast.AST, ast.AST] = {}
        self.annotations: set[ast.AST] = set()

        stack: list[tuple[ast.AST, _Scope]] = [(tree, self.root)]
        while stack:
            node, scope = stack.pop()
            self.scope_of[node] = scope
            inside = scope
            if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                inside = self.inner[node] = _Scope(scope)
            elif isinstance(node, ast.Global):
                scope.globals.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                scope.nonlocals.update(node.names)
            for part in _annotation_parts(node):
                if part is not None:
                    self.annotations.update(ast.walk(part))
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
                stack.append((child, inside))

        while any([self._collect(node) for node in ast.walk(tree)]):
            pass

    def targets(self, node: ast.AST) -> set[str]:
        """The dotted targets an expression can refer to; empty when unknown."""
        if isinstance(node, ast.Name):
            return self.scope_of[node].lookup(node.id)
        if isinstance(node, ast.Attribute):
            bases = self.targets(node.value)
            return {f"{base}.{node.attr}" for base in bases if base.count(".") < _MAX_DOTS}
        if isinstance(node, ast.Call) and self.targets(node.func) & _LOOP_GETTERS:
            return {_LOOP}
        return set()

    def _add(self, scope: _Scope, name: str, found: set[str]) -> bool:
        if name in scope.globals:
            scope = self.root
        elif name in scope.nonlocals and scope.parent is not None:
            scope = scope.parent
        held = scope.names.setdefault(name, set())
        new = found - held
        held |= new
        return bool(new)

    def _clock_targets(self, node: ast.AST) -> set[str]:
        return {found for found in self.targets(node) if found.split(".")[0] in _CLOCK_MODULES}

    def _bind(self, scope: _Scope, target: ast.AST, value: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return self._add(scope, target.id, self._clock_targets(value))
        pairs = isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List)
        if pairs and len(target.elts) == len(value.elts):
            return any(
                [self._bind(scope, t, v) for t, v in zip(target.elts, value.elts, strict=True)]
            )
        return False

    def _collect(self, node: ast.AST) -> bool:
        """Record the aliases one node binds; True when something new was learned."""
        scope = self.scope_of[node]
        changed = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                changed.append(
                    self._add(scope, alias.asname, {alias.name})
                    if alias.asname
                    else self._add(scope, top, {top})
                )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                name = alias.asname or alias.name
                changed.append(self._add(scope, name, {f"{node.module}.{alias.name}"}))
        elif isinstance(node, ast.Assign):
            changed += [self._bind(scope, target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            changed.append(self._bind(scope, node.target, node.value))
        elif isinstance(node, _FUNCTIONS):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            defaults = [
                *zip(
                    positional[len(positional) - len(args.defaults) :], args.defaults, strict=True
                ),
                *zip(args.kwonlyargs, args.kw_defaults, strict=True),
            ]
            changed += [
                self._add(self.inner[node], arg.arg, self._clock_targets(default))
                for arg, default in defaults
                if default is not None
            ]
        elif isinstance(node, ast.ClassDef):
            found = set().union(*(self._clock_targets(base) for base in node.bases))
            changed.append(self._add(scope, node.name, found))
        return any(changed)

    def harmless_class_use(self, node: ast.AST) -> bool:
        """A use of a clock class that cannot carry it on to a clock read."""
        parent = self.parent.get(node)
        if node in self.annotations or isinstance(parent, ast.Attribute):
            return True
        if isinstance(parent, ast.Compare):
            return all(isinstance(op, ast.Is | ast.IsNot) for op in parent.ops)
        call = self.parent.get(parent) if isinstance(parent, ast.Tuple) else parent
        if not isinstance(call, ast.Call):
            return False
        if call.func is node:
            return True
        checked = isinstance(call.func, ast.Name) and call.func.id in _TYPE_CHECKS
        return checked and (node in call.args or parent in call.args)


def _python_reads(tree: ast.Module) -> list[tuple[int, str]]:
    aliases = _Aliases(tree)
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*" and node.module in _CLOCK_MODULES:
                    reads.append((node.lineno, "clock/star-import"))
                elif f"{node.module}.{alias.name}" in _BANNED:
                    reads.append((node.lineno, "clock/python"))
        elif isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
            found = aliases.targets(node)
            if found & _BANNED:
                reads.append((node.lineno, "clock/python"))
            elif found & _CLOCK_CLASSES and not aliases.harmless_class_use(node):
                reads.append((node.lineno, "clock/class-value"))
        elif isinstance(node, ast.Call):
            if _STRFTIME in aliases.targets(node.func) and len(node.args) < 2:
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
        if rel.endswith(CYPHER_SUFFIXES):
            text = read_text(root, rel)
            reads = _cypher_reads(1, text, mask_cypher(text))
            findings += [Finding(rel, line, rule) for line, rule in reads]
        elif is_python(root, rel):
            findings += _check_python(root, rel)
    return findings
