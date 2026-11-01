"""The read side: every read runs in a read transaction, scoped by scope.py.

Read access mode is a safety rail and not access control: the driver manual gives no guarantee
for it, and Community has no roles. The real wall is the writer lint plus scope_cypher.
"""

import re
from typing import Any

from neo4j import Driver, Result, unit_of_work
from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime

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
# Lucene refuses a query past 1024 clauses, and each term costs one clause per field. Text past
# this cap is refused rather than cut: a caller must say what it left out.
MAX_SEARCH_TERMS = 64
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/<>=])')
_LUCENE_OPERATORS = frozenset({"AND", "OR", "NOT"})
_TEXT_RUN = re.compile(r"[^\W_]+")
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
    if isinstance(value, DateTime | Date):
        return value.to_native()
    return value


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


def lucene_terms(text: str, *, fuzzy: bool) -> list[str]:
    """The user's text as Lucene terms that cannot leave the group they are placed in."""
    terms = []
    for raw in text.split():
        runs = [run.casefold() for run in _TEXT_RUN.findall(raw)]
        if not runs or (not fuzzy and _ANALYZER_STOP_WORDS.issuperset(runs)):
            continue
        word = raw.lower() if raw in _LUCENE_OPERATORS else raw
        terms.append(_LUCENE_SPECIAL.sub(r"\\\1", word) + ("~" if fuzzy else ""))
    return terms


class WorldQuery:
    def __init__(self, driver: Driver, world_id: str, *, timeout: float = 30.0) -> None:
        self._driver = driver
        self._world_id = check_world_id(world_id)
        self._timeout = timeout

    @classmethod
    def primitives(cls, driver: Driver, *, timeout: float = 30.0) -> "WorldQuery":
        """Store-wide rows only. The sentinel skips the format check on purpose: the constructor
        must refuse '' from any other caller."""
        query = cls.__new__(cls)
        query._driver = driver
        query._world_id = PRIMITIVE_WORLD
        query._timeout = timeout
        return query

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        refuse_world_param(params)
        scoped = scope_cypher(cypher, "read")
        bound = {**params, WORLD_PARAM: self._world_id}
        return _run_read(self._driver, scoped, bound, self._world_id, self._timeout)

    def search(self, index: str, text: str, *, limit: int, fuzzy: bool = False) -> list[dict]:
        if index not in _FULLTEXT_NAMES:
            raise ScopeError(f"no such full-text index: {index!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ScopeError("limit must be a positive integer")
        if self._world_id == PRIMITIVE_WORLD:
            raise ScopeError("the primitive rows have no world term to search on")
        terms = lucene_terms(text, fuzzy=fuzzy)
        if not terms:
            return []
        if len(terms) > MAX_SEARCH_TERMS:
            raise ScopeError(f"more than {MAX_SEARCH_TERMS} search terms")
        # world_id is an indexed field, so a bare term would also match it: every term names the
        # text fields.
        group = " ".join(f"{field}:{term}" for field in _TEXT_FIELDS[index] for term in terms)
        # SEARCH filters after LIMIT, so only a filter inside the Lucene query gives a true top k
        # (S17). The analyzer splits a world id on hyphens, so the phrase alone is not an exact
        # match: the WHERE stays as the exact check.
        rows = _run_read(
            self._driver,
            f"MATCH (n) SEARCH n IN (FULLTEXT INDEX {index} FOR $q LIMIT $limit) SCORE AS score "
            "WHERE n.world_id = $world_id RETURN n, labels(n) AS labels, score",
            {
                "q": f'+world_id:"{self._world_id}" +({group})',
                "limit": limit,
                WORLD_PARAM: self._world_id,
            },
            self._world_id,
            self._timeout,
        )
        return [
            {"node": row["n"], "labels": sorted(row["labels"]), "score": row["score"]}
            for row in rows
        ]


class PlatformQuery:
    def __init__(self, driver: Driver, *, timeout: float = 30.0) -> None:
        self._driver = driver
        self._timeout = timeout

    def read(self, cypher: str, /, **params: Any) -> list[dict]:
        scoped = scope_cypher(cypher, "platform")
        return _run_read(self._driver, scoped, params, None, self._timeout)
