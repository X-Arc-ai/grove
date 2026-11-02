"""Kinds, meanings and property names by id, so the views show names and never registry ids.

A world's rows and the primitive rows (world_id '') are read apart: the scoper gives each query one
world. Code finds a row by its id; the name is what the person reads.
"""

import json
from dataclasses import dataclass, field

from grove.store.query import WorldQuery

UNKNOWN = "unknown"
DEFAULT_CARDINALITY = "many"

_KINDS = "MATCH (k:Kind) RETURN k.id AS id, k.name AS name, k.status_vocabulary AS vocabulary"
_MEANINGS = "MATCH (m:Meaning) RETURN m.id AS id, m.name AS name, m.cardinality AS cardinality"
_PROPERTY_NAMES = "MATCH (p:PropertyName) RETURN p.kind AS kind, p.name AS name"


@dataclass(frozen=True)
class Kind:
    id: str
    name: str
    status_vocab: tuple[str, ...]
    primitive: bool


@dataclass(frozen=True)
class Meaning:
    id: str
    name: str
    cardinality: str


@dataclass(frozen=True)
class Registry:
    kinds: dict[str, Kind] = field(default_factory=dict)
    meanings: dict[str, Meaning] = field(default_factory=dict)
    property_names: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def kind_name(self, kind_id: object) -> str:
        kind = self.kinds.get(kind_id) if isinstance(kind_id, str) else None
        return kind.name if kind else UNKNOWN

    def meaning(self, meaning_id: object) -> tuple[str, str]:
        """(name, cardinality) of a meaning id."""
        meaning = self.meanings.get(meaning_id) if isinstance(meaning_id, str) else None
        if meaning is None:
            return UNKNOWN, DEFAULT_CARDINALITY
        return meaning.name, meaning.cardinality

    def optional_props(self, kind_id: object) -> list[str]:
        return list(self.property_names.get(kind_id, ())) if isinstance(kind_id, str) else []


def status_words(vocabulary: object) -> tuple[str, ...]:
    """The status words of a kind's status_vocabulary, a JSON list of {status, role} (S31)."""
    if not isinstance(vocabulary, str):
        return ()
    try:
        entries = json.loads(vocabulary)
    except ValueError:
        return ()
    if not isinstance(entries, list):
        return ()
    words = []
    for entry in entries:
        word = entry.get("status") if isinstance(entry, dict) else entry
        if isinstance(word, str) and word:
            words.append(word)
    return tuple(words)


def _kinds(query: WorldQuery, *, primitive: bool) -> dict[str, Kind]:
    found = {}
    for row in query.read(_KINDS):
        if isinstance(row["id"], str) and isinstance(row["name"], str) and row["name"]:
            found[row["id"]] = Kind(
                row["id"], row["name"], status_words(row["vocabulary"]), primitive
            )
    return found


def _meanings(query: WorldQuery) -> dict[str, Meaning]:
    found = {}
    for row in query.read(_MEANINGS):
        if isinstance(row["id"], str) and isinstance(row["name"], str) and row["name"]:
            cardinality = row["cardinality"]
            found[row["id"]] = Meaning(
                row["id"],
                row["name"],
                cardinality if isinstance(cardinality, str) else DEFAULT_CARDINALITY,
            )
    return found


def _property_names(query: WorldQuery) -> dict[str, tuple[str, ...]]:
    by_kind: dict[str, set[str]] = {}
    for row in query.read(_PROPERTY_NAMES):
        if isinstance(row["kind"], str) and isinstance(row["name"], str) and row["name"]:
            by_kind.setdefault(row["kind"], set()).add(row["name"])
    return {kind: tuple(sorted(names)) for kind, names in by_kind.items()}


def read_registry(world: WorldQuery, primitives: WorldQuery) -> Registry:
    """The world's rows win over a primitive row only if one ever shared an id, which none does."""
    return Registry(
        kinds=_kinds(primitives, primitive=True) | _kinds(world, primitive=False),
        meanings=_meanings(primitives) | _meanings(world),
        property_names=_property_names(world),
    )
