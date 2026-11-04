"""The read side: every read runs in a read transaction, scoped by scope.py.

Read access mode is a safety rail and not access control: the driver manual gives no guarantee
for it, and Community has no roles. The real wall is the writer lint plus scope_cypher.
"""

import math
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from neo4j import Driver, Result, unit_of_work
from neo4j.graph import Node, Path, Relationship
from neo4j.spatial import Point
from neo4j.time import Date, DateTime, Duration, Time

from grove.store.catalogue import FULLTEXT_INDEXES
from grove.store.connection import DATABASE
from grove.store.errors import ScopeError, WorldLeak
from grove.store.primitives import PRIMITIVE_WORLD
from grove.store.scope import WORLD_PARAM, check_world_id, refuse_world_param, scope_cypher

CYPHER_PREFIX = "CYPHER 25 "

_FULLTEXT_NAMES = frozenset(index.name for index in FULLTEXT_INDEXES)
_TEXT_FIELDS = {
    index.name: tuple(prop for prop in index.props if prop != WORLD_PARAM)
    for index in FULLTEXT_INDEXES
}
# Lucene refuses a query past 1024 clauses, and each analyzed token costs one clause per field.
# Text past these caps is refused rather than cut: a caller must say what it left out. The length
# caps keep one fuzzy word from filling the shared server's heap.
MAX_SEARCH_TERMS = 64
MAX_SEARCH_TEXT = 1024
MAX_SEARCH_TERM = 64
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/<>=])')
_LUCENE_OPERATORS = frozenset({"AND", "OR", "NOT"})
# The analyzer drops these and all punctuation. A group with nothing left in it would vanish from
# the Lucene query and match every row of the world.
_ANALYZER_STOP_WORDS = frozenset(
    """a an and are as at be but by for if in into is it no not of on or such that the their then
    there these they this to was will with""".split()
)


def _world_of(element: Node | Relationship) -> object:
    return element.get(WORLD_PARAM)


def guard(value: Any, world_id: str) -> None:
    """Raise WorldLeak when a result holds a row of another world. Plain scalars pass unseen."""
    if isinstance(value, Node | Relationship):
        if _world_of(value) != world_id:
            raise WorldLeak(f"element {value.element_id} is outside the scope")
    elif isinstance(value, Path):
        for element in (*value.nodes, *value.relationships):
            guard(element, world_id)
    elif isinstance(value, dict):
        if WORLD_PARAM in value and value[WORLD_PARAM] != world_id:
            raise WorldLeak("a map result is outside the scope")
        for item in value.values():
            guard(item, world_id)
    elif isinstance(value, list | tuple):
        for item in value:
            guard(item, world_id)


def plain(value: Any) -> Any:
    # Duration and Point are tuples in the driver, so they come before the list branch.
    if isinstance(value, Duration):
        return value.iso_format()
    if isinstance(value, Point):
        point = {"srid": value.srid, "x": value[0], "y": value[1]}
        return point | ({"z": value[2]} if len(value) > 2 else {})
    if isinstance(value, Node | Relationship):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, Path):
        return {
            "nodes": [plain(node) for node in value.nodes],
            "relationships": [plain(link) for link in value.relationships],
        }
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if isinstance(value, DateTime | Date | Time):
        return value.to_native()
    return value


def check_timeout(timeout: object) -> float:
    """The driver reads 0 as no timeout at all, so only a positive finite number passes."""
    valid = isinstance(timeout, int | float) and not isinstance(timeout, bool)
    if not valid or not math.isfinite(timeout) or timeout <= 0:
        raise ScopeError("a timeout must be a positive number of seconds")
    return timeout


def collect(result: Result, world_id: str | None) -> list[dict]:
    """All rows of a result as dicts; world_id None means a platform read with no guard."""
    rows = []
    for record in result:
        row = dict(record.items())
        if world_id is not None:
            guard(row, world_id)
        rows.append(plain(row))
    return rows


def _run_read(
    driver: Driver, cypher: str, params: dict, world_id: str | None, timeout: float
) -> list[dict]:
    # tx.run refuses a Query object, so the timeout rides on the unit of work.
    @unit_of_work(timeout=timeout)
    def work(tx):
        return collect(tx.run(CYPHER_PREFIX + cypher, params), world_id)

    with driver.session(database=DATABASE) as session:
        return session.execute_read(work)


def _runs(raw: str) -> list[str]:
    """Runs of letters, marks and decimal digits: the characters the tokenizer keeps."""
    runs, current = [], ""
    for char in raw:
        category = unicodedata.category(char)
        if category[0] in "LM" or category == "Nd":
            current += char
        elif current:
            runs.append(current)
            current = ""
    return [*runs, current] if current else runs


def _folded(run: str) -> str:
    """A run as the analyzer's folding sees it: compatibility forms, accents and case folded."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", run))
    return "".join(char for char in decomposed if unicodedata.category(char)[0] != "M").casefold()


def lucene_terms(text: str, *, fuzzy: bool) -> list[str]:
    """The user's text as Lucene terms that cannot leave the group they are placed in."""
    if not isinstance(text, str):
        raise ScopeError("search text must be a string")
    if len(text) > MAX_SEARCH_TEXT:
        raise ScopeError(f"search text over {MAX_SEARCH_TEXT} characters")
    terms, tokens = [], 0
    for raw in text.split():
        if len(raw) > MAX_SEARCH_TERM:
            raise ScopeError(f"a search term over {MAX_SEARCH_TERM} characters")
        runs = _runs(raw)
        words = [folded for folded in map(_folded, runs) if folded]
        if not words or (not fuzzy and _ANALYZER_STOP_WORDS.issuperset(words)):
            continue
        # The analyzer splits on punctuation, so `a-b-c` costs three clauses per field.
        tokens += len(runs)
        word = raw.lower() if raw in _LUCENE_OPERATORS else raw
        terms.append(_LUCENE_SPECIAL.sub(r"\\\1", word) + ("~" if fuzzy else ""))
    if tokens > MAX_SEARCH_TERMS:
        raise ScopeError(f"more than {MAX_SEARCH_TERMS} search terms")
    return terms


def _search_query(
    world_id: str, index: str, text: str, *, limit: int, fuzzy: bool, post_filter: bool = True
) -> tuple[str, dict] | None:
    if index not in _FULLTEXT_NAMES:
        raise ScopeError(f"no such full-text index: {index!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ScopeError("limit must be a positive integer")
    if world_id == PRIMITIVE_WORLD:
        raise ScopeError("the primitive rows have no world term to search on")
    terms = lucene_terms(text, fuzzy=fuzzy)
    if not terms:
        return None
    group = " ".join(f"{field}:{term}" for field in _TEXT_FIELDS[index] for term in terms)
    guard_clause = "WHERE n.world_id = $world_id " if post_filter else ""
    return (
        f"MATCH (n) SEARCH n IN (FULLTEXT INDEX {index} FOR $q LIMIT $limit) SCORE AS score "
        f"{guard_clause}RETURN n, labels(n) AS labels, score",
        {"q": f'+world_id:"{world_id}" +({group})', "limit": limit, WORLD_PARAM: world_id},
    )


async def transaction_search(
    run: Callable[[str, dict], Awaitable[list[dict]]],
    world_id: str,
    index: str,
    text: str,
    *,
    fuzzy: bool = False,
) -> list[dict]:
    """Complete membership in one native transaction; deliberately discard shared-corpus scores.

    SEARCH requires LIMIT. The exact world inventory supplies an upper bound for every possible
    match, not a page size. The outer generation bracket rejects an intervening target-world write.
    Only the fixed catalogue supplies identifiers; run must guard and detach every returned row.
    """
    check_world_id(world_id)
    # Do not silently filter a foreign Lucene hit after LIMIT. An exceptional analyzer collision
    # must reach the native result guard and refuse, not hide an omitted target-world candidate.
    prepared = _search_query(world_id, index, text, limit=1, fuzzy=fuzzy, post_filter=False)
    if prepared is None:
        return []
    definition = next(entry for entry in FULLTEXT_INDEXES if entry.name == index)
    labels = " OR ".join(f"n:{label}" for label in definition.labels)
    inventory = await run(
        f"MATCH (n) WHERE n.world_id = $world_id AND ({labels}) RETURN count(n) AS total",
        {WORLD_PARAM: world_id},
    )
    total = inventory[0]["total"] if len(inventory) == 1 else None
    if type(total) is not int or not 0 <= total <= 2_147_483_647:
        raise ScopeError("the complete search inventory is unavailable")
    if total == 0:
        return []
    cypher, params = prepared
    params["limit"] = total
    rows = await run(cypher, params)
    candidates = [{"node": row["n"], "labels": sorted(row["labels"])} for row in rows]
    return sorted(candidates, key=lambda row: row["node"]["id"])


class WorldQuery:
    def __init__(self, driver: Driver, world_id: str, *, timeout: float = 30.0) -> None:
        self._driver = driver
        self._world_id = check_world_id(world_id)
        self._timeout = check_timeout(timeout)

    @classmethod
    def primitives(cls, driver: Driver, *, timeout: float = 30.0) -> "WorldQuery":
        """Store-wide rows only. The sentinel skips the format check on purpose: the constructor
        must refuse '' from any other caller."""
        query = cls.__new__(cls)
        query._driver = driver
        query._world_id = PRIMITIVE_WORLD
        query._timeout = check_timeout(timeout)
        return query

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        scoped = scope_cypher(cypher, "read")
        bound = {**params, WORLD_PARAM: self._world_id}
        return _run_read(self._driver, scoped, bound, self._world_id, self._timeout)

    def search(self, index: str, text: str, *, limit: int, fuzzy: bool = False) -> list[dict]:
        prepared = _search_query(self._world_id, index, text, limit=limit, fuzzy=fuzzy)
        if prepared is None:
            return []
        # SEARCH filters after LIMIT, so only a filter inside the Lucene query gives a true top k
        # (S17). The world id form of S36 makes the phrase exact in practice; the WHERE stays as
        # the exact check.
        rows = _run_read(self._driver, *prepared, self._world_id, self._timeout)
        return [
            {"node": row["n"], "labels": sorted(row["labels"]), "score": row["score"]}
            for row in rows
        ]


class PlatformQuery:
    def __init__(self, driver: Driver, *, timeout: float = 30.0) -> None:
        self._driver = driver
        self._timeout = check_timeout(timeout)

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        scoped = scope_cypher(cypher, "platform")
        return _run_read(self._driver, scoped, params, None, self._timeout)
