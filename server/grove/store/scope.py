"""World scoping: Neo4j Community has no row-level rule, so this parser stands where one would.

scope_cypher takes developer-written Cypher and adds world_id to every world node pattern and link
pattern, at exact offsets of the original text. It reads a masked copy, so a literal or a comment
cannot fool it. It is a parser of patterns and clauses and not of all Cypher: what it does not
understand it refuses, and clause names it does not know are refused by name.

Why the bound-variable rule follows WITH, UNION and subqueries: a variable that a projection
dropped is a fresh variable to the server, so a later bare node pattern would scan every world.

Why a zero lower bound on a variable-length link is refused: a zero-length match binds both ends
to one node with no link at all, so the link's world_id no longer anchors the pattern.

Why quoted names, dynamic labels and map parameters in a pattern are refused: each one hides a name
or a property from this parser. A `\\u` escape is refused because the server decodes it before it
reads the text, so it can spell a quote or a bracket that the mask does not see.

Why shortestPath is refused in a world query: the server rejects a property map inside it, so a
scoped form does not exist. Use the SHORTEST path selector.

Why no name may be spelled as, starts or ends: the word after the AS keyword is an alias and the
WITH after STARTS or ENDS is an operator, so a name with that spelling would hide the clause that
follows it. With the names refused, each of these words is always the keyword.

Out of reach: a query that reads a world's own data and returns it as a plain scalar is correct by
construction, and the WorldLeak guard does not see scalars.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from grove.store.catalogue import PLATFORM_LABELS, RELATIONSHIP_TYPES, WORLD_LABELS
from grove.store.errors import ScopeError

MODES = ("read", "write", "platform")
WORLD_PARAM = "world_id"

# Ruling S36: the full-text analyzer splits on hyphens and folds case, and with this form the
# tokens of one world id never occur inside another's unless both share the UUID.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_WORLD_ID = re.compile(rf"(?:t_[a-z0-9-]+_)?{_UUID}")
_STRING_FILL = '"'
_LINE_END = re.compile(r"[\r\n]")

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
    |(?P<string>"+)
    |(?P<param>\$[A-Za-z_][A-Za-z0-9_]*)
    |(?P<word>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    |(?P<punct>[()\[\]{}:,.|&!%*+\-/<>=^~])
    """,
    re.VERBOSE,
)
_OPENERS = {"(": ")", "[": "]", "{": "}"}

_SUBQUERY_WORDS = frozenset({"EXISTS", "COUNT", "COLLECT"})
_SHORTEST_FUNCTIONS = frozenset({"SHORTESTPATH", "ALLSHORTESTPATHS"})
_PATH_SELECTORS = frozenset(
    {
        "ACYCLIC",
        "ALL",
        "ANY",
        "DIFFERENT",
        "ELEMENTS",
        "GROUP",
        "GROUPS",
        "PATH",
        "PATHS",
        "RELATIONSHIPS",
        "REPEATABLE",
        "SHORTEST",
        "TRAIL",
        "WALK",
    }
)
_REFUSED_CLAUSES = {
    "USE": "USE is refused: one store, one world",
    "LOAD": "LOAD CSV is refused",
    "INSERT": "INSERT is refused: the scoper reads CREATE",
    "LET": "LET is refused",
    "FILTER": "FILTER is refused: use WHERE",
    "NEXT": "NEXT is refused",
    "SEARCH": "SEARCH is refused here: use WorldQuery.search",
    "SHOW": "SHOW is refused",
    "TERMINATE": "TERMINATE is refused",
    "FOR": "FOR is refused: use UNWIND",
}
# Words that stand only as keywords; see the module docstring.
_KEYWORD_ONLY = frozenset({"AS", "STARTS", "ENDS"})
# Keywords that need an operand after them, so the token before AS or STARTS is never one of them.
_OPERAND_KEYWORDS = frozenset(
    {
        "ALL",
        "AND",
        "BY",
        "CALL",
        "CASE",
        "CONTAINS",
        "CREATE",
        "DELETE",
        "DETACH",
        "DISTINCT",
        "ELSE",
        "FOREACH",
        "IN",
        "IS",
        "LIMIT",
        "MATCH",
        "MERGE",
        "NODETACH",
        "NOT",
        "OFFSET",
        "ON",
        "OPTIONAL",
        "OR",
        "ORDER",
        "REMOVE",
        "RETURN",
        "SET",
        "SKIP",
        "THEN",
        "UNION",
        "UNWIND",
        "WHEN",
        "WHERE",
        "WITH",
        "XOR",
        "YIELD",
    }
)
_CLAUSES = frozenset(
    {
        "CALL",
        "CREATE",
        "DELETE",
        "DETACH",
        "FINISH",
        "FOREACH",
        "LIMIT",
        "MATCH",
        "MERGE",
        "NODETACH",
        "OFFSET",
        "ON",
        "OPTIONAL",
        "ORDER",
        "REMOVE",
        "RETURN",
        "SET",
        "SKIP",
        "UNION",
        "UNWIND",
        "WHERE",
        "WITH",
    }
) | frozenset(_REFUSED_CLAUSES)
_EXPRESSION_CLAUSES = frozenset(
    {"DELETE", "DETACH", "LIMIT", "NODETACH", "OFFSET", "ORDER", "SKIP", "WHERE"}
)


def literal_spans(text: str) -> Iterator[tuple[int, int, str]]:
    """(start, end, kind) of each comment, string and quoted name; kind is comment, string, name."""
    position, size = 0, len(text)
    while position < size:
        char = text[position]
        if text.startswith("//", position):
            # The server ends a line comment at a lone carriage return too.
            found = _LINE_END.search(text, position)
            end, kind = (size if found is None else found.start()), "comment"
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                raise ScopeError("a comment has no end")
            end, kind = end + 2, "comment"
        elif char in "'\"`":
            end = position + 1
            while end < size and text[end] != char:
                end += 2 if text[end] == "\\" and char != "`" else 1
            if end >= size:
                raise ScopeError("a literal has no end")
            end, kind = end + 1, ("name" if char == "`" else "string")
        else:
            position += 1
            continue
        yield position, end, kind
        position = end


def mask(cypher: str) -> str:
    """Same length as the input: comments blank, each string a run of one filler character."""
    if "\\u" in cypher:
        raise ScopeError("a unicode escape is refused: write the character")
    chars = list(cypher)
    for start, end, kind in literal_spans(cypher):
        if kind == "name":
            raise ScopeError("a quoted name is refused: the catalogue has none")
        fill = _STRING_FILL if kind == "string" else " "
        chars[start:end] = [
            char if char in "\r\n" and kind == "comment" else fill for char in cypher[start:end]
        ]
    return "".join(chars)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Node:
    start: int
    close: int
    variable: str | None
    labels: tuple[str, ...]
    label_end: int  # token index of the last token before the map
    map_at: int | None
    map_param: bool
    where_at: int | None


@dataclass(frozen=True)
class _Link:
    open: int
    close: int
    end: int  # token index after the link
    spec_end: int
    map_at: int | None
    where_at: int | None
    types: tuple[str, ...]


def _tokenize(masked: str) -> tuple[list[_Token], dict[int, int]]:
    tokens: list[_Token] = []
    closes: dict[int, int] = {}
    stack: list[int] = []
    position = 0
    while position < len(masked):
        match = _TOKEN.match(masked, position)
        if match is None:
            raise ScopeError(f"a character the scoper does not read at offset {position}")
        position = match.end()
        kind = match.lastgroup or ""
        if kind == "space":
            continue
        text = match.group()
        if text in _OPENERS:
            stack.append(len(tokens))
        elif text in _OPENERS.values():
            if not stack or _OPENERS[tokens[stack[-1]].text] != text:
                raise ScopeError(f"a bracket does not match at offset {match.start()}")
            closes[stack.pop()] = len(tokens)
        tokens.append(_Token(kind, text, match.start(), match.end()))
    if stack:
        raise ScopeError("a bracket has no end")
    return tokens, closes


class _Scoper:
    def __init__(self, cypher: str, mode: str) -> None:
        self.text = cypher
        self.mode = mode
        self.stamps = mode != "platform"
        self.labels = PLATFORM_LABELS if mode == "platform" else WORLD_LABELS
        self.other_labels = WORLD_LABELS if mode == "platform" else PLATFORM_LABELS
        self.types = frozenset() if mode == "platform" else RELATIONSHIP_TYPES
        self.tokens, self.closes = _tokenize(mask(cypher))
        self.edits: list[tuple[int, str]] = []
        self.bound: set[str] = set()

    def scoped(self) -> str:
        self.refuse_keyword_names()
        if self.stamps:
            for token in self.tokens:
                if token.kind == "param" and token.text[1:] == WORLD_PARAM:
                    raise ScopeError("$world_id is bound by the layer, never by the caller")
        if self.mode == "write":
            # Anywhere, not only in SET: a map variable carries the key to a later `+=`.
            for index, token in enumerate(self.tokens):
                if token.text == WORLD_PARAM and self.is_punct(index + 1, ":"):
                    raise ScopeError("a pattern or map that states world_id is refused")
        self.query(0, len(self.tokens), set())
        text = self.text
        for offset, insert in sorted(self.edits, reverse=True):
            text = text[:offset] + insert + text[offset:]
        return text

    def is_punct(self, index: int, text: str) -> bool:
        if index >= len(self.tokens):
            return False
        token = self.tokens[index]
        return token.kind == "punct" and token.text == text

    def is_name(self, index: int) -> bool:
        return index < len(self.tokens) and self.tokens[index].kind == "word"

    def ends_operand(self, index: int) -> bool:
        """Whether the token at index can close an expression, as the token before AS must."""
        if index < 0:
            return False
        token = self.tokens[index]
        if token.kind in ("string", "number", "param"):
            return True
        if token.kind == "punct":
            return token.text in ")]}"
        named = index and self.tokens[index - 1].text in ".:"
        return bool(named) or token.text.upper() not in _OPERAND_KEYWORDS

    def refuse_keyword_names(self) -> None:
        """AS must sit between an operand and a plain alias, STARTS and ENDS between an operand
        and WITH. Anything else is a name spelled like the keyword."""
        for index, token in enumerate(self.tokens):
            upper = token.text.upper() if token.kind == "word" else ""
            if upper not in _KEYWORD_ONLY:
                continue
            following = self.tokens[index + 1].text.upper() if self.is_name(index + 1) else ""
            if upper == "AS":
                keyword = following not in ("", *_KEYWORD_ONLY)
            else:
                keyword = following == "WITH"
            if not (keyword and self.ends_operand(index - 1)):
                raise ScopeError(f"a name spelled {token.text!r} is refused: it is a keyword")

    def word(self, index: int) -> str:
        """The upper-case keyword at index; '' for a property, a label, an alias or a non-word."""
        if not self.is_name(index):
            return ""
        before = self.tokens[index - 1] if index else None
        if before is not None:
            if before.kind == "punct" and before.text in ".:":
                return ""
            if before.kind == "word" and before.text.upper() == "AS":
                return ""
        text = self.tokens[index].text.upper()
        if text == "WITH" and before is not None and before.text.upper() in ("STARTS", "ENDS"):
            return ""
        return text

    def clause_end(self, index: int, end: int) -> int:
        while index < end:
            if self.word(index) in _CLAUSES:
                return index
            index = self.closes[index] + 1 if index in self.closes else index + 1
        return end

    def split(self, index: int, end: int) -> Iterator[tuple[int, int]]:
        """The ranges between the commas at nesting level zero."""
        start = index
        while index < end:
            if self.is_punct(index, ","):
                yield start, index
                start = index + 1
            index = self.closes[index] + 1 if index in self.closes else index + 1
        if start < end:
            yield start, end

    def query(self, index: int, end: int, bound: set[str]) -> set[str]:
        """Walk the clauses of a query; return the node variables its RETURN gives back."""
        outer = self.bound
        self.bound = set(bound)
        returned: set[str] = set()
        while index < end:
            word = self.word(index)
            if word == "RETURN":
                index, returned = self.projection(index + 1, end)
            else:
                index = self.clause(word, index, end, bound)
        self.bound = outer
        return returned

    def clause(self, word: str, index: int, end: int, initial: set[str]) -> int:
        if word in _REFUSED_CLAUSES:
            raise ScopeError(_REFUSED_CLAUSES[word])
        if word in _EXPRESSION_CLAUSES:
            stop = self.clause_end(index + 1, end)
            self.expression(index + 1, stop)
            return stop
        if word == "MATCH":
            return self.patterns(index + 1, end)
        if word in ("CREATE", "MERGE"):
            if not (self.is_punct(index + 1, "(") or self.is_punct(index + 2, "=")):
                raise ScopeError(f"{word} of anything but a pattern is refused")
            return self.patterns(index + 1, end, creates=self.mode == "write")
        if word == "OPTIONAL":
            if self.word(index + 1) not in ("MATCH", "CALL"):
                raise ScopeError("OPTIONAL needs MATCH or CALL")
            return index + 1
        if word == "ON":
            if self.word(index + 2) != "SET":
                raise ScopeError("ON needs CREATE SET or MATCH SET")
            return index + 2
        if word == "WITH":
            index, names = self.projection(index + 1, end)
            self.bound = names
            return index
        if word == "UNWIND":
            return self.unwind(index + 1, end)
        if word == "SET":
            return self.updates(index + 1, end, self.set_item)
        if word == "REMOVE":
            return self.updates(index + 1, end, self.remove_item)
        if word == "FOREACH":
            return self.foreach(index + 1)
        if word == "CALL":
            return self.call(index + 1)
        if word == "UNION":
            self.bound = set(initial)
            return index + 2 if self.word(index + 1) in ("ALL", "DISTINCT") else index + 1
        if word == "FINISH":
            return index + 1
        raise ScopeError(f"a clause the scoper does not know: {self.tokens[index].text!r}")

    def projection(self, index: int, end: int) -> tuple[int, set[str]]:
        stop = self.clause_end(index, end)
        if self.word(index) == "DISTINCT":
            index += 1
        names: set[str] = set()
        for start, finish in self.split(index, stop):
            alias = finish - start >= 3 and self.tokens[finish - 2].text.upper() == "AS"
            source_end = finish - 2 if alias else finish
            self.expression(start, source_end)
            if source_end - start == 1:
                if self.is_punct(start, "*"):
                    names |= self.bound
                elif self.tokens[start].text in self.bound:
                    names.add(self.tokens[finish - 1].text)
        return stop, names

    def unwind(self, index: int, end: int) -> int:
        stop = self.clause_end(index, end)
        if stop - index < 3 or self.tokens[stop - 2].text.upper() != "AS":
            raise ScopeError("UNWIND needs AS")
        self.expression(index, stop - 2)
        return stop

    def foreach(self, index: int) -> int:
        if not self.is_punct(index, "("):
            raise ScopeError("FOREACH needs a parenthesis")
        close = self.closes[index]
        pipe = index + 1
        while pipe < close and not self.is_punct(pipe, "|"):
            pipe = self.closes[pipe] + 1 if pipe in self.closes else pipe + 1
        if pipe >= close:
            raise ScopeError("FOREACH needs a pipe")
        self.expression(index + 1, pipe)
        self.query(pipe + 1, close, self.bound)
        return close + 1

    def call(self, index: int) -> int:
        imported: set[str] = set()
        if self.is_punct(index, "("):
            close = self.closes[index]
            for position in range(index + 1, close):
                if self.is_punct(position, "*"):
                    imported |= self.bound
                elif self.is_name(position) and self.tokens[position].text in self.bound:
                    imported.add(self.tokens[position].text)
            index = close + 1
        if not self.is_punct(index, "{"):
            raise ScopeError("CALL of a procedure is refused")
        close = self.closes[index]
        self.bound |= self.query(index + 1, close, imported)
        return close + 1

    def updates(self, index: int, end: int, item) -> int:
        stop = self.clause_end(index, end)
        if self.mode != "write":
            self.expression(index, stop)
            return stop
        for start, finish in self.split(index, stop):
            item(start, finish)
        return stop

    def refuse_world_property(self, index: int) -> None:
        if self.tokens[index].text == WORLD_PARAM:
            raise ScopeError("a write to world_id is refused")

    def set_item(self, start: int, finish: int) -> None:
        if self.is_name(start) and self.is_punct(start + 1, ".") and self.is_name(start + 2):
            self.refuse_world_property(start + 2)
            if self.is_punct(start + 3, "="):
                self.expression(start + 4, finish)
                return
        elif self.is_name(start) and self.is_punct(start + 1, "="):
            raise ScopeError("a whole-map assignment is refused: it can drop world_id")
        elif (
            self.is_name(start) and self.is_punct(start + 1, "+") and self.is_punct(start + 2, "=")
        ):
            self.expression(start + 3, finish)
            return
        elif self.is_name(start) and self.is_punct(start + 1, ":"):
            self.label_test(start + 1)
            return
        raise ScopeError("a write form the scoper does not read")

    def remove_item(self, start: int, finish: int) -> None:
        if self.is_name(start) and self.is_punct(start + 1, ".") and self.is_name(start + 2):
            self.refuse_world_property(start + 2)
            if finish == start + 3:
                return
        elif self.is_name(start) and self.is_punct(start + 1, ":"):
            self.label_test(start + 1)
            return
        raise ScopeError("a write form the scoper does not read")

    def patterns(self, index: int, end: int, *, creates: bool = False) -> int:
        while index < end:
            if self.word(index) in _CLAUSES:
                return index
            if self.is_punct(index, "("):
                index, _ = self.path(index, end, creates)
            elif self.is_punct(index, ","):
                index += 1
            elif self.is_name(index) and self.is_punct(index + 1, "="):
                index += 2
            elif self.tokens[index].text.upper() in _SHORTEST_FUNCTIONS and self.stamps:
                raise ScopeError("shortestPath has no scoped form: use the SHORTEST selector")
            elif (
                self.tokens[index].kind == "number"
                or self.tokens[index].text.upper() in _PATH_SELECTORS
            ):
                index += 1
            else:
                raise ScopeError(f"a pattern the scoper does not read: {self.tokens[index].text!r}")
        return index

    def path(self, index: int, end: int, creates: bool = False) -> tuple[int, bool]:
        """One path pattern from a parenthesis; returns the next index and whether it links."""
        nodes: list[_Node] = []
        linked = False
        while index < end and self.is_punct(index, "("):
            node = self.node_at(index)
            if node is not None:
                nodes.append(node)
                index = node.close + 1
            else:
                close = self.closes[index]
                inner, inner_linked = self.path(index + 1, close, creates)
                if self.word(inner) == "WHERE":
                    self.expression(inner + 1, close)
                elif inner != close:
                    raise ScopeError("a pattern group the scoper does not read")
                linked = linked or inner_linked
                index = self.quantifier(close + 1)
            link = self.link_at(index)
            if link is not None:
                linked = True
                self.finish_link(link)
                index = self.quantifier(link.end)
                if not self.is_punct(index, "("):
                    raise ScopeError("a link needs a node after it")
        for node in nodes:
            self.finish_node(node, linked, creates)
        return index, linked

    def quantifier(self, index: int) -> int:
        if self.is_punct(index, "+"):
            return index + 1
        if self.is_punct(index, "*"):
            raise ScopeError("a zero lower bound on a link is refused: use + or {1,n}")
        if self.is_punct(index, "{"):
            close = self.closes[index]
            low = self.tokens[index + 1]
            if low.kind != "number" or self.bound_value(index + 1) == 0:
                raise ScopeError("a zero lower bound on a link is refused")
            for position in range(index + 2, close):
                if self.tokens[position].kind == "number":
                    self.bound_value(position)
            return close + 1
        return index

    def bound_value(self, index: int) -> int:
        text = self.tokens[index].text
        if not text.isdigit():
            raise ScopeError("a bound on a link must be a whole number")
        return int(text)

    def label_expression(self, index: int) -> tuple[tuple[str, ...], int]:
        """Names joined by |, & or :, from the first name; returns the names and the next index."""
        names = []
        while True:
            if not self.is_name(index):
                raise ScopeError("only plain names and |, & or : work in a label expression")
            names.append(self.tokens[index].text)
            index += 1
            if not any(self.is_punct(index, sign) for sign in "|&:"):
                return tuple(names), index
            index += 1
            if self.is_punct(index, ":"):
                index += 1

    def check_labels(self, labels: tuple[str, ...]) -> None:
        for label in labels:
            if label in self.other_labels:
                raise ScopeError(f"label {label} belongs to the other scope")
            if label not in self.labels:
                raise ScopeError(f"label {label} is not in the catalogue")

    def check_types(self, types: tuple[str, ...]) -> None:
        for name in types:
            if name not in self.types:
                raise ScopeError(f"link type {name} is not in this scope")

    def node_at(self, index: int) -> _Node | None:
        """A node pattern at an open parenthesis, or None when the group has another shape."""
        close = self.closes[index]
        position = index + 1
        variable = None
        labels: tuple[str, ...] = ()
        map_at = where_at = None
        map_param = False
        if self.is_name(position) and self.word(position) != "WHERE":
            variable = self.tokens[position].text
            position += 1
        label_end = position - 1
        if self.is_punct(position, ":"):
            if self.is_punct(position + 1, ":"):
                return None
            labels, position = self.label_expression(position + 1)
            label_end = position - 1
        if self.is_punct(position, "{"):
            map_at = position
            position = self.closes[position] + 1
        elif position < close and self.tokens[position].kind == "param":
            map_param = True
            position += 1
        if self.word(position) == "WHERE":
            where_at = position
            position = close
        if position != close:
            return None
        return _Node(index, close, variable, labels, label_end, map_at, map_param, where_at)

    def link_at(self, index: int) -> _Link | None:
        """A link pattern at index, or None when the tokens there are arithmetic."""
        position = index + 1 if self.is_punct(index, "<") else index
        if not self.is_punct(position, "-"):
            return None
        position += 1
        if self.is_punct(position, "["):
            return self.typed_link(position)
        if self.is_punct(position, "-"):
            position += 1
            if self.is_punct(position, ">"):
                position += 1
            if self.is_punct(position, "("):
                raise ScopeError("a link with no type is refused")
        return None

    def typed_link(self, index: int) -> _Link:
        close = self.closes[index]
        position = index + 1
        map_at = where_at = None
        if self.is_name(position) and self.word(position) != "WHERE":
            position += 1
        if not self.is_punct(position, ":"):
            raise ScopeError("a link with no type is refused")
        names = []
        position += 1
        while True:
            if not self.is_name(position):
                raise ScopeError("only plain names and | work in a link type")
            names.append(self.tokens[position].text)
            position += 1
            if not self.is_punct(position, "|"):
                break
            position += 1
            if self.is_punct(position, ":"):
                position += 1
        if self.is_punct(position, "*"):
            position = self.variable_length(position + 1)
        spec_end = position - 1
        if self.is_punct(position, "{"):
            map_at = position
            position = self.closes[position] + 1
        elif position < close and self.tokens[position].kind == "param":
            raise ScopeError("a map parameter in a pattern is refused: it can hide world_id")
        if self.word(position) == "WHERE":
            where_at = position
            position = close
        if position != close:
            raise ScopeError("a link the scoper does not read")
        position = close + 1
        if not self.is_punct(position, "-"):
            raise ScopeError("a link the scoper does not read")
        position += 1
        if self.is_punct(position, ">"):
            position += 1
        return _Link(index, close, position, spec_end, map_at, where_at, tuple(names))

    def variable_length(self, index: int) -> int:
        if index < len(self.tokens) and self.tokens[index].kind == "number":
            if self.bound_value(index) == 0:
                raise ScopeError("a zero lower bound on a link is refused")
            index += 1
        if self.is_punct(index, ".") and self.is_punct(index + 1, "."):
            index += 2
            if index < len(self.tokens) and self.tokens[index].kind == "number":
                self.bound_value(index)
                index += 1
        return index

    def stamp(self, map_at: int | None, after_token: int) -> None:
        if map_at is None:
            self.edits.append((self.tokens[after_token].end, " {world_id: $world_id}"))
            return
        empty = self.closes[map_at] == map_at + 1
        insert = "world_id: $world_id" + ("" if empty else ", ")
        self.edits.append((self.tokens[map_at].end, insert))

    def refuse_stated_world(self, start: int, close: int) -> None:
        for position in range(start, close):
            token = self.tokens[position]
            if token.kind == "word" and token.text == WORLD_PARAM:
                raise ScopeError("a pattern that states world_id is refused")

    def finish_node(self, node: _Node, linked: bool, creates: bool) -> None:
        if node.map_param:
            raise ScopeError("a map parameter in a pattern is refused: it can hide world_id")
        self.check_labels(node.labels)
        if self.stamps:
            self.refuse_stated_world(node.start, node.close)
        if node.labels:
            if self.stamps:
                self.stamp(node.map_at, node.label_end)
        elif node.variable not in self.bound:
            if not linked:
                raise ScopeError("a node with no label and no link can reach every world")
            if creates:
                raise ScopeError("a new node needs a label")
        if node.variable:
            self.bound.add(node.variable)
        self.inner(node.map_at, node.where_at, node.close)

    def finish_link(self, link: _Link) -> None:
        self.check_types(link.types)
        if self.stamps:
            self.refuse_stated_world(link.open, link.close)
            self.stamp(link.map_at, link.spec_end)
        self.inner(link.map_at, link.where_at, link.close)

    def inner(self, map_at: int | None, where_at: int | None, close: int) -> None:
        if map_at is not None:
            self.map_items(map_at)
        if where_at is not None:
            self.expression(where_at + 1, close)

    def map_items(self, open_at: int) -> None:
        for start, finish in self.split(open_at + 1, self.closes[open_at]):
            if self.is_name(start) and self.is_punct(start + 1, ":"):
                self.expression(start + 2, finish)
            elif not self.is_punct(start, "."):
                self.expression(start, finish)

    def label_test(self, index: int) -> int:
        """At the colon of `n:Label` in an expression: check the names, skip `::` types."""
        if self.is_punct(index + 1, ":"):
            return index + 2
        labels, after = self.label_expression(index + 1)
        self.check_labels(labels)
        return after

    def expression(self, index: int, end: int) -> None:
        while index < end:
            token = self.tokens[index]
            if token.kind == "word" and token.text.upper() in _SHORTEST_FUNCTIONS and self.stamps:
                raise ScopeError("shortestPath has no scoped form: use the SHORTEST selector")
            if self.is_punct(index, "("):
                index = self.group(index)
            elif self.is_punct(index, "["):
                self.expression(index + 1, self.closes[index])
                index = self.closes[index] + 1
            elif self.is_punct(index, "{"):
                close = self.closes[index]
                if index and self.tokens[index - 1].text.upper() in _SUBQUERY_WORDS:
                    self.subquery(index + 1, close)
                else:
                    self.map_items(index)
                index = close + 1
            elif self.is_punct(index, ":"):
                index = self.label_test(index)
            elif self.is_punct(index, "-") or self.is_punct(index, "<"):
                if not self.subtracts_a_list(index) and self.link_at(index) is not None:
                    raise ScopeError("a link outside a pattern")
                index += 1
            else:
                index += 1

    def subtracts_a_list(self, index: int) -> bool:
        """`-` then a bracket with no type, in an expression: arithmetic on a list element such as
        `a.n - [1][0]`. A link bracket has a `-` after it, and that shape stays a link."""
        position = index + 1 if self.is_punct(index, "<") else index
        if not (self.is_punct(position, "-") and self.is_punct(position + 1, "[")):
            return False
        close = self.closes[position + 1]
        at = position + 2
        while at < close:
            if self.is_punct(at, ":"):
                return False
            at = self.closes[at] + 1 if at in self.closes else at + 1
        return not self.is_punct(close + 1, "-")

    def group(self, index: int) -> int:
        node = self.node_at(index)
        if node is None:
            close = self.closes[index]
            self.expression(index + 1, close)
            return close + 1
        if self.link_at(node.close + 1) is not None:
            # Variables of a pattern in an expression die with it. Keeping them would let a later
            # bare node pattern pass as bound while the server reads it as a fresh scan.
            outer = set(self.bound)
            after = self.path(index, len(self.tokens))[0]
            self.bound = outer
            return after
        # A lone node in an expression is a label test: the server has no such pattern there.
        self.check_labels(node.labels)
        self.inner(node.map_at, node.where_at, node.close)
        return node.close + 1

    def subquery(self, index: int, end: int) -> None:
        """EXISTS, COUNT and COLLECT bodies see the outer variables and give nothing back."""
        outer = self.bound
        self.bound = set(outer)
        if self.word(index) not in _CLAUSES:
            index = self.patterns(index, end)
        self.query(index, end, self.bound)
        self.bound = outer


def scope_cypher(cypher: str, mode: str) -> str:
    if mode not in MODES:
        raise ScopeError(f"no such mode: {mode!r}")
    return _Scoper(cypher, mode).scoped()


def check_world_id(world_id: str) -> str:
    if not isinstance(world_id, str) or _WORLD_ID.fullmatch(world_id) is None:
        raise ScopeError("a world id is a lowercase UUID, or t_<slice>_<uuid> for a test (S36)")
    return world_id


def refuse_world_param(params: object) -> None:
    """world_id comes from the layer. A nested key counts: `+=` of a row map could write it."""
    if isinstance(params, dict):
        if WORLD_PARAM in params:
            raise ScopeError("$world_id is bound by the layer, never by the caller")
        for value in params.values():
            refuse_world_param(value)
    elif isinstance(params, list | tuple):
        for value in params:
            refuse_world_param(value)
